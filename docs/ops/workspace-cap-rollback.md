# Workspace Cap Enforcement — Rollback Runbook

`ENFORCE_WORKSPACE_CAP=true` の本番反映後、正当な workspace 作成が `Workspace limit reached` で reject される事象（false-positive denial）が発生した場合の **即時 rollback + 原因調査 + bonus 調整** 手順 (Issue #674 Phase 1 sub-C / #677)。

このフラグは workspace 作成の **owned-cap** を gate する。flip 前の log-only mode（`enforce=False`）では cap 超過を warn log のみで通すので false-positive denial は発生しない。flip 後は cap が hard gate になり、誤った `workspace_slot_bonus` 設定や grandfather migration 漏れで正当ユーザが reject されるリスクが現れる。本 runbook はその時の即応手順。

---

## 1. このフラグは何か / なぜ rollback が必要か

`backend/src/services/quota_service.py` の `check_workspace_creation_allowed` は **`cap = 1 + users.workspace_slot_bonus`** を gate する。フラグは:

- **目的**: TOCTOU race を `pg_advisory_xact_lock` で塞いだ上で（#677）、cap 超過を実際に deny する切替（#674 sub-C の最終段）。
- **動作**: `False` のとき warn log のみで通す、`True` のとき deny + 5xx ではなく `QuotaExceededError` で 4xx 応答。
- **default**: `False`（`backend/src/config/settings.py:enforce_workspace_cap`）。本番 flip は別 ops issue で 7-day log-only window 完走後に実施。

flip 後に false-positive denial が出る代表的シナリオ:

| シナリオ | 兆候 |
|---|---|
| Grandfather migration 漏れ user | `current_owned_workspaces >= max_owned_workspaces` でも本人は何も増やしていない |
| Admin がうっかり bonus を下げた | 直近の admin audit log に `workspace_slot_bonus -= N` 操作 |
| Stripe webhook (Phase 2) の double decrement | Phase 1 中は無関係。Phase 2 以降のみ |
| advisory lock の lock_timeout 連発 | `workspace_create_lock_failed` warn が大量、`reason=lock_timeout` |

---

## 2. Pre-condition / 前提

| 項目 | 想定値 |
|---|---|
| Backend release | #677 merge 済み (`QuotaService._acquire_workspace_create_lock` 存在) |
| Migration head | `e15_675_workspace_slot_bonus` 以降 |
| `ENFORCE_WORKSPACE_CAP` | `true`（本 runbook はこの状態で正当ユーザの denial が起きた時に使う） |
| Sub-B (#676 admin UI) | `workspace_slot_bonus` inc/dec の admin UI が merge 済みなら §4 は admin UI 経由、未 merge なら §4 の SQL fallback を使用 |
| 本番 DB アクセス | 既存 ops 手順（`psql` 経由）が available |

---

## 3. Procedure / 即時 rollback

### Step 1. フラグを `false` に戻す（deploy 不要）

memory.kagura-ai.com など single-server 構成では:

```bash
# 1. .env.prod を編集
sudo -e /opt/kagura-memory/.env.prod
#    ENFORCE_WORKSPACE_CAP=true  →  ENFORCE_WORKSPACE_CAP=false

# 2. uvicorn worker を reload
sudo systemctl reload kagura-api-green   # active container (blue-green の active 側)
```

> ⚠️ **重要**: `get_settings()` は `_settings` module-level singleton をキャッシュする (`backend/src/config/settings.py:502-519`)。`uvicorn reload` で **worker process が再起動して `_settings` が再初期化される必要がある**。`systemctl reload` が SIGHUP で workers を replace するか確認。replace されない場合は `systemctl restart`。

### Step 2. `/health` 200 確認

```bash
curl -fsS https://memory.kagura-ai.com/health
# {"status":"ok",...}
```

### Step 3. 設定値の実効確認

worker process 内で `enforce_workspace_cap` が `False` になったことを確認:

```bash
# admin endpoint がある場合
curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://memory.kagura-ai.com/admin/config 2>/dev/null | jq '.enforce_workspace_cap'
# false

# 無ければログから確認: 新規 workspace_creation_denied が出ても deny されないこと
# 直後の workspace_creation_denied 行が enforced=false を含むはず
journalctl -u kagura-api-green --since '1 minute ago' | grep workspace_creation_denied | tail -3
```

### Step 4. false-positive denial の沈静化を観測

reload 後 5 分間、`workspace_creation_denied enforced=true` の発生レートが減衰することを確認:

```bash
journalctl -u kagura-api-green --since '5 minutes ago' \
  | grep -c 'workspace_creation_denied.*enforced=true'
# 期待: reload 前のレートと比べて急減（reload 直後にバッファに残った分のみ）
```

---

## 4. Bonus 調整 / SQL fallback

> 📝 **TODO (sub-B #676 merge 後)**: このセクションは admin UI 手順に置き換える。`/admin/users/{user_id}` の `workspace_slot_bonus` inc/dec ボタンが UI で expose され、audit log も自動で記録されるようになるため、SQL を直接打つ運用は廃止。

sub-B (#676 admin UI) が未 merge / 未 deploy の段階で、影響を受けた user の bonus を手動で増やす手順。**oncall が深夜に対応する想定** で、誤適用を防ぐ条件付き SQL を載せる。

### Step 4.1. 影響 user の特定

log から `workspace_creation_denied enforced=true` を抽出して `user_id` を取り出す:

```bash
journalctl -u kagura-api-green --since '15 minutes ago' \
  | grep 'workspace_creation_denied' \
  | grep 'enforced=true' \
  | grep -oE 'user_id=[a-zA-Z0-9_-]+' \
  | sort -u
# user_id=u_abc12345
# user_id=u_def67890
```

ユーザに連絡を取り、**意図的に over-cap になっているのか、grandfather 漏れなのか** を確認する。意図的ならそのまま deny を維持（Step 1 の rollback は不要だった可能性、再確認）。漏れなら Step 4.2 へ。

### Step 4.2. 現在の bonus 値を確認

```sql
-- 影響 user の現在の slot 情報
SELECT
    u.id,
    u.email,
    u.workspace_slot_bonus,
    COUNT(w.id) FILTER (WHERE w.deleted_at IS NULL) AS owned_active,
    COUNT(w.id) FILTER (WHERE w.deleted_at IS NOT NULL) AS owned_soft_deleted
  FROM users u
  LEFT JOIN workspaces w ON w.owner_user_id = u.user_id
 WHERE u.user_id = '<user_id>'
 GROUP BY u.id, u.email, u.workspace_slot_bonus;
```

期待する `workspace_slot_bonus` は `MAX(0, owned_active - 1)`（grandfather migration の式と同じ）。実際の値と乖離していれば調整対象。

### Step 4.3. 条件付き UPDATE で bonus を増やす

```sql
-- 期待値 N を確定したうえで、現在値が N 未満の場合のみ更新
UPDATE users
   SET workspace_slot_bonus = :expected_bonus
 WHERE user_id = '<user_id>'
   AND workspace_slot_bonus < :expected_bonus;

-- 更新行数の確認
-- 1 row affected: 適用済み
-- 0 rows affected: 既に >= 期待値（追加調整不要、Step 4.2 を再確認）
```

> 🛡️ **条件付き UPDATE を使う理由**: 単純な `SET workspace_slot_bonus = N` だと、誰かが既に高い値を入れていた場合に **意図せず下げてしまう**。`workspace_slot_bonus < N` の WHERE 句で「下げない」を保証する。降格が必要な場面は本 runbook の対象外（別 admin 操作）。

### Step 4.4. 検証

```sql
SELECT user_id, workspace_slot_bonus FROM users WHERE user_id = '<user_id>';
-- 期待: workspace_slot_bonus = :expected_bonus
```

ユーザ側で workspace 作成が成功することも併せて確認。

---

## 5. Re-enable criteria / フラグを再度 `true` に戻す条件

Step 1 で `false` に戻した後、根本原因が解消されたと判断できるまで `true` への再 flip は **しない**。再 flip の必要条件:

- 直近 1 時間以内に `workspace_creation_denied{enforced=true}` の false-positive が 0 件
- 影響 user 全員に通知済み、bonus 調整が完了
- 同種の根本原因（migration 漏れ / admin 誤操作 / lock_timeout）が再発しないと判断できる説明が記録されている
- ops issue（`ops(quota): enforce_workspace_cap log-only window + prod flip (#674 sub-C follow)` 系）で再 flip 計画が separately tracked されている

判断が即時困難な場合は `false` のまま log-only mode を継続するのが安全側。log-only mode は cap 超過を warn log で可視化するだけで実害ゼロ。

---

## 6. References

- Issue #677 (sub-C): advisory lock + enforce_workspace_cap flip + runbook (本 runbook の起点)
- Issue #674 (epic): slot-based workspace cap pivot
- Issue #676 (sub-B): admin UI for `workspace_slot_bonus` inc/dec — merge 後に §4 を admin UI 手順に置換
- Code: `backend/src/services/quota_service.py:_acquire_workspace_create_lock`, `check_workspace_creation_allowed`
- Migration: `backend/alembic/versions/e15_675_workspace_slot_bonus.py` (grandfather backfill)
- Helper: `backend/src/utils/plan_resolver.py:get_user_workspace_cap_summary`
- Sibling runbook for tone reference: `docs/ops/r2-checksum-binding-rollout.md`
