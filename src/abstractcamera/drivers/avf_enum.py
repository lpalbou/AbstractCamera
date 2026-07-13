"""AVFoundation device enumeration (PyObjC): identity that cannot invert.

The 2026-07-12 inversion post-mortem (ADR 0009): webcam ids used to be
POSITIONS in ffmpeg's device list, consumed by OpenCV's separate
enumeration at a different time — Continuity cameras join/leave the device
set dynamically and the two enumerators drifted, so "MacBook Pro Camera"
streamed the iPhone. Identity now comes from the SAME objects we capture
from: `AVCaptureDevice.uniqueID()` — stable, opaque, and usable to reopen
the exact device (`deviceWithUniqueID_`). No index space exists anymore.

Enumeration is non-invasive (class-method listing: no device opens, no LED,
no TCC prompt — the prompt fires at session start). All PyObjC imports are
function-local so the module is importable on any platform.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# Structured type-string sets (extend as macOS grows new types). The
# Continuity check is layered: selector -> type string -> modelID -> name.
_BUILTIN_TYPE_PREFIX = "AVCaptureDeviceTypeBuiltIn"
_CONTINUITY_TYPES = {"AVCaptureDeviceTypeContinuityCamera"}
_DESKVIEW_TYPES = {"AVCaptureDeviceTypeDeskViewCamera"}
_CONTINUITY_NAME_HINTS = ("iphone", "ipad")
_BUILTIN_NAME_HINTS = ("macbook", "built-in", "facetime", "imac", "studio display")


@dataclass(frozen=True)
class AVFDevice:
    unique_id: str
    name: str
    device_type: str
    model_id: str
    kind: str  # built_in | continuity | external
    has_torch: bool  # identity oracle for hardware validation (iPhones have one)


def avfoundation_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import AVFoundation  # noqa: F401
        return True
    except Exception:
        return False


def classify_kind(device_type: str, model_id: str, name: str,
                  is_continuity: bool | None) -> str:
    """Layered classification, structured signals first, names LAST (the
    2026-07-12 lesson: never let a heuristic outrank device truth)."""
    if is_continuity:
        return "continuity"
    if device_type in _CONTINUITY_TYPES:
        return "continuity"
    if device_type.startswith(_BUILTIN_TYPE_PREFIX):
        return "built_in"
    lowered_model = model_id.lower()
    if lowered_model.startswith(("iphone", "ipad")):
        return "continuity"
    lowered_name = name.lower()
    if any(hint in lowered_name for hint in _CONTINUITY_NAME_HINTS):
        return "continuity"
    if any(hint in lowered_name for hint in _BUILTIN_NAME_HINTS):
        return "built_in"
    return "external"


def snapshot() -> list[AVFDevice] | None:
    """Every video AVCaptureDevice, Desk View pseudo-cameras filtered.
    None = AVFoundation unavailable (caller degrades honestly)."""
    if not avfoundation_available():
        return None
    import AVFoundation as AVF

    devices = []
    for device in AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo):
        device_type = str(device.deviceType() or "")
        name = str(device.localizedName() or "Camera")
        if device_type in _DESKVIEW_TYPES or "desk view" in name.lower():
            continue  # a synthetic top-down crop of another camera, not a device
        is_continuity = None
        if device.respondsToSelector_("isContinuityCamera"):
            is_continuity = bool(device.isContinuityCamera())
        model_id = str(device.modelID() or "")
        devices.append(AVFDevice(
            unique_id=str(device.uniqueID()),
            name=name,
            device_type=device_type,
            model_id=model_id,
            kind=classify_kind(device_type, model_id, name, is_continuity),
            has_torch=bool(device.hasTorch()) if device.respondsToSelector_("hasTorch") else False,
        ))
    return devices


def device_with_unique_id(unique_id: str):
    """The live AVCaptureDevice for a uniqueID, or None (left/stale)."""
    if not avfoundation_available():
        return None
    import AVFoundation as AVF

    return AVF.AVCaptureDevice.deviceWithUniqueID_(unique_id)
