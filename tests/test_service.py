"""CameraService behavior over the simulator (no hardware).

The service is the synchronous layer both AbstractCore surfaces (capability
plugin, tool set) delegate to, so its contracts are pinned here once:
dict-shaped results with explicit success, watermark-isolated capture
waits, honest errors, and JSON-safe payloads.
"""

import json
import tempfile
import time
import unittest

import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera.camera_manager import CameraManager
from abstractcamera.drivers.fake_driver import FakeDriver
from abstractcamera.hub import CameraHub
from abstractcamera.service import CameraService


def make_service(capture_root=None):
    """Service over a hub whose managers all use the module-level fake
    gphoto2 (fresh per test via fake_gp.reset() in setUp)."""
    root = capture_root or tempfile.mkdtemp(prefix="camsvc_test_")
    hub = CameraHub(
        capture_root=root,
        manager_factory=lambda: CameraManager(driver=FakeDriver(fake_gp)),
    )
    return CameraService(hub=hub)


class ServiceHarness(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(
            download_stall_s=(0.01, 0.03),
            trigger_latency_s=0.05,
            trigger_latency_jitter_s=0.02,
            file_added_offset_s=0.1,
        )
        self.service = make_service()

    def tearDown(self):
        try:
            self.service.close_all()
        finally:
            fake_gp.reset()


class DiscoveryAndLifecycle(ServiceHarness):
    def test_list_open_status_close_roundtrip(self):
        listed = self.service.list_cameras()
        self.assertTrue(listed["success"])
        self.assertTrue(listed["cameras"], "the fake transport must list a camera")

        opened = self.service.open()
        self.assertTrue(opened["success"])
        uid = opened["camera"]
        self.assertTrue(uid)
        self.assertTrue(opened["status"]["connected"])

        status = self.service.status()
        self.assertTrue(status["success"])
        self.assertEqual(status["active"], uid)
        self.assertIn(uid, status["cameras"])

        one = self.service.status(uid)
        self.assertTrue(one["success"])
        self.assertTrue(one["status"]["connected"])

        closed = self.service.close(uid)
        self.assertTrue(closed["success"])
        self.assertFalse(closed.get("connected", True))

    def test_concurrent_default_opens_claim_one_device(self):
        """P1 regression (adversarial 2026-07-21, reproduced pre-fix): two
        threads racing open(None) both passed the idempotent-default guard
        and created TWO live sessions on one physical device — which wedges
        real PTP transports. The open lock (+ the hub's connect mutex) must
        make the second opener JOIN the first session."""
        import threading

        results = [None, None]

        def opener(slot):
            results[slot] = self.service.open()

        threads = [threading.Thread(target=opener, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        self.assertTrue(all(r is not None and r["success"] for r in results), results)
        uids = {r["camera"] for r in results}
        self.assertEqual(len(uids), 1, f"both opens must land on ONE session: {results}")
        status = self.service.status()
        self.assertEqual(len(status["cameras"]), 1,
                         f"exactly one live manager may exist: {sorted(status['cameras'])}")

    def test_reopen_after_worker_death_reuses_the_uid(self):
        """P1 regression (adversarial 2026-07-21): an unplug (liveness
        watchdog) leaves a dead manager squatting on its uid, so every
        re-open minted nikon_z_6ii_2, _3, ... — splitting the capture folder
        and invalidating the uid agents stored. The next connect must reap
        the corpse and hand the SAME uid back."""
        opened = self.service.open()
        self.assertTrue(opened["success"])
        uid = opened["camera"]

        # Kill the worker the way the watchdog does: stop flag + worker
        # exits through its finally (same path as the unplug break).
        manager = self.service.hub.manager_for(uid)
        manager._stop_requested.set()
        deadline = time.time() + 10.0
        while time.time() < deadline and manager.status()["connected"]:
            time.sleep(0.02)
        self.assertFalse(manager.status()["connected"], "worker must be dead")
        # Frame state must not survive on the corpse (it used to retain the
        # last frame + up to 150 ring JPEGs until process exit).
        self.assertIsNone(manager.get_latest_frame()[0])

        reopened = self.service.open()
        self.assertTrue(reopened["success"], reopened)
        self.assertEqual(reopened["camera"], uid,
                         "re-open must reuse the base uid, not mint a suffix")
        status = self.service.status()
        self.assertEqual(sorted(status["cameras"]), [uid],
                         "the corpse must be reaped, not accumulate")

    def test_disconnect_stops_a_running_recording(self):
        """Adversarial P1 (2026-07-21, watch-flow pass): NO lifecycle path
        stopped a running movie — close/close_all/atexit left PTP bodies
        recording until the card filled and silently lost webcam MP4s. The
        worker's shutdown must toggle the recording OFF (so the movie file
        announces) before the final flush."""
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        self.assertTrue(opened["success"])
        uid = opened["camera"]
        manager = self.service.hub.manager_for(uid)

        manager.set_capture_mode("video")
        manager.request_trigger()
        deadline = time.time() + 10.0
        while time.time() < deadline and not manager.status()["movie_recording"]:
            time.sleep(0.05)
        self.assertTrue(manager.status()["movie_recording"], "recording must be running")

        closed = self.service.close(uid)
        self.assertTrue(closed["success"], closed)
        self.assertFalse(manager.status()["movie_recording"],
                         "disconnect must stop the recording, never strand it")
        # The stop act is on the record (corpse log survives; the stop
        # toggle logs a trigger event with the disconnect reason).
        kinds = [(e["kind"], e["reason"]) for e in manager.get_events(since_id=0)]
        self.assertIn(("trigger", "disconnect"), kinds,
                      f"the shutdown stop toggle must be logged: {kinds}")

    def test_stop_detection_names_a_surviving_recording(self):
        """Adversarial P1 second half: disarming a video-action watch while
        its recording runs must SAY so (agents used to close blind)."""
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        self.assertTrue(opened["success"])
        uid = opened["camera"]
        manager = self.service.hub.manager_for(uid)
        armed = self.service.start_detection(uid, action="video", target="motion")
        self.assertTrue(armed["success"], armed)
        # Simulate a detection-started recording.
        manager.request_trigger()
        deadline = time.time() + 10.0
        while time.time() < deadline and not manager.status()["movie_recording"]:
            time.sleep(0.05)

        stopped = self.service.stop_detection(uid)
        self.assertTrue(stopped["success"], stopped)
        self.assertTrue(stopped.get("movie_recording"),
                        "a surviving recording must be named in the result")
        self.assertIn("stop_recording", stopped.get("note", ""))
        ended = self.service.stop_recording(uid)
        self.assertTrue(ended["success"], ended)

    def test_shared_service_registers_exit_release(self):
        """P1 regression (adversarial 2026-07-21): get_shared_service() had
        no atexit hook (the legacy get_default_manager() does), so a routine
        host restart left cameras claimed — possibly RECORDING. Pin that the
        hook exists, is registered once, and closes the shared service's
        cameras when invoked."""
        import abstractcamera.service as service_mod

        original = service_mod._shared_service
        original_flag = service_mod._shared_service_atexit_registered
        try:
            service_mod._shared_service = None
            shared = service_mod.get_shared_service()
            self.assertTrue(service_mod._shared_service_atexit_registered)
            # Idempotent: a second resolve must not re-register.
            self.assertIs(service_mod.get_shared_service(), shared)

            closed = {"called": False}
            shared.close_all = lambda: closed.update(called=True)  # type: ignore[method-assign]
            service_mod._release_shared_service_at_exit()
            self.assertTrue(closed["called"], "exit hook must close_all() the shared service")
        finally:
            service_mod._shared_service = original
            service_mod._shared_service_atexit_registered = original_flag

    def test_operations_without_camera_fail_honestly(self):
        for result in (
            self.service.capture_photo(),
            self.service.capture_video(1.0),
            self.service.start_detection(),
            self.service.stop_detection(),
            self.service.get_events(),
            self.service.close(),
            self.service.status("ghost_uid"),
        ):
            self.assertFalse(result["success"])
            self.assertTrue(result["error"])

    def test_results_are_json_safe(self):
        self.service.open()
        photo = self.service.capture_photo()
        for payload in (self.service.list_cameras(), self.service.status(), photo,
                        self.service.get_events()):
            json.dumps(payload)  # raises on bytes or exotic types


class PhotoCapture(ServiceHarness):
    def test_capture_photo_returns_saved_path(self):
        self.service.open()
        result = self.service.capture_photo()
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["kind"], "photo")
        self.assertTrue(result["path"], "a locally-saved capture must carry its path")
        self.assertFalse(result["on_device"])
        self.assertNotIn("thumbnail", result["event"], "thumbnails must not ride JSON payloads")

    def test_capture_wait_ignores_stale_events(self):
        self.service.open()
        first = self.service.capture_photo()
        self.assertTrue(first["success"])
        second = self.service.capture_photo()
        self.assertTrue(second["success"])
        # Watermark isolation: the second wait must resolve to a NEWER event.
        self.assertGreater(second["event"]["id"], first["event"]["id"])
        self.assertNotEqual(first["path"], second["path"])

    def test_trigger_failure_reports_error(self):
        self.service.open()
        fake_gp.configure(trigger_fail_always=True)
        result = self.service.capture_photo(timeout_s=5.0)
        self.assertFalse(result["success"])
        self.assertIn("trigger failed", result["error"])

    def test_concurrent_capture_on_one_camera_is_refused(self):
        import threading

        self.service.open()
        fake_gp.configure(trigger_latency_s=0.4)  # keep the first capture busy
        results = {}

        def first():
            results["first"] = self.service.capture_photo()

        thread = threading.Thread(target=first)
        thread.start()
        time.sleep(0.1)  # first capture is inside its wait
        results["second"] = self.service.capture_photo()
        thread.join(timeout=15)
        self.assertTrue(results["first"]["success"], results["first"].get("error"))
        self.assertFalse(results["second"]["success"])
        self.assertIn("already in progress", results["second"]["error"])


class VideoCapture(ServiceHarness):
    def test_capture_video_records_and_reports(self):
        # The default Nikon profile refuses movie start (photo selector);
        # this scenario runs the movie-READY body.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        self.service.open()
        result = self.service.capture_video(0.6, timeout_s=3.0)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["duration_s"], 0.6)
        # The sim mirrors PTP bodies whose movie file stays on the card:
        # the recording is honest, delivery is declared.
        self.assertIn("delivered", result)
        if not result["delivered"]:
            self.assertIn("note", result)

    def test_stop_recording_ends_a_running_recording(self):
        # A recording NOT owned by a bounded capture (the orphan class the
        # adversarial P0 named): started by a raw mode-set + trigger.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        manager = self.service.hub.manager_for(opened["camera"])
        manager.set_capture_mode("video")
        manager.request_trigger()
        deadline = time.time() + 5
        while time.time() < deadline and not manager.status()["movie_recording"]:
            time.sleep(0.05)
        self.assertTrue(manager.status()["movie_recording"], "recording never started")

        result = self.service.stop_recording(timeout_s=2.0)
        self.assertTrue(result["success"], result.get("error"))
        self.assertFalse(manager.status()["movie_recording"])

    def test_stop_recording_without_recording_is_refused(self):
        self.service.open()
        result = self.service.stop_recording()
        self.assertFalse(result["success"])
        self.assertIn("No video recording", result["error"])

    def test_capture_photo_refused_while_recording(self):
        # P0 pin: a still capture's trigger would TOGGLE the recording off
        # instead of firing the shutter — must refuse, never fire.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        manager = self.service.hub.manager_for(opened["camera"])
        manager.set_capture_mode("video")
        manager.request_trigger()
        deadline = time.time() + 5
        while time.time() < deadline and not manager.status()["movie_recording"]:
            time.sleep(0.05)

        result = self.service.capture_photo(timeout_s=2.0)
        self.assertFalse(result["success"])
        self.assertIn("recording", result["error"])
        self.assertTrue(manager.status()["movie_recording"], "the refusal must not have toggled the recording")
        self.service.stop_recording(timeout_s=2.0)

    def test_start_detection_refused_while_recording(self):
        # P0 pin: arming auto-fire writes capture_mode — corrupting a
        # running recording's toggle pairing.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        manager = self.service.hub.manager_for(opened["camera"])
        manager.set_capture_mode("video")
        manager.request_trigger()
        deadline = time.time() + 5
        while time.time() < deadline and not manager.status()["movie_recording"]:
            time.sleep(0.05)

        armed = self.service.start_detection(target="motion", action="photo")
        self.assertFalse(armed["success"])
        self.assertIn("recording", armed["error"])
        monitor = self.service.start_detection(target="motion", action="monitor")
        self.assertTrue(monitor["success"], "monitor writes no capture mode and stays allowed")
        self.service.stop_recording(timeout_s=2.0)

    def test_capture_video_refused_while_auto_fire_armed(self):
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        self.service.open()
        self.service.start_detection(target="motion", action="photo")
        result = self.service.capture_video(0.6)
        self.assertFalse(result["success"])
        self.assertIn("auto-fire", result["error"])

    def test_capture_video_restores_capture_mode(self):
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        uid = opened["camera"]
        self.service.capture_video(0.6, timeout_s=3.0)
        status = self.service.status(uid)
        self.assertEqual(status["status"]["capture_mode"], "single")

    def test_movie_refusal_is_an_honest_error(self):
        # Default profile: movie start refused (selector on photo).
        self.service.open()
        result = self.service.capture_video(0.6, timeout_s=3.0)
        self.assertFalse(result["success"])
        self.assertIn("refused", result["error"])

    def test_duration_bounds_are_validated(self):
        self.service.open()
        for bad in (0.0, -3, 1e9, "soon"):
            result = self.service.capture_video(bad)
            self.assertFalse(result["success"])
            self.assertIn("duration_s", result["error"])

    def test_numeric_garbage_fails_as_dict_never_raises(self):
        # No-raise contract pin (adversarial finding: float(timeout_s)
        # raised bare ValueError through both integration surfaces — and in
        # capture_video AFTER the hardware had already recorded).
        self.service.open()
        for result in (
            self.service.capture_photo(timeout_s="soon"),
            self.service.capture_video(1.0, timeout_s="soon"),
            self.service.stop_recording(timeout_s="soon"),
            self.service.get_events(since_id="abc"),
            self.service.get_events(limit="abc"),
            self.service.start_detection(sensitivity="loud"),
        ):
            self.assertFalse(result["success"])
            self.assertIn("must be", result["error"])

    def test_validation_happens_before_hardware_acts(self):
        # A bad timeout must be refused BEFORE the recording starts.
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        opened = self.service.open()
        manager = self.service.hub.manager_for(opened["camera"])
        result = self.service.capture_video(0.6, timeout_s="soon")
        self.assertFalse(result["success"])
        self.assertFalse(manager.status()["movie_recording"])
        self.assertEqual(manager.status()["capture_mode"], "single")


class DeferredAndDefaultOpen(ServiceHarness):
    def test_capture_photo_under_auto_fire_returns_deferred_success(self):
        # Armed auto-fire defers downloads BY DESIGN — blocking for the
        # 'photo' event always timed out (adversarial P1). The honest
        # answer is a deferred success naming where the file goes.
        self.service.open()
        self.service.start_detection(target="motion", action="photo")
        result = self.service.capture_photo(timeout_s=10.0)
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(result.get("deferred"))
        self.assertIsNone(result["path"])
        self.assertIn("detection", result["note"])
        self.service.stop_detection()

    def test_capture_refused_while_backlog_flushes(self):
        # Stale-attribution guard (adversarial P1): after disarming, the
        # deferred backlog flushes; a new capture wait could claim one of
        # those photo events as its own result. Refuse until drained.
        self.service.open()
        self.service.start_detection(target="motion", action="photo")
        deferred = self.service.capture_photo(timeout_s=10.0)
        self.assertTrue(deferred["success"], deferred.get("error"))
        self.service.stop_detection()
        opened = self.service.hub.manager_for(None)
        if opened.status()["downloads_pending"] > 0:
            result = self.service.capture_photo(timeout_s=5.0)
            self.assertFalse(result["success"])
            self.assertIn("still downloading", result["error"])
        # Once the flush drains, capture works again.
        deadline = time.time() + 10
        while time.time() < deadline and opened.status()["downloads_pending"] > 0:
            time.sleep(0.05)
        result = self.service.capture_photo()
        self.assertTrue(result["success"], result.get("error"))

    def test_default_open_is_idempotent(self):
        first = self.service.open()
        second = self.service.open()
        self.assertTrue(second["success"])
        self.assertTrue(second.get("already_connected"))
        self.assertEqual(first["camera"], second["camera"])
        self.assertEqual(len(self.service.hub.statuses()), 1,
                         "a default re-open must not claim a second session on the same device")

    def test_close_during_capture_fails_fast_with_the_real_reason(self):
        import threading

        self.service.open()
        fake_gp.configure(trigger_latency_s=1.5)  # keep the wait busy
        results = {}

        def capture():
            results["capture"] = self.service.capture_photo(timeout_s=20.0)

        thread = threading.Thread(target=capture)
        started = time.time()
        thread.start()
        time.sleep(0.3)
        self.service.close()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "capture wait must not grind out its full timeout")
        self.assertLess(time.time() - started, 10.0)
        self.assertFalse(results["capture"]["success"])
        self.assertIn("disconnected", results["capture"]["error"])


