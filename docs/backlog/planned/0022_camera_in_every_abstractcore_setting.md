# 0022 — AbstractCamera in every AbstractCore install setting (light, apple, gpu)

**Created**: 2026-09-29
**Status**: Planned
**Priority**: P2
**Area**: packaging, cross-OS drivers, AbstractCore integration
**Related**: backlog 0005 (Linux/Windows webcam drivers, which this item absorbs), ADR 0003
(base deps carry frames), ADR 0006 (non-invasive discovery), ADR 0007 (regression policy for
unconnected hardware), ADR 0009 (webcam identity by uniqueID), ADR 0012 (AbstractCore
capability plugin)

---

## Summary

Operator ruling 2026-09-29: "abstractcamera should be in all settings, including light — it's
more a feature of which camera is connected. This may or may not require tweaking so the library
actually works across OS. If this is light work, do it now. If it's more complex, create a
planned backlog item."

Assessed 2026-09-29: it is **not light work**. The dependency itself installs wheel-only on
macOS, Linux and Windows, but only macOS gets a working camera from it. On Linux and Windows the
base install gives zero camera transports (no webcam driver exists there), and on headless Linux
the base's `opencv-python` cannot even be imported without a system `libGL.so.1`. Adding
`abstractcamera` to AbstractCore's base dependencies today would add 44–74 MB of wheels on
Linux/Windows for a camera that cannot work there. This item makes the base install useful on
all three systems first; AbstractCore then adds the dependency.

## Current code reality (checked 2026-09-29, abstractcamera 0.2.0 on PyPI, main 9172620)

### What the base install is

`pyproject.toml:44-53`: `numpy>=1.24`, `opencv-python>=4.8`, and on macOS only
`pyobjc-framework-AVFoundation`, `-Quartz`, `-libdispatch` (ADR 0009). `requires-python >=3.10`
(`pyproject.toml:27`); AbstractCore supports `>=3.9`, so a core dependency needs
`; python_version >= '3.10'` (the `abstract3d` precedent, core `pyproject.toml:98`).

Resolution, `uv pip compile --only-binary :all:`, Python 3.12, 2026-09-29:

| Target | Base resolves | Adds over AbstractCore 2.19.0 light |
| --- | --- | --- |
| macOS arm64 | yes: numpy 2.5.3, opencv-python 5.0.0.93, 7 pyobjc packages | opencv-python + pyobjc (numpy already via pandas). Measured: light venv 551 MB → 711 MB (+160 MB; cv2 alone 128 MB, pyobjc ~30 MB) |
| Linux x86_64 (manylinux_2_28) | yes: numpy, opencv-python | opencv-python (73.8 MB wheel) |
| Windows x86_64 | yes: numpy, opencv-python | opencv-python (44.0 MB wheel) |

Extras (same method; macOS with `MACOSX_DEPLOYMENT_TARGET=14.0`):

| Extra | macOS arm64 | Linux x86_64 | Windows x86_64 |
| --- | --- | --- | --- |
| `gphoto2` (tethered PTP) | wheel 2.6.4 for macOS 14+ only (6.5 MB, bundles libgphoto2 in `gphoto2/.dylibs`; no wheel for macOS 13 target) | wheel 2.6.4 (11.8 MB) | **no wheel; libgphoto2 does not support Windows** |
| `clips` (PyAV) | av wheel (18.2 MB) | av wheel (35.8 MB) | av wheel (27.6 MB) |
| `raw` (rawpy) | wheel (2.1 MB) | wheel (3.0 MB) | wheel (0.9 MB) |
| `dwarf` (websocket-client) | pure Python (0.1 MB) | same | same |

Already present in the other settings (resolved `abstractcore[apple]==2.19.0` on macOS 14 arm64,
`abstractcore[gpu]==2.19.0` on manylinux_2_35 and Windows): `apple` already has `opencv-python`
4.14 (via mlx-vlm / mlx-gen) and `av`; Linux `gpu` already has `opencv-python` (via mlx-gen) **and**
`opencv-python-headless` (via vllm / mistral-common) plus `av`; Windows `gpu` has only
`opencv-python-headless` and `av`. So on `apple` the camera adds only the pyobjc packages; on
Windows `gpu` it adds a second OpenCV distribution (dual `cv2` install). AbstractVision 0.3.32
removed mlx-gen from `gpu`, so Linux `gpu` will soon have only `opencv-python-headless` and the
camera would reintroduce the dual install there too.

