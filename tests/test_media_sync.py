"""Device-independent media download (`abstractcamera download`): the sync
engine's safety rules against BOTH shipped stores — a synthetic mounted
card (FilesystemMediaStore) and a scripted Wi-Fi album (protocol shape of
DwarfAlbumMediaStore). The rules live in the ENGINE, once: incremental
size-verified copies, verify-before-delete, protected entries never
deleted, space checks, dry-run parity."""

import os
import tempfile
import unittest
from unittest import mock

from abstractcamera import media_store, media_sync
from abstractcamera.cli import main as cli_main
from abstractcamera.media_store import (DwarfAlbumMediaStore,
                                        FilesystemMediaStore, MediaEntry,
                                        find_card_volumes)


GADGET_IDENTITY = {"media_name": "File-Stor Gadget", "removable": True,
                   "bus_protocol": "USB"}
# The 2026-07-16 incident volume (/Volumes/X): a USB DRIVE with album-like
# folders — diskutil reported an EMPTY MediaName and Removable=False.
DRIVE_IDENTITY = {"media_name": "", "removable": False, "bus_protocol": "USB"}


def gadget_identity(_path):
    return dict(GADGET_IDENTITY)


def drive_identity(_path):
    return dict(DRIVE_IDENTITY)


def write(path: str, payload: bytes = b"x" * 64) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def build_card(root: str) -> dict[str, str]:
    """A realistic DWARF card: photos + thumbnails, a FITS session, the
    dark library, a video, device logs, macOS droppings."""
    return {
        "photo": write(os.path.join(root, "Normal_Photos",
                                    "DWARF3_TELE_2026-07-05-21-43-29-008.jpg"),
                       b"\xff\xd8" + b"j" * 500),
        "thumb": write(os.path.join(root, "Normal_Photos", "Thumbnail",
                                    "DWARF3_TELE_2026-07-05-21-43-29-008.jpg"),
                       b"\xff\xd8" + b"t" * 40),
        "fits": write(os.path.join(root, "Astronomy",
                                   "DWARF_RAW_TELE_M 101_EXP_30_GAIN_100_2026",
                                   "ok_M 101_30s100_VIS_20260709_35C.fits"),
                      b"SIMPLE" + b"f" * 900),
        "dark": write(os.path.join(root, "Astronomy", "CALI_FRAME",
                                   "dark_30s_gain100.fits"),
                      b"SIMPLE" + b"d" * 300),
        "video": write(os.path.join(root, "Videos",
                                    "DWARF3_TELE_2026-07-04-21-41-11-003.mp4"),
                       b"\x00\x00\x00\x18ftyp" + b"v" * 2000),
        "syslog": write(os.path.join(root, ".log", "storage0000", "boot.log"),
                        b"device log"),
        "sysvol": write(os.path.join(root, "System Volume Information",
                                     "IndexerVolumeGuid"), b"guid"),
        "dsstore": write(os.path.join(root, "Normal_Photos", ".DS_Store"),
                         b"finder"),
        "appledouble": write(os.path.join(root, "Videos",
                                          "._DWARF3_TELE_2026-07-04.mp4"),
                             b"resource fork"),
    }


