# Read-only resume outcome recovery

Issue [#2875](https://github.com/kagura-ai/memory-cloud/issues/2875).
This extends [human feed management](feed-managed-sync.md) with an explicitly
result-only path. No new migration, registration, scope or rollout flag is added.

`POST /api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/management/resume/receipt`
accepts bounded, strict JSON with exactly:

```json
{
  "operation_id": "00000000-0000-4000-8000-000000000001",
  "preview_id": "00000000-0000-4000-8000-000000000002",
  "diff_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "original_subject": "original-confirming-human"
}
```

The caller needs the registered browser client's live KMC OIDC human
`integrations:manage` token and current workspace/Context Editor reach. Ordinary
login scope, Viewer, API keys and worker credentials cannot access this API.
The current Editor can reconcile an earlier Editor's operation: this is retained
workspace operational metadata, without impersonating the original actor or
renewing an expired grant, request, candidate or preview. Current authority loss
still denies access. Installation revocation/expiry, subscription loss or a
capacity hold alone do not remove metadata access when current reach remains.

Every successful response contains exactly `contract_version`, `workspace_id`,
`installation_id`, `operation_id`, `preview_id`, `diff_hash`, `original_subject`,
`result`, `receipt`, and UTC `as_of`. The contract version is
`sync-resume-receipt-v1`; identifiers/body/subject echo the validated request.

| Result | Receipt | Meaning at `as_of` |
| --- | --- | --- |
| `found` | Saved `mode=feed`, epoch, generation, stop_event_id, reason=null | Exact immutable resume action, request digest and original actor matched |
| `not_found` | null | No operation with that installation/operation ID existed under the transition fence |
| `mismatch` | null | That ID exists but its action, original body or actor differs; no unrelated metadata is disclosed |

The receipt is historical. It neither describes current mode nor authorizes
adoption, a new publication or a retry. Callers must independently verify current
target/source/authority before changing their own binding. Absence is also an
observation: it does not prove that a still-live original confirmation cannot
commit later. Safe abandonment must additionally prove the original publication
authority is no longer usable, using its original finite KMC deadlines or an
acknowledged revocation; never extend the original authorization window.

The lookup shares current identity/membership and NOWAIT workspace/installation
locks with management transitions and validates the retained ownership binding.
The Context metadata and, for private linked-owner reach, both actual identity-link
rows remain locked through the final reach check and observation commit. Contention
is refused without waiting; lookup does not take the global link mutation mutex.
It never calls confirmation, paid publication, enrollment or allocation; content,
preview, candidate, request, receipt, generation, quota and indexing state remain
unchanged. No original preview or credential is required to observe a receipt.
Audit of the HTTP access remains the existing control-plane behavior.

Authentication, authorization, rollout, contention, validation and infrastructure
failures remain private errors, never successful `not_found`. In particular,
HTTP409 from this API or `/resume/confirm` is not evidence of original absence.
All responses include `Cache-Control: no-store` and `Pragma: no-cache`.

This implementation does not merge prerequisites, enable any feature, deploy an
application, establish a live Public connection or permit billing actions.
