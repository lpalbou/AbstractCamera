# API reference

## Module surface

```python
from abstractcamera import (
    CameraManager,           # one camera: the orchestrator (alias: CameraController)
    CameraHub,               # several cameras at once (one manager/worker each)
    CameraControlError,      # all camera errors (alias: CameraError)
    list_cameras,            # non-invasive discovery across transports
    is_tethering_available,  # gphoto2-shaped transport resolves (PTP-only meaning)
    get_default_manager,     # process-wide instance + atexit release
    parse_jpeg_dimensions,   # JPEG SOF probe (no decode)
    ACTION_WIDGET_NAMES, CONFIG_WIDGET_NAMES,
)
```

## CameraHub (multi-camera hosts)

| Method | Contract |
| --- | --- |
| `CameraHub(capture_root=None, manager_factory=None)` | Registry of live managers keyed by device uid. `capture_root` is applied to every new manager. |
| `configure_managers(capture_root=, frame_analyzer=)` | Shared configuration applied to every new connection. |
| `list_cameras()` | Discovery entries annotated with live state: `connected`, `device_uid`, `active`. |
| `annotate_entries(entries)` | (Re)annotate cached discovery entries with CURRENT live state — for callers that cache the USB probe but must never serve stale connection flags. |
| `connect(camera_id=None)` | Connects (or returns the existing session for that id) and makes it ACTIVE. Other cameras keep running. Returns the status dict (+`device_uid`, `active`). |
| `manager_for(device_uid=None)` | The addressed `CameraManager` (None = the active one). Raises with honest text when absent. |
| `select(device_uid)` | Re-point the ACTIVE selection (single-panel hosts bind their controls to it). |
| `statuses()` | `device_uid -> status()` for every live camera (+`active` flag). |
| `disconnect(device_uid=None)` / `disconnect_all()` | Tear down one (active fallback: any survivor) or all. |

Device uid = the device slug (model/label snake_case, e.g. `nikon_z6_2`,
`macbook_pro_camera`), suffixed by serial tail or index when two identical
bodies are connected. Captures land in `<capture_root>/<device_uid>/`.

## CameraManager (all methods thread-safe)

| Method | Contract |
| --- | --- |
| `connect(camera_id=None)` | Claims the camera (default: first PTP body, else built-in webcam). Family defaults are applied with visible catch-log events. Raises `CameraControlError` with honest text. |
| `disconnect()` | Stops the worker (10s join), flushes deferred downloads first. |
| `list_cameras()` | Discovery entries: `{id, transport, name, name_confidence, default, ...}`. |
| `status()` | Full state: `available, connected, model, family, transport, camera_id, capabilities, config, pending_writes, fps, preview_size, detection_*, downloads_pending, rolling, interval, capture_mode, burst_*, movie_recording, last_error`. |
| `get_latest_frame()` | `(jpeg_bytes | None, sequence_int)` — the live-view frame. |
| `set_config_value(name, value)` | Queues a widget write; tracked in the `pending_writes` ledger until confirmed or explicitly reverted. Validated against the family's widget list. |
| `request_trigger()` | Fires the current capture mode (single/burst/video toggle). Refused during interval sequences. |
| `request_action(name, value=None)` | One-shot focus actions (`autofocusdrive`, `manualfocusdrive`); never cached, never replayed. |
| `set_capture_mode(mode, burst_count=, burst_hold_s=, burst_speed=)` | `single|burst|video`; burst knobs are family-dependent (see `capabilities.burst.mode`). |
| `set_detection_mode(mode, target=, sensitivity=)` | `off|monitor|auto` × `lightning|meteor|motion`; auto-fire is arbitrated against sequences. |
| `start_interval_sequence(interval_s, count, start_delay_s=0, liveview=True, sequence_name=None)` | Absolute-deadline intervalometer; validates exposure vs interval (family `nominal_exposure_s` when no shutter widget exists); JSONL manifest per sequence. `sequence_name` names the run (see `set_sequence_name`). |
| `stop_interval_sequence()` | Graceful stop; terminal ledger persists in `status()["interval"]`. |
| `set_rolling_buffer(enabled, seconds=)` / `save_rolling_clip()` | Last-N-seconds pre-capture ring; snapshot to MP4 (`[clips]`). |
| `get_events(since_id=0)` / `clear_events()` | Catch log (captures, detections, config honesty, errors) with thumbnails. |
| `set_capture_root(path)` | Device-layout root (default `~/Pictures`): captures land in `<root>/<device_slug>/`. |
| `set_sequence_name(name)` | Names the shooting sequence: everything captured while set (stills, bursts, movies, clips, manifests) nests in `.../<sequence_name>/`. `None` clears. |
| `set_save_policy(download_locally)` | `False` leaves captures on the camera's own storage (announced, never fetched); refused honestly by families without storage (`capabilities.save_to.modes`). Warns loudly when combined with a volatile capture target. |
| `set_capture_dir(path)` / `set_frame_analyzer(fn)` | Host integration: legacy explicit download directory (overrides the device layout); injected lightning analyzer. |

## The capabilities descriptor (`status()["capabilities"]`)

```python
{
  "family": "sony_alpha" | "nikon_z" | "webcam" | "generic",
  "display_name": str,
  "config_widgets": [...],      # dials this family can EVER have (hide the rest)
  "burst": {"mode": "count"|"duration", ...},
  "movie": {"can_preflight": bool, "can_confirm": bool, "note": str|None},
  "iso_auto": {"kind": "widget"|"choice"|"none", ...},
  "save_to": {"volatile_values": [...], "recommended_value": ..., "labels": {...},
               "modes": ["device", "local"]},  # webcam: ["local"] (no onboard storage)
  "focus": {"supported"?: false, "mf_requires_manual_focus": bool, "indication_widget": ...},
  "preview_during_exposure": bool,
  "exposure_controls"?: false,  # webcam: the hardware auto-exposes, period
  "notes"?: [...],
}
```

## Extending: a new family

1. Subclass `CameraAdapter` (`adapters/base.py`) — or `GenericPtpAdapter`
   for a PTP body — and encode the family's measured behaviors in the
   receipt methods (`write_widget`, `fire_single`, `fire_burst`,
   `toggle_movie`, `run_action`, `classify_event`, `capabilities`).
2. If the family is not gphoto2-transported, implement a `CameraSession`
   (see `session.py` for the behavioral contract; `WebcamSession` is the
   reference) and a `Driver` (`drivers/`).
3. Register: model match in `adapters/select_adapter` and/or a driver in
   `discovery.resolve_drivers`.
4. Add the family to the conformance parametrization in
   `tests/test_session_protocol.py` and validate on real hardware before
   claiming support (ADR 0006/0008).

## Errors

Everything raises `CameraControlError` with user-actionable text (which
device, which cause, what to do). Transport absence is a normal state:
`status()["available"]` is False and connects refuse with install hints.
