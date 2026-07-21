"""AbstractCore capability plugin integration (real CapabilityRegistry).

These tests exercise the plugin against AbstractCore's ACTUAL registry —
registration, generic discovery routes, instance caching — plus the
capability's own contracts (raise-on-failure, artifact refs, bytes opt-in).
They skip cleanly when abstractcore is not installed; everything camera-side
runs on the simulator (no hardware).
"""

import os
import tempfile
import unittest

import abstractcamera.sim.gphoto2 as fake_gp
from abstractcamera.camera_manager import CameraManager
from abstractcamera.drivers.fake_driver import FakeDriver
from abstractcamera.errors import CameraControlError
from abstractcamera.hub import CameraHub
from abstractcamera.service import CameraService, reset_shared_service

try:
    from abstractcore.capabilities.registry import CapabilityRegistry
    HAS_ABSTRACTCORE = True
except ImportError:  # pragma: no cover
    HAS_ABSTRACTCORE = False

from abstractcamera.integrations import abstractcore_plugin
from abstractcamera.integrations.abstractcore_plugin import (
    BACKEND_ID,
    _AbstractCameraCapability,
    register,
)


class _Owner:
    """Minimal AbstractCore-owner shape: capability plugins read owner.config."""

    def __init__(self, config=None):
        self.config = dict(config or {})


class _RecordingStore:
    """AbstractRuntime-shaped artifact store double (store() contract)."""

    def __init__(self):
        self.saved = []

    def store(self, content, *, content_type="application/octet-stream", run_id=None, tags=None, artifact_id=None):
        self.saved.append(
            {"content": bytes(content), "content_type": content_type, "run_id": run_id, "tags": tags}
        )

        class _Meta:
            artifact_id = f"art-{len(self.saved):04d}"

        return _Meta()


def make_capability(owner=None):
    """Capability over an ISOLATED service (never the process-shared one:
    tests must not leak sessions into each other)."""
    hub = CameraHub(
        capture_root=tempfile.mkdtemp(prefix="camplugin_test_"),
        manager_factory=lambda: CameraManager(driver=FakeDriver(fake_gp)),
    )
    return _AbstractCameraCapability(owner or _Owner(), service=CameraService(hub=hub))


class PluginHarness(unittest.TestCase):
    def setUp(self):
        fake_gp.reset()
        fake_gp.configure(
            download_stall_s=(0.01, 0.03),
            trigger_latency_s=0.05,
            trigger_latency_jitter_s=0.02,
            file_added_offset_s=0.1,
        )
        self.capability = make_capability()

    def tearDown(self):
        try:
            self.capability.close_all()
        finally:
            fake_gp.reset()


