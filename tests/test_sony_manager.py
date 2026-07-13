"""Sony A7R IV integration tests: the full CameraController driving the
simulated Sony body through the adapter — connect defaults, dials through
the pending-write ledger, trigger/download, silent-AF-refusal honesty,
press-and-hold burst, movie honesty, catch-log noise hygiene, and an
interval sequence with Sony shutter strings."""

import os
import sys
import tempfile
import time
import unittest


import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera import CameraControlError, CameraManager as CameraController
from abstractcamera.drivers.fake_driver import FakeDriver


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class SonyCameraHarness(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(profile="a7r4",
                          download_stall_s=(0.01, 0.03),
                          file_added_offset_s=0.1,
                          sony_settle_delay_s=(0.05, 0.2),
                          sony_busy_window_s=0.05,
                          sony_trigger_block_s=0.15,
                          sony_burst_fps=7.5)
        self.controller = CameraController(driver=FakeDriver(fake_gp))
        self.capture_dir = tempfile.mkdtemp(prefix="sony_captures_")
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


class SonyConnectionAndDefaults(SonyCameraHarness):
    def test_family_and_capabilities_in_status(self):
        status = self.connect()
        self.assertIn("Sony", status["model"])
        self.assertEqual(status["family"], "sony_alpha")
        caps = status["capabilities"]
        self.assertEqual(caps["burst"]["mode"], "duration")
        self.assertFalse(caps["movie"]["can_confirm"])
        self.assertEqual(caps["iso_auto"]["auto_choice"], "Auto ISO")

    def test_prioritymode_default_applied_and_confirmed(self):
        self.connect()
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"]
            .get("prioritymode", {}).get("value") == "Application",
        ), "prioritymode=Application connect default never settled")
        # The connect default is announced in the catch log.
        notes = " ".join(e["note"] for e in self.controller.get_events())
        self.assertIn("Remote control", notes)

    def test_card_plus_sdram_not_rewritten(self):
        self.connect()
        time.sleep(1.0)
        self.assertEqual(
            self.controller.status()["config"]["capturetarget"]["value"],
            "card+sdram",
            "'card+sdram' must survive connect (substring-'ram' false positive)")

    def test_no_nikon_widgets_in_cache(self):
        status = self.connect()
        for name in ("isoauto", "burstnumber", "movieprohibit", "recordingmedia", "liveviewsize"):
            self.assertNotIn(name, status["config"])


class SonyConfigWrites(SonyCameraHarness):
    def test_dial_write_confirms_through_ledger(self):
        self.connect()
        self.controller.set_config_value("iso", "1600")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("iso", {}).get("value") == "1600",
        ), "iso write never settled on the Sony fake")

    def test_lost_write_retried_by_adapter(self):
        self.connect()
        time.sleep(0.8)  # let connect defaults drain first
        fake_gp.configure(sony_lose_writes={"shutterspeed": 1})
        self.controller.set_config_value("shutterspeed", "5")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("shutterspeed", {}).get("value") == "5",
            timeout=12.0,
        ), "the silently-lost shutterspeed write was never retried to success")

    def test_sony_shutter_strings_parse_for_sequences(self):
        from abstractcamera.sequences import parse_shutter_speed_seconds
        self.assertAlmostEqual(parse_shutter_speed_seconds("1/60"), 1 / 60)
        self.assertAlmostEqual(parse_shutter_speed_seconds("13/10"), 1.3)
        self.assertAlmostEqual(parse_shutter_speed_seconds("30"), 30.0)
        with self.assertRaises(Exception):
            parse_shutter_speed_seconds("0/0")
        with self.assertRaises(Exception):
            parse_shutter_speed_seconds("Bulb")


class SonyTriggerAndDownload(SonyCameraHarness):
    def test_manual_trigger_downloads_arw(self):
        self.connect()
        # Manual focus: guaranteed firing (AF gating is a separate test).
        self.controller.set_config_value("focusmode", "Manual")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("focusmode", {}).get("value") == "Manual"))
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in self.controller.get_events()),
            timeout=15.0,
        ), "no photo event after the Sony trigger")
        photo = next(e for e in self.controller.get_events() if e["kind"] == "photo")
        self.assertIn(".ARW", photo["note"])
        self.assertTrue(photo["path"] and os.path.exists(photo["path"]))

    def test_silent_af_refusal_is_reported(self):
        """Hardware truth: in AF modes with no lock the body accepts the
        trigger and never fires. The catch log must SAY so."""
        fake_gp.configure(sony_af_wont_lock=True)
        self.connect()
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "error" and "no file arrived" in e["note"]
                        for e in self.controller.get_events()),
            timeout=15.0,
        ), "the silent AF refusal was never reported")
        self.assertFalse(any(e["kind"] == "photo" for e in self.controller.get_events()))

    def test_liveview_flows_at_sony_resolution(self):
        self.connect()
        self.assertTrue(wait_until(
            lambda: self.controller.get_latest_frame()[1] >= 3), "no live view frames")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["preview_size"] == [1024, 680],
            timeout=6.0,
        ), f"preview_size {self.controller.status()['preview_size']} != [1024, 680]")


