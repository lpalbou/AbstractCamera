"""Hardware validation: ALL connected cameras piloted AT THE SAME TIME
through one CameraHub — Nikon Z6 II + Sony A7R IV + MacBook camera
(+ iPhone Continuity when present). Each camera gets its own worker; live
views must flow CONCURRENTLY; captures land in per-device folders; a named
sequence runs on one body while another shoots stills and a webcam records
a movie — simultaneously.

Run:  python3 scripts/validate_multicam.py
"""

import os
import sys
import tempfile
import time

PASS, FAIL, INFO = [], [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)


def info(label, detail=""):
    print(f"[INFO] {label}" + (f" — {detail}" if detail else ""), flush=True)


def wait_until(predicate, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main():
    from abstractcamera import CameraHub

    root = tempfile.mkdtemp(prefix="multicam_root_")
    hub = CameraHub(capture_root=root)
    entries = hub.list_cameras()
    info("discovered", "; ".join(f"{e['id']}={e['name']}" for e in entries))

    nikon_entry = next((e for e in entries if "nikon" in e["name"].lower()), None)
    sony_entry = next((e for e in entries if "sony" in e["name"].lower()), None)
    builtin_entry = next((e for e in entries if e.get("kind") == "built_in"), None)
    iphone_entry = next((e for e in entries if e.get("kind") == "continuity"), None)

    # ---- 1. connect everything ------------------------------------------------
    managers = {}
    for label, entry in (("nikon", nikon_entry), ("sony", sony_entry),
                         ("builtin", builtin_entry), ("iphone", iphone_entry)):
        if entry is None:
            info(f"{label} not present — skipped")
            continue
        try:
            status = hub.connect(camera_id=entry["id"])
            managers[label] = hub.manager_for(status["device_uid"])
            check(f"connect {label}", status["connected"],
                  f"{status['model']} -> uid {status['device_uid']}")
        except Exception as exc:
            check(f"connect {label}", False, str(exc))

    check("at least 3 cameras live simultaneously", len(managers) >= 3,
          f"{len(managers)} connected: {sorted(managers)}")
    statuses = hub.statuses()
    check("hub statuses cover all live cameras", len(statuses) == len(managers),
          str(sorted(statuses)))
    uids = {label: m.status()["device_slug"] for label, m in managers.items()}
    info("device folders", str(uids))

    # ---- 2. concurrent live views ------------------------------------------------
    flows = {}
    for label, manager in managers.items():
        flows[label] = wait_until(lambda m=manager: m.get_latest_frame()[1] >= 10, timeout=15.0)
    check("ALL live views flow concurrently", all(flows.values()), str(flows))
    time.sleep(3.0)
    fps_report = {label: m.status()["fps"] for label, m in managers.items()}
    check("every stream keeps a real frame rate", all(v > 8 for v in fps_report.values()),
          str(fps_report))

    # ---- 3. simultaneous operations ------------------------------------------------
    # Nikon: named 3-shot sequence; Sony: two stills; builtin: a movie;
    # iphone: keeps streaming. All AT ONCE.
    if "nikon" in managers:
        nikon = managers["nikon"]
        # Unformatted-card session: route captures through the camera buffer.
        if any("Card not formatted" in (e["note"] or "") for e in nikon.get_events()):
            nikon.set_config_value("capturetarget", "Internal RAM")
            wait_until(lambda: nikon.status()["config"].get("capturetarget", {}).get("value")
                       == "Internal RAM", timeout=15.0)
        nikon.start_interval_sequence(interval_s=4.0, count=3, sequence_name="multicam run")

    if "builtin" in managers:
        builtin = managers["builtin"]
        builtin.set_capture_mode("video")
        builtin.request_trigger()  # start recording

    if "sony" in managers:
        sony = managers["sony"]
        sony.set_config_value("focusmode", "Manual")
        wait_until(lambda: sony.status()["config"].get("focusmode", {}).get("value") == "Manual",
                   timeout=12.0)
        sony.set_config_value("shutterspeed", "1/60")
        wait_until(lambda: sony.status()["config"].get("shutterspeed", {}).get("value") == "1/60",
                   timeout=12.0)

        def sony_photos():
            return len([e for e in sony.get_events() if e["kind"] == "photo"])

        # The body intermittently drops accepted triggers silently
        # (hardware-observed; the expectation watch reports each drop) —
        # fire until two stills actually land, bounded attempts.
        attempts = 0
        while sony_photos() < 2 and attempts < 5:
            attempts += 1
            before = sony_photos()
            sony.request_trigger()
            wait_until(lambda: sony_photos() > before, timeout=12.0)
        info("sony stills", f"{sony_photos()} landed in {attempts} attempts")

    if "builtin" in managers:
        time.sleep(2.0)
        managers["builtin"].request_trigger()  # stop recording
        wait_until(lambda: not managers["builtin"].status()["movie_recording"], timeout=10.0)

    if "nikon" in managers:
        check("nikon sequence completes while others work", wait_until(
            lambda: managers["nikon"].status()["interval"]["state"] == "complete", timeout=45.0),
            str(managers["nikon"].status()["interval"]))

    # ---- 4. per-device folders ------------------------------------------------------
    def files_under(slug, subdir=None):
        base = os.path.join(root, slug, subdir) if subdir else os.path.join(root, slug)
        if not os.path.isdir(base):
            return []
        return [f for f in os.listdir(base) if os.path.isfile(os.path.join(base, f))]

    if "nikon" in managers:
        slug = uids["nikon"]
        check("nikon sequence files in <device>/<sequence>/", wait_until(
            lambda: len([f for f in files_under(slug, "multicam_run")
                         if f.lower().endswith((".nef", ".jpg"))]) >= 3, timeout=45.0),
            f"{files_under(slug, 'multicam_run')}")
    if "sony" in managers:
        slug = uids["sony"]
        check("sony stills in <device>/", wait_until(
            lambda: len([f for f in files_under(slug) if f.lower().endswith(".arw")]) >= 2,
            timeout=30.0), f"{len(files_under(slug))} files")
    if "builtin" in managers:
        slug = uids["builtin"]
        check("builtin movie in <device>/", wait_until(
            lambda: any(f.lower().endswith(".mp4") for f in files_under(slug)), timeout=20.0),
            str(files_under(slug)))

    # ---- 5. isolation: settings of one body never leak to another --------------------
    if "nikon" in managers and "sony" in managers:
        sony_iso_before = managers["sony"].status()["config"].get("iso", {}).get("value")
        managers["nikon"].set_config_value("iso", "800")
        wait_until(lambda: managers["nikon"].status()["config"].get("iso", {}).get("value") == "800",
                   timeout=15.0)
        sony_iso_after = managers["sony"].status()["config"].get("iso", {}).get("value")
        check("config isolation between bodies", sony_iso_before == sony_iso_after,
              f"sony iso {sony_iso_before} -> {sony_iso_after}")

    # ---- 6. save policy: Sony device-only while Nikon still downloads -----------------
    if "sony" in managers:
        sony = managers["sony"]
        sony.set_save_policy(download_locally=False)
        photos_before = len([e for e in sony.get_events() if e["kind"] == "photo"])
        sony.request_trigger()
        check("sony device-only announce", wait_until(
            lambda: any(e["kind"] == "photo-pending" and "saved on the camera" in (e["note"] or "")
                        for e in sony.get_events()), timeout=15.0))
        time.sleep(2.0)
        photos_after = len([e for e in sony.get_events() if e["kind"] == "photo"])
        check("sony device-only: no local download", photos_after == photos_before)
        sony.set_save_policy(download_locally=True)

    # ---- 7. teardown -------------------------------------------------------------------
    hub.disconnect_all()
    check("all disconnected", len(hub.statuses()) == 0)

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