@unittest.skipUnless(HAS_ABSTRACTCORE, "abstractcore is not installed")
class RegistryIntegration(unittest.TestCase):
    """The plugin through core's REAL registry — the entry point's job,
    minus setuptools (register() is called directly, exactly what the
    entry point loader does)."""

    def setUp(self):
        # The registry-built capability resolves the PROCESS-SHARED service,
        # whose hub uses real discovery — pin discovery to the simulator,
        # reset the shared service so no session leaks between tests, and
        # point captures at a tmpdir (the suite must never write into the
        # operator's real ~/Pictures — adversarial finding). Env values are
        # saved/restored, not popped (an ambient operator value survives).
        self._saved_env = {
            k: os.environ.get(k) for k in ("ABSTRACTCAMERA_FAKE", "ABSTRACTCAMERA_CAPTURE_ROOT")
        }
        os.environ["ABSTRACTCAMERA_FAKE"] = "1"
        self.capture_root = tempfile.mkdtemp(prefix="camreg_test_")
        os.environ["ABSTRACTCAMERA_CAPTURE_ROOT"] = self.capture_root
        reset_shared_service()
        fake_gp.reset()
        fake_gp.configure(
            download_stall_s=(0.01, 0.03),
            trigger_latency_s=0.05,
            trigger_latency_jitter_s=0.02,
            file_added_offset_s=0.1,
        )
        self.registry = CapabilityRegistry(_Owner())
        # The registry loads entry-point plugins lazily; registering ours
        # directly keeps the test hermetic even when abstractcamera is not
        # pip-installed into the environment (the REAL entry-point lane is
        # pinned separately by EntryPointLane below).
        register(self.registry)

    def tearDown(self):
        try:
            instance = self.registry._instances.get(("camera", BACKEND_ID))
            if instance is not None:
                instance.close_all()
        finally:
            reset_shared_service()
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            fake_gp.reset()

    def test_backend_is_registered_with_metadata(self):
        infos = self.registry.list_backend_infos("camera")
        self.assertEqual(len(infos), 1)
        info = infos[0]
        self.assertEqual(info.backend_id, BACKEND_ID)
        self.assertEqual(info.capability, "camera")
        self.assertIn("abstractcamera", info.install_hint)
        self.assertTrue(info.description)
        self.assertTrue(info.config_hint)

    def test_generic_discovery_routes_work_unchanged_on_todays_core(self):
        providers = self.registry.available_providers("camera")
        self.assertTrue(any(p["provider_id"] == "fake" for p in providers))

        models = self.registry.list_models("camera")
        self.assertTrue(models, "the simulator must appear as a device")
        self.assertEqual(models[0]["provider_id"], "fake")

        operations = self.registry.list_operations("camera")
        op_ids = {op["operation_id"] for op in operations}
        self.assertLessEqual(
            {"capture_photo", "capture_video", "stop_recording", "start_detection", "open", "close"},
            op_ids,
        )

    def test_availability_facts_survive_cores_normalizer(self):
        # Adversarial finding: bare-string provider lists made core's
        # normalizer report uninstalled transports as "available". The
        # documented route must carry the plugin's own facts through.
        providers = self.registry.available_providers("camera")
        by_id = {p["provider_id"]: p for p in providers}
        # In fake mode the simulator is the ONLY installed transport.
        self.assertEqual(by_id["fake"]["status"], "available")
        self.assertTrue(by_id["fake"]["installed"])
        for transport in ("ptp", "dwarf"):
            self.assertEqual(by_id[transport]["status"], "not_installed", transport)
            self.assertFalse(by_id[transport]["installed"], transport)
        self.assertTrue(by_id["dwarf"]["remote"])
        self.assertTrue(by_id["fake"]["local"])

    def test_instance_is_cached_per_registry(self):
        first = self.registry._get_instance("camera")
        second = self.registry._get_instance("camera")
        self.assertIs(first, second)
        self.assertEqual(first.backend_id, BACKEND_ID)

    def test_capture_photo_through_registry_instance(self):
        # The full production path: registry factory -> shared service ->
        # fake-pinned discovery -> capture.
        capability = self.registry._get_instance("camera")
        capability.open()
        result = capability.capture_photo()
        self.assertTrue(result["path"])
        capability.close_all()

    def test_typed_helper_is_preferred_when_core_ships_it(self):
        calls = {}

        class _FutureRegistry:
            def register_camera_backend(self, **kwargs):
                calls.update(kwargs)

            def register_backend(self, **kwargs):  # pragma: no cover
                raise AssertionError("generic path must not be used when the typed helper exists")

        register(_FutureRegistry())
        self.assertEqual(calls["backend_id"], BACKEND_ID)
        self.assertTrue(callable(calls["factory"]))


def _entry_point_installed() -> bool:
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        selected = eps.select(group="abstractcore.capabilities_plugins")
        return any(ep.name == "abstractcamera" for ep in selected)
    except Exception:
        return False


