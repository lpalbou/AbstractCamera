"""Regression pins for the EXISTING camera controller behavior (gate 1).

Written BEFORE the intervalometer refactor (referee ruling): these tests pin
connect/status/config/trigger/download/burst/video/detection behavior under
the fake gphoto2 camera so the refactor cannot silently break the shipped
lightning/tethering features. No hardware involved.
"""

import os
import sys
import tempfile
import time
import unittest


import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera import CameraControlError, CameraManager as CameraController
from abstractcamera.drivers.fake_driver import FakeDriver


def wait_until(predicate, timeout=8.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class FakeCameraHarness(unittest.TestCase):
    """Fresh controller + fake module per test; never the singleton."""

    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(download_stall_s=(0.01, 0.03), trigger_latency_s=0.05,
                          trigger_latency_jitter_s=0.02, file_added_offset_s=0.1)
        self.controller = CameraController(driver=FakeDriver(fake_gp))
        self.capture_dir = tempfile.mkdtemp(prefix="fake_captures_")
        self.controller.set_capture_dir(self.capture_dir)

    def tearDown(self):
        try:
            self.controller.disconnect()
        finally:
            fake_gp.reset()

    def connect(self):
        status = self.controller.connect()
        self.assertTrue(status["connected"])
        return status


class ConnectionAndConfigRegression(FakeCameraHarness):
    def test_connect_status_and_config_cache(self):
        status = self.connect()
        self.assertIn("Fake", status["model"])
        self.assertTrue(status["liveview_running"])
        config = status["config"]
        self.assertIn("iso", config)
        self.assertIn("800", config["iso"]["value"])
        self.assertIn("choices", config["iso"])
        self.assertIn("100", config["iso"]["choices"])
        self.assertIn("shutterspeed", config)
        self.assertTrue(config["batterylevel"]["readonly"] if "batterylevel" in config else True)

    def test_config_write_applies_and_refreshes_cache(self):
        self.connect()
        self.controller.set_config_value("iso", "3200")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("iso", {}).get("value") == "3200"
        ), "pending config was not applied to the fake camera")

    def test_config_rejects_unknown_widget_and_disconnected(self):
        with self.assertRaises(CameraControlError):
            self.controller.set_config_value("iso", "800")  # not connected yet
        self.connect()
        with self.assertRaises(CameraControlError):
            self.controller.set_config_value("rm -rf", "boom")

    def test_disconnect_is_clean(self):
        self.connect()
        status = self.controller.disconnect()
        self.assertFalse(status["connected"])
        self.assertFalse(status["liveview_running"])


class TriggerAndDownloadRegression(FakeCameraHarness):
    def test_manual_trigger_downloads_file_and_logs_events(self):
        self.connect()
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in self.controller.get_events()),
            timeout=10.0,
        ), "no photo event after manual trigger")
        events = self.controller.get_events()
        kinds = [e["kind"] for e in events]
        self.assertIn("trigger", kinds)
        photo = next(e for e in events if e["kind"] == "photo")
        self.assertTrue(photo["path"] and os.path.exists(photo["path"]))
        fake_camera = fake_gp.get_last_camera()
        self.assertEqual(len(fake_camera.trigger_log), 1)

    def test_live_view_frames_flow(self):
        self.connect()
        self.assertTrue(wait_until(
            lambda: self.controller.get_latest_frame()[1] >= 3
        ), "live view frames did not flow")
        frame, _seq = self.controller.get_latest_frame()
        self.assertTrue(frame.startswith(b"\xff\xd8"))  # JPEG SOI

    def test_burst_mode_sets_drive_config(self):
        self.connect()
        self.controller.set_capture_mode("burst", burst_count=4)
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("capturemode", {}).get("value") == "Burst"
        ))
        self.assertEqual(self.controller.status()["burst_count"], 4)

    def test_burst_preview_pause_clears_on_announce(self):
        """Burst bookkeeping decrements on FILE_ADDED announce, NOT on
        download completion (adversarial verdict): with slow downloads, the
        preview pause must clear as soon as all burst files are announced."""
        fake_gp.configure(download_stall_s=(0.8, 1.0))
        self.connect()
        self.controller.set_capture_mode("burst", burst_count=2)
        time.sleep(0.5)
        self.controller.request_trigger()
        # The proof of announce-time accounting: at some instant the pause is
        # already cleared while downloads are still outstanding (with ~1s
        # per file, download-time accounting could never reach this state).
        self.assertTrue(wait_until(
            lambda: self.controller._preview_pause_until == 0.0
            and self.controller.status()["downloads_pending"] > 0,
            timeout=10.0,
        ), "burst pause did not clear on announce while downloads were pending")
        # And the queue fully drains afterwards.
        self.assertTrue(wait_until(
            lambda: self.controller.status()["downloads_pending"] == 0, timeout=15.0,
        ), "burst downloads never completed")

    def test_status_reports_downloads_pending(self):
        self.connect()
        status = self.controller.status()
        self.assertIn("downloads_pending", status)
        self.assertIn("downloads_deferred", status)
        self.assertEqual(status["downloads_pending"], 0)
        self.assertFalse(status["downloads_deferred"])

    def test_video_mode_trigger_toggles_recording(self):
        # The fake's DEFAULT scenario now mirrors the real Z6 II with the
        # photo/movie selector on photo (movie start refused, [-1]); this
        # pin runs the movie-READY scenario.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        self.connect()
        self.controller.set_capture_mode("video")
        self.controller.request_trigger()
        self.assertTrue(wait_until(lambda: self.controller.status()["movie_recording"]),
                        "video trigger did not start recording")
        self.controller.request_trigger()
        self.assertTrue(wait_until(lambda: not self.controller.status()["movie_recording"]),
                        "second video trigger did not stop recording")

    def test_video_mode_prohibited_body_reports_failure(self):
        """Selector-on-photo scenario (the fake default, mirroring the real
        body): movie start fails and an error event is logged."""
        self.connect()
        self.controller.set_capture_mode("video")
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "error" for e in self.controller.get_events()), timeout=6.0))
        self.assertFalse(self.controller.status()["movie_recording"])


class DetectionRegression(FakeCameraHarness):
    def test_detection_mode_settable_without_connection(self):
        status = self.controller.set_detection_mode("monitor")
        self.assertEqual(status["detection_mode"], "monitor")
        status = self.controller.set_detection_mode("off")
        self.assertEqual(status["detection_mode"], "off")

    def test_detection_modes_reject_unknown(self):
        with self.assertRaises(CameraControlError):
            self.controller.set_detection_mode("aggressive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
