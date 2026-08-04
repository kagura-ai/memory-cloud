"""GET /workspace/embedding-status must not leak private context contents (#1496).

Found while designing #1496, not by looking for it.

The endpoint returns up to 50 failed memories **with their summaries**
(`FailedMemoryInfo.summary`, first 200 characters). Its query was scoped by
`workspace_id` and `deleted_at` only — no accessible-context filter — while
every other stats path in the same module applies the workspace's three-way
privacy rule. So any workspace MEMBER could read the opening of another
member's PRIVATE context memories by asking for the embedding queue.

Two things made it easy to miss:

- it needs a memory whose embedding actually failed, which is unusual outside
  a workspace with no credential configured — precisely the situation #1496 is
  about, so the leak got materially more reachable at the same moment;
- the endpoint reads like a health/diagnostics route, and diagnostics tend not
  to be reviewed as data surfaces.

The rule pinned here is the one `/api/v1/contexts` uses: an owner sees
everything; a shared context is visible to every member; a private context is
visible only to whoever created it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from api.routes import workspace as workspace_routes


class TestTheQueryIsScopedToAccessibleContexts:
    """Read on the AST.

    Driving this endpoint takes a live Postgres session — the suite's DB
    fixtures skip on this machine and in any environment without the test
    database, which would make a behavioural test silently vacuous exactly
    where a security regression must not be. The structure is what regressed
    and the structure is what is asserted; the integration suite covers the
    endpoint end to end where a database exists.
    """

    @staticmethod
    def _src() -> str:
        return textwrap.dedent(inspect.getsource(workspace_routes.get_embedding_status))

    def test_it_narrows_by_context_membership(self):
        src = self._src()
        assert "Memory.context_id.in_(" in src, (
            "the embedding-status query no longer restricts to contexts the "
            "caller may see; it returns other members' private memory "
            "summaries (#1496)"
        )

    def test_the_private_context_rule_is_the_shared_one(self):
        """`not private OR created_by == me` — the same test used everywhere."""
        src = self._src()
        assert "Context.is_private" in src
        assert "Context.created_by == user_id" in src

    def test_an_owner_is_exempt_rather_than_special_cased(self):
        """Owners see everything, and the narrowing is skipped for them —
        not reimplemented with a different rule that could drift."""
        src = self._src()
        assert "owner_user_id" in src

    def test_the_narrowing_applies_to_the_failed_memory_details_too(self):
        """The counts were never the leak — the summaries were.

        `conditions` is shared by the status aggregate and the failed-memory
        SELECT, so the filter must join `conditions` rather than being applied
        to only one of them.
        """
        tree = ast.parse(self._src())
        appends = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "conditions"
        ]
        assert any("context_id" in ast.dump(a) and "in_" in ast.dump(a) for a in appends), (
            "the accessible-context restriction is not part of `conditions`, "
            "so the failed-memory SELECT that carries the summaries does not "
            "inherit it"
        )
