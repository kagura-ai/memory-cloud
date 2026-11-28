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

## Publishing a resource as a public MCP endpoint

A resource's data — a website, a product catalog, an FAQ — can be served to anyone, without sign-in, as a **read-only MCP endpoint**: visitors add the URL to Claude, ChatGPT or any MCP client and search it. No API key, resource token or OAuth flow is handed out. The operator must switch the feature on first (`MCP_PUBLIC_ENDPOINTS_ENABLED=true`, see [Deployment › Public MCP endpoints](deployment.md#public-mcp-endpoints--issue-2675)).

**Preconditions** (checked when you publish and again on every anonymous request):

- the resource exists in your current workspace (`setup_resource` created it);
- its context is live, **public** (`is_public=true`, which is plan-gated by the `public_contexts` feature) and not private;
- you are the workspace **owner** (session or an owner API key).

**The four owner calls** (`resource_id` is the slug):

```bash
# Publish: the approved public text is what anonymous clients see. Nothing falls
# back to the context's internal summary or usage guide.
curl -X POST "$KAGURA/api/v1/resources/products/public-mcp" \
  -H "Authorization: Bearer kagura_{owner_key}" -H "Content-Type: application/json" \
  -d '{"public_name": "Acme product catalog",
       "public_description": "Prices and specifications of our products.",
       "public_usage_guide": "Search by product name; reference(memory_id) for the full sheet."}'
# → 201 {"public_id": "pmcp_…", "endpoint_path": "/mcp/public/pmcp_…", "serving": true, …}

curl "$KAGURA/api/v1/resources/products/public-mcp" -H "Authorization: Bearer kagura_{owner_key}"   # read (404 when none)
curl -X PATCH "$KAGURA/api/v1/resources/products/public-mcp" -H "Authorization: Bearer kagura_{owner_key}" \
  -H "Content-Type: application/json" -d '{"public_usage_guide": ""}'                                # edit text ("" clears)
curl -X DELETE "$KAGURA/api/v1/resources/products/public-mcp" -H "Authorization: Bearer kagura_{owner_key}" # unpublish → 204
```

- The address is your deployment's origin plus `endpoint_path`. It is server-minted and opaque; one live endpoint per resource context (a second publish is `409`). Unpublishing takes effect on the next request, including for clients that are already connected; publishing again mints a **new** address and the old one stays `404`.
- `serving` says whether the endpoint answers right now (the operator switch is on and the preconditions still hold). Making the context private or non-public, moving it to another resource slug or deleting it closes the endpoint too, without a separate unpublish.
- Name ≤ 200 characters, description ≤ 2,000, usage guide ≤ 4,000.

**Usage budget:** when the operator enables it (`MCP_PUBLIC_USAGE_BUDGET_ENABLED`), all public endpoints of your workspace share one budget of anonymous tool calls per seven-day period (default 25,000), together with anonymous REST searches of a published context. The publish, read and update responses (POST, GET and PATCH) carry it as `usage_budget` (`state`, `limit`, `used`, `remaining`, `resets_at`, …); the numbers are the workspace's, the same on every endpoint. When it is used up, tool calls are refused until the period ends — the data, the addresses and the listing methods stay — and anyone can spend it, since the endpoint is anonymous; calling your own public URL is counted too. Contract: [Public usage budget](public-usage-budget.md).

**What is public:** the rows ingested for this resource (`details.resource_id` equals the slug) that are not deleted. A note a member writes by hand into the same context is not served — unless that member sets `details.resource_id` to the slug themselves, which anyone who can write to the context can do. Each row shows its `memory_id`, `doc_id` (your document id), summary, content (through `reference`), type, tags, `source_url` (only an `http(s)` URL), and timestamps. A row of a product or FAQ feed that nobody edited shows the record's title or question as its summary, its kind (`product`, `faq`) as its type and the record's URL as `source_url`. **Not public:** `details`, versions and history, importance, links, the context's summary / usage guide / guardrails / search configuration / statistics, and anything about the workspace. The five tools and their exact response fields: [MCP Tools › Public endpoint tools](mcp-tools.md#public-endpoint-tools).

**Usage of your endpoint:** each completed tool call is recorded as a usage event — tool, outcome, duration and opaque document keys; never the visitor's query, session id or address ([Public MCP usage events](public-mcp-usage-events.md)). `GET /api/v1/resources/{resource_id}/public-mcp/usage-health` (owner) shows the live endpoint's calls of the last 24 hours by outcome, the gap rows that report events the server could not record, and the event writer's queue:

```bash
curl "$KAGURA/api/v1/resources/products/public-mcp/usage-health" -H "Authorization: Bearer kagura_{owner_key}"
# → {"resource_id": "products", "endpoint_id": "…", "window_hours": 24, "events": 42,
#    "outcomes": {"success": 40, "rate_limited": 2}, "gaps": {"count": 0, "dropped_events": 0, "last_gap_at": null},
#    "writer": {"enabled": true, "queue_depth": 0, …, "process_local": true}, "retention_days": 90}
```

