"""DWARF Wi-Fi DEBUGGING script: exhaustive, offline-friendly diagnostics.

Built for the situation where the Mac must JOIN THE DWARF'S OWN Wi-Fi (AP
mode) and therefore has no internet: run this, let it log everything, then
bring the log file back for analysis. Every step is fenced — one failure
never stops the run — and every network exchange is logged (decoded when
the packet is known, hex-dumped when not).

    python3 scripts/debug_dwarf.py                 # full diagnostics, no photo
    python3 scripts/debug_dwarf.py --host 192.168.1.57
    python3 scripts/debug_dwarf.py --photo         # + one capture/download test
    python3 scripts/debug_dwarf.py --listen 20     # longer notification capture
    python3 scripts/debug_dwarf.py --no-lock       # purely passive (no master lock)

Outputs (bring these back):
    untracked/dwarf_debug_<timestamp>.log          # the full annotated log
    untracked/dwarf_params_<timestamp>.json        # the device's raw parameter tables
    untracked/dwarf_album_<timestamp>.json         # the raw album listing

The script never moves the mount.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import socket
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abstractcamera.drivers import dwarf_wire as wire3  # noqa: E402
from abstractcamera.drivers.dwarf_transport import (AP_MODE_HOST,  # noqa: E402
                                                    DwarfTransport,
                                                    configured_hosts)

UNTRACKED = os.path.join(os.path.dirname(__file__), "..", "untracked")

# Known packets for pretty decoding in the listen phase.
KNOWN_PACKETS = {
    wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE: ("host/slave mode", wire3.RES_NOTIFY_HOST_SLAVE_MODE),
    wire3.CMD_NOTIFY_ELE: ("battery %", wire3.COM_RES_WITH_INT),
    wire3.CMD_NOTIFY_CHARGE: ("charge state", wire3.COM_RES_WITH_INT),
    wire3.CMD_NOTIFY_SDCARD_INFO: ("sd card", wire3.RES_NOTIFY_SDCARD_INFO),
    wire3.CMD_NOTIFY_TEMPERATURE: ("temperature", wire3.RES_NOTIFY_TEMPERATURE),
    wire3.CMD_NOTIFY_TELE_RECORD_TIME: ("record time", wire3.RES_NOTIFY_RECORD_TIME),
    wire3.CMD_NOTIFY_STATE_ASTRO_GOTO: ("goto state", wire3.RES_NOTIFY_STATE),
    wire3.CMD_NOTIFY_STATE_ASTRO_CALIBRATION: ("calibration state", wire3.RES_NOTIFY_STATE),
    wire3.CMD_NOTIFY_STATE_ASTRO_TRACKING: ("tracking state", wire3.RES_NOTIFY_STATE_ASTRO_TRACKING),
    wire3.CMD_NOTIFY_TELE_SET_PARAM: ("tele param echo", wire3.RES_NOTIFY_PARAM),
    wire3.CMD_NOTIFY_POWER_OFF: ("power off", None),
    wire3.CMD_NOTIFY_ALBUM_UPDATE: ("album update", None),
}

PORTS = {"ws-control": 9900, "http-api": 8082, "http-media": 8092,
         "rtsp": 554, "ftp": 21}


class Log:
    """Console + file tee with timestamps; sections make the file scannable."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")
        self.failures: list[str] = []
        self.passes: list[str] = []

    def line(self, text: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        for chunk in (text or " ").splitlines() or [" "]:
            entry = f"[{stamp}] {chunk}"
            print(entry, flush=True)
            self._fh.write(entry + "\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        self.line("")
        self.line("=" * 72)
        self.line(f"== {title}")
        self.line("=" * 72)

    def result(self, label: str, ok: bool, detail: str = "") -> None:
        (self.passes if ok else self.failures).append(label)
        self.line(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))

    def exception(self, label: str) -> None:
        self.failures.append(label)
        self.line(f"[FAIL] {label} — exception:")
        for chunk in traceback.format_exc().splitlines():
            self.line(f"    {chunk}")

    def close(self) -> None:
        self._fh.close()


def run_cmd(log: Log, argv: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:
        log.line(f"    ({' '.join(argv)} failed: {exc})")
        return ""


# --------------------------------------------------------------------------- steps

def step_environment(log: Log) -> None:
    log.section("1. environment")
    log.line(f"time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    log.line(f"python: {sys.version.split()[0]}  platform: {platform.platform()}")
    try:
        import abstractcamera

        log.line(f"abstractcamera: {abstractcamera.__version__} "
                 f"({os.path.dirname(abstractcamera.__file__)})")
    except Exception:
        log.exception("import abstractcamera")
    try:
        import websocket

        log.line(f"websocket-client: {getattr(websocket, '__version__', '?')}")
    except ImportError:
        log.result("websocket-client installed", False,
                   "pip install 'abstractcamera[dwarf]' — ws steps will fail")
    try:
        import cv2

        log.line(f"opencv: {cv2.__version__}")
    except ImportError:
        log.result("opencv installed", False, "RTSP steps will fail")

    log.line("")
    log.line("--- interfaces (ifconfig, inet lines) ---")
    for line in run_cmd(log, ["ifconfig", "-a"]).splitlines():
        if line and (not line.startswith(("\t", " ")) or "inet " in line or "status" in line):
            log.line(f"    {line.strip()}")
    log.line("--- default route ---")
    for line in run_cmd(log, ["route", "-n", "get", "default"]).splitlines():
        if any(key in line for key in ("gateway", "interface")):
            log.line(f"    {line.strip()}")
    log.line("--- current Wi-Fi ---")
    ssid = run_cmd(log, ["networksetup", "-getairportnetwork", "en0"]).strip()
    log.line(f"    {ssid or '(unknown)'}")


def own_ip_and_gateway(log: Log) -> tuple[str | None, str | None]:
    own_ip = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("203.0.113.1", 9))  # no packets flow; kernel picks the route
        own_ip = sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    gateway = None
    for line in run_cmd(log, ["route", "-n", "get", "default"]).splitlines():
        if "gateway:" in line:
            gateway = line.split("gateway:")[-1].strip()
    return own_ip, gateway


def step_candidates(log: Log, explicit: str | None, sweep: bool) -> list[str]:
    log.section("2. candidate hosts")
    own_ip, gateway = own_ip_and_gateway(log)
    log.line(f"own ip: {own_ip}   gateway: {gateway}")
    candidates: list[str] = []

    def add(host: str | None, why: str) -> None:
        if host and host not in candidates and host != own_ip:
            candidates.append(host)
            log.line(f"candidate: {host}  ({why})")

    add(explicit, "--host")
    for host in configured_hosts():
        add(host, "ABSTRACTCAMERA_DWARF_HOSTS")
    add(AP_MODE_HOST, "AP-mode fixed IP")
    # In AP mode the DWARF IS the gateway — always worth probing.
    add(gateway, "default gateway")

    reachable = [h for h in candidates if DwarfTransport.probe(h, timeout_s=1.2)]
    for host in candidates:
        log.line(f"probe {host}:9900 -> {'OPEN' if host in reachable else 'closed/unreachable'}")

    if not reachable and (sweep or True):  # sweep is the whole point offline
        prefix = ".".join((own_ip or "").split(".")[:3])
        if prefix:
            log.line(f"sweeping {prefix}.0/24 for port 9900 ...")
            hosts = [f"{prefix}.{i}" for i in range(1, 255)]
            with concurrent.futures.ThreadPoolExecutor(96) as pool:
                hits = [h for h, ok in zip(hosts, pool.map(
                    lambda h: DwarfTransport.probe(h, timeout_s=0.7), hosts)) if ok]
            log.line(f"sweep hits: {hits or 'none'}")
            reachable += [h for h in hits if h not in reachable]
        else:
            log.line("no own ip — cannot derive a sweep prefix")
    log.result("a DWARF control port is reachable", bool(reachable),
               ", ".join(reachable) if reachable else
               "not on this network — check the Wi-Fi you are joined to")
    return reachable


def step_port_matrix(log: Log, host: str) -> None:
    log.section(f"3. port matrix — {host}")
    for name, port in PORTS.items():
        started = time.perf_counter()
        ok = DwarfTransport.probe(host, port=port, timeout_s=1.5)
        ms = (time.perf_counter() - started) * 1000
        log.line(f"{name:12s} {host}:{port:<5d} -> {'OPEN' if ok else 'closed'}  ({ms:.0f}ms)")


def step_http(log: Log, host: str, stamp: str) -> None:
    log.section(f"4. HTTP diagnostics — {host}")
    transport = DwarfTransport(host)
    try:
        payload = transport.default_params_config()
        sidecar = os.path.join(UNTRACKED, f"dwarf_params_{stamp}.json")
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        log.result("getDefaultParamsConfig", True, f"raw JSON -> {os.path.basename(sidecar)}")
        data = payload.get("data") if isinstance(payload, dict) else None
        log.line(f"top-level keys: {sorted(payload) if isinstance(payload, dict) else type(payload)}")
        if isinstance(data, dict):
            log.line(f"data keys: {sorted(data)}")
            for camera in data.get("cameras") or []:
                if isinstance(camera, dict):
                    names = [str(p.get('name')) for p in camera.get('supportParams') or []
                             if isinstance(p, dict)]
                    log.line(f"camera '{camera.get('name')}': params {names}")
        from abstractcamera.drivers.dwarf_session import DwarfSession

        exposure, gain = DwarfSession._parse_params_config(payload)
        log.result("exposure table parsed", bool(exposure),
                   f"{len(exposure)} entries, first: {exposure[:3]}")
        log.result("gain table parsed", bool(gain),
                   f"{len(gain)} entries, first: {gain[:3]}")
    except Exception:
        log.exception("getDefaultParamsConfig")

    try:
        entries = transport.album_media_infos(page_size=16)
        sidecar = os.path.join(UNTRACKED, f"dwarf_album_{stamp}.json")
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
        log.result("album listing", True,
                   f"{len(entries)} entries -> {os.path.basename(sidecar)}")
        for entry in entries[:5]:
            log.line(f"    {entry.get('fileName')}  ({entry.get('fileSize', '?')}, "
                     f"type {entry.get('mediaType')}, t={entry.get('modificationTime')})")
    except Exception:
        log.exception("album listing")


class _LoggingTransport(DwarfTransport):
    """DwarfTransport with every dispatched packet mirrored into the log."""

    log: Log | None = None

    def _dispatch(self, packet: dict) -> None:
        log = type(self).log
        if log is not None:
            cmd = int(packet.get("cmd", 0))
            name, spec = KNOWN_PACKETS.get(cmd, (None, None))
            ptype = {0: "REQ", 1: "RESP", 2: "NOTIFY", 3: "NOTIFY-RESP"}.get(
                int(packet.get("type", 0)), "?")
            data = packet.get("data", b"") or b""
            if name and spec is not None:
                try:
                    decoded = wire3.decode_message(spec, data)
                except ValueError:
                    decoded = f"<undecodable: {data[:32].hex()}>"
                log.line(f"ws<{ptype}> module={packet.get('module_id')} cmd={cmd} "
                         f"({name}): {decoded}")
            elif name:
                log.line(f"ws<{ptype}> module={packet.get('module_id')} cmd={cmd} ({name})")
            else:
                body = wire3.decode_message(wire3.COM_RESPONSE, data) if data else {}
                log.line(f"ws<{ptype}> module={packet.get('module_id')} cmd={cmd} "
                         f"len={len(data)} hex={data[:48].hex()}"
                         + (f" (as ComResponse: {body})" if data else ""))
        super()._dispatch(packet)


def step_websocket(log: Log, host: str, listen_s: float, try_lock: bool):
    log.section(f"5. WebSocket control plane — {host}")
    _LoggingTransport.log = log
    transport = _LoggingTransport(host)
    log.line(f"client_id: {transport.client_id}")
    try:
        transport.connect()
        log.result("ws connect", True, f"ws://{host}:9900/")
    except Exception:
        log.exception("ws connect")
        return None

    try:
        response = transport.send_request(
            wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_GET_SYSTEM_WORKING_STATE,
            timeout_s=8.0)
        decoded = wire3.decode_message(wire3.COM_RESPONSE, response.get("data", b""))
        log.result("GET_SYSTEM_WORKING_STATE", True, f"code={decoded.get('code')}")
    except Exception:
        log.exception("GET_SYSTEM_WORKING_STATE (some firmware only notifies — not fatal)")

    if try_lock:
        try:
            payload = wire3.encode_message(wire3.REQ_SET_MASTER_LOCK, {"lock": True})
            response = transport.send_request(
                wire3.MODULE_SYSTEM, wire3.CMD_SYSTEM_SET_MASTERLOCK, payload,
                alias_keys=((wire3.MODULE_NOTIFY, wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE),
                            (wire3.MODULE_SYSTEM, wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE)),
                timeout_s=8.0)
            if int(response.get("_cmd", 0)) == wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE:
                decoded = wire3.decode_message(wire3.RES_NOTIFY_HOST_SLAVE_MODE,
                                               response.get("data", b""))
                log.result("master lock", bool(decoded.get("lock")),
                           f"host/slave answer: {decoded} "
                           "(lock=False => the DWARFLAB app holds control)")
            else:
                decoded = wire3.decode_message(wire3.COM_RESPONSE, response.get("data", b""))
                log.result("master lock", decoded.get("code", -1) == 0,
                           f"ComResponse code={decoded.get('code')}")
        except Exception:
            log.exception("master lock")
    else:
        log.line("master lock SKIPPED (--no-lock)")

    log.line(f"listening for notifications for {listen_s:.0f}s "
             "(battery/temperature/host-slave usually chatter here)...")
    deadline = time.time() + listen_s
    count = 0
    while time.time() < deadline:
        try:
            transport.notifications.popleft()
            count += 1
        except IndexError:
            time.sleep(0.1)
    log.line(f"listen window over — {count} notification(s) consumed "
             "(every packet was logged above as it arrived)")
    return transport


def step_rtsp(log: Log, host: str) -> None:
    log.section(f"6. RTSP live view — {host}")
    try:
        import cv2  # noqa: F401
    except ImportError:
        log.result("rtsp", False, "opencv missing")
        return
    from abstractcamera.drivers.dwarf_transport import RtspFrameSource

    for channel, label in ((0, "tele"), (1, "wide")):
        url = f"rtsp://{host}:554/ch{channel}/stream0"
        source = RtspFrameSource(url)
        try:
            started = time.perf_counter()
            source.open()
            open_ms = (time.perf_counter() - started) * 1000
            frames, first = 0, None
            read_started = time.perf_counter()
            while frames < 30 and time.perf_counter() - read_started < 6.0:
                frame = source.read()
                if first is None:
                    first = frame.shape
                frames += 1
            elapsed = time.perf_counter() - read_started
            fps = frames / elapsed if elapsed > 0 else 0.0
            log.result(f"rtsp {label} ({url})", frames > 0,
                       f"open {open_ms:.0f}ms, {frames} frames, "
                       f"{fps:.1f} fps, shape {first}")
        except Exception:
            log.exception(f"rtsp {label} ({url})")
        finally:
            source.close()


def step_full_stack(log: Log, host: str, photo: bool) -> None:
    log.section(f"7. full stack (CameraManager) — {host}")
    try:
        import tempfile

        from abstractcamera import CameraManager
        from abstractcamera.drivers.dwarf_driver import DwarfDriver

        manager = CameraManager(driver=DwarfDriver(hosts=[host]))
        capture_dir = tempfile.mkdtemp(prefix="dwarf_debug_")
        manager.set_capture_dir(capture_dir)
        log.line(f"capture dir: {capture_dir}")
        status = manager.connect(camera_id=f"dwarf:{host}")
        log.result("manager.connect", status["connected"],
                   f"model={status['model']} family={status['family']}")
        log.line(f"actions: {status['actions']}")
        log.line(f"capabilities.mount: {(status['capabilities'] or {}).get('mount')}")

        deadline = time.time() + 12.0
        while time.time() < deadline and manager.get_latest_frame()[1] < 10:
            time.sleep(0.2)
        _, seq = manager.get_latest_frame()
        log.result("live frames through the manager", seq >= 10,
                   f"{seq} frames, fps={manager.status()['fps']}")

        config = manager.status()["config"]
        for widget in ("shutterspeed", "gain", "ircut", "battery", "temperature"):
            entry = config.get(widget)
            log.line(f"widget {widget}: "
                     + (f"value={entry.get('value')!r} choices={len(entry.get('choices', []))}"
                        if entry else "ABSENT"))

        if photo:
            log.line("firing one photo (shutter -> album -> download)...")
            manager.request_trigger()
            deadline = time.time() + 60.0
            photo_event = None
            while time.time() < deadline and photo_event is None:
                photo_event = next((e for e in manager.get_events()
                                    if e["kind"] == "photo"), None)
                time.sleep(0.5)
            log.result("photo downloaded", photo_event is not None,
                       (photo_event or {}).get("path", "no photo event within 60s"))
        else:
            log.line("photo test SKIPPED (opt in with --photo)")

        log.line("--- catch log ---")
        for event in manager.get_events():
            log.line(f"    [{event['kind']}] {(event['note'] or event.get('path') or '')[:140]}")
        manager.disconnect()
        log.result("manager.disconnect", True)
    except Exception:
        log.exception("full stack")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="DWARF IP (otherwise: env, AP IP, gateway, sweep)")
    parser.add_argument("--sweep", action="store_true",
                        help="force the /24 sweep even when a candidate answers")
    parser.add_argument("--photo", action="store_true",
                        help="also fire ONE photo and download it")
    parser.add_argument("--listen", type=float, default=8.0,
                        help="notification listen window in seconds (default 8)")
    parser.add_argument("--no-lock", action="store_true",
                        help="skip the master-lock attempt (purely passive)")
    args = parser.parse_args()

    os.makedirs(UNTRACKED, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log = Log(os.path.join(UNTRACKED, f"dwarf_debug_{stamp}.log"))
    log.line("DWARF Wi-Fi debug run — bring this file back for analysis:")
    log.line(f"    {os.path.abspath(log.path)}")

    try:
        step_environment(log)
        reachable = step_candidates(log, args.host, args.sweep)
        if reachable:
            host = reachable[0]
            step_port_matrix(log, host)
            step_http(log, host, stamp)
            transport = step_websocket(log, host, args.listen, not args.no_lock)
            if transport is not None:
                transport.close()
            step_rtsp(log, host)
            step_full_stack(log, host, args.photo)
    except KeyboardInterrupt:
        log.line("interrupted by user")

    log.section("summary")
    for label in log.passes:
        log.line(f"  PASS  {label}")
    for label in log.failures:
        log.line(f"  FAIL  {label}")
    log.line("")
    log.line(f"log file: {os.path.abspath(log.path)}")
    log.line("(plus dwarf_params_*.json / dwarf_album_*.json sidecars when HTTP worked)")
    failures = list(log.failures)
    log.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
