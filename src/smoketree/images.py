"""Polite image encoding for the vision backends.

Encodes an image to base64 for an LLM call, downscaling it to a long-edge cap and
stripping metadata (EXIF/GPS) along the way. Originals on disk are never touched.

Principles:
- downscale only (never upscale), preserving aspect ratio;
- strip metadata when we re-encode (privacy — phone photos carry GPS);
- if the cap is falsy or Pillow is unavailable, send the original bytes unchanged.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


def media_type_for(path: Path) -> str | None:
    return _MEDIA_TYPES.get(path.suffix.lstrip(".").lower())


def encode_image(path: Path, max_edge: int | None) -> tuple[str, str]:
    """Return ``(base64_data, media_type)`` for an image input.

    When ``max_edge`` is set and Pillow is available, the image is re-encoded:
    downscaled so its longest edge is at most ``max_edge`` (only if larger), with
    metadata dropped. Otherwise the original bytes are returned as-is.
    """
    ext = path.suffix.lstrip(".").lower()
    media_type = _MEDIA_TYPES.get(ext, "image/png")

    if not max_edge:
        return base64.standard_b64encode(path.read_bytes()).decode("ascii"), media_type

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return base64.standard_b64encode(path.read_bytes()).decode("ascii"), media_type

    with Image.open(path) as im:
        save_format = "JPEG" if ext in ("jpg", "jpeg") else (im.format or "PNG")
        longest = max(im.size)
        if longest > max_edge:
            scale = max_edge / longest
            im = im.resize(
                (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            )
        if save_format == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        # Re-saving without passing exif/icc drops metadata.
        im.save(buf, format=save_format, quality=90)
        data = buf.getvalue()

    return base64.standard_b64encode(data).decode("ascii"), media_type