class Detection(ServiceHarness):
    def test_start_detection_arms_and_reports_watermark(self):
        self.service.open()
        armed = self.service.start_detection(target="motion", action="photo", sensitivity=75)
        self.assertTrue(armed["success"], armed.get("error"))
        self.assertEqual(armed["detection_mode"], "auto")
        self.assertEqual(armed["detection_target"], "motion")
        self.assertEqual(armed["detection_sensitivity"], 75.0)
        self.assertEqual(armed["capture_mode"], "single")
        self.assertIsInstance(armed["event_watermark"], int)

        stopped = self.service.stop_detection()
        self.assertTrue(stopped["success"])
        self.assertEqual(stopped["detection_mode"], "off")

    def test_monitor_action_never_fires(self):
        self.service.open()
        armed = self.service.start_detection(target="lightning", action="monitor")
        self.assertTrue(armed["success"])
        self.assertEqual(armed["detection_mode"], "monitor")

    def test_video_action_sets_video_mode_and_warns(self):
        fake_gp.configure(movie_toggle_fails=False, movie_prohibit_text="")
        self.service.open()
        armed = self.service.start_detection(target="motion", action="video")
        self.assertTrue(armed["success"], armed.get("error"))
        self.assertEqual(armed["capture_mode"], "video")
        self.assertIn("toggles recording", armed["note"])

    def test_invalid_target_and_action_are_refused(self):
        self.service.open()
        self.assertFalse(self.service.start_detection(target="ghosts")["success"])
        self.assertFalse(self.service.start_detection(action="explode")["success"])


