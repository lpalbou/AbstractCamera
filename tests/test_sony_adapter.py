"""SonyAlphaAdapter unit tests against the simulated A7R IV (no controller,
no worker thread): the write-verify-retry loop, action mapping/choreography,
event classification, capability descriptor, and connect defaults — each
pinned to a behavior MEASURED on the real body (2026-07-12)."""

import os
import sys
import time
import unittest


import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera.adapters import select_adapter
from abstractcamera.adapters.sony_alpha import SonyAlphaAdapter


class SonyAdapterHarness(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(profile="a7r4", time_scale=0.5,
                          sony_settle_delay_s=(0.05, 0.15),
                          sony_busy_window_s=0.05)
        self.camera = fake_gp.Camera()
        self.camera.init()
        self.sunk_events = []
        self.adapter = SonyAlphaAdapter(fake_gp)
        self.adapter.attach(self.camera, lambda et, ed: self.sunk_events.append((et, ed)))

    def tearDown(self):
        fake_gp.reset()


class AdapterSelection(unittest.TestCase):
    def test_model_dispatch(self):
        self.assertEqual(select_adapter("Sony DSC-A7r IV (Control)", fake_gp).family, "sony_alpha")
        self.assertEqual(select_adapter("ILCE-7M3", fake_gp).family, "sony_alpha")
        self.assertEqual(select_adapter("Nikon Z 6II", fake_gp).family, "nikon_z")
        self.assertEqual(select_adapter("Canon EOS R5", fake_gp).family, "generic")
        self.assertEqual(select_adapter(None, fake_gp).family, "generic")


class WriteVerifyRetry(SonyAdapterHarness):
    def test_write_settles_and_reports_settled(self):
        receipt = self.adapter.write_widget(self.camera, "iso", "1600", time_budget_s=6.0)
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.settled, "verified write must report settled")
        self.assertEqual(self.camera.get_single_config("iso").get_value(), "1600")

    def test_silently_lost_write_is_retried_until_it_lands(self):
        """Measured on the body: a write can be accepted and never settle;
        the identical retry lands. The adapter must retry, not trust."""
        fake_gp.configure(sony_lose_writes={"shutterspeed": 1})
        receipt = self.adapter.write_widget(self.camera, "shutterspeed", "5", time_budget_s=8.0)
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.settled, "the retry after the lost write must settle")
        self.assertEqual(self.camera.get_single_config("shutterspeed").get_value(), "5")

    def test_busy_bad_parameters_is_retried_after_backoff(self):
        fake_gp.configure(sony_busy_window_s=0.6)
        first = self.adapter.write_widget(self.camera, "iso", "3200", time_budget_s=6.0)
        self.assertTrue(first.settled)
        # Immediately-following write hits the busy window; the adapter
        # must absorb the [-2] and land it.
        second = self.adapter.write_widget(self.camera, "f-number", "f/8", time_budget_s=6.0)
        self.assertTrue(second.ok)
        self.assertTrue(second.settled)
        self.assertEqual(self.camera.get_single_config("f-number").get_value(), "f/8")

    def test_budget_expiry_returns_unsettled_not_a_lie(self):
        fake_gp.configure(sony_lose_writes={"iso": 99})  # never lands
        receipt = self.adapter.write_widget(self.camera, "iso", "6400", time_budget_s=1.0)
        self.assertFalse(receipt.settled, "an unconfirmed write must not claim settled")

    def test_range_widget_write_settles(self):
        receipt = self.adapter.write_widget(self.camera, "colortemperature", 5500, time_budget_s=6.0)
        self.assertTrue(receipt.settled)
        self.assertEqual(float(self.camera.get_single_config("colortemperature").get_value()), 5500.0)