`GET /api/v1/resources/{resource_id}/public-mcp/usage-stats?days=7` (owner, [#3035](https://github.com/kagura-ai/memory-cloud/issues/3035)) gives the live endpoint's calls, searches and search outcomes per UTC day, by route and caller, the daily visitors a server with a bound key reported, and the documents its searches returned most often, with their public title and URL while the endpoint serves. These are calls to the public MCP endpoint and, while the operator records them (`PUBLIC_SEARCH_TELEMETRY_ENABLED`), REST public searches of the context made with a key bound to it; anonymous REST public searches are not included. Calls are not people, and answers a cache in front of the server served are not counted ([Statistics for the owner](public-mcp-usage-events.md#statistics-for-the-owner)). The context's overview page shows the same numbers to the workspace owner.

```bash
curl "$KAGURA/api/v1/resources/products/public-mcp/usage-stats?days=30" -H "Authorization: Bearer kagura_{owner_key}"
# → {"resource_id": "products", "window_days": 30, "totals": {"calls": 42, "searches": 30, "search_outcomes": {…}},
#    "days": [{"date": "2026-10-29", "calls": 0, …}, …], "documents": {"status": "ok", "publication_serving": true,
#    "items": [{"doc_key": "…", "searches": 12, "best_rank": 1, "title": "Trail shoe", "source_url": "https://example.com/p/1"}], …}, …}
```

Details and limits: [Statistics for the owner](public-mcp-usage-events.md#statistics-for-the-owner).

**Your own domain** ([#2809](https://github.com/kagura-ai/memory-cloud/issues/2809)): when the operator switches custom domains on (`MCP_PUBLIC_DOMAINS_ENABLED`; `GET /api/v1/system/info` then reports `features.public_mcp_domains: true`), the published endpoint can also answer at `https://<your host>/mcp`, for example `https://mcp.example.org/mcp`. The alias serves the same endpoint as the fixed address: same tools, limits and usage budget. Adding and verifying a domain needs the workspace's canonical owner (the owner the workspace belongs to); on a deployment that sells it, the workspace also needs the custom-domain right. One domain per published endpoint, a few per workspace (default 3).

```bash
# 1. Add the host name → 201, state "pending", with the TXT record to publish
curl -X POST "$KAGURA/api/v1/resources/products/public-mcp/domains" \
  -H "Authorization: Bearer kagura_{owner_key}" -H "Content-Type: application/json" \
  -d '{"hostname": "mcp.example.org"}'
# → {"domain_id": "…", "hostname": "mcp.example.org", "url": "https://mcp.example.org/mcp", "state": "pending",
#    "verification": {"record_type": "TXT", "record_name": "_kagura-public-mcp.mcp.example.org",
#                     "record_value": "kagura-public-mcp-verification=…"},
#    "routing": {"target": "mcp-domains.example.com", "status": null}, "serving": false, …}

# 2. In your DNS: publish that TXT record, and a CNAME from mcp.example.org to routing.target.

# 3. Verify → 200; "state": "active" and "serving": true once the record is found
curl -X POST "$KAGURA/api/v1/resources/products/public-mcp/domains/<domain_id>/verify" \
  -H "Authorization: Bearer kagura_{owner_key}"

curl "$KAGURA/api/v1/resources/products/public-mcp/domains" -H "Authorization: Bearer kagura_{owner_key}"       # list
curl -X DELETE "$KAGURA/api/v1/resources/products/public-mcp/domains/<domain_id>" \
  -H "Authorization: Bearer kagura_{owner_key}"                                                             # remove → 204
```

- **Keep the TXT record published.** The server re-checks it about once an hour; after 3 confirmed absences and 72 hours since it was last seen the domain is `suspended` (`suspended_reason: "proof_lost"`) and the alias answers `404`. Publishing the record again reinstates it at the next check (or at once with verify), within 30 days; after that the name is released.
- A verify that finds no record leaves the domain `pending` (`last_check.result: "missing"`); DNS changes can take a while to be visible. When the lookup cannot be answered the call is `503`: retry later. Verify is limited to 20 calls per workspace per hour and 6 activation attempts per day (`429` with `Retry-After`). An unverified domain is deleted after 7 days.
- `routing.status` (`ok`, `mismatch`, `unresolved`) only tells you whether the name points at the target; it never stops the alias.
- If another domain already holds the name, verify answers `409` `public_mcp_domain_held` once your own record was found; a name another workspace removed recently answers `409` `public_mcp_domain_in_cooldown` with `details.available_after` (default 7 days after the release).
- Removing the domain, or unpublishing the endpoint, ends the alias from the next request. Publishing again gives a new endpoint without a domain: add it again (with a new TXT value).
- Give clients the name exactly as the API returns it (lower case). All fields and errors: [API Reference › Custom domains of a public MCP endpoint](api-reference.md#custom-domains-of-a-public-mcp-endpoint); the full contract: [Public MCP custom domains](public-mcp-domains.md).

## Resource Tokens vs API Keys

| | API Key | Resource Token | Observation credential |
|---|---------|---------------|------------------------|
| **Who uses it** | AI assistants (MCP) | External systems (REST API) | A self-hosted dashboard ([Observability API](observability-api.md)) |
| **Authentication** | `Authorization: Bearer kagura_...` | `X-Resource-API-Key: ...` | `Authorization: Bearer kagura_obs_...` |
| **Scope** | Full workspace access | Single resource + context | One workspace or one resource, read-only |
| **Purpose** | remember/recall/explore | Ingest external data | Read resource, indexer, usage and ingestion metadata only |
| **Rate limit** | Plan-based API calls/day | Per-token events/hour | Per-credential requests/minute |
