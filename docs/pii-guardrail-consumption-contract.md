# PII Guardrail Consumption Contract (memory-cloud → ai-worker)

This document is the **written contract** for `pii_guardrail_config` — the
PII-scrubbing configuration that Kagura Memory Cloud stores on a connector and
that an external worker (e.g.
[`kagura-memory-ai-worker`](https://github.com/kagura-ai/kagura-memory-ai-worker))
**consumes** in its pre-compile stage to scrub personal data **before** chat
messages are ingested.

It is the sibling of
[`docs/connector-ingest-contract.md`](connector-ingest-contract.md): that doc
pins the *ingest* path; this one pins the *PII guardrail* the worker must apply
on the way in.

> **Scope.** This is the memory-cloud-side guidance deliverable of F6-d (#853),
> the final slice of epic #755. The worker-side pre-compile scrubbing
> implementation, the Discord/Teams connector smoke tests, and the epic-closing
> acceptance are tracked in
> [ai-worker#91](https://github.com/kagura-ai/kagura-memory-ai-worker/issues/91),
> **not** here. This doc does not close #755.

## Responsibility split (what lives where)

| Concern | Owner | Where |
|---|---|---|
| **Storage** of `pii_guardrail_config` | memory-cloud | `workspace_connectors.pii_guardrail_config` JSONB column (`models/resource.py:499`) |
| **Setting** the config | memory-cloud | `POST /api/v1/workspace-connectors` (`api/routes/workspace_connectors.py`) and the MCP `setup_connector` tool |
| **Interpreting + enforcing** the config (actual scrubbing) | **ai-worker** | pre-compile stage (ai-worker#91) |

**memory-cloud stores the config opaquely.** The only server-side validation is
"must be a JSON object" — `setup_connector` rejects a non-object with a
`validation_error` (`mcp_server/tools/resource.py:1097-1100`), and the REST
field is typed `dict[str, Any] | None`. **No key-level schema is enforced on the
server.** That makes *this document* the source of truth for the config shape:
the worker and whatever sets the config must agree here, because the database
will silently accept any object.

## Config lifecycle (current constraints)

- **Write-once at provision time.** `pii_guardrail_config` is accepted only by
  the connector-provisioning path. There is **no `PUT`/`PATCH` update endpoint**
  for it today, so a connector's guardrail config is fixed at setup. The
  `config_version` column exists (defaults to `1`) but there is no server route
  that bumps it yet. A connector that needs a different guardrail config must be
  re-provisioned, or an update path must be added first (track separately).
- **`null` is allowed.** The column is nullable and the field defaults to
  `None`. A connector can be provisioned with no guardrail config — see
  [Null config](#null-config-fail-closed) for the contract the worker must honor
  in that case.
- **`connector_type` is one of `slack` / `discord` / `teams`**
  (`services/connector_provisioning.py:30`). The guardrail contract is identical
  across all three; only the message-extraction differs (out of scope here).

## The hard invariant

> **PII must be scrubbed by the worker's pre-compile stage before the event is
> POSTed to the ingest endpoint.** memory-cloud must never receive raw,
> un-scrubbed PII in `resource_events.payload`.

memory-cloud has **no server-side PII filter** on the ingest path — once an
event reaches `POST /api/v1/resources/{resource_id}/events`, its `payload` is
persisted and projected into searchable text as-is. The guardrail is therefore a
**worker-side precondition of ingest**, not a server-side gate. Getting this
ordering wrong means raw PII lands in the store and in recall/explore results.

## Agreed config schema

Because the server stores the object opaquely, the following is the **agreed
shape** the worker consumes. Producers (setup UI / API callers) MUST emit this
shape; the worker MUST treat unknown keys as a hard error or ignore-with-warning
(its choice, but documented), never as silent pass-through.

```json
{
  "enabled": true,
  "detectors": ["EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "PERSON"],
  "redaction": "mask",
  "locale": "en",
  "fail_closed": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `enabled` | bool | Master switch. `false` = the worker performs **no** scrubbing (see Null config for the difference from an absent config). |
| `detectors` | string[] | Entity types the deterministic pre-LLM stage must detect (Presidio-style recognizer names). Empty array with `enabled: true` is a misconfiguration — the worker should reject it. |
| `redaction` | `"mask"` \| `"hash"` \| `"remove"` | How a matched span is rewritten. `mask` → fixed token (e.g. `<EMAIL>`); `hash` → stable pseudonym; `remove` → deleted. |
| `locale` | string | Recognizer locale (`en` / `ja` / …). Drives locale-specific detectors. |
| `fail_closed` | bool | If detection errors (model load failure, timeout), `true` = **drop the event** (do not ingest); `false` = ingest with a logged warning. Default to `true` for a guardrail. |

This schema is a **floor, not a ceiling** — additional keys may be added by
amending this doc. The worker pins its accepted version against this file.

## Defense-in-depth the worker must implement

A single redaction pass leaks. The worker's pre-compile stage MUST apply the
mandatory multi-stage pipeline:

1. **Pre-LLM deterministic redaction** — Presidio-style recognizers over the raw
   message text, driven by `detectors` + `locale`. Runs **before** any LLM call.
2. **In-prompt guardrail** — when the message is summarized/compiled by an LLM,
   the prompt instructs the model not to surface PII.
3. **Post-summary scan** — re-scan the compiled output before it becomes the
   ingest `payload`; a leak here is the last line before `resource_events`.

If the connector's content is ever used for dataset/training export, a **fourth
export-time rescan** is mandatory. (Export is out of scope for #853 but the
contract is stated here so it is not rediscovered later.)

## Null config (fail-closed)

`pii_guardrail_config = null` (column NULL / field absent) is **distinct** from
`{"enabled": false}`:

- `{"enabled": false}` is an **explicit operator decision** to ingest without
  scrubbing — the worker honors it and ingests raw.
- `null` is an **unconfigured** connector. The worker MUST treat an unconfigured
  guardrail as **fail-closed**: do **not** ingest chat content for a connector
  whose guardrail was never configured. Silently ingesting raw PII because the
  config was forgotten is the worst-case outcome a guardrail exists to prevent.

This asymmetry (`false` = opt-out, `null` = block) is the contract; memory-cloud
cannot enforce it (it stores `null` happily), so the worker owns it.

## End-to-end flow

```
operator → POST /api/v1/workspace-connectors { pii_guardrail_config: {...} }
             └─ stored opaquely in workspace_connectors.pii_guardrail_config (JSONB)

worker (pre-compile stage, ai-worker#91)
  └─ reads pii_guardrail_config for the connector
       └─ stage 1: deterministic redaction (detectors + locale)
            └─ stage 2: in-prompt guardrail during compile
                 └─ stage 3: post-summary rescan
                      └─ ONLY THEN: ResourceClient.ingest_event(payload=<scrubbed>)
                           └─ POST /api/v1/resources/{slug}/events  (see connector-ingest-contract.md)
```

## Known pitfalls

- **The server does not validate the schema.** A typo'd key (`detector` vs
  `detectors`) is accepted and stored. The worker must validate the config it
  reads and fail loudly on a shape it does not recognize.
- **Config is write-once.** No update endpoint exists; do not assume a connector's
  guardrail can be tightened in place without re-provisioning.
- **`null` ≠ `{"enabled": false}`.** See [Null config](#null-config-fail-closed).
- **Scrub before ingest, never after.** There is no server-side PII filter; the
  ingest endpoint persists `payload` verbatim.

## Related

- Epic #755 (F6) — chat ingest reuses Resource Foundation
- #850 (F6-a) — `workspace_connectors` schema incl. `pii_guardrail_config`
- #852 (F6-c) — connector ingest contract ([`connector-ingest-contract.md`](connector-ingest-contract.md))
- #853 (F6-d) — this guidance doc; worker-side scrub + Discord/Teams smoke + #755 close tracked in ai-worker#91
- [`docs/connector-ingest-contract.md`](connector-ingest-contract.md) — the ingest path this guardrail precedes
