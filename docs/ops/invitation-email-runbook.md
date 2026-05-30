# Workspace Invitation Email Runbook (Issue #654)

Creating a workspace invitation (`POST /api/v1/workspaces/{id}/invitations`)
dispatches an invitation email to the invitee. This is a **courtesy
notification**: the invitation row is the source of truth, so a failed email
never blocks or rolls back the invitation. The admin who created the invite
always receives the accept URL in the API response and can hand-deliver it if
needed.

## Providers

Delivery is governed by `EMAIL_PROVIDER` (shared with the account-erasure
emails — see `docs/ops/erasure-runbook.md`):

| `EMAIL_PROVIDER` | Behavior |
|---|---|
| `logging` (default) | No email is sent. A structured log line `workspace_invitation_email` is written with `to_email`, `workspace_name`, `expires_at`, `template`. The **accept URL / token is NOT logged** (it is the credential). The admin delivers the URL from the API response. |
| `resend` | The email ships via Resend over HTTPS. The accept URL goes to the invitee inbox only — never to local logs. |

## Flipping to Resend

No invitation-specific configuration exists — the invitation email reuses the
`#478` Resend wiring. To enable real delivery (see `.env.example` lines 71–75):

1. Verify a sending domain in Resend and set `RESEND_FROM_EMAIL` to an address
   on that domain.
2. Set `RESEND_API_KEY` (scope it to the single sending domain).
3. **DPA prerequisite** — accept the Resend Data Processing Addendum
   (<https://resend.com/legal/dpa>) and record it in
   `RESEND_DPA_ACCEPTED_AT` (ISO-8601 UTC). The backend refuses to boot with
   `EMAIL_PROVIDER=resend` unless `RESEND_FROM_EMAIL` is set
   (`Settings._validate_resend_config`).
4. Set `EMAIL_PROVIDER=resend` and restart the API.
5. Ensure `FRONTEND_URL` is the deployed origin — the absolute accept URL is
   built as `{FRONTEND_URL}/invite/{token}` (`build_invitation_url` in
   `services/invitation_service.py`, shared with the API response so the two
   can never drift).

## Sub-processor disclosure

Resend is a personal-data sub-processor (it receives the invitee email address
and the invitation link). Its disclosure lives in the **Privacy Policy**
sub-processor list (same entry added for `#478` / `#379`); no new disclosure is
required for invitations — confirm the existing Resend entry is published
before flipping `EMAIL_PROVIDER=resend` in production.

## Verifying

- **Logging mode**: create an invitation and grep production logs for
  `workspace_invitation_email`. Confirm the line carries `email_dispatch_required=true`
  and does **not** contain the token or accept URL.
- **Resend mode** (manual pre-flip smoke): with a Resend sandbox key, create an
  invitation to an address you control and confirm receipt + that the
  `{FRONTEND_URL}/invite/{token}` link resolves. CI uses mocks; this smoke is a
  manual gate (see the external-integration smoke pattern).
- A Resend SDK failure (network / 4xx / 5xx) logs `workspace_invitation_email_failed`
  with only `error_type` + `status_code` (no body, no URL) and returns `False`;
  the invitation row still commits.

## Out of scope (deferred)

HTML/branded template, Resend webhooks (delivery/bounce), reminder emails,
localized copy, and per-recipient rate limiting are tracked separately.
