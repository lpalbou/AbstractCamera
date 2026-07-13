"""Hardware validation: webcam identity after ADR 0009 (uniqueID-addressed
native AVFoundation capture) — written against the LIVE inversion of
2026-07-12, where positional ffmpeg/OpenCV mapping streamed the iPhone
under the MacBook's label.

Identity is now correct BY CONSTRUCTION (the AVCaptureDeviceInput is built
from the exact device object whose localizedName we display — no index
space exists). What remains automatable is CROSS-WIRING detection inside
our own plumbing: commands addressed to label X must land on stream X.
Oracle: set DIFFERENT resolutions on the two labeled streams and assert
each stream's SOF-confirmed size follows its OWN command. (The torch would
have bound labels to physical scenes, but macOS reports hasTorch=False for
Continuity devices — measured 2026-07-12. The final physical-scene check
is the human one in the app.)

Run:  python3 scripts/validate_webcam_identity.py
"""

import sys
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
    from abstractcamera import CameraHub, list_cameras

    entries = [c for c in list_cameras() if c["transport"] == "webcam"]
    for entry in entries:
        print(f"  {entry['id'][:36]}... | {entry['name']} | kind={entry['kind']} "
              f"| confidence={entry['name_confidence']}", flush=True)
    check("ids are uniqueIDs (no positional ids left)",
          all(not e["id"].split(":", 1)[1].isdigit() for e in entries))
    check("names are reported (same object as capture)",
          all(e["name_confidence"] == "reported" for e in entries))

    builtin = next((e for e in entries if e["kind"] == "built_in"), None)
    iphone = next((e for e in entries if e["kind"] == "continuity"), None)
    check("built-in camera classified", builtin is not None,
          builtin["name"] if builtin else "absent")
    check("continuity iPhone classified", iphone is not None,
          iphone["name"] if iphone else "absent")
    if not (builtin and iphone):
        print("Cannot run the inversion oracle without both devices.")
        return 1

    hub = CameraHub()
    import tempfile

    hub.configure_managers(capture_root=tempfile.mkdtemp(prefix="webcam_identity_"))

    # Connect BOTH cameras through their labeled entries, simultaneously.
    builtin_status = hub.connect(camera_id=builtin["id"])
    iphone_status = hub.connect(camera_id=iphone["id"])
    check("both webcams connect concurrently",
          builtin_status["connected"] and iphone_status["connected"],
          f"{builtin_status['device_uid']} + {iphone_status['device_uid']}")
    builtin_manager = hub.manager_for(builtin_status["device_uid"])
    iphone_manager = hub.manager_for(iphone_status["device_uid"])
    check("frames flow on both",
          wait_until(lambda: builtin_manager.get_latest_frame()[1] >= 5, timeout=15.0)
          and wait_until(lambda: iphone_manager.get_latest_frame()[1] >= 5, timeout=15.0))
    check("device_serial is the uniqueID",
          (builtin_manager.status()["device_serial"] or "").startswith(
              builtin["id"].split(":", 1)[1][:8]))

    # ---- THE ORACLE: per-stream command divergence ------------------------------
    # Two different resolutions, one per labeled stream; each SOF-confirmed
    # preview size must follow its OWN command (crossed wiring anywhere in
    # session/hub plumbing would flip them — exactly tonight's symptom).
    builtin_choices = builtin_manager.status()["config"]["imagesize"]["choices"]
    iphone_choices = iphone_manager.status()["config"]["imagesize"]["choices"]
    print(f"  choices: MacBook={builtin_choices} iPhone={iphone_choices}", flush=True)
    builtin_target = next((c for c in builtin_choices if c == "640x480"), builtin_choices[-1])
    iphone_target = next((c for c in iphone_choices if c == "1280x720"), iphone_choices[-1])
    check("distinct per-stream targets available", builtin_target != iphone_target,
          f"{builtin_target} vs {iphone_target}")

    builtin_manager.set_config_value("imagesize", builtin_target)
    iphone_manager.set_config_value("imagesize", iphone_target)
    expected_builtin = [int(v) for v in builtin_target.split("x")]
    expected_iphone = [int(v) for v in iphone_target.split("x")]
    check("ORACLE: labeled-MacBook stream follows ITS command",
          wait_until(lambda: builtin_manager.status()["preview_size"] == expected_builtin,
                     timeout=10.0),
          f"preview_size={builtin_manager.status()['preview_size']} wanted {expected_builtin}")
    check("ORACLE: labeled-iPhone stream follows ITS command",
          wait_until(lambda: iphone_manager.status()["preview_size"] == expected_iphone,
                     timeout=10.0),
          f"preview_size={iphone_manager.status()['preview_size']} wanted {expected_iphone}")
    # Restore native.
    builtin_manager.set_config_value("imagesize", builtin_choices[0])
    iphone_manager.set_config_value("imagesize", iphone_choices[0])
    time.sleep(1.0)

    hub.disconnect_all()
    check("clean teardown", len(hub.statuses()) == 0)

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
