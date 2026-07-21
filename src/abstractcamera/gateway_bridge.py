"""Camera → AbstractGateway event bridge: the camera as a WAKE SOURCE.

`abstractcamera watch` runs a standalone SENTINEL process that owns one
camera session end to end: open the camera, arm detection, and forward
catch-log events as durable gateway events (`POST /api/gateway/commands`,
type=emit_event, `durable: true`). Parked workflows/entities declaring the
mailbox (`events_mailbox` run var, the resident-agent drain contract) WAKE
on movement instead of burning model turns polling `camera_get_events` —
the emitted payload carries capture PATHS + detection metrics, so a woken
agent can read/analyze the file without holding the camera session.

Design constraints this shape respects:
- Camera sessions are PROCESS-LOCAL (a device claimed twice wedges the
  transport), so the sentinel OWNS its session; it never tries to observe
  a camera another process opened. Agent-held cameras inside a gateway
  process are a future host-side pump (the gateway ruled itself
  zero-camera-code; that lane needs its own design).
- Delivery is exactly-once AT THE GATEWAY: the command_id is DERIVED from
  (mailbox, camera, session epoch, event id) — the gateway's command-store
  idempotency turns a crash-replayed post into `duplicate: true`, never a
  double wake. Cursors persist to a state file so restarts resume where
  they left off; the event-log session epoch (wire contract 2026-07-21)
  resets cursors honestly when the camera reconnects.
- The gateway accepts commands ASYNCHRONOUSLY (queued, applied at the tick
  loop) — `accepted: true` means durably queued, not yet delivered; the
  runner's durable-mailbox append happens when the command applies.

Stdlib-only HTTP (urllib): the bridge must not drag client dependencies
into abstractcamera's base install.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request

# Event kinds forwarded by default: the sentinel's consumers care about
# what was SEEN and what files LANDED — trigger bookkeeping and device
# status notes are polling noise at the mailbox.
DEFAULT_FORWARD_KINDS = ("detection", "photo", "photo-pending", "clip", "error")

# The default cursor file is PER-MAILBOX: two sentinels on different
# mailboxes never share a file (CursorStore is whole-file replace — a
# shared file would last-writer-lose the other's cursors; adversarial P2
# 2026-07-21). Two sentinels on the SAME mailbox must pass distinct
# --state-file paths (documented limitation; the gateway's command
# idempotency bounds the damage to replayed duplicates either way).
DEFAULT_STATE_PATH_TEMPLATE = "~/.abstractcamera/gateway_bridge_{mailbox}.json"


def default_state_path(mailbox: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in mailbox) or "camera"
    return DEFAULT_STATE_PATH_TEMPLATE.format(mailbox=safe)


class GatewayEmitter:
    """Posts emit_event commands to a gateway; owns auth + idempotency.

    Split from the bridge loop so tests exercise the wire shape against a
    stub HTTP server and the loop against a stub emitter."""

    def __init__(self, base_url: str, *, mailbox: str, token: str | None = None,
                 timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.mailbox = mailbox
        self._token = token or os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN") or None
        self._timeout_s = timeout_s

    def command_id_for(self, camera: str, session: str, event_id: int) -> str:
        """Deterministic idempotency key: one camera event = one gateway
        command, across bridge restarts and crash replays."""
        raw = f"camera-bridge:{self.mailbox}:{camera}:{session}:{event_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def emit(self, *, camera: str, session: str, event: dict) -> dict:
        """POST one event envelope. Returns the gateway's response dict
        ({accepted, duplicate, seq}); raises on transport/HTTP errors so
        the caller's cursor never advances past an undelivered event."""
        envelope = {
            "command_id": self.command_id_for(camera, session, int(event["id"])),
            "run_id": self.mailbox,  # emit_event: run_id doubles as the session anchor
            "type": "emit_event",
            "payload": {
                "name": self.mailbox,
                "scope": "global",  # rendezvous by mailbox name alone
                "session_id": self.mailbox,
                "durable": True,
                "event_id": f"cam:{camera}:{session}:{event['id']}",
                "payload": {
                    "source": "abstractcamera",
                    "camera": camera,
                    "session": session,
                    "event": event,
                },
            },
        }
        data = json.dumps(envelope).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/gateway/commands",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


