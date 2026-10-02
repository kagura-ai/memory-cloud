"""``public_id`` columns on keys, tokens and invitations (#1008)."""

import re

import pytest
from sqlalchemy import String

from models.auth import APIKey, ShareKey, WorkspaceInvitation
from models.resource import ResourceToken
from utils.public_id import PublicIdPrefix, public_id_pattern, public_id_server_default

CASES = [
    (APIKey, PublicIdPrefix.API_KEY),
    (ShareKey, PublicIdPrefix.SHARE_KEY),
    (ResourceToken, PublicIdPrefix.RESOURCE_TOKEN),
    (WorkspaceInvitation, PublicIdPrefix.INVITATION),
]


@pytest.mark.parametrize(("model", "prefix"), CASES)
def test_public_id_column_shape(model: type, prefix: PublicIdPrefix) -> None:
    column = model.__table__.c.public_id
    assert isinstance(column.type, String)
    assert column.type.length == 32
    assert column.nullable is False
    assert column.unique is True
    # Python-side default: a base62 id with the resource's prefix.
    pid = column.default.arg(None)
    assert re.fullmatch(public_id_pattern(prefix), pid)
    # DB-side default for rows inserted by an app instance that predates
    # the column (rolling deploy).
    assert column.server_default.arg.text == public_id_server_default(prefix)


@pytest.mark.parametrize(("model", "prefix"), CASES)
def test_unique_constraint_name(model: type, prefix: PublicIdPrefix) -> None:
    names = {
        c.name
        for c in model.__table__.constraints
        if getattr(c, "columns", None) is not None and list(c.columns.keys()) == ["public_id"]
    }
    assert f"{model.__tablename__}_public_id_key" in names
