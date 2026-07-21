"""Capture-completion waits for CameraService.

Pure functions over a duck-typed manager (`status()`, `get_events()`),
extracted from service.py so the service file stays on the operation state
machine (locks, guards, lifecycle). Being manager-duck-typed is deliberate:
the adversarial reviews probe these waits with stub managers — determinism
the real worker thread cannot offer.

Wait discipline (adversarially audited 2026-07-19):
- Only capture-shaped error REASONS abort a wait — config-honesty events
  (dial reverts) share the log and must never be reported as a capture's
  failure.
- Timeouts carry a `timed_out: True` sentinel; callers branch on it, never
  on message text (a "Timed out" substring match once converted a real
  download failure into success).
- A disconnected camera fails FAST with the real reason — never grind out
  the timeout against a dead manager's frozen event log.
"""

from __future__ import annotations

import time

from abstractcamera.service_support import fail as _fail
from abstractcamera.service_support import ok as _ok
from abstractcamera.service_support import public_event as _public_event

MOVIE_STATE_TIMEOUT_S = 15.0
_EVENT_POLL_INTERVAL_S = 0.05

# Error-event reasons that terminate a capture wait we initiated. "manual"
# is the worker's reason for manually-triggered fire/movie-toggle failures
# (the reason strings are the _fire_trigger call sites' vocabulary);
# "config" is deliberately absent.
CAPTURE_ERROR_REASONS = ("manual", "trigger", "download", "connection", "camera")


def event_watermark(manager) -> int:
    """Highest event id RIGHT NOW; capture waits only consider events
    strictly newer than this (a stale 'photo' event from a previous
    capture must never satisfy a new wait)."""
    events = manager.get_events(since_id=0)
    return events[0]["id"] if events else 0


def wait_for_movie_state(manager, *, recording: bool, since_id: int) -> dict:
    """Wait until status()['movie_recording'] reaches the wanted state,
    surfacing worker-side refusals (movie needs [clips], movieprohibit,
    wedge recovery) as honest errors instead of a silent timeout.

    An error match is re-checked against the actual state once, because an
    error event can coexist with a toggle that in fact landed."""
    deadline = time.time() + MOVIE_STATE_TIMEOUT_S
    while time.time() < deadline:
        state = manager.status()
        if bool(state.get("movie_recording")) == recording:
            return _ok()
        if not state.get("connected"):
            return _fail("The camera was disconnected while confirming the recording toggle.")
        for event in reversed(manager.get_events(since_id=since_id)):
            if event.get("kind") == "error" and event.get("reason") in CAPTURE_ERROR_REASONS:
                if bool(manager.status().get("movie_recording")) == recording:
                    return _ok()
                return _fail(event.get("note") or "movie toggle failed", {"event": _public_event(event)})
        time.sleep(_EVENT_POLL_INTERVAL_S)
    wanted = "start" if recording else "stop"
    return _fail(
        f"Timed out waiting for video recording to {wanted} "
        f"(the camera never confirmed within {MOVIE_STATE_TIMEOUT_S:g}s).",
        {"timed_out": True},
    )


def wait_for_capture(
    manager,
    *,
    watermark: int,
    timeout_s: float,
    pending_meaning: str | None,
    what: str,
    min_trigger_id: int | None = None,
) -> dict:
    """Block until a capture completion event newer than `watermark`.

    Terminal outcomes:
    - kind="photo": the file was saved locally (path in the event).
    - kind="photo-pending" with pending_meaning="on_device": the shot
      exists on the camera's own storage (device-only save policy).
    - kind="photo-pending" with pending_meaning="deferred": detection
      auto-fire is armed, downloads are deferred by design — honest
      success now, the file lands when detection disarms.
    - kind="error" with a capture-shaped reason: honest failure.
    - camera disconnected: immediate honest failure.

    `min_trigger_id` closes the backlog-misattribution window (adversarial
    finding 2026-07-21): file events are stamped with their ANNOUNCE-time
    trigger seq, so a deferred download flushing DURING this wait carries
    an older trigger id and is skipped instead of being claimed as this
    capture's result. Events without a stamp (trigger_id None) are still
    accepted — refusing them would turn a stamping gap into a timeout.
    """
    def _stale(event: dict) -> bool:
        if min_trigger_id is None:
            return False
        stamped = event.get("trigger_id")
        return stamped is not None and stamped < min_trigger_id

    deadline = time.time() + timeout_s
    since = watermark
    while time.time() < deadline:
        events = manager.get_events(since_id=since)
        for event in reversed(events):  # oldest first
            kind = event.get("kind")
            # Staleness applies to ERROR events too (adversarial P1,
            # 2026-07-21): a deferred backlog file whose FETCH fails during
            # this wait (age valve / stop_detection flush) carries its
            # ORIGINAL act's stamp — reporting it as this capture's failure
            # is the same misattribution class as claiming its success.
            # Unstamped errors still abort (a stamping gap must degrade to
            # the old behavior, never mask a real failure).
            if kind in ("photo", "photo-pending", "error") and _stale(event):
                continue
            if kind == "photo":
                path = event.get("path")
                out = {
                    "kind": what,
                    "path": path,
                    "on_device": False,
                    "event": _public_event(event),
                }
                # The ruled sight-lane field (commons 3969/4089): results
                # that landed a LOCAL FILE carry handler-authored `media`
                # so the agent adapter's fold can put the image in front of
                # the model — bare path on the storeless lane ($artifact
                # refs are the plugin's override). ABSENT when no file
                # landed; never sniffed from prose.
                if path:
                    out["media"] = [path]
                return _ok(out)
            if kind == "photo-pending" and pending_meaning == "on_device":
                return _ok(
                    {
                        "kind": what,
                        "path": None,
                        "on_device": True,
                        "note": event.get("note"),
                        "event": _public_event(event),
                    }
                )
            if kind == "photo-pending" and pending_meaning == "deferred":
                return _ok(
                    {
                        "kind": what,
                        "path": None,
                        "on_device": False,
                        "deferred": True,
                        "note": (
                            "Captured; the file stays on the camera while detection "
                            "auto-fire is armed and downloads when detection disarms "
                            "(watch get_events for the 'photo' event with the path)."
                        ),
                        "event": _public_event(event),
                    }
                )
            if kind == "error" and event.get("reason") in CAPTURE_ERROR_REASONS:
                return _fail(event.get("note") or f"{what} capture failed", {"event": _public_event(event)})
        if events:
            since = max(e["id"] for e in events)
        if not manager.status().get("connected"):
            return _fail(f"The camera was disconnected while waiting for the {what}.")
        time.sleep(_EVENT_POLL_INTERVAL_S)
    return _fail(
        f"Timed out after {timeout_s:g}s waiting for the {what} to complete — "
        "the camera never reported the capture. Check status() and the event "
        "log; long exposures need a larger timeout_s.",
        {"timed_out": True},
    )
