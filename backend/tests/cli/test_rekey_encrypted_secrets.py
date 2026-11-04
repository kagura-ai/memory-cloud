"""``cli.rekey_encrypted_secrets``: move stored secrets to the current key (#2394).

A rotation puts the new key in ``API_KEY_SECRET`` and the old one in
``API_KEY_SECRET_PREVIOUS``; this command re-encrypts every stored value that
only an old key opens, compare-and-set, and reports counts (never a value).
The registry guard keeps a future encrypted column from being forgotten. The
database tests need a live Postgres (``TEST_DATABASE_URL``); the shared test
database may hold rows other modules left, encrypted under keys this test does
not know, so the counts are compared with a report taken before seeding.
"""

from __future__ import annotations

import importlib
import io
import json
import pkgutil
import secrets
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis as fakeaioredis
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import models
from db.base import Base
from models.auth import APIKey, ExternalAPIKey, OAuth2Client, User, Workspace
from models.resource import Resource, WorkspaceConnector
from models.worker_app import WorkerAppIdentity
from utils.encryption import APIKeyEncryption, derive_fernet_key, generate_fernet_key

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api.routes.connectors_slack import _install_key  # noqa: E402
from cli import rekey_encrypted_secrets as cli  # noqa: E402

# ---------------------------------------------------------------------------
# The registry covers every encrypted column
# ---------------------------------------------------------------------------

# Column names that hold an ``APIKeyEncryption`` ciphertext by convention.
_ENCRYPTED_NAME_SUFFIX = "_encrypted"
_ENCRYPTED_NAMES = {"totp_secret", "encrypted_value"}


def _all_columns() -> list[Any]:
    for module in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"models.{module.name}")
    return [column for table in Base.metadata.tables.values() for column in table.columns]


def _label(column: Any) -> str:
    return f"{column.table.name}.{column.name}"


def test_every_encrypted_column_is_in_the_registry() -> None:
    named = {
        _label(column)
        for column in _all_columns()
        if column.name.endswith(_ENCRYPTED_NAME_SUFFIX) or column.name in _ENCRYPTED_NAMES
    }
    registered = {cli.column_label(column) for column in cli.ENCRYPTED_COLUMNS}
    # Every column named like a ciphertext is re-keyed; a new one fails here
    # until it is added to ``ENCRYPTED_COLUMNS`` (or renamed).
    assert named - registered == set()
    assert named  # the walk found the models


def test_the_registry_names_real_single_key_tables() -> None:
    labels = [cli.column_label(column) for column in cli.ENCRYPTED_COLUMNS]
    assert len(labels) == len(set(labels))
    for column in cli.ENCRYPTED_COLUMNS:
        table = cli.column_table(column)
        assert len(table.primary_key.columns) == 1, cli.column_label(column)


def test_the_slack_install_pattern_matches_the_route() -> None:
    assert _install_key("handle").startswith(cli.SLACK_INSTALL_PREFIX)
    assert cli.SLACK_INSTALL_PATTERN == f"{cli.SLACK_INSTALL_PREFIX}*"


# ---------------------------------------------------------------------------
# Exit codes and usage (no database)
# ---------------------------------------------------------------------------


def _report(**counts: int) -> cli.RekeyReport:
    report = cli.RekeyReport(dry_run=True, key_count=2)
    report.columns["users.totp_secret"] = cli.Counts(**counts)
    return report


@pytest.fixture
def fake_db():
    async def _get_db():
        yield MagicMock()

    with patch("cli._oneshot.get_db", _get_db):
        yield


@pytest.mark.parametrize(
    ("report", "code"),
    [
        (_report(already_current=3), 0),
        (_report(already_current=1, changed_concurrently=1), 0),
        (_report(already_current=1, undecryptable=1), 3),
    ],
    ids=["clean", "changed-concurrently", "undecryptable"],
)
async def test_exit_code_of_a_report(fake_db, report, code, capsys) -> None:
    with patch.object(cli, "rekey_encrypted_secrets", AsyncMock(return_value=report)):
        assert await cli._main(cli._parse([])) == code
    assert "users.totp_secret" in capsys.readouterr().out


