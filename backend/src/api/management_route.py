"""Bound request bytes before JSON parsing; management bodies never enter logs."""

from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute


class BoundedManagementRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 1048576:
                    raise HTTPException(413, "Management request exceeds limit")
            request._body = bytes(data)
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return bounded
