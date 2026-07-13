"""CameraHub (concurrent sessions, identity, selection) + the capture
layout (<root>/<device_slug>[/<sequence>]) + the save policy."""

import os
import tempfile
import time
import unittest

from abstractcamera import CameraHub, CameraManager
from abstractcamera.drivers.fake_driver import FakeDriver
from abstractcamera.errors import CameraControlError
from abstractcamera.identity import sanitize_sequence_name, slugify

from test_webcam_family import FakeWebcamDriver


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class Slugs(unittest.TestCase):
    def test_model_slugs(self):
        self.assertEqual(slugify("Sony DSC-A7r IV (Control)"), "sony_dsc_a7r_iv")
        self.assertEqual(slugify("Nikon Z 6II"), "nikon_z_6ii")
        self.assertEqual(slugify("Webcam: MacBook Pro Camera"), "macbook_pro_camera")
        self.assertEqual(slugify("Webcam: Anne-Marie’s iPhone Camera"),
                         "anne_marie_s_iphone_camera")

    def test_sequence_names(self):
        self.assertEqual(sanitize_sequence_name("Orion Nebula #3"), "orion_nebula_3")
        self.assertIsNone(sanitize_sequence_name(""))
        self.assertIsNone(sanitize_sequence_name(None))


class FakeSonyDriver(FakeDriver):
    """Fake driver pinned to the Sony profile so hubs can mix families."""

    def __init__(self):
        import abstractcamera.sim.gphoto2 as sim

        sim.configure(profile="a7r4", sony_settle_delay_s=(0.02, 0.08),
                      sony_busy_window_s=0.02, sony_trigger_block_s=0.05,
                      download_stall_s=(0.01, 0.02), file_added_offset_s=0.05)
        super().__init__(sim)


class CaptureLayout(unittest.TestCase):
    def setUp(self):
        import abstractcamera.sim.gphoto2 as sim

        sim.reset()
        sim.configure(download_stall_s=(0.01, 0.02), trigger_latency_s=0.02,
                      trigger_latency_jitter_s=0.0, file_added_offset_s=0.05)
        self.root = tempfile.mkdtemp(prefix="capture_root_")
        self.manager = CameraManager(driver=FakeDriver())
        self.manager.set_capture_root(self.root)
        self.addCleanup(self.manager.disconnect)

    def test_device_folder_and_sequence_folder(self):
        self.manager.connect()
        status = self.manager.status()
        self.assertEqual(status["device_slug"], "nikon_z_6ii")
        self.assertEqual(status["capture_dir"], os.path.join(self.root, "nikon_z_6ii"))

        self.manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in self.manager.get_events()), timeout=10.0))
        photo = next(e for e in self.manager.get_events() if e["kind"] == "photo")
        self.assertTrue(photo["path"].startswith(os.path.join(self.root, "nikon_z_6ii")))

        # Named sequence: everything nests one level deeper.
        self.manager.set_sequence_name("Orion Test")
        self.assertEqual(self.manager.status()["sequence_name"], "orion_test")
        self.assertEqual(self.manager.status()["capture_dir"],
                         os.path.join(self.root, "nikon_z_6ii", "orion_test"))
        self.manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" and "orion_test" in (e["path"] or "")
                        for e in self.manager.get_events()), timeout=10.0))
        # Clearing restores the device folder.
        self.manager.set_sequence_name(None)
        self.assertEqual(self.manager.status()["capture_dir"],
                         os.path.join(self.root, "nikon_z_6ii"))

    def test_interval_with_sequence_name_files_manifest_in_folder(self):
        self.manager.connect()
        result = self.manager.start_interval_sequence(
            interval_s=1.0, count=2, sequence_name="Star Trails")
        self.assertEqual(result["status"], "sequence-armed")
        self.assertTrue(wait_until(
            lambda: self.manager.status()["interval"]["state"] == "complete", timeout=20.0))
        sequence_dir = os.path.join(self.root, "nikon_z_6ii", "star_trails")
        self.assertTrue(wait_until(
            lambda: os.path.isdir(os.path.join(sequence_dir, "sequences")), timeout=10.0),
            "the manifest must live under the named sequence folder")

    def test_legacy_capture_dir_override_still_wins(self):
        override = tempfile.mkdtemp(prefix="legacy_dir_")
        self.manager.set_capture_dir(override)
        self.manager.connect()
        self.assertEqual(self.manager.status()["capture_dir"], override)


