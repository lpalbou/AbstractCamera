# ADR 0007 — Regression policy for unconnected hardware

Status: accepted (2026-07-12)

## Decision

Hardware-validated behavior whose body is not currently connected is
protected by: (1) the simulator personalities encoding the measured quirks,
(2) the golden write-sequence pin (exact camera-write ordering of a scripted
session), (3) the ported regression suites whose assertions may MOVE but
never WEAKEN, and (4) for structural migrations, a transcript-equivalence
harness comparing pre- and post-change implementations on identical
simulator scenarios (ordered side-effects, catch-log sequences, window
arithmetic, downloads trajectories). Success claims require the named
validations passing — a merged diff is not a success claim (framework
evidence discipline).

## Applied

The 2026-07-12 extraction ran all four gates; the Sony A7R IV re-validated
on hardware through the package (22+11 checks) the same day; the Nikon Z6 II
remains protected by gates 1-4 pending its next physical session.
