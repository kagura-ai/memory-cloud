"""Re-encrypt the stored secrets under the current ``API_KEY_SECRET`` (#2394).

``utils.encryption`` encrypts every new value with ``API_KEY_SECRET`` and
decrypts with any key of the ring (``API_KEY_SECRET`` plus the comma-separated
``API_KEY_SECRET_PREVIOUS``). After a rotation the stored values are still
under the old key; this command re-encrypts them under the current one, so the
old key can be dropped. Runs where the API runs (same env: DATABASE_URL,
REDIS_URL, both key variables), e.g. inside the API container::

    python -m src.cli.rekey_encrypted_secrets                 # report (default, read-only)
    python -m src.cli.rekey_encrypted_secrets --apply --yes   # re-encrypt

From a host shell without the keys in the environment, both are read from the
running API container (``cli._env_guard``). The order of a rotation is in
docs/deployment.md ("Rotating API_KEY_SECRET").

What it reads: every column in ``ENCRYPTED_COLUMNS`` (TOTP secrets, the
display copies of API keys and OAuth client secrets, external/BYOK keys,
workspace connector tokens, LLM config, KMC and resource tokens, worker app
signing secrets, the signup-gate secrets), rows with a non-empty value, in
primary-key order, ``BATCH_SIZE`` at a time; and the pending Slack install
bundles held in Redis (``slack_install:*``, ``bot_token_enc``, which expire on
their own within ten minutes). Each value is counted once:

- ``already_current``: the current key opens it; left alone.
- ``re_encrypted``: only a previous key opens it; rewritten under the current
  key (in the report: what ``--apply`` would rewrite).
- ``undecryptable``: no key of the ring opens it; left byte-identical. The ids
  of such rows are listed (not the Redis handles); find the key they were
  written with and add it to ``API_KEY_SECRET_PREVIOUS``, or re-enter them.
- ``changed_concurrently``: the application rewrote the value between the
  read and the write. Every write is compare-and-set on the value read
  (``UPDATE ... WHERE id = :id AND col = :old``; WATCH/MULTI on Redis), so the
  newer value is kept and a rerun picks it up.

A re-encryption is not an edit: ``updated_at`` (any column with an
``onupdate``) is left as it was. Each batch commits on its own; a run can be
interrupted and rerun at any time. Nothing prints or logs a plaintext or a
ciphertext.

``--derive-fernet-key`` is a separate helper for a key that contains a comma
or surrounding blanks, which ``API_KEY_SECRET_PREVIOUS`` (split on commas,
stripped) cannot hold verbatim: it reads the secret from stdin exactly as the
variable holds it (one trailing newline dropped) and prints the Fernet key it
stands for, which opens the same values and can be listed instead. It touches
no database, Redis or settings::

    printf '%s' "$OLD_SECRET" | python -m src.cli.rekey_encrypted_secrets --derive-fernet-key

Exit codes: 0 ok · 1 error (Redis unreachable included; what was committed
stays) · 2 usage (``--yes`` without ``--apply``; nothing on stdin for
``--derive-fernet-key``) · 3 some value is undecryptable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from redis.exceptions import WatchError  # noqa: E402
from sqlalchemy import Column, Table, select, update  # noqa: E402
from sqlalchemy.engine import CursorResult  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import InstrumentedAttribute  # noqa: E402

from api.routes.connectors_slack import SLACK_INSTALL_PREFIX  # noqa: E402
from cli._env_guard import require_api_key_secrets, require_settings  # noqa: E402
from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from db.redis import get_redis_client  # noqa: E402
from models.auth import APIKey, ExternalAPIKey, OAuth2Client, User  # noqa: E402
from models.resource import WorkspaceConnector  # noqa: E402
from models.signup_gate import SignupGateConfig  # noqa: E402
from models.worker_app import WorkerAppIdentity  # noqa: E402
from utils.encryption import APIKeyEncryption, derive_fernet_key, get_encryptor  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

# Every column whose value goes through ``utils.encryption.APIKeyEncryption``.
# ``tests/cli/test_rekey_encrypted_secrets.py`` walks every model and fails on
# a column named ``*_encrypted`` / ``totp_secret`` / ``encrypted_value`` that
# is missing here.
ENCRYPTED_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    User.totp_secret,
    APIKey.plaintext_encrypted,
    ExternalAPIKey.encrypted_value,
    OAuth2Client.plaintext_secret_encrypted,
    WorkspaceConnector.oauth_tokens_encrypted,
    WorkspaceConnector.llm_config_encrypted,
    WorkspaceConnector.kmc_api_key_encrypted,
    WorkspaceConnector.resource_token_encrypted,
    WorkerAppIdentity.active_signing_secret_encrypted,
    WorkerAppIdentity.retiring_signing_secret_encrypted,
    SignupGateConfig.github_sponsors_token_encrypted,
    SignupGateConfig.github_sponsors_webhook_secret_encrypted,
)

SLACK_INSTALL_PATTERN = f"{SLACK_INSTALL_PREFIX}*"
SLACK_INSTALL_LABEL = f"redis {SLACK_INSTALL_PATTERN}"
_SLACK_TOKEN_FIELD = "bot_token_enc"  # noqa: S105 — a JSON field name, not a credential

BATCH_SIZE = 500
# Undecryptable ids listed per column; the count is always complete.
MAX_LISTED_IDS = 20

EXIT_UNDECRYPTABLE = 3


@dataclass
class Counts:
    """One column's outcome (counts and row ids, never a value)."""

    already_current: int = 0
    re_encrypted: int = 0
    undecryptable: int = 0
    changed_concurrently: int = 0
    undecryptable_ids: list[str] = field(default_factory=list)


