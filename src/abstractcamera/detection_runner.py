"""Live-view detection dispatch: flood gating, ring-buffer clips, and the
lightning/meteor/motion pipelines feeding auto-fire."""

from __future__ import annotations

import os
import time

import cv2
import numpy as np

from abstractcamera.constants import (
    AUTO_FIRE_COOLDOWN_SECONDS,
    DETECTION_ARC_MAX_FILL,
    DETECTION_ARC_MIN_ELONGATION,
    DETECTION_ARC_MIN_INTENSITY,
    DETECTION_DIM_MIN_MEAN,
    DETECTION_DIM_RELATIVE_MEAN_RATIO,
    DETECTION_EVENT_MERGE_SECONDS,
    DETECTION_MIN_P99_LIFT,
)
from abstractcamera.detection import MeteorDetector, MotionDetector
from abstractcamera.worker import sequence_active_now


class DetectionRunnerMixin:

    # into counter events (a busy night must not evict real catches from
    # the 200-event log or spam auto-fire).
    DETECTION_EVENT_RATE_CAP_PER_MIN = 10

    def _detection_flood_gate(self, now: float) -> bool:
        """True when this event may be logged; otherwise it is coalesced."""
        while self._detection_event_times and now - self._detection_event_times[0] > 60.0:
            self._detection_event_times.popleft()
        if len(self._detection_event_times) >= self.DETECTION_EVENT_RATE_CAP_PER_MIN:
            self._detection_suppressed_count += 1
            if self._detection_suppressed_count == 1 or self._detection_suppressed_count % 25 == 0:
                self._append_event(
                    kind="detection", reason="coalesced",
                    note=f"suppressed {self._detection_suppressed_count} detections in the last minute (rate cap)",
                )
            return False
        self._detection_event_times.append(now)
        if self._detection_suppressed_count:
            self._detection_suppressed_count = 0
        return True

    def _write_ring_clip(self, frames: list[tuple[float, bytes]] | None, kind: str) -> str | None:
        """Write a pre-snapshotted ring-buffer clip (±2s of preview JPEGs
        around a confirmed event) as an MJPEG file — the only artifact that
        actually CONTAINS a fast subject (a reactively fired still opens
        after the moment has passed; cat-passing test 2026-07-07: at 1/4s
        exposure the fired photos held only a motion-blurred shadow). The
        snapshot happens at detection time; this disk write runs AFTER any
        auto-fire trigger."""
        if not self._capture_dir or not frames or len(frames) < 5:
            return None
        try:
            clip_dir = os.path.join(self._capture_dir, f"{kind}-clips")
            os.makedirs(clip_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(clip_dir, f"{kind}_{stamp}.mjpeg")
            with open(path, "wb") as fh:
                for _t, frame_jpeg in frames:
                    fh.write(frame_jpeg)
            return path
        except Exception:
            return None

    def _run_new_detection(self, camera, jpeg: bytes) -> None:
        """Meteor/motion targets (lightning keeps its validated path)."""
        gray = self._decode_gray(jpeg)
        if gray is None:
            return
        now = time.time()

        if self._detection_target == "meteor":
            if self._meteor_detector is None:
                self._meteor_detector = MeteorDetector(
                    sensitivity=self._detection_sensitivity.get("meteor", 50.0))
            event = self._meteor_detector.process(gray, now=now)
        else:
            if self._motion_detector is None:
                self._motion_detector = MotionDetector(
                    sensitivity=self._detection_sensitivity.get("motion", 50.0))
            event = self._motion_detector.process(gray, now=now)

        if event is None:
            return
        if not self._detection_flood_gate(now):
            return

        # FIRE FIRST, log after (adversarial verdict 2026-07-07): the shutter
        # command is the time-critical step — thumbnail encodes and clip disk
        # writes used to run BEFORE it, adding 15-200ms of avoidable latency.
        # The meteor ring buffer is snapshotted in memory NOW (cheap; the
        # pre-streak frames must be captured at detection time) and written
        # to disk after the trigger.
        clip_frames = [(t, j) for t, j in list(self._preview_ring) if now - t <= 4.5]

        fired = False
        if (
            self._detection_mode == "auto"
            and (now - self._last_trigger_at) >= AUTO_FIRE_COOLDOWN_SECONDS
            and not sequence_active_now(self)  # arbitration: sequences own the trigger
        ):
            if event.kind == "meteor":
                if self._capture_mode == "video" and not self._movie_recording:
                    # The one auto-fire that genuinely captures shower
                    # siblings: start recording and leave it rolling.
                    self._fire_trigger(camera, reason="auto-meteor", score=event.score)
                    fired = True
                elif self._capture_mode != "video":
                    # Allowed, but honest: the still opens AFTER the streak.
                    self._fire_trigger(camera, reason="auto-meteor", score=event.score)
                    fired = True
                    self._append_event(kind="trigger", reason="auto-meteor",
                                       note="note: the fired photo will not contain the detected meteor (use Monitor + an interval sequence, or video mode)")
            else:
                self._fire_trigger(camera, reason=f"auto-{event.kind}", score=event.score)
                fired = True

        clip_path = self._write_ring_clip(clip_frames, event.kind)
        note = event.note + (" · clip saved" if clip_path else "")
        # After an auto-fire the thumbnail encode is skipped: the next loop
        # iteration should be pulling the next preview frame, not encoding
        # base64 for the log (monitor mode keeps thumbnails).
        self._append_event(kind="detection", reason=event.kind, note=note,
                           score=event.score,
                           thumbnail_jpeg=None if fired else jpeg,
                           path=clip_path)

    def _run_detection(
        self,
        camera,
        jpeg: bytes,
        previous_gray: np.ndarray | None,
        baseline_means: deque,
    ) -> np.ndarray | None:
        gray = self._decode_gray(jpeg)
        if gray is None:
            return previous_gray

        mean_value = float(gray.mean())
        baseline = float(np.median(baseline_means)) if len(baseline_means) >= 12 else mean_value
        baseline = max(0.35, baseline)

        analysis: dict[str, float] = {}
        if self._frame_analyzer is not None:
            try:
                analysis = self._frame_analyzer(gray)
            except Exception:
                analysis = {}

        p99 = float(analysis.get("p99", float(np.percentile(gray, 99))))
        baseline_p99 = float(analysis.get("baseline_p99", baseline * 2))

        dim_flash = (
            mean_value >= DETECTION_DIM_MIN_MEAN
            and mean_value >= baseline * DETECTION_DIM_RELATIVE_MEAN_RATIO
            and p99 >= baseline_p99 + DETECTION_MIN_P99_LIFT
        )
        arc_flash = (
            float(analysis.get("arc_elongation", 0.0)) >= DETECTION_ARC_MIN_ELONGATION
            and float(analysis.get("arc_intensity", 0.0)) >= DETECTION_ARC_MIN_INTENSITY
            and float(analysis.get("arc_fill", 1.0)) <= DETECTION_ARC_MAX_FILL
        )

        is_flash = dim_flash or arc_flash
        now = time.time()
        if is_flash and (now - self._last_detection_at) >= DETECTION_EVENT_MERGE_SECONDS:
            self._last_detection_at = now
            reasons = []
            if arc_flash:
                reasons.append("arc")
            if dim_flash:
                reasons.append("flash")
            score = round(mean_value / baseline, 1)
            # Fire BEFORE logging: lightning re-strikes ride on ~100ms —
            # the thumbnail encode must not sit between detection and shutter.
            fired = False
            if (
                self._detection_mode == "auto"
                and (now - self._last_trigger_at) >= AUTO_FIRE_COOLDOWN_SECONDS
                and not sequence_active_now(self)  # arbitration: never fire into a sequence
            ):
                self._fire_trigger(camera, reason="auto-detect", score=score)
                fired = True
            self._append_event(
                kind="detection",
                reason="+".join(reasons),
                note=f"mean {mean_value:.1f} vs baseline {baseline:.1f}",
                score=score,
                thumbnail_jpeg=None if fired else jpeg,
            )

        # Only quiet frames feed the baseline so a long flash cannot poison it.
        if not is_flash:
            baseline_means.append(mean_value)
        return gray

    @staticmethod
    def _decode_gray(jpeg: bytes) -> np.ndarray | None:
        array = np.frombuffer(jpeg, dtype=np.uint8)
        decoded = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
        return decoded
