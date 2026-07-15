"""Camera family adapter boundary (elected design, 2026-07-12).

The CameraController owns the worker thread, the scheduling windows, the
pending-write ledger, downloads, detection, and the catch log. EVERY camera
touch goes through a CameraAdapter selected at connect time from the detected
model, so family quirks (write settling, trigger semantics, burst mechanics,
movie honesty, event noise) live in one adapter file per family instead of
conditionals inside the hardware-validated worker loop.

THREADING CONTRACT: adapter methods that take a `camera` argument run ONLY on
the controller's worker thread and may block (bounded by `time_budget_s`
where present). Everything else must be pure/read-only after attach(). The
controller owns the camera session lifecycle (init/exit/reconnect); adapters
never create or destroy it. Adapters that pump `wait_for_event` internally
(write verification, burst holds) MUST forward every non-timeout event to the
event sink provided at attach() — swallowing a FILE_ADDED would lose a shot
announcement.

GenericPtpAdapter is the PRE-adapter controller behavior moved verbatim
(hardware-validated on the Nikon Z6 II): unknown bodies behave exactly as
they did before the extraction. NikonZAdapter only adds identity/capability
metadata on top.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# Widget probe list for generic PTP bodies (moved verbatim from
# camera_control.CONFIG_WIDGET_NAMES; the Nikon-specific dual names simply
# don't resolve on other bodies and stay out of the cache).
GENERIC_CONFIG_WIDGET_NAMES = (
    "iso",
    "isoauto",
    "shutterspeed",
    "shutterspeed2",
    "f-number",
    "whitebalance",
    # Kelvin WB: body-dependent name — both are probed, absent ones simply
    # don't appear in the cache.
    "colortemperature",
    "whitebalancetemperature",
    "exposurecompensation",
    "expprogram",
    "usermode",
    "capturemode",
    "burstnumber",
    "shootingspeed",
    "capturetarget",
    "imagequality",
    "imagesize",
    "liveviewsize",
    "focusmode",
    "exposuremetermode",
    "batterylevel",
    # Movie diagnostics: the body's own prohibit reasons (RO status text on
    # Nikon Z) and the recording-media route.
    "movieprohibit",
    "recordingmedia",
)


@dataclass(frozen=True)
class ConnectDefault:
    """One sane-default write queued at connect (user choices stand later)."""
    name: str
    value: str
    note: str
    ledger: bool = False  # True: track in the pending-write honesty ledger


@dataclass(frozen=True)
class WriteReceipt:
    ok: bool
    # True when the adapter VERIFIED the value on the body before returning
    # (Sony write-verify-retry); False defers settlement to cache refreshes
    # (Nikon path — bit-identical to the pre-adapter behavior).
    settled: bool = False
    actual: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CaptureTiming:
    """What one fire attempt means for the controller's windows."""
    issued_at: float
    command_ms: int
    drain_window_s: float               # FILE_ADDED window from issued_at
    preview_pause_s: float = 0.0        # 0 = don't pause the preview
    preview_pause_replace: bool = False  # True: assignment (Nikon burst), not max()
    expected_files: int | None = None   # burst announce accounting (None = unknown)
    expect_file_within_s: float | None = None  # silent-refusal honesty window
    no_file_note: str | None = None     # catch-log note when that window expires


@dataclass(frozen=True)
class MovieReceipt:
    ok: bool
    recording: bool
    note: str = ""
    refused: bool = False       # pre-check refusal: nothing was written
    error: str | None = None
    hint: str = ""              # appended to the catch-log note only
    confirmed: bool = True      # False: the body cannot confirm recording over USB
    probe_session: bool = False  # True: controller should probe/recover the session
    drain_window_s: float = 0.0


@dataclass(frozen=True)
class ActionReceipt:
    ok: bool
    note: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClassifiedEvent:
    kind: str                   # "timeout" | "file_added" | "status" | "noise"
    folder: str | None = None
    name: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class EscalationResult:
    name: str
    value: str
    ok: bool
    note: str = ""


