"""Device-independent media download/sync engine (ADR 0011).

One engine, any MediaStore (`media_store.py`): list the device's media,
copy what is missing locally, verify sizes, and — only when asked — delete
the device copies that verified. The SAFETY RULES live here, once, so no
device adapter can get them wrong independently:

- Copy is incremental: a file whose local twin already matches in size is
  skipped (device mtimes are untrustworthy — the DWARF's clock has
  produced year-2038 stamps; sizes are truth).
- Every copy is verified (size readback) before it counts.
- Deletion touches ONLY entries whose local copy verifies AT DELETE TIME;
  a copy failure protects its file from deletion by construction.
- `protected` entries (device-functional state like the DWARF's dark
  library) are copied but NEVER deleted.
- Entries whose size the device cannot report are copied but never
  deleted (they cannot be verified).
- The destination disk is checked BEFORE the first byte moves (512MB
  floor kept free).
- `dry_run` walks the identical decision path and touches nothing.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from abstractcamera.identity import default_capture_root
from abstractcamera.media_store import MediaEntry, contained_path

DEST_FREE_FLOOR_BYTES = 512 * 1024 * 1024
# Pessimistic per-file budget for entries the device reports with an
# unknown size, so an unsized stream cannot slip past the free-space floor.
UNKNOWN_SIZE_BUDGET_BYTES = 512 * 1024 * 1024


@dataclass
class SyncReport:
    source: str = ""
    dest: str = ""
    copied: int = 0
    copied_bytes: int = 0
    skipped: int = 0          # already present locally (same size)
    deleted: int = 0
    deleted_protected: int = 0  # calibration files deleted (explicit opt-in)
    protected: int = 0        # matched files kept on the device (protected)
    failures: list[str] = field(default_factory=list)
    pruned: int = 0           # store-specific cleanup items (emptied folders)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures


def _local_size(dest: str, relpath: str) -> int:
    try:
        return os.path.getsize(os.path.join(dest, relpath))
    except OSError:
        return -1


def _human(count: int | float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{value:.1f}TB"


def sync_store(store, dest: str | None = None, *, delete: bool = False,
               delete_protected: bool = False, dry_run: bool = False,
               log=print) -> SyncReport:
    """Download everything from `store`; optionally free the device after.

    `store` is any MediaStore adapter (see media_store.py). `dest` defaults
    to the family capture layout: `~/Pictures/<device_slug>/`.
    `delete_protected` extends `delete` to the protected entries too (the
    device's calibration library) — an EXPLICIT opt-in because the device
    functionally needs those files (re-shooting darks costs a session);
    the verified-local-copy rule still applies to every deletion.
    """
    dest = os.path.abspath(os.path.expanduser(
        dest or os.path.join(default_capture_root(), store.device_slug)))
    report = SyncReport(source=store.describe(), dest=dest, dry_run=dry_run)

    reason = store.validate()
    if reason:
        report.failures.append(reason)
        return report

    # dest must not sit inside the source volume (a filesystem store knows
    # its root): copying onto the very card being read — and then deleting
    # from it — is a footgun with no legitimate use.
    source_root = getattr(store, "root", None)
    if source_root:
        source_real = os.path.realpath(source_root)
        dest_real = os.path.realpath(dest)
        if dest_real == source_real or dest_real.startswith(source_real + os.sep):
            report.failures.append(
                f"destination {dest} is inside the source {source_root} — "
                "choose a --dest outside the device.")
            return report

    try:
        entries = store.list_media()
    except Exception as exc:
        report.failures.append(f"listing device media failed: {exc}")
        return report

    # Two device entries that map to ONE local relpath cannot both be
    # verified by a single local file — deleting both would lose the
    # device's own second file. Such relpaths are excluded from deletion
    # (adversarial finding 2026-07-16).
    from collections import Counter

    relpath_counts = Counter(entry.relpath for entry in entries)

    # ---- the plan, announced BEFORE the first byte moves -------------------------
    to_copy = [entry for entry in entries
               if _local_size(dest, entry.relpath) < 0
               or (entry.size >= 0 and _local_size(dest, entry.relpath) != entry.size)]
    needed = sum(entry.size for entry in to_copy if entry.size > 0)
    # Unknown-size entries (size < 0) get budgeted at a pessimistic floor
    # so an unsized/streaming entry cannot silently blow past the free
    # floor (adversarial finding 2026-07-16).
    unknown_to_copy = sum(1 for entry in to_copy if entry.size < 0)
    needed += unknown_to_copy * UNKNOWN_SIZE_BUDGET_BYTES
    total_bytes = sum(entry.size for entry in entries if entry.size > 0)
    log(f"device media: {len(entries)} files ({_human(total_bytes)}) — "
        f"new to download: {len(to_copy)}"
        + (f" ({_human(needed)})" if to_copy else ""))
    if not dry_run and to_copy:
        os.makedirs(dest, exist_ok=True)
        free = shutil.disk_usage(dest).free
        if needed > free - DEST_FREE_FLOOR_BYTES:
            report.failures.append(
                f"not enough space in {dest}: need ~{_human(needed)}, "
                f"free {_human(free)} (512MB floor kept)")
            return report

    # ---- copy pass ----------------------------------------------------------------
    # relpaths that hold (or, in a dry run, WOULD hold) a size-verified
    # local copy — the only entries the delete pass may consider.
    verified: set[str] = set()
    for entry in entries:
        local_path = os.path.join(dest, entry.relpath)
        local_size = _local_size(dest, entry.relpath)
        if entry.size >= 0 and local_size == entry.size:
            report.skipped += 1
            verified.add(entry.relpath)
            continue
        if entry.size < 0 and local_size >= 0:
            # Unknown device size + a local copy present: skip the re-copy
            # but do NOT mark verified — unverifiable files never delete.
            report.skipped += 1
            continue
        # Containment: the device/host-supplied relpath must resolve inside
        # dest. The single choke point that stops a `..`/absolute path from
        # writing outside the destination (P0 adversarial finding).
        safe_local = contained_path(dest, entry.relpath)
        if safe_local is None:
            report.failures.append(
                f"unsafe path from device (escapes destination): {entry.relpath}")
            continue
        if dry_run:
            log(f"would copy  {entry.relpath}"
                + (f"  ({_human(entry.size)})" if entry.size >= 0 else ""))
            report.copied += 1
            report.copied_bytes += max(0, entry.size)
            if entry.size >= 0:
                verified.add(entry.relpath)  # the plan: post-copy it verifies
            continue
        try:
            os.makedirs(os.path.dirname(safe_local), exist_ok=True)
            store.fetch(entry, safe_local)
            fetched = os.path.getsize(safe_local)
            if entry.size >= 0 and fetched != entry.size:
                raise OSError(
                    f"size mismatch after copy ({fetched} != {entry.size})")
        except Exception as exc:
            report.failures.append(f"copy failed: {entry.relpath}: {exc}")
            continue
        log(f"copied  {entry.relpath}  ({_human(fetched)})")
        report.copied += 1
        report.copied_bytes += fetched
        if entry.size >= 0:
            verified.add(entry.relpath)

    # ---- delete pass (verified entries only) -----------------------------------------
    if delete:
        if not getattr(store, "can_delete", False):
            report.failures.append(
                f"this device store cannot delete ({store.describe()})")
            return report
        # The store's own deletion guard runs EVEN IN DRY RUNS and even for
        # explicitly-named sources: a copy/backup of a device's card must
        # never be deletable through this engine (hardware identity, not
        # content, decides — see FilesystemMediaStore.deletion_guard).
        guard = getattr(store, "deletion_guard", None)
        reason = guard() if callable(guard) else None
        if reason:
            report.failures.append(reason)
            return report
        # Surface WHICH physical volume will be freed (UUID when known) —
        # the operator confirms the volume, not just a device-class string.
        target_id = getattr(store, "deletion_target_id", None)
        if callable(target_id):
            log(f"freeing device storage: {target_id()}")
        deleted_entries: list[MediaEntry] = []
        for entry in entries:
            if entry.protected and not delete_protected:
                report.protected += 1
                continue
            # A relpath shared by two device entries cannot be verified by
            # one local file — never delete either (the second file's bytes
            # would be lost). Excluded loudly.
            if relpath_counts[entry.relpath] > 1:
                report.failures.append(
                    f"kept on device (ambiguous: {relpath_counts[entry.relpath]} "
                    f"device files map to one local path): {entry.relpath}")
                continue
            # Re-verify against the DISK at delete time (a local file that
            # changed since the copy pass protects its device twin). Dry
            # runs consult the plan.
            still_verified = (entry.relpath in verified
                              and (dry_run
                                   or _local_size(dest, entry.relpath) == entry.size))
            if not still_verified:
                report.failures.append(
                    f"kept on device (no verified local copy): {entry.relpath}")
                continue
            if dry_run:
                log(f"would delete  {entry.relpath}"
                    + ("  (calibration)" if entry.protected else ""))
                report.deleted += 1
                report.deleted_protected += 1 if entry.protected else 0
                continue
            try:
                store.delete(entry)
            except Exception as exc:
                report.failures.append(f"delete failed: {entry.relpath}: {exc}")
                continue
            report.deleted += 1
            report.deleted_protected += 1 if entry.protected else 0
            deleted_entries.append(entry)
        if not dry_run and deleted_entries:
            try:
                report.pruned = int(store.finalize_delete(deleted_entries))
            except Exception as exc:
                report.failures.append(f"post-delete cleanup failed: {exc}")

    return report


def run_cli(args, log=print) -> int:
    """The `abstractcamera download` command body (argparse Namespace in)."""
    from abstractcamera.media_store import (DwarfAlbumMediaStore,
                                            FilesystemMediaStore,
                                            find_card_volumes)

    delete_protected = bool(getattr(args, "delete_calibrations", False))
    if delete_protected and not args.delete:
        log("--delete-calibrations extends --delete (it adds the device's "
            "calibration library to the deletion) — pass both flags.")
        return 1

    if getattr(args, "host", None):
        store = DwarfAlbumMediaStore(args.host)
    elif args.source is not None:
        store = FilesystemMediaStore(args.source)
    else:
        volumes = find_card_volumes()
        if not volumes:
            log("No device card is mounted. Plug the USB-C cable and enable "
                "storage mode (DWARF: in the DWARFLAB app), pass --source "
                "PATH, or use --host IP for the Wi-Fi album. "
                "`abstractcamera list` shows connected devices and media "
                "sources.")
            return 1
        if len(volumes) > 1:
            log("Several device cards are mounted — pick one with --source "
                "(`abstractcamera list` shows them all):")
            for root, layout in volumes:
                log(f"  --source \"{root}\"  ({layout.slug})")
            return 1
        store = FilesystemMediaStore(volumes[0][0], volumes[0][1])

    log(f"device: {store.describe()}")
    # Copy-side honesty: importing from a volume that is NOT the device's
    # own storage is allowed (backups are legitimate) but must SAY so —
    # a case-insensitive filesystem can make a personal drive's
    # astronomy/photos/videos folders look like a device card (live
    # incident 2026-07-16).
    guard = getattr(store, "deletion_guard", None)
    if callable(guard) and not args.delete:
        reason = guard()
        if reason:
            log("NOTE: this volume is NOT the device's own storage — "
                "treating it as a copy/backup import. Check the plan below "
                "before proceeding (deletion would be refused here).")
    report = sync_store(store, args.dest, delete=args.delete,
                        delete_protected=delete_protected,
                        dry_run=args.dry_run, log=log)

    prefix = "would be " if report.dry_run else ""
    log("")
    if report.ok and not args.delete and report.copied == 0 and report.skipped:
        # The everything-is-already-here outcome must SAY so — "copied: 0"
        # alone reads like a failure (operator confusion, 2026-07-16).
        log(f"Nothing new on the device: all {report.skipped} media files "
            f"are already downloaded in {report.dest} (device hierarchy "
            "preserved).")
    else:
        log(f"source: {report.source}")
        log(f"dest:   {report.dest}  (device hierarchy preserved)")
        log(f"{prefix}copied: {report.copied} files ({_human(report.copied_bytes)}), "
            f"already present: {report.skipped}")
    if args.delete:
        log(f"{prefix}deleted from the device: {report.deleted} files"
            + (f" (incl. {report.deleted_protected} calibration files)"
               if report.deleted_protected else "")
            + (f" (+{report.pruned} emptied folders)" if report.pruned else ""))
        if report.protected:
            log(f"kept on the device (calibration library): {report.protected} "
                "files — add --delete-calibrations to remove them too")
    for failure in report.failures:
        log(f"FAILED: {failure}")
    return 0 if report.ok else 1
