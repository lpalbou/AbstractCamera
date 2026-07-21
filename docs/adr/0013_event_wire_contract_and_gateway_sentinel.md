# 0013 — The event log is a wire contract; the camera is a wake source

Date: 2026-07-21 · Status: accepted

## Context

ADR 0012 made the catch-log an API: LLM tools (`camera_get_events`) and
`/v1/camera/events` poll it as consumers. An adversarial pass (2026-07-21)
found the contract underspecified in exactly the ways machine consumers
hit: eviction was indistinguishable from quiet (bounded deque, ~minutes
under busy auto-fire), cursor epochs reset invisibly on reconnect (a
stored `since_id` silently hid the new session's events), photo events
carried no correlation to the trigger act that produced them (a deferred
backlog file flushing during a capture wait could be claimed as that
capture's result), and detectors threw away structured metrics at the
boundary (prose notes only). Separately: polling is the wrong shape for
"wake me when something moves" — the framework's differentiator is parked
durable runs woken by `emit_event`, and the camera had no producer half.

## Decision

1. **Explicit cursor contract.** Every manager mints a `session` epoch at
   connect; `get_events` responses carry it, plus `evicted` +
   `first_retained_id` when the bounded log dropped events past the
   caller's cursor. Consumers reset cursors on epoch change; gaps are
   signaled, never silent. All seven event kinds are documented wire
   vocabulary (`detection`, `trigger`, `photo`, `photo-pending`, `clip`,
   `camera-event`, `error`).

2. **Announce-time trigger correlation.** Every trigger ACT increments a
   per-manager `trigger_seq` before hardware fires; file events are
   stamped with the seq current at ANNOUNCE time and the stamp rides the
   deferred-download queue. Capture waits snapshot the seq before firing
   and skip stamped events below it. This is deliberately an
   approximation — PTP gives no true file↔trigger link; files announced
   after trigger N and before N+1 belong to N (a burst's files all carry
   its one seq). The existing guards (per-camera capture lock, busy
   refusal while `downloads_pending`, armed-mode refusals) close the
   orderings the approximation alone would miss. Unstamped events are
   still accepted by waits: a stamping gap must degrade to the old
   behavior, never to a timeout.

3. **The sentinel lane (`abstractcamera watch`, `gateway_bridge.py`).**
   Camera as event PRODUCER: a standalone process owns one camera session
   (open → arm detection → forward → disarm/close), polls the local event
   log, and posts matching events as DURABLE gateway events
   (`POST /api/gateway/commands`, type=emit_event, global-scope mailbox —
   the resident-agent rendezvous). Parked workflows/entities declaring
   the mailbox wake on movement; the payload carries capture paths +
   detection metrics so the woken agent works without holding the camera.
   Delivery semantics: at-least-once toward the gateway (cursors persist
   per camera+session AFTER each accepted emit; failures hold the cursor),
   exactly-once past it (command ids derived from
   mailbox+camera+session+event id — the gateway command store dedupes
   crash replays; live-verified `duplicate: true`). Stdlib-only HTTP: the
   bridge must not drag client dependencies into the base install.

4. **Look without shooting.** `camera_preview_photo` (eleventh tool,
   `CameraService.preview_photo`) saves the current live-view frame — no
   shutter actuation, no capture event, nothing on the camera's card. An
   agent asked "what do you see?" no longer fires a physical shutter.
   Classified `captures_environment: True` (it records the surroundings;
   ask-by-default like every recording tool) with
   `remote_write_capable: False` (frame pulls are reads).

## Adversarial folds (one subagent pass, 2026-07-21 — 2 P1 / 8 P2)

The operator-mandated adversary attacked the wave; every accepted finding
is folded and test-pinned. The two P1s shared one theme: the wave built a
correct correlation/epoch PRODUCER and left two CONSUMERS reading the old
world — (1) the capture wait's ERROR branch ignored the trigger stamp, so
a backlog file whose fetch failed mid-wait was reported as the fresh
capture's failure (stale-stamped errors now skip; unstamped errors still
abort); (2) the direct-manager reconnect path re-minted the epoch but kept
events/counters, making the "new epoch = reset your cursor" contract a
lie for `get_default_manager()`-style hosts and double-waking bridge
consumers with history (a reconnect now clears the log and restarts ids
— a new epoch IS a new id space). P2 folds: sequence frames are trigger
acts (own seq per frame — a 100-frame timelapse no longer stamps 100
files with one stale act); fatal bridge auth errors (401/403/404) are
named instead of "retrying, nothing lost"; the default cursor file is
per-mailbox (whole-file replace loses a co-tenant's cursors); `--kinds`
strips whitespace; relative `--state-file` paths persist; `preview_frame`
fails fast on a dead camera instead of "retry shortly"; `public_status`
carries `session`; the hub docstring no longer teaches chained bare
connects (default resolution has no "next unclaimed device" notion — a
chain double-claims). Honest residuals documented rather than "fixed":
under armed auto-fire a detection act can interleave between a manual
capture's seq snapshot and its fire (shape-identical deferred result,
harmless), and announce-time stamping cannot distinguish act N's slow
file from act N+1's (the timeout-retry window) — inherent to PTP's
missing file↔trigger link.

## Consequences

- The bridge owns its camera session end to end; it deliberately does NOT
  observe cameras other processes opened (a device claimed twice wedges
  the transport). Agent-held cameras inside a gateway process would need
  a host-side pump — a separate design, gateway ruled itself
  zero-camera-code.
- One state file per bridge process by default; concurrent bridges should
  use distinct `--state-file` paths (documented limitation).
- Consumers that stored cursors before this contract see `session` appear
  and should adopt the reset rule; the response is otherwise
  backward-compatible (added keys only).
- The `_pending_downloads` queue tuple grew a `trigger_id` element —
  internal shape, no external consumer.
