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
| `users` | `user_id = :uid` | 最後 |

### 2.5 Audit logs (Step 5)

`audit_logs` 行は **保持** (legal retention)、ただし `user_email` / `user_id` カラムを SHA256 で pseudonymize。salt は per-deployment 定数 (`_AUDIT_PSEUDO_SALT`) で同一ユーザー由来行の cross-row 相関は維持、original sub/email は不可逆。

### 2.6 Redis (Step 6, best-effort)

- `session:*` (`SessionManager.delete_user_sessions`)
- `co_act:{user_id}:*` (`clear_co_activations`)
- `rate_limit:user:{user_id}:*` + `quota:user:{user_id}:*` (`clear_user_rate_limits`)

`quota:ws:{workspace_id}:*` は user-identifying でないため触らない。embedding cache は xxHash key で user-addressable でないため TTL 自然失効。

### 2.7 Audit row + finalize (Steps 7-9)

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
    "postgres": {"users": 1, "memories": 0, "api_keys": 3, ...},
    "audit_logs_pseudonymized": 17,
    "redis": {"sessions": 2, "co_act": 0, "rate_limit": 1}
  }
}
```

### 5.4 失敗時の手動再試行

- `erasure_requests.status='failed'` の行は cron で自動再試行されない
- `failure_reason` を確認し、原因 (例: Qdrant 接続障害) を解消
- 同じユーザーで `POST /api/v1/admin/users/{user_id}/erase` を再実行 → `ErasureAlreadyInProgressError` (409) が返る場合は失敗行を `cancelled` に手動 update してから再実行

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
- `erasure_email_*` — 手動送信待ちメール

監視 alert (将来):

- `erasure_execute_failed` 発生 → admin に通知
- `erasure_sweep_executed count > 0` が 24h 続く → SLA 違反警告
- `email_dispatch_required=true` の未処理ログ件数 → 1 営業日超過で alert

---

## 9. 関連リソース

- Issue #360 — feat(auth): right-to-erasure endpoint (本 issue)
- Issue #379 — docs(legal): Privacy Policy Internal Observability Metrics section
- `backend/src/services/account_erasure_service.py` — 実装本体
- `backend/alembic/versions/c01_360_erasure_requests.py` — schema migration
- `backend/src/services/email_service.py` — Email service interface + LoggingEmailService stub
