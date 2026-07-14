# Ops runbook: agent workload credentials — mint / rotate / revoke (RFC-0002 F5)

- **Status**: Signed off (gating ops runbook for P0-2 general availability)
- **Issue**: [#1262](https://github.com/kagura-ai/memory-cloud/issues/1262) — gating item F5 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: operators of production/self-hosted deployments; workspace owners
  provisioning credentials for agent workloads; implementers of P0-2 (agent-bound keys)
- **Depends on**: the owner-provisioned member-key flow (#1165, **shipped** —
  `backend/src/api/routes/member_credentials.py`); the Agent Registry & Context Bindings
  design (F1, #1258, `docs/design/agent-registry-and-bindings.md`) for the [P0-2] deltas only

This runbook covers the full operational lifecycle of a **workload credential** — the API key
an agent process authenticates with — as the system exists **today**. Everything in the
numbered procedures is shipped, verified behavior. Blocks marked **[P0-2]** describe what
changes when agent-bound keys (`api_keys.agent_id`) land; blocks marked **[P0-3]** describe
the bootstrap contract (F2, #1259); blocks marked **[P1]** describe the deferred DLP item.
None of the marked blocks are implemented yet.

## Scope and non-goals

**In scope**

- Minting, rotating, and revoking workload credentials via the owner-provisioned member-key
  flow (#1165): mandatory expiry, strict privilege downgrade, true one-time plaintext display.
- Expiry policy for workload credentials.
- The `enable_owner_key_member_management` deployment kill-switch: behavior, incident use,
  recovery.
- The normative rule that credentials MUST NOT be written as memory content, and the response
  procedure when the rule is violated.

**Out of scope**

- Agent registry / binding CRUD (F1, #1258) and the bootstrap contract (F2, #1259).
- Share keys and public-bound keys (`bound_context_id`) — separate, self-only surfaces;
  the owner-provisioned flow rejects them by design.
- OAuth device tokens (rejected on every credential-management endpoint; see below).
- Server-side secret-pattern screening (the P1 DLP item — stated here as a pointer only).
- First-class service accounts (mint without a pre-existing member) — deferred to a separate
  issue per RFC-0002.

## Workload identity model (current reality, restated from RFC-0002)

A workload identity today is the composition of two existing rows — no new tables:

1. A `workspace_members` row with role `member` or `viewer`, optionally narrowed by
   `allowed_context_ids` (enforced for member/viewer reads in
   `backend/src/services/permission_service.py`). The target identity must already exist as a
   workspace member before a credential can be minted for it.
2. An owner-minted `api_keys` row for that member with **mandatory expiry**. Keys have the
   form `kagura_<random>` (`API_KEY_PREFIX`, `backend/src/auth/api_keys.py`); only the SHA-256
   hash is stored for verification, and the non-secret 16-character `key_prefix` is the
   identifier used in audit rows and logs.

The three guarantees this runbook leans on, all implemented in
`backend/src/api/routes/member_credentials.py`:

- **Mandatory expiry** — `expires_days` is REQUIRED on the owner-provisioned mint (400 if
  omitted), bounded 1–3650 by the request schema (`CreateAPIKeyRequest.expires_days`,
  `Field(ge=1, le=3650)` in `backend/src/models/schemas.py`).
- **Strict privilege downgrade** — the target's workspace role must be `member` or `viewer`
  (`_require_downgrade_target`); an owner key can never mint for itself, another owner, or an
  admin. The same helper gates mint and revoke so the allowed-target set cannot diverge.
- **True one-time plaintext display** — the key is minted with `auto_hide_minutes=0` and then
  force-hidden via `apply_zero_knowledge_hide` (`backend/src/auth/api_keys.py`): `hidden_at`
  set, `visibility_expires_at` cleared, and the Fernet-encrypted at-rest copy
  (`plaintext_encrypted`) nulled. The plaintext exists **only** in the single 201 response.
  There is no re-reveal path; a fumbled response means revoke + re-mint.

> **[P0-2]** RFC-0002 adds exactly one column and one verify-time consequence:
> `api_keys.agent_id` (nullable FK → `agents.id`, `ON DELETE CASCADE`, mutually exclusive
> with `bound_context_id` via CHECK), and verification that rejects keys whose agent is
> `suspended`/`retired`. The mint service additionally validates
> `agents.workspace_id == api_keys.workspace_id`. The credential *lifecycle* in this runbook
> is unchanged by RFC-0002. Migration note: the RFC's sketch revision ids are stale — the
> repo's alembic head is `e62_1245_assign_mem_idx`; new migrations chain from the current
> head at implementation time.

## Conventions used in examples

All values below are placeholders. Never paste a real plaintext key, workspace id, or member
id into a ticket, chat, log, or memory.

```bash
export API=https://memory.example.com          # deployment base URL (dummy)
export WORKSPACE_ID=11111111-1111-1111-1111-111111111111
export TARGET_USER_ID=svc-agent-user           # pre-existing member/viewer
export OWNER_KEY=kagura_...                    # workspace-owner API key (never echo)
```

---

## 1. Preconditions (all procedures)

1. **Caller identity.** The programmatic path requires a **workspace-owner API key** on the
   path workspace. Authorization runs through the single chokepoint
   `authorize_workspace_management` (`backend/src/auth/programmatic_workspace_auth.py`):
   - OAuth bearer tokens → **403** always (`_reject_oauth`; every `kagura auth login` device
     token carries `memory:read memory:write`, so accepting it would silently turn every MCP
     token into a credential-management credential).
   - A workspace-scoped key bound to a *different* workspace → **uniform 404** (#963
     confinement; no existence probing).
   - Session principals take the web-UI path (unchanged #252 semantics; self-mint only for
     key creation).
2. **Kill-switch is ON.** `enable_owner_key_member_management` must be `true` (the default)
   for any programmatic member/credential management. See section 6.
3. **Target exists and is downgrade-eligible.** The target is already a workspace member with
   role `member` or `viewer`. Owners/admins are not valid targets (403).
4. **A secret sink is ready.** Decide *before* minting where the one-time plaintext goes:
   your deployment secret manager, or the in-platform zero-knowledge secret store
   (`secret_put` / `secret_get`, `backend/src/mcp_server/tools/secrets.py` — the server holds
   only opaque `age` ciphertext it cannot read). Never a memory (section 7).

---

## 2. Procedure: mint a workload credential

Endpoint: `POST /api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys`
(handler `create_api_key` → `_owner_provisioned_mint`,
`backend/src/api/routes/member_credentials.py`).

Guardrails, all enforced **before any write**:

| Check | Failure |
|---|---|
| Caller key is workspace **owner** on the path workspace | 403 (or uniform 404 if the key is bound to another workspace) |
| OAuth bearer principal | 403 |
| Target == caller (anti self-replication: a leaked owner key must not mint fresh keys for itself and defeat revocation) | 403 |
| Target role is `member`/`viewer` (`_require_downgrade_target`) | 403 |
| `expires_days` present (1–3650) | 400 if omitted; 422 outside schema bounds |
| `bound_context_id` absent (public-bound keys stay self-only) | 400 |
| Key `name` unique among the target's **active** keys in this workspace | 400 (`ValueError` from `APIKeyManager.create_key`) |

**Steps**

1. Confirm the target's membership and role (owner session UI, or
   `GET /api/v1/workspaces/$WORKSPACE_ID/members/$TARGET_USER_ID/credentials` — the response
   includes `target_user_role`).
2. Mint, piping the response directly to your secret sink — do not leave the plaintext in
   terminal scrollback or shell history:

   ```bash
   curl -sS -X POST \
     -H "Authorization: Bearer $OWNER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"name": "svc-ci-runner-2026-07", "expires_days": 90}' \
     "$API/api/v1/workspaces/$WORKSPACE_ID/members/$TARGET_USER_ID/credentials/api-keys" \
     | jq -r '.plaintext_key' | your-secret-manager put svc-ci-runner-2026-07
   ```

   The 201 response is the **only** appearance of `plaintext_key` (`is_visible: false`,
   `visibility_expires_at: null` — force-hidden at mint). Record the returned `id`,
   `key_prefix`, and `expires_at` in your inventory; they are non-secret.
3. Use a versioned name (`svc-<workload>-<yyyy-mm>`): name uniqueness is scoped to the
   target's active keys, so rotation (procedure 3) can mint the successor while the old key
   is still live only if the names differ.

**Verification**

1. `GET .../credentials` again: the new key is listed with `plaintext_key: null`,
   `is_visible: false`, and the expected `expires_at`. (Programmatic responses are *always*
   metadata-only — plaintext never appears in a response that may be logged.)
2. An `audit_logs` row exists (`AuditLog`, `backend/src/models/auth.py`) with
   `action = 'member_api_key_provisioned'`, `resource = 'api_key:<id>'`, and
   `user_metadata` carrying the acting owner key's prefix (`key_prefix`), the minted key's
   prefix (`minted_key_prefix`), and `expires_days`.
3. Smoke-test the new key with a low-privilege read as the workload would.

**Rollback**: revoke the freshly minted key (procedure 4). If the 201 response was lost
before the plaintext reached the secret sink, there is deliberately no recovery — revoke and
re-mint.

---

## 3. Procedure: rotate a workload credential

There is **no atomic rotate endpoint on the programmatic surface** — the
`.../credentials/api-key/regenerate` endpoint is session-only (web UI, 10-minute visibility
window) and is not part of the workload flow. Rotation is an overlapped mint + cutover +
revoke:

1. **Mint the successor** (procedure 2) under the next versioned name
   (e.g. `svc-ci-runner-2026-10`), with the same `expires_days` policy.
2. **Deploy** the new plaintext to the workload via your secret sink.
3. **Verify cutover**: the new key's `last_used_at` populates on `GET .../credentials`
   (updated on verification, write-throttled per #947) and workload traffic succeeds.
4. **Revoke the predecessor** (procedure 4).
5. **Verify** the old key now fails with 401.

Keep the overlap window short (target: hours, not days). Rotate at or before the key's
half-life so expiry (section 5) stays a backstop, never the routine cutover mechanism.

---

## 4. Procedure: revoke a workload credential

Endpoint: `DELETE /api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys/{key_id}`
(handler `delete_api_key_by_id`, `backend/src/api/routes/member_credentials.py`).

Semantics on the programmatic (owner-key) path:

- **Soft revoke**: `revoked_at` is set and the row is **retained for forensics**;
  `apply_zero_knowledge_hide` additionally drops any at-rest plaintext copy. This applies to
  *all* programmatic revocations, including an owner revoking their own key — a programmatic
  credential action is never a silent hard delete.
- **Audited before mutation**: the `member_api_key_revoked` audit row is written before the
  state change and commits atomically with it.
- **Already-revoked → uniform 404**: the forensic row's continued existence is not revealed;
  a repeat revoke cannot overwrite the original `revoked_at` or append duplicate audit rows.
  Treat a 404 on a key you know you revoked as success, not an error.
- **Cross-member revoke is downgrade-gated**: same `_require_downgrade_target` as mint.
- **Account-global keys are refused** (uniform 404) for cross-member revoke: a key with
  neither `workspace_id` nor `bound_context_id` may be the one the member uses in *other*
  workspaces, and the path-workspace owner has no proven authority over it. Escalate to the
  member themselves (session self-delete) or a platform admin.

**Steps**

1. Identify the `key_id` and `key_prefix` from `GET .../credentials` or the audit trail.
2. Revoke:

   ```bash
   curl -sS -X DELETE \
     -H "Authorization: Bearer $OWNER_KEY" \
     "$API/api/v1/workspaces/$WORKSPACE_ID/members/$TARGET_USER_ID/credentials/api-keys/$KEY_ID"
   # → {"status": "revoked", "key_id": ...}
   ```

**Verification**

1. An authenticated call with the revoked key returns 401. Revocation and expiry take effect
   at the next verification — `APIKeyManager.verify_key` (`backend/src/auth/api_keys.py`)
   consults the database row and returns `None` for revoked or expired keys; there is no
   token cache to purge.
2. `GET .../credentials` shows `revoked_at` set on the key.
3. An `audit_logs` row exists with `action = 'member_api_key_revoked'`,
   `user_metadata.revoked_key_prefix`, and `user_metadata.self_revoke`.

**Emergency notes**

- To stop a workload's access *now*, revoke its keys — the kill-switch (section 6) stops
  credential **management**, not credential **usage**.
- Session fallback (e.g. while the kill-switch is off): a workspace owner's session can
  delete a member's key via the legacy `DELETE .../credentials/api-key` endpoint, but that is
  a **hard delete of the most recent active key** — the forensic row is lost. Snapshot the
  key's metadata and the audit trail first if the deletion is incident-related.
- **[P0-2]** Agent-bound keys add a fleet kill: setting `agents.status = 'suspended'` rejects
  every key bound to that agent at verify time — one row update instead of revoking N keys.
  Until then, per-key revocation is the only lever.

---

## 5. Expiry policy for workload credentials

Mechanics (shipped):

- `expires_days` is **mandatory** on the owner-provisioned path (1–3650). Never-expiring
  workload keys cannot be minted; session self-mint keys keep their historical no-expiry
  behavior and are not workload credentials.
- Expiry is enforced at verification: an expired key fails auth with 401 immediately, with no
  grace period (`APIKeyManager.verify_key` returns `None` when `expires_at` has passed).
- `expires_at` is observable metadata on `GET .../credentials`
  (`MemberAPIKeyResponse.expires_at`, `backend/src/models/schemas.py`).
- There is **no server-side expiry-warning notification today**. Expiry tracking is an
  operator responsibility. A read-only inventory query for keys expiring within 14 days:

  ```sql
  SELECT key_prefix, name, user_id, workspace_id, expires_at
  FROM api_keys
  WHERE revoked_at IS NULL
    AND expires_at IS NOT NULL
    AND expires_at < now() + interval '14 days'
  ORDER BY expires_at;
  ```

Policy (normative for workload credentials on managed deployments; self-hosted operators may
tighten):

| Workload class | `expires_days` | Rotation cadence |
|---|---|---|
| One-off jobs, experiments, demos | 7–30 | none — let it expire |
| CI pipelines | 90 | rotate quarterly (procedure 3) |
| Long-running production agents | 90–365 | rotate at half-life |
| > 365 days | exception only — document the justification | mandatory scheduled rotation |

The API ceiling of 3650 days exists as a schema bound, not a recommendation; do not mint at
the ceiling. Expiry is the backstop for forgotten credentials — planned rotation (procedure
3) is the routine mechanism, so a key that reaches its `expires_at` in production indicates a
rotation-process failure worth investigating.

---

## 6. Kill-switch: `enable_owner_key_member_management`

**What it is.** A deployment-level boolean setting
(`backend/src/config/settings.py`, default `true`; set via the environment as
`ENABLE_OWNER_KEY_MEMBER_MANAGEMENT`). It exists because an owner's ordinary read/write API
key *also* carries member/credential-management power (#1164/#1165) — a stolen owner key
could otherwise mint member keys or add members. This is an interim control pending per-key
scopes.

**Behavior when `false`** (enforced at the single chokepoint
`authorize_workspace_management`, `backend/src/auth/programmatic_workspace_auth.py`; covered
by `backend/tests/auth/test_programmatic_workspace_auth.py`):

- Every **API-key principal** on the member-management (`workspaces.py`), invitation
  (`invitations.py`), and member-credential (`member_credentials.py`) surfaces is rejected
  with 403: *"Owner-API-key member management is disabled on this deployment. Use a
  workspace-owner session."* This includes the programmatic credential **view** path, not
  just mint/revoke.
- The rejection happens **before** the workspace-confinement lookup, uniformly for every
  key — a key bound elsewhere cannot distinguish "switch off" from #963 confinement, so the
  switch adds no existence-probing oracle.
- Each rejection emits the structured log event `workspace_mgmt_owner_key_disabled`.

**What it does NOT do:**

- It does not revoke or disable anything. Existing member keys keep working for memory
  operations — the switch gates the *management* surface, not the verify path.
- Session principals are untouched — but the session surface is **narrower** than the
  programmatic one. Per the session permission matrix (`check_can_manage`,
  `backend/src/services/member_credentials_service.py`), owners/admins retain
  regenerate/revoke/delete on other members' keys plus self-mint; there is **no
  cross-member mint** on the session path, and session self-mint does not accept
  `expires_days`. See section 6.2 for the full list of what a switched-off deployment
  gives up.

### 6.1 Incident procedure: suspected owner-key compromise

1. **Flip the switch**: set `ENABLE_OWNER_KEY_MEMBER_MANAGEMENT=false` on the backend
   environment and restart the backend. This immediately stops further programmatic minting,
   revoking, and member/invitation changes with *any* key, including the stolen one.
2. **Verify**: a programmatic mint attempt now returns the 403 above, and
   `workspace_mgmt_owner_key_disabled` appears in logs.
3. **Rotate the compromised owner key itself** using an owner **session** (web UI) — the
   session regenerate/self-mint paths are unaffected by the switch.
4. **Triage the audit trail** before deleting anything:

   ```sql
   SELECT created_at, action, resource, user_id, user_metadata
   FROM audit_logs
   WHERE action IN ('member_api_key_provisioned', 'member_api_key_revoked')
   ORDER BY created_at DESC;
   ```

   Pivot on `user_metadata->>'key_prefix'` (the acting key) to find every mutation performed
   with the stolen key, and on `minted_key_prefix` to enumerate attacker-minted keys. Also
   review member additions/invitations in the same window (same audit lane, #1164).
5. **Revoke attacker-minted keys.** Preferred: after step 3, re-enable the switch and use the
   *new* owner key for per-id **soft** revokes (procedure 4) — this retains forensic rows and
   writes audit rows. Alternative while the switch stays off: owner-session hard delete
   (section 4 emergency notes) — snapshot key metadata first, since hard delete destroys the
   row.
6. **Verify** each revoked key fails with 401.

### 6.2 Recovery procedure: re-enabling

1. Confirm the incident is closed and all involved owner keys have been rotated.
2. Set `ENABLE_OWNER_KEY_MEMBER_MANAGEMENT=true` (or remove the override — `true` is the
   default) and restart the backend.
3. Smoke-test the surface end-to-end with dummy values: mint a key for a test member with
   `expires_days: 1` (procedure 2), confirm the 201 + audit row, then revoke it (procedure 4)
   and confirm the 401 + audit row.

Deployments that never want programmatic member management may leave the switch off
permanently — but the web UI does **not** cover the full workload-credential lifecycle.
A permanently-off deployment gives up:

- **Cross-member first-mint.** The session path is strictly self-mint (`check_can_manage`
  grants owners/admins no `create` on others, and `get_or_create_credentials` performs no
  lazy initialization), so the *first* key for a service member can only be minted by that
  identity logging in and self-minting — an owner cannot provision it.
- **Mandatory expiry.** Session self-mint rejects `expires_days` with 400, so any
  session-minted workload key never expires; the section 5 expiry policy is unachievable
  via a web-UI-only lifecycle.
- **Force-hidden one-time display.** Session-minted keys use the timed visibility window
  (cleared later by the auto-hide sweeper, section 8) instead of being force-hidden at
  mint.
- **Soft revoke by key id.** The session fallback for another member's key is the hard
  delete of their most recent active key (section 4 emergency notes) — the forensic row is
  lost.
- **Audit rows.** `member_api_key_provisioned` / `member_api_key_revoked` are written only
  on the programmatic path; session actions on this surface are intentionally not audited
  there (section 8).

---

## 7. Rule: agent credentials MUST NOT be written as memory content

**Normative (restated from RFC-0002, Workload identity).** Agent credentials — and secret
material generally (`kagura_` keys, PEM blocks, JWTs, cloud credentials) — MUST NOT be
written as memory content: not via `remember` / `update_memory`, not in agent state
(`set_state`), not in context descriptions or usage guides, not in tags, not in files
uploaded as memory sources.

**Why this is a hard rule and not hygiene advice:** memories are a *retrieval surface*.
Anything stored as memory content is embedded, indexed, and replayed into model context by
`recall` and (P0-3) the bootstrap bundle — across sessions, and to every principal with read
access to the context. A secret written as a memory is a secret published to the context's
entire read set, with derived copies (vector points, caches) that outlive a single delete.

**Enforcement, by phase:**

- **P0 (now): write-path guidance.** The `remember`/`update_memory` tool descriptions state
  the rule. Compliance is on the writer.
- **[P0-3]: bootstrap restatement.** The bootstrap `instructions` block will restate the
  rule to every agent at session start (F2 design, #1259 — not yet implemented).
- **[P1]: server-side DLP.** Secret-pattern screening at the write path (`kagura_` key
  prefixes, PEM headers, JWT shapes) is the designated P1 item. Until it lands, nothing
  server-side stops a determined or confused writer — which is why the response procedure
  below assumes exposure.

**Where secrets go instead:** the deployment's secret manager, or the in-platform
zero-knowledge secret store (`secret_put` / `secret_get`,
`backend/src/mcp_server/tools/secrets.py`) — the server stores opaque `age` ciphertext it
cannot decrypt, values are never listed, and access is default-deny per grant.

### 7.1 Response procedure: a credential was written as a memory

1. **Treat the credential as compromised.** Revoke it (procedure 4) and mint a replacement
   (procedure 2) *before* cleaning up the memory — deletion does not un-expose it.
2. **Delete the memory** (`forget` on the offending memory id). Assume derived artifacts
   (vector index points, caches) take time to converge; the revocation in step 1 is what
   actually closes the exposure.
3. **Review access**: check who/what could have read the context in the exposure window
   (context membership, agent read sets, recall audit rows where available).
4. **Feed back**: remind the writing human or fix the writing agent's prompt/instructions;
   recurring violations are the signal to prioritize the [P1] DLP item on your deployment.

---

## 8. Audit and observability reference

All rows/events below are shipped and verifiable in the tree:

- **Audit rows** (`audit_logs`; `AuditLog` in `backend/src/models/auth.py`, written via
  `audit_programmatic_workspace_action` in `backend/src/auth/programmatic_workspace_auth.py`;
  session actions on this surface are intentionally not audited there):
  - `member_api_key_provisioned` — resource `api_key:<id>`; metadata: `workspace_id`,
    `target`, `via: api_key`, `key_prefix` (acting key), `minted_key_prefix`, `expires_days`.
  - `member_api_key_revoked` — resource `api_key:<id>`; metadata: `key_prefix` (acting key),
    `revoked_key_prefix`, `self_revoke`.
- **Structured log events**: `member_api_key_provisioned`, `member_api_key_revoked`,
  `api_key_created`, `api_key_deleted`, `workspace_mgmt_owner_key_disabled` (kill-switch),
  `workspace_mgmt_scoped_key_confined` (#963), `workspace_mgmt_oauth_denied`,
  `workspace_mgmt_unrecognized_principal`.
- **Auto-hide sweeper**: hourly job (`backend/src/tasks/credentials_tasks.py` →
  `backend/src/background/auto_hide_credentials.py`) clears plaintext visibility for
  session-minted keys after their window. Owner-provisioned keys are already force-hidden at
  mint and are not dependent on the sweeper.
- Suggested monitoring: alert on `member_api_key_provisioned` from unexpected actor
  `key_prefix` values; run the expiring-keys inventory query (section 5) on a schedule.

> **[P0-2]** Per-operation memory audit (MemoryAccessEvent: which agent read/wrote which
> memory) is RFC-0002 F3/P0 scope, not part of this runbook.

---

## Sign-off checklist (maps to #1262)

- [x] Mint / rotate / revoke via the owner-provisioned member-key flow (#1165): mandatory
      expiry, strict privilege downgrade, true one-time plaintext display (procedures 2–4;
      verified against `backend/src/api/routes/member_credentials.py` and
      `backend/src/auth/api_keys.py`)
- [x] Expiry-policy guidance for workload credentials (section 5: mandatory 1–3650, verify-time
      enforcement, no-notification caveat, inventory query, per-workload-class policy table)
- [x] `enable_owner_key_member_management` kill-switch behavior and recovery procedure
      (section 6; verified against `backend/src/auth/programmatic_workspace_auth.py` and
      `backend/src/config/settings.py`)
- [x] Explicit rule: agent credentials MUST NOT be written as memory content — write-path
      guidance now, server-side secret-pattern screening as the [P1] DLP item (section 7,
      including the violation-response procedure)
