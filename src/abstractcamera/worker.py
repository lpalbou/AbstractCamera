"""The camera worker loop: one thread owns every session call.

Moved VERBATIM from the hardware-validated host controller; the only edits
are the adjudicated session-boundary swaps (driver-created sessions, wire
constants, driver-selected adapters) — see the migration ledger in the
package docs. Scheduling windows, drain budgets, watchdog thresholds, and
interleavings are the validated asset and must not be reordered.
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np

from abstractcamera.constants import (
    DETECTION_BASELINE_WINDOW_FRAMES,
    LIVEVIEW_FAILURE_DISCONNECT_THRESHOLD,
)
from abstractcamera.jpeg import parse_jpeg_dimensions


def sequence_active_now(controller) -> bool:
    sequence = controller._interval_sequence
    return sequence is not None and sequence.is_active


class WorkerLoopMixin:

    def _worker_main(self, started, failure: list[str]) -> None:
        # Session creation is driver-owned (ADR 0001): the driver targets the
        # requested camera id (port binding) or the transport default. A
        # stale id or a claim failure surfaces as the connect error.
        try:
            camera = self._driver.create_session(self._camera_id)
            camera.init()
        except Exception as exc:
            failure.append(f"Failed to connect to the camera: {exc}")
            started.set()
            return

        try:
            model = "Camera"
            try:
                abilities = camera.get_abilities()
                model = abilities.model or "Camera"
            except Exception:
                pass
            # Family dispatch: one adapter instance per connection, attached
            # with an event sink so any events the adapter pumps internally
            # (write verification, burst holds) still reach the announce/log
            # paths — a swallowed FILE_ADDED would lose a shot.
            try:
                adapter = self._driver.select_adapter(model)
                adapter.attach(
                    camera,
                    lambda event_type, event_data: self._route_adapter_event(
                        camera, event_type, event_data),
                )
            except Exception as exc:
                failure.append(f"Camera family setup failed: {exc}")
                started.set()
                return
            with self._state_lock:
                self._adapter = adapter
            self._refresh_config_cache(camera)
            # Device identity for the capture layout (<root>/<device_slug>/)
            # and multi-body disambiguation: model slug + best-effort serial.
            from abstractcamera.identity import slugify

            serial = adapter.read_serial(camera)
            with self._state_lock:
                if self._device_slug is None:
                    self._device_slug = slugify(model)
                self._device_serial = serial
            self._recompute_capture_dir()
            with self._state_lock:
                self._connected = True
                self._camera_model = model
                self._liveview_running = True
                self._last_error = None
            started.set()

            # Family-specific sane defaults at connect (one queued write each,
            # with an explicit event; later user choices stand). Nikon:
            # RAM->card, isoauto Off. Sony: prioritymode Application,
            # sdram->card+sdram.
            with self._state_lock:
                cache_snapshot = dict(self._config_cache)
            for default in adapter.connect_default_writes(cache_snapshot):
                with self._state_lock:
                    self._pending_config[default.name] = default.value
                    if default.ledger:
                        self._pending_writes[default.name] = {
                            "value": str(default.value), "requested_at": time.time(),
                            "state": "pending", "actual": None, "mismatches": 0,
                        }
                self._append_event(kind="trigger", reason="config", note=default.note)
            # Body-state warnings (unformatted card...): failures these cause
            # are cryptic ([-1] on every trigger) — say the cause up front.
            for warning in adapter.connect_warnings(cache_snapshot):
                self._append_event(kind="error", reason="camera", note=warning)

            fps_window: deque[float] = deque(maxlen=120)
            previous_gray: np.ndarray | None = None
            baseline_means: deque[float] = deque(maxlen=DETECTION_BASELINE_WINDOW_FRAMES)
            consecutive_preview_failures = 0
            last_pending_refresh_at = 0.0
            detection_frame_toggle = False

            while not self._stop_requested.is_set():
                sequence = self._interval_sequence

                # ---- interval sequence servicing (absolute deadlines) ----
                # Sequence mutations happen under the state lock: status()
                # serializes the ledger under it, and lock-free writes let it
                # observe torn states (shots_done ahead of next_index).
                if sequence is not None and sequence.is_active:
                    if self._interval_stop_requested:
                        self._interval_stop_requested = False
                        with self._state_lock:
                            sequence.stop()
                        self._finish_sequence(sequence, "stopped by user")
                        continue

                    with self._state_lock:
                        verdict = sequence.poll()
                    if verdict == "fire":
                        self._fire_sequence_shot(camera, sequence)
                        continue
                    if verdict == "skip":
                        with self._state_lock:
                            record = sequence.record_missed()
                        self._write_manifest_line(sequence, {
                            "shot": record.index, "deadline": record.deadline,
                            "result": "missed", "note": record.note,
                        })
                        self._append_event(kind="error", reason="interval",
                                           note=f"shot {record.index} missed (worker was blocked past the slot)")
                        if not sequence.is_active:
                            self._finish_sequence(sequence, "complete")
                        continue
                    if verdict == "done":
                        self._finish_sequence(sequence, "complete")
                        continue

                    # verdict == "wait": inside the pre-deadline quiet window,
                    # skip preview/config/drain and sleep precisely until due
                    # (a preview costs 20-100ms — enough to blow a deadline).
                    time_left = sequence.deadline() - time.time()
                    if time_left <= 0.25:
                        time.sleep(max(0.0, min(time_left, 0.05)))
                        continue

                # Safe window (F7): during an active sequence, config writes
                # (0.5-1.5s transactions on real bodies) only land when the
                # next deadline is comfortably far. The threshold must be
                # interval-aware: a fixed 2s window NEVER opens at interval
                # <= 2s and starved every config/focus request for the whole
                # sequence, then replayed them as a burst at the end
                # (breaker finding).
                sequence_active = sequence is not None and sequence.is_active
                config_safe_margin = 2.0 if not sequence_active else min(2.0, sequence.interval_s * 0.5)
                config_budget_ok = (
                    not sequence_active
                    or (sequence.deadline() - time.time()) > config_safe_margin
                )
                # Silent-refusal honesty (family-flagged, set at fire time):
                # the shot window expired with no FILE_ADDED — say so once.
                if self._expect_file_deadline and time.time() > self._expect_file_deadline:
                    note = self._expect_file_note or "no file arrived after the trigger"
                    with self._state_lock:
                        self._expect_file_deadline = 0.0
                        self._expect_file_note = None
                    self._append_event(kind="error", reason="trigger", note=note)

                if config_budget_ok:
                    if self._config_refresh_requested.is_set():
                        self._config_refresh_requested.clear()
                        self._refresh_config_cache(camera)
                    self._apply_pending_config(camera)
                    self._apply_pending_actions(camera)
                    self._service_write_escalations(camera)
                    # Pending-write settlement needs periodic re-reads: the
                    # Nikon applies lazily (~5-7s) and reverts silently.
                    # (Settled entries also age out via _settle, so run it
                    # whenever the ledger is non-empty.)
                    with self._state_lock:
                        has_entries = bool(self._pending_writes)
                        has_unsettled = any(
                            entry["state"] == "pending" for entry in self._pending_writes.values()
                        )
                    # While Auto-Fire is armed, periodic settlement re-reads
                    # (0.26s each) are paused — they steal preview frames at
                    # exactly the moment detection matters most (referee
                    # finding). Explicit refresh requests still run above.
                    if (
                        has_entries
                        and time.time() - last_pending_refresh_at > 2.5
                        and self._detection_mode != "auto"
                    ):
                        last_pending_refresh_at = time.time()
                        if has_unsettled:
                            self._refresh_config_cache(camera)
                        else:
                            with self._state_lock:
                                cache_now = dict(self._config_cache)
                            self._settle_pending_writes(cache_now)

                if self._trigger_requested.is_set():
                    self._trigger_requested.clear()
                    if sequence_active_now(self):
                        # Belt-and-braces arbitration: a flag set in the race
                        # window just before arming must not fire mid-sequence.
                        self._append_event(kind="error", reason="interval",
                                           note="manual trigger ignored — a sequence is running")
                    else:
                        self._fire_trigger(camera, reason="manual")

                # "The deadline wins" must hold INSIDE the drain too: one 25MB
                # NEF file_get blocks 1-3s on real USB, so every drain call
                # gets a hard time budget derived from the next deadline
                # (breaker finding: an unbounded drain in the pause branch
                # blew 2s-interval slots).
                drain_time_budget = (
                    (sequence.deadline() - time.time()) - 0.6 if sequence_active else 10.0
                )

                # DOWNLOAD POLICY (adversarial verdict 2026-07-07): while
                # Auto-Fire is armed, the worker only ANNOUNCES captures
                # (~30ms poll) and never file_gets — a 1-3s NEF download on
                # this thread blinded detection after every shot, which is
                # exactly when re-strikes/second movements happen. Files stay
                # safe on the card; the queue flushes on disarm, on a quiet
                # non-armed loop, or via the age valve below.
                armed_auto = self._detection_mode == "auto" and not sequence_active

                # Bursts abort if live view keeps pulling preview frames, so
                # the loop switches to pure event draining until the burst
                # (and its downloads) are done.
                if time.time() < self._preview_pause_until:
                    self._detection_paused_reason = "exposure in progress"
                    if armed_auto:
                        self._poll_capture_events(camera)
                    elif drain_time_budget > 0.2:
                        self._drain_capture_events(camera, time_budget_s=drain_time_budget)
                    time.sleep(0.02)
                    continue

                # Downloads vs deadlines: the deadline wins. Drain events only
                # when the budget allows; with capturetarget=Card nothing is
                # lost by waiting (F1: window is exposure-aware, set at fire).
                if time.time() < self._event_drain_until:
                    if armed_auto:
                        self._poll_capture_events(camera)
                    elif drain_time_budget > 1.0:
                        # Honesty: this drain can block for seconds — the UI
                        # must not claim detection is live during it.
                        if self._detection_mode != "off":
                            self._detection_paused_reason = "downloading capture"
                        self._drain_capture_events(camera, time_budget_s=drain_time_budget)

                # Deferred-queue servicing outside the drain window.
                if self._downloads_pending:
                    flush_now = self._download_flush_requested.is_set() or not armed_auto
                    aged_out = self._oldest_pending_download_age() > self.DEFERRED_DOWNLOAD_MAX_AGE_S
                    if flush_now and (not sequence_active or drain_time_budget > 1.5):
                        self._download_flush_requested.clear()
                        if self._detection_mode != "off":
                            self._detection_paused_reason = "downloading capture"
                        self._flush_pending_downloads(
                            camera,
                            time_budget_s=(10.0 if not sequence_active else drain_time_budget),
                        )
                    elif aged_out:
                        # Age valve: one bounded download so an endless armed
                        # session still surfaces files eventually.
                        with self._state_lock:
                            head = self._pending_downloads[0] if self._pending_downloads else None
                        if head is not None:
                            self._download_one_pending(camera, head[0], head[1])
                            with self._state_lock:
                                if self._pending_downloads and self._pending_downloads[0] == head:
                                    self._pending_downloads.popleft()
                                self._pending_download_keys.discard((head[0], head[1]))
                                self._downloads_pending = len(self._pending_downloads)

                liveview_wanted = not sequence_active or sequence.liveview
                if not liveview_wanted:
                    self._detection_paused_reason = "live view off during the sequence"
                    time.sleep(0.05)
                    continue
                self._detection_paused_reason = None

                frame_started = time.perf_counter()
                try:
                    cam_file = camera.capture_preview()
                    jpeg = bytes(cam_file.get_data_and_size())
                    consecutive_preview_failures = 0
                except Exception as exc:
                    consecutive_preview_failures += 1
                    with self._state_lock:
                        self._last_error = f"Live view frame failed: {exc}"
                    # Liveness watchdog (F8): a pulled cable used to leave
                    # "connected: true" forever and any sequence stuck.
                    if consecutive_preview_failures >= LIVEVIEW_FAILURE_DISCONNECT_THRESHOLD:
                        if sequence is not None and sequence.is_active:
                            with self._state_lock:
                                sequence.abort(f"camera lost at shot {sequence.shots_done}")
                            self._finish_sequence(
                                sequence,
                                f"aborted at {sequence.shots_done}/{sequence.count or '∞'} — camera lost",
                            )
                        self._append_event(kind="error", reason="connection",
                                           note="camera stopped responding — disconnected")
                        break
                    time.sleep(0.25)
                    continue

                fps_window.append(time.perf_counter())
                if len(fps_window) >= 2:
                    span = fps_window[-1] - fps_window[0]
                    fps = (len(fps_window) - 1) / span if span > 0 else 0.0
                else:
                    fps = 0.0

                with self._state_lock:
                    self._latest_frame = jpeg
                    self._latest_frame_seq += 1
                    self._measured_fps = fps

                # The preview ring feeds detection event clips AND the rolling
                # pre-capture buffer; appending here (not in the detection
                # path) keeps it filling whenever frames flow.
                if self._rolling_enabled or self._detection_mode != "off":
                    self._preview_ring.append((time.time(), jpeg))

                # preview_size truth signal: SOF parse (no decode). Two
                # consecutive frames at a new size before reporting it —
                # a transient malformed frame must not trigger reattaches.
                dims = parse_jpeg_dimensions(jpeg)
                if dims is not None:
                    if dims == self._preview_size_candidate and dims != self._preview_size:
                        with self._state_lock:
                            self._preview_size = dims
                    self._preview_size_candidate = dims

                if self._detection_mode != "off":
                    # Budget policy: rolling mean of detection cost; above
                    # 8ms/frame, process every 2nd frame (temporal logic is
                    # dt-based, not frame-count-based).
                    detection_frame_toggle = not detection_frame_toggle
                    if not (self._detection_frame_skip and detection_frame_toggle):
                        detection_started = time.perf_counter()
                        if self._detection_target == "lightning":
                            previous_gray = self._run_detection(camera, jpeg, previous_gray, baseline_means)
                        else:
                            self._run_new_detection(camera, jpeg)
                        cost_ms = (time.perf_counter() - detection_started) * 1000.0
                        self._detection_cost_ms = 0.9 * self._detection_cost_ms + 0.1 * cost_ms
                        self._detection_frame_skip = self._detection_cost_ms > 8.0
                else:
                    previous_gray = None
                    baseline_means.clear()

                # Yield briefly so HTTP threads can read state; the preview
                # call itself paces the loop at the camera's max rate.
                elapsed = time.perf_counter() - frame_started
                if elapsed < 0.004:
                    time.sleep(0.004 - elapsed)
        finally:
            # Deferred files are downloaded before the session closes: files
            # remain on the card either way, but the user expects everything
            # they shot to land on this computer when they disconnect.
            try:
                if self._downloads_pending:
                    # Budget stays under disconnect()'s 10s worker join.
                    self._flush_pending_downloads(camera, time_budget_s=8.0, ignore_stop=True)
            except Exception:
                pass
            try:
                camera.exit()
            except Exception:
                pass
            with self._state_lock:
                sequence = self._interval_sequence
            if sequence is not None and sequence.is_active:
                with self._state_lock:
                    sequence.abort(f"worker stopped at shot {sequence.shots_done}")
                self._finish_sequence(sequence, f"aborted at shot {sequence.shots_done} — worker stopped")
            with self._state_lock:
                self._connected = False
                self._liveview_running = False
                self._measured_fps = 0.0

    def _route_adapter_event(self, camera, event_type, event_data) -> None:
        """Event sink handed to the adapter at attach(): events pumped inside
        adapter calls (write verification, burst holds) route through the
        same announce/log paths as the worker's own drains."""
        adapter = self._adapter
        if adapter is None:
            return
        classified = adapter.classify_event(event_type, event_data)
        if classified.kind == "file_added":
            self._announce_file_added(event_data, camera)
        elif classified.kind == "status" and classified.note:
            self._append_event(kind="camera-event", reason="camera", note=classified.note)