async def test_a_redis_failure_exits_1(fake_db, capsys) -> None:
    report = _report(already_current=1)
    report.redis_error = "ConnectionError"
    with patch.object(cli, "rekey_encrypted_secrets", AsyncMock(return_value=report)):
        assert await cli._main(cli._parse([])) == 1
    out = capsys.readouterr().out
    assert "ConnectionError" in out


async def test_apply_without_anything_to_rewrite_skips_the_write(fake_db) -> None:
    run = AsyncMock(return_value=_report(already_current=2))
    with patch.object(cli, "rekey_encrypted_secrets", run):
        assert await cli._main(cli._parse(["--apply", "--yes"])) == 0
    assert [c.kwargs["dry_run"] for c in run.call_args_list] == [True]


async def test_apply_writes_after_the_report(fake_db) -> None:
    planned = _report(re_encrypted=2)
    applied = _report(already_current=0, re_encrypted=2)
    applied.dry_run = False
    run = AsyncMock(side_effect=[planned, applied])
    with patch.object(cli, "rekey_encrypted_secrets", run):
        assert await cli._main(cli._parse(["--apply", "--yes"])) == 0
    assert [c.kwargs["dry_run"] for c in run.call_args_list] == [True, False]


@pytest.mark.parametrize(
    ("answer", "dry_runs"), [("n", [True]), ("y", [True, False])], ids=["declined", "confirmed"]
)
async def test_apply_without_yes_asks_before_writing(fake_db, capsys, answer, dry_runs) -> None:
    applied = _report(re_encrypted=2)
    applied.dry_run = False
    run = AsyncMock(side_effect=[_report(re_encrypted=2), applied])
    with (
        patch.object(cli, "rekey_encrypted_secrets", run),
        patch("builtins.input", return_value=answer) as prompt,
    ):
        assert await cli._main(cli._parse(["--apply"])) == 0
    assert "Update 2 encrypted value(s) to the current key?" in prompt.call_args.args[0]
    assert [c.kwargs["dry_run"] for c in run.call_args_list] == dry_runs
    assert ("skipped" in capsys.readouterr().out) is (answer == "n")


async def test_yes_without_apply_is_a_usage_error(fake_db) -> None:
    run = AsyncMock()
    with patch.object(cli, "rekey_encrypted_secrets", run):
        assert await cli._main(cli._parse(["--yes"])) == 2
    run.assert_not_called()


def test_the_report_lists_undecryptable_ids_but_caps_them(capsys) -> None:
    report = _report(undecryptable=30)
    report.columns["users.totp_secret"].undecryptable_ids = [str(i) for i in range(30)]
    cli._print_report(report)
    out = capsys.readouterr().out
    assert "0, 1, 2" in out
    assert "and 10 more" in out


# ---------------------------------------------------------------------------
# --derive-fernet-key (no database, Redis or settings)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_backends(monkeypatch):
    """Fail the test if the helper resolves keys, builds settings or opens a store."""
    refuse = MagicMock(side_effect=AssertionError("--derive-fernet-key touched a backend"))
    for name in ("require_api_key_secrets", "require_settings", "get_redis_client"):
        monkeypatch.setattr(cli, name, refuse)
    with patch("cli._oneshot.get_db", refuse):
        yield


