# FAQ

**Can I control several cameras at the same time?**

Yes — `CameraHub` runs one `CameraManager` (and one worker thread) per
connected camera: concurrent live views, independent dials, sequences,
detections, and recordings. Validated with four cameras at once (two PTP
bodies + the built-in camera + a Continuity iPhone). See
`docs/getting-started.md` § Several cameras at once.

**Where do my captures go?**

`~/Pictures/<device>/` by default — one folder per camera (`nikon_z6_2`,
`macbook_pro_camera`...). Set a sequence name and everything nests in
`~/Pictures/<device>/<sequence>/` until you clear it. Hosts can move the
root (`set_capture_root`) or pin an explicit directory (`set_capture_dir`).
With `set_save_policy(download_locally=False)` captures stay on the
camera's own storage and are only announced in the event feed.

**Which cameras are supported?**
Hardware-validated: Nikon Z (Z6 II), Sony Alpha (A7R IV), macOS built-in
cameras (MacBook Pro), and an iPhone via Continuity Camera (validated
wirelessly at 1080p). Other libgphoto2-supported PTP bodies get the generic
adapter — the honest write ledger and capture flows apply, family quirks may
not.

**Why does my iPhone show up as a camera with no cable connected?**
That is Apple's Continuity Camera: an iPhone/iPad signed into the same Apple
ID advertises itself over Wi-Fi/Bluetooth proximity, and macOS exposes it as
a SYSTEM camera device — AVFoundation (and therefore OpenCV/ffmpeg) sees it
like any webcam. AbstractCamera classifies every webcam entry with a `kind`
(`built_in` | `continuity` | `external`), labels Continuity devices
explicitly with a wireless note, sorts them LAST, and never makes them the
default — connecting someone's phone must be an informed choice, not an
accident. Explicitly selected, it works as a normal webcam-family camera
(the phone's screen shows Apple's Continuity indicator while active).

**Why can't I control ISO/exposure/white balance/focus on the MacBook
camera or a Continuity iPhone?**

Because macOS forbids it — for every app, not just this one. The manual
AVFoundation APIs (`setExposureModeCustom`, focus lens position, WB gains)
are iOS-only; measured on this hardware, every one of them reports
unsupported on macOS, for the built-in camera AND Continuity iPhones. The
one manual control macOS grants is ZOOM (`videoZoomFactor`, a digital
crop) — exposed as a dial. iPhone framing/depth effects (Center Stage,
Portrait, Studio Light) are macOS SYSTEM toggles: Control Center → Video
Effects while the camera is live; apps cannot set them programmatically.

**Why doesn't the webcam expose ISO/shutter/aperture dials?**
Because the hardware doesn't: every `cv2 CAP_PROP_*` control set returns
False on AVFoundation (measured). The package never fabricates dials
(ADR 0004) — the webcam family exposes resolution, and its stills are
honestly labeled as video frames.

**Can two apps use the same webcam?**
On the validated machine AVFoundation SHARES the device (a second in-process
open delivered frames while connected — measured 2026-07-12). Sharing
behavior varies across macOS versions; a losing side surfaces the honest
open error, and a dying stream trips the liveness watchdog into an honest
disconnect.

**Why does connecting a tethered camera kill `ptpcamerad`?**
macOS's own PTP daemons claim every camera on plug-in (for Photos/Image
Capture) and cause `[-53] Could not claim the USB device`. Releasing them is
the standard libgphoto2-on-macOS workaround; they respawn on demand. The
webcam driver never does this.

**Why is my Sony movie note saying recording "cannot be confirmed"?**
The A7R IV accepts movie start/stop over USB but reports no recording
status, emits no events, and announces no file (measured). The receipt says
exactly that. The webcam family is the opposite: the package writes the MP4
itself, so movie receipts are confirmable.

**Why did my config write get "reverted"?**
The body kept a different value after the patience window (physical dial
ownership, ISO-Auto override, mode-dial gating...). The catch-log message
names the specific cause when known. On Sony, writes are verified in-call
with retries first — a revert there means the body genuinely refused.

**Can a webcam's name point at the wrong camera?**
Not anymore. Names USED to come from ffmpeg's device list positionally
mapped onto OpenCV indices — and that mapping inverted on real hardware
(2026-07-12). Since ADR 0009, the id IS the AVFoundation uniqueID and the
session opens that exact device object natively: the name and the stream
come from the same object (`name_confidence: "reported"`). Stale ids from
before a Continuity join/leave refuse with "refresh and pick again".

**Does detection work on the webcam?**
Yes — detection, the rolling buffer, ring clips, and the intervalometer all
consume live-view frames and are family-independent. Motion detection on
the built-in camera is genuinely useful (auto-fire grabs frames).

**What does `ABSTRACTCAMERA_FAKE=1` do?**
Replaces ALL transports with the simulator (deterministic, no USB, no LED):
the exact semantics camera-less hosts and CI need. Configure scenarios via
`abstractcamera.sim.gphoto2.configure(...)` (profiles: `z6ii`, `a7r4`).
