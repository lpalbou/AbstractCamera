"""Camera discovery and driver resolution across transports (ADR 0006).

Resolution rules:
- ABSTRACTCAMERA_FAKE=1 replaces ALL transports with the simulator (exactly
  the semantics hosts had with their fake-camera env switches): tests and
  camera-less dev stay deterministic — no USB probing, no webcam LEDs.
- Otherwise: PTP (when python-gphoto2 is importable) + the platform webcam
  driver (when available). Listing is NON-INVASIVE: no device is opened, no
  camera LED lights up, no TCC permission prompt fires at list time.

Default camera when the host names none: the first PTP body (the historic
behavior) — else the built-in webcam, preferring "MacBook/Built-in" labels
over iPhone Continuity cameras (never silently pick the user's phone).

Resolution happens at call time, not import time (testable, and env changes
between connects are honored).
"""

from __future__ import annotations

import os

from abstractcamera.errors import CameraControlError


def fake_mode_active() -> bool:
    return os.environ.get("ABSTRACTCAMERA_FAKE") == "1"


def is_tethering_available() -> bool:
    """Back-compat, EXACT historic meaning: a gphoto2-shaped transport module
    resolves (real python-gphoto2, or the simulator in fake mode). Says
    nothing about webcams — general availability is status()['available']."""
    if fake_mode_active():
        return True
    from abstractcamera.drivers.gphoto2_driver import load_gphoto2_module

    return load_gphoto2_module() is not None


def resolve_drivers() -> list:
    """Active drivers in default-preference order."""
    if fake_mode_active():
        from abstractcamera.drivers.fake_driver import FakeDriver

        return [FakeDriver()]
    drivers = []
    from abstractcamera.drivers.gphoto2_driver import Gphoto2Driver

    ptp = Gphoto2Driver()
    if ptp.available():
        drivers.append(ptp)
    try:
        from abstractcamera.drivers.webcam_driver import WebcamDriver

        webcam = WebcamDriver()
        if webcam.available():
            drivers.append(webcam)
    except ImportError:
        pass
    # Network smart telescopes (DWARF): listed only when hosts are
    # configured (ABSTRACTCAMERA_DWARF_HOSTS) — nothing is probed here.
    from abstractcamera.drivers.dwarf_driver import DwarfDriver

    dwarf = DwarfDriver()
    if dwarf.available():
        drivers.append(dwarf)
    return drivers


def any_transport_available() -> bool:
    return bool(resolve_drivers())


def list_cameras() -> list[dict]:
    """Aggregate, non-invasive enumeration across active drivers. The first
    entry of the default driver is flagged default=True."""
    entries: list[dict] = []
    for driver in resolve_drivers():
        try:
            entries.extend(driver.list_cameras())
        except Exception:
            continue  # one broken transport must not hide the others
    for entry in entries:
        entry["default"] = False
    default = _default_entry(entries)
    if default is not None:
        default["default"] = True
    return entries


def _default_entry(entries: list[dict]) -> dict | None:
    for entry in entries:  # PTP/fake first — the historic behavior
        if entry["transport"] in ("ptp", "fake"):
            return entry
    webcams = [e for e in entries if e["transport"] == "webcam"]
    # Kind-ranked (structured field from the driver; name fallback for
    # drivers that predate it): the machine's own camera, then USB cameras,
    # and a wireless Continuity iPhone only as the last resort — connecting
    # someone's phone must be a CHOICE, not a default.
    def rank(entry: dict) -> int:
        kind = entry.get("kind")
        if kind is None:
            name = entry.get("name", "").lower()
            kind = ("continuity" if "iphone" in name or "ipad" in name
                    else "built_in" if "macbook" in name or "built-in" in name
                    else "external")
        return {"built_in": 0, "external": 1, "continuity": 2}.get(kind, 1)

    return min(webcams, key=rank) if webcams else None


def resolve_driver_for(camera_id: str | None):
    """Map a camera id (or None = default) to the driver that owns it."""
    drivers = resolve_drivers()
    if not drivers:
        raise CameraControlError(
            "No camera transport is available — install abstractcamera[gphoto2] "
            "for tethered bodies, or use a machine with a camera."
        )
    if camera_id is None:
        return drivers[0]
    prefix = camera_id.split(":", 1)[0]
    for driver in drivers:
        if driver.driver_id == prefix:
            return driver
    raise CameraControlError(
        f"No transport can open '{camera_id}' — the camera list may have "
        "changed; refresh and pick again."
    )
