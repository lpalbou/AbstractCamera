"""Webcam family: session behavior (against a scripted FakeFrameSource
double — no real device, no PyObjC, no TCC prompt in CI) and the full
manager loop (connect, frames, resolution dial through the ledger,
shoot/burst into the capture dir, movie receipts, motion detection,
rolling clip, interval sequence via nominal_exposure_s)."""

import os
import tempfile
import time
import unittest

import cv2
import numpy as np

from abstractcamera import CameraManager
from abstractcamera.adapters.webcam import WebcamAdapter
from abstractcamera.drivers.webcam_session import WebcamSession
from abstractcamera.errors import CameraControlError
from abstractcamera import wire

FAKE_UNIQUE_ID = "FAKE-0011-0005-MBP"


class FakeFrameSource:
    """Scripted AVFFrameSource double (the ADR 0009 seam): same surface,
    pure numpy. Device-reported dims: 1080p + 720p (the measured MBP set)."""

    REPORTED_DIMS = [(1920, 1080), (1280, 720)]

    def __init__(self, unique_id, label, *, opened=True, frames=True,
                 moving=False, denied=False, fps=60.0, has_flash=False):
        self.unique_id = unique_id
        self.lost = False
        self._label = label
        self._opened = opened
        self._frames = frames
        self._moving = moving
        self._denied = denied
        self._frame_interval = 1.0 / fps
        self._has_flash = has_flash
        self._width, self._height = 1920, 1080
        self._tick = 0

    def open(self):
        if self._denied:
            raise CameraControlError(
                "macOS has denied camera access for this app — allow it in "
                "System Settings → Privacy & Security → Camera, then reconnect.")
        if not self._opened:
            raise CameraControlError(
                f"'{self._label}' refused to open — it may be held exclusively "
                "by another app.")
        if not self._frames:
            raise CameraControlError(
                f"No frames arrived from '{self._label}' — if macOS just showed "
                "a camera permission prompt, answer it and reconnect.")

    def read(self, timeout_s=0.75):
        if self.lost or not self._opened or not self._frames:
            raise CameraControlError(f"'{self._label}' stopped delivering frames.")
        # The REAL source blocks for the next frame (latest-frame semantics,
        # conformance item 3): the double must pace identically or ring/fps
        # arithmetic downstream tests against a fantasy frame rate.
        time.sleep(self._frame_interval)
        self._tick += 1
        frame = np.full((self._height, self._width, 3), 12, dtype=np.uint8)
        noise = np.random.default_rng(self._tick).integers(0, 4, frame.shape, dtype=np.uint8)
        frame = cv2.add(frame, noise)
        if self._moving and self._tick > 12:
            # A bright block drifting across the frame: real motion for the
            # detector, gain-step-immune (local, not global).
            x = 50 + (self._tick * 9) % 600
            cv2.rectangle(frame, (x, 200), (x + 160, 360), (235, 235, 235), -1)
        return frame

    def format_dims(self):
        return list(self.REPORTED_DIMS)

    def current_dims(self):
        return (self._width, self._height)

    def set_dims(self, width, height):
        if (width, height) not in self.REPORTED_DIMS:
            raise CameraControlError(f"Unsupported resolution: {width}x{height}")
        self._width, self._height = width, height

    # Zoom mirrors the measured AVFoundation surface (1..16 digital crop).
    def zoom_range(self):
        return (1.0, 16.0)

    def zoom(self):
        return getattr(self, "_zoom", 1.0)

    def set_zoom(self, factor):
        self._zoom = max(1.0, min(16.0, float(factor)))

    # Flash mirrors the Continuity-iPhone surface (constructor-scripted).
    def has_flash(self):
        return getattr(self, "_has_flash", False)

    def fire_flash_photo(self, mode):
        import threading

        class Ticket:
            def __init__(self):
                self.done = threading.Event()
                self.photo = None
                self.error = None

        ticket = Ticket()
        self.flash_fired = getattr(self, "flash_fired", 0) + 1
        # Deterministic delivered photo: a bright JPEG distinguishable from
        # the dark stream frames.
        bright = np.full((480, 640, 3), 200, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", bright)
        ticket.photo = encoded.tobytes() if ok else None
        ticket.done.set()
        return ticket

    def close(self):
        self.lost = True
        self._opened = False


class FakeWebcamDriver:
    driver_id = "webcam"

    def __init__(self, **session_kwargs):
        self._session_kwargs = session_kwargs

    def available(self):
        return True

    def list_cameras(self):
        return [{"id": f"webcam:{FAKE_UNIQUE_ID}", "transport": "webcam",
                 "name": "Fake MacBook Camera", "name_confidence": "reported"}]

    def prepare_connect(self, camera_id):
        pass

    def create_session(self, camera_id):
        return WebcamSession(
            unique_id=FAKE_UNIQUE_ID, label="Fake MacBook Camera",
            source_factory=lambda uid, label: FakeFrameSource(uid, label,
                                                              **self._session_kwargs))

    def select_adapter(self, model):
        return WebcamAdapter(None)


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class SessionBehavior(unittest.TestCase):
    def _session(self, **kwargs):
        return WebcamSession(
            unique_id=FAKE_UNIQUE_ID, label="Fake MacBook Camera",
            source_factory=lambda uid, label: FakeFrameSource(uid, label, **kwargs))

    def test_held_device_open_is_honest(self):
        session = self._session(opened=False)
        with self.assertRaises(CameraControlError) as ctx:
            session.init()
        self.assertIn("held exclusively", str(ctx.exception))

    def test_tcc_denial_is_deterministic(self):
        """ADR 0009: denied TCC is diagnosed BEFORE opening (it used to be
        inferred from a mute frame timeout)."""
        session = self._session(denied=True)
        with self.assertRaises(CameraControlError) as ctx:
            session.init()
        self.assertIn("Privacy & Security", str(ctx.exception))

    def test_frameless_open_names_the_prompt(self):
        session = self._session(frames=False)
        with self.assertRaises(CameraControlError) as ctx:
            session.init()
        self.assertIn("permission prompt", str(ctx.exception))

    def test_resolution_choices_are_device_reported(self):
        session = self._session()
        session.init()
        widget = session.get_single_config("imagesize")
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        self.assertEqual(set(choices), {"1920x1080", "1280x720"},
                         "choices = device-reported dims ∩ ladder, nothing fabricated")
        session.exit()

    def test_session_carries_unique_id(self):
        """The identity that makes the hub's device_serial stable."""
        session = self._session()
        session.init()
        self.assertEqual(session.unique_id, FAKE_UNIQUE_ID)
        adapter = WebcamAdapter(None)
        self.assertEqual(adapter.read_serial(session), FAKE_UNIQUE_ID)
        session.exit()

    def test_zoom_widget_reads_and_writes(self):
        """The ONE manual control macOS grants (videoZoomFactor): ladder
        choices within the device range, readback-confirmed writes."""
        session = self._session()
        session.init()
        widget = session.get_single_config("zoom")
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        self.assertEqual(widget.get_value(), "1x")
        self.assertIn("2x", choices)
        self.assertIn("16x", choices)

        widget.set_value("3x")
        session.set_single_config("zoom", widget)
        self.assertEqual(session.get_single_config("zoom").get_value(), "3x")
        session.exit()

    def test_non_granted_controls_refuse_with_the_reason(self):
        session = self._session()
        session.init()
        widget = session.get_single_config("imagesize")
        widget_name = "iso"
        with self.assertRaises(CameraControlError) as ctx:
            session.set_single_config(widget_name, widget)
        self.assertIn("macOS reserves", str(ctx.exception))
        session.exit()

    def test_flash_widget_only_on_devices_with_flash(self):
        """MacBook camera: no flash, no widget (measured). iPhone: widget
        with off/auto/on."""
        without = self._session()
        without.init()
        with self.assertRaises(CameraControlError):
            without.get_single_config("flashmode")
        without.exit()

        with_flash = self._session(has_flash=True)
        with_flash.init()
        widget = with_flash.get_single_config("flashmode")
        self.assertEqual(widget.get_value(), "off")
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        self.assertEqual(choices, ["off", "auto", "on"])
        with_flash.exit()

    def test_flash_capture_routes_through_photo_pipeline(self):
        """Flash armed -> the still is the DELIVERED photo (bright test
        JPEG), not a stream frame; flash off -> stream frame, no firing."""
        session = self._session(has_flash=True)
        session.init()
        source = session._source

        session.trigger_capture()  # flash off: normal path
        self.assertEqual(getattr(source, "flash_fired", 0), 0)
        _, event = session.wait_for_event(200)
        dark = bytes(session.file_get("/", event.name, wire.GP_FILE_TYPE_NORMAL)
                     .get_data_and_size())

        widget = session.get_single_config("flashmode")
        widget.set_value("on")
        session.set_single_config("flashmode", widget)
        session.trigger_capture()
        self.assertEqual(source.flash_fired, 1)
        _, event = session.wait_for_event(200)
        bright = bytes(session.file_get("/", event.name, wire.GP_FILE_TYPE_NORMAL)
                       .get_data_and_size())

        import numpy as np

        dark_mean = cv2.imdecode(np.frombuffer(dark, np.uint8), cv2.IMREAD_GRAYSCALE).mean()
        bright_mean = cv2.imdecode(np.frombuffer(bright, np.uint8), cv2.IMREAD_GRAYSCALE).mean()
        self.assertGreater(bright_mean, dark_mean + 50,
                           "the flash capture must carry the delivered photo")
        session.exit()

    def test_conformance_items(self):
        """The session behavioral contract (Mod #4) for the webcam."""
        session = self._session()
        session.init()
        event_type, event_data = session.wait_for_event(20)
        self.assertEqual(event_type, wire.GP_EVENT_TIMEOUT)
        self.assertIsNone(event_data)
        jpeg = bytes(session.capture_preview().get_data_and_size())
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        session.trigger_capture()
        event_type, event_data = session.wait_for_event(200)
        self.assertEqual(event_type, wire.GP_EVENT_FILE_ADDED)
        data = bytes(session.file_get(event_data.folder, event_data.name,
                                      wire.GP_FILE_TYPE_NORMAL).get_data_and_size())
        self.assertGreater(len(data), 1000)
        session.exit()
        with self.assertRaises(Exception):
            session.capture_preview()  # closed stream must RAISE (watchdog)


class ManagerIntegration(unittest.TestCase):
    def _manager(self, **session_kwargs) -> CameraManager:
        manager = CameraManager(driver=FakeWebcamDriver(**session_kwargs))
        self.addCleanup(manager.disconnect)
        capture_dir = tempfile.mkdtemp(prefix="webcam_captures_")
        manager.set_capture_dir(capture_dir)
        self.capture_dir = capture_dir
        return manager

    def test_connect_capabilities_and_frames(self):
        manager = self._manager()
        status = manager.connect()
        self.assertTrue(status["connected"])
        self.assertEqual(status["family"], "webcam")
        caps = status["capabilities"]
        self.assertFalse(caps["focus"]["supported"])
        self.assertFalse(caps["exposure_controls"])
        self.assertEqual(caps["config_widgets"], ["imagesize", "zoom", "flashmode", "fps"])
        self.assertTrue(caps["movie"]["can_confirm"])
        self.assertTrue(wait_until(lambda: manager.get_latest_frame()[1] >= 3))
        self.assertIn("imagesize", status["config"] or manager.status()["config"])

    def test_resolution_dial_through_ledger(self):
        manager = self._manager()
        manager.connect()
        manager.set_config_value("imagesize", "1280x720")
        self.assertTrue(wait_until(
            lambda: manager.status()["config"].get("imagesize", {}).get("value") == "1280x720"))
        self.assertTrue(wait_until(
            lambda: manager.status()["preview_size"] == [1280, 720], timeout=6.0),
            "the SOF truth signal must confirm the switch end-to-end")

    def test_unsupported_widget_refused_honestly(self):
        manager = self._manager()
        manager.connect()
        with self.assertRaises(CameraControlError):
            manager.set_config_value("iso", "800")  # not in this family's list

    def test_shoot_lands_in_capture_dir(self):
        manager = self._manager()
        manager.connect()
        manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in manager.get_events()), timeout=10.0))
        photo = next(e for e in manager.get_events() if e["kind"] == "photo")
        self.assertTrue(photo["path"] and os.path.exists(photo["path"]))
        self.assertIn(".jpg", photo["path"])

    def test_burst_downloads_every_frame(self):
        manager = self._manager()
        manager.connect()
        manager.set_capture_mode("burst", burst_count=6)
        manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: len([e for e in manager.get_events() if e["kind"] == "photo"]) >= 6,
            timeout=15.0), "every burst frame must land (store depth ~1 via sink pump)")
        failures = [e for e in manager.get_events()
                    if e["kind"] == "error" and "failed to fetch" in (e["note"] or "")]
        self.assertEqual(failures, [])

    def test_movie_confirmable_receipts(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("[clips] extra not installed")
        manager = self._manager()
        manager.connect()
        manager.set_capture_mode("video")
        manager.request_trigger()
        self.assertTrue(wait_until(lambda: manager.status()["movie_recording"], timeout=8.0))
        notes = [e["note"] for e in manager.get_events() if e["kind"] == "trigger"]
        self.assertTrue(any("recording MP4" in n for n in notes), notes)
        time.sleep(1.0)
        manager.request_trigger()
        self.assertTrue(wait_until(lambda: not manager.status()["movie_recording"], timeout=8.0))
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" and ".mp4" in (e["path"] or "")
                        for e in manager.get_events()), timeout=10.0),
            "the finished MP4 must land through the normal capture path")

    def test_focus_actions_refused(self):
        manager = self._manager()
        manager.connect()
        manager.request_action("autofocusdrive")
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "error" and "focus" in (e["note"] or "")
                        for e in manager.get_events()), timeout=6.0))

    def test_motion_detection_fires_on_scripted_movement(self):
        manager = self._manager(moving=True)
        manager.connect()
        manager.set_detection_mode("monitor", target="motion", sensitivity=80)
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "detection" for e in manager.get_events()),
            timeout=20.0), "scripted motion must register on the webcam feed")

    def test_interval_sequence_via_nominal_exposure(self):
        manager = self._manager()
        manager.connect()
        result = manager.start_interval_sequence(interval_s=1.0, count=2)
        self.assertEqual(result["status"], "sequence-armed")
        self.assertTrue(wait_until(
            lambda: manager.status()["interval"]["state"] == "complete", timeout=15.0),
            manager.status()["interval"])
        self.assertEqual(manager.status()["interval"]["shots_done"], 2)

    def test_rolling_clip_saves(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("[clips] extra not installed")
        manager = self._manager()
        manager.connect()
        manager.set_rolling_buffer(True, seconds=3.0)
        self.assertTrue(wait_until(
            lambda: manager.status()["rolling"]["buffered_s"] >= 1.5, timeout=15.0))
        clip = manager.save_rolling_clip()
        self.assertTrue(os.path.exists(clip["path"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