def _stdin(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


@pytest.mark.parametrize(
    "secret",
    ["a,b", " with blanks ", "carriage\r"],
    ids=["comma", "blanks", "carriage-return"],
)
async def test_derive_fernet_key_reads_stdin_verbatim(
    no_backends, monkeypatch, capsys, caplog, secret
) -> None:
    # ``printf '%s' "$OLD_SECRET" | ...``: no newline, nothing stripped.
    _stdin(monkeypatch, secret.encode())
    assert await cli._main(cli._parse(["--derive-fernet-key"])) == 0
    out = capsys.readouterr()
    assert out.out == f"{derive_fernet_key(secret)}\n"
    assert out.err == ""
    assert caplog.records == []
    token = APIKeyEncryption(secret).encrypt("sk-under-the-old-key")
    assert APIKeyEncryption(out.out.strip()).decrypt(token) == "sk-under-the-old-key"


@pytest.mark.parametrize("data", ["echoed-secret\n", "two-newlines\n\n", "\n"])
async def test_derive_fernet_key_keeps_a_trailing_newline_and_says_so(
    no_backends, monkeypatch, capsys, caplog, data
) -> None:
    _stdin(monkeypatch, data.encode())
    assert await cli._main(cli._parse(["--derive-fernet-key"])) == 0
    out = capsys.readouterr()
    # Derived from the bytes as given, newline included.
    assert out.out == f"{derive_fernet_key(data)}\n"
    assert out.out != f"{derive_fernet_key(data[:-1])}\n"
    note = out.err.splitlines()
    assert len(note) == 1
    assert "ends with a newline" in note[0] and "printf '%s'" in note[0]
    # Nothing about the input beyond that: not the secret, not in the logs.
    stem = data.strip()
    assert not stem or stem not in out.err
    assert caplog.records == []


async def test_derive_fernet_key_passes_a_raw_fernet_key_through(
    no_backends, monkeypatch, capsys
) -> None:
    key = generate_fernet_key()
    _stdin(monkeypatch, key.encode())
    assert await cli._main(cli._parse(["--derive-fernet-key"])) == 0
    assert capsys.readouterr().out == f"{key}\n"


@pytest.mark.parametrize(
    ("data", "argv"),
    [
        (b"", ["--derive-fernet-key"]),
        (b"k", ["--derive-fernet-key", "--yes"]),
    ],
    ids=["empty", "with-yes"],
)
async def test_derive_fernet_key_usage_errors(no_backends, monkeypatch, capsys, data, argv) -> None:
    _stdin(monkeypatch, data)
    assert await cli._main(cli._parse(argv)) == 2
    assert capsys.readouterr().out == ""


def test_derive_fernet_key_excludes_apply() -> None:
    with pytest.raises(SystemExit):
        cli._parse(["--derive-fernet-key", "--apply"])


# ---------------------------------------------------------------------------
# The Redis-held Slack install bundles (fakeredis, no database)
# ---------------------------------------------------------------------------


@pytest.fixture
def keys() -> SimpleNamespace:
    old, new, stranger = generate_fernet_key(), generate_fernet_key(), generate_fernet_key()
    return SimpleNamespace(
        old=old,
        new=new,
        ring=APIKeyEncryption(new, previous_keys=[old]),
        under_old=APIKeyEncryption(old),
        under_new=APIKeyEncryption(new),
        under_stranger=APIKeyEncryption(stranger),
    )


def _bundle(token: str) -> str:
    return json.dumps(
        {
            "workspace_id": str(uuid4()),
            "app_key": "default",
            "bot_token_enc": token,
            "team_id": "T1",
            "team_name": "Team",
            "installing_admin_user_id": "U1",
            "granted_scopes": ["chat:write"],
        }
    )


@pytest_asyncio.fixture
async def slack_redis(keys) -> AsyncIterator[SimpleNamespace]:
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    world = SimpleNamespace(
        redis=redis,
        old=_install_key("old-handle"),
        current=_install_key("current-handle"),
        bad=_install_key("bad-handle"),
        empty=_install_key("empty-handle"),
        plaintext="xoxb-slack-bot-token-for-2394",
    )
    await redis.set(world.old, _bundle(keys.under_old.encrypt(world.plaintext)), ex=600)
    await redis.set(world.current, _bundle(keys.under_new.encrypt(world.plaintext)), ex=600)
    await redis.set(world.bad, _bundle(keys.under_stranger.encrypt(world.plaintext)), ex=600)
    await redis.set(world.empty, _bundle(""), ex=600)
    await redis.set("slack_oauth_state:abc", "not-a-bundle", ex=600)
    yield world
    await redis.flushall()
    await redis.aclose()


async def _snapshot(redis: Any) -> dict[str, str]:
    return {key: await redis.get(key) async for key in redis.scan_iter(match="*")}


async def test_slack_installs_report_writes_nothing(keys, slack_redis) -> None:
    before = await _snapshot(slack_redis.redis)
    counts = await cli.rekey_slack_installs(slack_redis.redis, keys.ring, dry_run=True)
    assert (counts.already_current, counts.re_encrypted, counts.undecryptable) == (1, 1, 1)
    assert counts.changed_concurrently == 0
    assert counts.undecryptable_ids == []  # a handle is not listed
    assert await _snapshot(slack_redis.redis) == before


async def test_slack_installs_apply_re_encrypts_and_keeps_the_ttl(keys, slack_redis) -> None:
    redis = slack_redis.redis
    before = await _snapshot(redis)
    counts = await cli.rekey_slack_installs(redis, keys.ring, dry_run=False)
    assert (counts.already_current, counts.re_encrypted, counts.undecryptable) == (1, 1, 1)

    rewritten = json.loads(await redis.get(slack_redis.old))
    assert keys.under_new.decrypt(rewritten["bot_token_enc"]) == slack_redis.plaintext
    # Everything else in the bundle is kept as it was.
    original = json.loads(before[slack_redis.old])
    assert {k: v for k, v in rewritten.items() if k != "bot_token_enc"} == {
        k: v for k, v in original.items() if k != "bot_token_enc"
    }
    assert 0 < await redis.ttl(slack_redis.old) <= 600
    for key in (slack_redis.current, slack_redis.bad, slack_redis.empty, "slack_oauth_state:abc"):
        assert await redis.get(key) == before[key]

    again = await cli.rekey_slack_installs(redis, keys.ring, dry_run=True)
    assert (again.already_current, again.re_encrypted, again.undecryptable) == (2, 0, 1)


async def test_a_bundle_changed_before_the_write_keeps_the_newer_value(
    keys, slack_redis, monkeypatch
) -> None:
    redis = slack_redis.redis
    newer = _bundle(keys.under_new.encrypt("xoxb-newer-token"))
    original_write = cli._write_slack_install

    async def _racing_write(pipe: Any, key: str, value: str) -> bool:
        await redis.set(key, newer, keepttl=True)  # another connection
        return await original_write(pipe, key, value)

    monkeypatch.setattr(cli, "_write_slack_install", _racing_write)
    counts = await cli.rekey_slack_installs(redis, keys.ring, dry_run=False)
    assert counts.changed_concurrently == 1
    assert counts.re_encrypted == 0
    assert await redis.get(slack_redis.old) == newer


async def test_a_redis_failure_is_reported_not_raised(keys) -> None:
    broken = MagicMock()
    broken.scan_iter = MagicMock(side_effect=ConnectionError("redis down"))
    report = await cli.rekey_encrypted_secrets(
        MagicMock(), encryptor=keys.ring, redis=broken, columns=(), dry_run=True
    )
    assert report.redis_error == "ConnectionError"
    assert cli.SLACK_INSTALL_LABEL not in report.columns


# ---------------------------------------------------------------------------
# Database: a full rotation (report → apply → report)
# ---------------------------------------------------------------------------

_PLAINTEXT = {
    "totp_old": "JBSWY3DPEHPK3PXP-2394-old",
    "totp_new": "JBSWY3DPEHPK3PXP-2394-new",
    "api_key": "kmc_live_plaintext_for_2394",
    "external": "sk-proj-external-key-for-2394",
    "external_raced": "sk-proj-raced-key-for-2394",
    "external_unknown": "sk-proj-unknown-key-for-2394",
    "client_secret": "oauth-client-secret-for-2394",
    "llm_config": json.dumps({"provider": "openai", "model": "m", "api_key": "sk-llm-2394"}),
    "resource_token": "rt_resource_token_for_2394",
    "signing_secret": "worker-signing-secret-for-2394",
}


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, keys) -> AsyncIterator[dict[str, Any]]:
    """One row (or two) in each kind of encrypted column, under the OLD key."""
    tag = uuid4().hex[:8]
    old, new, stranger = keys.under_old, keys.under_new, keys.under_stranger
    user_id = f"rekey-{tag}"
    user = User(
        email=f"{user_id}@example.com",
        user_id=user_id,
        name="Rekey",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        totp_secret=old.encrypt(_PLAINTEXT["totp_old"]),
    )
    current_user = User(
        email=f"{user_id}-cur@example.com",
        user_id=f"{user_id}-cur",
        name="Rekey current",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        totp_secret=new.encrypt(_PLAINTEXT["totp_new"]),
    )
    db_session.add_all([user, current_user])
    workspace = Workspace(
        id=uuid4(),
        name=f"ws-2394-{tag}",
        plan_name="free",
        owner_user_id=user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    await db_session.flush()
    api_key = APIKey(
        key_hash=secrets.token_hex(32),
        key_prefix="kmc_live_2394",
        name=f"key-{tag}",
        user_id=user_id,
        workspace_id=workspace.id,
        plaintext_encrypted=old.encrypt(_PLAINTEXT["api_key"]),
    )

    def external(provider: str, value: str) -> ExternalAPIKey:
        # One enabled key per (workspace, provider).
        return ExternalAPIKey(
            key_name=f"{provider}-{tag}",
            provider=provider,
            encrypted_value=value,
            user_id=user_id,
            workspace_id=workspace.id,
        )

    ext = external("provider-a", old.encrypt(_PLAINTEXT["external"]))
    raced = external("provider-b", old.encrypt(_PLAINTEXT["external_raced"]))
    unknown = external("provider-c", stranger.encrypt(_PLAINTEXT["external_unknown"]))
    client = OAuth2Client(
        client_id=f"client-{tag}",
        client_secret_hash=secrets.token_hex(32),
        client_name="Rekey client",
        owner_id=user_id,
        workspace_id=workspace.id,
        redirect_uris=["https://example.com/cb"],
        plaintext_secret_encrypted=old.encrypt(_PLAINTEXT["client_secret"]),
    )
    resource = Resource(id=uuid4(), workspace_id=workspace.id, resource_id=f"res-{tag}")
    db_session.add_all([api_key, ext, raced, unknown, client, resource])
    await db_session.flush()
    connector = WorkspaceConnector(
        resource_pk=resource.id,
        workspace_id=workspace.id,
        connector_type="slack",
        created_by=user_id,
        llm_config_encrypted=old.encrypt(_PLAINTEXT["llm_config"]),
        resource_token_encrypted=old.encrypt(_PLAINTEXT["resource_token"]),
    )
    app = WorkerAppIdentity(
        platform="slack",
        app_key=f"rekey-{tag}",
        display_name="Rekey app",
        active_signing_secret_encrypted=old.encrypt(_PLAINTEXT["signing_secret"]),
    )
    db_session.add_all([connector, app])
    await db_session.commit()

    rows = {
        "totp_old": (User.totp_secret, User.id, user.id),
        "totp_new": (User.totp_secret, User.id, current_user.id),
        "api_key": (APIKey.plaintext_encrypted, APIKey.id, api_key.id),
        "external": (ExternalAPIKey.encrypted_value, ExternalAPIKey.id, ext.id),
        "external_raced": (ExternalAPIKey.encrypted_value, ExternalAPIKey.id, raced.id),
        "external_unknown": (ExternalAPIKey.encrypted_value, ExternalAPIKey.id, unknown.id),
        "client_secret": (
            OAuth2Client.plaintext_secret_encrypted,
            OAuth2Client.id,
            client.id,
        ),
        "llm_config": (
            WorkspaceConnector.llm_config_encrypted,
            WorkspaceConnector.id,
            connector.id,
        ),
        "resource_token": (
            WorkspaceConnector.resource_token_encrypted,
            WorkspaceConnector.id,
            connector.id,
        ),
        "signing_secret": (
            WorkerAppIdentity.active_signing_secret_encrypted,
            WorkerAppIdentity.id,
            app.id,
        ),
    }
    # Read the ids now: a later commit or rollback expires the instances.
    app_id, workspace_id = app.id, workspace.id
    user_ids = [user.id, current_user.id]
    yield {"rows": rows}

    await db_session.rollback()
    await db_session.execute(delete(WorkerAppIdentity).where(WorkerAppIdentity.id == app_id))
    await db_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
    await db_session.execute(delete(User).where(User.id.in_(user_ids)))
    await db_session.commit()


async def _values(db: AsyncSession, world: dict[str, Any]) -> dict[str, str]:
    values = {}
    for name, (column, pk, row_id) in world["rows"].items():
        values[name] = (await db.execute(select(column).where(pk == row_id))).scalar_one()
    await db.rollback()
    return values


_FIELDS = ("already_current", "re_encrypted", "undecryptable", "changed_concurrently")


def _delta(report: cli.RekeyReport, baseline: cli.RekeyReport) -> dict[str, tuple[int, ...]]:
    """Counts of this test's rows: the report minus the one taken before seeding."""
    delta = {}
    for label, counts in report.columns.items():
        base = baseline.columns.get(label, cli.Counts())
        values = tuple(getattr(counts, f) - getattr(base, f) for f in _FIELDS)
        if any(values):
            delta[label] = values
    return delta


async def _run(db: AsyncSession, keys: Any, *, dry_run: bool) -> cli.RekeyReport:
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    try:
        return await cli.rekey_encrypted_secrets(
            db, encryptor=keys.ring, redis=redis, dry_run=dry_run, batch_size=2
        )
    finally:
        await redis.aclose()


@pytest_asyncio.fixture
async def baseline(db_session: AsyncSession, keys) -> cli.RekeyReport:
    return await _run(db_session, keys, dry_run=True)


async def test_a_rotation_end_to_end(db_session, keys, baseline, world, monkeypatch) -> None:
    seeded = await _values(db_session, world)

    # 1. The report writes nothing and counts each row once.
    report = await _run(db_session, keys, dry_run=True)
    assert report.dry_run is True
    assert report.key_count == 2
    assert _delta(report, baseline) == {
        "users.totp_secret": (1, 1, 0, 0),
        "api_keys.plaintext_encrypted": (0, 1, 0, 0),
        "external_api_keys.encrypted_value": (0, 2, 1, 0),
        "oauth_clients.plaintext_secret_encrypted": (0, 1, 0, 0),
        "workspace_connectors.llm_config_encrypted": (0, 1, 0, 0),
        "workspace_connectors.resource_token_encrypted": (0, 1, 0, 0),
        "worker_app_identities.active_signing_secret_encrypted": (0, 1, 0, 0),
    }
    unknown_id = world["rows"]["external_unknown"][2]
    assert str(unknown_id) in report.columns["external_api_keys.encrypted_value"].undecryptable_ids
    assert await _values(db_session, world) == seeded

    # 2. The apply; the application rewrites one row between the read and the write.
    raced_column, raced_pk, raced_id = world["rows"]["external_raced"]
    newer = keys.under_new.encrypt("sk-proj-written-by-the-app")
    original_write = cli._write_rows
    raced: list[bool] = []

    async def _racing_write(db: AsyncSession, column: Any, rows: list[Any]) -> Any:
        # The integer ids of api_keys and external_api_keys can coincide on a
        # fresh database, so the race must fire on the raced COLUMN's batch.
        if (
            not raced
            and column is raced_column
            and any(row_id == raced_id for row_id, _old, _new in rows)
        ):
            raced.append(True)
            assert db_session.bind is not None
            async with AsyncSession(db_session.bind) as other:
                await other.execute(
                    update(ExternalAPIKey)
                    .where(ExternalAPIKey.id == raced_id)
                    .values(encrypted_value=newer)
                )
                await other.commit()
        return await original_write(db, column, rows)

    monkeypatch.setattr(cli, "_write_rows", _racing_write)
    applied = await _run(db_session, keys, dry_run=False)
    assert raced == [True]
    assert _delta(applied, baseline) == {
        "users.totp_secret": (1, 1, 0, 0),
        "api_keys.plaintext_encrypted": (0, 1, 0, 0),
        "external_api_keys.encrypted_value": (0, 1, 1, 1),
        "oauth_clients.plaintext_secret_encrypted": (0, 1, 0, 0),
        "workspace_connectors.llm_config_encrypted": (0, 1, 0, 0),
        "workspace_connectors.resource_token_encrypted": (0, 1, 0, 0),
        "worker_app_identities.active_signing_secret_encrypted": (0, 1, 0, 0),
    }

    # Every row this test owns opens with the NEW key alone, the raced row
    # keeps the application's value, the unknown row is byte-identical.
    after = await _values(db_session, world)
    assert after["external_raced"] == newer
    assert after["external_unknown"] == seeded["external_unknown"]
    assert after["totp_new"] == seeded["totp_new"]  # already current: untouched
    for name, value in after.items():
        if name in ("external_raced", "external_unknown"):
            continue
        assert keys.under_new.decrypt(value) == _PLAINTEXT[name], name
        if name != "totp_new":
            assert value != seeded[name], name

    # 3. A second run finds everything under the current key.
    again = await _run(db_session, keys, dry_run=True)
    assert _delta(again, baseline) == {
        "users.totp_secret": (2, 0, 0, 0),
        "api_keys.plaintext_encrypted": (1, 0, 0, 0),
        "external_api_keys.encrypted_value": (2, 0, 1, 0),
        "oauth_clients.plaintext_secret_encrypted": (1, 0, 0, 0),
        "workspace_connectors.llm_config_encrypted": (1, 0, 0, 0),
        "workspace_connectors.resource_token_encrypted": (1, 0, 0, 0),
        "worker_app_identities.active_signing_secret_encrypted": (1, 0, 0, 0),
    }

    # 4. Drop the old key: the application reads every rewritten row.
    for name, value in (await _values(db_session, world)).items():
        if name in ("external_raced", "external_unknown"):
            continue
        assert APIKeyEncryption(keys.new).decrypt(value) == _PLAINTEXT[name]


async def test_a_re_encryption_does_not_touch_updated_at(db_session, keys, world) -> None:
    _column, _pk, connector_id = world["rows"]["llm_config"]
    stamp = select(WorkspaceConnector.updated_at).where(WorkspaceConnector.id == connector_id)
    before = (await db_session.execute(stamp)).scalar_one()
    await db_session.rollback()
    await _run(db_session, keys, dry_run=False)
    assert (await db_session.execute(stamp)).scalar_one() == before
    await db_session.rollback()


@pytest.fixture
def cli_keys(keys, monkeypatch):
    """The command's own key resolution: the cached settings hold the ring."""
    import utils.encryption as enc_module
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "api_key_secret", keys.new)
    monkeypatch.setattr(get_settings(), "api_key_secret_previous", keys.old)
    monkeypatch.setattr(enc_module, "_encryptor", None)
    monkeypatch.setenv("API_KEY_SECRET", keys.new)
    monkeypatch.delenv("API_KEY_SECRET_PREVIOUS", raising=False)
    return keys


