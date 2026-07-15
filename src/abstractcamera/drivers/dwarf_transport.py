"""DWARF network transport: control-plane WebSocket + album REST + RTSP.

One DwarfTransport owns every network surface of one DWARF unit:

- WebSocket (port 9900): protobuf commands and notifications (dwarf_wire).
  A private reader thread routes request responses to waiting callers and
  queues notifications for the session to drain on the worker thread.
- HTTP (port 8082): the album index (`/album/list/mediaInfos`) and the
  parameter tables (`/getDefaultParamsConfig` — exposure/gain gear names).
- HTTP (port 8092): media file downloads by album `filePath`.
- RTSP (port 554): live view frames (`/ch0/stream0` tele, `/ch1/stream0`
  wide), read with OpenCV (FFmpeg backend) — cv2 is already a base dep.

The network dependency (`websocket-client`) is an EXTRA (ADR 0003:
transports are extras); its absence raises an actionable install hint at
connect time, never at import time.

THREADING: public send/HTTP methods are called from the manager's worker
thread only (session contract). The reader thread is transport-private:
it touches only the pending-request table (lock-guarded) and the
notification deque (thread-safe append; drained by the worker).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
from collections import deque

from abstractcamera.drivers import dwarf_wire as wire3
from abstractcamera.errors import CameraControlError

# The DWARF grants ONE master controller at a time, negotiated by ws client
# id. This id is the DWARF 3 service id used by existing tooling; override
# with ABSTRACTCAMERA_DWARF_CLIENT_ID for hardware variants.
DEFAULT_CLIENT_ID = "0000DAF3-0000-1000-8000-00805F9B34FB"

AP_MODE_HOST = "192.168.88.1"  # fixed device IP when you join the DWARF's own Wi-Fi

WS_PORT = 9900
API_PORT = 8082
MEDIA_PORT = 8092
RTSP_PORT = 554

_RECV_IDLE_S = 0.5          # reader wake period (checks for shutdown)
_NOTIFICATION_BACKLOG = 256  # bounded: old unread notifications drop oldest


class _PendingRequest:
    __slots__ = ("event", "response", "error", "keys")

    def __init__(self, keys: tuple):
        self.event = threading.Event()
        self.response: dict | None = None
        self.error: str | None = None
        self.keys = keys  # every (module_id, cmd) that may answer this request


class DwarfTransport:
    """Every network surface of one DWARF unit (see module docstring)."""

    def __init__(self, host: str, *, client_id: str | None = None,
                 ws_port: int = WS_PORT, api_port: int = API_PORT,
                 media_port: int = MEDIA_PORT, rtsp_port: int = RTSP_PORT,
                 timeout_s: float = 10.0):
        self.host = host
        self.client_id = (client_id
                          or os.environ.get("ABSTRACTCAMERA_DWARF_CLIENT_ID")
                          or DEFAULT_CLIENT_ID)
        self.ws_port = ws_port
        self.api_port = api_port
        self.media_port = media_port
        self.rtsp_port = rtsp_port
        self.timeout_s = timeout_s

        self._ws = None
        self._reader: threading.Thread | None = None
        self._closing = threading.Event()
        self._pending: dict[tuple[int, int], _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self.notifications: deque = deque(maxlen=_NOTIFICATION_BACKLOG)

    # -- probing (non-invasive: a TCP connect, no device state touched) -------
    @staticmethod
    def probe(host: str, *, port: int = WS_PORT, timeout_s: float = 1.0) -> bool:
        sock = socket.socket()
        sock.settimeout(timeout_s)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    # -- websocket lifecycle ----------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None and getattr(self._ws, "connected", False)

    def connect(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            raise CameraControlError(
                "DWARF control needs the websocket transport — "
                "pip install abstractcamera[dwarf]"
            )
        try:
            ws = websocket.WebSocket()
            ws.connect(f"ws://{self.host}:{self.ws_port}/", timeout=self.timeout_s)
        except Exception as exc:
            raise CameraControlError(
                f"Could not reach the DWARF at {self.host}:{self.ws_port} — "
                f"check that it is powered on and on this network ({exc})"
            )
        ws.settimeout(_RECV_IDLE_S)
        self._ws = ws
        self._closing.clear()
        self._reader = threading.Thread(target=self._reader_main,
                                        name="dwarf-ws-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closing.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        reader = self._reader
        if reader is not None:
            reader.join(timeout=3.0)
            self._reader = None
        self._flush_pending("connection closed")

    # -- request/response --------------------------------------------------------
    def send_request(self, module_id: int, cmd: int, payload: bytes = b"", *,
                     response_spec: dict | None = None,
                     alias_keys: tuple = (),
                     timeout_s: float | None = None) -> dict:
        """Send one command and wait for its response packet.

        `alias_keys`: extra (module_id, cmd) keys that may carry the answer —
        some replies arrive under the NOTIFY module (e.g. host/slave mode
        answering a master-lock request). Returns the decoded response dict
        with `_module_id`/`_cmd` annotations.
        """
        if self._ws is None:
            raise CameraControlError("The DWARF connection is closed.")
        keys = ((module_id, cmd),) + tuple(alias_keys)
        pending = _PendingRequest(keys)
        with self._pending_lock:
            for key in keys:
                if key in self._pending:
                    raise CameraControlError(
                        f"another DWARF request for command {key[1]} is already pending")
            for key in keys:
                self._pending[key] = pending
        frame = wire3.encode_packet(module_id, cmd, payload, client_id=self.client_id)
        try:
            self._ws.send_binary(frame)
        except Exception as exc:
            self._unregister(pending)
            raise CameraControlError(f"Sending to the DWARF failed: {exc}")
        if not pending.event.wait(timeout=timeout_s or self.timeout_s):
            self._unregister(pending)
            raise CameraControlError(
                f"The DWARF did not answer command {cmd} within "
                f"{timeout_s or self.timeout_s:.0f}s.")
        if pending.error:
            raise CameraControlError(f"DWARF connection error: {pending.error}")
        return pending.response or {}

    def send_command(self, module_id: int, cmd: int, spec: dict | None = None,
                     values: dict | None = None, *,
                     timeout_s: float | None = None) -> int:
        """Send a command whose response is ComResponse; returns its code."""
        payload = wire3.encode_message(spec or {}, values or {})
        response = self.send_request(module_id, cmd, payload, timeout_s=timeout_s)
        data = response.get("data", b"")
        decoded = wire3.decode_message(wire3.COM_RESPONSE, data) if data else {"code": 0}
        return int(decoded.get("code", 0))

    def _unregister(self, pending: _PendingRequest) -> None:
        with self._pending_lock:
            for key in pending.keys:
                if self._pending.get(key) is pending:
                    del self._pending[key]

    def _flush_pending(self, reason: str) -> None:
        with self._pending_lock:
            pendings, self._pending = set(self._pending.values()), {}
        for pending in pendings:
            pending.error = reason
            pending.event.set()

    # -- reader thread -------------------------------------------------------------
    def _reader_main(self) -> None:
        while not self._closing.is_set():
            ws = self._ws
            if ws is None:
                return
            try:
                frame = ws.recv()
            except Exception as exc:
                # settimeout() idles surface as WebSocketTimeoutException —
                # keep listening; anything else is a dead connection.
                if type(exc).__name__ == "WebSocketTimeoutException":
                    continue
                if not self._closing.is_set():
                    self._flush_pending(str(exc) or type(exc).__name__)
                return
            if isinstance(frame, str):
                continue  # the control plane is binary-only
            if not frame:
                self._flush_pending("connection closed by the DWARF")
                return
            try:
                packet = wire3.decode_packet(frame)
            except ValueError:
                continue  # unreadable frame: skip, keep the connection
            self._dispatch(packet)

    def _dispatch(self, packet: dict) -> None:
        key = (int(packet.get("module_id", 0)), int(packet.get("cmd", 0)))
        packet["_module_id"], packet["_cmd"] = key
        with self._pending_lock:
            pending = self._pending.get(key)
            if pending is not None:
                for pending_key in pending.keys:
                    if self._pending.get(pending_key) is pending:
                        del self._pending[pending_key]
        if pending is not None:
            pending.response = packet
            pending.event.set()
            # Host/slave answers double as broadcasts; fall through so state
            # watchers see them too.
            if key[1] != wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE:
                return
        if int(packet.get("type", 0)) == wire3.TYPE_NOTIFICATION or pending is not None:
            self.notifications.append(packet)

    # -- album / params REST -----------------------------------------------------
    def _http_json(self, url: str, payload: dict | None = None) -> dict:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            raise CameraControlError(f"DWARF HTTP request failed ({url}): {exc}")

    def album_media_infos(self, *, media_type: int = 0, page_index: int = 0,
                          page_size: int = 8) -> list[dict]:
        """Newest-first album entries (fileName/filePath/modificationTime)."""
        payload = {"mediaType": media_type, "pageIndex": page_index,
                   "pageSize": page_size}
        url = f"http://{self.host}:{self.api_port}/album/list/mediaInfos"
        response = self._http_json(url, payload)
        data = response.get("data")
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, dict):  # firmware variants nest the list
            for value in data.values():
                if isinstance(value, list):
                    return [entry for entry in value if isinstance(entry, dict)]
        return []

    def default_params_config(self) -> dict:
        """The device's own parameter tables (exposure/gain gears, filters)."""
        url = f"http://{self.host}:{self.api_port}/getDefaultParamsConfig"
        return self._http_json(url)

    def fetch_media(self, file_path: str) -> bytes:
        """Download one album file by its device filePath."""
        path = "/" + file_path.strip().lstrip("/")
        last_error: Exception | None = None
        # 8092 serves media; some firmware revisions serve paths on 8082 too.
        for port in (self.media_port, self.api_port):
            url = f"http://{self.host}:{port}{path}"
            try:
                with urllib.request.urlopen(url, timeout=max(self.timeout_s, 30.0)) as response:
                    return response.read()
            except Exception as exc:
                last_error = exc
        raise CameraControlError(
            f"Downloading {file_path} from the DWARF failed: {last_error}")

    # -- live view ---------------------------------------------------------------
    def rtsp_url(self, channel: int = 0) -> str:
        return f"rtsp://{self.host}:{self.rtsp_port}/ch{channel}/stream0"


