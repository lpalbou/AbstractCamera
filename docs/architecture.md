# Architecture

## The three layers

```
CameraManager (camera_manager.py + mixins)     family-agnostic orchestration
  └── CameraAdapter (adapters/)                family quirks, one file each
        └── CameraSession (session.py/wire.py) transport: gphoto2 / webcam / sim
```

- **CameraManager** owns the worker thread (ONE thread owns every camera
  call — the C library is not thread-safe per camera), the scheduling
  windows (exposure-aware drain budgets where the interval deadline always
  wins), the pending-write honesty ledger, deferred vs fetch-on-announce
  download policy, detection dispatch and auto-fire arbitration, the rolling
  ring, the liveness watchdog, and the catch log. Hosts use only its
  thread-safe public API. The manager class is composed from focused mixin
  modules (`worker`, `config_ledger`, `capture_ops`, `downloads`,
  `detection_runner`, `clips`) that share state defined in one `__init__`.
- **CameraAdapter** (per family) owns everything a family does differently:
  connect-time defaults, the write policy (Sony: write→pump→verify→retry),
  trigger semantics as data (`CaptureTiming`: window sizes, preview-pause,
  silent-refusal watches), burst mechanics (count drive vs press-and-hold),
  movie policy (`MovieReceipt`: prohibit pre-checks, confirmability),
  focus-action choreography, event classification (noise filtering), and
  the `capabilities` descriptor host UIs adapt to.
- **CameraSession** is the transport: the protocol the manager loop speaks
  (`init/exit/get_abilities/capture_preview/trigger_capture/wait_for_event/
  file_get` + optional single-config widget I/O). Three implementations:
  the real `gphoto2.Camera` (structural typing, zero wrapping), the
  simulator, and `WebcamSession`. Constants are numerically pinned to
  libgphoto2 (`wire.py`) — that is what makes the transports
  interchangeable without translation.

Drivers (`drivers/`) create sessions and own transport-specific setup:
`Gphoto2Driver` (autodetect, port binding for multi-body targeting, macOS
PTP-daemon release), `WebcamDriver` (non-invasive AVFoundation enumeration
with ffmpeg-based naming), `FakeDriver` (the test seam). `discovery.py`
resolves drivers per connect (fake env → simulator only) and aggregates
`list_cameras()`.

## Threading contract

- Worker thread: every session and camera-touching adapter call.
- Any thread: the manager's public API (state lock + command flags), pure
  adapter policy methods.
- Session-private helper threads (the webcam movie encoder) never touch the
  capture handle; frames reach them through bounded queues.
- Adapters that pump events internally forward EVERY event to the sink the
  manager attached — a swallowed FILE_ADDED would lose a shot announcement.

## Multi-camera (CameraHub, ADR 0008)

`CameraManager` is strictly single-camera; piloting several cameras at once
is a `CameraHub` of managers — one worker thread per camera, which is
libgphoto2's thread-safety model (safe across different cameras, unsafe
within one). The hub owns device identity (model slug + serial
disambiguation → the uid that names `~/Pictures/<device>/` capture folders,
HTTP addressing, and panel selection), an ACTIVE selection for single-panel
hosts, and survivor fallback on disconnect. Hardware-validated with four
simultaneous cameras (Nikon Z6 II + Sony A7R IV + built-in + Continuity
iPhone): concurrent live views at 15-40 fps each while a named timelapse,
stills, and a movie recording ran on different bodies.

## Hardware-scarred invariants (do not "clean up")

- Widget I/O uses libgphoto2's single-config API: a full config-tree walk is
  ~3.7s vs ~8ms per widget on a Nikon Z6 II, and one full walk SEGFAULTED a
  real Sony A7R IV — the Sony adapter refuses to run without single-config.
- Windows never shorten: `max()` folding of drain/pause windows (a long
  exposure's window must survive later shots).
- Nikon Z: `trigger_capture` blocks through the exposure; windows anchor at
  command ISSUE. Sony: it blocks ~1.2s regardless; live view survives
  exposures; AF-gated triggers can be silently refused (expectation watch).
- Sony keeps ~2 unfetched capture objects (sdram slots): fetch-on-announce
  or lose the middle of every burst.
- Deferred downloads while Auto-Fire is armed (announce-only polling): a
  1-3s NEF download on the worker thread blinds detection exactly when
  re-strikes happen. The queue flushes on disarm, quiet loops, a 120s age
  valve, and before disconnect (`ignore_stop`).
- The write ledger declares `reverted` only after patience + two stable
  mismatches, with family escalations first (Nikon isoauto accepts the
  write with live view paused).

## Origin

Extracted 2026-07-12 from the BlackPixel desktop editor's tethering stack
(hardware-validated on a Nikon Z6 II and a Sony A7R IV) through a 3-agent
adversarial design review; the session-protocol design won over a
transport-ownership interface primarily because the moved worker loop had to
stay verbatim (the Nikon body was not available to re-validate a rewrite).
The full adjudication is preserved in ADR 0001; the migration was gated by a
golden write-sequence pin and a transcript-equivalence harness comparing the
pre-move and post-move implementations on identical simulator scenarios.
