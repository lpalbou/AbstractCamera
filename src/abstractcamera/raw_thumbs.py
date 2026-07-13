"""RAW capture thumbnails for the catch log (self-contained; [raw] extra).

RAW files (NEF/ARW/...) carry a full-resolution embedded JPEG; extracting it
lets a catch-log row show the shot instead of a blank tile. rawpy is an
optional dependency — absent, callers fall through to no-thumbnail (never an
error). The functions mirror the host-side originals byte-for-byte so the
manager's download path behaves identically after the extraction.
"""

from __future__ import annotations

import io

SUPPORTED_RAW_EXTENSIONS = {
    ".nef", ".arw", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
}


def is_raw_extension(ext: str) -> bool:
    return (ext or "").lower() in SUPPORTED_RAW_EXTENSIONS


def extract_raw_thumbnail_jpeg(file_bytes: bytes) -> bytes | None:
    """Embedded JPEG thumbnail bytes (for tether catch logs), or None."""
    import rawpy

    try:
        with rawpy.imread(io.BytesIO(file_bytes)) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            return bytes(thumb.data)
    except Exception:
        pass
    return None
