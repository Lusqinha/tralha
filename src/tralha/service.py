"""Business rules shared by the REST API, the MCP server, the panel and the CLI.

Functions that receive principal=None are local calls (the CLI, or the public route of a
public-read bucket) and skip scope checks.

Guarantees the rest of the code relies on:
- get_object(touch=False) does not count as an access. The panel uses it so that looking
  at a file does not keep alive something a "no access for N days" rule should delete.
- When the quota fills up and the bucket allows eviction, the oldest files go first, but
  never a file with an active link, so images in PRs do not break.
- An upload token is marked as used before the write, so two concurrent uploads with the
  same token cannot both succeed.
- The original type and size are recorded whenever compression happens, even when the
  original file is not kept, so the panel can show how much was saved.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, delete, exists, func, or_, select, update

from tralha import db, images, storage, system
from tralha.auth import Principal, principal_for_key_id
from tralha.config import get_settings
from tralha.db import buckets, lifecycle_rules, now, objects, shares, upload_tokens

log = logging.getLogger("tralha")

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
INDEXABLE_MAX = 2 * 1024 * 1024
INDEXABLE_TYPES = ("text/", "application/json")
ON_FULL = ("reject", "evict-oldest")
RULE_BASIS = ("created", "last-access")
TOUCH_INTERVAL_SECONDS = 3600
SEARCH_OVERFETCH = 5


class ServiceError(Exception):
    status = 400


class NotFound(ServiceError):
    status = 404


class Forbidden(ServiceError):
    status = 403


class Conflict(ServiceError):
    status = 409


class Invalid(ServiceError):
    status = 400


@dataclass(frozen=True)
class StoredObject:
    id: int
    bucket: str
    key: str
    sha256: str
    size: int
    content_type: str
    metadata: dict[str, Any]
    expires_at: int | None
    created_at: int
    updated_at: int
    last_accessed_at: int | None = None
    original_sha256: str | None = None
    original_content_type: str | None = None
    original_size: int | None = None

    @property
    def path(self):
        return storage.blob_path(self.sha256)

    @property
    def original_path(self):
        return storage.blob_path(self.original_sha256) if self.original_sha256 else None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "bucket": self.bucket,
            "key": self.key,
            "size": self.size,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "metadata": self.metadata,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
        }
        if self.original_content_type:
            data["original"] = {
                "content_type": self.original_content_type,
                "size": self.original_size,
                "kept": self.original_sha256 is not None,
            }
        return data


def _object_from_row(row, bucket_name: str) -> StoredObject:
    return StoredObject(
        id=row.id,
        bucket=bucket_name,
        key=row.key,
        sha256=row.blob_sha256,
        size=row.size,
        content_type=row.content_type,
        metadata=json.loads(row.metadata) if row.metadata else {},
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_accessed_at=row.last_accessed_at,
        original_sha256=row.original_sha256,
        original_content_type=row.original_content_type,
        original_size=row.original_size,
    )


def validate_key(key: str) -> str:
    if not key or len(key) > 1024:
        raise Invalid("key is empty or longer than 1024 characters")
    if key.startswith("/") or any(ord(c) < 32 for c in key):
        raise Invalid("key cannot start with '/' or contain control characters")
    if any(part in (".", "..") for part in key.split("/")):
        raise Invalid("key cannot have '.' or '..' segments")
    return key


def _require(principal: Principal, bucket: str, perm: str) -> None:
    if not principal.can(bucket, perm):
        raise Forbidden(f"missing '{perm}' permission on bucket '{bucket}'")


def _bucket_row(conn, name: str):
    row = conn.execute(select(buckets).where(buckets.c.name == name)).first()
    if row is None:
        raise NotFound(f"bucket '{name}' does not exist")
    return row


def _alive(now_ts: int):
    return or_(objects.c.expires_at.is_(None), objects.c.expires_at > now_ts)


def _key_starts_with(prefix: str):
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return objects.c.key.like(escaped + "%", escape="\\")


def _has_active_share(now_ts: int):
    return exists().where(
        shares.c.object_id == objects.c.id,
        or_(shares.c.expires_at.is_(None), shares.c.expires_at > now_ts),
    )


def _stored_size():
    return objects.c.size + case(
        (objects.c.original_sha256.is_not(None), objects.c.original_size), else_=0
    )


def _touch(conn, object_id: int) -> None:
    ts = now()
    conn.execute(
        update(objects)
        .where(
            objects.c.id == object_id,
            or_(
                objects.c.last_accessed_at.is_(None),
                objects.c.last_accessed_at < ts - TOUCH_INTERVAL_SECONDS,
            ),
        )
        .values(last_accessed_at=ts)
    )


def require_admin(principal: Principal | None) -> None:
    if principal is not None and not principal.is_admin:
        raise Forbidden("this operation requires the '*:admin' scope")


def _validate_bucket_fields(fields: dict[str, Any]) -> dict[str, Any]:
    if fields.get("visibility", "private") not in ("private", "public-read"):
        raise Invalid("visibility must be 'private' or 'public-read'")
    if fields.get("compression") not in (None, *system.COMPRESSION_MODES):
        raise Invalid(
            f"compression must be empty (inherit) or {' | '.join(system.COMPRESSION_MODES)}"
        )
    if fields.get("on_full", "reject") not in ON_FULL:
        raise Invalid(f"on_full must be {' | '.join(ON_FULL)}")
    if fields.get("max_bytes") is not None and fields["max_bytes"] <= 0:
        raise Invalid("max_bytes must be positive")
    return fields


def create_bucket(
    principal: Principal | None,
    name: str,
    visibility: str = "private",
    expire_after: int | None = None,
    max_bytes: int | None = None,
    on_full: str = "reject",
    compression: str | None = None,
) -> dict[str, Any]:
    require_admin(principal)
    if not BUCKET_RE.match(name):
        raise Invalid("bucket name: 3-63 characters, [a-z0-9-], no hyphen at either end")
    fields = _validate_bucket_fields(
        {
            "visibility": visibility,
            "max_bytes": max_bytes,
            "on_full": on_full,
            "compression": compression,
        }
    )
    with db.get_engine().begin() as conn:
        if conn.execute(select(buckets.c.id).where(buckets.c.name == name)).first():
            raise Conflict(f"bucket '{name}' already exists")
        conn.execute(buckets.insert().values(name=name, created_at=now(), **fields))
    if expire_after:
        add_rule(principal, name, expire_after)
    return get_bucket(principal, name)


def update_bucket(principal: Principal | None, name: str, **fields: Any) -> dict[str, Any]:
    require_admin(principal)
    allowed = {"visibility", "max_bytes", "on_full", "compression"}
    if unknown := set(fields) - allowed:
        raise Invalid(f"campos desconhecidos: {', '.join(sorted(unknown))}")
    _validate_bucket_fields(fields)
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, name)
        if fields:
            conn.execute(update(buckets).where(buckets.c.id == b.id).values(**fields))
    return get_bucket(principal, name)


def _bucket_stats_query(ts: int):
    return (
        select(
            buckets,
            func.count(objects.c.id).label("objects"),
            func.coalesce(func.sum(_stored_size()), 0).label("bytes"),
        )
        .select_from(
            buckets.outerjoin(objects, and_(objects.c.bucket_id == buckets.c.id, _alive(ts)))
        )
        .group_by(buckets.c.id)
        .order_by(buckets.c.name)
    )


def _bucket_dict(row, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": row.name,
        "visibility": row.visibility,
        "compression": row.compression,
        "max_bytes": row.max_bytes,
        "on_full": row.on_full,
        "objects": row.objects,
        "bytes": int(row.bytes),
        "rules": rules,
    }


def list_buckets(principal: Principal | None) -> list[dict[str, Any]]:
    with db.get_engine().connect() as conn:
        rows = conn.execute(_bucket_stats_query(now())).all()
        rules = _rules_by_bucket(conn)
    return [
        _bucket_dict(r, rules.get(r.id, []))
        for r in rows
        if principal is None or principal.can(r.name, "read")
    ]


def get_bucket(principal: Principal | None, name: str) -> dict[str, Any]:
    if principal is not None:
        _require(principal, name, "read")
    with db.get_engine().connect() as conn:
        row = conn.execute(_bucket_stats_query(now()).where(buckets.c.name == name)).first()
        if row is None:
            raise NotFound(f"bucket '{name}' does not exist")
        rules = _rules_by_bucket(conn).get(row.id, [])
    return _bucket_dict(row, rules)


def _rules_by_bucket(conn) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(select(lifecycle_rules).order_by(lifecycle_rules.c.id)):
        result.setdefault(r.bucket_id, []).append(
            {
                "id": r.id,
                "prefix": r.prefix,
                "expire_after": r.expire_after,
                "basis": r.basis,
                "keep_shared": r.keep_shared,
            }
        )
    return result


def add_rule(
    principal: Principal | None,
    bucket: str,
    expire_after: int,
    prefix: str = "",
    basis: str = "created",
    keep_shared: bool = False,
) -> dict[str, Any]:
    require_admin(principal)
    if expire_after <= 0:
        raise Invalid("expire_after must be positive")
    if basis not in RULE_BASIS:
        raise Invalid(f"basis must be {' | '.join(RULE_BASIS)}")
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, bucket)
        rule_id = conn.execute(
            lifecycle_rules.insert().values(
                bucket_id=b.id,
                prefix=prefix,
                expire_after=expire_after,
                basis=basis,
                keep_shared=keep_shared,
                created_at=now(),
            )
        ).inserted_primary_key[0]
    return {
        "id": rule_id,
        "prefix": prefix,
        "expire_after": expire_after,
        "basis": basis,
        "keep_shared": keep_shared,
    }


def delete_rule(principal: Principal | None, rule_id: int) -> None:
    require_admin(principal)
    with db.get_engine().begin() as conn:
        n = conn.execute(delete(lifecycle_rules).where(lifecycle_rules.c.id == rule_id)).rowcount
    if not n:
        raise NotFound(f"rule {rule_id} does not exist")


def delete_bucket(principal: Principal | None, name: str) -> None:
    require_admin(principal)
    with db.get_engine().begin() as conn:
        row = _bucket_row(conn, name)
        if conn.execute(select(objects.c.id).where(objects.c.bucket_id == row.id).limit(1)).first():
            raise Conflict(f"bucket '{name}' is not empty")
        conn.execute(delete(buckets).where(buckets.c.id == row.id))


def list_objects(
    principal: Principal,
    bucket: str,
    prefix: str = "",
    delimiter: str | None = "/",
    limit: int = 200,
) -> dict[str, Any]:
    _require(principal, bucket, "read")
    ts = now()
    with db.get_engine().connect() as conn:
        b = _bucket_row(conn, bucket)
        rows = conn.execute(
            select(objects)
            .where(
                objects.c.bucket_id == b.id,
                _key_starts_with(prefix),
                _alive(ts),
            )
            .order_by(objects.c.key)
            .limit(5000)
        ).all()

    items, prefixes = [], []
    for row in rows:
        rest = row.key[len(prefix) :]
        if delimiter and delimiter in rest:
            common = prefix + rest.split(delimiter, 1)[0] + delimiter
            if common not in prefixes:
                prefixes.append(common)
            continue
        items.append(_object_from_row(row, bucket).to_dict())
    truncated = len(items) > limit or len(rows) == 5000
    return {
        "bucket": bucket,
        "prefix": prefix,
        "prefixes": prefixes,
        "objects": items[:limit],
        "truncated": truncated,
    }


def store_object(
    principal: Principal,
    bucket: str,
    key: str,
    writer: storage.BlobWriter,
    metadata: dict[str, Any] | None = None,
    ttl: int | None = None,
) -> StoredObject:
    try:
        _require(principal, bucket, "write")
        validate_key(key)
        return _store(bucket, key, writer, metadata, ttl)
    except BaseException:
        writer.abort()
        raise


def compression_policy(bucket_compression: str | None) -> images.Policy:
    settings = system.get_all()
    return images.Policy(
        mode=bucket_compression or settings["compression"],
        quality=settings["compression_quality"],
        keep_original=settings["keep_original"],
    )


def _maybe_compress(
    writer: storage.BlobWriter, content_type: str, policy: images.Policy
) -> tuple[storage.BlobWriter, str, storage.BlobWriter | None]:
    if not images.eligible(content_type, policy.mode):
        return writer, content_type, None
    try:
        data = images.to_webp_if_smaller(writer.tmp_path, content_type, policy)
    except Exception:
        log.warning("falha ao comprimir; mantendo o original", exc_info=True)
        return writer, content_type, None
    if data is None:
        return writer, content_type, None
    compressed = storage.write_bytes(data)
    if policy.keep_original:
        return compressed, "image/webp", writer
    writer.abort()
    return compressed, "image/webp", None


def _make_room(conn, b, needed: int, keep_id: int | None, ts: int) -> None:
    used = conn.execute(
        select(func.coalesce(func.sum(_stored_size()), 0)).where(
            objects.c.bucket_id == b.id, _alive(ts), objects.c.id != (keep_id or -1)
        )
    ).scalar_one()
    overflow = used + needed - b.max_bytes
    if overflow <= 0:
        return
    if b.on_full != "evict-oldest" or needed > b.max_bytes:
        raise Conflict(f"bucket '{b.name}' is over quota")

    victims, freed = [], 0
    candidates = conn.execute(
        select(objects.c.id, _stored_size().label("stored"))
        .where(
            objects.c.bucket_id == b.id,
            objects.c.id != (keep_id or -1),
            ~_has_active_share(ts),
        )
        .order_by(objects.c.updated_at)
    )
    for row in candidates:
        victims.append(row.id)
        freed += row.stored
        if freed >= overflow:
            break
    if freed < overflow:
        raise Conflict(f"bucket '{b.name}' is over quota (the remaining files have active links)")
    _delete_objects(conn, victims)


def _store(bucket: str, key: str, writer: storage.BlobWriter, metadata, ttl) -> StoredObject:
    content_type = storage.sniff(writer.head, key)
    with db.get_engine().connect() as conn:
        bucket_compression = _bucket_row(conn, bucket).compression

    original_content_type, original_size = content_type, writer.size
    writer, content_type, original = _maybe_compress(
        writer, content_type, compression_policy(bucket_compression)
    )
    compressed_from = (
        (original_content_type, original_size) if content_type != original_content_type else None
    )
    try:
        return _save_row(
            bucket, key, writer, content_type, metadata, ttl, original, compressed_from
        )
    except BaseException:
        if original is not None:
            original.abort()
        writer.abort()
        raise


def _save_row(
    bucket, key, writer, content_type, metadata, ttl, original, compressed_from
) -> StoredObject:
    ts = now()
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, bucket)
        existing = conn.execute(
            select(objects).where(objects.c.bucket_id == b.id, objects.c.key == key)
        ).first()

        if b.max_bytes is not None:
            needed = writer.size + (original.size if original else 0)
            _make_room(conn, b, needed, existing.id if existing else None, ts)

        values = {
            "blob_sha256": writer.commit(),
            "size": writer.size,
            "content_type": content_type,
            "metadata": json.dumps(metadata) if metadata else None,
            "original_sha256": original.commit() if original else None,
            "original_content_type": compressed_from[0] if compressed_from else None,
            "original_size": compressed_from[1] if compressed_from else None,
            "expires_at": ts + ttl if ttl else None,
            "updated_at": ts,
        }
        if existing:
            conn.execute(update(objects).where(objects.c.id == existing.id).values(**values))
            object_id = existing.id
        else:
            object_id = conn.execute(
                objects.insert().values(bucket_id=b.id, key=key, created_at=ts, **values)
            ).inserted_primary_key[0]

        if content_type.startswith(INDEXABLE_TYPES) and writer.size <= INDEXABLE_MAX:
            content = storage.blob_path(values["blob_sha256"]).read_text("utf-8", errors="replace")
            db.index_doc(conn, object_id, f"{key}\n{content}")
        else:
            db.unindex_doc(conn, object_id)

        row = conn.execute(select(objects).where(objects.c.id == object_id)).one()
    return _object_from_row(row, bucket)


def put_bytes(principal: Principal, bucket: str, key: str, data: bytes, **kwargs) -> StoredObject:
    _require(principal, bucket, "write")
    return store_object(principal, bucket, key, storage.write_bytes(data), **kwargs)


def get_object(
    principal: Principal | None, bucket: str, key: str, touch: bool = True
) -> StoredObject:
    if principal is not None:
        _require(principal, bucket, "read")
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, bucket)
        if principal is None and b.visibility != "public-read":
            raise NotFound("object not found")
        row = conn.execute(
            select(objects).where(objects.c.bucket_id == b.id, objects.c.key == key, _alive(now()))
        ).first()
        if row is not None and touch:
            _touch(conn, row.id)
    if row is None:
        raise NotFound(f"object '{key}' does not exist in '{bucket}'")
    return _object_from_row(row, bucket)


def delete_object(principal: Principal, bucket: str, key: str) -> None:
    _require(principal, bucket, "delete")
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, bucket)
        row = conn.execute(
            select(objects.c.id).where(objects.c.bucket_id == b.id, objects.c.key == key)
        ).first()
        if row is None:
            raise NotFound(f"object '{key}' does not exist in '{bucket}'")
        _delete_objects(conn, [row.id])


def _delete_objects(conn, ids: list[int]) -> None:
    for object_id in ids:
        db.unindex_doc(conn, object_id)
    conn.execute(delete(shares).where(shares.c.object_id.in_(ids)))
    conn.execute(delete(objects).where(objects.c.id.in_(ids)))


def share_url(token: str) -> str:
    return f"{get_settings().public_url}/p/{token}"


def create_share(
    principal: Principal, bucket: str, key: str, expires_in: int | None = None
) -> dict[str, Any]:
    _require(principal, bucket, "share")
    obj = get_object(principal, bucket, key, touch=False)
    token = secrets.token_urlsafe(16)
    ts = now()
    expires_at = ts + expires_in if expires_in else None
    with db.get_engine().begin() as conn:
        conn.execute(
            shares.insert().values(
                token=token, object_id=obj.id, expires_at=expires_at, created_at=ts
            )
        )
    url = share_url(token)
    name = key.rsplit("/", 1)[-1]
    markdown = f"![{name}]({url})" if obj.content_type.startswith("image/") else f"[{name}]({url})"
    return {"token": token, "url": url, "markdown": markdown, "expires_at": expires_at}


def list_shares(principal: Principal, bucket: str, key: str) -> list[dict[str, Any]]:
    _require(principal, bucket, "share")
    obj = get_object(principal, bucket, key, touch=False)
    ts = now()
    with db.get_engine().connect() as conn:
        rows = conn.execute(
            select(shares)
            .where(
                shares.c.object_id == obj.id,
                or_(shares.c.expires_at.is_(None), shares.c.expires_at > ts),
            )
            .order_by(shares.c.created_at)
        ).all()
    return [
        {
            "token": r.token,
            "url": share_url(r.token),
            "expires_at": r.expires_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def revoke_share(principal: Principal, token: str) -> None:
    with db.get_engine().begin() as conn:
        row = conn.execute(
            select(shares.c.token, buckets.c.name)
            .select_from(shares.join(objects).join(buckets))
            .where(shares.c.token == token)
        ).first()
        if row is None:
            raise NotFound("link not found")
        _require(principal, row.name, "share")
        conn.execute(delete(shares).where(shares.c.token == token))


def resolve_share(token: str) -> StoredObject:
    ts = now()
    with db.get_engine().begin() as conn:
        row = conn.execute(
            select(objects, buckets.c.name.label("bucket_name"))
            .select_from(shares.join(objects).join(buckets))
            .where(
                shares.c.token == token,
                or_(shares.c.expires_at.is_(None), shares.c.expires_at > ts),
                _alive(ts),
            )
        ).first()
        if row is not None:
            _touch(conn, row.id)
    if row is None:
        raise NotFound("invalid or expired link")
    return _object_from_row(row, row.bucket_name)


def create_upload(
    principal: Principal, bucket: str, key: str, ttl: int | None = None
) -> dict[str, Any]:
    _require(principal, bucket, "write")
    validate_key(key)
    settings = get_settings()
    token = secrets.token_urlsafe(24)
    expires_at = now() + settings.upload_ttl
    with db.get_engine().begin() as conn:
        b = _bucket_row(conn, bucket)
        conn.execute(
            upload_tokens.insert().values(
                token=token,
                bucket_id=b.id,
                key=key,
                api_key_id=principal.key_id,
                ttl=ttl,
                expires_at=expires_at,
            )
        )
    url = f"{settings.api_url}/upload/{token}"
    return {
        "url": url,
        "method": "PUT",
        "expires_at": expires_at,
        "curl": f"curl -sfT <file> {url}",
    }


def _pending_upload(conn, token: str, ts: int):
    row = conn.execute(
        select(upload_tokens, buckets.c.name.label("bucket_name"))
        .select_from(upload_tokens.join(buckets))
        .where(
            upload_tokens.c.token == token,
            upload_tokens.c.used_at.is_(None),
            upload_tokens.c.expires_at > ts,
        )
    ).first()
    if row is None:
        raise NotFound("upload token is invalid, expired or already used")
    creator = principal_for_key_id(row.api_key_id)
    if creator is None or not creator.can(row.bucket_name, "write"):
        raise Forbidden("the key that created this upload was revoked or lost access")
    return row


def check_upload(token: str) -> None:
    with db.get_engine().connect() as conn:
        _pending_upload(conn, token, now())


def consume_upload(token: str, writer: storage.BlobWriter) -> StoredObject:
    try:
        ts = now()
        with db.get_engine().begin() as conn:
            row = _pending_upload(conn, token, ts)
            claimed = conn.execute(
                update(upload_tokens)
                .where(upload_tokens.c.token == token, upload_tokens.c.used_at.is_(None))
                .values(used_at=ts)
            ).rowcount
            if claimed != 1:
                raise NotFound("upload token is invalid, expired or already used")
        return _store(row.bucket_name, row.key, writer, None, row.ttl)
    except BaseException:
        writer.abort()
        raise


def search(
    principal: Principal, query: str, bucket: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if bucket:
        _require(principal, bucket, "read")
    ts = now()
    with db.get_engine().connect() as conn:
        hits = db.search_docs(conn, query, limit * SEARCH_OVERFETCH)
        if not hits:
            return []
        snippets = dict(hits)
        rows = conn.execute(
            select(objects, buckets.c.name.label("bucket_name"))
            .select_from(objects.join(buckets))
            .where(objects.c.id.in_(list(snippets)), _alive(ts))
        ).all()
    by_id = {r.id: r for r in rows}
    results = []
    for object_id, snippet in hits:
        row = by_id.get(object_id)
        if (
            row is None
            or (bucket and row.bucket_name != bucket)
            or not principal.can(row.bucket_name, "read")
        ):
            continue
        results.append({**_object_from_row(row, row.bucket_name).to_dict(), "snippet": snippet})
        if len(results) == limit:
            break
    return results


def _rule_reason(rule) -> str:
    days = rule.expire_after / 86400
    age = f"{days:g} day(s)" if days >= 1 else f"{rule.expire_after // 3600} hour(s)"
    where = f" in {rule.prefix}" if rule.prefix else ""
    if rule.basis == "last-access":
        return f"Not accessed for over {age}{where}"
    return f"Uploaded over {age} ago{where}"


def plan_cleanup(ts: int | None = None) -> list[dict[str, Any]]:
    ts = ts or now()
    victims: dict[int, dict[str, Any]] = {}
    with db.get_engine().connect() as conn:
        for row in conn.execute(
            select(objects.c.id, objects.c.key, objects.c.size, buckets.c.name)
            .select_from(objects.join(buckets))
            .where(objects.c.expires_at <= ts)
        ):
            victims[row.id] = {
                "bucket": row.name,
                "key": row.key,
                "size": row.size,
                "reason": "Expiry set at upload",
            }

        rules = conn.execute(
            select(lifecycle_rules, buckets.c.name.label("bucket_name")).select_from(
                lifecycle_rules.join(buckets)
            )
        ).all()
        for rule in rules:
            age_from = (
                objects.c.updated_at
                if rule.basis == "created"
                else func.coalesce(objects.c.last_accessed_at, objects.c.updated_at)
            )
            query = select(objects.c.id, objects.c.key, objects.c.size).where(
                objects.c.bucket_id == rule.bucket_id,
                _key_starts_with(rule.prefix),
                age_from <= ts - rule.expire_after,
            )
            if rule.keep_shared:
                query = query.where(~_has_active_share(ts))
            for row in conn.execute(query):
                victims.setdefault(
                    row.id,
                    {
                        "bucket": rule.bucket_name,
                        "key": row.key,
                        "size": row.size,
                        "reason": _rule_reason(rule),
                    },
                )
    return [{"id": object_id, **info} for object_id, info in victims.items()]


def run_gc() -> dict[str, int]:
    ts = now()
    victims = plan_cleanup(ts)
    with db.get_engine().begin() as conn:
        if victims:
            _delete_objects(conn, [v["id"] for v in victims])
        conn.execute(delete(shares).where(shares.c.expires_at <= ts))
        conn.execute(delete(upload_tokens).where(upload_tokens.c.expires_at <= ts - 86400))
        referenced = {r[0] for r in conn.execute(select(objects.c.blob_sha256).distinct())}
        referenced |= {
            r[0]
            for r in conn.execute(
                select(objects.c.original_sha256).where(objects.c.original_sha256.is_not(None))
            )
        }
    return {
        "deleted_objects": len(victims),
        "removed_blobs": storage.gc_blobs(referenced),
    }