class ConnectDefaults(SonyAdapterHarness):
    def test_prioritymode_application_requested(self):
        cache = self.adapter.read_config_cache(self.camera)
        defaults = {d.name: d for d in self.adapter.connect_default_writes(cache)}
        self.assertIn("prioritymode", defaults)
        self.assertEqual(defaults["prioritymode"].value, "Application")
        self.assertTrue(defaults["prioritymode"].ledger)

    def test_card_plus_sdram_is_left_alone(self):
        """'card+sdram' contains the substring 'ram': the old generic rule
        would rewrite it. The Sony rule matches 'sdram' EXACTLY."""
        cache = self.adapter.read_config_cache(self.camera)
        self.assertEqual(cache["capturetarget"]["value"], "card+sdram")
        defaults = [d.name for d in self.adapter.connect_default_writes(cache)]
        self.assertNotIn("capturetarget", defaults)

    def test_sdram_only_is_rewritten_to_card_sdram(self):
        fake_gp.configure(widget_overrides={"capturetarget": "sdram"})
        camera = fake_gp.Camera()
        camera.init()
        cache = self.adapter.read_config_cache(camera)
        defaults = {d.name: d for d in self.adapter.connect_default_writes(cache)}
        self.assertEqual(defaults["capturetarget"].value, "card+sdram")

    def test_no_isoauto_default_emitted(self):
        cache = self.adapter.read_config_cache(self.camera)
        self.assertNotIn("isoauto", cache)
        defaults = [d.name for d in self.adapter.connect_default_writes(cache)]
        self.assertNotIn("isoauto", defaults)


class Capabilities(SonyAdapterHarness):
    def test_descriptor_shape(self):
        cache = self.adapter.read_config_cache(self.camera)
        caps = self.adapter.capabilities(cache)
        self.assertEqual(caps["family"], "sony_alpha")
        self.assertEqual(caps["burst"]["mode"], "duration")
        self.assertFalse(caps["movie"]["can_confirm"])
        self.assertEqual(caps["iso_auto"], {"kind": "choice", "auto_choice": "Auto ISO"})
        self.assertEqual(caps["save_to"]["volatile_values"], ["sdram"])
        self.assertTrue(caps["preview_during_exposure"])
        self.assertTrue(caps["focus"]["mf_requires_manual_focus"])

    def test_sequence_preflight_warns_on_af_focus(self):
        cache = self.adapter.read_config_cache(self.camera)
        warnings = self.adapter.sequence_preflight_warnings(cache)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Manual", warnings[0])
        cache["focusmode"]["value"] = "Manual"
        self.assertEqual(self.adapter.sequence_preflight_warnings(cache), [])


class Actions(SonyAdapterHarness):
    def test_manual_focus_requires_manual_mode(self):
        cache = self.adapter.read_config_cache(self.camera)  # focusmode=Automatic
        receipt = self.adapter.run_action(self.camera, "manualfocusdrive", "100", cache)
        self.assertFalse(receipt.ok)
        self.assertIn("Manual", receipt.error)

    def test_manual_focus_scales_ui_units_to_step_codes(self):
        settle = self.adapter.write_widget(self.camera, "focusmode", "Manual", time_budget_s=6.0)
        self.assertTrue(settle.settled)
        cache = self.adapter.read_config_cache(self.camera)
        position_before = float(self.camera.get_single_config("focalposition").get_value())
        receipt = self.adapter.run_action(self.camera, "manualfocusdrive", "500", cache)
        self.assertTrue(receipt.ok, receipt.error)
        position_after = float(self.camera.get_single_config("focalposition").get_value())
        self.assertNotEqual(position_before, position_after, "the lens must move")

    def test_af_drive_presses_and_releases(self):
        cache = self.adapter.read_config_cache(self.camera)
        receipt = self.adapter.run_action(self.camera, "autofocusdrive", None, cache)
        self.assertTrue(receipt.ok, receipt.error)
        # The half-press must not be left held (a stuck half-press blocks
        # later triggers on the real body).
        writes = [n for _t, _api, n in self.camera.config_write_log if n == "autofocus"]
        self.assertEqual(len(writes), 2, "autofocus must be pressed AND released")

    def test_unknown_action_refused(self):
        receipt = self.adapter.run_action(self.camera, "selftimer", None, {})
        self.assertFalse(receipt.ok)


