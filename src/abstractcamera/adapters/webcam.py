"""Webcam family adapter: the machine's own camera as a first-class family.

Capability honesty is the whole design (ADR 0004): ONE real dial
(imagesize/resolution — riding the existing "Size" dial in host UIs), no
pretend exposure controls, stills that are video frames and say so, burst as
a frame count at the sensor's native rate, and movie recording that is
genuinely CONFIRMABLE (the package writes the MP4 itself) — the exact
inversion of the Sony movie honesty note. Detection, rolling clips, and the
intervalometer ride the frame stream unchanged.
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

# zoom = AVFoundation videoZoomFactor; flashmode = a REAL photo capture
# with the device flash (Continuity iPhones report hasFlash and the flash
# physically fires — measured 2026-07-13; the MacBook camera has none, so
# the widget is absent there). Manual exposure/ISO/WB/focus stay absent:
# those AVFoundation APIs are iOS-only (measured unsupported, ADR 0004).
WEBCAM_CONFIG_WIDGET_NAMES = ("imagesize", "zoom", "flashmode", "fps")
BURST_MAX_FRAMES = 60  # bounded by worker-time (~2s at 30fps), not storage


class WebcamAdapter(CameraAdapter):
    family = "webcam"
    display_name = "Built-in camera"
    preview_survives_exposure = True   # there are no exposures to survive
    fetch_on_announce = True           # captures are in-process; fetch is free

    def __init__(self, transport_module=None):
        # No transport module: the session IS the device. The base class
        # tolerates None (only widget-type helpers would dereference it, and
        # this adapter overrides all of them).
        super().__init__(transport_module)
        self._session = None

    # -- lifecycle -----------------------------------------------------------
    def attach(self, camera, event_sink) -> None:
        super().attach(camera, event_sink)
        self._session = camera

    # -- identity / policy ------------------------------------------------------
    def config_widget_names(self) -> tuple[str, ...]:
        return WEBCAM_CONFIG_WIDGET_NAMES

    def settle_patience_s(self) -> float:
        return 3.0  # resolution switches verify against the next frame

    def nominal_exposure_s(self, config_cache: dict) -> float | None:
        """The effective exposure of a video frame is the frame interval —
        a FLOOR, not a constant: AVFoundation auto-exposure lengthens
        integration in low light (documented; immaterial for interval
        validation where the 1s minimum dominates)."""
        fps = 30.0
        if self._session is not None:
            fps = self._session.measured_fps
        try:
            cached = float(str((config_cache.get("fps") or {}).get("value", "")) or fps)
            fps = cached if cached > 0 else fps
        except ValueError:
            pass
        return 1.0 / max(1.0, fps)

    def capture_mode_plan(self, mode: str, burst_count: int,
                          burst_hold_s: float, burst_speed: str) -> dict[str, str]:
        return {}  # no drive-mode widget exists; burst is adapter choreography

    def read_serial(self, camera) -> str | None:
        """The AVFoundation uniqueID (ADR 0009): stable device identity —
        the hub disambiguates two identical-label webcams with its tail."""
        unique_id = getattr(camera, "unique_id", None)
        return str(unique_id) if unique_id else None

    def save_modes(self) -> list[str]:
        return ["local"]  # this camera has no storage of its own

    def capabilities(self, config_cache: dict) -> dict:
        return {
            "family": self.family,
            "display_name": self.display_name,
            "burst": {"mode": "count", "min": 2, "max": BURST_MAX_FRAMES},
            "movie": {
                "can_preflight": True,
                "can_confirm": True,
                "note": ("recorded on this computer as MP4 — start/stop are fully "
                         "confirmable" if self._movie_available()
                         else "movie recording needs the [clips] extra "
                              "(pip install abstractcamera[clips])"),
            },
            "iso_auto": {"kind": "none"},
            "save_to": {"volatile_values": [], "recommended_value": None, "labels": {},
                        "modes": ["local"]},
            "focus": {"supported": False, "mf_requires_manual_focus": False,
                      "indication_widget": None},
            "preview_during_exposure": True,
            "exposure_controls": False,
            # The host UI hides dials whose widgets this family cannot have
            # (adjudication Mod #7: no locked ghost controls).
            "config_widgets": list(WEBCAM_CONFIG_WIDGET_NAMES),
            "notes": [
                "Stills are video frames at the configured resolution (no separate photo pipeline).",
                "macOS reserves exposure, ISO, shutter, white balance and focus for its own "
                "auto algorithms on this transport (the manual AVFoundation APIs are iOS-only "
                "— measured on this hardware): those dials do not exist here, on ANY app. "
                "Zoom is the one manual control (an OS digital crop); iPhones add a real "
                "Flash (fires on capture). iPhone framing/depth effects (Center Stage, "
                "Portrait, Studio Light) are macOS system toggles: Control Center → "
                "Video Effects while the camera is live.",
            ],
        }

    def _movie_available(self) -> bool:
        return self._session is not None and self._session.movie_available()

    # -- camera I/O ------------------------------------------------------------------
    def read_config_cache(self, camera) -> dict:
        cache: dict = {}
        for name in WEBCAM_CONFIG_WIDGET_NAMES:
            try:
                cache[name] = self._read_entry(camera.get_single_config(name))
            except Exception:
                continue
        return cache

    def _read_entry(self, widget) -> dict:
        # Standalone (no transport module): the wire widget types are fixed.
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
        return WriteReceipt(ok=True, settled=(actual == str(value)), actual=actual)

    def _pump_session_events(self, camera, seconds: float) -> None:
        """Forward session events through the sink (announce -> immediate
        fetch): keeps the object store depth at ~1 during bursts
        (adjudication Mod #1 — no silent frame loss to store bounds)."""
        from abstractcamera import wire

        deadline = time.perf_counter() + max(0.0, seconds)
        while True:
            event_type, event_data = camera.wait_for_event(5)
            if event_type == wire.GP_EVENT_TIMEOUT:
                if time.perf_counter() >= deadline:
                    return
                continue
            if self._event_sink is not None:
                self._event_sink(event_type, event_data)

    def fire_single(self, camera, exposure_s: float, config_cache: dict) -> CaptureTiming:
        started = time.perf_counter()
        camera.trigger_capture()
        command_ms = int(round((time.perf_counter() - started) * 1000))
        return CaptureTiming(
            # Anchored AFTER the trigger returns: a flash capture blocks
            # ~2-4s inside trigger_capture (photo pipeline + delivery) —
            # windows anchored at issue time expired before the queued
            # FILE_ADDED was ever drained (found on the Continuity iPhone,
            # 2026-07-13). The file event is already queued at this point;
            # the windows only need to cover the drain+announce.
            issued_at=time.time(),
            command_ms=command_ms,
            drain_window_s=1.0,
            preview_pause_s=0.0,
            expected_files=1,
            # Doubles as a stream-health check: no frame within 2s = the
            # device died (there is no AF to refuse on this family).
            expect_file_within_s=2.0,
            no_file_note="the camera produced no frame — the device may have been lost",
        )

    def fire_burst(self, camera, exposure_s: float, count: int, hold_s: float) -> CaptureTiming:
        """N consecutive frames at the sensor's native rate, announced AND
        fetched one by one through the sink (store depth stays ~1)."""
        count = max(2, min(BURST_MAX_FRAMES, int(count)))
        issued_at = time.time()
        started = time.perf_counter()
        for _ in range(count):
            camera.trigger_capture()
            self._pump_session_events(camera, 0.001)
        command_ms = int(round((time.perf_counter() - started) * 1000))
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            drain_window_s=2.0,
            preview_pause_s=0.0,
            expected_files=None,  # already announced through the sink
        )

    def toggle_movie(self, camera, start: bool) -> MovieReceipt:
        if start:
            if not camera.movie_available():
                return MovieReceipt(
                    ok=False, recording=False, refused=True,
                    error="movie recording needs the [clips] extra (pip install abstractcamera[clips])",
                )
            try:
                camera.start_movie()
                return MovieReceipt(ok=True, recording=True, confirmed=True,
                                    note="recording MP4 on this computer")
            except Exception as exc:
                return MovieReceipt(ok=False, recording=False, error=str(exc))
        try:
            stats = camera.stop_movie()
        except Exception as exc:
            return MovieReceipt(ok=False, recording=True, error=str(exc))
        note = "video recording stopped — the MP4 lands in the capture folder"
        if stats and stats.get("dropped"):
            note += f" ({stats['dropped']} frames dropped during encode)"
        self._pump_session_events(camera, 0.05)  # announce the finished file now
        return MovieReceipt(ok=True, recording=False, confirmed=True, note=note,
                            drain_window_s=2.0)

    def run_action(self, camera, name: str, value: str | None, config_cache: dict) -> ActionReceipt:
        return ActionReceipt(ok=False,
                             error="this camera has no remotely drivable focus")

    def classify_event(self, event_type, event_data) -> ClassifiedEvent:
        from abstractcamera import wire

        if event_type == wire.GP_EVENT_FILE_ADDED and event_data is not None:
            return ClassifiedEvent(kind="file_added",
                                   folder=event_data.folder, name=event_data.name)
        if event_type == wire.GP_EVENT_TIMEOUT:
            return ClassifiedEvent(kind="timeout")
        return ClassifiedEvent(kind="noise")
