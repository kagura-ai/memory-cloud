"""Staged OAuth credential hashing and explicit finalization (#2399).

The Alembic migration installs bridge mode, because migrations run before old
API processes are drained. Never infer that old writers are gone from an empty
table. All compatible processes read the durable mode inside the issuance
transaction; finalization excludes writers until the mode and CHECK commit.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.logger import get_logger

HASH_PREFIX = "sha256$"
HASH_PATTERN = r"^sha256\$[0-9a-f]{64}$"
NON_HASH_SQL = (
    "access_token !~ :pattern OR (refresh_token IS NOT NULL AND refresh_token !~ :pattern)"
)
logger = get_logger(__name__)


def hash_oauth_token(token: str | None) -> str | None:
    """Hash an issued opaque credential; preserve absent refresh tokens."""
    if token is None:
        return None
    return HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_lookup_values(presented: str) -> tuple[str, ...]:
    """Return hash and legacy lookup candidates, rejecting stored hash inputs.

    The reserved prefix contains ``$``, absent from issued urlsafe tokens.
    Reject the entire prefix (including malformed hashes) before either lookup
    to prevent a database digest from becoming a bearer credential.
    """
    if not presented or presented.startswith(HASH_PREFIX):
        return ()
    return (str(hash_oauth_token(presented)), presented)


def token_matches(presented: str, stored: str | None) -> bool:
    """Match a credential after a query without comparing plaintext to a digest."""
    return stored is not None and any(
        hmac.compare_digest(candidate.encode(), stored.encode())
        for candidate in token_lookup_values(presented)
    )


def issuance_storage_mode(session: Session) -> str:
    """Read durable issuance mode after excluding concurrent finalization.

    PostgreSQL's ROW EXCLUSIVE lock is compatible with other token writers but
    conflicts with finalization. It must precede the mode read: otherwise a
    writer can read bridge, wait behind the finalizer, then insert plaintext.
    SQLite is used only by isolated unit tests and has no LOCK TABLE syntax.
    A missing state row/schema fails closed; it never silently means bridge.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE oauth_tokens IN ROW EXCLUSIVE MODE"))
    mode = session.execute(text("SELECT mode FROM oauth_token_storage WHERE id = 1")).scalar_one()
    if mode not in ("bridge", "hashed"):
        raise RuntimeError("Invalid OAuth token storage mode")
    return mode


def finalize_oauth_token_storage(
    session: Session, *, writers_drained: bool, lock_timeout_ms: int = 5000
) -> int:
    """Convert every token and prohibit plaintext writes in one transaction.

    Requires a fresh session and operator confirmation that all incompatible
    readers and writers have stopped. A failure rolls back data, CHECK and mode
    together. Lock waits are bounded; retry after contention is safe.

    Returns:
        Number of rows whose access or refresh credential was converted.
    """
    if not writers_drained:
        raise ValueError("Confirm --writers-drained after stopping all incompatible processes")
    if not 1 <= lock_timeout_ms <= 60000:
        raise ValueError("lock_timeout_ms must be between 1 and 60000")
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OAuth token storage finalization requires PostgreSQL")
    with session.begin():
        session.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{lock_timeout_ms}ms"},
        )
        # ADD CHECK also needs ACCESS EXCLUSIVE. Acquire it first to avoid a
        # reader-to-writer lock upgrade deadlock halfway through finalization.
        session.execute(text("LOCK TABLE oauth_tokens IN ACCESS EXCLUSIVE MODE"))
        mode = session.execute(
            text("SELECT mode FROM oauth_token_storage WHERE id = 1 FOR UPDATE")
        ).scalar_one()
        if mode not in ("bridge", "hashed"):
            raise RuntimeError("Invalid OAuth token storage mode")
        converted = session.execute(
            text("SELECT count(*) FROM oauth_tokens WHERE " + NON_HASH_SQL),
            {"pattern": HASH_PATTERN},
        ).scalar_one()
        session.execute(
            text(
                "UPDATE oauth_tokens SET "
                "access_token = CASE WHEN access_token ~ :pattern THEN access_token "
                "ELSE 'sha256$' || encode(sha256(convert_to(access_token, 'UTF8')), 'hex') END, "
                "refresh_token = CASE WHEN refresh_token IS NULL OR refresh_token ~ :pattern "
                "THEN refresh_token ELSE 'sha256$' || "
                "encode(sha256(convert_to(refresh_token, 'UTF8')), 'hex') END "
                "WHERE " + NON_HASH_SQL
            ),
            {"pattern": HASH_PATTERN},
        )
        # Validated in this same transaction, under the already-held exclusive
        # lock: splitting ALTER/VALIDATE would expose a half-finalized state.
        session.execute(
            text("ALTER TABLE oauth_tokens DROP CONSTRAINT IF EXISTS ck_oauth_tokens_hashed")
        )
        session.execute(
            text(
                "ALTER TABLE oauth_tokens ADD CONSTRAINT ck_oauth_tokens_hashed CHECK "
                "(access_token ~ '^sha256\\$[0-9a-f]{64}$' AND "
                "(refresh_token IS NULL OR refresh_token ~ '^sha256\\$[0-9a-f]{64}$'))"
            )
        )
        session.execute(text("UPDATE oauth_token_storage SET mode = 'hashed' WHERE id = 1"))
        remaining = session.execute(
            text("SELECT count(*) FROM oauth_tokens WHERE " + NON_HASH_SQL),
            {"pattern": HASH_PATTERN},
        ).scalar_one()
        if remaining:
            raise RuntimeError("OAuth token storage verification failed")
    return converted


def main() -> None:
    """Finalize storage only after an explicit operator drain acknowledgement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writers-drained", action="store_true", required=True)
    parser.add_argument("--lock-timeout-ms", type=int, default=5000)
    args = parser.parse_args()
    from db.base import get_sync_session

    with get_sync_session() as session:
        converted = finalize_oauth_token_storage(
            session, writers_drained=args.writers_drained, lock_timeout_ms=args.lock_timeout_ms
        )
    logger.info("oauth_token_storage_finalized", converted_rows=converted, non_hash_rows=0)


if __name__ == "__main__":
    main()
