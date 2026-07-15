"""Hardware validation: a REAL DWARF smart telescope through the shipped
stack (discovery -> CameraManager -> DwarfAdapter -> live view, dials,
capture+album download, telemetry) plus OPT-IN mount motion.

SAFETY: nothing moves unless explicitly requested. Default run is
read-only + one photo. Motion flags:
  --nudge            tiny joystick nudge (1s at 1 deg/s) + stop
  --goto RA,DEC[,L]  astro GOTO in degrees (needs prior calibration)
  --goto-solar NAME  solar-system GOTO (e.g. moon)
  --calibrate        astro calibration (the mount WILL slew around the sky)

Run:  python3 scripts/validate_dwarf.py [--host IP] [--nudge] ...
Discovery order: --host, ABSTRACTCAMERA_DWARF_HOSTS, AP-mode 192.168.88.1,
then an ACTIVE sweep of the local /24 for port 9900 (deliberate: the
library never scans; this script is the scanning tool).
"""

import argparse
import concurrent.futures
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

PASS, FAIL, INFO = [], [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""),
          flush=True)


def info(label, detail=""):
    INFO.append(label)
    print(f"[INFO] {label}" + (f" — {detail}" if detail else ""), flush=True)


def wait_until(predicate, timeout=15.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def local_subnet_hosts() -> list[str]:
    """Every /24 peer of this machine's primary interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("203.0.113.1", 9))  # no packets flow; kernel picks the route
        own_ip = sock.getsockname()[0]
    except OSError:
        return []
    finally:
        sock.close()
    prefix = ".".join(own_ip.split(".")[:3])
    return [f"{prefix}.{i}" for i in range(1, 255)]


def discover(explicit_host: str | None) -> str | None:
    from abstractcamera.drivers.dwarf_transport import (AP_MODE_HOST, DwarfTransport,
                                                        configured_hosts)

    candidates: list[str] = []
    if explicit_host:
        candidates.append(explicit_host)
    candidates += configured_hosts()
    candidates.append(AP_MODE_HOST)
    for host in candidates:
        info("probing", f"{host}:9900")
        if DwarfTransport.probe(host, timeout_s=1.5):
            return host
    info("sweeping the local /24 for port 9900", "active scan, script-only behavior")
    hosts = local_subnet_hosts()
    with concurrent.futures.ThreadPoolExecutor(96) as pool:
        hits = [h for h, ok in zip(hosts, pool.map(
            lambda h: DwarfTransport.probe(h, timeout_s=0.7), hosts)) if ok]
    if hits:
        info("sweep found", ", ".join(hits))
        return hits[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="DWARF IP (skips discovery)")
    parser.add_argument("--nudge", action="store_true",
                        help="tiny joystick nudge + stop (the mount MOVES)")
    parser.add_argument("--goto", dest="goto_radec",
                        help="astro GOTO 'ra_deg,dec_deg[,label]' (the mount SLEWS)")
    parser.add_argument("--goto-solar", dest="goto_solar",
                        help="solar-system GOTO target name (the mount SLEWS)")
    parser.add_argument("--calibrate", action="store_true",
                        help="astro calibration (the mount SLEWS repeatedly)")
    args = parser.parse_args()

    from abstractcamera import CameraManager
    from abstractcamera.drivers.dwarf_driver import DwarfDriver

    host = discover(args.host)
    check("discovery", host is not None,
          f"DWARF at {host}" if host else
          "no DWARF found — put it in STA mode on this network (DWARFLAB app "
          "shows its IP) or join its own Wi-Fi and re-run")
    if host is None:
        return summary()

    manager = CameraManager(driver=DwarfDriver(hosts=[host]))
    capture_dir = tempfile.mkdtemp(prefix="abstractcamera_dwarf_validation_")
    manager.set_capture_dir(capture_dir)
    info("captures land in", capture_dir)

    # ---- 1. connect + identity + capabilities --------------------------------
    print("connecting...", flush=True)
    try:
        status = manager.connect(camera_id=f"dwarf:{host}")
    except Exception as exc:
        check("connect", False, str(exc))
        return summary()
    check("connect", status["connected"], f"model={status['model']}")
    check("family is dwarf", status["family"] == "dwarf")
    caps = status["capabilities"] or {}
    check("mount capability advertised", (caps.get("mount") or {}).get("goto"),
          str(caps.get("mount")))
    check("mount actions exposed",
          all(a in status["actions"] for a in ("gotoradec", "joystick", "stopgoto")))

    # ---- 2. live view ----------------------------------------------------------
    check("live frames flow", wait_until(lambda: manager.get_latest_frame()[1] >= 10),
          f"fps={manager.status()['fps']}")

    # ---- 3. device dials -------------------------------------------------------
    config = manager.status()["config"]
    exposure = config.get("shutterspeed") or {}
    check("exposure dial carries the device table", bool(exposure.get("choices")),
          f"{len(exposure.get('choices') or [])} values")
    gain = config.get("gain") or {}
    check("gain dial carries the device table", bool(gain.get("choices")),
          f"{len(gain.get('choices') or [])} values")
    if exposure.get("choices"):
        target = exposure["choices"][len(exposure["choices"]) // 2]
        manager.set_config_value("shutterspeed", target)
        check(f"exposure write ({target})", wait_until(
            lambda: manager.status()["config"].get("shutterspeed", {}).get("value") == target,
            timeout=10.0))
    battery = (manager.status()["config"].get("battery") or {}).get("value")
    info("battery", battery or "no notification yet")

    # ---- 4. capture -> album -> local download ---------------------------------
    manager.request_trigger()
    check("photo lands locally (album download)", wait_until(
        lambda: any(e["kind"] == "photo" and e.get("path")
                    for e in manager.get_events()), timeout=45.0),
        "shutter -> DWARF album -> Wi-Fi download")

    # ---- 5. mount (OPT-IN only) --------------------------------------------------
    if args.nudge:
        manager.request_action("joystick", "90,0.5,1")
        time.sleep(1.0)
        manager.request_action("joystickstop")
        check("joystick nudge + stop", wait_until(
            lambda: any("mount motion stopped" in (e["note"] or "")
                        for e in manager.get_events()), timeout=10.0))
    if args.calibrate:
        manager.request_action("calibrate")
        check("calibration reported", wait_until(
            lambda: any("calibration" in (e["note"] or "").lower()
                        for e in manager.get_events()), timeout=180.0),
            "watch the catch log for 'calibration success/failed'")
    if args.goto_radec:
        manager.request_action("gotoradec", args.goto_radec)
        check("goto acknowledged", wait_until(
            lambda: any("GOTO" in (e["note"] or "") for e in manager.get_events()),
            timeout=20.0))
        info("goto progress", "watch for 'GOTO success/failed' status events")
        time.sleep(20)
    if args.goto_solar:
        manager.request_action("gotosolar", args.goto_solar)
        check("solar goto acknowledged", wait_until(
            lambda: any("GOTO" in (e["note"] or "") for e in manager.get_events()),
            timeout=20.0))
        time.sleep(20)
    if not any((args.nudge, args.calibrate, args.goto_radec, args.goto_solar)):
        info("mount motion skipped", "opt in with --nudge / --goto / --calibrate")

    # ---- 6. recent device chatter ------------------------------------------------
    for event in manager.get_events()[-12:]:
        info(f"event[{event['kind']}]", (event["note"] or event.get("path") or "")[:100])

    manager.disconnect()
    check("disconnect clean", not manager.status()["connected"])
    return summary()


def summary() -> int:
    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
