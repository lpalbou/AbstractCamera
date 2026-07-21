# Changelog

## Unreleased

- **Adversarial pass on the whole wave (operator-mandated, one subagent —
  2 P1 / 8 P2, all folded + test-pinned; ADR 0013 § Adversarial folds).**
  The P1 theme: correct correlation/epoch PRODUCERS with two CONSUMERS
  still reading the old world — the capture wait's error branch ignored
  trigger stamps (a backlog fetch failure mid-wait was reported as the
  fresh capture's failure), and direct-manager reconnects re-minted the
  session epoch while keeping old events/counters (cursor-reset consumers
  re-read history as new; the bridge would re-emit it). Both fixed
  consumer-side. P2 folds: sequence frames are numbered trigger acts;
  bridge fatal-auth honesty; per-mailbox cursor files; CLI input hygiene;
  fail-fast preview on dead cameras; `session` in public status; honest
  hub connect docstring.
- **Camera → gateway event bridge: the sentinel lane (backlog 0016).**
  New `abstractcamera watch` verb + `gateway_bridge.py`: a standalone
  process opens a camera, arms detection, and forwards catch-log events as
  DURABLE AbstractGateway events (`POST /api/gateway/commands`,
  type=emit_event, global-scope mailbox) — parked workflows/entities
  declaring the mailbox WAKE on movement instead of polling
  `camera_get_events` with model turns. Delivery is at-least-once toward
  the gateway and exactly-once past its command store (command_id derived
  from mailbox+camera+session+event id — crash replays dedupe as
  `duplicate: true`, live-verified against a running gateway); cursors
  persist per (camera, session epoch) and reset honestly when the camera
  reconnects; stdlib-only HTTP.
- **Event-log wire contract (backlog 0015).** The event log is an API for
  LLM/workflow consumers now, so the contract is explicit: `get_events`
  responses carry `session` (the id-space epoch — a new value means the
  camera reconnected and cursors reset) and `evicted`/`first_retained_id`
  (the bounded log dropped events past your cursor — previously
  indistinguishable from "quiet"). File events carry `trigger_id`
  correlating them to their trigger act (announce-time stamping), and
  capture waits skip stale-stamped backlog files — closing the
  misattribution window where a deferred download flushing mid-wait could
  be claimed as the fresh capture's result. Detection events now carry the
  detector's structured `metrics` (bbox/centroid/speed/duration) instead
  of prose-only notes. All seven wire kinds are documented (`detection`,
  `trigger`, `photo`, `photo-pending`, `clip`, `camera-event`, `error`).
- **`camera_preview_photo`: look without shooting (backlog 0017).** The
  eleventh tool (+ `CameraService.preview_photo`) saves the current
  live-view frame as a JPEG and returns its path — no shutter actuation,
  no capture event, nothing on the camera's card. The default answer to
  "what do you see?"; classified `captures_environment` (ask-by-default)
  like every recording tool, but `remote_write_capable: False` (frame
  pulls are reads).
- **P1 lifecycle fixes (adversarial pass 2026-07-21, all empirically
  reproduced pre-fix).** (a) Concurrent `open()` double-claimed one
  physical device (check-then-act race in both the service guard and
  `hub.connect`) — both layers now serialize their whole
  check→create→connect→register window, so a retry-after-slow-open JOINS
  the in-flight open instead of racing it (real PTP transports wedge on a
  double claim). (b) An unplug (liveness-watchdog death) left a dead
  manager squatting on its uid forever: every re-open minted a suffixed
  uid (`nikon_z_6ii_2`, `_3`, …), splitting the capture folder and
  invalidating stored agent uids, while corpses accumulated frames — the
  next connect now reaps dead managers (uid + capture folder restored),
  and the worker clears frame/ring state on every exit path. (c)
  `get_shared_service()` had no exit hook (the legacy singleton did):
  a routine host restart could leave a camera claimed — or RECORDING —
  with deferred downloads stranded; an atexit now runs `close_all()`
  (bounded worker joins, downloads flushed per disconnect's contract).
- **P0 fixed in core's tree (owner-accepted, commons c3987).** All eleven
  `/v1/camera/*` handlers in abstractcore were `async def` calling
  blocking capability ops — one 600s video capture serialized the whole
  server behind it (the head-of-line wedge class core's audio endpoints
  document). Converted to sync-def (FastAPI threadpool dispatch, the
  audio_speech precedent) with a router-iterating pin test; core applied
  the same structural pin to its audio lane the same hour.
- **Camera skill draft.** `skills/camera-piloting/SKILL.md` teaches agents
  the two id spaces, look-vs-shoot etiquette, capture/detection
  choreography with the new cursor rules, cleanup honesty, and the
  sentinel pattern — handed to the skill seat for library adoption.
- **Approval-defaults helper for host policies (backlog 0012).**
  `camera_tool_approval_defaults()` derives auto-approve/require-approval
  name sets from `CAMERA_TOOL_CLASSIFICATION` (never hand-listed): a tool
  auto-approves only when it neither mutates local state, nor reaches
  remote devices, nor records the physical surroundings — the consumption
  surface for AbstractRuntime's `ToolApprovalPolicy`. These are DEFAULTS,
  not a floor (operator ruling, commons c3938): a user may auto-accept
  camera tools through the host's policy like any other tool; the
  derivation just never auto-approves capture without that explicit user
  choice. FAILS CLOSED (adversarial pass, operator-mandated 0012 gate): an
  entry missing a fact key or carrying an extra one goes to
  require_approval — the fail-closed default lives in the code, not the
  exhaustiveness test, so a drifted classification can only ever be
  stricter. Documented consumer caveat: auto-approval means the tool does
  not itself capture/mutate/reach-remote, NOT zero imagery egress —
  `camera_get_events`/`camera_status` return capture file paths, so
  unattended hosts should pair the toolset with a non-auto file-read policy
  (a dedicated capture-reference privacy tag was RULED against at the
  semantics desk: references are host-policy composition, not a tool fact).
- **AbstractCore capability plugin + AI tool set (ADR 0012).** abstractcamera
  is now an optional AbstractCore capability plugin, like abstractvision and
  abstractvoice: the `abstractcore.capabilities_plugins` entry point
  registers the `camera` capability (`backend_id="abstractcamera:hub"`)
  through core's generic registry path — turn cameras on/off, take photos
  and bounded video clips, arm motion/lightning/meteor detection with
  auto-capture, read the event log, grab preview frames; capture payloads
  ride file paths by default, bytes on request, `{"$artifact": ...}` refs
  when an artifact store is provided. New `service.py` (`CameraService`) is
  the synchronous dict-shaped operation layer both integration surfaces
  share (event-watermark capture waits, bounded timeouts, honest errors).
  New `integrations/abstractcore_tools.py` ships ten explicit `camera_*`
  tools for `generate(tools=camera_tools())` with a camera-owned
  classification map (`mutating` / `remote_write_capable` /
  `captures_environment` — the fact privacy/approval layers key on).
  Integration tests run against core's REAL CapabilityRegistry/ToolRegistry
  on the built-in simulator (no hardware in CI). Core-side facade +
  `/v1/camera/*` server routes are asked/tracked at commons c3135.
  TWO ADVERSARIAL SUBAGENT PASSES folded (1 P0 / 8 P1 / 15 P2, all
  accepted findings fixed + test-pinned; ADR 0012 § Adversarial folds):
  the P0 was capture-mode writes bypassing the per-camera capture lock —
  a concurrent mode flip turned a bounded recording's stop toggle into a
  still trigger and stranded the recording with no stop surface; now every
  mode-writing op holds the lock, recording-state guards refuse
  conflicting captures, and `stop_recording` (service/capability/tool)
  is the escape hatch. Also folded: honest DEFERRED capture results under
  armed auto-fire (blocking always timed out), stale-download attribution
  guard, movie-wait error-reason filtering, `timed_out` sentinel (never
  substring matching), disconnect fail-fast, no-raise numeric coercion
  before hardware acts, PEP 562 lazy package imports (core processes no
  longer pay OpenCV for listing capabilities), full provider records
  through core's normalizer, `ABSTRACTCAMERA_CAPTURE_ROOT` honored on the
  tools path, idempotent default `open()`, wire-visible id-space teaching
  in tool descriptions, isolated spec copies, artifact-store error
  translation, 64MB inline-content cap, and tmpdir capture roots in tests.
- **`abstractcamera download` — download ALL device media, across devices
  (ADR 0011).** One sync engine (`media_sync.sync_store`) owns every
  safety rule — incremental size-verified copies (device mtimes are
  untrustworthy: the DWARF's clock has produced year-2038 stamps),
  destination space checks before the first byte, deletion ONLY of files
  whose local copy verifies AT DELETE TIME, `protected` device state
  (the DWARF's `Astronomy/CALI_FRAME` dark library) copied but never
  deleted, device system files never even listed, `--dry-run` walking the
  identical decision path — over per-device `MediaStore` adapters
  (`media_store.py`): `FilesystemMediaStore` (any USB-mounted card,
  declarative `CardLayout`, detected by album SIGNATURE never volume
  label) and `DwarfAlbumMediaStore` (the Wi-Fi album: REST index,
  streamed downloads, `/album/delete`). PTP-card stores (Sony/Nikon over
  libgphoto2) are the named next adapters — the engine is ready
  unchanged.   CLI: `abstractcamera download [--source PATH | --host IP]
  [--dest PATH] [--delete] [--delete-calibrations] [--dry-run]`
  (`--delete-calibrations` extends `--delete` to the calibration library,
  still under the verified-copy rule); library surface:
  `sync_store(store, ..., delete_protected=)`. Copy+delete paths
  validated against a real DWARF 3 card (5430 files, 34.8GB; card freed
  to 150MB with the calibration library preserved and locally verified).
  `abstractcamera list` now also shows mounted media sources, and the
  download summary states the everything-already-downloaded outcome
  explicitly.   HARDWARE-IDENTITY GUARDRAIL (live incident 2026-07-16: an
  operator's external drive with `astronomy/`+`videos/` folders matched
  the content signature on macOS's case-insensitive filesystem and was
  offered as a `--delete` target): detection is now HARDWARE-IDENTITY-ONLY
  — folder contents are never consulted; a volume is a camera only when
  its `diskutil` identity matches the device (DWARF: `File-Stor Gadget`
  on removable-USB backing, measured). Deletion is refused on any other
  volume (dry runs and explicit `--source` included; unknown identity
  fails safe); an empty freshly-formatted camera card is still detected;
  copy-only imports from non-device volumes proceed with a loud note; the
  deletion prompt surfaces the volume UUID.
- **Path-containment hardening (adversarial review 2026-07-16).** After
  the identity gate the engine still moved bytes by device/host-supplied
  strings; a fable5 adversary found an album `..`-path arbitrary-overwrite
  (P0) and a symlinked-media-root escape on copy/delete (P1). Fixed at one
  choke point: `sanitize_relpath` strips `..`/absolute components,
  `contained_path` refuses any target escaping `dest`, `FilesystemMediaStore`
  skips symlinked roots/subdirs/files and re-proves every delete/prune
  target inside the volume's realpath (the adapter is safe by construction),
  colliding local relpaths are never deleted (no silent loss of the
  camera's own data), `dest`-inside-source is refused, and unknown-size
  entries are budgeted against the free-space floor. New regression suite
  `tests/test_media_security.py` pins every finding closed.

- **DWARF smart telescopes (new `dwarf` family, ADR 0010).** A DWARF 3 is
  piloted over Wi-Fi through the existing abstraction: RTSP live view,
  exposure/gain dials carrying the device's OWN gear tables, IR-cut filter
  positions, stills/burst/movie landing in the device album (microSD) and
  downloading over HTTP, battery/temperature telemetry. The MOUNT is
  exposed as family actions on the one-shot action channel (never cached,
  never replayed): `gotoradec` (RA/Dec degrees, J2000), `gotosolar`,
  `stopgoto`, `calibrate`, `joystick`/`joystickstop`; the canonical focus
  actions map to the astro autofocus and single-step focus. GOTO/
  calibration/tracking progress arrives in the catch log as the device's
  own state notifications. Master-lock honesty: connect() refuses with
  actionable text when the DWARFLAB app holds control. Protocol implemented
  from DwarfLab's published API v2 spec (vendored minimal proto3 codec —
  the GPL community bridges are not linked); `websocket-client` is the one
  new dependency behind the `dwarf` extra. Discovery is configured, never
  scanned (`ABSTRACTCAMERA_DWARF_HOSTS`); `scripts/validate_dwarf.py` is
  the active-discovery + hardware validation tool (mount motion opt-in).
- **Adapters can extend the action vocabulary** —
  `CameraAdapter.family_action_names()` (default empty) adds family
  actions to `request_action`/`status()["actions"]`, and
  `poll_session_events()` (default no-op) lets spontaneous-speaking
  devices (telescope state notifications) surface events between preview
  frames. Catch-log action events now carry `reason: "action"` (was
  `"focus"` — the channel outgrew focus drives).
- **`CameraHub.annotate_entries(entries)`** — the live-state annotation of
  discovery entries (connected / device_uid / active) split out of
  `list_cameras()`, so callers that cache the expensive USB probe (gphoto2
  autodetect: 0.35-0.73s) can still serve FRESH connection state on every
  request. `list_cameras()` behavior is unchanged (probe + annotate).
- **PTP NULL-value segfault fixed (`ptp_safe`).** python-gphoto2's
  `CameraWidget.get_value()` runs `PyUnicode_FromString(NULL)` when a body
  hands back a NULL string value — an uncatchable SIGSEGV (observed
  2026-07-12: a packaged-app crash connecting a Sony A7R IV; bodies return
  NULL transiently mid-wake). Every string widget read from real hardware
  now goes through a ctypes reader that NULL-checks the C pointer BEFORE
  any Python string is built (`gp_widget_get_value`/`gp_widget_get_choice`
  straight from the loaded libgphoto2); NULL surfaces as an absent value,
  never a crash. Wired through the config-cache walk, write-verify
  read-backs, serial reads, and movie-prohibit reads. Simulator and test
  widgets keep their normal path.
- **Webcam zoom dial** — the ONE manual control macOS grants
  (`videoZoomFactor`, a digital crop; readback-confirmed writes through
  the ledger, ladder within the device-reported range, measured 1-16x on
  both machines). Manual exposure/ISO/shutter/WB/focus remain ABSENT
  because the AVFoundation APIs for them are iOS-only — measured
  unsupported on this hardware for both the built-in camera and a
  Continuity iPhone; the capability notes now say so explicitly and point
  at macOS's own Video Effects toggles (Center Stage/Portrait/Studio
  Light) for iPhone framing/depth effects.
- **Webcam identity fixed at the root (ADR 0009).** The positional
  ffmpeg↔OpenCV name/index mapping INVERTED on real hardware (2026-07-12:
  "MacBook Pro Camera" streamed the iPhone — Continuity cameras reorder
  the device set dynamically). Elected via a 2-design adversarial review:
  webcam ids are now `webcam:<AVCaptureDevice uniqueID>` and capture is
  NATIVE AVFoundation opened by that uniqueID — the enumerated object IS
  the capture target, no index space exists to invert. Names are
  `reported` (same object), kinds are structured-first
  (isContinuityCamera/deviceType/modelID before name heuristics),
  resolutions are device-reported formats (activeFormat switching, no more
  probe-by-trial), TCC denial is diagnosed deterministically before open,
  and `read_serial()` returns the uniqueID (stable hub disambiguation).
  ffmpeg enumeration is deleted. Old positional ids refuse loudly.
  Residual failures all fail CLOSED (refusal/disconnect), never a wrong
  stream. Validated 11/11 on the previously-inverted machine, including a
  cross-wiring oracle (per-label resolution commands followed by the
  correct streams). New macOS deps: pyobjc-framework-AVFoundation/Quartz/
  libdispatch. Test seam: FakeFrameSource (pure numpy, frame-paced).
- **CameraHub — pilot several cameras at once.** One manager/worker per
  connected camera (libgphoto2's per-camera thread-safety model), an ACTIVE
  selection for single-panel hosts, connect-by-id reuse, shared manager
  configuration (capture root, frame analyzer), and annotated discovery
  (`connected` / `device_uid` / `active`). Hardware-validated with FOUR
  simultaneous cameras (Nikon Z6 II + Sony A7R IV + MacBook camera + iPhone
  Continuity): concurrent live views, a named Nikon timelapse during Sony
  stills and a webcam movie, per-body config isolation.
- **Device identity + capture layout.** Every camera gets a filesystem-safe
  device slug (model/label snake_case + serial disambiguation for identical
  bodies); captures land in `<capture_root>/<device_slug>/` (default root:
  `~/Pictures`). `set_sequence_name()` nests everything one level deeper
  (`.../<sequence_name>/`); `start_interval_sequence(sequence_name=...)`
  names a timelapse in one call. `set_capture_dir()` keeps its legacy
  explicit-directory meaning.
- **Save policy.** `set_save_policy(download_locally=False)` leaves captures
  on the camera's own storage (announced honestly in the event feed, never
  fetched); families without onboard storage refuse device-only. A loud
  warning fires when device-only meets a volatile capture target (camera
  RAM) — those shots would exist nowhere.
- **Nikon Z hardware re-validation through the package** (first real-body
  run since the extraction): connect-by-id among two PTP bodies, ledger
  writes, single/burst/named-sequence captures — 18/18. New honesty path
  discovered on hardware: an unformatted card fails EVERY capture with a
  bare `[-1]` — the adapter now warns at connect (`connect_warnings`) and
  names the cause on failed triggers (`diagnose_trigger_failure`).
- **Sony trigger-drop honesty (hardware truth 2026-07-12):** the A7R IV
  intermittently accepts a trigger and never fires even in Manual focus
  (busy applying settings/writing card). The no-file expectation watch now
  arms on EVERY single fire with mode-specific copy, not just in AF modes.
- Webcam discovery: every entry now carries a structured `kind`
  (`built_in` | `continuity` | `external`) so hosts can tell the machine's
  own camera from a nearby iPhone/iPad that macOS exposes wirelessly via
  Continuity Camera. Continuity devices sort last, carry an explicit
  wireless note, and are never the connect default (informed choice only).
  Validated live: the iPhone connects as a normal webcam-family camera
  (1080p frames over Wi-Fi).
- Hardware-validation scripts write their captures to temp directories
  instead of the repository tree.

## 0.1.0 - 2026-07-12

Initial release: extraction of BlackPixel's hardware-validated camera stack
into a standalone AbstractFramework package, elected through a 3-agent
adversarial design review (session-protocol design with 12 adjudicated
modifications; see `docs/adr/0001`).

- `CameraManager` (parallel to AbstractVision's `VisionManager`): thread-safe
  orchestration of live view, config dials with a write-verification honesty
  ledger, single/burst/movie capture, focus actions, an absolute-deadline
  intervalometer with per-sequence JSONL manifests, live-view detection
  (lightning/meteor/motion) with auto-fire arbitration, rolling pre-capture
  clips, deferred/immediate capture downloads, and a liveness watchdog.
- Session protocol (`wire.py`, `session.py`): constants numerically pinned to
  libgphoto2; behavioral contract executable in tests (timeout semantics,
  raise-on-unservable preview, announce→fetch ordering).
- Family adapters: Nikon Z (hardware-validated on a Z6 II, 2026-07-07/08),
  Sony Alpha (hardware-validated on an A7R IV, 2026-07-12: async write
  settling with verify-retry, busy backoff + paced requeue, prioritymode
  gating, press-and-hold burst, silent-AF-refusal watch, fetch-on-announce
  against sdram slot eviction, unconfirmable-movie honesty), generic PTP
  fallback, and the new webcam family (validated on a MacBook Pro camera:
  resolution dial with SOF-probe confirmation, in-process confirmable MP4
  recording, honest absence of exposure/focus controls).
- Transport drivers + non-invasive multi-camera discovery (gphoto2 with
  port binding for multi-body setups, AVFoundation webcams with best-effort
  ffmpeg-based naming and explicit Continuity labeling, simulator).
- Simulator: gphoto2-module-shaped, with scriptable Nikon Z6 II and Sony
  A7R IV personalities (`ABSTRACTCAMERA_FAKE=1`).
- Test suite: 134 tests (ported hardware-regression suites with unweakened
  assertions incl. the golden write-sequence pin, session conformance,
  discovery, webcam family) plus hardware validation scripts for the Sony
  (22 + 11 checks) and the webcam (21 checks); a transcript-equivalence
  gate proved the extraction behavior-identical to the pre-move host code.
