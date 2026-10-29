"""Pillow operations. Only image_worker imports this module, already inside the sandbox.

Pillow may only open PNG, JPEG, GIF and WebP. Without that format list, Image.open tries
every format it knows, including EPS, which calls Ghostscript.
"""

import warnings

from PIL import Image, ImageSequence, UnidentifiedImageError

FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
MAX_PIXELS = 50_000_000
MAX_ANIMATION_PIXELS = 150_000_000
THUMB_SIZE = 480
MODEL_PREVIEW_SIZE = 1568

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)


class Rejected(Exception):
    pass


def preload_codecs() -> None:
    Image.init()


def _open(path: str) -> Image.Image:
    try:
        img = Image.open(path, formats=FORMATS)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as e:
        raise Rejected(str(e)) from e
    pixels = img.size[0] * img.size[1]
    frames = getattr(img, "n_frames", 1)
    if pixels > MAX_PIXELS or pixels * frames > MAX_ANIMATION_PIXELS:
        img.close()
        raise Rejected(f"{img.size[0]}x{img.size[1]} with {frames} frame(s)")
    return img


def _flat(img: Image.Image) -> Image.Image:
    return img.convert("RGBA" if img.mode in ("RGBA", "LA", "P", "PA") else "RGB")


def to_webp(src: str, dest: str, lossless: bool, quality: int) -> None:
    options = {"lossless": lossless, "quality": 100 if lossless else quality, "method": 4}
    with _open(src) as img:
        if getattr(img, "n_frames", 1) == 1:
            _flat(img).save(dest, "WEBP", exif=b"", **options)
            return
        frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
        durations = [
            frame.info.get("duration", img.info.get("duration", 100))
            for frame in ImageSequence.Iterator(img)
        ]
        frames[0].save(
            dest,
            "WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=img.info.get("loop", 0),
            **options,
        )


def thumbnail(src: str, dest: str) -> None:
    with _open(src) as img:
        img.seek(0)
        small = _flat(img)
        small.thumbnail((THUMB_SIZE, THUMB_SIZE))
        small.save(dest, "WEBP", quality=75, method=4)


def model_preview(src: str, dest: str) -> None:
    with _open(src) as img:
        img.seek(0)
        preview = _flat(img)
        preview.thumbnail((MODEL_PREVIEW_SIZE, MODEL_PREVIEW_SIZE))
        preview.save(dest, "PNG", optimize=True)


OPERATIONS = {"webp": to_webp, "thumbnail": thumbnail, "model_preview": model_preview}