class EventClassification(SonyAdapterHarness):
    def test_property_noise_is_classified_noise(self):
        classified = self.adapter.classify_event(
            fake_gp.GP_EVENT_UNKNOWN,
            'PTP Property d20e changed, "PTP Property 0x0000" to "Unknown"')
        self.assertEqual(classified.kind, "noise")
        classified = self.adapter.classify_event(
            fake_gp.GP_EVENT_UNKNOWN, "PTP Event c202, Param1 ffffc001")
        self.assertEqual(classified.kind, "noise")

    def test_card_full_still_surfaces(self):
        classified = self.adapter.classify_event(
            fake_gp.GP_EVENT_UNKNOWN, "Memory card full")
        self.assertEqual(classified.kind, "status")

    def test_file_added_and_capture_complete(self):
        class _Data:
            folder, name = "/", "capt_A7R0001.ARW"
        classified = self.adapter.classify_event(fake_gp.GP_EVENT_FILE_ADDED, _Data())
        self.assertEqual(classified.kind, "file_added")
        self.assertEqual(classified.folder, "/")
        classified = self.adapter.classify_event(fake_gp.GP_EVENT_CAPTURE_COMPLETE, None)
        self.assertEqual(classified.kind, "noise")


class FireSemantics(SonyAdapterHarness):
    def test_fire_single_af_mode_carries_refusal_watch(self):
        cache = self.adapter.read_config_cache(self.camera)
        timing = self.adapter.fire_single(self.camera, 0.02, cache)
        self.assertEqual(timing.preview_pause_s, 0.0, "Sony preview survives exposures")
        self.assertIsNotNone(timing.expect_file_within_s)
        self.assertIn("Manual", timing.no_file_note)

    def test_fire_single_manual_mode_keeps_a_drop_watch(self):
        """Hardware truth (2026-07-12): the body intermittently drops
        accepted triggers even in Manual focus — the watch arms in EVERY
        mode, with mode-specific honest copy."""
        self.adapter.write_widget(self.camera, "focusmode", "Manual", time_budget_s=6.0)
        cache = self.adapter.read_config_cache(self.camera)
        timing = self.adapter.fire_single(self.camera, 0.02, cache)
        self.assertIsNotNone(timing.expect_file_within_s)
        self.assertIn("silently dropped", timing.no_file_note)
        self.assertNotIn("autofocus cannot lock", timing.no_file_note)

    def test_fire_burst_press_hold_release_announces_files(self):
        # Drive must stick: Application priority first (measured gate).
        self.assertTrue(self.adapter.write_widget(
            self.camera, "prioritymode", "Application", time_budget_s=6.0).settled)
        self.assertTrue(self.adapter.write_widget(
            self.camera, "capturemode", "Continuous Shooting Hi", time_budget_s=6.0).settled)
        timing = self.adapter.fire_burst(self.camera, 0.02, count=0, hold_s=0.8)
        self.assertEqual(timing.preview_pause_s, 0.0)
        self.assertIsNone(timing.expected_files, "Sony burst count is body-decided")
        # The release must ALWAYS be written.
        writes = [n for _t, _api, n in self.camera.config_write_log if n == "capture"]
        self.assertEqual(len(writes), 2, "capture must be pressed AND released")
        # Drain the tail: files must arrive (announced via the sink or drained now).
        deadline = time.time() + 6.0
        files = [ed.name for et, ed in self.sunk_events
                 if et == fake_gp.GP_EVENT_FILE_ADDED]
        while time.time() < deadline:
            event_type, event_data = self.camera.wait_for_event(50)
            if event_type == fake_gp.GP_EVENT_FILE_ADDED:
                files.append(event_data.name)
        self.assertGreaterEqual(len(files), 2, f"a held burst must yield several files, got {files}")

    def test_movie_receipt_is_honest(self):
        receipt = self.adapter.toggle_movie(self.camera, start=True)
        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.confirmed, "Sony cannot confirm recording over USB")
        self.assertIn("REC", receipt.note)
        receipt = self.adapter.toggle_movie(self.camera, start=False)
        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.confirmed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
