"""Google user resolution helpers (Issue #655).

Companion to :mod:`utils.github_user`. Where GitHub's helper hits the
public GitHub API to map ``username → numeric ID``, Google has no
equivalent: the OIDC ``sub`` claim is opaque and there is no public
``email → sub`` lookup endpoint that doesn't require admin SDK access to
a Google Workspace.

For v1 of the Google signup-gate work we resolve subs by querying our own
``users`` table — i.e. an admin can only add a Google user to the
allowlist *after* that user has OAuth'd at least once, because the
allowlist needs the immutable ``sub`` for matching. That's an accepted
bootstrap UX gap (issue #655 Out of Scope: "Pre-OAuth invitation (email-
only allowlist entry, sub filled at first callback). Phase 2.").

Why not match on email in the allowlist itself? Because email is mutable
at the IdP — a match-on-email allowlist re-opens the email-change attack
that the provider-aware ``subject_id`` design was added to close.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User


class GoogleUserNotFound(Exception):
    """Raised when an email has no matching Google-authenticated user.

    Mirrors :class:`utils.github_user.GitHubUserNotFound` so admin API
    handlers can map both to a 404 response uniformly.
    """


async def resolve_google_sub_by_email(email: str, db: AsyncSession) -> tuple[str, str]:
    """Find the Google OIDC ``sub`` for an email already present in ``users``.

    Args:
        email: Candidate email. Case-insensitive match against
            ``users.email`` (which is stored case-sensitively but treated
            as canonical lowercase by the OAuth callback flow).
        db: Active async DB session.

    Returns:
        ``(sub, email)`` tuple. ``sub`` is the immutable OIDC identifier
        suitable for ``signup_allowlist.subject_id``; ``email`` is the
        canonical row email (in case the caller wants to mirror the
        ``users``-side casing).

    Raises:
        GoogleUserNotFound: No ``users`` row matches ``email`` AND
            ``auth_provider='google'``.
    """
    # Case-insensitive match — emails landing via Google OIDC are
    # lowercase per Google's userinfo response, but the canonical
    # storage isn't enforced, so the match needs to forgive case drift.
    #
    # Use ``func.lower(...) == lower(email)`` rather than ``email.ilike(...)``
    # so that ``%`` and ``_`` in an admin-supplied email (RFC 5321 permits
    # both in the local part) are treated as literal characters, not as SQL
    # LIKE wildcards. PR #657 Copilot review / CSO finding #2: with `ilike`,
    # ``a%@example.com`` would over-match any Google user matching the
    # surrounding pattern and silently allowlist the wrong account.
    result = await db.execute(
        select(User.user_id, User.email)
        .where(User.auth_provider == "google")
        .where(func.lower(User.email) == email.lower())
        .limit(1)
    )
    row = result.first()
    if row is None:
        raise GoogleUserNotFound(
            f"No Google-authenticated user found with email '{email}'. "
            "The user must complete Google OAuth at least once before they "
            "can be added to the allowlist (Phase 2 will add pre-OAuth "
            "invitation by email)."
        )
    sub, canonical_email = row
    return sub, canonical_email
