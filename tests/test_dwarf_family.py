"""DWARF family: wire codec vectors, session behavior against a scripted
FakeDwarfTransport (no hardware, no network — ADR 0007), adapter action
mapping (mount GOTO/joystick as family actions), driver configuration, and
the full manager loop (connect, frames, album-announced captures, mount
action through request_action)."""

import os
import tempfile
import time
import unittest
from collections import deque
from unittest import mock

import cv2
import numpy as np

from abstractcamera import CameraManager, wire
from abstractcamera.adapters.dwarf import DwarfAdapter
from abstractcamera.drivers import dwarf_wire as wire3
from abstractcamera.drivers.dwarf_driver import DwarfDriver
from abstractcamera.drivers.dwarf_session import DwarfSession
from abstractcamera.errors import CameraControlError

HOST = "198.51.100.23"  # documentation range (RFC 5737): never a real device


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def tiny_jpeg(level: int = 128) -> bytes:
    frame = np.full((48, 64, 3), level, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


PARAMS_CONFIG = {
    "code": 0,
    "data": {
        "cameras": [
            {
                "name": "Tele Camera",
                "supportParams": [
                    {"name": "Exposure",
                     "gearMode": {"values": [
                         {"index": 0, "name": "1/2000"},
                         {"index": 60, "name": "1"},
                         {"index": 72, "name": "15"},
                     ]}},
                    {"name": "Gain",
                     "gearMode": {"values": [
                         {"index": 0, "name": "0"},
                         {"index": 30, "name": "80"},
                     ]}},
                ],
            },
            {"name": "Wide Camera", "supportParams": [
                {"name": "Exposure",
                 "gearMode": {"values": [{"index": 0, "name": "1/8000"}]}},
            ]},
        ],
    },
}


class FakeDwarfTransport:
    """Scripted DwarfTransport double: same surface, no network. Commands
    are recorded; the album and notification stream are test-driven."""

    def __init__(self, host, *, master: str = "granted"):
        self.host = host
        self.master = master
        self.notifications = deque()
        self.commands: list[tuple[int, int, dict]] = []
        self.requests: list[tuple[int, int]] = []
        self.album: list[dict] = []
        self.media: dict[str, bytes] = {}
        self.codes: dict[int, int] = {}
        self.params_payload: dict = PARAMS_CONFIG
        self.album_error = False
        self.connected = False

    # -- lifecycle --
    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    # -- control plane --
    def send_request(self, module_id, cmd, payload=b"", *, response_spec=None,
                     alias_keys=(), timeout_s=None):
        self.requests.append((module_id, cmd))
        if cmd == wire3.CMD_SYSTEM_SET_MASTERLOCK and self.master != "granted":
            return {
                "_module_id": wire3.MODULE_NOTIFY,
                "_cmd": wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE,
                "data": wire3.encode_message(wire3.RES_NOTIFY_HOST_SLAVE_MODE,
                                             {"mode": 1, "lock": False}),
            }
        return {"_module_id": module_id, "_cmd": cmd,
                "data": wire3.encode_message(wire3.COM_RESPONSE, {"code": 0})}

    def send_command(self, module_id, cmd, spec=None, values=None, *, timeout_s=None):
        self.commands.append((module_id, cmd, dict(values or {})))
        return self.codes.get(cmd, 0)

    # -- REST --
    def album_media_infos(self, *, media_type=0, page_index=0, page_size=8):
        if self.album_error:
            raise CameraControlError("album service unavailable")
        return list(self.album)[:page_size]

    def default_params_config(self):
        return self.params_payload

    def fetch_media(self, file_path):
        data = self.media.get(file_path)
        if data is None:
            raise CameraControlError(f"no such media: {file_path}")
        return data

    def rtsp_url(self, channel=0):
        return f"fake-rtsp://{self.host}/ch{channel}"

    # -- test helpers --
    def add_album_file(self, name: str, folder: str = "/sdcard/DWARF_3/Photos",
                       payload: bytes | None = None):
        path = f"{folder}/{name}"
        self.album.insert(0, {"fileName": name, "filePath": path,
                              "mediaType": 1,
                              "modificationTime": int(time.time())})
        self.media[path] = payload or tiny_jpeg()

    def notify(self, cmd: int, spec: dict | None, values: dict | None):
        data = wire3.encode_message(spec or {}, values or {})
        self.notifications.append({"cmd": cmd, "_cmd": cmd,
                                   "type": wire3.TYPE_NOTIFICATION, "data": data})


class FakeLiveSource:
    """Scripted RTSP frame source: steady synthetic frames, kill-switchable."""

    def __init__(self, url, fps: float = 30.0):
        self.url = url
        self.dead = False
        self._interval = 1.0 / fps
        self._tick = 0

    def open(self):
        if self.dead:
            raise CameraControlError("The DWARF live stream did not open.")

    def read(self):
        if self.dead:
            raise CameraControlError("No frame from the DWARF live stream.")
        time.sleep(self._interval)
        self._tick += 1
        frame = np.full((360, 640, 3), 10, dtype=np.uint8)
        noise = np.random.default_rng(self._tick).integers(0, 4, frame.shape, dtype=np.uint8)
        return cv2.add(frame, noise)

    def close(self):
        self.dead = True


def make_session(master: str = "granted"):
    holder: dict = {}

    def transport_factory(host):
        holder["transport"] = FakeDwarfTransport(host, master=master)
        return holder["transport"]

    def live_factory(url):
        holder["live"] = FakeLiveSource(url)
        return holder["live"]

    session = DwarfSession(HOST, transport_factory=transport_factory,
                           live_source_factory=live_factory)
    return session, holder


class WireCodec(unittest.TestCase):
    def test_defaults_are_omitted_and_decode_back(self):
        self.assertEqual(wire3.encode_message(wire3.COM_RESPONSE, {"code": 0}), b"")
        self.assertEqual(wire3.decode_message(wire3.COM_RESPONSE, b""), {"code": 0})

    def test_negative_int32_roundtrip_and_vector(self):
        payload = wire3.encode_message(wire3.COM_RESPONSE, {"code": -11501})
        # proto3 int32 negatives ride as 64-bit two's-complement varints.
        self.assertEqual(payload[0], 0x08)
        self.assertEqual(len(payload), 11)  # tag + 10 varint bytes
        self.assertEqual(wire3.decode_message(wire3.COM_RESPONSE, payload)["code"],
                         -11501)

    def test_goto_dso_known_vector(self):
        import struct

        payload = wire3.encode_message(
            wire3.REQ_GOTO_DSO, {"ra": 83.82, "dec": -5.39, "target_name": "M42"})
        expected = (b"\x09" + struct.pack("<d", 83.82)
                    + b"\x11" + struct.pack("<d", -5.39)
                    + b"\x1a\x03M42")
        self.assertEqual(payload, expected)

    def test_packet_roundtrip_carries_identity(self):
        frame = wire3.encode_packet(wire3.MODULE_ASTRO,
                                    wire3.CMD_ASTRO_START_GOTO_DSO,
                                    b"\x01\x02", client_id="test-client")
        packet = wire3.decode_packet(frame)
        self.assertEqual(packet["module_id"], wire3.MODULE_ASTRO)
        self.assertEqual(packet["cmd"], wire3.CMD_ASTRO_START_GOTO_DSO)
        self.assertEqual(packet["type"], wire3.TYPE_REQUEST)
        self.assertEqual(packet["data"], b"\x01\x02")
        self.assertEqual(packet["client_id"], "test-client")

    def test_unknown_fields_are_skipped(self):
        payload = wire3.encode_message(wire3.COM_RESPONSE, {"code": 3})
        with_unknown = payload + b"\x58\x07"  # field 11 varint: not in spec
        self.assertEqual(wire3.decode_message(wire3.COM_RESPONSE, with_unknown),
                         {"code": 3})

    def test_repeated_message_decode(self):
        payload = wire3.encode_message(wire3.RES_NOTIFY_PARAM, {"param": [
            {"id": 1, "index": 60}, {"id": 2, "index": 30}]})
        decoded = wire3.decode_message(wire3.RES_NOTIFY_PARAM, payload)
        self.assertEqual([p["index"] for p in decoded["param"]], [60, 30])


class SessionBehavior(unittest.TestCase):
    def test_master_lock_denied_is_honest(self):
        session, _ = make_session(master="slave")
        with self.assertRaises(CameraControlError) as ctx:
            session.init()
        self.assertIn("DWARFLAB app", str(ctx.exception))

    def test_init_opens_camera_and_builds_device_dials(self):
        session, holder = make_session()
        session.init()
        transport = holder["transport"]
        opened = [(m, c) for m, c, _ in transport.commands]
        self.assertIn((wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_OPEN_CAMERA),
                      opened)
        widget = session.get_single_config("shutterspeed")
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        self.assertEqual(choices, ["1/2000", "1", "15"],
                         "dial choices are the DEVICE's own gear names (tele table)")
        gain = session.get_single_config("gain")
        self.assertEqual([gain.get_choice(i) for i in range(gain.count_choices())],
                         ["0", "80"])
        session.exit()
        self.assertIn((wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_CLOSE_CAMERA),
                      [(m, c) for m, c, _ in transport.commands])

    def test_exposure_write_sends_manual_mode_once_then_index(self):
        session, holder = make_session()
        session.init()
        transport = holder["transport"]
        transport.commands.clear()

        widget = session.get_single_config("shutterspeed")
        widget.set_value("15")
        session.set_single_config("shutterspeed", widget)
        self.assertEqual([(m, c) for m, c, _ in transport.commands],
                         [(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_EXP_MODE),
                          (wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_EXP)])
        self.assertEqual(transport.commands[1][2], {"index": 72})

        transport.commands.clear()
        widget.set_value("1/2000")
        session.set_single_config("shutterspeed", widget)
        self.assertEqual([(m, c) for m, c, _ in transport.commands],
                         [(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_EXP)],
                         "manual mode is negotiated once per session")
        self.assertEqual(session.nominal_exposure_s(), 0.0005,
                         "the dial value IS the exposure")
        session.exit()

    def test_unknown_dial_value_refuses_with_device_vocabulary(self):
        session, _ = make_session()
        session.init()
        widget = session.get_single_config("shutterspeed")
        widget.set_value("1/3")
        with self.assertRaises(CameraControlError) as ctx:
            session.set_single_config("shutterspeed", widget)
        self.assertIn("device offers", str(ctx.exception))
        session.exit()

    def test_capture_announces_new_album_file_and_downloads(self):
        session, holder = make_session()
        session.init()
        transport = holder["transport"]
        transport.add_album_file("OLD_20260101.jpeg")
        # Pre-existing files were seeded at init from an empty album; this
        # one arrives before any capture — it must never announce.
        session.trigger_capture()
        self.assertIn((wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_PHOTOGRAPH),
                      [(m, c) for m, c, _ in transport.commands])
        payload = tiny_jpeg(level=200)
        transport.add_album_file("DWARF_20260714.jpeg", payload=payload)

        event_type, event_data = session.wait_for_event(500)
        while event_type == wire.GP_EVENT_UNKNOWN:
            event_type, event_data = session.wait_for_event(500)
        self.assertEqual(event_type, wire.GP_EVENT_FILE_ADDED)
        announced = {event_data.name}
        event_type, extra = session.wait_for_event(200)
        if event_type == wire.GP_EVENT_FILE_ADDED:
            announced.add(extra.name)
        self.assertIn("DWARF_20260714.jpeg", announced)
        self.assertIn("OLD_20260101.jpeg", announced,
                      "a file that appeared mid-window is a capture too")

        data = bytes(session.file_get("/sdcard/DWARF_3/Photos",
                                      "DWARF_20260714.jpeg",
                                      wire.GP_FILE_TYPE_NORMAL).get_data_and_size())
        self.assertEqual(data, payload)
        session.exit()

    def test_files_seen_at_init_never_announce(self):
        holder: dict = {}

        def transport_factory(host):
            transport = FakeDwarfTransport(host)
            transport.add_album_file("PREEXISTING.jpeg")
            holder["transport"] = transport
            return transport

        session = DwarfSession(HOST, transport_factory=transport_factory,
                               live_source_factory=lambda url: FakeLiveSource(url))
        session.init()
        session.trigger_capture()
        event_type, event_data = session.wait_for_event(300)
        while event_type == wire.GP_EVENT_UNKNOWN:
            event_type, event_data = session.wait_for_event(300)
        self.assertEqual(event_type, wire.GP_EVENT_TIMEOUT,
                         "album history must not masquerade as new captures")
        session.exit()

    def test_notifications_feed_telemetry_and_status_events(self):
        session, holder = make_session()
        session.init()
        transport = holder["transport"]
        transport.notify(wire3.CMD_NOTIFY_ELE, wire3.COM_RES_WITH_INT, {"value": 83})
        transport.notify(wire3.CMD_NOTIFY_TEMPERATURE, wire3.RES_NOTIFY_TEMPERATURE,
                         {"code": 0, "temperature": 21})
        transport.notify(wire3.CMD_NOTIFY_STATE_ASTRO_GOTO, wire3.RES_NOTIFY_STATE,
                         {"state": 4})
        event_type, note = session.wait_for_event(200)
        self.assertEqual(event_type, wire.GP_EVENT_UNKNOWN)
        self.assertEqual(note, "GOTO success")
        self.assertEqual(session.get_single_config("battery").get_value(), "83%")
        self.assertEqual(session.get_single_config("temperature").get_value(), "21°C")
        session.exit()

    def test_preview_is_jpeg_and_dead_stream_raises(self):
        session, holder = make_session()
        session.init()
        jpeg = bytes(session.capture_preview().get_data_and_size())
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        holder["live"].dead = True
        with self.assertRaises(CameraControlError):
            session.capture_preview()  # the watchdog counts raises
        session.exit()

    def test_mount_commands_reach_the_wire(self):
        session, holder = make_session()
        session.init()
        transport = holder["transport"]
        transport.commands.clear()

        session.goto_dso(83.82, -5.39, "M42")
        session.goto_solar("jupiter")
        session.stop_goto()
        session.joystick(90.0, 2.0, 99.0)  # out-of-range length/speed clamp
        session.joystick_stop()
        session.astro_autofocus()
        session.manual_focus_step(-1)

        sent = [(m, c) for m, c, _ in transport.commands]
        self.assertEqual(sent, [
            (wire3.MODULE_ASTRO, wire3.CMD_ASTRO_START_GOTO_DSO),
            (wire3.MODULE_ASTRO, wire3.CMD_ASTRO_START_GOTO_SOLAR_SYSTEM),
            (wire3.MODULE_ASTRO, wire3.CMD_ASTRO_STOP_GOTO),
            (wire3.MODULE_MOTOR, wire3.CMD_STEP_MOTOR_SERVICE_JOYSTICK),
            (wire3.MODULE_MOTOR, wire3.CMD_STEP_MOTOR_SERVICE_JOYSTICK_STOP),
            (wire3.MODULE_FOCUS, wire3.CMD_FOCUS_START_ASTRO_AUTO_FOCUS),
            (wire3.MODULE_FOCUS, wire3.CMD_FOCUS_MANUAL_SINGLE_STEP_FOCUS),
        ])
        goto_values = transport.commands[0][2]
        self.assertEqual((goto_values["ra"], goto_values["dec"]), (83.82, -5.39))
        self.assertEqual(transport.commands[1][2]["index"], 4, "jupiter = 4")
        joystick_values = transport.commands[3][2]
        self.assertEqual(joystick_values["vector_length"], 1.0)
        self.assertEqual(joystick_values["speed"], 30.0)
        self.assertEqual(transport.commands[6][2]["direction"], 0, "near = 0")

        with self.assertRaises(CameraControlError):
            session.goto_solar("pluto")  # not in the device vocabulary
        session.exit()

    def test_device_error_codes_speak_plainly(self):
        session, holder = make_session()
        session.init()
        holder["transport"].codes[wire3.CMD_ASTRO_START_GOTO_DSO] = -11505
        with self.assertRaises(CameraControlError) as ctx:
            session.goto_dso(83.82, -5.39, "M42")
        self.assertIn("GOTO failed", str(ctx.exception))
        session.exit()


class AdapterBehavior(unittest.TestCase):
    class StubSession:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*args):
                self.calls.append((name,) + args)
            return record

    def test_family_actions_and_capabilities(self):
        adapter = DwarfAdapter(None)
        self.assertEqual(adapter.family, "dwarf")
        self.assertIn("gotoradec", adapter.family_action_names())
        caps = adapter.capabilities({})
        self.assertEqual(caps["mount"]["kind"], "alt-az")
        self.assertIn("radec", caps["mount"]["goto"])
        self.assertIn("gotoradec", caps["actions"])
        self.assertEqual(caps["save_to"]["modes"], ["device", "local"])

    def test_goto_value_parsing_is_strict(self):
        parse = DwarfAdapter._parse_goto_value
        self.assertEqual(parse("83.82,-5.39,M42"), (83.82, -5.39, "M42"))
        self.assertEqual(parse("10.5, 41.2"), (10.5, 41.2, ""))
        for bad in (None, "", "M42", "400,0", "0,95", "83.8"):
            with self.assertRaises(CameraControlError, msg=repr(bad)):
                parse(bad)

    def test_run_action_routes_to_the_session(self):
        adapter = DwarfAdapter(None)
        session = self.StubSession()
        receipt = adapter.run_action(session, "gotoradec", "83.82,-5.39,M42", {})
        self.assertTrue(receipt.ok)
        self.assertEqual(session.calls, [("goto_dso", 83.82, -5.39, "M42")])

        session.calls.clear()
        receipt = adapter.run_action(session, "joystick", "90,1,5", {})
        self.assertTrue(receipt.ok)
        self.assertEqual(session.calls, [("joystick", 90.0, 1.0, 5.0)])

        session.calls.clear()
        receipt = adapter.run_action(session, "manualfocusdrive", "near", {})
        self.assertTrue(receipt.ok)
        self.assertEqual(session.calls, [("manual_focus_step", -1)])

        receipt = adapter.run_action(session, "gotosolar", "", {})
        self.assertFalse(receipt.ok)
        self.assertIn("target name", receipt.error)

        receipt = adapter.run_action(session, "unknownaction", None, {})
        self.assertFalse(receipt.ok)