class SavePolicy(unittest.TestCase):
    def setUp(self):
        import abstractcamera.sim.gphoto2 as sim

        sim.reset()
        sim.configure(download_stall_s=(0.01, 0.02), trigger_latency_s=0.02,
                      trigger_latency_jitter_s=0.0, file_added_offset_s=0.05)
        self.manager = CameraManager(driver=FakeDriver())
        self.manager.set_capture_root(tempfile.mkdtemp(prefix="policy_root_"))
        self.addCleanup(self.manager.disconnect)

    def test_device_only_policy_announces_without_downloading(self):
        self.manager.connect()
        self.manager.set_save_policy(download_locally=False)
        self.assertFalse(self.manager.status()["download_locally"])
        self.manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo-pending" and "saved on the camera" in e["note"]
                        for e in self.manager.get_events()), timeout=10.0))
        time.sleep(1.0)
        self.assertEqual(self.manager.status()["downloads_pending"], 0)
        self.assertFalse(any(e["kind"] == "photo" for e in self.manager.get_events()),
                         "no local file may appear under device-only policy")
        # Back to local: downloads resume.
        self.manager.set_save_policy(download_locally=True)
        self.manager.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" for e in self.manager.get_events()), timeout=10.0))

    def test_webcam_refuses_device_only(self):
        manager = CameraManager(driver=FakeWebcamDriver())
        manager.set_capture_root(tempfile.mkdtemp(prefix="webcam_policy_"))
        self.addCleanup(manager.disconnect)
        manager.connect()
        with self.assertRaises(CameraControlError):
            manager.set_save_policy(download_locally=False)


class HubSessions(unittest.TestCase):
    def test_two_families_concurrently(self):
        """A PTP body (fake Nikon) and a webcam live at the same time, each
        with its own worker, selection switching between them."""
        drivers = [FakeDriver(), FakeWebcamDriver()]
        hub = CameraHub(capture_root=tempfile.mkdtemp(prefix="hub_root_"),
                        manager_factory=lambda: CameraManager(driver=drivers.pop(0)))
        self.addCleanup(hub.disconnect_all)

        first = hub.connect()
        self.assertEqual(first["family"], "nikon_z")
        self.assertTrue(first["active"])
        second = hub.connect()
        self.assertEqual(second["family"], "webcam")
        self.assertTrue(second["active"], "the newest connection becomes active")

        statuses = hub.statuses()
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(s["connected"] for s in statuses.values()))
        uids = sorted(statuses)
        self.assertEqual(uids, ["fake_macbook_camera", "nikon_z_6ii"])

        # Both live views flow CONCURRENTLY.
        nikon = hub.manager_for("nikon_z_6ii")
        webcam = hub.manager_for("fake_macbook_camera")
        self.assertTrue(wait_until(lambda: nikon.get_latest_frame()[1] >= 3))
        self.assertTrue(wait_until(lambda: webcam.get_latest_frame()[1] >= 3))

        # Selection addresses the right manager; the other keeps running.
        hub.select("nikon_z_6ii")
        self.assertEqual(hub.manager_for(None), nikon)
        frames_before = webcam.get_latest_frame()[1]
        time.sleep(0.5)
        self.assertGreater(webcam.get_latest_frame()[1], frames_before,
                           "the non-selected camera must keep streaming")

        # Per-device capture folders stay separate.
        nikon.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" and "nikon_z_6ii" in (e["path"] or "")
                        for e in nikon.get_events()), timeout=10.0))
        webcam.request_trigger()
        self.assertTrue(wait_until(
            lambda: any(e["kind"] == "photo" and "fake_macbook_camera" in (e["path"] or "")
                        for e in webcam.get_events()), timeout=10.0))

        # Disconnecting the active camera falls back to the survivor.
        hub.disconnect("nikon_z_6ii")
        self.assertEqual(hub.active_uid, "fake_macbook_camera")
        self.assertEqual(len(hub.statuses()), 1)

    def test_manager_for_unknown_uid_refuses(self):
        hub = CameraHub()
        with self.assertRaises(CameraControlError):
            hub.manager_for("nope")
        with self.assertRaises(CameraControlError):
            hub.manager_for(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
