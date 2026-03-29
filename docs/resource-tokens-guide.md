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
- Free plan: No resource tokens (higher tier plan required)
- Basic plan: Up to 3 active tokens
- Pro plan: Up to 30 active tokens

## Resource Tokens vs API Keys

| | API Key | Resource Token |
|---|---------|---------------|
| **Who uses it** | AI assistants (MCP) | External systems (REST API) |
| **Authentication** | `Authorization: Bearer kagura_...` | `X-Resource-API-Key: ...` |
| **Scope** | Full workspace access | Single resource + context |
| **Purpose** | remember/recall/explore | Ingest external data |
| **Rate limit** | Plan-based API calls/day | Per-token events/hour |