class DriverBehavior(unittest.TestCase):
    def test_unconfigured_driver_is_unavailable(self):
        with mock.patch.dict(os.environ, {"ABSTRACTCAMERA_DWARF_HOSTS": ""}):
            driver = DwarfDriver()
            self.assertFalse(driver.available())
            self.assertEqual(driver.list_cameras(), [])

    def test_env_configured_hosts_are_listed(self):
        with mock.patch.dict(os.environ,
                             {"ABSTRACTCAMERA_DWARF_HOSTS": "192.168.88.1, 10.0.0.9"}):
            driver = DwarfDriver()
            entries = driver.list_cameras()
            self.assertEqual([e["id"] for e in entries],
                             ["dwarf:192.168.88.1", "dwarf:10.0.0.9"])
            self.assertTrue(all(e["kind"] == "smart_telescope" for e in entries))
            self.assertTrue(all(e["name_confidence"] == "configured" for e in entries))

    def test_create_session_parses_the_host(self):
        driver = DwarfDriver(hosts=["10.1.2.3"])
        session = driver.create_session("dwarf:10.1.2.3")
        self.assertEqual(session.host, "10.1.2.3")
        default = driver.create_session(None)
        self.assertEqual(default.host, "10.1.2.3")

    def test_unconfigured_connect_refuses_actionably(self):
        driver = DwarfDriver(hosts=[])
        with self.assertRaises(CameraControlError) as ctx:
            driver.create_session(None)
        self.assertIn("ABSTRACTCAMERA_DWARF_HOSTS", str(ctx.exception))

    def test_dwarf_never_becomes_the_default_camera(self):
        from abstractcamera import discovery

        entries = [
            {"id": "dwarf:10.0.0.9", "transport": "dwarf",
             "name": "DWARF 3 (10.0.0.9)", "kind": "smart_telescope"},
            {"id": "webcam:AAA", "transport": "webcam",
             "name": "MacBook Pro Camera", "kind": "built_in"},
        ]
        self.assertEqual(discovery._default_entry(entries)["id"], "webcam:AAA",
                         "connecting a telescope must be a choice, not a default")