class CursorStore:
    """Per-(camera, session) event cursors, persisted as one small JSON file
    so a restarted bridge resumes instead of re-emitting history. Writes are
    atomic (tmp + replace); a corrupt/missing file degrades to empty with a
    warning — the gateway-side command idempotency absorbs any replay."""

    def __init__(self, path: str | None):
        self._path = os.path.expanduser(path) if path else None
        self._cursors: dict[str, dict] = {}
        if self._path and os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._cursors = {str(k): dict(v) for k, v in loaded.items()
                                     if isinstance(v, dict)}
            except Exception as exc:
                print(f"#FALLBACK bridge cursor state unreadable ({exc}); starting fresh "
                      "(gateway command idempotency absorbs any replay)")

    def get(self, camera: str, session: str) -> int:
        entry = self._cursors.get(camera)
        if not entry or entry.get("session") != session:
            return 0  # new camera or new session epoch: cursor resets
        return int(entry.get("last_id") or 0)

    def set(self, camera: str, session: str, last_id: int) -> None:
        self._cursors[camera] = {"session": session, "last_id": int(last_id)}
        if not self._path:
            return
        try:
            # dirname of a bare relative filename is "" — makedirs("")
            # raises and the cursor silently never persisted (adversarial
            # P2 2026-07-21).
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cursors, fh)
            os.replace(tmp, self._path)
        except Exception as exc:
            print(f"#FALLBACK bridge cursor state not persisted ({exc}); "
                  "a restart may replay events (deduplicated gateway-side)")


class CameraEventBridge:
    """The sentinel loop: poll the local CameraService event log, forward
    matching events to the gateway mailbox, persist cursors.

    The bridge does NOT open cameras — the CLI wrapper owns session
    lifecycle (open/arm/close) so the loop stays testable with stub
    services and emitters."""

    def __init__(self, service, emitter: GatewayEmitter, *,
                 cameras: list[str] | None = None,
                 kinds: tuple[str, ...] = DEFAULT_FORWARD_KINDS,
                 poll_interval_s: float = 1.0,
                 cursor_store: CursorStore | None = None):
        self._service = service
        self._emitter = emitter
        self._cameras = list(cameras) if cameras else None  # None = all live
        self._kinds = tuple(kinds)
        self._poll_interval_s = max(0.1, float(poll_interval_s))
        self._cursors = cursor_store or CursorStore(None)
        self._stop = threading.Event()
        self.forwarded = 0  # observability: total events delivered

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        # A dead gateway raises out of poll_once and the cursor never
        # advances past the undelivered event (at-least-once). The error
        # print is debounced — one line per minute, not one per poll.
        last_error_print = 0.0
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # the loop must survive anything transient
                now = time.time()
                if now - last_error_print >= 60.0:
                    print(self._describe_poll_failure(exc))
                    last_error_print = now
            self._stop.wait(self._poll_interval_s)

    def _describe_poll_failure(self, exc: Exception) -> str:
        """Honest failure text: auth/route refusals are NOT transient —
        'retrying, nothing lost' on a 401 reassures the operator while
        nothing will EVER deliver until the token is fixed (adversarial
        P2, 2026-07-21)."""
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return (f"#FALLBACK bridge REFUSED by the gateway (HTTP {code}) — this will not "
                    "recover on its own: check --token / ABSTRACTGATEWAY_AUTH_TOKEN "
                    "(cursor holds; events deliver once auth is fixed)")
        if code == 404:
            return ("#FALLBACK bridge target not found (HTTP 404) — check --gateway URL "
                    "(expected the AbstractGateway API root, e.g. http://127.0.0.1:8080)")
        return (f"#FALLBACK bridge poll failed ({exc}); retrying every "
                f"{self._poll_interval_s:g}s (cursor holds, nothing lost)")

    def poll_once(self) -> int:
        """One poll cycle over the addressed cameras. Returns events
        forwarded this cycle."""
        sent = 0
        for camera in self._target_cameras():
            sent += self._forward_camera(camera)
        return sent

    # -- internals ---------------------------------------------------------

    def _target_cameras(self) -> list[str]:
        if self._cameras:
            return self._cameras
        status = self._service.status()
        if not status.get("success"):
            return []
        cameras = status.get("cameras") or {}
        return [uid for uid, s in cameras.items() if s.get("connected")]

    def _forward_camera(self, camera: str) -> int:
        # Session first: the cursor is only meaningful inside one epoch.
        result = self._service.get_events(camera, since_id=0, limit=1)
        if not result.get("success"):
            return 0
        session = str(result.get("session") or "")
        if not session:
            # Pre-contract manager (duck-typed test stub): fall back to a
            # stable pseudo-session so cursors still work.
            session = "no-epoch"
        since = self._cursors.get(camera, session)

        page = self._service.get_events(camera, since_id=since, limit=100)
        if not page.get("success"):
            return 0
        if page.get("evicted"):
            print(f"#FALLBACK bridge missed events on {camera}: log evicted past "
                  f"cursor {since} (first retained {page.get('first_retained_id')}) — "
                  "gap forwarded as-is; poll interval may be too slow for this scene")
        sent = 0
        cursor = since
        for event in page.get("events") or []:
            event_id = int(event["id"])
            if event.get("kind") in self._kinds:
                # Raises on transport/HTTP failure — the cursor then stays
                # BEFORE this event, so the retry re-emits it (at-least-once
                # toward the gateway; exactly-once past its command store via
                # the derived command_id).
                self._emitter.emit(camera=camera, session=session, event=event)
                sent += 1
                self.forwarded += 1
            cursor = max(cursor, event_id)
            self._cursors.set(camera, session, cursor)
        return sent


