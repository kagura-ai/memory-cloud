"""Admin-configurable signup gate service (Issue #358 Phase 1).

Sits in front of the OAuth callback's user-creation step. When the gate is
disabled (default OSS behavior), delegates to the existing env-based
``_check_registration_allowed`` so nothing changes for self-hosters who
don't need allowlists. When enabled, applies the configured mode — Phase 1
implements ``manual`` only; ``github_sponsors`` / ``both`` raise
NotImplementedError and are covered by the Phase 2 follow-up issue.
"""

import os
from typing import Literal
from uuid import UUID

from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from models.auth import User
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from utils.github_user import resolve_github_user_id
from utils.logger import get_logger

logger = get_logger(__name__)

SignupGateMode = Literal["manual", "github_sponsors", "both"]


async def check_signup_access(
    *,
    provider: Literal["github", "google"],
    oauth_sub: str,
    email: str,
    username: str | None = None,
) -> RedirectResponse | None:
    """Run the signup gate for an OAuth callback.

    Convenience wrapper that opens a DB session, instantiates the service,
    and delegates to ``check_access``. Keeps OAuth callback handlers free of
    the ``async for db in get_db()`` boilerplate and the inline service
    import (which exists to break a circular dependency with auth.py).

    Returns None when signup is allowed, a RedirectResponse when blocked.
    """
    async for db in get_db():
        gate = SignupGateService(db)
        return await gate.check_access(
            provider=provider,
            oauth_sub=oauth_sub,
            email=email,
            username=username,
        )
    return None


