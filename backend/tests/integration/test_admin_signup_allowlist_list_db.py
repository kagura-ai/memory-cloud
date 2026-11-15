"""The paged, searchable admin allowlist list on real rows (#2831).

Other tests leave allowlist rows in the session's database, so every test but
the first scopes its rows with a unique token in ``subject_label``; the first
one seeds rows newer than anything else can be.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.signup_gate import SignupAllowlistEntry

from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

LIST = "/api/v1/admin/signup-gate/allowlist"
ENTRY_KEYS = {
    "id",
    "provider",
    "subject_id",
    "subject_label",
    "github_user_id",
    "github_username",
    "source",
    "state",
    "added_by_user_id",
    "created_at",
}


def _entry(
    label: str,
    *,
    provider: str = "github",
    source: str = "manual",
    state: str = "active",
    created_at: datetime | None = None,
) -> SignupAllowlistEntry:
    subject = uuid4().hex
    entry = SignupAllowlistEntry(
        id=uuid4(),
        provider=provider,
        subject_id=subject,
        subject_label=label,
        github_user_id=subject[:32],
        github_username=f"legacy-{subject[:8]}",
        source=source,
        state=state,
        added_by_user_id="admin_runner",
    )
    if created_at is not None:
        entry.created_at = created_at
    return entry


async def test_without_parameters_answers_the_first_50_rows_newest_first_with_total(
    db_session: AsyncSession,
) -> None:
    start = datetime(2099, 1, 1)
    rows = [
        _entry(f"newest-{index:02d}", created_at=start + timedelta(minutes=index))
        for index in range(55)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    table_total = (
        await db_session.execute(select(func.count()).select_from(SignupAllowlistEntry))
    ).scalar_one()

    async with admin_http(db_session) as client:
        response = await client.get(LIST)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["limit"], body["offset"]) == (50, 0)
    assert body["total"] == table_total >= 55
    assert [item["id"] for item in body["items"]] == [str(row.id) for row in rows[:4:-1]]
    assert set(body["items"][0]) == ENTRY_KEYS
    assert body["items"][0]["created_at"].endswith("Z")


async def test_walking_all_pages_returns_every_row_once_when_created_at_is_shared(
    db_session: AsyncSession,
) -> None:
    token = uuid4().hex
    same = datetime(2031, 6, 6, 8, 0, 0)
    rows = [_entry(f"{token}-tie-{index}", created_at=same) for index in range(5)]
    rows += [
        _entry(f"{token}-before", created_at=same - timedelta(hours=1)),
        _entry(f"{token}-after", created_at=same + timedelta(hours=1)),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    seen: list[str] = []
    async with admin_http(db_session) as client:
        for offset in range(0, 10, 2):
            response = await client.get(
                LIST, params={"search": token, "limit": 2, "offset": offset}
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 7
            assert (body["limit"], body["offset"]) == (2, offset)
            seen += [item["id"] for item in body["items"]]

    ties = sorted(str(row.id) for row in rows[:5])
    # created_at descending, then id.
    assert seen == [str(rows[6].id), *ties, str(rows[5].id)]


async def test_search_matches_wildcards_literally_and_ignores_case(
    db_session: AsyncSession,
) -> None:
    token = uuid4().hex
    labels = [
        f"{token} Sale 50% Off",
        f"{token} Sale 500 Off",
        f"{token} snake_case",
        f"{token} snakeXcase",
        f"{token} back\\slash",
    ]
    db_session.add_all([_entry(label) for label in labels])
    await db_session.commit()

    async def found(search: str) -> tuple[list[str], int]:
        response = await client.get(LIST, params={"search": search})
        assert response.status_code == 200, response.text
        body = response.json()
        return sorted(item["subject_label"] for item in body["items"]), body["total"]

    async with admin_http(db_session) as client:
        assert await found(f"{token} sale 50%") == ([labels[0]], 1)
        assert await found(f"{token} SNAKE_") == ([labels[2]], 1)
        assert await found(f"{token} back\\") == ([labels[4]], 1)
        assert await found(f"  {token.upper()} SALE  ") == (labels[:2], 2)
        assert (await found(token))[1] == 5
        # A wildcard alone is a character, not "anything".
        everything = (await found(""))[1]
        assert (await found("   "))[1] == everything
        assert (await found("%"))[1] < everything
        assert (await found("_"))[1] < everything


async def test_search_does_not_read_subject_id_or_the_legacy_fields(
    db_session: AsyncSession,
) -> None:
    token = uuid4().hex
    entry = _entry("plain label")
    entry.subject_id = f"sub-{token}"
    entry.github_username = f"legacy-{token}"
    db_session.add(entry)
    await db_session.commit()

    async with admin_http(db_session) as client:
        response = await client.get(LIST, params={"search": token})

    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_provider_source_and_state_filter_exactly_and_combine_with_search(
    db_session: AsyncSession,
) -> None:
    token = uuid4().hex
    db_session.add_all(
        [
            _entry(f"{token} a", provider="github", source="manual", state="active"),
            _entry(f"{token} b", provider="google", source="manual", state="active"),
            _entry(f"{token} c", provider="email", source="beta_invite", state="active"),
            _entry(f"{token} d", provider="github", source="beta_invite", state="revoked"),
            _entry(f"{token} e", provider="github", source="github_sponsors", state="grace"),
        ]
    )
    await db_session.commit()

    async def found(**params: str) -> tuple[list[str], int]:
        response = await client.get(LIST, params={"search": token, **params})
        assert response.status_code == 200, response.text
        body = response.json()
        return sorted(item["subject_label"][-1] for item in body["items"]), body["total"]

    async with admin_http(db_session) as client:
        assert await found() == (["a", "b", "c", "d", "e"], 5)
        assert await found(provider="github") == (["a", "d", "e"], 3)
        assert await found(provider="google") == (["b"], 1)
        assert await found(provider="email") == (["c"], 1)
        assert await found(source="beta_invite") == (["c", "d"], 2)
        assert await found(source="github_sponsors") == (["e"], 1)
        assert await found(state="active") == (["a", "b", "c"], 3)
        assert await found(state="grace") == (["e"], 1)
        assert await found(provider="github", source="beta_invite", state="revoked") == (["d"], 1)
        assert await found(provider="google", source="beta_invite") == ([], 0)
        # total counts the filtered rows, not the page.
        response = await client.get(
            LIST, params={"search": token, "provider": "github", "limit": 1}
        )
        assert len(response.json()["items"]) == 1
        assert response.json()["total"] == 3


async def test_reserved_columns_are_not_in_the_answer(db_session: AsyncSession) -> None:
    token = uuid4().hex
    entry = _entry(f"{token} reserved", state="grace")
    entry.sponsor_tier_cents = 987_654_321
    entry.grace_until = datetime(2077, 7, 7, 7, 7, 7)
    entry.revoked_at = datetime(2078, 8, 8, 8, 8, 8)
    db_session.add(entry)
    await db_session.commit()

    async with admin_http(db_session) as client:
        response = await client.get(LIST, params={"search": token})

    (item,) = response.json()["items"]
    assert set(item) == ENTRY_KEYS
    for forbidden in ("987654321", "2077-07-07", "2078-08-08", "sponsor_tier_cents"):
        assert forbidden not in response.text
