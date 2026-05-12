"""Pin the e10_624 seed migration to the Issue #624 design contract.

The e10_624 migration seeds a single global public OAuth2 client
(``client_id='kagura-cli'``) used by the Kagura Memory Python SDK to
drive RFC 8628 device-flow login. The seed shape is fixed by the issue
review and re-verifying it from a live DB on every test run is overkill
— the actual reachable failure mode is someone editing the migration
file in a way that silently drifts from the design contract. These
pins fail loud on that edit.

Three classes of invariant are pinned:

1. **Revision chain** — ``revision`` / ``down_revision`` so the linear
   apply order from ``e09_608`` is preserved.
2. **Seed shape** — the constants ``_CLIENT_ID``, ``_SCOPE``,
   ``_GRANT_TYPES_JSON``, ``_REDIRECT_URIS_JSON``,
   ``_RESPONSE_TYPES_JSON`` exactly match the Issue #624 design.
   ``_SCOPE`` is also cross-pinned against ``ALL_ADVERTISED_SCOPES``
   so the seed cannot drift to a scope the server does not advertise.
3. **SQL invariants** — the upgrade SQL uses ``ON CONFLICT (client_id)
   DO NOTHING`` (idempotency), sets ``owner_id``/``workspace_id`` to
   ``NULL``, sets ``token_endpoint_auth_method='none'`` and
   ``client_secret_hash=''`` (public client). The downgrade DELETEs
   the seed row by ``client_id`` so the alembic chain stays reversible
   (``d04_519_oauth_owner_nullable.downgrade()`` would otherwise fail
   with ``owner_id=NULL`` still present); dependent tokens are
   cleaned up via existing ``ON DELETE CASCADE`` FKs on
   ``oauth_tokens`` and ``oauth_device_codes``.

The scope-string relationship test (gate1 #3, scaled-down) is also
covered here: ``memory:admin`` MUST NOT appear in the seeded scope.
Route-level enforcement (``require_scope("memory:admin")`` on admin
routes) is the responsibility of #608/#615; this test pins only the
seed-side of that contract.
"""

import importlib
import inspect
import json
import sys
from pathlib import Path

_BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

_ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_VERSIONS))

from auth.mcp_scopes import ALL_ADVERTISED_SCOPES  # noqa: E402

e10 = importlib.import_module("e10_624_seed_kagura_cli_client")


class TestE10SeedKaguraCliRevisionChain:
    def test_revision_id(self) -> None:
        assert e10.revision == "e10_624_seed_kagura_cli_client"

    def test_down_revision_chains_from_e09(self) -> None:
        """Linear chain pin: e10 follows e09_608 narrow. Reordering would
        break ``alembic upgrade head`` ordering."""
        assert e10.down_revision == "e09_608_dcr_default_narrow"

    def test_branch_labels_unset(self) -> None:
        assert e10.branch_labels is None
        assert e10.depends_on is None


class TestE10SeedShapeConstants:
    def test_client_id(self) -> None:
        """SDK companion (`kagura-memory-python-sdk#100`) hard-codes
        ``client_id='kagura-cli'``. Renaming here desyncs SDK auth."""
        assert e10._CLIENT_ID == "kagura-cli"

    def test_client_name(self) -> None:
        """``client_name`` shows on the ``/device`` consent page — end
        users see this string. Pin it so consent UX doesn't silently
        drift."""
        assert e10._CLIENT_NAME == "Kagura Memory CLI"

    def test_scope_excludes_memory_admin(self) -> None:
        """Gate1 review (#3, scaled-down): the seeded scope MUST NOT
        contain ``memory:admin``. Narrowing-first ordering per #608 D1
        means admin is route-enforced; admitting it here would silently
        grant admin to every SDK user."""
        scopes = set(e10._SCOPE.split())
        assert "memory:admin" not in scopes, (
            f"seeded scope contains memory:admin ({sorted(scopes)}) — "
            "this defeats the narrowing-first ordering from #608 D1. "
            "Route-level require_scope on admin endpoints is the other "
            "half of that contract (#608/#615); the seed side MUST stay "
            "admin-less."
        )

    def test_scope_is_subset_of_advertised(self) -> None:
        """Every seeded scope MUST be in ``ALL_ADVERTISED_SCOPES`` — the
        seed cannot grant a scope the server does not even advertise
        (would be a silent advertise-without-enforcement gap in the
        opposite direction)."""
        scopes = set(e10._SCOPE.split())
        advertised = set(ALL_ADVERTISED_SCOPES)
        unknown = scopes - advertised
        assert unknown == set(), (
            f"seeded scope contains unadvertised scopes {sorted(unknown)} — "
            f"runtime advertises {sorted(advertised)}."
        )

    def test_scope_exact_value(self) -> None:
        """Pin the exact ordered scope string. Issue #624 specifies
        ``memory:read memory:write`` and the SDK side may rely on this
        ordering for token introspection diffing."""
        assert e10._SCOPE == "memory:read memory:write"

    def test_grant_types_device_code_and_refresh_only(self) -> None:
        """Public client: ``authorization_code`` must NOT appear. The
        seeded grant_types limit this client to device-flow + refresh."""
        grants = set(json.loads(e10._GRANT_TYPES_JSON))
        assert grants == {
            "urn:ietf:params:oauth:grant-type:device_code",
            "refresh_token",
        }, (
            f"seeded grant_types {sorted(grants)} drift from the public "
            "device-flow shape. authorization_code MUST stay out of this "
            "client (it would require PKCE plumbing and is out of v1 scope)."
        )

    def test_response_types_empty(self) -> None:
        """No auth-code response — pin the empty list, not a placeholder."""
        assert json.loads(e10._RESPONSE_TYPES_JSON) == []

    def test_redirect_uris_shape(self) -> None:
        """OOB sentinel for pure device-flow + loopback wildcard for
        future PKCE fallback. The exact ordering of the array is part
        of the design and is the first-redirect-uri default Authlib
        will return from ``get_default_redirect_uri()`` — pin it."""
        uris = json.loads(e10._REDIRECT_URIS_JSON)
        assert uris == [
            "urn:ietf:wg:oauth:2.0:oob",
            "http://127.0.0.1:0/",
        ]


