"""Device identity: stable, filesystem-safe names for capture folders.

Every connected camera gets a device SLUG derived from its model/label
(parentheticals stripped, snake_case): "Sony DSC-A7r IV (Control)" →
`sony_dsc_a7r_iv`; "Webcam: MacBook Pro Camera" → `macbook_pro_camera`.
Two identical bodies are disambiguated by the CameraHub with a serial
suffix (PTP bodies report one) or an index. Captures land under
`<capture_root>/<device_slug>/` and, when a sequence name is set,
`<capture_root>/<device_slug>/<sequence_name>/`.
"""

from __future__ import annotations

import os
import re


def slugify(text: str) -> str:
    """Lowercase snake_case, parentheticals stripped, safe for folders."""
    text = re.sub(r"\([^)]*\)", " ", str(text or ""))
    text = text.replace("Webcam:", " ")
    text = re.sub(r"[^a-z0-9]+", "_", text.lower())
    return text.strip("_") or "camera"


def default_capture_root() -> str:
    """The owner-specified default: the user's Pictures folder — captures
    belong where users look for pictures, not in an app-support dir."""
    return os.path.expanduser(os.path.join("~", "Pictures"))


def sanitize_sequence_name(name: str | None) -> str | None:
    """Sequence names become folder names: same slug rules, None when empty."""
    if name is None:
        return None
    cleaned = slugify(name)
    return cleaned if cleaned and cleaned != "camera" else None
