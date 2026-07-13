"""Hardware validation, part 2: detection on live view, focus actions
(AF drive + MF nudges through the canonical action names), and the rolling
buffer — the remaining Capture-tab features on the real A7R IV."""

import os
import sys
import tempfile
import time


from abstractcamera import CameraManager as CameraController

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
    controller = CameraController()
    capture_dir = tempfile.mkdtemp(prefix="abstractcamera_hw_validation_")
    controller.set_capture_dir(capture_dir)

    status = controller.connect()
    check("connect", status["connected"], status["model"])

    # ---- detection modes on the live feed ----
    controller.set_detection_mode("monitor", target="lightning", sensitivity=50)
    time.sleep(4.0)
    status = controller.status()
    check("detection active on live view", status["detection_active"],
          f"paused_reason={status['detection_paused_reason']}")
    fps_detection = status["fps"]
    check("fps under detection > 12", fps_detection > 12, f"{fps_detection} fps")
    controller.set_detection_mode("monitor", target="meteor", sensitivity=60)
    time.sleep(3.0)
    check("meteor detector runs on Sony feed",
          controller.status()["detection_active"],
          f"fps={controller.status()['fps']}")
    controller.set_detection_mode("off")

    # ---- focus actions through canonical names ----
    original_focusmode = str(controller.status()["config"].get("focusmode", {}).get("value"))
    controller.request_action("autofocusdrive")
    time.sleep(2.5)
    af_errors = [e for e in controller.get_events()
                 if e["kind"] == "error" and "autofocusdrive" in (e["note"] or "")]
    check("AF drive action accepted", not af_errors,
          af_errors[0]["note"] if af_errors else "")

    # MF nudge outside Manual: must produce the FRIENDLY refusal, not [-2].
    if original_focusmode != "Manual":
        controller.request_action("manualfocusdrive", "100")
        friendly = wait_until(lambda: any(
            e["kind"] == "error" and "Focus Mode = Manual" in (e["note"] or "")
            for e in controller.get_events()), timeout=6.0)
        check("MF nudge outside Manual refused with friendly copy", friendly)

    controller.set_config_value("focusmode", "Manual")
    check("focusmode -> Manual", wait_until(
        lambda: controller.status()["config"].get("focusmode", {}).get("value") == "Manual",
        timeout=10.0))
    position_before = controller.status()["config"].get("focalposition", {}).get("value")
    # Watermark: only errors NEWER than this belong to the Manual-mode nudges
    # (the earlier outside-Manual refusal is intentional and must not count).
    events_watermark = max((e["id"] for e in controller.get_events()), default=0)
    for delta in ("300", "300", "300"):
        controller.request_action("manualfocusdrive", delta)
    time.sleep(3.0)
    controller.refresh_config_from_camera()
    position_after = controller.status()["config"].get("focalposition", {}).get("value")
    mf_errors = [e for e in controller.get_events(since_id=events_watermark)
                 if e["kind"] == "error" and "manualfocusdrive" in (e["note"] or "")]
    check("MF nudges in Manual accepted", not mf_errors,
          f"focalposition {position_before} -> {position_after}"
          + (f" | {mf_errors[0]['note']}" if mf_errors else ""))
    for delta in ("-300", "-300", "-300"):
        controller.request_action("manualfocusdrive", delta)
    time.sleep(2.0)

    # ---- rolling buffer on the Sony feed ----
    controller.set_rolling_buffer(True, seconds=5.0)
    check("rolling buffer fills", wait_until(
        lambda: controller.status()["rolling"]["buffered_s"] >= 3.0, timeout=15.0),
        f"buffered={controller.status()['rolling']['buffered_s']}s")
    try:
        clip = controller.save_rolling_clip()
        check("rolling clip saved", os.path.exists(clip["path"]),
              f"{clip['duration_s']}s @ {clip['fps']}fps")
    except Exception as exc:
        check("rolling clip saved", False, str(exc))
    controller.set_rolling_buffer(False)

    # ---- restore ----
    controller.set_config_value("focusmode", original_focusmode)
    wait_until(lambda: controller.status()["config"].get("focusmode", {}).get("value") == original_focusmode,
               timeout=10.0)
    print("focusmode restored:", controller.status()["config"].get("focusmode", {}).get("value"), flush=True)
    status = controller.disconnect()
    check("disconnect clean", not status["connected"])

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
        print("\ncatch log:")
        for event in controller.get_events()[:25]:
            print(f"  [{event['kind']}/{event['reason']}] {event['note']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