@pytest.fixture
def cli_db(db_session):
    """Route the command's ``get_db()`` to the test session, Redis to a fake."""

    async def _get_db():
        yield db_session

    redis = fakeaioredis.FakeRedis(decode_responses=True)
    with (
        patch("cli._oneshot.get_db", _get_db),
        patch.object(cli, "get_redis_client", return_value=redis),
    ):
        yield db_session


async def test_the_command_never_prints_a_secret(
    db_session, cli_keys, world, cli_db, capsys, caplog
) -> None:
    seeded = await _values(db_session, world)

    # The unknown row stays undecryptable: exit 3 on the report and the apply.
    assert await cli._main(cli._parse([])) == 3
    report_out = capsys.readouterr()
    assert await _values(db_session, world) == seeded
    assert await cli._main(cli._parse(["--apply", "--yes"])) == 3
    apply_out = capsys.readouterr()
    rewritten = await _values(db_session, world)
    assert rewritten["api_key"] != seeded["api_key"]

    text = report_out.out + report_out.err + apply_out.out + apply_out.err + caplog.text
    assert "users.totp_secret" in report_out.out
    assert "dry run" in report_out.out
    assert "external_api_keys.encrypted_value" in apply_out.out
    for value in (*_PLAINTEXT.values(), *seeded.values(), *rewritten.values()):
        assert value not in text
    for key in (cli_keys.old, cli_keys.new):
        assert key not in text
