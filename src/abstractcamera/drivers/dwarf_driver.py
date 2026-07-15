"""DWARF transport driver: network smart telescopes as a camera family.

Discovery is CONFIGURED, not scanned (ADR 0006 non-invasiveness applied to
a network transport): list_cameras() must stay fast and quiet for every
caller, so this driver lists exactly the hosts named by
ABSTRACTCAMERA_DWARF_HOSTS (comma-separated) or passed to the constructor.
No subnet sweeps, no TCP probes at list time — reachability is settled at
connect, with honest errors. `scripts/validate_dwarf.py` is the active
discovery tool (it sweeps and probes, as a deliberate act).

AP mode: joining the DWARF's own Wi-Fi puts the device at 192.168.88.1 —
    export ABSTRACTCAMERA_DWARF_HOSTS=192.168.88.1
STA mode: the DWARF joins your network (IP shown in the DWARFLAB app under
connection settings) — export that IP instead.
"""

from __future__ import annotations

from abstractcamera.errors import CameraControlError


class DwarfDriver:
    driver_id = "dwarf"

    def __init__(self, hosts: list[str] | None = None):
        self._hosts = hosts

    @property
    def transport(self):
        return None  # network sessions carry their own transport object

    def _configured_hosts(self) -> list[str]:
        if self._hosts is not None:
            return list(self._hosts)
        from abstractcamera.drivers.dwarf_transport import configured_hosts

        return configured_hosts()

    def available(self) -> bool:
        # Listable exactly when hosts are configured. The websocket extra is
        # checked at connect time (actionable install hint), not here — a
        # missing extra should not silently HIDE a configured telescope.
        return bool(self._configured_hosts())

    def list_cameras(self) -> list[dict]:
        entries: list[dict] = []
        for host in self._configured_hosts():
            entries.append({
                "id": f"dwarf:{host}",
                "transport": "dwarf",
                "name": f"DWARF 3 ({host})",
                "kind": "smart_telescope",
                # The name comes from configuration, not the device — the
                # device's word arrives at connect (get_abilities).
                "name_confidence": "configured",
                "note": ("smart telescope with a pilotable alt-az mount "
                         "(GOTO, joystick, calibration as camera actions)"),
            })
        return entries

    def prepare_connect(self, camera_id: str | None) -> None:
        pass  # nothing to release; never touch PTP daemons from this driver

    def create_session(self, camera_id: str | None):
        from abstractcamera.drivers.dwarf_session import DwarfSession

        hosts = self._configured_hosts()
        if camera_id is None:
            if not hosts:
                raise CameraControlError(
                    "No DWARF is configured — set ABSTRACTCAMERA_DWARF_HOSTS "
                    "to the telescope's IP (192.168.88.1 in AP mode).")
            host = hosts[0]
        else:
            host = camera_id.split(":", 1)[1] if ":" in camera_id else camera_id
            if not host:
                raise CameraControlError(
                    f"'{camera_id}' names no DWARF host — expected 'dwarf:<ip>'.")
        return DwarfSession(host)

    def select_adapter(self, model: str | None):
        from abstractcamera.adapters.dwarf import DwarfAdapter

        return DwarfAdapter(None)
