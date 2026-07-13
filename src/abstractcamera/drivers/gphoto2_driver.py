"""PTP transport driver: real libgphoto2 sessions.

Owns everything gphoto2-specific that used to live in the host controller:
the lazy module import, autodetect, the macOS PTP-daemon release before a
claim, session creation with optional port binding (targeting one of several
connected bodies), and family-adapter selection for detected models.
"""

from __future__ import annotations

import time

from abstractcamera.errors import CameraControlError


def load_gphoto2_module():
    """Lazy import: [gphoto2] is an extra; absence is a normal, honest state."""
    try:
        import gphoto2 as gp
        return gp
    except ImportError:
        return None


class Gphoto2Driver:
    driver_id = "ptp"

    def __init__(self, gp_module=None):
        self._gp = gp_module if gp_module is not None else load_gphoto2_module()

    @property
    def transport(self):
        return self._gp

    def available(self) -> bool:
        return self._gp is not None

    def list_cameras(self) -> list[dict]:
        if self._gp is None:
            return []
        entries: list[dict] = []
        camera_list = self._gp.Camera.autodetect()
        seen_names = set()
        for index in range(camera_list.count()):
            name = camera_list.get_name(index)
            address = camera_list.get_value(index)
            if name in seen_names:
                continue
            seen_names.add(name)
            entries.append({
                "id": f"ptp:{address}",
                "transport": "ptp",
                "name": str(name),
                "address": str(address),
                # The camera reports its own model string over PTP.
                "name_confidence": "reported",
                # USB addresses are stable while plugged, NOT across replugs:
                # hosts should re-list before connect.
            })
        return entries

    def prepare_connect(self, camera_id: str | None) -> None:
        """Release macOS's own PTP daemons before claiming the camera.

        ptpcamerad/mscamerad grab every PTP device on plug-in (for Photos and
        Image Capture) and cause GP_ERROR ([-53] Could not claim the USB
        device). They respawn on demand, so killing them is the standard and
        safe gphoto2-on-macOS workaround. PTP-transport-specific: the webcam
        driver must never do this.
        """
        import subprocess

        killed_any = False
        for daemon in ("ptpcamerad", "mscamerad-xpc", "mscamerad"):
            try:
                result = subprocess.run(["killall", daemon], capture_output=True, timeout=5)
                killed_any = killed_any or result.returncode == 0
            except Exception:
                pass
        if killed_any:
            # Let the daemon actually release the USB interface: init() while
            # it is mid-teardown can crash deep in libgphoto2 (observed as a
            # SIGSEGV on connect with the Sony A7R IV, 2026-07-12).
            time.sleep(0.5)

    def create_session(self, camera_id: str | None):
        gp = self._gp
        if gp is None:
            raise CameraControlError(
                "Tethering support is not installed (python-gphoto2 missing — "
                "pip install abstractcamera[gphoto2])."
            )
        camera = gp.Camera()
        if camera_id:
            address = camera_id.split(":", 1)[1] if camera_id.startswith("ptp:") else camera_id
            self._bind_port(camera, address)
        return camera

    def _bind_port(self, camera, address: str) -> None:
        """Target a specific body when several are connected. A stale address
        (replug renumbering) refuses honestly instead of silently opening a
        different camera."""
        gp = self._gp
        detected = gp.Camera.autodetect()
        addresses = [detected.get_value(i) for i in range(detected.count())]
        if address not in addresses:
            raise CameraControlError(
                f"No camera is present at {address} — the camera list changed; "
                "refresh the list and pick again."
            )
        port_list = gp.PortInfoList()
        port_list.load()
        index = port_list.lookup_path(address)
        camera.set_port_info(port_list[index])

    def select_adapter(self, model: str | None):
        from abstractcamera.adapters import select_adapter

        return select_adapter(model, self._gp)
