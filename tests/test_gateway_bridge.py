"""Camera → gateway event bridge (the sentinel lane, backlog 0016).

Three lanes:
- Wire shape: GatewayEmitter posts the exact emit_event command contract
  (type, durable, global scope, derived idempotent command_id) against a
  real local HTTP server.
- Loop semantics: cursor advance/persist, session-epoch reset, kind
  filtering, at-least-once on gateway failure — against stub services.
- End-to-end over the simulator: real CameraService events flow through
  the bridge into a captured envelope list.
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from abstractcamera.gateway_bridge import (
    CameraEventBridge,
    CursorStore,
    GatewayEmitter,
)


class _RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.received.append((self.path, dict(self.headers), body))
        payload = json.dumps({"accepted": True, "duplicate": False, "seq": len(self.server.received)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *args):  # keep test output clean
        pass


class EmitterWire(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.server.received = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def test_emit_posts_the_command_contract(self):
        emitter = GatewayEmitter(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            mailbox="camera", token="tok-123",
        )
        event = {"id": 7, "kind": "detection", "reason": "motion", "note": "x",
                 "metrics": {"fraction_pct": 3.2}, "trigger_id": None}
        response = emitter.emit(camera="nikon_z_6ii", session="abc123", event=event)
        self.assertTrue(response["accepted"])

        path, headers, body = self.server.received[0]
        self.assertEqual(path, "/api/gateway/commands")
        self.assertEqual(headers.get("Authorization"), "Bearer tok-123")
        self.assertEqual(body["type"], "emit_event")
        self.assertEqual(body["run_id"], "camera")
        payload = body["payload"]
        self.assertEqual(payload["name"], "camera")
        self.assertEqual(payload["scope"], "global")
        self.assertEqual(payload["session_id"], "camera")
        self.assertTrue(payload["durable"], "wakes must not drop while the resident is busy")
        self.assertEqual(payload["event_id"], "cam:nikon_z_6ii:abc123:7")
        inner = payload["payload"]
        self.assertEqual(inner["source"], "abstractcamera")
        self.assertEqual(inner["camera"], "nikon_z_6ii")
        self.assertEqual(inner["event"]["metrics"], {"fraction_pct": 3.2})

    def test_command_id_is_deterministic_and_event_scoped(self):
        emitter = GatewayEmitter("http://example.invalid", mailbox="camera")
        a = emitter.command_id_for("cam", "s1", 7)
        self.assertEqual(a, emitter.command_id_for("cam", "s1", 7),
                         "a crash-replayed emit must reuse the same command id")
        self.assertNotEqual(a, emitter.command_id_for("cam", "s1", 8))
        self.assertNotEqual(a, emitter.command_id_for("cam", "s2", 7),
                            "a new session epoch is a new id space")


class _StubService:
    """Duck-typed CameraService: a scripted event log per camera."""

    def __init__(self, session="epoch1"):
        self.session = session
        self.events = []  # oldest first, service-shaped (dicts with id/kind)
        self.evicted = False

    def status(self):
        return {"success": True, "cameras": {"cam": {"connected": True}}}

    def get_events(self, camera, *, since_id=0, limit=100, kinds=None, include_thumbnails=False):
        page = [dict(e) for e in self.events if e["id"] > since_id][:limit]
        out = {"success": True, "camera": camera, "events": page,
               "last_id": page[-1]["id"] if page else since_id,
               "truncated": False, "session": self.session}
        if self.evicted:
            out["evicted"] = True
            out["first_retained_id"] = self.events[0]["id"] if self.events else None
        return out


class _StubEmitter:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.mailbox = "camera"
        self.base_url = "stub://gateway"

    def emit(self, *, camera, session, event):
        if self.fail:
            raise OSError("gateway unreachable")
        self.sent.append((camera, session, event))
        return {"accepted": True, "duplicate": False, "seq": len(self.sent)}


class BridgeLoop(unittest.TestCase):
    def test_forwards_matching_kinds_and_advances_cursor(self):
        service = _StubService()
        service.events = [
            {"id": 1, "kind": "trigger", "reason": "manual"},   # filtered out
            {"id": 2, "kind": "detection", "reason": "motion"},
            {"id": 3, "kind": "photo", "reason": "captured", "path": "/tmp/a.jpg"},
        ]
        emitter = _StubEmitter()
        bridge = CameraEventBridge(service, emitter, cameras=["cam"],
                                   cursor_store=CursorStore(None))
        sent = bridge.poll_once()
        self.assertEqual(sent, 2, "trigger events are polling noise, not wakes")
        self.assertEqual([e["id"] for _, _, e in emitter.sent], [2, 3])
        # Cursor advanced past ALL seen events (filtered ones included).
        self.assertEqual(bridge.poll_once(), 0, "nothing may re-emit")

    def test_session_epoch_change_resets_cursor(self):
        service = _StubService(session="epoch1")
        service.events = [{"id": 1, "kind": "photo", "reason": "captured"}]
        emitter = _StubEmitter()
        with tempfile.TemporaryDirectory() as tmp:
            store = CursorStore(f"{tmp}/state.json")
            bridge = CameraEventBridge(service, emitter, cameras=["cam"], cursor_store=store)
            self.assertEqual(bridge.poll_once(), 1)
            # Camera reconnects: new epoch, ids restart at 1.
            service.session = "epoch2"
            service.events = [{"id": 1, "kind": "photo", "reason": "captured"}]
            self.assertEqual(bridge.poll_once(), 1,
                             "a new session epoch must reset the cursor, not hide events")

    def test_cursor_persists_across_bridge_restarts(self):
        service = _StubService()
        service.events = [{"id": 1, "kind": "photo", "reason": "captured"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/state.json"
            bridge = CameraEventBridge(service, _StubEmitter(), cameras=["cam"],
                                       cursor_store=CursorStore(path))
            self.assertEqual(bridge.poll_once(), 1)
            # New process: fresh objects over the same state file.
            emitter2 = _StubEmitter()
            bridge2 = CameraEventBridge(service, emitter2, cameras=["cam"],
                                        cursor_store=CursorStore(path))
            self.assertEqual(bridge2.poll_once(), 0,
                             "a restarted bridge must resume, not re-emit history")

    def test_gateway_failure_holds_the_cursor(self):
        service = _StubService()
        service.events = [{"id": 1, "kind": "photo", "reason": "captured"}]
        emitter = _StubEmitter(fail=True)
        bridge = CameraEventBridge(service, emitter, cameras=["cam"],
                                   cursor_store=CursorStore(None))
        with self.assertRaises(OSError):
            bridge.poll_once()
        emitter.fail = False
        self.assertEqual(bridge.poll_once(), 1,
                         "the event must deliver after the gateway recovers")


class EndToEndOverSimulator(unittest.TestCase):
    def test_real_service_events_flow_through_the_bridge(self):
        import abstractcamera.sim.gphoto2 as fake_gp
        from abstractcamera.camera_manager import CameraManager
        from abstractcamera.drivers.fake_driver import FakeDriver
        from abstractcamera.hub import CameraHub
        from abstractcamera.service import CameraService

        fake_gp.reset()
        fake_gp.configure(download_stall_s=(0.01, 0.03), trigger_latency_s=0.05,
                          trigger_latency_jitter_s=0.02, file_added_offset_s=0.1)
        root = tempfile.mkdtemp(prefix="bridge_e2e_")
        hub = CameraHub(capture_root=root,
                        manager_factory=lambda: CameraManager(driver=FakeDriver(fake_gp)))
        service = CameraService(hub=hub)
        self.addCleanup(service.close_all)
        self.addCleanup(fake_gp.reset)

        opened = service.open()
        self.assertTrue(opened["success"])
        camera = opened["camera"]
        captured = service.capture_photo(camera)
        self.assertTrue(captured["success"], captured)

        emitter = _StubEmitter()
        bridge = CameraEventBridge(service, emitter, cameras=[camera],
                                   cursor_store=CursorStore(None))
        sent = bridge.poll_once()
        self.assertGreaterEqual(sent, 1)
        kinds = {e["kind"] for _, _, e in emitter.sent}
        self.assertIn("photo", kinds)
        sessions = {s for _, s, _ in emitter.sent}
        self.assertEqual(len(sessions), 1)
        self.assertNotIn("no-epoch", sessions, "the real service must serve the epoch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
