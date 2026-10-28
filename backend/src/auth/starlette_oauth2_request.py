"""Starlette/FastAPI OAuth2Request integration for Authlib 1.6.5.

Provides OAuth2Request wrapper that properly implements payload property
for Starlette/FastAPI Request objects.

Based on Django integration pattern:
https://github.com/authlib/authlib/blob/master/authlib/integrations/django_oauth2/requests.py
"""

from collections import defaultdict
from typing import Any, cast

from authlib.oauth2.rfc6749 import OAuth2Request
from starlette.requests import Request


class StarletteOAuth2Payload:
    """OAuth2 payload implementation for Starlette/FastAPI.

    Authlib 1.6.5 requires payload to support attribute access.
    This class extracts data from Starlette Request's query_params and form_data.
    """

    def __init__(self, request: Request, *, include_form: bool = True):
        """Initialize payload from Starlette Request.

        Args:
            request: Starlette/FastAPI Request object
            include_form: Read the preloaded form as well (it applies over the
                query string). False reads the query string only: the consent
                POST, whose OAuth parameters travel in its query and whose form
                carries only the user's answer (#2199).
        """
        self._request = request
        self._include_form = include_form
        self._data_cache = None

    def __getattr__(self, key: str):
        """Allow attribute access like payload.response_type.

        Authlib grants access payload.response_type, payload.client_id, etc.
        as attributes rather than dict keys. This method enables that pattern.

        Args:
            key: Attribute name to access

        Returns:
            Value from data dict, or None if key doesn't exist

        Note:
            Returns None for missing keys instead of raising AttributeError.
            This matches Authlib's expectation for optional parameters like scope.
        """
        # Avoid infinite recursion for private attributes
        if key.startswith("_"):
            raise AttributeError(key)

        # Return value or None (not AttributeError) for Authlib compatibility
        return self.data.get(key)

    def __getitem__(self, key: str):
        """Allow dict-style access like payload['response_type']."""
        return self.data.get(key)

    def get(self, key, default=None):
        """Dict-like get method for compatibility."""
        return self.data.get(key, default)

    @property
    def data(self) -> dict[str, str]:
        """Get flattened data dict from query + form.

        Returns:
            Dict with single value per key (later values override earlier)
        """
        if self._data_cache is not None:
            return self._data_cache

        data: dict[str, str] = {}

        # Query params first (OAuth2 /authorize params are here)
        for k, v in self._request.query_params.items():
            data[k] = v

        # Preloaded form data (set by preload_form dependency)
        form: dict | None = (
            getattr(self._request.state, "form_data", None) if self._include_form else None
        )
        if form:
            for k, v in form.items():
                data[k] = v

        # RFC 6749 §§3.1/3.2: a single empty value is omitted for lookup.
        # Keep datalist untouched so empty occurrences still count toward the
        # separate duplicate guard (#2238), before scalar values are consumed.
        data = {key: value for key, value in data.items() if value != ""}
        self._data_cache = data
        return data

    @property
    def datalist(self) -> dict[str, list[str]]:
        """Get data dict with list values (supports multiple values per key).

        Returns:
            Dict with list of values per key
        """
        values: dict[str, list[str]] = defaultdict(list)

        # Query params (multi-value support)
        for k, v in self._request.query_params.multi_items():
            values[k].append(v)

        if not self._include_form:
            return values

        # Form data: every value of a repeated field (``form_items``, #1686),
        # else the one-value-per-name ``form_data``.
        form_items = getattr(self._request.state, "form_items", None)
        form: dict | None = getattr(self._request.state, "form_data", None)
        if form_items is not None:
            for k, v in form_items:
                values[k].append(v)
        elif form:
            for k, v in form.items():
                values[k].append(v)

        return values


class StarletteOAuth2Request(OAuth2Request):
    """OAuth2Request implementation for Starlette/FastAPI.

    Wraps Starlette Request and provides OAuth2-compatible interface
    with proper payload support for Authlib 1.6.5.

    This follows the same pattern as Django OAuth2 integration.
    """

    def __init__(self, request: Request, *, include_form: bool = True):
        """Create OAuth2Request from Starlette Request.

        Args:
            request: Starlette/FastAPI Request object
            include_form: See :class:`StarletteOAuth2Payload`; False also
                leaves ``form`` empty (#2199).
        """
        uri = str(request.url)

        # Handle X-Forwarded-Proto for HTTPS behind reverse proxy
        if request.headers.get("x-forwarded-proto") == "https":
            uri = uri.replace("http://", "https://", 1)

        # Initialize base OAuth2Request
        super().__init__(
            method=request.method,
            uri=uri,
            headers=dict(request.headers),
        )

        # Set payload explicitly (required for Authlib 1.6.5).
        # Authlib's OAuth2Request types `payload` as `None` initially, then
        # mutates it at runtime — the assignment here is a contract Authlib
        # documents but the type stub does not capture. Cast keeps the
        # narrowing local to this constructor.
        self.payload = cast(Any, StarletteOAuth2Payload(request, include_form=include_form))
        self._request = request
        self._include_form = include_form

    @property
    def form(self):
        """Get form data for client authentication.

        Authlib uses request.form to extract client_id/client_secret
        for client_secret_post authentication method.

        Returns:
            Dict of form data from request.state.form_data; empty when the
            request was created with ``include_form=False``
        """
        if not self._include_form:
            return {}
        return getattr(self._request.state, "form_data", {})
