"""DWARF family adapter: a smart telescope is a camera PLUS a mount.

Camera semantics ride the existing abstraction unchanged (dials, stills,
burst, movie, live view). The mount is exposed as FAMILY ACTIONS — the
same one-shot, never-cached, never-replayed semantics as focus drives
(replaying a cached slew on reconnect would physically move the telescope,
exactly the hazard the action contract exists for):

    gotoradec  value "ra_deg,dec_deg[,label]"   plate-solved astro GOTO
    gotosolar  value "moon"|"jupiter"|...        solar-system GOTO
    stopgoto   —                                 stop the slew
    calibrate  —                                 astro calibration (sky-solve)
    joystick   value "angle_deg,length,speed"    manual slew vector (0° = +x,
                                                 counterclockwise; length 0-1
                                                 scales speed, speed in °/s)
    joystickstop —                               stop manual motion

plus the canonical focus actions (`autofocusdrive` -> astro autofocus,
`manualfocusdrive` value "near"/"far" -> single-step focus).

Capability honesty (ADR 0004): exposure/gain dials exist exactly when the
device published its gear tables; stills land on the DWARF's microSD and
are downloaded over Wi-Fi (save_modes device+local); GOTO/tracking success
is reported by the device's own state notifications, surfaced as catch-log
status events.
"""

from __future__ import annotations

import time

from abstractcamera.adapters.base import (
    ActionReceipt,
    CameraAdapter,
    CaptureTiming,
    ClassifiedEvent,
    MovieReceipt,
    WriteReceipt,
)
from abstractcamera.errors import CameraControlError

DWARF_CONFIG_WIDGET_NAMES = ("shutterspeed", "gain", "ircut", "battery", "temperature")
DWARF_ACTION_NAMES = ("gotoradec", "gotosolar", "stopgoto", "calibrate",
                      "joystick", "joystickstop")
BURST_MAX_FRAMES = 99  # the device's burst counter is a wire int; bounded sanely


