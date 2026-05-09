# R2 ChecksumSHA256 Binding — Rollout Runbook

`R2_CHECKSUM_BINDING_ENABLED` フラグを `false → true` に切り替える際の事前確認・実行・ロールバック手順 (Issue #556 Phase 1.5, PR #574 / Issue #576)。

このフラグは R2 server-side body integrity gate を活性化する。SDK 側が `x-amz-checksum-sha256` リクエストヘッダーを送るよう更新された **後** に、Self-hosted operator が手動で flip する設計。古い SDK が混在するうちに flip すると、すべての upload が `HTTP 403 SignatureDoesNotMatch` で失敗する。

---

## 1. 何のフラグか / なぜ必要か

PR #574 (#556) で `R2Storage.generate_presigned_put` に S3 Object Integrity (`ChecksumSHA256` param + `x-amz-checksum-sha256` SigV4 signing) を配線した。これは:

- **目的**: per-workspace dedup-poisoning の塞ぎ込み (#485 Phase 1 で Copilot レビューが 4 回連続で flag していた gap)。
- **動作**: R2 が受信 body の sha256 を declared 値と照合、ミスマッチなら **bytes が persist される前に HTTP 400 BadDigest** で reject。
- **現在の既定値**: `R2_CHECKSUM_BINDING_ENABLED=false` (`backend/src/config/settings.py:r2_checksum_binding_enabled`)。flag を有効化するまで Phase 1 と同じ振る舞い (no integrity check)。

> **なぜ flag が要るか**: integrity check は upload 側 (SDK / web) が `x-amz-checksum-sha256` ヘッダーを正しい値で送ることが前提。古い SDK / 古いブラウザ実装が混じる過渡期に flip すると、整合しないクライアントが全部 `HTTP 403 SignatureDoesNotMatch` で落ちる。SDK 配布 → 採用 → flag flip の順序を守るための gate。

---

## 2. Pre-condition / 現状

このバージョンで本 runbook を実行する前提:

| 項目 | 想定値 |
|---|---|
| Backend release | PR #574 以降がデプロイ済み (`alembic` head が `e07_556_sha256_lowercase_index` 以降) |
| `R2_CHECKSUM_BINDING_ENABLED` | `false` (default) |
| `kagura-memory-python-sdk` | `x-amz-checksum-sha256` ヘッダーを送る release が **rollout 済み** |
| Production observability | structured logs (structlog) が読める状態 |
| `file_objects` row count | 0 でない場合のみ意味がある (0 のうちは flip しても影響ゼロ) |

> ⚠️ **2026-05-09 時点の memory.kagura-ai.com の状態**: `file_objects=0` (Production deploy savepoint 参照)。R2 Storage はライブだが利用ゼロ。SDK FilesClient release もまだ。flip しても直接影響を受けるユーザーは現状ゼロ。**この状況で flip を急ぐ理由はない** — SDK FilesClient の release を待ってから本 runbook を回すのが本筋。

---

## 3. Rollout シーケンス (CHANGELOG #556 拡張)

CHANGELOG `## Unreleased - 2026-05-09 R2 sha256 binding` の 4-step を operator 向けに展開する:

### Step 1 — Backend release deploy (`R2_CHECKSUM_BINDING_ENABLED=false` のまま)

PR #574 を main にマージ + production deploy。フラグは default `false` のまま、Phase 1 と同等の振る舞い。

```bash
# Self-hosted operator 想定の deploy 手順例 (memory-cloud Docker compose)
git pull origin main
make rebuild                  # backend image rebuild
make migrate                  # alembic upgrade (e07_556_sha256_lowercase_index)
# deploy 後の確認
curl -fsS https://your-host/health | jq .
```

この時点で behavior change はない。`file_objects` 行が増えても `ChecksumSHA256` は params に含まれない (r2.py:136-137 の if 分岐)。

### Step 2 — SDK release

`kagura-memory-python-sdk` を `x-amz-checksum-sha256` ヘッダーを送る release に更新。

> 📌 **2026-05-09 時点で SDK 側 release はまだ存在しない**。`backend/src/config/settings.py:r2_checksum_binding_enabled` の docstring が言及する `kagura-memory-python-sdk >= 0.4.0` は **placeholder** (現行 SDK は v0.13.0 系で、`FilesClient` 自体が未実装 — memory-cloud#556 land 待ちで起票見送りされている)。SDK FilesClient の actual release tag が確定したら、この runbook と settings.py docstring を同時に書き換える。

このセクションは **TODO**: SDK FilesClient の最初の release が出た時点で、具体的な version (例: `kagura-memory-python-sdk >= v0.X.Y`) を確定させ、本 runbook の Step 4 pre-flip check / Step 5 verification の SQL/log 検索条件をその threshold で更新する。

### Step 3 — SDK 採用待ち

SDK release 後、tenant / 利用者側の更新を待つ。Self-hosted の場合は配布チャンネル (PyPI announce / Slack / mailing list 等) で告知。

判定条件: 「production の `/api/v1/files/reserve` を叩いてくる active client のうち、新しい SDK が **十分な比率** に達した」。

「十分」の閾値は運用判断:
- Conservative: 100% (どの古い SDK が叩いても 403 にしたくない場合)
- Pragmatic: 95% (旧 SDK ユーザーには「アップグレードしてください」を出して fail させる)
- Aggressive: もっと低い比率でも可 (ユーザーベース小、サポート連絡経路がある場合)

### Step 4 — Flag flip (`R2_CHECKSUM_BINDING_ENABLED=true`)

Pre-flip safety check (§4) を**必ず**通してから実行。

具体的な手順は §5 を参照。`docs/deployment.md` の blue/green スイッチ手順 (`/opt/kagura-memory/active-color` 切替) と整合させる。

---

## 4. Pre-flip Safety Check (manual procedure)

flag を `true` にする前に、production traffic の SDK 分布が **新 SDK 100%** (or 運用判断で許容できる比率) になっていることを operator が手で確認する。

### 4.1 まず現状把握: file_objects 行数

```sql
-- production DB に admin で接続
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS last_7d
FROM file_objects;
```

`total = 0` のうちは flip しても直接影響を受けるユーザーがいない。本 runbook を厳密に回す必要は薄い。`last_30d > 0` になってから本格的に注意する。

### 4.2 SDK 分布の確認 (User-Agent ベース)

> ⚠️ **現状の制約**: `POST /api/v1/files/reserve` (presigned PUT URL を発行する endpoint) は **User-Agent を audit_logs に保存していない** (`backend/src/api/routes/files.py:146` を参照)。同 endpoint への inbound User-Agent は backend の structured log (structlog) には残るが、検索可能な集計テーブルには入らない。
>
> したがって現状の確認手段は「production logs の grep」になる。将来 audit_logs に capture する追加ワークが望ましい (§9 Future Work 参照)。

```bash
# Self-hosted operator の log inspection 例 (production blue/green setup)
ACTIVE=$(cat /opt/kagura-memory/active-color)        # blue or green
# 1. backend container の structlog 出力を直近 7 日分取得
docker logs "kagura-api-$ACTIVE" --since 168h 2>&1 \
  | grep -E '"path": "/api/v1/files/reserve"' \
  | jq -r '.user_agent // "(missing)"' \
  | sort | uniq -c | sort -rn

# 期待出力例:
#   142 kagura-memory-python-sdk/0.4.1 python/3.12
#     8 kagura-memory-python-sdk/0.3.0 python/3.11   ← 古い! flip すると 403
#     3 Mozilla/5.0 (Macintosh; Intel ...)
```

`kagura-memory-python-sdk/<old version>` が残っているうちは **flip しない**。

> **Web app からの直 upload を SDK と区別**: `Mozilla/...` 系は `frontend/` 経由のブラウザ upload。フロントエンドは PR #574 と同時に新しいヘッダーを送るよう更新されているため、ブラウザは閾値判定の対象外 (常に新)。 SDK の `kagura-memory-python-sdk/<version>` 行のみ確認すれば良い。

### 4.3 補足: `r2_checksum_binding_enabled` の現値を確認

flip 前にも flip 後にも、settings に何が入っているか直接確認:

```bash
# Backend container 内で
docker exec "kagura-api-$(cat /opt/kagura-memory/active-color)" python -c \
  "from config.settings import settings; print(settings.r2_checksum_binding_enabled)"
# 期待出力 (flip 前): False
```

---

## 5. Flip 操作 (Step 4 詳細)

memory-cloud は blue/green デプロイ (`docs/deployment.md` 参照)。inactive 側で flag だけ更新 → warm-up → switch という流れ。具体的な service 名と active-color ファイルは `docs/deployment.md` に従う。

```bash
# 例: 現在 active が blue、inactive が green
ACTIVE=$(cat /opt/kagura-memory/active-color)        # blue or green
INACTIVE=$( [ "$ACTIVE" = "blue" ] && echo "green" || echo "blue" )

# 1. .env.prod の R2_CHECKSUM_BINDING_ENABLED を true に編集
#    (sed なり手で書き換え。`>>` で append すると重複するので注意)

# 2. inactive 側コンテナを force-recreate して env を反映
docker compose --env-file .env.prod up -d --force-recreate "kagura-api-$INACTIVE"

# 3. inactive 側の health 確認 (port は docs/deployment.md と整合させる)
docker exec "kagura-api-$INACTIVE" python -c \
  "from config.settings import settings; print(settings.r2_checksum_binding_enabled)"
# 期待出力: True

# 4. blue/green switch (active-color ファイルの更新と nginx reload) — docs/deployment.md
```

---

## 6. Post-flip verification

flip 直後 5 分で確認:

### 6.1 設定が反映されているか

```bash
docker exec "kagura-api-$(cat /opt/kagura-memory/active-color)" python -c \
  "from config.settings import settings; print(settings.r2_checksum_binding_enabled)"
# 期待出力: True
```

### 6.2 直近 upload が成功しているか

```sql
-- 直近 5 分の file_objects の状態遷移を確認
SELECT status, COUNT(*) AS n,
       MAX(created_at) AS last_seen
FROM file_objects
WHERE created_at >= NOW() - INTERVAL '5 minutes'
GROUP BY status
ORDER BY n DESC;

-- 期待: status='uploaded' が積み上がる。
--       status='reserved' は upload 進行中 (TTL 内)、もしくは orphan sweep 待ち。
--       status='failed' が急増したら 403 / 400 が出ている可能性 — §6.3 で確認。
```

`file_objects.status` の遷移は `reserved → uploaded` (成功) もしくは `reserved → failed` (TTL 経過 + orphan sweep) のいずれか (`backend/src/models/file_objects.py` の CHECK constraint より)。

### 6.3 backend log で 403 が出ていないか

```bash
ACTIVE=$(cat /opt/kagura-memory/active-color)
docker logs "kagura-api-$ACTIVE" --since 5m 2>&1 \
  | grep -E '403|SignatureDoesNotMatch|BadDigest' \
  | head
# 期待: 出力なし、or 想定内 (古い SDK ユーザーへのエラー、サポート連絡で誘導済)
```

403 が **想定外** の頻度で出る場合は §7 Rollback。

---

## 7. Rollback procedure

`HTTP 403 SignatureDoesNotMatch` が想定外に出ている場合 (= 古い SDK が想定より残っていた) のロールバック。

```bash
# Step 5 と逆順
# 1. .env.prod の R2_CHECKSUM_BINDING_ENABLED を false に戻す
# 2. inactive 側 force-recreate
docker compose --env-file .env.prod up -d --force-recreate kagura-api-<inactive-color>
# 3. switch を戻す (active を元の color に)
```

> 💡 **古い SDK 側で起きるエラー**: `aiohttp` / `httpx` の PUT response が `HTTP 403 SignatureDoesNotMatch` を返す。SDK 側にとっては「prensigned URL が無効」に見えるが、原因は **headers に `x-amz-checksum-sha256` が無い**。SDK のアップグレードで解消する。Rollback でサーバ側の binding を一旦切り戻し、SDK 側の rollout を待つ。

> ⚠️ **Rollback で持続的な影響はない**: flag は backend 内部の Params dict 配線にしか効かない。flip 期間中に「成功して `status=uploaded` になった file_objects」はそのまま残る (rollback で破壊されない)。flip 期間中に **失敗して `status=reserved` のまま TTL 切れになった行** は orphan sweep で `status=failed` に遷移する (`idx_file_objects_reserved_expires`)。

---

## 8. Troubleshooting

### Q: flip 後すぐ 403 が出始めた

- A: 古い SDK が残っていた可能性が高い。§7 Rollback。次回 flip 時は §4.2 の SDK 分布確認をより長期間 (最低 7 日) でやる。

### Q: 一部 tenant だけ 403、それ以外は OK

- A: 該当 tenant の SDK アップグレードを促すサポート対応。flip 自体は維持してよい。tenant ごとに flag を分けたい要件が生じれば、`r2_checksum_binding_enabled` を per-workspace 設定に拡張する別 issue が必要。

### Q: CHANGELOG / settings docstring が言う `kagura-memory-python-sdk >= 0.4.0` の正体は?

- A: PR #574 当時の placeholder。**実際の SDK FilesClient release は memory-cloud#556 land 待ちでまだ起票されていない** (memory `c41708e3` 参照)。SDK FilesClient が ship したらその version で確定し、本 runbook + `backend/src/config/settings.py` docstring を**同時に**更新する必要がある。

### Q: `HTTP 400 BadDigest` (403 ではなく) が出た

- A: これは flag が**正しく動いている**証拠。SDK / client が送った `x-amz-checksum-sha256` の値と、実際に upload した body の sha256 が不一致。つまり「dedup poisoning を防いだ」ケース。SDK 側の bug もしくはクライアント側の改ざん試行。SDK 側 bug の可能性が高ければ調査して fix。

---

## 9. Future Work (現状の制約 → 改善候補)

§4.2 の現状制約に対する将来改善:

1. **`/api/v1/files/reserve` の inbound User-Agent を audit_logs に capture**
   - 現状は backend structlog にしか残らないため、運用上の集計が grep 頼り
   - `backend/src/api/routes/files.py:146` の `reserve_upload` ハンドラで `request.headers.get("user-agent")` を取得し、audit_logs に行を書く
   - これにより §4.2 の SDK 分布確認が SQL ワンライナーで済む

2. **自動 probe (Issue #576 が当初要求していた scope)**
   - audit_logs に user_agent が capture されれば、`make doctor-r2-checksum-readiness` のような Makefile target で SDK 分布 + 推奨 threshold を自動判定できる
   - SDK FilesClient ship 後、threshold が確定してから着手するのが合理的

3. **frontend (browser upload) の `x-amz-checksum-sha256` 送信確認テスト**
   - 既に PR #574 と同時更新されているはずだが、E2E テストが薄い場合は別途 follow-up

これらは Issue #576 の元 scope に含まれていたが、SDK FilesClient release ship 待ちのため本 runbook では deferred。SDK ship 後に必要に応じて起票する。

---

## Cross-references

- **Parent issue**: #556 (sha256 body binding design)
- **Parent PR**: #574 (`feat(storage): bind sha256 to presigned PUT via ChecksumSHA256`)
- **CHANGELOG entry**: `## Unreleased - 2026-05-09 R2 sha256 binding` (this section is the canonical 4-step source)
- **Settings**: `backend/src/config/settings.py:r2_checksum_binding_enabled`
- **Code under flag**: `backend/src/storage/r2.py:136-137` (`if self._enable_checksum_binding: params["ChecksumSHA256"] = ...`)
- **Test (mock-based)**: `backend/tests/storage/test_r2.py::TestGeneratePresignedPutChecksumBinding` (#575)
- **Test (live, gated)**: `backend/tests/integration/test_r2_live.py::TestGeneratePresignedPut` (D1/D2/D3 cases)
- **Related runbooks**: `docs/ops/erasure-runbook.md`, `docs/ops/embedding-threshold-measurement.md`, `docs/ops/resource-indexer-backfill.md`
- **Deployment guide**: `docs/deployment.md` (blue/green flow)

---

> 📝 **このドキュメントの保守**: SDK FilesClient release が出た時点で、§3 Step 2 / §4.2 / §8 Q3 / §9 #1, #2 を確定値に更新すること。本 runbook と `backend/src/config/settings.py:r2_checksum_binding_enabled` docstring の **両方** に SDK version threshold が現れる — 同時更新が必要。
