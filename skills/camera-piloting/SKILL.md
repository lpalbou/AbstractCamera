---
name: camera-piloting
description: Pilot real cameras through the camera_* tools — discovery vs device ids, look-vs-shoot etiquette, capture workflows, detection choreography with event-cursor rules, and honest cleanup. Use when a task involves taking photos/videos, watching for motion, or reading camera events.
---

# Camera piloting

Eleven `camera_*` tools drive real hardware (tethered Sony/Nikon bodies,
webcams, DWARF smart telescopes). Cameras are PHYSICAL: shutters wear,
devices stay claimed until closed, and captures record real people and
places — capture verbs ask for approval by default, and the user's consent
governs. Never work around a denied approval.

## The two id spaces (most common mistake)

- `camera_id` — a DISCOVERY id from `camera_list_devices`. ONLY
  `camera_open` accepts it.
- `camera` — the DEVICE UID that `camera_open` returns (also in
  `camera_status`). Every other tool takes this one.

Passing a discovery id to a capture tool fails with "No live camera has
device uid…" — re-read the `camera_open` result, don't re-list.

## Look, don't shoot (default etiquette)

- "What do you see?" / framing / monitoring → `camera_preview_photo`:
  saves the live-view frame silently (no shutter, preview resolution).
- A real photograph the user asked for → `camera_capture_photo`
  (full resolution, shutter fires, file downloads from the camera).
- Bounded clip → `camera_capture_video(duration_s=...)`. If it reports a
  recording is running, `camera_stop_recording` is the escape hatch.

## Canonical capture workflow

1. `camera_open` (empty `camera_id` = default device; already-open is
   returned, not re-claimed). Slow PTP opens can take ~20s — retrying the
   same open joins the in-flight one.
2. `camera_status` if unsure of state (recording? downloads pending?).
3. Capture. On `deferred: true`: the file stays on the camera while
   detection auto-fire is armed — poll `camera_get_events` for the
   `photo` event carrying the local `path` after disarm.
4. `camera_close` when DONE — a claimed camera blocks every other
   application on the machine.

## Detection (watch for motion/lightning/meteors)

1. `camera_start_detection(target="motion", action="photo")` — action
   `monitor` logs without firing the shutter; `photo`/`video` auto-fire
   on hits. Note: motion/meteor detections also SAVE a short pre-event
   ring clip to disk in every mode, monitor included (the clip path rides
   the detection event) — monitor skips captures, not recording. Note the
   returned `event_watermark`.
2. Poll `camera_get_events(since_id=<cursor>)`; the returned `last_id` is
   the next cursor. Detection events carry `metrics` (bbox/centroid/speed)
   — use them instead of parsing the prose note.
3. Cursor rules: if `session` CHANGES between polls the camera
   reconnected — reset the cursor to 0. `evicted: true` means events were
   dropped between your cursor and `first_retained_id` (poll faster).
   `photo`/`photo-pending` events carry `trigger_id` correlating them to
   their trigger act.
4. `camera_stop_detection` when done; deferred files download at disarm.

## Event kinds (the full set)

`detection` (seen; `metrics`, and motion/meteor hits carry `path` to
their auto-saved ring clip — monitor mode included), `trigger` (capture
act issued), `photo` (file saved locally; `path`), `photo-pending` (on
the camera; downloads later or stays per save policy), `clip`
(pre-capture ring clip), `camera-event` (device status), `error`.

## Cleanup honesty

Close what you opened. If a task ends with detection armed or a recording
running, disarm/stop FIRST (`camera_stop_detection`,
`camera_stop_recording`), then `camera_close`. A recording left running
fills the card; a claimed device wedges other hosts.

## Wake a workflow on motion (event API, no polling loop)

Detection runs on the camera's own worker thread; every hit lands in the
event log (`camera_get_events`, oldest-first, cursored). You do NOT poll it
from an LLM loop — a producer AT A FRAMEWORK ENTRY watches the events and
emits a durable wake, and a workflow parks until then. abstractcamera is a
dependency of abstractcore and ships no gateway-facing daemon; the wake
producer is a gateway-hosted run or a flow that consumes this event API.

If you ARE authoring that producer/consumer flow: the wake event is a
GLOBAL-scope event named after the mailbox, which the gateway keys as
`evt:global:global:<mailbox>` (e.g. `evt:global:global:camera`). A
`wait_event` node passes its `event_key` VERBATIM as the wait key, so it
must be the FULL `evt:global:global:camera` string — parking on the bare
`camera` never wakes (durable envelopes pile up in the run's inbox while it
sleeps). The clean alternative: an `on_event` node with scope Global and
name `camera` builds the key for you. The producer half must be a
code/tool long-poll (no LLM call per check), never an Agent node polling
in a loop — that would burn tokens idling.
