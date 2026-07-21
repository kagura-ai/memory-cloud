# Slack Public Distribution — operator runbook (#1429)

**Status:** the actual activation is a manual action in the Slack app admin
console (api.slack.com). It is **not** a code change and cannot be performed by
CI or the app itself. This runbook is the checklist for the operator who owns
the shared Kagura Slack app.

## Why this is needed

The shared Kagura Slack app is created inside one Slack workspace (its "home"
/ development workspace — the `kagura-ai` team). Until **Public Distribution**
is activated, Slack only allows that app to be installed into its home
workspace. Any external tenant that clicks **Connect Slack** (the OAuth flow)
to install into *their* workspace gets:

```
Error: invalid_team_for_non_distributed_app
```

This is a Slack-side constraint, not a Kagura bug. Its blast radius is **every
external tenant**, not one workspace: while the app is non-distributed, only the
home-workspace team can OAuth-connect Slack. Hosted-SaaS onboarding (#1426)
assumes this is activated.

Scope: this affects the **hosted SaaS** shared app only. **Self-host is out of
scope** — a self-host operator brings their own Slack app (installed into their
own workspace, which is a non-distributed install into the app's *home* team and
therefore allowed) and registers it via the BYO "link existing app" form +
worker app identity. See [worker app identities](#byo-alternative-self-host).

## Preconditions (Slack's distribution checklist)

In **api.slack.com/apps → {the Kagura app} → Manage Distribution**, Slack gates
"Activate Public Distribution" on:

- [ ] **Redirect URL(s)** configured under *OAuth & Permissions* (the production
      OAuth callback; must be HTTPS and exact-match).
- [ ] **No hardcoded information in install links** — the "Add to Slack" /
      install URL must not embed a static `team` parameter.
- [ ] At least one **bot scope** requested (Kagura uses `channels:history`,
      `channels:read`, `groups:history`, `chat:*` per the OAuth consent screen).
- [ ] App **name, icon, and short/long description** present (Slack requires the
      basic listing metadata even without App Directory submission).

Activating distribution is sufficient for OAuth-into-any-workspace. Submitting
to the **App Directory** (public listing + Slack review) is a *separate,
optional* step and is **not** required for external tenants to connect.

## Activation steps

1. Open **api.slack.com/apps** → select the shared Kagura app.
2. **Manage Distribution** → work down the checklist until every item is green.
3. Click **Activate Public Distribution**.
4. Confirm the "Add to Slack" install URL contains **no** `team=` parameter.

## Verification

- [ ] From a Slack workspace **other than** the app's home team, run the
      **Connect Slack** OAuth flow in Kagura and confirm it completes (no
      `invalid_team_for_non_distributed_app`).
- [ ] Confirm a connector is created bound to that external team, and the
      `oauth.v2.access` callback populated `team.id` + bot token server-side
      (the tenant never pastes `xoxb-…` / team id manually on this path).

## Rollback / notes

- Public Distribution can be **deactivated** in the same Manage Distribution
  screen; existing installations keep working, but new external installs revert
  to the `invalid_team_for_non_distributed_app` error.
- The **signing secret** is unaffected by distribution — it is an app-level
  secret copied once from *Basic Information* into the worker app identity
  (`/admin/worker-apps`), and is **not** returned by OAuth. OAuth only yields the
  per-installation `team.id` + bot token.

## BYO alternative (self-host)

If activating distribution is not desired, an external party can instead bring
their **own** Slack app:

1. Create a Slack app owned by their workspace, add the bot scopes, install it
   into their own workspace (allowed: non-distributed install into the app's
   home team), and copy the `xoxb-…` bot token.
2. A **system admin** registers that app's signing secret as a worker app
   identity (`app_key ≠ default`) via `/admin/worker-apps` (instance-admin
   gated — see the worker app identities screen). On shared SaaS this is a
   per-tenant operator action; on self-host the operator is the tenant.
3. Use Kagura's **link existing Slack app** form (team id + bot token) — no
   OAuth redirect, so `invalid_team_for_non_distributed_app` never occurs.