class DwarfAdapter(CameraAdapter):
    family = "dwarf"
    display_name = "DWARF smart telescope"
    # The RTSP live stream keeps flowing while the device exposes stills
    # (it is a separate pipeline on the device side).
    preview_survives_exposure = True
    # Album files persist on the DWARF's microSD and stay addressable —
    # the deferred-download policy applies unchanged.
    fetch_on_announce = False

    def __init__(self, transport_module=None):
        super().__init__(transport_module)
        self._session = None

    # -- lifecycle ---------------------------------------------------------------
    def attach(self, camera, event_sink) -> None:
        super().attach(camera, event_sink)
        self._session = camera

    # -- identity / policy ----------------------------------------------------------
    def config_widget_names(self) -> tuple[str, ...]:
        return DWARF_CONFIG_WIDGET_NAMES

    def family_action_names(self) -> tuple[str, ...]:
        return DWARF_ACTION_NAMES

    def settle_patience_s(self) -> float:
        return 5.0  # command-acknowledged writes settle immediately

    def nominal_exposure_s(self, config_cache: dict) -> float | None:
        """The exposure dial value IS the exposure (device gear names parse
        as shutter speeds: '1/2000', '15s'...). Unknown -> 0 (readout-tail
        windows only), never None: the PTP shutter parser has nothing
        honest to say about this family."""
        session = self._session
        if session is not None:
            try:
                return float(session.nominal_exposure_s())
            except Exception:
                pass
        return 0.0

    def read_serial(self, camera) -> str | None:
        """Network identity: the device host (stable per unit on a
        configured network) — disambiguates two DWARFs in one hub."""
        host = getattr(camera, "host", None)
        return str(host).replace(".", "-") if host else None

    def save_modes(self) -> list[str]:
        return ["device", "local"]  # microSD always; local = Wi-Fi download

    def capabilities(self, config_cache: dict) -> dict:
        return {
            "family": self.family,
            "display_name": self.display_name,
            "config_widgets": list(DWARF_CONFIG_WIDGET_NAMES),
            "burst": {"mode": "count", "min": 2, "max": BURST_MAX_FRAMES},
            "movie": {
                "can_preflight": False,
                "can_confirm": True,
                "note": ("recorded on the DWARF's microSD; the MP4 appears in "
                         "the album (and downloads locally) when stopped"),
            },
            "iso_auto": {"kind": "none"},
            "save_to": {"volatile_values": [], "recommended_value": None,
                        "labels": {}, "modes": self.save_modes()},
            "focus": {"supported": True, "mf_requires_manual_focus": False,
                      "indication_widget": None},
            "preview_during_exposure": True,
            "mount": {
                "kind": "alt-az",
                "goto": ["radec", "solar_system"],
                "joystick": True,
                "calibration": True,
                "tracking": "the device tracks automatically after a GOTO",
            },
            "actions": list(DWARF_ACTION_NAMES),
            "notes": [
                "The DWARF grants ONE controller at a time (master lock): "
                "close the DWARFLAB app to pilot it from here.",
                "Captures land on the DWARF's microSD card and download over "
                "Wi-Fi — allow a few seconds between shutter and file.",
                "GOTO needs a prior calibration under the open sky "
                "(the 'calibrate' action).",
                "The wide-angle lens is not piloted yet (telephoto only).",
            ],
        }

    # -- camera I/O ---------------------------------------------------------------------
    def read_config_cache(self, camera) -> dict:
        cache: dict = {}
        for name in DWARF_CONFIG_WIDGET_NAMES:
            try:
                cache[name] = self._read_entry(camera.get_single_config(name))
            except Exception:
                continue
        return cache

    def _read_entry(self, widget) -> dict:
        from abstractcamera import wire

        entry: dict = {
            "value": widget.get_value(),
            "readonly": bool(widget.get_readonly()),
        }
        if widget.get_type() in (wire.GP_WIDGET_RADIO, wire.GP_WIDGET_MENU):
            entry["choices"] = [widget.get_choice(i) for i in range(widget.count_choices())]
        elif widget.get_type() == wire.GP_WIDGET_RANGE:
            low, high, step = widget.get_range()
            entry["range"] = [float(low), float(high), float(step)]
        return entry

    def write_widget(self, camera, name: str, value, time_budget_s: float = 5.0) -> WriteReceipt:
        try:
            widget = camera.get_single_config(name)
            widget.set_value(str(value))
            camera.set_single_config(name, widget)
        except Exception as exc:
            return WriteReceipt(ok=False, error=str(exc))
        actual = str(camera.get_single_config(name).get_value())
        # The device ACKNOWLEDGED the command (code 0) — that IS settlement
        # on this protocol.
        return WriteReceipt(ok=True, settled=(actual == str(value)), actual=actual)

    def fire_single(self, camera, exposure_s: float, config_cache: dict) -> CaptureTiming:
        issued_at = time.time()
        started = time.perf_counter()
        camera.trigger_capture()
        command_ms = int(round((time.perf_counter() - started) * 1000))
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            # Shutter -> album-entry latency rides on top of the exposure.
            drain_window_s=exposure_s + 30.0,
            preview_pause_s=0.0,  # the RTSP stream keeps flowing
            expected_files=1,
            expect_file_within_s=exposure_s + 30.0,
            no_file_note=("no file appeared in the DWARF's album — check the "
                          "microSD card and Wi-Fi link"),
        )

    def fire_burst(self, camera, exposure_s: float, count: int, hold_s: float) -> CaptureTiming:
        count = max(2, min(BURST_MAX_FRAMES, int(count)))
        issued_at = time.time()
        started = time.perf_counter()
        camera.start_burst(count)
        command_ms = int(round((time.perf_counter() - started) * 1000))
        per_frame = max(1.0, exposure_s + 1.0)
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            drain_window_s=per_frame * count + 30.0,
            preview_pause_s=0.0,
            expected_files=count,
        )

    def toggle_movie(self, camera, start: bool) -> MovieReceipt:
        try:
            if start:
                camera.start_record()
                return MovieReceipt(ok=True, recording=True, confirmed=True,
                                    note="recording on the DWARF's microSD")
            camera.stop_record()
            return MovieReceipt(ok=True, recording=False, confirmed=True,
                                note=("video recording stopped — the MP4 appears "
                                      "in the album"),
                                drain_window_s=30.0)
        except CameraControlError as exc:
            return MovieReceipt(ok=False, recording=not start, error=str(exc))

    # -- actions (mount + focus) ------------------------------------------------------
    def run_action(self, camera, name: str, value: str | None, config_cache: dict) -> ActionReceipt:
        try:
            if name == "gotoradec":
                ra_deg, dec_deg, label = self._parse_goto_value(value)
                camera.goto_dso(ra_deg, dec_deg, label)
                return ActionReceipt(ok=True, note=(
                    f"GOTO {label or 'target'} (RA {ra_deg:g}\N{DEGREE SIGN}, "
                    f"Dec {dec_deg:g}\N{DEGREE SIGN}) started — progress arrives "
                    "as GOTO status events"))
            if name == "gotosolar":
                target = str(value or "").strip()
                if not target:
                    raise CameraControlError(
                        "gotosolar needs a target name (e.g. 'moon', 'jupiter').")
                camera.goto_solar(target)
                return ActionReceipt(ok=True, note=f"GOTO {target} started")
            if name == "stopgoto":
                camera.stop_goto()
                return ActionReceipt(ok=True, note="GOTO stopped")
            if name == "calibrate":
                camera.start_calibration()
                return ActionReceipt(ok=True, note=(
                    "astro calibration started — needs open sky; progress "
                    "arrives as calibration status events"))
            if name == "joystick":
                angle, length, speed = self._parse_joystick_value(value)
                camera.joystick(angle, length, speed)
                return ActionReceipt(ok=True, note=(
                    f"mount moving (angle {angle:g}\N{DEGREE SIGN}, speed "
                    f"{speed:g}\N{DEGREE SIGN}/s) — send joystickstop to halt"))
            if name == "joystickstop":
                camera.joystick_stop()
                return ActionReceipt(ok=True, note="mount motion stopped")
            if name == "autofocusdrive":
                camera.astro_autofocus()
                return ActionReceipt(ok=True, note="astro autofocus started")
            if name == "manualfocusdrive":
                direction = -1 if str(value or "").strip().lower() in (
                    "near", "-1", "-3", "in") else 1
                camera.manual_focus_step(direction)
                return ActionReceipt(ok=True)
            return ActionReceipt(ok=False,
                                 error=f"this telescope has no '{name}' action")
        except CameraControlError as exc:
            return ActionReceipt(ok=False, error=str(exc))

    @staticmethod
    def _parse_goto_value(value: str | None) -> tuple[float, float, str]:
        parts = [part.strip() for part in str(value or "").split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise CameraControlError(
                "gotoradec needs 'ra_deg,dec_deg[,label]' — RA/Dec in DEGREES "
                "(J2000), e.g. '83.82,-5.39,M42'.")
        try:
            ra_deg, dec_deg = float(parts[0]), float(parts[1])
        except ValueError:
            raise CameraControlError(
                f"gotoradec could not parse '{value}' — RA/Dec must be decimal "
                "degrees, e.g. '83.82,-5.39,M42'.")
        if not 0.0 <= ra_deg < 360.0 or not -90.0 <= dec_deg <= 90.0:
            raise CameraControlError(
                "gotoradec out of range — RA in [0,360), Dec in [-90,90] degrees.")
        label = parts[2] if len(parts) > 2 else ""
        return ra_deg, dec_deg, label

    @staticmethod
    def _parse_joystick_value(value: str | None) -> tuple[float, float, float]:
        parts = [part.strip() for part in str(value or "").split(",")]
        if len(parts) != 3:
            raise CameraControlError(
                "joystick needs 'angle_deg,length,speed' — e.g. '90,1,5' "
                "(angle 0-360 from +x counterclockwise, length 0-1, speed "
                "0.1-30 degrees/s).")
        try:
            angle, length, speed = (float(parts[0]), float(parts[1]), float(parts[2]))
        except ValueError:
            raise CameraControlError(
                f"joystick could not parse '{value}' — three numbers expected.")
        return angle % 360.0, length, speed

    # -- events -------------------------------------------------------------------------
    def poll_session_events(self, camera) -> None:
        """The DWARF speaks spontaneously (GOTO/calibration/tracking state,
        battery, album updates) — forward everything queued, without
        blocking (wait_for_event(0) drains and returns immediately)."""
        from abstractcamera import wire

        if self._event_sink is None:
            return
        while True:
            event_type, event_data = camera.wait_for_event(0)
            if event_type == wire.GP_EVENT_TIMEOUT:
                return
            self._event_sink(event_type, event_data)

    def classify_event(self, event_type, event_data) -> ClassifiedEvent:
        from abstractcamera import wire

        if event_type == wire.GP_EVENT_FILE_ADDED and event_data is not None:
            return ClassifiedEvent(kind="file_added",
                                   folder=event_data.folder, name=event_data.name)
        if event_type == wire.GP_EVENT_TIMEOUT:
            return ClassifiedEvent(kind="timeout")
        note = str(event_data).strip() if event_data is not None else ""
        if note:
            # Device state notifications (GOTO/calibration/tracking/power)
            # belong in the catch log — they are the mount's own voice.
            return ClassifiedEvent(kind="status", note=note[:200])
        return ClassifiedEvent(kind="noise")