class FakeDwarfDriver:
    driver_id = "dwarf"

    def __init__(self):
        self.transports: list[FakeDwarfTransport] = []

    def available(self):
        return True

    def list_cameras(self):
        return [{"id": f"dwarf:{HOST}", "transport": "dwarf",
                 "name": f"DWARF 3 ({HOST})", "kind": "smart_telescope",
                 "name_confidence": "configured"}]

    def prepare_connect(self, camera_id):
        pass

    def create_session(self, camera_id):
        def transport_factory(host):
            transport = FakeDwarfTransport(host)
            self.transports.append(transport)
            return transport

        return DwarfSession(HOST, transport_factory=transport_factory,
                            live_source_factory=lambda url: FakeLiveSource(url))

    def select_adapter(self, model):
        return DwarfAdapter(None)


class ManagerIntegration(unittest.TestCase):
    def _manager(self):
        driver = FakeDwarfDriver()
        manager = CameraManager(driver=driver)
        self.addCleanup(manager.disconnect)
        capture_dir = tempfile.mkdtemp(prefix="dwarf_captures_")
        manager.set_capture_dir(capture_dir)
        return manager, driver

    def test_connect_exposes_family_mount_and_actions(self):
        manager, _ = self._manager()
        status = manager.connect()
        self.assertTrue(status["connected"])
        self.assertEqual(status["family"], "dwarf")
        self.assertIn("DWARF 3", status["model"])
        self.assertEqual(status["capabilities"]["mount"]["kind"], "alt-az")
        self.assertIn("gotoradec", status["actions"])
        self.assertIn("autofocusdrive", status["actions"])
        self.assertTrue(wait_until(lambda: manager.get_latest_frame()[1] >= 3),
                        "live view frames must flow from the RTSP double")
        self.assertTrue(wait_until(
            lambda: manager.status()["config"].get("shutterspeed", {})
            .get("choices") == ["1/2000", "1", "15"], timeout=6.0),
            "the exposure dial must carry the device's own table")

    def test_mount_action_reaches_the_wire_through_the_worker(self):
        manager, driver = self._manager()
        manager.connect()
        result = manager.request_action("gotoradec", "83.82,-5.39,M42")
        self.assertEqual(result["status"], "action-queued")
        self.assertTrue(wait_until(
            lambda: any(cmd == wire3.CMD_ASTRO_START_GOTO_DSO
                        for _, cmd, _ in driver.transports[-1].commands),
            timeout=8.0), "the queued GOTO must reach the (fake) telescope")
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "trigger" and "GOTO" in (e["note"] or "")
                        for e in manager.get_events()), timeout=8.0),
            [e for e in manager.get_events()])

    def test_foreign_family_actions_stay_refused(self):
        manager, _ = self._manager()
        manager.connect()
        with self.assertRaises(CameraControlError):
            manager.request_action("fly_to_the_moon")

    def test_capture_downloads_album_file_into_capture_dir(self):
        manager, driver = self._manager()
        manager.connect()
        manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: driver.transports
            and any(cmd == wire3.CMD_CAMERA_TELE_PHOTOGRAPH
                    for _, cmd, _ in driver.transports[-1].commands), timeout=8.0))
        driver.transports[-1].add_album_file("DWARF_TEST_0001.jpeg")
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in manager.get_events()),
            timeout=15.0), "the album file must announce and download")
        photo = next(e for e in manager.get_events() if e["kind"] == "photo")
        self.assertTrue(photo["path"] and os.path.exists(photo["path"]))

    def test_goto_status_notifications_land_in_catch_log(self):
        manager, driver = self._manager()
        manager.connect()
        self.assertTrue(wait_until(lambda: bool(driver.transports), timeout=5.0))
        driver.transports[-1].notify(wire3.CMD_NOTIFY_STATE_ASTRO_GOTO,
                                     wire3.RES_NOTIFY_STATE, {"state": 1})
        driver.transports[-1].notify(wire3.CMD_NOTIFY_STATE_ASTRO_GOTO,
                                     wire3.RES_NOTIFY_STATE, {"state": 4})
        self.assertTrue(wait_until(
            lambda: any("GOTO success" in (e["note"] or "")
                        for e in manager.get_events()), timeout=8.0),
            "the mount's own voice must reach the catch log")


if __name__ == "__main__":
    unittest.main(verbosity=2)
