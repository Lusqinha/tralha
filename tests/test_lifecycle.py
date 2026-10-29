import io

from PIL import Image, ImageChops, ImageDraw
from sqlalchemy import update

from tralha import service, system
from tralha.auth import Principal
from tralha.db import get_engine, objects

ADMIN = Principal(0, "test", (("*", "admin"),))


def screenshot_png() -> bytes:
    img = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(img)
    for y in range(20, 600, 24):
        draw.rectangle((20, y, 20 + (y * 7) % 700, y + 10), fill=(40, 40, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def age(key: str, seconds: int) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            update(objects)
            .where(objects.c.key == key)
            .values(updated_at=objects.c.updated_at - seconds)
        )


def test_compression_is_off_by_default(client):
    obj = service.put_bytes(ADMIN, "scratch", "a.png", screenshot_png())
    assert obj.content_type == "image/png"


def test_lossless_compression_keeps_every_pixel(client):
    system.update({"compression": "lossless"})
    png = screenshot_png()
    obj = service.put_bytes(ADMIN, "scratch", "shot.png", png)

    assert obj.content_type == "image/webp"
    assert obj.size < len(png)
    assert obj.original_content_type == "image/png"
    with Image.open(io.BytesIO(png)) as a, Image.open(obj.path) as b:
        assert ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


def test_bucket_can_opt_out_of_system_compression(client):
    system.update({"compression": "lossy"})
    service.update_bucket(None, "scratch", compression="off")
    assert (
        service.put_bytes(ADMIN, "scratch", "a.png", screenshot_png()).content_type == "image/png"
    )


def test_rule_expires_old_objects_but_keeps_shared_ones(client):
    service.put_bytes(ADMIN, "scratch", "tmp/old.txt", b"old")
    service.put_bytes(ADMIN, "scratch", "tmp/shared.txt", b"old, with a link")
    service.put_bytes(ADMIN, "scratch", "tmp/new.txt", b"new")
    service.put_bytes(ADMIN, "scratch", "keep/old.txt", b"outside the prefix")
    service.create_share(ADMIN, "scratch", "tmp/shared.txt")
    for key in ("tmp/old.txt", "tmp/shared.txt", "keep/old.txt"):
        age(key, 10 * 86400)

    service.add_rule(None, "scratch", 7 * 86400, prefix="tmp/", keep_shared=True)
    assert [v["key"] for v in service.plan_cleanup()] == ["tmp/old.txt"]

    service.run_gc()
    remaining = service.list_objects(ADMIN, "scratch", delimiter=None)["objects"]
    assert sorted(o["key"] for o in remaining) == ["keep/old.txt", "tmp/new.txt", "tmp/shared.txt"]


def test_full_bucket_evicts_oldest_unshared(client):
    service.update_bucket(None, "scratch", max_bytes=10, on_full="evict-oldest")
    service.put_bytes(ADMIN, "scratch", "a", b"aaaa")
    service.put_bytes(ADMIN, "scratch", "b", b"bbbb")
    service.create_share(ADMIN, "scratch", "a")
    age("a", 100)
    age("b", 50)

    service.put_bytes(ADMIN, "scratch", "c", b"cccc")
    keys = sorted(o["key"] for o in service.list_objects(ADMIN, "scratch")["objects"])
    assert keys == ["a", "c"]


def test_admin_panel_requires_login(client):
    res = client.get("/admin/files", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/admin/login"


def test_admin_can_upload_into_folder(client):
    from tralha.auth import create_key

    key = create_key("admin", "*:admin")
    client.post("/admin/login", data={"key": key})
    res = client.post(
        "/admin/actions/upload",
        data={"bucket": "scratch", "prefix": "pr-9/", "folder": "telas"},
        files=[("files", ("a.png", screenshot_png())), ("files", ("../b.txt", b"hi"))],
        follow_redirects=False,
    )
    assert res.status_code == 303
    keys = sorted(
        o["key"] for o in service.list_objects(ADMIN, "scratch", delimiter=None)["objects"]
    )
    assert keys == ["pr-9/telas/a.png", "pr-9/telas/b.txt"]
