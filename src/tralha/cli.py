"""Local administration. Talks to the database directly instead of going through the API."""

import argparse
import asyncio
import re
import sys
from datetime import datetime

from sqlalchemy import select, update

from tralha import service, system
from tralha.auth import InvalidScope, create_key
from tralha.config import get_settings
from tralha.db import api_keys, get_engine, init_db, now

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(value: str) -> int:
    m = _DURATION.match(value)
    if not m:
        raise argparse.ArgumentTypeError("use a format like 30m, 12h or 7d")
    return int(m[1]) * _UNITS[m[2]]


def size(value: str) -> int:
    m = re.match(r"^(\d+)([KMG]B?)?$", value.upper())
    if not m:
        raise argparse.ArgumentTypeError("use a format like 500MB or 2GB")
    return int(m[1]) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[(m[2] or "")[:1]]


def cmd_serve(args) -> None:
    import uvicorn

    settings = get_settings()

    def server(app: str, port: int) -> uvicorn.Server:
        config = uvicorn.Config(
            app,
            factory=True,
            host=args.host or settings.host,
            port=port,
            proxy_headers=True,
            forwarded_allow_ips=settings.forwarded_allow_ips,
            server_header=False,
        )
        return uvicorn.Server(config)

    private = server("tralha.app:create_app", args.port or settings.port)
    public = server("tralha.app:create_public_app", args.public_port or settings.public_port)

    async def run_both() -> None:
        tasks = [asyncio.create_task(s.serve()) for s in (private, public)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        private.should_exit = public.should_exit = True
        await asyncio.gather(*tasks)

    asyncio.run(run_both())


def cmd_key_create(args) -> None:
    key = create_key(args.name, " ".join(args.scope), args.expires)
    print(key)
    print("Store it now: the key is not shown again.", file=sys.stderr)


def cmd_key_list(_args) -> None:
    with get_engine().connect() as conn:
        rows = conn.execute(select(api_keys).order_by(api_keys.c.id)).all()
    for r in rows:
        print(
            f"{r.id:>4}  {r.prefix}…  {r.name:<20} {_key_status(r):<22} "
            f"last used {_when(r.last_used_at)} from {r.last_used_ip or '-'}  {r.scopes}"
        )


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def _key_status(row) -> str:
    if row.revoked_at:
        return "revoked"
    if row.expires_at and row.expires_at <= now():
        return "expired"
    if row.expires_at:
        return f"expires {_when(row.expires_at)}"
    return "active"


def cmd_key_revoke(args) -> None:
    with get_engine().begin() as conn:
        n = conn.execute(
            update(api_keys).where(api_keys.c.id == args.id).values(revoked_at=now())
        ).rowcount
    if not n:
        sys.exit(f"key {args.id} not found")


def cmd_bucket_create(args) -> None:
    visibility = "public-read" if args.public else "private"
    print(
        service.create_bucket(
            None,
            args.name,
            visibility,
            expire_after=args.ttl,
            max_bytes=args.max_size,
            on_full=args.on_full,
            compression=args.compression,
        )
    )


def cmd_bucket_list(_args) -> None:
    for b in service.list_buckets(None):
        quota = f"/{b['max_bytes']}" if b["max_bytes"] else ""
        compression = b["compression"] or "inherit"
        print(
            f"{b['name']:<24} {b['visibility']:<12} {b['objects']:>6} obj  {b['bytes']}{quota} B  compression={compression}"
        )
        for r in b["rules"]:
            print(f"    rule #{r['id']}: {_rule_text(r)}")


def _rule_text(r) -> str:
    prefix = f"'{r['prefix']}*' " if r["prefix"] else ""
    keep = ", keeps files with an active link" if r["keep_shared"] else ""
    return f"{prefix}deletes after {r['expire_after']}s ({r['basis']}){keep}"


def cmd_bucket_set(args) -> None:
    fields = {}
    if args.public is not None:
        fields["visibility"] = "public-read" if args.public else "private"
    if args.max_size is not None:
        fields["max_bytes"] = args.max_size or None
    if args.on_full:
        fields["on_full"] = args.on_full
    if args.compression:
        fields["compression"] = None if args.compression == "inherit" else args.compression
    print(service.update_bucket(None, args.name, **fields))


def cmd_rule_add(args) -> None:
    rule = service.add_rule(
        None, args.bucket, args.expire, args.prefix, args.basis, args.keep_shared
    )
    print(f"rule #{rule['id']}: {_rule_text(rule)}")


def cmd_rule_rm(args) -> None:
    service.delete_rule(None, args.id)


def cmd_settings(args) -> None:
    try:
        values = system.update(dict(args.set)) if args.set else system.get_all()
    except system.InvalidSetting as e:
        sys.exit(f"error: {e}")
    for name, value in values.items():
        print(f"{name} = {value}")


def cmd_gc(args) -> None:
    if args.dry_run:
        for v in service.plan_cleanup():
            print(f"{v['bucket']}/{v['key']}  ({v['reason']})")
        return
    print(service.run_gc())


def setting(value: str) -> tuple[str, str]:
    name, sep, val = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError("use name=value")
    return name, val


def main() -> None:
    parser = argparse.ArgumentParser(prog="tralha")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser(
        "serve", help="start the private port (API, MCP, panel) and the public port (links)"
    )
    p.add_argument("--host", help="default: TRALHA_HOST or 0.0.0.0")
    p.add_argument("--port", type=int, help="private port; default: TRALHA_PORT or 8000")
    p.add_argument(
        "--public-port", type=int, help="public port; default: TRALHA_PUBLIC_PORT or 8001"
    )
    p.set_defaults(fn=cmd_serve)

    key = sub.add_parser("key", help="API keys").add_subparsers(required=True)
    p = key.add_parser("create", help="create a key and print its value")
    p.add_argument("name")
    p.add_argument(
        "--scope",
        action="append",
        required=True,
        help="bucket:permission, repeatable. E.g. --scope 'scratch:write'",
    )
    p.add_argument("--expires", type=duration, help="key lifetime, e.g. 90d; default: no expiry")
    p.set_defaults(fn=cmd_key_create)
    key.add_parser("list").set_defaults(fn=cmd_key_list)
    p = key.add_parser("revoke")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_key_revoke)

    bucket = sub.add_parser("bucket", help="buckets").add_subparsers(required=True)
    p = bucket.add_parser("create")
    p.add_argument("name")
    p.add_argument("--public", action="store_true", help="public read access at /b/<bucket>/<key>")
    p.add_argument(
        "--ttl", type=duration, help="adds a rule that deletes objects after this long, e.g. 7d"
    )
    p.add_argument("--max-size", type=size, help="quota, e.g. 2GB")
    p.add_argument("--on-full", choices=service.ON_FULL, default="reject")
    p.add_argument(
        "--compression",
        choices=system.COMPRESSION_MODES,
        help="default: inherit from the system setting",
    )
    p.set_defaults(fn=cmd_bucket_create)
    bucket.add_parser("list").set_defaults(fn=cmd_bucket_list)
    p = bucket.add_parser("set", help="change a bucket")
    p.add_argument("name")
    p.add_argument("--public", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--max-size", type=size, help="0 removes the quota")
    p.add_argument("--on-full", choices=service.ON_FULL)
    p.add_argument("--compression", choices=("inherit", *system.COMPRESSION_MODES))
    p.set_defaults(fn=cmd_bucket_set)

    rule = sub.add_parser("rule", help="cleanup rules").add_subparsers(required=True)
    p = rule.add_parser("add")
    p.add_argument("bucket")
    p.add_argument("--expire", type=duration, required=True, help="e.g. 7d or 12h")
    p.add_argument("--prefix", default="")
    p.add_argument("--basis", choices=service.RULE_BASIS, default="created")
    p.add_argument(
        "--keep-shared", action="store_true", help="never delete objects with an active link"
    )
    p.set_defaults(fn=cmd_rule_add)
    p = rule.add_parser("rm")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_rule_rm)

    p = sub.add_parser("settings", help="show or change system settings")
    p.add_argument("set", nargs="*", type=setting, help="e.g. compression=lossless")
    p.set_defaults(fn=cmd_settings)

    p = sub.add_parser("gc", help="apply cleanup rules and remove orphan blobs")
    p.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args()
    if args.fn is not cmd_serve:
        init_db()
    try:
        args.fn(args)
    except (service.ServiceError, InvalidScope) as e:
        sys.exit(f"error: {e}")
