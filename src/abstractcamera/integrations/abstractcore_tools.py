"""Camera tool set for LLM tool calling (AbstractCore integration).

AbstractCore's builtin tool inventory is deliberately core-only and its
global tool registration is deprecated — the durable contract is EXPLICIT
tools (core ruling, commons c3168: tools are a security surface; entry-point
auto-registration would let `pip install anything` silently widen every
agent's tool surface). This module is that explicit surface for cameras:

    from abstractcamera.integrations.abstractcore_tools import camera_tools
    llm.generate("Take a photo if something moves", tools=camera_tools())

Every tool delegates to the process-wide CameraService (the same sessions
the `camera` capability drives — imported lazily so listing tool specs
never loads OpenCV), returns a JSON-safe dict with an explicit `success`
marker (the ToolRegistry failure contract), and carries honest
user-actionable error text.

ID SPACES (stated in every wire-visible description because parameter
docstrings never reach the model): `camera_id` is a DISCOVERY id from
camera_list_devices, accepted ONLY by camera_open; `camera` is the DEVICE
UID that camera_open/camera_status return, used by every other tool.

LOOK vs SHOOT: camera_preview_photo saves the live-view frame silently
(no shutter, preview resolution) — the default answer to "what do you
see?"; camera_capture_photo actuates the physical shutter for a real
full-resolution capture. Both record the surroundings and default to
require-approval.

SIGHT LANE (operator-ruled, commons 3969/4089): capture/preview results
that landed a local file carry a handler-authored `media` list — bare
paths on this storeless tool lane (the capability plugin overrides with
`{"$artifact": id}` refs when an artifact store is present). The field is
AUTHORED at the source, never sniffed from prose, and ABSENT when no
local file landed (deferred, on-device, and undelivered results alike).
The ruled consumer contract is LIVE: abstractagent's ReAct adapter folds
`media` into the next model call so the agent SEES what it shot (agent's
media-ref fold, receipt c4133; LIVE-PROVEN by flow's adversary c4193 — a
gateway-hosted flow captured a real JPEG through camera_capture_photo and
the model described the actual room). Core's analyze_media stays the
RE-LOOK path afterwards (bounded text, one attempt; it PIL-verifies the
file decodes, so a stale ref refuses loudly instead of describing a
placeholder — core c4269). Deliberately out of scope: get_events rows (a
busy auto-fire page would fold N images against caps designed for single
tool results) — detection captures enter the sight lane when the agent
reads the event's path and looks at it explicitly.

Classification (the facts this package owns about its OWN tools, in core's
inventory vocabulary plus one domain tag, ruled per
decision:domain-tool-classification-tags):
- mutating: changes local host state (claims hardware, writes capture files).
- remote_write_capable: sends state-changing requests to remote systems
  (only DWARF Wi-Fi telescope mount/capture control qualifies; listing does not).
- captures_environment: true when invoking the tool RECORDS THE PHYSICAL
  SURROUNDINGS of the machine through any ambient sensor — camera today, a
  microphone-capture tool tomorrow (identical privacy event: real people
  recorded without consent). Screen capture is deliberately OUT (on-screen
  content is a different privacy event with its own future tag). Approval
  layers and privacy policies key on this fact — a capture tool
  auto-approved in an unattended loop records real people without consent.
  (Semantics passed spelling + this definition + the per-tool assignment,
  commons c3176. Promoted UNCHANGED to the framework-shared fact vocabulary
  by the tool-tiers item-D naming pass, 2026-07-22.)
- standing_effect: true when the call ARMS AN ONGOING PROCESS that keeps
  acting after the call returns — among camera's tools, only
  camera_start_detection (auto-fire keeps shooting on detections with
  nobody at the gate; one approval covers unbounded future shutters, the
  exact unattended-loop event captures_environment names). Ruled spelling
  from the tool-tiers item-D naming pass (semantics desk, core inventory
  desk verified; camera adopted c4497 — camera's proposed `standing` lost
  to core's `standing_effect`, one name per fact). Grant layers use it to
  select revocation-on-tighten semantics (host revokes the armed process
  via camera_stop_detection) instead of gate-on-next-call; the tier
  presentation renders it as a mandatory modifier, never a numeric bump.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

try:
    from abstractcore.tools import tool
    from abstractcore.tools.core import ToolDefinition
except ImportError as exc:  # pragma: no cover - exercised only without abstractcore
    raise ImportError(
        "abstractcamera.integrations.abstractcore_tools requires abstractcore: "
        'pip install "abstractcore"'
    ) from exc

# The fact keys every classification entry MUST declare. The approval
# derivation fails CLOSED against this set (an entry missing a key — e.g. a
# new tool whose author forgot `captures_environment` — must never slip into
# the auto-approve bucket on the strength of the keys that happen to be
# present; adversarial finding 2026-07-21). The exhaustiveness test pins it
# too, but the fail-closed DEFAULT is enforced in the CODE, not by the test.
# `standing_effect` joined with the tool-tiers item-D ruled vocabulary
# (schema v3; camera adopted c4497) — the partition is UNCHANGED by its
# arrival (the only standing tool already asked via captures_environment).
_CLASSIFICATION_FACTS = (
    "mutating",
    "remote_write_capable",
    "captures_environment",
    "standing_effect",
)

# The facts approval/policy layers consume (see module docstring). This map
# is exhaustive over CAMERA_TOOLS by construction and pinned by tests both
# ways — classification is a deliberate decision at add AND remove time
# (core's inventory rule, applied to this package's own tools).
CAMERA_TOOL_CLASSIFICATION: Dict[str, Dict[str, bool]] = {
    "camera_list_devices": {"mutating": False, "remote_write_capable": False, "captures_environment": False, "standing_effect": False},
    "camera_open": {"mutating": True, "remote_write_capable": True, "captures_environment": True, "standing_effect": False},
    "camera_close": {"mutating": True, "remote_write_capable": True, "captures_environment": False, "standing_effect": False},
    "camera_status": {"mutating": False, "remote_write_capable": False, "captures_environment": False, "standing_effect": False},
    # preview_photo: no shutter, no remote state change (frame pull is a
    # read) — but it RECORDS THE PHYSICAL SURROUNDINGS to a local file, so
    # captures_environment is unambiguously true (and mutating: it writes
    # the file). Same ask-by-default tier as the capture verbs.
    "camera_preview_photo": {"mutating": True, "remote_write_capable": False, "captures_environment": True, "standing_effect": False},
    "camera_capture_photo": {"mutating": True, "remote_write_capable": True, "captures_environment": True, "standing_effect": False},
    "camera_capture_video": {"mutating": True, "remote_write_capable": True, "captures_environment": True, "standing_effect": False},
    "camera_stop_recording": {"mutating": True, "remote_write_capable": True, "captures_environment": False, "standing_effect": False},
    # start_detection is camera's ONE standing authority: it arms auto-fire
    # that keeps shooting after the call returns (grant layers: tightening
    # below its tier means REVOKING via camera_stop_detection, adopted
    # c4444 — no per-shot gate exists by construction).
    "camera_start_detection": {"mutating": True, "remote_write_capable": True, "captures_environment": True, "standing_effect": True},
    "camera_stop_detection": {"mutating": True, "remote_write_capable": True, "captures_environment": False, "standing_effect": False},
    "camera_get_events": {"mutating": False, "remote_write_capable": False, "captures_environment": False, "standing_effect": False},
}


def _service():
    """The process-wide CameraService, imported at CALL time — the camera
    stack (OpenCV) must never load just because a host listed tool specs."""
    from abstractcamera.service import get_shared_service

    return get_shared_service()


@tool(
    description=(
        "List available cameras with connection state; returns discovery `id` (for camera_open) "
        "and `device_uid` (the address every other camera tool uses)."
    ),
    tags=["camera", "hardware"],
    when_to_use="First step of any camera task: discover what is available and whether something is already connected.",
)
def camera_list_devices() -> Dict[str, Any]:
    """List every camera the machine can see, without claiming any device.

    Returns discovery entries: `id` (use with camera_open), `name`,
    `transport` (ptp/webcam/dwarf/fake), `connected`, `device_uid` (the
    address for every other camera tool once connected), `active`, and
    `default`.
    """
    return _service().list_cameras()


@tool(
    description=(
        "Turn a camera on. `camera_id` = discovery id from camera_list_devices (empty = default "
        "device). Returns `camera`: the device uid all other camera tools use."
    ),
    tags=["camera", "hardware"],
    when_to_use="Before capture/detection when camera_status shows nothing connected.",
)
def camera_open(camera_id: str = "") -> Dict[str, Any]:
    """Connect a camera and make it the active one.

    Args:
        camera_id: Discovery id from camera_list_devices (the `id` field).
            Empty claims the default device (first tethered body, else the
            built-in webcam); when a camera is already open, empty returns
            it instead of claiming a second device.

    Returns the device uid (address for every other camera tool) and a
    status snapshot.
    """
    return _service().open(camera_id or None)


@tool(
    description=(
        "Turn a camera off and release the device (flushes pending downloads first). "
        "`camera` = device uid from camera_open; empty closes the active camera."
    ),
    tags=["camera", "hardware"],
    when_to_use="When done with a camera; leaving it claimed blocks other applications.",
)
def camera_close(camera: str = "") -> Dict[str, Any]:
    """Disconnect a camera.

    Args:
        camera: Device uid of a live camera (from camera_open/camera_status).
            Empty closes the active camera.
    """
    return _service().close(camera or None)


@tool(
    description=(
        "Camera state: connection, capture mode, detection, recording, pending downloads, "
        "last error. `camera` = device uid; empty reports every live camera."
    ),
    tags=["camera", "hardware"],
    when_to_use="To check what is connected and what the camera is doing before acting.",
)
def camera_status(camera: str = "") -> Dict[str, Any]:
    """Status of one live camera, or all of them.

    Args:
        camera: Device uid; empty reports every live camera plus which one
            is active.
    """
    return _service().status(camera or None)


@tool(
    description=(
        "Silently save the camera's CURRENT live-view frame as a JPEG and return its path — "
        "look WITHOUT firing the shutter. `camera` = device uid from camera_open."
    ),
    tags=["camera", "capture"],
    when_to_use=(
        "To see what the camera sees (framing, monitoring, 'what do you see?') — prefer this "
        "over camera_capture_photo unless a real full-resolution shot is wanted."
    ),
)
def camera_preview_photo(camera: str = "", wait_s: float = 2.0) -> Dict[str, Any]:
    """Save the current live-view frame to a JPEG file (no shutter).

    Args:
        camera: Device uid of a live camera; empty uses the active camera.
        wait_s: How long to wait for the first frame right after opening
            (the preview stream needs a moment to start).

    Returns `path` to the saved JPEG. Preview resolution (the live-view
    stream), not a full-resolution capture — use camera_capture_photo for
    a real shot. No shutter actuation, no capture event, nothing written
    on the camera's own storage.
    """
    return _service().preview_photo(camera or None, wait_s=wait_s)


@tool(
    description=(
        "Take one photo now and wait for the file; returns `path` (or an honest deferred/"
        "on-device note). `camera` = device uid from camera_open, NOT the discovery id."
    ),
    tags=["camera", "capture"],
    when_to_use=(
        "When the user wants a REAL photo (full resolution, shutter fires). For just seeing "
        "what the camera sees, camera_preview_photo is silent and free."
    ),
)
def camera_capture_photo(camera: str = "", timeout_s: float = 30.0) -> Dict[str, Any]:
    """Fire one still capture and block until it lands.

    Args:
        camera: Device uid; empty uses the active camera.
        timeout_s: How long to wait for the capture to complete (long
            exposures need more; capped at 300s).

    Returns `path` (the saved image file — analyze it with vision/media
    tools). `on_device: true` means the save policy keeps files on the
    camera's storage; `deferred: true` means detection auto-fire is armed
    and the file downloads when detection disarms.
    """
    return _service().capture_photo(camera or None, timeout_s=timeout_s)


@tool(
    description=(
        "Record a video clip (0.5-600s) and wait for the file; `path` may be null when the "
        "body keeps the movie on its own storage (honest note). `camera` = device uid."
    ),
    tags=["camera", "capture"],
    when_to_use="When the user wants a bounded video recording (0.5-600s). Open a camera first.",
)
def camera_capture_video(duration_s: float = 5.0, camera: str = "", timeout_s: float = 30.0) -> Dict[str, Any]:
    """Record video for `duration_s` seconds, then stop and save.

    Args:
        duration_s: Recording length in seconds (0.5 to 600).
        camera: Device uid; empty uses the active camera.
        timeout_s: How long to wait for the movie FILE after the recording
            stops (capped at 300s); bodies that keep movies on their own
            card return `delivered: false` with a note instead of the path.

    Webcam recording needs the [clips] extra; refusals are reported
    honestly in `error`.
    """
    return _service().capture_video(duration_s, camera or None, timeout_s=timeout_s)


@tool(
    description=(
        "Stop a running video recording (e.g. one started by detection auto-fire) and collect "
        "the movie file. `camera` = device uid; empty = active camera."
    ),
    tags=["camera", "capture"],
    when_to_use="When a recording is running (camera_status shows movie_recording=true) and it should end now.",
)
def camera_stop_recording(camera: str = "", timeout_s: float = 15.0) -> Dict[str, Any]:
    """Stop the running recording and collect the movie file.

    Args:
        camera: Device uid; empty uses the active camera.
        timeout_s: How long to wait for the movie file after stopping.

    Fails honestly when no recording is running.
    """
    return _service().stop_recording(camera or None, timeout_s=timeout_s)


@tool(
    description=(
        "Arm motion/lightning/meteor detection: auto-capture photo or video, or monitor-only; "
        "poll camera_get_events after. Omit `sensitivity` to keep current (never send null)."
    ),
    tags=["camera", "detection"],
    when_to_use="For 'take a photo/video when something moves' tasks: arm here, then poll camera_get_events.",
)
def camera_start_detection(
    action: str = "photo",
    target: str = "motion",
    sensitivity: Optional[float] = None,
    camera: str = "",
) -> Dict[str, Any]:
    """Arm live-view detection with optional auto-capture.

    Args:
        action: "photo" = auto-fire a still on each detection (cooldown
            applies); "video" = first detection starts recording, a later
            one stops it (camera_stop_recording ends it manually);
            "monitor" = log detections without firing the shutter. NOTE:
            motion/meteor detections ALSO save a short pre-event ring clip
            to disk in every mode, monitor included — its path rides the
            detection event ("monitor" still writes clip files, it only
            skips captures).
        target: What to detect — "motion", "lightning", or "meteor".
        sensitivity: 0-100 (higher = more sensitive); omit to keep the
            current setting.
        camera: Device uid; empty uses the active camera.

    Returns the armed state plus `event_watermark` — pass it as `since_id`
    to camera_get_events to read only what happens AFTER arming.
    """
    return _service().start_detection(camera or None, target=target, action=action, sensitivity=sensitivity)


@tool(
    description=(
        "Disarm camera detection and flush captures deferred while armed. `camera` = device "
        "uid; empty = active camera."
    ),
    tags=["camera", "detection"],
    when_to_use="When the watch task is over, before closing the camera.",
)
def camera_stop_detection(camera: str = "") -> Dict[str, Any]:
    """Turn detection off.

    Args:
        camera: Device uid; empty uses the active camera.
    """
    return _service().stop_detection(camera or None)


@tool(
    description=(
        "Read camera events oldest-first; page with `since_id`=previous `last_id`. If "
        "`session` changes, reset since_id to 0; `evicted:true` = events were dropped. "
        "`camera` = device uid."
    ),
    tags=["camera", "detection"],
    when_to_use="Poll after arming detection, or to find the file paths of recent captures.",
)
def camera_get_events(since_id: int = 0, camera: str = "", limit: int = 50) -> Dict[str, Any]:
    """Events newer than `since_id`, oldest first (cursor pagination).

    Args:
        since_id: Only events with id greater than this (use the previous
            call's `last_id`, or `event_watermark` from camera_start_detection).
        camera: Device uid; empty uses the active camera.
        limit: Maximum events per page; when `truncated` is true, call again
            with `since_id` = the returned `last_id` for the next page.

    Event kinds (the full wire set): "detection" (what was seen; `metrics`
    carries bbox/centroid/speed measurements, and motion/meteor detections
    carry `path` to their auto-saved ring clip — monitor mode included),
    "trigger" (a capture act was issued), "photo" (file saved locally —
    `path`), "photo-pending" (shot exists on the camera; downloads later or
    stays per save policy), "clip" (pre-capture ring clip), "camera-event"
    (device status), "error".
    Cursor rules: `session` names the id space — a NEW session value means
    the camera reconnected and ids restarted (reset your cursor to 0);
    `evicted: true` means the bounded log dropped events between your
    cursor and `first_retained_id` (poll faster or accept the gap). File
    events carry `trigger_id` correlating them to their trigger act.
    """
    return _service().get_events(camera or None, since_id=since_id, limit=limit)


# Definition order is presentation order in prompts: discovery -> lifecycle
# -> look -> capture -> detection reads as a workflow.
CAMERA_TOOLS = (
    camera_list_devices,
    camera_open,
    camera_close,
    camera_status,
    camera_preview_photo,
    camera_capture_photo,
    camera_capture_video,
    camera_stop_recording,
    camera_start_detection,
    camera_stop_detection,
    camera_get_events,
)


def camera_tools() -> List[Any]:
    """The camera tool functions, ready for `generate(tools=camera_tools())`
    (AbstractCore accepts @tool-decorated callables directly).

    Returns the LIVE decorated callables — their `_tool_definition` is a
    shared module-level object (inherent to abstractcore's `@tool`). A host
    that rewrites tool metadata for a provider wire must use
    `camera_tool_definitions()`/`camera_tool_specs()` (isolated copies);
    mutating a returned callable's `_tool_definition` poisons every reader
    process-wide (adversarial finding 2026-07-21)."""
    return list(CAMERA_TOOLS)


def camera_tool_definitions() -> List[ToolDefinition]:
    """ToolDefinition COPIES (schema + metadata) for hosts that manage
    registries/executors themselves (AbstractRuntime toolsets, gateways).
    Copies, because hosts commonly rewrite specs for provider wires — a
    mutation must never corrupt the module-level definitions every other
    caller reads (adversarial finding)."""
    out: List[ToolDefinition] = []
    for fn in CAMERA_TOOLS:
        definition = copy.copy(fn._tool_definition)
        definition.parameters = copy.deepcopy(fn._tool_definition.parameters)
        definition.tags = list(fn._tool_definition.tags or [])
        definition.examples = list(fn._tool_definition.examples or [])
        out.append(definition)
    return out


def camera_tool_specs() -> List[Dict[str, Any]]:
    """Flat dict specs ({name, description, parameters, ...}) — the
    canonical caller shape for AbstractCore's `generate(tools=...)` wire.
    Deep-copied per call (same shared-mutation rule as the definitions)."""
    return [copy.deepcopy(definition.to_dict()) for definition in camera_tool_definitions()]


def camera_tool_approval_defaults() -> Dict[str, List[str]]:
    """Approval defaults for hosts with name-set approval policies
    (AbstractRuntime's ToolApprovalPolicy shape: auto_approve_tools /
    require_approval_tools).

    DERIVED from CAMERA_TOOL_CLASSIFICATION, never hand-listed (a copy
    would rot when a tool or fact changes): a camera tool auto-approves
    ONLY when every declared fact key is present AND every one is False —
    it neither mutates local state, nor reaches remote devices, nor records
    the physical surroundings, nor arms a standing process. Everything else
    DEFAULTS to require-approval.

    These are DEFAULTS, not a floor (operator ruling 2026-07-21, commons
    c3938): "a user must be able to auto accept camera or ask the agent to
    request permissions, like for any other tool." A host's user-facing
    policy (e.g. AbstractRuntime's run-scoped tool_policy) may auto-accept
    `captures_environment` tools on the USER'S say-so — what must never
    happen is auto-approval that nobody chose (the unattended-loop privacy
    event: real people recorded without consent).

    FAILS CLOSED (adversarial finding 2026-07-21): an entry missing a fact
    key, or with an extra/unknown key, goes to require_approval — the
    fail-closed default is enforced HERE, not delegated to the
    exhaustiveness test. A drifted or half-written classification can only
    ever be MORE strict, never silently auto-approve.

    CAVEAT for consumers (adversarial finding 2026-07-21, model-completeness
    gap, NOT a derivation bug): auto-approval means the tool does not itself
    capture/mutate/reach-remote — it does NOT mean zero imagery egress.
    `camera_get_events` returns filesystem PATHS to already-captured stills/
    clips and `camera_status` returns `capture_dir`; paired with an
    auto-approved `read_file`, an agent can read capture imagery with no
    further approval (one `camera_start_detection` approval then arms
    unbounded auto-fire whose paths all flow through these read surfaces).
    Trigger is precise (runtime cross-verify c3920): AbstractRuntime's
    `read_file` is workspace-walled, so the egress is live exactly when
    `capture_dir` sits INSIDE the run workspace — the common unattended
    setup; out-of-workspace capture paths refuse on containment. Hosts
    wiring the camera toolset for unattended use should therefore keep the
    capture root OUTSIDE the agent workspace, pair the toolset with a
    NON-auto file-read policy, or gate `camera_get_events`/`camera_status`.
    A dedicated privacy fact for capture-reference disclosure was taken to
    the semantics desk and RULED against (c3924): references are composition
    (host-policy jurisdiction), not a tool fact — the caveat + host gating IS
    the design. REOPEN TRIGGER (the ruling's boundary): if a future camera
    tool ever returns imagery CONTENT in-band (inline thumbnails / preview
    frames in an event payload) rather than references, that tool discloses
    the environment itself and the classification question genuinely returns
    to the semantics desk. Paths are not the trigger; bytes are. (This is
    exactly why `camera_get_events` strips thumbnails by default — keep it
    reference-only or the reopen fires.)
    """
    facts_keys = set(_CLASSIFICATION_FACTS)
    auto: List[str] = []
    require: List[str] = []
    for name, facts in CAMERA_TOOL_CLASSIFICATION.items():
        # Fail closed: only an entry that declares EXACTLY the fact set
        # and sets every one False may auto-approve. Missing/extra keys or
        # any True fact => approval side.
        if set(facts.keys()) == facts_keys and not any(facts.values()):
            auto.append(name)
        else:
            require.append(name)
    return {"auto_approve": sorted(auto), "require_approval": sorted(require)}
