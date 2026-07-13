"""WebcamSession: the machine's own camera speaking the CameraSession
protocol, captured natively from AVFoundation by device uniqueID
(ADR 0009 — no positional index space, so names cannot invert; OpenCV
remains for JPEG encoding only).

Hardware facts measured on a MacBook Pro (2026-07-12): 1920x1080 native,
first frame ~0.6s after session start (plus a one-time macOS camera-
permission prompt per host process), device-REPORTED format lists
(landscape video formats: 1080p/720p/480p on the built-in camera), and NO
manual exposure/gain/WB/focus control. The capability surface is honest
about all of it: one real dial (imagesize), stills that ARE video frames,
and movie recording that is genuinely confirmable because this process
writes the file itself.

Threading: all protocol methods run on the manager's worker thread (the
session contract); AVFoundation's delegate queue is private to the frame
source; the movie encoder is the one session-private thread and consumes a
bounded frame queue — neither ever touches protocol state.
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
from collections import deque

import cv2

from abstractcamera import wire
from abstractcamera.errors import CameraControlError

PREVIEW_JPEG_QUALITY = 85
STILL_JPEG_QUALITY = 95
# Presented resolution choices = device-reported dims ∩ this ladder, plus
# the native format — familiar sizes only, nothing fabricated.
RESOLUTION_LADDER = ((3840, 2160), (1920, 1080), (1280, 720), (640, 480))
MOVIE_QUEUE_FRAMES = 90  # ~3s at 30fps; overflow drops oldest and is counted
RESOLUTION_SETTLE_S = 1.5  # in-flight frames at the old size drain quickly


class _MovieRecorder:
    """Session-private H.264 encoder thread (PyAV). Frames are teed into a
    bounded queue by capture_preview; encode never stalls the preview."""

    def __init__(self, path: str, width: int, height: int, fps: float):
        import av

        self._path = path
        self._queue: queue.Queue = queue.Queue(maxsize=MOVIE_QUEUE_FRAMES)
        self._dropped = 0
        self._frames_written = 0
        self._stop = threading.Event()
        width -= width % 2
        height -= height % 2
        self._container = av.open(path, "w")
        self._stream = self._container.add_stream("h264", rate=max(1, int(round(fps))))
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = "yuv420p"
        self._stream.options = {"crf": "20", "preset": "veryfast"}
        self._thread = threading.Thread(target=self._run, name="webcam-movie-encoder",
                                        daemon=True)
        self._thread.start()

    def offer(self, frame_bgr) -> None:
        try:
            self._queue.put_nowait(frame_bgr)
        except queue.Full:
            # Drop-oldest keeps the encoder from stalling the preview loop.
            try:
                self._queue.get_nowait()
                self._dropped += 1
                self._queue.put_nowait(frame_bgr)
            except queue.Empty:
                pass

    def _run(self) -> None:
        import av

        while not (self._stop.is_set() and self._queue.empty()):
            try:
                frame_bgr = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            height, width = self._stream.height, self._stream.width
            video_frame = av.VideoFrame.from_ndarray(frame_bgr[:height, :width], format="bgr24")
            for packet in self._stream.encode(video_frame):
                self._container.mux(packet)
            self._frames_written += 1

    def finish(self, timeout_s: float = 3.0) -> tuple[str, int, int]:
        """Stop, flush (bounded), close. Returns (path, frames, dropped)."""
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        try:
            for packet in self._stream.encode():
                self._container.mux(packet)
            self._container.close()
        except Exception:
            pass
        return self._path, self._frames_written, self._dropped


class WebcamSession:
    """One local camera, addressed by AVFoundation uniqueID.

    source_factory is the test seam: FakeFrameSource implements the frame
    source surface (open/read/format_dims/current_dims/set_dims/close,
    `lost`, `unique_id`) in pure numpy — the whole suite runs camera-less.
    """

    def __init__(self, unique_id: str, label: str, source_factory=None):
        self.unique_id = unique_id
        self._label = label
        self._source_factory = source_factory or self._native_source
        self._source = None
        self._events: deque = deque()
        self._objects: dict[str, object] = {}  # name -> CameraFile-shaped
        self._sequence = 0
        self._resolution_choices: list[str] = []
        self._current_resolution = ""
        self._measured_fps = 30.0
        self._recorder: _MovieRecorder | None = None
        self._movie_tee_enabled = False
        self._flash_mode = "off"  # armed per-capture; only offered when hasFlash

    @staticmethod
    def _native_source(unique_id: str, label: str):
        from abstractcamera.drivers.avf_capture import AVFFrameSource

        return AVFFrameSource(unique_id, label)

    # -- lifecycle -------------------------------------------------------------
    def init(self) -> None:
        source = self._source_factory(self.unique_id, self._label)
        source.open()  # raises honest, specific errors (TCC, vanished, held)
        self._source = source
        self._build_resolution_choices()
        self._measure_fps()

    def _build_resolution_choices(self) -> None:
        """Device-reported formats ∩ the familiar ladder, native first —
        replaces the old cv2 probe-by-trial (the device SAYS what it has)."""
        native_w, native_h = self._source.current_dims()
        native = f"{native_w}x{native_h}"
        reported = set(self._source.format_dims())
        choices = [native]
        for width, height in RESOLUTION_LADDER:
            entry = f"{width}x{height}"
            if (width, height) in reported and entry not in choices:
                choices.append(entry)
        self._resolution_choices = choices
        self._current_resolution = native

    def _measure_fps(self) -> None:
        started = time.perf_counter()
        frames = 0
        while frames < 10 and time.perf_counter() - started < 1.5:
            try:
                self._source.read()
                frames += 1
            except CameraControlError:
                break
        elapsed = time.perf_counter() - started
        if frames >= 2 and elapsed > 0:
            self._measured_fps = max(1.0, min(120.0, frames / elapsed))

    def exit(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.finish()
            except Exception:
                pass
            self._recorder = None
        if self._source is not None:
            self._source.close()
            self._source = None

    def get_abilities(self):
        return wire.Abilities(model=f"Webcam: {self._label}")

    # -- streaming ---------------------------------------------------------------
    def _read_frame(self):
        if self._source is None:
            raise CameraControlError("The camera session is closed.")
        # The watchdog counts raises: a dead stream must not be papered
        # over with stale frames (session behavioral contract, item 2).
        frame = self._source.read()
        if self._movie_tee_enabled and self._recorder is not None:
            self._recorder.offer(frame)
        return frame

    def capture_preview(self):
        frame = self._read_frame()
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
        if not ok:
            raise CameraControlError("JPEG encoding of the preview frame failed.")
        return wire.BytesFile(encoded.tobytes())

    # -- capture -------------------------------------------------------------------
    FLASH_PHOTO_TIMEOUT_S = 4.0

    def trigger_capture(self) -> None:
        """A still IS a video frame on this hardware (the capability notes
        say so) — except with the flash armed on a device that has one
        (Continuity iPhones): the still routes through a real photo capture
        so the flash fires in sync (measured 2026-07-13)."""
        payload: bytes | None = None
        flash_attempted = False
        if self._flash_mode != "off" and getattr(self._source, "has_flash", lambda: False)():
            flash_attempted = True
            ticket = self._source.fire_flash_photo(self._flash_mode)
            # The initiation rides the host's MAIN queue; no delivery
            # (headless embedders without a main loop) is normal — fall
            # back to the flash-lit stream frame.
            if ticket.done.wait(timeout=self.FLASH_PHOTO_TIMEOUT_S) and ticket.photo:
                payload = ticket.photo
        if payload is None:
            frame = None
            # The video stream STALLS during a flash pre-fire sequence
            # (measured): give the fallback read a patient window.
            attempts = 6 if flash_attempted else 1
            for attempt in range(attempts):
                try:
                    frame = self._read_frame()
                    break
                except CameraControlError:
                    if attempt == attempts - 1:
                        raise
                    time.sleep(0.3)
            ok, encoded = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, STILL_JPEG_QUALITY])
            if not ok:
                raise CameraControlError("JPEG encoding of the still failed.")
            payload = encoded.tobytes()
        self._sequence += 1
        name = f"webcam_{time.strftime('%Y%m%d_%H%M%S')}_{self._sequence:04d}.jpg"
        self._objects[name] = wire.BytesFile(payload)
        self._events.append((wire.GP_EVENT_FILE_ADDED, wire.EventData("/", name)))

    # -- movie ------------------------------------------------------------------
    def movie_available(self) -> bool:
        try:
            import av  # noqa: F401
            return True
        except ImportError:
            return False

    def start_movie(self) -> None:
        if self._recorder is not None:
            return
        frame = self._read_frame()
        path = os.path.join(tempfile.mkdtemp(prefix="abstractcamera_movie_"), "movie.mp4")
        self._recorder = _MovieRecorder(path, frame.shape[1], frame.shape[0],
                                        self._measured_fps)
        self._movie_tee_enabled = True
        self._recorder.offer(frame)

    def stop_movie(self) -> dict | None:
        if self._recorder is None:
            return None
        self._movie_tee_enabled = False
        path, frames, dropped = self._recorder.finish()
        self._recorder = None
        self._sequence += 1
        name = f"webcam_movie_{time.strftime('%Y%m%d_%H%M%S')}_{self._sequence:04d}.mp4"
        self._objects[name] = wire.FileBackedFile(path)
        self._events.append((wire.GP_EVENT_FILE_ADDED, wire.EventData("/", name)))
        return {"frames": frames, "dropped": dropped}

    # -- events / files -------------------------------------------------------------
    def wait_for_event(self, timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if self._events:
                return self._events.popleft()
            if time.time() >= deadline:
                return (wire.GP_EVENT_TIMEOUT, None)
            time.sleep(0.002)

    def file_get(self, folder: str, name: str, file_type: int):
        payload = self._objects.pop(name, None)
        if payload is None:
            raise CameraControlError(f"No such capture object: {name}")
        return payload

    # -- single-config widget I/O ---------------------------------------------------
    # The REAL dials macOS grants: imagesize (device-reported formats) and
    # zoom (videoZoomFactor — an OS digital crop; measured: the ONLY manual
    # control, everything exposure/WB/focus reports unsupported on macOS).
    ZOOM_LADDER = (1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0)

    def _zoom_choices(self) -> list[str]:
        low, high = self._source.zoom_range()
        if high <= low:
            return []
        return [f"{step:g}x" for step in self.ZOOM_LADDER if low <= step <= high]

    @staticmethod
    def _format_zoom(factor: float) -> str:
        return f"{round(factor, 1):g}x"

    def get_single_config(self, name: str):
        if name == "imagesize":
            return wire.ProtocolWidget("imagesize", wire.GP_WIDGET_RADIO,
                                       self._current_resolution,
                                       choices=self._resolution_choices)
        if name == "zoom":
            choices = self._zoom_choices()
            if not choices:
                raise CameraControlError("Widget not found: zoom")
            return wire.ProtocolWidget("zoom", wire.GP_WIDGET_RADIO,
                                       self._format_zoom(self._source.zoom()),
                                       choices=choices)
        if name == "flashmode":
            # Only devices WITH a flash offer the dial (Continuity iPhones;
            # the MacBook camera has none — measured).
            if not getattr(self._source, "has_flash", lambda: False)():
                raise CameraControlError("Widget not found: flashmode")
            return wire.ProtocolWidget("flashmode", wire.GP_WIDGET_RADIO,
                                       self._flash_mode,
                                       choices=["off", "auto", "on"])
        if name == "fps":
            return wire.ProtocolWidget("fps", wire.GP_WIDGET_TEXT,
                                       f"{self._measured_fps:.0f}", readonly=True)
        raise CameraControlError(f"Widget not found: {name}")

    def set_single_config(self, name: str, widget) -> None:
        if name == "flashmode":
            requested = str(widget.get_value()).lower()
            if requested not in ("off", "auto", "on"):
                raise CameraControlError(f"Unusable flash mode: {widget.get_value()!r}")
            if not getattr(self._source, "has_flash", lambda: False)():
                raise CameraControlError(f"'{self._label}' has no flash.")
            self._flash_mode = requested
            return
        if name == "zoom":
            requested = str(widget.get_value()).rstrip("x")
            try:
                factor = float(requested)
            except ValueError:
                raise CameraControlError(f"Unusable zoom value: {widget.get_value()!r}")
            self._source.set_zoom(factor)
            # Confirmation is the device's own readback (the ledger declares
            # an unconfirmed write reverted, like every other dial).
            return
        if name != "imagesize":
            raise CameraControlError(
                f"'{self._label}' exposes no manual control over {name} — "
                "macOS reserves exposure/white-balance/focus for its own "
                "auto algorithms on this transport."
            )
        requested = str(widget.get_value())
        if requested not in self._resolution_choices:
            raise CameraControlError(f"Unsupported resolution: {requested}")
        width, height = (int(v) for v in requested.split("x"))
        self._source.set_dims(width, height)
        # Confirm on the actual stream: in-flight frames at the old size
        # drain first. A refused/unconfirmed switch keeps the old value —
        # the manager's pending-write ledger declares the revert honestly.
        deadline = time.time() + RESOLUTION_SETTLE_S
        while time.time() < deadline:
            try:
                frame = self._read_frame()
            except CameraControlError:
                break
            if frame.shape[1] == width and frame.shape[0] == height:
                self._current_resolution = requested
                return

    @property
    def measured_fps(self) -> float:
        return self._measured_fps
