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

> **障害時 (v0.125.0, #2282)**: この削除が失敗すると (どちらの backend でも、接続・timeout・API エラーはすべて `QdrantError`) erasure はここで止まる。これが最初のステップなので Postgres は何も消えておらず `users` 行も残っているため (vector の削除は collection ごとなので、失敗した collection より前の collection の point は削除済みのことがある。再実行が残りを削除する)、request は `failed` にならず `cooling_off` に戻り、`scheduled_for` = 15 分後、`deleted_data_summary.retry` に `{attempts, step: "vector_store", error_class, last_attempt_at}` が入る (ID・例外テキストは入らない)。hourly の cron sweep が期限後に再実行するので実際の間隔は最大 1 時間。4 回目 (最初の実行を含む) も失敗すると `failed` + `failure_reason = "vector_store_unavailable after 4 attempts: …"` (ID はそのまま。新しい request を作るのが再試行)。`QdrantError` 以外の例外 (client 生成時の設定エラー等) は従来どおり即 `failed`。待機中の self-service 行はユーザーがアカウント画面から取り消せる (Postgres はまだ何も消えていない。前述の削除済み point の memory は再 embedding されるまで検索に出ない)。v0.128.0 (#2304) からは、このステップを 1 回以上試した request が取り消されたとき、または 4 回目で `failed` になったとき、サービスが本人の live な embedding 済み (`success`) memory と embedding 実行中 (`processing`) の memory をすべて `pending` に戻す (resource indexer の行も含む。embedding パイプラインが行から indexer の point を作り直す。retry 予算を使い切った stale な `processing` 行は除く)。各バッチ (500 行) は他のトランザクションが持つ行ロックを最大 5 秒待ち、超えると再キューを止めて `erasure_embeddings_requeue_failed` を出す (取り消し・失敗の結果は変わらない)。件数は `deleted_data_summary.retry.embeddings_requeued`、時刻は `requeued_at`。コストは memory 1 件につき embedding 1 回 (+ chunk)。embedding sweep が通常のペース (30 秒ごとに 20 件、`created_at` の古い順) で処理するので、件数が多いと他のメンバーの新しい memory の embedding がその分遅れる。v0.129.0 (#2358) からは、このステップが成功した後 Postgres の commit 前に失敗した request (`workspace_transfer_required`、Postgres ステップ内のエラー) と、以前の試行がある request が再実行で別の理由により `failed` になったとき (待機中に始まった課金契約、このステップの設定エラー等) も、`failed` を書いた後に同じ方法で本人の memory を再キューする (アカウントは残り、point は消えている可能性があるため)。このステップが一度も失敗していなければ `retry.attempts` は 0 (`failure_reason` が失敗の理由)。Postgres の commit 後の失敗は再キューしない (行は pseudonym のもので、point は設計どおり削除済み)。v0.125.0 より前にこのステップで失敗した request、SQL で取り消した行、v0.129.0 より前に commit 前の失敗で `failed` になった行は対象外 (§5.4)。admin 強制削除の行は待機中も本人には取り消せない。恒常的な `QdrantError` (不正な filter 等) も約 3 時間で `failed` になる。

> ⚠️ **Multi-tenant 設計の trade-off**: 共有 workspace 内でユーザーが author の point も削除される。GDPR 上の data subject = author のため意図的な挙動。共有 workspace co-owner には事前同意 (Privacy Policy / ToS) が前提。 v0.129.0 (#2363) からは、Postgres ステップの commit 時点でまだ embedding 待ち・再試行中 (`pending` / `processing` / `failed`) だった本人の残存 memory をキューから外す (`embedding_status = 'failed'`、`embedding_error` は `erased_author:` で始まり、retry 予算は使い切り) ので、embedding sweep が pseudonym 名義で point を作り直すことはない。commit 前に読み込んで実行中だった run が削除の後に point を書いた場合は、その run が自分の point を削除する (`embedding_points_removed_after_lost_claim`)。vector の削除から Postgres ステップの行ロックまでの間に run が point を書いて `success` にした行は、commit の直後にもう一度 raw sub で point を削除して消す (best-effort。件数は `deleted_data_summary.qdrant_after_commit`、失敗すると `qdrant_after_commit_failed` に例外クラス)。これらの行は自動の再試行では戻らない。戻すのは `POST /api/v1/admin/embedding/retry-failed` (context または workspace 単位) と context の embedding モデルの切り替え (`success` 以外の行をすべて再キューする) で、戻すと pseudonym 名義・`details` なしで embedding される。

### 2.3 Workspace ownership (Step 3)

所有 workspace ごとに:

- **他に admin/owner がいる場合**: ownership を `user_id` 昇順で alphabetical first の admin に auto-transfer。
- **admin が他に居ないが member がいる場合**: `WorkspaceTransferRequiredError` (HTTP 409)。ユーザーは ownership を譲渡してから再実行する必要がある。
- **sole owner の場合**: そのまま step 4 で workspace ごと削除 (cascade で contexts/memories/edges/...)。

譲渡された workspace の connector write key のうち本人名義のものは、step 4 で新しい owner の名義に移る (#2305)。

### 2.4 Postgres (Step 4)

FK の依存逆順で削除:

| テーブル | 条件 | 備考 |
|---|---|---|
| `oauth_tokens` | `user_id = :uid` | FK cascade なし |
| `oauth_authorization_codes` | `user_id = :uid` | FK cascade なし |
| `oauth_clients` | `owner_id = :uid` | tokens は cascade で残らず |
| `external_api_keys` | `user_id = :uid` | Fernet 暗号化 3rd-party キー |
| `api_keys` (`connector_id IS NOT NULL`, `revoked_at IS NULL`) | `user_id = :uid` かつ workspace の owner が本人でない | 削除せず `user_id` を workspace の `owner_user_id` に付け替え (#2305)。connector の write key は workspace のもので、worker が持つ平文はそのまま有効、以後の rotate は owner 名義で発行される。counts キー = `connector_api_keys_reowned`。メンバー削除・admin のユーザー削除・`delete_admin` CLI も同じ helper (`services/connector_key_ownership.py`) で owner に付け替える (#2352) |
| `api_keys` | `user_id = :uid` | connector key 以外 (revoked の connector key を含む) |
| `graph_memory` | `user_id = :uid` | orphan, FK cascade なし |
| `usage_stats` | `user_id = :uid` | |
| `user_plans` | `user_id = :uid` | PK = user_id |
| `workspace_members` | `user_id = :uid` | |
| `workspace_invitations` | `invited_by = :uid OR email = :email` | |
| `plan_changes.changed_by` | SHA256 pseudonymize | legal retention 維持 |
| `workspaces` | `owner_user_id = :uid` | step 3 後の sole-owner のみ。cascade で contexts/memories/etc. |
| `memories` (残存行) | `user_id` を SHA256 pseudonymize + `details = NULL`。`success` 以外の行は `embedding_status = 'failed'` + `embedding_error = 'erased_author: …'` + `embedding_retry_count = 3` + `embedding_attempted_at` (#2363) | #1336: 共有/移譲 workspace に残る本人著メモリの scrub。details の NULL 化で生成列 (location_lat/lon, trigger_from/until) も自動 NULL。tombstone 行は #2263 から scrub せず削除。embedding キューから外した件数は counts キー `memories_embedding_dequeued` (pseudonymize の前に raw sub で実行) |
| `worker_app_identities.created_by` / `updated_by` | SHA256 pseudonymize | #1358: global 行 (workspace cascade なし)。legal retention 維持、operator sub のリンクのみ切断 |
| `config_overrides.updated_by` | SHA256 pseudonymize | #1365: #1358 と同形の global 行。admin sub のリンク切断 |
| authorship 列族 (残存 workspace 行) | SHA256 pseudonymize | #1365: `workspace_connectors` / `resources` / `resource_tokens` / `workspace_addons` / `file_objects` / `secrets` / `secret_versions` / `recipient_pubkeys` の `created_by` + `recipient_pubkeys.attested_by` + `secret_grants.granted_by` + `contexts.created_by` + `external_api_keys.updated_by`。counts キー = `authorship_columns_pseudonymized` (12 列の合計) |
| `secret_access_log.actor_user_id` / `recipient_identity` | SHA256 pseudonymize (e72 trigger carve-out) | #1365: append-only + HMAC hash chain。**pseudonymize した行の per-row entry_hash 再計算は fail する (意図的・検出可能)**。chain リンク (prev_hash→entry_hash) は無傷で他行の検証は通る。GDPR/APPI 消去義務 > 当該行の content 認証、という受容トレードオフ。`verify_audit_chain` は 64-hex pseudonym 形の mismatch を `erasure_pseudonymized` として分類して walk を継続する (それ以外の mismatch は従来どおり tamper alarm)。残余リスク: raw SQL 権限を持つ攻撃者は identity 列改竄を 64-hex に偽装できるが、その攻撃者クラスは trigger も erasure コードも実行できるため chain の脅威モデル外。carve-out 外の全列は完全に tamper-evident のまま |
| `memories` / `memory_revisions` (他人著で本人が `details` を編集した memory) | `details` の top-level key 単位 (#2244 / #2275) | `memories` 行の lock 後、tombstone 削除の次。本人が値を入れた live な key (後の member が dict ごと書き戻して残った key も含む) を本人の書き込み前の値に戻す (無ければ key を削除)。新しい version と `writer='erasure'` の revision 行 (prior = 上書きした値から本人の key を除いたもの + 除いた key の sha256 一覧 `erased_details_keys` + `restored_from`)。v0.129.0 から revision の prior からは本人の key だけを除き、残りの key はそのまま。除いた key は名前ではなく sha256 だけを `erased_details_keys` に追記する (後の erasure がそれを「削除」ではなく消去済みの値として扱い、2 つの erasure の実行順で結果が変わらないようにするため。版の再構成・session rollback はこの一覧を読まない)。本人著の memory の prior は従来どおり `details` ごと削除 + `erased` marker。counts キー = `details_restored` / `details_restored_unavailable` (memory 数) / `details_keys_reverted` (key 数) / `details_keys_ambiguous` (保持した key 数、下記) / `revision_details_stripped` (行数) |
| `audit_logs` (本人の行 / 本人を名指す他人の行) | SHA256 pseudonymize (§2.5)。本人の行は `ip_address` / `user_agent` も NULL (#2345) | #2287: 同一トランザクション内、`users` 削除の直前。counts キー = `audit_logs_pseudonymized` (本人の行)、`audit_mentions_pseudonymized` (他人の行、#2298) |
| `users` | `user_id = :uid` | 最後 |

ここまでが 1 つの Postgres トランザクション。どこかで失敗すると全体が rollback され (user 行は残る)、request は marker なしの `failed` (id はそのまま) になり、新しい request で再実行できる。

`details_keys_ambiguous` (v0.129.0, #2275): v0.129.0 より前の erasure は本人が書いた状態を prior から `details` ごと削除していたため、その上の別 member の編集に見える key が本人の値の書き戻しか別 member 自身の値か判別できない。そうした key は戻さずに保持して数だけ数える (別 member の書き込みを推測で消さない)。`reprocess_erasures` の plan / apply は `details keys ambiguous  <request id>  N key(s) kept (stripped history, see runbook)` を表示する (key 名・値・pseudonym は表示しない)。この行は書き込み対象ではないので apply しても何も変わらない。v0.129.0 へ上げたら plan (`--all`、`--apply` なし) を一度走らせて件数を確認し、0 でなければ扱いを決める (follow-up)。

#2305 より前の erasure (メンバー削除・admin のユーザー削除は #2352 より前) で write key を削除された connector (worker の key が認証されず書けない) は、workspace の owner か admin が `POST /api/v1/workspace-connectors/{connector_id}/rotate-kmc-key` を一度呼べば復旧する: connector に有効な key が 1 本も紐付いていなければ、workspace の owner 名義で新しい key を発行する (worker は次の poll で取り直す)。有効な key が別にある場合は従来どおり `422` で、connector を削除して再登録する。key の名義人が workspace のメンバーでなくなっている connector も、同じ rotate で新しい key が owner 名義になる。lock 待ちが 5 秒を超えると `503` `CONNECTOR-003` (再試行する)。

### 2.5 Audit logs (Step 4 の一部)

`audit_logs` 行は **保持** (legal retention)、ただし本人が actor の行 (`user_id = :uid`) の `user_id` を SHA256 で pseudonymize、`user_email` は本人の email と一致する場合のみ pseudonymize (他人が actor の行に残る actor の email は保持)、`resource` 内の本人の email / sub も置換する (pseudonym で列長 255 を超える値は 255 文字に切り詰め、erasure が失敗しないようにする)。本人の行の `ip_address` / `user_agent` は NULL にする (v0.128.0, #2345。request 行と同じ扱い)。salt は per-deployment 定数 (`_AUDIT_PSEUDO_SALT`) で同一ユーザー由来行の cross-row 相関は維持、original sub/email は不可逆。

他人が actor の行で本人を名指す値 (v0.128.0, #2298) は **完全一致の場合のみ** 同じ pseudonym に置き換える (部分一致の置換はしない): `resource` が `user:<本人の email>` / `user:<本人の sub>` の行 (admin が本人を操作した行) は `resource` だけを `user:<pseudonym>` に、`user_email` が本人の email の行 (本人が admin として他人を操作した行。`user_id` は操作された側) は `user_email` を pseudonym にし、actor である本人の `ip_address` / `user_agent` を NULL にする (#2345)。admin が本人を操作した行の admin の `user_id` / `user_email`・`ip_address` / `user_agent`、どちらの行の `user_id`・`action`・hash はそのまま (説明責任の証跡)。本人の行と他人の行は 1 回の id 順ロックでまとめて取ってから更新する (互いを操作し合った 2 人の erasure が同時に走っても deadlock しない)。`user_metadata` 内の他メンバーの ID (#2348) と、API key 経由の行の `user_email = '<sub>@api'` は対象外 (別 issue)。v0.128.0 から `audit_logs.user_id` に index (`ix_audit_logs_user_id`, f17) がある。

v0.125.0 (#2287) から Step 4 の Postgres トランザクション内で実行される (`backend/src/services/erasure_details.py` の `pseudonymize_audit_logs`)。v0.124.x 以前は Postgres の commit 後の別ステップで、そこで失敗すると (`failure_reason` = `after_postgres_commit: audit_logs: <class>`、v0.124.0 より前は marker なしの例外文) 本人の audit 行が sub と email を残したままになる。`reprocess_erasures` がそれを pseudonymize する: `users` 行のない `audit_logs.user_id` のうち pseudonym が request のものと一致する値が本人の sub、email は同じ account の request の `user_email_hash` (salt なし sha256) のいずれかと一致する `user_email` 値。email が見つからなければ `user_email` は残し sub だけ置換する。同じ sub が再登録されて `users` 行がある場合は一致させず、行は保持して (`user_id` はそのまま) `rows kept` と表示する。ただし v0.128.0 からは、その行の `user_email` / `resource` に消去済みの旧 email (現在どの `users` 行も使っていないもの) が完全一致で残っていれば、下記の mention 処理でその値が pseudonym になり、その行の `ip_address` / `user_agent` も NULL になる ([deployment.md](../deployment.md) の "Erasures run before v0.124.0")。`rows kept` 行はその sub が `users` にある限り毎回の実行で繰り返し表示される (想定どおり。対処不要)。`--request <id>` でも同じ account の他の request の email まで復元する。 v0.128.0 から (#2302) v0.125.0 より前の note 行 (raw sub を保存) も、導出した pseudonym で同じ処理を行い、行自体も pseudonymize する (§5.4)。

v0.128.0 からは `reprocess_erasures` が過去の erasure にも上記を適用する (pseudonym ごとに 1 実行 1 回、request のトランザクション内): 本人を名指す他人の行 (#2298) は、request の `user_email_hash` と一致する email をテーブルの `user_email` 値と `user:` resource から探して完全一致で書き換え (表示は `pseudonymized audit mentions  <id>  N row(s)`)、その email が `users` 行に再び存在する (同じアドレスで再登録された) 場合は書き換えず `audit mentions  <id>  rows kept: the email is in use again` と表示する (その email が `users` にある限り毎回表示される。想定どおり)。同じ email を 2 つの消去済み account が使っていた場合は、その実行で最初に処理した request の分として 1 回だけ数える (plan と apply で同じ)。pseudonym を持つ本人の行の `ip_address` / `user_agent` を NULL にし、`account_erasure` 行の `initiated_by` が本人の sub なら pseudonym にする (#2345、`cleared audit request metadata  <id>  N row(s)`、N は変更した行数)。email の探索は実行ごとに `audit_logs` を 2 回全件走査する (plan も同じ)。email・sub・IP・user agent・hash は表示しない。

**v0.125.0 へ上げたら、次の erasure を実行する前に `reprocess_erasures --all` (plan → `--all --apply --yes`) を一度走らせること。** 再登録された sub を v0.125.0 で erasure すると、その時点の email だけで pseudonymize されるため、前回の erasure が v0.124.x 以前に残した audit 行の旧 email は raw のまま pseudonym 側に移り、以後 `reprocess_erasures` から到達できなくなる。

**v0.128.0 へ上げたら (f17 の index 作成後)、`reprocess_erasures --all` (plan → `--all --apply --yes`) をもう一度走らせること。** 過去の erasure が他人の行に残した本人の email / sub と、本人の行・`account_erasure` 行の IP / user agent がこれで消える。変更のない request は何も表示しない。

`ENVIRONMENT=production` では `AUDIT_PSEUDO_SALT` が公開 default (`kagura-erasure-v1`) や `change-me-audit-pseudo-salt` のままだと起動しない (#2225)。default から外すのは必須だが、すでに private な値は変えないこと: pseudonym は書き込み時に一度だけ計算され再計算されないため lookup は壊れないが、変更前と変更後に pseudonymize された同一人物の行は相関しなくなる (salt の存在理由そのもの)。短い private salt も受け入れる (長さチェックなし)。salt を変更した後は `reprocess_erasures` も変更前の erasure の残留 audit 行を pseudonym で見つけられない (一致しないので行はそのまま残る)。

### 2.6 Redis (Step 5, best-effort)

- `session:*` (`SessionManager.delete_user_sessions`)
- `co_act:{user_id}:*` (`clear_co_activations`)
- `rate_limit:user:{user_id}:*` + `quota:user:{user_id}:*` (`clear_user_rate_limits`)

`quota:ws:{workspace_id}:*` は user-identifying でないため触らない。embedding cache は xxHash key で user-addressable でないため TTL 自然失効。

### 2.7 Audit row + finalize (Steps 6-8)

- 新規 `audit_logs` 行を `action="account_erasure"` で書き込み (`deleted_data_summary` 全文付き)。`ip_address` / `user_agent` は持たない (admin 強制削除でも。request 行と同じ、v0.128.0, #2345)。`user_metadata.initiated_by` は request 行と同じ規則で、本人の sub と一致するときだけ pseudonym (`is_self_service` では判定しない)。admin の ID はそのまま
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
    "postgres": {"users": 1, "memories": 0, "api_keys": 3, "audit_logs_pseudonymized": 17, "audit_mentions_pseudonymized": 2, ...},
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
- 取り消された self-service 行、または 4 回とも失敗した行の本人は、一部の collection で point が削除済みのことがある (§2.2)。v0.128.0 (#2304) からはサービスが本人の live な memory を自動で再キューする (`deleted_data_summary.retry.embeddings_requeued`)。v0.129.0 (#2358) からは、vector の削除が成功した後 Postgres の commit 前に失敗した行 (`workspace_transfer_required` 等) と、以前の試行がある行が再実行で別の理由により失敗した場合も同様 (`retry.attempts` が 0 のことがある)。判定は 1 つ: `postgres` がない `failed` 行で、summary に `qdrant` があるか `retry.attempts > 0` のものは `retry.embeddings_requeued` を持つはず。持たない行 (自動の再キューが失敗した: `erasure_embeddings_requeue_failed`、または v0.129.0 より前の行) は手動 SQL を実行する (一部のバッチが commit 済みでも手動 SQL はそのまま実行してよい: `success` 行だけを動かす)。ほかに手動の再キューが要るのは、v0.125.0 より前にこのステップで失敗した行 (`retry` がない) と SQL で取り消した行。本人のアカウントが残る場合は、本人が author の live な memory を手動で再キューして embedding sweep に point を作り直させる (memory 数だけ embedding のコストがかかる。作り直されるのは active な collection の point のみ。サービスと違い `processing` 行は動かさないが、実行中の run が終われば次の手動実行で拾える): `UPDATE memories SET embedding_status = 'pending' WHERE user_id = '<subject-sub>' AND deleted_at IS NULL AND embedding_status = 'success';`
- 上記の手動 SQL は Postgres ステップが commit 済みの erasure (`complete`、または `deleted_data_summary.postgres` を持つ行) には実行しない (pseudonym を `<subject-sub>` に入れない): 本人の残存行は pseudonym のもので point は設計どおり削除済み、commit 時点で embedding 待ちだった行も v0.129.0 (#2363) から意図的に終端状態にしてある (`embedding_error` が `erased_author:` で始まる)
- `erased_author:` の行は `embedding_unsearchable_backlog` に `erased` として数えられる (`stalled` にも含まれる。`other` には含まれない)。障害ではなく対処不要で、これらの行だけでは警告は出ない。ただし障害の後に context / workspace 単位で `POST /api/v1/admin/embedding/retry-failed` を実行したとき、または context の embedding モデルを切り替えたときは、その範囲の `erased_author:` の行も pseudonym 名義で embedding し直される
- `erasure_late_points_delete_failed` (完了した request の `deleted_data_summary.qdrant_after_commit_failed`): commit 後の 2 回目の point 削除が失敗した。vector の削除と行ロックの間に embedding された行があれば、その point が raw sub のまま残っている可能性がある。request 行には pseudonym しかないため、受付時の記録等で sub が分かる場合に限り vector store 側で `user_id` の filter 削除を手動で行う
- `erasure_requests.status='failed'` の行は cron で自動再試行されない
- `failure_reason` を確認し、原因 (例: Qdrant 接続障害) を解消
- 同じユーザーで `POST /api/v1/admin/users/{user_id}/erase` を再実行 → `ErasureAlreadyInProgressError` (409) が返る場合は失敗行を `cancelled` に手動 update してから再実行
- `failure_reason` が `after_postgres_commit: ` で始まる行は再試行できない (Postgres の削除は commit 済みで `users` 行がない)。`reprocess_erasures` の対象 ([deployment.md](../deployment.md) の "Requests that failed after the Postgres step")
- `after_postgres_commit: interrupted: sweep` (v0.125.0, #2286): Postgres の commit 後にプロセスが落ちた (再起動・OOM・タスクの cancel) まま 1 時間経った `in_progress` 行を、cron sweep が `users` 行なしで見つけて `failed` にしたもの。Postgres ステップが同じトランザクションで request 行に書く `deleted_data_summary.postgres` が "実行済み" の印。行は pseudonymize 済み、Redis は sweep が best-effort で削除。v0.128.0 から (#2302) sweep は閉じた直後に、その実行が復元した memory (restore 自身の `memory_revisions` 行 = `writer='erasure'`・本人の pseudonym・`memory_scrub_at` 以降) の vector payload を patch し、件数を `deleted_data_summary.postgres.details_payload_*` に書く (patch に失敗した memory は embedding 再キュー)。`details_payload_synced` キーがない行は sweep 自身の patch が中断したか v0.128.0 より前に閉じた行 (または `after_postgres_commit: payloads: <class>` = 実行自身の payload ステップ内で失敗した行。`reprocess_erasures` は `after_postgres_commit:` の行すべてをこの条件で扱う) なので、`reprocess_erasures --request <id>` (plan、`would resync payloads <id> <n> memory(ies)`) → `--apply` で一度だけ patch して件数を記録する (2 回目は何もしない)。`account_erasure` audit 行、完了メールは行われない (本人の email は `users` 行と一緒に削除済みで、request 行には hash しか残らないため、受付時のチケット等で宛先が分かる場合に限り §7 の手順で手動送信できる)。`postgres` がなければ実行されていないので従来どおり `complete` + `{"note": "user_row_already_absent"}` (v0.125.0 からは ID も pseudonymize し、summary に `"pseudonymized": true`)。v0.124.x 以前に中断した行は summary がないためこちらになる
- 中断行の sub が sweep より前に再登録された場合 (v0.128.0, #2302): sweep は user の有無ではなく `postgres` summary で判断し、同じく `interrupted: sweep` の `failed` で閉じる。新しいアカウントには `_execute` を実行せず (vector point・workspace・Postgres・`users` 行に触れない)、Redis キーも新しいアカウントのものなので削除しない (summary に `redis` キーなし)。ログは `erasure_sweep_subject_reregistered`。閉じた行は新しいアカウントの erasure request を妨げない (409 にならない)
- v0.125.0 より前に sweep が書いた note 行 (`complete` + `{"note": "user_row_already_absent"}`、`pseudonymized` なし、`user_id` が raw sub) は、v0.128.0 から `reprocess_erasures --all` (plan、`would pseudonymize note row <id>`) → `--all --apply --yes` で pseudonymize される (#2302): `user_id` を導出した pseudonym に、`ip_address` / `user_agent` を NULL に、`initiated_by` は本人の sub のときだけ pseudonym に、summary に `"pseudonymized": true`。status と `completed_at` は変えない。本人の残留 audit 行 (§2.5) と、本人を名指す他人の行・本人の行の IP / user agent (#2298 / #2345) も同じトランザクションでその pseudonym により処理される (初回の `--all --apply` で他人の行の `resource` / `user_email` が書き換わる。その email が `users` 行で使われていれば保持)。v0.124.x 以前に Postgres の commit 後で中断した実行もこの行になるため、その pseudonym で復元された memory (`erasure` の revision 行) があれば commit 後に vector payload を一度 patch する (`would resync payloads <id> <n> memory(ies)`)。sub は表示しない

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
- `erasure_sweep_payload_resync_failed` — close は commit 済みで、その後の payload 再 patch (下記) が失敗した (request_id + 例外クラス。行は終端状態で sweep は再試行しない。§5.4 の `reprocess_erasures --request <id>` で補う。#2302)
- `erasure_sweep_payloads_resynced` — 中断行を閉じた直後に、復元した memory の vector payload を patch した (request_id + memory 数 + `details_payload_*` 件数のみ。#2302)
- `erasure_sweep_subject_reregistered` — 中断行の sub が再登録されていた: 行は `interrupted: sweep` で閉じ、新しいアカウントには触れず Redis も削除していない (request_id のみ。#2302)
- `erasure_finalize_request_missing` — `_finalize` の UPDATE が 0 行 (request 行が実行中に削除された。request_id のみ。#2302)。`_finalize` は実行の claim (`started_at`) で guard しない: Postgres の commit 後にしか呼ばれず、その事実は新しい claim より優先されるため (sweep の `interrupted` close も上書きして `complete` にする)
- `erasure_embeddings_requeued` — vector store の試行後に取り消し / `failed` になった request で、本人の memory を embedding に再キューした (request_id + `memories` 件数。#2304。#2358: Postgres の commit 前の失敗の後も)
- `erasure_embeddings_requeue_failed` — 上記の再キューが失敗した (request_id + 例外クラス。取り消し・失敗の結果は変わらない)。§5.4 の手動 SQL を実行する
- `erasure_embeddings_requeue_unrecorded` — 再キューはしたが件数を request 行に書けなかった (行の status が変わっていた。request_id + `memories`)
- `embedding_points_removed_after_lost_claim` — embedding の run が claim を失った後、自分が書いた point を削除した (memory_id + collection + `reason`: `author_changed` = 実行中に行の author が変わった (erasure で pseudonymize された)、`row_gone` = 実行中に行が削除された。#2363)。`author_changed` で別の run が実行中だった行は `pending` に戻して作り直す (erasure がキューから外した行は戻さない)
- `embedding_points_removal_failed` — 上記の削除が失敗した (memory_id + collection + 例外クラス)。point は孤児として残る: orphan sweep を待つか、memory id で point を手動削除する
- `embedding_unsearchable_backlog` — embedding sweep の backlog 警告。v0.129.0 から `erased` (erasure がキューから外した残存行、#2363) を別に数え、`erased` の行だけでは出ない
- `erasure_late_points_delete_failed` — Postgres の commit 後の 2 回目の point 削除が失敗した (request_id + 例外クラス。request は完了する。§5.4)
- `erasure_email_*` — 手動送信待ちメール

監視 alert (将来):

- `erasure_execute_failed` 発生 → admin に通知
- `erasure_retry_scheduled` が続く → vector store の障害。4 回目で `failed` になる (§5.4)
- `erasure_sweep_user_row_absent` (`ran_in_postgres=true`) 発生 → 中断した erasure。§5.4 を確認
- `erasure_sweep_subject_reregistered` 発生 → 中断した erasure の後に同じ ID で再登録があった。新しいアカウントのセッションは削除していないので通常は対処不要 (本人から問い合わせがあれば状態を確認)
- `erasure_sweep_executed count > 0` が 24h 続く → SLA 違反警告
- `email_dispatch_required=true` の未処理ログ件数 → 1 営業日超過で alert

---

## 9. 関連リソース

- Issue #360 — feat(auth): right-to-erasure endpoint (本 issue)
- Issue #379 — docs(legal): Privacy Policy Internal Observability Metrics section
- `backend/src/services/account_erasure_service.py` — 実装本体
- `backend/alembic/versions/c01_360_erasure_requests.py` — schema migration
- `backend/src/services/email_service.py` — Email service interface + LoggingEmailService stub
