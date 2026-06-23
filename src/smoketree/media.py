"""Media-type inference from a file path (by extension).

The path core carries no per-input media declarations, so the LLM/diffusion backends
infer how to treat each resolved input from its extension: text/data are inlined into
prompts, images are encoded and attached.
"""

from __future__ import annotations

from pathlib import Path

MediaType = str  # "image" | "audio" | "video" | "text" | "data"

_EXT_MEDIA: dict[str, MediaType] = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "bmp": "image", "tiff": "image", "tif": "image",
    "wav": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio",
    "aac": "audio", "m4a": "audio",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "webm": "video",
    "txt": "text", "md": "text",
    "json": "data", "yaml": "data", "yml": "data", "csv": "data",
}


def infer_media(path: Path) -> MediaType:
    """Best-effort media type for ``path`` (defaults to ``text``)."""
    return _EXT_MEDIA.get(path.suffix.lstrip(".").lower(), "text")
