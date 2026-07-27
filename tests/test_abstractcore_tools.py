"""Camera tool set through AbstractCore's REAL tool machinery.

Pins: schema generation from the decorated functions, classification
exhaustiveness (both directions — core's inventory rule applied to this
package), and end-to-end execution through core's ToolRegistry against the
simulator, including the dict success/error contract the registry reads.
Skips cleanly when abstractcore is not installed.
"""

import os
import tempfile
import unittest

import abstractcamera.sim.gphoto2 as fake_gp

try:
    import abstractcore  # noqa: F401
    HAS_ABSTRACTCORE = True
except ImportError:  # pragma: no cover
    HAS_ABSTRACTCORE = False

if HAS_ABSTRACTCORE:
    from abstractcore.tools.core import ToolCall
    from abstractcore.tools.registry import ToolRegistry

    from abstractcamera.integrations.abstractcore_tools import (
        CAMERA_TOOL_CLASSIFICATION,
        CAMERA_TOOLS,
        camera_tool_definitions,
        camera_tool_specs,
        camera_tools,
    )
    from abstractcamera.service import get_shared_service, reset_shared_service


class _EnvGuard:
    """Save/restore env keys around a test — an ambient operator value must
    survive the suite (adversarial finding: tearDown clobbered it)."""

    KEYS = ("ABSTRACTCAMERA_FAKE", "ABSTRACTCAMERA_CAPTURE_ROOT")

    def save(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}

    def restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@unittest.skipUnless(HAS_ABSTRACTCORE, "abstractcore is not installed")
class ToolDefinitions(unittest.TestCase):
    def test_every_tool_carries_a_definition(self):
        definitions = camera_tool_definitions()
        self.assertEqual(len(definitions), len(CAMERA_TOOLS))
        for definition in definitions:
            self.assertTrue(definition.name.startswith("camera_"))
            self.assertTrue(definition.description)
            self.assertIsInstance(definition.parameters, dict)

    def test_specs_are_flat_dicts_with_parameters(self):
        specs = camera_tool_specs()
        self.assertEqual(len(specs), len(CAMERA_TOOLS))
        for spec in specs:
            self.assertIn("name", spec)
            self.assertIn("description", spec)
            self.assertIn("parameters", spec)

    def test_classification_is_exhaustive_both_ways(self):
        # Core's inventory rule applied here: a tool without a declared
        # classification refuses, and a classification naming no tool
        # refuses — deliberate decisions at add AND remove time.
        tool_names = {fn._tool_definition.name for fn in CAMERA_TOOLS}
        classified = set(CAMERA_TOOL_CLASSIFICATION.keys())
        self.assertEqual(tool_names, classified)
        for name, facts in CAMERA_TOOL_CLASSIFICATION.items():
            self.assertEqual(
                # standing_effect joined with the tool-tiers item-D ruled
                # vocabulary (schema v3; adopted c4497).
                {"mutating", "remote_write_capable", "captures_environment", "standing_effect"},
                set(facts.keys()),
                f"classification facts drifted for {name}",
            )

    def test_standing_effect_marks_exactly_the_standing_authority(self):
        """Item-D ruled fact (schema v3): standing_effect is TRUE only for
        camera_start_detection — the one tool that arms an ongoing process
        (auto-fire) that keeps acting after the call returns. Grant layers
        key revocation-on-tighten semantics on this fact (adopted c4444),
        so a drift in either direction misroutes revocation duties."""
        from abstractcamera.integrations.abstractcore_tools import camera_tool_approval_defaults

        for name, facts in CAMERA_TOOL_CLASSIFICATION.items():
            expected = name == "camera_start_detection"
            self.assertEqual(facts["standing_effect"], expected, name)
        # The fact's arrival must NOT move the approval partition: the only
        # standing tool already asked via captures_environment.
        defaults = camera_tool_approval_defaults()
        self.assertEqual(
            ["camera_get_events", "camera_list_devices", "camera_status"],
            defaults["auto_approve"],
        )

    def test_capture_tools_declare_environment_capture(self):
        for name in ("camera_preview_photo", "camera_capture_photo", "camera_capture_video",
                     "camera_start_detection", "camera_open"):
            self.assertTrue(CAMERA_TOOL_CLASSIFICATION[name]["captures_environment"], name)
        for name in ("camera_list_devices", "camera_status", "camera_get_events", "camera_stop_recording"):
            self.assertFalse(CAMERA_TOOL_CLASSIFICATION[name]["captures_environment"], name)
        # preview pulls frames (a read) — it must NOT claim remote writes,
        # but it writes a local file, so mutating is true.
        preview = CAMERA_TOOL_CLASSIFICATION["camera_preview_photo"]
        self.assertFalse(preview["remote_write_capable"])
        self.assertTrue(preview["mutating"])

    def test_camera_tools_returns_callables(self):
        for fn in camera_tools():
            self.assertTrue(callable(fn))
            self.assertTrue(hasattr(fn, "_tool_definition"))

    def test_approval_defaults_fail_closed_on_drifted_entry(self):
        # The fail-closed default is enforced in CODE, not by the
        # exhaustiveness test (adversarial finding 2026-07-21): an entry
        # missing a fact key — e.g. a new passive-sensor tool whose author
        # forgot captures_environment — must go to require_approval, never
        # auto, on the strength of the keys that happen to be present.
        import abstractcamera.integrations.abstractcore_tools as mod

        original = dict(mod.CAMERA_TOOL_CLASSIFICATION)
        try:
            mod.CAMERA_TOOL_CLASSIFICATION = dict(original)
            # (a) empty dict, (b) partial (missing captures_environment),
            # (c) extra/unknown key — every one must fall to require.
            mod.CAMERA_TOOL_CLASSIFICATION["camera_ghost_a"] = {}
            mod.CAMERA_TOOL_CLASSIFICATION["camera_ghost_b"] = {"mutating": False, "remote_write_capable": False}
            mod.CAMERA_TOOL_CLASSIFICATION["camera_ghost_c"] = {
                "mutating": False,
                "remote_write_capable": False,
                "captures_environment": False,
                "unknown_fact": False,
            }
            defaults = mod.camera_tool_approval_defaults()
            for ghost in ("camera_ghost_a", "camera_ghost_b", "camera_ghost_c"):
                self.assertIn(ghost, defaults["require_approval"], ghost)
                self.assertNotIn(ghost, defaults["auto_approve"], ghost)
        finally:
            mod.CAMERA_TOOL_CLASSIFICATION = original

    def test_approval_defaults_derive_from_classification(self):
        from abstractcamera.integrations.abstractcore_tools import camera_tool_approval_defaults

        defaults = camera_tool_approval_defaults()
        # Partition: every tool lands in exactly one bucket.
        tool_names = {fn._tool_definition.name for fn in CAMERA_TOOLS}
        self.assertEqual(set(defaults["auto_approve"]) | set(defaults["require_approval"]), tool_names)
        self.assertEqual(set(defaults["auto_approve"]) & set(defaults["require_approval"]), set())
        # Derivation truth: auto only when every classified fact is False.
        for name in defaults["auto_approve"]:
            self.assertFalse(any(CAMERA_TOOL_CLASSIFICATION[name].values()), name)
        for name in defaults["require_approval"]:
            self.assertTrue(any(CAMERA_TOOL_CLASSIFICATION[name].values()), name)
        # The default: every captures_environment tool defaults to
        # require-approval. This is a DEFAULT, not a floor — the operator
        # ruled (c3938) users may auto-accept camera like any other tool;
        # the derivation just never auto-approves without that choice.
        for name, facts in CAMERA_TOOL_CLASSIFICATION.items():
            if facts["captures_environment"]:
                self.assertIn(name, defaults["require_approval"], name)
        # Today's concrete partition (a change here is a deliberate decision).
        self.assertEqual(defaults["auto_approve"], ["camera_get_events", "camera_list_devices", "camera_status"])


