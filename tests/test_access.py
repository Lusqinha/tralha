import pytest

from tralha.service import Invalid, validate_key
from tralha.storage import sniff

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_requires_api_key(client):
    assert client.get("/api/buckets").status_code == 401
    assert (
        client.get("/api/buckets", headers={"Authorization": "Bearer trh_nope"}).status_code == 401
    )
    assert client.post("/mcp", json={}).status_code == 401


def test_scope_is_enforced_per_bucket(client, auth):
    assert (
        client.put("/api/buckets/scratch/objects/a.txt", content=b"hi", headers=auth).status_code
        == 201
    )
    assert (
        client.put("/api/buckets/secret/objects/a.txt", content=b"hi", headers=auth).status_code
        == 403
    )
    assert client.get("/api/buckets/secret/objects", headers=auth).status_code == 403
    assert client.delete("/api/buckets/scratch/objects/a.txt", headers=auth).status_code == 403
    assert [b["name"] for b in client.get("/api/buckets", headers=auth).json()] == ["scratch"]


@pytest.mark.parametrize("key", ["../etc/passwd", "a/../../b", "/abs", "a/./b", "a\x00b", ""])
def test_rejects_unsafe_keys(key):
    with pytest.raises(Invalid):
        validate_key(key)


def test_upload_token_is_single_use(client, auth):
    up = client.post("/api/buckets/scratch/uploads", json={"key": "img/x.png"}, headers=auth).json()
    path = up["url"].removeprefix("http://testserver")

    first = client.put(path, content=PNG, headers={"content-type": "text/html"})
    assert first.status_code == 201
    assert first.json()["content_type"] == "image/png"
    assert client.put(path, content=PNG).status_code == 404


def test_share_link_is_public_and_revocable(client, auth):
    client.put("/api/buckets/scratch/objects/x.png", content=PNG, headers=auth)
    share = client.post("/api/buckets/scratch/shares", json={"key": "x.png"}, headers=auth).json()
    path = share["url"].removeprefix("http://testserver")

    res = client.get(path)
    assert res.status_code == 200
    assert res.content == PNG
    assert "sandbox" in res.headers["content-security-policy"]

    assert client.delete(f"/api/shares/{share['token']}", headers=auth).status_code == 204
    assert client.get(path).status_code == 404


def test_private_object_is_not_reachable_without_share(client, auth):
    client.put("/api/buckets/scratch/objects/x.png", content=PNG, headers=auth)
    assert client.get("/b/scratch/x.png").status_code == 404


def test_uploaded_html_is_served_as_download(client, auth):
    client.put("/api/buckets/scratch/objects/x.html", content=b"<script>1</script>", headers=auth)
    share = client.post("/api/buckets/scratch/shares", json={"key": "x.html"}, headers=auth).json()
    res = client.get(share["url"].removeprefix("http://testserver"))
    assert res.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in res.headers["content-security-policy"]


def test_sniff_ignores_extension_for_binary_signatures():
    assert sniff(b"<svg onload=alert(1)>", "x.png") == "image/svg+xml"
    assert sniff(PNG, "x.txt") == "image/png"


def test_public_port_only_serves_links(client):
    from fastapi.testclient import TestClient

    from tralha.app import create_public_app

    public = TestClient(create_public_app())
    assert public.get("/healthz").status_code == 200
    for path in ("/api/buckets", "/admin/login", "/mcp", "/upload/x", "/api/docs"):
        assert public.get(path).status_code == 404, path


def test_upload_token_dies_with_revoked_key(client, auth):
    from tralha.db import api_keys, get_engine

    up = client.post("/api/buckets/scratch/uploads", json={"key": "a.png"}, headers=auth).json()
    with get_engine().begin() as conn:
        conn.execute(api_keys.update().values(revoked_at=1))
    assert client.put(up["url"].removeprefix("http://testserver"), content=PNG).status_code == 403
