"""Web panel at /admin: files, buckets, cleanup rules and settings.

Login uses a *:admin API key, and the session lives in an HttpOnly, SameSite=Strict cookie.
Against CSRF, every POST is also refused when Origin points to another host.
Pages have no inline script: the JS comes from /admin/static and the CSP only allows 'self'.
"""

import hashlib
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool

from tralha import images, service, storage, system
from tralha.auth import Principal, authenticate, principal_for_key_id
from tralha.config import get_settings
from tralha.db import admin_sessions, get_engine, now
from tralha.public import PRIVATE_NO_CACHE, sanitized_markdown_html, serve, serve_original

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"
THUMB_CACHE_FOREVER = "private, max-age=31536000, immutable"

COOKIE = "tralha_admin"
SESSION_TTL = 7 * 86400
PREVIEW_TEXT_MAX = 200_000
IMAGE_TYPES = images.DECODABLE_TYPES


def _bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _date(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _duration_human(seconds: int) -> str:
    for singular, plural, size in (("day", "days", 86400), ("hour", "hours", 3600)):
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {singular if n == 1 else plural}"
    return f"{seconds // 60} min"


templates.env.filters.update(
    bytes=_bytes, date=_date, duration=_duration, duration_human=_duration_human, urlpath=quote
)


class LoginRequired(Exception):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_principal(token: str | None) -> Principal | None:
    if not token:
        return None
    with get_engine().connect() as conn:
        key_id = conn.execute(
            select(admin_sessions.c.key_id).where(
                admin_sessions.c.token_hash == _hash(token),
                admin_sessions.c.expires_at > now(),
            )
        ).scalar()
    principal = principal_for_key_id(key_id) if key_id else None
    return principal if principal and principal.is_admin else None


def admin(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None) or session_principal(
        request.cookies.get(COOKIE)
    )
    if principal is None:
        raise LoginRequired
    return principal


def reject_cross_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "invalid origin")


Admin = Annotated[Principal, Depends(admin)]
Post = Depends(reject_cross_origin)


def _panel_csp() -> str:
    public_origin = "{0.scheme}://{0.netloc}".format(urlsplit(get_settings().public_url))
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: {public_origin}; media-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )


def page(
    request: Request,
    template: str,
    principal: Principal | None = None,
    section: str = "files",
    status_code: int = 200,
    **context,
) -> Response:
    context.setdefault("current_bucket", context.get("bucket"))
    response = templates.TemplateResponse(
        request,
        template,
        {
            "flash": request.query_params.get("msg"),
            "nav_buckets": service.list_buckets(principal) if principal else [],
            "section": section,
            **context,
        },
        status_code=status_code,
    )
    response.headers["Content-Security-Policy"] = _panel_csp()
    response.headers["Cache-Control"] = "no-store"
    return response


def back(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg})
    return RedirectResponse(url, status_code=303)


def files_url(bucket: str, prefix: str = "") -> str:
    url = f"/admin/files/{bucket}"
    return url + "?" + urlencode({"prefix": prefix}) if prefix else url


def object_url(bucket: str, key: str) -> str:
    return f"/admin/object/{bucket}/{quote(key)}"


templates.env.globals.update(files_url=files_url, object_url=object_url)


@router.get("/login")
def login_form(request: Request):
    return page(request, "login.html")


@router.post("/login", dependencies=[Post])
def login(request: Request, key: Annotated[str, Form()]):
    client_ip = request.client.host if request.client else None
    principal = authenticate(f"Bearer {key.strip()}", client_ip)
    if principal is None or not principal.is_admin:
        return page(
            request,
            "login.html",
            status_code=401,
            error="Invalid key, or the key lacks the '*:admin' scope.",
        )
    token = secrets.token_urlsafe(32)
    with get_engine().begin() as conn:
        conn.execute(delete(admin_sessions).where(admin_sessions.c.expires_at <= now()))
        conn.execute(
            admin_sessions.insert().values(
                token_hash=_hash(token), key_id=principal.key_id, expires_at=now() + SESSION_TTL
            )
        )
    response = RedirectResponse("/admin/files", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=get_settings().secure_cookies or request.url.scheme == "https",
        path="/admin",
    )
    return response


