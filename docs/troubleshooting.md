# Troubleshooting

**`Tethering support is not installed (python-gphoto2 missing)`**
Install the extra: `pip install "abstractcamera[gphoto2]"`. The PyPI package
name is `gphoto2` (python-gphoto2); floor 2.5.10 for the single-config API.

**`[-53] Could not claim the USB device` on connect**
Another process holds the camera. The package already releases macOS's PTP
daemons before claiming; quit Photos/Image Capture/other tethering apps and
retry. A crash can leave a stale claim — replug the USB cable.

**Connect SEGFAULTs or crashes right after a daemon release**
Fixed in the package (a 0.5s settle after killing `ptpcamerad` — claiming
mid-teardown crashed deep in libgphoto2 on a real A7R IV). If you see it,
you are bypassing `Driver.prepare_connect`.

**`No frames arrived — macOS may have denied camera access`**
Grant camera permission to the HOST process (System Settings → Privacy &
Security → Camera): the terminal/IDE in dev, the app bundle when packaged.
Packaged apps must ship `NSCameraUsageDescription` in Info.plist or macOS
kills the process instead of prompting. The same message appears when
another app holds the device exclusively — cv2 cannot distinguish the two;
the text says so.

**The process died with SIGSEGV in `_wrap_CameraWidget_get_value` (pre-0.2)**
python-gphoto2 segfaults when a body returns a NULL string value (bodies
do this transiently mid-wake). Fixed structurally: all string widget reads
go through `ptp_safe` (ctypes NULL-guard against the loaded libgphoto2);
a NULL reads as an absent value.

**Webcam name showed the WRONG camera (pre-0.2 versions)**
Fixed at the root (ADR 0009): ids are now AVFoundation uniqueIDs and the
session opens that exact device object — the old positional ffmpeg↔OpenCV
mapping (which inverted on 2026-07-12) is gone. If a host persisted an old
`webcam:<number>` id, connect refuses with "refresh and pick again".

**`the camera list changed — refresh and pick again`**
Camera ids are positional/address-based and renumber when devices come and
go (USB replug, iPhone proximity). Re-list and reconnect; the refusal exists
so you never silently open the wrong camera.

**Sony writes keep "reverting" right after a burst**
The body answers `[-2] Bad parameters` for ~10-15s while flushing frames to
the card. The adapter retries in-call and the manager requeues transient
failures with pacing — if you still see a revert, the busy phase outlasted
the retry budget; wait and re-apply.

**Sony fires nothing in AF focus modes (no error)**
Measured behavior: with focus priority and no lock (dark scene), the body
accepts the trigger and silently refuses to fire. The manager reports it
("no file arrived...") and suggests Manual focus; sequences preflight-warn.

**Movie refuses with `movie recording needs the [clips] extra`**
`pip install "abstractcamera[clips]"` (PyAV). The refusal is deliberate —
nothing pretends to record.

**Rolling clip says "buffer is still filling"**
The ring holds the RECENT contiguous span only; stale frames from an
earlier phase don't count (that lie was found and fixed on hardware). Wait
for `status()["rolling"]["buffered_s"]` to reach ~2s.

**`Could not reach the DWARF at <ip>:9900`**
The telescope is not on this network (or asleep). AP mode: join the
DWARF's own Wi-Fi — the device is always `192.168.88.1`. STA mode: connect
the DWARF to your router in the DWARFLAB app (Connection Settings shows
its IP) and export `ABSTRACTCAMERA_DWARF_HOSTS=<that ip>`. Discovery
sweep: `python3 scripts/validate_dwarf.py` probes the local /24 for the
control port (the library itself never scans).

**`The DWARF granted only observer access`**
The device allows ONE master controller and the DWARFLAB app currently
holds it. Close the app (or release control in it) and reconnect. The
refusal is deliberate: a session without the master lock looks connected
but every write would fail downstream.

**DWARF connects but `no file appeared in the DWARF's album`**
Captures land on the telescope's microSD first: no card (or a full one)
means no file — the DWARF's own error codes surface in the catch log
(`no SD card is present`, `writing ... failed`). Slow Wi-Fi can also push
the album entry past the announce window; the file still lands in the
album and downloads on the next capture's poll.

**DWARF GOTO fails immediately**
`GOTO failed (target below horizon or plate solving failed)` is the
device's own refusal. Run `request_action("calibrate")` once under open
sky first (`no GOTO has run yet` names the same gap), check the target is
above the horizon, and mind the mount limit warnings in the catch log.

**Simulated camera in tests without env vars**
`CameraManager(driver=FakeDriver(abstractcamera.sim.gphoto2))` — the
injection seam used by the package's own suites.
