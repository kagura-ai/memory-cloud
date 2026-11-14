"""The single ``Authorization`` header of a service-token request (#2801)."""

from __future__ import annotations

from starlette.requests import Request


def sole_authorization(request: Request) -> str | None:
    """Return the request's only ``Authorization`` value, else ``None``.

    Two or more headers are treated exactly like none, so a service-token
    check never judges whichever copy a proxy happened to put first. The
    header name is matched case-insensitively.
    """
    values = request.headers.getlist("authorization")
    return values[0] if len(values) == 1 else None
