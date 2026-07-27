# API reference

## AbstractCore integration (ADR 0012)

Installing abstractcamera beside abstractcore registers the `camera`
capability automatically (entry point group
`abstractcore.capabilities_plugins`, backend id `abstractcamera:hub`).

| Surface | Contract |
| --- | --- |
| `abstractcamera.service.CameraService` | Synchronous dict-in/dict-out ops over a `CameraHub`: `list_cameras/open/close/close_all/status/preview_frame/preview_photo/capture_photo/capture_video/stop_recording/start_detection/stop_detection/get_events`. Every result carries `success`; failures carry actionable `error` text and NEVER raise on bad input. Capture waits watch the event log from a pre-trigger watermark with bounded timeouts (`timed_out: true` sentinel) and skip stale-stamped backlog files (`trigger_id` correlation); everything that writes capture mode or triggers holds a per-camera capture lock (concurrent captures refuse honestly; `stop_recording` is the escape hatch for recordings started by detection auto-fire). Under armed auto-fire, `capture_photo` returns an honest DEFERRED success (downloads land at disarm). `open()` is idempotent AND serialized (concurrent opens join the in-flight claim instead of double-claiming the device); re-opens after an unplug reap the dead session and reuse its uid. `preview_photo` saves the current live-view frame (no shutter). `get_events` responses carry the wire contract: `session` (id-space epoch; new value = reconnect, reset cursors), `evicted`/`first_retained_id` (bounded-log gap signal), per-event `trigger_id` + detection `metrics`. `get_shared_service()` is the process-wide instance both integration surfaces use; it honors `ABSTRACTCAMERA_CAPTURE_ROOT` and registers an atexit that releases cameras (flushing downloads) on clean process exit. |
| `integrations.abstractcore_plugin` | The capability plugin (`register(registry)`); import-light — the camera stack (OpenCV) loads on first USE, never at plugin/registry load. Capability methods raise `CameraControlError` on failure (core convention) and return JSON-safe dicts (core ruling c3168). `capture_photo`/`capture_video`/`stop_recording` return paths by default, add base64 content (`data_b64`, capped 64MB — use the artifact store beyond) with `include_bytes=True`, and store `{"$artifact": ...}` refs when `artifact_store=` is provided; `preview_frame` returns JPEG bytes as the return value (the one documented exception to the dict rule) or an artifact ref. Catalog routes: `available_providers()` (full transport records, derived from live driver resolution), `list_models()` (devices), `list_operations()`. `register()` ALSO contributes the tool set + its approval partition through core (operator layering ruling dm#16-20: ONLY abstractcore imports abstractcamera — runtime/gateway consume camera tools through `abstractcore.capabilities.capability_tools("camera")` / `capability_tool_policy("camera")`, never by importing this package; duck-typed, so older cores without the surface still get the backend). Note: the camera hub is process-shared — the LAST configured `camera_capture_root` wins for newly opened cameras across every consumer in the process. |
| `integrations.abstractcore_tools` | Eleven explicit `camera_*` tools for LLM tool calling: `camera_list_devices`, `camera_open`, `camera_close`, `camera_status`, `camera_preview_photo` (look without shooting — live-view frame, no shutter), `camera_capture_photo`, `camera_capture_video`, `camera_stop_recording`, `camera_start_detection`, `camera_stop_detection`, `camera_get_events`. Sight lane (operator-ruled): results that land a local file carry handler-authored `media` (bare paths here; `{"$artifact": id}` refs on the capability lane with a store) — absent when no file landed; the consumer contract is LIVE end to end: abstractagent's adapter folds `media` into the next model call (live-proven — a flow-authored agent captured a real JPEG and the model described the actual room; core's `analyze_media` is the re-look path afterwards). Accessors: `camera_tools()` (callables for `generate(tools=...)`), `camera_tool_definitions()` (ToolDefinitions), `camera_tool_specs()` (flat dicts). `CAMERA_TOOL_CLASSIFICATION` declares `mutating`/`remote_write_capable`/`captures_environment` per tool, exhaustively. `camera_tool_approval_defaults()` derives host approval defaults from the classification (auto-approve only when every fact is false — today `camera_list_devices`/`camera_status`/`camera_get_events`; every `captures_environment` tool defaults to require-approval, user-overridable through host policy per the operator ruling — a default, not a floor): the consumption surface for AbstractRuntime's `ToolApprovalPolicy` (backlog 0012). |
| Detection → event API (wake-on-motion) | Detection runs in-process (the `CameraManager` worker thread); results land in the cursor-contracted event log readable via `camera_get_events` (tool), `detection_events` (capability op), and `/v1/camera/events` (server). To WAKE a durable run on motion, a consumer AT A FRAMEWORK ENTRY (a gateway-hosted run or a flow that holds a camera open through the capability) watches that log and emits the wake event via the gateway's OWN `emit_event`; a flow `wait_event`/`on_event` node then resumes. abstractcamera provides the capability + the event API and holds ZERO gateway-API knowledge — the `abstractcamera watch` daemon was removed (ADR 0013 § Amendment, operator ruling dm#14: a dependency of abstractcore must never reach up to the gateway). |

