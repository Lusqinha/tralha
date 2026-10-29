from starlette.responses import JSONResponse


class _BodyTooLarge(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app, max_bytes: int, overrides: dict[str, int] | None = None):
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    def _limit_for(self, path: str) -> int:
        return self.overrides.get(path, self.max_bytes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        limit = self._limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return await _too_large(scope, receive, send)

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal response_started
            response_started |= message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await _too_large(scope, receive, send)


async def _too_large(scope, receive, send):
    response = JSONResponse({"detail": "request body too large"}, status_code=413)
    await response(scope, receive, send)