class RtspFrameSource:
    """Live-view frames from the DWARF's RTSP stream (OpenCV/FFmpeg).

    read() RAISES when no frame arrives — the manager's liveness watchdog
    counts raises (session contract item 2); returning stale frames would
    silently disarm it.
    """

    def __init__(self, url: str):
        self._url = url
        self._capture = None

    def open(self) -> None:
        import cv2

        # FFmpeg over TCP: the DWARF's Wi-Fi drops too many UDP packets for
        # clean frames; TCP interleave is the reliable transport here.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        capture = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
        if not capture.isOpened():
            capture.release()
            raise CameraControlError(
                f"The DWARF live stream did not open ({self._url}) — "
                "the camera may still be starting its video pipeline."
            )
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 2)  # keep preview near-live
        except Exception:
            pass
        self._capture = capture

    def read(self):
        if self._capture is None:
            raise CameraControlError("The DWARF live stream is closed.")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise CameraControlError("No frame from the DWARF live stream.")
        return frame

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def configured_hosts() -> list[str]:
    """Hosts named by ABSTRACTCAMERA_DWARF_HOSTS (comma-separated). AP mode
    users list the fixed device IP: export ABSTRACTCAMERA_DWARF_HOSTS=192.168.88.1"""
    raw = os.environ.get("ABSTRACTCAMERA_DWARF_HOSTS", "")
    return [host.strip() for host in raw.split(",") if host.strip()]
