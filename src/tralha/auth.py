"""API keys scoped per bucket.

A scope looks like bucket:permission, for example scratch:write, pr-*:share or *:read.
Permissions are read, write, delete, share and admin; admin includes all of them and can
create buckets. Only the key's hash is stored, so create_key is the one chance to see it.
"""

import hashlib
import secrets
from dataclasses import dataclass
from fnmatch import fnmatchcase

from sqlalchemy import select, update

from tralha.db import api_keys, get_engine, now

PERMISSIONS = {"read", "write", "delete", "share", "admin"}
KEY_PREFIX = "trh_"


class InvalidScope(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    key_id: int
    name: str
    scopes: tuple[tuple[str, str], ...]

    def can(self, bucket: str, perm: str) -> bool:
        return any(
            fnmatchcase(bucket, pattern) and granted in (perm, "admin")
            for pattern, granted in self.scopes
        )

    @property
    def is_admin(self) -> bool:
        return ("*", "admin") in self.scopes


def parse_scopes(raw: str) -> tuple[tuple[str, str], ...]:
    scopes = []
    for item in raw.split():
        pattern, sep, perm = item.rpartition(":")
        if not sep or not pattern or perm not in PERMISSIONS:
            raise InvalidScope(
                f"invalid scope: {item!r} (use bucket:{'|'.join(sorted(PERMISSIONS))})"
            )
        scopes.append((pattern, perm))
    if not scopes:
        raise InvalidScope("provide at least one scope")
    return tuple(scopes)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(name: str, scopes: str) -> str:
    parse_scopes(scopes)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    with get_engine().begin() as conn:
        conn.execute(
            api_keys.insert().values(
                name=name,
                prefix=key[:12],
                key_hash=_hash(key),
                scopes=scopes,
                created_at=now(),
            )
        )
    return key


def _principal_from_row(row) -> Principal:
    return Principal(key_id=row.id, name=row.name, scopes=parse_scopes(row.scopes))


def principal_for_key_id(key_id: int) -> Principal | None:
    with get_engine().connect() as conn:
        row = conn.execute(
            select(api_keys).where(api_keys.c.id == key_id, api_keys.c.revoked_at.is_(None))
        ).first()
    return _principal_from_row(row) if row else None


def authenticate(authorization: str | None) -> Principal | None:
    if not authorization:
        return None
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key.startswith(KEY_PREFIX):
        return None
    with get_engine().begin() as conn:
        row = conn.execute(
            select(api_keys).where(
                api_keys.c.key_hash == _hash(key.strip()),
                api_keys.c.revoked_at.is_(None),
            )
        ).first()
        if row is None:
            return None
        conn.execute(update(api_keys).where(api_keys.c.id == row.id).values(last_used_at=now()))
    return _principal_from_row(row)
