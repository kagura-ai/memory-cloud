"""``StarletteOAuth2Payload`` / ``StarletteOAuth2Request`` sources (#2199).

By default the preloaded form applies over the query string and every value
of a repeated form field is kept (the token endpoint, #1686). With
``include_form=False`` (the consent POST) only the query string is read and
``.form`` is empty, so a form field cannot change what Authlib validates.
"""

from __future__ import annotations

from starlette.requests import Request

from auth.starlette_oauth2_request import StarletteOAuth2Payload, StarletteOAuth2Request


def _request(query: str, form: dict[str, str], items: list[tuple[str, str]]) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("testserver", 443),
            "path": "/api/v1/oauth/authorize",
            "query_string": query.encode(),
            "headers": [],
        }
    )
    request.state.form_data = form
    request.state.form_items = items
    return request


_QUERY = "client_id=public&scope=memory%3Aread&scope=memory%3Awrite"
_FORM = {"client_id": "confidential", "scope": "memory:admin", "confirm": "yes"}
_ITEMS = [
    ("client_id", "confidential"),
    ("scope", "memory:admin"),
    ("scope", "memory:admin2"),
    ("confirm", "yes"),
]


def test_query_only_payload_ignores_the_form() -> None:
    payload = StarletteOAuth2Payload(_request(_QUERY, _FORM, _ITEMS), include_form=False)

    assert payload.data == {"client_id": "public", "scope": "memory:write"}
    assert payload.client_id == "public"
    assert "confirm" not in payload.data
    assert dict(payload.datalist) == {
        "client_id": ["public"],
        "scope": ["memory:read", "memory:write"],
    }


def test_query_only_request_has_no_form() -> None:
    oauth_request = StarletteOAuth2Request(_request(_QUERY, _FORM, _ITEMS), include_form=False)

    assert oauth_request.form == {}
    assert oauth_request.payload.client_id == "public"
    assert oauth_request.payload.scope == "memory:write"


def test_default_payload_applies_the_form_over_the_query() -> None:
    payload = StarletteOAuth2Payload(_request(_QUERY, _FORM, _ITEMS))

    assert payload.data == {"client_id": "confidential", "scope": "memory:admin", "confirm": "yes"}
    # Every repeated value is kept, from both sources (#1686 repeated_parameter).
    assert dict(payload.datalist) == {
        "client_id": ["public", "confidential"],
        "scope": ["memory:read", "memory:write", "memory:admin", "memory:admin2"],
        "confirm": ["yes"],
    }


def test_default_request_exposes_the_form() -> None:
    oauth_request = StarletteOAuth2Request(_request(_QUERY, _FORM, _ITEMS))

    assert oauth_request.form == _FORM
    assert oauth_request.payload.client_id == "confidential"
