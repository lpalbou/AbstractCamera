"""Device media stores: one inventory surface across camera devices (ADR 0011).

"Download ALL the files from the device" is the same operation whether the
device is a DWARF microSD on USB, the DWARF album over Wi-Fi, or (next) a
PTP body's card over libgphoto2 — list the media, fetch each file, verify,
optionally delete the device copy. A MediaStore adapter owns the
DEVICE-SPECIFIC mechanics; the sync engine (`media_sync.py`) owns the
safety rules (verify-before-delete, protected entries, space checks) so no
adapter can get them wrong independently.

Adapter contract (structural, like CameraSession):

    device_slug   -> str    local layout folder (~/Pictures/<device_slug>/)
    describe()    -> str    one honest line for logs ("USB card at ...")
    list_media()  -> list[MediaEntry]   the FULL media inventory; device
                            system state (logs, firmware) is never listed
    fetch(entry, local_path)            download ONE file (raise on failure;
                            the engine verifies size afterwards)
    can_delete    -> bool   whether deletion is offered at all
    delete(entry)           remove ONE device copy (only ever called on
                            entries the engine verified locally)
    finalize_delete(deleted) -> int     store-specific cleanup (prune emptied
                            card folders...); returns items cleaned

Entries carry `protected=True` when the device NEEDS the file (the DWARF's
astronomy dark library): the engine copies protected entries but never
deletes them.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class MediaEntry:
    """One device media file. `relpath` doubles as the local layout path;
    `ref` is store-opaque addressing (album dict, PTP folder tuple...)."""

    relpath: str
    size: int
    protected: bool = False
    ref: object = None


def sanitize_relpath(relpath: str) -> str | None:
    """A device/host-supplied relative path reduced to safe local layout
    components: no absolute anchor, no `..`, no `.`, no empty segments,
    forward OR backslashes tolerated. Returns None when nothing safe
    remains (the caller must skip such an entry, never write it). This is
    the last line against a malicious/buggy device escaping the
    destination via `os.path.join` (adversarial finding 2026-07-16)."""
    if not relpath:
        return None
    normalized = str(relpath).replace("\\", "/")
    safe_parts: list[str] = []
    for part in normalized.split("/"):
        part = part.strip()
        if part in ("", ".", ".."):
            continue
        if os.path.isabs(part) or ":" in part:  # drive letters / alt streams
            continue
        safe_parts.append(part)
    return os.path.join(*safe_parts) if safe_parts else None


def contained_path(root: str, relpath: str) -> str | None:
    """Resolve `relpath` under `root` and return the absolute path ONLY if
    it stays inside `root` (symlinks resolved). None on any escape — the
    single containment check every fetch/delete flows through so no
    device-supplied string can address outside its tree."""
    safe_rel = sanitize_relpath(relpath)
    if safe_rel is None:
        return None
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, safe_rel))
    if target == root_real or target.startswith(root_real + os.sep):
        return target
    return None


@dataclass(frozen=True)
class CardLayout:
    """A camera family's mounted storage: the HARDWARE identity that
    proves a volume IS that camera, and where media lives on it.

    DETECTION IS HARDWARE-ONLY (operator ruling 2026-07-16, after a live
    near-miss): folder names are NEVER evidence of a camera — an
    operator's personal drive carrying `astronomy/`+`videos/` folders
    matched a folder signature (macOS filesystems are case-insensitive)
    and was offered as a --delete target. A copy/backup of a card is
    content-identical by definition, so content cannot prove anything.
    `media_dirs` only says WHERE MEDIA LIVES on a volume already proven
    by hardware (or explicitly named for a copy-only import)."""

    slug: str
    media_dirs: tuple[str, ...]
    protected_subtrees: tuple[str, ...] = ()
    # Non-hidden system entries to skip anywhere in the tree.
    system_entries: frozenset = frozenset({"System Volume Information"})
    # Device identity the volume's backing MEDIA must report (diskutil
    # MediaName). A layout with an EMPTY tuple can never be auto-detected
    # and never authorizes deletion (fail-safe): it is copy-import-only.
    device_media_names: tuple[str, ...] = ()


# The DWARF 3 card (measured 2026-07-14): album dirs at the volume root;
# CALI_FRAME is the device's dark-frame library — deleting it would
# silently force re-shooting darks before the next stacking session.
# "File-Stor Gadget" is the identity the DWARF's own USB storage mode
# presents (measured; the Linux mass-storage gadget) — a card in a USB
# READER presents the reader's identity instead and deliberately does NOT
# match (fail-safe: delete via the device's own USB mode).
DWARF_CARD_LAYOUT = CardLayout(
    slug="dwarf_3",
    media_dirs=("Normal_Photos", "Astronomy", "Burst", "Panoramas", "Videos"),
    protected_subtrees=(os.path.join("Astronomy", "CALI_FRAME"),),
    device_media_names=("File-Stor Gadget",),
)

KNOWN_CARD_LAYOUTS = (DWARF_CARD_LAYOUT,)


def _volume_device_identity(path: str) -> dict | None:
    """Hardware identity of the volume containing `path` (macOS: diskutil).

    Returns {"media_name", "removable", "bus_protocol", "volume_uuid"} or
    None when the identity cannot be established (non-macOS, diskutil
    failure, or a plain subdir path diskutil declines) — callers treat
    None as NOT the device (fail-safe, never fail-open)."""
    import plistlib
    import subprocess

    try:
        out = subprocess.run(["diskutil", "info", "-plist", path],
                             capture_output=True, timeout=10.0)
        if out.returncode != 0:
            return None
        info = plistlib.loads(out.stdout)
    except Exception:
        return None
    return {
        "media_name": str(info.get("MediaName", "")),
        "removable": bool(info.get("Removable", False)
                          or info.get("RemovableMedia", False)),
        "bus_protocol": str(info.get("BusProtocol", "")),
        "volume_uuid": str(info.get("VolumeUUID", "")),
    }


def device_identity_matches(layout: CardLayout, identity: dict | None) -> bool:
    """True when the volume's HARDWARE identity is this camera's own
    storage. Requires the declared device MediaName AND removable-USB
    backing. A layout without a declared identity can never match
    (fail-safe: such layouts are copy-import-only); unknown identity
    never matches.

    HONEST LIMIT: MediaName is the Linux mass-storage gadget's own string
    (the DWARF presents "File-Stor Gadget"), which another Linux
    mass-storage gadget could also present. This gate EXCLUDES every
    normal drive (internal APFS, USB SSDs, backups — none report a camera
    gadget string on removable-USB backing; the 2026-07-16 incident drive
    is rejected), but it is identity-by-declaration, not per-unit
    attestation. The deletion prompt surfaces the volume UUID so the
    operator sees exactly which physical volume will be freed."""
    if not layout.device_media_names:
        return False
    if not identity:
        return False
    if identity.get("media_name") not in layout.device_media_names:
        return False
    if not bool(identity.get("removable")):
        return False
    # A camera card is USB-attached mass storage. An internal/virtual
    # volume that somehow reported a matching name is excluded here — the
    # bus is checked only when diskutil reported one (older macOS may omit
    # it; absence is not, by itself, disqualifying given the name+removable
    # match already holds).
    bus = identity.get("bus_protocol", "")
    if bus and bus != "USB":
        return False
    return True


def find_card_volumes(volumes_root: str = "/Volumes",
                      layouts: tuple[CardLayout, ...] = KNOWN_CARD_LAYOUTS,
                      identity_fn=None,
                      ) -> list[tuple[str, CardLayout]]:
    """Mounted volumes that ARE a known camera's own storage, proven by
    HARDWARE IDENTITY ALONE. Folder contents are never consulted: a
    personal drive or a backup copy of a card must never appear here, no
    matter what folders it carries (operator ruling 2026-07-16). An empty
    freshly-formatted camera card IS detected (it is the camera's
    storage; it simply holds no media yet)."""
    identity_fn = identity_fn or _volume_device_identity
    matches: list[tuple[str, CardLayout]] = []
    try:
        entries = sorted(os.listdir(volumes_root))
    except OSError:
        return []
    for name in entries:
        root = os.path.join(volumes_root, name)
        if not os.path.isdir(root):
            continue
        identity = identity_fn(root)
        for layout in layouts:
            if device_identity_matches(layout, identity):
                matches.append((root, layout))
                break
    return matches


class FilesystemMediaStore:
    """A mounted device card (USB mass storage): media listed by walking
    the layout's album dirs; system/hidden entries never listed.

    COPYING from any folder shaped like the layout is allowed (importing a
    backup is legitimate and harmless). DELETING requires the volume to
    present the DEVICE's own hardware identity — see deletion_guard()."""

    def __init__(self, root: str, layout: CardLayout = DWARF_CARD_LAYOUT,
                 identity_fn=None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.layout = layout
        self.device_slug = layout.slug
        self.can_delete = True
        self._identity_fn = identity_fn or _volume_device_identity

    def describe(self) -> str:
        return f"mounted card at {self.root} ({self.layout.slug} layout)"

    def deletion_guard(self) -> str | None:
        """None when deleting here is safe (the volume IS the device's own
        storage); else the refusal reason. Content can never prove this —
        a backup copy is identical by definition — so the volume's
        HARDWARE identity decides (live incident 2026-07-16: an operator
        drive carrying similar folders was one --delete away from losing
        data)."""
        identity = self._identity_fn(self.root)
        if device_identity_matches(self.layout, identity):
            return None
        seen = ((identity or {}).get("media_name") or "unknown device")
        expected = " / ".join(self.layout.device_media_names)
        return (
            f"deletion refused: {self.root} is not {self.layout.slug}'s own "
            f"storage (volume hardware reports '{seen}', the device presents "
            f"'{expected}') — it looks like a drive or a backup COPY of the "
            "card. Copying is fine; deleting is only allowed on the device "
            "itself (plug the camera and enable its USB storage mode).")

    def deletion_target_id(self) -> str:
        """A human-checkable identifier for the volume a deletion would
        free — the volume UUID when known, else the mount path. Surfaced
        before deletion so the operator confirms the physical volume, not
        just a device-class string."""
        identity = self._identity_fn(self.root) or {}
        uuid = identity.get("volume_uuid")
        return f"{self.root} (volume {uuid})" if uuid else self.root

    def validate(self) -> str | None:
        """None when syncing from here makes sense; else the reason.

        The camera's own storage (hardware-proven) is always valid — an
        empty freshly-formatted card is honestly '0 media files'. Any
        OTHER volume is a copy-only import source and must actually
        carry media folders, or there is nothing here to import."""
        if not os.path.isdir(self.root):
            return f"source is not a directory: {self.root}"
        if device_identity_matches(self.layout, self._identity_fn(self.root)):
            return None
        if not any(os.path.isdir(os.path.join(self.root, media))
                   for media in self.layout.media_dirs):
            return (f"{self.root} is not the {self.layout.slug} camera's own "
                    f"storage and carries no {self.layout.slug} media "
                    f"directories ({', '.join(self.layout.media_dirs)}) — "
                    "nothing to import here.")
        return None

    def _is_system_entry(self, name: str) -> bool:
        # Hidden entries cover the device's own `.log/`, macOS `.fseventsd`/
        # `.DS_Store`, and AppleDouble `._*` droppings — none of them media.
        return (name.startswith(".") or name.startswith("_")
                or name in self.layout.system_entries)

    def _is_protected(self, relpath: str) -> bool:
        return any(relpath == subtree or relpath.startswith(subtree + os.sep)
                   for subtree in self.layout.protected_subtrees)

    def list_media(self) -> list[MediaEntry]:
        entries: list[MediaEntry] = []
        root_real = os.path.realpath(self.root)
        for media in self.layout.media_dirs:
            media_root = os.path.join(self.root, media)
            # A SYMLINKED media root would let os.walk (which always enters
            # its base) copy from — and later delete through — files
            # OUTSIDE the card. The card boundary is sacred: skip it
            # (adversarial finding 2026-07-16).
            if os.path.islink(media_root) or not os.path.isdir(media_root):
                continue
            for dirpath, dirnames, filenames in os.walk(media_root):
                # Never descend a symlinked subdirectory either (os.walk
                # defaults to followlinks=False, but be explicit — a future
                # edit must not silently flip it).
                dirnames[:] = [d for d in dirnames
                               if not self._is_system_entry(d)
                               and not os.path.islink(os.path.join(dirpath, d))]
                for filename in sorted(filenames):
                    if self._is_system_entry(filename):
                        continue
                    path = os.path.join(dirpath, filename)
                    # The file's real path must stay inside the volume — a
                    # symlinked FILE pointing outside is not the card's data.
                    real = os.path.realpath(path)
                    if real != root_real and not real.startswith(root_real + os.sep):
                        continue
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue  # vanished mid-walk (card pulled)
                    relpath = os.path.relpath(path, self.root)
                    entries.append(MediaEntry(
                        relpath=relpath, size=size,
                        protected=self._is_protected(relpath), ref=path))
        return entries

    def _contained_ref(self, entry: MediaEntry) -> str:
        """The entry's on-card path, re-proven inside the volume at
        use-time (defense-in-depth: the adapter is safe even if a caller
        bypasses the engine). Raises when it would escape."""
        ref = os.path.realpath(str(entry.ref))
        root_real = os.path.realpath(self.root)
        if ref != root_real and not ref.startswith(root_real + os.sep):
            raise OSError(f"refusing to touch a path outside the card: {ref}")
        return str(entry.ref)

    def fetch(self, entry: MediaEntry, local_path: str) -> None:
        # follow_symlinks=False: never dereference a symlinked source into
        # a bulk copy of whatever it points at.
        shutil.copy2(self._contained_ref(entry), local_path, follow_symlinks=False)

    def delete(self, entry: MediaEntry) -> None:
        os.remove(self._contained_ref(entry))

    def finalize_delete(self, deleted: list[MediaEntry]) -> int:
        """Prune emptied session folders; album roots and protected
        folders always survive (the device expects them). The walk is
        clamped to the volume's real root so a stray path can never climb
        out and rmdir a parent elsewhere."""
        root_real = os.path.realpath(self.root)
        keep = {os.path.realpath(os.path.join(self.root, media))
                for media in self.layout.media_dirs}
        keep |= {os.path.realpath(os.path.join(self.root, subtree))
                 for subtree in self.layout.protected_subtrees}
        pruned = 0
        emptied = {os.path.dirname(str(entry.ref)) for entry in deleted}
        for dirpath in sorted(emptied, key=len, reverse=True):
            current = os.path.realpath(dirpath)
            while (current not in keep and os.path.isdir(current)
                   and current != root_real
                   and current.startswith(root_real + os.sep)):
                try:
                    if os.listdir(current):
                        break
                    os.rmdir(current)
                    pruned += 1
                except OSError:
                    break
                current = os.path.dirname(current)
        return pruned


class DwarfAlbumMediaStore:
    """The DWARF album over Wi-Fi (DwarfLab API v2): list via the album
    REST index, fetch via streamed HTTP download, delete via the album
    delete endpoint. Works while the telescope is on the network — no USB
    cable, no storage-mode toggle. NOTE: implemented from the published
    spec; not yet validated against live hardware (like the rest of the
    dwarf family — scripts/validate_dwarf.py is the moment of truth).
    """

    # The device's own dark-frame library lives under Astronomy/CALI_FRAME
    # on the card; album entries pointing there must never be deleted.
    PROTECTED_MARKERS = ("CALI_FRAME",)
    MEDIA_DIR_MARKERS = ("Normal_Photos", "Astronomy", "Burst", "Panoramas",
                         "Videos")

    def __init__(self, host: str, transport=None):
        from abstractcamera.drivers.dwarf_transport import DwarfTransport

        self.host = host
        self._transport = transport or DwarfTransport(host)
        self.device_slug = "dwarf_3"
        self.can_delete = True

    def describe(self) -> str:
        return f"DWARF album over Wi-Fi at {self.host}"

    def validate(self) -> str | None:
        return None  # reachability surfaces as honest fetch errors

    def _relpath(self, file_path: str, file_name: str) -> str | None:
        """Mirror the card layout locally: the path from the last known
        album dir down; unknown shapes land under Album/<basename>.

        Every component is sanitized (sanitize_relpath): the device's
        filePath/fileName are UNTRUSTED strings over plain HTTP, and a
        `..`/absolute component would otherwise escape the destination via
        os.path.join (P0 adversarial finding 2026-07-16). Returns None
        when nothing safe remains — the caller skips the entry."""
        parts = [part for part in file_path.split("/") if part]
        for index in range(len(parts) - 1, -1, -1):
            if parts[index] in self.MEDIA_DIR_MARKERS:
                return sanitize_relpath("/".join(parts[index:]))
        return sanitize_relpath(os.path.join("Album", os.path.basename(file_name)))

    def list_media(self) -> list[MediaEntry]:
        # pageSize=0 = no paging (API v2): the whole inventory in one call.
        raw = self._transport.album_media_infos(media_type=0, page_index=0,
                                                page_size=0)
        entries: list[MediaEntry] = []
        for item in raw:
            name = str(item.get("fileName", ""))
            path = str(item.get("filePath", ""))
            if not name or not path:
                continue
            relpath = self._relpath(path, name)
            if relpath is None:
                continue  # device gave a path with nothing safe in it
            size = _parse_size(item.get("fileSize"))
            protected = any(marker in path.split("/")
                            for marker in self.PROTECTED_MARKERS)
            entries.append(MediaEntry(relpath=relpath, size=size,
                                      protected=protected, ref=dict(item)))
        return entries

    def fetch(self, entry: MediaEntry, local_path: str) -> None:
        self._transport.fetch_media_to(str(entry.ref["filePath"]), local_path)

    def delete(self, entry: MediaEntry) -> None:
        self._transport.album_delete([entry.ref])

    def finalize_delete(self, deleted: list[MediaEntry]) -> int:
        return 0  # the device owns its folders over this transport


def _parse_size(value) -> int:
    """Album sizes arrive as display strings ("689.38 KB") or numbers."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    units = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
             "TB": 1024 ** 4}
    for unit, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.upper().endswith(unit):
            try:
                return int(float(text[: -len(unit)].strip()) * factor)
            except ValueError:
                return -1
    try:
        return int(float(text))
    except ValueError:
        return -1


__all__ = ["MediaEntry", "CardLayout", "DWARF_CARD_LAYOUT",
           "KNOWN_CARD_LAYOUTS", "find_card_volumes", "device_identity_matches",
           "sanitize_relpath", "contained_path", "FilesystemMediaStore",
           "DwarfAlbumMediaStore"]
