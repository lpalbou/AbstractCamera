"""Sony Alpha (E-mount mirrorless) family adapter.

Every behavior here was measured on a REAL Sony A7R IV (ILCE-7RM4, fw 1.20,
libgphoto2 2.5.34, 2026-07-12; probe scripts in untracked/sony_probe/):

- Config writes are ACCEPTED in milliseconds but settle asynchronously
  0.3-1.7s later — and are sometimes silently lost (a shutterspeed write
  vanished with no error; the identical retry settled). Writes therefore
  run a write -> pump-events -> verify -> retry loop.
- Rapid consecutive writes raise GPhoto2Error [-2] 'Bad parameters' (the
  body is still applying the previous change); a retry after ~0.8s of
  event pumping succeeds.
- Drive-mode (capturemode) writes only settle under prioritymode=
  'Application'; exposure widgets settle under either priority. The
  adapter requests Application at connect so remote drive control works.
- trigger_capture blocks ~1.1-1.3s REGARDLESS of exposure length (the
  Nikon Z blocks through the whole exposure) and ~0.03s in Manual focus.
  In AF focus modes the body SILENTLY refuses to fire when focus cannot
  lock: no exception, no FILE_ADDED. The timing carries an expectation
  window so the controller can report the refusal honestly.
- Live view (1024x680 @ ~26.5fps) KEEPS RUNNING during still exposures:
  no preview pause is needed, so detection stays live between frames.
- Burst has no frame count: drive 'Continuous Shooting Lo/Mid/Hi/Hi+' and
  HOLD the `capture` toggle (press=1 ... release=0). A 1.2s hold at Hi
  produced 9 ARW files (trailing FILE_ADDEDs for ~10s after release).
- FILE_ADDED events arrive at folder '/' (capt_XXXXXXXX.ARW); file_get is
  near-instant because the driver spools the object during event pumping.
- No movieprohibit / recordingmedia / isoauto / liveviewsize / burstnumber
  widgets. 'Auto ISO' is an iso CHOICE. Movie start/stop toggles are
  accepted but recording is NOT confirmable over USB (no events, no
  status widget) — the receipts say so honestly.
- One full get_config()+label tree walk SEGFAULTED the process; only the
  single-config path is safe. attach() refuses bodies without it.
"""

from __future__ import annotations

import time

from abstractcamera.adapters.base import (
    ActionReceipt,
    CameraAdapter,
    CaptureTiming,
    ClassifiedEvent,
    ConnectDefault,
    MovieReceipt,
    WriteReceipt,
)

SONY_CONFIG_WIDGET_NAMES = (
    "iso",
    "shutterspeed",
    "f-number",
    "whitebalance",
    "colortemperature",
    "exposurecompensation",
    "expprogram",
    "capturemode",
    "capturetarget",
    "imagequality",
    "jpegquality",
    "imagesize",
    "aspectratio",
    "focusmode",
    "exposuremetermode",
    "batterylevel",
    # PC-remote priority: 'Camera' | 'Application' (drive-mode writes only
    # stick under Application — hardware-measured).
    "prioritymode",
    # RO status widgets the UI can surface.
    "focusindication",
    "focalposition",
)

# Canonical action names (the wire protocol the frontend speaks) -> Sony
# widget names.
SONY_ACTION_NAMES = {
    "autofocusdrive": "autofocus",
    "manualfocusdrive": "manualfocus",
}

# Frontend MF deltas are tuned for Nikon's ±32767 unit range; Sony's
# manualfocus takes a step-size CODE ±1..±7. Map magnitude bands to codes
# (thresholds bracket the UI's gestures: 1px drag=±4, wheel notch=±25,
# accumulated flushes up to ±500+).
SONY_MF_MAGNITUDE_BANDS = ((25, 1), (80, 2), (200, 3), (400, 5))
SONY_MF_MAX_CODE = 7

