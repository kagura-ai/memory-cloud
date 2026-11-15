"""The custom domain license id cannot be registered as a sync service (#2809).

``public-mcp-domains`` is the paid integration license of verified custom
domains; a synchronization service under that id would share its license
row. The refusal precedes every statement: the session here raises on any
use.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.sync_service import register_service
from models.public_mcp_domain import DOMAIN_SERVICE_ID


class _NoDatabase:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the session was used: {name}")


def test_register_service_refuses_the_reserved_id() -> None:
    session: Any = _NoDatabase()
    with pytest.raises(ValueError, match="Invalid service ID"):
        register_service(session, DOMAIN_SERVICE_ID, "browser-client", "service-client")


def test_the_reserved_id_is_the_domain_license() -> None:
    assert DOMAIN_SERVICE_ID == "public-mcp-domains"
