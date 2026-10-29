import struct
import subprocess
import sys
import textwrap
import zlib

import pytest

from tralha import images
from tralha.image_worker import _landlock_abi

ESCAPE_ATTEMPTS = textwrap.dedent(
    """
    import os, socket, subprocess, sys
    from tralha.image_worker import apply_resource_limits, confine_to

    apply_resource_limits()
    assert confine_to(sys.argv[1], sys.argv[2])

    def blocked(attempt):
        try:
            attempt()
        except OSError:
            return True
        return False

    results = {
        "shell": blocked(lambda: subprocess.run(["/bin/sh", "-c", "id"])),
        "fork": blocked(os.fork),
        "read_outside": blocked(lambda: open("/etc/passwd").read()),
        "write_outside": blocked(lambda: open(sys.argv[3], "w").write("x")),
        "connect": blocked(lambda: socket.create_connection(("1.1.1.1", 80), timeout=2)),
        "read_input": not blocked(lambda: open(sys.argv[1], "rb").read()),
        "write_output": not blocked(lambda: open(sys.argv[2] + "/out", "w").write("ok")),
    }
    print(results)
    sys.exit(0 if all(results.values()) else 1)
    """
)


def png_header_only(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.mark.skipif(_landlock_abi() < 1, reason="kernel has no Landlock")
def test_confined_worker_cannot_escape(tmp_path):
    src = tmp_path / "input.png"
    src.write_bytes(b"fake")
    out_dir = tmp_path / "job"
    out_dir.mkdir()
    done = subprocess.run(
        [sys.executable, "-I", "-c", ESCAPE_ATTEMPTS, str(src), str(out_dir), str(tmp_path / "x")],
        check=False,
        capture_output=True,
        text=True,
        env={},
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_eps_disguised_as_png_never_reaches_ghostscript(client, tmp_path):
    eps = tmp_path / "evil.png"
    eps.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n(id) system\n")
    with pytest.raises(images.ImageRejected):
        images.thumbnail(eps, tmp_path / "thumb.webp")


def test_pixel_bomb_is_rejected_before_decoding(client, tmp_path):
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(png_header_only(30_000, 30_000))
    with pytest.raises(images.ImageRejected):
        images.thumbnail(bomb, tmp_path / "thumb.webp")
