"""Transport drivers: who creates camera sessions (ADR 0001/0006).

A Driver owns one transport (PTP via gphoto2, the package simulator, the
AVFoundation webcam): it enumerates cameras, prepares the transport for a
connect (e.g. releasing macOS PTP daemons — a PTP-only concern), creates
CameraSession objects, and selects the family adapter for a detected model.
The manager never imports a transport library itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from abstractcamera.session import CameraSession


@runtime_checkable
class Driver(Protocol):
    driver_id: str  # "ptp" | "fake" | "webcam"

    def available(self) -> bool: ...

    def list_cameras(self) -> list[dict]: ...

    def prepare_connect(self, camera_id: str | None) -> None: ...

    def create_session(self, camera_id: str | None) -> CameraSession: ...

    def select_adapter(self, model: str | None): ...