def run_watch_cli(args) -> int:
    """`abstractcamera watch` — open, arm, forward, and clean up honestly."""
    from abstractcamera.service import get_shared_service

    service = get_shared_service()

    opened = service.open(getattr(args, "camera_id", None) or None)
    if not opened.get("success"):
        print(f"watch: cannot open the camera: {opened.get('error')}")
        return 1
    camera = opened["camera"]
    print(f"watch: camera open — {camera}")

    detect = getattr(args, "detect", None)
    if detect:
        armed = service.start_detection(
            camera,
            target=detect,
            action=getattr(args, "action", "photo") or "photo",
            sensitivity=getattr(args, "sensitivity", None),
        )
        if not armed.get("success"):
            print(f"watch: cannot arm detection: {armed.get('error')}")
            service.close(camera)
            return 1
        print(f"watch: detection armed — {detect} → {getattr(args, 'action', 'photo')}")

    emitter = GatewayEmitter(
        getattr(args, "gateway", None) or "http://127.0.0.1:8080",
        mailbox=getattr(args, "mailbox", None) or "camera",
        token=getattr(args, "token", None),
    )
    # Whitespace-tolerant kinds parsing: "detection, photo" must mean
    # detection+photo, not detection+nothing (adversarial P2 2026-07-21 —
    # the unstripped " photo" matched no kind, silently).
    kinds_raw = getattr(args, "kinds", None) or ",".join(DEFAULT_FORWARD_KINDS)
    kinds = tuple(k.strip() for k in kinds_raw.split(",") if k.strip())
    bridge = CameraEventBridge(
        service,
        emitter,
        cameras=[camera],
        kinds=kinds,
        poll_interval_s=getattr(args, "poll_interval", 1.0) or 1.0,
        cursor_store=CursorStore(
            getattr(args, "state_file", None) or default_state_path(emitter.mailbox)),
    )

    def _stop_signal(_signum, _frame):
        print("watch: stopping…")
        bridge.request_stop()

    signal.signal(signal.SIGINT, _stop_signal)
    signal.signal(signal.SIGTERM, _stop_signal)

    print(f"watch: forwarding {','.join(bridge._kinds)} events to "
          f"{emitter.base_url} mailbox '{emitter.mailbox}' — Ctrl-C to stop")
    try:
        bridge.run_forever()
    finally:
        # Leave the hardware clean: disarm + close even on an exception path
        # (the recording/claimed-camera lesson from the atexit finding).
        if detect:
            service.stop_detection(camera)
        service.close(camera)
        print(f"watch: closed {camera}; {bridge.forwarded} event(s) forwarded")
    return 0


__all__ = [
    "CameraEventBridge",
    "CursorStore",
    "DEFAULT_FORWARD_KINDS",
    "GatewayEmitter",
    "default_state_path",
    "run_watch_cli",
]
