"""AVFFrameSource: native AVFoundation capture, opened by uniqueID.

The device OBJECT we enumerated is the capture target — no index space, so
the 2026-07-12 name/device inversion is impossible by construction
(ADR 0009). Replaces cv2.VideoCapture for the webcam family; OpenCV stays
for JPEG encoding only.

Threading model (deadlock-freedom by construction):
- All lifecycle + read calls come from the manager's worker thread (the
  session contract); the delegate runs on a private SERIAL dispatch queue.
- The delegate does the minimum and takes ONE lock (the frame Condition):
  lock pixel buffer -> one strided copy -> publish (array, seq) -> notify.
  CVPixelBuffers belong to AVFoundation's pool and are recycled after the
  callback returns — the copy is mandatory, never retained.
- The worker never executes on the dispatch queue; stopRunning is only
  ever called from the worker (calling it on the delegate queue is the
  classic AVF deadlock).

Measured on this hardware (2026-07-12): first frame ~0.6s after
startRunning, 1080p BGRA, device-reported format list includes portrait
and square Center-Stage variants (filtered to landscape video formats).
"""

from __future__ import annotations

import threading
import time

import numpy as np

from abstractcamera.errors import CameraControlError

FIRST_FRAME_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 0.75
_delegate_class_cache: list = []
_photo_delegate_class_cache: list = []


