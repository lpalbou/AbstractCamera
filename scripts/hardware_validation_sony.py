"""Hardware validation: the REAL CameraController + SonyAlphaAdapter driving
the REAL Sony A7R IV. Exercises the exact code paths BlackPixel ships:
connect (adapter selection + defaults), capabilities, dial writes through
the ledger, single trigger + ARW download, live view, burst press-and-hold,
and a short interval sequence. Restores camera settings afterwards.

Run:  python3 scripts/hardware_validation_sony.py
"""

import os
import sys
import tempfile
import time


from abstractcamera import CameraManager as CameraController

PASS = []
FAIL = []


def check(label: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)


def wait_until(predicate, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def wait_config(controller, name, value, timeout=12.0):
    return wait_until(
        lambda: str(controller.status()["config"].get(name, {}).get("value")) == str(value),
        timeout=timeout)


def main():
    controller = CameraController()
    capture_dir = tempfile.mkdtemp(prefix="abstractcamera_hw_validation_")
    controller.set_capture_dir(capture_dir)

    # ---- 1. connect + identity ------------------------------------------------
    print("connecting...", flush=True)
    status = controller.connect()
    check("connect", status["connected"], f"model={status['model']}")
    check("family=sony_alpha", status["family"] == "sony_alpha", str(status["family"]))
    caps = status["capabilities"] or {}
    check("capabilities: duration burst", caps.get("burst", {}).get("mode") == "duration")
    check("capabilities: movie unconfirmable", caps.get("movie", {}).get("can_confirm") is False)

    # ---- 2. connect defaults ---------------------------------------------------
    check("prioritymode=Application settled",
          wait_config(controller, "prioritymode", "Application", timeout=15.0),
          str(controller.status()["config"].get("prioritymode", {}).get("value")))
    target = str(controller.status()["config"].get("capturetarget", {}).get("value"))
    check("capturetarget untouched (card+sdram)", target == "card+sdram", target)

    # ---- 3. live view ----------------------------------------------------------
    check("live view frames flow",
          wait_until(lambda: controller.get_latest_frame()[1] >= 10, timeout=10.0))
    wait_until(lambda: controller.status()["preview_size"] is not None, timeout=8.0)
    status = controller.status()
    check("preview size reported", status["preview_size"] == [1024, 680],
          str(status["preview_size"]))
    time.sleep(3.0)
    fps = controller.status()["fps"]
    check("live view fps > 15", fps > 15, f"{fps} fps")

    # ---- 4. dial writes through the ledger ------------------------------------
    original = {}
    for name, test_value in (("iso", "1600"), ("shutterspeed", "1/100")):
        original[name] = str(controller.status()["config"].get(name, {}).get("value"))
        controller.set_config_value(name, test_value)
        check(f"write {name}={test_value} settled", wait_config(controller, name, test_value),
              f"was {original[name]}")

    # Kelvin WB path
    original["whitebalance"] = str(controller.status()["config"].get("whitebalance", {}).get("value"))
    original["colortemperature"] = str(controller.status()["config"].get("colortemperature", {}).get("value"))
    controller.set_config_value("whitebalance", "Choose Color Temperature")
    check("write whitebalance=Choose Color Temperature settled",
          wait_config(controller, "whitebalance", "Choose Color Temperature"))
    controller.set_config_value("colortemperature", "5500")
    check("write colortemperature=5500 settled",
          wait_until(lambda: float(controller.status()["config"]
                                   .get("colortemperature", {}).get("value", 0)) == 5500.0,
                     timeout=12.0))

    # ---- 5. focus mode + single trigger + download ------------------------------
    original["focusmode"] = str(controller.status()["config"].get("focusmode", {}).get("value"))
    controller.set_config_value("focusmode", "Manual")
    check("write focusmode=Manual settled", wait_config(controller, "focusmode", "Manual"))

    events_before = {e["id"] for e in controller.get_events()}
    controller.request_trigger()
    got_photo = wait_until(
        lambda: any(e["kind"] == "photo" and e["id"] not in events_before
                    for e in controller.get_events()),
        timeout=25.0)
    check("single trigger -> photo downloaded", got_photo)
    if got_photo:
        photo = next(e for e in controller.get_events()
                     if e["kind"] == "photo" and e["id"] not in events_before)
        exists = photo["path"] and os.path.exists(photo["path"])
        size = os.path.getsize(photo["path"]) if exists else 0
        check("ARW file on disk", bool(exists) and size > 10_000_000,
              f"{photo['note']} ({size/1e6:.1f}MB)")

    # ---- 6. burst press-and-hold ------------------------------------------------
    controller.set_capture_mode("burst", burst_hold_s=1.0, burst_speed="Hi")
    drive_ok = wait_config(controller, "capturemode", "Continuous Shooting Hi", timeout=15.0)
    check("burst drive settled (Continuous Shooting Hi)", drive_ok,
          str(controller.status()["config"].get("capturemode", {}).get("value")))
    if drive_ok:
        photos_before = len([e for e in controller.get_events() if e["kind"] == "photo"])
        controller.request_trigger()
        burst_ok = wait_until(
            lambda: len([e for e in controller.get_events() if e["kind"] == "photo"]) >= photos_before + 3,
            timeout=45.0)
        n = len([e for e in controller.get_events() if e["kind"] == "photo"]) - photos_before
        check("burst hold -> >=3 photos downloaded", burst_ok, f"{n} frames")
    controller.set_capture_mode("single")
    check("drive restored to Single Shot",
          wait_config(controller, "capturemode", "Single Shot", timeout=15.0))

    # ---- 7. short interval sequence ---------------------------------------------
    controller.set_config_value("shutterspeed", "1/60")
    wait_config(controller, "shutterspeed", "1/60")
    result = controller.start_interval_sequence(interval_s=4.0, count=3)
    check("sequence armed", result["status"] == "sequence-armed",
          str(result.get("warning", "")))
    done = wait_until(
        lambda: controller.status()["interval"]["state"] in ("complete", "aborted"),
        timeout=60.0)
    interval = controller.status()["interval"]
    check("sequence complete 3/3",
          done and interval["state"] == "complete" and interval["shots_done"] == 3,
          f"state={interval['state']} done={interval['shots_done']} "
          f"failed={interval['shots_failed']} missed={interval['shots_missed']}")
    # let the downloads drain
    wait_until(lambda: controller.status()["downloads_pending"] == 0, timeout=30.0)

    # ---- 8. restore + disconnect ---------------------------------------------
    for name in ("iso", "shutterspeed", "whitebalance", "colortemperature", "focusmode"):
        value = original.get(name)
        if value and value != "None":
            controller.set_config_value(name, value)
            wait_config(controller, name, value, timeout=10.0)
    print("restored:", {k: str(controller.status()['config'].get(k, {}).get('value'))
                        for k in original}, flush=True)
    status = controller.disconnect()
    check("disconnect clean", not status["connected"])

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
    # dump the catch log tail for the record
    print("\ncatch log (most recent first):")
    for event in controller.get_events()[:40]:
        print(f"  [{event['kind']}/{event['reason']}] {event['note']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
