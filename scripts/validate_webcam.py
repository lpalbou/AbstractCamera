"""Hardware validation: the REAL MacBook camera through the packaged
CameraManager — list, connect, fps under detection, resolution dial, shoot,
burst, movie, rolling clip, interval sequence, double-open measurement.

Run:  python3 scripts/validate_webcam.py
(The first run from a new host process triggers the one-time macOS camera
permission prompt — grant it and re-run if the connect check fails.)
"""

import os
import sys
import tempfile
import time

PASS, FAIL, INFO = [], [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)


def info(label, detail):
    INFO.append((label, detail))
    print(f"[INFO] {label} — {detail}", flush=True)


def wait_until(predicate, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main():
    from abstractcamera import CameraManager, list_cameras

    # ---- 1. discovery -----------------------------------------------------------
    cameras = list_cameras()
    webcams = [c for c in cameras if c["transport"] == "webcam"]
    check("discovery lists webcams", bool(webcams),
          "; ".join(f"{c['id']}={c['name']}" for c in webcams))
    built_in = [c for c in webcams if "macbook" in c["name"].lower()]
    check("built-in camera named", bool(built_in),
          built_in[0]["name"] if built_in else "no MacBook-named entry")
    iphone = [c for c in webcams if "continuity" in c["name"].lower()]
    info("Continuity iPhone visibility", iphone[0]["name"] if iphone else "not present")
    target = built_in[0] if built_in else webcams[0]

    # ---- 2. connect -------------------------------------------------------------
    manager = CameraManager()
    capture_dir = tempfile.mkdtemp(prefix="abstractcamera_webcam_validation_")
    manager.set_capture_dir(capture_dir)
    status = manager.connect(camera_id=target["id"])
    check("connect", status["connected"], f"model={status['model']}")
    check("family=webcam", status["family"] == "webcam")
    caps = status["capabilities"] or {}
    check("capabilities honest", caps.get("exposure_controls") is False
          and caps.get("focus", {}).get("supported") is False
          and caps.get("movie", {}).get("can_confirm") is True,
          str({k: caps.get(k) for k in ("exposure_controls",)}))

    # ---- 3. live view + detection budget ----------------------------------------
    check("frames flow", wait_until(lambda: manager.get_latest_frame()[1] >= 10, timeout=10.0))
    wait_until(lambda: manager.status()["preview_size"] is not None, timeout=6.0)
    size = manager.status()["preview_size"]
    check("preview size reported", size is not None and size[0] >= 1280, str(size))
    manager.set_detection_mode("monitor", target="motion", sensitivity=70)
    time.sleep(4.0)
    fps = manager.status()["fps"]
    check("fps >= 20 at native res with detection on", fps >= 20.0, f"{fps} fps")
    detections = [e for e in manager.get_events() if e["kind"] == "detection"]
    info("motion events during idle scene", f"{len(detections)} (wave at the camera to test live)")
    manager.set_detection_mode("off")

    # ---- 4. resolution dial through the ledger ----------------------------------
    choices = manager.status()["config"].get("imagesize", {}).get("choices", [])
    info("honored resolutions", str(choices))
    if "1280x720" in choices:
        manager.set_config_value("imagesize", "1280x720")
        check("resolution write settles", wait_until(
            lambda: manager.status()["config"].get("imagesize", {}).get("value") == "1280x720",
            timeout=8.0))
        check("SOF truth signal confirms 720p", wait_until(
            lambda: manager.status()["preview_size"] == [1280, 720], timeout=8.0),
            str(manager.status()["preview_size"]))
        manager.set_config_value("imagesize", choices[0])
        wait_until(lambda: manager.status()["config"].get("imagesize", {}).get("value") == choices[0],
                   timeout=8.0)

    # ---- 5. shoot / burst ---------------------------------------------------------
    manager.request_trigger()
    got_photo = wait_until(
        lambda: any(e["kind"] == "photo" and (e["path"] or "").endswith(".jpg")
                    for e in manager.get_events()), timeout=10.0)
    photo = next((e for e in manager.get_events() if e["kind"] == "photo"), None)
    check("shoot -> JPEG in capture dir",
          got_photo and photo and os.path.exists(photo["path"]),
          photo["path"] if photo else "none")

    photos_before = len([e for e in manager.get_events() if e["kind"] == "photo"])
    manager.set_capture_mode("burst", burst_count=5)
    manager.request_trigger()
    check("burst 5 -> 5 files", wait_until(
        lambda: len([e for e in manager.get_events() if e["kind"] == "photo"]) >= photos_before + 5,
        timeout=15.0),
        f"{len([e for e in manager.get_events() if e['kind'] == 'photo']) - photos_before} frames")
    manager.set_capture_mode("single")

    # ---- 6. movie ------------------------------------------------------------------
    manager.set_capture_mode("video")
    manager.request_trigger()
    check("movie starts confirmed", wait_until(
        lambda: manager.status()["movie_recording"], timeout=8.0))
    time.sleep(3.0)
    manager.request_trigger()
    check("movie stops", wait_until(lambda: not manager.status()["movie_recording"], timeout=8.0))
    got_movie = wait_until(
        lambda: any(e["kind"] == "photo" and (e["path"] or "").endswith(".mp4")
                    for e in manager.get_events()), timeout=10.0)
    movie = next((e for e in manager.get_events()
                  if e["kind"] == "photo" and (e["path"] or "").endswith(".mp4")), None)
    frame_count = 0
    if movie and os.path.exists(movie["path"]):
        try:
            import av

            with av.open(movie["path"]) as container:
                frame_count = sum(1 for _ in container.decode(video=0))
        except Exception as exc:
            info("movie decode", f"failed: {exc}")
    check("movie MP4 on disk with >= 45 frames (3s)", got_movie and frame_count >= 45,
          f"{movie['path'] if movie else 'none'} ({frame_count} frames)")
    manager.set_capture_mode("single")

    # ---- 7. rolling clip ------------------------------------------------------------
    manager.set_rolling_buffer(True, seconds=4.0)
    check("rolling buffer fills", wait_until(
        lambda: manager.status()["rolling"]["buffered_s"] >= 2.5, timeout=15.0),
        f"{manager.status()['rolling']['buffered_s']}s")
    try:
        clip = manager.save_rolling_clip()
        check("rolling clip saved", os.path.exists(clip["path"]),
              f"{clip['duration_s']}s @ {clip['fps']}fps")
    except Exception as exc:
        check("rolling clip saved", False, str(exc))
    manager.set_rolling_buffer(False)

    # ---- 8. interval sequence --------------------------------------------------------
    result = manager.start_interval_sequence(interval_s=2.0, count=5)
    check("sequence armed (nominal exposure path)", result["status"] == "sequence-armed")
    check("sequence 5/5", wait_until(
        lambda: manager.status()["interval"]["state"] == "complete", timeout=30.0)
        and manager.status()["interval"]["shots_done"] == 5,
        str(manager.status()["interval"]))

    # ---- 9. double-open measurement ---------------------------------------------------
    import cv2

    second = cv2.VideoCapture(0)
    ok_second = second.isOpened() and second.read()[0]
    second.release()
    info("double-open (second in-process VideoCapture while connected)",
         "second open delivered frames (AVFoundation shares)" if ok_second
         else "second open refused/frameless (exclusive)")
    fps_after = manager.status()["fps"]
    check("live view survived the double-open probe", fps_after > 10, f"{fps_after} fps")

    # ---- 10. disconnect ---------------------------------------------------------------
    status = manager.disconnect()
    check("disconnect clean", not status["connected"])

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        print("FAILED:", FAIL)
        print("\ncatch log (recent):")
        for event in manager.get_events()[:25]:
            print(f"  [{event['kind']}/{event['reason']}] {event['note']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
