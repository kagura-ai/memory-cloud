"""#2348: exact audit metadata/fallback erasure and historical residue repair."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cli import reprocess_erasures as cli
from models.auth import AuditLog, User
from models.erasure import ErasureRequest
from services.erasure_details import (
    AUDIT_SUBJECT_METADATA_KEYS,
    count_audit_mentions,
    erasure_pseudonym,
    pseudonymize_audit_trail,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def subject(
    async_engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[dict[str, Any]]:
    tag = uuid.uuid4().hex
    subject_kind = getattr(request, "param", None)
    sub = "a" * 64 if subject_kind == "hex-sub" else f"audit-subject-{tag}"
    if subject_kind == "email-sub":
        sub += "@example.invalid"

    with patch("services.account_erasure_service._audit_salt", return_value="2348-test-salt"):
        pseudonym = erasure_pseudonym(sub)
        erasure = ErasureRequest(
            id=uuid.uuid4(),
            user_id=pseudonym,
            user_email_hash="0" * 64,
            initiated_by="admin",
            is_self_service=False,
            reason_code="other",
            status="complete",
            completed_at=utcnow(),
            deleted_data_summary={"postgres": {"users": 1}},
        )
        async with _maker(async_engine)() as db:
            db.add(erasure)
            await db.commit()
        try:
            yield {"sub": sub, "p": pseudonym, "request": erasure.id}
        finally:
            async with _maker(async_engine)() as db:
                await db.execute(delete(AuditLog).where(AuditLog.action == sub))
                await db.execute(delete(AuditLog).where(AuditLog.user_id == pseudonym))
                await db.execute(delete(ErasureRequest).where(ErasureRequest.id == erasure.id))
                await db.execute(delete(User).where(User.user_id == sub))
                await db.commit()


def _audit(
    subject: dict[str, Any],
    actor: str,
    *,
    email: str = "other@example.invalid",
    metadata: Any = None,
) -> AuditLog:
    return AuditLog(
        user_id=actor,
        user_email=email,
        action=subject["sub"],
        resource="workspace:unrelated",
        user_metadata=metadata,
        ip_address="192.0.2.4",
        user_agent="other-agent",
    )


async def _snapshot(db: AsyncSession, subject: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = (
        await db.execute(select(AuditLog.__table__).where(AuditLog.action == subject["sub"]))
    ).mappings()
    return {row["id"]: dict(row) for row in rows}


async def test_live_erasure_only_rewrites_exact_known_json_strings(
    async_engine: AsyncEngine, subject: dict[str, Any]
) -> None:
    sub, pseudonym = subject["sub"], subject["p"]
    own = [
        _audit(subject, sub, email=e, metadata=dict.fromkeys(AUDIT_SUBJECT_METADATA_KEYS, sub))
        for e in (sub, f"{sub}@api")
    ]
    mentions = [
        _audit(
            subject,
            f"other-{i}",
            metadata={key: sub, "workspace_id": str(uuid.uuid4()), "keep": [1, None, False]},
        )
        for i, key in enumerate(AUDIT_SUBJECT_METADATA_KEYS)
    ]
    untouched = [
        _audit(subject, "unrelated", metadata=v)
        for v in (
            None,
            [],
            [sub],
            sub,
            123,
            {"target_user_id": [sub]},
            {"target_user_id": 123},
            {"target_user_id": None},
            {"target_user_id": f"{sub}-other", "nested": {"target_user_id": sub}},
        )
    ]
    async with _maker(async_engine)() as db:
        db.add_all([*own, *mentions, *untouched])
        await db.commit()
        before = await _snapshot(db, subject)
        assert await count_audit_mentions(db, [], subs=[sub], exclude_user_id=sub) == 6
        assert await pseudonymize_audit_trail(db, sub, []) == (2, 6)
        await db.commit()
        after = await _snapshot(db, subject)
        for row in own:
            assert after[row.id] == {
                **before[row.id],
                "user_id": pseudonym,
                "user_email": pseudonym,
                "user_metadata": dict.fromkeys(AUDIT_SUBJECT_METADATA_KEYS, pseudonym),
                "ip_address": None,
                "user_agent": None,
            }
        for row, key in zip(mentions, AUDIT_SUBJECT_METADATA_KEYS, strict=True):
            assert after[row.id] == {
                **before[row.id],
                "user_metadata": {**before[row.id]["user_metadata"], key: pseudonym},
            }
        for row in untouched:
            assert after[row.id] == before[row.id]
        assert await pseudonymize_audit_trail(db, sub, []) == (0, 0)


@pytest.mark.parametrize(
    "location", [*AUDIT_SUBJECT_METADATA_KEYS, "email", "api_email", "resource"]
)
async def test_historical_metadata_or_fallback_only_discovery(
    async_engine: AsyncEngine,
    subject: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    location: str,
) -> None:
    sub, pseudonym = subject["sub"], subject["p"]
    # No audit user_id contains the raw sub: the earlier erasure already scrubbed it.
    row = _audit(subject, pseudonym)
    if location in AUDIT_SUBJECT_METADATA_KEYS:
        row.user_metadata = {location: sub, "untouched": "other-member"}
    elif location in {"email", "api_email"}:
        row.user_email = sub if location == "email" else f"{sub}@api"
    else:
        row.resource = f"user:{sub}"
    async with _maker(async_engine)() as db:
        db.add(row)
        await db.commit()
        before = await _snapshot(db, subject)
        plan = await cli.reprocess_erasures(db, request_id=subject["request"])
        cli._print_plan(plan)
        assert await _snapshot(db, subject) == before
        assert plan.changed[0].audit_mentions_pseudonymized == 1
        applied = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        cli._print_applied(applied)
        assert applied.changed[0].audit_mentions_pseudonymized == 1
        assert applied.changed[0].audit_logs_pseudonymized == 0
        assert applied.changed[0].audit_metadata_cleared == plan.changed[0].audit_metadata_cleared
        await db.refresh(row)
        if location in AUDIT_SUBJECT_METADATA_KEYS:
            assert row.user_metadata == {location: pseudonym, "untouched": "other-member"}
        elif location in {"email", "api_email"}:
            assert row.user_email == pseudonym
        else:
            assert row.resource == f"user:{pseudonym}"
        after = await _snapshot(db, subject)
        second = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        assert second.changes == 0
        assert await _snapshot(db, subject) == after
    assert sub not in capsys.readouterr().out


async def test_own_metadata_counts_once(async_engine: AsyncEngine, subject: dict[str, Any]) -> None:
    sub = subject["sub"]
    async with _maker(async_engine)() as db:
        db.add(_audit(subject, sub, email=f"{sub}@api", metadata={"target_user_id": sub}))
        await db.commit()
        plan = await cli.reprocess_erasures(db, request_id=subject["request"])
        applied = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        for result in (plan, applied):
            assert result.changed[0].audit_logs_pseudonymized == 1
            assert result.changed[0].audit_mentions_pseudonymized == 0


@pytest.mark.parametrize("registered_before_discovery", [True, False])
async def test_re_registration_preserves_all_mentions(
    async_engine: AsyncEngine, subject: dict[str, Any], registered_before_discovery: bool
) -> None:
    sub = subject["sub"]
    async with _maker(async_engine)() as db:
        db.add(_audit(subject, sub, email=f"{sub}@api", metadata={"target_user_id": sub}))
        db.add(_audit(subject, "other", metadata={"assigned_by": sub}))
        await db.commit()
        if not registered_before_discovery:
            candidates = await cli._audit_subs(db)
            assert candidates.orphans[subject["p"]] == sub
        db.add(
            User(
                user_id=sub, email="re-registered@example.invalid", name="new account", role="user"
            )
        )
        await db.commit()
        before = await _snapshot(db, subject)
        if registered_before_discovery:
            result = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
            assert result.audit_kept == [subject["request"]]
        else:
            # Stale discovery must still be harmless in the locked UPDATE itself.
            assert await pseudonymize_audit_trail(
                db, sub, [], orphaned_only=True, mention_subs=[sub]
            ) == (0, 0)
            await db.commit()
        assert await _snapshot(db, subject) == before


@pytest.mark.parametrize(
    "metadata", [None, [], ["raw-sub"], "raw-sub", 123, {"target_user_id": ["raw-sub"]}]
)
async def test_own_non_string_or_non_object_metadata_is_preserved(
    async_engine: AsyncEngine, subject: dict[str, Any], metadata: Any
) -> None:
    async with _maker(async_engine)() as db:
        row = _audit(subject, subject["sub"], metadata=metadata)
        db.add(row)
        await db.commit()
        assert await pseudonymize_audit_trail(db, subject["sub"], []) == (1, 0)
        await db.commit()
        await db.refresh(row)
        assert row.user_metadata == metadata
        assert row.user_email == "other@example.invalid"


@pytest.mark.parametrize("subject", ["hex-sub"], indirect=True)
async def test_raw_hex_subject_is_not_mistaken_for_an_erasure_pseudonym(
    async_engine: AsyncEngine, subject: dict[str, Any]
) -> None:
    async with _maker(async_engine)() as db:
        row = _audit(subject, subject["p"], metadata={"target_user_id": subject["sub"]})
        db.add(row)
        await db.commit()
        candidates = await cli._audit_subs(db, {subject["p"]})
        assert candidates.orphans[subject["p"]] == subject["sub"]
        assert erasure_pseudonym(subject["p"]) not in candidates.orphans
        result = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        assert result.changed[0].audit_mentions_pseudonymized == 1
        await db.refresh(row)
        assert row.user_metadata == {"target_user_id": subject["p"]}


@pytest.mark.parametrize("historical", [False, True])
async def test_escaped_null_legacy_json_cannot_block_erasure(
    async_engine: AsyncEngine, subject: dict[str, Any], historical: bool
) -> None:
    sub, pseudonym = subject["sub"], subject["p"]
    metadata = {
        "target_user_id": sub,
        "free_text": "before\u0000after",
        "nested": {"keep": [False, None, 9]},
    }
    changed = _audit(subject, "other-member", metadata=metadata)
    untouched = _audit(
        subject,
        "another-member",
        metadata={"target_user_id": "unrelated-sub", "free_text": "\u0000"},
    )
    own = _audit(subject, sub, email=f"{sub}@api", metadata={"promoted_by": sub, "keep": "\u0000"})
    async with _maker(async_engine)() as db:
        db.add_all([changed, untouched, own])
        await db.commit()
        before = await _snapshot(db, subject)
        changed_id, untouched_id, own_id = changed.id, untouched.id, own.id
        if historical:
            plan = await cli.reprocess_erasures(db, request_id=subject["request"])
            applied = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
            for result in (plan, applied):
                assert result.changed[0].audit_logs_pseudonymized == 1
                assert result.changed[0].audit_mentions_pseudonymized == 1
        else:
            assert await pseudonymize_audit_trail(db, sub, []) == (1, 1)
            await db.commit()
        after = await _snapshot(db, subject)
        assert after[changed_id] == {
            **before[changed_id],
            "user_metadata": {**metadata, "target_user_id": pseudonym},
        }
        assert after[own_id]["user_metadata"] == {"promoted_by": pseudonym, "keep": "\u0000"}
        assert after[untouched_id] == before[untouched_id]
        assert await pseudonymize_audit_trail(db, sub, []) == (0, 0)


@pytest.mark.parametrize("still_mentions_subject", [False, True])
async def test_metadata_patch_rechecks_fresh_locked_values(
    async_engine: AsyncEngine, subject: dict[str, Any], still_mentions_subject: bool
) -> None:
    from services import erasure_details

    sub, pseudonym = subject["sub"], subject["p"]
    async with _maker(async_engine)() as seed:
        row = _audit(subject, "another-member", metadata={"target_user_id": sub, "keep": "old"})
        seed.add(row)
        await seed.commit()
        row_id = row.id
    scanned, proceed = asyncio.Event(), asyncio.Event()
    scan = erasure_details._audit_metadata_ids

    async def pause_after_scan(*args: Any, **kwargs: Any) -> set[int]:
        ids = await scan(*args, **kwargs)
        scanned.set()
        await proceed.wait()
        return ids

    async def erase() -> tuple[int, int]:
        async with _maker(async_engine)() as db:
            result = await pseudonymize_audit_trail(db, sub, [])
            await db.commit()
            return result

    with patch.object(erasure_details, "_audit_metadata_ids", pause_after_scan):
        task = asyncio.create_task(erase())
        try:
            await asyncio.wait_for(scanned.wait(), timeout=10)
            # A concurrent actor commits a new value after discovery, before the union lock.
            current = {
                "target_user_id": sub if still_mentions_subject else "other-member",
                "keep": "new\u0000value",
            }
            async with _maker(async_engine)() as writer:
                await writer.execute(
                    update(AuditLog).where(AuditLog.id == row_id).values(user_metadata=current)
                )
                await writer.commit()
            proceed.set()
            assert await asyncio.wait_for(task, timeout=10) == (0, int(still_mentions_subject))
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with _maker(async_engine)() as db:
        actual = (
            await db.execute(select(AuditLog.user_metadata).where(AuditLog.id == row_id))
        ).scalar_one()
        assert actual == {
            **current,
            "target_user_id": pseudonym if still_mentions_subject else "other-member",
        }


async def test_metadata_update_rechecks_registration_after_the_active_subject_read(
    async_engine: AsyncEngine, subject: dict[str, Any]
) -> None:
    from services import erasure_details

    sub = subject["sub"]
    async with _maker(async_engine)() as seed:
        row = _audit(subject, "another-member", metadata={"target_user_id": sub})
        seed.add(row)
        await seed.commit()
        row_id = row.id
    checked, proceed = asyncio.Event(), asyncio.Event()
    active_subs = erasure_details._active_audit_subs
    checks = 0

    async def pause_at_write(*args: Any, **kwargs: Any) -> set[str]:
        nonlocal checks
        result = await active_subs(*args, **kwargs)
        checks += 1
        if checks == 2:
            checked.set()
            await proceed.wait()
        return result

    async def erase() -> tuple[int, int]:
        async with _maker(async_engine)() as db:
            result = await pseudonymize_audit_trail(db, sub, [], orphaned_only=True)
            await db.commit()
            return result

    with patch.object(erasure_details, "_active_audit_subs", pause_at_write):
        task = asyncio.create_task(erase())
        try:
            await asyncio.wait_for(checked.wait(), timeout=10)
            async with _maker(async_engine)() as signup:
                signup.add(
                    User(
                        user_id=sub,
                        email="registered-during-sweep@example.invalid",
                        name="new account",
                        role="user",
                    )
                )
                await signup.commit()
            proceed.set()
            assert await asyncio.wait_for(task, timeout=10) == (0, 0)
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with _maker(async_engine)() as db:
        actual = (
            await db.execute(select(AuditLog.user_metadata).where(AuditLog.id == row_id))
        ).scalar_one()
        assert actual == {"target_user_id": sub}


async def test_mutual_metadata_mentions_share_one_ordered_lock_union(
    async_engine: AsyncEngine, subject: dict[str, Any]
) -> None:
    from services import erasure_details

    first, second = subject["sub"], f"{subject['sub']}-second"
    async with _maker(async_engine)() as seed:
        rows = [
            _audit(subject, first, metadata={"target_user_id": second}),
            _audit(subject, second, metadata={"target_user_id": first}),
        ]
        seed.add_all(rows)
        await seed.commit()
        ids = [row.id for row in rows]
    barrier = asyncio.Barrier(2)
    scan = erasure_details._audit_metadata_ids

    async def scan_together(*args: Any, **kwargs: Any) -> set[int]:
        result = await scan(*args, **kwargs)
        await barrier.wait()
        return result

    async def erase(sub: str) -> tuple[int, int]:
        async with _maker(async_engine)() as db:
            result = await pseudonymize_audit_trail(db, sub, [])
            await db.commit()
            return result

    with patch.object(erasure_details, "_audit_metadata_ids", scan_together):
        assert await asyncio.wait_for(asyncio.gather(erase(first), erase(second)), timeout=15) == [
            (1, 1),
            (1, 1),
        ]
    async with _maker(async_engine)() as db:
        actual = (
            await db.execute(
                select(AuditLog.user_id, AuditLog.user_metadata)
                .where(AuditLog.id.in_(ids))
                .order_by(AuditLog.id)
            )
        ).all()
        assert actual == [
            (erasure_pseudonym(first), {"target_user_id": erasure_pseudonym(second)}),
            (erasure_pseudonym(second), {"target_user_id": erasure_pseudonym(first)}),
        ]


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("target_location", ["none", "metadata", "resource", "both"])
@pytest.mark.parametrize(
    ("subject", "fallback"), [("email-sub", "bare"), ("normal-sub", "api")], indirect=["subject"]
)
async def test_sub_fallback_cannot_claim_another_live_actors_email(
    async_engine: AsyncEngine,
    subject: dict[str, Any],
    historical: bool,
    target_location: str,
    fallback: str,
) -> None:
    sub, pseudonym = subject["sub"], subject["p"]
    actor = f"live-actor-{uuid.uuid4().hex}"
    email = sub if fallback == "bare" else f"{sub}@api"
    metadata = (
        {"target_user_id": sub, "keep": [1, None]}
        if target_location in {"metadata", "both"}
        else {"keep": "other member"}
    )
    row = _audit(subject, actor, email=email, metadata=metadata)
    if target_location in {"resource", "both"}:
        row.resource = f"user:{sub}"
    try:
        async with _maker(async_engine)() as db:
            db.add(User(user_id=actor, email=email, name="other actor", role="user"))
            db.add(row)
            await db.commit()
            row_id = row.id
            before = (await _snapshot(db, subject))[row_id]
            expected_mentions = int(target_location != "none")
            if historical:
                plan = await cli.reprocess_erasures(db, request_id=subject["request"])
                applied = await cli.reprocess_erasures(
                    db, request_id=subject["request"], dry_run=False
                )
                for result in (plan, applied):
                    assert (
                        sum(c.audit_mentions_pseudonymized for c in result.changed)
                        == expected_mentions
                    )
                    assert sum(c.audit_logs_pseudonymized for c in result.changed) == 0
            else:
                assert await pseudonymize_audit_trail(db, sub, []) == (0, expected_mentions)
                await db.commit()
            actual = (await _snapshot(db, subject))[row_id]
            expected = dict(before)
            if target_location in {"metadata", "both"}:
                expected["user_metadata"] = {**metadata, "target_user_id": pseudonym}
            if target_location in {"resource", "both"}:
                expected["resource"] = f"user:{pseudonym}"
            assert actual == expected
            assert (actual["user_email"], actual["ip_address"], actual["user_agent"]) == (
                email,
                "192.0.2.4",
                "other-agent",
            )
    finally:
        async with _maker(async_engine)() as db:
            await db.execute(delete(User).where(User.user_id == actor))
            await db.commit()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff", "\ud800suffix", "\x00"])
async def test_historical_discovery_skips_invalid_auth_sub_metadata_candidates(
    async_engine: AsyncEngine,
    subject: dict[str, Any],
    surrogate: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sub, pseudonym = subject["sub"], subject["p"]
    bad = _audit(
        subject, "unrelated-member", metadata={"target_user_id": surrogate, "keep": [surrogate]}
    )
    good = _audit(subject, pseudonym, metadata={"target_user_id": sub, "assigned_by": surrogate})
    async with _maker(async_engine)() as db:
        db.add_all([bad, good])
        await db.commit()
        bad_id, good_id = bad.id, good.id
        before = await _snapshot(db, subject)
        candidates = await cli._audit_subs(db)
        assert candidates.orphans[pseudonym] == sub
        plan = await cli.reprocess_erasures(db, request_id=subject["request"])
        cli._print_plan(plan)
        assert await _snapshot(db, subject) == before
        applied = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        cli._print_applied(applied)
        for result in (plan, applied):
            assert result.changed[0].audit_mentions_pseudonymized == 1
        after = await _snapshot(db, subject)
        assert after[bad_id] == before[bad_id]
        assert after[good_id]["user_metadata"] == {
            "target_user_id": pseudonym,
            "assigned_by": surrogate,
        }
        again = await cli.reprocess_erasures(db, request_id=subject["request"], dry_run=False)
        assert again.changes == 0
    assert sub not in capsys.readouterr().out
