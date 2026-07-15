"""Regenerate llms-full.txt from llms.txt + the core docs (run after doc changes)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = [
    "README.md", "docs/getting-started.md", "docs/architecture.md", "docs/api.md",
    "docs/faq.md", "docs/troubleshooting.md",
    "docs/adr/0001_session_protocol_boundary.md",
    "docs/adr/0002_family_semantics_are_adapter_owned.md",
    "docs/adr/0003_base_deps_carry_frames_transports_are_extras.md",
    "docs/adr/0004_capability_honesty.md",
    "docs/adr/0005_detection_in_package_analyzers_injected.md",
    "docs/adr/0006_discovery_and_camera_identity.md",
    "docs/adr/0007_regression_policy_for_unconnected_hardware.md",
    "docs/adr/0008_multi_camera_hub_and_capture_layout.md",
    "docs/adr/0009_webcam_identity_by_unique_id.md",
    "docs/adr/0010_dwarf_network_family_and_mount_actions.md",
    "CHANGELOG.md",
]

parts = [(ROOT / "llms.txt").read_text(), "\n\n---\n\n# Full documentation bundle\n"]
for path in DOCS:
    parts.append(f"\n\n## FILE: {path}\n\n" + (ROOT / path).read_text())
(ROOT / "llms-full.txt").write_text("".join(parts))
print("llms-full.txt regenerated")
