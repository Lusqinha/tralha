"""Everything tralha does with images: compression on upload, panel thumbnails and the
smaller copy the MCP server sends to the model.

No image is decoded in this process. Each operation runs in image_worker, an isolated
process, and only the finished bytes come back here.

Compression turns PNG, JPEG and GIF into WebP. Lossless mode skips JPEG because it almost
always grows as lossless WebP. A file is only replaced when the new version is at least
MIN_SAVING_RATIO smaller. Video is out of scope: it needs ffmpeg and a background queue.
"""

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tralha.config import get_settings
from tralha.image_worker import EXIT_OK, EXIT_REJECTED, LANDLOCK_UNAVAILABLE

log = logging.getLogger("tralha")

MIN_SAVING_RATIO = 0.05
JOB_TIMEOUT_SECONDS = 30

Mode = Literal["off", "lossless", "lossy"]
LOSSLESS_TYPES = ("image/png", "image/gif")
LOSSY_TYPES = ("image/png", "image/jpeg", "image/gif")
DECODABLE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


class ImageRejected(Exception):
    pass


class ImageJobFailed(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    mode: Mode
    quality: int
    keep_original: bool


_warned_without_landlock = False


def _run_isolated(op: str, src: Path, **params) -> bytes:
    global _warned_without_landlock
    tmp_root = get_settings().tmp_dir
    tmp_root.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix="img-", dir=tmp_root))
    dest = job_dir / "out"
    job = {"op": op, "src": str(src), "dest": str(dest), **params}
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "tralha.image_worker", json.dumps(job)],
            env={},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=JOB_TIMEOUT_SECONDS,
            check=False,
        )
        stderr = done.stderr.decode(errors="replace")
        if LANDLOCK_UNAVAILABLE in stderr and not _warned_without_landlock:
            _warned_without_landlock = True
            log.warning("kernel has no Landlock: images are processed with resource limits only")
        if done.returncode == EXIT_REJECTED:
            raise ImageRejected(stderr.strip())
        if done.returncode != EXIT_OK:
            raise ImageJobFailed(f"worker exited with {done.returncode}: {stderr[-400:]}")
        return dest.read_bytes()
    except subprocess.TimeoutExpired as e:
        raise ImageJobFailed(f"worker took longer than {JOB_TIMEOUT_SECONDS}s") from e
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def eligible(content_type: str, mode: str) -> bool:
    if mode == "lossless":
        return content_type in LOSSLESS_TYPES
    if mode == "lossy":
        return content_type in LOSSY_TYPES
    return False


def to_webp_if_smaller(path: Path, content_type: str, policy: Policy) -> bytes | None:
    if not eligible(content_type, policy.mode):
        return None
    data = _run_isolated("webp", path, lossless=policy.mode == "lossless", quality=policy.quality)
    if len(data) > path.stat().st_size * (1 - MIN_SAVING_RATIO):
        return None
    return data


def thumbnail(src: Path, dest: Path) -> None:
    data = _run_isolated("thumbnail", src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial")
    partial.write_bytes(data)
    partial.replace(dest)


def model_preview_png(src: Path) -> bytes:
    return _run_isolated("model_preview", src)
