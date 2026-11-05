export const MCP_RESOURCE_TOOLS = [
  "setup_resource",
  "ingest_events",
  "get_resource_schema",
  "get_resource_impact",
  "list_resource_tokens",
] as const;

export const MCP_TOOL_PRESETS = [
  { key: "exploration", tools: ["explore", "list_edges"] },
  { key: "ingestion", tools: MCP_RESOURCE_TOOLS },
  { key: "analysis", tools: ["analyze_context", "get_analysis", "list_analyses", "get_active_analysis", "get_cluster"] },
  { key: "secrets", tools: ["secret_register_pubkey", "secret_put", "secret_get", "secret_list", "secret_revoke_grant"] },
] as const;
