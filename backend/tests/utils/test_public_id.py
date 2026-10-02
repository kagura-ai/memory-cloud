"""Opaque public ids for keys, tokens and invitations (#1008)."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.public_id import (
    APIKeyPublicId,
    InvitationPublicId,
    PublicIdPrefix,
    ResourceTokenPublicId,
    ShareKeyPublicId,
    new_public_id,
    public_id_pattern,
    public_id_server_default,
)


@pytest.mark.parametrize(
    ("prefix", "literal"),
    [
        (PublicIdPrefix.API_KEY, "akey"),
        (PublicIdPrefix.SHARE_KEY, "skey"),
        (PublicIdPrefix.RESOURCE_TOKEN, "rtok"),
        (PublicIdPrefix.INVITATION, "winv"),
    ],
)
def test_prefix_values(prefix: PublicIdPrefix, literal: str) -> None:
    assert prefix == literal


@pytest.mark.parametrize("prefix", list(PublicIdPrefix))
def test_new_public_id_shape(prefix: PublicIdPrefix) -> None:
    pid = new_public_id(prefix)
    assert re.fullmatch(rf"{prefix}_[0-9A-Za-z]{{22}}", pid)
    assert re.fullmatch(public_id_pattern(prefix), pid)
    assert len(pid) <= 32  # column is String(32)


def test_new_public_id_is_random() -> None:
    ids = {new_public_id(PublicIdPrefix.API_KEY) for _ in range(1000)}
    assert len(ids) == 1000


def test_never_uses_secret_prefix() -> None:
    assert all(not p.value.startswith("kagura") for p in PublicIdPrefix)


@pytest.mark.parametrize("prefix", list(PublicIdPrefix))
def test_pattern_rejects_other_shapes(prefix: PublicIdPrefix) -> None:
    pattern = re.compile(public_id_pattern(prefix))
    other = next(p for p in PublicIdPrefix if p != prefix)
    for bad in (
        "1",
        "42",
        f"{prefix}_",
        f"{prefix}_{'a' * 21}",
        f"{prefix}_{'a' * 23}",
        f"{prefix}_{'a' * 21}-",
        f"{other}_{'a' * 22}",
        f" {prefix}_{'a' * 22}",
    ):
        assert not pattern.fullmatch(bad), bad


@pytest.mark.parametrize("prefix", list(PublicIdPrefix))
def test_server_default_sql_names_the_prefix(prefix: PublicIdPrefix) -> None:
    sql = public_id_server_default(prefix)
    assert sql.startswith(f"'{prefix}_' || ")
    assert "gen_random_uuid()" in sql


def test_path_aliases_reject_integers_with_422() -> None:
    app = FastAPI()

    @app.get("/k/{key_id}")
    async def _k(key_id: APIKeyPublicId) -> dict[str, str]:
        return {"id": key_id}

    @app.get("/s/{key_id}")
    async def _s(key_id: ShareKeyPublicId) -> dict[str, str]:
        return {"id": key_id}

    @app.get("/t/{token_id}")
    async def _t(token_id: ResourceTokenPublicId) -> dict[str, str]:
        return {"id": token_id}

    @app.get("/i/{invitation_id}")
    async def _i(invitation_id: InvitationPublicId) -> dict[str, str]:
        return {"id": invitation_id}

    client = TestClient(app)
    for path, prefix in (
        ("k", PublicIdPrefix.API_KEY),
        ("s", PublicIdPrefix.SHARE_KEY),
        ("t", PublicIdPrefix.RESOURCE_TOKEN),
        ("i", PublicIdPrefix.INVITATION),
    ):
        assert client.get(f"/{path}/123").status_code == 422
        pid = new_public_id(prefix)
        resp = client.get(f"/{path}/{pid}")
        assert resp.status_code == 200
        assert resp.json() == {"id": pid}
