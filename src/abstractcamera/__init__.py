"""abstractcamera: camera control abstractions for the Abstract ecosystem.

One thread-safe orchestrator (CameraManager) drives any camera family behind
a session protocol: tethered PTP bodies over libgphoto2 (Nikon Z and Sony
Alpha adapters, hardware-validated; generic PTP fallback), the machine's own
cameras (macOS AVFoundation webcams), and a scriptable simulator for
camera-less development and tests. Live view, honest config dials with a
write-verification ledger, single/burst/movie capture, focus actions, an
absolute-deadline intervalometer, live-view detection (lightning/meteor/
motion) with auto-fire, rolling pre-capture clips, and capture downloads.

The base install is lightweight (numpy + OpenCV); device transports are
explicit extras: abstractcamera[gphoto2] for tethered bodies,
abstractcamera[clips] for MP4 clip encoding, abstractcamera[raw] for RAW
thumbnails.
"""

from abstractcamera.camera_manager import CONFIG_WIDGET_NAMES, CameraManager
from abstractcamera.constants import ACTION_WIDGET_NAMES
from abstractcamera.discovery import is_tethering_available, list_cameras
from abstractcamera.errors import CameraControlError, CameraError
from abstractcamera.hub import CameraHub
from abstractcamera.jpeg import parse_jpeg_dimensions

# Back-compat alias for hosts that predate the extraction.
CameraController = CameraManager

__version__ = "0.1.0"
__author__ = "Laurent-Philippe Albou"
__email__ = "contact@abstractcore.ai"

__all__ = [
    "ACTION_WIDGET_NAMES",
    "CONFIG_WIDGET_NAMES",
    "CameraControlError",
    "CameraController",
    "CameraError",
    "CameraHub",
    "CameraManager",
    "get_default_manager",
    "is_tethering_available",
    "list_cameras",
    "parse_jpeg_dimensions",
    "__version__",
]

_default_manager: CameraManager | None = None


def get_default_manager() -> CameraManager:
    """Lazy process-wide manager with a best-effort clean release at exit:
    the worker is a daemon thread, so without this the camera could be left
    claimed (or recording) when the host process quits mid-session."""
    global _default_manager
    if _default_manager is None:
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