class CardDetection(unittest.TestCase):
    """DETECTION IS HARDWARE-ONLY (operator ruling 2026-07-16): folder
    names are never evidence of a camera."""

    def test_detection_is_hardware_identity_only(self):
        volumes = tempfile.mkdtemp(prefix="volumes_")
        card = os.path.join(volumes, "U盘")
        build_card(card)

        def per_volume_identity(path):
            return gadget_identity(path) if path == card else drive_identity(path)

        os.makedirs(os.path.join(volumes, "Backup", "Normal_Photos"))
        os.makedirs(os.path.join(volumes, "Macintosh HD", "Applications"))
        matches = find_card_volumes(volumes, identity_fn=per_volume_identity)
        self.assertEqual([(card, media_store.DWARF_CARD_LAYOUT)], matches)

    def test_empty_camera_card_is_still_detected(self):
        """A freshly formatted card IS the camera's storage — hardware
        says so; it merely holds no media yet. Folder absence must not
        hide the device."""
        volumes = tempfile.mkdtemp(prefix="volumes_")
        os.makedirs(os.path.join(volumes, "NO NAME"))
        matches = find_card_volumes(volumes, identity_fn=gadget_identity)
        self.assertEqual([(os.path.join(volumes, "NO NAME"),
                           media_store.DWARF_CARD_LAYOUT)], matches)

    def test_backup_drive_with_album_folders_is_never_detected(self):
        """THE 2026-07-16 INCIDENT: an operator's external DRIVE carrying
        album-shaped folders matched the content signature and was offered
        as a --delete target. Content cannot distinguish a copy from the
        device — hardware identity must, so the drive is EXCLUDED."""
        volumes = tempfile.mkdtemp(prefix="volumes_")
        build_card(os.path.join(volumes, "X"))  # a backup ON a drive
        self.assertEqual(
            find_card_volumes(volumes, identity_fn=drive_identity), [])

    def test_unknown_identity_is_excluded_fail_safe(self):
        volumes = tempfile.mkdtemp(prefix="volumes_")
        build_card(os.path.join(volumes, "U盘"))
        self.assertEqual(
            find_card_volumes(volumes, identity_fn=lambda _p: None), [])

    def test_identity_matcher_semantics(self):
        layout = media_store.DWARF_CARD_LAYOUT
        self.assertTrue(media_store.device_identity_matches(
            layout, GADGET_IDENTITY))
        self.assertFalse(media_store.device_identity_matches(
            layout, DRIVE_IDENTITY))
        self.assertFalse(media_store.device_identity_matches(layout, None))
        # A layout WITHOUT a declared hardware identity can never match:
        # it is copy-import-only and never authorizes deletion (fail-safe).
        no_identity = media_store.CardLayout(slug="x", media_dirs=("A",))
        self.assertFalse(media_store.device_identity_matches(
            no_identity, None))
        self.assertFalse(media_store.device_identity_matches(
            no_identity, GADGET_IDENTITY))

    def test_missing_volumes_root_is_empty(self):
        self.assertEqual(find_card_volumes("/nonexistent/volumes"), [])


