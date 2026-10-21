"""Epoch validation for Observation GETs after serialization and at ASGI send admission.

No authorization lock spans a network await. A send admitted before revocation
may still complete afterwards; already admitted bytes cannot be retracted.
"""

from typing import Any, Callable, Coroutine
import asyncio

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.decision_observation.read_authorization import ReadAuthorization, ReadAuthorizationExpired


class AuthorizedReadResponse(Response):
    def __init__(self, response: ASGIApp, authorization: ReadAuthorization, denial: JSONResponse) -> None:
        self.response, self.authorization, self.denial = response, authorization, denial

    async def verify_for_send(self) -> None:
        # Even a contended control-plane lock must not block the WebUI loop.
        await asyncio.to_thread(self.authorization.verify)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False
        protected = True

        async def guarded_send(message: Message) -> None:
            nonlocal started, protected
            if message["type"] == "http.response.start" and message["status"] >= 400:
                protected = False  # Preserve existing authentication/validation error semantics.
            if protected and message["type"] in {"http.response.start", "http.response.body"}:
                await self.verify_for_send()
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.response(scope, receive, guarded_send)
        except ReadAuthorizationExpired:
            if started:
                # Status/Content-Length are already committed. Abort rather than send
                # stale data or an error body under the successful response's headers.
                raise
            denial = scope.get("observation_read_denial", self.denial)
            await denial(scope, receive, send)


class ObservationReadMiddleware:
    """Production guard outside gzip; unrelated paths and annotation PUTs pass through."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        prefix = "/api/webui/decision-observation"
        if (
            scope["type"] != "http" or scope["method"] != "GET"
            or not (scope["path"] == prefix or scope["path"].startswith(prefix + "/"))
        ):
            await self.app(scope, receive, send)
            return
        from src.decision_observation.recorder import store

        authorization = await asyncio.to_thread(ReadAuthorization, store)
        scope["observation_read_authorization"] = authorization
        response = AuthorizedReadResponse(
            self.app, authorization, JSONResponse({"detail": "会话不在当前观察授权范围"}, status_code=403)
        )
        await response(scope, receive, send)


class ObservationReadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        if "GET" not in self.methods:
            return original  # Annotation writes retain their existing commit protocol.

        async def guarded(request: Request) -> Response:
            from src.decision_observation.recorder import store

            authorization = request.scope.get("observation_read_authorization")
            outer_guard = authorization is not None
            if authorization is None:
                # Also protect routers mounted independently of the production app.
                authorization = await asyncio.to_thread(ReadAuthorization, store)
            request.state.observation_read_authorization = authorization
            response = await original(request)  # Includes FastAPI response serialization.
            if self.name in {"current", "history", "annotation_targets"}:
                denial = JSONResponse({"detail": {"code": "annotation_authorization_changed"}}, status_code=409)
            elif self.name in {"detail", "context"}:
                denial = JSONResponse({"detail": "机会不存在或不在授权范围"}, status_code=404)
            else:
                denial = JSONResponse({"detail": "会话不在当前观察授权范围"}, status_code=403)
            request.scope["observation_read_denial"] = denial
            if outer_guard or response.status_code >= 400:
                return response  # One final sender, never duplicated inside gzip.
            return AuthorizedReadResponse(response, authorization, denial)

        return guarded
