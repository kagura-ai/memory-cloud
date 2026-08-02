"""``referral_memory_bonus`` must stay OUT of the addon machinery (Issue #1470).

READ THIS BEFORE "FIXING" A FAILURE HERE.

If one of these tests goes red, the correct fix is almost never to update the
test. It is to remove ``referral_memory_bonus`` from whichever addon collection
it was just added to. The referral reward is deliberately NOT an addon, and each
of the three collections guarded below would destroy it:

1. ``internal_billing._ADDON_COLUMNS`` — the billing push has FULL-REPLACE
   semantics: when the payload carries ``addons`` it zeroes every column in this
   map before applying the provided values. Billing knows nothing about
   referrals, so a referral bonus listed here evaporates the moment a referred
   user converts to paid — the worst possible time.

2. ``AddonCalculatorService.recalculate_workspace_bonuses``'s bonuses dict — the
   ``addon_*`` columns are a CACHE of ``SUM(WorkspaceAddon.quantity x
   unit_value)``, rewritten with that absolute value on every recalc. A referral
   bonus has no backing ``WorkspaceAddon`` row, so listing it here resets it to
   0 on the next recalc. #665 fixed exactly this class of bug for admin grants.

3. ``admin_plans._ADDON_FIELD_SPECS`` — drives the admin quota PUT, whose
   divisibility check would reject the reward amount outright
   (``ADDON_UNIT_VALUES["extra_memory"]`` is 10000, so the minimum grantable
   memory addon is 20x a single referral reward), and whose absolute
   ``admin_grant_quantity`` arithmetic would treat the referral portion as an
   admin grant to be re-derived.

The reward's source of truth is the ``referral_grants`` ledger; the column is
recomputed as an absolute SUM over non-revoked rows by ``ReferralService``.
"""

from __future__ import annotations

import inspect

from api.routes.admin_plans import _ADDON_FIELD_SPECS
from api.routes.internal_billing import _ADDON_COLUMNS
from models.auth import Workspace
from services.addon_calculator_service import ADDON_UNIT_VALUES, AddonCalculatorService

REFERRAL_COLUMN = "referral_memory_bonus"

_WHY = (
    f"{REFERRAL_COLUMN} must stay outside the addon machinery (#1470) — see this "
    "module's docstring. Remove it from the collection rather than updating this test."
)


def test_referral_column_absent_from_billing_addon_columns() -> None:
    """The billing push must not be able to zero the referral bonus."""
    assert REFERRAL_COLUMN not in _ADDON_COLUMNS.values(), (
        f"{_WHY} internal_billing._ADDON_COLUMNS full-replaces every column it "
        "lists, so a referral bonus here is wiped by any billing push carrying "
        "`addons` — i.e. at the exact moment a referred user converts to paid."
    )
    assert REFERRAL_COLUMN not in _ADDON_COLUMNS, _WHY


def test_referral_column_absent_from_addon_cache_recalc() -> None:
    """The addon cache recalc must not overwrite the referral bonus."""
    source = inspect.getsource(AddonCalculatorService.recalculate_workspace_bonuses)
    assert REFERRAL_COLUMN not in source, (
        f"{_WHY} recalculate_workspace_bonuses writes the ABSOLUTE "
        "SUM(WorkspaceAddon.quantity * unit_value) into every column it names. A "
        "referral bonus has no backing WorkspaceAddon row, so it would be reset "
        "to 0 on the next recalc (the #665 bug, reintroduced)."
    )


def test_referral_column_absent_from_admin_quota_field_specs() -> None:
    """The admin quota PUT must not treat the referral bonus as an addon."""
    field_names = {spec.field_name for spec in _ADDON_FIELD_SPECS}
    assert REFERRAL_COLUMN not in field_names, (
        f"{_WHY} _ADDON_FIELD_SPECS drives the admin quota PUT, whose "
        "divisibility check would reject the reward amount and whose absolute "
        "admin_grant_quantity arithmetic would re-derive the referral portion as "
        "an admin grant."
    )


def test_referral_column_has_no_addon_unit_value() -> None:
    """No ``ADDON_UNIT_VALUES`` entry may exist for the referral reward.

    A unit value is a *re-valuing multiplier*: changing it re-prices every
    existing row at the next recalc. The referral amounts are snapshotted per
    ledger row precisely so that cannot happen.
    """
    assert not any("referral" in key for key in ADDON_UNIT_VALUES), (
        f"{_WHY} ADDON_UNIT_VALUES is a re-valuing multiplier; referral amounts "
        "are snapshotted per ledger row instead."
    )


def test_referral_column_is_not_addon_prefixed() -> None:
    """The naming itself is load-bearing.

    Several of the guarded collections are maintained by pattern-matching on the
    ``addon_`` prefix. Renaming this column to ``addon_referral_memory_bonus``
    would invite exactly the mistakes the other tests here forbid.
    """
    assert not REFERRAL_COLUMN.startswith("addon_"), _WHY
    assert hasattr(Workspace, REFERRAL_COLUMN), (
        "Workspace.referral_memory_bonus is missing — the referral reward has no column to land in."
    )
