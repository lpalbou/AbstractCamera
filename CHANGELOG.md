# Changelog

## Unreleased

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
