# WHERE 軸 v1 — `details.location` + `recall_nearby` 設計

- **日付**: 2026-07-17
- **状態**: 実装済み — issue [#1331](https://github.com/kagura-ai/memory-cloud/issues/1331)（gate1 green 2026-07-17、同日実装・PR #1341）
- **決定経緯**: 新機能ブレスト（GPS/空間・数値KVS）→ 7並列コードベース調査 → A案採択
- **駆動ユースケース**: (a) モバイル/フィールドエージェント（現場記録・訪問履歴）、(b) パーソナルライフログ。IoT 生ストリームは明示的にスコープ外

## 1. 背景と動機

Kagura の記憶は「時間軸」（Time Memory: `type='time'` + `details.trigger` + `recall_upcoming`）という意味検索と直交する決定論クエリ軸を既に持つ。本設計はその対になる第二の軸「空間（where）」を追加する。

- 記憶の粒度は「圧縮された意味 + 座標」。GPS トラックログ（全測位点）は扱わない — それは TSDB の領分
- 競合調査（2026-07-17 実施）: Mem0 / Zep(Graphiti) / Letta / OpenAI memory のいずれも geo-filtered recall を持たない。agent memory platform カテゴリにおいて WHERE 軸は実差別化要因

## 2. スコープ

**v1 に含む**: `details.location` データ契約と書き込み検証、生成列 + 部分 index + CHECK、決定論 MCP ツール `recall_nearby`、プライバシー不変条件、テスト一式。

**v1 に含まない（follow-up issue 化）**:
1. semantic recall への geo フィルタ（Qdrant payload 拡張 + geo index + 既存 point backfill + Lance ミラー）
2. HOW-MUCH 軸（数値測定履歴）— 専用テーブル案で別設計（§9 参照）
3. SDK `recall_nearby` ラッパ / CLI `--details` フラグ（kagura-memory-python-sdk 側、別リリーストレイン）
4. REST `GET /memory/list` への geo フィルタパラメータ
5. geofence / polygon 検索（必要になれば Qdrant v1.6.0+ の geo_polygon が PG 無改造の逃げ道）
6. user-forgotten tombstone の期限付き hard-purge sweep（§7 既知の制限を参照）

## 3. 確定した設計判断

| 分岐 | 決定 | 理由 |
|---|---|---|
| v1 スコープ | A案: PG 決定論レーンのみ | 座標を PG 単一ストアに閉じ、forget/erasure の既存カバレッジをそのまま効かせる。最小で出して差別化を先取り |
| ゲート方式 | **直交属性**（type 不問、`details.location` キー存在でゲート） | 「場所付き troubleshooting メモ」を許す。type を増やさない substrate 決定（2026-06-03）と整合。time 軸の type ゲートからの意図的逸脱 |
| tombstone 残留 | 既知制限として文書化 + follow-up issue | プラットフォーム全体の既存ギャップであり location スコープ外で直すのが規律適合 |
| PostGIS | 不採用 | イメージ乗換え（+30〜50MB + 運用負担）に対し、得るものは楕円体精度 ~0.5% と polygon のみ。点データの radius/bbox には過剰 |
| 拡張（cube/earthdistance） | v1 では使わない | pinned `postgres:18.4-alpine` に同梱確認済みだが、bbox prefilter + haversine で十分。managed PG への可搬性も plain 案が優位 |

## 4. データ契約

```
details.location = {
  "lat":   number（必須、-90..90）,
  "lon":   number（必須、-180..180）,
  "label": string（任意、≤256 文字）,
  "text":  string（任意、pass-through。全体は details 1MB 上限に服する）
}
```

- **検証**: `utils/geo_location.py` の `normalize_location()`（`utils/time_trigger.py` を鋳型に `LocationValidationError(ValueError)`）
  - lat/lon は int/float のみ受理。**bool を明示拒否**（`_require_int` の bool-is-int 罠の float 版）、**文字列数値も拒否**（MCP arg coercion は details 内部を再帰しないため、`"35.6"` を黙って通すと生成列 NULL → recall_nearby から不可視。早期 422 が正）
  - NaN / ±Inf 拒否、範囲検証
  - **未知キーは拒否**（許可キー: lat / lon / label / text。将来 accuracy_m / altitude 等はサーバ側バージョンアップで追加）
  - 正規化時に **7桁固定小数へ丸めて書き戻し**（≈1cm 精度）。~~JSONB 数値の指数表記を排除し~~ **(#1344 訂正)** `memories.details` は json 型（jsonb ではない）で挿入テキストを逐語保存するため、`json.dumps` は `0 < |値| < 1e-4` を指数表記（`5e-05`）で出力し、丸めでは排除できない。生成列 regex ガード側が指数表記を受理する（下記 §5）
- **ゲート**: `details` に `location` キーが存在する時のみ検証発火。非 dict（例: 既存データの `{"location": "Tokyo office"}` 文字列）や不正 shape は ValueError → 422。**実装前に prod の details に別 shape の location キーが存在しないかを確認する**（存在すれば移行判断を追加）
- **正典は `details.location` のみ**。`context.location` は禁止（context JSONB は Qdrant payload に複製されるため座標が第2ストアへ漏れる）— ツール説明と docs に明記

## 5. スキーマ（migration は e30_877 の 1:1 ミラー）

- 生成列（`models/memory.py` の resource_version パターン L264-271 を踏襲）:
  ```sql
  location_lat  DOUBLE PRECISION GENERATED ALWAYS AS (
    CASE WHEN details->'location'->>'lat' ~ '^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]{1,2})?$'
         THEN (details->'location'->>'lat')::double precision ELSE NULL END) STORED
  -- location_lon 同型
  ```
  float キャストは IMMUTABLE のため本物の数値列が持てる（time 軸の TEXT 固定幅トリックは ::timestamp が STABLE だったための回避であり、ここでは不要）。regex ガードにより不正値は NULL 化し INSERT を落とさない（raw SQL 経路の防御）。**指数表記の受理は #1344 で追加**（details は json 型・逐語保存のため app 経路の `5e-05` が届く）— exponent 2 桁上限で、regex を通った字句は必ず double precision に overflow なくキャストできる
- 部分 btree: `idx_memories_location (location_lat, location_lon) WHERE location_lat IS NOT NULL AND deleted_at IS NULL` — **述語に tombstone 条件を含める**（`idx_memories_delivery_always` L398 と同型。time index L407 に無い改良で、述語漏れが seq scan として即可視化される）
- CHECK `valid_location_range`: IS NOT NULL ガード付きで lat ∈ [-90,90] AND lon ∈ [-180,180]（CHECK は NULL で通過するため明示ガード必須 — e30_877 L44-63 の教訓）。ORM `__table_args__` と migration にバイト同一で記述（`test_schema_drift.py` / `test_create_all_vs_alembic_drift.py` が自動照合）
- migration: `down_revision` は実装時点の head を確認（調査時点では `e67_1281_agent_ws`）。STORED 生成列追加はテーブル書換を伴う — **prod の memories 行数を事前確認**（e30_877 は同型で本番通過済み）

## 6. 書き込みパスとクエリ面

### 書き込み

- `MemoryService._apply_location()` を `_apply_time_trigger`（memory_service.py L269-299）の兄弟として追加し、**remember (L422) / _update_in_place (L617-625) / patch_memory (L839-853) の3箇所すべて**から呼ぶ（1箇所でも抜けると生成列 NULL → 不可視、という time と同一の罠）
- エラー写像は既存配線: `LocationValidationError(ValueError)` → MCP `validation_error`（tools/memory.py L120-128 / L256-264 パターン）→ REST 422（routes/memory.py L129-132 / L346-349 パターン）
- `update_memory` / `patch` の details 全置換契約は維持（location だけの部分更新は無い）。「details を送り直すと location が消える」round-trip 挙動はテストで pin し、ツール説明に明記

### recall_nearby（決定論 MCP ツール、`recall_upcoming` の完全ミラー）

```
recall_nearby(context_id, lat, lon, radius_m=1000, k=20)
  required: [context_id, lat, lon] / readOnly: true / Hebbian 副作用なし
```

- クエリ側検証も `normalize` と同基準（範囲・bool/str 拒否）。DB セッション取得前に検証し `validation_error` 返却（handle_recall_upcoming L299-315 パターン）
- clamp: k ∈ [1,100]（default 20、`clamp_upcoming_k` ミラー）、radius_m ∈ [1, 1_000_000]（1m〜1,000km）
- `services/geo_memory.py` に単一実装（time_memory.py L37-103 ミラー）:
  1. `deleted_at IS NULL` + `context_id` ゲート + `location_lat IS NOT NULL`
  2. bbox prefilter（部分 index を効かせる）: `lat BETWEEN` + `lon BETWEEN`。経度幅は `radius / (111320 * cos(lat))` 補正、**±180° 跨ぎは経度2区間の OR**、極近傍（|lat|+radius が 90° を跨ぐ）は経度全域にフォールバック
  3. SQL 式 haversine で `ORDER BY distance ASC LIMIT k`
  4. `filter_memory_rows_by_binding`（#1299、subtractive — k を割り込んでも backfill しない。time と同じ）
- `trusted_only` パラメータは配線のみで default False（recall_upcoming と同じ理由: user-initiated read は injection 面でない。将来 bootstrap/always 面が location を消費する場合は必ず True）
- 返却: `memory_id, summary, type, details, distance_m`（distance 昇順）
- コンテキスト解決は `_resolve_context_for_read`（均一404、CWE-639）。**cross-context nearby は作らない** — private context 横断の位置開示オラクルになる（recall の per-context binding ループにも前例なし）
- MAE operation 語彙: recall_upcoming 同様 operation=None（log-only shadow）で開始。語彙拡張は #1301 と合流

## 7. プライバシー不変条件

| # | 不変条件 | 状態 |
|---|---|---|
| 1 | provenance server-stamp（'connector' 偽装不可） | 既存保証（変更不要） |
| 2 | trust_tier 二段ゲートの雛形適用（trusted_only 配線） | 新規（テンプレ有: time_memory.py L70-75） |
| 3 | context スコープ + 均一404 + RBAC | 新規（テンプレ有） |
| 4 | #1299 binding filter 適用 | 新規（テンプレ有） |
| 5 | tombstone 除外（クエリ述語 + index 述語の両方） | 新規（index 述語は time 超えの改良） |
| 6 | **ログ規律: recall_nearby の生 lat/lon をサーバログ・MAE metadata に書かない**（丸め or 省略のみ可）。recall が生クエリをログする既存習慣への明示的カウンター。レビュー基準に含める | 新規 |
| 7 | connector ingest からの location 剥奪: `resource_indexer._LINEAGE_RESERVED_KEYS` に `'location'` 追加 + strip テスト拡張（#896 ルール「生成列を駆動する details キーは worker 供給を剥ぐ」） | 新規 |
| 8 | `remember` ツール説明の PII 文言改訂（location は第一級ペイロード。それ以外の PII 禁止は維持） | 新規 |
| 9 | export/データ境界: memories は RAW_EXPORTABLE — details.location は GDPR export に正しく乗る。recall_nearby 応答 schema を EXPORT_SURFACE_SCHEMA_NAMES に分類 | 小 |

**既知の制限（v1 で文書化、follow-up issue 起票）**:
- **tombstone 永続残留**: `forget()` は PG soft-delete + Qdrant point 削除であり、PG の tombstone 行（座標含む）は永久残留する（purge sweep は sleep-merge 敗者のみ対象）。GPS により深刻化する既存ギャップ。期限付き hard-purge sweep（file GC パターン、7d 前例）を別 issue で
- **erasure 非対称**: アカウント消去は共有 workspace の PG 行（details 込み）を残す既知の文書化済み挙動（erasure-runbook.md L41-51）。GPS で法的露出が増幅 — 同 follow-up issue で扱う
- **PII scrubbing は効かない**: プラットフォームの PII ガードは ai-worker の connector ingest 専用。`remember(details.location)` にスクラブは無く、今後も無い（座標は意図されたペイロード）— 誤解防止のため明記

## 8. テスト計画（time 軸 7 ファイル構成の 1:1 ミラー + geo 固有）

1. `tests/utils/test_geo_location.py` — 純ロジック（範囲、bool/str/NaN/Inf 拒否、未知キー拒否、7桁丸め、指数表記排除）
2. `tests/services/test_remember_geo_memory.py` — 3書き込みパスの検証発火・非 location メモリのパススルー
3. `tests/mcp_server/test_recall_nearby.py` — mock-db + emitted SQL shape（bbox 2区間 OR、haversine ORDER BY、k/radius clamp、malformed 引数、context_id 必須）
4. `tests/mcp_server/test_update_memory_geo.py` — update 経由 ValueError→validation_error、details 全置換で location 消失の round-trip pin
5. `tests/integration/test_location_cols_migration.py` — 生成列 round-trip（raw INSERT→SELECT、不正値の NULL 化）
6. geo 固有エッジ: antimeridian（±180° 跨ぎ）、高緯度 cos 補正、極フォールバック、radius clamp 境界
7. 自動ゲート: `test_schema_drift.py` / `test_create_all_vs_alembic_drift.py`（CHECK/index/列のモデル⇔migration 照合）、`test_resource_indexer.py` の strip テスト拡張
8. プライバシー回帰: recall_nearby ログ出力に生座標が含まれないことの pin（#1324 型の read-path 回帰テスト様式）

## 9. HOW-MUCH 軸の先行知見（本 spec のスコープ外、次期設計の前提）

調査で確定した反証: **sleep には type 除外が一切存在しない**。measurement を memory 行にすると (1) consolidation が参照ゼロ working 行を物理削除、(2) dedup が数値系列を near-duplicate マージで破壊、(3) 測定点ごとに embedding コスト発生。よって次期設計は**専用テーブル（agent_states 型: embedding なし・recall 構造的除外）+ 節目のみ通常メモリ**のハイブリッドを起点とする。series 集計は cost_aggregation_service の date_trunc バケット + window cap パターンを再利用。

## 10. 主要タッチポイント（実装チェックリスト）

- `backend/src/utils/geo_location.py` — 新設（normalize_location / parse クエリ側検証 / haversine・bbox ヘルパ）
- `backend/src/models/memory.py` — 生成列 2 本（L306 直後）+ `__table_args__` に index/CHECK
- `backend/alembic/versions/eNN_location_cols.py` — e30_877 ミラー（down_revision は実装時 head 確認）
- `backend/src/services/memory_service.py` — `_apply_location` + 3 呼び出し点
- `backend/src/services/geo_memory.py` — 新設（query_nearby_memories + clamp）
- `backend/src/mcp_server/tools/memory.py` — `handle_recall_nearby`
- `backend/src/mcp_server/tools/_definitions.py` — ツール schema + remember 説明改訂（PII 文言含む）
- `backend/src/mcp_server/tools/__init__.py` — 3点登録（read-only リスト / dispatch / import）
- `backend/src/services/resource_indexer.py` — `_LINEAGE_RESERVED_KEYS` へ `'location'`
- `backend/src/models/data_boundary.py` — 応答 schema 分類
- docs 4 ファイル（api-reference.md / concepts.md / mcp-input-schemas.md / claude-skills/smoke-test.md）
- 任意: `memory_health_service.py` の endpoints に `mcp:recall_nearby`