class SonyBurst(SonyCameraHarness):
    def test_burst_mode_plans_continuous_drive(self):
        self.connect()
        self.controller.set_capture_mode("burst", burst_hold_s=0.8, burst_speed="Hi")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"]
            .get("capturemode", {}).get("value") == "Continuous Shooting Hi",
            timeout=12.0,
        ), "Continuous drive never settled (prioritymode gate?)")
        status = self.controller.status()
        self.assertEqual(status["burst_hold_s"], 0.8)
        self.assertEqual(status["burst_speed"], "Hi")

    def test_burst_hold_announces_and_downloads_files(self):
        self.connect()
        self.controller.set_capture_mode("burst", burst_hold_s=0.8, burst_speed="Hi")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"]
            .get("capturemode", {}).get("value") == "Continuous Shooting Hi",
            timeout=12.0,
        ))
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: len([e for e in self.controller.get_events() if e["kind"] == "photo"]) >= 3,
            timeout=25.0,
        ), "a held burst must download several files")
        # The capture toggle must be released after the hold (press +
        # release). Photos start landing DURING the hold (fetch-on-announce),
        # so wait for the release rather than asserting instantly.
        fake_camera = fake_gp.get_last_camera()

        def capture_writes():
            return [n for _t, _api, n in fake_camera.config_write_log if n == "capture"]
        self.assertTrue(wait_until(lambda: len(capture_writes()) >= 2, timeout=10.0),
                        "the capture toggle was never released")
        self.assertEqual(len(capture_writes()), 2)
        # NO file may be lost to sdram slot eviction: fetch-on-announce is
        # the fix for the hardware-observed [-1] on burst files 2..N-1.
        time.sleep(1.0)
        failures = [e for e in self.controller.get_events()
                    if e["kind"] == "error" and "failed to fetch" in (e["note"] or "")]
        self.assertEqual(failures, [], f"burst files were lost to slot eviction: {failures}")


class SonyMovieHonesty(SonyCameraHarness):
    def test_video_toggle_reports_unconfirmed(self):
        self.connect()
        self.controller.set_capture_mode("video")
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: self.controller.status()["movie_recording"], timeout=8.0))
        notes = [e["note"] for e in self.controller.get_events() if e["kind"] == "trigger"]
        self.assertTrue(any("REC" in n for n in notes),
                        f"the start note must say recording is unconfirmed: {notes}")
        self.controller.request_trigger()
        self.assertTrue(wait_until(
            lambda: not self.controller.status()["movie_recording"], timeout=8.0))


class SonyEventHygiene(SonyCameraHarness):
    def test_property_noise_stays_out_of_the_catch_log(self):
        fake_gp.configure(sony_property_noise_interval_s=0.05)
        self.connect()
        self.controller.set_config_value("focusmode", "Manual")
        wait_until(lambda: self.controller.status()["config"]
                   .get("focusmode", {}).get("value") == "Manual")
        self.controller.request_trigger()
        wait_until(lambda: any(e["kind"] == "photo" for e in self.controller.get_events()),
                   timeout=15.0)
        time.sleep(1.0)
        noise = [e for e in self.controller.get_events()
                 if e["kind"] == "camera-event" and "PTP" in (e["note"] or "")]
        self.assertEqual(noise, [], f"PTP property noise leaked into the catch log: {noise[:3]}")

    def test_injected_card_full_still_surfaces(self):
        self.connect()
        fake_gp.get_last_camera().inject_event(0.1, fake_gp.GP_EVENT_UNKNOWN, "Memory card full")
        self.controller.set_config_value("focusmode", "Manual")  # forces event pumping
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "camera-event" and "full" in (e["note"] or "").lower()
                        for e in self.controller.get_events()),
            timeout=10.0,
        ), "a real camera-side error was filtered out with the noise")


class SonyIntervalSequence(SonyCameraHarness):
    def test_short_sequence_fires_with_sony_shutter_strings(self):
        self.connect()
        self.controller.set_config_value("focusmode", "Manual")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["config"].get("focusmode", {}).get("value") == "Manual"))
        result = self.controller.start_interval_sequence(interval_s=1.0, count=2)
        self.assertEqual(result["status"], "sequence-armed")
        self.assertTrue(wait_until(
            lambda: self.controller.status()["interval"]["state"] == "complete",
            timeout=20.0,
        ), f"sequence did not complete: {self.controller.status()['interval']}")
        self.assertEqual(self.controller.status()["interval"]["shots_done"], 2)

    def test_af_focus_mode_produces_preflight_warning(self):
        self.connect()  # focusmode=Automatic by default
        wait_until(lambda: self.controller.status()["config"]
                   .get("prioritymode", {}).get("value") == "Application")
        result = self.controller.start_interval_sequence(interval_s=1.0, count=1)
        self.assertIn("warning", result)
        self.assertIn("Manual", result["warning"])
        self.controller.stop_interval_sequence()
        wait_until(lambda: not self.controller.status()["interval"]["state"] in ("armed", "running"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