@router.post("/logout", dependencies=[Post])
def logout(request: Request):
    if token := request.cookies.get(COOKIE):
        with get_engine().begin() as conn:
            conn.execute(delete(admin_sessions).where(admin_sessions.c.token_hash == _hash(token)))
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE, path="/admin")
    return response


@router.get("")
@router.get("/")
def home(_: Admin):
    return RedirectResponse("/admin/files", status_code=303)


def _crumbs(prefix: str) -> list[tuple[str, str]]:
    crumbs, acc = [], ""
    for part in prefix.strip("/").split("/") if prefix else []:
        acc += part + "/"
        crumbs.append((part, acc))
    return crumbs


@router.get("/files")
def files_home(request: Request, p: Admin):
    return page(request, "files.html", p, bucket=None)


@router.get("/files/{bucket}")
def files(request: Request, p: Admin, bucket: str, prefix: str = ""):
    listing = service.list_objects(p, bucket, prefix, "/", limit=500)
    crumbs = _crumbs(prefix)
    return page(
        request,
        "files.html",
        p,
        bucket=bucket,
        prefix=prefix,
        crumbs=crumbs,
        listing=listing,
        image_types=IMAGE_TYPES,
    )


@router.get("/object/{bucket}/{key:path}")
def object_detail(request: Request, p: Admin, bucket: str, key: str):
    obj = service.get_object(p, bucket, key, touch=False)
    text = markdown = None
    if obj.content_type.startswith("text/") or obj.content_type == "application/json":
        content = obj.path.read_text("utf-8", errors="replace")
        truncated = len(content) > PREVIEW_TEXT_MAX
        content = content[:PREVIEW_TEXT_MAX]
        if obj.content_type == "text/markdown":
            markdown = sanitized_markdown_html(content)
        text = {"content": content, "truncated": truncated}
    parent = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
    return page(
        request,
        "object.html",
        p,
        crumbs=_crumbs(parent),
        bucket=bucket,
        key=key,
        obj=obj,
        name=key.rsplit("/", 1)[-1],
        parent=parent,
        shares=service.list_shares(p, bucket, key),
        text=text,
        markdown=markdown,
        image_types=IMAGE_TYPES,
    )


@router.get("/raw/{bucket}/{key:path}")
def raw(request: Request, p: Admin, bucket: str, key: str, original: bool = False):
    obj = service.get_object(p, bucket, key, touch=False)
    if original:
        return serve_original(obj, request)
    return serve(obj, request, PRIVATE_NO_CACHE)


@router.get("/thumb/{bucket}/{key:path}")
def thumb(p: Admin, bucket: str, key: str):
    obj = service.get_object(p, bucket, key, touch=False)
    if obj.content_type not in images.DECODABLE_TYPES:
        raise service.NotFound("no thumbnail for this type")
    dest = storage.thumb_path(obj.sha256)
    if not dest.exists():
        try:
            images.thumbnail(obj.path, dest)
        except (images.ImageRejected, images.ImageJobFailed) as e:
            raise service.NotFound("thumbnail unavailable") from e
    return FileResponse(
        dest, media_type="image/webp", headers={"Cache-Control": THUMB_CACHE_FOREVER}
    )


@router.post("/actions/share", dependencies=[Post])
def action_share(
    p: Admin,
    bucket: Annotated[str, Form()],
    key: Annotated[str, Form()],
    expires_in: Annotated[int, Form()] = 0,
):
    service.create_share(p, bucket, key, expires_in or None)
    return back(object_url(bucket, key), "Link created.")


@router.post("/actions/revoke", dependencies=[Post])
def action_revoke(
    p: Admin,
    token: Annotated[str, Form()],
    bucket: Annotated[str, Form()],
    key: Annotated[str, Form()],
):
    service.revoke_share(p, token)
    return back(object_url(bucket, key), "Link revoked.")


