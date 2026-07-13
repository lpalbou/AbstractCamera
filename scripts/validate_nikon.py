"""Hardware validation: the REAL Nikon Z6 II through the packaged
CameraManager — the first physical-body run since the extraction (package
backlog 0007 / ADR 0007). Exercises connect-by-id (port binding among TWO
connected PTP bodies), identity/capture layout, dials through the ledger,
single + burst + a short interval sequence, and honest disconnect.

Run:  python3 scripts/validate_nikon.py
"""

import os
import sys
import tempfile
import time

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)


def wait_until(predicate, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main():
    from abstractcamera import CameraManager, list_cameras

    cameras = list_cameras()
    nikon = [c for c in cameras if "nikon" in c["name"].lower()]
    sony = [c for c in cameras if "sony" in c["name"].lower()]
    check("discovery lists the Nikon", bool(nikon), nikon[0]["name"] if nikon else "absent")
    check("both PTP bodies visible", bool(nikon and sony),
          f"{len([c for c in cameras if c['transport'] == 'ptp'])} PTP entries")

    manager = CameraManager()
    root = tempfile.mkdtemp(prefix="nikon_validation_root_")
    manager.set_capture_root(root)

    # Connect BY ID: with two PTP bodies present, port binding must claim
    # the Nikon specifically (new code path on multi-body hardware).
    status = manager.connect(camera_id=nikon[0]["id"])
    check("connect by id", status["connected"], f"model={status['model']}")
    check("family=nikon_z", status["family"] == "nikon_z", status["family"])
    check("device slug", (status["device_slug"] or "").startswith("nikon"),
          f"{status['device_slug']} (serial: {status['device_serial']})")

    # Live view.
    check("live view frames flow",
          wait_until(lambda: manager.get_latest_frame()[1] >= 10, timeout=15.0))
    time.sleep(2.5)
    fps = manager.status()["fps"]
    check("live view fps > 15", fps > 15, f"{fps} fps")

    # Card health: an unformatted/absent card fails EVERY capture with a
    # bare [-1] (hardware truth this run surfaced). The package warns at
    # connect; this validation then routes captures through the camera
    # buffer (USB-download-only) so the capture path is still proven.
    card_warning = next((e["note"] for e in manager.get_events()
                         if e["kind"] == "error" and "card" in (e["note"] or "").lower()
                         and "format" in (e["note"] or "").lower()), None)
    original_target = str(manager.status()["config"].get("capturetarget", {}).get("value"))
    if card_warning:
        print(f"[INFO] card problem reported at connect: {card_warning[:100]}", flush=True)
        manager.set_config_value("capturetarget", "Internal RAM")
        check("capture target -> camera buffer (card unusable)", wait_until(
            lambda: manager.status()["config"].get("capturetarget", {}).get("value") == "Internal RAM",
            timeout=15.0))

    # Dials through the ledger (Nikon settles lazily ~5-7s).
    original = {}
    for name, value in (("iso", "1600"), ("shutterspeed", "0.0100s")):
        entry = manager.status()["config"].get(name, {})
        original[name] = str(entry.get("value"))
        if not entry:
            check(f"write {name}", False, "widget absent (mode dial position?)")
            continue
        manager.set_config_value(name, value)
        check(f"write {name}={value} confirmed", wait_until(
            lambda n=name, v=value: manager.status()["config"].get(n, {}).get("value") == v,
            timeout=15.0), f"was {original[name]}")

    # Single shot -> NEF into <root>/<slug>/.
    manager.request_trigger()
    got_photo = wait_until(
        lambda: any(e["kind"] == "photo" for e in manager.get_events()), timeout=30.0)
    photo = next((e for e in manager.get_events() if e["kind"] == "photo"), None)
    slug = manager.status()["device_slug"]
    check("single trigger -> photo downloaded", got_photo,
          photo["note"] if photo else "none")
    check("file lands in the device folder",
          bool(photo and photo["path"] and slug in photo["path"]
               and os.path.exists(photo["path"])),
          photo["path"] if photo else "none")

    # Burst (count drive — the Nikon path).
    photos_before = len([e for e in manager.get_events() if e["kind"] == "photo"])
    manager.set_capture_mode("burst", burst_count=3)
    check("burst drive settles", wait_until(
        lambda: manager.status()["config"].get("capturemode", {}).get("value") == "Burst",
        timeout=15.0))
    manager.request_trigger()
    check("burst 3 -> 3 files", wait_until(
        lambda: len([e for e in manager.get_events() if e["kind"] == "photo"]) >= photos_before + 3,
        timeout=45.0),
        f"{len([e for e in manager.get_events() if e['kind'] == 'photo']) - photos_before} frames")
    manager.set_capture_mode("single")
    wait_until(lambda: manager.status()["config"].get("capturemode", {}).get("value") == "Single Shot",
               timeout=15.0)

    # Named interval sequence -> <root>/<slug>/<sequence>/.
    result = manager.start_interval_sequence(interval_s=4.0, count=2,
                                             sequence_name="Z Validation")
    check("sequence armed (named)", result["status"] == "sequence-armed",
          str(result.get("warning", "")))
    check("sequence 2/2", wait_until(
        lambda: manager.status()["interval"]["state"] == "complete", timeout=45.0)
        and manager.status()["interval"]["shots_done"] == 2,
        str(manager.status()["interval"]))
    sequence_dir = os.path.join(root, slug, "z_validation")

    def sequence_file_count() -> int:
        if not os.path.isdir(sequence_dir):
            return 0
        return len([f for f in os.listdir(sequence_dir)
                    if f.lower().endswith((".nef", ".jpg"))])

    # The post-sequence drain window keeps collecting announcements for up
    # to 20s; wait on the FILES, not on the transient pending counter.
    check("sequence files in the named folder",
          wait_until(lambda: sequence_file_count() >= 2, timeout=40.0),
          f"{sequence_file_count()} files in {sequence_dir}")
    manager.set_sequence_name(None)

    # Restore + disconnect.
    if card_warning and original_target and original_target != "None":
        manager.set_config_value("capturetarget", original_target)
        wait_until(lambda: manager.status()["config"].get("capturetarget", {}).get("value") == original_target,
                   timeout=15.0)
    for name, value in original.items():
        if value and value != "None":
            manager.set_config_value(name, value)
            wait_until(lambda n=name, v=value: manager.status()["config"].get(n, {}).get("value") == v,
                       timeout=15.0)
    print("restored:", {k: manager.status()["config"].get(k, {}).get("value") for k in original},
          flush=True)
    status = manager.disconnect()
    check("disconnect clean", not status["connected"])

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
        for event in manager.get_events()[:25]:
            print(f"  [{event['kind']}/{event['reason']}] {event['note']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
