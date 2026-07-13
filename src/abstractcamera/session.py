"""The CameraSession protocol (ADR 0001): what every transport implements.

The protocol is not an invention — it is the enumeration of what the
hardware-validated manager loop already speaks. Three implementations
satisfy it today:

- the real ``gphoto2.Camera`` (structural typing, zero wrapping — critical
  because capture_preview runs 26-60x per second),
- ``abstractcamera.sim.gphoto2.Camera`` (the simulator, both Nikon Z6 II and
  Sony A7R IV personalities),
- ``abstractcamera.drivers.webcam_session.WebcamSession`` (AVFoundation).

BEHAVIORAL CONTRACT (executable in tests/test_session_protocol.py — these
semantics are load-bearing, not stylistic):

1. ``wait_for_event(timeout_ms)`` returns ``(GP_EVENT_TIMEOUT, None)`` when
   idle; it NEVER raises for an empty queue.
2. ``capture_preview()`` RAISES while the body cannot serve frames (Nikon
   during exposures, pulled USB, closed webcam) — the manager's liveness
   watchdog counts raises; a session that politely returns stale frames
   would silently rewire the watchdog and the pause logic.
3. ``capture_preview()`` blocks roughly one frame interval — the call paces
   the worker loop at the camera's real rate.
4. A capture announces itself as ``GP_EVENT_FILE_ADDED`` with an EventData
   whose (folder, name) can be passed to ``file_get`` to obtain the bytes.
5. ``init()`` raises with honest, user-actionable text when the device
   cannot be claimed.
6. Single-config widget I/O is OPTIONAL and feature-detected via hasattr
   (exactly how the adapters probe it); sessions without widgets simply
   don't implement it.

THREADING: every method is called from the manager's single worker thread
only. Sessions must not require other threads to call in; internal helper
threads (e.g. a movie encoder) are session-private and must never touch the
capture handle used by the worker calls.

Lifecycle ownership: the manager creates sessions through a Driver, calls
init()/exit(), and runs wedge recovery (exit → init) itself. Sessions never
self-destruct.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from abstractcamera.wire import EventData


@runtime_checkable
class CameraSession(Protocol):
    # -- lifecycle (worker thread only) --
    def init(self) -> None: ...

    def exit(self) -> None: ...

    def get_abilities(self) -> Any: ...  # .model: str

    # -- streaming / capture --
    def capture_preview(self) -> Any: ...  # CameraFile-shaped (.get_data_and_size)

    def trigger_capture(self) -> None: ...

    # -- events / files --
    def wait_for_event(self, timeout_ms: int) -> tuple[int, EventData | None]: ...

    def file_get(self, folder: str, name: str, file_type: int) -> Any: ...  # CameraFile-shaped