class Events(ServiceHarness):
    def test_wire_contract_session_eviction_and_trigger_ids(self):
        """Wire-contract pass (adversarial 2026-07-21): the event log is an
        API for LLM/workflow consumers now — session epoch names the id
        space, eviction is signaled instead of silent, and file events carry
        their trigger act."""
        self.service.open()
        result = self.service.capture_photo()
        self.assertTrue(result["success"], result)

        events = self.service.get_events(since_id=0)
        self.assertTrue(events["success"])
        self.assertTrue(events.get("session"), "responses must carry the session epoch")
        # The trigger act and its photo share the same trigger_id.
        by_kind = {}
        for event in events["events"]:
            by_kind.setdefault(event["kind"], []).append(event)
        self.assertIn("trigger", by_kind)
        self.assertIn("photo", by_kind)
        trig_id = by_kind["trigger"][0].get("trigger_id")
        self.assertIsNotNone(trig_id, "trigger events must be stamped")
        self.assertEqual(by_kind["photo"][0].get("trigger_id"), trig_id,
                         "the photo must correlate to its trigger act")

        # Eviction: shrink the ring, overflow it, and poll with a stale
        # cursor — the gap must be SIGNALED, not silent.
        manager = self.service.hub.manager_for(None)
        import collections
        with manager._state_lock:
            old_events = manager._events
            manager._events = collections.deque(old_events, maxlen=3)
        for i in range(6):
            manager._append_event(kind="camera-event", reason="test", note=f"filler {i}")
        stale = self.service.get_events(since_id=1)
        self.assertTrue(stale["success"])
        self.assertTrue(stale.get("evicted"), "a stale cursor over an overflowed ring must say so")
        self.assertIsNotNone(stale.get("first_retained_id"))

        # Session epoch changes across a reconnect (fresh manager) — the
        # consumer's reset signal.
        first_session = events["session"]
        self.service.close()
        self.service.open()
        fresh = self.service.get_events(since_id=0)
        self.assertTrue(fresh.get("session"))
        self.assertNotEqual(fresh["session"], first_session,
                            "a reconnect must mint a new session epoch")

    def test_capture_wait_skips_stale_backlog_photo_events(self):
        """The misattribution window (adversarial 2026-07-21): a deferred
        backlog file flushing DURING a capture wait used to be claimed as
        that capture's result. Stamped trigger ids + min_trigger_id filter
        must skip the stale file and claim the right one."""
        from abstractcamera.service_waits import wait_for_capture

        class StubManager:
            def __init__(self):
                self.events = [
                    # newest-first, as the real manager returns
                    {"id": 3, "kind": "photo", "reason": "captured",
                     "path": "/tmp/fresh.jpg", "trigger_id": 7},
                    {"id": 2, "kind": "photo", "reason": "captured",
                     "path": "/tmp/stale_backlog.jpg", "trigger_id": 3},
                ]

            def get_events(self, since_id=0):
                return [dict(e) for e in self.events if e["id"] > since_id]

            def status(self):
                return {"connected": True}

        result = wait_for_capture(
            StubManager(), watermark=1, timeout_s=2.0,
            pending_meaning=None, what="photo", min_trigger_id=7,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["path"], "/tmp/fresh.jpg",
                         "the stale backlog file must not satisfy the wait")

    def test_capture_wait_skips_stale_backlog_error_events(self):
        """Adversarial P1 (2026-07-21): a backlog file whose FETCH fails
        during this wait (age valve / stop_detection flush) carries its
        ORIGINAL act's stamp — reporting it as this capture's failure is
        the same misattribution as claiming its success. Unstamped errors
        must still abort."""
        from abstractcamera.service_waits import wait_for_capture

        class StubManager:
            def __init__(self):
                self.events = [
                    {"id": 3, "kind": "photo", "reason": "captured",
                     "path": "/tmp/fresh.jpg", "trigger_id": 7},
                    {"id": 2, "kind": "error", "reason": "download",
                     "note": "failed to fetch OLD_BACKLOG.NEF", "trigger_id": 3},
                ]

            def get_events(self, since_id=0):
                return [dict(e) for e in self.events if e["id"] > since_id]

            def status(self):
                return {"connected": True}

        result = wait_for_capture(
            StubManager(), watermark=1, timeout_s=2.0,
            pending_meaning=None, what="photo", min_trigger_id=7,
        )
        self.assertTrue(result["success"], f"a STALE error must not abort: {result}")
        self.assertEqual(result["path"], "/tmp/fresh.jpg")

        class StubManagerUnstamped(StubManager):
            def __init__(self):
                self.events = [
                    {"id": 2, "kind": "error", "reason": "download",
                     "note": "real failure", "trigger_id": None},
                ]

        result = wait_for_capture(
            StubManagerUnstamped(), watermark=1, timeout_s=1.0,
            pending_meaning=None, what="photo", min_trigger_id=7,
        )
        self.assertFalse(result["success"], "an UNSTAMPED error must still abort")
        self.assertIn("real failure", result["error"])

    def test_direct_manager_reconnect_restarts_the_id_space(self):
        """Adversarial P1 (2026-07-21): the direct-manager reconnect path
        (get_default_manager / host-held managers) re-minted the epoch but
        kept old events + counters — consumers resetting cursors on the new
        epoch re-read the OLD session's events as new, and the bridge would
        re-emit them under fresh command ids. A new epoch must be a NEW id
        space: events cleared, ids restarting at 1."""
        opened = self.service.open()
        self.assertTrue(opened["success"])
        manager = self.service.hub.manager_for(opened["camera"])
        manager._append_event(kind="camera-event", reason="test", note="old session")
        first_epoch = manager.session_epoch
        self.assertTrue(manager.get_events(since_id=0))

        manager.disconnect()
        manager.connect()
        try:
            self.assertNotEqual(manager.session_epoch, first_epoch)
            self.assertEqual(manager.get_events(since_id=0), [],
                             "old events must not survive into the new epoch")
            self.assertEqual(manager.trigger_seq, 0)
            manager._append_event(kind="camera-event", reason="test", note="new session")
            self.assertEqual(manager.get_events(since_id=0)[0]["id"], 1,
                             "ids must restart with the new epoch")
        finally:
            manager.disconnect()

    def test_capture_results_carry_the_sight_lane_media_field(self):
        """Operator-ruled sight lane (commons 3969/4089): results that
        landed a LOCAL file carry handler-authored `media` (bare path on
        the storeless lane); results with no local file carry NO media key
        — an absent field is the honest shape, never an empty list."""
        self.service.open()
        photo = self.service.capture_photo()
        self.assertTrue(photo["success"], photo)
        self.assertEqual(photo.get("media"), [photo["path"]])

        preview = self.service.preview_photo(wait_s=5.0)
        self.assertTrue(preview["success"], preview)
        self.assertEqual(preview.get("media"), [preview["path"]])

        # Deferred (armed auto-fire): no local file yet -> no media key.
        armed = self.service.start_detection(action="photo", target="motion")
        self.assertTrue(armed["success"], armed)
        deferred = self.service.capture_photo()
        self.assertTrue(deferred["success"], deferred)
        self.assertTrue(deferred.get("deferred"))
        self.assertNotIn("media", deferred,
                         "no local file landed — the media key must be absent")
        self.service.stop_detection()
        # The deferred file flushes asynchronously at disarm; the backlog
        # guard refuses new captures until it drains.
        manager = self.service.hub.manager_for(None)
        deadline = time.time() + 10.0
        while time.time() < deadline and manager.status()["downloads_pending"] > 0:
            time.sleep(0.05)
        self.assertEqual(manager.status()["downloads_pending"], 0)

        # On-device save policy: the shot stays on the camera -> no media.
        manager.set_save_policy(download_locally=False)
        on_device = self.service.capture_photo()
        self.assertTrue(on_device["success"], on_device)
        self.assertTrue(on_device.get("on_device"))
        self.assertNotIn("media", on_device,
                         "on-device results must not carry media")
        manager.set_save_policy(download_locally=True)

    def test_video_results_carry_media_when_the_file_lands(self):
        """The video lane rides the same wait branch (what='video' is a
        label), but the sim's movie profile keeps files on-card — so the
        file-lands lane is pinned via an injected photo event through
        _stop_and_collect's wait (adversarial P2 2026-07-21: claimed in the
        CHANGELOG, previously untested)."""
        from abstractcamera.service_waits import wait_for_capture

        class StubManager:
            def __init__(self):
                self.events = [
                    {"id": 2, "kind": "photo", "reason": "captured",
                     "path": "/tmp/movie.mp4", "trigger_id": 5},
                ]

            def get_events(self, since_id=0):
                return [dict(e) for e in self.events if e["id"] > since_id]

            def status(self):
                return {"connected": True}

        result = wait_for_capture(
            StubManager(), watermark=1, timeout_s=2.0,
            pending_meaning=None, what="video", min_trigger_id=5,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["media"], ["/tmp/movie.mp4"])

        # And the undelivered fallback (recording confirmed, file never
        # announced) must NOT carry media — capture_video's timeout shape.
        class StubManagerNoFile(StubManager):
            def __init__(self):
                self.events = []

        timed_out = wait_for_capture(
            StubManagerNoFile(), watermark=0, timeout_s=0.3,
            pending_meaning=None, what="video",
        )
        self.assertFalse(timed_out["success"])
        self.assertNotIn("media", timed_out)

    def test_detection_metrics_ride_events(self):
        """Detector metrics must reach consumers structured, not prose-only
        (adversarial 2026-07-21)."""
        self.service.open()
        manager = self.service.hub.manager_for(None)
        manager._append_event(kind="detection", reason="motion", note="motion 3.2%",
                              score=1.4, metrics={"fraction_pct": 3.2, "centroid": [12, 34]})
        events = self.service.get_events(kinds=["detection"])
        self.assertTrue(events["events"], "the detection event must surface")
        self.assertEqual(events["events"][-1].get("metrics"),
                         {"fraction_pct": 3.2, "centroid": [12, 34]})

    def test_events_are_chronological_filtered_and_cursored(self):
        self.service.open()
        self.service.capture_photo()
        self.service.capture_photo()
        result = self.service.get_events(since_id=0)
        self.assertTrue(result["success"])
        ids = [e["id"] for e in result["events"]]
        self.assertEqual(ids, sorted(ids), "events must read oldest-first")
        self.assertEqual(result["last_id"], ids[-1])

        photos = self.service.get_events(kinds=["photo"])
        self.assertTrue(all(e["kind"] == "photo" for e in photos["events"]))
        self.assertEqual(len(photos["events"]), 2)

        nothing_new = self.service.get_events(since_id=result["last_id"])
        self.assertEqual(nothing_new["events"], [])
        self.assertEqual(nothing_new["last_id"], result["last_id"])

    def test_truncation_pages_forward_without_losing_events(self):
        self.service.open()
        self.service.capture_photo()
        self.service.capture_photo()
        full = self.service.get_events(since_id=0)
        page_one = self.service.get_events(since_id=0, limit=2)
        self.assertTrue(page_one["truncated"])
        collected = list(page_one["events"])
        cursor = page_one["last_id"]
        for _ in range(20):  # bounded: pages are >=1 event each
            page = self.service.get_events(since_id=cursor, limit=2)
            collected.extend(page["events"])
            cursor = page["last_id"]
            if not page["truncated"] and not page["events"]:
                break
            if not page["truncated"]:
                break
        self.assertEqual([e["id"] for e in collected], [e["id"] for e in full["events"]],
                         "cursor pagination must reach every event exactly once")

    def test_thumbnails_stripped_by_default(self):
        self.service.open()
        self.service.capture_photo()
        events = self.service.get_events()["events"]
        self.assertTrue(events)
        self.assertTrue(all("thumbnail" not in e for e in events))
        with_thumbs = self.service.get_events(include_thumbnails=True)["events"]
        self.assertTrue(any("thumbnail" in e for e in with_thumbs))


if __name__ == "__main__":
    unittest.main()