@dataclass
class RekeyReport:
    """The outcome of one run, per column label (``table.column``)."""

    dry_run: bool
    key_count: int
    columns: dict[str, Counts] = field(default_factory=dict)
    # Class name of the error that stopped the Redis step, if one did.
    redis_error: str | None = None

    def total(self, name: str) -> int:
        """The sum of one ``Counts`` field over every column."""
        return sum(getattr(counts, name) for counts in self.columns.values())

    @property
    def re_encrypted(self) -> int:
        return self.total("re_encrypted")

    @property
    def undecryptable(self) -> int:
        return self.total("undecryptable")


def column_table(column: InstrumentedAttribute[Any]) -> Table:
    """The table a registry column lives on."""
    return cast(Table, column.class_.__table__)


def _column(column: InstrumentedAttribute[Any]) -> Column[Any]:
    return column_table(column).c[column.key]


def column_label(column: InstrumentedAttribute[Any]) -> str:
    """``table.column``, the label of the report."""
    return f"{column_table(column).name}.{_column(column).name}"


def _classify(encryptor: APIKeyEncryption, value: str) -> bool | None:
    """True: under the current key; False: a previous key only; None: no key."""
    try:
        return encryptor.is_current(value)
    except ValueError:
        return None


async def _write_rows(
    db: AsyncSession,
    column: InstrumentedAttribute[Any],
    rows: list[tuple[Any, str, str]],
) -> tuple[int, int]:
    """Compare-and-set each ``(id, old, new)``; returns (written, changed meanwhile).

    The columns with an ``onupdate`` are set to themselves so a
    re-encryption does not count as an edit.
    """
    table = column_table(column)
    target = _column(column)
    (pk,) = table.primary_key.columns
    kept = {c.name: c for c in table.columns if c.onupdate is not None}
    written = changed = 0
    for row_id, old, new in rows:
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(table).where(pk == row_id, target == old).values({target.name: new, **kept})
            ),
        )
        if result.rowcount == 1:
            written += 1
        else:
            changed += 1
    return written, changed


