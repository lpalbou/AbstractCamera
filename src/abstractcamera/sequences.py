"""Intervalometer scheduling logic (pure: no gphoto2, no threads).

Referee-adjudicated design (2026-07-07). The camera worker loop DRIVES this
class; everything time-critical is computed here so the math is testable
without a camera:

- ABSOLUTE deadlines: deadline(n) = t0 + n*interval (never now+interval) —
  relative scheduling accumulates worker-loop jitter as drift.
- Missed-slot policy: a slot serviced later than deadline + interval/2 is
  SKIPPED and logged; never fire late, never re-phase (a late frame ruins a
  timelapse more than a missing one).
- Failure policy: a trigger exception marks the shot failed; 3 consecutive
  failures abort the sequence.
- Shutter-speed parsing: Nikon-style strings ("0.0333s", "1/320", "30s",
  "Bulb", "Time"). Bulb/Time/unparseable REFUSE the start (no bulb plumbing
  exists; promising it without hardware validation would be dishonest).

States: idle -> armed -> running -> {complete | stopped | aborted}.
Terminal states persist until the next start.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


SEQUENCE_STATES = ("idle", "armed", "running", "complete", "stopped", "aborted")
MISSED_SLOT_FRACTION = 0.5
MAX_CONSECUTIVE_FAILURES = 3
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 3600.0
MAX_COUNT = 9999
MAX_START_DELAY_S = 3600.0
EXPOSURE_MARGIN_S = 0.7
RAW_MARGIN_WARNING_S = 2.0


class IntervalValidationError(ValueError):
    """The sequence cannot start (bad parameters, Bulb, disconnected...)."""


def parse_shutter_speed_seconds(value) -> float:
    """Parse a gphoto2 shutterspeed choice string to seconds.

    Raises IntervalValidationError for Bulb/Time/unparseable — the caller
    refuses to start (never guesses an exposure)."""
    text = str(value or "").strip()
    if not text:
        raise IntervalValidationError("The camera did not report a shutter speed.")
    lowered = text.lower()
    if lowered in ("bulb", "time"):
        raise IntervalValidationError(
            "Set a numeric shutter speed — Bulb/Time exposures are not supported by the intervalometer yet."
        )
    cleaned = text.rstrip("sS").strip()
    try:
        if "/" in cleaned:
            numerator, denominator = cleaned.split("/", 1)
            seconds = float(numerator) / float(denominator)
        else:
            seconds = float(cleaned)
    except (ValueError, ZeroDivisionError) as exc:
        raise IntervalValidationError(f"Unrecognized shutter speed '{text}'.") from exc
    if not (0.0 < seconds <= 900.0):
        raise IntervalValidationError(f"Shutter speed '{text}' is out of the supported range.")
    return seconds


@dataclass
class ShotRecord:
    index: int  # 1-based
    deadline: float
    fired_at: float | None = None
    result: str = "pending"  # fired | missed | failed
    note: str = ""


@dataclass
class IntervalSequence:
    """One armed/running sequence. Mutated only by the owning worker (plus
    the stop flag). All timestamps are epoch seconds."""

    interval_s: float
    count: int  # 0 = infinite
    start_delay_s: float = 0.0
    liveview: bool = True
    exposure_s: float = 0.0

    state: str = "armed"
    started_at: float = field(default_factory=time.time)
    shots_done: int = 0
    shots_failed: int = 0
    shots_missed: int = 0
    consecutive_failures: int = 0
    next_index: int = 0  # 0-based n for deadline math
    last_error: str | None = None
    finished_at: float | None = None
    shot_log: list[ShotRecord] = field(default_factory=list)
    # Owned by the camera controller: per-sequence manifest file. Kept ON the
    # sequence so a finish/restart race can never write one sequence's
    # summary into another sequence's manifest.
    manifest_path: str | None = None

    # ------------------------------------------------------------------
    @property
    def t0(self) -> float:
        return self.started_at + self.start_delay_s

    def deadline(self, n: int | None = None) -> float:
        index = self.next_index if n is None else n
        return self.t0 + index * self.interval_s

    @property
    def is_active(self) -> bool:
        return self.state in ("armed", "running")

    @property
    def is_infinite(self) -> bool:
        return self.count == 0

    def next_shot_at(self) -> float | None:
        if not self.is_active:
            return None
        return self.deadline()

    # ------------------------------------------------------------------
    def poll(self, now: float | None = None) -> str:
        """What should the worker do right now?
        Returns one of: 'wait' | 'fire' | 'skip' | 'done'."""
        if not self.is_active:
            return "done"
        now = time.time() if now is None else now
        if not self.is_infinite and self.next_index >= self.count:
            self.state = "complete"
            self.finished_at = now
            return "done"
        due = self.deadline()
        if now < due:
            if self.state == "armed" and now >= self.started_at:
                pass  # armed -> running happens on the first FIRE
            return "wait"
        if now > due + self.interval_s * MISSED_SLOT_FRACTION:
            return "skip"
        return "fire"

    def record_fired(self, fired_at: float | None = None) -> ShotRecord:
        fired_at = time.time() if fired_at is None else fired_at
        record = ShotRecord(index=self.next_index + 1, deadline=self.deadline(),
                            fired_at=fired_at, result="fired")
        self.shot_log.append(record)
        self.state = "running"
        self.shots_done += 1
        self.consecutive_failures = 0
        self.next_index += 1
        self._maybe_complete()
        return record

    def record_failed(self, note: str) -> ShotRecord:
        record = ShotRecord(index=self.next_index + 1, deadline=self.deadline(),
                            result="failed", note=note)
        self.shot_log.append(record)
        self.state = "running"
        self.shots_failed += 1
        self.consecutive_failures += 1
        self.last_error = note
        self.next_index += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.state = "aborted"
            self.finished_at = time.time()
            self.last_error = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive trigger failures ({note})"
        else:
            self._maybe_complete()
        return record

    def record_missed(self, note: str = "worker blocked past the slot") -> ShotRecord:
        record = ShotRecord(index=self.next_index + 1, deadline=self.deadline(),
                            result="missed", note=note)
        self.shot_log.append(record)
        self.state = "running"
        self.shots_missed += 1
        self.next_index += 1
        self._maybe_complete()
        return record

    def stop(self) -> None:
        if self.is_active:
            self.state = "stopped"
            self.finished_at = time.time()

    def abort(self, reason: str) -> None:
        if self.is_active:
            self.state = "aborted"
            self.last_error = reason
            self.finished_at = time.time()

    def _maybe_complete(self) -> None:
        if self.is_active and not self.is_infinite and self.next_index >= self.count:
            self.state = "complete"
            self.finished_at = time.time()

    # ------------------------------------------------------------------
    def to_status(self) -> dict:
        return {
            "state": self.state,
            "shots_done": self.shots_done,
            "shots_total": None if self.is_infinite else self.count,
            "shots_failed": self.shots_failed,
            "shots_missed": self.shots_missed,
            "started_at": self.started_at,
            "start_delay_s": self.start_delay_s,
            "interval_s": self.interval_s,
            "exposure_s": self.exposure_s,
            "liveview": self.liveview,
            "next_shot_at": self.next_shot_at(),
            "last_error": self.last_error,
            "finished_at": self.finished_at,
        }


def validate_sequence_request(
    *,
    interval_s: float,
    count: int,
    start_delay_s: float,
    shutter_value,
    capture_mode: str,
    movie_recording: bool,
    imagequality: str | None = None,
    capturetarget: str | None = None,
    exposure_s_override: float | None = None,
) -> tuple[float, str | None]:
    """Validate a start request. Returns (exposure_s, warning_or_none).
    Raises IntervalValidationError with the SPECIFIC reason.

    exposure_s_override: families whose exposure is not widget-driven (a
    webcam's effective exposure is its frame interval) pass their nominal
    exposure; None keeps the shutter-widget path — including the Bulb/'0/0'
    refusals — byte-identical for PTP bodies."""
    if capture_mode != "single":
        raise IntervalValidationError(
            "The intervalometer fires single frames — switch Capture Mode to Single first."
        )
    if movie_recording:
        raise IntervalValidationError("Stop the video recording before starting a sequence.")
    if not (MIN_INTERVAL_S <= float(interval_s) <= MAX_INTERVAL_S):
        raise IntervalValidationError(
            f"Interval must be between {MIN_INTERVAL_S:g}s and {MAX_INTERVAL_S:g}s."
        )
    if not (0 <= int(count) <= MAX_COUNT):
        raise IntervalValidationError(f"Frame count must be 0 (infinite) to {MAX_COUNT}.")
    if not (0.0 <= float(start_delay_s) <= MAX_START_DELAY_S):
        raise IntervalValidationError(f"Start delay must be 0 to {MAX_START_DELAY_S:g}s.")

    if exposure_s_override is not None:
        exposure_s = float(exposure_s_override)
        if not (0.0 < exposure_s <= 900.0):
            raise IntervalValidationError(
                f"The camera reported an unusable exposure ({exposure_s_override!r})."
            )
    else:
        if shutter_value is None:
            raise IntervalValidationError(
                "The camera did not report a shutter speed — set the mode dial to M."
            )
        exposure_s = parse_shutter_speed_seconds(shutter_value)

    if float(interval_s) < exposure_s + EXPOSURE_MARGIN_S:
        raise IntervalValidationError(
            f"Interval {interval_s:g}s is too short for a {exposure_s:g}s exposure — "
            f"allow at least {exposure_s + EXPOSURE_MARGIN_S:.1f}s."
        )

    warning = None
    quality = str(imagequality or "")
    target = str(capturetarget or "")
    if (
        float(interval_s) < exposure_s + RAW_MARGIN_WARNING_S
        and "NEF" in quality.upper()
        and "RAM" in target.upper()
    ):
        warning = (
            "RAW to internal RAM with a tight interval: downloads may lag behind — "
            "prefer capturetarget = Memory card."
        )
    return exposure_s, warning
