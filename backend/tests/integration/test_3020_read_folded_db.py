"""#3020 read side, ``folded``: a link Sleep applied folds as its suggestion did.

Built on the ``marked`` read side (its world and cases are in
``test_3020_read_marked_db.py``, which also holds the both-in-the-pool fold):
an applied pair reads as it read before the link. A chain newer -(applied)->
older -(verified suggestion)-> oldest folds both under the newer; an older
memory two applied links name folds under whichever newer one is in the pool
(the newer of the two when both are); user facts lane rows carry ``folded`` like
recall rows, for an applied link and for a verified suggestion without one,
and never name a memory the page already shows.

Committed and cleaned up per test (``committed_cleanup``). Not executed in the
local unit run — needs the DB container.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update

from models.memory import Memory
from tests.integration._supersede_apply_support import (
    committed_cleanup,
    memory,
    new_report,
    run_phase,
    verified_candidate,
    verified_pair,
)
from tests.integration.test_3020_read_marked_db import build_world, everything, recall
from utils.datetime import utcnow


@pytest_asyncio.fixture(loop_scope="session")
async def committed(db_session):
    async with committed_cleanup(db_session) as ids:
        yield ids


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)


@pytest.mark.asyncio
async def test_a_chain_through_an_applied_link_folds_both_under_the_newer(committed):
    db = committed.db
    w = await build_world(db, committed)
    newer_id, older_id = w.applied
    older = await db.get(Memory, older_id)
    oldest = memory(
        w.s,
        summary="the office was on floor 1",
        content="floor 1 at first",
        created_at=older.created_at - timedelta(days=1),
    )
    db.add(oldest)
    await db.flush()
    await db.execute(
        update(Memory)
        .where(Memory.id == older_id)
        .values(
            supersede_candidate=verified_candidate(
                oldest, newer_summary=older.summary, newer_content=older.content
            )
        )
    )
    oldest_id = oldest.id
    await db.commit()

    response = await recall(db, w, [*everything(w), oldest_id])

    by_id = {r.memory_id: r for r in response.results}
    assert older_id not in by_id and oldest_id not in by_id
    head = by_id[newer_id]
    assert (head.folded.count, head.folded.memory_ids) == (2, [older_id, oldest_id])


@pytest.mark.asyncio
async def test_a_lane_row_carries_folded_for_an_applied_link(committed):
    w = await build_world(committed.db, committed)
    newer, older = w.applied

    response = await recall(committed.db, w, [w.other], lane=[newer, older])

    (row,) = response.user_facts
    assert row.memory_id == newer
    assert (row.folded.count, row.folded.memory_ids) == (1, [older])
    assert row.superseded_by is None


@pytest.mark.asyncio
async def test_a_lane_row_carries_folded_for_a_verified_suggestion(committed):
    """Also without an applied link: the lane folded before, and now says so."""
    db = committed.db
    w = await build_world(db, committed)
    newer, older = await verified_pair(db, w.s, created_at=utcnow() - timedelta(days=3))
    newer_id, older_id = newer.id, older.id
    await db.commit()

    response = await recall(db, w, [w.other], lane=[newer_id, older_id])

    (row,) = response.user_facts
    assert row.memory_id == newer_id
    assert (row.folded.count, row.folded.memory_ids) == (1, [older_id])
    # The lane resolves no supersede suggestion for its rows.
    assert row.supersede_candidate is None


@pytest.mark.asyncio
async def test_two_applied_links_into_one_older_memory_fold_it_under_the_one_in_the_pool(
    committed,
):
    db = committed.db
    w = await build_world(db, committed)
    n1_id, older_id = w.applied
    older = await db.get(Memory, older_id)
    n1 = await db.get(Memory, n1_id)
    summary, content = "the office moved to floor 7", "floor 7 since winter"
    n2 = memory(
        w.s,
        summary=summary,
        content=content,
        created_at=n1.created_at + timedelta(hours=1),
        supersede_candidate=verified_candidate(older, newer_summary=summary, newer_content=content),
    )
    db.add(n2)
    await db.flush()
    report = await new_report(db, w.s, utcnow() - timedelta(minutes=30))
    _, result = await run_phase(db, w.s, report)
    assert result.details["applied"] == 1
    n2_id = n2.id
    await db.commit()

    # The annotation names N2 (the newest superseder); only N1 is in the pool.
    first = await recall(db, w, [older_id, n1_id, w.other])
    by_id = {r.memory_id: r for r in first.results}
    assert set(by_id) == {n1_id, w.other}
    assert (by_id[n1_id].folded.count, by_id[n1_id].folded.memory_ids) == (1, [older_id])

    # Both newer ones: no verdict compared them, both stay, O under the newer.
    both = await recall(db, w, [older_id, n1_id, n2_id, w.other])
    by_id = {r.memory_id: r for r in both.results}
    assert set(by_id) == {n1_id, n2_id, w.other}
    assert by_id[n2_id].folded.memory_ids == [older_id]
    assert by_id[n1_id].folded is None


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["applied", "verified_suggestion"])
async def test_a_lane_row_never_folds_a_memory_the_page_shows(committed, link):
    db = committed.db
    w = await build_world(db, committed)
    if link == "applied":
        newer_id, older_id = w.applied
    else:
        newer, older = await verified_pair(db, w.s, created_at=utcnow() - timedelta(days=3))
        newer_id, older_id = newer.id, older.id
        await db.commit()

    response = await recall(db, w, [older_id], lane=[newer_id, older_id])

    assert [r.memory_id for r in response.results] == [older_id]
    (row,) = response.user_facts
    assert row.memory_id == newer_id
    assert row.folded is None


@pytest.mark.asyncio
async def test_the_controls_still_hide_and_do_not_fold(committed):
    w = await build_world(committed.db, committed)

    response = await recall(committed.db, w, everything(w))

    by_id = {r.memory_id: r for r in response.results}
    for newer, older in (w.accepted, w.shadow):
        assert older not in by_id
        assert by_id[newer].folded is None