@unittest.skipUnless(HAS_ABSTRACTCORE, "abstractcore is not installed")
@unittest.skipUnless(
    _entry_point_installed(),
    "abstractcamera is not pip-installed with its entry point (editable metadata stale?)",
)
class EntryPointLane(unittest.TestCase):
    """The REAL setuptools entry-point lane — importlib.metadata discovery
    through core's registry, no direct register() call (the direct-register
    tests above deliberately bypass this; ADR 0012's end-to-end claim rests
    here)."""

    def test_registry_discovers_the_plugin_via_entry_points(self):
        registry = CapabilityRegistry(_Owner())
        status = registry.status()
        seen = {p.get("name") for p in status["plugins_seen"]}
        self.assertIn("abstractcamera", seen)
        errors = [e for e in status["plugin_errors"] if e.get("name") == "abstractcamera"]
        self.assertEqual(errors, [], f"plugin errors: {errors}")
        infos = registry.list_backend_infos("camera")
        self.assertTrue(any(i.backend_id == BACKEND_ID for i in infos))


class CapabilityContract(PluginHarness):
    def test_failures_raise_camera_control_error(self):
        with self.assertRaises(CameraControlError):
            self.capability.capture_photo()  # nothing connected
        with self.assertRaises(CameraControlError):
            self.capability.status("ghost_uid")

    def test_open_capture_close(self):
        opened = self.capability.open()
        self.assertTrue(opened["status"]["connected"])
        result = self.capability.capture_photo()
        self.assertTrue(result["path"])
        self.assertNotIn("data", result, "bytes only ride when explicitly requested")
        closed = self.capability.close()
        self.assertFalse(closed.get("connected", True))

    def test_include_bytes_attaches_json_safe_base64(self):
        import base64
        import json

        self.capability.open()
        result = self.capability.capture_photo(include_bytes=True)
        # JSON-safe is the CONTRACT (core ruling c3168): results may land in
        # runtime ledgers, so content rides as base64, never raw bytes.
        json.dumps(result)
        self.assertTrue(base64.b64decode(result["data_b64"]))
        self.assertTrue(result["content_type"])

    def test_artifact_store_receives_capture(self):
        self.capability.open()
        store = _RecordingStore()
        result = self.capability.capture_photo(artifact_store=store, run_id="run-1", tags={"who": "test"})
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0]["run_id"], "run-1")
        ref = result["artifact"]
        self.assertEqual(ref["$artifact"], "art-0001")
        self.assertEqual(ref["size_bytes"], len(store.saved[0]["content"]))
        self.assertTrue(ref["filename"])

    def test_preview_frame_bytes_and_artifact_modes(self):
        self.capability.open()
        jpeg = self.capability.preview_frame()
        self.assertIsInstance(jpeg, bytes)
        store = _RecordingStore()
        ref = self.capability.preview_frame(artifact_store=store)
        self.assertEqual(ref["content_type"], "image/jpeg")
        self.assertEqual(ref["$artifact"], "art-0001")

    def test_detection_cycle(self):
        self.capability.open()
        armed = self.capability.start_detection(target="motion", action="monitor")
        self.assertEqual(armed["detection_mode"], "monitor")
        events = self.capability.detection_events(since_id=0)
        self.assertIn("events", events)
        stopped = self.capability.stop_detection()
        self.assertEqual(stopped["detection_mode"], "off")

    def test_available_providers_derive_from_discovery(self):
        # This process has no fixed driver injected at the DISCOVERY level,
        # so availability reflects the real machine; the shape contract is
        # what matters here.
        catalog = self.capability.available_providers()
        self.assertIn("providers", catalog)
        self.assertIn("available_providers", catalog)
        self.assertIn("details", catalog)
        for detail in catalog["details"].values():
            self.assertIn("installed", detail)

    def test_capture_root_config_is_applied(self):
        root = tempfile.mkdtemp(prefix="camroot_cfg_")
        capability = make_capability(_Owner({"camera_capture_root": root}))
        capability.open()
        result = capability.capture_photo()
        self.assertTrue(result["path"].startswith(root))
        capability.close_all()


if __name__ == "__main__":
    unittest.main()
