"""Simulated gphoto2 module for camera-less development and testing.

Referee-adjudicated design (2026-07-07): this is a SIMULATOR (a dev tool),
not a test fixture — it lives in backend/ so the dev server can run with
BLACKPIXEL_FAKE_CAMERA=1 and the Playwright proof can drive the real UI,
while pytest configures the same module for scripted scenarios.

It implements exactly the gphoto2 surface backend/camera_control.py and the
family adapters touch: Camera (init/exit/get_abilities/get_config/set_config/
get_single_config/set_single_config/capture_preview/trigger_capture/
wait_for_event/file_get), widget tree navigation, and the constants.

PROFILES (configure(profile=...)): widget trees live in
backend/fake_camera_profiles.py.
- "z6ii" (default): simulated Nikon Z6 II — the original simulator,
  unchanged behavior. Scripted knobs: trigger latency, FILE_ADDED timing
  derived from the CURRENT shutterspeed, download stalls, busy-NAK when
  triggered mid-exposure, RAW+JPEG dual-file emission, widget disappearance
  outside M, preview failure (USB pull), injectable card-full events, silent
  dial reverts, live-view-gated writes.
- "a7r4": simulated Sony A7R IV reproducing the behaviors MEASURED on the
  real body (2026-07-12): async write settling (readback returns the old
  value until the settle lands during event pumping), silently lost writes,
  [-2] busy on rapid consecutive writes, prioritymode gating of drive-mode
  writes, exposure-independent trigger blocking, silent AF-gated trigger
  refusal, press-and-hold Continuous burst on the `capture` toggle,
  unconfirmable movie toggles, PTP property-change event noise, FILE_ADDED
  at folder '/', preview that keeps flowing during exposures.

HONESTY: everything proven against this module is scheduler/state/UI logic.
Real trigger latency, PTP busy behavior, bulb, focus-drive step sizes, and
USB contention remain hardware-validated only where the probe scripts
(untracked/sony_probe/) or the Z6 II sessions measured them.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import threading
import time

from abstractcamera.sim.preview_render import PREVIEW_SIZES, render_preview_jpeg
from abstractcamera.sim.profiles import build_a7r4_widgets, build_z6ii_widgets

# --- gphoto2 constants (values mirror libgphoto2) -------------------------
GP_EVENT_UNKNOWN = 0
GP_EVENT_TIMEOUT = 1
GP_EVENT_FILE_ADDED = 2
GP_EVENT_CAPTURE_COMPLETE = 4

GP_WIDGET_WINDOW = 0
GP_WIDGET_SECTION = 1
GP_WIDGET_TEXT = 2
GP_WIDGET_RANGE = 3
GP_WIDGET_TOGGLE = 4
GP_WIDGET_RADIO = 5
GP_WIDGET_MENU = 6
GP_WIDGET_BUTTON = 7
GP_WIDGET_DATE = 8

GP_FILE_TYPE_NORMAL = 1


class GPhoto2Error(Exception):
    """Mirror of gphoto2.GPhoto2Error (controller catches bare Exception)."""


# --- module-level scenario configuration -----------------------------------

_DEFAULTS = {
    "profile": "z6ii",               # z6ii (Nikon) | a7r4 (Sony)
    "trigger_latency_s": 0.30,
    "trigger_latency_jitter_s": 0.10,
    "file_added_offset_s": 0.35,     # readout/card-write after exposure end
    "download_stall_s": (0.05, 0.15),  # (min, max) per file_get
    "busy_nak": True,                # trigger during exposure raises
    "trigger_fail_always": False,    # every trigger raises (failure-abort tests)
    "emit_raw_and_jpeg": None,       # None = derive from imagequality
    "widget_disappearance": True,    # shutterspeed vanishes outside M
    "preview_fail": False,           # simulate USB pull
    "preview_interval_s": 0.025,     # ~40fps preview pacing
    "inject_camera_events": [],      # list of (delay_s, note) card-full etc.
    "time_scale": 1.0,               # <1 speeds up simulated waits (tests)
    # ---- Sony (a7r4) behaviors, all hardware-measured 2026-07-12 ----
    "sony_settle_delay_s": (0.25, 0.8),  # async write settle window
    "sony_lose_writes": {},          # name -> count of writes to silently lose
    "sony_busy_window_s": 0.3,       # [-2] Bad parameters within this after a write
    "sony_trigger_block_s": 1.2,     # trigger blocks this long, NOT the exposure
    "sony_af_wont_lock": False,      # AF-gated silent trigger refusal
    "sony_burst_fps": 7.5,           # files per second while capture is held
    "sony_property_noise_interval_s": 0.0,  # >0: emit PTP property noise events
}

_scenario = dict(_DEFAULTS)
_scenario_lock = threading.Lock()
_last_camera = None
_rng = random.Random(20260707)


def configure(**kwargs) -> None:
    with _scenario_lock:
        for key, value in kwargs.items():
            if key not in _DEFAULTS:
                raise KeyError(f"Unknown fake camera scenario key: {key}")
            _scenario[key] = value


def reset() -> None:
    global _last_camera
    with _scenario_lock:
        _scenario.clear()
        _scenario.update(_DEFAULTS)
    _last_camera = None


def get_last_camera() -> "Camera | None":
    return _last_camera


def _cfg(key):
    with _scenario_lock:
        return _scenario[key]


def _scaled(seconds: float) -> float:
    return max(0.0, float(seconds) * float(_cfg("time_scale")))


# --- widget tree ------------------------------------------------------------

class _Widget:
    def __init__(self, name, wtype, value, choices=None, wrange=None, readonly=False):
        self.name = name
        self.wtype = wtype
        self.value = value
        self.choices = list(choices or [])
        self.wrange = wrange
        self.readonly = readonly

    def get_type(self):
        return self.wtype

    def get_value(self):
        return self.value

    def set_value(self, value):
        if self.wtype == GP_WIDGET_RANGE:
            self.value = float(value)
        elif self.wtype == GP_WIDGET_TOGGLE:
            self.value = int(value)
        else:
            self.value = str(value)

    def get_readonly(self):
        return 1 if self.readonly else 0

    def count_choices(self):
        return len(self.choices)

    def get_choice(self, index):
        return self.choices[index]

    def get_range(self):
        return tuple(self.wrange)


def _build_widgets() -> dict[str, _Widget]:
    """Profile dispatch: widget trees live in backend/fake_camera_profiles.py
    (z6ii moved there verbatim; a7r4 from the real-body probe)."""
    if _cfg("profile") == "a7r4":
        return build_a7r4_widgets(_Widget, _cfg)
    return build_z6ii_widgets(_Widget, _cfg)


class _ConfigTree:
    """A get_config() snapshot: navigation + staged writes committed by
    set_config, like the real library."""

    def __init__(self, camera: "Camera"):
        self._camera = camera

    def get_child_by_name(self, name: str) -> _Widget:
        widgets = self._camera._visible_widgets()
        if name not in widgets:
            raise GPhoto2Error(f"Widget not found: {name}")
        widget = widgets[name]
        lv_engaged = int(self._camera._widgets.get("viewfinder", _Widget("viewfinder", GP_WIDGET_TOGGLE, 1)).value or 0) == 1
        if name in _cfg("revert_writes") or (name in _cfg("lv_gated_writes") and lv_engaged):
            # Dial-controlled widget: accept the write silently and revert —
            # exactly what the Z6 II does to iso/expprogram in U2 (always)
            # and to isoauto while remote live view is engaged.
            original = widget.value

            class _RevertingWidget:
                def __getattr__(self, attr):
                    return getattr(widget, attr)

                def set_value(self, value):
                    widget.set_value(value)
                    widget.value = original  # silently reverted

            return _RevertingWidget()
        return widget


class _Abilities:
    """Model string follows the active profile (drives adapter selection)."""

    PROFILE_MODELS = {
        "z6ii": "Nikon Z 6II (Fake)",
        "a7r4": "Sony DSC-A7r IV (Control) (Fake)",
    }

    def __init__(self, profile: str = "z6ii"):
        self.model = self.PROFILE_MODELS.get(profile, self.PROFILE_MODELS["z6ii"])


class _EventData:
    def __init__(self, folder: str, name: str):
        self.folder = folder
        self.name = name



# Scripted overlays for detection testing: each entry draws into preview
# frames as a function of time. Set via configure(inject_streaks=[...]) /
# configure(inject_motion_blobs=[...]) / configure(gain_step=...).
_DEFAULTS.update({
    "inject_streaks": [],       # {t0, x0, y0, angle_deg, speed_px_s, duration_s, brightness}
    "inject_motion_blobs": [],  # {t0, x, y, w, h, duration_s, brightness}
    "gain_step": None,          # {t0, duration_s, factor} global exposure flicker
    # Movie behavior (bug-3 coverage): by default the simulated body is in
    # the photo selector position and refuses movie start with the real
    # Z6 II prohibit text (hardware-measured 2026-07-07).
    "movie_toggle_fails": True,
    "movie_prohibit_text": "Movie prohibit conditions: Not in application mode,Set liveview selector is enabled",
    # Dial-controlled write reverts (hardware-measured: U2 silently reverts
    # iso/isoauto/expprogram writes while reporting readonly=false).
    "revert_writes": [],
    # Live-view-gated writes (hardware-measured 2026-07-08: the Z6 II keeps
    # isoauto unchanged while remote live view is engaged, but accepts the
    # write with the viewfinder released). Reverts only while viewfinder=1.
    "lv_gated_writes": [],
    # Initial widget values for the next session (name -> value).
    "widget_overrides": {},
})
_scenario.update({
    k: _DEFAULTS[k]
    for k in ("inject_streaks", "inject_motion_blobs", "gain_step",
              "movie_toggle_fails", "movie_prohibit_text", "revert_writes",
              "lv_gated_writes", "widget_overrides")
})



_preview_epoch = time.time()


def _render_preview_jpeg(size_name: str = "VGA") -> bytes:
    """Delegates to sim.preview_render with this module's scenario state."""
    return render_preview_jpeg(size_name, _cfg, _preview_epoch)


