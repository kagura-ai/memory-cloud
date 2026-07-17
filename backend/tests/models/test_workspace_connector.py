"""Unit tests for the WorkspaceConnector model (Issue #850, F6-a of #755).

Schema-only slice. Verifies:
- the Fernet round-trip helpers keep plaintext OAuth tokens off the column,
- the 1:1 ``resource_pk`` contract (NOT NULL + UNIQUE),
- the model is intentionally exempt from ``_enforce_resource_pk_invariant``
  (it carries no ``resource_id`` slug, so the dual-write guard does not apply).

These are ORM-mock / metadata-introspection tests (no DB) so they run under
``make test-local``.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from models.resource import (
    ResourceToken,
    WorkspaceConnector,
    _enforce_resource_pk_invariant,
)


@pytest.fixture
def _fernet_env(monkeypatch):
    """Set a deterministic Fernet key and reset the get_encryptor singleton."""
    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    import utils.encryption as enc_module

    enc_module._encryptor = None
    yield
    enc_module._encryptor = None


class TestOAuthTokenEncryption:
    def test_round_trip(self, _fernet_env):
        c = WorkspaceConnector()
        tokens = {"access_token": "xoxb-abc-123", "refresh_token": "xoxr-def-456"}
        c.set_oauth_tokens(tokens)

        # Ciphertext is stored, and plaintext never appears in the column.
        assert c.oauth_tokens_encrypted is not None
        assert "xoxb-abc-123" not in c.oauth_tokens_encrypted
        assert "xoxr-def-456" not in c.oauth_tokens_encrypted
        # Round-trips back to the original bundle.
        assert c.get_oauth_tokens() == tokens

    def test_empty_bundle_clears_column(self, _fernet_env):
        c = WorkspaceConnector()
        c.set_oauth_tokens({"a": "b"})
        assert c.oauth_tokens_encrypted is not None

        c.set_oauth_tokens(None)
        assert c.oauth_tokens_encrypted is None
        assert c.get_oauth_tokens() is None

    def test_get_when_unset_returns_none(self, _fernet_env):
        assert WorkspaceConnector().get_oauth_tokens() is None


class TestSchemaContract:
    def test_resource_pk_not_null_and_unique(self):
        table = WorkspaceConnector.__table__
        assert "resource_pk" in table.columns
        # New table => no Phase-1 nullable shadow window. NOT NULL from creation.
        assert table.c.resource_pk.nullable is False
        # 1:1 connector -> resource enforced by a UNIQUE on resource_pk.
        unique_col_sets = {
            tuple(col.name for col in uc.columns)
            for uc in table.constraints
            if uc.__class__.__name__ == "UniqueConstraint"
        }
        assert ("resource_pk",) in unique_col_sets

    def test_no_resource_id_slug_column(self):
        # Links purely by the resource_pk UUID — no slug mirror, which
        # sidesteps the CWE-639 slug-reuse class entirely.
        assert "resource_id" not in WorkspaceConnector.__table__.columns

    def test_connector_type_check_constraint_present(self):
        check_names = {
            c.name
            for c in WorkspaceConnector.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert "check_connector_type" in check_names

    def test_dispatch_uniqueness_is_app_qualified(self):
        table = WorkspaceConnector.__table__
        assert table.c.app_key.nullable is False
        indexes = {
            index.name: tuple(column.name for column in index.columns) for index in table.indexes
        }
        assert indexes["ix_workspace_connectors_app_team"] == (
            "connector_type",
            "app_key",
            "external_team_id",
        )

    def test_exempt_from_resource_pk_invariant_listener(self):
        # The dual-write guard hooks the slug-bearing satellites only.
        assert not event.contains(
            WorkspaceConnector, "before_insert", _enforce_resource_pk_invariant
        )
        # Sanity check that a slug-bearing satellite IS hooked, so this test
        # would fail loudly if the listener wiring changed shape.
        assert event.contains(ResourceToken, "before_insert", _enforce_resource_pk_invariant)
