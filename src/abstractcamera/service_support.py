"""Presentation + validation helpers for CameraService.

Split from service.py so the service file stays focused on the operation
state machine (locks, waits, guards) the adversarial reviews audit; these
helpers are pure functions with no camera state.
"""

from __future__ import annotations


def ok(payload: dict | None = None) -> dict:
    out = {"success": True}
    if payload:
        out.update(payload)
    return out


def fail(error: str, payload: dict | None = None) -> dict:
    out = {"success": False, "error": str(error)}
    if payload:
        out.update(payload)
    return out


def coerce_number(value, name: str, *, lo: float, hi: float, default: float | None = None):
    """(number, None) or (None, error_text). No-raise by contract: service
    operations return failure DICTS for bad input — a bare ValueError from
    request JSON would violate both integration surfaces' error contracts
    (adversarial finding 2026-07-19: float(timeout_s) raised through the
    no-raise dict contract, and in capture_video AFTER the hardware acted)."""
    if value is None and default is not None:
        return float(default), None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{name} must be a number, got {value!r}"
    if number != number:  # NaN
        return None, f"{name} must be a number, got NaN"
    return min(max(number, lo), hi), None


def coerce_int(value, name: str, *, lo: int, hi: int):
    """(int, None) or (None, error_text) — same no-raise contract."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, f"{name} must be an integer, got {value!r}"
    return min(max(number, lo), hi), None


def public_event(event: dict, *, include_thumbnail: bool = False) -> dict:
    """Catch-log event stripped for JSON payloads: thumbnails are base64
    data-URLs (tens of KB each) that would flood LLM contexts and HTTP
    responses; callers that want them opt in."""
    out = {k: v for k, v in event.items() if k != "thumbnail"}
    if include_thumbnail:
        out["thumbnail"] = event.get("thumbnail")
    return out


def public_status(status: dict) -> dict:
    """JSON-safe status subset: drop the full config-cache dump (large,
    widget-shaped, and irrelevant to capture callers) but keep every
    state field an agent needs to reason about the camera."""
    keep = (
        "available", "connected", "model", "family", "transport", "camera_id",
        "device_uid", "device_slug", "active", "capture_dir", "sequence_name",
        "download_locally", "liveview_running", "fps", "preview_size",
        "detection_mode", "detection_target", "detection_sensitivity",
        "detection_active", "detection_paused_reason", "downloads_pending",
        "capture_mode", "burst_count", "movie_recording", "interval",
        "rolling", "last_error", "event_count",
        # The event-log id-space epoch (wire contract 2026-07-21): agents
        # can learn a reconnect from status alone, not just get_events.
        "session",
    )
    out = {k: status.get(k) for k in keep if k in status}
    capabilities = status.get("capabilities")
    if isinstance(capabilities, dict):
        out["capabilities"] = {
            k: capabilities.get(k)
            for k in ("family", "display_name", "burst", "movie", "save_to", "mount", "notes")
            if k in capabilities
        }
    return out
