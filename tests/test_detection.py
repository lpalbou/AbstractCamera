"""Gates for the live-view detection pipelines (backend/detection.py) and
their controller integration (target dispatch, flood control, ring clip,
pending-write ledger, movie prohibit).

The synthetic gates prove the LOGIC (accept/reject geometry, debounce,
gain immunity, budgets); real-sky rates are unvalidated and labeled so.
"""

import os
import sys
import tempfile
import time
import unittest

import numpy as np
import cv2


import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera import CameraControlError, CameraManager as CameraController, parse_jpeg_dimensions
from abstractcamera.drivers.fake_driver import FakeDriver
from abstractcamera.detection import MeteorDetector, MotionDetector

RNG = np.random.default_rng(9)
H, W = 424, 640


def sky_frame():
    return RNG.normal(12, 3, (H, W)).clip(0, 255).astype(np.uint8)


def settle(det, t0=0.0, frames=45, dt=1 / 30):
    t = t0
    for _ in range(frames):
        det.process(sky_frame(), now=t)
        t += dt
    return t


class MeteorGates(unittest.TestCase):
    def test_accepts_fast_streak_with_metrics(self):
        det = MeteorDetector(sensitivity=50)
        t = settle(det)
        event = None
        for i in range(12):
            f = sky_frame()
            x, y = int(100 + 8 * i), int(100 + 4 * i)
            cv2.line(f, (x - 16, y - 8), (x, y), 200, 2)
            event = det.process(f, now=t) or event
            t += 1 / 30
        self.assertIsNotNone(event, "8px/frame streak not detected")
        self.assertEqual(event.kind, "meteor")
        # Metrics within 15% of the injected geometry (referee gate).
        self.assertAlmostEqual(event.metrics["speed_px_frame"], 8.9, delta=8.9 * 0.15)
        self.assertAlmostEqual(event.metrics["angle_deg"], 27.0, delta=8.0)

    def test_rejects_slow_satellite(self):
        det = MeteorDetector(sensitivity=50)
        t = settle(det)
        events = 0
        for i in range(150):  # 5s at 0.4 px/frame
            f = sky_frame()
            x = int(100 + 0.4 * i)
            cv2.line(f, (x - 10, 200), (x, 200), 200, 2)
            if det.process(f, now=t):
                events += 1
            t += 1 / 30
        self.assertEqual(events, 0, "slow steady object (satellite/plane) fired the meteor detector")

    def test_rejects_blinking_strobe(self):
        det = MeteorDetector(sensitivity=50)
        t = settle(det)
        events = 0
        for i in range(120):  # plane strobe: same locus, 1Hz blink
            f = sky_frame()
            if (i // 15) % 2 == 0:
                cv2.circle(f, (300 + i // 4, 180), 3, 220, -1)
            if det.process(f, now=t):
                events += 1
            t += 1 / 30
        self.assertEqual(events, 0, "blinking strobe fired the meteor detector")

    def test_rejects_global_gain_step(self):
        det = MeteorDetector(sensitivity=50)
        t = settle(det)
        events = 0
        for i in range(30):
            f = (sky_frame().astype(np.float32) * 1.8).clip(0, 255).astype(np.uint8)
            if det.process(f, now=t):
                events += 1
            t += 1 / 30
        self.assertEqual(events, 0, "exposure/AWB gain step fired the meteor detector")

    def test_reseeds_after_preview_gap(self):
        """The first frame after an exposure pause must not diff against a
        stale background and fire a full-frame event."""
        det = MeteorDetector(sensitivity=50)
        t = settle(det)
        t += 8.0  # exposure pause
        events = 0
        for i in range(20):
            brighter = (sky_frame().astype(np.float32) + 15).clip(0, 255).astype(np.uint8)
            if det.process(brighter, now=t):
                events += 1
            t += 1 / 30
        self.assertEqual(events, 0)

    def test_budget(self):
        det = MeteorDetector()
        frame = sky_frame()
        settle(det)
        t0 = time.perf_counter()
        for i in range(100):
            det.process(frame, now=100 + i / 30)
        per_frame_ms = (time.perf_counter() - t0) / 100 * 1000
        self.assertLess(per_frame_ms, 8.0, f"meteor {per_frame_ms:.2f}ms/frame")


class MotionGates(unittest.TestCase):
    def test_triggers_on_blob_with_debounce(self):
        det = MotionDetector(sensitivity=50)
        t = settle(det, frames=70)
        events = []
        for i in range(10):
            f = sky_frame()
            cv2.rectangle(f, (200 + i * 4, 150), (330 + i * 4, 260), 160, -1)
            e = det.process(f, now=t)
            if e:
                events.append((i, e))
            t += 1 / 30
        self.assertTrue(events, "moving blob did not trigger motion")
        first_index, event = events[0]
        self.assertGreaterEqual(first_index, 2, "debounce (3 consecutive frames) not honored")
        self.assertGreater(event.metrics["fraction_pct"], 1.0)
        self.assertGreater(event.metrics["bbox"][2], 60)

    def test_ignores_global_flicker(self):
        det = MotionDetector(sensitivity=50)
        t = settle(det, frames=70)
        events = 0
        for i in range(30):
            factor = 1.6 if i % 2 else 1.0
            f = (sky_frame().astype(np.float32) * factor).clip(0, 255).astype(np.uint8)
            if det.process(f, now=t):
                events += 1
            t += 1 / 30
        self.assertEqual(events, 0, "global exposure flicker fired the motion detector")

    def test_cooldown(self):
        det = MotionDetector(sensitivity=50)
        t = settle(det, frames=70)
        events = 0
        for i in range(90):  # 3s of continuous motion
            f = sky_frame()
            cv2.rectangle(f, (150 + i * 2, 150), (300 + i * 2, 260), 160, -1)
            if det.process(f, now=t):
                events += 1
            t += 1 / 30
        self.assertLessEqual(events, 4, f"cooldown not honored ({events} events in 3s)")

    def test_budget(self):
        det = MotionDetector()
        frame = sky_frame()
        settle(det, frames=70)
        t0 = time.perf_counter()
        for i in range(100):
            det.process(frame, now=100 + i / 30)
        per_frame_ms = (time.perf_counter() - t0) / 100 * 1000
        self.assertLess(per_frame_ms, 8.0, f"motion {per_frame_ms:.2f}ms/frame")

    def test_high_sensitivity_faster_debounce(self):
        """Owner failure mode: a fast subject visible for ~150ms (a few
        frames) never survived the 3-frame debounce. At sensitivity >= 95 a
        single confirming frame fires (explicit false-positive trade)."""
        det = MotionDetector(sensitivity=95)
        t = settle(det, frames=70)
        events = []
        for i in range(4):  # ~130ms crossing at 30fps
            f = sky_frame()
            cv2.rectangle(f, (200 + i * 30, 150), (330 + i * 30, 260), 160, -1)
            e = det.process(f, now=t)
            if e:
                events.append((i, e))
            t += 1 / 30
        self.assertTrue(events, "fast crossing not detected at sensitivity 95")
        self.assertLessEqual(events[0][0], 1, "sensitivity 95 should confirm on the first frame")

    def test_no_reseed_storm_on_steady_scene(self):
        """Regression (owner: 'it takes too much time to shoot', 2026-07-07):
        the gain-normalization reference fed on uint8-truncated normalized
        means — on dark scenes the feedback loop dragged the reference down
        ~6%/pass, the 10% gain guard re-seeded ~70×/10s, and the detector
        was suppressed (blind) most of the time. A steady noisy scene must
        produce ZERO re-seeds after warmup."""
        det = MotionDetector(sensitivity=95)
        t = settle(det, frames=70)
        reseeds = 0
        for i in range(300):
            before = det._suppress_until
            det.process(sky_frame(), now=t)
            if det._suppress_until > before:
                reseeds += 1
            t += 1 / 30
        self.assertEqual(reseeds, 0, f"{reseeds} spurious re-seeds on a steady scene")

    def test_detection_within_two_frames_at_high_sensitivity(self):
        """Latency gate: at sensitivity 95, a clear subject must produce an
        event within 2 processed frames of appearing (the reseed storm used
        to stretch this to ~30 frames)."""
        det = MotionDetector(sensitivity=95)
        t = settle(det, frames=70)
        for i in range(3):
            f = sky_frame()
            cv2.rectangle(f, (200, 150), (330, 260), 180, -1)
            if det.process(f, now=t) is not None:
                self.assertLessEqual(i, 1, "event later than 2 frames")
                return
            t += 1 / 30
        self.fail("no event within 3 frames of a clear subject")

    def test_mid_sensitivity_two_frame_debounce(self):
        det = MotionDetector(sensitivity=80)
        t = settle(det, frames=70)
        events = []
        for i in range(6):
            f = sky_frame()
            cv2.rectangle(f, (200 + i * 10, 150), (330 + i * 10, 260), 160, -1)
            e = det.process(f, now=t)
            if e:
                events.append(i)
            t += 1 / 30
        self.assertTrue(events, "crossing not detected at sensitivity 80")
        self.assertEqual(events[0], 1, "sensitivity 80 should confirm on the second frame")


class JpegDimensionGates(unittest.TestCase):
    def test_sof_parse(self):
        for (w, h) in ((640, 424), (1024, 680), (320, 212)):
            ok, encoded = cv2.imencode(".jpg", np.zeros((h, w, 3), np.uint8))
            self.assertTrue(ok)
            self.assertEqual(parse_jpeg_dimensions(encoded.tobytes()), (w, h))
        self.assertIsNone(parse_jpeg_dimensions(b"not a jpeg"))
        self.assertIsNone(parse_jpeg_dimensions(b""))


class ControllerIntegrationGates(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(download_stall_s=(0.005, 0.02), trigger_latency_s=0.05,
                          file_added_offset_s=0.05)
        self.controller = CameraController(driver=FakeDriver(fake_gp))
        self.capture_dir = tempfile.mkdtemp(prefix="det_captures_")
        self.controller.set_capture_dir(self.capture_dir)

    def tearDown(self):
        try:
            self.controller.disconnect()
        finally:
            fake_gp.reset()

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_target_and_sensitivity_roundtrip(self):
        status = self.controller.set_detection_mode("monitor", target="meteor", sensitivity=70)
        self.assertEqual(status["detection_mode"], "monitor")
        self.assertEqual(status["detection_target"], "meteor")
        self.assertEqual(status["detection_sensitivity"], 70.0)
        status = self.controller.set_detection_mode("monitor", target="motion")
        self.assertEqual(status["detection_sensitivity"], 50.0)  # per-target
        with self.assertRaises(CameraControlError):
            self.controller.set_detection_mode("monitor", target="ufo")

    def test_meteor_detection_end_to_end_with_clip(self):
        """Scripted streak in the fake preview -> detection event with a
        saved ring-buffer clip (the artifact that CONTAINS the meteor)."""
        self.controller.connect()
        self.controller.set_detection_mode("monitor", target="meteor", sensitivity=60)
        # Let the detector settle, then inject a fast streak.
        time.sleep(1.5)
        fake_gp.configure(inject_streaks=[{
            "t0": time.time() - fake_gp._preview_epoch + 0.3,
            "x0": 80, "y0": 80, "angle_deg": 30,
            "speed_px_s": 260, "duration_s": 0.5, "brightness": 235,
        }])
        found = self.wait_for(lambda: any(
            e["kind"] == "detection" and e["reason"] == "meteor"
            for e in self.controller.get_events()
        ), timeout=12.0)
        self.assertTrue(found, f"no meteor event; events: {[(e['kind'], e['note'][:40]) for e in self.controller.get_events()]}")
        meteor_events = [e for e in self.controller.get_events()
                         if e["kind"] == "detection" and e["reason"] == "meteor"]
        with_clip = [e for e in meteor_events if e.get("path")]
        self.assertTrue(with_clip, "no ring-buffer clip saved")
        self.assertTrue(os.path.exists(with_clip[0]["path"]))
        self.assertIn("meteor-clips", with_clip[0]["path"])

    def test_motion_detection_end_to_end(self):
        self.controller.connect()
        self.controller.set_detection_mode("monitor", target="motion", sensitivity=70)
        time.sleep(2.5)
        fake_gp.configure(inject_motion_blobs=[{
            "t0": time.time() - fake_gp._preview_epoch + 0.3,
            "x": 200, "y": 150, "w": 120, "h": 90,
            "duration_s": 2.0, "brightness": 170,
        }])
        found = self.wait_for(lambda: any(
            e["kind"] == "detection" and e["reason"] == "motion"
            for e in self.controller.get_events()
        ), timeout=12.0)
        self.assertTrue(found, "no motion event on scripted blob")
        # The clip is the artifact that shows the subject (cat-passing test):
        # motion events must carry a saved ring-buffer clip like meteors do.
        motion_events = [e for e in self.controller.get_events()
                         if e["kind"] == "detection" and e["reason"] == "motion"]
        with_clip = [e for e in motion_events if e.get("path")]
        self.assertTrue(with_clip, "no ring-buffer clip saved for motion")
        self.assertTrue(os.path.exists(with_clip[0]["path"]))
        self.assertIn("motion-clips", with_clip[0]["path"])

    def test_gain_step_triggers_nothing(self):
        self.controller.connect()
        self.controller.set_detection_mode("monitor", target="motion", sensitivity=70)
        time.sleep(2.5)
        fake_gp.configure(gain_step={
            "t0": time.time() - fake_gp._preview_epoch + 0.2,
            "duration_s": 2.0, "factor": 1.8,
        })
        time.sleep(3.0)
        detections = [e for e in self.controller.get_events() if e["kind"] == "detection"]
        self.assertEqual(len(detections), 0, f"gain step fired: {[e['note'] for e in detections]}")

    def test_preview_size_signal(self):
        self.controller.connect()
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["preview_size"] == [640, 424], timeout=6.0))
        self.controller.set_config_value("liveviewsize", "XGA")
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["preview_size"] == [1024, 680], timeout=10.0),
            f"preview_size stuck at {self.controller.status()['preview_size']}")

    def test_pending_write_ledger_confirm_and_revert(self):
        fake_gp.configure(revert_writes=["expprogram"])
        self.controller.connect()
        # Accepted write -> confirmed and removed from the ledger.
        self.controller.set_config_value("iso", "1600")
        self.assertTrue(self.wait_for(
            lambda: "iso" not in self.controller.status()["pending_writes"], timeout=8.0))
        self.assertEqual(self.controller.status()["config"]["iso"]["value"], "1600")
        # Dial-controlled write -> REVERTED with a visible event.
        self.controller.set_config_value("expprogram", "P")
        self.assertTrue(self.wait_for(
            lambda: any(
                e["kind"] == "error" and "reverted" in e["note"] and "mode dial" in e["note"]
                for e in self.controller.get_events()
            ),
            timeout=25.0,
        ), f"no revert event; ledger: {self.controller.status()['pending_writes']}")

    def test_auto_fire_defers_downloads_and_keeps_detecting(self):
        """The critical stall (owner 2026-07-07): a 1-3s NEF download on the
        worker thread blinded detection after every auto-fire. While armed,
        downloads must be DEFERRED (announced as photo-pending) and preview
        frames must keep flowing; disarming flushes the queue to disk."""
        fake_gp.configure(download_stall_s=(1.2, 1.5), trigger_latency_s=0.05,
                          file_added_offset_s=0.1)
        self.controller.connect()
        self.controller.set_detection_mode("auto", target="motion", sensitivity=70)
        time.sleep(2.5)  # detector settle
        fake_gp.configure(inject_motion_blobs=[{
            "t0": time.time() - fake_gp._preview_epoch + 0.3,
            "x": 200, "y": 150, "w": 120, "h": 90,
            "duration_s": 2.0, "brightness": 170,
        }])
        # Auto-fire happens, file is announced but NOT downloaded.
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["downloads_pending"] >= 1, timeout=12.0),
            f"no deferred download; events: {[(e['kind'], e['note'][:40]) for e in self.controller.get_events()]}")
        self.assertTrue(self.controller.status()["downloads_deferred"])
        pending_rows = [e for e in self.controller.get_events() if e["kind"] == "photo-pending"]
        self.assertTrue(pending_rows, "no photo-pending announce row")
        # Detection must keep running THROUGH the deferral window: preview
        # frames keep advancing (the old code froze here for seconds).
        seq_before = self.controller._latest_frame_seq
        time.sleep(1.0)
        seq_after = self.controller._latest_frame_seq
        self.assertGreater(seq_after, seq_before + 10,
                           f"preview stalled during deferral ({seq_after - seq_before} frames in 1s)")
        # No photo (downloaded) events yet while armed.
        self.assertFalse([e for e in self.controller.get_events() if e["kind"] == "photo"])
        # Disarm -> flush: files land on disk with paths.
        self.controller.set_detection_mode("monitor")
        self.assertTrue(self.wait_for(
            lambda: any(e["kind"] == "photo" and e.get("path") for e in self.controller.get_events()),
            timeout=15.0), "disarm did not flush deferred downloads")
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["downloads_pending"] == 0, timeout=15.0))

    def test_fire_before_detection_log_order(self):
        """The shutter command must precede the detection log entry (the
        thumbnail encode used to sit between detection and trigger)."""
        fake_gp.configure(trigger_latency_s=0.05, file_added_offset_s=0.1)
        self.controller.connect()
        self.controller.set_detection_mode("auto", target="motion", sensitivity=70)
        time.sleep(2.5)
        fake_gp.configure(inject_motion_blobs=[{
            "t0": time.time() - fake_gp._preview_epoch + 0.3,
            "x": 200, "y": 150, "w": 120, "h": 90,
            "duration_s": 2.0, "brightness": 170,
        }])
        self.assertTrue(self.wait_for(lambda: any(
            e["kind"] == "trigger" and e["reason"] == "auto-motion"
            for e in self.controller.get_events()), timeout=12.0))
        events = sorted(self.controller.get_events(), key=lambda e: e["id"])
        trigger_id = next(e["id"] for e in events
                          if e["kind"] == "trigger" and e["reason"] == "auto-motion")
        detection_id = next(e["id"] for e in events
                            if e["kind"] == "detection" and e["reason"] == "motion")
        self.assertLess(trigger_id, detection_id,
                        "trigger must be issued BEFORE the detection log entry")

    def test_detection_paused_reason_during_download(self):
        """Monitor mode still downloads inline — but the status must say the
        worker is blind instead of claiming detection is active."""
        fake_gp.configure(download_stall_s=(1.5, 1.8), trigger_latency_s=0.05,
                          file_added_offset_s=0.1)
        self.controller.connect()
        self.controller.set_detection_mode("monitor", target="motion")
        time.sleep(1.0)
        self.controller.request_trigger()
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["detection_paused_reason"] == "downloading capture",
            timeout=10.0), "no honest 'downloading capture' paused reason")
        # And it clears once the download is done.
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["detection_paused_reason"] is None,
            timeout=10.0))

    def test_rolling_buffer_keep_saves_playable_mp4(self):
        """Rolling pre-capture buffer: enable, let it fill, Keep -> an MP4
        whose duration matches the buffered window, saved without stopping
        the live view."""
        self.controller.connect()
        self.controller.set_rolling_buffer(True, seconds=4)
        self.assertTrue(self.wait_for(
            lambda: self.controller.status()["rolling"]["buffered_s"] >= 2.0, timeout=10.0),
            f"buffer did not fill: {self.controller.status()['rolling']}")
        seq_before = self.controller._latest_frame_seq
        result = self.controller.save_rolling_clip()
        self.assertTrue(os.path.exists(result["path"]))
        self.assertIn("rolling-clips", result["path"])
        self.assertGreaterEqual(result["duration_s"], 1.5)
        self.assertGreater(result["fps"], 5)
        # Playable: decode it back and count frames.
        import av
        with av.open(result["path"]) as container:
            decoded = sum(1 for _ in container.decode(video=0))
        self.assertGreaterEqual(decoded, result["frames"] - 5)
        # Live view kept flowing during the encode (it runs off-worker).
        self.assertGreater(self.controller._latest_frame_seq, seq_before)
        # Catch log carries the clip event.
        clip_events = [e for e in self.controller.get_events() if e["kind"] == "clip"]
        self.assertTrue(clip_events)

    def test_rolling_buffer_refusals(self):
        self.controller.connect()
        with self.assertRaises(CameraControlError):
            self.controller.save_rolling_clip()  # not enabled
        self.controller.set_rolling_buffer(True, seconds=10)
        with self.assertRaises(CameraControlError):
            self.controller.save_rolling_clip()  # still filling

    def test_isoauto_defaults_off_at_connect(self):
        """Owner (2026-07-08): ISO Auto silently overrides manual ISO and
        'by default it should be off'. Connecting to a body with isoauto=On
        queues an Off write with a visible event."""
        fake_gp.configure(widget_overrides={"isoauto": "On"})
        self.controller.connect()
        self.assertTrue(self.wait_for(
            lambda: (self.controller.status()["config"].get("isoauto") or {}).get("value") == "Off",
            timeout=10.0), f"isoauto stayed {self.controller.status()['config'].get('isoauto')}")
        notes = [e["note"] for e in self.controller.get_events() if e["reason"] == "config"]
        self.assertTrue(any("ISO Auto was turned Off" in n for n in notes), notes)

    def test_isoauto_lv_gated_write_retried_with_viewfinder_pause(self):
        """Owner (2026-07-08): 'iso auto off does not work'. Hardware: the
        Z6 II silently keeps isoauto while remote live view is engaged. The
        ledger's revert path must retry ONCE with the viewfinder released
        and confirm the write instead of declaring a revert."""
        fake_gp.configure(widget_overrides={"isoauto": "On"},
                          lv_gated_writes=["isoauto"])
        self.controller.connect()
        # The connect-time default itself goes through the gated path:
        # write -> silent revert (LV engaged) -> patience -> LV-pause retry
        # -> accepted -> confirmed.
        self.assertTrue(self.wait_for(
            lambda: (self.controller.status()["config"].get("isoauto") or {}).get("value") == "Off",
            timeout=30.0),
            f"isoauto never reached Off: ledger={self.controller.status()['pending_writes']}")
        retried = [e for e in self.controller.get_events()
                   if "retried isoauto" in e["note"] and "live view paused" in e["note"]]
        self.assertTrue(retried, "no LV-pause retry event")
        # Live view re-engaged after the retry.
        camera = fake_gp.get_last_camera()
        self.assertEqual(int(camera._widgets["viewfinder"].value or 0), 1)
        # And the ledger settles as confirmed, not reverted.
        self.assertTrue(self.wait_for(
            lambda: "isoauto" not in self.controller.status()["pending_writes"], timeout=15.0))

    def test_movie_prohibit_refused_without_write(self):
        """Prohibit text non-empty -> refusal carries the camera's own
        reasons and the movie widget is never toggled."""
        self.controller.connect()
        camera = fake_gp.get_last_camera()
        writes_before = len(camera.config_write_log)
        self.controller.set_capture_mode("video")
        time.sleep(0.8)
        writes_after_mode = len(camera.config_write_log)
        self.controller.request_trigger()
        self.assertTrue(self.wait_for(lambda: any(
            "video start refused" in e["note"] for e in self.controller.get_events()
        ), timeout=6.0))
        refusal = next(e for e in self.controller.get_events() if "video start refused" in e["note"])
        self.assertIn("application mode", refusal["note"].lower())
        self.assertIn("selector", refusal["note"].lower())
        self.assertFalse(self.controller.status()["movie_recording"])
        # No movie write went to the camera after the refusal check.
        self.assertEqual(int(camera._widgets["movie"].value or 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
