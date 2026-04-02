Search Kagura Memory Cloud for relevant past knowledge and patterns.

Use the kagura memory cloud MCP tools to search for: $ARGUMENTS

Steps:
1. Use `recall` with context_id appropriate to the query (default: kagura-dev), query="$ARGUMENTS", k=10, use_rerank=false
2. Display results in a table format showing: memory_id, summary, type, importance, tags
3. If relevant results found, suggest using `reference` for detailed content on the most relevant match
4. If no results found, suggest broader search terms
