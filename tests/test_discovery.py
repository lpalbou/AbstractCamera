"""Discovery and driver resolution (adjudication Mod #11): fake-mode
replacement, PTP-first default ordering, id routing, stale-id refusal, and
the built-in-over-Continuity webcam preference."""

import os
import unittest
from unittest import mock

from abstractcamera import discovery
from abstractcamera.errors import CameraControlError


class FakeModeResolution(unittest.TestCase):
    def test_fake_env_replaces_all_transports(self):
        with mock.patch.dict(os.environ, {"ABSTRACTCAMERA_FAKE": "1"}):
            drivers = discovery.resolve_drivers()
            self.assertEqual([d.driver_id for d in drivers], ["fake"])
            self.assertTrue(discovery.is_tethering_available())

    def test_fake_listing_carries_ids(self):
        with mock.patch.dict(os.environ, {"ABSTRACTCAMERA_FAKE": "1"}):
            entries = discovery.list_cameras()
            self.assertTrue(entries)
            self.assertTrue(entries[0]["id"].startswith("fake:"))
            self.assertTrue(entries[0]["default"])


class DefaultOrdering(unittest.TestCase):
    def _entries(self, *entries):
        return list(entries)

    def test_ptp_first(self):
        entries = self._entries(
            {"id": "webcam:0", "transport": "webcam", "name": "MacBook Pro Camera"},
            {"id": "ptp:usb:002,001", "transport": "ptp", "name": "Sony DSC-A7r IV"},
        )
        self.assertEqual(discovery._default_entry(entries)["id"], "ptp:usb:002,001")

    def test_builtin_preferred_over_continuity_iphone(self):
        entries = self._entries(
            {"id": "webcam:0", "transport": "webcam", "name": "Alex's iPhone Camera",
             "kind": "continuity"},
            {"id": "webcam:1", "transport": "webcam", "name": "MacBook Pro Camera",
             "kind": "built_in"},
        )
        self.assertEqual(discovery._default_entry(entries)["id"], "webcam:1",
                         "never silently pick the user's phone")

    def test_kind_fallback_from_names(self):
        """Entries from drivers without a kind field rank by name heuristics."""
        entries = self._entries(
            {"id": "webcam:0", "transport": "webcam", "name": "Alex's iPhone Camera"},
            {"id": "webcam:1", "transport": "webcam", "name": "MacBook Pro Camera"},
        )
        self.assertEqual(discovery._default_entry(entries)["id"], "webcam:1")

    def test_continuity_only_machine_still_gets_a_default(self):
        entries = self._entries(
            {"id": "webcam:0", "transport": "webcam", "name": "Alex's iPhone Camera",
             "kind": "continuity"},
        )
        self.assertEqual(discovery._default_entry(entries)["id"], "webcam:0",
                         "the only camera is a valid default — the picker labels it")

    def test_no_cameras_gives_none(self):
        self.assertIsNone(discovery._default_entry([]))


class DeviceKindClassification(unittest.TestCase):
    """ADR 0009 layering: structured signals outrank names; names remain the
    last fallback (the 2026-07-12 inversion lesson)."""

    def test_structured_signals_first(self):
        from abstractcamera.drivers.avf_enum import classify_kind

        self.assertEqual(
            classify_kind("AVCaptureDeviceTypeBuiltInWideAngleCamera", "", "Whatever", None),
            "built_in")
        self.assertEqual(
            classify_kind("AVCaptureDeviceTypeContinuityCamera", "", "Mystery", None),
            "continuity")
        self.assertEqual(classify_kind("AVCaptureDeviceTypeExternal", "", "Cam", True),
                         "continuity", "the isContinuityCamera selector is authoritative")
        self.assertEqual(classify_kind("AVCaptureDeviceTypeExternal", "iPhone16,1", "Cam", None),
                         "continuity", "modelID identifies phones typed as plain External")

    def test_name_fallback_last(self):
        from abstractcamera.drivers.avf_enum import classify_kind

        # The measured 2026-07-12 case: iPhone typed "External", no selector.
        self.assertEqual(
            classify_kind("AVCaptureDeviceTypeExternal", "", "Anne-Marie's iPhone Camera", None),
            "continuity")
        self.assertEqual(classify_kind("", "", "FaceTime HD Camera", None), "built_in")
        self.assertEqual(classify_kind("AVCaptureDeviceTypeExternal", "", "Logitech BRIO", None),
                         "external")


class DriverRouting(unittest.TestCase):
    def test_unknown_prefix_refuses_honestly(self):
        with mock.patch.dict(os.environ, {"ABSTRACTCAMERA_FAKE": "1"}):
            with self.assertRaises(CameraControlError):
                discovery.resolve_driver_for("webcam:0")  # fake mode: no webcam driver

    def test_default_resolution_prefers_first_driver(self):
        with mock.patch.dict(os.environ, {"ABSTRACTCAMERA_FAKE": "1"}):
            driver = discovery.resolve_driver_for(None)
            self.assertEqual(driver.driver_id, "fake")


if __name__ == "__main__":
    unittest.main(verbosity=2)
