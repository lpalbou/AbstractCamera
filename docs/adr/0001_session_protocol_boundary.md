# ADR 0001 — Session-protocol boundary

Status: accepted (2026-07-12, 3-agent adversarial election)

## Decision

The manager owns the worker loop and session lifecycle; camera families
provide SESSION objects implementing the protocol the loop already speaks
(`init/exit/get_abilities/capture_preview/trigger_capture/wait_for_event/
file_get` + optional single-config widget I/O). Wire constants (`wire.py`)
are numerically pinned to libgphoto2 and evolve additively only
(`SESSION_PROTOCOL_VERSION`).

## Context

The loop is ~1,900 lines of policy extracted from real hardware failures
(deadline-aware drains, deferred downloads, the write ledger, watchdog,
wedge recovery), hardware-validated on bodies not always available for
re-validation. The competing design (backends own ALL transport I/O behind
a semantic interface) required rewriting the most hardware-scarred ~330
lines, provable only against simulators. The simulator itself
(`sim/gphoto2.py`) is the existence proof that the protocol is implementable
without gphoto2; the webcam session is the second proof.

## Consequences

- The validated loop moved VERBATIM (gated by a golden write-sequence pin
  and a transcript-equivalence harness against the pre-move code).
- Non-PTP families pay a small translation tax (synthesized FILE_ADDED
  events, an in-process "download"): ~7 small fictions per webcam capture,
  all flowing through machinery the regression suites already exercise.
- The protocol's BEHAVIORAL items (timeout tuples, raise-on-unservable
  preview, loop pacing) are executable conformance tests, not prose.
- Adapters may know their family's session subtype (Sony assumes
  single-config; the webcam adapter calls `start_movie`); the protocol
  constrains the shared loop, not family-private choreography.
