"""Live-view detection pipelines: meteor streaks and generic motion.

Referee-adjudicated (2026-07-07) from GMN/RMS and UFOCapture practice:
- METEOR: running-mean background (alpha=0.05), gain guard (>10% global mean
  step re-seeds), MAD threshold K*sigma+J (K=3.5, J=6), 3x3 median blur (no
  morphological opening — it erases 1-2px-wide streaks), connected-component
  gates (area>=4, elongation>=3, fill<=0.35, length>=8px), temporal
  confirmation: 2-4 collinear candidates within 0.35s (residual<=2px, angle
  drift<=15 degrees), speed 2-20 px/frame @30fps (dt-scaled), duration<=1.5s
  else the locus is a plane/satellite and is suppressed for 10s;
  mask fraction >2% skips the frame (cloud/lightning/gain step).
- MOTION: 2x downscale, global-gain normalization to a rolling-median mean
  (exposure/AWB flicker cancels exactly), running average alpha=0.02,
  threshold max(8, 3*sigma), trigger on foreground fraction >= f_min for 3
  consecutive frames; sensitivity 0-100 log-maps f_min 5% -> 0.2%.

Both detectors re-seed after preview gaps >0.5s (the frame after an
exposure pause otherwise diffs against a stale background and fires a
full-frame false event) and suppress output for 1s after a re-seed.

HONESTY: thresholds start from RMS/UFOCapture values and are tuned on
synthetic gates; real-sky rates (meteor SNR at preview gain, scintillation,
plane strobes) are NOT validated yet and are labeled so in the docs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class DetectionEvent:
    kind: str  # "meteor" | "motion"
    note: str
    score: float
    metrics: dict = field(default_factory=dict)


class _BaseDetector:
    RESEED_GAP_S = 0.5
    RESEED_SUPPRESS_S = 1.0
    GAIN_STEP_FRACTION = 0.10

    def __init__(self):
        self._background: np.ndarray | None = None
        self._last_frame_at: float | None = None
        self._suppress_until = 0.0
        self._mean_history: list[float] = []

    def _prepare(self, gray: np.ndarray, now: float) -> np.ndarray | None:
        """Common guards. Returns the float32 frame, or None when this frame
        must not produce detections (re-seed / gain step / suppression)."""
        frame = gray.astype(np.float32)

        gap = None if self._last_frame_at is None else now - self._last_frame_at
        self._last_frame_at = now

        mean_value = float(frame.mean())
        rolling = float(np.median(self._mean_history)) if len(self._mean_history) >= 8 else mean_value
        self._mean_history.append(mean_value)
        if len(self._mean_history) > 60:
            self._mean_history.pop(0)

        needs_reseed = (
            self._background is None
            or self._background.shape != frame.shape
            or (gap is not None and gap > self.RESEED_GAP_S)
            or (rolling > 1e-3 and abs(mean_value / rolling - 1.0) > self.GAIN_STEP_FRACTION)
        )
        if needs_reseed:
            self._background = frame.copy()
            self._suppress_until = now + self.RESEED_SUPPRESS_S
            return None
        if now < self._suppress_until:
            self._accumulate(frame)
            return None
        return frame

    def _accumulate(self, frame: np.ndarray) -> None:
        raise NotImplementedError


class MeteorDetector(_BaseDetector):
    """Fast faint streaks: threshold -> line-like components -> collinear
    multi-frame track with speed and duration gates."""

    BACKGROUND_ALPHA = 0.05
    THRESHOLD_J = 6.0
    MIN_AREA_PX = 4
    MIN_ELONGATION = 3.0
    MAX_FILL = 0.35
    MIN_LENGTH_PX = 8.0
    CONFIRM_WINDOW_S = 0.35
    COLLINEAR_RESIDUAL_PX = 2.0
    MAX_ANGLE_DRIFT_DEG = 15.0
    MAX_TRACK_DURATION_S = 1.5
    LOCUS_SUPPRESS_S = 10.0
    MAX_MASK_FRACTION = 0.02

    def __init__(self, sensitivity: float = 50.0):
        super().__init__()
        self.set_sensitivity(sensitivity)
        self._candidates: list[dict] = []  # {x, y, angle, t, length, peak}
        self._suppressed_loci: list[dict] = []  # {x, y, until}
        self._last_event_at = -math.inf

    def set_sensitivity(self, sensitivity: float) -> None:
        s = float(np.clip(sensitivity, 0.0, 100.0)) / 100.0
        # 0 -> K=5.0 speed floor 3 px/f; 50 -> K=3.5 floor 2; 100 -> K=2.5 floor 1.
        self.threshold_k = 5.0 - 2.5 * s
        self.speed_floor_px_frame = 3.0 - 2.0 * s
        self.speed_ceiling_px_frame = 20.0

    def _accumulate(self, frame: np.ndarray) -> None:
        cv2.accumulateWeighted(frame, self._background, self.BACKGROUND_ALPHA)

    def process(self, gray: np.ndarray, now: float | None = None) -> DetectionEvent | None:
        now = time.time() if now is None else now
        frame = self._prepare(gray, now)
        if frame is None:
            return None

        diff = frame - self._background
        self._accumulate(frame)
        np.clip(diff, 0.0, None, out=diff)
        # NO smoothing: a 3x3 median erases 1px-wide lines entirely, and a
        # Gaussian fattens them until the elongation gate fails (both
        # verified on injected streaks). RMS thresholds raw diffs too; salt
        # noise is handled by the area>=4 + elongation + fill gates.
        diff_blurred = diff

        sample = diff_blurred[::4, ::4]
        med = float(np.median(sample))
        mad = float(np.median(np.abs(sample - med))) * 1.4826
        sigma = max(mad, 0.5)
        threshold = med + self.threshold_k * sigma + self.THRESHOLD_J
        mask = (diff_blurred > threshold).astype(np.uint8)

        mask_fraction = float(mask.mean())
        if mask_fraction > self.MAX_MASK_FRACTION:
            return None  # cloud / lightning / global step: not a streak

        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        best = None
        for i in range(1, n_labels):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < self.MIN_AREA_PX:
                continue
            w = float(stats[i, cv2.CC_STAT_WIDTH])
            h = float(stats[i, cv2.CC_STAT_HEIGHT])
            length = math.hypot(w, h)
            elongation = length / max(1.0, math.sqrt(area))
            fill = area / max(1.0, w * h)
            if elongation < self.MIN_ELONGATION or fill > self.MAX_FILL or length < self.MIN_LENGTH_PX:
                continue
            cx, cy = centroids[i]
            if self._locus_suppressed(cx, cy, now):
                continue
            angle = math.degrees(math.atan2(h if h > 0 else 1.0, w if w > 0 else 1.0))
            component = diff_blurred[labels == i]
            peak = float(component.max()) if component.size else 0.0
            candidate = {"x": float(cx), "y": float(cy), "angle": angle, "t": now,
                         "length": length, "peak": peak}
            if best is None or peak > best["peak"]:
                best = candidate

        if best is not None:
            self._candidates.append(best)
        self._candidates = [c for c in self._candidates if now - c["t"] <= self.MAX_TRACK_DURATION_S + 0.5]

        return self._confirm(now)

    def _locus_suppressed(self, x: float, y: float, now: float) -> bool:
        self._suppressed_loci = [l for l in self._suppressed_loci if l["until"] > now]
        return any(math.hypot(x - l["x"], y - l["y"]) < 60.0 for l in self._suppressed_loci)

    def _confirm(self, now: float) -> DetectionEvent | None:
        recent = [c for c in self._candidates if now - c["t"] <= self.CONFIRM_WINDOW_S]
        if len(recent) < 2:
            return None

        # Slow-object rejection: a locus that keeps producing candidates for
        # longer than MAX_TRACK_DURATION_S is a plane/satellite/strobe.
        oldest, newest = self._candidates[0], self._candidates[-1]
        track_duration = newest["t"] - oldest["t"]
        if track_duration > self.MAX_TRACK_DURATION_S and math.hypot(
            newest["x"] - oldest["x"], newest["y"] - oldest["y"]
        ) < 200.0:
            self._suppressed_loci.append({"x": newest["x"], "y": newest["y"],
                                          "until": now + self.LOCUS_SUPPRESS_S})
            self._candidates.clear()
            return None

        pts = np.array([[c["x"], c["y"]] for c in recent], dtype=np.float64)
        times = np.array([c["t"] for c in recent])
        dt_total = float(times.max() - times.min())
        if dt_total <= 1e-3:
            return None
        # Collinearity: residual of the best-fit line.
        centered = pts - pts.mean(axis=0)
        _u, s, vt = np.linalg.svd(centered, full_matrices=False)
        residual = float(s[1] / math.sqrt(len(pts))) if len(s) > 1 else 0.0
        if residual > self.COLLINEAR_RESIDUAL_PX:
            return None
        angles = [c["angle"] for c in recent]
        if max(angles) - min(angles) > self.MAX_ANGLE_DRIFT_DEG:
            return None

        displacement = float(np.linalg.norm(pts[-1] - pts[0]))
        frames_spanned = max(1.0, dt_total * 30.0)
        speed_px_frame = displacement / frames_spanned
        if not (self.speed_floor_px_frame <= speed_px_frame <= self.speed_ceiling_px_frame):
            return None

        if now - self._last_event_at < 2.0:  # per-mode merge window
            return None
        self._last_event_at = now
        length = max(c["length"] for c in recent)
        angle = float(np.median(angles))
        peak = max(c["peak"] for c in recent)
        self._candidates.clear()
        return DetectionEvent(
            kind="meteor",
            note=f"streak {length:.0f}px @ {angle:.0f}° · {speed_px_frame:.1f}px/f · {dt_total:.2f}s",
            score=round(peak, 1),
            metrics={"length_px": round(length, 1), "angle_deg": round(angle, 1),
                     "speed_px_frame": round(speed_px_frame, 2),
                     "duration_s": round(dt_total, 3), "peak": round(peak, 1)},
        )


class MotionDetector(_BaseDetector):
    """Generic scene-change trigger, immune to global exposure flicker."""

    BACKGROUND_ALPHA = 0.02
    DEBOUNCE_FRAMES = 3

    def __init__(self, sensitivity: float = 50.0):
        super().__init__()
        self.set_sensitivity(sensitivity)
        self._consecutive = 0
        self._last_event_at = -math.inf
        # RAW means for gain normalization. Reusing _prepare's _mean_history
        # (normalized means) created a truncation feedback loop — see process().
        self._raw_mean_history: list[float] = []

    def set_sensitivity(self, sensitivity: float) -> None:
        s100 = float(np.clip(sensitivity, 0.0, 100.0))
        s = s100 / 100.0
        # Log map: 0 -> 5% of frame, 50 -> ~1%, 100 -> 0.2%.
        self.f_min = 0.05 * (0.04 ** s)
        # Debounce scales with sensitivity (adversarial verdict 2026-07-07):
        # a fast subject crossing the frame in <3 processed frames never
        # confirmed at all ("none of the tests captured the movements").
        # High sensitivity explicitly trades false-positive risk for speed:
        # 0-74 -> 3 frames, 75-94 -> 2, >=95 -> 1.
        if s100 >= 95.0:
            self._debounce_frames = 1
        elif s100 >= 75.0:
            self._debounce_frames = 2
        else:
            self._debounce_frames = self.DEBOUNCE_FRAMES

    def _accumulate(self, frame: np.ndarray) -> None:
        cv2.accumulateWeighted(frame, self._background, self.BACKGROUND_ALPHA)

    def process(self, gray: np.ndarray, now: float | None = None) -> DetectionEvent | None:
        now = time.time() if now is None else now
        small = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2), interpolation=cv2.INTER_AREA)

        # Global gain normalization BEFORE the base guards: exposure/AWB
        # steps must cancel exactly, not merely re-seed.
        #
        # CRITICAL (found 2026-07-07, "it takes too much time to shoot"):
        # the rolling reference must come from RAW frame means. It used to
        # come from _prepare's history of already-normalized, uint8-TRUNCATED
        # frames — on dark scenes (mean ~9) truncation loses ~6% per pass,
        # the feedback loop dragged the reference down 9.2->3.3 in 10s, and
        # the 10% gain guard then re-seeded + suppressed the detector ~70
        # times per 10s of plain sky. The detector was blind most of the
        # time; events only squeezed through between suppression windows
        # (measured: 955ms detection delay that should have been ~30ms).
        mean_now = float(small.mean())
        self._raw_mean_history.append(mean_now)
        if len(self._raw_mean_history) > 60:
            self._raw_mean_history.pop(0)
        rolling = (
            float(np.median(self._raw_mean_history))
            if len(self._raw_mean_history) >= 8 else mean_now
        )
        normalized = small.astype(np.float32) * (rolling / max(mean_now, 1.0)) if rolling > 1e-3 else small.astype(np.float32)

        # Pass the FLOAT frame straight through — the uint8 round-trip was
        # pure precision loss (and the source of the truncation loop).
        frame = self._prepare(normalized, now)
        if frame is None:
            self._consecutive = 0
            return None

        diff = np.abs(frame - self._background)
        self._accumulate(frame)
        sample = diff[::4, ::4]
        med = float(np.median(sample))
        sigma = float(np.median(np.abs(sample - med))) * 1.4826
        threshold = max(8.0, 3.0 * max(sigma, 0.5))
        mask = (diff > threshold).astype(np.uint8)
        fraction = float(mask.mean())

        if fraction < self.f_min:
            self._consecutive = 0
            return None
        self._consecutive += 1
        if self._consecutive < self._debounce_frames:
            return None
        if now - self._last_event_at < 1.0:  # event merge
            return None
        self._last_event_at = now
        self._consecutive = 0

        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n_labels > 1:
            biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            bx = int(stats[biggest, cv2.CC_STAT_LEFT]) * 2
            by = int(stats[biggest, cv2.CC_STAT_TOP]) * 2
            bw = int(stats[biggest, cv2.CC_STAT_WIDTH]) * 2
            bh = int(stats[biggest, cv2.CC_STAT_HEIGHT]) * 2
            cx, cy = centroids[biggest]
            cx, cy = int(cx * 2), int(cy * 2)
        else:
            bx = by = bw = bh = cx = cy = 0
        return DetectionEvent(
            kind="motion",
            note=f"motion {fraction * 100:.1f}% · bbox {bw}x{bh} @ ({cx},{cy})",
            score=round(fraction * 100.0, 2),
            metrics={"fraction_pct": round(fraction * 100.0, 2),
                     "bbox": [bx, by, bw, bh], "centroid": [cx, cy]},
        )