class CameraAdapter(ABC):
    """One camera family's behavior behind the abstraction boundary."""

    family: str = "generic"
    display_name: str = "PTP camera"
    # Families whose live view keeps flowing during a still exposure
    # (hardware fact per family). The interval scheduler consults this to
    # skip preview pauses so detection stays live between sequence frames.
    preview_survives_exposure: bool = False
    # Families where announced objects are EVICTED by later captures (Sony
    # exposes one sdram slot: a 10-frame burst kept only the first and last
    # objects fetchable — hardware-observed 2026-07-12). True = download each
    # file immediately at announce time (cheap there: the driver pre-spools
    # the object during event pumping, file_get measured ~10ms for 53MB).
    # False = files persist addressable (Nikon card paths) and the deferred-
    # download policy applies unchanged.
    fetch_on_announce: bool = False

    def __init__(self, gp_module):
        self._gp = gp_module
        self._event_sink = None  # set at attach(); callable(event_type, event_data)

    # -- lifecycle (worker thread) ------------------------------------------
    def attach(self, camera, event_sink) -> None:
        """Called once by the worker after camera.init(). `event_sink` routes
        any event the adapter pumps internally back through the controller's
        announce/log paths (never swallow a FILE_ADDED)."""
        self._event_sink = event_sink

    # -- identity / policy (pure, thread-safe) -------------------------------
    @abstractmethod
    def config_widget_names(self) -> tuple[str, ...]:
        ...

    @abstractmethod
    def capabilities(self, config_cache: dict) -> dict:
        """Frontend-facing capability descriptor derived from the config
        cache snapshot. Computed on the worker at each cache refresh."""
        ...

    def settle_patience_s(self) -> float:
        """Ledger patience before mismatches count toward a revert."""
        return 10.0

    def family_action_names(self) -> tuple[str, ...]:
        """Family-specific one-shot ACTIONS beyond the canonical focus
        drives (same contract: executed once, never cached, never replayed
        — a replayed mount slew would physically move a telescope). The
        manager accepts ACTION_WIDGET_NAMES plus these while this family
        is connected."""
        return ()

    def poll_session_events(self, camera) -> None:
        """Called once per worker loop iteration (between preview frames).

        PTP bodies only produce events around captures, so the default is a
        no-op — the capture drain windows handle them. Families whose
        device SPEAKS SPONTANEOUSLY (a telescope reporting GOTO progress,
        battery, tracking state) override this to forward queued session
        events through the event sink. Implementations must be non-blocking
        and bounded (this runs on the live-view thread)."""
        return

    def settle_escalations(self) -> dict[str, str]:
        """widget name -> escalation kind, consulted when a ledger entry is
        about to be declared reverted (e.g. Nikon's live-view-pause retry)."""
        return {}

    def connect_default_writes(self, config_cache: dict) -> list[ConnectDefault]:
        return []

    def connect_warnings(self, config_cache: dict) -> list[str]:
        """Body-state problems worth a loud catch-log warning at connect
        (unformatted card, missing card...). Pure cache inspection."""
        return []

    def sequence_preflight_warnings(self, config_cache: dict) -> list[str]:
        return []

    def capture_mode_plan(self, mode: str, burst_count: int,
                          burst_hold_s: float, burst_speed: str) -> dict[str, str]:
        """Widget writes that make `mode` effective on this body (queued
        through the normal pending-config path, insertion order preserved)."""
        return {}

    # -- camera I/O (worker thread only) --------------------------------------
    @abstractmethod
    def read_config_cache(self, camera) -> dict:
        ...

    @abstractmethod
    def write_widget(self, camera, name: str, value, time_budget_s: float = 5.0) -> WriteReceipt:
        ...

    def is_transient_write_error(self, error: str | None) -> bool:
        """True when a failed write is worth a paced requeue by the
        controller (e.g. Sony answers [-2] busy for seconds after a burst
        while flushing to the card). Generic bodies fail fast (their errors
        are real refusals; retrying dial-owned widgets would just delay the
        honest revert message)."""
        return False

    def nominal_exposure_s(self, config_cache: dict) -> float | None:
        """Effective exposure for families whose exposure is NOT a shutter
        widget (a webcam's is its frame interval). None = the interval
        validator parses the shutter widget exactly as before, including the
        Bulb/'0/0' refusals — the honest path for every PTP body."""
        return None

    def read_serial(self, camera) -> str | None:
        """Best-effort device serial (device identity for capture folders
        and multi-body disambiguation). PTP bodies expose a `serialnumber`
        status widget; absence is normal (webcams) and returns None."""
        from abstractcamera import ptp_safe

        try:
            if self._supports_single_config(camera):
                value = str(ptp_safe.widget_value(
                    camera.get_single_config("serialnumber")) or "").strip()
                value = value.lstrip("0")  # Sony zero-pads to 32 chars
                return value or None
        except Exception:
            pass
        return None

    def save_modes(self) -> list[str]:
        """Where captures can live: 'device' (the camera's own storage) and/
        or 'local' (downloaded to this machine). PTP bodies support both —
        the local download is a POLICY (files stay on the card either way,
        capturetarget permitting); a webcam has no storage of its own."""
        return ["device", "local"]

    def escalate_writes(self, camera, items: list[tuple[str, str]], kind: str) -> list[EscalationResult]:
        return [EscalationResult(name=n, value=v, ok=False,
                                 note=f"no escalation available for {n}") for n, v in items]

    @abstractmethod
    def fire_single(self, camera, exposure_s: float, config_cache: dict) -> CaptureTiming:
        ...

    @abstractmethod
    def fire_burst(self, camera, exposure_s: float, count: int, hold_s: float) -> CaptureTiming:
        ...

    @abstractmethod
    def toggle_movie(self, camera, start: bool) -> MovieReceipt:
        ...

    @abstractmethod
    def run_action(self, camera, name: str, value: str | None, config_cache: dict) -> ActionReceipt:
        ...

    @abstractmethod
    def classify_event(self, event_type, event_data) -> ClassifiedEvent:
        ...

    def diagnose_trigger_failure(self, camera, error: str) -> str | None:
        """A named cause for a failed trigger when the body offers one
        (worker thread; may read status widgets). None = no diagnosis."""
        return None

    # -- shared plumbing -------------------------------------------------------
    @staticmethod
    def _supports_single_config(camera) -> bool:
        return hasattr(camera, "get_single_config") and hasattr(camera, "set_single_config")

    def _raw_write_widget(self, camera, name: str, value) -> None:
        """One widget write. HARDWARE-MEASURED (Z6 II, 2026-07-07): a full
        get_config tree walk costs 3.7s vs 8ms for get_single_config — the
        full-tree path made EVERY control feel dead. Single-config is the
        only acceptable path; the full tree is a fallback for libraries
        without it."""
        gp = self._gp
        if self._supports_single_config(camera):
            widget = camera.get_single_config(name)
            widget_type = widget.get_type()
            if widget_type == gp.GP_WIDGET_RANGE:
                widget.set_value(float(value))
            elif widget_type == gp.GP_WIDGET_TOGGLE:
                widget.set_value(int(value))
            else:
                widget.set_value(str(value))
            camera.set_single_config(name, widget)
            return
        config = camera.get_config()
        widget = config.get_child_by_name(name)
        widget_type = widget.get_type()
        if widget_type == gp.GP_WIDGET_RANGE:
            widget.set_value(float(value))
        elif widget_type == gp.GP_WIDGET_TOGGLE:
            widget.set_value(int(value))
        else:
            widget.set_value(str(value))
        camera.set_config(config)

    def _read_entry(self, widget) -> dict:
        from abstractcamera import ptp_safe

        gp = self._gp
        entry: dict = {
            # NULL-guarded (ptp_safe): a body can hand back a NULL string
            # value transiently — python-gphoto2's own get_value() SEGFAULTS
            # on it (observed on a Sony A7R IV connect, 2026-07-12).
            "value": ptp_safe.widget_value(widget),
            # Read-only mirrors the camera's own rules (mode dial position,
            # lens capabilities); the UI shows locks.
            "readonly": bool(widget.get_readonly()),
        }
        if widget.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            entry["choices"] = ptp_safe.widget_choices(widget)
        elif widget.get_type() == gp.GP_WIDGET_RANGE:
            low, high, step = widget.get_range()
            entry["range"] = [float(low), float(high), float(step)]
        return entry


