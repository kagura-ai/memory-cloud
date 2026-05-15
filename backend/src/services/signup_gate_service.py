"""Admin-configurable signup gate service (Issue #358 Phase 1, extended #655).

Sits in front of the OAuth callback's user-creation step. When the gate is
disabled (default OSS behavior), delegates to the existing env-based
``_check_registration_allowed`` so nothing changes for self-hosters who
don't need allowlists. When enabled, applies the configured mode — Phase 1
implements ``manual`` only; ``github_sponsors`` / ``both`` raise
NotImplementedError and are covered by the Phase 2 follow-up issue.

#655 removed the Google pass-through that #358 Phase 1 had as a temporary
trust-Google's-test-users-list contract; Google's "Testing" status does not
enforce that list for non-sensitive scopes, so the gate now applies
provider-uniformly. Both providers match on the immutable IdP identity
(``provider`` + ``subject_id``) — GitHub numeric ID for github rows, OIDC
``sub`` claim for google rows. Email is never used for matching to keep
email-change attacks closed.
"""

import hashlib
import os
from typing import Literal, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.roles import _OAUTH_CALLBACK_ACTOR
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog, User
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from utils.github_user import resolve_github_user_id
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

SignupGateMode = Literal["manual", "github_sponsors", "both"]
SignupGateProvider = Literal["github", "google"]

# ``signup_allowlist.github_user_id`` is a deprecated ``String(64)``
# kept NOT NULL during the #655 migration window. For non-github
# providers we synthesize a ``<provider>:<subject_id>`` value. The
# current schema's unique key is ``(provider, subject_id, source)``,
# so a truncated legacy column value is normally harmless — BUT the
# downgrade migration in ``e14_655_signup_allowlist_provider`` restores
# the old unique constraint on ``(github_user_id, source)``. Two
# distinct ``subject_id`` values whose first 64 chars of
# ``<provider>:<subject_id>`` are equal (e.g. Phase 2 pending sentinels
# for two emails sharing a 49+ char prefix) would then collide and
# break downgrade. Use a hash-based sentinel when the readable form
# would exceed the column limit so each distinct ``subject_id`` maps
# to a distinct legacy value (PR #673 Copilot review #4 finding F).
_LEGACY_USER_ID_COLUMN_LEN = 64


def _legacy_user_id_for_non_github(provider: str, subject_id: str) -> str:
    """Build a downgrade-safe ``github_user_id`` value for a non-github row.

    - Returns the readable ``f"{provider}:{subject_id}"`` when it fits
      the 64-char column (the common case: a real OIDC sub is ~21 chars).
    - Falls back to ``f"{provider}:<sha256(subject_id) hex prefix>"``
      truncated to fit when the readable form would overflow. The hash
      input is the full ``subject_id``, so distinct subject_ids never
      collide regardless of shared prefixes.

    The column is deprecated and slated for removal in a #655 follow-up;
    this helper is the temporary write-side safety until that drop lands.
    """
    readable = f"{provider}:{subject_id}"
    if len(readable) <= _LEGACY_USER_ID_COLUMN_LEN:
        return readable
    prefix = f"{provider}:"
    digest_budget = _LEGACY_USER_ID_COLUMN_LEN - len(prefix)
    digest = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:digest_budget]}"


