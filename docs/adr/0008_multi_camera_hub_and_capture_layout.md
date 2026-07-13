# ADR 0008: Multi-camera hub, device identity, and the capture layout

Status: accepted (2026-07-12)

## Context

The host's Capture panel grew from "one camera at a time" to "pilot every
connected camera at once" (owner directive 2026-07-12): a Nikon Z6 II, a
Sony A7R IV, the MacBook's own camera, and an iPhone (Continuity) must run
concurrently — each with its own dials, sequences, detections, recordings —
and captures must land somewhere a user can FIND, organized per device.

Two designs were on the table:

1. Make `CameraManager` internally multi-session (one worker juggling all
   cameras, or a session registry inside the manager).
2. Keep `CameraManager` strictly single-camera and add a thin `CameraHub`
   that owns N managers.

## Decision

**Design 2.** `CameraManager` stays exactly what the hardware validation
proved: one camera, one worker thread that owns ALL of that camera's I/O.
This is also libgphoto2's documented thread-safety model — the library is
safe across DIFFERENT cameras on different threads, unsafe within one.
`CameraHub` is a registry keyed by device uid: connect-by-id reuse, an
ACTIVE selection for single-panel hosts, shared manager configuration
(capture root, frame analyzer), annotated discovery, survivor fallback on
disconnect. No manager internals changed for concurrency.

**Device identity**: at connect the manager derives a filesystem-safe slug
from the model/label (`Nikon Z6_2` → `nikon_z6_2`, parentheticals stripped)
plus a best-effort serial (`serialnumber` widget; webcams have none). The
hub disambiguates identical bodies with a serial-tail suffix (else an
index). The uid names everything user-visible: capture folders, HTTP
addressing, panel selection.

**Capture layout** (owner-specified): `<capture_root>/<device_slug>/`, with
root defaulting to `~/Pictures` — captures belong where users look for
pictures. An optional SEQUENCE NAME (`set_sequence_name` /
`start_interval_sequence(sequence_name=...)`) nests everything one level
deeper. `set_capture_dir()` keeps its legacy explicit-override meaning for
embedders and tests.

**Save policy**: `set_save_policy(download_locally=False)` announces
captures in the event feed but never fetches — files live on the camera's
own storage. Families without storage (`save_modes() == ["local"]`) refuse
device-only honestly. Device-only + a volatile capture target (camera RAM)
draws a loud error event: those shots would exist NOWHERE.

## Consequences

- Concurrency cost is one thread per camera — measured fine with four
  cameras (2 PTP + 2 AVFoundation, 15-40 fps each, stills/timelapse/movie
  running simultaneously; see `scripts/validate_multicam.py`).
- The `get_default_manager()` singleton remains for single-camera hosts;
  hub and singleton must not share a body (first claim wins, second refuses
  honestly — unchanged transport behavior).
- Hosts address devices explicitly (`device_uid`) or implicitly (the hub's
  ACTIVE selection); the BlackPixel routes default to ACTIVE, which keeps
  every historic single-camera call working unchanged.
- A tiny window exists between a manager's connect and the hub's uid
  finalization for identical-body suffixing; captures cannot occur in it
  (no user-visible trigger path exists before connect returns).
