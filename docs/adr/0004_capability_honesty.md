# ADR 0004 — Capability honesty

Status: accepted (2026-07-12)

## Decision

Capabilities never pretend, in either direction: no fabricated controls (a
webcam exposes no ISO dial; every cv2 CAP_PROP_* set returns False on the
validated hardware — so there is no exposure surface at all), and no
unmapped realities (resolution rides the standard imagesize dial; webcam
movie recording IS confirmable because the package writes the file, and the
receipt says so — the exact inversion of Sony's unconfirmable movie note).
Degraded modes disclose themselves: best-effort names carry
`name_confidence`, unverifiable operations carry honest notes, reverted
writes name their cause, `config_widgets` lets hosts hide what cannot exist
instead of rendering locked ghosts.

## Consequences

Host UIs adapt from `status()["capabilities"]` instead of sniffing widget
names; honesty regressions are test failures (the webcam suite asserts the
absence of pretend surfaces).
