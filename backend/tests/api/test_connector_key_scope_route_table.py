"""The connector key REST list against the app's route table (#2929).

``CONNECTOR_KEY_REST_ROUTES`` (``auth.connector_key_scope``) names the REST
routes that accept a connector key. Every entry must be a registered
(method, route template) pair, so a renamed route cannot silently leave the
list; and every route that reads a bearer API key must do so through one of the
dependencies that apply the connector key rule, so a route that reads the key
itself cannot skip it. The behaviour of every route with a real connector key
is the DB-backed sweep in ``tests/integration/test_connector_key_scope_routes_db.py``.
"""

from __future__ import annotations

from auth.connector_key_scope import CONNECTOR_KEY_REST_ROUTES
from auth.dependencies import (
    get_api_key,
    get_user_from_api_key_or_session,
    require_session_auth,
    verify_api_key_user,
)
from tests.api._route_inventory import _iter_routes, route_dependency_calls

# The dependencies that turn a bearer API key into a REST principal and apply
# the connector key rule (#2929), and the one that refuses every bearer.
_GATED_CONSUMERS = (verify_api_key_user, get_user_from_api_key_or_session, require_session_auth)

# Routes that read the bearer API key themselves. Each one is justified here.
_OWN_KEY_READERS = {
    # Public context search and info (#626): only a public-bound key is
    # accepted; a connector key is workspace-scoped, never bound
    # (``ck_api_keys`` makes the two exclusive), so it is refused with 403
    # before any principal exists.
    ("POST", "/api/v1/public/{context_id}/search"),
    ("GET", "/api/v1/public/{context_id}/info"),
}


def test_every_listed_route_is_registered() -> None:
    registered = {(method, path) for method, path, _route, _extra in _iter_routes()}
    assert CONNECTOR_KEY_REST_ROUTES <= registered, sorted(CONNECTOR_KEY_REST_ROUTES - registered)


def test_every_bearer_api_key_reader_applies_the_connector_key_rule() -> None:
    unchecked = []
    own_readers = set()
    for method, path, route, extra in _iter_routes():
        calls = route_dependency_calls(route, extra)
        if get_api_key not in calls:
            continue
        if any(consumer in calls for consumer in _GATED_CONSUMERS):
            continue
        own_readers.add((method, path))
        if (method, path) not in _OWN_KEY_READERS:
            unchecked.append((method, path))
    assert unchecked == []
    # The justification list does not go stale.
    assert own_readers == _OWN_KEY_READERS
