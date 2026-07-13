"""Session-protocol conformance (adjudication Mod #4): the BEHAVIORAL
contract items the manager loop relies on, executable and parametrized over
transports. A session that 'politely never raises' from capture_preview
would silently rewire the liveness watchdog — these semantics are
load-bearing, not stylistic.

Also the Mod #2 guard: the manager core must never reference a gphoto2
module attribute — wire.py is the constants source, so a webcam-only
([gphoto2]-less) install can never AttributeError in the drain loops.
"""

import os
import pathlib
import re
import time
import unittest

import abstractcamera.sim.gphoto2 as sim_gp
from abstractcamera import wire
from abstractcamera.session import CameraSession


def make_sim_session(profile: str):
    sim_gp.reset()
    sim_gp.configure(profile=profile)
    camera = sim_gp.Camera()
    return camera


class WireConstantsPinned(unittest.TestCase):
    def test_values_match_libgphoto2(self):
        """The numeric pin (ADR 0001). When the real library is installed,
        assert against it; the sim asserts always."""
        for module in filter(None, (sim_gp, _real_gp())):
            self.assertEqual(module.GP_EVENT_UNKNOWN, wire.GP_EVENT_UNKNOWN)
            self.assertEqual(module.GP_EVENT_TIMEOUT, wire.GP_EVENT_TIMEOUT)
            self.assertEqual(module.GP_EVENT_FILE_ADDED, wire.GP_EVENT_FILE_ADDED)
            self.assertEqual(module.GP_EVENT_CAPTURE_COMPLETE, wire.GP_EVENT_CAPTURE_COMPLETE)
            self.assertEqual(module.GP_FILE_TYPE_NORMAL, wire.GP_FILE_TYPE_NORMAL)
            self.assertEqual(module.GP_WIDGET_RANGE, wire.GP_WIDGET_RANGE)
            self.assertEqual(module.GP_WIDGET_TOGGLE, wire.GP_WIDGET_TOGGLE)
            self.assertEqual(module.GP_WIDGET_RADIO, wire.GP_WIDGET_RADIO)


def _real_gp():
    try:
        import gphoto2

        return gphoto2
    except ImportError:
        return None


class NoModuleLevelGpReferences(unittest.TestCase):
    CORE_MODULES = ("camera_manager.py", "worker.py", "config_ledger.py",
                    "capture_ops.py", "downloads.py", "detection_runner.py",
                    "clips.py", "jpeg.py", "discovery.py", "sequences.py")

    def test_core_never_references_a_gp_module_attribute(self):
        """All 16 historic `gp.` sites must be gone from the family-agnostic
        core (they live in wire.py, the drivers, or adapter-local transport
        aliases now)."""
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "abstractcamera"
        pattern = re.compile(r"(?<![\w._])gp\.")
        offenders = []
        for name in self.CORE_MODULES:
            for lineno, line in enumerate((src / name).read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if pattern.search(code):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "module-level gp.* references would crash webcam-only installs")


class SessionConformance(unittest.TestCase):
    """Behavioral items, run against both simulator personalities. The
    webcam session runs the same assertions against its FakeFrameSource
    double in test_webcam_family (ADR 0009 seam)."""

    PROFILES = ("z6ii", "a7r4")

    def tearDown(self):
        sim_gp.reset()

    def test_satisfies_structural_protocol(self):
        for profile in self.PROFILES:
            session = make_sim_session(profile)
            self.assertIsInstance(session, CameraSession)

    def test_wait_for_event_times_out_instead_of_raising(self):
        for profile in self.PROFILES:
            session = make_sim_session(profile)
            session.init()
            event_type, event_data = session.wait_for_event(20)
            self.assertEqual(event_type, wire.GP_EVENT_TIMEOUT)
            self.assertIsNone(event_data)
            session.exit()

    def test_capture_preview_raises_while_unservable(self):
        """The watchdog counts raises; a session that returns stale frames
        instead would silently disable USB-pull detection."""
        sim_gp.reset()
        sim_gp.configure(profile="z6ii", preview_fail=True)
        session = sim_gp.Camera()
        with self.assertRaises(Exception):
            session.init()  # simulated absent camera refuses init
        sim_gp.reset()
        sim_gp.configure(profile="z6ii", busy_nak=True, trigger_latency_s=0.0,
                         trigger_latency_jitter_s=0.0)
        session = sim_gp.Camera()
        session.init()
        # Nikon semantics: preview REFUSES during an exposure.
        widget = session.get_single_config("shutterspeed")
        widget.set_value("2s")
        session.set_single_config("shutterspeed", widget)
        session.trigger_capture()
        with self.assertRaises(Exception):
            session.capture_preview()
        session.exit()

    def test_preview_survives_exposure_on_sony(self):
        session = make_sim_session("a7r4")
        session.init()
        session.trigger_capture()
        jpeg = bytes(session.capture_preview().get_data_and_size())
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        session.exit()

    def test_file_added_round_trips_to_file_get(self):
        for profile in self.PROFILES:
            session = make_sim_session(profile)
            session.init()
            if profile == "a7r4":  # Manual focus: guaranteed firing
                widget = session.get_single_config("focusmode")
                widget.set_value("Manual")
                session.set_single_config("focusmode", widget)
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    session.wait_for_event(30)
                    if session.get_single_config("focusmode").get_value() == "Manual":
                        break
            session.trigger_capture()
            deadline = time.time() + 10.0
            payload = None
            while time.time() < deadline:
                event_type, event_data = session.wait_for_event(50)
                if event_type == wire.GP_EVENT_FILE_ADDED:
                    payload = event_data
                    break
            self.assertIsNotNone(payload, f"{profile}: no FILE_ADDED after trigger")
            self.assertTrue(hasattr(payload, "folder") and hasattr(payload, "name"))
            data = bytes(session.file_get(payload.folder, payload.name,
                                          wire.GP_FILE_TYPE_NORMAL).get_data_and_size())
            self.assertGreater(len(data), 100)
            session.exit()

    def test_preview_blocks_roughly_one_frame_interval(self):
        session = make_sim_session("z6ii")
        session.init()
        session.capture_preview()
        started = time.perf_counter()
        for _ in range(5):
            session.capture_preview()
        per_frame = (time.perf_counter() - started) / 5
        self.assertGreater(per_frame, 0.005, "preview must pace the loop")
        session.exit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
