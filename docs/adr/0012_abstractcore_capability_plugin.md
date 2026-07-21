# ADR 0012 — AbstractCore integration: one service, capability plugin + explicit tool set

Date: 2026-07-19 · Status: accepted

## Context

The operator directed abstractcamera to become an optional AbstractCore
capability plugin the way abstractvision/abstractvoice extend core: turn a
camera on/off, take photo/video, motion-detect-and-capture — plus a tool
set so an AI, agent, or entity can drive it, surfaced on the AbstractCore
server. AbstractCore is the target: camera adapts to core's existing
patterns, never the reverse.

Investigation facts (2026-07-19, verified against both reference plugins
and core's registry):

- Core discovers plugins via the `abstractcore.capabilities_plugins` entry
  point group; `register(registry)` receives a duck-typed registry — a
  plugin needs ZERO imports from abstractcore.
- Core's registry has typed helpers (voice/audio/vision/music/scene3d) and
  hardcoded facades, but the GENERIC `register_backend(capability=...)`
  path plus generic discovery routes (`available_providers`, `list_models`,
  `list_operations`) work for ANY capability string TODAY. A first-class
  `core.camera` facade, `CameraCapability` protocol, and `/v1/camera/*`
  server routes require core-side additions (asked at commons c3135; core
  owns that merge).
- Core's builtin tool inventory is deliberately core-only and global tool
  registration is deprecated: the durable tool contract is EXPLICIT tools
  passed to `generate(tools=...)` or registered on a host executor
  (AbstractRuntime toolsets are the later runtime/gateway lane).
- CameraManager triggers are asynchronous (worker fires; results surface
  as catch-log events). Request/response callers need a synchronous
  "capture and tell me where it landed" layer that does not exist.

## Decision

1. **One service, N surfaces.** `service.py` (`CameraService`) is the
   synchronous dict-in/dict-out operation layer over a `CameraHub`:
   list/open/close/status/preview_frame/capture_photo/capture_video/
   start_detection/stop_detection/get_events. Capture completion is
   resolved by watching the per-camera event log from a watermark taken
   BEFORE the trigger; waits are bounded and timeouts are honest errors.
   The capability plugin, the tool set, and any future HTTP route all
   delegate here — the safety/arbitration rules stay in CameraManager,
   the request/response adaptation exists exactly once.

2. **Process-shared service.** Cameras are process-wide hardware (a body
   claimed by two hubs wedges the transport), so both integration surfaces
   resolve `get_shared_service()`: an agent that opened a camera through a
   tool and a host calling `core.camera.capture_photo(...)` address the
   SAME session.

3. **Capability via the generic registry path.**
   `integrations/abstractcore_plugin.py::register(registry)` uses
   `registry.register_backend(capability="camera", backend_id=
   "abstractcamera:hub", ...)` — works on today's core unchanged — and
   automatically prefers a typed `register_camera_backend(...)` when core
   ships one. Capability methods RAISE `CameraControlError` on failure
   (core's facade/server convention); the service's success/error dicts
   are unwrapped at this boundary. Capture payloads follow the vision
   plugin's conventions: paths by default, bytes on `include_bytes=True`,
   `{"$artifact": ...}` refs when an `artifact_store` is provided.

4. **Tools are explicit, classified, and camera-owned.**
   `integrations/abstractcore_tools.py` ships ten `camera_*` tools as
   `@tool`-decorated functions plus `camera_tools()` /
   `camera_tool_definitions()` / `camera_tool_specs()` accessors — no
   global registration, no entry-point magic. The module declares the
   facts this package owns about its OWN tools
   (`CAMERA_TOOL_CLASSIFICATION`: mutating / remote_write_capable /
   `captures_environment`), exhaustively both ways (core's inventory
   rule). `captures_environment` is the camera-specific fact approval and
   privacy layers must key on: these tools photograph the physical world.

5. **The simulator is the CI hardware.** All integration tests run against
   core's REAL `CapabilityRegistry` / `ToolRegistry` with
   `ABSTRACTCAMERA_FAKE=1` (or an injected `FakeDriver`) — the entry
   point, registration, discovery routes, capture flows, and tool
   execution are exercised end-to-end with zero devices attached.

## Consequences

- abstractcamera keeps zero hard dependency on abstractcore; the entry
  point is inert until abstractcore is installed beside it.
- Until core lands the camera facade, Python callers use
  `core.capabilities.available_providers("camera")` (generic routes) or
  the plugin/tool surfaces directly; `status()` does not list "camera"
  (core-side, asked).
- Server `/v1/camera/*` routes are core-side work (extension endpoints —
  OpenAI has no camera API; `/v1/audio/music` is the precedent).
- Runtime/gateway toolset registration (approval tiers, entity grants) is
  a follow-up lane and consumes `camera_tool_definitions()` +
  `CAMERA_TOOL_CLASSIFICATION` as its source of truth.

## Core rulings folded (commons c3168, 2026-07-19)

- Capability name "camera" + the op set APPROVED. Contract hardened: every
  capability op returns JSON-SAFE dicts (results may land in runtime
  ledgers) — `include_bytes=True` therefore attaches base64 (`data_b64`),
  never raw bytes; capture bytes ride `artifact_store` when provided.
  `detection_events` stays non-blocking poll-shaped (a blocking wait in a
  capability op would freeze generate-path callers). The protocol stays
  minimal-required (backend_id + ops; discovery optional-by-duck-typing).
- Core-side surface: camera drafts the patch IN CORE'S TREE (separate
  files: `server/camera_endpoints.py`; additive marked blocks in types.py/
  registry.py), core owner-reviews and merges. Routes follow the
  `/v1/audio/music` precedent exactly — 501 with install_hint when the
  plugin is absent; photo response OpenAI-images-shaped
  `{created, data: [{b64_json}]}`.
- Tools stay EXPLICIT-IMPORT, deliberately no entry-point group (core's
  security rationale, verbatim intent): tools are a security surface —
  entry-point auto-registration would let `pip install anything` silently
  widen every agent's tool surface, breaking grant semantics (grants are
  capability-level by name; containment binds at composition — a host must
  consciously compose which ToolDefinitions it registers). A future group,
  if ever, would serve discovery-not-registration and needs its own
  security review.
