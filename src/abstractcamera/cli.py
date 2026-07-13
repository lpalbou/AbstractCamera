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
    args = parser.parse_args(argv)

    if args.command == "list":
        from abstractcamera import list_cameras

        entries = list_cameras()
        if not entries:
            print("No cameras found (install abstractcamera[gphoto2] for tethered bodies).")
            return 1
        for entry in entries:
            marker = "*" if entry.get("default") else " "
            confidence = "" if entry.get("name_confidence") == "reported" else "  (name is best-effort)"
            print(f"{marker} {entry['id']:<18} {entry['name']}{confidence}")
        return 0

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