WRITE_SETTLE_PATIENCE_S = 2.5      # measured settle 0.3-1.7s
WRITE_BUSY_BACKOFF_S = 0.8         # [-2] busy retry backoff (measured)
EVENT_PUMP_SLICE_MS = 50
TRIGGER_FILE_MARGIN_S = 8.0        # FILE_ADDED margin beyond the exposure
BURST_TRAIL_DRAIN_S = 12.0         # trailing FILE_ADDEDs after hold release


class SonyAlphaAdapter(CameraAdapter):
    family = "sony_alpha"
    display_name = "Sony Alpha"
    preview_survives_exposure = True
    # One sdram transfer slot: later captures evict unfetched objects
    # (burst files 2..N-1 answered [-1] when fetched after the announce
    # loop). Downloads are near-free here (driver pre-spools), so each file
    # is fetched the moment it is announced.
    fetch_on_announce = True

    # -- lifecycle -------------------------------------------------------------
    def attach(self, camera, event_sink) -> None:
        super().attach(camera, event_sink)
        if not self._supports_single_config(camera):
            # A full get_config tree walk segfaulted the interpreter on the
            # A7R IV — there is no safe fallback path on this family.
            raise RuntimeError(
                "This Sony body needs libgphoto2's single-config API "
                "(python-gphoto2 >= 2.5.10) — the full-tree fallback is unsafe here."
            )

    # -- identity / policy -------------------------------------------------------
    def config_widget_names(self) -> tuple[str, ...]:
        return SONY_CONFIG_WIDGET_NAMES

    def settle_patience_s(self) -> float:
        # Most writes are already verified in-call; the ledger patience only
        # covers genuinely dial-owned reverts (expprogram...), which show up
        # fast on this family.
        return 6.0

    def connect_default_writes(self, config_cache: dict) -> list[ConnectDefault]:
        defaults: list[ConnectDefault] = []
        # PC-remote priority: without Application, drive-mode (burst) writes
        # silently never settle. Exposure dials keep working in M either way
        # (hardware-measured). Ledger-tracked so a refusal is reported.
        priority = str((config_cache.get("prioritymode") or {}).get("value", ""))
        if priority and priority != "Application":
            defaults.append(ConnectDefault(
                name="prioritymode", value="Application",
                note="Remote control was enabled (Priority Set → Application) so drive mode and remote settings stick. Set it back to Camera if you want body-side control.",
                ledger=True,
            ))
        # 'sdram' = camera buffer only: shots die with the session. The
        # measured default 'card+sdram' is ideal (card safety + instant USB
        # download) and is left alone — matching the VALUE exactly avoids
        # the substring-'ram' false positive on 'card+sdram'.
        target = str((config_cache.get("capturetarget") or {}).get("value", ""))
        if target == "sdram":
            defaults.append(ConnectDefault(
                name="capturetarget", value="card+sdram",
                note="Save To was set to card+buffer (was: buffer only — shots would exist only until disconnect). Change it back for USB-only workflows.",
                ledger=False,
            ))
        return defaults

    def sequence_preflight_warnings(self, config_cache: dict) -> list[str]:
        focusmode = str((config_cache.get("focusmode") or {}).get("value", ""))
        if focusmode and focusmode != "Manual":
            return [
                f"Focus Mode is {focusmode}: this body silently skips frames when "
                "autofocus cannot lock (dark sky). Switch Focus Mode to Manual for "
                "guaranteed firing."
            ]
        return []

    def capture_mode_plan(self, mode: str, burst_count: int,
                          burst_hold_s: float, burst_speed: str) -> dict[str, str]:
        if mode == "single":
            return {"capturemode": "Single Shot"}
        if mode == "burst":
            speed = burst_speed if burst_speed in ("Lo", "Mid", "Hi", "Hi+") else "Hi"
            return {"capturemode": f"Continuous Shooting {speed}"}
        # Movie: no recordingmedia widget on this family — clips always land
        # on the card; nothing to configure.
        return {}

    def capabilities(self, config_cache: dict) -> dict:
        return {
            "family": self.family,
            "display_name": self.display_name,
            "config_widgets": list(self.config_widget_names()),
            # Burst is press-and-hold: duration is the knob, count is not
            # controllable. Speeds mirror the body's drive choices.
            "burst": {"mode": "duration", "speeds": ["Lo", "Mid", "Hi", "Hi+"],
                      "min_hold_s": 0.2, "max_hold_s": 5.0},
            "movie": {
                "can_preflight": False,
                "can_confirm": False,
                "note": "Recording cannot be confirmed over USB on this body — check the red REC indicator on the camera screen.",
            },
            "iso_auto": {"kind": "choice", "auto_choice": "Auto ISO"},
            "save_to": {
                "volatile_values": ["sdram"],
                "recommended_value": "card+sdram",
                "labels": {
                    "sdram": "Camera buffer → this computer only",
                    "card+sdram": "Card + instant download (recommended)",
                    "card": "Memory card only",
                },
                "modes": self.save_modes(),
            },
            "focus": {
                "mf_requires_manual_focus": True,
                "indication_widget": "focusindication",
            },
            "preview_during_exposure": True,
        }

    # -- event pumping -------------------------------------------------------
    def _pump_events(self, camera, seconds: float) -> None:
        """Service camera events for `seconds`, forwarding EVERYTHING to the
        controller's sink (a swallowed FILE_ADDED = a lost shot announcement).
        Sony settles config writes during event pumping, so this doubles as
        the settle wait."""
        deadline = time.perf_counter() + max(0.0, seconds)
        while time.perf_counter() < deadline:
            try:
                event_type, event_data = camera.wait_for_event(EVENT_PUMP_SLICE_MS)
            except Exception:
                return
            if self._event_sink is not None and event_type != self._gp.GP_EVENT_TIMEOUT:
                self._event_sink(event_type, event_data)

    @staticmethod
    def _values_match(requested, actual) -> bool:
        if str(requested) == str(actual):
            return True
        try:  # RANGE widgets: '5500' vs 5500.0
            return abs(float(requested) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False

    def _read_value(self, camera, name: str):
        from abstractcamera import ptp_safe

        try:
            # NULL-guarded: the verify pump reads back mid-settle, exactly
            # when a transient NULL string value is most likely.
            return ptp_safe.widget_value(camera.get_single_config(name))
        except Exception:
            return None

    # -- camera I/O ---------------------------------------------------------------
    def read_config_cache(self, camera) -> dict:
        cache: dict = {}
        for name in self.config_widget_names():
            try:
                cache[name] = self._read_entry(camera.get_single_config(name))
            except Exception:
                continue
        return cache

    @staticmethod
    def _is_busy_error(text: str | None) -> bool:
        return bool(text) and ("-2" in text or "Bad parameters" in text)

    def is_transient_write_error(self, error: str | None) -> bool:
        # Post-burst the body answers [-2] for several seconds while
        # flushing frames to the card (hardware-observed): worth requeueing.
        return self._is_busy_error(error)

    def write_widget(self, camera, name: str, value, time_budget_s: float = 5.0) -> WriteReceipt:
        """Write-verify-retry (the hardware-mandated Sony write path).

        BUDGET-driven, not attempt-counted: after a burst the body answers
        [-2] busy for several seconds (hardware-observed while flushing to
        the card), so the loop keeps retrying — writes and busy backoffs —
        until the budget runs out. The budget bounds the WHOLE loop so a
        sequence's safe window is respected: on expiry the write returns
        unsettled/failed honestly and the controller decides (ledger watch
        or bounded requeue)."""
        budget_deadline = time.perf_counter() + max(0.5, float(time_budget_s))
        last_error: str | None = None
        actual = None
        wrote_at_least_once = False
        while time.perf_counter() < budget_deadline:
            try:
                self._raw_write_widget(camera, name, value)
                wrote_at_least_once = True
                last_error = None
            except Exception as exc:
                last_error = str(exc)
                if self._is_busy_error(last_error):
                    # Busy applying a previous change / flushing captures:
                    # pump and retry within the budget.
                    self._pump_events(camera, min(
                        WRITE_BUSY_BACKOFF_S,
                        max(0.1, budget_deadline - time.perf_counter())))
                    continue
                return WriteReceipt(ok=False, error=last_error)
            # Verify with patience: the value settles asynchronously.
            verify_deadline = min(
                time.perf_counter() + WRITE_SETTLE_PATIENCE_S, budget_deadline)
            while time.perf_counter() < verify_deadline:
                self._pump_events(camera, 0.15)
                actual = self._read_value(camera, name)
                if self._values_match(value, actual):
                    return WriteReceipt(ok=True, settled=True, actual=str(actual))
            # Not settled: the write may have been silently lost (measured
            # behavior) — loop retries the identical write.
        return WriteReceipt(
            ok=wrote_at_least_once and last_error is None,
            settled=False,
            actual=None if actual is None else str(actual),
            error=last_error,
        )

    def fire_single(self, camera, exposure_s: float, config_cache: dict) -> CaptureTiming:
        issued_at = time.time()
        command_started = time.perf_counter()
        camera.trigger_capture()  # blocks ~1.2s (not exposure-length) on this family
        command_ms = int(round((time.perf_counter() - command_started) * 1000))
        focusmode = str((config_cache.get("focusmode") or {}).get("value", ""))
        af_gated = bool(focusmode) and focusmode != "Manual"
        # Silent-drop honesty, ALL modes (hardware-observed 2026-07-12): the
        # body sometimes accepts a trigger and never fires — systematically
        # in AF modes without a lock, and intermittently even in Manual
        # focus (busy digesting settings/card writes). No error is raised
        # and no file appears; the expectation watch is the only honest
        # signal, so it arms on every single fire with a mode-specific note.
        if af_gated:
            note = (f"no file arrived after the trigger — in {focusmode} focus this body "
                    "silently refuses to fire when autofocus cannot lock; switch Focus "
                    "Mode to Manual for guaranteed firing")
        else:
            note = ("no file arrived after the trigger — the body silently dropped it "
                    "(it does this intermittently while busy applying settings or "
                    "writing the card); fire again")
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            drain_window_s=exposure_s + TRIGGER_FILE_MARGIN_S,
            preview_pause_s=0.0,  # live view survives exposures (measured)
            expect_file_within_s=exposure_s + TRIGGER_FILE_MARGIN_S,
            no_file_note=note,
        )

    def fire_burst(self, camera, exposure_s: float, count: int, hold_s: float) -> CaptureTiming:
        """Press-and-hold burst: capture=1, pump events for the hold (files
        announce through the sink), capture=0. The release is uncondition-
        al — a stuck 'pressed' state would keep the camera firing forever."""
        hold_s = max(0.2, min(5.0, float(hold_s)))
        issued_at = time.time()
        command_started = time.perf_counter()
        pressed = False
        try:
            self._raw_write_widget(camera, "capture", 1)
            pressed = True
            self._pump_events(camera, hold_s)
        finally:
            if pressed:
                try:
                    self._raw_write_widget(camera, "capture", 0)
                except Exception:
                    # One retry after a pump: a failed release is the one
                    # state we can never leave behind.
                    self._pump_events(camera, 0.5)
                    self._raw_write_widget(camera, "capture", 0)
        command_ms = int(round((time.perf_counter() - command_started) * 1000))
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            # Trailing FILE_ADDEDs keep arriving well after release
            # (measured: ~10s tail for a 1.2s hold at Hi).
            drain_window_s=hold_s + BURST_TRAIL_DRAIN_S,
            preview_pause_s=0.0,
            expected_files=None,  # the body decides the frame count
        )

    def toggle_movie(self, camera, start: bool) -> MovieReceipt:
        """No prohibit pre-check exists and recording is unconfirmable over
        USB (no events, no status widget) — the receipt is honest about it."""
        try:
            self._raw_write_widget(camera, "movie", 1 if start else 0)
            return MovieReceipt(
                ok=True,
                recording=start,
                confirmed=False,
                note=(
                    "record command sent — this body does not confirm recording over "
                    "USB; check the red REC indicator on the camera screen"
                    if start else
                    "stop command sent — the clip (if recording was active) is on the "
                    "memory card; this body does not announce movie files over USB"
                ),
                drain_window_s=0.0 if start else 3.0,
            )
        except Exception as exc:
            return MovieReceipt(ok=False, recording=not start, error=str(exc),
                                probe_session=True)

    def run_action(self, camera, name: str, value: str | None, config_cache: dict) -> ActionReceipt:
        widget_name = SONY_ACTION_NAMES.get(name)
        if widget_name is None:
            return ActionReceipt(ok=False, error=f"unsupported action {name} on this family")
        if widget_name == "manualfocus":
            focusmode = str((config_cache.get("focusmode") or {}).get("value", ""))
            if focusmode and focusmode != "Manual":
                # The body answers [-2] Bad parameters here; say why instead.
                return ActionReceipt(
                    ok=False,
                    error=f"manual focus nudges need Focus Mode = Manual (now: {focusmode})",
                )
            try:
                delta = float(value or 0)
            except ValueError:
                return ActionReceipt(ok=False, error=f"bad focus delta {value!r}")
            if delta == 0:
                return ActionReceipt(ok=True)
            code = SONY_MF_MAX_CODE
            for threshold, band_code in SONY_MF_MAGNITUDE_BANDS:
                if abs(delta) <= threshold:
                    code = band_code
                    break
            signed = code if delta > 0 else -code
            try:
                self._raw_write_widget(camera, "manualfocus", float(signed))
                return ActionReceipt(ok=True)
            except Exception as exc:
                return ActionReceipt(ok=False, error=str(exc))
        # AF drive: press-and-release choreography (leaving autofocus=1 would
        # hold the half-press forever and block later triggers).
        try:
            self._raw_write_widget(camera, "autofocus", 1)
            self._pump_events(camera, 0.8)
            indication = self._read_value(camera, "focusindication")
            try:
                self._raw_write_widget(camera, "autofocus", 0)
            except Exception:
                self._pump_events(camera, 0.5)
                self._raw_write_widget(camera, "autofocus", 0)
            note = None
            text = str(indication or "")
            if "Focus Locked" in text:
                note = "autofocus: focus locked"
            elif "No Focus" in text:
                note = "autofocus: no lock (low contrast) — shots may silently skip in AF modes"
            return ActionReceipt(ok=True, note=note)
        except Exception as exc:
            return ActionReceipt(ok=False, error=str(exc))

    def classify_event(self, event_type, event_data) -> ClassifiedEvent:
        gp = self._gp
        if event_type == gp.GP_EVENT_TIMEOUT:
            return ClassifiedEvent(kind="timeout")
        if event_type == gp.GP_EVENT_FILE_ADDED and event_data is not None:
            return ClassifiedEvent(kind="file_added",
                                   folder=event_data.folder, name=event_data.name)
        note = str(event_data).strip() if event_data is not None else ""
        if not note:
            return ClassifiedEvent(kind="noise")
        lowered = note.lower()
        # The A7R IV streams "PTP Property 0x... changed" / "PTP Event c2xx"
        # notifications at several per second: structural noise, never shown.
        if lowered.startswith("ptp property") or lowered.startswith("ptp event"):
            return ClassifiedEvent(kind="noise")
        if "full" in lowered or "error" in lowered or "fail" in lowered:
            return ClassifiedEvent(kind="status", note=note[:200])
        if event_type != gp.GP_EVENT_UNKNOWN:
            return ClassifiedEvent(kind="status", note=note[:200])
        return ClassifiedEvent(kind="noise")
