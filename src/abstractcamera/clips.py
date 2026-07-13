"""Rolling pre-capture buffer: continuous last-N-seconds of live view,
snapshot-to-MP4 on demand ([clips] extra / PyAV; honest refusal absent).
Encoding runs OFF the camera worker thread — it must never steal preview
frames."""

from __future__ import annotations

import os
import time
from collections import deque

import cv2
import numpy as np

from abstractcamera.errors import CameraControlError

def encode_rolling_clip_mp4(frames: list[tuple[float, bytes]], path: str, fps: float) -> None:
    """Mux buffered live-view JPEGs into an H.264 MP4 (PyAV). Runs OFF the
    camera worker thread — encoding must never steal preview frames."""
    import av

    first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        raise CameraControlError("The buffered frames could not be decoded.")
    height, width = first.shape[:2]
    # H.264 wants even dimensions.
    width -= width % 2
    height -= height % 2
    with av.open(path, "w") as container:
        stream = container.add_stream("h264", rate=max(1, int(round(fps))))
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "20", "preset": "veryfast"}
        for _t, jpeg in frames:
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            bgr = bgr[:height, :width]
            video_frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            for packet in stream.encode(video_frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)



class RollingClipsMixin:
    def set_rolling_buffer(self, enabled: bool, seconds: float | None = None) -> dict:
        """Continuous pre-capture buffer: the worker keeps the last N seconds
        of live-view JPEGs in memory; save_rolling_clip() snapshots them.
        Memory bound: ~10s × 60fps × ~30KB VGA ≈ 20MB."""
        if seconds is not None:
            seconds = float(np.clip(float(seconds), 3.0, 30.0))
        with self._state_lock:
            if seconds is not None:
                self._rolling_seconds = seconds
            self._rolling_enabled = bool(enabled)
            capacity = max(150, int(self._rolling_seconds * 65)) if self._rolling_enabled else 150
            if capacity != (self._preview_ring.maxlen or 0):
                self._preview_ring = deque(self._preview_ring, maxlen=capacity)
        return self.status()

    def _rolling_buffered_seconds(self) -> float:
        """RECENT contiguous span of the preview ring (caller holds the state
        lock). The naive newest-minus-oldest span lied after any dormant
        phase: frames left by an earlier detection session made the buffer
        read full while save_rolling_clip's age filter (correctly) discarded
        them — 'Keep' then failed on a supposedly full buffer (found during
        the Sony hardware validation, 2026-07-12; family-independent)."""
        ring = self._preview_ring
        if not self._rolling_enabled or len(ring) < 2:
            return 0.0
        now = time.time()
        newest = ring[-1][0]
        if now - newest > 1.5:
            return 0.0  # the feed is stale — nothing recent is buffered
        span_start = newest
        previous = newest
        for timestamp, _jpeg in reversed(ring):
            # Stop at a feed gap (dormant phase) or beyond the window.
            if previous - timestamp > 1.0 or now - timestamp > self._rolling_seconds + 0.5:
                break
            span_start = timestamp
            previous = timestamp
        return round(min(self._rolling_seconds, newest - span_start), 1)

    def save_rolling_clip(self) -> dict:
        """Write the buffered window to an MP4 (no camera access — safe from
        any thread; the worker keeps appending while we encode a snapshot)."""
        with self._state_lock:
            if not self._rolling_enabled:
                raise CameraControlError("The rolling buffer is off — enable it first.")
            frames = list(self._preview_ring)
            seconds = self._rolling_seconds
        now = time.time()
        frames = [(t, j) for t, j in frames if now - t <= seconds + 0.5]
        if len(frames) < 10 or frames[-1][0] - frames[0][0] < 1.0:
            raise CameraControlError("The buffer is still filling — wait a moment and try again.")
        if not self._capture_dir:
            raise CameraControlError("No capture directory is configured.")
        clip_dir = os.path.join(self._capture_dir, "rolling-clips")
        os.makedirs(clip_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(clip_dir, f"rolling_{stamp}.mp4")
        duration = frames[-1][0] - frames[0][0]
        fps = max(1.0, (len(frames) - 1) / duration)
        encode_rolling_clip_mp4(frames, path, fps)
        self._append_event(
            kind="clip", reason="rolling",
            note=f"kept the last {duration:.1f}s ({len(frames)} frames @ {fps:.0f}fps)",
            path=path, thumbnail_jpeg=frames[-1][1],
        )
        return {"path": path, "frames": len(frames), "duration_s": round(duration, 2), "fps": round(fps, 1)}
