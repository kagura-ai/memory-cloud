"""Per-channel destination contexts of a chat connector (#1759): the pure parts.

Shape validation of a ``channel_routes`` PATCH, the capability list of a
worker, and the fail-closed reading of stored routes (what the worker is
vended, what the admin sees). The database reads are covered in
``tests/integration/test_1759_channel_routes_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.connector_channel_routes import (
    CHANNEL_ROUTES_CAPABILITY,
    MAX_CHANNEL_ROUTES,
    ChannelRouteInput,
    effective_routing_from_rows,
    effective_unrouted,
    parse_capabilities,
    parse_channel_routes,
    route_views_from_rows,
)
from utils.exceptions import ValidationError

WS = uuid4()


def _row(
    channel_id: str,
    action: str = "route",
    context_id=None,
    *,
    workspace_id=WS,
    deleted_at=None,
    name: str | None = "sales",
    is_private: bool | None = True,
):
    """A stored route joined with its context (None columns = no context row)."""
    has_context = context_id is not None and workspace_id is not None
    return SimpleNamespace(
        channel_id=channel_id,
        action=action,
        context_id=context_id,
        context_workspace_id=workspace_id if has_context else None,
        context_deleted_at=deleted_at if has_context else None,
        context_name=name if has_context else None,
        context_is_private=is_private if has_context else None,
    )


# ------------------------------------------------------------------ shape


def test_null_and_empty_mean_no_routes():
    assert parse_channel_routes(None) == []
    assert parse_channel_routes([]) == []


def test_action_defaults_to_route_and_ids_are_parsed():
    ctx = uuid4()
    parsed = parse_channel_routes(
        [{"channel_id": "C01", "context_id": str(ctx)}, {"channel_id": "C02", "action": "skip"}]
    )
    assert parsed == [
        ChannelRouteInput(channel_id="C01", action="route", context_id=ctx),
        ChannelRouteInput(channel_id="C02", action="skip", context_id=None),
    ]


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([{"channel_id": "", "context_id": str(uuid4())}], id="blank-id"),
        pytest.param([{"channel_id": "   ", "action": "skip"}], id="whitespace-id"),
        pytest.param([{"channel_id": "C" * 65, "action": "skip"}], id="long-id"),
        pytest.param([{"channel_id": 123, "action": "skip"}], id="non-string-id"),
        pytest.param([{"channel_id": "C01"}], id="route-without-context"),
        pytest.param(
            [{"channel_id": "C01", "action": "route", "context_id": None}], id="route-null"
        ),
        pytest.param(
            [{"channel_id": "C01", "action": "skip", "context_id": str(uuid4())}],
            id="skip-with-context",
        ),
        pytest.param([{"channel_id": "C01", "action": "archive"}], id="unknown-action"),
        pytest.param(
            [{"channel_id": "C01", "action": "skip"}, {"channel_id": "C01", "action": "skip"}],
            id="duplicate",
        ),
        pytest.param([{"channel_id": "C01", "context_id": "not-a-uuid"}], id="bad-uuid"),
        pytest.param(["C01"], id="not-an-object"),
        pytest.param("C01", id="not-a-list"),
    ],
)
def test_malformed_routes_are_refused_on_the_field(items):
    with pytest.raises(ValidationError) as exc:
        parse_channel_routes(items)
    assert exc.value.details.get("field") == "channel_routes"


def test_the_list_is_capped():
    items = [{"channel_id": f"C{i:05d}", "action": "skip"} for i in range(MAX_CHANNEL_ROUTES)]
    assert len(parse_channel_routes(items)) == MAX_CHANNEL_ROUTES
    items.append({"channel_id": "CEXTRA", "action": "skip"})
    with pytest.raises(ValidationError):
        parse_channel_routes(items)


def test_a_refusal_names_the_channel_not_a_context():
    with pytest.raises(ValidationError) as exc:
        parse_channel_routes([{"channel_id": "C0SECRET", "action": "route"}])
    assert "C0SECRET" in str(exc.value)


# ------------------------------------------------------------ capability


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("channel_routes", frozenset({CHANNEL_ROUTES_CAPABILITY})),
        (" channel_routes , future_thing,", frozenset({"channel_routes", "future_thing"})),
        ("CHANNEL_ROUTES", frozenset({"CHANNEL_ROUTES"})),
    ],
)
def test_capabilities_are_comma_separated_tokens(raw, expected):
    assert parse_capabilities(raw) == expected


# ------------------------------------------------------- the switch value


@pytest.mark.parametrize(
    ("stored", "effective"),
    [("default", "default"), ("skip", "skip"), (None, "skip"), ("", "skip"), ("DEFAULT", "skip")],
)
def test_an_unknown_switch_value_is_skip(stored, effective):
    assert effective_unrouted(stored) == effective


# ------------------------------------------------- what the worker is vended


def test_no_rows_and_default_switch_do_not_use_routing():
    routing = effective_routing_from_rows([], WS, "default")
    assert routing.uses_routing is False
    assert routing.vend_routes() == []
    assert routing.unrouted_private_channels == "default"


def test_the_switch_alone_uses_routing():
    routing = effective_routing_from_rows([], WS, "skip")
    assert routing.uses_routing is True


def test_a_live_route_in_the_workspace_is_vended_with_its_context():
    ctx = uuid4()
    routing = effective_routing_from_rows([_row("C02", "route", ctx)], WS, "default")
    assert routing.uses_routing is True
    assert routing.vend_routes() == [{"channel_id": "C02", "action": "route", "context_id": ctx}]


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_row("C01", "route", None), id="hard-deleted (SET NULL)"),
        pytest.param(_row("C01", "route", uuid4(), deleted_at=datetime(2026, 1, 1)), id="soft"),
        pytest.param(_row("C01", "route", uuid4(), workspace_id=uuid4()), id="other-workspace"),
        pytest.param(_row("C01", "route", uuid4(), workspace_id=None), id="context-row-gone"),
        pytest.param(_row("C01", "archive", None), id="unknown-action"),
        pytest.param(_row("C01", "skip", None), id="skip"),
    ],
)
def test_anything_but_a_live_destination_is_vended_as_skip(row):
    """Fail closed: a channel with a route row never falls back to the
    default context, and a context outside the workspace is never vended."""
    routing = effective_routing_from_rows([row], WS, "default")
    assert routing.uses_routing is True
    assert routing.vend_routes() == [{"channel_id": "C01", "action": "skip"}]


def test_vended_routes_are_ordered_by_channel():
    a, b = uuid4(), uuid4()
    routing = effective_routing_from_rows(
        [_row("C9", "route", a), _row("C1", "skip"), _row("C5", "route", b)], WS, "default"
    )
    assert [r["channel_id"] for r in routing.vend_routes()] == ["C1", "C5", "C9"]


def test_the_digest_follows_the_effective_routes_and_the_switch():
    ctx = uuid4()
    live = effective_routing_from_rows([_row("C1", "route", ctx)], WS, "default")
    same = effective_routing_from_rows([_row("C1", "route", ctx)], WS, "default")
    gone = effective_routing_from_rows(
        [_row("C1", "route", ctx, deleted_at=datetime(2026, 1, 1))], WS, "default"
    )
    hard_gone = effective_routing_from_rows([_row("C1", "route", None)], WS, "default")
    switched = effective_routing_from_rows([_row("C1", "route", ctx)], WS, "skip")
    assert live.digest() == same.digest()
    assert len({live.digest(), gone.digest(), switched.digest()}) == 3
    # A soft-deleted and a hard-deleted destination are both "skip".
    assert gone.digest() == hard_gone.digest()


# --------------------------------------------------- what the admin is shown


def test_views_report_status_and_hide_what_is_outside_the_workspace():
    ok_ctx, soft_ctx, foreign_ctx = uuid4(), uuid4(), uuid4()
    views = route_views_from_rows(
        [
            _row("C1", "route", ok_ctx, name="sales", is_private=True),
            _row("C2", "route", soft_ctx, deleted_at=datetime(2026, 1, 1), name="old"),
            _row("C3", "route", foreign_ctx, workspace_id=uuid4(), name="theirs"),
            _row("C4", "route", None),
            _row("C5", "skip"),
            _row("C6", "archive"),
        ],
        WS,
    )
    by_id = {v.channel_id: v for v in views}
    assert by_id["C1"].status == "ok"
    assert (by_id["C1"].context_id, by_id["C1"].context_name) == (ok_ctx, "sales")
    assert by_id["C1"].context_is_private is True
    # A soft-deleted destination in the workspace keeps its id, not its name.
    assert by_id["C2"].status == "destination_missing"
    assert (by_id["C2"].context_id, by_id["C2"].context_name) == (soft_ctx, None)
    # Another workspace's context: neither its id nor its name.
    assert by_id["C3"].status == "destination_missing"
    assert (by_id["C3"].context_id, by_id["C3"].context_name) == (None, None)
    assert by_id["C3"].context_is_private is None
    assert by_id["C4"].status == "destination_missing"
    assert (by_id["C5"].action, by_id["C5"].status) == ("skip", "ok")
    # An unknown stored action reads as what it does: skip.
    assert (by_id["C6"].action, by_id["C6"].status) == ("skip", "ok")
