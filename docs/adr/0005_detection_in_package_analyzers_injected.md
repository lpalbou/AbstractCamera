# ADR 0005 — Detection lives in-package; host analyzers are injected

Status: accepted (2026-07-12)

## Decision

The meteor/motion detectors (`detection.py`) and their worker-loop wiring
(budgeted dispatch, flood gating, ring clips, auto-fire arbitration) are
package-owned. Host-specific frame analytics (the origin host's lightning
metrics) stay host-owned and are injected via `set_frame_analyzer`.

## Context

The detectors are generic live-view analysis (numpy+cv2, zero host imports)
with deep loop wiring (lazy instantiation, per-target reset, sensitivity
plumbing, cost-budget frame skipping). Inverting them through injection
would force a detector-factory protocol whose only implementation lived in
one host — abstraction with negative value. The analyzer seam already
existed and is the correct boundary.
