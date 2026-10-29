"""Files on disk, addressed by the sha256 of their content: blobs/ab/cd/<sha256>.

An object key never becomes a path on disk; the database links keys to blobs. Uploads go
to tmp/ while the hash is computed and only then move to blobs/ with an atomic rename.
The GC leaves files younger than an hour alone so it never deletes an upload in progress.

The content type is detected from the first bytes, because the client's header cannot be
trusted.
"""

import hashlib
import os
import secrets
import time
from pathlib import Path

from tralha.config import get_settings

SNIFF_BYTES = 512


class TooLarge(Exception):
    pass


def blob_path(sha256: str) -> Path:
    return get_settings().blobs_dir / sha256[:2] / sha256[2:4] / sha256


class BlobWriter:
    def __init__(self, max_bytes: int | None = None):
        settings = get_settings()
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes if max_bytes is not None else settings.max_upload_bytes
        self.tmp_path = settings.tmp_dir / secrets.token_hex(16)
        self._file = open(self.tmp_path, "wb")
        self._hash = hashlib.sha256()
        self.size = 0
        self.head = b""

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            self.abort()
            raise TooLarge(f"file larger than {self.max_bytes // (1024 * 1024)} MB")
        if len(self.head) < SNIFF_BYTES:
            self.head += chunk[: SNIFF_BYTES - len(self.head)]
        self._hash.update(chunk)
        self._file.write(chunk)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def finish(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()

    def commit(self) -> str:
        sha = self.sha256
        dest = blob_path(sha)
        if dest.exists():
            self.tmp_path.unlink(missing_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.tmp_path, dest)
        return sha

    def abort(self) -> None:
        if not self._file.closed:
            self._file.close()
        self.tmp_path.unlink(missing_ok=True)


def write_bytes(data: bytes) -> BlobWriter:
    writer = BlobWriter()
    try:
        writer.write(data)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    return writer


def gc_blobs(referenced: set[str], min_age: int = 3600) -> int:
    settings = get_settings()
    cutoff = time.time() - min_age
    removed = 0
    for root in (settings.blobs_dir, settings.tmp_dir, settings.thumbs_dir):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.stat().st_mtime > cutoff:
                continue
            if root == settings.tmp_dir or path.stem not in referenced:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def thumb_path(sha256: str) -> Path:
    return get_settings().thumbs_dir / sha256[:2] / f"{sha256}.webp"


_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
    (b"PK\x03\x04", "application/zip"),
]

_TEXT_EXTENSIONS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".log": "text/plain",
    ".txt": "text/plain",
}


def _decode_text_prefix(head: bytes) -> str | None:
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as e:
        last_char_cut_in_half = e.start >= len(head) - 3
        return head[: e.start].decode("utf-8") if last_char_cut_in_half else None


def sniff(head: bytes, key: str) -> str:
    for sig, ctype in _SIGNATURES:
        if head.startswith(sig):
            return ctype
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    decoded = _decode_text_prefix(head)
    if decoded is None or "\x00" in decoded:
        return "application/octet-stream"
    if "<svg" in decoded and decoded.lstrip().startswith(("<svg", "<?xml")):
        return "image/svg+xml"
    return _TEXT_EXTENSIONS.get(Path(key).suffix.lower(), "text/plain")
