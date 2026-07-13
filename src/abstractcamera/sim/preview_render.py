"""Simulated live-view frame rendering (split from the gphoto2 simulator).

A real encoded JPEG (night-sky-ish gradient + noise) so cv2.imdecode, the
detection path, and <img> all accept the frames. The frame SIZE follows the
liveviewsize widget (resolution-switch regression coverage needs the size to
actually change), and scripted streaks / motion blobs / gain steps draw as a
function of wall time for detection gates. The scenario getter is injected by
the simulator module so this file carries no mutable state of its own.
"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np

PREVIEW_SIZES = {"QVGA": (320, 212), "VGA": (640, 424), "XGA": (1024, 680)}


def render_preview_jpeg(size_name: str, cfg, epoch: float) -> bytes:
    width, height = PREVIEW_SIZES.get(size_name, PREVIEW_SIZES["VGA"])
    rng = np.random.default_rng(int(time.time() * 50) % (2**31))
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    yy = np.linspace(18, 6, height, dtype=np.float32)[:, None]
    frame[:, :, 0] = yy.astype(np.uint8)
    frame[:, :, 1] = (yy * 0.8).astype(np.uint8)
    frame[:, :, 2] = (yy * 0.7).astype(np.uint8)
    noise = rng.normal(0, 3, (height, width)).astype(np.int16)
    frame[:, :, 1] = np.clip(frame[:, :, 1].astype(np.int16) + noise, 0, 255).astype(np.uint8)

    now = time.time() - epoch
    scale = width / 640.0

    for streak in cfg("inject_streaks"):
        dt = now - float(streak["t0"])
        if not (0.0 <= dt <= float(streak["duration_s"])):
            continue
        angle = math.radians(float(streak.get("angle_deg", 30.0)))
        distance = float(streak.get("speed_px_s", 200.0)) * dt * scale
        x = float(streak.get("x0", 100)) * scale + distance * math.cos(angle)
        y = float(streak.get("y0", 100)) * scale + distance * math.sin(angle)
        # ~2 frames of motion blur + afterglow, 1px wide: a real meteor
        # streak is long and THIN (elongation is the detector's core gate).
        tail = float(streak.get("speed_px_s", 200.0)) * 0.066 * scale
        x2 = x - tail * math.cos(angle)
        y2 = y - tail * math.sin(angle)
        cv2.line(frame, (int(x2), int(y2)), (int(x), int(y)),
                 tuple([int(streak.get("brightness", 220))] * 3), 1)

    for blob in cfg("inject_motion_blobs"):
        dt = now - float(blob["t0"])
        if not (0.0 <= dt <= float(blob["duration_s"])):
            continue
        x = int(float(blob.get("x", 200)) * scale)
        y = int(float(blob.get("y", 200)) * scale)
        w = int(float(blob.get("w", 60)) * scale)
        h = int(float(blob.get("h", 40)) * scale)
        # Drift so it reads as motion, not a static bright patch.
        x += int(20 * scale * dt)
        cv2.rectangle(frame, (x, y), (x + w, y + h),
                      tuple([int(blob.get("brightness", 140))] * 3), -1)

    gain_step = cfg("gain_step")
    if gain_step:
        dt = now - float(gain_step["t0"])
        if 0.0 <= dt <= float(gain_step["duration_s"]):
            frame = np.clip(frame.astype(np.float32) * float(gain_step.get("factor", 1.8)), 0, 255).astype(np.uint8)

    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return encoded.tobytes() if ok else b""
