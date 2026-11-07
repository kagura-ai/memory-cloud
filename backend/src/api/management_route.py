"""Bound request bytes before JSON parsing; management bodies never enter logs."""

from collections.abc import Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from utils.exceptions import ManagementRequestTooLargeError


class BoundedManagementRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 1048576:
                    raise ManagementRequestTooLargeError()
            request._body = bytes(data)
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return bounded
