"""Pin the e09_608 migration to the runtime DCR scope contract.

The e09_608 migration narrows ``DCR_DEFAULT_SCOPE`` by stripping
``memory:admin`` from rows that match the post-#592 canonical default. It
uses string-equal matching (``WHERE scope = :old_scope``) — anything that
does not match the exact pre-narrow default is left untouched, which
preserves clients that explicitly requested narrower or wider custom
scope sets (R1 policy from #608 gate1 review).

This test pins three invariants that, if regressed, would either over-
strip legitimate clients or fail to strip the silent auto-grant cohort:

1. The migration's ``_PRE_NARROW_CANONICAL`` exactly matches the
   string that the post-#592 (``e08_592_oauth_scope_canonicalize``)
   migration wrote into the DB. If a future change edits one but not
   the other, ``WHERE scope = :old_scope`` matches zero rows on
   real-world clients.
2. The migration's ``_POST_NARROW_CANONICAL`` exactly matches the
   current runtime ``DCR_DEFAULT_SCOPE`` (the string the application
   issues today). If the runtime value drifts from the migration, new
   rows get the runtime default while the migration still rewrites
   pre-narrow rows to the stale value — the two paths diverge.
3. ``memory:admin`` is the ONLY scope removed. If the migration is
   accidentally edited to also strip ``memory:delete`` (D4) or
   ``openid`` (D5) in this revision, those follow-up issues lose their
   separate migration story and we lose the audit trail.

These are deliberately string-level checks; we are NOT exercising the
migration against a live DB here (that is covered by the integration
suite when ``alembic upgrade head`` runs end-to-end). The string pins
are cheap, fast, and catch the realistic regression: someone editing
the migration constants without realising they are bound to runtime
contracts.
"""

import importlib
import sys
from pathlib import Path

_BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

_ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_VERSIONS))

from auth.mcp_scopes import (  # noqa: E402
    ALL_ADVERTISED_SCOPES,
    DCR_DEFAULT_SCOPE,
)

e08 = importlib.import_module("e08_592_oauth_scope_canonicalize")
e09 = importlib.import_module("e09_608_dcr_default_narrow")


class TestE09DcrDefaultNarrowMigrationPins:
    def test_pre_narrow_string_matches_e08_post_canonical(self) -> None:
        """e09's pre-narrow string is exactly what e08 wrote into the DB.

        If e08's ``_CANONICAL_AT_E08`` is ever edited, e09 must follow — or
        ``UPDATE ... WHERE scope = :old_scope`` matches zero real rows.
        """
        assert e09._PRE_NARROW_CANONICAL == e08._CANONICAL_AT_E08

    def test_post_narrow_string_matches_runtime_default(self) -> None:
        """e09's post-narrow string is exactly what the application issues today.

        If the runtime ``DCR_DEFAULT_SCOPE`` is changed (e.g. another scope
        added or reordered), e09 must follow — otherwise newly-issued rows
        get the runtime default while the migration still rewrites pre-narrow
        rows to the stale post-narrow value, diverging the two paths.
        """
        assert e09._POST_NARROW_CANONICAL == DCR_DEFAULT_SCOPE

    def test_only_memory_admin_is_removed(self) -> None:
        """The set difference between pre-narrow and post-narrow is exactly
        ``{memory:admin}``. Stripping anything else in this revision steals
        scope from a future #608 sub-PR.
        """
        pre = set(e09._PRE_NARROW_CANONICAL.split())
        post = set(e09._POST_NARROW_CANONICAL.split())
        removed = pre - post
        added = post - pre
        assert removed == {"memory:admin"}, (
            f"e09 removes {sorted(removed)}, expected only memory:admin. "
            "memory:delete is owned by #608 D4; openid is owned by #608 D5; "
            "do not strip them in this revision."
        )
        assert added == set(), f"e09 unexpectedly adds {sorted(added)}"

    def test_pre_narrow_string_is_full_advertised_union(self) -> None:
        """The pre-narrow string is exactly ``ALL_ADVERTISED_SCOPES`` as a
        space-separated string — i.e. the union that #592 established as
        the post-fix canonical default. Pins the chain of reasoning end to
        end: e08 wrote the union into the DB, e09 narrows by one scope.
        """
        assert set(e09._PRE_NARROW_CANONICAL.split()) == set(ALL_ADVERTISED_SCOPES)

    def test_down_revision_chain(self) -> None:
        """e09 revises e08. Re-ordering the migration chain by editing
        ``down_revision`` would break the linear apply order, so pin it.
        """
        assert e09.revision == "e09_608_dcr_default_narrow"
        assert e09.down_revision == "e08_592_oauth_scope_canonicalize"