Detection actions: `action="photo"` auto-fires a still per detection
(cooldown-gated); `action="video"` starts recording on the first detection
and stops it on a later one; `action="monitor"` only logs. Targets:
`motion`, `lightning`, `meteor`. `camera_get_events(since_id=...)` is the
polling surface (`event_watermark` from `camera_start_detection` is the
starting cursor).

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
    sync_store,              # download ALL device media (ADR 0011); SyncReport out
    FilesystemMediaStore,    # media store: USB-mounted card (CardLayout-driven)
    DwarfAlbumMediaStore,    # media store: the DWARF album over Wi-Fi
    find_card_volumes,       # mounted cards by album signature (never volume label)
    MediaEntry, SyncReport,
    ACTION_WIDGET_NAMES, CONFIG_WIDGET_NAMES,
)
```

## Device media downloads (`abstractcamera download`, ADR 0011)

| Surface | Contract |
| --- | --- |
| `sync_store(store, dest=None, *, delete=False, delete_protected=False, dry_run=False, log=print)` | Downloads every media file `store` lists into `dest` (default `~/Pictures/<store.device_slug>/`), size-verified and incremental — `protected` entries (the device calibration library) are always DOWNLOADED. With `delete`, removes device copies that verify locally AT DELETE TIME; protected entries survive unless `delete_protected` (CLI: `--delete-calibrations`) opts in; unverifiable entries never delete. Returns a `SyncReport` (copied/skipped/deleted/deleted_protected/protected/failures). |
| `FilesystemMediaStore(root, layout=DWARF_CARD_LAYOUT)` | Any mounted device card. `CardLayout` declares the album dirs, protected subtrees, and the local slug — adding a device's card is a declaration, not code. |
| `DwarfAlbumMediaStore(host)` | The DWARF album over Wi-Fi: REST index, streamed downloads, `/album/delete`. |
| `find_card_volumes()` | `(mount_point, layout)` for volumes matching a known card signature under `/Volumes`. |

A MediaStore adapter is ~6 methods (`list_media`/`fetch`/`delete`/
`finalize_delete`/`describe`/`validate` + `device_slug`, `can_delete`) —
the sync engine owns all safety rules, so new devices (PTP cards over
libgphoto2 are next) inherit them unchanged.

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
| `request_action(name, value=None)` | One-shot actions; never cached, never replayed. Canonical focus actions (`autofocusdrive`, `manualfocusdrive`) plus the family's own (`status()["actions"]` — the DWARF family adds mount actions: `gotoradec` "ra_deg,dec_deg[,label]", `gotosolar` "moon"/"jupiter"/..., `stopgoto`, `calibrate`, `joystick` "angle_deg,length,speed", `joystickstop`). |
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
  "family": "sony_alpha" | "nikon_z" | "webcam" | "dwarf" | "generic",
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
  "mount"?: {"kind": "alt-az", "goto": [...], "joystick": bool,   # smart telescopes
             "calibration": bool, "tracking": str},
  "actions"?: [...],            # family actions beyond the focus drives
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
