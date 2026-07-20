# Multi-Platform Connector Readiness

> **Status: design note (#1390) — not implemented.** Discord / Teams connectors
> are on the ai-worker roadmap. This note decides how the connectors UI and
> contracts generalize so the second provider is an *addition*, not a rewrite —
> while deliberately building nothing speculative. The second-provider PR
> should be executable against this document.

## Current state (v0.55.3)

| Layer | Generalization status |
|---|---|
| Frontend connect CTA / rows | Driven by the `CONNECTOR_PROVIDERS` descriptor (`frontend/src/lib/connectors/providers.ts`, landed with #1389) — Slack enabled, Discord/Teams rendered as disabled "coming soon" |
| Frontend settings dialog | Slack-vocabulary labels (`channelsLabel`, helpers reference Slack UI) |
| Callback / error params | Slack-specific: `?slack_install=`, `?slack_error=`, `slackCancelled*` / `slackFailed*` / `slackExpired*` i18n keys |
| Backend routing | `/api/v1/connectors/slack/*` (OAuth install, callback, pending-install) |
| Backend data model | Already platform-shaped: `WorkspaceConnector.connector_type` ∈ `CONNECTOR_TYPES = {"slack", "discord", "teams"}` (`connector_provisioning.py`); worker-side data layer is platform-agnostic (`SourceIdentifier` + `Platform` enum), only the worker's connector glue (webhook/auth/slash) is Slack-only |

## 1. Provider descriptor (frontend) — implemented skeleton

`ConnectorProviderDescriptor` carries `key`, `name` (brand, untranslated),
`icon`, `connectFlow` (`"oauth" | "manual"`), `enabled`, and `installUrl?`.
Contract decisions worth keeping:

- **Routing lives in the descriptor.** `installUrl` is present iff the
  provider's connect flow is wired. Flipping `enabled: true` without wiring
  `installUrl` yields a dead button — never another provider's OAuth consent
  screen. The second-provider PR adds its own `installUrl` (and, if needed, a
  provider-specific pending-install fetch).
- The descriptor is UI-only. It must not grow backend knowledge (endpoint
  shapes, scope lists); those stay server-side.

Additions the second provider will need (add then, not now):

- `capabilities` flags consumed by the settings dialog (see §2), e.g.
  `{ channelSelection: "flat" | "guild-scoped", vision: boolean }`.
- Per-provider pending-install handling (`?slack_install=` equivalent).

## 2. Capability-driven settings vocabulary

The "channels" concept differs per provider:

| Provider | Ingest scope unit | ID shape |
|---|---|---|
| Slack | channels (flat) | `C…` channel IDs; team `T…` / Enterprise `E…` |
| Discord | servers (guilds) → channels | numeric snowflakes, guild-scoped |
| Teams | teams → channels | GUID-ish, tenant-scoped |

Decision: the settings dialog's *labels, helper copy, and client-side ID
validation* derive from provider capabilities; the *stored contract* stays a
flat `channel_ids: list[str]` vended opaquely to the worker (the worker's
`SourceIdentifier` already scopes IDs by platform). Guild/team hierarchy, if a
provider requires it for selection UX, is a picker-time concern (cf. the
[Slack channel picker design](slack-channel-picker.md)) — not a storage-shape
change.

## 3. Generalization policy — deliberately deferred until the 2nd provider lands

Renaming working Slack-specific surfaces now is churn with no user value.
These renames are **batched into the second-provider PR**, which is the first
change that can actually test them:

1. `?slack_error=<reason>` → `?connector_error=<reason>&provider=<p>` — same
   allowlisted-reason contract (`cancelled | failed | expired`, unknown
   collapses to `failed`); `provider` joins the allowlist discipline. Keep
   accepting `slack_error` for one release as a compatibility read.
2. `slackCancelled*` / `slackFailed*` / `slackExpired*` i18n keys →
   provider-interpolated generic keys ("{provider} 連携に失敗しました").
3. Backend routing `/connectors/slack/*` → per-provider routers under
   `/connectors/{provider}/*`, sharing the browser-facing redirect helpers
   (allowlist + non-reflection, #1375/#1381 pattern) — the helper is already
   provider-parameterized in spirit; make it so in signature.

## 4. Non-goals

- **No speculative backend abstraction.** The same altitude judgment as the
  #1381 review: `auth.py` and `connectors_slack.py` keep sibling helpers with
  different threat models; a shared "generic connector OAuth" layer is only
  justified once a second concrete implementation exists.
- **PII defaults and the vend contract stay per-provider decisions.** Discord
  content norms ≠ Slack workspace norms; do not inherit Slack's defaults
  implicitly.
- Worker-side connector glue generalization is owned by the ai-worker repo
  (its data layer is ready; glue refactoring waits for the Discord spec —
  YAGNI).

## Second-provider PR checklist

1. Descriptor entry: `enabled: true`, `installUrl`, `capabilities`.
2. Backend: `/connectors/{provider}/*` router + shared redirect helpers;
   `connector_error`/`provider` params (§3.1) with `slack_error` compat read.
3. i18n: generic provider-interpolated keys (§3.2); retire `slack*` keys.
4. Settings dialog: capability-driven labels/validation (§2).
5. Row/readiness: no change needed — `connectorReadiness` /
   `connectorDisplayName` are already provider-neutral.

Refs: #1376, #1389, #1390.
