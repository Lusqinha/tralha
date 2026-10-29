"""REST API authenticated with a Bearer key. Keep it on the private network.

Token uploads check the token before reading the body, so nobody without permission can
push 25 MB at the server.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tralha import service, storage, system
from tralha.auth import Principal, authenticate
from tralha.public import PRIVATE_NO_CACHE, serve, serve_original

router = APIRouter()


def principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    client_ip = request.client.host if request.client else None
    p = getattr(request.state, "principal", None) or authenticate(authorization, client_ip)
    if p is None:
        raise HTTPException(
            401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"}
        )
    return p


Auth = Annotated[Principal, Depends(principal)]


class BucketIn(BaseModel):
    name: str
    visibility: str = "private"
    expire_after: int | None = Field(None, description="seconds; creates a cleanup rule")
    max_bytes: int | None = None
    on_full: str = "reject"
    compression: str | None = Field(None, description="empty inherits the system setting")


class BucketPatch(BaseModel):
    visibility: str | None = None
    max_bytes: int | None = None
    on_full: str | None = None
    compression: str | None = None


class RuleIn(BaseModel):
    expire_after: int = Field(description="segundos")
    prefix: str = ""
    basis: str = "created"
    keep_shared: bool = False


class ShareIn(BaseModel):
    key: str
    expires_in: int | None = Field(None, description="seconds; empty means until revoked")


class UploadIn(BaseModel):
    key: str
    ttl: int | None = None


async def _read_body(request: Request) -> storage.BlobWriter:
    writer = storage.BlobWriter()
    try:
        async for chunk in request.stream():
            writer.write(chunk)
        writer.finish()
    except storage.TooLarge as e:
        raise HTTPException(413, str(e)) from e
    except BaseException:
        writer.abort()
        raise
    return writer


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/api/buckets")
def list_buckets(p: Auth):
    return service.list_buckets(p)


@router.post("/api/buckets", status_code=201)
def create_bucket(p: Auth, body: BucketIn):
    return service.create_bucket(p, **body.model_dump())


@router.patch("/api/buckets/{bucket}")
def update_bucket(p: Auth, bucket: str, body: BucketPatch):
    return service.update_bucket(p, bucket, **body.model_dump(exclude_unset=True))


@router.delete("/api/buckets/{bucket}", status_code=204)
def delete_bucket(p: Auth, bucket: str):
    service.delete_bucket(p, bucket)


@router.post("/api/buckets/{bucket}/rules", status_code=201)
def add_rule(p: Auth, bucket: str, body: RuleIn):
    return service.add_rule(p, bucket, **body.model_dump())


@router.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(p: Auth, rule_id: int):
    service.delete_rule(p, rule_id)


@router.get("/api/settings")
def get_settings(p: Auth):
    service.require_admin(p)
    return system.get_all()


@router.patch("/api/settings")
def update_settings(p: Auth, body: dict[str, Any]):
    service.require_admin(p)
    try:
        return system.update(body)
    except system.InvalidSetting as e:
        raise service.Invalid(str(e)) from e


@router.get("/api/cleanup")
def preview_cleanup(p: Auth):
    service.require_admin(p)
    return service.plan_cleanup()


@router.get("/api/buckets/{bucket}/objects")
def list_objects(
    p: Auth,
    bucket: str,
    prefix: str = "",
    delimiter: str | None = "/",
    limit: int = Query(200, le=1000),
):
    return service.list_objects(p, bucket, prefix, delimiter or None, limit)


@router.put("/api/buckets/{bucket}/objects/{key:path}", status_code=201)
async def put_object(
    p: Auth,
    bucket: str,
    key: str,
    request: Request,
    ttl: int | None = None,
    x_tralha_metadata: Annotated[str | None, Header()] = None,
):
    if not p.can(bucket, "write"):
        raise service.Forbidden(f"missing 'write' permission on bucket '{bucket}'")
    try:
        metadata = json.loads(x_tralha_metadata) if x_tralha_metadata else None
    except json.JSONDecodeError as e:
        raise service.Invalid("X-Tralha-Metadata must be JSON") from e
    writer = await _read_body(request)
    obj = await run_in_threadpool(
        service.store_object, p, bucket, key, writer, metadata=metadata, ttl=ttl
    )
    return obj.to_dict()


@router.get("/api/buckets/{bucket}/objects/{key:path}")
def get_object(p: Auth, bucket: str, key: str, request: Request, original: bool = False):
    obj = service.get_object(p, bucket, key)
    if original:
        return serve_original(obj, request)
    return serve(obj, request, PRIVATE_NO_CACHE)


@router.delete("/api/buckets/{bucket}/objects/{key:path}", status_code=204)
def delete_object(p: Auth, bucket: str, key: str):
    service.delete_object(p, bucket, key)


@router.post("/api/buckets/{bucket}/shares", status_code=201)
def create_share(p: Auth, bucket: str, body: ShareIn):
    return service.create_share(p, bucket, body.key, body.expires_in)


@router.delete("/api/shares/{token}", status_code=204)
def revoke_share(p: Auth, token: str):
    service.revoke_share(p, token)


@router.post("/api/buckets/{bucket}/uploads", status_code=201)
def create_upload(p: Auth, bucket: str, body: UploadIn):
    return service.create_upload(p, bucket, body.key, body.ttl)


@router.get("/api/search")
def search(p: Auth, q: str, bucket: str | None = None, limit: int = Query(20, le=100)):
    return service.search(p, q, bucket, limit)


@router.put("/upload/{token}", status_code=201)
async def upload_with_token(token: str, request: Request):
    await run_in_threadpool(service.check_upload, token)
    writer = await _read_body(request)
    obj = await run_in_threadpool(service.consume_upload, token, writer)
    return obj.to_dict()