class TestE10UpgradeSqlInvariants:
    def test_upgrade_uses_on_conflict_do_nothing(self) -> None:
        """Idempotency contract: ``ON CONFLICT (client_id) DO NOTHING``
        makes re-runs safe AND preserves operator-edited fields."""
        src = inspect.getsource(e10.upgrade)
        assert "ON CONFLICT (client_id) DO NOTHING" in src, (
            "e10.upgrade() must use ON CONFLICT (client_id) DO NOTHING — "
            "ON CONFLICT DO UPDATE would silently overwrite operator-edited "
            "client_name / redirect_uris on re-run."
        )

    def test_upgrade_inserts_null_owner_and_workspace(self) -> None:
        """DCR-pattern (per #519): ``owner_id`` and ``workspace_id`` are
        both NULL so the workspace is resolved at consent time."""
        src = inspect.getsource(e10.upgrade)
        # Hand-grep the VALUES tuple. Both columns must be inserted as
        # SQL NULL literals (not the Python string 'NULL').
        assert "owner_id" in src
        assert "workspace_id" in src
        # The VALUES block has NULL on the two consecutive lines after
        # the client_name parameter; check both NULLs are present.
        assert src.count("NULL,") >= 2, (
            "e10.upgrade() VALUES must contain owner_id=NULL and "
            "workspace_id=NULL — at least 2 NULL,-separated values are "
            "expected before the JSON casts."
        )

    def test_upgrade_marks_public_client(self) -> None:
        """``client_secret_hash=''`` + ``token_endpoint_auth_method='none'``
        — the two halves of the public-client contract."""
        src = inspect.getsource(e10.upgrade)
        assert "'none'" in src, (
            "e10.upgrade() must set token_endpoint_auth_method='none' — "
            "anything else would break public-client device-flow."
        )

    def test_downgrade_deletes_the_seed_row(self) -> None:
        """Downgrade MUST remove the ``kagura-cli`` row so the alembic
        chain stays reversible — ``d04_519_oauth_owner_nullable`` sits
        earlier and its downgrade runs ``ALTER COLUMN owner_id SET NOT
        NULL``, which fails while this row's ``owner_id=NULL`` is still
        present. ``ON DELETE CASCADE`` on ``oauth_tokens`` and
        ``oauth_device_codes`` handles dependent cleanup atomically.

        See module docstring 'Downgrade policy' for the operational
        impact (active SDK sessions are invalidated)."""
        src = inspect.getsource(e10.downgrade)
        assert "DELETE FROM oauth_clients" in src, (
            "e10.downgrade() must DELETE the seed row. A no-op leaves "
            "an owner_id=NULL row that breaks d04_519.downgrade()."
        )
        # The DELETE must be scoped to the seeded client_id — a bare
        # ``DELETE FROM oauth_clients`` without a WHERE would wipe
        # every registered client.
        assert "WHERE client_id" in src, (
            "e10.downgrade() DELETE must include WHERE client_id = "
            ":client_id — a bare DELETE would wipe every OAuth client "
            "registered in the system."
        )

    def test_downgrade_uses_bound_client_id_constant(self) -> None:
        """The DELETE WHERE clause must bind ``_CLIENT_ID`` — pinning
        the constant prevents a rename from drifting the DELETE target
        away from the INSERT target."""
        src = inspect.getsource(e10.downgrade)
        assert "client_id=_CLIENT_ID" in src, (
            "e10.downgrade() must bind WHERE client_id to the module-"
            "level ``_CLIENT_ID`` constant so the DELETE target stays "
            "in sync with the INSERT target."
        )