class EngineWithCardStore(unittest.TestCase):
    def setUp(self):
        self.card = tempfile.mkdtemp(prefix="dwarf_card_")
        self.dest = tempfile.mkdtemp(prefix="dwarf_local_")
        self.files = build_card(self.card)
        self.store = FilesystemMediaStore(self.card,
                                          identity_fn=gadget_identity)

    def _sync(self, **kwargs):
        return media_sync.sync_store(self.store, self.dest,
                                     log=lambda *_: None, **kwargs)

    def _relpath(self, key: str) -> str:
        return os.path.relpath(self.files[key], self.card)

    def test_copies_all_media_and_only_media(self):
        report = self._sync()
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.copied, 5, "photo+thumb+fits+dark+video")
        for key in ("photo", "thumb", "fits", "dark", "video"):
            local = os.path.join(self.dest, self._relpath(key))
            self.assertEqual(os.path.getsize(local),
                             os.path.getsize(self.files[key]), key)
        self.assertFalse(os.path.exists(os.path.join(self.dest, ".log")))
        self.assertFalse(os.path.exists(
            os.path.join(self.dest, "System Volume Information")))
        self.assertFalse(os.path.exists(
            os.path.join(self.dest, "Normal_Photos", ".DS_Store")))
        self.assertFalse(any(name.startswith("._") for name in
                             os.listdir(os.path.join(self.dest, "Videos"))))

    def test_rerun_is_idempotent(self):
        self._sync()
        report = self._sync()
        self.assertEqual((report.copied, report.skipped), (0, 5))

    def test_changed_size_recopies(self):
        self._sync()
        write(self.files["photo"], b"\xff\xd8" + b"J" * 900)
        report = self._sync()
        self.assertEqual(report.copied, 1)
        self.assertEqual(
            os.path.getsize(os.path.join(self.dest, self._relpath("photo"))), 902)

    def test_dry_run_touches_nothing(self):
        report = self._sync(dry_run=True)
        self.assertEqual(report.copied, 5)
        self.assertEqual(os.listdir(self.dest), [])

    def test_non_card_source_refuses(self):
        store = FilesystemMediaStore(tempfile.mkdtemp(prefix="not_a_card_"),
                                     identity_fn=drive_identity)
        report = media_sync.sync_store(store, self.dest, log=lambda *_: None)
        self.assertFalse(report.ok)
        self.assertIn("nothing to import", report.failures[0])

    def test_empty_device_card_syncs_honestly(self):
        """Hardware-proven device with no media: valid source, zero files."""
        store = FilesystemMediaStore(tempfile.mkdtemp(prefix="fresh_card_"),
                                     identity_fn=gadget_identity)
        report = media_sync.sync_store(store, self.dest, log=lambda *_: None)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual((report.copied, report.skipped), (0, 0))

    def test_delete_from_a_backup_drive_refuses_copy_still_works(self):
        """The deletion guard closes the incident for EXPLICIT --source
        too: a backup on a drive copies fine, deletes never — and the dry
        run refuses identically (the plan must not promise a deletion the
        real run would refuse)."""
        drive_store = FilesystemMediaStore(self.card,
                                           identity_fn=drive_identity)
        report = media_sync.sync_store(drive_store, self.dest,
                                       log=lambda *_: None)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.copied, 5, "copying a backup is legitimate")

        for dry_run in (True, False):
            report = media_sync.sync_store(drive_store, self.dest,
                                           delete=True, dry_run=dry_run,
                                           log=lambda *_: None)
            self.assertFalse(report.ok)
            self.assertEqual(report.deleted, 0)
            self.assertIn("deletion refused", report.failures[0])
            self.assertIn("backup COPY", report.failures[0])
        for key in ("photo", "thumb", "fits", "dark", "video"):
            self.assertTrue(os.path.exists(self.files[key]), key)

    def test_insufficient_space_refuses_before_copying(self):
        fake = mock.Mock(free=10)
        with mock.patch.object(media_sync.shutil, "disk_usage", return_value=fake):
            report = self._sync()
        self.assertFalse(report.ok)
        self.assertIn("not enough space", report.failures[0])
        self.assertEqual(report.copied, 0)

    def test_delete_removes_verified_media_only(self):
        report = self._sync(delete=True)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.deleted, 4, "photo+thumb+fits+video")
        for key in ("photo", "thumb", "fits", "video"):
            self.assertFalse(os.path.exists(self.files[key]), key)
        # The dark library is device-functional state: copied AND kept.
        self.assertTrue(os.path.exists(self.files["dark"]))
        self.assertEqual(report.protected, 1)
        self.assertTrue(os.path.exists(self.files["syslog"]))
        self.assertTrue(os.path.exists(self.files["sysvol"]))

    def test_emptied_session_folders_prune_but_roots_stay(self):
        self._sync(delete=True)
        self.assertFalse(os.path.isdir(os.path.join(
            self.card, "Astronomy", "DWARF_RAW_TELE_M 101_EXP_30_GAIN_100_2026")))
        self.assertFalse(os.path.isdir(os.path.join(
            self.card, "Normal_Photos", "Thumbnail")))
        for media in ("Normal_Photos", "Astronomy", "Videos"):
            self.assertTrue(os.path.isdir(os.path.join(self.card, media)), media)
        self.assertTrue(os.path.isdir(os.path.join(self.card, "Astronomy",
                                                   "CALI_FRAME")))

    def test_unverified_local_copy_protects_the_device_file(self):
        self._sync()
        local_photo = os.path.join(self.dest, self._relpath("photo"))
        real_getsize = os.path.getsize

        def lying_getsize(path):
            if os.path.abspath(path) == os.path.abspath(local_photo):
                return 1  # the local copy "shrank" between copy and delete
            return real_getsize(path)

        with mock.patch.object(media_sync.os.path, "getsize",
                               side_effect=lying_getsize):
            report = self._sync(delete=True)
        self.assertTrue(os.path.exists(self.files["photo"]),
                        "no verified local copy => the device file stays")
        self.assertTrue(any("kept on device" in f for f in report.failures))

    def test_dry_run_delete_touches_nothing(self):
        self._sync()
        report = self._sync(delete=True, dry_run=True)
        self.assertEqual(report.deleted, 4)
        for key in ("photo", "thumb", "fits", "video"):
            self.assertTrue(os.path.exists(self.files[key]), key)

    def test_delete_calibrations_extends_delete_verified(self):
        """Explicit opt-in removes the calibration library too — but only
        entries whose local copy verifies (same rule as all deletions)."""
        report = self._sync(delete=True, delete_protected=True)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.deleted, 5, "media + the dark library")
        self.assertEqual(report.deleted_protected, 1)
        self.assertEqual(report.protected, 0)
        self.assertFalse(os.path.exists(self.files["dark"]))
        self.assertTrue(os.path.exists(os.path.join(
            self.dest, self._relpath("dark"))),
            "the local calibration backup is the precondition")
        # System state still untouched, even with the widest flags.
        self.assertTrue(os.path.exists(self.files["syslog"]))
        self.assertTrue(os.path.exists(self.files["sysvol"]))

    def test_delete_calibrations_still_requires_local_verification(self):
        self._sync()
        local_dark = os.path.join(self.dest, self._relpath("dark"))
        real_getsize = os.path.getsize

        def lying_getsize(path):
            if os.path.abspath(path) == os.path.abspath(local_dark):
                return 1
            return real_getsize(path)

        with mock.patch.object(media_sync.os.path, "getsize",
                               side_effect=lying_getsize):
            report = self._sync(delete=True, delete_protected=True)
        self.assertTrue(os.path.exists(self.files["dark"]),
                        "unverified calibration backup => card copy stays")
        self.assertTrue(any("dark" in f for f in report.failures))


