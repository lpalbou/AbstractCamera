"""abstractcamera: camera control abstractions for the Abstract ecosystem.

One thread-safe orchestrator (CameraManager) drives any camera family behind
a session protocol: tethered PTP bodies over libgphoto2 (Nikon Z and Sony
Alpha adapters, hardware-validated; generic PTP fallback), the machine's own
cameras (macOS AVFoundation webcams), DWARF smart telescopes over Wi-Fi, and
a scriptable simulator for camera-less development and tests. Live view,
honest config dials with a write-verification ledger, single/burst/movie
capture, focus actions, an absolute-deadline intervalometer, live-view
detection (lightning/meteor/motion) with auto-fire, rolling pre-capture
clips, and capture downloads.

The base install is lightweight (numpy + OpenCV); device transports are
explicit extras: abstractcamera[gphoto2] for tethered bodies,
abstractcamera[clips] for MP4 clip encoding, abstractcamera[raw] for RAW
thumbnails.

IMPORT CONTRACT: `import abstractcamera` is LIGHT (PEP 562 lazy exports).
The camera stack (OpenCV/numpy via camera_manager) loads on first attribute
use, never at package import — the AbstractCore capability plugin is
imported by every core process that lists capabilities, camera user or not
(adversarial finding 2026-07-19), and it must not cost them cv2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.2.1"
__author__ = "Laurent-Philippe Albou"
__email__ = "contact@abstractcore.ai"

# Public name -> (module, attribute) for lazy resolution. CameraController
# is the back-compat alias for hosts that predate the extraction.
_LAZY_EXPORTS = {
    "CONFIG_WIDGET_NAMES": ("abstractcamera.camera_manager", "CONFIG_WIDGET_NAMES"),
    "CameraManager": ("abstractcamera.camera_manager", "CameraManager"),
    "CameraController": ("abstractcamera.camera_manager", "CameraManager"),
    "ACTION_WIDGET_NAMES": ("abstractcamera.constants", "ACTION_WIDGET_NAMES"),
    "is_tethering_available": ("abstractcamera.discovery", "is_tethering_available"),
    "list_cameras": ("abstractcamera.discovery", "list_cameras"),
    "CameraControlError": ("abstractcamera.errors", "CameraControlError"),
    "CameraError": ("abstractcamera.errors", "CameraError"),
    "CameraHub": ("abstractcamera.hub", "CameraHub"),
    "parse_jpeg_dimensions": ("abstractcamera.jpeg", "parse_jpeg_dimensions"),
    "DwarfAlbumMediaStore": ("abstractcamera.media_store", "DwarfAlbumMediaStore"),
    "FilesystemMediaStore": ("abstractcamera.media_store", "FilesystemMediaStore"),
    "MediaEntry": ("abstractcamera.media_store", "MediaEntry"),
    "find_card_volumes": ("abstractcamera.media_store", "find_card_volumes"),
    "SyncReport": ("abstractcamera.media_sync", "SyncReport"),
    "sync_store": ("abstractcamera.media_sync", "sync_store"),
}

__all__ = [
    *sorted(_LAZY_EXPORTS.keys()),
    "get_default_manager",
    "__version__",
]

if TYPE_CHECKING:  # pragma: no cover - static analysis only (re-export idiom)
    from abstractcamera.camera_manager import CONFIG_WIDGET_NAMES as CONFIG_WIDGET_NAMES
    from abstractcamera.camera_manager import (  # noqa: F401 - alias re-export, listed in __all__
        CameraManager as CameraController,
    )
    from abstractcamera.camera_manager import CameraManager as CameraManager
    from abstractcamera.constants import ACTION_WIDGET_NAMES as ACTION_WIDGET_NAMES
    from abstractcamera.discovery import is_tethering_available as is_tethering_available
    from abstractcamera.discovery import list_cameras as list_cameras
    from abstractcamera.errors import CameraControlError as CameraControlError
    from abstractcamera.errors import CameraError as CameraError
    from abstractcamera.hub import CameraHub as CameraHub
    from abstractcamera.jpeg import parse_jpeg_dimensions as parse_jpeg_dimensions
    from abstractcamera.media_store import DwarfAlbumMediaStore as DwarfAlbumMediaStore
    from abstractcamera.media_store import FilesystemMediaStore as FilesystemMediaStore
    from abstractcamera.media_store import MediaEntry as MediaEntry
    from abstractcamera.media_store import find_card_volumes as find_card_volumes
    from abstractcamera.media_sync import SyncReport as SyncReport
    from abstractcamera.media_sync import sync_store as sync_store


def __getattr__(name: str):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module 'abstractcamera' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(entry[0])
    value = getattr(module, entry[1])
    globals()[name] = value  # cache: next access skips __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))


_default_manager = None


def get_default_manager():
    """Lazy process-wide manager with a best-effort clean release at exit:
    the worker is a daemon thread, so without this the camera could be left
    claimed (or recording) when the host process quits mid-session."""
    global _default_manager
    if _default_manager is None:
        from abstractcamera.camera_manager import CameraManager

        _default_manager = CameraManager()

        import atexit

        def _release_at_exit() -> None:
            try:
                _default_manager._stop_requested.set()
                worker = _default_manager._worker
                if worker is not None and worker.is_alive():
                    worker.join(timeout=3.0)
            except Exception:
                pass

        atexit.register(_release_at_exit)
    return _default_manager
