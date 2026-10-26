# Account Erasure Runbook

GDPR Art.17 (right to erasure) / APPI 第22条 / 利用停止請求対応のオペレーション手順 (Issue #360, v0.14.1+)。

このリリースで `EmailService` は **stub 実装** (`LoggingEmailService`) のため、ユーザー通知メールは **on-call admin が手動で送信** する。本 runbook はその手順と、関連する compliance 設計の要点をまとめる。

---

## 1. データフローと SLA

GDPR Art.12(3) の「受領後 1 ヶ月以内」応答義務を満たすため、以下のタイミングで対応する。

| 段階 | アクション | SLA |
|---|---|---|
| 受領 | ユーザーが `POST /api/v1/me/account/erasure-request` を実行、もしくは admin が `POST /api/v1/admin/users/{id}/erase` で実行 | 即時 |
| 受領通知メール | `erasure_email_receipt` ログを admin が `support@kagura-ai.com` 経由で手動送信 | 受領後 **1 営業日以内** |
| Cooling-off | 7 日 (self-service のみ。admin 強制は skip) | 7 日 |
| Cooling-off 開始通知 | `erasure_email_cooling_off_started` ログを手動送信 (確認後すぐ) | 確認後 24 時間以内 |
| 削除実行 | `erasure_tasks.sweep_pending_erasures` cron が hourly に scan、`scheduled_for <= NOW()` で実行 | cooling-off 終了後 24 時間以内 |
| 完了通知メール | `erasure_email_complete` ログを手動送信 | 完了後 **24 時間以内** |

合計: 受領 → 完了 = 最大 **9 営業日**、SLA 1 ヶ月に余裕あり。

---

## 2. 削除対象データ (storage 横断)

`AccountErasureService._execute()` は以下の順序でクロスストア削除を行う。`erasure_requests.deleted_data_summary` に各ステップの行数が記録される。

### 2.1 Stripe (Step 1, best-effort)

`workspaces.stripe_customer_id` / `stripe_subscription_id` が populated な所有 workspace ごとに:

1. `stripe.Subscription.cancel(...)` — subscription 即時キャンセル
2. `stripe.Customer.delete(...)` — Stripe 側顧客レコード削除

`BILLING_ENABLED=false` の場合は no-op。失敗は warn ログ + `deleted_data_summary` に未完了として残り、Postgres 削除は続行される。orphan な Stripe 顧客は Stripe ダッシュボードから手動削除可能。

### 2.2 Qdrant (Step 2)

`db.qdrant.delete_user_points(user_id)` が `kagura_memories*` 全 collection (per-model variant 含む) で `Filter(must=[FieldCondition(key="user_id", match=user_id)])` による bulk delete を実行。

> Lance backend (Lite): 同エントリポイントが `LanceStore.delete_user_points` に委譲され、`kagura_memories*` 各テーブルへ `user_id = :sub` の SQL delete を発行する(#1336)。確認は Qdrant collection ではなく Lance テーブルの行数で行う。

> **障害時 (v0.125.0, #2282)**: この削除が失敗すると (どちらの backend でも、接続・timeout・API エラーはすべて `QdrantError`) erasure はここで止まる。これが最初のステップなので Postgres は何も消えておらず `users` 行も残っているため (vector の削除は collection ごとなので、失敗した collection より前の collection の point は削除済みのことがある。再実行が残りを削除する)、request は `failed` にならず `cooling_off` に戻り、`scheduled_for` = 15 分後、`deleted_data_summary.retry` に `{attempts, step: "vector_store", error_class, last_attempt_at}` が入る (ID・例外テキストは入らない)。hourly の cron sweep が期限後に再実行するので実際の間隔は最大 1 時間。4 回目 (最初の実行を含む) も失敗すると `failed` + `failure_reason = "vector_store_unavailable after 4 attempts: …"` (ID はそのまま。新しい request を作るのが再試行)。`QdrantError` 以外の例外 (client 生成時の設定エラー等) は従来どおり即 `failed`。待機中の self-service 行はユーザーがアカウント画面から取り消せる (Postgres はまだ何も消えていない。前述の削除済み point の memory は再 embedding されるまで検索に出ない)。v0.128.0 (#2304) からは、このステップを 1 回以上試した request が取り消されたとき、または 4 回目で `failed` になったとき、サービスが本人の live な embedding 済み (`success`) memory と embedding 実行中 (`processing`) の memory をすべて `pending` に戻す (resource indexer の行も含む。embedding パイプラインが行から indexer の point を作り直す。retry 予算を使い切った stale な `processing` 行は除く)。各バッチ (500 行) は他のトランザクションが持つ行ロックを最大 5 秒待ち、超えると再キューを止めて `erasure_embeddings_requeue_failed` を出す (取り消し・失敗の結果は変わらない)。件数は `deleted_data_summary.retry.embeddings_requeued`、時刻は `requeued_at`。コストは memory 1 件につき embedding 1 回 (+ chunk)。embedding sweep が通常のペース (30 秒ごとに 20 件、`created_at` の古い順) で処理するので、件数が多いと他のメンバーの新しい memory の embedding がその分遅れる。v0.125.0 より前にこのステップで失敗した request、SQL で取り消した行、vector の削除は成功したがその後 Postgres の commit 前に失敗した request (workspace の移譲エラー等) は対象外 (§5.4)。admin 強制削除の行は待機中も本人には取り消せない。恒常的な `QdrantError` (不正な filter 等) も約 3 時間で `failed` になる。

> ⚠️ **Multi-tenant 設計の trade-off**: 共有 workspace 内でユーザーが author の point も削除される。GDPR 上の data subject = author のため意図的な挙動。共有 workspace co-owner には事前同意 (Privacy Policy / ToS) が前提。

### 2.3 Workspace ownership (Step 3)

所有 workspace ごとに:

- **他に admin/owner がいる場合**: ownership を `user_id` 昇順で alphabetical first の admin に auto-transfer。
- **admin が他に居ないが member がいる場合**: `WorkspaceTransferRequiredError` (HTTP 409)。ユーザーは ownership を譲渡してから再実行する必要がある。
- **sole owner の場合**: そのまま step 4 で workspace ごと削除 (cascade で contexts/memories/edges/...)。

### 2.4 Postgres (Step 4)

FK の依存逆順で削除:

| テーブル | 条件 | 備考 |
|---|---|---|
| `oauth_tokens` | `user_id = :uid` | FK cascade なし |
| `oauth_authorization_codes` | `user_id = :uid` | FK cascade なし |
| `oauth_clients` | `owner_id = :uid` | tokens は cascade で残らず |
| `external_api_keys` | `user_id = :uid` | Fernet 暗号化 3rd-party キー |
| `api_keys` | `user_id = :uid` | |
| `graph_memory` | `user_id = :uid` | orphan, FK cascade なし |
| `usage_stats` | `user_id = :uid` | |
| `user_plans` | `user_id = :uid` | PK = user_id |
| `workspace_members` | `user_id = :uid` | |
| `workspace_invitations` | `invited_by = :uid OR email = :email` | |
| `plan_changes.changed_by` | SHA256 pseudonymize | legal retention 維持 |
| `workspaces` | `owner_user_id = :uid` | step 3 後の sole-owner のみ。cascade で contexts/memories/etc. |
| `memories` (残存行) | `user_id` を SHA256 pseudonymize + `details = NULL` | #1336: 共有/移譲 workspace に残る本人著メモリの scrub。details の NULL 化で生成列 (location_lat/lon, trigger_from/until) も自動 NULL。tombstone 行も対象 |
| `worker_app_identities.created_by` / `updated_by` | SHA256 pseudonymize | #1358: global 行 (workspace cascade なし)。legal retention 維持、operator sub のリンクのみ切断 |
| `config_overrides.updated_by` | SHA256 pseudonymize | #1365: #1358 と同形の global 行。admin sub のリンク切断 |
| authorship 列族 (残存 workspace 行) | SHA256 pseudonymize | #1365: `workspace_connectors` / `resources` / `resource_tokens` / `workspace_addons` / `file_objects` / `secrets` / `secret_versions` / `recipient_pubkeys` の `created_by` + `recipient_pubkeys.attested_by` + `secret_grants.granted_by` + `contexts.created_by` + `external_api_keys.updated_by`。counts キー = `authorship_columns_pseudonymized` (12 列の合計) |
| `secret_access_log.actor_user_id` / `recipient_identity` | SHA256 pseudonymize (e72 trigger carve-out) | #1365: append-only + HMAC hash chain。**pseudonymize した行の per-row entry_hash 再計算は fail する (意図的・検出可能)**。chain リンク (prev_hash→entry_hash) は無傷で他行の検証は通る。GDPR/APPI 消去義務 > 当該行の content 認証、という受容トレードオフ。`verify_audit_chain` は 64-hex pseudonym 形の mismatch を `erasure_pseudonymized` として分類して walk を継続する (それ以外の mismatch は従来どおり tamper alarm)。残余リスク: raw SQL 権限を持つ攻撃者は identity 列改竄を 64-hex に偽装できるが、その攻撃者クラスは trigger も erasure コードも実行できるため chain の脅威モデル外。carve-out 外の全列は完全に tamper-evident のまま |
| `audit_logs` (本人の行) | SHA256 pseudonymize (§2.5) | #2287: 同一トランザクション内、`users` 削除の直前。counts キー = `audit_logs_pseudonymized` |
| `users` | `user_id = :uid` | 最後 |

ここまでが 1 つの Postgres トランザクション。どこかで失敗すると全体が rollback され (user 行は残る)、request は marker なしの `failed` (id はそのまま) になり、新しい request で再実行できる。

### 2.5 Audit logs (Step 4 の一部)

`audit_logs` 行は **保持** (legal retention)、ただし本人が actor の行 (`user_id = :uid`) の `user_id` を SHA256 で pseudonymize、`user_email` は本人の email と一致する場合のみ pseudonymize (他人が actor の行に残る actor の email は保持)、`resource` 内の本人の email / sub も置換する (pseudonym で列長 255 を超える値は 255 文字に切り詰め、erasure が失敗しないようにする)。salt は per-deployment 定数 (`_AUDIT_PSEUDO_SALT`) で同一ユーザー由来行の cross-row 相関は維持、original sub/email は不可逆。admin が本人を操作した行 (`user_id` が admin) は対象外。

v0.125.0 (#2287) から Step 4 の Postgres トランザクション内で実行される (`backend/src/services/erasure_details.py` の `pseudonymize_audit_logs`)。v0.124.x 以前は Postgres の commit 後の別ステップで、そこで失敗すると (`failure_reason` = `after_postgres_commit: audit_logs: <class>`、v0.124.0 より前は marker なしの例外文) 本人の audit 行が sub と email を残したままになる。`reprocess_erasures` がそれを pseudonymize する: `users` 行のない `audit_logs.user_id` のうち pseudonym が request のものと一致する値が本人の sub、email は同じ account の request の `user_email_hash` (salt なし sha256) のいずれかと一致する `user_email` 値。email が見つからなければ `user_email` は残し sub だけ置換する。同じ sub が再登録されて `users` 行がある場合は一致させず、行は保持して `rows kept` と表示する ([deployment.md](../deployment.md) の "Erasures run before v0.124.0")。`rows kept` 行はその sub が `users` にある限り毎回の実行で繰り返し表示される (想定どおり。対処不要)。`--request <id>` でも同じ account の他の request の email まで復元する。

**v0.125.0 へ上げたら、次の erasure を実行する前に `reprocess_erasures --all` (plan → `--all --apply --yes`) を一度走らせること。** 再登録された sub を v0.125.0 で erasure すると、その時点の email だけで pseudonymize されるため、前回の erasure が v0.124.x 以前に残した audit 行の旧 email は raw のまま pseudonym 側に移り、以後 `reprocess_erasures` から到達できなくなる。

`ENVIRONMENT=production` では `AUDIT_PSEUDO_SALT` が公開 default (`kagura-erasure-v1`) や `change-me-audit-pseudo-salt` のままだと起動しない (#2225)。default から外すのは必須だが、すでに private な値は変えないこと: pseudonym は書き込み時に一度だけ計算され再計算されないため lookup は壊れないが、変更前と変更後に pseudonymize された同一人物の行は相関しなくなる (salt の存在理由そのもの)。短い private salt も受け入れる (長さチェックなし)。salt を変更した後は `reprocess_erasures` も変更前の erasure の残留 audit 行を pseudonym で見つけられない (一致しないので行はそのまま残る)。

### 2.6 Redis (Step 5, best-effort)

- `session:*` (`SessionManager.delete_user_sessions`)
- `co_act:{user_id}:*` (`clear_co_activations`)
- `rate_limit:user:{user_id}:*` + `quota:user:{user_id}:*` (`clear_user_rate_limits`)

`quota:ws:{workspace_id}:*` は user-identifying でないため触らない。embedding cache は xxHash key で user-addressable でないため TTL 自然失効。

### 2.7 Audit row + finalize (Steps 6-8)

- 新規 `audit_logs` 行を `action="account_erasure"` で書き込み (`deleted_data_summary` 全文付き)
- `erasure_requests.status='complete'`、`completed_at`、`deleted_data_summary` を確定
- 完了通知メール (stub 経由 → 手動送信)

### 2.8 `bm25_idf_drift_log` の取り扱い

`bm25_idf_drift_log.context_id` は `contexts.id ON DELETE CASCADE` のため、Step 4 の workspace 削除で context が消える際に **自動で cascade 削除される**。明示的な追加処理は不要。

#379 で本テーブルは "pseudonymous personal data under GDPR Art.4(5)" と分類されているが、context cascade が削除責務を満たすため erasure scope に含まれている (削除のスコープからの除外ではない)。

---

## 3. バックアップ retention

- **最大バックアップ齢**: 90 日 (Privacy Policy + 本 runbook で明記)
- **`erasure_requests` のバックアップ含有**: 必須。`erasure_requests` 行 (pseudonymized) は backup から除外しない
- **復元時 re-apply hook**: バックアップから DB を復元した場合、`erasure_requests.status='complete'` 行を起点に対応データ (Postgres 行 / Qdrant point / Redis key) を再削除する手順を運用に組み込む。具体的なスクリプトは v0.14.x で別 issue として実装予定 (現状: 復元 SOP の手動チェックリストに項目追加)

`erasure_requests` 自体の retention は **5 年** (GDPR Art.5(1)(b) / Art.30 説明責任証拠)。migration コメントと本 runbook で明記。DB 側の自動 purge job は **設定しない** — 5 年経過後の削除は legal review 後に手動で行う。

---

## 4. 第三者プロセッサ通知 (GDPR Art.19)

採用済みプロセッサ別の通知タイプと SLA。**自動通知 = API で SDK 側に削除を伝播 / 手動通知 = support@ から運用チケットで連絡**。

| プロセッサ | データ種別 | 通知タイプ | SLA | 担当 |
|---|---|---|---|---|
| OpenAI (embedding) | 入力テキスト (cache) | n/a | — | — | OpenAI は user_id を保存しない、cache 自然失効 |
| Anthropic (LLM judge) | プロンプト | n/a | — | — | request id ベース、user data なし |
| Voyage AI (rerank) | クエリ | n/a | — | — | 同上 |
| Stripe | customer / subscription | **auto** | 即時 | `cancel_subscription_and_delete_customer_for_erasure` で API 通知 |
| Sentry / Datadog 等 | error / observability | (未採用なら) n/a | — | (採用後追加) |
| Resend / SES 等 | email log | (未採用 — stub のため n/a) | — | (採用後追加) |

未採用プロセッサが追加された場合は本表を更新し、必要なら自動通知を `_execute()` に組み込む。

---

## 5. Admin 強制削除手順

### 5.1 エンドポイント

`POST /api/v1/admin/users/{user_id}/erase` (system_admin only)

```json
{
  "reason_code": "user_request_via_support",
  "reason_detail": "support@ 経由のメール削除要求 (チケット ZD-1234)"
}
```

### 5.2 reason_code 一覧

| コード | 用途 |
|---|---|
| `user_request_via_support` | support@ 等経由のユーザーからの要求 |
| `legal_order` | 裁判所命令 / 当局からの指示 |
| `inactivity_policy` | 利用停止規約による強制削除 |
| `abuse_violation` | ToS 違反 / abuse 対応 |
| `other` | 上記に該当しない (`reason_detail` に詳細必須) |

### 5.3 戻り値

```json
{
  "request_id": "...",
  "status": "complete",
  "deleted_data_summary": {
    "stripe": {...},
    "qdrant": {"kagura_memories": 42},
    "workspaces": {...},
    "postgres": {"users": 1, "memories": 0, "api_keys": 3, "audit_logs_pseudonymized": 17, ...},
    "redis": {"sessions": 2, "co_act": 0, "rate_limit": 1}
  }
}
```

503 `ERASURE-007` (v0.125.0, #2282): 最初のステップ (vector store の削除) が失敗した。Postgres は何も消えておらず、request は `cooling_off` で残り、`details.scheduled_for` 以降の最初の hourly cron sweep が再実行する (最大 1 時間後)。本人はこの request を取り消せない。その間に同じユーザーへ再度 force-erase すると `ErasureAlreadyInProgressError` (409, `existing_status: "cooling_off"`)。

```json
{
  "error": "ERASURE-007",
  "message": "The vector store is unavailable; the erasure request is kept and will run again on the first hourly sweep after 2026-01-01T00:15:00Z.",
  "details": {
    "request_id": "...",
    "attempt": 1,
    "max_attempts": 4,
    "scheduled_for": "2026-01-01T00:15:00Z",
    "error_class": "QdrantError"
  }
}
```

4 回目の失敗では request は `failed` になり、レスポンスは vector store 自身のエラー (502 `EXT-001`)。

### 5.4 失敗時の手動再試行

- vector store の障害 (§2.2) は手動再試行不要 (v0.125.0, #2282): request は `cooling_off` に戻り、cron sweep が 15 分以上後に最大 4 回まで自動で再実行する。`deleted_data_summary.retry.attempts` が失敗した回数。4 回とも失敗した行 (`failure_reason` が `vector_store_unavailable after 4 attempts: ` で始まる) は、障害を解消してから新しい request を作る (下記)
- 取り消された self-service 行、または 4 回とも失敗した行の本人は、一部の collection で point が削除済みのことがある (§2.2)。v0.128.0 (#2304) からはサービスが本人の live な memory を自動で再キューする (`deleted_data_summary.retry.embeddings_requeued`)。手動の再キューが要るのは、サービスが扱わない次の場合だけ: v0.125.0 より前にこのステップで失敗した行 (`retry` がない)、SQL で取り消した行、vector の削除が成功した後 Postgres の commit 前に失敗した行 (`deleted_data_summary.qdrant` があり `postgres` がない `failed` 行。`workspace_transfer_required` 等)、`retry.attempts > 0` なのに `embeddings_requeued` がない行 (自動の再キューが失敗した: `erasure_embeddings_requeue_failed`)。本人のアカウントが残る場合は、本人が author の live な memory を手動で再キューして embedding sweep に point を作り直させる (memory 数だけ embedding のコストがかかる。作り直されるのは active な collection の point のみ。サービスと違い `processing` 行は動かさないが、実行中の run が終われば次の手動実行で拾える): `UPDATE memories SET embedding_status = 'pending' WHERE user_id = '<subject-sub>' AND deleted_at IS NULL AND embedding_status = 'success';`
- `erasure_requests.status='failed'` の行は cron で自動再試行されない
- `failure_reason` を確認し、原因 (例: Qdrant 接続障害) を解消
- 同じユーザーで `POST /api/v1/admin/users/{user_id}/erase` を再実行 → `ErasureAlreadyInProgressError` (409) が返る場合は失敗行を `cancelled` に手動 update してから再実行
- `failure_reason` が `after_postgres_commit: ` で始まる行は再試行できない (Postgres の削除は commit 済みで `users` 行がない)。`reprocess_erasures` の対象 ([deployment.md](../deployment.md) の "Requests that failed after the Postgres step")
- `after_postgres_commit: interrupted: sweep` (v0.125.0, #2286): Postgres の commit 後にプロセスが落ちた (再起動・OOM・タスクの cancel) まま 1 時間経った `in_progress` 行を、cron sweep が `users` 行なしで見つけて `failed` にしたもの。Postgres ステップが同じトランザクションで request 行に書く `deleted_data_summary.postgres` が "実行済み" の印。行は pseudonymize 済み、Redis は sweep が best-effort で削除。復元した memory の vector payload の patch、`account_erasure` audit 行、完了メールは行われない (本人の email は `users` 行と一緒に削除済みで、request 行には hash しか残らないため、受付時のチケット等で宛先が分かる場合に限り §7 の手順で手動送信できる)。`postgres` がなければ実行されていないので従来どおり `complete` + `{"note": "user_row_already_absent"}` (v0.125.0 からは ID も pseudonymize し、summary に `"pseudonymized": true`)。v0.124.x 以前に中断した行は summary がないためこちらになる

```sql
UPDATE erasure_requests
SET status = 'cancelled', cancelled_at = NOW()
WHERE id = '<failed-request-id>';
```

---

## 6. Privacy Policy stopgap (Art.20 portability)

データポータビリティ (GDPR Art.20) は **本リリースでは out of scope**。`erasure_request` 完了前に自分のデータを取り出したいユーザーは:

1. `support@kagura-ai.com` に「データエクスポート要求」として連絡
2. SLA 1 ヶ月以内に admin が手動で Postgres / Qdrant / 関連データを抽出して提供
3. 提供完了後、ユーザーの判断で `/me/account/erasure-request` を発行

Privacy Policy の Art.17 セクションに上記 stopgap を明記すること (#379 の Privacy Policy ドラフトと整合)。Art.20 自動化は v0.14.x の別 issue。

---

## 7. on-call admin 向けメール送信ガイド

`LoggingEmailService` は以下のような構造化ログを出力する:

```json
{"event": "erasure_email_receipt", "to_email": "alice@example.com",
 "request_id": "...", "email_dispatch_required": true,
 "template": "erasure_receipt"}
```

**手順**:

1. ログ集約システムで `email_dispatch_required=true` を grep
2. `template` に応じて以下のテンプレートで `support@kagura-ai.com` から送信

### Template: erasure_receipt (受領後 1 営業日以内)

```
Subject: [Kagura Memory Cloud] アカウント削除リクエストを受け付けました

(本文)
このたびは Kagura Memory Cloud のアカウント削除リクエストをいただき、ありがとうございます。

リクエスト ID: {request_id}
受付日時: {received_at}

このメールは GDPR 第12条に基づく受領通知です。
今後、確認リンクをクリックいただいた後、7 日間の cooling-off 期間を経て削除を実行します。

ご不明な点があれば support@kagura-ai.com までご連絡ください。

Kagura Memory Cloud Operations
```

### Template: erasure_cooling_off_started

```
Subject: [Kagura Memory Cloud] アカウント削除のクーリングオフ期間が開始されました

(本文)
ご確認ありがとうございました。
{scheduled_for} (UTC) にアカウントとすべての関連データの削除を実行します。

それまでの間、削除をキャンセルしたい場合は Web UI から「アカウント削除をキャンセル」を選択するか、support@ までご連絡ください。

Kagura Memory Cloud Operations
```

### Template: erasure_complete (完了後 24 時間以内)

```
Subject: [Kagura Memory Cloud] アカウントの削除が完了しました

(本文)
リクエスト ID {request_id} の削除を完了しました。

GDPR Art.30 に基づく説明責任のため、削除事実を pseudonymize した形で 5 年間保持します。これは個人を特定できない形式のため、お客様の個人データではありません。

ご利用ありがとうございました。

Kagura Memory Cloud Operations
```

---

## 8. 観測

主要ログ event:

- `erasure_request_created` — pending row 作成
- `erasure_request_confirmed` — cooling_off へ遷移
- `erasure_request_cancelled` — ユーザーキャンセル
- `erasure_admin_force_started` — admin 強制削除開始
- `erasure_sweep_executed` — cron sweep 完了 (count 含む)
- `erasure_execute_failed` — 実行失敗 (request_id + error 含む)
- `erasure_retry_scheduled` — vector store の障害で request を `cooling_off` に戻した (request_id + attempt + max_attempts + scheduled_for + 例外クラス。#2282)
- `erasure_retry_request_moved` — 上記で戻そうとした request が別の処理で状態を変えていた、または後の sweep が再 claim していた (何も書いていない。request_id のみ)。呼び出し元には vector store のエラー (502) が返る
- `erasure_request_cancel_lost` — ユーザーの取り消しが cron sweep の claim と競合し、行がすでに `in_progress` だった (取り消しは 409 `ERASURE-006`、erasure は実行される。request_id + 現在の status)
- `erasure_sweep_retry_scheduled` — cron sweep 中の上記 (request_id + attempt + max_attempts + scheduled_for)。sweep は次の request に進み、実行件数には数えない
- `erasure_failed_after_postgres_commit` — Postgres commit 後の失敗 (request_id + step + 例外クラスのみ)
- `erasure_sweep_user_row_absent` — sweep が `users` 行のない request を閉じた (request_id + `ran_in_postgres`: true なら `interrupted: sweep` の `failed`、false なら `complete` + note。#2286)
- `erasure_sweep_redis_cleanup_failed` — 上記の中断行の Redis 削除に失敗 (request_id + 例外クラス。キーは TTL で消える)
- `erasure_sweep_request_moved` — sweep が閉じようとした request が claim 後に別の処理で状態を変えていた (何も書いていない。request_id のみ)
- `erasure_sweep_close_failed` — 上記の close が DB エラーで失敗 (request_id + 例外クラス。行は `in_progress` のまま、次回 sweep で再試行)
- `erasure_embeddings_requeued` — vector store の試行後に取り消し / `failed` になった request で、本人の memory を embedding に再キューした (request_id + `memories` 件数。#2304)
- `erasure_embeddings_requeue_failed` — 上記の再キューが失敗した (request_id + 例外クラス。取り消し・失敗の結果は変わらない)。§5.4 の手動 SQL を実行する
- `erasure_embeddings_requeue_unrecorded` — 再キューはしたが件数を request 行に書けなかった (行の status が変わっていた。request_id + `memories`)
- `erasure_email_*` — 手動送信待ちメール

監視 alert (将来):

- `erasure_execute_failed` 発生 → admin に通知
- `erasure_retry_scheduled` が続く → vector store の障害。4 回目で `failed` になる (§5.4)
- `erasure_sweep_user_row_absent` (`ran_in_postgres=true`) 発生 → 中断した erasure。§5.4 を確認
- `erasure_sweep_executed count > 0` が 24h 続く → SLA 違反警告
- `email_dispatch_required=true` の未処理ログ件数 → 1 営業日超過で alert

---

## 9. 関連リソース

- Issue #360 — feat(auth): right-to-erasure endpoint (本 issue)
- Issue #379 — docs(legal): Privacy Policy Internal Observability Metrics section
- `backend/src/services/account_erasure_service.py` — 実装本体
- `backend/alembic/versions/c01_360_erasure_requests.py` — schema migration
- `backend/src/services/email_service.py` — Email service interface + LoggingEmailService stub
