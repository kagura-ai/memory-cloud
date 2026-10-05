"""When a sign-in provider's link to an account counts as established (#1875).

Attaching a sign-in provider (``POST /me/account/link-provider``) needs only a
live session. Until the link has stood for ``IDENTITY_LINK_SIGN_IN_WINDOW``, a
sign-in through it is therefore an ordinary sign-in and nothing more: it does
not prove the account for an identity link (``api.routes.auth``), and it does
not become the account's primary provider, whose email and name are synced
onto the account (``auth.roles``). Both read the one rule here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config.constants import IDENTITY_LINK_SIGN_IN_WINDOW
from utils.datetime import utcnow

# How far apart a provider row and its account may be written and still count
# as one first sign-in. ``RoleManager.ensure_user`` inserts both in one
# transaction with the database's ``now()``, so they are normally equal; the
# slack only allows for a future change of either default.
CREATED_TOGETHER = timedelta(seconds=5)


def provider_link_established(
    *,
    sub_is_user_id: bool,
    linked_at: datetime | None,
    account_created_at: datetime | None,
) -> bool:
    """Whether a ``user_oauth_providers`` row is an established link.

    A row older than ``IDENTITY_LINK_SIGN_IN_WINDOW`` is. A younger one is only
    when it is the identity the account was created with: its sub is the
    account's ``user_id`` (``sub_is_user_id``) AND the row was written together
    with the ``users`` row (a first sign-in inserts both in one transaction, so
    their times agree). Matching the sub alone is not enough — an account can
    unlink its original identity and have it attached again, and subs are
    scoped by provider, so another provider's could equal the id; a row
    attached later carries the time of the attach.

    Times are naive UTC, as stored. A row with no readable time is not
    established.
    """
    if linked_at is None:
        return False
    if utcnow() - linked_at > IDENTITY_LINK_SIGN_IN_WINDOW:
        return True
    return (
        sub_is_user_id
        and account_created_at is not None
        and abs(linked_at - account_created_at) <= CREATED_TOGETHER
    )
