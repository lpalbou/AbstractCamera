# ADR 0010 — DWARF network family: a smart telescope is a camera plus a mount

Date: 2026-07-14 · Status: accepted

## Context

The DWARF 3 smart telescope is a Wi-Fi device: protobuf commands over a
WebSocket (port 9900, one MASTER controller at a time), RTSP live view,
captures landing in an on-device album (microSD) served over HTTP, and an
alt-az mount with GOTO/joystick/calibration. It is the first camera in
this package that (a) lives on the network rather than on USB/AVFoundation
and (b) has controllable degrees of freedom beyond the sensor.

## Decision

1. **The session protocol absorbs the network transport unchanged**
   (ADR 0001). `DwarfSession` maps: RTSP frames -> `capture_preview()`;
   the PHOTOGRAPH command -> `trigger_capture()`; album polling -> a
   `GP_EVENT_FILE_ADDED` whose `(folder, name)` is the album `filePath`;
   HTTP download -> `file_get`. The manager, hub, detection, sequences and
   BlackPixel all drive the telescope with zero new concepts.

2. **The mount rides the ACTION channel as FAMILY ACTIONS.** Actions
   already had exactly the right contract for motion (one-shot, never
   cached, never replayed — replaying a cached slew on reconnect would
   physically move the telescope). `CameraAdapter.family_action_names()`
   (default `()`) extends the accepted action vocabulary per family;
   the DWARF adds `gotoradec`, `gotosolar`, `stopgoto`, `calibrate`,
   `joystick`, `joystickstop`, and maps the canonical focus actions to the
   device's astro/manual focus. GOTO/calibration progress arrives through
   the device's own state notifications, forwarded between preview frames
   (`poll_session_events`, a no-op for families that only speak around
   captures) into the catch log.

3. **Discovery is configured, never scanned** (ADR 0006 applied to a
   network transport). `list_cameras()` lists exactly the hosts named by
   `ABSTRACTCAMERA_DWARF_HOSTS`; no subnet sweeps or TCP probes ride the
   library's list path. `scripts/validate_dwarf.py` is the deliberate
   scanning tool. A DWARF entry is never the default camera (connecting a
   telescope is a choice, like a Continuity iPhone).

4. **The protocol layer is implemented from DwarfLab's PUBLISHED API v2
   spec** with a minimal vendored proto3 codec (`drivers/dwarf_wire.py`).
   The known community bridges are GPL-licensed and cannot be linked from
   this MIT package; the wire format itself is a published interface. The
   only new dependency is `websocket-client` behind the `dwarf` extra
   (ADR 0003: transports are extras; frame processing stays in base).

5. **Master-lock honesty.** The DWARF grants one controller. `init()`
   requests the master lock and REFUSES with actionable text when the
   device answers slave (close the DWARFLAB app) — a session that
   silently ran as an observer would break every write path downstream
   while looking connected.

## Consequences

- Capture latency is honest: shutter -> album entry -> Wi-Fi download
  takes seconds; capture windows say so (`expect_file_within_s` ≈
  exposure + 30s) and the no-file note names the microSD/Wi-Fi.
- Exposure/gain dials carry the device's OWN gear tables (fetched from
  `/getDefaultParamsConfig`); no fabricated ranges (ADR 0004). Values
  parse as shutter speeds, so the intervalometer validation rides
  `nominal_exposure_s` unchanged.
- The wide-angle lens and the astro live-stacking pipeline are NOT piloted
  in v1; the capability notes say so explicitly.
- Hardware validation: `scripts/validate_dwarf.py` (mount motion strictly
  opt-in via flags; the default run is read-only plus one photo).