async def _rekey_column(
    db: AsyncSession,
    column: InstrumentedAttribute[Any],
    encryptor: APIKeyEncryption,
    *,
    dry_run: bool,
    batch_size: int,
) -> Counts:
    counts = Counts()
    table = column_table(column)
    target = _column(column)
    (pk,) = table.primary_key.columns
    last: Any = None
    while True:
        stmt = (
            select(pk, target)
            .where(target.is_not(None), target != "")
            .order_by(pk)
            .limit(batch_size)
        )
        if last is not None:
            stmt = stmt.where(pk > last)
        rows = (await db.execute(stmt)).all()
        if not rows:
            await db.rollback()
            return counts
        last = rows[-1][0]
        to_write: list[tuple[Any, str, str]] = []
        for row_id, value in rows:
            state = _classify(encryptor, value)
            if state is None:
                counts.undecryptable += 1
                counts.undecryptable_ids.append(str(row_id))
            elif state:
                counts.already_current += 1
            elif dry_run:
                counts.re_encrypted += 1
            else:
                to_write.append((row_id, value, encryptor.rotate(value)))
        if to_write:
            written, changed = await _write_rows(db, column, to_write)
            counts.re_encrypted += written
            counts.changed_concurrently += changed
            await db.commit()
        else:
            # End the read transaction: no snapshot or lock outlives a batch.
            await db.rollback()


async def _write_slack_install(pipe: Any, key: str, value: str) -> bool:
    """Write ``value`` if the WATCHed ``key`` is unchanged; keep its TTL.

    Returns:
        False when the bundle changed or expired since it was read.
    """
    pipe.multi()
    pipe.set(key, value, xx=True, keepttl=True)
    try:
        (stored,) = await pipe.execute()
    except WatchError:
        return False
    return bool(stored)


async def rekey_slack_installs(redis: Any, encryptor: APIKeyEncryption, *, dry_run: bool) -> Counts:
    """Re-encrypt the bot token of the pending Slack install bundles.

    A bundle without a token is not counted; one that is not a JSON object
    counts as undecryptable. The handles are not listed: a handle completes
    an install.
    """
    counts = Counts()
    async for key in redis.scan_iter(match=SLACK_INSTALL_PATTERN, count=500):
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            raw = await pipe.get(key)
            if raw is None:
                continue  # expired since the SCAN
            try:
                bundle = json.loads(raw)
            except ValueError:
                bundle = None
            if not isinstance(bundle, dict):
                counts.undecryptable += 1
                continue
            token = bundle.get(_SLACK_TOKEN_FIELD)
            if not token:
                continue
            state = _classify(encryptor, str(token))
            if state is None:
                counts.undecryptable += 1
            elif state:
                counts.already_current += 1
            elif dry_run:
                counts.re_encrypted += 1
            else:
                bundle[_SLACK_TOKEN_FIELD] = encryptor.rotate(str(token))
                if await _write_slack_install(pipe, key, json.dumps(bundle)):
                    counts.re_encrypted += 1
                else:
                    counts.changed_concurrently += 1
    return counts