async def check_signup_access(
    *,
    provider: SignupGateProvider,
    oauth_sub: str,
    email: str,
    username: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> RedirectResponse | None:
    """Run the signup gate for an OAuth callback.

    Convenience wrapper that opens a DB session, instantiates the service,
    and delegates to ``check_access``. Keeps OAuth callback handlers free of
    the ``async for db in get_db()`` boilerplate and the inline service
    import (which exists to break a circular dependency with auth.py).

    Args:
        provider: OAuth provider (``"github"`` or ``"google"``).
        oauth_sub: Immutable IdP identity — GitHub numeric ID (as string)
            for github, OIDC ``sub`` claim for google. Matched verbatim
            against ``signup_allowlist.subject_id``.
        email: Candidate signup email. Used only for the existing-user /
            legacy-gate paths; never for allowlist matching.
        username: Display label (GitHub login for github, email for google).
            Stored in ``audit_logs.user_metadata`` and structlog only; not
            a matching key.
        ip_address: Caller IP, passed into the audit row for blocked-signup
            events (#655). Optional — None when called from a non-HTTP
            context.
        user_agent: Caller User-Agent header, same role as ``ip_address``.

    Returns:
        None when signup is allowed, a RedirectResponse when blocked.
    """
    async for db in get_db():
        gate = SignupGateService(db)
        return await gate.check_access(
            provider=provider,
            oauth_sub=oauth_sub,
            email=email,
            username=username,
            ip_address=ip_address,
            user_agent=user_agent,
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
        provider: SignupGateProvider,
        oauth_sub: str,
        email: str,
        username: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> RedirectResponse | None:
        """Return None when signup is allowed, RedirectResponse when blocked.

        Rules (top-down, applied uniformly across both providers since #655):
        1. ``enabled=false`` → delegate to legacy env-based gate (OSS path).
        2. Existing user (users row matching this email or user_id/sub) → allowed
           (login not signup; user_id match handles email-change-at-IdP case).
        3. First user (users table empty) → allowed (initial admin bootstrap).
        4. ``mode == "manual"`` → allowed iff ``(provider, subject_id=oauth_sub)``
           is on the allowlist with ``state='active'``.
        5. ``mode in ("github_sponsors", "both")`` → NotImplementedError when
           provider is ``"github"`` (Phase 2 work). When provider is
           ``"google"`` these modes fall back to manual semantics (sponsorship
           is GitHub-specific; #655 docs this).
        """
        config = await self._load_config()

        if not config.enabled:
            return await self._legacy_check(email, oauth_sub)

        # #655: the historical Google pass-through is removed. Google's
        # "Testing" status does NOT enforce the test-users list for
        # non-sensitive scopes — the warning screen is click-through. The
        # gate now applies uniformly to both providers, matching on the
        # immutable IdP identity (subject_id).

        if await self._is_existing_user(email, oauth_sub):
            return None

        if await self._is_first_user():
            return None

        # Validate the mode against the Literal allow-list before narrowing.
        # The DB has a CHECK constraint that pins ``mode`` to the same set,
        # but admins with DB access can disable CHECK constraints, and a
        # corrupted row would otherwise fall through to the NotImplementedError
        # at the bottom of this function with a misleading "reserved for
        # Phase 2" message. Raising here surfaces the actual problem.
        if config.mode not in ("manual", "github_sponsors", "both"):
            raise ValueError(
                f"unknown signup_gate_config.mode value: {config.mode!r} "
                f"(expected one of: manual, github_sponsors, both)"
            )
        # Pyright can't narrow ``Mapped[str]`` through the ``in`` check
        # above, but the runtime guard means the cast is safe here.
        effective_mode: SignupGateMode = cast(SignupGateMode, config.mode)

        # #655: sponsors-style modes only apply to GitHub; for Google fall
        # back to manual semantics (a Google user is never a "GitHub
        # Sponsor"). Document the fallback explicitly so a future admin
        # reading the gate logic isn't surprised by why mode='github_sponsors'
        # only checks the manual allowlist for Google.
        if provider == "google" and effective_mode in ("github_sponsors", "both"):
            effective_mode = "manual"

        if effective_mode == "manual":
            if await self._is_allowlisted(provider, oauth_sub, effective_mode):
                return None
            # Phase 2 (#655 follow-up): Google admins can pre-allowlist a
            # target by email before the user has OAuth'd. Such rows carry
            # a sentinel ``subject_id='pending:<email>'`` (set by the admin
            # POST handler). On first OAuth callback for that email we
            # rewrite ``subject_id`` to the real OIDC ``sub`` so subsequent
            # logins match the regular ``(provider, subject_id)`` path
            # above. GitHub has no email-only fallback because
            # ``resolve_github_user_id`` resolves to a numeric ID at
            # add-time, so the pending state only exists for Google.
            if provider == "google" and await self._promote_pending_google_entry(
                email=email, oauth_sub=oauth_sub
            ):
                return None
            await self._record_blocked_signup(
                provider=provider,
                oauth_sub=oauth_sub,
                email=email,
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return self._blocked_response(provider=provider, oauth_sub=oauth_sub)

        # Reached only for provider="github" + mode in ("github_sponsors",
        # "both") since the Google fallback above coerced those modes to
        # "manual", and "manual" itself returned in the block above. With
        # SignupGateMode being a 3-value Literal, this branch is the
        # exhaustive remainder — there is no trailing-fallback return.
        raise NotImplementedError(
            f"Signup gate mode '{effective_mode}' is reserved for Phase 2 "
            "(Issue #358 follow-up). In Phase 1 the admin UI disables "
            "selecting these modes; reaching this branch indicates a "
            "direct DB edit."
        )

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
        """Resolve a GitHub username to canonical ID via GitHub API and insert.

        Legacy single-provider entry point kept for backward compatibility
        with the existing admin HTTP API. New callers should prefer
        :meth:`add_to_allowlist_entry` which is provider-aware.

        Raises:
            GitHubUserNotFound: The username does not exist on GitHub.
            ValueError: The (provider='github', subject_id, source='manual')
                triple already exists.
        """
        user_id, canonical_login = await resolve_github_user_id(github_username)
        return await self.add_to_allowlist_entry(
            provider="github",
            subject_id=user_id,
            subject_label=canonical_login,
            added_by_user_id=added_by_user_id,
            # Backward-compat: legacy column writes too. These shadow the
            # provider/subject_id pair for the GitHub case so the
            # deprecated columns remain populated for existing tooling
            # until they get physically dropped.
            github_user_id=user_id,
            github_username=canonical_login,
        )

    async def add_to_allowlist_entry(
        self,
        *,
        provider: SignupGateProvider,
        subject_id: str,
        subject_label: str,
        added_by_user_id: str,
        github_user_id: str | None = None,
        github_username: str | None = None,
    ) -> SignupAllowlistEntry:
        """Insert a provider-aware allowlist entry.

        Args:
            provider: ``"github"`` or ``"google"``.
            subject_id: Immutable IdP identity (GitHub numeric ID or Google
                OIDC ``sub``). Stored in ``signup_allowlist.subject_id``
                and used as the matching key.
            subject_label: Display label (GitHub login or email). Snapshot
                taken at add time; NOT used for matching.
            added_by_user_id: Admin user_id (auth subject) doing the add.
            github_user_id / github_username: Legacy column writes for the
                GitHub case. When ``provider == "github"``, both MUST be
                provided (the legacy columns are still NOT NULL during the
                migration window). When ``provider == "google"``, both
                are populated with sentinel values (``"google:<sub>"`` /
                ``"<email>"``) since the columns remain NOT NULL until a
                future migration drops them.

        Raises:
            ValueError: The (provider, subject_id, source='manual') triple
                already exists.
        """
        # Sentinel values for the deprecated legacy columns when the row is
        # for a non-GitHub provider. The legacy columns stay NOT NULL during
        # the migration window (#655), so we MUST write something. The
        # ``<provider>:<subject_id>`` format is chosen deliberately:
        #
        # - GitHub real values are pure numeric strings (no colons), so the
        #   prefixed sentinel cannot accidentally collide with a real GitHub
        #   numeric ID. Even if the e14 downgrade-then-upgrade cycle restored
        #   the old (github_user_id, source) UNIQUE constraint, two rows for
        #   the same (provider="google", sub) would still collide via the
        #   sentinel — which is the SAME failure mode as the new (provider,
        #   subject_id, source) UNIQUE, so the legacy constraint enforcing
        #   uniqueness on the sentinel is a feature, not a bug.
        # - The colon prefix makes the value's origin obvious to anyone
        #   spot-querying the table during the migration window.
        if provider == "github":
            if github_user_id is None or github_username is None:
                raise ValueError(
                    "github_user_id and github_username are required when "
                    "provider='github' (legacy columns are still NOT NULL)"
                )
            legacy_user_id = github_user_id
            legacy_username = github_username
        else:
            # See ``_legacy_user_id_for_non_github`` for the column-fit +
            # downgrade-safe construction. Real OIDC subs (~21 chars)
            # land in the readable branch; Phase 2 pending sentinels
            # with long emails take the hash branch so distinct
            # subject_ids never collide on the legacy column.
            legacy_user_id = _legacy_user_id_for_non_github(provider, subject_id)
            legacy_username = subject_label

        existing = await self.db.execute(
            select(SignupAllowlistEntry).where(
                SignupAllowlistEntry.provider == provider,
                SignupAllowlistEntry.subject_id == subject_id,
                SignupAllowlistEntry.source == "manual",
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(
                f"{provider} subject '{subject_label}' is already on the manual allowlist"
            )

        entry = SignupAllowlistEntry(
            provider=provider,
            subject_id=subject_id,
            subject_label=subject_label,
            github_user_id=legacy_user_id,
            github_username=legacy_username,
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
                f"{provider} subject '{subject_label}' is already on the manual allowlist"
            ) from exc
        await self.db.refresh(entry)
        logger.info(
            "signup_allowlist_added",
            provider=provider,
            subject_id=subject_id,
            subject_label=subject_label,
            added_by=added_by_user_id,
        )
        return entry

    async def remove_from_allowlist(self, entry_id: UUID) -> None:
        entry = await self.db.get(SignupAllowlistEntry, entry_id)
        if entry is None:
            raise ValueError(f"Allowlist entry {entry_id} not found")
        # Snapshot provider-aware fields before deletion so the log line
        # survives the cascade. ``subject_label`` is the user-facing
        # identifier post-#655 (GitHub login or email).
        provider = entry.provider
        subject_id = entry.subject_id
        subject_label = entry.subject_label
        await self.db.delete(entry)
        await self.db.commit()
        logger.info(
            "signup_allowlist_removed",
            provider=provider,
            subject_id=subject_id,
            subject_label=subject_label,
        )

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

    async def _is_existing_user(self, email: str, user_id: str) -> bool:
        # Use ``.first()`` rather than ``.scalar_one_or_none()`` because the
        # ``or_(email, user_id)`` predicate can legitimately match two
        # DIFFERENT rows once a user has signed up via two providers and
        # the IdP-side primary email then drifts onto a third row's email.
        # Concrete shape that triggered MultipleResultsFound: GitHub user
        # (sub=N, email=A) exists, separate Google user (sub=M, email=B)
        # exists, the GitHub user's primary email at the IdP changes to B,
        # callback fires with sub=N + email=B → email matches the Google
        # row, user_id matches the GitHub row, two distinct hits.
        # Returning True is correct here — the user IS existing (the
        # GitHub row), and the cross-row email collision is detected and
        # surfaced as ConflictError → /login?error=email_in_use down in
        # RoleManager._sync_existing_user when the UPDATE attempts the
        # email move.
        result = await self.db.execute(
            select(User.id).where(or_(User.email == email, User.user_id == user_id)).limit(1)
        )
        return result.first() is not None

    async def _is_first_user(self) -> bool:
        result = await self.db.execute(select(func.count()).select_from(User))
        return (result.scalar() or 0) == 0

    async def _promote_pending_google_entry(self, *, email: str, oauth_sub: str) -> bool:
        """Promote a pending Google allowlist row to the real OIDC sub.

        Phase 2 (#655 follow-up): admin pre-allowlists a Google user
        by email before they have OAuth'd. The row carries a sentinel
        ``subject_id='pending:<email-lower>'``; on the user's first
        OAuth callback, this method looks up the pending row, rewrites
        ``subject_id`` to the real sub, and updates the legacy column
        ``github_user_id`` (still NOT NULL during the #655 migration
        window) to ``google:<sub>``. ``subject_label`` (the email) is
        left unchanged — it was a snapshot at add-time and the email
        may legitimately differ from the IdP's current email by the
        time the user OAuths.

        Email match is case-insensitive (IdP convention).

        Args:
            email: Email from the OAuth ``userinfo`` payload.
            oauth_sub: OIDC ``sub`` claim, the immutable per-IdP ID.

        Returns:
            True if a pending row was found and promoted; False
            otherwise. The caller treats False as "no pending row —
            apply the normal block".
        """
        pending_subject_id = f"pending:{email.lower()}"
        result = await self.db.execute(
            select(SignupAllowlistEntry)
            .where(
                SignupAllowlistEntry.provider == "google",
                SignupAllowlistEntry.subject_id == pending_subject_id,
                SignupAllowlistEntry.state == "active",
                SignupAllowlistEntry.source == "manual",
            )
            .limit(1)
        )
        pending = result.scalar_one_or_none()
        if pending is None:
            return False

        # Snapshot the PK before mutating + committing so the rollback /
        # success-log paths can reference the ID without re-reading the
        # ORM instance. After ``db.rollback()`` the async session expires
        # attached instances; accessing ``pending.id`` afterwards can
        # trigger an implicit lazy load in async context and raise
        # ``MissingGreenlet``, masking the intended recovery path
        # (PR #673 Copilot review #3).
        pending_id = pending.id

        pending.subject_id = oauth_sub
        # Keep the deprecated NOT-NULL column populated with the
        # ``<provider>:<subject_id>`` sentinel format used by other
        # google rows. ``_legacy_user_id_for_non_github`` keeps the
        # value ≤64 chars and collision-resistant under the downgrade
        # migration's restored unique on ``(github_user_id, source)``.
        # An OIDC sub is ~21 chars so the readable branch wins here;
        # the helper makes that explicit instead of hand-truncating.
        # The label column is left as the email, which is the snapshot
        # the admin wrote at add-time.
        pending.github_user_id = _legacy_user_id_for_non_github("google", oauth_sub)

        try:
            await self.db.commit()
        except IntegrityError:
            # The commit collided on the unique
            # ``(provider, subject_id, source)`` constraint. Two
            # realistic causes:
            #   1. A concurrent admin add or another promotion just
            #      inserted a ``(google, oauth_sub, manual)`` row →
            #      the user IS now allowlisted; returning False would
            #      send them through the blocked-signup redirect
            #      despite a winning entry being present
            #      (PR #673 Copilot review #3 finding E).
            #   2. A stale/inconsistent real-sub row for the same sub
            #      exists outside the active+manual scope (different
            #      source / state). The re-check below will not see
            #      it, so we return False and fall through to block
            #      — the original defensive intent of the handler.
            # After rollback the in-flight UPDATE is discarded; the
            # pending sentinel row remains for a future retry.
            await self.db.rollback()
            race_result = await self.db.execute(
                select(SignupAllowlistEntry.id)
                .where(
                    SignupAllowlistEntry.provider == "google",
                    SignupAllowlistEntry.subject_id == oauth_sub,
                    SignupAllowlistEntry.state == "active",
                    SignupAllowlistEntry.source == "manual",
                )
                .limit(1)
            )
            race_winner_present = race_result.first() is not None
            logger.warning(
                "signup_allowlist_pending_promote_race",
                email_hmac=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                pending_id=str(pending_id),
                race_winner_present=race_winner_present,
            )
            return race_winner_present

        logger.info(
            "signup_allowlist_pending_promoted",
            email_hmac=hmac_sha256_hex(email, get_settings().audit_hmac_key),
            pending_id=str(pending_id),
        )
        return True

    async def _is_allowlisted(
        self,
        provider: SignupGateProvider,
        subject_id: str,
        mode: SignupGateMode,
    ) -> bool:
        # Filter by source so mode='manual' doesn't silently accept a
        # sponsor-only row in Phase 2, and mode='github_sponsors' doesn't
        # silently accept a manually-added row — i.e. keep 'manual' and 'both'
        # semantically distinct. mode='both' omits the source filter by design.
        # #655: match key is (provider, subject_id), not github_user_id alone.
        filters = [
            SignupAllowlistEntry.provider == provider,
            SignupAllowlistEntry.subject_id == subject_id,
            SignupAllowlistEntry.state == "active",
        ]
        if mode == "manual":
            filters.append(SignupAllowlistEntry.source == "manual")
        elif mode == "github_sponsors":
            filters.append(SignupAllowlistEntry.source == "github_sponsors")
        # mode == "both" → no source filter

        # A single (provider, subject_id) can have multiple rows (one per
        # source), so scalar_one_or_none would raise MultipleResultsFound
        # when mode='both' matches two rows. first() returns whichever
        # active row hits first.
        result = await self.db.execute(select(SignupAllowlistEntry.id).where(*filters).limit(1))
        return result.first() is not None

    async def _legacy_check(self, email: str, user_id: str) -> RedirectResponse | None:
        """Delegate to the pre-existing env-based gate.

        Lazy import to avoid a circular dependency (``api.routes.auth``
        imports service modules for other flows). Shares ``self.db`` with
        ``_check_registration_allowed`` so the OAuth callback opens only one
        DB pool connection per attempt instead of two.
        """
        from api.routes.auth import _check_registration_allowed

        return await _check_registration_allowed(email, self.db, user_id=user_id)

    def _blocked_response(
        self,
        *,
        provider: SignupGateProvider | None = None,
        oauth_sub: str | None = None,
    ) -> RedirectResponse:
        """Build the blocked-signup redirect.

        #655: query params carry provider + first 8 chars of the OIDC sub
        so the frontend ``/signup-blocked`` page can surface "show this to
        the admin and they'll grant access" guidance without leaking the
        full immutable identity. ``oauth_sub`` is sliced to 8 chars (cf.
        Git short-SHA convention) — enough for an admin to disambiguate
        between blocked attempts when correlating with the audit_log row
        they share a request with, but short enough to be uncomfortable as
        a leaked tracking handle on its own.

        ``provider`` / ``oauth_sub`` are intentionally optional so the
        function can still be called without context (e.g. a defensive
        terminal-branch in :meth:`check_access`).
        """
        # Use urllib.parse.urlencode for correctness-by-default. Today both
        # ``provider`` (Literal) and the GitHub/Google ``oauth_sub`` prefix
        # are URL-safe, but a future provider whose ``sub`` could include
        # ``&`` / ``#`` / ``+`` / non-ASCII would silently corrupt the URL
        # if we hand-concatenated (Copilot review #5 on PR #657).
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        params: dict[str, str] = {}
        if provider is not None:
            params["provider"] = provider
        if oauth_sub:
            params["sub"] = oauth_sub[:8]
        suffix = ("?" + urlencode(params)) if params else ""
        return RedirectResponse(f"{frontend_url}/signup-blocked{suffix}", status_code=303)

    async def _record_blocked_signup(
        self,
        *,
        provider: SignupGateProvider,
        oauth_sub: str,
        email: str,
        username: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Record a blocked signup attempt to structlog AND audit_logs.

        #655 (CSO gate1 D2): without a durable trail in ``audit_logs``,
        admins cannot triage who tried to sign up after the fact — only
        live structlog tails can. The audit row keeps the email HMAC'd
        (never plaintext, matching the convention from
        ``RoleManager._sync_existing_user``) and pins the IdP identity by
        ``user_id`` so a future cleanup workflow can correlate the
        blocked attempt with the eventual allowlist add.

        ``user_metadata`` deliberately stores only the provider type — never
        the email or any other PII. The HMAC in ``new_value_hash`` is the
        canonical email reference for an audit reader; storing the plaintext
        email in metadata too would defeat that design intent and contradict
        the ``auth/roles.py`` precedent (PR #657 CSO finding #1). For admin
        triage, correlate ``user_id`` (immutable OIDC sub) + ``created_at``
        + ``new_value_hash`` against the live OAuth flow.

        Failures inside this method are swallowed deliberately. The
        gate's primary responsibility is correctness of the block
        decision; an audit-write failure should never escalate into a
        callback 500 for the user being blocked. The structlog warn line
        below preserves observability of audit-write failures.

        Session commit invariant (PR #657 Copilot loop 2 finding #7): this
        method calls ``await self.db.commit()`` on the session that
        ``check_signup_access`` borrowed from ``get_db()``. The gate runs
        as step 3.5 of the OAuth callback — BEFORE any other DB mutation
        (``ensure_user`` and downstream writes happen at step 4, only when
        the gate passes). So at gate-time the session has no other pending
        writes and the early commit can only flush the AuditLog row we
        just added. **If a future refactor moves DB work in front of the
        gate, switch this to ``async with self.db.begin_nested():`` to
        scope the audit write to a SAVEPOINT** rather than committing the
        outer transaction.
        """
        logger.info(
            "signup_blocked",
            provider=provider,
            subject_id=oauth_sub,
            username=username,
        )

        try:
            hmac_key = get_settings().audit_hmac_key
            audit = AuditLog(
                user_email=_OAUTH_CALLBACK_ACTOR,
                user_id=oauth_sub,
                action="signup_blocked",
                resource=f"signup_gate:{provider}",
                new_value_hash=hmac_sha256_hex(email, hmac_key),
                # Provider type only — see docstring on PII rationale.
                user_metadata={"provider": provider},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            self.db.add(audit)
            await self.db.commit()
        except Exception as exc:
            # Roll the failed audit out of the session so a later commit
            # on this session doesn't trip over it. The rollback itself
            # may fail (e.g. connection already torn down by the same
            # error that broke commit) — that's expected and not
            # actionable here; debug-log it so the rare case is still
            # observable without bloating production logs.
            try:
                await self.db.rollback()
            except Exception as rollback_exc:
                logger.debug(
                    "signup_blocked_rollback_failed",
                    provider=provider,
                    subject_id=oauth_sub,
                    error=str(rollback_exc),
                )
            logger.warning(
                "signup_blocked_audit_failed",
                provider=provider,
                subject_id=oauth_sub,
                error=str(exc),
            )
