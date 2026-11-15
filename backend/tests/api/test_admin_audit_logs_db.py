"""Admin read of the audit log against Postgres (#2835).

``api.main.app`` with ``get_db`` on the test database and real ``audit_logs``
rows, one per listed action as its writer stores it. Every column the route
does not answer is seeded with a unique token, and each test reads the raw
response body for it.

The seeded rows are the newest of the table (the highest ids), so they lead
every answer whatever other tests left behind, and are removed afterwards.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from api.routes.admin_audit_logs import AUDIT_LOG_ACTIONS, PLAIN_VALUE_ACTIONS
from auth.dependencies import get_current_user_optional
from auth.system_admin_mode import build_operator_principal
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog

pytestmark = pytest.mark.asyncio(loop_scope="session")

URL = "/api/v1/admin/audit-logs"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
OPERATOR = "ops@example.com"
ENTRY_KEYS = {
    "id",
    "created_at",
    "action",
    "user_id",
    "user_email",
    "resource",
    "old_value",
    "new_value",
    "metadata",
}
UNLISTED = [
    "terms.accepted",
    "user.signed_up",
    "role_assign",
    "capacity_purge",
    "public_bound_key_created",
]

# One row per listed action, as its writer stores it:
# (user_id, user_email, resource, old value, new value, user_metadata).
STORED: dict[str, tuple[str, str, str, str | None, str | None, dict[str, Any]]] = {
    "system_role_update": (
        "1001",
        "admin@example.com",
        "user:local:alice",
        "user",
        "read_only",
        {
            "actor_user_id": "1001",
            "target_user_id": "local:alice",
            "old_role": "user",
            "new_role": "read_only",
        },
    ),
    "system_admin_promote": (
        "local:alice",
        "admin@example.com",
        "user:alice@example.com",
        "user",
        "admin",
        {"promoted_by": "admin@example.com", "is_initial_admin": False},
    ),
    "system_admin_demote": (
        "local:alice",
        "admin@example.com",
        "user:alice@example.com",
        "admin",
        "user",
        {"demoted_by": "admin@example.com", "was_initial_admin": False},
    ),
    "workspace_slot_bonus_update": (
        "1001",
        "admin@example.com",
        "user:alice@example.com",
        "0",
        "2",
        {
            "actor_user_id": "1001",
            "target_user_id": "local:alice",
            "before_value": 0,
            "after_value": 2,
            "delta": 2,
            "reason": "agreed with support",
        },
    ),
    "referral_grant_revoke": (
        "1001",
        "admin@example.com",
        "referral_grant:8a1c6c0e-58a5-4a4b-9a55-0b9b6a1d2f01",
        "400",
        "0",
        {
            "actor_user_id": "1001",
            "referrer_user_id": "local:bob",
            "referred_user_id": "local:carol",
            "referrer_bonus_memories": 200,
            "referred_bonus_memories": 200,
            "reason": "duplicate account",
        },
    ),
    "workspace_ownership_force_transfer": (
        "1003",
        "second-admin@example.com",
        "workspace:5d5b1c50-4c6b-4f0e-8a41-3f0f6a7f3c02",
        None,
        None,
        {
            "break_glass": True,
            "reason": "owner left the company",
            "previous_owner_id": "local:bob",
            "new_owner_id": "local:carol",
            "ownership_epoch": 3,
            "target_was_member": True,
            "self_transfer": False,
            "dual_control": True,
            "force_transfer_request_id": "0d6b1f3e-1111-4222-8333-444455556666",
            "initiated_by_user_id": "1001",
            "initiated_by_email": "admin@example.com",
            "approved_by_user_id": "1003",
        },
    ),
    "workspace_ownership_transfer": (
        "local:bob",
        "bob@example.com",
        "workspace:5d5b1c50-4c6b-4f0e-8a41-3f0f6a7f3c02",
        None,
        None,
        {"previous_owner_id": "local:bob", "new_owner_id": "local:carol", "ownership_epoch": 4},
    ),
    "workspace_purged": (
        "1001",
        "admin@example.com",
        "workspace:5d5b1c50-4c6b-4f0e-8a41-3f0f6a7f3c02",
        None,
        None,
        {
            "previous_owner_id": "local:bob",
            "soft_deleted": True,
            "contexts": 2,
            "memories": 40,
            "billing_contract_active": False,
        },
    ),
    "account_erasure": (
        "9f" * 32,
        "7c" * 32,
        "user_pseudonym:" + "9f" * 8,
        None,
        None,
        {
            "request_id": "0d6b1f3e-aaaa-4bbb-8ccc-ddddeeeeffff",
            "is_self_service": False,
            "initiated_by": "1001",
            "reason_code": "legal_order",
            "deleted_data_summary": {"postgres": {"memories": 40}, "qdrant": {"points": 40}},
        },
    ),
    "context_restore": (
        "1001",
        "admin@example.com",
        "context:1b2c3d4e-0000-4000-8000-000000000042",
        None,
        None,
        {
            "workspace_id": "5d5b1c50-4c6b-4f0e-8a41-3f0f6a7f3c02",
            "name": "release-notes",
            "renamed_from": None,
            "deleted_at": "2026-11-10T08:00:00Z",
            "deleted_by": "local:bob",
            "memories_restored": 12,
            "memories_left_deleted": 1,
            "files_restored": 0,
            "file_bytes_restored": 0,
        },
    ),
    "bm25_drift_reveal_terms": (
        "1001",
        "admin@example.com",
        "bm25_drift_log:17",
        None,
        None,
        {
            "outcome": "revealed",
            "context_id": "1b2c3d4e-0000-4000-8000-000000000042",
            "reason": "drift investigation",
            "num_terms_resolved": 3,
            "num_terms_total": 5,
        },
    ),
    "mcp_notice.created": (
        "1001",
        "admin@example.com",
        "mcp_notice:6f0e1d2c-0000-4000-8000-000000000007",
        None,
        "ab" * 32,
        {
            "text_chars": 42,
            "starts_at": "2026-11-15T09:00:00+00:00",
            "expires_at": "2026-11-20T09:00:00+00:00",
            "plans": ["free", "pro"],
        },
    ),
    "mcp_notice.withdrawn": (
        "1001",
        "admin@example.com",
        "mcp_notice:6f0e1d2c-0000-4000-8000-000000000007",
        None,
        "ab" * 32,
        {"text_chars": 42},
    ),
    "signup_blocked": (
        "google:104",
        "oauth-callback",
        "signup_gate:google",
        None,
        "cd" * 32,
        {"provider": "google"},
    ),
    "signup_admitted": (
        "google:105",
        "oauth-callback",
        "signup_gate:google",
        None,
        "ef" * 32,
        {
            "provider": "google",
            "admission": "workspace_invitation",
            "invitations": [
                {
                    "id": "inv_3kT9",
                    "workspace_id": "5d5b1c50-4c6b-4f0e-8a41-3f0f6a7f3c02",
                    "invited_by": "local:bob",
                }
            ],
            "invitation_count": 1,
        },
    ),
    "beta_invite.redeemed": (
        "local:dave",
        "email-signup",
        "beta_invite:2c1b0a99-0000-4000-8000-000000000003",
        None,
        "0a" * 32,
        {"provider": "email", "inviter_user_id": "local:bob", "allowlist_entry_id": 31},
    ),
}


def _token() -> str:
    return "tok" + secrets.token_hex(12)


def _answered_metadata(action: str) -> dict[str, Any]:
    metadata = copy.deepcopy(STORED[action][5])
    metadata.pop("deleted_data_summary", None)
    return metadata


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    ids: list[int] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def token(self) -> str:
        value = _token()
        self.tokens.append(value)
        return value

    async def seed(self, action: str, **overrides: Any) -> int:
        """Insert one row; ``ip_address`` and ``user_agent`` hold their own tokens."""
        user_id, user_email, resource, old, new, metadata = STORED.get(
            action, ("local:x", "x@example.com", "thing:1", None, None, {})
        )
        values: dict[str, Any] = {
            "user_id": user_id,
            "user_email": user_email,
            "action": action,
            "resource": resource,
            "old_value_hash": old,
            "new_value_hash": new,
            "user_metadata": copy.deepcopy(metadata),
            "ip_address": self.token(),
            "user_agent": self.token(),
        }
        values.update(overrides)
        async with self.factory() as db:
            row_id = (
                await db.execute(insert(AuditLog).values(**values).returning(AuditLog.id))
            ).scalar_one()
            await db.commit()
        self.ids.append(row_id)
        return row_id

    async def seed_each_listed(self, **overrides: Any) -> dict[str, int]:
        return {action: await self.seed(action, **overrides) for action in STORED}

    async def get(self, **kw: Any) -> httpx.Response:
        return await self.client.get(URL, **kw)

    async def pages(self, **params: Any) -> list[httpx.Response]:
        """Follow ``next_before_id`` to the last page."""
        responses: list[httpx.Response] = []
        cursor: dict[str, Any] = {}
        while True:
            response = await self.get(params={**params, **cursor})
            assert response.status_code == 200, response.text
            responses.append(response)
            following = response.json()["next_before_id"]
            if following is None:
                return responses
            assert following == response.json()["entries"][-1]["id"]
            cursor = {"before_id": following}

    def assert_no_token(self, *responses: httpx.Response) -> None:
        assert self.tokens
        for response in responses:
            for token in self.tokens:
                assert token not in response.text
            for name in ("ip_address", "user_agent", "_hash", "user_metadata"):
                assert name not in response.text


@pytest.fixture
async def world(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            yield db

    async def _admin() -> dict:
        return dict(ADMIN_SESSION)

    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    transport = httpx.ASGITransport(app=app)
    state: World | None = None
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            state = World(factory=maker, client=client)
            yield state
    finally:
        app.dependency_overrides.clear()
        if state is not None and state.ids:
            async with maker() as db:
                await db.execute(delete(AuditLog).where(AuditLog.id.in_(state.ids)))
                await db.commit()


def test_the_fixture_covers_the_allow_list() -> None:
    assert set(STORED) == AUDIT_LOG_ACTIONS
    assert not set(UNLISTED) & AUDIT_LOG_ACTIONS


async def test_each_listed_action_is_answered_as_stored(world: World) -> None:
    # N1: a token in ``ip_address`` and ``user_agent`` of a row of every listed action
    seeded = await world.seed_each_listed()
    response = await world.get(params={"limit": 100})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"entries", "next_before_id"}
    entries = body["entries"][: len(STORED)]
    # newest first
    assert [entry["id"] for entry in entries] == sorted(seeded.values(), reverse=True)
    for entry in entries:
        action = entry["action"]
        user_id, user_email, resource, old, new, _ = STORED[action]
        assert set(entry) == ENTRY_KEYS
        assert entry["id"] == seeded[action]
        assert (entry["user_id"], entry["user_email"], entry["resource"]) == (
            user_id,
            user_email,
            resource,
        )
        assert entry["metadata"] == _answered_metadata(action)
        if action in PLAIN_VALUE_ACTIONS:
            assert (entry["old_value"], entry["new_value"]) == (old, new)
        else:
            assert (entry["old_value"], entry["new_value"]) == (None, None)
        # ``created_at`` carries its zone
        assert entry["created_at"].endswith("Z")
        assert datetime.fromisoformat(entry["created_at"]).utcoffset().total_seconds() == 0
    world.assert_no_token(response)
    assert "deleted_data_summary" not in response.text
    # the sign-up rows' HMACs and the notice rows' digest
    for action in sorted(AUDIT_LOG_ACTIONS - PLAIN_VALUE_ACTIONS):
        stored_value = STORED[action][4]
        assert stored_value is None or stored_value not in response.text, action


async def test_the_value_columns_of_the_other_actions_are_never_answered(world: World) -> None:
    # N2: a token in both value columns of every action that is not a plain-value one
    for action in sorted(AUDIT_LOG_ACTIONS - PLAIN_VALUE_ACTIONS):
        await world.seed(action, old_value_hash=world.token(), new_value_hash=world.token())
    plain = {action: await world.seed(action) for action in sorted(PLAIN_VALUE_ACTIONS)}
    response = await world.get(params={"limit": 100})
    assert response.status_code == 200, response.text
    entries = {entry["id"]: entry for entry in response.json()["entries"]}
    for row_id in world.ids:
        entry = entries[row_id]
        if row_id in plain.values():
            assert (entry["old_value"], entry["new_value"]) == STORED[entry["action"]][3:5]
        else:
            assert (entry["old_value"], entry["new_value"]) == (None, None)
    answered_null = {entries[i]["action"] for i in world.ids if i not in plain.values()}
    assert {"signup_blocked", "signup_admitted", "beta_invite.redeemed"} <= answered_null
    assert {"mcp_notice.created", "mcp_notice.withdrawn"} <= answered_null
    world.assert_no_token(response)


async def test_metadata_is_answered_through_the_key_list(world: World) -> None:
    # N3: an extra key, a nested object and a nested list in every row's metadata
    seeded: dict[str, int] = {}
    for action in STORED:
        metadata = copy.deepcopy(STORED[action][5])
        metadata["added_later"] = world.token()
        metadata["nested"] = {"inner": world.token(), "deeper": {"inner": world.token()}}
        metadata["items"] = [world.token(), {"inner": world.token()}]
        if action == "account_erasure":
            metadata["deleted_data_summary"] = {
                "note": world.token(),
                "workspaces": {"transferred": [{"new_owner_user_id": world.token()}]},
                "pseudonym_salt": world.token(),
            }
        if action == "signup_admitted":
            metadata["invitations"][0]["email"] = world.token()
            metadata["invitations"][0]["extra"] = {"inner": world.token()}
        seeded[action] = await world.seed(action, user_metadata=metadata)
    response = await world.get(params={"limit": 100})
    assert response.status_code == 200, response.text
    entries = {entry["id"]: entry for entry in response.json()["entries"]}
    for action, row_id in seeded.items():
        # the listed keys are intact
        assert entries[row_id]["metadata"] == _answered_metadata(action), action
    world.assert_no_token(response)
    for name in ("added_later", "nested", "deleted_data_summary", "pseudonym_salt"):
        assert name not in response.text


async def test_metadata_that_is_not_an_object_answers_null(world: World) -> None:
    empty = await world.seed("signup_blocked", user_metadata=None)
    text = await world.seed("signup_blocked", user_metadata=[world.token()])
    response = await world.get(params={"limit": 2})
    assert [(e["id"], e["metadata"]) for e in response.json()["entries"]] == [
        (text, None),
        (empty, None),
    ]
    world.assert_no_token(response)


async def test_an_action_outside_the_allow_list_is_never_returned(world: World) -> None:
    # N4: rows of five unlisted actions with a token in every column, beside one
    # listed row that shares their ``user_id``, ``user_email`` and ``resource``.
    actor, address, resource = world.token(), world.token(), world.token()
    when = datetime(2097, 1, 1, 12, 0, 0)
    shared = {
        "user_id": actor,
        "user_email": address,
        "resource": resource,
        "created_at": when,
    }
    control = await world.seed(
        "signup_blocked", user_metadata={"provider": "google"}, new_value_hash=None, **shared
    )
    hidden = [
        await world.seed(
            action,
            old_value_hash=world.token(),
            new_value_hash=world.token(),
            user_metadata={"provider": world.token(), "reason": world.token()},
            **shared,
        )
        for action in UNLISTED
    ]
    # the three shared values are answered for the listed row: not tokens to look for
    for value in (actor, address, resource):
        world.tokens.remove(value)

    def ids(response: httpx.Response) -> list[int]:
        return [entry["id"] for entry in response.json()["entries"]]

    # each filter alone
    for params in (
        {"user_id": actor},
        {"user_email": address},
        {"resource": resource},
        {"from": "2097-01-01T00:00:00Z"},
        {"from": "2097-01-01T12:00:00Z", "to": "2097-01-01T12:00:01Z"},
    ):
        response = await world.get(params=params)
        assert response.status_code == 200, response.text
        assert ids(response) == [control], params
        assert response.json()["next_before_id"] is None
        world.assert_no_token(response)
    for params in ({"to": "2097-01-02T00:00:00Z"}, {"before_id": max(hidden) + 1}, {}):
        response = await world.get(params={**params, "limit": 100})
        assert response.status_code == 200, response.text
        assert control in ids(response), params
        assert not set(hidden) & set(ids(response)), params
        world.assert_no_token(response)

    # ``before_id`` paging to the end, with no filter at all
    pages = await world.pages(limit=100)
    seen = [row_id for page in pages for row_id in ids(page)]
    assert control in seen
    assert not set(hidden) & set(seen)
    assert len(seen) == len(set(seen))
    world.assert_no_token(*pages)
    async with world.factory() as db:
        listed_rows = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.in_(sorted(AUDIT_LOG_ACTIONS)))
        )
    assert len(seen) == listed_rows

    # asking for one is refused
    for action in UNLISTED:
        refused = await world.get(params={"action": action})
        assert refused.status_code == 422, action
        mixed = await world.get(params={"action": ["signup_blocked", action]})
        assert mixed.status_code == 422, action


async def test_a_row_written_in_service_mode_shows_the_operator(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N5: the actor strings a writer gets from the operator principal
    principal = build_operator_principal(OPERATOR)
    row_id = await world.seed(
        "system_role_update",
        user_id=principal["user_id"],
        user_email=principal["email"],
        user_metadata={
            "actor_user_id": principal["user_id"],
            "target_user_id": "local:alice",
            "old_role": "user",
            "new_role": "read_only",
        },
    )
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    app.dependency_overrides.pop(get_current_user_optional, None)
    headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}

    response = await world.get(params={"user_id": f"operator:{OPERATOR}"}, headers=headers)
    assert response.status_code == 200, response.text
    [entry] = [e for e in response.json()["entries"] if e["id"] == row_id]
    assert entry["user_id"] == entry["user_email"] == f"operator:{OPERATOR}"
    assert entry["metadata"]["actor_user_id"] == f"operator:{OPERATOR}"
    world.assert_no_token(response)
    # as every gated route
    assert (await world.get()).status_code == 401
    monkeypatch.setattr(settings, "system_admin_mode", "off")
    assert (await world.get(headers=headers)).status_code == 404


async def test_before_id_pages_through_the_matching_rows_once_newest_first(world: World) -> None:
    actor = "local:" + _token()
    actions = ["signup_blocked", "system_role_update", "signup_blocked", "account_erasure"] * 2
    seeded = [await world.seed(action, user_id=actor) for action in actions[:7]]
    await world.seed("signup_blocked")  # another actor's row, newer than all of them
    await world.seed("terms.accepted", user_id=actor)  # the same actor, not listed

    pages = await world.pages(user_id=actor, limit=3)
    assert [[e["id"] for e in page.json()["entries"]] for page in pages] == [
        seeded[:3:-1],
        seeded[3:0:-1],
        seeded[:1],
    ]
    assert [page.json()["next_before_id"] for page in pages] == [seeded[4], seeded[1], None]

    # a page that ends exactly on the last matching row has no next page
    blocked = [
        row_id
        for row_id, action in zip(seeded, actions, strict=False)
        if action == "signup_blocked"
    ]
    assert len(blocked) == 4
    exact = await world.pages(user_id=actor, action="signup_blocked", limit=4)
    assert [[e["id"] for e in page.json()["entries"]] for page in exact] == [blocked[::-1]]
    two = await world.pages(user_id=actor, action="signup_blocked", limit=2)
    assert [[e["id"] for e in page.json()["entries"]] for page in two] == [
        blocked[:1:-1],
        blocked[1::-1],
    ]
    world.assert_no_token(*pages, *exact, *two)


async def test_the_filters_combine(world: World) -> None:
    actor, other = "local:" + _token(), "local:" + _token()
    resource = "workspace:" + _token()
    early, late = datetime(2097, 3, 1, 9, 0, 0), datetime(2097, 3, 1, 10, 0, 0)
    target = await world.seed("workspace_purged", user_id=actor, resource=resource, created_at=late)
    await world.seed("workspace_purged", user_id=other, resource=resource, created_at=late)
    await world.seed("workspace_purged", user_id=actor, created_at=late)
    await world.seed("workspace_purged", user_id=actor, resource=resource, created_at=early)
    transfer = await world.seed(
        "workspace_ownership_transfer", user_id=actor, resource=resource, created_at=late
    )

    def ids(response: httpx.Response) -> list[int]:
        return [entry["id"] for entry in response.json()["entries"]]

    both = {"user_id": actor, "resource": resource, "from": "2097-03-01T09:30:00Z"}
    assert ids(await world.get(params=both)) == [transfer, target]
    one = await world.get(params={**both, "action": "workspace_purged"})
    assert ids(one) == [target]
    assert ids(await world.get(params={**both, "user_email": "admin@example.com"})) == [target]
    assert ids(await world.get(params={**both, "user_email": "bob@example.com"})) == [transfer]
    assert ids(await world.get(params={**both, "before_id": transfer})) == [target]
    assert ids(await world.get(params={**both, "to": "2097-03-01T10:00:00Z"})) == []
    several = {**both, "action": ["workspace_purged", "workspace_ownership_transfer"]}
    assert ids(await world.get(params=several)) == [transfer, target]
    # exact match: no trimming, no case folding, no wildcard
    for value in (actor.upper(), actor + " ", actor[:-1] + "_", actor[:-1] + "%", actor[:-1]):
        assert ids(await world.get(params={"user_id": value})) == []
    world.assert_no_token(one)


async def test_from_is_inclusive_and_to_exclusive_in_any_zone(world: World) -> None:
    actor = "local:" + _token()
    stamps = [datetime(2097, 5, 1, 0, 0, second) for second in (0, 1, 2)]
    first, second, third = [
        await world.seed("signup_blocked", user_id=actor, created_at=stamp) for stamp in stamps
    ]

    async def ids(**params: str) -> list[int]:
        response = await world.get(params={"user_id": actor, **params})
        assert response.status_code == 200, response.text
        return [entry["id"] for entry in response.json()["entries"]]

    assert await ids(**{"from": "2097-05-01T00:00:01Z"}) == [third, second]
    assert await ids(to="2097-05-01T00:00:01Z") == [first]
    assert await ids(**{"from": "2097-05-01T00:00:01Z", "to": "2097-05-01T00:00:02Z"}) == [second]
    assert await ids(**{"from": "2097-05-01T00:00:01Z", "to": "2097-05-01T00:00:01Z"}) == []
    # the same instants written in another zone
    assert await ids(
        **{"from": "2097-05-01T09:00:01+09:00", "to": "2097-04-30T19:00:02-05:00"}
    ) == [second]
    response = await world.get(params={"user_id": actor, "limit": 1})
    assert response.json()["entries"][0]["created_at"] == "2097-05-01T00:00:02Z"


async def test_the_read_writes_no_audit_row(world: World) -> None:
    await world.seed("signup_blocked")

    async def count() -> int:
        async with world.factory() as db:
            return (await db.scalar(select(func.count()).select_from(AuditLog))) or 0

    before = await count()
    assert (await world.get()).status_code == 200
    assert (await world.get(params={"action": "signup_blocked", "limit": 1})).status_code == 200
    assert (await world.get(params={"action": "terms.accepted"})).status_code == 422
    assert await count() == before