class GenericPtpAdapter(CameraAdapter):
    """The pre-adapter controller mechanics, moved verbatim. Hardware truth
    behind every timing constant is the Nikon Z6 II (2026-07-07/08); unknown
    bodies get exactly the behavior the controller always had."""

    family = "generic"
    display_name = "PTP camera"
    preview_survives_exposure = False

    # -- identity / policy -----------------------------------------------------
    def config_widget_names(self) -> tuple[str, ...]:
        return GENERIC_CONFIG_WIDGET_NAMES

    def settle_patience_s(self) -> float:
        # Revert declaration: patience covers the measured 5-7s Nikon settle.
        return 10.0

    def settle_escalations(self) -> dict[str, str]:
        # Widgets that some bodies silently revert WHILE remote live view is
        # engaged but accept with the viewfinder released (hardware: Z6 II
        # kept isoauto=On through a ledger-tracked write in M mode,
        # 2026-07-08). One retry with live view paused before the honest
        # revert declaration.
        return {"isoauto": "lv_pause"}

    def connect_default_writes(self, config_cache: dict) -> list[ConnectDefault]:
        defaults: list[ConnectDefault] = []
        # Default-to-card: "Internal RAM" means photos exist ONLY via USB
        # download and die with the session — the owner lost buffered shots
        # to exactly this. One queued write at connect, with an explicit
        # event; later user choices stand.
        target_entry = config_cache.get("capturetarget") or {}
        if "ram" in str(target_entry.get("value", "")).lower():
            card_choice = next(
                (c for c in target_entry.get("choices", []) if "card" in str(c).lower()),
                None,
            )
            if card_choice:
                defaults.append(ConnectDefault(
                    name="capturetarget", value=str(card_choice),
                    note="Save To was set to the memory card (was: camera buffer). Change it back for USB-only workflows.",
                    ledger=False,
                ))
        # Default ISO Auto to Off at connect (owner request 2026-07-08):
        # Auto silently overrides every manual ISO choice. Ledger-tracked so
        # the live-view-pause retry and revert honesty both apply.
        isoauto_entry = config_cache.get("isoauto") or {}
        if str(isoauto_entry.get("value", "")).lower() == "on":
            defaults.append(ConnectDefault(
                name="isoauto", value="Off",
                note="ISO Auto was turned Off (it overrides manual ISO). Flip the ISO Auto dial to On if you want it back.",
                ledger=True,
            ))
        return defaults

    def capture_mode_plan(self, mode: str, burst_count: int,
                          burst_hold_s: float, burst_speed: str) -> dict[str, str]:
        if mode == "single":
            return {"capturemode": "Single Shot"}
        if mode == "burst":
            return {"capturemode": "Burst", "burstnumber": str(burst_count)}
        # Movies must land on the card; RAM recording is rejected.
        return {"recordingmedia": "Card"}

    def connect_warnings(self, config_cache: dict) -> list[str]:
        """The movieprohibit status text carries CARD problems that block
        stills too (hardware-observed on a Z6 II: 'Card not formatted' made
        every trigger_capture fail with a useless [-1]). Say it at connect,
        where the user can still fix it."""
        prohibit = str((config_cache.get("movieprohibit") or {}).get("value", ""))
        if "card" in prohibit.lower():
            reasons = prohibit.replace("Movie prohibit conditions:", "").strip()
            card_reasons = [r.strip() for r in reasons.split(",") if "card" in r.lower()]
            if card_reasons:
                return [
                    f"the camera reports: {', '.join(card_reasons)} — captures to the "
                    "card WILL FAIL. Format the card in the camera, or switch Save To "
                    "to the camera buffer (photos then exist only via USB download)."
                ]
        return []

    def capabilities(self, config_cache: dict) -> dict:
        save_to = config_cache.get("capturetarget") or {}
        choices = [str(c) for c in save_to.get("choices", [])]
        # Buffer-only targets: "ram" without "card" (Nikon: "Internal RAM").
        volatile = [c for c in choices if "ram" in c.lower() and "card" not in c.lower()]
        recommended = next((c for c in choices if "card" in c.lower()), None)
        return {
            "family": self.family,
            "display_name": self.display_name,
            # Widgets this family can EVER have: host UIs hide controls for
            # absent names instead of rendering locked ghosts (Mod #7).
            "config_widgets": list(self.config_widget_names()),
            "burst": {"mode": "count", "min": 2, "max": 200},
            "movie": {
                "can_preflight": "movieprohibit" in config_cache,
                "can_confirm": True,
                "note": None,
            },
            "iso_auto": (
                {"kind": "widget", "widget": "isoauto"}
                if "isoauto" in config_cache else {"kind": "none"}
            ),
            "save_to": {
                "volatile_values": volatile,
                "recommended_value": recommended,
                "labels": {},
                "modes": self.save_modes(),
            },
            "focus": {"mf_requires_manual_focus": False, "indication_widget": None},
            "preview_during_exposure": self.preview_survives_exposure,
        }

    # -- camera I/O -------------------------------------------------------------
    def read_config_cache(self, camera) -> dict:
        cache: dict = {}
        try:
            if self._supports_single_config(camera):
                # HARDWARE-MEASURED (Z6 II, 2026-07-07): 23 single-config
                # reads take 0.26s vs 3.7s for one full-tree walk.
                for name in self.config_widget_names():
                    try:
                        cache[name] = self._read_entry(camera.get_single_config(name))
                    except Exception:
                        continue
            else:
                config = camera.get_config()
                for name in self.config_widget_names():
                    try:
                        cache[name] = self._read_entry(config.get_child_by_name(name))
                    except Exception:
                        continue
        except Exception:
            pass
        return cache

    def write_widget(self, camera, name: str, value, time_budget_s: float = 5.0) -> WriteReceipt:
        try:
            self._raw_write_widget(camera, name, value)
            # settled=False: settlement rides on cache refreshes, exactly as
            # before the extraction (Nikon applies lazily, ~5-7s).
            return WriteReceipt(ok=True, settled=False)
        except Exception as exc:
            return WriteReceipt(ok=False, error=str(exc))

    def escalate_writes(self, camera, items: list[tuple[str, str]], kind: str) -> list[EscalationResult]:
        """One-shot retry for live-view-gated widget writes: release the
        remote viewfinder, rewrite, re-engage (moved verbatim)."""
        results: list[EscalationResult] = []
        released = False
        try:
            self._raw_write_widget(camera, "viewfinder", 0)
            released = True
            time.sleep(0.4)
        except Exception:
            pass  # body without the widget: retry the write anyway
        for name, value in items:
            try:
                self._raw_write_widget(camera, name, value)
                results.append(EscalationResult(
                    name=name, value=value, ok=True,
                    note=f"retried {name} = {value} with live view paused"))
            except Exception as exc:
                results.append(EscalationResult(
                    name=name, value=value, ok=False,
                    note=f"{name} = {value} also refused with live view paused: {exc}"))
        if released:
            try:
                self._raw_write_widget(camera, "viewfinder", 1)
            except Exception:
                pass
        return results

    def fire_single(self, camera, exposure_s: float, config_cache: dict) -> CaptureTiming:
        # Anchor at command ISSUE: on the Z6 II trigger_capture blocks for
        # the whole exposure (hardware-measured 2026-07-07), so anchoring
        # windows at return would double every exposure-derived window.
        issued_at = time.time()
        command_started = time.perf_counter()
        camera.trigger_capture()  # exceptions propagate to the controller
        command_ms = int(round((time.perf_counter() - command_started) * 1000))
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            # Captured files announce themselves AFTER the exposure ends:
            # the drain window covers the exposure plus a readout/card tail.
            drain_window_s=exposure_s + 15.0,
            # Pause preview for the exposure: pulling preview frames during
            # an exposure fails on real bodies (counts toward the watchdog).
            preview_pause_s=(exposure_s + 0.5) if exposure_s > 0.5 else 0.0,
        )

    def fire_burst(self, camera, exposure_s: float, count: int, hold_s: float) -> CaptureTiming:
        # Burst drive: one trigger = burstnumber frames (Z bodies fire the
        # whole burst on a single remote trigger in Burst drive mode).
        issued_at = time.time()
        command_started = time.perf_counter()
        camera.trigger_capture()
        command_ms = int(round((time.perf_counter() - command_started) * 1000))
        return CaptureTiming(
            issued_at=issued_at,
            command_ms=command_ms,
            drain_window_s=exposure_s + 15.0,
            # Live view frames abort bursts on some bodies: pause preview for
            # the whole burst window (assignment semantics preserved from the
            # pre-adapter code, hence preview_pause_replace).
            preview_pause_s=(time.time() - issued_at) + min(12.0, 2.0 + 0.6 * count),
            preview_pause_replace=True,
            expected_files=count,
        )

    def _read_movie_prohibit_reasons(self, camera) -> str:
        """The body's own explanation of why movie start is refused
        (hardware-verified on the Z6 II: a RO status text). Empty when
        recording is allowed or the widget is absent."""
        try:
            from abstractcamera import ptp_safe

            if self._supports_single_config(camera):
                widget = camera.get_single_config("movieprohibit")
            else:
                config = camera.get_config()
                widget = config.get_child_by_name("movieprohibit")
            text = str(ptp_safe.widget_value(widget) or "").strip()
            if not text or "should not be prohibited" in text.lower():
                return ""
            return text.replace("Movie prohibit conditions:", "").strip()
        except Exception:
            return ""

    def toggle_movie(self, camera, start: bool) -> MovieReceipt:
        target_state = 1 if start else 0
        if start:
            # Ask the camera FIRST: on Nikon Z the movie toggle fails with a
            # useless "[-1] Unspecified error" while the movieprohibit status
            # carries the actual reasons. Refusing BEFORE any write avoids
            # the session wedge entirely.
            prohibit = self._read_movie_prohibit_reasons(camera)
            if prohibit:
                friendly = prohibit
                if "liveview selector" in prohibit.lower() or "application mode" in prohibit.lower():
                    friendly += " — flip the camera's photo/video selector to the movie position"
                return MovieReceipt(ok=False, recording=False, refused=True, error=friendly)
        try:
            self._raw_write_widget(camera, "movie", target_state)
            return MovieReceipt(
                ok=True, recording=start,
                note="video recording started" if start else "video recording stopped",
                drain_window_s=0.0 if start else 15.0,
            )
        except Exception as exc:
            # Include the REAL exception text; the selector hint is a suffix,
            # not a replacement.
            prohibit = self._read_movie_prohibit_reasons(camera)
            detail = f"{exc}" + (f" — camera reports: {prohibit}" if prohibit else "")
            hint = (" — flip the camera's photo/video selector to the movie position"
                    if start and not prohibit else "")
            # Wedge recovery (hardware-observed): a failed movie toggle can
            # leave the PTP session half-dead. The controller owns the
            # session lifecycle, so it runs the probe/recovery. Recording
            # state is unchanged by a failed toggle: a failed start leaves us
            # not recording, a failed stop leaves the camera still rolling.
            return MovieReceipt(ok=False, recording=not start,
                                error=detail, hint=hint, probe_session=True)

    def run_action(self, camera, name: str, value: str | None, config_cache: dict) -> ActionReceipt:
        gp = self._gp
        try:
            # Single-config path: AF drive measured 0.44s vs ~4s through the
            # full tree (user report 2026-07-07: "autofocus EXTREMELY slow").
            if self._supports_single_config(camera):
                widget = camera.get_single_config(name)
                widget_type = widget.get_type()
                if widget_type == gp.GP_WIDGET_RANGE:
                    widget.set_value(float(value if value is not None else 0))
                elif widget_type == gp.GP_WIDGET_TOGGLE:
                    widget.set_value(1)
                else:
                    widget.set_value(str(value or ""))
                camera.set_single_config(name, widget)
            else:
                config = camera.get_config()
                widget = config.get_child_by_name(name)
                widget_type = widget.get_type()
                if widget_type == gp.GP_WIDGET_RANGE:
                    widget.set_value(float(value if value is not None else 0))
                elif widget_type == gp.GP_WIDGET_TOGGLE:
                    widget.set_value(1)
                else:
                    widget.set_value(str(value or ""))
                camera.set_config(config)
            return ActionReceipt(ok=True)
        except Exception as exc:
            return ActionReceipt(ok=False, error=str(exc))

    def classify_event(self, event_type, event_data) -> ClassifiedEvent:
        gp = self._gp
        if event_type == gp.GP_EVENT_TIMEOUT:
            return ClassifiedEvent(kind="timeout")
        if event_type == gp.GP_EVENT_FILE_ADDED and event_data is not None:
            return ClassifiedEvent(kind="file_added",
                                   folder=event_data.folder, name=event_data.name)
        # Card-full and other camera-side errors used to be silently
        # discarded — surface anything that carries text (keyword-gated for
        # UNKNOWN events, verbatim from the pre-adapter filter).
        note = str(event_data).strip() if event_data is not None else ""
        if note and event_type != gp.GP_EVENT_UNKNOWN:
            return ClassifiedEvent(kind="status", note=note[:200])
        if note and ("full" in note.lower() or "error" in note.lower() or "fail" in note.lower()):
            return ClassifiedEvent(kind="status", note=note[:200])
        return ClassifiedEvent(kind="noise")

    def diagnose_trigger_failure(self, camera, error: str) -> str | None:
        """A cause for a failed trigger, when the body can name one. Nikon Z
        fails with a bare '[-1] Unspecified error' while the movieprohibit
        STATUS text carries the real reason — hardware-observed: an
        unformatted card fails EVERY capture this way."""
        try:
            prohibit = self._read_movie_prohibit_reasons(camera)
        except Exception:
            return None
        if prohibit and "card" in prohibit.lower():
            card_reasons = [r.strip() for r in prohibit.split(",") if "card" in r.lower()]
            if card_reasons:
                return (f"the camera reports: {', '.join(card_reasons)} — format the card "
                        "in the camera, or switch Save To to the camera buffer")
        return None