def _basename(filename: str | None) -> str:
    return (filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def _upload_prefix(prefix: str, folder: str) -> str:
    folder = folder.strip().strip("/")
    return prefix + (folder + "/" if folder else "")


async def _store_upload(p: Principal, bucket: str, key: str, upload: UploadFile):
    writer = storage.BlobWriter()
    try:
        while chunk := await upload.read(1024 * 1024):
            writer.write(chunk)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    return await run_in_threadpool(service.store_object, p, bucket, key, writer)


@router.post("/actions/upload", dependencies=[Post])
async def action_upload(
    p: Admin,
    bucket: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    prefix: Annotated[str, Form()] = "",
    folder: Annotated[str, Form()] = "",
):
    target = _upload_prefix(prefix, folder)
    sent, errors = 0, []
    for upload in files:
        name = _basename(upload.filename)
        if not name:
            continue
        try:
            await _store_upload(p, bucket, target + name, upload)
            sent += 1
        except (service.ServiceError, storage.TooLarge) as e:
            errors.append(f"{name}: {e}")
        finally:
            await upload.close()

    msg = f"Uploaded {sent} file(s)."
    if errors:
        msg = f"Error: {len(errors)} file(s) failed (" + "; ".join(errors[:3]) + f"). {msg}"
    return back(files_url(bucket, target), msg)


@router.post("/actions/delete", dependencies=[Post])
def action_delete(p: Admin, bucket: Annotated[str, Form()], key: Annotated[str, Form()]):
    service.delete_object(p, bucket, key)
    parent = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
    return back(files_url(bucket, parent), f"Deleted '{key}'.")


def _optional_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


@router.get("/buckets")
def buckets_page(request: Request, p: Admin):
    return page(
        request,
        "buckets.html",
        p,
        section="buckets",
        buckets=service.list_buckets(p),
        modes=system.COMPRESSION_MODES,
        system_compression=system.get_all()["compression"],
    )


@router.post("/buckets", dependencies=[Post])
def bucket_create(
    p: Admin,
    name: Annotated[str, Form()],
    visibility: Annotated[str, Form()] = "private",
):
    service.create_bucket(p, name.strip(), visibility)
    return back("/admin/buckets", f"Created bucket '{name}'.")


@router.post("/buckets/{name}", dependencies=[Post])
def bucket_update(
    p: Admin,
    name: str,
    visibility: Annotated[str, Form()],
    compression: Annotated[str, Form()],
    on_full: Annotated[str, Form()],
    max_mb: Annotated[str | None, Form()] = None,
):
    max_mb_value = _optional_int(max_mb)
    service.update_bucket(
        p,
        name,
        visibility=visibility,
        compression=compression or None,
        on_full=on_full,
        max_bytes=max_mb_value * 1024 * 1024 if max_mb_value else None,
    )
    return back("/admin/buckets", f"Saved bucket '{name}'.")


@router.post("/buckets/{name}/rules", dependencies=[Post])
def rule_add(
    p: Admin,
    name: str,
    amount: Annotated[int, Form()],
    unit: Annotated[int, Form()],
    prefix: Annotated[str, Form()] = "",
    basis: Annotated[str, Form()] = "created",
    keep_shared: Annotated[bool, Form()] = False,
):
    service.add_rule(p, name, amount * unit, prefix.strip(), basis, keep_shared)
    return back("/admin/buckets", "Rule added.")


@router.post("/rules/{rule_id}/delete", dependencies=[Post])
def rule_delete(p: Admin, rule_id: int):
    service.delete_rule(p, rule_id)
    return back("/admin/buckets", "Rule removed.")


@router.get("/settings")
def settings_page(request: Request, p: Admin):
    cleanup = service.plan_cleanup()
    return page(
        request,
        "settings.html",
        p,
        section="settings",
        settings=system.get_all(),
        modes=system.COMPRESSION_MODES,
        cleanup=cleanup,
        cleanup_bytes=sum(v["size"] for v in cleanup),
    )


@router.post("/settings", dependencies=[Post])
def settings_update(
    p: Admin,
    compression: Annotated[str, Form()],
    compression_quality: Annotated[int, Form()],
    keep_original: Annotated[bool, Form()] = False,
):
    service.require_admin(p)
    try:
        system.update(
            {
                "compression": compression,
                "compression_quality": compression_quality,
                "keep_original": keep_original,
            }
        )
    except system.InvalidSetting as e:
        return back("/admin/settings", f"Error: {e}")
    return back("/admin/settings", "Compression saved. It applies to the next uploads.")


@router.post("/cleanup", dependencies=[Post])
def cleanup_run(p: Admin):
    service.require_admin(p)
    result = service.run_gc()
    return back("/admin/settings", f"Cleanup done: deleted {result['deleted_objects']} object(s).")
