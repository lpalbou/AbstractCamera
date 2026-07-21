"""AbstractCore capability plugin: the `camera` capability.

Loaded by AbstractCore through the `abstractcore.capabilities_plugins`
entry point group (declared in pyproject.toml). The module deliberately
imports NOTHING from abstractcore — `register(registry)` receives the
registry and duck-types it, so abstractcamera keeps zero hard dependency
on the host (exactly the abstractvision/abstractvoice plugin posture).

Import weight matters here: this module is imported by EVERY AbstractCore
process that touches capability discovery (`status()`, catalog routes),
camera user or not. The camera stack (OpenCV/numpy via camera_manager) is
therefore imported lazily — registration and catalog listing never pull it
(adversarial finding 2026-07-19: the first draft cost every core process
the cv2 import).

Registration uses the registry's GENERIC `register_backend(capability=
"camera", ...)` path, which works on today's AbstractCore unchanged; when
core ships a typed `register_camera_backend(...)` helper (asked at commons
c3135, ruled c3168), the plugin picks it up automatically.

Capability surface convention (core ruling c3168): operations RAISE
`CameraControlError` with user-actionable text on failure (the core
facade/server convention, like vision/voice) and return JSON-SAFE dicts —
capture content rides as base64 (`data_b64`) or artifact refs, never raw
bytes; `preview_frame` returns bytes as the RETURN VALUE (the vision-plugin
convention for image payloads), the one documented exception.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from abstractcamera.errors import CameraControlError

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps imports lazy
    from abstractcamera.service import CameraService

BACKEND_ID = "abstractcamera:hub"

# JSON-safe content ceiling for include_bytes: base64 expands x1.33 and the
# result dict may land in runtime ledgers (ADR 0012). A bounded video or a
# RAW burst can exceed RAM/ledger sanity — steer big payloads to the
# artifact store, which exists for exactly this.
MAX_INLINE_CONTENT_BYTES = 64 * 1024 * 1024

_INSTALL_HINT = 'pip install "abstractcamera"'
_CONFIG_HINT = (
    "No configuration is required for the default device (first tethered PTP "
    "body, else the built-in webcam). Optional: camera_capture_root / "
    "ABSTRACTCAMERA_CAPTURE_ROOT sets where captures land (default ~/Pictures/"
    "<device>/) — the camera hub is process-shared, so the LAST configured "
    "root wins for newly opened cameras across every consumer in the process. "
    "ABSTRACTCAMERA_FAKE=1 replaces all transports with the built-in simulator "
    "(development without hardware); extras: abstractcamera[gphoto2] for "
    "tethered bodies, [clips] for MP4 recording, [dwarf] for DWARF telescopes."
)

# The operation catalog served through core's generic
# `list_operations("camera")` route — one record per capability op.
_OPERATIONS: tuple[Dict[str, Any], ...] = (
    {"operation_id": "list_cameras", "task": "camera_discovery", "output_modalities": ["json"]},
    {"operation_id": "open", "task": "camera_control", "output_modalities": ["json"]},
    {"operation_id": "close", "task": "camera_control", "output_modalities": ["json"]},
    {"operation_id": "status", "task": "camera_control", "output_modalities": ["json"]},
    {
        "operation_id": "capture_photo",
        "task": "photo_capture",
        "output_modalities": ["image"],
        "artifact_output": True,
    },
    {
        "operation_id": "capture_video",
        "task": "video_capture",
        "output_modalities": ["video"],
        "artifact_output": True,
        "required_parameters": ["duration_s"],
    },
    {"operation_id": "stop_recording", "task": "video_capture", "output_modalities": ["json"]},
    {"operation_id": "preview_frame", "task": "camera_preview", "output_modalities": ["image"]},
    {
        "operation_id": "start_detection",
        "task": "motion_detection",
        "output_modalities": ["json"],
        "parameter_schema": {
            "target": {"type": "string", "enum": ["motion", "lightning", "meteor"]},
            "action": {"type": "string", "enum": ["photo", "video", "monitor"]},
            "sensitivity": {"type": "number", "minimum": 0, "maximum": 100},
        },
    },
    {"operation_id": "stop_detection", "task": "motion_detection", "output_modalities": ["json"]},
    {"operation_id": "detection_events", "task": "motion_detection", "output_modalities": ["json"]},
)

# Transport catalog facts (notes only — availability is DERIVED from
# discovery.resolve_drivers(), never re-implemented here).
_TRANSPORT_NOTES: Dict[str, Dict[str, Any]] = {
    "ptp": {
        "local": True,
        "remote": False,
        "note": "tethered bodies over libgphoto2 (Nikon Z, Sony Alpha, generic PTP) — abstractcamera[gphoto2]",
    },
    "webcam": {
        "local": True,
        "remote": False,
        "note": "built-in/USB webcams (macOS AVFoundation)",
    },
    "dwarf": {
        "local": False,
        "remote": True,
        "note": "DWARF smart telescopes over Wi-Fi (ABSTRACTCAMERA_DWARF_HOSTS) — abstractcamera[dwarf]",
    },
    "fake": {
        "local": True,
        "remote": False,
        "note": "built-in simulator (ABSTRACTCAMERA_FAKE=1)",
    },
}


def _owner_cfg(owner: Any, key: str) -> Optional[str]:
    try:
        cfg = getattr(owner, "config", None)
        if isinstance(cfg, dict):
            value = cfg.get(key)
            if value is not None:
                text = str(value).strip()
                return text or None
    except Exception:
        return None
    return None


def _content_type_for(path: str) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    ext = Path(str(path)).suffix.lower()
    # Camera formats mimetypes does not know everywhere.
    raw_types = {
        ".nef": "image/x-nikon-nef",
        ".arw": "image/x-sony-arw",
        ".dng": "image/x-adobe-dng",
        ".mjpeg": "video/x-motion-jpeg",
    }
    return raw_types.get(ext, "application/octet-stream")


def _artifact_ref_from_store(
    artifact_store: Any,
    content: bytes,
    *,
    content_type: str,
    filename: Optional[str],
    run_id: Optional[str],
    tags: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Store bytes through a duck-typed artifact store (AbstractRuntime's
    `store(content, content_type=..., run_id=..., tags=...)` shape, mirrored
    by core's ArtifactStoreLike) and return a `{"$artifact": ...}` ref.
    Store failures are translated to the capability's error type — a raw
    OSError/TypeError from a mis-shaped store must not leak through the
    raise-CameraControlError contract (adversarial finding)."""
    store_fn = getattr(artifact_store, "store", None)
    if not callable(store_fn):
        raise CameraControlError(
            "artifact_store does not expose store(content, ...) — pass an "
            "AbstractRuntime-shaped artifact store or omit it to receive file paths."
        )
    try:
        result = store_fn(content, content_type=content_type, run_id=run_id, tags=dict(tags or {}) or None)
    except Exception as exc:
        raise CameraControlError(
            f"artifact_store.store(...) failed: {exc} — the capture file is still on "
            "disk; retry with a working store or omit artifact_store."
        ) from exc
    artifact_id = getattr(result, "artifact_id", None) or getattr(result, "id", None)
    if artifact_id is None and isinstance(result, (str, int)):
        artifact_id = result
    if artifact_id is None and isinstance(result, dict):
        artifact_id = result.get("artifact_id") or result.get("id") or result.get("$artifact")
    if artifact_id is None or (isinstance(artifact_id, str) and not artifact_id.strip()):
        raise CameraControlError(
            "artifact_store.store(...) returned no artifact id — cannot build an artifact ref."
        )
    ref: Dict[str, Any] = {"$artifact": str(artifact_id), "content_type": content_type}
    if filename:
        ref["filename"] = str(filename)
    ref["size_bytes"] = len(content)
    return ref


