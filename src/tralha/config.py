"""Settings read from TRALHA_* environment variables.

api_url is where the API, MCP and uploads live, and it should stay on the private network.
public_url is the host that faces the internet and goes into /p/... links.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    public_url: str
    api_url: str
    max_upload_bytes: int
    upload_ttl: int
    gc_interval: int
    host: str
    port: int
    public_port: int
    forwarded_allow_ips: str
    admin_allowed_ips: str
    api_docs: bool

    @property
    def secure_cookies(self) -> bool:
        return self.api_url.startswith("https://")

    @property
    def max_request_bytes(self) -> int:
        return self.max_upload_bytes + 1024 * 1024

    @property
    def max_json_bytes(self) -> int:
        return 1024 * 1024

    @property
    def max_panel_upload_bytes(self) -> int:
        return self.max_upload_bytes * 4 + 1024 * 1024

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"


@lru_cache
def get_settings() -> Settings:
    port = int(os.environ.get("TRALHA_PORT", "8000"))
    public_port = int(os.environ.get("TRALHA_PUBLIC_PORT", "8001"))
    api_url = os.environ.get("TRALHA_API_URL", f"http://localhost:{port}").rstrip("/")
    public_url = os.environ.get("TRALHA_PUBLIC_URL", f"http://localhost:{public_port}").rstrip("/")
    data_dir = Path(os.environ.get("TRALHA_DATA_DIR", "./data")).resolve()
    return Settings(
        data_dir=data_dir,
        database_url=os.environ.get("TRALHA_DATABASE_URL", f"sqlite:///{data_dir / 'tralha.db'}"),
        public_url=public_url,
        api_url=api_url,
        max_upload_bytes=int(os.environ.get("TRALHA_MAX_UPLOAD_MB", "25")) * 1024 * 1024,
        upload_ttl=int(os.environ.get("TRALHA_UPLOAD_TTL", "600")),
        gc_interval=int(os.environ.get("TRALHA_GC_INTERVAL", "3600")),
        host=os.environ.get("TRALHA_HOST", "0.0.0.0"),
        port=port,
        public_port=public_port,
        forwarded_allow_ips=os.environ.get("TRALHA_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        admin_allowed_ips=os.environ.get("TRALHA_ADMIN_ALLOWED_IPS", ""),
        api_docs=os.environ.get("TRALHA_API_DOCS", "").lower() in ("1", "true", "yes"),
    )
