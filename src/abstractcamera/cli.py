"""Minimal CLI for manual testing: `abstractcamera list` / `abstractcamera preview`.

The preview command deliberately opens the selected camera (this is what
triggers the one-time macOS camera-permission prompt for a new host process)
and reports measured live-view fps — the quickest hardware sanity check.
"""

from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="abstractcamera",
                                     description="Camera control abstractions (Abstract ecosystem)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="enumerate cameras across transports (non-invasive)")
    preview = sub.add_parser("preview", help="connect and measure live view for a few seconds")
    preview.add_argument("--camera-id", default=None, help="id from `abstractcamera list`")
    preview.add_argument("--seconds", type=float, default=5.0)
    download = sub.add_parser(
        "download",
        help="download ALL media from a device to this computer",
        description=(
            "Downloads every media file a device holds (photos, FITS "
            "sessions, videos, panoramas) into ~/Pictures/<device>/ — "
            "re-runs skip files already present. Sources: a USB-mounted "
            "device card (auto-detected under /Volumes, or --source PATH) "
            "or a DWARF album over Wi-Fi (--host IP). With --delete, device "
            "files whose local copy VERIFIED are removed — protected device "
            "state (the astronomy dark library, system files) always stays."))
    download.add_argument("--source", default=None,
                          help="card mount point (default: auto-detect under /Volumes)")
    download.add_argument("--host", default=None,
                          help="DWARF IP for Wi-Fi album download instead of USB")
    download.add_argument("--dest", default=None,
                          help="destination folder (default: ~/Pictures/<device>)")
    download.add_argument("--delete", action="store_true",
                          help="after verified copy, delete the media from the device")
    download.add_argument("--delete-calibrations", action="store_true",
                          help="with --delete: also delete the device's calibration "
                               "library (darks/bias/flats — the device needs these "
                               "for stacking; re-shooting them costs a session)")
    download.add_argument("--dry-run", action="store_true",
                          help="show what would happen; touch nothing")
    watch = sub.add_parser(
        "watch",
        help="sentinel: arm detection and forward camera events to an AbstractGateway mailbox",
        description=(
            "Opens a camera, optionally arms detection, and forwards catch-log "
            "events (detections with metrics, capture file paths, errors) as "
            "DURABLE gateway events — parked workflows/entities declaring the "
            "mailbox wake on movement instead of polling. Cursors persist "
            "across restarts; delivery is deduplicated by the gateway command "
            "store. Ctrl-C stops, disarms, and releases the camera."))
    watch.add_argument("--camera-id", default=None, help="id from `abstractcamera list` (default device when omitted)")
    watch.add_argument("--gateway", default="http://127.0.0.1:8080", help="gateway base URL")
    watch.add_argument("--mailbox", default="camera", help="event name + global-scope session id (the rendezvous)")
    watch.add_argument("--token", default=None, help="bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN)")
    watch.add_argument("--detect", default="motion", choices=["motion", "lightning", "meteor", "none"],
                       help="detection target to arm (none = forward events only)")
    watch.add_argument("--action", default="photo", choices=["photo", "video", "monitor"],
                       help="what detection does on a hit (monitor = log only)")
    watch.add_argument("--sensitivity", type=float, default=None, help="0-100 (default 50)")
    watch.add_argument("--kinds", default=None,
                       help="comma-separated event kinds to forward "
                            "(default: detection,photo,photo-pending,clip,error)")
    watch.add_argument("--poll-interval", type=float, default=1.0, help="seconds between event polls")
    watch.add_argument("--state-file", default=None,
                       help="cursor persistence path (default: ~/.abstractcamera/"
                            "gateway_bridge_<mailbox>.json; concurrent sentinels on ONE "
                            "mailbox need distinct paths)")
    args = parser.parse_args(argv)

    if args.command == "list":
        from abstractcamera import find_card_volumes, list_cameras

        entries = list_cameras()
        cards = find_card_volumes()
        if entries:
            print("cameras (control — connect/preview):")
            for entry in entries:
                marker = "*" if entry.get("default") else " "
                confidence = "" if entry.get("name_confidence") == "reported" else "  (name is best-effort)"
                print(f"{marker} {entry['id']:<18} {entry['name']}{confidence}")
        else:
            print("No cameras found (install abstractcamera[gphoto2] for tethered bodies).")
        if cards:
            print("\nmedia sources (files — `abstractcamera download`):")
            for root, layout in cards:
                print(f"  {root}  ({layout.slug} card)   "
                      f"-> download --source \"{root}\"")
        return 0 if (entries or cards) else 1

    if args.command == "download":
        from abstractcamera.media_sync import run_cli

        return run_cli(args)

    if args.command == "watch":
        if args.detect == "none":
            args.detect = None
        from abstractcamera.gateway_bridge import run_watch_cli

        return run_watch_cli(args)

    if args.command == "preview":
        from abstractcamera import CameraManager

        manager = CameraManager()
        status = manager.connect(camera_id=args.camera_id)
        print(f"connected: {status['model']} (family {status['family']})")
        deadline = time.time() + max(1.0, args.seconds)
        frames = 0
        last_seq = 0
        while time.time() < deadline:
            _frame, seq = manager.get_latest_frame()
            if seq != last_seq:
                frames += seq - last_seq
                last_seq = seq
            time.sleep(0.05)
        print(f"live view: {manager.status()['fps']} fps "
              f"({frames} frames observed), preview_size={manager.status()['preview_size']}")
        manager.disconnect()
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
