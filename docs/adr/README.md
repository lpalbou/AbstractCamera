# Architecture Decision Records

| ADR | Decision |
| --- | --- |
| [0001](0001_session_protocol_boundary.md) | Session-protocol boundary: the manager loop moves verbatim; families provide sessions speaking the pinned wire protocol |
| [0002](0002_family_semantics_are_adapter_owned.md) | Family semantics are adapter-owned; one worker thread owns all camera I/O |
| [0003](0003_base_deps_carry_frames_transports_are_extras.md) | Base deps carry frame processing (numpy+OpenCV); transports are explicit extras |
| [0004](0004_capability_honesty.md) | Capability honesty: absence is a first-class answer; no pretend controls |
| [0005](0005_detection_in_package_analyzers_injected.md) | Detection lives in-package; host analyzers are injected |
| [0006](0006_discovery_and_camera_identity.md) | Discovery is non-invasive; ids are positional and refuse when stale |
| [0007](0007_regression_policy_for_unconnected_hardware.md) | Regression policy for unconnected hardware: pins and simulators may move, assertions never weaken |
| [0008](0008_multi_camera_hub_and_capture_layout.md) | Multi-camera: a hub of single-camera managers (one worker each), device-slug identity, `~/Pictures/<device>/[<sequence>/]` layout, on-device/local save policy |
| [0009](0009_webcam_identity_by_unique_id.md) | Webcam identity by AVFoundation uniqueID with native capture — no index space between enumeration and open (fixes the 2026-07-12 name/device inversion); fail-closed residuals only |
| [0010](0010_dwarf_network_family_and_mount_actions.md) | DWARF network family: the session protocol absorbs a Wi-Fi telescope (RTSP live view, album-backed captures); the MOUNT rides the action channel as family actions (one-shot, never replayed); discovery is configured, never scanned; wire codec vendored from the published spec |
