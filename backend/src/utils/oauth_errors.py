"""RFC 6749 §5.2 / RFC 7591 §3.2.2 error response helper for OAuth routes.

Compliant OAuth 2.0 clients (including the Anthropic SDK that ships with
Claude Code) parse error responses with a Zod-style schema expecting the
``error``/``error_description`` field shape. FastAPI's default
``HTTPException`` serializer wraps everything under ``{"detail": ...}``,
which causes those parsers to fail with a confusing ``ZodError`` instead of
surfacing the human-readable rejection reason. Use ``rfc6749_error_response``
in OAuth-protocol routes whenever returning a non-2xx response that a client
SDK is expected to consume programmatically.
"""

from fastapi.responses import JSONResponse


def rfc6749_error_response(
    error: str,
    description: str,
    status_code: int = 400,
) -> JSONResponse:
    """Build an OAuth 2.0 error response per RFC 6749 §5.2 / RFC 7591 §3.2.2.

    The response includes the ``Cache-Control: no-store`` and ``Pragma: no-cache``
    headers required by RFC 6749 §5.1/§5.2 so that intermediaries cannot cache
    the rejection envelope (caching a 4xx/429 OAuth response could mask
    transient configuration problems and confuse re-authentication attempts).
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )
