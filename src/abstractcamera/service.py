"""CameraService: synchronous request/response camera operations.

CameraManager is an orchestrator: triggers are asynchronous (the worker
thread fires them; results surface later as catch-log events). Hosts that
speak request/response — LLM tools, AbstractCore capability facades, HTTP
routes — need "take a photo, tell me where it landed" as ONE blocking call.
CameraService is that layer: dict-in/dict-out operations over a CameraHub,
with capture completion resolved by watching the per-camera event log from
a watermark taken BEFORE the trigger (no ordering race, no event loss).

One service instance per process is the intended shape (hardware is a
process-wide resource: a camera claimed twice wedges the transport), so the
module also exposes `get_shared_service()`. Both the AbstractCore capability
plugin and the AI tool set delegate here — an agent that opened a camera
through a tool and a host calling `core.camera.capture_photo(...)` address
the SAME sessions.

Contracts (adversarially audited 2026-07-19):
- Every operation returns a JSON-safe dict with `"success"` set explicitly;
  failures carry user-actionable `"error"` text and NEVER raise for bad
  input (request JSON reaches these functions unvalidated).
- Bytes never ride result dicts; captures are reported by PATH and preview
  frames are returned as a separate bytes value by the one method
  documented to do so.
- Everything that writes `capture_mode` or fires the trigger holds the
  per-camera capture lock: the trigger is a TOGGLE in video mode, so an
  unguarded mode write mid-recording strands a running recording (P0,
  adversarial review 2026-07-19).
"""

from __future__ import annotations

import atexit
import os
import threading
import time
import weakref

from abstractcamera.errors import CameraControlError
from abstractcamera.hub import CameraHub
from abstractcamera.service_support import (
    coerce_int,
    coerce_number,
    fail as _fail,
    ok as _ok,
    public_event as _public_event,
    public_status,
)
from abstractcamera.service_waits import (
    event_watermark as _event_watermark_fn,
    wait_for_capture as _wait_for_capture_fn,
    wait_for_movie_state as _wait_for_movie_state_fn,
)

# Capture waits are bounded: PTP bodies take seconds between shutter and
# FILE_ADDED (long exposures take the exposure time on top); these defaults
# are honest ceilings, not expected latencies.
DEFAULT_PHOTO_TIMEOUT_S = 30.0
MAX_PHOTO_TIMEOUT_S = 300.0
DEFAULT_VIDEO_MAX_DURATION_S = 600.0
DEFAULT_STOP_RECORDING_TIMEOUT_S = 15.0
_EVENT_POLL_INTERVAL_S = 0.05

DETECTION_TARGETS = ("motion", "lightning", "meteor")
DETECTION_ACTIONS = ("photo", "video", "monitor")

_BUSY_ERROR = "Another capture is already in progress on this camera — wait for it to finish."
_RECORDING_ERROR = (
    "The camera is recording video — stop_recording() first (a still capture's "
    "trigger would stop the recording instead of firing the shutter)."
)


class CameraService:
    """Synchronous camera operations over one CameraHub.

    Addressing vocabulary (two id spaces, deliberately explicit):
    - `camera_id`: a DISCOVERY id (from `list_cameras()`), used only by
      `open()` to claim a device.
    - `camera`: the DEVICE UID of a live camera (from `open()`/`status()`),
      used by every other operation; None/"" means the active camera.
    """

    def __init__(self, *, hub: CameraHub | None = None, capture_root: str | None = None):
        if hub is None and not capture_root:
            # The env override must work for EVERY surface that reaches the
            # shared service (tools included) — reading it only in the
            # capability constructor left tool captures in ~/Pictures while
            # the operator's env said otherwise (adversarial finding).
            capture_root = os.environ.get("ABSTRACTCAMERA_CAPTURE_ROOT") or None
        self._hub = hub or CameraHub(capture_root=capture_root)
        # One capture lock PER CAMERA (keyed by manager instance; weak so a
        # disconnected manager's entry dies with it): two concurrent capture
        # waits on one body would claim each other's completion events, and
        # unpaired video start/stop toggles corrupt recording state. Other
        # cameras stay fully concurrent (the hub runs one worker each).
        self._capture_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._capture_locks_guard = threading.Lock()
        # Serializes open(): the idempotent-default guard is a check-then-act
        # over hub state, so two concurrent default opens both passed it and
        # double-claimed one device (adversarial finding 2026-07-21,
        # reproduced on the sim — real PTP transports can wedge). The second
        # opener now waits and re-checks; retry-after-slow-open (the
        # canonical agent pattern) therefore JOINS the in-flight open
        # instead of racing it. Worst case a concurrent open of a SECOND
        # camera waits out the first's 20s connect window — accepted.
        self._open_lock = threading.Lock()

    @property
    def hub(self) -> CameraHub:
        return self._hub

    def configure(self, *, capture_root: str | None = None) -> None:
        """Shared manager configuration; applies to cameras opened AFTER
        this call (live sessions keep their capture layout). LAST caller
        wins for new connections — the hub is process-shared, so two hosts
        configuring different roots must expect this (documented in the
        plugin config_hint)."""
        if capture_root:
            self._hub.configure_managers(capture_root=capture_root)

    # ------------------------------------------------------------------
    # Discovery / lifecycle
    # ------------------------------------------------------------------

    def list_cameras(self) -> dict:
        try:
            entries = self._hub.list_cameras()
        except CameraControlError as exc:
            return _fail(str(exc))
        return _ok({"cameras": entries, "active": self._hub.active_uid})

    def open(self, camera_id: str | None = None) -> dict:
        """Claim a camera (turn it on for this process) and make it active.

        `camera_id` is a discovery id; None claims the default device.
        A DEFAULT open while a camera is already live returns that camera
        (idempotent): retry-after-slow-open is the canonical agent pattern,
        and a second default claim on the same physical device can wedge
        the transport (adversarial finding). Opening a SECOND camera is an
        explicit act — pass its discovery id.

        Serialized end to end (_open_lock): the idempotent guard is a
        check-then-act, so concurrent opens raced past it and double-claimed
        one device; the hub's connect mutex additionally guards direct hub
        callers."""
        with self._open_lock:
            if not camera_id:
                active = self._hub.active_uid
                if active is not None:
                    try:
                        manager = self._hub.manager_for(active)
                    except CameraControlError:
                        manager = None
                    if manager is not None and manager.status().get("connected"):
                        return _ok(
                            {
                                "camera": active,
                                "status": public_status(manager.status()),
                                "already_connected": True,
                                "note": "A camera is already open; pass a camera_id to open another device.",
                            }
                        )
            try:
                status = self._hub.connect(camera_id or None)
            except CameraControlError as exc:
                return _fail(str(exc))
            return _ok({"camera": status.get("device_uid"), "status": public_status(status)})

    def close(self, camera: str | None = None) -> dict:
        """Release a camera (turn it off for this process). Flushes deferred
        downloads first — that is CameraManager.disconnect's contract."""
        try:
            uid = camera or self._hub.active_uid
            self._hub.disconnect(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))
        return _ok({"camera": uid, "connected": False})

    def close_all(self) -> dict:
        self._hub.disconnect_all()
        return _ok({"connected": False})

    def status(self, camera: str | None = None) -> dict:
        """One camera's status (None = active), or every live camera when
        nothing is connected and no address was given."""
        if camera:
            try:
                manager = self._hub.manager_for(camera)
            except CameraControlError as exc:
                return _fail(str(exc))
            return _ok({"camera": camera, "status": public_status(manager.status())})
        statuses = self._hub.statuses()
        return _ok(
            {
                "active": self._hub.active_uid,
                "cameras": {uid: public_status(st) for uid, st in statuses.items()},
            }
        )

    def preview_frame(self, camera: str | None = None, *, wait_s: float = 2.0) -> tuple[dict, bytes | None]:
        """Latest live-view frame. Returns (result_dict, jpeg_bytes) — bytes
        ride beside the dict, never inside it (JSON-safety rule).

        A camera opened moments ago has no frame yet (the worker is still
        starting the preview stream), so this waits up to `wait_s` for the
        FIRST frame instead of failing a race the caller cannot see."""
        wait, err = coerce_number(wait_s, "wait_s", lo=0.0, hi=30.0, default=2.0)
        if err:
            return _fail(err), None
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc)), None
        deadline = time.time() + wait
        while True:
            jpeg, sequence = manager.get_latest_frame()
            if jpeg is not None:
                return (
                    _ok({"sequence": sequence, "content_type": "image/jpeg", "size_bytes": len(jpeg)}),
                    jpeg,
                )
            # Fail FAST on a dead camera (adversarial P2, 2026-07-21): a
            # watchdog-dead manager stays addressable until the next
            # connect reaps it, and "retry shortly" sent agents into a
            # retry loop against a corpse that will never frame again.
            if not manager.status().get("connected"):
                return (
                    _fail(
                        "The camera is not connected (it may have been unplugged) — "
                        "open it again before requesting a preview."
                    ),
                    None,
                )
            if time.time() >= deadline:
                return (
                    _fail(
                        "No live-view frame is available yet — the camera may still be "
                        "starting its preview stream; retry shortly."
                    ),
                    None,
                )
            time.sleep(_EVENT_POLL_INTERVAL_S)

    def preview_photo(self, camera: str | None = None, *, wait_s: float = 2.0) -> dict:
        """Save the CURRENT live-view frame as a JPEG and return its path —
        looking without shooting. No shutter fires, no capture event logs,
        nothing lands on the camera's card; the frame is whatever the
        preview stream is showing right now (preview resolution, not a
        full-resolution capture). The silent answer to "what do you see?"
        — capture_photo actuates the physical shutter and should be
        reserved for shots that matter (adversarial roadmap 2026-07-21)."""
        result, jpeg = self.preview_frame(camera, wait_s=wait_s)
        if not result.get("success"):
            return result
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))
        capture_dir = manager.status().get("capture_dir")
        if not capture_dir:
            return _fail("The camera has no capture directory configured — set a capture root first.")
        try:
            os.makedirs(capture_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            # Microsecond suffix: same-second preview calls must not clobber
            # each other (agents look repeatedly while framing).
            unique = f"{int((time.time() % 1) * 1_000_000):06d}"
            path = os.path.join(capture_dir, f"preview_{stamp}_{unique}.jpg")
            with open(path, "wb") as fh:
                fh.write(jpeg)
        except OSError as exc:
            return _fail(f"Could not save the preview frame: {exc}")
        return _ok(
            {
                "camera": camera or self._hub.active_uid,
                "path": path,
                "size_bytes": len(jpeg),
                "sequence": result.get("sequence"),
                "content_type": "image/jpeg",
                "note": "Live-view frame (preview resolution); no shutter fired, nothing logged on the camera.",
            }
        )

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_lock_for(self, manager) -> threading.Lock:
        with self._capture_locks_guard:
            lock = self._capture_locks.get(manager)
            if lock is None:
                lock = threading.Lock()
                self._capture_locks[manager] = lock
            return lock

    def capture_photo(self, camera: str | None = None, *, timeout_s: float | None = None) -> dict:
        """Fire one still and block until the capture lands.

        Outcomes: a locally-saved file (path), an on-device save
        (save policy), or — while detection auto-fire is armed — an honest
        DEFERRED success (`deferred: true`, the file downloads when
        detection disarms; blocking on it here would always time out
        because armed mode defers downloads by design)."""
        timeout, err = coerce_number(
            timeout_s, "timeout_s", lo=1.0, hi=MAX_PHOTO_TIMEOUT_S, default=DEFAULT_PHOTO_TIMEOUT_S
        )
        if err:
            return _fail(err)
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))

        lock = self._capture_lock_for(manager)
        if not lock.acquire(blocking=False):
            # Refusing beats queueing: a silently serialized wait can stack
            # N callers behind one long exposure with no explanation.
            return _fail(_BUSY_ERROR)
        try:
            status = manager.status()
            if status.get("movie_recording"):
                return _fail(_RECORDING_ERROR)
            detection_auto = status.get("detection_mode") == "auto"
            if not detection_auto and int(status.get("downloads_pending") or 0) > 0:
                # Stale-attribution guard (adversarial finding): photo
                # events carry no trigger correlation, so a flushing
                # backlog file would be returned as THIS capture's result.
                return _fail(
                    f"{status.get('downloads_pending')} earlier capture(s) are still downloading "
                    "from the camera — poll get_events until they land, then retry."
                )
            try:
                if status.get("capture_mode") != "single":
                    manager.set_capture_mode("single")
                watermark = self._event_watermark(manager)
                # Trigger-correlation snapshot (wire-contract 2026-07-21):
                # OUR trigger act will be seq+1 or later; file events stamped
                # below that are stale backlog and must not satisfy this
                # wait. HONEST LIMITS (adversarial P2): while auto-fire is
                # armed a detection act CAN land between snapshot and our
                # fire (its photo-pending is shape-identical to ours —
                # harmless), and announce-time stamping cannot distinguish a
                # slow file from act N announcing after act N+1 fired (the
                # timeout-then-retry window). The filter closes the BACKLOG
                # class; the residuals are inherent to PTP's missing
                # file↔trigger link and documented in ADR 0013.
                pre_trigger_seq = getattr(manager, "trigger_seq", 0)
                manager.request_trigger()
            except CameraControlError as exc:
                return _fail(str(exc))

            pending_meaning = None
            if not bool(status.get("download_locally", True)):
                pending_meaning = "on_device"
            elif detection_auto:
                pending_meaning = "deferred"
            return self._wait_for_capture(
                manager,
                watermark=watermark,
                timeout_s=timeout,
                pending_meaning=pending_meaning,
                what="photo",
                min_trigger_id=int(pre_trigger_seq) + 1,
            )
        finally:
            lock.release()

    def capture_video(
        self,
        duration_s: float,
        camera: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict:
        """Record a bounded clip: start recording, hold for `duration_s`,
        stop, and block until the movie file lands (or is honestly reported
        on-camera). The per-camera capture lock keeps concurrent captures
        from interleaving start/stop toggles (the trigger is a TOGGLE —
        unpaired toggles flip recordings on/off for the other caller)."""
        duration, err = coerce_number(duration_s, "duration_s", lo=0.5, hi=DEFAULT_VIDEO_MAX_DURATION_S)
        if err:
            return _fail(err)
        if duration != float(duration_s):
            return _fail(
                f"duration_s must be between 0.5 and {DEFAULT_VIDEO_MAX_DURATION_S:g} seconds, got {duration_s!r}"
            )
        timeout, err = coerce_number(
            timeout_s, "timeout_s", lo=1.0, hi=MAX_PHOTO_TIMEOUT_S, default=DEFAULT_PHOTO_TIMEOUT_S
        )
        if err:
            return _fail(err)
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))

        lock = self._capture_lock_for(manager)
        if not lock.acquire(blocking=False):
            return _fail(_BUSY_ERROR)
        try:
            status = manager.status()
            if status.get("movie_recording"):
                return _fail(
                    "The camera is already recording video — stop_recording() first."
                )
            if status.get("detection_mode") == "auto":
                # Auto-fire is a competing trigger initiator: a detection
                # mid-clip would TOGGLE the recording out from under this
                # bounded capture.
                return _fail(
                    "Detection auto-fire is armed — stop_detection() before a bounded "
                    "video capture (a detection would toggle the recording mid-clip)."
                )
            if int(status.get("downloads_pending") or 0) > 0:
                return _fail(
                    f"{status.get('downloads_pending')} earlier capture(s) are still downloading "
                    "from the camera — poll get_events until they land, then retry."
                )
            previous_mode = status.get("capture_mode")

            try:
                if previous_mode != "video":
                    manager.set_capture_mode("video")
                start_watermark = self._event_watermark(manager)
                manager.request_trigger()
            except CameraControlError as exc:
                return _fail(str(exc))

            started = self._wait_for_movie_state(manager, recording=True, since_id=start_watermark)
            if not started["success"]:
                self._restore_capture_mode(manager, previous_mode)
                return started

            time.sleep(duration)

            result = self._stop_and_collect(
                manager,
                timeout_s=timeout,
                download_locally=bool(status.get("download_locally", True)),
            )
            if result["success"]:
                result["duration_s"] = duration
            self._restore_capture_mode(manager, previous_mode)
            return result
        finally:
            lock.release()

    def stop_recording(self, camera: str | None = None, *, timeout_s: float | None = None) -> dict:
        """Stop a RUNNING video recording (one started by detection
        auto-fire in video mode, an external host, or a crashed bounded
        capture) and wait for the movie file. The escape hatch the
        adversarial review demanded: without it an orphaned recording had
        no stop surface at all."""
        timeout, err = coerce_number(
            timeout_s, "timeout_s", lo=1.0, hi=MAX_PHOTO_TIMEOUT_S, default=DEFAULT_STOP_RECORDING_TIMEOUT_S
        )
        if err:
            return _fail(err)
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))

        lock = self._capture_lock_for(manager)
        if not lock.acquire(blocking=False):
            # A bounded capture_video owns its own stop toggle; refusing
            # here prevents a double-stop (= restart) race.
            return _fail(_BUSY_ERROR)
        try:
            status = manager.status()
            if not status.get("movie_recording"):
                return _fail("No video recording is running on this camera.")
            return self._stop_and_collect(
                manager,
                timeout_s=timeout,
                download_locally=bool(status.get("download_locally", True)),
            )
        finally:
            lock.release()

    def _stop_and_collect(self, manager, *, timeout_s: float, download_locally: bool) -> dict:
        """Toggle a running recording OFF, confirm, and wait for the movie
        file — shared tail of capture_video and stop_recording (callers
        hold the capture lock)."""
        stop_watermark = self._event_watermark(manager)
        # The stop toggle is itself a trigger act; the movie file announces
        # at/after it, so files stamped below the stop act's seq are stale
        # backlog (same correlation rule as stills).
        pre_stop_seq = getattr(manager, "trigger_seq", 0)
        try:
            manager.request_trigger()
        except CameraControlError as exc:
            # The recording is running and the stop failed — that is a
            # real state the caller must know about, never mask it.
            return _fail(f"Video recording is running but could not be stopped: {exc}")

        stopped = self._wait_for_movie_state(manager, recording=False, since_id=stop_watermark)
        if not stopped["success"]:
            return stopped

        result = self._wait_for_capture(
            manager,
            watermark=stop_watermark,
            timeout_s=timeout_s,
            pending_meaning=None if download_locally else "on_device",
            what="video",
            min_trigger_id=int(pre_stop_seq) + 1,
        )
        if not result["success"] and result.get("timed_out"):
            # The recording genuinely happened (start AND stop confirmed);
            # file announcement is family-dependent — some PTP bodies keep
            # the movie on their card without an event (hardware-measured;
            # the simulator mirrors it). Report the recording honestly.
            # Branching on the SENTINEL, never on message text (adversarial
            # finding: a download-failure note containing "Timed out"
            # was converted into success by a substring match).
            movie_caps = (manager.status().get("capabilities") or {}).get("movie") or {}
            confirmed = bool(movie_caps.get("can_confirm", True))
            return _ok(
                {
                    "kind": "video",
                    "path": None,
                    "delivered": False,
                    "note": (
                        ("Recording started and stopped" if confirmed
                         else "Recording start/stop was accepted but is not confirmable over "
                              "this transport")
                        + ", and the camera never announced the movie file — it likely "
                        "resides on the camera's own storage. Use the device media "
                        "download (sync_store / `abstractcamera download`) to fetch it."
                    ),
                }
            )
        if result["success"]:
            result["delivered"] = result.get("path") is not None
        return result

    # ------------------------------------------------------------------
    # Detection (motion / lightning / meteor)
    # ------------------------------------------------------------------

    def start_detection(
        self,
        camera: str | None = None,
        *,
        target: str = "motion",
        action: str = "photo",
        sensitivity: float | None = None,
    ) -> dict:
        """Arm live-view detection.

        action="photo"/"video" = auto-fire that capture on detection;
        action="monitor" = log detections without firing. Capture mode is
        set BEFORE arming so the first detection fires the right thing;
        the capture lock is held for the mode write (P0 fix: an unguarded
        mode flip during a bounded video capture turned its stop toggle
        into a still trigger and stranded the recording)."""
        if target not in DETECTION_TARGETS:
            return _fail(f"Unknown detection target {target!r} — use one of {', '.join(DETECTION_TARGETS)}.")
        if action not in DETECTION_ACTIONS:
            return _fail(f"Unknown detection action {action!r} — use one of {', '.join(DETECTION_ACTIONS)}.")
        if sensitivity is not None:
            sensitivity, err = coerce_number(sensitivity, "sensitivity", lo=0.0, hi=100.0)
            if err:
                return _fail(err)
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))

        lock = self._capture_lock_for(manager)
        if not lock.acquire(blocking=False):
            return _fail(_BUSY_ERROR)
        try:
            if action != "monitor" and manager.status().get("movie_recording"):
                return _fail(
                    "The camera is recording video — stop_recording() before arming "
                    "auto-fire (its mode write would corrupt the running recording)."
                )
            try:
                if action == "photo":
                    manager.set_capture_mode("single")
                elif action == "video":
                    manager.set_capture_mode("video")
                mode = "monitor" if action == "monitor" else "auto"
                status = manager.set_detection_mode(mode, target=target, sensitivity=sensitivity)
            except CameraControlError as exc:
                return _fail(str(exc))
        finally:
            lock.release()

        note = None
        if action == "video":
            note = (
                "Video auto-fire toggles recording: the first detection starts the "
                "recording and a later detection (after the cooldown) stops it; "
                "stop_recording() ends it manually."
            )
        out = {
            "camera": camera or self._hub.active_uid,
            "detection_mode": status.get("detection_mode"),
            "detection_target": status.get("detection_target"),
            "detection_sensitivity": status.get("detection_sensitivity"),
            "capture_mode": status.get("capture_mode"),
            "event_watermark": self._event_watermark(manager),
        }
        if note:
            out["note"] = note
        return _ok(out)

    def stop_detection(self, camera: str | None = None) -> dict:
        """Disarm detection. Leaving auto-fire also flushes any downloads
        that were deferred while armed (CameraManager's contract). Writes
        no capture mode, so it stays lock-free (safe during captures)."""
        try:
            manager = self._hub.manager_for(camera or None)
            status = manager.set_detection_mode("off")
        except CameraControlError as exc:
            return _fail(str(exc))
        return _ok(
            {
                "camera": camera or self._hub.active_uid,
                "detection_mode": status.get("detection_mode"),
                "downloads_pending": status.get("downloads_pending"),
            }
        )

    def get_events(
        self,
        camera: str | None = None,
        *,
        since_id: int = 0,
        kinds: tuple[str, ...] | list[str] | None = None,
        limit: int = 100,
        include_thumbnails: bool = False,
    ) -> dict:
        """Catch-log events newer than `since_id`, oldest first. Poll this
        after `start_detection` — the returned `last_id` is the next call's
        `since_id`.

        WIRE CONTRACT (2026-07-21 — LLM/workflow consumers poll this as an
        API): event kinds are `detection` (a detector fired; `metrics`
        carries the machine-readable measurements), `trigger` (a capture act
        was issued), `photo` (a file landed locally; `path`), `photo-pending`
        (the shot exists on the camera — deferred or device-only save),
        `clip` (a rolling-ring clip was written), `camera-event` (device
        status notes), `error`. Cursor rules: `session` names the id space —
        it CHANGES when the camera reconnects and ids restart, so a consumer
        seeing a new session resets its cursor to 0. `evicted: true` means
        the bounded log dropped events between your cursor and
        `first_retained_id` (the ring holds ~minutes under busy auto-fire) —
        poll faster or accept the gap; the ids are contiguous, so gaps are
        also self-evident."""
        since, err = coerce_int(since_id, "since_id", lo=0, hi=2**63 - 1)
        if err:
            return _fail(err)
        page, err = coerce_int(limit, "limit", lo=1, hi=500)
        if err:
            return _fail(err)
        try:
            manager = self._hub.manager_for(camera or None)
        except CameraControlError as exc:
            return _fail(str(exc))
        events = manager.get_events(since_id=since)
        events.reverse()  # manager returns newest-first; chronological reads better
        if kinds:
            wanted = {str(k) for k in kinds}
            events = [e for e in events if e.get("kind") in wanted]
        truncated = len(events) > page
        if truncated:
            # Forward pagination: keep the OLDEST page — the returned
            # last_id cursor then reaches the rest on the next poll.
            # Keeping the newest would silently drop the middle forever.
            events = events[:page]
        last_id = events[-1]["id"] if events else since
        out = {
            "camera": camera or self._hub.active_uid,
            "events": [_public_event(e, include_thumbnail=include_thumbnails) for e in events],
            "last_id": last_id,
            "truncated": truncated,
        }
        # Cursor-contract fields (degrade gracefully for duck-typed test
        # managers that predate them).
        session = getattr(manager, "session_epoch", None)
        if session:
            out["session"] = session
        window = getattr(manager, "event_window", None)
        if callable(window):
            first_id, _last, counter = window()
            evicted = since > 0 and (
                (first_id is not None and first_id > since + 1)
                or (first_id is None and counter > since)
            )
            if evicted:
                out["evicted"] = True
                out["first_retained_id"] = first_id
        return _ok(out)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _event_watermark(manager) -> int:
        return _event_watermark_fn(manager)

    @staticmethod
    def _restore_capture_mode(manager, previous_mode) -> None:
        if previous_mode in ("single", "burst"):
            try:
                manager.set_capture_mode(previous_mode)
            except CameraControlError:
                pass  # cosmetic restore; the capture already succeeded

    @staticmethod
    def _wait_for_movie_state(manager, *, recording: bool, since_id: int) -> dict:
        return _wait_for_movie_state_fn(manager, recording=recording, since_id=since_id)

    @staticmethod
    def _wait_for_capture(manager, *, watermark: int, timeout_s: float, pending_meaning: str | None,
                          what: str, min_trigger_id: int | None = None) -> dict:
        return _wait_for_capture_fn(
            manager, watermark=watermark, timeout_s=timeout_s, pending_meaning=pending_meaning,
            what=what, min_trigger_id=min_trigger_id,
        )


# ---------------------------------------------------------------------------
# Process-wide shared service (hardware is a process-wide resource)
# ---------------------------------------------------------------------------

_shared_service: CameraService | None = None
_shared_service_lock = threading.Lock()
_shared_service_atexit_registered = False


def _release_shared_service_at_exit() -> None:
    """Best-effort clean release at interpreter exit, mirroring the legacy
    `get_default_manager()` hook: workers are daemon threads, so without
    this a routine host restart (gateway/server processes restart OFTEN in
    this stack) leaves the camera claimed — or RECORDING until the card
    fills — and strands deferred downloads (adversarial finding 2026-07-21).
    close_all() runs each manager's disconnect contract (flush deferred
    downloads, stop recordings, release the transport) with its bounded
    worker join. SIGKILL still loses; this covers the clean-exit/SIGTERM-
    handler case, which is the routine one."""
    service = _shared_service
    if service is None:
        return
    try:
        service.close_all()
    except Exception:
        pass


def get_shared_service() -> CameraService:
    """The process-wide CameraService. The AbstractCore capability plugin
    and the AI tool set both resolve here so every surface addresses the
    same camera sessions (claiming one camera from two hubs wedges the
    transport — sharing is correctness, not convenience)."""
    global _shared_service, _shared_service_atexit_registered
    with _shared_service_lock:
        if _shared_service is None:
            _shared_service = CameraService()
            # Registered once per process (the hook reads the CURRENT global
            # at exit time, so test-seam resets never stack callbacks).
            if not _shared_service_atexit_registered:
                atexit.register(_release_shared_service_at_exit)
                _shared_service_atexit_registered = True
        return _shared_service


def reset_shared_service() -> None:
    """Test seam: drop the shared service (closing its cameras). Production
    code has no reason to call this."""
    global _shared_service
    with _shared_service_lock:
        service, _shared_service = _shared_service, None
    if service is not None:
        service.close_all()
