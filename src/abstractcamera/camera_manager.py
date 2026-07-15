"""CameraManager: the public, thread-safe camera orchestrator.

The high-level object of AbstractCamera (parallel to abstractvision's
VisionManager). Owns the worker thread, the scheduling windows, the
pending-write honesty ledger, downloads, detection dispatch, the rolling
buffer, and the catch log; every camera TOUCH goes through the family
adapter of a driver-created session (ADR 0001/0002). Host applications
interact only with the thread-safe public API.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from collections import deque
from typing import Any

import cv2
import numpy as np

from abstractcamera import discovery
from abstractcamera.adapters.base import GENERIC_CONFIG_WIDGET_NAMES
from abstractcamera.capture_ops import CaptureOpsMixin
from abstractcamera.clips import RollingClipsMixin
from abstractcamera.config_ledger import ConfigLedgerMixin
from abstractcamera.constants import (
    ACTION_WIDGET_NAMES,
    CATCH_LOG_MAX_EVENTS,
    EVENT_THUMBNAIL_MAX_EDGE,
    FrameAnalyzer,
)
from abstractcamera.detection_runner import DetectionRunnerMixin
from abstractcamera.downloads import DownloadsMixin
from abstractcamera.errors import CameraControlError
from abstractcamera.sequences import (
    IntervalSequence,
    IntervalValidationError,
    validate_sequence_request,
)
from abstractcamera.worker import WorkerLoopMixin

CONFIG_WIDGET_NAMES = list(GENERIC_CONFIG_WIDGET_NAMES)


class CameraManager(WorkerLoopMixin, ConfigLedgerMixin, CaptureOpsMixin,
                    DownloadsMixin, DetectionRunnerMixin, RollingClipsMixin):
    """Owns one camera connection and its live view worker."""

    def __init__(self, driver=None):
        # Explicit driver (tests, embedders) beats registry resolution;
        # None resolves per-connect from the environment (fake/ptp/webcam).
        self._fixed_driver = driver
        self._driver = driver
        self._camera_id: str | None = None
        # Device identity (set at connect): slug drives the capture folder
        # (<root>/<device_slug>[/<sequence_name>]); serial disambiguates
        # identical bodies (the hub may override the slug with a suffix).
        self._device_slug: str | None = None
        self._device_serial: str | None = None
        # Capture layout: either an explicit legacy dir (set_capture_dir) or
        # the root/<device>/<sequence> scheme (set_capture_root).
        self._capture_root: str | None = None
        self._capture_dir_override: str | None = None
        self._sequence_name: str | None = None
        # Save policy: download captures to this machine (True, historic
        # behavior) or leave them on the device's own storage (False).
        self._download_locally = True

        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._trigger_requested = threading.Event()
        self._camera_model: str | None = None
        self._camera_address: str | None = None
        # Family adapter (backend/camera_adapters): selected from the
        # detected model at connect; owns every family-specific behavior.
        # Only the worker thread calls camera-touching adapter methods.
        self._adapter = None
        self._capabilities: dict | None = None
        self._connected = False
        self._liveview_running = False
        self._detection_mode = "off"  # off | monitor | auto
        self._detection_target = "lightning"  # lightning | meteor | motion
        self._detection_sensitivity: dict[str, float] = {"lightning": 50.0, "meteor": 50.0, "motion": 50.0}
        self._meteor_detector: MeteorDetector | None = None
        self._motion_detector: MotionDetector | None = None
        self._detection_paused_reason: str | None = None
        self._detection_cost_ms = 0.0
        self._detection_frame_skip = False
        self._detection_event_times: deque[float] = deque(maxlen=64)
        self._detection_suppressed_count = 0
        self._preview_ring: deque[tuple[float, bytes]] = deque(maxlen=150)  # ~±2.5s at 30fps
        # Rolling pre-capture buffer (owner request 2026-07-07): continuously
        # keep the last N seconds of live view; "Keep" snapshots it to a clip.
        # The ring holds JPEGs as delivered (no re-encode in the worker).
        self._rolling_enabled = False
        self._rolling_seconds = 10.0
        self._preview_size: tuple[int, int] | None = None
        self._preview_size_candidate: tuple[int, int] | None = None
        self._pending_writes: dict[str, dict] = {}  # name -> {value, requested_at, state, actual, mismatches}
        self._capture_mode = "single"  # single | burst | video
        self._burst_count = 3
        # Duration-burst families (Sony press-and-hold): the hold length and
        # drive speed are the knobs instead of a frame count.
        self._burst_hold_s = 1.0
        self._burst_speed = "Hi"
        self._movie_recording = False
        self._frame_analyzer: FrameAnalyzer | None = None
        self._latest_frame: bytes | None = None
        self._latest_frame_seq = 0
        self._measured_fps = 0.0
        self._events: deque[dict] = deque(maxlen=CATCH_LOG_MAX_EVENTS)
        self._event_counter = 0
        self._last_error: str | None = None
        self._last_trigger_at = 0.0
        self._last_detection_at = 0.0
        self._config_cache: dict[str, Any] = {}
        self._pending_config: dict[str, str] = {}
        # Paced requeue bookkeeping for TRANSIENT write failures (Sony
        # answers [-2] busy for seconds after a burst while flushing to the
        # card): name -> attempts / earliest-next-try.
        self._config_retry_counts: dict[str, int] = {}
        self._config_retry_not_before: dict[str, float] = {}
        self._pending_actions: list[tuple[str, str | None]] = []  # FIFO: order matters for focus nudges
        self._capture_dir: str | None = None
        self._event_drain_until = 0.0
        self._preview_pause_until = 0.0
        self._interval_sequence: IntervalSequence | None = None
        self._interval_stop_requested = False
        self._interval_start_lock = threading.Lock()  # one arm at a time (double-start race)
        self._saved_detection_mode: str | None = None
        # Deferred-download queue (adversarial verdict 2026-07-07): while
        # Auto-Fire is armed, FILE_ADDED events are ANNOUNCED immediately but
        # the 1-3s file_get is deferred — a download on the sole gphoto2
        # thread blinded detection for seconds after every shot ("the auto
        # fire pauses", owner). capturetarget=Card makes deferral lossless.
        self._pending_downloads: deque[tuple[str, str, float]] = deque()  # (folder, name, announced_at)
        self._pending_download_keys: set[tuple[str, str]] = set()
        self._downloads_pending = 0
        self._download_flush_requested = threading.Event()
        self._config_refresh_requested = threading.Event()
        self._config_cache_version = 0
        # Silent-refusal honesty (Sony: AF-gated triggers are accepted and
        # never fire — no error, no file). Set from CaptureTiming at fire,
        # cleared by any FILE_ADDED, reported once on expiry.
        self._expect_file_deadline = 0.0
        self._expect_file_note: str | None = None

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def set_frame_analyzer(self, analyzer: FrameAnalyzer) -> None:
        self._frame_analyzer = analyzer

    def set_capture_dir(self, path: str) -> None:
        """Legacy explicit capture directory (overrides the device layout;
        a set sequence name still nests under it)."""
        self._capture_dir_override = path
        self._recompute_capture_dir()

    def set_capture_root(self, path: str) -> None:
        """Device-layout root: captures land in <root>/<device_slug>/
        (and <root>/<device_slug>/<sequence_name>/ when a sequence name is
        set). Default root: ~/Pictures."""
        self._capture_root = path
        self._recompute_capture_dir()

    def set_sequence_name(self, name: str | None) -> dict:
        """Name the current shooting sequence: everything captured while it
        is set (stills, bursts, movies, clips, interval manifests) lands in
        the sequence subfolder. None/empty clears it."""
        from abstractcamera.identity import sanitize_sequence_name

        cleaned = sanitize_sequence_name(name)
        if name and not cleaned:
            raise CameraControlError(f"Unusable sequence name: {name!r}")
        with self._state_lock:
            self._sequence_name = cleaned
        self._recompute_capture_dir()
        return self.status()

    def set_save_policy(self, download_locally: bool) -> dict:
        """Where captures live: downloaded to this machine (True) or left on
        the device's own storage (False). Families without onboard storage
        (webcams) refuse device-only honestly."""
        download_locally = bool(download_locally)
        adapter = self._adapter
        if not download_locally and adapter is not None \
                and "device" not in adapter.save_modes():
            raise CameraControlError(
                "This camera has no storage of its own — captures can only "
                "be saved on this machine."
            )
        with self._state_lock:
            self._download_locally = download_locally
        if not download_locally:
            # Honesty guard: device-only with a volatile capture target
            # (camera RAM) means shots die with the session — warn loudly.
            target = str((self._config_cache.get("capturetarget") or {}).get("value", ""))
            caps = self._capabilities or {}
            volatile = caps.get("save_to", {}).get("volatile_values", [])
            if target and target in volatile:
                self._append_event(
                    kind="error", reason="config",
                    note=(f"Save To is '{target}' (camera buffer): with local download OFF "
                          "these shots exist NOWHERE once the session ends — switch Save To "
                          "to the memory card."),
                )
        return self.status()

    def _recompute_capture_dir(self) -> None:
        from abstractcamera.identity import default_capture_root

        with self._state_lock:
            sequence_name = self._sequence_name
            if self._capture_dir_override:
                base = self._capture_dir_override
            else:
                root = self._capture_root or default_capture_root()
                base = os.path.join(root, self._device_slug) if self._device_slug else None
            self._capture_dir = os.path.join(base, sequence_name) \
                if (base and sequence_name) else base

    def set_device_slug(self, slug: str) -> None:
        """Hub hook: final device folder name (serial/index suffix when two
        identical bodies are connected)."""
        with self._state_lock:
            self._device_slug = slug
        self._recompute_capture_dir()

    def list_cameras(self) -> list[dict]:
        """Non-invasive enumeration across transports (no device is opened).
        A fixed driver (test seam) lists only its own cameras."""
        if self._fixed_driver is not None:
            entries = list(self._fixed_driver.list_cameras())
            for index, entry in enumerate(entries):
                entry["default"] = index == 0
            return entries
        return discovery.list_cameras()

    def detect_cameras(self) -> list[dict]:
        """Legacy PTP-shaped listing ({name, address}); prefer list_cameras()."""
        entries = self.list_cameras()
        return [{"name": e["name"], "address": e.get("address", e["id"])} for e in entries]

    def connect(self, camera_id: str | None = None) -> dict:
        with self._state_lock:
            if self._connected:
                return self.status()
        # Driver resolution happens per-connect (env/registry) unless a fixed
        # driver was injected; prepare_connect runs transport-specific setup
        # (PTP: release macOS camera daemons) at the exact historic point.
        driver = self._fixed_driver or discovery.resolve_driver_for(camera_id)
        with self._state_lock:
            self._driver = driver
            self._camera_id = camera_id
        driver.prepare_connect(camera_id)
        self._stop_requested.clear()
        self._trigger_requested.clear()
        started = threading.Event()
        failure: list[str] = []
        worker = threading.Thread(
            target=self._worker_main,
            args=(started, failure),
            name="camera-liveview",
            daemon=True,
        )
        worker.start()
        if not started.wait(timeout=20.0):
            self._stop_requested.set()
            raise CameraControlError("Timed out while connecting to the camera.")
        if failure:
            raise CameraControlError(failure[0])
        with self._state_lock:
            self._worker = worker
        return self.status()

    def disconnect(self) -> dict:
        self._stop_requested.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=10.0)
        with self._state_lock:
            self._worker = None
            self._connected = False
            self._liveview_running = False
            self._latest_frame = None
            self._measured_fps = 0.0
            self._adapter = None
            self._capabilities = None
            # Re-resolve on the next connect (env/registry may have changed);
            # a fixed driver (test seam) stays fixed. Identity clears with
            # the connection — a different body may claim this manager next.
            self._driver = self._fixed_driver
            self._camera_id = None
            self._device_slug = None
            self._device_serial = None
        return self.status()

    def request_trigger(self) -> dict:
        if not self._connected:
            raise CameraControlError("No camera is connected.")
        with self._state_lock:
            sequence = self._interval_sequence
            if sequence is not None and sequence.is_active:
                # Single trigger arbiter: a manual fire mid-exposure corrupts
                # the sequence's per-shot accounting and can NAK the camera.
                raise CameraControlError("An interval sequence is running — stop it to fire manually.")
        self._trigger_requested.set()
        return {"status": "trigger-requested"}

    def request_action(self, name: str, value: str | None = None) -> dict:
        """Queue a one-shot camera ACTION (focus drive). Executed exactly once
        by the worker; never cached, never replayed (see ACTION_WIDGET_NAMES)."""
        if not self._connected:
            raise CameraControlError("No camera is connected.")
        adapter = self._adapter
        allowed = tuple(ACTION_WIDGET_NAMES) + (
            adapter.family_action_names() if adapter is not None else ())
        if name not in allowed:
            raise CameraControlError(f"Unsupported camera action: {name}")
        with self._state_lock:
            sequence = self._interval_sequence
            if sequence is not None and sequence.is_active:
                # Refusing beats queueing: drives deferred by the safe window
                # would replay as one physical lens slam at sequence end.
                raise CameraControlError("An interval sequence is running — stop it before driving focus.")
            if len(self._pending_actions) >= 16:
                raise CameraControlError("Too many queued focus actions — wait for the camera to catch up.")
            self._pending_actions.append((name, value))
        return {"status": "action-queued", "name": name, "value": value}

    def refresh_config_from_camera(self, timeout: float = 2.5) -> bool:
        """Ask the worker to re-read the camera's config tree and wait for
        it. Validating against a stale cache would trust a shutter value the
        user has since changed on the physical dial (breaker finding)."""
        if not self._connected:
            return False
        with self._state_lock:
            version_before = self._config_cache_version
        self._config_refresh_requested.set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._state_lock:
                if self._config_cache_version != version_before:
                    return True
            time.sleep(0.02)
        return False

    def start_interval_sequence(
        self,
        *,
        interval_s: float,
        count: int,
        start_delay_s: float = 0.0,
        liveview: bool = True,
        sequence_name: str | None = None,
    ) -> dict:
        """Arm the intervalometer. Validation is strict and specific; the
        worker loop owns all subsequent state transitions. The start lock
        makes check-validate-assign atomic (two concurrent starts used to
        both pass the is-active check and clobber each other)."""
        if not self._connected:
            raise CameraControlError("No camera is connected.")
        if sequence_name is not None:
            # A named timelapse: every frame + the manifest land in
            # <device_dir>/<sequence_name>/ (persists until changed).
            self.set_sequence_name(sequence_name)
        if not self._interval_start_lock.acquire(blocking=False):
            raise CameraControlError("Another sequence start is already in progress.")
        try:
            with self._state_lock:
                existing = self._interval_sequence
                if existing is not None and existing.is_active:
                    raise CameraControlError("A sequence is already running — stop it first.")

            # Re-read the physical dials before trusting them.
            self.refresh_config_from_camera()

            with self._state_lock:
                shutter_entry = self._config_cache.get("shutterspeed") or self._config_cache.get("shutterspeed2")
                quality_entry = self._config_cache.get("imagequality")
                target_entry = self._config_cache.get("capturetarget")
                program_entry = self._config_cache.get("expprogram")
                capture_mode = self._capture_mode
                movie_recording = self._movie_recording

            # Families without a shutter widget (webcams: the effective
            # exposure is the frame interval) declare a nominal exposure;
            # None keeps the widget-parsing path byte-identical, including
            # the Bulb/'0/0' refusals.
            exposure_override = None
            adapter_now = self._adapter
            if adapter_now is not None:
                with self._state_lock:
                    cache_for_exposure = dict(self._config_cache)
                exposure_override = adapter_now.nominal_exposure_s(cache_for_exposure)

            try:
                exposure_s, warning = validate_sequence_request(
                    interval_s=interval_s,
                    count=count,
                    start_delay_s=start_delay_s,
                    shutter_value=(shutter_entry or {}).get("value"),
                    capture_mode=capture_mode,
                    movie_recording=movie_recording,
                    imagequality=(quality_entry or {}).get("value"),
                    capturetarget=(target_entry or {}).get("value"),
                    exposure_s_override=exposure_override,
                )
            except IntervalValidationError as exc:
                raise CameraControlError(str(exc)) from exc

            program = str((program_entry or {}).get("value") or "")
            if program and program.upper() not in ("M", "MANUAL"):
                extra = (
                    f"exposure was validated once at {exposure_s:g}s, but in {program} mode the camera "
                    "may auto-vary it during the sequence — M mode is recommended."
                )
                warning = f"{warning} {extra}" if warning else extra

            # Family-specific preflight honesty (Sony: AF focus modes can
            # silently skip frames when focus can't lock).
            adapter = self._adapter
            if adapter is not None:
                with self._state_lock:
                    cache_snapshot = dict(self._config_cache)
                for extra in adapter.sequence_preflight_warnings(cache_snapshot):
                    warning = f"{warning} {extra}" if warning else extra

            sequence = IntervalSequence(
                interval_s=float(interval_s),
                count=int(count),
                start_delay_s=float(start_delay_s),
                liveview=bool(liveview),
                exposure_s=exposure_s,
            )
            with self._state_lock:
                existing = self._interval_sequence
                if existing is not None and existing.is_active:
                    raise CameraControlError("A sequence is already running — stop it first.")
                self._interval_sequence = sequence
                self._interval_stop_requested = False
                # Detection auto-fire is a competing trigger initiator: demote
                # it to monitor for the sequence (detections stay logged) and
                # restore it at any terminal state.
                if self._detection_mode == "auto":
                    self._saved_detection_mode = "auto"
                    self._detection_mode = "monitor"
                else:
                    self._saved_detection_mode = None
        finally:
            self._interval_start_lock.release()
        self._open_sequence_manifest(sequence)
        self._append_event(
            kind="trigger",
            reason="interval",
            note=(
                f"sequence armed: {count if count else '∞'} frames @ {interval_s:g}s"
                + (f", starts in {start_delay_s:g}s" if start_delay_s else "")
            ),
        )
        response = {"status": "sequence-armed", "interval": sequence.to_status()}
        if warning:
            response["warning"] = warning
        return response

    def stop_interval_sequence(self) -> dict:
        with self._state_lock:
            self._interval_stop_requested = True
        return self.status()

    def set_detection_mode(self, mode: str, target: str | None = None,
                           sensitivity: float | None = None) -> dict:
        if mode not in ("off", "monitor", "auto"):
            raise CameraControlError(f"Unknown detection mode: {mode}")
        if target is not None and target not in ("lightning", "meteor", "motion"):
            raise CameraControlError(f"Unknown detection target: {target}")
        with self._state_lock:
            sequence = self._interval_sequence
            if mode == "auto" and sequence is not None and sequence.is_active:
                # Trigger arbitration is not optional: flipping auto-fire on
                # mid-sequence re-enables a competing trigger initiator
                # (breaker CRITICAL). Monitor keeps the detections logged.
                raise CameraControlError(
                    "An interval sequence is running — Auto-Fire is disabled until it ends (use Monitor)."
                )
            previous_mode = self._detection_mode
            self._detection_mode = mode
            if previous_mode == "auto" and mode != "auto":
                # Leaving Auto-Fire: download everything deferred while armed.
                self._download_flush_requested.set()
            if target is not None and target != self._detection_target:
                self._detection_target = target
                # Each target owns its state; reset on switch.
                self._meteor_detector = None
                self._motion_detector = None
                self._preview_ring.clear()
            if sensitivity is not None:
                s = float(np.clip(sensitivity, 0.0, 100.0))
                self._detection_sensitivity[self._detection_target] = s
                if self._meteor_detector is not None and self._detection_target == "meteor":
                    self._meteor_detector.set_sensitivity(s)
                if self._motion_detector is not None and self._detection_target == "motion":
                    self._motion_detector.set_sensitivity(s)
        return self.status()

    def set_capture_mode(self, mode: str, burst_count: int | None = None,
                         burst_hold_s: float | None = None,
                         burst_speed: str | None = None) -> dict:
        """Select what the trigger does: one still, a burst, or start/stop video.

        The family adapter translates the mode into the body's widget writes
        (Nikon: Burst drive + frame count; Sony: Continuous drive + a
        press-and-hold duration); the actual shutter/record action still goes
        through the worker on trigger.
        """
        if mode not in ("single", "burst", "video"):
            raise CameraControlError(f"Unknown capture mode: {mode}")
        if not self._connected:
            raise CameraControlError("No camera is connected.")
        with self._state_lock:
            sequence = self._interval_sequence
            if sequence is not None and sequence.is_active and mode != "single":
                raise CameraControlError("An interval sequence is running — it requires Single capture mode.")
        with self._state_lock:
            self._capture_mode = mode
            if burst_count is not None:
                self._burst_count = max(2, min(200, int(burst_count)))
            if burst_hold_s is not None:
                self._burst_hold_s = float(np.clip(float(burst_hold_s), 0.2, 5.0))
            if burst_speed is not None and burst_speed in ("Lo", "Mid", "Hi", "Hi+"):
                self._burst_speed = burst_speed
            plan = self._adapter.capture_mode_plan(
                mode, self._burst_count, self._burst_hold_s, self._burst_speed)
            for widget_name, widget_value in plan.items():
                self._pending_config[widget_name] = widget_value
        return self.status()

    def get_latest_frame(self) -> tuple[bytes | None, int]:
        with self._state_lock:
            return self._latest_frame, self._latest_frame_seq

    def get_events(self, since_id: int = 0) -> list[dict]:
        with self._state_lock:
            return [dict(event) for event in self._events if event["id"] > since_id]

    def clear_events(self) -> None:
        with self._state_lock:
            self._events.clear()

    def status(self) -> dict:
        with self._state_lock:
            sequence = self._interval_sequence
            return {
                # ANY transport usable (fake env, PTP, webcam) — the honest
                # answer to "can this machine do camera work at all".
                "available": (self._fixed_driver is not None
                              or discovery.any_transport_available()),
                "camera_id": self._camera_id,
                "transport": self._driver.driver_id if self._driver is not None else None,
                # Device identity + capture layout + save policy.
                "device_slug": self._device_slug,
                "device_serial": self._device_serial,
                "capture_dir": self._capture_dir,
                "sequence_name": self._sequence_name,
                "download_locally": self._download_locally,
                "connected": self._connected,
                "model": self._camera_model,
                "address": self._camera_address,
                "liveview_running": self._liveview_running,
                "fps": round(self._measured_fps, 1),
                "detection_mode": self._detection_mode,
                "detection_target": self._detection_target,
                "detection_sensitivity": self._detection_sensitivity.get(self._detection_target, 50.0),
                "detection_active": (
                    self._detection_mode != "off"
                    and self._connected
                    and self._detection_paused_reason is None
                ),
                "detection_paused_reason": self._detection_paused_reason,
                # Deferred-download queue: shots announced but not yet pulled
                # from the camera (Auto-Fire keeps the worker detecting).
                "downloads_pending": self._downloads_pending,
                "downloads_deferred": self._detection_mode == "auto" and self._downloads_pending > 0,
                "rolling": {
                    "enabled": self._rolling_enabled,
                    "seconds": self._rolling_seconds,
                    "buffered_s": self._rolling_buffered_seconds(),
                },
                "capture_mode": self._capture_mode,
                "burst_count": self._burst_count,
                "burst_hold_s": self._burst_hold_s,
                "burst_speed": self._burst_speed,
                "movie_recording": self._movie_recording,
                "event_count": len(self._events),
                "last_error": self._last_error,
                "config": dict(self._config_cache),
                # The honest resolution signal: the size of frames ACTUALLY
                # flowing (config confirms ~6s before frames change).
                "preview_size": list(self._preview_size) if self._preview_size else None,
                # Pending-write ledger (write-revert honesty): the UI keeps
                # showing the user's choice until confirmed/reverted here.
                "pending_writes": {
                    name: {"value": entry["value"], "state": entry["state"],
                           "actual": entry.get("actual")}
                    for name, entry in self._pending_writes.items()
                },
                # Sequence ledger: counters survive the frontend's tab-hidden
                # phases trivially (unlike catch-log events, which evict).
                "interval": sequence.to_status() if sequence is not None else {"state": "idle"},
                # Canonical focus actions plus this family's own (a
                # telescope adds mount actions; see family_action_names).
                "actions": list(ACTION_WIDGET_NAMES) + (
                    list(self._adapter.family_action_names())
                    if self._adapter is not None else []),
                # Family descriptor: what THIS body offers, so the UI adapts
                # (burst count vs duration, movie confirmability, ISO Auto
                # story, Save To vocabulary...). None until connected.
                "family": self._adapter.family if self._adapter is not None else None,
                "capabilities": dict(self._capabilities) if self._capabilities else None,
            }

    def set_config_value(self, name: str, value: str) -> dict:
        """Queue-free config set: only valid while the worker owns the camera.

        Config writes are rare (pre-arm step), so we set a pending request the
        worker applies between frames rather than locking the camera object.
        """
        if not self._connected:
            raise CameraControlError("No camera is connected.")
        adapter = self._adapter
        allowed = adapter.config_widget_names() if adapter is not None else CONFIG_WIDGET_NAMES
        if name not in allowed:
            raise CameraControlError(f"Unsupported config field: {name}")
        with self._state_lock:
            self._pending_config[name] = value
            # Ledger entry: confirmed when the cache reports the value,
            # REVERTED when the camera keeps a different stable value —
            # dial-controlled widgets silently revert while lying
            # readonly=false (hardware-measured on the Z6 II in U2).
            self._pending_writes[name] = {
                "value": str(value),
                "requested_at": time.time(),
                "state": "pending",
                "actual": None,
                "mismatches": 0,
            }
        return {"status": "config-queued", "name": name, "value": value}

    # ------------------------------------------------------------------
    # Worker thread: owns every gphoto2 call
    # ------------------------------------------------------------------

    def _append_event(
        self,
        *,
        kind: str,
        reason: str,
        note: str = "",
        score: float | None = None,
        thumbnail_jpeg: bytes | None = None,
        path: str | None = None,
    ) -> None:
        thumbnail_data_url = None
        if thumbnail_jpeg is not None:
            try:
                array = np.frombuffer(thumbnail_jpeg, dtype=np.uint8)
                frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
                if frame is not None:
                    height, width = frame.shape[:2]
                    scale = EVENT_THUMBNAIL_MAX_EDGE / max(height, width, 1)
                    if scale < 1.0:
                        frame = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
                    ok, encoded = cv2.imencode(".jpg", frame)
                    if ok:
                        import base64

                        thumbnail_data_url = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")
            except Exception:
                thumbnail_data_url = None

        with self._state_lock:
            self._event_counter += 1
            self._events.appendleft(
                {
                    "id": self._event_counter,
                    "kind": kind,
                    "reason": reason,
                    "note": note,
                    "score": score,
                    "timestamp": time.time(),
                    "thumbnail": thumbnail_data_url,
                    "path": path,
                }
            )
