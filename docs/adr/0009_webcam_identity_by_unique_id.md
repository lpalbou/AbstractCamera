# ADR 0009: Webcam identity by uniqueID (native AVFoundation capture)

Status: accepted (2026-07-12)
Amends: ADR 0006 (webcam id scheme and naming portions)

## Context: the inversion

The original webcam family mapped device NAMES from `ffmpeg -f avfoundation
-list_devices` positionally onto `cv2.VideoCapture(index)` — two different
enumerators, consulted at two different times. On 2026-07-12 the mapping
INVERTED on the owner's machine: the row labeled "MacBook Pro Camera"
streamed the iPhone and vice versa. Continuity cameras join and leave the
AVFoundation device set dynamically; any positional join between
enumerators rots silently.

Load-bearing measurements from the live broken state:

- OpenCV 4.13 enumerates with the LEGACY API (`devicesWithMediaType:` —
  cap_avfoundation_mac.mm:358), muxed devices appended after video.
- Post-open verification is IMPOSSIBLE: `isInUseByAnotherApplication()`
  reads False even while another process actively holds the device.
- This macOS reports the Continuity iPhone's `deviceType` as plain
  `"AVCaptureDeviceTypeExternal"` (not the Continuity type), and
  `hasTorch()` False for it (the torch cannot serve as an identity oracle).
- A DiscoverySession with builtin/external/continuity type filters MISSED
  the iPhone entirely under this PyObjC version's constants.

## Decision (adversarial review: 2 designs + adjudication)

Design A (same-enumerator index resolution, keep OpenCV capture) was
rejected: its correctness would ride an unversioned private detail of
opencv-python, and its residual failure (device-set churn inside the open
bracket) fails WRONG (silently wrong stream). The elected Design B removes
the index space entirely:

1. **Ids are device identities**: `webcam:<AVCaptureDevice uniqueID>`.
   Names come from `localizedName()` on the SAME object —
   `name_confidence: "reported"`. Old positional ids refuse loudly.
2. **Capture is native AVFoundation, opened by uniqueID**
   (`drivers/avf_capture.py`): `deviceWithUniqueID_` →
   `AVCaptureDeviceInput` → `AVCaptureSession` + `AVCaptureVideoDataOutput`
   (BGRA) → delegate on a private serial dispatch queue → one strided copy
   per frame into numpy under a single Condition. OpenCV remains for JPEG
   encoding only. The delegate takes ONE lock and never calls session
   methods; lifecycle is worker-thread-only (stopRunning on the delegate
   queue is the classic AVF deadlock, excluded by construction).
3. **Kind classification is structured-first**: isContinuityCamera
   selector → deviceType sets → modelID prefix → name heuristics LAST.
4. **Resolutions are device-reported** (`formats` → dimensions, landscape
   video formats, ∩ familiar ladder + native; switched via activeFormat,
   confirmed on the actual stream) — replaces probe-by-trial.
5. **TCC is diagnosed deterministically** (authorizationStatus pre-check)
   instead of being inferred from a mute frame timeout.
6. `read_serial()` returns the uniqueID: the hub's identical-label
   disambiguation becomes stable across reconnects.

Every residual failure mode fails CLOSED (refusal, honest disconnect) —
never a silently wrong stream. Remaining wrong-LABEL paths are macOS-side
(two devices with identical localizedNames: streams stay correct, the
picker is ambiguous).

## Validation

`scripts/validate_webcam_identity.py` on the previously-inverted machine:
11/11 — uniqueID ids, reported names, structured kinds, both webcams
streaming concurrently, and the cross-wiring oracle (different resolutions
commanded per label; each SOF-confirmed stream followed its OWN command).
Full suite 152 green with the FakeFrameSource seam (pure numpy, no PyObjC,
CI-safe). The physical label↔scene binding was human-confirmed in the app.

## Consequences

- New macOS deps (base): pyobjc-framework-AVFoundation / Quartz /
  libdispatch. Hosts bundling with PyInstaller add them to hiddenimports.
- ffmpeg is no longer consulted for anything (enumeration deleted).
- The fake seam changed from FakeVideoCapture (cv2 double) to
  FakeFrameSource (frame-source double) — same scripted behaviors, plus
  real frame pacing (a fantasy frame rate distorted ring arithmetic).
- Non-macOS: the driver reports honest absence exactly as before.