class SignupGateService:
    """Gatekeeper for OAuth signup callbacks + admin CRUD for allowlist."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # OAuth callback gate
    # ------------------------------------------------------------------

    async def check_access(
        self,
        *,
        provider: Literal["github", "google"],
        oauth_sub: str,
        email: str,
        username: str | None,
    ) -> RedirectResponse | None:
        """Return None when signup is allowed, RedirectResponse when blocked.

        Phase 1 rules (top-down):
        1. ``enabled=false`` → delegate to legacy env-based gate (OSS path).
        2. ``provider == "google"`` → pass through (Google-side controls signup;
           backend is a no-op for Google in Phase 1).
        3. Existing user (users row for this email) → allowed (login, not signup).
        4. First user (users table empty) → allowed (initial admin bootstrap).
        5. ``mode == "manual"`` → allowed iff ``github_user_id`` is on the
           allowlist with ``state='active'``.
        6. ``mode in ("github_sponsors", "both")`` → NotImplementedError
           (Phase 2).
        """
        config = await self._load_config()

        if not config.enabled:
            return await self._legacy_check(email)

        # Google's own OAuth + workspace configuration decides who can sign up
        # on that side — having two places (backend allowlist + Google console)
        # gate the same flow would just confuse admins. Backend stays neutral
        # for provider=google in Phase 1.
        if provider == "google":
            return None

        if await self._is_existing_user(email):
            return None

        if await self._is_first_user():
            return None

        if config.mode == "manual":
            if await self._is_allowlisted(oauth_sub):
                return None
            logger.info(
                "signup_blocked",
                provider=provider,
                github_user_id=oauth_sub,
                username=username,
            )
            return self._blocked_response()

        if config.mode in ("github_sponsors", "both"):
            raise NotImplementedError(
                f"Signup gate mode '{config.mode}' is reserved for Phase 2 "
                "(Issue #358 follow-up). In Phase 1 the admin UI disables "
                "selecting these modes; reaching this branch indicates a "
                "direct DB edit."
            )

        return self._blocked_response()

    # ------------------------------------------------------------------
    # Admin: config
    # ------------------------------------------------------------------

    async def get_config(self) -> SignupGateConfig:
        return await self._load_config()

    async def update_config(self, *, enabled: bool, mode: SignupGateMode) -> SignupGateConfig:
        config = await self._load_config()
        config.enabled = enabled
        config.mode = mode
        await self.db.commit()
        await self.db.refresh(config)
        return config

    # ------------------------------------------------------------------
    # Admin: allowlist
    # ------------------------------------------------------------------

    async def list_allowlist(self) -> list[SignupAllowlistEntry]:
        result = await self.db.execute(
            select(SignupAllowlistEntry).order_by(SignupAllowlistEntry.created_at.desc())
        )
        return list(result.scalars().all())

    async def add_to_allowlist(
        self, *, github_username: str, added_by_user_id: str
    ) -> SignupAllowlistEntry:
        """Resolve username to canonical ID via GitHub API and insert.

        Raises:
            GitHubUserNotFound: The username does not exist on GitHub.
            ValueError: The (github_user_id, source='manual') pair already exists.
        """
        user_id, canonical_login = await resolve_github_user_id(github_username)

        existing = await self.db.execute(
            select(SignupAllowlistEntry).where(
                SignupAllowlistEntry.github_user_id == user_id,
                SignupAllowlistEntry.source == "manual",
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"User '{canonical_login}' is already on the manual allowlist")

        entry = SignupAllowlistEntry(
            github_user_id=user_id,
            github_username=canonical_login,
            source="manual",
            state="active",
            added_by_user_id=added_by_user_id,
        )
        self.db.add(entry)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            # Race: a concurrent add between our SELECT duplicate-check above
            # and this COMMIT passed the unique index check first. Convert to
            # the same ValueError the pre-check raises so callers see one
            # consistent "duplicate" signal instead of a 500.
            await self.db.rollback()
            raise ValueError(
                f"User '{canonical_login}' is already on the manual allowlist"
            ) from exc
        await self.db.refresh(entry)
        logger.info(
            "signup_allowlist_added",
            github_username=canonical_login,
            github_user_id=user_id,
            added_by=added_by_user_id,
        )
        return entry

    async def remove_from_allowlist(self, entry_id: UUID) -> None:
        entry = await self.db.get(SignupAllowlistEntry, entry_id)
        if entry is None:
            raise ValueError(f"Allowlist entry {entry_id} not found")
        username = entry.github_username
        await self.db.delete(entry)
        await self.db.commit()
        logger.info("signup_allowlist_removed", github_username=username)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _load_config(self) -> SignupGateConfig:
        """Return the singleton config row, creating it if somehow missing.

        The migration seeds the row, but on rare upgrade paths (manual DB
        tinkering, restore from partial backup) it can go missing; creating
        on demand keeps the admin UI functional rather than 500-ing.
        """
        result = await self.db.execute(select(SignupGateConfig).where(SignupGateConfig.id == 1))
        config = result.scalar_one_or_none()
        if config is None:
            config = SignupGateConfig(id=1, enabled=False, mode="manual")
            self.db.add(config)
            try:
                await self.db.commit()
            except IntegrityError:
                # Race: a concurrent caller inserted the singleton row between
                # our SELECT above and this COMMIT. Roll back our duplicate
                # insert and re-SELECT the row that now exists.
                await self.db.rollback()
                result = await self.db.execute(
                    select(SignupGateConfig).where(SignupGateConfig.id == 1)
                )
                config = result.scalar_one()
            else:
                await self.db.refresh(config)
        return config

    async def _is_existing_user(self, email: str) -> bool:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none() is not None

    async def _is_first_user(self) -> bool:
        result = await self.db.execute(select(func.count()).select_from(User))
        return (result.scalar() or 0) == 0

    async def _is_allowlisted(self, github_user_id: str) -> bool:
        # A GitHub user can have multiple rows (one per source — e.g. manual +
        # github_sponsors once Phase 2 ships), so scalar_one_or_none would raise
        # MultipleResultsFound. first() returns whichever active row hits first,
        # which is all this check needs.
        result = await self.db.execute(
            select(SignupAllowlistEntry.id)
            .where(
                SignupAllowlistEntry.github_user_id == github_user_id,
                SignupAllowlistEntry.state == "active",
            )
            .limit(1)
        )
        return result.first() is not None

    async def _legacy_check(self, email: str) -> RedirectResponse | None:
        """Delegate to the pre-existing env-based gate.

        Lazy import to avoid a circular dependency (``api.routes.auth``
        imports service modules for other flows). Shares ``self.db`` with
        ``_check_registration_allowed`` so the OAuth callback opens only one
        DB pool connection per attempt instead of two.
        """
        from api.routes.auth import _check_registration_allowed

        return await _check_registration_allowed(email, self.db)

    def _blocked_response(self) -> RedirectResponse:
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(f"{frontend_url}/signup-blocked", status_code=303)
