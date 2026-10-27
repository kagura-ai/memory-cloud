---
description: Manage admin account (create, reset password, delete, MFA)
---

Manage the password admin account for Kagura Memory Cloud.

## Available Actions

Ask the user which action they want, or infer from context:

### 1. Create Admin

Run interactively (requires user input for password + MFA):

```
! cd backend && python -m src.cli.create_admin
```

Creates: admin user, workspace, API key, `.mcp.json`

### 2. Reset Password / MFA

Run interactively:

```
! cd backend && python -m src.cli.reset_password
```

Options: reset password only, disable MFA only, or both. Can re-enable MFA after disabling.

### 3. Delete Admin

Run interactively:

```
! cd backend && python -m src.cli.delete_admin
```

If solo admin: deletes user + workspace + API keys.
If other users exist: deletes admin only, preserves workspaces. Workspaces the
admin owns go to `--transfer-to USER` (user_id, login_id or email); where other
users are members it must be one of them, a workspace only the admin is in is
handed over regardless. Without it the command lists them and stops (#2371).

After deletion, run create_admin to set up again.

## Notes

- Docker API container must be running (CLI reads `API_KEY_SECRET` from it)
- All commands must be run from the `backend/` directory
- MFA is enabled by default during admin creation (recommended)
- Admin login uses arbitrary login ID (not email)
- Admin email is set to `{login_id}@local` (not a real email)
