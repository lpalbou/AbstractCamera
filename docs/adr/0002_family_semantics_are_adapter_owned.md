# ADR 0002 — Family semantics are adapter-owned; one worker thread

Status: accepted (imported from the origin host's adversarial election,
2026-07-12, re-ratified for the package)

## Decision

Every family-divergent behavior (write policy, trigger semantics, burst
mechanics, movie policy, focus choreography, event noise, connect defaults,
capabilities) lives in one `CameraAdapter` subclass per family. The manager
stays family-agnostic. ONE worker thread owns every camera call; adapter
methods that touch the camera run only on it; adapters that pump events
internally forward every event to the manager's sink (no swallowed
FILE_ADDED). The manager owns the session lifecycle including wedge
recovery; adapters request it via receipts (`probe_session`).

## Consequences

A new family is one adapter file (+ a session/driver when not gphoto2-
transported); the manager does not change. Receipt dataclasses
(`WriteReceipt`, `CaptureTiming`, `MovieReceipt`, `ActionReceipt`) carry
family semantics as data.
