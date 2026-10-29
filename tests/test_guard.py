from fastapi.testclient import TestClient
from sqlalchemy import select

from tralha.auth import create_key
from tralha.config import get_settings
from tralha.db import api_keys, get_engine
from tralha.guard import MAX_FAILURES

BIG_BODY = b"x" * (5 * 1024 * 1024)


def test_body_is_never_read_without_a_key(client):
    res = client.post(
        "/api/buckets", content=BIG_BODY, headers={"content-type": "application/json"}
    )
    assert res.status_code == 401


def test_json_routes_accept_at_most_one_megabyte(client, auth):
    res = client.post(
        "/api/buckets", content=BIG_BODY, headers={**auth, "content-type": "application/json"}
    )
    assert res.status_code == 413


def test_repeated_bad_keys_lock_the_client_out(client, auth):
    bad = {"Authorization": "Bearer trh_not-a-real-key"}
    for _ in range(MAX_FAILURES):
        assert client.get("/api/buckets", headers=bad).status_code == 401
    assert client.get("/api/buckets", headers=auth).status_code == 429


def test_failed_panel_logins_count_toward_the_lockout(client):
    for _ in range(MAX_FAILURES):
        assert client.post("/admin/login", data={"key": "trh_wrong"}).status_code == 401
    assert client.get("/admin/login").status_code == 429


def test_panel_hidden_from_ips_outside_the_allowlist(client, monkeypatch):
    from tralha.app import create_app

    monkeypatch.setenv("TRALHA_ADMIN_ALLOWED_IPS", "10.0.0.0/8")
    get_settings.cache_clear()
    with TestClient(create_app()) as restricted:
        assert restricted.get("/admin/login").status_code == 404
        assert restricted.get("/healthz").status_code == 200


def test_expired_key_is_rejected(client):
    key = create_key("short-lived", "*:read", expires_in=-1)
    res = client.get("/api/buckets", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 401


def test_key_use_records_the_client_ip(client, auth):
    client.get("/api/buckets", headers=auth)
    with get_engine().connect() as conn:
        ip = conn.execute(select(api_keys.c.last_used_ip).where(api_keys.c.name == "test")).scalar()
    assert ip == "testclient"