_TINY_JPEG = _render_preview_jpeg()


class _CamFile:
    def __init__(self, data: bytes):
        self._data = data

    def get_data_and_size(self):
        return self._data

    def save(self, path: str):
        with open(path, "wb") as fh:
            fh.write(self._data)


class Camera:
    """One simulated tethered body. Time-ordered event queue; exposure state
    derived from the CURRENT shutterspeed widget at trigger time. The active
    profile (z6ii | a7r4) is latched at construction."""

    def __init__(self):
        global _last_camera
        self._profile = str(_cfg("profile"))
        self._widgets = _build_widgets()
        self._events: list[tuple[float, int, int, object]] = []  # (due, tiebreak, type, data)
        self._event_tiebreak = itertools.count()
        self._lock = threading.RLock()
        self._busy_until = 0.0
        self._file_counter = 0
        self._initialized = False
        self.trigger_log: list[float] = []  # epoch times of accepted triggers
        self.config_write_log: list[tuple[float, str, object]] = []
        # ---- Sony (a7r4) state ----
        self._pending_settles: list[tuple[float, str, object]] = []  # (due, name, value)
        self._busy_write_until = 0.0
        self._capture_pressed_at: float | None = None
        self._capture_released_at: float | None = None
        self._burst_emitted = 0
        self._last_noise_at = 0.0
        self._movie_state = 0
        # sdram transfer slots (hardware-observed 2026-07-12): the body keeps
        # only the ~2 most recent unfetched objects; older ones answer [-1]
        # on file_get. Most recent last.
        self._sony_available_objects: list[str] = []
        _last_camera = self

        for delay_s, note in _cfg("inject_camera_events"):
            self._push_event(time.time() + _scaled(delay_s), GP_EVENT_UNKNOWN, note)

    # -- lifecycle --
    def init(self):
        if _cfg("preview_fail"):
            raise GPhoto2Error("[-105] Unknown model (simulated absent camera)")
        self._initialized = True

    def exit(self):
        self._initialized = False

    def get_abilities(self):
        return _Abilities(self._profile)

    @staticmethod
    def autodetect():
        class _List:
            def __init__(self, items):
                self._items = items

            def count(self):
                return len(self._items)

            def get_name(self, i):
                return self._items[i][0]

            def get_value(self, i):
                return self._items[i][1]

        profile = str(_cfg("profile"))
        return _List([(_Abilities(profile).model,
                       "usb:002,001" if profile == "a7r4" else "usb:001,004")])

    # -- config --
    def _visible_widgets(self) -> dict[str, _Widget]:
        widgets = dict(self._widgets)
        # Nikon-only behavior: shutterspeed vanishes outside M. The Sony
        # expprogram is a readonly dial mirror and hides nothing.
        if (self._profile == "z6ii" and _cfg("widget_disappearance")
                and widgets["expprogram"].value != "M"):
            widgets.pop("shutterspeed", None)
        return widgets

    def get_config(self):
        return _ConfigTree(self)

    # Single-widget API (libgphoto2 >= 2.5.10). The real controller prefers
    # this path (hardware-measured 8ms vs 3.7s for a full-tree walk on the
    # Z6 II), so the fake MUST implement it with the same semantics as
    # set_config: write log, silent dial reverts, movie refusal.
    def get_single_config(self, name: str) -> _Widget:
        widgets = self._visible_widgets()
        if name not in widgets:
            raise GPhoto2Error(f"Widget not found: {name}")
        if self._profile == "a7r4":
            # DETACHED copy (Sony write semantics): reads always show the
            # authoritative (settled) value; a set_value on the copy carries
            # the REQUEST to set_single_config without mutating state — the
            # real body's readback returns the old value until the async
            # settle lands (hardware-measured).
            auth = widgets[name]
            return _Widget(auth.name, auth.wtype, auth.value,
                           list(auth.choices), auth.wrange, auth.readonly)
        return _ConfigTree(self).get_child_by_name(name)

    def set_single_config(self, name: str, widget) -> None:
        with self._lock:
            self.config_write_log.append((time.time(), "set_single_config", name))
        if self._profile == "a7r4":
            self._sony_apply_write(name, widget)
            return
        if name == "movie" and int(self._widgets["movie"].value or 0) == 1 and _cfg("movie_toggle_fails"):
            self._widgets["movie"].value = 0
            raise GPhoto2Error("[-1] Unspecified error")
        prohibit = self._widgets.get("movieprohibit")
        if prohibit is not None:
            prohibit.value = _cfg("movie_prohibit_text")

    # -- Sony write semantics (every rule hardware-measured 2026-07-12) ----
    def _sony_apply_write(self, name: str, widget) -> None:
        now = time.time()
        requested = widget.get_value()
        auth = self._widgets.get(name)
        if auth is None:
            raise GPhoto2Error(f"Widget not found: {name}")

        # Action toggles first (never busy-gated on the real body).
        if name == "capture":
            state = int(requested or 0)
            with self._lock:
                if state == 1 and self._capture_pressed_at is None:
                    self._capture_pressed_at = now
                    self._capture_released_at = None
                    self._burst_emitted = 0
                elif state == 0 and self._capture_pressed_at is not None \
                        and self._capture_released_at is None:
                    self._capture_released_at = now
            return
        if name == "movie":
            # Accepted silently; recording is UNVERIFIABLE over USB — no
            # events, no state change visible (measured).
            self._movie_state = int(requested or 0)
            return
        if name == "autofocus":
            state = int(requested or 0)
            indication = self._widgets.get("focusindication")
            if indication is not None:
                if state == 1:
                    indication.value = ("No Focus - Low Contrast"
                                        if _cfg("sony_af_wont_lock") else "Focus Locked")
                else:
                    indication.value = "Unlock"
            return
        if name == "manualfocus":
            # Requires focusmode=Manual, else [-2] (measured); step code ±1..7.
            if str(self._widgets["focusmode"].value) != "Manual":
                raise GPhoto2Error("[-2] Bad parameters")
            step = float(requested or 0.0)
            if abs(step) > 7:
                raise GPhoto2Error("[-2] Bad parameters")
            position = self._widgets.get("focalposition")
            if position is not None:
                moved = float(position.value) - (0.5 * step)
                position.value = max(0.0, min(100.0, moved))
            return
        if name == "bulb":
            return  # accepted; bulb remains out of scope (never settled remotely)

        # Config widgets: busy gate, validation, async settle.
        if now < self._busy_write_until:
            # Rapid consecutive writes: the body is still applying the
            # previous change (measured).
            raise GPhoto2Error("[-2] Bad parameters")
        if auth.readonly:
            raise GPhoto2Error("[-2] Bad parameters")
        if auth.choices and str(requested) not in [str(c) for c in auth.choices]:
            raise GPhoto2Error("[-2] Bad parameters")
        self._busy_write_until = now + _scaled(_cfg("sony_busy_window_s"))

        lose = dict(_cfg("sony_lose_writes") or {})
        if lose.get(name, 0) > 0:
            # SILENTLY LOST write (measured: accepted, never settles, no
            # error). The scenario counter decrements so the retry lands.
            lose[name] = lose[name] - 1
            with _scenario_lock:
                _scenario["sony_lose_writes"] = lose
            return
        if name == "capturemode" and str(self._widgets["prioritymode"].value) != "Application":
            # Drive-mode writes never settle under Camera priority (measured).
            return
        lo, hi = _cfg("sony_settle_delay_s")
        due = now + _scaled(_rng.uniform(float(lo), float(hi)))
        with self._lock:
            self._pending_settles.append((due, name, requested))

    def set_config(self, config: _ConfigTree):
        # Widgets mutate on set_value (same object identity); log the commit.
        with self._lock:
            self.config_write_log.append((time.time(), "set_config", None))
        # Movie start refusal (bug-3 coverage): mirrors the real Z6 II with
        # the photo/movie selector on photo — [-1] with no useful text; the
        # useful text lives in the movieprohibit STATUS widget.
        movie = self._widgets.get("movie")
        if movie is not None and int(movie.value or 0) == 1 and _cfg("movie_toggle_fails"):
            movie.value = 0
            raise GPhoto2Error("[-1] Unspecified error")
        # Keep the prohibit text in sync with the scenario.
        prohibit = self._widgets.get("movieprohibit")
        if prohibit is not None:
            prohibit.value = _cfg("movie_prohibit_text")

    # -- preview --
    def capture_preview(self):
        if _cfg("preview_fail"):
            raise GPhoto2Error("[-7] I/O problem (simulated USB pull)")
        # Nikon bodies refuse preview DURING an exposure (breaker fidelity
        # finding); the Sony A7R IV keeps serving frames through exposures
        # (hardware-measured 8-31ms responses mid-exposure).
        if (self._profile != "a7r4" and _cfg("busy_nak")
                and time.time() < self._busy_until):
            raise GPhoto2Error("[-110] I/O in progress (exposure running, simulated)")
        time.sleep(_scaled(_cfg("preview_interval_s")))
        liveviewsize = self._widgets.get("liveviewsize")
        # Sony has no liveviewsize widget: fixed 1024x680 (measured) = XGA.
        size_name = str(liveviewsize.value) if liveviewsize is not None else "XGA"
        has_injection = _cfg("inject_streaks") or _cfg("inject_motion_blobs") or _cfg("gain_step")
        if size_name != "VGA" or has_injection:
            # Live render: resolution follows the widget (bug-1 regression)
            # and scripted detection overlays are time-dependent.
            return _CamFile(_render_preview_jpeg(size_name))
        return _CamFile(_TINY_JPEG)

    # -- capture --
    def current_exposure_seconds(self) -> float:
        widget = self._widgets["shutterspeed"]
        text = str(widget.value).strip().rstrip("s")
        if text in ("Bulb", "Time"):
            return 30.0
        try:
            if "/" in text:
                num, den = text.split("/", 1)
                return float(num) / float(den)
            return float(text)
        except (ValueError, ZeroDivisionError):  # '0/0' placeholder included
            return 1.0 / 30.0

    def _emit_capture_files(self, fired_at: float, shots: int, exposure: float) -> None:
        quality = str(self._widgets["imagequality"].value)
        emit_raw_jpeg = _cfg("emit_raw_and_jpeg")
        if emit_raw_jpeg is None:
            emit_raw_jpeg = "+" in quality
        if self._profile == "a7r4":
            # Measured on the body: FILE_ADDED at folder '/', capt_*.ARW.
            folder, stem = "/", "capt_A7R"
            primary_ext = ".ARW" if "RAW" in quality else ".JPG"
        else:
            folder, stem = "/store_00010001/DCIM/100NZ6_2", "DSC_"
            primary_ext = ".NEF" if "NEF" in quality else ".JPG"
        for shot in range(shots):
            with self._lock:
                self._file_counter += 1
                n = self._file_counter
            due = fired_at + _scaled(exposure * (shot + 1) + _cfg("file_added_offset_s"))
            self._push_event(due, GP_EVENT_FILE_ADDED,
                             _EventData(folder, f"{stem}{n:04d}{primary_ext}"))
            if emit_raw_jpeg:
                self._push_event(due + 0.05, GP_EVENT_FILE_ADDED,
                                 _EventData(folder, f"{stem}{n:04d}.JPG"))

    def trigger_capture(self):
        now = time.time()
        if _cfg("trigger_fail_always"):
            raise GPhoto2Error("[-110] I/O in progress (camera busy, simulated)")

        if self._profile == "a7r4":
            # Sony semantics (measured): the call blocks a FIXED ~1.2s (or
            # ~0.03s in Manual focus) regardless of the exposure length.
            focusmode = str(self._widgets["focusmode"].value)
            block_s = 0.03 if focusmode == "Manual" else _cfg("sony_trigger_block_s")
            time.sleep(_scaled(block_s))
            if focusmode != "Manual" and _cfg("sony_af_wont_lock"):
                # SILENT refusal: trigger accepted, nothing fires (measured).
                return
            fired_at = time.time()
            exposure = self.current_exposure_seconds()
            with self._lock:
                self.trigger_log.append(fired_at)
                self._busy_until = fired_at + _scaled(exposure)
            self._emit_capture_files(fired_at, 1, exposure)
            return

        if _cfg("busy_nak") and now < self._busy_until:
            raise GPhoto2Error("[-110] I/O in progress (camera busy, simulated)")
        latency = _cfg("trigger_latency_s") + _rng.uniform(
            -_cfg("trigger_latency_jitter_s"), _cfg("trigger_latency_jitter_s")
        )
        time.sleep(_scaled(max(0.0, latency)))
        exposure = self.current_exposure_seconds()
        fired_at = time.time()
        # Burst drive: one trigger = burstnumber frames (real Z bodies fire
        # the whole burst on a single remote trigger in Burst drive mode).
        shots = 1
        capturemode = self._widgets.get("capturemode")
        if capturemode is not None and str(capturemode.value) == "Burst":
            try:
                shots = max(1, int(float(self._widgets["burstnumber"].value)))
            except Exception:
                shots = 1
        with self._lock:
            self.trigger_log.append(fired_at)
            self._busy_until = fired_at + _scaled(exposure) * shots
        self._emit_capture_files(fired_at, shots, exposure)

    def _push_event(self, due: float, event_type: int, data) -> None:
        with self._lock:
            heapq.heappush(self._events, (due, next(self._event_tiebreak), event_type, data))

    def inject_event(self, delay_s: float, event_type: int, data) -> None:
        """Test hook: push an arbitrary event (card full, etc)."""
        self._push_event(time.time() + _scaled(delay_s), event_type, data)

    # -- Sony time-driven state (settles, burst emission, noise) -------------
    def _sony_service_state(self) -> None:
        now = time.time()
        # 1. Apply due config settles (writes land during event pumping —
        #    exactly how the real body behaves).
        with self._lock:
            due_settles = [s for s in self._pending_settles if s[0] <= now]
            self._pending_settles = [s for s in self._pending_settles if s[0] > now]
        for _due, name, value in due_settles:
            auth = self._widgets.get(name)
            if auth is not None:
                auth.set_value(value)
        # 2. Continuous-drive burst: while `capture` is held (and for the
        #    already-elapsed hold after release), emit files at sony_burst_fps
        #    from press+0.35s. Measured: 9 files for a 1.2s hold at Hi, with
        #    a multi-second announce tail.
        with self._lock:
            pressed_at = self._capture_pressed_at
            released_at = self._capture_released_at
        if pressed_at is not None:
            drive = str(self._widgets["capturemode"].value)
            continuous = drive.startswith("Continuous Shooting")
            fps = max(0.5, float(_cfg("sony_burst_fps")))
            end = released_at if released_at is not None else now
            hold_s = max(0.0, end - pressed_at)
            total = max(1, int(math.floor(hold_s * fps))) if continuous else 1
            if released_at is None and continuous:
                # Mid-hold: only frames whose fire time has passed.
                total = int(math.floor(max(0.0, now - pressed_at) * fps))
            emitted = self._burst_emitted
            if total > emitted:
                exposure = self.current_exposure_seconds()
                for k in range(emitted, total):
                    fire_time = pressed_at + 0.35 + k / fps
                    with self._lock:
                        self._file_counter += 1
                        n = self._file_counter
                    # Announce tail: ~1.1s spacing after release (measured).
                    due = max(fire_time + _scaled(exposure + _cfg("file_added_offset_s")),
                              now + _scaled(0.05) * (k - emitted))
                    self._push_event(due, GP_EVENT_FILE_ADDED,
                                     _EventData("/", f"capt_A7R{n:04d}.ARW"))
                self._burst_emitted = total
            if released_at is not None and total <= self._burst_emitted:
                with self._lock:
                    self._capture_pressed_at = None
                    self._capture_released_at = None
        # 3. Property-change noise (the real body streams these constantly).
        noise_interval = float(_cfg("sony_property_noise_interval_s") or 0.0)
        if noise_interval > 0 and now - self._last_noise_at >= noise_interval:
            self._last_noise_at = now
            self._push_event(now, GP_EVENT_UNKNOWN,
                             'PTP Property 00000000 changed, "PTP Property 0x0000" to "Unknown type 0x0000"')

    def wait_for_event(self, timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if self._profile == "a7r4":
                self._sony_service_state()
            with self._lock:
                head = self._events[0] if self._events else None
            now = time.time()
            if head is not None and head[0] <= now:
                with self._lock:
                    due, _tb, event_type, data = heapq.heappop(self._events)
                    if (self._profile == "a7r4" and event_type == GP_EVENT_FILE_ADDED
                            and data is not None):
                        # sdram slot model (hardware-observed): each announced
                        # object evicts all but the ~2 most recent UNFETCHED
                        # ones; evicted names answer [-1] on file_get. Prompt
                        # per-announce fetches never hit this; batch drains
                        # after a burst lose the middle files — exactly the
                        # measured failure.
                        self._sony_available_objects.append(data.name)
                        del self._sony_available_objects[:-2]
                return event_type, data
            wait_until = min(deadline, head[0]) if head is not None else deadline
            if now >= deadline:
                return GP_EVENT_TIMEOUT, None
            time.sleep(min(0.005, max(0.0005, wait_until - now)))

    def file_get(self, folder: str, name: str, file_type: int):
        if self._profile == "a7r4":
            with self._lock:
                if name not in self._sony_available_objects:
                    # Evicted from the sdram slot (hardware truth: [-1]).
                    raise GPhoto2Error("[-1] Unspecified error")
                self._sony_available_objects.remove(name)
            # Near-instant: the driver pre-spooled the object during event
            # pumping (53MB in ~10ms measured).
            time.sleep(_scaled(0.01))
            return _CamFile(_TINY_JPEG)
        lo, hi = _cfg("download_stall_s")
        time.sleep(_scaled(_rng.uniform(lo, hi)))
        return _CamFile(_TINY_JPEG)
