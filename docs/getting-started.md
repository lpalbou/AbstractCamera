# Getting started

## Install

```bash
pip install abstractcamera                  # webcam + simulator
pip install "abstractcamera[gphoto2]"       # + tethered PTP bodies
pip install "abstractcamera[clips,raw]"     # + MP4 encoding, RAW thumbnails
pip install "abstractcamera[dwarf]"         # + DWARF smart telescopes (Wi-Fi)
```

macOS note for tethered bodies: the package releases Apple's PTP daemons
(`ptpcamerad`/`mscamerad`) before claiming a camera — this is the standard
libgphoto2-on-macOS workaround and the daemons respawn on demand.

macOS note for the webcam: the FIRST camera access from a new host process
triggers the system permission prompt (System Settings → Privacy & Security
→ Camera). Packaged apps must carry `NSCameraUsageDescription` in their
Info.plist or macOS kills the process instead of prompting.

## Enumerate and connect

```python
from abstractcamera import CameraManager, list_cameras

for camera in list_cameras():
    print(camera["id"], camera["name"], camera.get("kind"), camera.get("name_confidence"))
# ptp:usb:002,001            Sony DSC-A7r IV (Control)   None        reported
# webcam:1A2B3C4D-5E6F-...   MacBook Pro Camera          built_in    reported
# webcam:9F8E7D6C-0A1B-...   ... iPhone Camera           continuity  reported

manager = CameraManager()
manager.connect()                      # default: first PTP body, else built-in webcam
manager.connect(camera_id="webcam:1A2B3C4D-...")  # or pick explicitly
```

Listing is non-invasive (no device opens, no LED). Webcam ids are
AVFoundation uniqueIDs and the session opens THAT device object natively
(ADR 0009) — a name can never point at the wrong camera. Re-list before
connecting; a stale id refuses honestly instead of opening a different
device.

Webcam entries carry a `kind`: `built_in` (the machine's own camera),
`continuity` (a nearby iPhone/iPad exposed WIRELESSLY by macOS Continuity
Camera — same Apple ID, no cable), or `external` (USB cameras). Continuity
devices sort last and are never the default; select one explicitly and it
works as a normal webcam-family camera.

## Smart telescopes (DWARF) — a camera you can also steer

DWARF 3 units are Wi-Fi devices (`pip install "abstractcamera[dwarf]"`).
Discovery is configured, not scanned: name the telescope's IP and it
appears in `list_cameras()` like any other camera.

```bash
# AP mode (you joined the DWARF's own Wi-Fi): the device is always 192.168.88.1
# STA mode (the DWARF joined YOUR network): the DWARFLAB app shows its IP
export ABSTRACTCAMERA_DWARF_HOSTS=192.168.88.1
```

```python
manager = CameraManager()
manager.connect(camera_id="dwarf:192.168.88.1")   # master lock + RTSP live view

manager.set_config_value("shutterspeed", "15")    # the device's own gear tables
manager.set_config_value("gain", "80")
manager.request_trigger()   # shutter -> DWARF album (microSD) -> Wi-Fi download

# The MOUNT is driven through one-shot actions (never cached, never replayed):
manager.request_action("calibrate")                     # once, under open sky
manager.request_action("gotoradec", "83.82,-5.39,M42")  # RA/Dec in degrees (J2000)
manager.request_action("gotosolar", "moon")
manager.request_action("joystick", "90,1,5")            # angle°, length 0-1, °/s
manager.request_action("joystickstop")
manager.request_action("autofocusdrive")                # astro autofocus
```

GOTO/calibration/tracking progress arrives in the catch log
(`get_events()`) as the device's own status notifications ("GOTO
running/success/failed"). One caveat: the DWARF grants ONE controller at a
time — close the DWARFLAB app (or release control there) or connect()
refuses with exactly that message. Hardware smoke test:
`python3 scripts/validate_dwarf.py` (mount motion strictly opt-in).

## Live view, dials, capture

```python
status = manager.status()               # model, family, capabilities, config, fps...
jpeg, seq = manager.get_latest_frame()  # latest live-view JPEG

manager.set_config_value("iso", "800")
# -> status()["pending_writes"]["iso"] goes pending -> confirmed | reverted
#    (dial-owned widgets on real bodies silently revert; the ledger says so)

manager.request_trigger()               # single shot; downloads to set_capture_dir(...)
manager.set_capture_mode("burst", burst_count=5)          # count families
manager.set_capture_mode("burst", burst_hold_s=1.0, burst_speed="Hi")  # duration families (Sony)
manager.set_capture_mode("video"); manager.request_trigger()  # movie start/stop
```

Consult `status()["capabilities"]` before building UI: burst mode
(count vs duration), movie confirmability, the ISO-Auto story, Save-To
vocabulary, focus support, and `config_widgets` (the dials this family can
ever have — hide the rest).

## Several cameras at once

```python
from abstractcamera import CameraHub

hub = CameraHub()                        # capture root defaults to ~/Pictures
for entry in hub.list_cameras():         # discovery + live state annotations
    status = hub.connect(camera_id=entry["id"])
    print(status["device_uid"])          # nikon_z6_2, sony_dsc_a7r_iv, macbook_pro_camera...

nikon = hub.manager_for("nikon_z6_2")    # each camera: its own CameraManager
nikon.start_interval_sequence(interval_s=5, count=100, sequence_name="orion run")
hub.manager_for("sony_dsc_a7r_iv").request_trigger()   # meanwhile, a Sony still
hub.select("macbook_pro_camera")         # the ACTIVE camera (default target)
hub.disconnect_all()
```

Each connected camera runs its own worker thread (libgphoto2 is thread-safe
per camera): live views stream concurrently and sequences/detection/
recordings keep running on non-selected cameras. Hardware-validated with
four simultaneous cameras (two PTP bodies + two webcams).

## Where captures land

```python
manager.set_capture_root("~/Pictures")   # the default
manager.request_trigger()                # -> ~/Pictures/<device_slug>/capture_*.nef
manager.set_sequence_name("orion run")   # -> ~/Pictures/<device_slug>/orion_run/...
manager.set_save_policy(download_locally=False)  # stay on the camera's card
```

Device slugs are filesystem-safe model names (`nikon_z6_2`); two identical
bodies get serial-suffixed folders. With local download OFF the event feed
still announces every capture (`saved on the camera`); a volatile capture
target (camera RAM) plus device-only draws a loud warning — those shots
would exist nowhere.

## Detection, intervalometer, rolling clips

```python
manager.set_detection_mode("monitor", target="motion", sensitivity=70)
manager.set_detection_mode("auto", target="lightning")   # auto-fire

manager.start_interval_sequence(interval_s=5.0, count=100, start_delay_s=10)
# absolute deadlines; per-sequence JSONL manifest under <capture_dir>/sequences/

manager.set_rolling_buffer(True, seconds=10)
clip = manager.save_rolling_clip()      # "keep the last N seconds" -> MP4 ([clips])
```

## Camera-less development

```bash
ABSTRACTCAMERA_FAKE=1 python your_app.py
```

Every transport is replaced by the simulator. Scenario scripting:

```python
import abstractcamera.sim.gphoto2 as sim
sim.configure(profile="a7r4")            # or "z6ii" (default)
sim.configure(trigger_latency_s=0.3, inject_streaks=[...])
```

Tests inject the simulator per-manager instead:
`CameraManager(driver=FakeDriver(sim_module))`.

## CLI

```bash
abstractcamera list       # enumerate across transports
abstractcamera preview    # connect + measure live-view fps (triggers the TCC prompt)
```
