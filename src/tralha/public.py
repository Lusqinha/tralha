"""Unauthenticated routes (/p and /b). They are the only ones that should face the internet.

Everything served here carries a sandbox CSP, so an uploaded SVG or HTML file cannot run
anything. The cache is short because a link can be revoked, and GitHub's image proxy
honors max-age.
"""

from dataclasses import replace
from html import escape
from urllib.parse import quote

import nh3
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from markdown_it import MarkdownIt

from tralha import service
from tralha.service import StoredObject

router = APIRouter()

_md = MarkdownIt("commonmark", {"linkify": False}).enable("table").enable("strikethrough")

INLINE_TYPES = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "application/pdf",
    "video/mp4",
    "video/webm",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
)

SANDBOX_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "media-src 'self'; frame-ancestors 'none'"
)
RENDERED_MARKDOWN_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src *; frame-ancestors 'none'"
)
PUBLIC_CACHE = "public, max-age=3600"
PRIVATE_NO_CACHE = "private, no-store"

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --fg: #1f2328; --bg: #fff; --muted: #59636e; --line: #d1d9e0; --code: #f6f8fa; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg: #e6edf3; --bg: #0d1117; --muted: #9198a1; --line: #3d444d; --code: #151b23; }} }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.6 system-ui, sans-serif; }}
main {{ max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }}
header {{ color: var(--muted); font-size: 13px; border-bottom: 1px solid var(--line); padding-bottom: 8px; margin-bottom: 24px; }}
pre, code {{ background: var(--code); border-radius: 6px; font: 13px/1.5 ui-monospace, monospace; }}
code {{ padding: 2px 5px; }} pre {{ padding: 12px 16px; overflow-x: auto; }} pre code {{ padding: 0; }}
img {{ max-width: 100%; }} table {{ border-collapse: collapse; display: block; overflow-x: auto; }}
th, td {{ border: 1px solid var(--line); padding: 6px 12px; }}
blockquote {{ margin: 0; padding-left: 16px; border-left: 4px solid var(--line); color: var(--muted); }}
a {{ color: #4493f8; }}
</style></head>
<body><main><header>{title} · <a href="?raw=1">raw</a></header>{body}</main></body></html>"""


def sanitized_markdown_html(source: str) -> str:
    return nh3.clean(_md.render(source))


def render_markdown(source: str, title: str) -> str:
    return _PAGE.format(title=escape(title), body=sanitized_markdown_html(source))


def serve(obj: StoredObject, request: Request, cache_control: str) -> Response:
    etag = f'"{obj.sha256}"'
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": SANDBOX_CSP,
        "Referrer-Policy": "no-referrer",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    filename = obj.key.rsplit("/", 1)[-1]
    if obj.content_type == "text/markdown" and "raw" not in request.query_params:
        html = render_markdown(obj.path.read_text("utf-8", errors="replace"), filename)
        headers["Content-Security-Policy"] = RENDERED_MARKDOWN_CSP
        return HTMLResponse(html, headers=headers)

    media_type = obj.content_type
    if media_type.startswith("text/") or media_type == "application/json":
        media_type += "; charset=utf-8"
    disposition = "inline" if obj.content_type in INLINE_TYPES else "attachment"
    headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
    return FileResponse(obj.path, media_type=media_type, headers=headers)


def serve_original(obj: StoredObject, request: Request) -> Response:
    if obj.original_path is None:
        raise service.NotFound("the original of this object was not kept")
    original = replace(obj, sha256=obj.original_sha256, content_type=obj.original_content_type)
    return serve(original, request, PRIVATE_NO_CACHE)


@router.get("/p/{token}")
def get_share(token: str, request: Request):
    return serve(service.resolve_share(token), request, PUBLIC_CACHE)


@router.get("/b/{bucket}/{key:path}")
def get_public_object(bucket: str, key: str, request: Request):
    return serve(service.get_object(None, bucket, key), request, PUBLIC_CACHE)