- The new classification tag string `captures_environment` requires a
  semantics pass before engraving into shipped definitions (asked commons
  c3172; `mutating`/`remote_write_capable` are the existing inventory
  vocabulary and ride unchanged). PASSED same-day (c3176) with one
  amendment adopted verbatim: the definition is SENSOR-GENERAL (any
  ambient sensor — camera today, a microphone tool tomorrow; screen
  capture deliberately out), so future audio tools extend this one privacy
  axis instead of minting siblings. General shape for domain tags recorded
  as decision:domain-tool-classification-tags.

## Operator ruling folded (commons c3938, 2026-07-21): defaults, not a floor

- `camera_tool_approval_defaults()` ships ask-by-default for every tool
  with any true fact, and that is a DEFAULT, not a floor: the operator
  ruled "a user must be able to auto accept camera or ask the agent to
  request permissions, like for any other tool." Host policies (e.g.
  AbstractRuntime's run-scoped tool_policy) may auto-accept
  `captures_environment` tools on the user's explicit say-so. What the
  derivation guarantees is narrower and permanent: capture never
  auto-approves WITHOUT a user's choice, and a drifted classification
  entry fails closed to require-approval. The never-auto contingency
  (a host-side hard floor) is dead.

## Adversarial folds (2 subagent passes, 2026-07-19 — 1 P0 / 8 P1 / 15 P2)

Two independent adversaries (code/logic; consumer-contract) attacked the
integration. Every accepted finding is folded and test-pinned:

- **P0 — capture-mode writes bypassed the capture lock**: the trigger is a
  TOGGLE in video mode, so an unguarded `set_capture_mode` during a bounded
  recording turned its stop toggle into a still trigger and STRANDED the
  recording with no stop surface. Now: `start_detection` takes the
  per-camera capture lock; `capture_photo`/`start_detection` refuse while
  `movie_recording`; `capture_video` refuses while auto-fire is armed; and
  `stop_recording()` exists as the escape hatch (service op, capability op,
  and the tenth tool).
- **Deferred-download honesty**: armed auto-fire defers downloads by
  design, so `capture_photo` under it ALWAYS timed out; it now returns an
  honest deferred success. A flushing backlog could be mis-attributed as a
  new capture's result (photo events carry no trigger correlation); capture
  now refuses while `downloads_pending > 0` outside armed mode.
- **Wait-loop discipline**: movie-state waits filter error events by
  capture-shaped reason (a config-honesty revert was reported as the
  recording's failure while it recorded on) and re-check actual state
  before failing; timeouts carry a `timed_out` sentinel (a "Timed out"
  SUBSTRING match had converted a real download failure into success);
  waits fail fast when the camera disconnects mid-capture.
- **No-raise contract**: all numeric inputs coerce via
  `service_support.coerce_number/coerce_int` and fail as dicts BEFORE any
  hardware acts (a bad `timeout_s` used to raise bare ValueError AFTER the
  recording ran).
- **Import weight**: `import abstractcamera` and the plugin module are now
  LAZY (PEP 562) — core processes listing capabilities no longer pay the
  OpenCV import; the camera stack loads on first use.
- **Consumer truths**: `available_providers()` carries full provider
  records through core's normalizer (bare strings made uninstalled
  transports read "available"); ABSTRACTCAMERA_CAPTURE_ROOT is honored at
  the shared-service choke point (the tools path ignored it); default
  `open()` is idempotent (a retry double-claimed the device); tool
  descriptions teach the two id spaces on the wire itself (docstring Args
  never reach the model; core caps descriptions at 200 chars);
  `camera_tool_specs()`/`camera_tool_definitions()` return isolated copies
  (a host mutation corrupted the shared schema); artifact-store failures
  translate to `CameraControlError`; `include_bytes` is capped (64MB) with
  the artifact store named for larger payloads; tests capture into
  tmpdirs, never the operator's real `~/Pictures`.

Deliberately NOT changed: `preview_frame` returning raw JPEG bytes as the
RETURN VALUE stays the documented exception to the JSON-safe-dict rule
(vision-plugin convention; an artifact ref when a store is passed); the
hub's process-shared capture-root semantics (LAST configurer wins for new
connections) stay and are documented in the config_hint.
