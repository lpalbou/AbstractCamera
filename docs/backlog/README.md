# Backlog

## Open items

| ID | Item | Notes |
| --- | --- | --- |
| 0001 | Size-aware rolling ring | The pre-capture ring is seconds-based; at 1080p/q85 a 10s ring is ~66-130MB (disclosed in docs). A byte-budgeted ring would bound memory explicitly. |
| 0002 | Bulb exposures | Both validated PTP bodies refuse remote `Bulb` writes; the `bulb` action toggle on Sony is accepted but unvalidated. Needs a hardware session dedicated to bulb choreography + timing ownership. |
| 0004 | Webcam duration bursts | Count mode shipped (validated); `round(hold_s * fps)` duration bursts are trivial if a host wants the Sony-style knob on webcams. |
| 0005 | Linux/Windows webcam drivers | The webcam driver is macOS/AVFoundation only (honest `available()=False` elsewhere); v4l2 and DirectShow enumeration are future families of work. |
| 0006 | Canon EOS family | `eosremoterelease` press choreography, EOS event vocabulary; one adapter file + a `select_adapter` match when a body is available to measure. |
| 0008 | Sony trigger-drop root cause | The A7R IV intermittently accepts `trigger_capture` and never fires, even in Manual focus (observed 2026-07-12; the expectation watch reports each drop honestly). Root cause unknown — candidate: internal busy states while digesting settings/card writes. Needs a dedicated hardware session correlating drops with preceding operations. |
| 0009 | Hub-level "fire all" choreography | The hub pilots cameras independently; a synchronized multi-camera trigger (single wall-clock deadline across bodies) would serve rigs. Needs per-family latency compensation to be meaningful. |

## Completed

| ID | Item | Date |
| --- | --- | --- |
| — | Extraction from BlackPixel + Sony/webcam hardware validation | 2026-07-12 |
| 0007 | Nikon Z re-validation through the package: 18/18 on the Z6 II (connect-by-id among two PTP bodies, ledger writes, single/burst/named sequence). Bonus honesty: unformatted-card warnings at connect + named trigger-failure causes. `scripts/validate_nikon.py` | 2026-07-12 |
| 0003 | AVFoundation device identity — closed by ADR 0009 after the positional mapping INVERTED on hardware: webcam ids are uniqueIDs, capture is native AVFoundation by uniqueID (2-design adversarial review; 11/11 `scripts/validate_webcam_identity.py`; the pyobjc dependency was weighed and accepted as base, darwin-only) | 2026-07-12 |
| — | CameraHub multi-camera piloting + device-slug capture layout (`~/Pictures/<device>/[<sequence>/]`) + save policy; 16/16 four-camera simultaneous hardware validation. `scripts/validate_multicam.py`, ADR 0008 | 2026-07-12 |
