"""MCP server at /mcp.

AccessGuard answers 401 before a request reaches the protocol. Each tool still
authenticates again, because it runs in another task and cannot see the request state.
Tool docstrings are the description the model reads to decide what to call, so changing
the wording changes its behavior.
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image
from pydantic import Field

from tralha import images, service, storage
from tralha.auth import Principal, authenticate

INSTRUCTIONS = """\
Tralha is self-hosted storage for AI artifacts: screenshots, images, reports and notes.

- Text and markdown: use put_text.
- Binary files (images, PDFs, videos): do NOT send the content through MCP. Call create_upload,
  run the returned curl command with the local file path, then call share.
- To attach an image to a PR or issue, use the `markdown` field returned by share.
- Use buckets with cleanup rules (e.g. scratch) for throwaway material, and search_docs to find
  old notes.
"""

TEXT_MAX_CHARS = 200_000

mcp = FastMCP("tralha", instructions=INSTRUCTIONS, mask_error_details=True)


def _principal() -> Principal:
    request = get_http_request()
    client_ip = request.client.host if request.client else None
    p = authenticate(request.headers.get("authorization"), client_ip)
    if p is None:
        raise ToolError("missing or invalid API key")
    return p


def _call(fn, *args, **kwargs):
    try:
        return fn(_principal(), *args, **kwargs)
    except (service.ServiceError, storage.TooLarge) as e:
        raise ToolError(str(e)) from e


Bucket = Annotated[str, Field(description="Bucket name")]
Key = Annotated[str, Field(description="Object key, e.g. pr-123/login/before.png")]
Seconds = Annotated[
    int | None,
    Field(description="Seconds until it expires. Empty means only the bucket rules apply"),
]


@mcp.tool
def list_buckets() -> list[dict[str, Any]]:
    """List the buckets this key can read, with object counts and bytes used."""
    return _call(service.list_buckets)


@mcp.tool
def create_bucket(
    name: Bucket,
    visibility: Annotated[str, Field(description="private | public-read")] = "private",
    expire_after: Annotated[
        int | None, Field(description="Seconds. Deletes objects older than this")
    ] = None,
) -> dict[str, Any]:
    """Create a bucket. Requires the admin scope."""
    return _call(service.create_bucket, name, visibility, expire_after)


@mcp.tool
def list_objects(
    bucket: Bucket,
    prefix: Annotated[str, Field(description="Filter keys by prefix, e.g. pr-123/")] = "",
    recursive: Annotated[
        bool,
        Field(description="True lists everything under the prefix without grouping by folder"),
    ] = False,
) -> dict[str, Any]:
    """List objects in a bucket. 'prefixes' are the virtual subfolders."""
    return _call(service.list_objects, bucket, prefix, None if recursive else "/")


@mcp.tool
def put_text(
    bucket: Bucket,
    key: Key,
    content: Annotated[str, Field(description="Text content (markdown, JSON, log, CSV...)")],
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Free-form metadata, e.g. {'repo': 'x', 'pr': 12}"),
    ] = None,
    ttl: Seconds = None,
) -> dict[str, Any]:
    """Store a text document. .md files are indexed for search and rendered on the public link."""
    obj = _call(
        service.put_bytes,
        bucket,
        key,
        content.encode("utf-8"),
        metadata=metadata,
        ttl=ttl,
    )
    return obj.to_dict()


@mcp.tool
def create_upload(bucket: Bucket, key: Key, ttl: Seconds = None) -> dict[str, Any]:
    """Create a single-use upload URL for binary files.

    Send the file with the command in `curl`, replacing <file> with the local path.
    The URL expires in a few minutes."""
    return _call(service.create_upload, bucket, key, ttl)


@mcp.tool
def get_object(bucket: Bucket, key: Key) -> Any:
    """Read an object. Text comes back as text; images come back as a (downscaled) image you can see."""
    obj = _call(service.get_object, bucket, key)
    info = obj.to_dict()
    if obj.content_type in images.DECODABLE_TYPES:
        try:
            return [info, Image(data=images.model_preview_png(obj.path), format="png")]
        except (images.ImageRejected, images.ImageJobFailed):
            return [info, "Could not build a preview. Use share to look at it."]
    if obj.content_type.startswith("text/") or obj.content_type == "application/json":
        content = obj.path.read_text("utf-8", errors="replace")
        if len(content) > TEXT_MAX_CHARS:
            content = (
                content[:TEXT_MAX_CHARS] + f"\n\n[... truncated at {TEXT_MAX_CHARS} characters]"
            )
        return [info, content]
    return [info, "Binary content. Use share to get a link."]


@mcp.tool
def share(
    bucket: Bucket,
    key: Key,
    expires_in: Annotated[
        int | None,
        Field(description="Seconds. Empty means it lasts until revoked (use this for PRs)"),
    ] = None,
) -> dict[str, Any]:
    """Create a public link for the object. Returns `url` and `markdown` ready to paste into a PR or issue."""
    return _call(service.create_share, bucket, key, expires_in)


@mcp.tool
def revoke_share(
    token: Annotated[str, Field(description="Link token (the last part of the /p/<token> URL)")],
) -> str:
    """Revoke a public link."""
    _call(service.revoke_share, token)
    return "revoked"


@mcp.tool
def delete_object(bucket: Bucket, key: Key) -> str:
    """Delete an object and all of its public links."""
    _call(service.delete_object, bucket, key)
    return "deleted"


@mcp.tool
def search_docs(
    query: Annotated[str, Field(description="Search terms")],
    bucket: Annotated[str | None, Field(description="Limit to one bucket")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """Full-text search over text documents (markdown, logs, JSON...)."""
    return _call(service.search, query, bucket, limit)
