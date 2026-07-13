# ADR 0006 — Discovery and camera identity

Status: accepted (2026-07-12)

## Decision

`list_cameras()` is NON-INVASIVE: no device opens at list time (no LED, no
permission prompt, no contention). Ids are transport-prefixed
(`ptp:<address>`, `webcam:<index>`) and positional; a stale id REFUSES with
"refresh and pick again" — never silently opens a different device. Webcam
names come from ffmpeg's AVFoundation lister (Desk View and screen-capture
pseudo-devices filtered), labeled `best_effort`, and every webcam entry
carries a structured `kind`: `built_in` (the machine's own camera),
`continuity` (a nearby iPhone/iPad that macOS exposes WIRELESSLY via
Continuity Camera — same Apple ID, no cable), or `external` (USB cameras).
Continuity devices sort last, carry an explicit wireless note, and never
become the default: connecting someone's phone must be an informed choice.
Default order: first PTP body (historic host behavior), else the best-ranked
webcam (built-in → external → continuity-as-last-resort).
`ABSTRACTCAMERA_FAKE=1` replaces all transports with the simulator;
resolution happens per-connect, not at import.
