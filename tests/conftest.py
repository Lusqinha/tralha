import pytest
from fastapi.testclient import TestClient

from tralha import service
from tralha.auth import create_key
from tralha.config import get_settings
from tralha.db import get_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRALHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRALHA_API_URL", "http://testserver")
    get_settings.cache_clear()
    get_engine.cache_clear()

    from tralha.app import create_app

    with TestClient(create_app()) as c:
        service.create_bucket(None, "scratch")
        service.create_bucket(None, "secret")
        yield c

    get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def auth(client):
    key = create_key("test", "scratch:read scratch:write scratch:share")
    return {"Authorization": f"Bearer {key}"}