class FakeAlbumTransport:
    """Scripted DwarfTransport album surface (list/fetch/delete)."""

    def __init__(self):
        self.items: list[dict] = []
        self.payloads: dict[str, bytes] = {}
        self.deleted: list[dict] = []

    def add(self, file_path: str, payload: bytes, size_text: str | None = None,
            media_type: int = 1):
        name = file_path.rsplit("/", 1)[-1]
        self.items.append({
            "fileName": name, "filePath": file_path, "mediaType": media_type,
            "fileSize": size_text if size_text is not None else str(len(payload)),
            "modificationTime": 1780000000,
        })
        self.payloads[file_path] = payload

    def album_media_infos(self, *, media_type=0, page_index=0, page_size=8):
        return list(self.items)

    def fetch_media_to(self, file_path: str, local_path: str) -> None:
        with open(local_path, "wb") as out:
            out.write(self.payloads[file_path])

    def album_delete(self, items: list[dict]) -> None:
        self.deleted.extend(items)
        for item in items:
            path = item["filePath"]
            self.items = [i for i in self.items if i["filePath"] != path]
            self.payloads.pop(path, None)


class EngineWithAlbumStore(unittest.TestCase):
    def setUp(self):
        self.transport = FakeAlbumTransport()
        self.transport.add("/sdcard/DWARF_3/Normal_Photos/DWARF3_A.jpg",
                           b"\xff\xd8" + b"a" * 700)
        self.transport.add(
            "/sdcard/DWARF_3/Astronomy/SESSION_M42/ok_M42_30s.fits",
            b"SIMPLE" + b"s" * 500)
        self.transport.add("/sdcard/DWARF_3/Astronomy/CALI_FRAME/dark.fits",
                           b"SIMPLE" + b"d" * 200)
        self.store = DwarfAlbumMediaStore("198.51.100.23",
                                          transport=self.transport)
        self.dest = tempfile.mkdtemp(prefix="dwarf_album_local_")

    def _sync(self, **kwargs):
        return media_sync.sync_store(self.store, self.dest,
                                     log=lambda *_: None, **kwargs)

    def test_album_paths_mirror_the_card_layout(self):
        report = self._sync()
        self.assertTrue(report.ok, report.failures)
        self.assertTrue(os.path.exists(os.path.join(
            self.dest, "Normal_Photos", "DWARF3_A.jpg")))
        self.assertTrue(os.path.exists(os.path.join(
            self.dest, "Astronomy", "SESSION_M42", "ok_M42_30s.fits")))

    def test_display_size_strings_parse_and_verify(self):
        # The album reports sizes as display strings; 0.5KB == 512 bytes.
        self.transport.add("/sdcard/DWARF_3/Videos/clip.mp4", b"v" * 512,
                           size_text="0.5 KB", media_type=2)
        report = self._sync()
        self.assertTrue(report.ok, report.failures)
        report = self._sync()
        self.assertEqual(report.copied, 0, "size-string files must verify too")

    def test_delete_spares_the_dark_library_and_uses_the_album_api(self):
        report = self._sync(delete=True)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.deleted, 2)
        self.assertEqual(report.protected, 1)
        deleted_paths = {item["filePath"] for item in self.transport.deleted}
        self.assertNotIn("/sdcard/DWARF_3/Astronomy/CALI_FRAME/dark.fits",
                         deleted_paths)
        self.assertEqual(len(self.transport.deleted), 2)
        self.assertIn("fileName", self.transport.deleted[0],
                      "album delete needs the listing's own addressing")

    def test_unparseable_size_copies_but_never_deletes(self):
        self.transport.add("/sdcard/DWARF_3/Videos/odd.mp4", b"o" * 100,
                           size_text="unknown", media_type=2)
        report = self._sync(delete=True)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Videos",
                                                    "odd.mp4")))
        self.assertTrue(any("odd.mp4" in f for f in report.failures),
                        "unverifiable file must be kept on device, loudly")
        remaining = {item["filePath"] for item in self.transport.items}
        self.assertIn("/sdcard/DWARF_3/Videos/odd.mp4", remaining)