### What works per OS with the base install

- **macOS**: webcams (built-in, USB, Continuity) through native AVFoundation, hardware-validated
  (ADR 0009, `scripts/validate_webcam_identity.py`); tethered bodies with `[gphoto2]`; DWARF with
  `[dwarf]` + configured hosts.
- **Linux**: no webcam driver. `WebcamDriver.available()` returns False off macOS
  (`src/abstractcamera/drivers/webcam_driver.py:35`, "v4l2/Windows enumeration is future work").
  Only `[gphoto2]` bodies and DWARF work, both behind extras.
- **Windows**: no webcam driver, no gphoto2 wheel. Only DWARF, behind `[dwarf]` + configured hosts.

Probe (scratch venv with AbstractCore 2.19.0 light + abstractcamera 0.2.0, platform facts patched
to Linux / Windows, no device touched, no `ABSTRACTCAMERA_FAKE`): `resolve_drivers() == []`,
`list_cameras() == []`, plugin `available_providers() == []`, `list_models() == []`, and the
default connect raises `CameraControlError("No camera transport is available — install
abstractcamera[gphoto2] ...")` (`src/abstractcamera/discovery.py:118`). The degradation is clean,
but that message advises a bare extra, which the 2026-09-29 install-hint ruling forbids for an
AbstractCore user.

### The headless-Linux import problem

The Linux `opencv-python` wheel is the GUI build: its bundled `libQt5Gui` links `libGL.so.1`,
which manylinux leaves to the host (checked by reading the 5.0.0.93 manylinux_2_28 wheel;
`opencv-python-headless` has no Qt and no GL reference). On a server or container without
`libgl1`, `import cv2` fails. `camera_manager.py:21` imports cv2 at module top, and the plugin's
`list_models()` → `list_cameras()` → `CameraService` path imports `camera_manager` (traced in the
probe: cv2 is first imported by `list_models`, not by registration or `available_providers`). So on
such a host, AbstractCore's catalog call for the camera capability would raise ImportError instead
of reporting "no camera". abstractcamera itself never uses highgui (no `imshow` / `namedWindow` /
`waitKey` in `src/`), so the headless build would serve it; ADR 0003 chose `opencv-python` and
accepted the double install with hosts shipping headless.

### Tests (2026-09-29)

`pytest tests` against the installed 0.2.0 wheel with OpenCV **5.0.0** (what a fresh install
resolves today, the floor has no cap) and AbstractCore 2.19.0: 311 passed. OpenCV 5 is therefore
not a blocker on macOS.

### AbstractCore side today

Core's default install hint for a missing camera plugin says camera is not part of the three
settings (`abstractcore/capabilities/registry.py:649-656`, `server/camera_endpoints.py:83`), and
this package's own hint says the same (`integrations/abstractcore_plugin.py:52-58`, commit
9172620). Both must change in the same wave that adds the dependency.

## Goals

- `pip install abstractcore` (and `[apple]`, `[gpu]`) installs abstractcamera, and every machine
  with a connected camera can use it: webcams on macOS, Linux and Windows; tethered bodies
  wherever libgphoto2 wheels exist.
- A machine with no camera reports "no camera connected" through every surface (plugin, tools,
  `/v1/camera/*`, catalog) without raising, on all three systems, including headless Linux.

## Scope

1. **OpenCV distribution decision** (revisits ADR 0003; write the ADR amendment). Options:
   (a) switch the base to `opencv-python-headless` (no GL dependency, same `cv2` API minus
   highgui, which is unused) — but `apple` pulls `opencv-python` via mlx-vlm / mlx-gen, so macOS
   `apple` would hold both; (b) keep `opencv-python` everywhere and make every catalog/list path
   cv2-free (lazy import of `camera_manager` behind `list_cameras`, which only needs drivers),
   so a missing `libGL` breaks capture only, with an honest error; (c) platform markers
   (`opencv-python` on darwin/win32, `opencv-python-headless` on linux). Measure each against the
   three resolved settings (dual-install count per setting) and pick one. Whatever is chosen, the
   catalog path must not import cv2 (fix (b) is needed in every option).
