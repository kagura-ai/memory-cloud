#!/bin/bash
# sync-memory-to-cloud.sh
# PostToolUse Hook: Auto-sync Claude Code memory files to Kagura Memory Cloud
# Issue #318 Phase 1
#
# When Claude Code writes a memory file, this script calls the Memory Cloud
# MCP endpoint via JSON-RPC to invoke the remember tool.
#
# Input: stdin JSON (tool_input with file_path)
# Output: none (syncs in background)
#
# Environment variables:
#   KAGURA_MCP_URL: MCP Streamable HTTP endpoint (required)
#                   e.g., http://localhost:8080/mcp/w/{workspace_id}
#   KAGURA_MCP_TOKEN: Bearer auth token (required)
#   KAGURA_CONTEXT_ID: Fixed context_id (if unset, auto-resolved from project name)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Skip if not a memory file
[ -z "$FILE_PATH" ] && exit 0
case "$FILE_PATH" in
  */memory/*.md) ;; # Only .md files in memory directories
  *) exit 0 ;;
esac

# Skip MEMORY.md (index file)
case "$FILE_PATH" in
  */MEMORY.md) exit 0 ;;
esac

# Skip if file doesn't exist (deletion case)
[ -f "$FILE_PATH" ] || exit 0

# Skip if MCP URL or token not configured
[ -z "${KAGURA_MCP_URL:-}" ] && exit 0
[ -z "${KAGURA_MCP_TOKEN:-}" ] && exit 0

# --- Parse frontmatter ---
CONTENT=$(cat "$FILE_PATH")

# Extract YAML frontmatter (between --- delimiters)
FRONTMATTER=$(echo "$CONTENT" | sed -n '/^---$/,/^---$/p' | sed '1d;$d')
# Body: everything after the second ---
BODY=$(echo "$CONTENT" | awk 'BEGIN{c=0} /^---$/{c++; if(c==2){found=1; next}} found{print}')

# Extract fields from frontmatter
NAME=$(echo "$FRONTMATTER" | grep -E '^name:' | sed 's/^name:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "")
DESCRIPTION=$(echo "$FRONTMATTER" | grep -E '^description:' | sed 's/^description:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "")
TYPE=$(echo "$FRONTMATTER" | grep -E '^type:' | sed 's/^type:\s*//' | sed 's/^"\(.*\)"$/\1/' || echo "pattern")

[ -z "$NAME" ] && exit 0

# --- Resolve context_id ---
# Use KAGURA_CONTEXT_ID (UUID) if set, otherwise resolve from project name via list_contexts
if [ -n "${KAGURA_CONTEXT_ID:-}" ]; then
  CONTEXT_ID="$KAGURA_CONTEXT_ID"
else
  # Extract project directory name from file path
  PROJECT_DIR=$(echo "$FILE_PATH" | grep -oP '(?<=projects/)[^/]+' || echo "")
  if [ -n "$PROJECT_DIR" ]; then
    CONTEXT_NAME=$(echo "$PROJECT_DIR" | sed 's/.*-works-//' | sed 's/.*-home-//')
  else
    CONTEXT_NAME="default"
  fi

  # Resolve context UUID from name via list_contexts
  LIST_RESULT=$(curl -s -X POST "${KAGURA_MCP_URL}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${KAGURA_MCP_TOKEN}" \
    -d '{"jsonrpc":"2.0","id":0,"method":"tools/call","params":{"name":"list_contexts","arguments":{}}}' \
    2>/dev/null || echo "")

  if [ -n "$LIST_RESULT" ]; then
    # Match context UUID by name from response
    CONTEXT_ID=$(echo "$LIST_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | \
      grep -oP "\"id\":\s*\"[^\"]+\"[^}]*\"name\":\s*\"${CONTEXT_NAME}\"" | \
      grep -oP '(?<="id":\s")[^"]+' | head -1 || echo "")

    # Fall back to first context if no match
    if [ -z "$CONTEXT_ID" ]; then
      CONTEXT_ID=$(echo "$LIST_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | \
        grep -oP '"id":\s*"[0-9a-f-]{36}"' | head -1 | grep -oP '[0-9a-f-]{36}' || echo "")
    fi
  fi

  # Skip if context_id could not be resolved
  [ -z "$CONTEXT_ID" ] && exit 0
fi

# --- Map Claude Code type to Memory Cloud type ---
case "$TYPE" in
  feedback) MC_TYPE="lesson" ;;
  project) MC_TYPE="decision" ;;
  reference) MC_TYPE="pattern" ;;
  user) MC_TYPE="lesson" ;;
  *) MC_TYPE="pattern" ;;
esac

# --- Map type to importance ---
case "$TYPE" in
  project) IMPORTANCE=0.9 ;;
  feedback) IMPORTANCE=0.85 ;;
  reference) IMPORTANCE=0.8 ;;
  user) IMPORTANCE=0.7 ;;
  *) IMPORTANCE=0.8 ;;
esac

# --- Build MCP JSON-RPC payload ---
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

# --- Sync to Memory Cloud (background) ---
(
  curl -s -X POST "${KAGURA_MCP_URL}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${KAGURA_MCP_TOKEN}" \
    -d "$PAYLOAD" \
    >/dev/null 2>&1 || true
) &

exit 0
