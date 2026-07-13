# ADR 0003 — Base deps carry frames; transports are extras

Status: accepted (2026-07-12)

## Decision

Base install: `numpy` + `opencv-python`. Extras: `[gphoto2]` (PTP bodies,
floor 2.5.10 for single-config), `[clips]` (PyAV MP4 encoding), `[raw]`
(rawpy thumbnails).

## Context

This deviates from the framework's empty-base pattern (AbstractVision ADR
0003) deliberately: the manager itself decodes JPEGs (detection dispatch,
thumbnails, preview probes) and the always-available webcam family needs
OpenCV — a camera package that cannot process frames would not be a camera
package. `pip install abstractcamera` on a Mac gives a WORKING camera stack
with zero native transport libraries.

## Consequences

Hosts shipping `opencv-python-headless` will double-install OpenCV; accepted
and documented. Absent extras refuse honestly in-receipt with install hints
(never crash, never pretend).