@unittest.skipUnless(HAS_ABSTRACTCORE, "abstractcore is not installed")
class ToolExecution(unittest.TestCase):
    """End-to-end through core's ToolRegistry over the simulator. The tools
    address the process-shared service, so ABSTRACTCAMERA_FAKE routes its
    hub to the simulator and the shared service is reset around each test.
    A tmpdir capture root keeps test captures OUT of the operator's real
    ~/Pictures (adversarial finding: the suite leaked NEFs there)."""

    def setUp(self):
        self._env = _EnvGuard()
        self._env.save()
        os.environ["ABSTRACTCAMERA_FAKE"] = "1"
        self.capture_root = tempfile.mkdtemp(prefix="camtools_test_")
        os.environ["ABSTRACTCAMERA_CAPTURE_ROOT"] = self.capture_root
        fake_gp.reset()
        fake_gp.configure(
            download_stall_s=(0.01, 0.03),
            trigger_latency_s=0.05,
            trigger_latency_jitter_s=0.02,
            file_added_offset_s=0.1,
        )
        reset_shared_service()
        self.registry = ToolRegistry()
        for fn in camera_tools():
            self.registry.register(fn)

    def tearDown(self):
        reset_shared_service()
        self._env.restore()
        fake_gp.reset()

    def _run(self, name, arguments=None):
        return self.registry.execute_tool(ToolCall(name=name, arguments=dict(arguments or {})))

    def test_full_workflow_through_core_registry(self):
        listed = self._run("camera_list_devices")
        self.assertTrue(listed.success, listed.error)
        self.assertTrue(listed.output["cameras"])

        opened = self._run("camera_open")
        self.assertTrue(opened.success, opened.error)
        uid = opened.output["camera"]

        photo = self._run("camera_capture_photo", {"camera": uid})
        self.assertTrue(photo.success, photo.error)
        self.assertTrue(photo.output["path"])
        # The env capture root must govern the TOOLS path too (adversarial
        # finding: only the capability honored it; tool captures landed in
        # the real ~/Pictures).
        self.assertTrue(
            photo.output["path"].startswith(self.capture_root),
            f"capture landed outside ABSTRACTCAMERA_CAPTURE_ROOT: {photo.output['path']}",
        )

        armed = self._run("camera_start_detection", {"action": "monitor", "target": "motion"})
        self.assertTrue(armed.success, armed.error)

        events = self._run("camera_get_events", {"since_id": 0})
        self.assertTrue(events.success, events.error)
        self.assertTrue(any(e["kind"] == "photo" for e in events.output["events"]))

        stopped = self._run("camera_stop_detection")
        self.assertTrue(stopped.success, stopped.error)

        closed = self._run("camera_close", {"camera": uid})
        self.assertTrue(closed.success, closed.error)

    def test_stop_recording_tool_reports_honestly_without_recording(self):
        self._run("camera_open")
        result = self._run("camera_stop_recording")
        self.assertFalse(result.success)
        self.assertIn("No video recording", result.error)

    def test_import_weight_stays_light(self):
        """Load-bearing framework-wide since registration went unconditional
        (adversary F2 2026-07-21: every get_default_toolsets() call now pays
        this import, and the lightness claim was asserted in docstrings but
        never pinned): importing the tools module must NOT load the camera
        stack — OpenCV/numpy load on first tool CALL, not at listing time."""
        import subprocess
        import sys

        probe = (
            "import sys\n"
            "import abstractcamera.integrations.abstractcore_tools\n"
            "heavy = [m for m in ('cv2', 'numpy', 'abstractcamera.service',\n"
            "                     'abstractcamera.camera_manager')\n"
            "         if m in sys.modules]\n"
            "print('HEAVY:' + ','.join(heavy))\n"
        )
        proc = subprocess.run([sys.executable, "-c", probe],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("HEAVY:")][-1]
        self.assertEqual(line, "HEAVY:",
                         f"importing the tools module must stay light, loaded: {line}")

    def test_tool_results_carry_bare_path_media(self):
        """Sight lane through the TOOL lane (commons 3969/4089): tool
        results carry handler-authored `media` as bare paths (no artifact
        store in-process — runtime's executor half may lift them)."""
        self._run("camera_open")
        photo = self._run("camera_capture_photo")
        self.assertTrue(photo.success, photo.error)
        self.assertEqual(photo.output.get("media"), [photo.output["path"]])
        preview = self._run("camera_preview_photo")
        self.assertTrue(preview.success, preview.error)
        self.assertEqual(preview.output.get("media"), [preview.output["path"]])

    def test_preview_photo_looks_without_shooting(self):
        """The eleventh tool (roadmap 2026-07-21): a silent live-view frame
        — file lands under the capture root, NO capture events log (the
        proof no shutter path ran)."""
        opened = self._run("camera_open")
        self.assertTrue(opened.success, opened.error)

        before = self._run("camera_get_events", {"since_id": 0})
        watermark = before.output["last_id"]

        result = self._run("camera_preview_photo")
        self.assertTrue(result.success, result.error)
        path = result.output["path"]
        self.assertTrue(path.startswith(self.capture_root), path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 0)

        after = self._run("camera_get_events", {"since_id": watermark})
        capture_kinds = [e["kind"] for e in after.output["events"]
                         if e["kind"] in ("trigger", "photo", "photo-pending")]
        self.assertEqual(capture_kinds, [],
                         "a preview must not fire the shutter or log capture events")

    def test_specs_are_isolated_copies(self):
        # Hosts rewrite specs for provider wires; a mutation must never
        # corrupt the module-level definitions (adversarial finding).
        specs = camera_tool_specs()
        specs[0]["parameters"]["CORRUPTED"] = {"type": "string"}
        fresh = camera_tool_specs()
        self.assertNotIn("CORRUPTED", fresh[0]["parameters"])
        definitions = camera_tool_definitions()
        definitions[0].parameters["CORRUPTED"] = {"type": "string"}
        self.assertNotIn(
            "CORRUPTED",
            CAMERA_TOOLS[0]._tool_definition.parameters,
            "definition copies must not alias the module-level schema",
        )

    def test_failures_surface_as_tool_errors(self):
        # Nothing connected: the dict {"success": False, "error": ...} must
        # be read by core's registry as a FAILED call, not a payload.
        result = self._run("camera_capture_photo")
        self.assertFalse(result.success)
        self.assertTrue(result.error)

    def test_string_arguments_are_coerced(self):
        # Tool-call parsers preserve raw strings (backlog-039 class):
        # numeric strings must coerce against the declared schema.
        self._run("camera_open")
        result = self._run("camera_get_events", {"since_id": "0", "limit": "5"})
        self.assertTrue(result.success, result.error)


if __name__ == "__main__":
    unittest.main()
