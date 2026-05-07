"""Attachment API Routes — DEPRECATED.

Issue #555: All endpoints return HTTP 410 Gone after the #485 R2 file
storage migration. SDK clients and the frontend already use
``/api/v1/files/*``. The legacy routes remain in OpenAPI (with
``deprecated=True``) so SDK regeneration surfaces the retirement
clearly to any straggler clients.

The ``Attachment`` model and its BYTEA ``data`` column are preserved
until a follow-up PR drops them after another release window.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.dependencies import APIKeyOrSessionUser
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/attachments", tags=["attachments"])

_SUCCESSOR = "/api/v1/files/"
_DEPRECATION_HEADERS = {
    "Sunset": "Wed, 13 May 2026 00:00:00 GMT",
    "Deprecation": "true",
    "Link": f'<{_SUCCESSOR}>; rel="successor-version"',
}
_GONE_BODY = {
    "error": "RES-004",
    "message": "/api/v1/attachments/* has been retired. Use /api/v1/files/* instead.",
    "details": {"successor": _SUCCESSOR},
}


def _gone_response(request: Request, user: dict) -> JSONResponse:
    """Emit 410 Gone with deprecation headers and a structured warn log.

    The auth dependency is preserved on every route so ``user_id`` is
    available here — that is the signal we need to track down clients
    still hitting the legacy surface.
    """
    logger.warning(
        "legacy_attachment_route_hit",
        path=request.url.path,
        method=request.method,
        user_id=user.get("user_id"),
        user_agent=request.headers.get("user-agent"),
    )
    return JSONResponse(
        status_code=410,
        content=_GONE_BODY,
        headers=_DEPRECATION_HEADERS,
    )


@router.post("/memories/{memory_id}", status_code=410, deprecated=True)
async def upload_attachment_gone(
    memory_id: str,
    request: Request,
    user: APIKeyOrSessionUser,
) -> JSONResponse:
    return _gone_response(request, user)


@router.get("/memories/{memory_id}", status_code=410, deprecated=True)
async def list_attachments_gone(
    memory_id: str,
    request: Request,
    user: APIKeyOrSessionUser,
) -> JSONResponse:
    return _gone_response(request, user)


@router.get("/{attachment_id}", status_code=410, deprecated=True)
async def download_attachment_gone(
    attachment_id: str,
    request: Request,
    user: APIKeyOrSessionUser,
) -> JSONResponse:
    return _gone_response(request, user)


@router.delete("/{attachment_id}", status_code=410, deprecated=True)
async def delete_attachment_gone(
    attachment_id: str,
    request: Request,
    user: APIKeyOrSessionUser,
) -> JSONResponse:
    return _gone_response(request, user)
