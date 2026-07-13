"""Webcam transport driver (macOS AVFoundation, native).

Enumeration is NON-INVASIVE (ADR 0006): no device is opened at list time —
opening lights the camera LED, can fire the TCC permission prompt, and can
contend with whatever app owns the device.

Identity (ADR 0009, after the 2026-07-12 inversion): ids are
`webcam:<AVCaptureDevice uniqueID>` and the session opens THAT device
object — no positional index space exists between listing and opening.
Names come from the same device objects (`localizedName`), so
name_confidence is `reported`. Kind classification is structured
(deviceType/isContinuityCamera/modelID), with names only as the last
fallback. iPhone Continuity cameras are labeled and never become the
default.
"""

from __future__ import annotations

import platform

from abstractcamera.errors import CameraControlError


class WebcamDriver:
    driver_id = "webcam"

    def __init__(self):
        self._is_macos = platform.system() == "Darwin"

    @property
    def transport(self):
        return None  # the session IS the device; no transport module exists

    def available(self) -> bool:
        if not self._is_macos:
            return False  # v4l2/Windows enumeration is future work — honest
        try:
            import cv2  # noqa: F401  (JPEG encoding)
        except ImportError:
            return False
        from abstractcamera.drivers.avf_enum import avfoundation_available

        return avfoundation_available()

    def list_cameras(self) -> list[dict]:
        if not self.available():
            return []
        from abstractcamera.drivers.avf_enum import snapshot

        devices = snapshot()
        if devices is None:
            return []
        entries: list[dict] = []
        for device in devices:
            entries.append({
                "id": f"webcam:{device.unique_id}",
                "transport": "webcam",
                "name": device.name,
                "kind": device.kind,
                "name_confidence": "reported",
                "note": (
                    "a nearby iPhone/iPad exposed wirelessly by macOS Continuity Camera "
                    "(same Apple ID, no cable)" if device.kind == "continuity"
                    else "identity is the AVFoundation uniqueID — the session opens this exact device"
                ),
            })
        # Stable presentation order inside the transport: the machine's own
        # camera first, USB next, wireless Continuity devices LAST.
        rank = {"built_in": 0, "external": 1, "continuity": 2}
        entries.sort(key=lambda e: rank.get(e.get("kind"), 1))
        return entries

    def prepare_connect(self, camera_id: str | None) -> None:
        pass  # nothing to release; never touch PTP daemons from this driver

    def create_session(self, camera_id: str | None):
        from abstractcamera.drivers.webcam_session import WebcamSession

        entries = self.list_cameras()
        if not entries:
            raise CameraControlError("No webcam is available on this machine.")
        if camera_id is None:
            # list_cameras() is kind-ranked (built-in first, Continuity
            # last): the default is never silently someone's phone.
            chosen = entries[0]
        else:
            matches = [e for e in entries if e["id"] == camera_id]
            if not matches:
                suffix = camera_id.split(":", 1)[1] if ":" in camera_id else camera_id
                if suffix.isdigit():
                    # Pre-ADR-0009 positional id: mapping it to a position
                    # would resurrect the inversion bug. Refuse, one time.
                    raise CameraControlError(
                        "Camera ids changed from positions to device identities "
                        "(they can no longer point at the wrong camera) — "
                        "refresh the camera list and pick again."
                    )
                raise CameraControlError(
                    f"No camera is present at '{camera_id}' — the camera list "
                    "changed; refresh and pick again."
                )
            chosen = matches[0]
        return WebcamSession(unique_id=chosen["id"].split(":", 1)[1],
                             label=chosen["name"])

    def select_adapter(self, model: str | None):
        from abstractcamera.adapters.webcam import WebcamAdapter

        adapter = WebcamAdapter(None)
        if model and model.lower().startswith("webcam:"):
            adapter.display_name = model.split(":", 1)[1].strip()
        return adapter
