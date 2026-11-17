# Setup-only billing handoff for a registered sync service

Issue [#2878](https://github.com/kagura-ai/memory-cloud/issues/2878), milestone
v0.167.0. The shared machine contract is
[sync-setup-handoff-v1.json](../../backend/tests/fixtures/sync-setup-handoff-v1.json).
Public/PAY consumers must implement that same strict version before rollout.

## Authority boundary

A current canonical workspace owner can enter payment's sync-add-on **setup**
flow from Public, save a payment method and explicitly agree to recurring terms
before brand review. This credential creates no brand approval, KMC integration
consent, subscription, paid entitlement, resource, Context or endpoint. It grants
no generic billing portal or Max-change access.

The legacy `POST /api/v1/billing/handoff` stays session-only and unchanged.
Do not give Public a KMC session cookie, personal API key or billing publisher
secret. The new token has a different fixed audience/type/purpose and no generic
`role: owner`. Older generic payment verifiers must reject its audience too.

## HTTP and signed contract

`POST /api/v1/workspaces/{workspace_id}/sync-billing-handoffs` accepts exactly the
fixture's `application_id` UUID, strict integer `application_revision`
(1–2147483646), and `input_digest` (64 lowercase hex characters). It accepts no
query, price/customer/card/plan/service/target override or browser review flag.
Reject duplicate JSON/header ambiguity, unknown fields and non-UTF8 JSON.
Body maximum is4096bytes with a5s deadline. Namespace/rate checks precede DB;
body reading precedes the first identity SQL and any authority transaction.
Invalid input is400; oversized input413. Every outcome is no-store with fixed,
non-reflective errors.

Only the isolated live OIDC access namespace and explicit `integrations:manage`
scope are accepted. Ordinary login, sessions, personal/API/Resource Tokens and
workers cannot mint. Scope alone is not ownership. Canonical owner and OWNER
membership must both match; the authenticated browser selects its registered
enabled service. Account/workspace/membership, token/grant/client and registration
are fenced through signing and final COMMIT. Competing authority changes are
retryable409. Final-boundary expiry returns no credential and rolls back audit.
No remote payment call runs under those locks.

JOSE fixes `alg: Ed25519`, operator-managed `kid` and
`typ: kagura-sync-setup+jwt`. Audience is exactly `kagura-billing-sync-setup`,
purpose exactly `sync_addon_setup`. Issuer is the configured billing-handoff
issuer. Refuse a generic-audience configuration that collides with the setup
audience. No generic billing token is minted as an intermediate value.

Claims are exactly the fixture's required set: issuer/audience/purpose, canonical
subject/workspace, registered service/browser, application UUID/revision/digest,
current ownership `epoch`, UTC integer `iat`/`exp` and random single-use `jti`.
The application tuple is an owner-approved **setup context**, not an assertion
that KMC has inspected Public's review database.

Lifetime is the shorter of60seconds, configured legacy handoff TTL if shorter,
and the original/reloaded OIDC token/grant deadline. Integer expiry never rounds
past that deadline. A new JTI does not refresh an earlier credential or consent.

Response200 has `contract_version: sync-setup-handoff-v1`,
`token_type: sync_addon_setup`, opaque `token`, `kid`, `jti`, UTC `expires_at`
and `url`. URL contains only the configured HTTPS payment origin, fixed
`/addons/sync/enter` path and a single `t` token parameter.

## Required consumers

Public uses a separate current-owner/session/selection/application-bound
Code+S256PKCE flow for `openid profile integrations:manage`. Ordinary login stays
`openid profile workspaces:read`. Verify the same human, signed issuer/audience,
nonce/state and exact elevated scope; keep tokens ephemeral/server-only, use a
distinct encrypted flow/callback, preserve the normal session and revoke the
separate refresh grant afterwards. Validate the fixed response/URL binding.

Payment needs a **separate setup-only entry point**. Verify allowlisted
Ed25519 key/kid, exact algorithm/type/issuer/audience/purpose, claim bounds,
original timing and every expected application/service/workspace binding.
Atomically consume JTI before establishing only a setup-scoped session. Strip
the token using a fixed redirect and no-referrer. Never establish generic portal
or Max-change authority from this token.

Payment independently checks current canonical ownership and Public's current
application tuple before saving explicit recurring consent. Store the original
epoch/revision/digest with exact text/hash, price and finite consent deadline.
Old PAY-01 rows lacking these facts require fresh explicit consent; do not
backfill today's authority into an older agreement.

Transfer-away-and-back leaves the old handoff/consent at its original epoch.
A current owner may explicitly mint a fresh setup credential with the new epoch;
this does not update or validate an old consent. Before charge and activation,
compare the saved tuple/epoch/subject to current Public review and exact KMC
consent validation. Pre-charge validation is not a reservation. Success pages,
setup completion and this credential never grant paid rights or create targets.

Those consumer behaviors belong to
[PUB-09](https://github.com/kagura-ai/kagura-public/issues/39) and
[PAY-02](https://github.com/kagura-ai/kagura-payment/issues/201).
Issuer tests alone do not establish deployed integration.

## Rollout and audit

The route follows default-off `SYNC_INTEGRATIONS_ENABLED` and isolated OIDC
enablement. Register the service/browser and exact separate Public redirect URI.
Configure protected `BILLING_HANDOFF_SIGNING_KEY`, `BILLING_HANDOFF_KEY_ID`,
issuer/audience and `PAYMENT_PUBLIC_BASE_URL`. Payment base must be an ASCII
HTTPS origin without credentials, path, query or fragment. Missing/invalid
signing or landing configuration fails503. Keep private signing material in KMC.

The new path has a distinct `sync-setup-handoff` OIDC rate bucket. Invalid rate
configuration/store failure fails503 before DB/crypto; over budget is429.
It does not reuse the legacy generic handoff's fail-open limiter.

Successful issuance appends `sync.setup_handoff.created` through the existing
erasure-aware audit boundary with scalar workspace/service/operation identity
and authenticated actor. No token, source body, digest or card data is audited.
Errors/logs contain no token/code/subject/body or native crypto/connection text.
Existing sync audit retention applies.

Coordinate consumer deployment and test key rotation, JTI replay, expiry,
transfer and outages before enabling setup. This work performs no registration,
real Stripe operation, merge, release or deployment.
