# Resource Tokens Guide

Resource Tokens allow external systems to push data into Kagura Memory Cloud, making it searchable by AI assistants via MCP tools (recall, explore, reference).

## When to Use Resource Tokens

| Use Case | Example | How It Works |
|----------|---------|-------------|
| **Product catalog sync** | Shopify, WooCommerce | Product updates → Kagura → AI answers "What's the price of X?" |
| **Document sync** | Confluence, Notion, Google Docs | Page updates → Kagura → AI searches internal knowledge |
| **CRM integration** | Salesforce, HubSpot | Customer data → Kagura → AI answers "What's this customer's history?" |
| **Chat/messaging** | Slack, Teams, Discord | Channel messages → Kagura → AI searches team discussions |
| **CI/CD events** | GitHub Actions, Jenkins | Build results → Kagura → AI answers "What failed recently?" |
| **Log collection** | Datadog, CloudWatch | Alerts/incidents → Kagura → AI searches past incidents |
| **IoT/sensor data** | Device telemetry | Readings → Kagura → AI analyzes trends |

## How It Works

```
External System → POST /api/v1/resources/{resource_id}/events
                  (authenticated with X-Resource-API-Key header)
                  → Kagura indexes data into Qdrant + Memory table
                  → AI can recall/explore/reference the data via MCP
```

### Key Concepts

- **Resource ID**: A namespace for the data source (e.g., `products`, `slack-decisions`, `jira-tickets`)
- **Document ID**: Unique identifier within a resource (e.g., product SKU, Slack message ID)
- **Version**: Integer version number. When a new version is ingested, old versions are automatically cleaned up
- **Payload**: The actual data (JSON object). Projected into searchable text via Resource Schema

## Quick Start

### 1. Create a Resource Token

In the Web UI: **Integrations → Resource Tokens → Create Token**

Or via API:
```bash
curl -X POST http://localhost:8080/api/v1/resource-tokens \
  -H "Authorization: Bearer kagura_{your_api_key}" \
  -H "Content-Type: application/json" \
  -d '{
    "resource_id": "products",
    "context_id": "your-context-uuid",
    "description": "Product catalog sync",
    "quota_events_per_hour": 1000
  }'
```

The response carries the plaintext `token` (shown once) and the token's `id`, a public id of
the form `rtok_` followed by 22 letters and digits. Use that id to edit or revoke the token:

```bash
curl -X PATCH http://localhost:8080/api/v1/resource-tokens/rtok_3fJ0kQ9pLm2ZtX7cVb1NaR \
  -H "Authorization: Bearer kagura_{your_api_key}" \
  -H "Content-Type: application/json" \
  -d '{"description": "Product catalog sync (nightly)"}'

curl -X DELETE http://localhost:8080/api/v1/resource-tokens/rtok_3fJ0kQ9pLm2ZtX7cVb1NaR \
  -H "Authorization: Bearer kagura_{your_api_key}"
```

An integer id (the format before #1008) is rejected with `422`.

### 2. Send Data

```bash
curl -X POST http://localhost:8080/api/v1/resources/products/events \
  -H "X-Resource-API-Key: YOUR_RESOURCE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "op": "upsert",
    "doc_id": "SKU-001",
    "version": 1,
    "payload": {
      "name": "Wireless Headphones",
      "price": 79.99,
      "category": "Electronics",
      "description": "Noise-cancelling Bluetooth headphones"
    }
  }'
```

### 3. Search via MCP

Your AI assistant can now find this data:
```
> recall(query="wireless headphones price", context_id="...")
→ [SKU-001] Wireless Headphones - $79.99
```

## Operations

| Operation | `op` value | Description |
|-----------|-----------|-------------|
| **Upsert** | `"upsert"` | Create or update a document. New version auto-cleans old versions. |
| **Delete** | `"delete"` | Remove a document. Without version: deletes all versions. |

## Integration Examples

### Slack Integration

```python
# Slack Bolt App → Kagura
import requests

@app.event("message")
def handle_message(event, say):
    requests.post(
        f"{KAGURA_URL}/api/v1/resources/slack-decisions/events",
        headers={"X-Resource-API-Key": RESOURCE_TOKEN},
        json={
            "op": "upsert",
            "doc_id": event["ts"],  # Slack timestamp as unique ID
            "version": 1,
            "payload": {
                "channel": event["channel"],
                "user": event["user"],
                "text": event["text"],
                "timestamp": event["ts"],
            }
        }
    )
```

### GitHub Actions Integration

```yaml
# .github/workflows/notify-kagura.yml
- name: Send build result to Kagura
  run: |
    curl -X POST $KAGURA_URL/api/v1/resources/ci-builds/events \
      -H "X-Resource-API-Key: $RESOURCE_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{
        "op": "upsert",
        "doc_id": "${{ github.run_id }}",
        "version": 1,
        "payload": {
          "repo": "${{ github.repository }}",
          "branch": "${{ github.ref_name }}",
          "status": "${{ job.status }}",
          "commit": "${{ github.sha }}"
        }
      }'
```

### Cron-based Sync (Product Catalog)

```python
import requests

def sync_products():
    products = fetch_products_from_shopify()
    for product in products:
        requests.post(
            f"{KAGURA_URL}/api/v1/resources/products/events",
            headers={"X-Resource-API-Key": RESOURCE_TOKEN},
            json={
                "op": "upsert",
                "doc_id": product["id"],
                "version": product["updated_at_version"],
                "payload": {
                    "name": product["title"],
                    "price": product["price"],
                    "inventory": product["inventory_quantity"],
                }
            }
        )
```

## Versioning

- Each `(resource_id, doc_id, version)` creates a unique entry
- When a new version is ingested, **old versions are automatically deleted**
- Only the latest version is kept (no manual cleanup needed)
- Delete with `version=null` removes all versions of a document

## Quotas

- Each token has a `quota_events_per_hour` limit
- **Creating** a resource token (or a resource via `setup_resource`) requires a plan with the `resources` feature — XL (`promax`) by default (#1551). Lower plans cannot mint new tokens.
- The per-plan active-token caps are **serve-only** limits for tokens a workspace already holds — existing tokens stay valid, listed and editable (description, active flag) after a downgrade; nothing is revoked: S/free 0, M/basic 3, L/pro 30, XL/promax 150. For plans with the feature the cap is the second gate at creation time.
- The cap counts the **workspace's** active tokens, whoever minted them (#1919) — not the caller's — excluding connector-owned tokens (those take `max_connectors` seats instead). Two owners share one cap.
- A quota **raise** is checked against the **current** tier's aggregate ceiling (cap × 10,000 events/hour, summed over the same set of tokens the cap counts), so on Free (cap 0) any positive quota edit is refused (400) and on Basic the sum must fit 30,000 — the token itself keeps serving at its stored quota. Lowering is never refused. Because the cap and the ceiling share one population and a token is at most 10,000 events/hour, a workspace whose active-token count is within its cap can always raise every token to the maximum; a workspace holding more tokens than the cap (after a downgrade, or minted before the cap counted per workspace) must lower or revoke first.

## Resource Tokens vs API Keys

| | API Key | Resource Token |
|---|---------|---------------|
| **Who uses it** | AI assistants (MCP) | External systems (REST API) |
| **Authentication** | `Authorization: Bearer kagura_...` | `X-Resource-API-Key: ...` |
| **Scope** | Full workspace access | Single resource + context |
| **Purpose** | remember/recall/explore | Ingest external data |
| **Rate limit** | Plan-based API calls/day | Per-token events/hour |
