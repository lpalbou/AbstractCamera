"""Manager tuning constants shared across the mixin modules (Mod #3 of the
adjudicated design: these were module-level constants in the original
monolithic controller and are consumed from more than one mixin, so they get
one explicit home instead of NameError-prone per-module copies).

Every value is hardware-scarred or adversarially elected — see the consuming
code's comments for the measurement behind each number. Do not tune casually.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

FrameAnalyzer = Callable[[np.ndarray], dict]

CATCH_LOG_MAX_EVENTS = 200
DETECTION_BASELINE_WINDOW_FRAMES = 240
AUTO_FIRE_COOLDOWN_SECONDS = 2.5
EVENT_THUMBNAIL_MAX_EDGE = 160
# Detection thresholds mirror the validated video lightning detector.
DETECTION_DIM_RELATIVE_MEAN_RATIO = 3.0
DETECTION_DIM_MIN_MEAN = 1.2
DETECTION_MIN_P99_LIFT = 4.0
DETECTION_ARC_MIN_ELONGATION = 3.0
DETECTION_ARC_MIN_INTENSITY = 200.0
DETECTION_ARC_MAX_FILL = 0.40
DETECTION_EVENT_MERGE_SECONDS = 1.0

# One-shot ACTIONS (focus drives): executed once by the worker, NEVER cached,
# never in status().config, never replayed — caching a drive value and
# re-applying it on reconnect would physically move the lens. These are the
# CANONICAL wire names hosts speak; family adapters map them to the body's
# own widget names (Sony: autofocus/manualfocus).
ACTION_WIDGET_NAMES = ["autofocusdrive", "manualfocusdrive"]

# Watchdog: consecutive live-view failures before declaring the camera lost
# (USB pull). ~8 failures x 0.25s retry sleep = ~2s to an honest disconnect.
LIVEVIEW_FAILURE_DISCONNECT_THRESHOLD = 8
