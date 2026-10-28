"""#2325: the context-less views follow the member's context access list.

``GET /memory/list``, ``GET /memory/stats`` and ``GET /memory/access-patterns``
without ``context_id`` list or count the caller's own memories in the contexts
the caller reaches (``auth.workspace_roles.reachable_context_ids``: owner and
admin every context of their workspaces, a member or viewer exactly its list,
a viewer with no list every context, a member with no list none). The
workspace totals (``GET /workspaces/{id}/stats``) and the context-less
``memory-timeline`` add up the contexts the caller can open, as
``/workspaces/{id}/contexts/stats`` does: owners and admins keep every
context, a member the ones its list and context privacy open.

The World and the ``_reach`` oracle are ``test_member_context_access_db``'s.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from auth.workspace_roles import WorkspaceRole
from models.auth import IdentityLink, Workspace
from tests.integration import test_member_context_access_db as mca

# The World fixtures, re-exported for this module's tests.
world = mca.world
live = mca.live

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Memories per context in the World: the creator's note and pinned rule, plus
# the one mA wrote in B.
MEMORIES_IN = dict.fromkeys(mca.CONTEXTS, 2)
MEMORIES_IN["B"] += 1


def _own_reachable(caller: str) -> set[str]:
    """The memory keys the caller wrote in contexts it reaches."""
    keys: set[str] = set()
    for ctx, (_, creator) in mca.CONTEXTS.items():
        if creator == caller and mca._reach(caller, ctx):
            keys |= {ctx, f"{ctx}_pin"}
    if caller == "mA" and mca._reach("mA", "B"):
        keys.add("mA_in_B")
    return keys


def _contexts_opened_to(caller: str) -> set[str]:
    """What the workspace totals cover: every context for owner/admin, else
    the contexts the member reaches (list and privacy)."""
    if mca.CALLERS[caller][0] in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
        return set(mca.CONTEXTS)
    return {ctx for ctx in mca.CONTEXTS if mca._reach(caller, ctx)}


async def _own_views(w: mca.World, caller: str, **rest_kw) -> tuple[set[str], int, int]:
    """(listed ids, stats total, access-patterns total) of the context-less views."""
    async with mca._rest(w, caller, **rest_kw) as c:
        listed = await c.get("/api/v1/memory/list", params={"limit": 100})
        stats = await c.get("/api/v1/memory/stats")
        patterns = await c.get("/api/v1/memory/access-patterns")
    assert listed.status_code == 200, listed.text
    assert stats.status_code == 200, stats.text
    assert patterns.status_code == 200, patterns.text
    body = listed.json()
    assert body["total"] == len(body["memories"])
    return (
        {m["id"] for m in body["memories"]},
        stats.json()["total_count"],
        sum(patterns.json()["type_distribution"].values()),
    )


@pytest.mark.parametrize("caller", mca.ALL_CALLERS)
async def test_context_less_own_views_follow_the_list(live, caller) -> None:
    w: mca.World = live
    expected = {str(w.mem[k]) for k in _own_reachable(caller)}
    listed, counted, patterned = await _own_views(w, caller)
    assert listed == expected, (caller, listed)
    assert counted == len(expected), caller
    assert patterned == len(expected), caller


async def test_a_workspace_scoped_key_reaches_its_workspace_only(live) -> None:
    """A workspace-scoped API key keeps to its workspace here as on every
    context-addressed path (#963): the owner's rows in W are not listed
    through a key scoped to another workspace."""
    w: mca.World = live
    everything = {str(w.mem[k]) for k in _own_reachable("owner")}
    assert await _own_views(w, "owner", api_key_workspace_id=w.ws["w"]) == (
        everything,
        len(everything),
        len(everything),
    )
    assert await _own_views(w, "owner", api_key_workspace_id=w.ws["x"]) == (set(), 0, 0)


async def test_own_rows_stay_listed_after_the_link_is_gone(live) -> None:
    """Reach, not privacy, decides here: a row the caller wrote in a private
    context on its list stays in its own views after the identity link that
    opened the context is removed — while the context itself is no longer
    readable by id (the context resolver's privacy rule, unchanged)."""
    w: mca.World = live
    mine = mca._memory(w.ws["w"], w.ctx["pO"], w.u["linked_m"], "linked_m wrote this in pO")
    w.session.add(mine)
    await w.session.execute(
        delete(IdentityLink).where(IdentityLink.user_id.in_([w.u["owner"], w.u["linked_m"]]))
    )
    await w.session.commit()
    assert await _own_views(w, "linked_m") == ({str(mine.id)}, 1, 1)
    async with mca._rest(w, "linked_m") as c:
        by_context = await c.get("/api/v1/memory/list", params={"context_id": str(w.ctx["pO"])})
    assert by_context.status_code == 404, by_context.text


@pytest.mark.parametrize("caller", mca.ALL_CALLERS)
async def test_workspace_totals_follow_the_list(live, caller) -> None:
    w: mca.World = live
    async with mca._rest(w, caller) as c:
        stats = await c.get(f"/api/v1/workspaces/{w.ws['w']}/stats")
        timeline = await c.get(
            f"/api/v1/workspaces/{w.ws['w']}/memory-timeline", params={"days": 7}
        )
    if caller == "outsider" or mca.CALLERS[caller][0] == WorkspaceRole.VIEWER:
        # The member floor of these routes is unchanged: a viewer and a
        # non-member get no totals at all.
        assert stats.status_code in (403, 404), stats.text
        assert timeline.status_code in (403, 404), timeline.text
        return
    opened = _contexts_opened_to(caller)
    memories = sum(MEMORIES_IN[ctx] for ctx in opened)
    assert stats.status_code == 200, stats.text
    assert stats.json()["context_count"] == len(opened), (caller, stats.text)
    assert stats.json()["total_memories"] == memories, (caller, stats.text)
    assert stats.json()["member_count"] == len(mca.CALLERS)
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["memories_created_in_period"] == memories, (caller, timeline.text)


async def test_timeline_with_a_context_id_keeps_the_per_context_rule(live) -> None:
    """``context_id`` still goes through the per-context resolver: a context
    outside the member's list is not found, one inside it is counted."""
    w: mca.World = live
    async with mca._rest(w, "mA") as c:
        inside = await c.get(
            f"/api/v1/workspaces/{w.ws['w']}/memory-timeline",
            params={"days": 7, "context_id": str(w.ctx["A"])},
        )
        outside = await c.get(
            f"/api/v1/workspaces/{w.ws['w']}/memory-timeline",
            params={"days": 7, "context_id": str(w.ctx["B"])},
        )
    assert inside.status_code == 200, inside.text
    assert inside.json()["memories_created_in_period"] == MEMORIES_IN["A"]
    assert outside.status_code == 404, outside.text


@pytest.mark.parametrize("caller", [c for c in mca.ALL_CALLERS if c != "outsider"])
async def test_dashboard_names_only_reached_contexts(live, caller) -> None:
    """``GET /workspace/stats`` (the dashboard card) lists by name only the
    contexts the caller reaches; the rest aggregate with the private ones,
    and the totals still cover every context."""
    w: mca.World = live
    async with mca._rest(w, caller) as c:
        r = await c.get("/api/v1/workspace/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {str(w.ctx[k]): k for k in mca.CONTEXTS}
    named = {by_id[row["context_id"]] for row in body["contexts"]}
    if caller == "owner":
        expected = set(mca.CONTEXTS)
    else:
        # The route's own privacy rule (creator only, no identity links)
        # under the reach rule.
        expected = {
            ctx
            for ctx, (private, creator) in mca.CONTEXTS.items()
            if (not private or creator == caller) and mca._reach(caller, ctx)
        }
    assert named == expected, (caller, named)
    hidden = body["private_aggregation"] or {"context_count": 0, "memory_count": 0}
    assert hidden["context_count"] == len(mca.CONTEXTS) - len(expected)
    assert body["context_count"] == len(mca.CONTEXTS)
    # The total is unchanged by the list: the named rows plus the aggregate.
    shown = sum(row["memory_count"] for row in body["contexts"])
    assert body["total_memories"] == shown + hidden["memory_count"]


@pytest.mark.parametrize("caller", ["admin", "mA", "v_null"])
async def test_dashboard_hides_context_names_until_a_member_regains_a_seat(live, caller) -> None:
    """A plan downgrade removes context reach, including dashboard names.

    The quota totals remain available, and restoring a seat restores the
    same names without changing the member's context access list.
    """
    w: mca.World = live
    async with mca._rest(w, caller) as c:
        before = await c.get("/api/v1/workspace/stats")
    assert before.status_code == 200, before.text
    assert before.json()["contexts"]

    workspace = await w.session.get(Workspace, w.ws["w"])
    assert workspace is not None
    workspace.plan_name = "free"
    await w.session.commit()
    async with mca._rest(w, caller) as c:
        paused = await c.get("/api/v1/workspace/stats")
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["contexts"] == []
    assert body["context_count"] == before.json()["context_count"]
    assert body["total_memories"] == before.json()["total_memories"]
    assert body["private_aggregation"] == {
        "context_count": body["context_count"],
        "memory_count": body["total_memories"],
    }
    assert body["unsearchable_memories"] == 0
    assert body["stalled_memories"] == 0
    assert body["erased_memories"] == 0

    async with mca._rest(w, "owner") as c:
        owner = await c.get("/api/v1/workspace/stats")
    assert owner.status_code == 200, owner.text
    assert len(owner.json()["contexts"]) == len(mca.CONTEXTS)

    workspace.plan_name = "pro"
    await w.session.commit()
    async with mca._rest(w, caller) as c:
        restored = await c.get("/api/v1/workspace/stats")
    assert restored.status_code == 200, restored.text
    assert restored.json()["contexts"] == before.json()["contexts"]
