"""Checks that run on the private port before any route sees the request.

FastAPI reads and parses a request body before it resolves dependencies, so an auth
dependency alone would let anyone without a key make the server buffer a large body.
AccessGuard rejects those requests while the body is still unread:

- /api and /mcp need a valid Bearer key; /admin needs a panel session, except the login
  page and static files. The principal found here is stored in the request state so the
  routes do not authenticate twice.
- When TRALHA_ADMIN_ALLOWED_IPS is set, /admin answers 404 to any other client.
- An IP that sends MAX_FAILURES bad credentials within FAILURE_WINDOW_SECONDS is refused
  with 429 for LOCKOUT_SECONDS. Failed logins and unknown upload tokens count too. The
  counters live in memory, which fits the single process tralha runs as.
"""

import ipaddress
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from tralha import admin
from tralha.auth import authenticate, bearer_key

MAX_FAILURES = 20
FAILURE_WINDOW_SECONDS = 300
LOCKOUT_SECONDS = 900

PANEL_OPEN_PATHS = ("/admin/login", "/admin/static/")
FAILURE_STATUSES = {"/admin/login": {401}, "/upload/": {403, 404}}


class FailedAuthLimiter:
    def __init__(self):
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def seconds_locked(self, ip: str) -> int:
        until = self._locked_until.get(ip, 0)
        if until <= time.monotonic():
            self._locked_until.pop(ip, None)
            return 0
        return int(until - time.monotonic()) + 1

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        failures = self._failures[ip]
        failures.append(now)
        while failures and failures[0] < now - FAILURE_WINDOW_SECONDS:
            failures.popleft()
        if len(failures) >= MAX_FAILURES:
            self._locked_until[ip] = now + LOCKOUT_SECONDS
            del self._failures[ip]


def parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [
        ipaddress.ip_network(item.strip(), strict=False) for item in raw.split(",") if item.strip()
    ]


def _in_networks(ip: str, networks) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


class AccessGuard:
    def __init__(self, app, admin_networks, open_paths: tuple[str, ...] = ()):
        self.app = app
        self.admin_networks = admin_networks
        self.open_paths = open_paths
        self.limiter = FailedAuthLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        client_ip = scope["client"][0] if scope.get("client") else ""
        is_panel = path == "/admin" or path.startswith("/admin/")

        if is_panel and self.admin_networks and not _in_networks(client_ip, self.admin_networks):
            return await Response(status_code=404)(scope, receive, send)

        if retry_after := self.limiter.seconds_locked(client_ip):
            response = JSONResponse(
                {"detail": "too many failed attempts, try again later"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            return await response(scope, receive, send)

        rejection = await self._authenticate(scope, path, client_ip, is_panel)
        if rejection is not None:
            return await rejection(scope, receive, send)

        failure_statuses = next(
            (codes for prefix, codes in FAILURE_STATUSES.items() if path.startswith(prefix)),
            None,
        )
        if failure_statuses is None:
            return await self.app(scope, receive, send)

        async def count_failures(message):
            if message["type"] == "http.response.start" and message["status"] in failure_statuses:
                self.limiter.record_failure(client_ip)
            await send(message)

        await self.app(scope, receive, count_failures)

    async def _authenticate(self, scope, path: str, client_ip: str, is_panel: bool):
        if path.startswith(self.open_paths) or path.startswith(PANEL_OPEN_PATHS):
            return None
        headers = dict(scope["headers"])

        if path.startswith(("/api/", "/mcp")):
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            principal = await run_in_threadpool(authenticate, authorization, client_ip)
            if principal is None:
                if bearer_key(authorization):
                    self.limiter.record_failure(client_ip)
                return JSONResponse(
                    {"detail": "missing or invalid API key"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            scope.setdefault("state", {})["principal"] = principal
            return None

        if is_panel:
            cookies = SimpleCookie(headers.get(b"cookie", b"").decode("latin-1"))
            token = cookies[admin.COOKIE].value if admin.COOKIE in cookies else None
            principal = await run_in_threadpool(admin.session_principal, token)
            if principal is None:
                if token:
                    self.limiter.record_failure(client_ip)
                if scope["method"] == "GET":
                    return RedirectResponse("/admin/login", status_code=303)
                return JSONResponse({"detail": "sign in again"}, status_code=401)
            scope.setdefault("state", {})["principal"] = principal
        return None
