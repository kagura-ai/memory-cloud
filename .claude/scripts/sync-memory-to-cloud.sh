#!/bin/bash
# sync-memory-to-cloud.sh
# PostToolUse Hook: Claude Code自動メモリ → Kagura Memory Cloud同期
# Issue #318 Phase 1
#
# Claude Codeがメモリファイルを書き込んだ時に、
# Memory CloudのMCPエンドポイントにJSON-RPCでrememberを呼び出す。
#
# 入力: stdin にJSON (tool_input with file_path)
# 出力: なし (バックグラウンドで同期)
#
# 環境変数:
#   KAGURA_MCP_URL: MCP Streamable HTTPエンドポイント (必須)
#                   例: http://localhost:8080/mcp/w/{workspace_id}
#   KAGURA_MCP_TOKEN: Bearer認証トークン (必須)
#   KAGURA_CONTEXT_ID: 固定のcontext_id (未設定時はプロジェクト名から自動推定)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# メモリファイルでなければスキップ
[ -z "$FILE_PATH" ] && exit 0
case "$FILE_PATH" in
  */memory/*.md) ;; # メモリディレクトリのmdファイルのみ対象
  *) exit 0 ;;
esac

# MEMORY.md (インデックス) はスキップ
case "$FILE_PATH" in
  */MEMORY.md) exit 0 ;;
esac

# ファイルが存在しなければスキップ (削除の場合)
[ -f "$FILE_PATH" ] || exit 0

# MCP URL とトークンが設定されていなければスキップ
[ -z "${KAGURA_MCP_URL:-}" ] && exit 0
[ -z "${KAGURA_MCP_TOKEN:-}" ] && exit 0

# --- frontmatter パース ---
CONTENT=$(cat "$FILE_PATH")

# YAMLフロントマターを抽出 (--- で囲まれた部分)
FRONTMATTER=$(echo "$CONTENT" | sed -n '/^---$/,/^---$/p' | sed '1d;$d')
# 本文: 2番目の --- 以降をすべて取得
BODY=$(echo "$CONTENT" | awk 'BEGIN{c=0} /^---$/{c++; if(c==2){found=1; next}} found{print}')

# frontmatterからフィールド抽出
NAME=$(echo "$FRONTMATTER" | grep -E '^name:' | sed 's/^name:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "")
DESCRIPTION=$(echo "$FRONTMATTER" | grep -E '^description:' | sed 's/^description:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "")
TYPE=$(echo "$FRONTMATTER" | grep -E '^type:' | sed 's/^type:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "pattern")

[ -z "$NAME" ] && exit 0

# --- context_id 解決 ---
# KAGURA_CONTEXT_ID (UUID) が設定されていればそのまま使用
# 未設定の場合はプロジェクト名からlist_contextsで解決を試みる
if [ -n "${KAGURA_CONTEXT_ID:-}" ]; then
  CONTEXT_ID="$KAGURA_CONTEXT_ID"
else
  # プロジェクトディレクトリ名を抽出
  PROJECT_DIR=$(echo "$FILE_PATH" | grep -oP '(?<=projects/)[^/]+' || echo "")
  if [ -n "$PROJECT_DIR" ]; then
    CONTEXT_NAME=$(echo "$PROJECT_DIR" | sed 's/.*-works-//' | sed 's/.*-home-//')
  else
    CONTEXT_NAME="default"
  fi

  # list_contexts でcontext名からUUIDを解決
  LIST_RESULT=$(curl -s -X POST "${KAGURA_MCP_URL}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${KAGURA_MCP_TOKEN}" \
    -d '{"jsonrpc":"2.0","id":0,"method":"tools/call","params":{"name":"list_contexts","arguments":{}}}' \
    2>/dev/null || echo "")

  if [ -n "$LIST_RESULT" ]; then
    # レスポンスからcontext名でマッチするUUIDを抽出
    CONTEXT_ID=$(echo "$LIST_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | \
      grep -oP "\"id\":\s*\"[^\"]+\"[^}]*\"name\":\s*\"${CONTEXT_NAME}\"" | \
      grep -oP '(?<="id":\s")[^"]+' | head -1 || echo "")

    # 見つからなければ最初のcontextを使う
    if [ -z "$CONTEXT_ID" ]; then
      CONTEXT_ID=$(echo "$LIST_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | \
        grep -oP '"id":\s*"[0-9a-f-]{36}"' | head -1 | grep -oP '[0-9a-f-]{36}' || echo "")
    fi
  fi

  # それでも見つからなければスキップ
  [ -z "$CONTEXT_ID" ] && exit 0
fi

# --- typeマッピング (Claude Code type → Memory Cloud type) ---
case "$TYPE" in
  feedback) MC_TYPE="lesson" ;;
  project) MC_TYPE="decision" ;;
  reference) MC_TYPE="pattern" ;;
  user) MC_TYPE="lesson" ;;
  *) MC_TYPE="pattern" ;;
esac

# --- importanceマッピング ---
case "$TYPE" in
  project) IMPORTANCE=0.9 ;;
  feedback) IMPORTANCE=0.85 ;;
  reference) IMPORTANCE=0.8 ;;
  user) IMPORTANCE=0.7 ;;
  *) IMPORTANCE=0.8 ;;
esac

# --- MCP JSON-RPC ペイロード構築 ---
PAYLOAD=$(jq -n \
  --arg summary "$NAME: $DESCRIPTION" \
  --arg context_summary "$DESCRIPTION" \
  --arg content "$BODY" \
  --arg type "$MC_TYPE" \
  --argjson importance "$IMPORTANCE" \
  --arg context_id "$CONTEXT_ID" \
  --arg cc_type "$TYPE" \
  '{
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: {
      name: "remember",
      arguments: {
        context_id: $context_id,
        summary: $summary,
        context_summary: $context_summary,
        content: $content,
        type: $type,
        importance: $importance,
        tags: ["claude-code", "auto-memory", $cc_type]
      }
    }
  }')

# --- Memory Cloud に同期 (バックグラウンド) ---
(
  curl -s -X POST "${KAGURA_MCP_URL}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${KAGURA_MCP_TOKEN}" \
    -d "$PAYLOAD" \
    >/dev/null 2>&1 || true
) &

exit 0
