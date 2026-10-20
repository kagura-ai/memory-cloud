"""``list`` with tags_match='all' + tags_normalize groups tags like recall (#2086).

recall ANDs one group per requested tag (``tag_filter_groups``, #2071): a
requested tag is satisfied by any one of its spellings, and two requested tags
whose folds collide but whose numbers differ stay two required tags. ``list``
used to AND one fold per tag, so one stored ``pr-123`` satisfied a filter on
``pr-123`` AND ``pr-1-23`` (the lane's fold drops ``-``). The SQL-shape tests
run without a database; the ``db_session`` ones skip when none is reachable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.memory_listing import compile_memory_filters, list_memories
from tests.services.test_memory_listing import _row, _scope


def _all(*tags: str) -> dict:
    return {"tags": list(tags), "tags_match": "all", "tags_normalize": True}


def _sql(filters: dict) -> list[str]:
    return [
        str(p.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for p in compile_memory_filters(filters)
    ]


# ------------------------------------------------------------------ SQL shape


def test_colliding_tags_with_other_numbers_stay_two_guarded_groups():
    preds = _sql(_all("pr-123", "pr-1-23"))
    assert len(preds) == 2
    assert all("= 'pr123'" in p for p in preds)
    assert "= '123'" in preds[0] and "= '1,23'" in preds[1]


def test_one_tag_written_twice_is_one_group_without_a_number_guard():
    preds = _sql(_all("Deploy", "deploy", "todo"))
    assert len(preds) == 2
    assert "IN ('deploy')" in preds[0] and "IN ('todo')" in preds[1]
    assert not any("[^0-9]+" in p for p in preds)


def test_listed_tags_recall_treats_as_one_form_one_group():
    # recall's grouping (normalize_tag) also folds plurals: either spelling counts.
    preds = _sql(_all("log", "logs"))
    assert len(preds) == 1 and "IN ('log', 'logs')" in preds[0]


def test_tags_that_do_not_collide_get_no_number_guard():
    # The lane's fold keeps ".", so these two never fold together here.
    preds = _sql(_all("v0.11.0", "v0.1.10"))
    assert len(preds) == 2
    assert "IN ('v0.11.0')" in preds[0] and "IN ('v0.1.10')" in preds[1]
    assert not any("[^0-9]+" in p for p in preds)


def test_every_tag_stays_required_past_the_group_cap():
    tags = [f"tag-{i}" for i in range(60)]
    assert len(_sql(_all(*tags))) == 60


def test_any_mode_is_unchanged():
    preds = _sql({"tags": ["pr-123", "pr-1-23"], "tags_normalize": True})
    assert len(preds) == 1 and " OR " in preds[0]
    assert "[^0-9]+" not in preds[0]


# ---------------------------------------------------------------- against PG


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        # recall's #2071 cases, on spellings this lane's fold collides.
        (_all("pr-123", "pr-1-23"), {"both"}),
        # Alone, a tag stays number-blind, as recall's widening is.
        (_all("pr-123"), {"both", "one", "upper", "other"}),
        (_all("PR-123", "pr-1-23"), {"both"}),
        (_all("Deploy", "deploy"), {"deploy"}),
        (_all("pr_123", "pr-123"), {"both", "one", "upper", "other"}),
    ],
)
async def test_list_all_groups_match_recall(db_session, filters, expected):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    rows = {
        "both": await _row(db_session, owner, ws, ctx, summary="both", tags=["pr-123", "pr-1-23"]),
        "one": await _row(db_session, owner, ws, ctx, summary="one", tags=["pr-123"]),
        "upper": await _row(db_session, owner, ws, ctx, summary="upper", tags=["PR_123"]),
        "other": await _row(db_session, owner, ws, ctx, summary="other", tags=["pr-1-23"]),
        "deploy": await _row(db_session, owner, ws, ctx, summary="deploy", tags=["DEPLOY"]),
    }
    await db_session.commit()
    page = await list_memories(db_session, context_id=ctx, owner_user_id=None, filters=filters)
    assert {m.summary for m in page.rows} == {rows[k].summary for k in expected}