def _delegate_class():
    """The NSObject delegate subclass, created lazily (PyObjC import) and
    exactly once per process (re-declaring an ObjC class name raises)."""
    if _delegate_class_cache:
        return _delegate_class_cache[0]
    import CoreMedia as CM
    import Quartz
    from Foundation import NSObject

    class _AbstractCameraFrameDelegate(NSObject):
        def initWithSink_(self, sink):
            self = objc_super_init(self)
            if self is None:
                return None
            self._sink = sink
            return self

        def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
            try:
                pixel_buffer = CM.CMSampleBufferGetImageBuffer(sample_buffer)
                if pixel_buffer is None:
                    return
                Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
                try:
                    height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
                    width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
                    bytes_per_row = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
                    base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
                    # Rows may be padded: reshape by stride, slice to width,
                    # drop alpha. BGRA -> BGR keeps the numpy pipeline as-is.
                    array = np.frombuffer(base.as_buffer(bytes_per_row * height),
                                          dtype=np.uint8)
                    array = array.reshape(height, bytes_per_row // 4, 4)[:, :width, :3].copy()
                finally:
                    Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
                self._sink(array)
            except Exception:
                # A raising delegate would spam the dispatch queue; frame
                # loss is visible downstream as read() timeouts (honest).
                pass

    def objc_super_init(instance):
        import objc

        return objc.super(_AbstractCameraFrameDelegate, instance).init()

    _delegate_class_cache.append(_AbstractCameraFrameDelegate)
    return _AbstractCameraFrameDelegate


class FlashPhotoTicket:
    """One in-flight flash photo. `done` is set by the delegate; `photo`
    carries the JPEG when macOS delivered it (delivery rides the MAIN run
    loop — pumped in GUI apps, absent in bare scripts; callers must treat
    a missing photo as normal and fall back to the flash-lit stream)."""

    def __init__(self):
        self.done = threading.Event()
        self.photo: bytes | None = None
        self.error: str | None = None


def _photo_delegate_class():
    if _photo_delegate_class_cache:
        return _photo_delegate_class_cache[0]
    import objc
    from Foundation import NSObject

    protocol = objc.protocolNamed("AVCapturePhotoCaptureDelegate")

    class _AbstractCameraPhotoDelegate(NSObject, protocols=[protocol]):
        def initWithTicket_(self, ticket):
            self = objc.super(_AbstractCameraPhotoDelegate, self).init()
            if self is None:
                return None
            self._ticket = ticket
            return self

        def captureOutput_didFinishProcessingPhoto_error_(self, output, photo, error):
            ticket = self._ticket
            try:
                if error is not None:
                    ticket.error = str(error)
                elif photo is not None:
                    data = photo.fileDataRepresentation()
                    ticket.photo = bytes(data) if data is not None else None
            finally:
                ticket.done.set()

    _photo_delegate_class_cache.append(_AbstractCameraPhotoDelegate)
    return _AbstractCameraPhotoDelegate


def _authorization_error() -> str | None:
    """Deterministic TCC diagnosis BEFORE opening (denied used to surface as
    a mute 3s frame timeout)."""
    import AVFoundation as AVF

    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if status in (1, 2):  # restricted, denied
        return ("macOS has denied camera access for this app — allow it in "
                "System Settings → Privacy & Security → Camera, then reconnect.")
    return None  # authorized (3) or notDetermined (0: the prompt fires at start)


class AVFFrameSource:
    """Frames from one AVCaptureDevice, addressed by uniqueID.

    Surface (worker-thread-only): open() / read() / format_dims() /
    set_dims() / close() / torch() + `lost`, `unique_id`. This is also the
    test seam: FakeFrameSource implements the same surface in pure numpy.
    """

    def __init__(self, unique_id: str, label: str):
        self.unique_id = unique_id
        self.lost = False
        self._label = label
        self._device = None
        self._session = None
        self._output = None
        self._delegate = None
        self._queue = None
        self._photo_output = None
        self._photo_delegate = None  # kept alive for the in-flight capture
        self._condition = threading.Condition()
        self._latest_frame: np.ndarray | None = None
        self._sequence = 0
        self._consumed_sequence = 0

    # -- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        import AVFoundation as AVF
        import Quartz
        from libdispatch import dispatch_queue_create

        from abstractcamera.drivers.avf_enum import device_with_unique_id

        denied = _authorization_error()
        if denied:
            raise CameraControlError(denied)
        device = device_with_unique_id(self.unique_id)
        if device is None:
            raise CameraControlError(
                f"'{self._label}' is no longer present — the camera list "
                "changed (Continuity cameras come and go); refresh and pick again."
            )
        self._device = device

        session = AVF.AVCaptureSession.alloc().init()
        device_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if device_input is None:
            raise CameraControlError(
                f"'{self._label}' refused to open ({error}) — it may be held "
                "exclusively by another app."
            )
        session.addInput_(device_input)

        output = AVF.AVCaptureVideoDataOutput.alloc().init()
        output.setAlwaysDiscardsLateVideoFrames_(True)  # latest-frame semantics
        output.setVideoSettings_(
            {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA})
        delegate = _delegate_class().alloc().initWithSink_(self._publish)
        queue = dispatch_queue_create(b"abstractcamera.webcam.frames", None)
        output.setSampleBufferDelegate_queue_(delegate, queue)
        session.addOutput_(output)

        session.startRunning()
        self._session, self._output = session, output
        self._delegate, self._queue = delegate, queue

        deadline = time.time() + FIRST_FRAME_TIMEOUT_S
        with self._condition:
            if not self._condition.wait_for(lambda: self._sequence > 0,
                                            timeout=FIRST_FRAME_TIMEOUT_S):
                pass
        if self._sequence == 0 and time.time() >= deadline:
            self.close()
            raise CameraControlError(
                f"No frames arrived from '{self._label}' — if macOS just showed "
                "a camera permission prompt, answer it and reconnect; otherwise "
                "another app may hold the device."
            )

    def _publish(self, array: np.ndarray) -> None:
        with self._condition:
            self._latest_frame = array
            self._sequence += 1
            self._condition.notify_all()

    def close(self) -> None:
        # Order matters (worker thread only): stop the graph, break the
        # delegate retain cycle, then unblock any pending read.
        if self._session is not None:
            try:
                self._session.stopRunning()
            except Exception:
                pass
        if self._output is not None:
            try:
                self._output.setSampleBufferDelegate_queue_(None, None)
            except Exception:
                pass
        self._session = None
        self._output = None
        self._delegate = None
        self._queue = None
        self._photo_output = None
        self._photo_delegate = None
        self._device = None
        with self._condition:
            self.lost = True
            self._condition.notify_all()

    # -- flash (Continuity iPhones report hasFlash; measured firing 2026-07-13) ----
    def has_flash(self) -> bool:
        device = self._device
        try:
            return bool(device is not None and device.hasFlash())
        except Exception:
            return False

    def fire_flash_photo(self, mode: str) -> FlashPhotoTicket:
        """Fire a photo capture with the flash ('on'|'auto') through an
        AVCapturePhotoOutput added to the live session.

        Threading truth (measured 2026-07-13): the photo delegate delivers
        on the thread whose run loop hosted the INITIATION — initiating
        from a bare worker thread means delivery NEVER arrives. The
        initiation is therefore dispatched onto the MAIN queue (alive in
        every GUI host; the packaged app pumps it). Headless embedders
        without a main loop get the documented fallback: the ticket times
        out and the caller uses the flash-lit stream frame."""
        import AVFoundation as AVF
        from libdispatch import dispatch_async, dispatch_get_main_queue

        if not self.has_flash():
            raise CameraControlError(f"'{self._label}' has no flash.")
        if self._photo_output is None:
            photo_output = AVF.AVCapturePhotoOutput.alloc().init()
            self._session.beginConfiguration()
            if not self._session.canAddOutput_(photo_output):
                self._session.commitConfiguration()
                raise CameraControlError(
                    f"'{self._label}' refused a photo output on the live session.")
            self._session.addOutput_(photo_output)
            self._session.commitConfiguration()
            self._photo_output = photo_output
            time.sleep(0.3)  # output graph settles (measured sufficient)

        ticket = FlashPhotoTicket()
        delegate = _photo_delegate_class().alloc().initWithTicket_(ticket)
        self._photo_delegate = delegate  # keep alive until delivery
        photo_output = self._photo_output

        def initiate():
            try:
                settings = AVF.AVCapturePhotoSettings.photoSettings()
                settings.setFlashMode_(1 if mode == "on" else 2)  # AVCaptureFlashMode
                photo_output.capturePhotoWithSettings_delegate_(settings, delegate)
            except Exception as exc:
                ticket.error = str(exc)
                ticket.done.set()

        dispatch_async(dispatch_get_main_queue(), initiate)
        return ticket

    # -- frames ---------------------------------------------------------------------
    def read(self, timeout_s: float = READ_TIMEOUT_S) -> np.ndarray:
        """The NEXT frame (blocks ~one frame interval — the session contract's
        pacing behavior). Raises on loss/closure; the manager's watchdog
        counts those raises exactly as it did for cv2 read failures."""
        if self._session is None:
            raise CameraControlError("The camera session is closed.")
        with self._condition:
            got = self._condition.wait_for(
                lambda: self._sequence > self._consumed_sequence or self.lost,
                timeout=timeout_s)
            if self.lost:
                raise CameraControlError(f"'{self._label}' session was closed.")
            if got:
                self._consumed_sequence = self._sequence
                return self._latest_frame
        # No frame inside the window: distinguish walkaway from a hiccup.
        if self._device is not None and not bool(self._device.isConnected()):
            self.lost = True
            raise CameraControlError(
                f"'{self._label}' left (Continuity cameras disconnect when the "
                "phone moves away or locks)."
            )
        raise CameraControlError(f"'{self._label}' stopped delivering frames.")

    # -- formats ---------------------------------------------------------------------
    def format_dims(self) -> list[tuple[int, int]]:
        """Device-REPORTED landscape video dimensions (no probe-by-trial).
        Portrait/square Center-Stage variants are filtered."""
        import CoreMedia as CM

        dims: list[tuple[int, int]] = []
        for fmt in self._device.formats():
            try:
                if not fmt.videoSupportedFrameRateRanges():
                    continue  # photo-only formats (Continuity lists some)
                dimensions = CM.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
                if dimensions.width >= dimensions.height:  # landscape video only
                    dims.append((int(dimensions.width), int(dimensions.height)))
            except Exception:
                continue
        seen = []
        for entry in dims:
            if entry not in seen:
                seen.append(entry)
        return seen

    def current_dims(self) -> tuple[int, int]:
        import CoreMedia as CM

        dimensions = CM.CMVideoFormatDescriptionGetDimensions(
            self._device.activeFormat().formatDescription())
        return int(dimensions.width), int(dimensions.height)

    # -- zoom (the ONE manual control macOS grants: a digital crop) -----------------
    # Measured 2026-07-12 on both machines: every manual-exposure/focus/WB
    # AVCaptureDevice API reports unsupported on macOS (iOS-only); only
    # videoZoomFactor is accepted (1..16, readback-confirmed).
    def zoom_range(self) -> tuple[float, float]:
        device = self._device
        try:
            return (float(device.minAvailableVideoZoomFactor()),
                    float(device.maxAvailableVideoZoomFactor()))
        except Exception:
            return (1.0, 1.0)

    def zoom(self) -> float:
        try:
            return float(self._device.videoZoomFactor())
        except Exception:
            return 1.0

    def set_zoom(self, factor: float) -> None:
        device = self._device
        low, high = self.zoom_range()
        factor = max(low, min(high, float(factor)))
        ok = device.lockForConfiguration_(None)
        if isinstance(ok, tuple):
            ok = ok[0]
        if not ok:
            raise CameraControlError("The camera refused a configuration lock.")
        try:
            device.setVideoZoomFactor_(factor)
        finally:
            device.unlockForConfiguration()

    def set_dims(self, width: int, height: int) -> None:
        """activeFormat switch (not sessionPreset: presets silently reset
        activeFormat and hide the output size). Confirmation is read()'s
        job — in-flight frames at the old size are expected briefly."""
        import CoreMedia as CM

        target = None
        for fmt in self._device.formats():
            try:
                if not fmt.videoSupportedFrameRateRanges():
                    continue
                dimensions = CM.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
                if (int(dimensions.width), int(dimensions.height)) == (width, height):
                    target = fmt
                    break
            except Exception:
                continue
        if target is None:
            raise CameraControlError(f"Unsupported resolution: {width}x{height}")
        ok = self._device.lockForConfiguration_(None)
        if isinstance(ok, tuple):  # PyObjC returns (bool, error) for by-ref
            ok = ok[0]
        if not ok:
            raise CameraControlError("The camera refused a configuration lock.")
        try:
            self._device.setActiveFormat_(target)
        finally:
            self._device.unlockForConfiguration()

