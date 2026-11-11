"""Which OAuth clients are Google connected apps (#2773).

Google's connected apps program asks the authorization server not to rotate
the refresh token of its client: a refresh answers with the exact refresh
token the client sent ("persistent linking"). Every other client keeps
rotation (RFC 6819 §5.2.2.3).

A client counts as Google's only when DCR stored it under
:data:`GOOGLE_CONNECTED_APPS_PROVIDER` (which DCR assigns only when every
registered redirect URI is in ``GOOGLE_CONNECTED_APPS_REDIRECT_URIS``) and
every one of its redirect URIs is still in that setting. Changing a client's
redirect URIs or emptying the setting therefore restores rotation. Manual
client creation cannot store this provider (its ``provider`` pattern leaves
it out). The client must also hold no grant beyond the code grant and
refresh: a device-code poller, for one, gets its tokens without going
through Google's redirector, so such a client rotates.
"""

from __future__ import annotations

from typing import Any

from config.settings import get_settings

#: ``oauth_clients.provider`` of a Google connected-apps client (set by DCR only).
GOOGLE_CONNECTED_APPS_PROVIDER = "google_connected_apps"

#: Grants a kept-refresh client may hold: tokens reach it only through the
#: code grant's redirect to a listed URI, then through refresh.
_KEPT_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})


def is_google_connected_apps_redirect_uri(redirect_uri: str) -> bool:
    """Whether ``redirect_uri`` is listed in ``GOOGLE_CONNECTED_APPS_REDIRECT_URIS``.

    Compared character for character with the setting as it is now.
    """
    return redirect_uri in get_settings().google_connected_apps_redirect_uri_list


def keeps_refresh_token(client: Any) -> bool:
    """Whether a refresh by ``client`` keeps its refresh token (no rotation).

    Args:
        client: The ``OAuth2Client`` row of the request.

    Returns:
        True only for a ``google_connected_apps`` client with at least one
        redirect URI, every one of them in the current setting, and no grant
        type beyond ``authorization_code`` and ``refresh_token``.
    """
    if getattr(client, "provider", None) != GOOGLE_CONNECTED_APPS_PROVIDER:
        return False
    if not set(getattr(client, "grant_types", None) or []) <= _KEPT_GRANT_TYPES:
        return False
    redirect_uris = getattr(client, "redirect_uris", None) or []
    if not redirect_uris:
        return False
    listed = get_settings().google_connected_apps_redirect_uri_list
    return all(uri in listed for uri in redirect_uris)
