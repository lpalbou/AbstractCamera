"""JPEG structural probes (no decode)."""

from __future__ import annotations

def parse_jpeg_dimensions(jpeg: bytes) -> tuple[int, int] | None:
    """JPEG SOF marker scan (microseconds, no decode): the honest signal
    that a live-view resolution switch has actually reached the stream."""
    if not jpeg or jpeg[:2] != b"\xff\xd8":
        return None
    i = 2
    length = len(jpeg)
    while i + 9 < length:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        marker = jpeg[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = (jpeg[i + 5] << 8) | jpeg[i + 6]
            width = (jpeg[i + 7] << 8) | jpeg[i + 8]
            return (width, height) if width and height else None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        segment_length = (jpeg[i + 2] << 8) | jpeg[i + 3]
        i += 2 + max(segment_length, 2)
    return None
