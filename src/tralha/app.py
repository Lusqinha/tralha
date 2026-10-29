"""The two tralha HTTP apps, each on its own port.

create_public_app only knows /p, /b and /healthz, and it is the only one that should face
the internet. create_app has everything (API, MCP, uploads, the panel and the links too)
and stays on the private network. The split uses ports because anyone outside can forge
the Host header.

In the private app the MCP app is mounted at "/" last and answers /mcp; routes declared
before it take precedence.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.staticfiles import StaticFiles

from tralha import admin, api, public, service
from tralha.config import Settings, get_settings
from tralha.db import init_db
from tralha.limits import BodySizeLimit
from tralha.mcp_server import BearerMiddleware, mcp

log = logging.getLogger("tralha")


async def _gc_loop(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            result = await run_in_threadpool(service.run_gc)
            log.info("gc: %s", result)
        except Exception:
            log.exception("gc failed")


def _prepare_storage() -> Settings:
    settings = get_settings()
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    return settings


def _service_errors_as_json(app: FastAPI) -> None:
    @app.exception_handler(service.ServiceError)
    async def _service_error(_: Request, exc: service.ServiceError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)


def create_public_app() -> FastAPI:
    _prepare_storage()
    app = FastAPI(title="tralha", docs_url=None, redoc_url=None, openapi_url=None)
    _service_errors_as_json(app)
    app.add_middleware(BodySizeLimit, max_bytes=0)
    app.include_router(public.router)
    app.add_api_route("/healthz", api.healthz)
    return app


def create_app() -> FastAPI:
    settings = _prepare_storage()

    mcp_app = mcp.http_app(path="/mcp", middleware=[Middleware(BearerMiddleware)])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):
            task = asyncio.create_task(_gc_loop(settings.gc_interval))
            yield
            task.cancel()

    app = FastAPI(
        title="tralha",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(service.ServiceError)
    async def _service_error(request: Request, exc: service.ServiceError):
        if request.url.path.startswith("/admin") and request.method == "POST":
            referer = request.headers.get("referer", "")
            target = referer if referer.startswith(str(request.base_url)) else "/admin/files"
            return admin.back(target.split("?msg=")[0].split("&msg=")[0], f"Error: {exc}")
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(admin.LoginRequired)
    async def _login_required(_: Request, __: admin.LoginRequired):
        return RedirectResponse("/admin/login", status_code=303)

    app.add_middleware(
        BodySizeLimit,
        max_bytes=settings.max_request_bytes,
        overrides={"/admin/actions/upload": settings.max_panel_upload_bytes},
    )
    app.include_router(public.router)
    app.include_router(api.router)
    app.include_router(admin.router)
    app.mount("/admin/static", StaticFiles(directory=admin.STATIC_DIR), name="admin-static")
    app.mount("/", mcp_app)
    return app