async def rekey_encrypted_secrets(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    encryptor: APIKeyEncryption | None = None,
    redis: Any = None,
    columns: Sequence[InstrumentedAttribute[Any]] = ENCRYPTED_COLUMNS,
    batch_size: int = BATCH_SIZE,
) -> RekeyReport:
    """Report (``dry_run``) or re-encrypt every stored secret under the current key.

    Args:
        db: The session. Each batch is its own transaction.
        dry_run: Count only; write nothing.
        encryptor: The key ring; ``get_encryptor()`` by default.
        redis: The Redis client; ``get_redis_client()`` by default.
        columns: The columns to walk (``ENCRYPTED_COLUMNS``).
        batch_size: Rows read per batch.

    Returns:
        The counts per column (never a value).
    """
    encryptor = encryptor or get_encryptor()
    report = RekeyReport(dry_run=dry_run, key_count=encryptor.key_count)
    for column in columns:
        report.columns[column_label(column)] = await _rekey_column(
            db, column, encryptor, dry_run=dry_run, batch_size=batch_size
        )
    try:
        client = redis if redis is not None else get_redis_client()
        report.columns[SLACK_INSTALL_LABEL] = await rekey_slack_installs(
            client, encryptor, dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001 - reported; the database part stands
        report.redis_error = type(exc).__name__
        logger.warning("rekey_slack_installs_failed", error_class=report.redis_error)
    if not dry_run:
        logger.info(
            "encrypted_secrets_rekeyed",
            re_encrypted=report.re_encrypted,
            undecryptable=report.undecryptable,
            changed_concurrently=report.total("changed_concurrently"),
        )
    return report


def _print_report(report: RekeyReport) -> None:
    """The per-column table and the total; ids of undecryptable rows, capped."""
    print(f"key ring: API_KEY_SECRET + {report.key_count - 1} previous key(s)")
    width = max((len(label) for label in report.columns), default=0)
    rewrite = "to_re_encrypt" if report.dry_run else "re_encrypted"
    print(
        f"{'column':{width}}  {'already_current':>15}  {rewrite:>13}  "
        f"{'undecryptable':>13}  {'changed_concurrently':>20}"
    )

    def line(label: str, counts: Counts) -> str:
        return (
            f"{label:{width}}  {counts.already_current:>15}  {counts.re_encrypted:>13}  "
            f"{counts.undecryptable:>13}  {counts.changed_concurrently:>20}"
        )

    for label, counts in report.columns.items():
        print(line(label, counts))
    total = Counts(
        already_current=report.total("already_current"),
        re_encrypted=report.re_encrypted,
        undecryptable=report.undecryptable,
        changed_concurrently=report.total("changed_concurrently"),
    )
    print(line("total", total))
    if report.redis_error:
        print(f"{SLACK_INSTALL_LABEL}: not checked, Redis failed ({report.redis_error})")
    for label, counts in report.columns.items():
        if not counts.undecryptable_ids:
            continue
        ids = counts.undecryptable_ids
        listed = ", ".join(ids[:MAX_LISTED_IDS])
        more = f" (and {len(ids) - MAX_LISTED_IDS} more)" if len(ids) > MAX_LISTED_IDS else ""
        print(f"undecryptable {label} ids: {listed}{more}")
    if report.undecryptable:
        print(
            "undecryptable values are left as they are: add the key they were written "
            "with to API_KEY_SECRET_PREVIOUS and run again, or re-enter them"
        )
    if report.total("changed_concurrently"):
        print("changed_concurrently values were rewritten by the application; run again")


def _summary(report: RekeyReport) -> str:
    return f"{report.re_encrypted} encrypted value(s) to the current key"


def _derive_fernet_key_from_stdin() -> int:
    """Print the Fernet key the secret on stdin stands for (``--derive-fernet-key``).

    The secret is read as bytes, exactly as the variable holds it; only one
    trailing newline (``printenv``, ``echo``) is dropped. No logging, no
    settings, no database or Redis.
    """
    raw = sys.stdin.buffer.read().decode()
    secret = raw[:-1] if raw.endswith("\n") else raw
    if not secret:
        print("error: --derive-fernet-key reads the secret from stdin", file=sys.stderr)
        return 2
    print(derive_fernet_key(secret))
    return 0


async def _main(args: argparse.Namespace) -> int:
    if args.derive_fernet_key:
        if args.yes:
            print("error: --yes needs --apply", file=sys.stderr)
            return 2
        return _derive_fernet_key_from_stdin()
    configure_logging(args.log_level)
    # Both keys before the settings are cached (#2239): the encryptor reads
    # them from the settings.
    require_settings(api_key_secret=require_api_key_secrets())
    last: list[RekeyReport] = []

    async def run(db: AsyncSession, dry_run: bool) -> RekeyReport:
        report = await rekey_encrypted_secrets(db, dry_run=dry_run)
        last.append(report)
        return report

    code = await run_plan_apply(
        run=run,
        print_plan=_print_report,
        changes=lambda report: report.re_encrypted,
        noun="encrypted value",
        verb="update",
        summary=_summary,
        print_applied=_print_report,
        apply=args.apply,
        assume_yes=args.yes,
    )
    if code or not last:
        return code
    if last[-1].redis_error:
        return 1
    if last[-1].undecryptable:
        return EXIT_UNDECRYPTABLE
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="report the counts, write nothing (default)",
    )
    mode.add_argument("--apply", action="store_true", help="re-encrypt under the current key")
    mode.add_argument(
        "--derive-fernet-key",
        action="store_true",
        help=(
            "print the Fernet key of the secret on stdin, to list a key with a comma "
            "or surrounding blanks in API_KEY_SECRET_PREVIOUS"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
