"""Fire paths (manual, sequence), capture-timing window folding, movie
toggles with wedge recovery, and per-sequence JSONL manifests."""

from __future__ import annotations

import json
import os
import time

from abstractcamera.adapters import CaptureTiming
from abstractcamera.sequences import IntervalSequence, parse_shutter_speed_seconds
from abstractcamera.worker import sequence_active_now


class CaptureOpsMixin:

    def _fire_sequence_shot(self, camera, sequence: IntervalSequence) -> None:
        """Fire one scheduled frame and account for it in the ledger."""
        shot_index = sequence.next_index + 1
        deadline = sequence.deadline()
        try:
            # HARDWARE-MEASURED (Nikon Z6 II, 2026-07-07): trigger_capture
            # BLOCKS for the whole exposure (1.6s exposure -> 1.76s call);
            # Sony bodies return in ~1.2s regardless. The adapter anchors
            # issued_at at command ISSUE either way — anchoring at return
            # would report every Nikon frame "late" by one exposure.
            with self._state_lock:
                cache_snapshot = dict(self._config_cache)
            timing = self._adapter.fire_single(camera, sequence.exposure_s, cache_snapshot)
            fired_at = timing.issued_at
            with self._state_lock:
                self._last_trigger_at = fired_at
                record = sequence.record_fired(fired_at)
            self._apply_capture_timing(timing)
            # Pause preview around the exposure on families whose preview
            # fails during exposures (Nikon; the window is already spent if
            # trigger_capture blocked through it — exactly right). Sequence
            # shots pause unconditionally: back-to-back frames leave no room
            # for a partial preview between exposure end and the next slot.
            if not self._adapter.preview_survives_exposure:
                self._preview_pause_until = max(
                    self._preview_pause_until, fired_at + sequence.exposure_s + 0.5
                )
            self._write_manifest_line(sequence, {
                "shot": record.index, "deadline": deadline, "fired_at": fired_at,
                "result": "fired", "command_ms": timing.command_ms,
            })
            total = sequence.count if sequence.count else "∞"
            self._append_event(kind="trigger", reason="interval",
                               note=f"frame {record.index}/{total} ({timing.command_ms}ms)")
        except Exception as exc:
            with self._state_lock:
                record = sequence.record_failed(str(exc))
            self._write_manifest_line(sequence, {
                "shot": record.index, "deadline": deadline,
                "result": "failed", "note": str(exc),
            })
            # Bare [-1]s hide real causes (unformatted card...): ask the
            # family for a diagnosis before logging.
            diagnosis = self._adapter.diagnose_trigger_failure(camera, str(exc))
            self._append_event(kind="error", reason="interval",
                               note=f"frame {shot_index} trigger failed: {exc}"
                                    + (f" — {diagnosis}" if diagnosis else ""))
        if not sequence.is_active:
            self._finish_sequence(
                sequence,
                sequence.last_error if sequence.state == "aborted" else "complete",
            )

    def _finish_sequence(self, sequence: IntervalSequence, note: str) -> None:
        """Terminal-state bookkeeping: restore detection mode, close the
        manifest, and leave the ledger visible in status()."""
        with self._state_lock:
            saved_mode = self._saved_detection_mode
            self._saved_detection_mode = None
            if saved_mode is not None and self._detection_mode == "monitor":
                self._detection_mode = saved_mode
        self._write_manifest_line(sequence, {
            "sequence": sequence.state,
            "shots_done": sequence.shots_done,
            "shots_failed": sequence.shots_failed,
            "shots_missed": sequence.shots_missed,
            "note": note,
        })
        summary = (
            f"sequence {sequence.state}: {sequence.shots_done} fired"
            + (f", {sequence.shots_failed} failed" if sequence.shots_failed else "")
            + (f", {sequence.shots_missed} missed" if sequence.shots_missed else "")
        )
        kind = "error" if sequence.state == "aborted" else "trigger"
        self._append_event(kind=kind, reason="interval", note=summary)
        # Keep downloading whatever the camera still owes us.
        self._event_drain_until = max(self._event_drain_until, time.time() + 20.0)

    def _open_sequence_manifest(self, sequence: IntervalSequence) -> None:
        """Append-only JSONL record per sequence: survives crashes as a
        readable account of the night (shot deadlines, results, files).
        The path lives ON the sequence — a shared controller field let a
        finish/restart race write one sequence's footer into another's file."""
        if not self._capture_dir:
            return
        try:
            manifest_dir = os.path.join(self._capture_dir, "sequences")
            os.makedirs(manifest_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            sequence.manifest_path = os.path.join(manifest_dir, f"sequence_{stamp}_{id(sequence) & 0xffff:04x}.jsonl")
            self._write_manifest_line(sequence, {
                "sequence": "armed",
                "interval_s": sequence.interval_s,
                "count": sequence.count,
                "start_delay_s": sequence.start_delay_s,
                "exposure_s": sequence.exposure_s,
                "started_at": sequence.started_at,
            })
        except Exception:
            sequence.manifest_path = None

    @staticmethod
    def _write_manifest_line(sequence: IntervalSequence, payload: dict) -> None:
        path = sequence.manifest_path
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(time.time(), 3), **payload}) + "\n")
        except Exception:
            pass

    def _apply_capture_timing(self, timing: CaptureTiming) -> None:
        """Fold one fire's CaptureTiming into the controller's windows.
        max(): never SHORTEN a window another shot already opened (F1);
        replace semantics exist only for the Nikon burst pause, which was
        always an assignment anchored at command return."""
        self._event_drain_until = max(
            self._event_drain_until, timing.issued_at + timing.drain_window_s)
        if timing.preview_pause_s > 0.0:
            if timing.preview_pause_replace:
                self._preview_pause_until = timing.issued_at + timing.preview_pause_s
            else:
                self._preview_pause_until = max(
                    self._preview_pause_until, timing.issued_at + timing.preview_pause_s)
        if timing.expected_files is not None:
            self._burst_photos_pending = timing.expected_files
        if timing.expect_file_within_s is not None:
            with self._state_lock:
                self._expect_file_deadline = timing.issued_at + timing.expect_file_within_s
                self._expect_file_note = timing.no_file_note

    def _current_exposure_estimate_s(self) -> float:
        """Exposure from the cached shutter value; 0.0 when unparseable
        (Bulb/'0/0'/absent) — windows then cover only the readout tail."""
        try:
            shutter_entry = (
                self._config_cache.get("shutterspeed")
                or self._config_cache.get("shutterspeed2")  # some bodies expose only this
                or {}
            )
            return parse_shutter_speed_seconds(shutter_entry.get("value"))
        except Exception:
            return 0.0

    def _fire_trigger(self, camera, *, reason: str, score: float | None = None) -> None:
        if self._capture_mode == "video":
            self._toggle_movie_recording(camera, reason=reason)
            return
        try:
            # Captured files announce themselves via camera events AFTER the
            # exposure ends: the adapter derives an exposure-aware drain
            # window, or long exposures strand on the camera (F1).
            exposure_s = self._current_exposure_estimate_s()
            with self._state_lock:
                cache_snapshot = dict(self._config_cache)
            if self._capture_mode == "burst":
                timing = self._adapter.fire_burst(
                    camera, exposure_s, self._burst_count, self._burst_hold_s)
            else:
                timing = self._adapter.fire_single(camera, exposure_s, cache_snapshot)
            with self._state_lock:
                self._last_trigger_at = timing.issued_at
            self._apply_capture_timing(timing)
            label = "burst" if self._capture_mode == "burst" else "shutter"
            self._append_event(
                kind="trigger",
                reason=reason,
                note=f"{label} command {timing.command_ms}ms",
                score=score,
            )
        except Exception as exc:
            diagnosis = self._adapter.diagnose_trigger_failure(camera, str(exc))
            detail = f"{exc}" + (f" — {diagnosis}" if diagnosis else "")
            with self._state_lock:
                self._last_error = f"Trigger failed: {detail}"
            self._append_event(kind="error", reason=reason, note=f"trigger failed: {detail}")

    def _toggle_movie_recording(self, camera, *, reason: str) -> None:
        start = not self._movie_recording
        receipt = self._adapter.toggle_movie(camera, start)

        if receipt.refused:
            # Pre-check refusal (Nikon movieprohibit): nothing was written,
            # the body's own reasons are the message.
            with self._state_lock:
                self._last_error = f"Movie start refused by the camera: {receipt.error}"
            self._append_event(kind="error", reason=reason,
                               note=f"video start refused: {receipt.error}")
            return

        if receipt.ok:
            with self._state_lock:
                self._movie_recording = receipt.recording
            self._append_event(kind="trigger", reason=reason,
                               note=receipt.note or ("video recording started" if start
                                                     else "video recording stopped"))
            if receipt.drain_window_s > 0:
                self._event_drain_until = max(
                    self._event_drain_until, time.time() + receipt.drain_window_s)
            return

        # Toggle failed with a real exception.
        detail = receipt.error or "unknown error"
        with self._state_lock:
            self._last_error = f"Movie toggle failed: {detail}"
        self._append_event(
            kind="error",
            reason=reason,
            note=f"video {'start' if start else 'stop'} failed: {detail}" + receipt.hint,
        )
        if receipt.probe_session:
            # Wedge recovery (hardware-observed): a failed movie toggle can
            # leave the PTP session half-dead — the user's session ended
            # with a wedged camera and lost buffered captures. Probe once;
            # on failure, one in-place reconnect before the watchdog path.
            # The controller owns the session lifecycle, so recovery lives
            # here, not in the adapter.
            try:
                camera.capture_preview()
            except Exception:
                self._append_event(kind="error", reason="connection",
                                   note="movie failure destabilized the camera session — reconnecting")
                try:
                    camera.exit()
                except Exception:
                    pass
                try:
                    time.sleep(0.5)
                    camera.init()
                    self._refresh_config_cache(camera)
                    self._append_event(kind="trigger", reason="connection",
                                       note="camera session recovered")
                except Exception as reconnect_exc:
                    with self._state_lock:
                        self._last_error = f"Session recovery failed: {reconnect_exc}"
                    # The liveness watchdog will finish the disconnect.

    # Deferred downloads: force one download once the oldest queued file is
    # this old, even while armed (card-pressure/feedback safety valve).
    DEFERRED_DOWNLOAD_MAX_AGE_S = 120.0
