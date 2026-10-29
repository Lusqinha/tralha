"""System settings that the panel, the API and the CLI can change without a restart.

They live in the database instead of environment variables. Compression starts off on
purpose: turning it on is the administrator's call.
"""

import json
from typing import Any

from sqlalchemy import delete, select

from tralha.db import get_engine, system_settings

COMPRESSION_MODES = ("off", "lossless", "lossy")

DEFAULTS: dict[str, Any] = {
    "compression": "off",
    "compression_quality": 80,
    "keep_original": False,
}


class InvalidSetting(ValueError):
    pass


def _validate(name: str, value: Any) -> Any:
    if name not in DEFAULTS:
        raise InvalidSetting(f"unknown setting: {name}")
    if name == "compression" and value not in COMPRESSION_MODES:
        raise InvalidSetting(f"compression must be {' | '.join(COMPRESSION_MODES)}")
    if name == "compression_quality":
        value = int(value)
        if not 1 <= value <= 100:
            raise InvalidSetting("compression_quality must be between 1 and 100")
    if name == "keep_original":
        if isinstance(value, str):
            value = value.lower() in ("1", "true", "on", "yes")
        value = bool(value)
    return value


def get_all() -> dict[str, Any]:
    with get_engine().connect() as conn:
        rows = conn.execute(select(system_settings)).all()
    stored = {r.name: json.loads(r.value) for r in rows if r.name in DEFAULTS}
    return {**DEFAULTS, **stored}


def update(values: dict[str, Any]) -> dict[str, Any]:
    clean = {name: _validate(name, value) for name, value in values.items()}
    with get_engine().begin() as conn:
        for name, value in clean.items():
            conn.execute(delete(system_settings).where(system_settings.c.name == name))
            conn.execute(system_settings.insert().values(name=name, value=json.dumps(value)))
    return get_all()
