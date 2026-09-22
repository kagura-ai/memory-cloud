# Codex hook stdin fixtures

Synthetic stdin payloads shaped after `codex-rs/hooks/src/schema.rs` at `rust-v0.155.1`
(`PreToolUseCommandInput`, `PostToolUseCommandInput`, `SessionStartCommandInput`). Every
fixture carries the documented fields; `pre_tool_use_subagent.json` adds `agent_id` /
`agent_type`. The Bash `tool_response` shape is not documented by Codex - the object in
`post_tool_use_bash_nonzero.json` is synthetic and the tests rely only on the contract's
string-leaf rule. Payloads captured from a real Codex run belong in `captured/` (manual
check 3 of the #1620 protocol; redact `cwd` and `transcript_path`).
