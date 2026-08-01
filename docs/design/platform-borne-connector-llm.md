# Platform-Borne Connector LLM on SaaS Deployments

> **Status: design note (#1392) — not implemented.** Today the worker-config
> vend requires a per-connector BYO `llm_config` bundle, so every deployment —
> including SaaS — forces the admin to bring their own LLM key. Product
> intent: on SaaS the connector LLM is **platform-borne**; BYOK is an
> override, not a prerequisite.

## Resolution order at vend time

When the worker fetches a connector's config (`GET /api/v1/workers/config`),
the `llm` block resolves in this order:

1. **Connector BYOK bundle** (`llm_config`, Fernet-encrypted) — explicit
   per-connector override, unchanged from today.
2. **Workspace LiteLLM virtual key** (`litellm_virtual_key_id`) — the SaaS
   lane. The column exists and is admin-editable (#1376) but is currently
   stored-not-vended; this design is where it becomes real.
3. **Platform default** (SaaS) / **server-configured default** (self-hosted:
   env-based `Settings` defaults are an intended feature on self-host — the
   deployment-mode two-sidedness lesson from the Memory Analysis review).
4. Nothing available → `llm: null` (readiness semantics below).

## Mechanism: the LiteLLM virtual-key lane

- The platform runs a LiteLLM proxy; each connector (or workspace) gets a
  **virtual key** so usage is metered per tenant. `litellm_virtual_key_id`
  identifies that key; the vend hands the worker a proxy base URL + the
  virtual key instead of a raw provider credential.
- Must align with the downstream connector worker's dedicated-LLM-credential
  vend design — reference only; no cross-repo scope in this note. The
  convergence requirement: one vend shape both consumers can read
  (`{mode: "byok" | "virtual-key" | "platform", ...}` rather than an opaque
  dict whose meaning depends on who filled it).

## Cost control

Platform-borne usage is metered within plan / connector-seat limits using the
**check + record** pattern proven by managed embeddings (#709/#1030/#1033,
`embedding_daily_cap_usd` / `embedding_monthly_cap_usd` on `PlanTier`):

- Per-plan daily/monthly USD caps for connector LLM usage (new `PlanTier`
  fields, env-overridable like the embedding caps).
- Check before vend-window activation; record from LiteLLM usage callbacks.
- Cap exhaustion degrades the connector to "paused (quota)" — a vend-visible
  state, not a silent failure; the UI surfaces it via the readiness lane.
- Metered add-on top-ups stack via the add-on lane (see
  [add-on entitlements](addon-entitlements.md)).

## Vend contract: readiness semantics change

Today `llm: null` means "un-vendable" and the UI's `connectorReadiness`
(`frontend/src/lib/api/workspace-connectors.ts`) encodes exactly that:
`missingLlm = !llm_config_present` (the virtual key deliberately does NOT
count — #1388/#1389). When a platform lane exists:

- `missingLlm` becomes `!(llm_config_present || platformLlmAvailable)` where
  `platformLlmAvailable` is a deployment capability, not per-connector state.
- The flip is a **one-line change in one helper** by design — both the dialog
  summary and the row badges already route through `connectorReadiness`.

## Deployment-mode awareness (UI)

- Mode signal: a feature flag on `GET /api/v1/system/info` (intentionally
  public, non-sensitive deployment flags the web UI reads pre-auth — the
  existing pattern per `.claude/rules/security.md`), e.g.
  `connector_llm: "platform" | "self-hosted-default" | "byok-required"`.
- SaaS: LLM section defaults to "Kagura 提供の LLM を使用(追加設定不要)"
  with the BYOK fields in a fold (flips the default presentation delivered by
  #1388 — that issue explicitly labeled its copy "for the current behavior").
- Self-hosted: "サーバー既定を使用" + per-connector override fields.

## Non-goals / boundaries

- No pricing or billing mechanics in this repo — metering counts and caps are
  entitlement facts; purchase flows live in the external billing service
  (see [add-on entitlements](addon-entitlements.md) for the boundary rule).
- No change to the BYOK contract (`provider` + `model` required, `api_key`
  provider-dependent — `_validate_llm_config`).
- Sleep/analysis LLM provider selection is a separate lane (BYOK-scoped
  today) and out of scope.

Refs: #1376, #1380, #1388, #1389, #1393.
