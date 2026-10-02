"""Opaque public ids for keys, tokens and invitations (#1008).

The integer primary keys of ``api_keys``, ``share_keys``, ``resource_tokens``
and ``workspace_invitations`` stay internal (joins, foreign keys, usage
stats). What leaves the server — response bodies, path parameters, MCP tool
arguments, new audit rows — is a prefixed opaque id: ``<prefix>_`` followed
by 22 base62 characters, about 131 bits of randomness. One prefix per
resource type, never ``kagura_`` (that is what secrets look like).

Rows inserted without a ``public_id`` (an app instance from before the
migration still running during a rolling deploy) get one from the column's
``server_default``: 22 lowercase hex characters from ``gen_random_uuid()``.
Hex is a subset of base62, so those ids match the same pattern.
"""

from __future__ import annotations

import secrets
import string
from enum import StrEnum
from typing import Annotated

from fastapi import Path
from sqlalchemy import String, text
from sqlalchemy.orm import MappedColumn, mapped_column

BASE62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
PUBLIC_ID_BODY_LENGTH = 22
# ``<prefix>_`` (5) + 22 = 27; the column leaves room for a longer prefix.
PUBLIC_ID_COLUMN_LENGTH = 32


class PublicIdPrefix(StrEnum):
    """The prefix of each resource type's public id."""

    API_KEY = "akey"
    SHARE_KEY = "skey"
    RESOURCE_TOKEN = "rtok"
    INVITATION = "winv"


def new_public_id(prefix: PublicIdPrefix) -> str:
    """Return a fresh public id such as ``akey_3fJ0...``.

    Args:
        prefix: The resource type's prefix.

    Returns:
        ``<prefix>_`` plus 22 base62 characters from ``secrets``.
    """
    body = "".join(secrets.choice(BASE62_ALPHABET) for _ in range(PUBLIC_ID_BODY_LENGTH))
    return f"{prefix}_{body}"


def public_id_pattern(prefix: PublicIdPrefix) -> str:
    """Return the anchored regex a public id of ``prefix`` matches.

    Args:
        prefix: The resource type's prefix.

    Returns:
        A regex string usable as a Pydantic / FastAPI ``pattern``.
    """
    return rf"^{prefix}_[0-9A-Za-z]{{{PUBLIC_ID_BODY_LENGTH}}}$"


def public_id_server_default(prefix: PublicIdPrefix) -> str:
    """Return the SQL expression the ``public_id`` column defaults to.

    Used by the model (``server_default=text(...)``) and inlined verbatim in
    the e91 migration, so ``create_all`` and alembic produce the same DDL.

    Args:
        prefix: The resource type's prefix.

    Returns:
        A PostgreSQL expression producing ``<prefix>_`` + 22 hex characters.
    """
    return (
        f"'{prefix}_' || substr(replace((gen_random_uuid())::text, '-', ''), 1, "
        f"{PUBLIC_ID_BODY_LENGTH})"
    )


def public_id_column(prefix: PublicIdPrefix) -> MappedColumn[str]:
    """Return the ``public_id`` column for a model whose ids use ``prefix``.

    ``unique=True`` yields the ``<table>_public_id_key`` constraint via the
    metadata naming convention, matching the e91 migration.

    Args:
        prefix: The resource type's prefix.

    Returns:
        A ``mapped_column`` with a Python default (base62) and a DB default.
    """
    return mapped_column(
        String(PUBLIC_ID_COLUMN_LENGTH),
        nullable=False,
        unique=True,
        default=lambda: new_public_id(prefix),
        server_default=text(public_id_server_default(prefix)),
    )


def _path(prefix: PublicIdPrefix, what: str) -> object:
    return Path(
        pattern=public_id_pattern(prefix),
        description=f"Public id of the {what} (`{prefix}_` + 22 base62 characters).",
        examples=[f"{prefix}_{'0' * PUBLIC_ID_BODY_LENGTH}"],
    )


APIKeyPublicId = Annotated[str, _path(PublicIdPrefix.API_KEY, "API key")]
ShareKeyPublicId = Annotated[str, _path(PublicIdPrefix.SHARE_KEY, "share key")]
ResourceTokenPublicId = Annotated[str, _path(PublicIdPrefix.RESOURCE_TOKEN, "resource token")]
InvitationPublicId = Annotated[str, _path(PublicIdPrefix.INVITATION, "workspace invitation")]
