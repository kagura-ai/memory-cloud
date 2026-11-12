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
``sub`` claim for google rows. Allowlist grants never match on an email
address, which keeps email-change attacks closed.

The one address match (#2785): a pending workspace invitation addressed to the
applicant admits a new person, in both gate states, only when the provider
reports the address verified (Google ``email_verified``, GitHub's verified
primary address; an email sign-up's address is proven by the followed link).
It writes no grant: the invitation admits while it is pending, and each
admission is recorded as a ``signup_admitted`` audit row. The same rule holds
for a pending Google allowlist row (``pending:<email>``), which is promoted
only for a verified address.
"""

import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Literal, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.roles import _OAUTH_CALLBACK_ACTOR, email_signup_account_clause
from auth.system_admin_mode import first_user_bootstrap_applies
from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog, User, WorkspaceInvitation
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from utils.datetime import utcnow
from utils.github_user import resolve_github_user_id
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

SignupGateMode = Literal["manual", "github_sponsors", "both"]
# The providers whose identities the gate admits. ``email`` (#1734) is never
# one of them: an ``email`` allowlist row records a beta invite redeemed by an
# email sign-up and admits no OAuth identity.
SignupGateProvider = Literal["github", "google"]
# Which sign-up control admitted an email sign-up (#1734).
EmailSignupAdmission = Literal[
    "first_user", "open_registration", "workspace_invitation", "beta_invite"
]
# ``audit_logs.user_email`` actor label of the email sign-up's rows.
EMAIL_SIGNUP_ACTOR = "email-signup"
# Audit action of a sign-up admitted by a pending workspace invitation (#2785).
SIGNUP_ADMITTED_ACTION = "signup_admitted"
# How many invitations a ``signup_admitted`` row names (the total is recorded too).
_ADMISSION_RECORD_INVITATIONS = 5

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
    beta_invite_token_hash: str | None = None,
    email_verified: bool = False,
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
        email: Candidate signup email. Used for the existing-user check and,
            when ``email_verified``, to find a pending workspace invitation
            addressed to it (#2785) or a pending Google allowlist row; never
            to match an allowlist grant.
        username: Display label (GitHub login for github, email for google).
            Stored in ``audit_logs.user_metadata`` and structlog only; not
            a matching key.
        ip_address: Caller IP, passed into the audit row for blocked-signup
            events (#655). Optional — None when called from a non-HTTP
            context.
        user_agent: Caller User-Agent header, same role as ``ip_address``.
        beta_invite_token_hash: ``sha256_hex`` of a closed-beta invite token
            carried through the OAuth round trip (#1581), or None. Only ever
            the hash — the callback never sees the plaintext.
        email_verified: True only when the provider reports ``email`` as
            verified (Google ``email_verified is True``; always True for
            GitHub, whose callback keeps only the verified primary address).
            The default is False: an address nobody vouched for matches no
            invitation and no pending row.

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
            beta_invite_token_hash=beta_invite_token_hash,
            email_verified=email_verified,
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
        beta_invite_token_hash: str | None = None,
        email_verified: bool = False,
    ) -> RedirectResponse | None:
        """Return None when signup is allowed, RedirectResponse when blocked.

        Rules (top-down, applied uniformly across both providers since #655):
        1. ``enabled=false`` → the manual allowlist, a pending Google row
           (verified address only), then the legacy env-based gate (OSS path),
           whose invitation branch admits a verified address only (#2785).
        2. Existing user (users row matching this email or user_id/sub) → allowed
           (login not signup; user_id match handles email-change-at-IdP case).
        3. First user (users table empty) → allowed (initial admin bootstrap).
        4. ``mode == "manual"`` → allowed iff ``(provider, subject_id=oauth_sub)``
           is on the allowlist with ``state='active'`` (``source`` ``manual`` or
           ``beta_invite``), or a pending Google row is promoted (verified
           address only), or a pending workspace invitation is addressed to the
           verified address (#2785, recorded as ``signup_admitted``), or — last,
           only when everything above said no — a valid closed-beta invite is
           redeemed for this identity (#1581).
        5. ``mode in ("github_sponsors", "both")`` → NotImplementedError when
           provider is ``"github"`` (Phase 2 work). When provider is
           ``"google"`` these modes fall back to manual semantics (sponsorship
           is GitHub-specific; #655 docs this).
        """
        config = await self._load_config()

        if not config.enabled:
            # #1031: the manual allowlist is an explicit "always allow this
            # identity" grant — honor it even when the gate is disabled, before
            # falling back to the legacy env gate (which blocks new signups when
            # ALLOW_REGISTRATION=false). Matching is identical to the gate-ON
            # manual path: (provider, subject_id=oauth_sub), source='manual',
            # state='active' (the state filter lives in _is_allowlisted, so a
            # revoked entry never resurfaces here).
            if await self._is_allowlisted(provider, oauth_sub, "manual"):
                return None
            # Parity with gate-ON: a Google admin can pre-allowlist by email
            # (pending:<email> sentinel) before the user has OAuth'd. Promote
            # that row to the real OIDC sub on first callback so the pre-grant
            # is honored under the disabled gate too. GitHub has no email-only
            # pending state (its IDs resolve at add-time), so it is skipped.
            # The row is matched by address: a verified address only (#2785).
            if (
                provider == "google"
                and email_verified
                and await self._promote_pending_google_entry(email=email, oauth_sub=oauth_sub)
            ):
                return None
            return await self._legacy_check(
                email,
                oauth_sub,
                provider=provider,
                email_verified=email_verified,
                ip_address=ip_address,
                user_agent=user_agent,
            )

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
            # add-time, so the pending state only exists for Google. The row
            # is matched by address: a verified address only (#2785).
            if (
                provider == "google"
                and email_verified
                and await self._promote_pending_google_entry(email=email, oauth_sub=oauth_sub)
            ):
                return None
            # #2785: a pending workspace invitation addressed to the verified
            # address admits, before the beta invite so an invitee keeps the
            # token. Nothing is consumed; the admission is recorded.
            if email_verified and await self._admit_by_workspace_invitation(
                provider=provider,
                oauth_sub=oauth_sub,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
            ):
                return None
            # #1581: closed-beta invite link — the LAST thing tried, only where
            # the gate would otherwise block. Placement is the contract: every
            # path above returned already, so an existing user, the first user,
            # an allowlisted identity or a promoted pending row never burns a
            # token, and the disabled gate (legacy / open registration) never
            # reaches here at all.
            if await self._redeem_beta_invite(
                token_hash=beta_invite_token_hash,
                provider=provider,
                oauth_sub=oauth_sub,
                email=email,
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
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
    # Email sign-up (#1734)
    # ------------------------------------------------------------------

    async def email_signup_admissible(
        self, *, email: str, beta_invite_token_hash: str | None
    ) -> EmailSignupAdmission | None:
        """Would the sign-up controls admit a NEW account at ``email``? Consumes nothing.

        The request-time check of an email sign-up: advisory only, the
        completion runs :meth:`admit_email_signup`. An existing account at
        the address is not an admission here (the caller handles it).

        Same controls as the OAuth callbacks, minus the grants that belong to
        a provider identity (the manual allowlist, pending Google rows):

        - first account of an empty deployment (every gate state);
        - gate off: ``ALLOW_REGISTRATION``, else a pending workspace
          invitation addressed to ``email`` on a plan that allows it;
        - gate on: a pending workspace invitation addressed to ``email``
          (#2785), else a redeemable beta invite (``ALLOW_REGISTRATION`` does
          not admit, as for OAuth).

        The address is verified by construction: the link goes to it alone.
        """
        config = await self._load_config()
        return await self._email_admission(
            gate_enabled=config.enabled,
            gate_mode=config.mode,
            email=email,
            beta_invite_token_hash=beta_invite_token_hash,
            redeem=None,
            record_invitations=None,
        )

    async def admit_email_signup(
        self,
        *,
        gate_enabled: bool,
        gate_mode: str,
        email: str,
        new_user_id: str,
        beta_invite_token_hash: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> EmailSignupAdmission | None:
        """Run the sign-up controls for an email account being created now. Never commits.

        Called inside the account-creating transaction with the gate config
        read before it (``_load_config`` may commit). A beta invite is
        redeemed here: allowlist row (``provider='email'``, ``subject_id`` =
        ``new_user_id``), the single-use claim and the audit row, in a
        savepoint that a lost race rolls back on its own. An admission by a
        workspace invitation stages its ``signup_admitted`` audit row (#2785),
        committed with the account or not at all. Nothing is written when the
        answer is ``None``.
        """

        def record_invitations(invitations: list[WorkspaceInvitation]) -> None:
            self.db.add(
                self._invitation_admission_record(
                    actor=EMAIL_SIGNUP_ACTOR,
                    provider="email",
                    subject_id=new_user_id,
                    email=email,
                    invitations=invitations,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )

        async def redeem() -> bool:
            return await self._stage_email_beta_invite_redemption(
                token_hash=beta_invite_token_hash,
                new_user_id=new_user_id,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
            )

        return await self._email_admission(
            gate_enabled=gate_enabled,
            gate_mode=gate_mode,
            email=email,
            beta_invite_token_hash=beta_invite_token_hash,
            redeem=redeem,
            record_invitations=record_invitations,
        )

    async def email_signup_available_for_invitation(
        self,
        *,
        email_restricted: bool,
        plan_blocked: bool,
        owner_role: bool,
        workspace_deleted: bool,
    ) -> bool:
        """Can a workspace invitation's addressee create an account by email? (#1734)

        For the public invitation preview: true only where the email form can
        work, so an invitation that admits nobody (link-only, plan-blocked, an
        owner-role row, a deleted workspace) never leads to a form that
        silently sends nothing. The same eligibility as the admission
        (:meth:`_addressed_invitations`), in both gate states (#2785); with
        the gate off ``ALLOW_REGISTRATION`` admits anyone. Names no address.
        """
        settings = get_settings()
        if not settings.enable_email_signup:
            return False
        if first_user_bootstrap_applies() and await self._is_first_user():
            return True
        invitation_admits = (
            email_restricted and not plan_blocked and not owner_role and not workspace_deleted
        )
        config = await self._load_config()
        if config.enabled:
            # A corrupted mode refuses every email sign-up (_email_admission).
            return config.mode in ("manual", "github_sponsors", "both") and invitation_admits
        if settings.allow_registration:
            return True
        return invitation_admits

    async def _email_admission(
        self,
        *,
        gate_enabled: bool,
        gate_mode: str,
        email: str,
        beta_invite_token_hash: str | None,
        redeem: Callable[[], Awaitable[bool]] | None,
        record_invitations: Callable[[list[WorkspaceInvitation]], None] | None,
    ) -> EmailSignupAdmission | None:
        """The sign-up controls for an email identity, in the OAuth order.

        ``redeem`` / ``record_invitations`` are None at request time (read
        only) and stage the beta invite redemption / the ``signup_admitted``
        row at completion, inside the account-creating transaction.
        """
        if first_user_bootstrap_applies() and await self._is_first_user():
            return "first_user"
        settings = get_settings()
        if not gate_enabled:
            # The legacy gate's order (``_check_registration_allowed``). A beta
            # invite is never consumed with the gate off, as for OAuth.
            if settings.allow_registration:
                return "open_registration"
            return self._invitation_admission(
                await self._addressed_invitations(email), record_invitations
            )
        if gate_mode not in ("manual", "github_sponsors", "both"):
            # A corrupted mode refuses (check_access raises for OAuth).
            logger.warning("email_signup_unknown_gate_mode")
            return None
        # #2785: a pending workspace invitation addressed to the address, before
        # the beta invite (an invitee keeps the token, as for OAuth).
        if self._invitation_admission(await self._addressed_invitations(email), record_invitations):
            return "workspace_invitation"
        # Sponsor modes are GitHub-specific; for email they mean manual (as
        # for Google). The manual allowlist and pending Google rows belong to
        # a provider identity and never admit an email identity.
        if not beta_invite_token_hash or not settings.enable_beta_invites:
            return None
        if redeem is None:
            invite = await BetaInviteService(self.db).find_redeemable(
                beta_invite_token_hash, utcnow()
            )
            return "beta_invite" if invite is not None else None
        return "beta_invite" if await redeem() else None

    @staticmethod
    def _invitation_admission(
        invitations: list[WorkspaceInvitation],
        record_invitations: Callable[[list[WorkspaceInvitation]], None] | None,
    ) -> EmailSignupAdmission | None:
        if not invitations:
            return None
        if record_invitations is not None:
            record_invitations(invitations)
        return "workspace_invitation"

    async def _stage_email_beta_invite_redemption(
        self,
        *,
        token_hash: str | None,
        new_user_id: str,
        email: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> bool:
        """Spend a beta invite on a new email account (#1734); never commits.

        The email twin of :meth:`_redeem_beta_invite`: the same three writes
        (allowlist row, guarded single-use claim, ``beta_invite.redeemed``
        audit row), staged in a SAVEPOINT of the account-creating transaction
        instead of committed. A lost race or a constraint error rolls back the
        savepoint only, and the caller refuses the sign-up.
        """
        if not token_hash or not get_settings().enable_beta_invites:
            return False
        invites = BetaInviteService(self.db)
        now = utcnow()
        invite = await invites.find_redeemable(token_hash, now)
        if invite is None:
            logger.info("beta_invite_redeem_rejected", provider="email", subject_id=new_user_id)
            return False
        invite_id = invite.id
        inviter_user_id = invite.inviter_user_id
        entry_id = uuid4()
        savepoint = await self.db.begin_nested()
        try:
            self.db.add(
                SignupAllowlistEntry(
                    id=entry_id,
                    provider="email",
                    subject_id=new_user_id,
                    subject_label=email,
                    github_user_id=_legacy_user_id_for_non_github("email", new_user_id),
                    github_username=email,
                    source="beta_invite",
                    state="active",
                    added_by_user_id=inviter_user_id,
                )
            )
            await self.db.flush()
            claimed = await invites.mark_redeemed(
                token_hash=token_hash, allowlist_entry_id=entry_id, now=now
            )
            if not claimed:
                await savepoint.rollback()
                logger.info(
                    "beta_invite_redeem_lost_race",
                    invite_id=str(invite_id),
                    provider="email",
                    subject_id=new_user_id,
                )
                return False
            self.db.add(
                AuditLog(
                    user_email=EMAIL_SIGNUP_ACTOR,
                    user_id=new_user_id,
                    action="beta_invite.redeemed",
                    resource=f"beta_invite:{invite_id}",
                    new_value_hash=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                    user_metadata={
                        "provider": "email",
                        "inviter_user_id": inviter_user_id,
                        "allowlist_entry_id": str(entry_id),
                    },
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
            logger.info("beta_invite_redeem_conflict", provider="email", subject_id=new_user_id)
            return False
        logger.info(
            "beta_invite_redeemed",
            invite_id=str(invite_id),
            provider="email",
            subject_id=new_user_id,
            inviter_user_id=inviter_user_id,
        )
        return True

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
        #
        # #1734: plus the account email sign-up created for the address, in
        # any case: ensure_user links to it or refuses, never creates. Folded
        # into the same SELECT.
        result = await self.db.execute(
            select(User.id)
            .where(
                or_(
                    User.email == email,
                    User.user_id == user_id,
                    email_signup_account_clause(email),
                )
            )
            .limit(1)
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

    async def _addressed_invitations(self, email: str) -> list[WorkspaceInvitation]:
        """The pending workspace invitations that admit ``email`` (#2785). Read only.

        ``InvitationService.get_pending_invitations_for_email`` with its default
        eligible set (row exists = not revoked, not accepted, not expired,
        workspace not deleted, plan with ``team_invitations``, addressed to
        the address case-insensitively; a link-only row never matches), minus
        owner-role rows (accept refuses them). ASCII addresses only, on both
        sides, compared again here: the database's ``lower()`` and Python's can
        differ outside ASCII. The caller has checked that the address is
        verified.
        """
        if not email or not email.isascii():
            return []
        # Lazy: invitation_service imports workspace code that imports this module.
        from services.invitation_service import InvitationService

        wanted = email.lower()
        found = await InvitationService(self.db).get_pending_invitations_for_email(email=email)
        return [
            inv
            for inv in found
            if inv.email is not None
            and inv.email.isascii()
            and inv.email.lower() == wanted
            and inv.role != WorkspaceRole.OWNER
        ]

    def _invitation_admission_record(
        self,
        *,
        actor: str,
        provider: str,
        subject_id: str,
        email: str,
        invitations: list[WorkspaceInvitation],
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuditLog:
        """The ``signup_admitted`` audit row of an admission by invitation (#2785).

        Each invitation is named by its public id with its workspace and
        inviter, so the record stays readable after the invitation row is
        revoked or purged. The address only as HMAC; never the token or URL.
        """
        return AuditLog(
            user_email=actor,
            user_id=subject_id,
            action=SIGNUP_ADMITTED_ACTION,
            resource=f"signup_gate:{provider}",
            new_value_hash=hmac_sha256_hex(email, get_settings().audit_hmac_key),
            user_metadata={
                "provider": provider,
                "admission": "workspace_invitation",
                "invitations": [
                    {
                        "id": inv.public_id,
                        "workspace_id": str(inv.workspace_id),
                        "invited_by": inv.invited_by,
                    }
                    for inv in invitations[:_ADMISSION_RECORD_INVITATIONS]
                ],
                "invitation_count": len(invitations),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def _admit_by_workspace_invitation(
        self,
        *,
        provider: SignupGateProvider,
        oauth_sub: str,
        email: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> bool:
        """Admit a new OAuth identity whose verified address is invited (#2785).

        True = admitted: at least one invitation from
        :meth:`_addressed_invitations`, and its ``signup_admitted`` row
        committed. The commit is part of the admission, as for
        :meth:`_redeem_beta_invite`: a database error propagates (the callback
        ends in ``oauth_failed``) and nobody is admitted. Nothing is consumed,
        so a retry is safe. Same session invariant as
        :meth:`_record_blocked_signup`: the gate runs before any other write
        of the callback, so the commit carries this row only.
        """
        invitations = await self._addressed_invitations(email)
        if not invitations:
            return False
        self.db.add(
            self._invitation_admission_record(
                actor=_OAUTH_CALLBACK_ACTOR,
                provider=provider,
                subject_id=oauth_sub,
                email=email,
                invitations=invitations,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        count = len(invitations)
        await self.db.commit()
        # Never the address.
        logger.info(
            "signup_admitted",
            provider=provider,
            subject_id=oauth_sub,
            admission="workspace_invitation",
            invitations=count,
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
            # #1581: a redeemed closed-beta invite writes a ``beta_invite`` row.
            # It is the same kind of grant as a manual one — an explicit "allow
            # this identity", attributed to a user instead of an admin — so it
            # is honoured wherever manual rows are (incl. the #1031 gate-off
            # path, which calls this with mode="manual"). Without it an invitee
            # whose user creation failed AFTER redemption (e.g. email_in_use)
            # would be locked out with their invite already spent.
            filters.append(SignupAllowlistEntry.source.in_(("manual", "beta_invite")))
        elif mode == "github_sponsors":
            filters.append(SignupAllowlistEntry.source == "github_sponsors")
        # mode == "both" → no source filter

        # A single (provider, subject_id) can have multiple rows (one per
        # source), so scalar_one_or_none would raise MultipleResultsFound
        # when mode='both' matches two rows. first() returns whichever
        # active row hits first.
        result = await self.db.execute(select(SignupAllowlistEntry.id).where(*filters).limit(1))
        return result.first() is not None

    async def _redeem_beta_invite(
        self,
        *,
        token_hash: str | None,
        provider: SignupGateProvider,
        oauth_sub: str,
        email: str,
        username: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> bool:
        """Spend a closed-beta invite on this identity (#1581). True = admitted.

        One transaction, three writes: the ``signup_allowlist`` row (keyed on the
        immutable IdP identity — the e-mail is the label only, #655), the
        single-use claim on ``beta_invites``, and the audit row. They commit
        together or not at all, so a consumed invite always has its allowlist
        row and an allowlist row never exists for an invite someone else won.

        The claim is ``BetaInviteService.mark_redeemed`` — a guarded ``UPDATE``
        that must hit exactly one row. Two people racing one link both get this
        far; the row lock serializes them and the loser's ``UPDATE`` matches
        nothing, so its allowlist INSERT is rolled back and it is blocked.

        Session invariant (see :meth:`_record_blocked_signup`): the gate runs
        before any other DB mutation in the callback, so the ``commit()`` /
        ``rollback()`` here can only touch the rows this method added.

        ``ENABLE_BETA_INVITES=false`` is the kill switch for redemption too, not
        only for minting: the invite table is not even read.

        Args:
            token_hash: ``sha256_hex`` of the presented token, or None.
            provider: OAuth provider of the signing-up identity.
            oauth_sub: Immutable IdP identity — becomes ``subject_id``.
            email: Invitee e-mail — ``subject_label`` and the audit HMAC input.
            username: GitHub login (legacy ``github_username`` column); unused
                for google.
            ip_address: Caller IP for the audit row.
            user_agent: Caller User-Agent for the audit row.

        Returns:
            True when the invite was redeemed (or this identity already holds
            the winning row from a parallel tab); False to fall through to block.
        """
        if not token_hash or not get_settings().enable_beta_invites:
            return False

        invites = BetaInviteService(self.db)
        now = utcnow()
        invite = await invites.find_redeemable(token_hash, now)
        if invite is None:
            # Unknown / expired / revoked / already used — deliberately one
            # undifferentiated outcome. The blocked-signup audit row follows.
            logger.info("beta_invite_redeem_rejected", provider=provider, subject_id=oauth_sub)
            return False

        # Snapshot before any commit/rollback: both expire ORM instances, and
        # touching ``invite.id`` afterwards would lazy-load in async context
        # (MissingGreenlet) — same trap as ``_promote_pending_google_entry``.
        invite_id = invite.id
        inviter_user_id = invite.inviter_user_id

        # Deprecated NOT-NULL legacy columns (#655): filled exactly the way the
        # admin route fills them — numeric ID + login for github, the
        # ``<provider>:<sub>`` sentinel + label for everything else.
        if provider == "github":
            legacy_user_id = oauth_sub
            legacy_username = username or email
        else:
            legacy_user_id = _legacy_user_id_for_non_github(provider, oauth_sub)
            legacy_username = email

        # Client-side id: the claim UPDATE needs it as the FK value.
        entry_id = uuid4()
        try:
            self.db.add(
                SignupAllowlistEntry(
                    id=entry_id,
                    provider=provider,
                    subject_id=oauth_sub,
                    subject_label=email,
                    github_user_id=legacy_user_id,
                    github_username=legacy_username,
                    source="beta_invite",
                    state="active",
                    added_by_user_id=inviter_user_id,
                )
            )
            await self.db.flush()
        except IntegrityError:
            # (provider, subject_id, 'beta_invite') already exists: this same
            # identity is redeeming in two tabs and the other one got there
            # first, or an inactive row for it lingers. Discard our insert and
            # answer from the allowlist — a winning active row IS the grant; an
            # inactive one is not, and the token stays unspent.
            await self.db.rollback()
            return await self._is_allowlisted(provider, oauth_sub, "manual")

        claimed = await invites.mark_redeemed(
            token_hash=token_hash, allowlist_entry_id=entry_id, now=now
        )
        if not claimed:
            # Lost the single-use race (or the invite died since the pre-check).
            # The rollback takes our allowlist row with it.
            await self.db.rollback()
            logger.info(
                "beta_invite_redeem_lost_race",
                invite_id=str(invite_id),
                provider=provider,
                subject_id=oauth_sub,
            )
            return False

        self.db.add(
            AuditLog(
                user_email=_OAUTH_CALLBACK_ACTOR,
                user_id=oauth_sub,
                action="beta_invite.redeemed",
                resource=f"beta_invite:{invite_id}",
                # E-mail HMAC'd, never plaintext — same as _record_blocked_signup.
                new_value_hash=hmac_sha256_hex(email, get_settings().audit_hmac_key),
                # Invite id only: never the token, its hash, or the URL.
                user_metadata={
                    "provider": provider,
                    "inviter_user_id": inviter_user_id,
                    "allowlist_entry_id": str(entry_id),
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        await self.db.commit()

        logger.info(
            "beta_invite_redeemed",
            invite_id=str(invite_id),
            provider=provider,
            subject_id=oauth_sub,
            inviter_user_id=inviter_user_id,
        )
        return True

    async def _legacy_check(
        self,
        email: str,
        user_id: str,
        *,
        provider: SignupGateProvider,
        email_verified: bool,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> RedirectResponse | None:
        """Delegate to the pre-existing env-based gate.

        Lazy import to avoid a circular dependency (``api.routes.auth``
        imports service modules for other flows). Shares ``self.db`` with
        ``_check_registration_allowed`` so the OAuth callback opens only one
        DB pool connection per attempt instead of two.

        Its invitation branch is :meth:`_admit_by_workspace_invitation` (the
        same eligibility and record as with the gate on), handed over only
        for a verified address (#2785).
        """
        from api.routes.auth import _check_registration_allowed

        async def admit_by_invitation() -> bool:
            return await self._admit_by_workspace_invitation(
                provider=provider,
                oauth_sub=user_id,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
            )

        return await _check_registration_allowed(
            email,
            self.db,
            user_id=user_id,
            admit_by_invitation=admit_by_invitation if email_verified else None,
        )

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
