# ADR 0011 — Device media stores: one download/sync engine, per-device adapters

Date: 2026-07-16 · Status: accepted

## Context

"Download ALL the files from the device (and optionally free it)" is a
capability users expect from every camera this package pilots — a DWARF
card over USB today, the DWARF album over Wi-Fi, a Sony A7R IV or Nikon
Z6 II card over PTP tomorrow. The first implementation (a DWARF-only
`import` command, 2026-07-16, never released) hardcoded the card layout
AND the safety rules in one module; a second device would have duplicated
the rules — and deletion rules that exist in N copies eventually diverge
in the copy that deletes someone's photos.

## Decision

1. **Split mechanics from policy.** A `MediaStore` adapter
   (`media_store.py`) owns the DEVICE-SPECIFIC mechanics behind a small
   structural contract: `device_slug`, `describe()`, `validate()`,
   `list_media() -> [MediaEntry]`, `fetch(entry, local_path)`,
   `can_delete`, `delete(entry)`, `finalize_delete(deleted)`. The sync
   engine (`media_sync.sync_store`) owns every SAFETY RULE, once:
   incremental size-verified copies, destination space checks
   (512MB floor), verify-at-delete-time, `protected` entries never
   deleted, unverifiable (size-unknown) entries never deleted, dry-run
   walking the identical decision path.

2. **`MediaEntry.protected` is the device-functional-state flag.** Some
   files live among media but belong to the device (the DWARF's
   `Astronomy/CALI_FRAME` dark library — deleting it silently forces
   re-shooting darks). Stores mark them; the engine ALWAYS downloads
   them (they are photos — operator ruling 2026-07-16) and refuses to
   delete them unless the caller explicitly opts in
   (`delete_protected` / CLI `--delete-calibrations`, which still runs
   the verified-local-copy rule). Device system state (logs, firmware)
   is never even LISTED.

3. **Two stores ship now**: `FilesystemMediaStore` (any mounted card,
   parameterized by a declarative `CardLayout`; DWARF's layout is the
   first — detection by album-dir SIGNATURE, never by volume label) and
   `DwarfAlbumMediaStore` (the Wi-Fi album: REST index, streamed HTTP
   fetch, `/album/delete`). A PTP card store (libgphoto2 folder walk +
   `file_get`/`file_delete`) is the named next adapter; the engine is
   ready for it unchanged.

4. **Detection is HARDWARE-IDENTITY-ONLY (2026-07-16 live incident +
   operator ruling).** An operator's external DRIVE carrying `astronomy/`
   and `videos/` folders matched a folder signature (macOS filesystems
   are case-insensitive) and was OFFERED as a `--delete` target. The root
   cause is structural: content can never distinguish the device from a
   copy/backup of it — a copy is identical by definition. So folder
   contents are NEVER consulted for detection. `find_card_volumes()`
   returns a volume only when its `diskutil` identity matches
   `CardLayout.device_media_names` on removable-USB backing (the DWARF
   presents `MediaName = "File-Stor Gadget"`, measured). A freshly
   formatted (empty) camera card IS detected; a drive with camera-shaped
   folders is NOT. `deletion_guard()` refuses deletion — dry runs and
   explicit `--source` included — on any volume that fails the identity
   check; copy-only import from a non-matching volume proceeds (backup
   restore is legitimate) with a loud note. Unknown identity (diskutil
   failure, non-macOS) counts as NOT the device: fail-safe. A card in a
   USB READER presents the reader's identity and refuses deletion. HONEST
   LIMIT: `MediaName` is the Linux mass-storage gadget's own string, not a
   per-unit attestation — it excludes every ordinary drive but another
   Linux gadget device could present it; the deletion prompt surfaces the
   volume UUID so the operator confirms the physical volume.

5. **Device/host-supplied PATHS are contained at a single choke point
   (2026-07-16 adversarial review).** After the identity gate, the engine
   still moved bytes by strings the device/host supplied — an album
   `filePath` with `..` could overwrite arbitrary local files (P0), and a
   symlinked media root could make copy/delete escape the card (P1).
   Rules now: (a) `sanitize_relpath` strips `..`/absolute/`.` components
   from every device-supplied path; (b) `contained_path` re-resolves each
   local target and refuses anything that escapes `dest`; (c)
   `FilesystemMediaStore` skips symlinked media roots and symlinked
   subdirs/files, copies with `follow_symlinks=False`, and re-proves each
   `delete()`/prune target inside the volume's realpath (the ADAPTER is
   safe by construction, not only via the engine); (d) two device entries
   mapping to one local relpath are never deleted (the second file's
   bytes would be lost); (e) `dest` inside the source is refused;
   (f) unknown-size entries are budgeted pessimistically against the
   free-space floor.

4. **One CLI verb**: `abstractcamera download [--source PATH | --host IP]
   [--dest PATH] [--delete] [--dry-run]` — auto-detects mounted cards,
   defaults the destination to the family capture layout
   (`~/Pictures/<device_slug>/`).

## Consequences

- Sizes, not mtimes, are the comparison truth (measured: the DWARF's
  clock stamps card files with year-2038 dates; exFAT mtimes there are
  decoration).
- A store without trustworthy sizes still syncs (files copy; they are
  just excluded from deletion — honesty over convenience).
- The engine is transport-agnostic but NOT concurrency-managed: one sync
  per device at a time is the caller's responsibility (same posture as
  the manager's single worker).
- Deleting between listing and deletion is safe against device-side
  changes: deletion re-verifies the LOCAL copy at delete time and the
  device file is addressed by the listing's own reference, so a vanished
  file surfaces as an honest per-file failure, never a wrong deletion.