class CliSurface(unittest.TestCase):
    def test_cli_download_end_to_end_dry_run(self):
        card = tempfile.mkdtemp(prefix="dwarf_card_")
        dest = tempfile.mkdtemp(prefix="dwarf_local_")
        build_card(card)
        with mock.patch.object(media_store, "_volume_device_identity",
                               gadget_identity):
            exit_code = cli_main(["download", "--source", card, "--dest", dest,
                                  "--delete", "--dry-run"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(os.listdir(dest), [])

    def test_copy_from_non_device_volume_warns(self):
        card = tempfile.mkdtemp(prefix="dwarf_card_")
        dest = tempfile.mkdtemp(prefix="dwarf_local_")
        build_card(card)
        with mock.patch.object(media_store, "_volume_device_identity",
                               drive_identity):
            lines: list[str] = []
            exit_code = media_sync.run_cli(
                mock.Mock(source=card, host=None, dest=dest, delete=False,
                          delete_calibrations=False, dry_run=True),
                log=lines.append)
        self.assertEqual(exit_code, 0, "copying stays allowed")
        self.assertTrue(any("NOT the device's own storage" in line
                            for line in lines),
                        "the copy plan must announce the identity mismatch")

    def test_delete_calibrations_without_delete_refuses(self):
        card = tempfile.mkdtemp(prefix="dwarf_card_")
        build_card(card)
        lines: list[str] = []
        exit_code = media_sync.run_cli(
            mock.Mock(source=card, host=None, dest=None, delete=False,
                      delete_calibrations=True, dry_run=False),
            log=lines.append)
        self.assertEqual(exit_code, 1)
        self.assertIn("--delete", lines[0])

    def test_cli_delete_calibrations_flag_reaches_the_engine(self):
        card = tempfile.mkdtemp(prefix="dwarf_card_")
        dest = tempfile.mkdtemp(prefix="dwarf_local_")
        files = build_card(card)
        with mock.patch.object(media_store, "_volume_device_identity",
                               gadget_identity):
            exit_code = cli_main(["download", "--source", card, "--dest", dest,
                                  "--delete", "--delete-calibrations"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(os.path.exists(files["dark"]),
                         "the calibration library deletes with the flag")
        self.assertTrue(os.path.exists(files["syslog"]))

    def test_all_present_outcome_says_so(self):
        """`copied: 0` alone reads like a failure — the summary must state
        that everything is already downloaded and WHERE (operator
        confusion, 2026-07-16)."""
        card = tempfile.mkdtemp(prefix="dwarf_card_")
        dest = tempfile.mkdtemp(prefix="dwarf_local_")
        build_card(card)
        media_sync.run_cli(mock.Mock(source=card, host=None, dest=dest,
                                     delete=False, delete_calibrations=False,
                                     dry_run=False),
                           log=lambda *_: None)
        lines: list[str] = []
        exit_code = media_sync.run_cli(
            mock.Mock(source=card, host=None, dest=dest, delete=False,
                      delete_calibrations=False, dry_run=False),
            log=lines.append)
        self.assertEqual(exit_code, 0)
        summary = "\n".join(lines)
        self.assertIn("Nothing new on the device", summary)
        self.assertIn(dest, summary)
        self.assertIn("hierarchy preserved", summary)
        self.assertIn("device media: 5 files", summary,
                      "the plan line must announce inventory + new counts")

    def test_multiple_cards_point_at_list_and_source(self):
        volumes = tempfile.mkdtemp(prefix="volumes_")
        build_card(os.path.join(volumes, "CARD_A"))
        build_card(os.path.join(volumes, "CARD_B"))
        with mock.patch("abstractcamera.media_store.find_card_volumes",
                        return_value=find_card_volumes(
                            volumes, identity_fn=gadget_identity)):
            lines: list[str] = []
            exit_code = media_sync.run_cli(
                mock.Mock(source=None, host=None, dest=None, delete=False,
                          delete_calibrations=False, dry_run=False),
                log=lines.append)
        self.assertEqual(exit_code, 1)
        summary = "\n".join(lines)
        self.assertIn("--source", summary)
        self.assertIn("abstractcamera list", summary)
        self.assertIn("CARD_A", summary)
        self.assertIn("CARD_B", summary)

    def test_cli_list_shows_media_sources(self):
        volumes = tempfile.mkdtemp(prefix="volumes_")
        card = os.path.join(volumes, "U盘")
        build_card(card)
        import io
        from contextlib import redirect_stdout

        # The CLI resolves both names from the package top level at call
        # time — patch THOSE bindings, not the defining modules.
        with mock.patch("abstractcamera.find_card_volumes",
                        return_value=find_card_volumes(
                            volumes, identity_fn=gadget_identity)) as finder:
            with mock.patch("abstractcamera.list_cameras", return_value=[]):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = cli_main(["list"])
        finder.assert_called()
        self.assertEqual(exit_code, 0, "a mounted card alone is a listable device")
        output = buffer.getvalue()
        self.assertIn("media sources", output)
        self.assertIn(card, output)
        self.assertIn("dwarf_3 card", output)

    def test_cli_without_any_device_is_actionable(self):
        with mock.patch.object(media_sync, "run_cli", wraps=media_sync.run_cli):
            with mock.patch("abstractcamera.media_store.find_card_volumes",
                            return_value=[]):
                lines: list[str] = []
                exit_code = media_sync.run_cli(
                    mock.Mock(source=None, host=None, dest=None, delete=False,
                              delete_calibrations=False, dry_run=False),
                    log=lines.append)
        self.assertEqual(exit_code, 1)
        self.assertIn("--host", lines[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
