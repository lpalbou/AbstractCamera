# 0013 — The camera event log is a wire contract (consumed at a framework entry)

Date: 2026-07-21 · Status: accepted (§3 superseded same day — see the amendment)

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
boundary (prose notes only). Separately, "wake me when something moves"
wants a durable run woken by an event rather than a polling loop — the
question of WHO produces that event is answered by the layering (see §3).

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

3. **~~The sentinel lane (`abstractcamera watch`, `gateway_bridge.py`).~~**
   **SUPERSEDED 2026-07-21 (operator ruling, dm:camera--laurent#14) — the
   daemon was REMOVED. See the amendment below.** The original decision
   shipped a standalone `abstractcamera watch` process that polled the
   local event log and posted events to the gateway's command API. That
   was an architecture error: abstractcamera is a dependency of
   abstractcore, and the daemon reached two layers UP (hardcoding
   abstractgateway's `/api/gateway/commands` shape and the
   `evt:global:global:<mailbox>` wait-key convention) and was launchable
   by nobody in the gateway-first operating model.

4. **Look without shooting.** `camera_preview_photo` (eleventh tool,
   `CameraService.preview_photo`) saves the current live-view frame — no
   shutter actuation, no capture event, nothing on the camera's card. An
   agent asked "what do you see?" no longer fires a physical shutter.
   Classified `captures_environment: True` (it records the surroundings;
   ask-by-default like every recording tool) with
   `remote_write_capable: False` (frame pulls are reads).

## Amendment (2026-07-21, operator dm#14): the producer belongs at an entry

The framework has exactly TWO entries — **core and gateway**. A dependency
below abstractcore must never reach up to either from below. So the
"camera as a wake source" idea keeps its GOOD half and loses its wrong
half:

- **KEPT — the event API (§1, §2).** Detection runs in-process (the
  `CameraManager` worker thread); its results land in the bounded,
  cursor-contracted event log, readable through the `camera_get_events`
  tool, the `detection_events` capability op, and `/v1/camera/events`.
  That is the capability's own surface and it is the clean seam a producer
  reads. This is the whole value of the wire-contract work — it survives
  the daemon's removal intact.
- **REMOVED — the daemon (§3).** `gateway_bridge.py`, the `watch` CLI
  verb, and `tests/test_gateway_bridge.py` are deleted. abstractcamera
  encodes zero gateway-API knowledge.
- **CORRECT SHAPE — the producer is a consumer of the event API, at an
  entry.** A gateway-hosted durable run (or a flow that holds a camera
  open through the capability) watches the event log and emits the wake
  event using the gateway's OWN `emit_event` — the gateway talking to the
  gateway, in-layer. The camera-in-flows adversary (below) already proved
  the CONSUMER exists: a flow `wait_event`/`on_event` node wakes on such
  an event with zero new machinery. Who builds/owns that producer is a
  core/gateway decision (coordinated on commons), not camera's to ship.

## Adversarial folds (one subagent pass, 2026-07-21 — 2 P1 / 8 P2)

The operator-mandated adversary attacked the original wave; every accepted
finding was folded and test-pinned. The two P1s were in the EVENT-CONTRACT
lane (which survives) and both hold: (1) the capture wait's ERROR branch
ignored the trigger stamp, so a backlog file whose fetch failed mid-wait
was reported as the fresh capture's failure (stale-stamped errors now
skip; unstamped errors still abort); (2) the direct-manager reconnect path
re-minted the epoch but kept events/counters, making the "new epoch =
reset your cursor" contract a lie for `get_default_manager()`-style hosts
(a reconnect now clears the log and restarts ids — a new epoch IS a new id
space). Surviving P2 folds in the event/capture lane: sequence frames are
trigger acts (own seq per frame); `preview_frame` fails fast on a dead
camera; `public_status` carries `session`; the hub docstring no longer
teaches chained bare connects. (Bridge-only P2s — auth-error wording,
per-mailbox cursor files, `--kinds`/`--state-file` hygiene, unplug reopen —
died with `gateway_bridge.py`.) Honest residuals in the surviving lane:
under armed auto-fire a detection act can interleave between a manual
capture's seq snapshot and its fire (shape-identical deferred result,
harmless), and announce-time stamping cannot distinguish act N's slow file
from act N+1's — inherent to PTP's missing file↔trigger link.

## Camera-in-flows reachability (adversary 2026-07-21, operator dm#13)

An adversarial pass on "camera features are reachable from an AbstractFlow
workflow through the EXISTING nodes" confirmed the claim — NO new nodes are
needed for camera access, and it is the evidence for the §3 amendment:
(1) one registry feeds the tool picker, the run's tool map, and the
executor, so a flow Agent/tool_calls node's `allowed_tools` resolves camera
names that actually execute; (2) the sight-lane `media` field survives the
flow tool_calls result verbatim (dict outputs are not projected to a
schema); (3) a flow `wait_event`/`on_event` node wakes on a global-scope
camera event — the in-layer consumer the producer would target. The ONE
finding (P1, doc-class): a `wait_event` node must park on the FULL resolved
key `evt:global:global:<mailbox>`, never the bare mailbox name. DWARF mount
actions (`request_action`) remain unreachable above the Python library and
need a TOOL (not a node) when DWARF unparks.

## Consequences

- abstractcamera holds ZERO gateway-API knowledge — no endpoint paths, no
  command shapes, no wait-key conventions. The layering (`abstractgateway →
  abstractruntime → abstractcore → abstractcamera`) is respected: camera
  offers a capability and an event API, and never reaches up.
- The wake-on-motion producer is a follow-up owned at a framework entry
  (core/gateway) or a flow; camera's obligation is only to keep the event
  API clean and consumable. Tracked as backlog 0016 (re-scoped).
- Consumers that stored cursors before this contract see `session` appear
  and should adopt the reset rule; the response is otherwise
  backward-compatible (added keys only).
- The `_pending_downloads` queue tuple grew a `trigger_id` element —
  internal shape, no external consumer.
