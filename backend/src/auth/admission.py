"""How an account was admitted: the value set and the gate's claim (#2803).

A leaf module (no imports from ``services``, ``models`` or ``api``) shared by
the signup gate, the account-creating paths, the ``users`` model and the admin
API.

``users.admitted_via`` is written once, in the transaction that creates the
row, and never rewritten. The value says which control let the account in:

- ``beta_invite``: a closed-beta invite link (the invite id, the issuer and,
  for a user-minted invite, the inviter are recorded too);
- ``allowlist``: a manual allowlist row (a pending Google row included);
- ``workspace_invitation``: a pending workspace invitation to the address;
- ``open_registration``: ``ALLOW_REGISTRATION`` with the gate off;
- ``first_user``: the first account of an empty deployment;
- ``admin_cli``: a local admin created by an admin CLI;
- ``unknown``: no evidence (accounts from before #2803, or a creation the
  gate did not describe, e.g. a lost first-account race).

The invite's label is the inviter's private note (#1595): it is never part
of the record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal, get_args

AdmittedVia = Literal[
    "admin_cli",
    "allowlist",
    "beta_invite",
    "first_user",
    "open_registration",
    "unknown",
    "workspace_invitation",
]
ADMITTED_VIA_VALUES: Final[tuple[str, ...]] = get_args(AdmittedVia)

# What a signup gate can answer when it admits a new account.
SignupAdmission = Literal[
    "allowlist",
    "beta_invite",
    "first_user",
    "open_registration",
    "workspace_invitation",
]

# Who minted the invite an account was admitted with: an admin (the inviter's
# ``users.role`` when the account is created) or an ordinary user.
InviteIssuer = Literal["admin", "user"]


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    """The gate's claim about a sign-up it admitted.

    ``beta_invite_id`` and ``inviter_user_id`` are set only for
    ``via == "beta_invite"`` (both None when the invite is gone). The issuer
    is not part of the claim: it is resolved when the row is written, from
    the inviter's row locked then.
    """

    via: SignupAdmission
    beta_invite_id: uuid.UUID | None = None
    inviter_user_id: str | None = None

    def __post_init__(self) -> None:
        if self.via != "beta_invite" and (
            self.beta_invite_id is not None or self.inviter_user_id is not None
        ):
            raise ValueError("invite details belong to a beta_invite admission only")
