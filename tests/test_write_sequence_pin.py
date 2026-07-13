"""Golden call-sequence pin for the camera adapter extraction (2026-07-12).

Captured against the PRE-refactor controller: a scripted session must send
the SAME camera writes in the SAME order after the family-adapter extraction
as before it. The outcome-level regression tests (test_backend_camera_
regression.py) prove state results; this pin proves call ORDER — the thing a
restructuring can silently change while every outcome test stays green.
The Nikon Z6 II that validated this ordering on hardware is not connected,
so this executable pin is the only ordering protection available.
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


class WriteLogPin(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(download_stall_s=(0.01, 0.03), trigger_latency_s=0.05,
                          trigger_latency_jitter_s=0.0, file_added_offset_s=0.1)
        self.controller = CameraController(driver=FakeDriver(fake_gp))
        self.controller.set_capture_dir(tempfile.mkdtemp(prefix="pin_captures_"))

    def tearDown(self):
        try:
            self.controller.disconnect()
        finally:
            fake_gp.reset()

    def _wait_config(self, name, value):
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get(name, {}).get("value") == value,
            timeout=10.0,
        ), f"{name} never reached {value!r}")

    def test_scripted_session_write_sequence_is_stable(self):
        status = self.controller.connect()
        self.assertTrue(status["connected"])
        fake_camera = fake_gp.get_last_camera()

        # 1. Two config writes (order preserved through the pending dict).
        self.controller.set_config_value("iso", "3200")
        self._wait_config("iso", "3200")
        self.controller.set_config_value("shutterspeed", "2s")
        self._wait_config("shutterspeed", "2s")

        # 2. One manual trigger; wait for the photo to fully download.
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in self.controller.get_events()),
            timeout=15.0,
        ), "no photo event after the single trigger")

        # 3. Burst mode (queues capturemode then burstnumber) + trigger.
        self.controller.set_capture_mode("burst", burst_count=3)
        self._wait_config("capturemode", "Burst")
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: len(fake_camera.trigger_log) >= 2, timeout=10.0,
        ), "burst trigger never reached the camera")

        # 4. Video mode (queues recordingmedia); the default fake scenario
        #    REFUSES movie start via the prohibit pre-check: no movie write.
        self.controller.set_capture_mode("video")
        self._wait_config("recordingmedia", "Card")
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "error" and "video" in e["note"]
                        for e in self.controller.get_events()),
            timeout=8.0,
        ), "prohibited movie start did not produce the refusal event")

        # Let pending downloads and events settle before reading the logs.
        wait_until(lambda: self.controller.status()["downloads_pending"] == 0, timeout=15.0)

        write_names = [name for _t, _api, name in fake_camera.config_write_log
                       if name is not None]
        self.assertEqual(
            write_names,
            ["iso", "shutterspeed", "capturemode", "burstnumber", "recordingmedia"],
            "camera write SEQUENCE changed — the adapter extraction must be pure code motion",
        )
        self.assertEqual(len(fake_camera.trigger_log), 2,
                         "trigger count changed for the scripted session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
