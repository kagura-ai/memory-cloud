"""Non-interactive admin seed for the authed-a11y CI lane (#840).

Unlike ``create_admin`` (interactive: prompts for login id / password / MFA, and
also provisions an API key + embedding provider + ``.mcp.json``), this script
creates the MINIMAL fixture the authenticated a11y specs need:

- a password admin user (``auth_method="password"``, **no MFA**) whose
  ``login_id`` / password come from ``E2E_ADMIN_LOGIN_ID`` / ``E2E_ADMIN_PASSWORD``,
- a personal Pro-plan workspace + owner membership (so ``/workspace/dashboard``
  renders for the seeded admin).

It does NOT create an API key or configure an embedding provider, so it needs
neither ``API_KEY_SECRET`` nor a provider — only ``DATABASE_URL`` (read by
``cli.db.get_sync_database_url``). It is idempotent: if a password admin with the
given ``login_id`` already exists it exits 0 without changes, so re-runs in a
re-used CI database are safe.

Usage (CI):
    E2E_ADMIN_LOGIN_ID=e2e-admin E2E_ADMIN_PASSWORD='...' \\
        python -m src.cli.seed_e2e_admin
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.password import hash_password  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from models.auth import User, Workspace, WorkspaceMember  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


def seed_e2e_admin() -> None:
    login_id = os.environ.get("E2E_ADMIN_LOGIN_ID", "").strip()
    password = os.environ.get("E2E_ADMIN_PASSWORD", "")
    if not login_id or not password:
        print("✗ E2E_ADMIN_LOGIN_ID and E2E_ADMIN_PASSWORD must be set.")
        sys.exit(1)

    engine = create_engine(get_sync_database_url())
    try:
        with Session(engine) as db:
            existing = db.execute(
                select(func.count())
                .select_from(User)
                .where(User.login_id == login_id, User.auth_method == "password")
            ).scalar()
            if existing and existing > 0:
                print(f"✓ Password admin '{login_id}' already exists — nothing to do.")
                return

            is_first_admin = (
                db.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.auth_method == "password", User.role == "admin")
                ).scalar()
                or 0
            ) == 0

            admin = User(
                login_id=login_id,
                email=f"{login_id}@local",
                user_id=f"local:{login_id}",
                name=login_id,
                role="admin",
                auth_method="password",
                password_hash=hash_password(password),
                totp_secret=None,
                totp_enabled=False,
                is_initial_admin=is_first_admin,
                last_login_at=utcnow(),
            )
            db.add(admin)
            db.flush()

            workspace = Workspace(
                name="E2E Personal Workspace",
                owner_user_id=admin.user_id,
                plan_name="pro",
            )
            db.add(workspace)
            db.flush()
            db.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=admin.user_id,
                    role="owner",
                )
            )
            admin.current_workspace_id = workspace.id
            db.commit()
            print(f"✓ Seeded e2e admin '{login_id}' + Pro workspace {workspace.id}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    seed_e2e_admin()
