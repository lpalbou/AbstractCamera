"""CameraHub: pilot several cameras at the same time.

Each connected camera gets its OWN CameraManager (and therefore its own
worker thread — the one-thread-owns-all-I/O rule is per camera, which is
exactly libgphoto2's thread-safety model). The hub owns:

- the registry of live managers keyed by device uid (the device slug, with
  a serial/index suffix when two identical bodies are connected),
- an ACTIVE selection (hosts with a single control panel bind it to the
  camera being configured; every other camera keeps running — sequences,
  detection, recordings continue in their own workers),
- shared manager configuration (capture root, frame analyzer) applied to
  every new connection.

Discovery entries are annotated with the live state so a host can render a
device list with one call.
"""

from __future__ import annotations

import threading

from abstractcamera import discovery
from abstractcamera.camera_manager import CameraManager
from abstractcamera.errors import CameraControlError


class CameraHub:
    def __init__(self, capture_root: str | None = None, manager_factory=None):
        self._lock = threading.Lock()
        self._managers: dict[str, CameraManager] = {}
        self._active_uid: str | None = None
        self._capture_root = capture_root
        self._frame_analyzer = None
        self._manager_factory = manager_factory or CameraManager
        self._list_fn = None  # test seam; None = discovery.list_cameras

    # -- shared manager configuration ---------------------------------------
    def configure_managers(self, *, capture_root: str | None = None,
                           frame_analyzer=None, list_fn=None) -> None:
        if capture_root is not None:
            self._capture_root = capture_root
        if frame_analyzer is not None:
            self._frame_analyzer = frame_analyzer
        if list_fn is not None:
            self._list_fn = list_fn

    # -- discovery + live state -----------------------------------------------
    def list_cameras(self) -> list[dict]:
        """Discovery entries annotated with connection state: `connected`,
        `device_uid` (when live), and `active`."""
        entries = (self._list_fn or discovery.list_cameras)()
        return self.annotate_entries(entries)

    def annotate_entries(self, entries: list[dict]) -> list[dict]:
        """(Re)annotate discovery entries with CURRENT live state. Split out
        so callers that cache the expensive USB probe can still serve fresh
        connected/active flags (probe snapshots age well; state must not)."""
        with self._lock:
            live_by_camera_id = {
                manager.status()["camera_id"]: (uid, manager)
                for uid, manager in self._managers.items()
                if manager.status()["connected"]
            }
            active_uid = self._active_uid
        for entry in entries:
            live = live_by_camera_id.get(entry["id"])
            entry["connected"] = live is not None
            entry["device_uid"] = live[0] if live else None
            entry["active"] = bool(live and live[0] == active_uid)
        return entries

    def statuses(self) -> dict[str, dict]:
        """device_uid -> status() for every live manager (+ `active` flag)."""
        with self._lock:
            managers = dict(self._managers)
            active_uid = self._active_uid
        out: dict[str, dict] = {}
        for uid, manager in managers.items():
            status = manager.status()
            status["device_uid"] = uid
            status["active"] = uid == active_uid
            out[uid] = status
        return out

    @property
    def active_uid(self) -> str | None:
        return self._active_uid

    def available(self) -> bool:
        return discovery.any_transport_available() or bool(self._managers)

    # -- connection lifecycle -----------------------------------------------------
    def connect(self, camera_id: str | None = None) -> dict:
        """Connect a camera (default resolution rules apply when camera_id is
        None) and make it the ACTIVE one. Reconnecting an already-live
        camera_id returns its existing session."""
        with self._lock:
            for uid, manager in self._managers.items():
                status = manager.status()
                if status["connected"] and camera_id is not None \
                        and status["camera_id"] == camera_id:
                    self._active_uid = uid
                    status["device_uid"] = uid
                    status["active"] = True
                    return status

        manager = self._manager_factory()
        if self._capture_root:
            manager.set_capture_root(self._capture_root)
        if self._frame_analyzer is not None:
            manager.set_frame_analyzer(self._frame_analyzer)
        manager.connect(camera_id)

        with self._lock:
            uid = self._register_locked(manager)
            self._active_uid = uid
        status = manager.status()
        status["device_uid"] = uid
        status["active"] = True
        return status

    def _register_locked(self, manager: CameraManager) -> str:
        """Final device uid: the manager's slug, suffixed by serial tail or
        index when an identical body is already live (two Z6 IIs...)."""
        status = manager.status()
        base = status["device_slug"] or "camera"
        uid = base
        if uid in self._managers:
            serial = status.get("device_serial")
            if serial:
                uid = f"{base}_{serial[-4:].lower()}"
        counter = 2
        while uid in self._managers:
            uid = f"{base}_{counter}"
            counter += 1
        if uid != base:
            manager.set_device_slug(uid)  # capture folder follows the uid
        self._managers[uid] = manager
        return uid

    def manager_for(self, device_uid: str | None = None) -> CameraManager:
        """The addressed manager, or the ACTIVE one when device_uid is None."""
        with self._lock:
            uid = device_uid or self._active_uid
            manager = self._managers.get(uid) if uid else None
        if manager is None:
            raise CameraControlError(
                "No camera is connected." if device_uid is None
                else f"No connected camera has id '{device_uid}' — refresh the list."
            )
        return manager

    def select(self, device_uid: str) -> dict:
        manager = self.manager_for(device_uid)
        with self._lock:
            self._active_uid = device_uid
        status = manager.status()
        status["device_uid"] = device_uid
        status["active"] = True
        return status

    def disconnect(self, device_uid: str | None = None) -> dict:
        with self._lock:
            uid = device_uid or self._active_uid
            manager = self._managers.pop(uid, None) if uid else None
            if self._active_uid == uid:
                # Fall back to any remaining live camera.
                self._active_uid = next(iter(self._managers), None)
        if manager is None:
            raise CameraControlError("No camera is connected.")
        return manager.disconnect()

    def disconnect_all(self) -> None:
        with self._lock:
            managers = list(self._managers.values())
            self._managers.clear()
            self._active_uid = None
        for manager in managers:
            try:
                manager.disconnect()
            except Exception:
                pass