class _AbstractCameraCapability:
    """AbstractCore `camera` capability backed by abstractcamera.

    One instance per capability registry, but every instance shares the
    process-wide CameraService/CameraHub: a camera is a process-wide
    hardware resource — two hubs claiming one body wedges the transport,
    and an agent that opened a camera through a tool must be able to
    capture through `core.camera` in the same session.
    """

    backend_id = BACKEND_ID

    def __init__(self, owner: Any, *, service: Optional["CameraService"] = None):
        self._owner = owner
        self._service_override = service
        self._service_lock = threading.Lock()
        self._service_resolved: Optional["CameraService"] = None

    @property
    def _service(self) -> "CameraService":
        """The camera stack, imported on FIRST USE (never at plugin load —
        catalog routes and registry discovery must stay OpenCV-free)."""
        with self._service_lock:
            if self._service_resolved is None:
                if self._service_override is not None:
                    self._service_resolved = self._service_override
                else:
                    from abstractcamera.service import get_shared_service

                    self._service_resolved = get_shared_service()
                capture_root = _owner_cfg(self._owner, "camera_capture_root") or (
                    os.environ.get("ABSTRACTCAMERA_CAPTURE_ROOT") or None
                )
                if capture_root:
                    self._service_resolved.configure(capture_root=capture_root)
            return self._service_resolved

    # -- result adaptation --------------------------------------------------

    @staticmethod
    def _unwrap(result: Dict[str, Any]) -> Dict[str, Any]:
        """Service dicts carry success/error; the capability convention is
        raise-on-failure with the same honest text."""
        if not result.get("success", False):
            raise CameraControlError(str(result.get("error") or "camera operation failed"))
        out = dict(result)
        out.pop("success", None)
        return out

    # -- discovery / lifecycle ------------------------------------------------

    def list_cameras(self) -> List[Dict[str, Any]]:
        return list(self._unwrap(self._service.list_cameras()).get("cameras") or [])

    def open(self, camera_id: Optional[str] = None, **_: Any) -> Dict[str, Any]:
        return self._unwrap(self._service.open(camera_id))

    def close(self, camera: Optional[str] = None, **_: Any) -> Dict[str, Any]:
        return self._unwrap(self._service.close(camera))

    def close_all(self) -> Dict[str, Any]:
        return self._unwrap(self._service.close_all())

    def status(self, camera: Optional[str] = None) -> Dict[str, Any]:
        return self._unwrap(self._service.status(camera))

    # -- capture ------------------------------------------------------------

    def capture_photo(
        self,
        camera: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        artifact_store: Any = None,
        run_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        include_bytes: bool = False,  # attaches base64 (`data_b64`), JSON-safe by contract
        **_: Any,
    ) -> Dict[str, Any]:
        out = self._unwrap(self._service.capture_photo(camera, timeout_s=timeout_s))
        return self._attach_capture_payload(
            out,
            artifact_store=artifact_store,
            run_id=run_id,
            tags=tags,
            include_bytes=include_bytes,
        )

    def capture_video(
        self,
        duration_s: float,
        camera: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        artifact_store: Any = None,
        run_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        include_bytes: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        out = self._unwrap(self._service.capture_video(duration_s, camera, timeout_s=timeout_s))
        return self._attach_capture_payload(
            out,
            artifact_store=artifact_store,
            run_id=run_id,
            tags=tags,
            include_bytes=include_bytes,
        )

    def stop_recording(
        self,
        camera: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        artifact_store: Any = None,
        run_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        include_bytes: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        # Same payload lane as capture_video (adversarial P2 2026-07-21:
        # the asymmetry gave store-holding hosts bare-path media for
        # detection-started recordings while capture_video got refs).
        out = self._unwrap(self._service.stop_recording(camera, timeout_s=timeout_s))
        return self._attach_capture_payload(
            out,
            artifact_store=artifact_store,
            run_id=run_id,
            tags=tags,
            include_bytes=include_bytes,
        )

    def preview_frame(
        self,
        camera: Optional[str] = None,
        *,
        artifact_store: Any = None,
        run_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        **_: Any,
    ) -> Any:
        """Latest live-view JPEG: raw bytes by default (the vision-plugin
        convention for image payloads — the documented exception to the
        dict rule), or an artifact ref when a store is provided."""
        result, jpeg = self._service.preview_frame(camera)
        self._unwrap(result)
        assert jpeg is not None  # _unwrap raised otherwise
        if artifact_store is None:
            return jpeg
        return _artifact_ref_from_store(
            artifact_store,
            jpeg,
            content_type="image/jpeg",
            filename=f"preview_{int(time.time())}.jpg",
            run_id=run_id,
            tags=tags,
        )

    def _attach_capture_payload(
        self,
        out: Dict[str, Any],
        *,
        artifact_store: Any,
        run_id: Optional[str],
        tags: Optional[Dict[str, str]],
        include_bytes: bool,
    ) -> Dict[str, Any]:
        """Optionally attach content / an artifact ref for a capture that
        landed as a local file. Content rides as BASE64 (`data_b64`), never
        raw bytes — every capability op returns JSON-safe dicts (core
        ruling c3168: results may land in runtime ledgers). Captures with
        no local file (on-device save policy, deferred downloads, movies
        the body kept on its card) have no bytes to attach — the error
        names the actual state instead of discarding it."""
        path = out.get("path")
        wants_payload = bool(artifact_store is not None or include_bytes)
        if not wants_payload:
            return out
        if not path:
            if out.get("on_device"):
                raise CameraControlError(
                    "The capture was saved on the camera's own storage (save policy is "
                    "device-only) — there are no local bytes to attach. Use sync_store()/"
                    "`abstractcamera download` to fetch device media, or switch the save "
                    "policy to local downloads."
                )
            if out.get("deferred"):
                raise CameraControlError(
                    "The capture happened but its download is deferred while detection "
                    "auto-fire is armed — poll detection_events for the 'photo' event "
                    "(it carries the path), or stop_detection to flush now."
                )
            if out.get("delivered") is False:
                raise CameraControlError(
                    "The recording completed but the camera never announced the movie "
                    "file (it likely resides on the camera's own storage) — use "
                    "sync_store()/`abstractcamera download` to fetch it."
                )
            raise CameraControlError("The capture reported no local file path — cannot attach bytes.")
        try:
            content = Path(path).read_bytes()
        except OSError as exc:
            raise CameraControlError(f"Captured file could not be read back: {exc}") from exc
        content_type = _content_type_for(path)
        out["content_type"] = content_type
        if include_bytes:
            if len(content) > MAX_INLINE_CONTENT_BYTES:
                raise CameraControlError(
                    f"The capture is {len(content) / 1e6:.0f} MB — too large to inline as "
                    f"base64 (cap {MAX_INLINE_CONTENT_BYTES / 1e6:.0f} MB). The file is at "
                    f"{path}; pass artifact_store= for large payloads."
                )
            out["data_b64"] = base64.b64encode(content).decode("ascii")
        if artifact_store is not None:
            out["artifact"] = _artifact_ref_from_store(
                artifact_store,
                content,
                content_type=content_type,
                filename=Path(path).name,
                run_id=run_id,
                tags=tags,
            )
            # Sight-lane override (commons 3969/4089 ruling): with a store
            # present, `media` carries the $artifact ref — the durable
            # currency runtime's llm_client already resolves — instead of
            # the bare path the service degraded to. One ref spelling
            # ($artifact), dict-shaped items; the `artifact` key stays for
            # existing consumers.
            out["media"] = [dict(out["artifact"])]
        return out

    # -- detection ------------------------------------------------------------

    def start_detection(
        self,
        camera: Optional[str] = None,
        *,
        target: str = "motion",
        action: str = "photo",
        sensitivity: Optional[float] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        return self._unwrap(
            self._service.start_detection(camera, target=target, action=action, sensitivity=sensitivity)
        )

    def stop_detection(self, camera: Optional[str] = None, **_: Any) -> Dict[str, Any]:
        return self._unwrap(self._service.stop_detection(camera))

    def detection_events(
        self,
        camera: Optional[str] = None,
        *,
        since_id: int = 0,
        kinds: Optional[List[str]] = None,
        limit: int = 100,
        include_thumbnails: bool = False,
    ) -> Dict[str, Any]:
        return self._unwrap(
            self._service.get_events(
                camera,
                since_id=since_id,
                kinds=kinds,
                limit=limit,
                include_thumbnails=include_thumbnails,
            )
        )

    # -- catalog routes (core's generic capability discovery) -----------------

    def available_providers(self, *, task: Optional[str] = None) -> Dict[str, Any]:
        """Transport availability, no device claiming: safe for catalog/UI
        routes. 'Providers' here are camera transports, derived from the
        SAME driver resolution connect() uses — the catalog can never claim
        a transport the connect path would refuse.

        The `providers` list carries FULL RECORDS (not bare ids): core's
        `_normalize_provider_records` reads this key first and would
        otherwise discard the installed/local/remote facts (adversarial
        finding: the documented route reported uninstalled transports as
        available)."""
        from abstractcamera import discovery

        try:
            active_ids = [driver.driver_id for driver in discovery.resolve_drivers()]
        except Exception:
            active_ids = []
        details: Dict[str, Dict[str, Any]] = {}
        for transport_id, facts in _TRANSPORT_NOTES.items():
            details[transport_id] = {
                "id": transport_id,
                "provider_id": transport_id,
                "installed": transport_id in active_ids,
                "status": "available" if transport_id in active_ids else "not_installed",
                **facts,
            }
        for transport_id in active_ids:  # a driver this catalog predates
            details.setdefault(
                transport_id,
                {
                    "id": transport_id,
                    "provider_id": transport_id,
                    "installed": True,
                    "status": "available",
                    "local": True,
                    "remote": False,
                    "note": None,
                },
            )
        return {
            "task": task,
            "providers": list(details.values()),
            "available_providers": [tid for tid, d in details.items() if d["installed"]],
            "details": details,
        }

    def list_available_providers(self, *, task: Optional[str] = None) -> Dict[str, Any]:
        return self.available_providers(task=task)

    def list_models(self, *, task: Optional[str] = None, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        """'Models' for a camera capability are the DEVICES discovery sees.
        Non-invasive (no claiming), so catalog routes can call it freely."""
        entries = self.list_cameras()
        out: List[Dict[str, Any]] = []
        for entry in entries:
            transport = str(entry.get("transport") or "")
            if provider and transport != str(provider):
                continue
            out.append(
                {
                    "model_id": str(entry.get("id") or ""),
                    "provider_id": transport,
                    "tasks": ["photo_capture", "video_capture", "motion_detection"],
                    "local": transport != "dwarf",
                    "remote": transport == "dwarf",
                    "status": "connected" if entry.get("connected") else "available",
                    "raw_metadata": {
                        k: entry.get(k)
                        for k in ("name", "name_confidence", "default", "device_uid", "active")
                        if k in entry
                    },
                }
            )
        return out

    def list_operations(self, *, task: Optional[str] = None) -> List[Dict[str, Any]]:
        ops = [dict(op) for op in _OPERATIONS]
        if task:
            ops = [op for op in ops if op.get("task") == task]
        return ops


def register(registry: Any) -> None:
    """Register abstractcamera as an AbstractCore capability plugin.

    Loaded via the `abstractcore.capabilities_plugins` entry point group.
    Prefers the typed `register_camera_backend(...)` helper when the host
    core ships one; today's generic `register_backend(capability=...)`
    path is fully supported by core's registry. Import-light by contract:
    nothing here touches the camera stack.
    """

    def _factory(owner: Any) -> _AbstractCameraCapability:
        return _AbstractCameraCapability(owner)

    description = (
        "AbstractCamera capability plugin: pilot real cameras — tethered PTP "
        "bodies (Nikon Z, Sony Alpha), built-in webcams, DWARF smart "
        "telescopes. Turn cameras on/off, take photos and videos, run "
        "motion/lightning/meteor detection with auto-capture."
    )

    typed = getattr(registry, "register_camera_backend", None)
    if callable(typed):
        typed(
            backend_id=BACKEND_ID,
            factory=_factory,
            priority=0,
            description=description,
            install_hint=_INSTALL_HINT,
            config_hint=_CONFIG_HINT,
        )
        return
    registry.register_backend(
        capability="camera",
        backend_id=BACKEND_ID,
        factory=_factory,
        priority=0,
        description=description,
        install_hint=_INSTALL_HINT,
        config_hint=_CONFIG_HINT,
    )