2. **Linux webcam driver (V4L2)**, absorbing backlog 0005's Linux half. Non-invasive
   enumeration from sysfs (`/sys/class/video4linux/video*/name`, capture-capable nodes only) with
   stable identity from `/dev/v4l/by-id/*` (fallback `by-path`), never positional indices (ADR
   0009); capture by device path through `cv2.VideoCapture(path, cv2.CAP_V4L2)`; permission errors
   (`video` group) reported honestly. Pure Python + OpenCV; no new dependency.
3. **Windows webcam driver**, absorbing 0005's Windows half. OpenCV cannot enumerate device names,
   and opening by MSMF/DirectShow index is exactly the positional mapping ADR 0009 forbids. Candidate
   stacks to measure: WinRT `Windows.Devices.Enumeration` (id + name; `winrt-*` wheels) with capture
   through WinRT `MediaCapture` or a verified id→index mapping; or DirectShow enumeration
   (`pygrabber`). Any new dependency must be wheel-only, permissive-licensed and marked
   `sys_platform == 'win32'`.
4. **Hints**: the "No camera transport is available" message and `_INSTALL_HINT` stop naming
   bare extras for AbstractCore users; say which transports this host has and that the camera is
   part of every setting.
5. **Transport extras in the settings** (decide per extra, then tell core): `dwarf`
   (websocket-client, 0.1 MB, pure Python) can move to base; `raw` is small and wheel-only
   everywhere; `clips` (av) is already in `apple` and `gpu`; `gphoto2` has wheels for macOS 14+
   arm64 and Linux only, so at most a platform-marked entry in `apple` (macOS) and `gpu` (Linux),
   never in light, never on Windows.
6. **Python 3.9**: core's dependency carries `python_version >= '3.10'` (the camera stays absent on
   3.9, reported by core's existing hint) — or the package lowers its floor; decide.

## Non-goals

- New camera families (Canon EOS is 0006), bulb (0002), DWARF choreography (0020).
- Advising any install command other than the three AbstractCore settings.

## Dependencies

- A Linux machine with a UVC webcam and a Windows machine with a webcam for the hardware
  validations (ADR 0007: success claims need the named validations; simulators cannot prove
  enumeration identity).
- AbstractCore owner for the core half (base dependency, hints, docs, `tests/test_install_settings.py`,
  the install-hint guard test) and the root `abstractframework` pins (release sequence:
  abstractcamera release → abstractcore → root).

## Acceptance criteria

- [ ] `uv pip compile --only-binary :all:` resolves `abstractcore` (with the new dependency) for
      macOS arm64, Linux x86_64 manylinux_2_28 and Windows x86_64, and `[apple]` / `[gpu]` on their
      platforms; the report lists what the camera adds per setting and the number of OpenCV
      distributions installed per setting.
- [ ] In a clean container without `libgl1` (e.g. `python:3.12-slim`), `import abstractcamera`,
      plugin registration, `available_providers()`, `list_models()` and `/v1/camera/cameras` report
      no camera without raising.
- [ ] Linux: a UVC webcam is listed with a by-id identity without being opened (no LED), and
      captures a photo; unplug/replug keeps its id; `validate_webcam_identity`-style script passes.
- [ ] Windows: same as Linux with the chosen stack; two cameras never swap identities.
- [ ] Hints: no runtime string advises `pip install abstractcamera...` to an AbstractCore user
      (core's `tests/test_install_hints_no_bare_packages.py` covers the camera plugin strings).
- [ ] ADR 0003 amended (OpenCV distribution) and ADR 0009 extended (identity on Linux/Windows).
- [ ] Core half landed: `abstractcamera>=<released version>; python_version >= '3.10'` in core
      base dependencies, camera hint says "part of every setting", docs + CHANGELOG + llms files
      regenerated, core hermetic suite green.

## Validation

Hermetic: simulator-backed tests for the new drivers (sysfs and WinRT enumerations faked from
recorded fixtures), the `uv pip compile` matrix above, a headless-container import test.
Hardware: one Linux and one Windows webcam session each, with transcripts recorded under
`scripts/validate_*`.
