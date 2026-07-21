"""Adversarial hardening regressions (2026-07-16 review): every finding
that let a device/host-supplied path escape its tree — or that could lose
the camera's OWN data — is pinned closed here. These tests are the reason
the containment code exists; they must never be weakened."""

import os
import tempfile
import unittest
from unittest import mock

from abstractcamera import media_store, media_sync
from abstractcamera.media_store import (DwarfAlbumMediaStore,
                                        FilesystemMediaStore, MediaEntry,
                                        contained_path, sanitize_relpath)

GADGET = {"media_name": "File-Stor Gadget", "removable": True,
          "bus_protocol": "USB", "volume_uuid": "TEST-UUID-0001"}
DRIVE = {"media_name": "", "removable": False, "bus_protocol": "USB"}


def gadget_identity(_path):
    return dict(GADGET)


def write(path: str, payload: bytes = b"x" * 64) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


class RelpathSanitization(unittest.TestCase):
    def test_traversal_and_absolute_components_are_stripped(self):
        self.assertEqual(sanitize_relpath("Normal_Photos/a.jpg"),
                         os.path.join("Normal_Photos", "a.jpg"))
        self.assertEqual(sanitize_relpath("../../../etc/passwd"),
                         os.path.join("etc", "passwd"))
        self.assertEqual(sanitize_relpath("/etc/shadow"),
                         os.path.join("etc", "shadow"))
        self.assertEqual(sanitize_relpath("a/../../b"), os.path.join("a", "b"))
        self.assertEqual(sanitize_relpath("weird\\..\\win"), os.path.join("weird", "win"))
        for empty in ("", "..", "../..", "/", ".", "///"):
            self.assertIsNone(sanitize_relpath(empty), repr(empty))

    def test_contained_path_keeps_everything_inside_root(self):
        root = tempfile.mkdtemp()
        root_real = os.path.realpath(root)
        # A normal path resolves inside root.
        inside = contained_path(root, "Videos/clip.mp4")
        self.assertTrue(inside.startswith(root_real + os.sep))
        # A traversal path has its .. stripped and STILL resolves inside
        # root (safe: the file lands in-root under a cleaned name, never
        # outside) — the containment invariant is "never escapes", which
        # holds for every input.
        neutralized = contained_path(root, "../escape.txt")
        self.assertTrue(neutralized.startswith(root_real + os.sep),
                        "a .. path must resolve INSIDE root, never outside")
        # Nothing-safe-left inputs return None (the caller skips them).
        self.assertIsNone(contained_path(root, "../.."))


class AlbumPathTraversal(unittest.TestCase):
    """P0: a malicious/buggy DWARF host must never write outside dest."""

    class HostileTransport:
        def __init__(self, items):
            self._items = items

        def album_media_infos(self, **_):
            return self._items

        def fetch_media_to(self, file_path, local_path):
            with open(local_path, "wb") as fh:
                fh.write(b"HOSTILE")

    def test_dotdot_filepath_cannot_escape_dest(self):
        dest = tempfile.mkdtemp(prefix="dest_")
        sentinel_dir = tempfile.mkdtemp(prefix="outside_")
        # filePath climbs out; fileName carries slashes too.
        items = [
            {"fileName": "x.jpg",
             "filePath": "/sdcard/Normal_Photos/../../../../../../../../"
                         + sentinel_dir.lstrip("/") + "/PWNED.txt",
             "fileSize": "7", "mediaType": 1},
            {"fileName": "../../PWNED2.txt",
             "filePath": "/sdcard/weird/../../PWNED2.txt",
             "fileSize": "7", "mediaType": 1},
        ]
        store = DwarfAlbumMediaStore("198.51.100.7",
                                     transport=self.HostileTransport(items))
        report = media_sync.sync_store(store, dest, log=lambda *_: None)
        # Everything that got written stays UNDER dest.
        for root, _dirs, files in os.walk(dest):
            for name in files:
                full = os.path.realpath(os.path.join(root, name))
                self.assertTrue(full.startswith(os.path.realpath(dest) + os.sep),
                                f"escaped dest: {full}")
        self.assertFalse(os.path.exists(os.path.join(sentinel_dir, "PWNED.txt")),
                         "the .. path must never reach outside dest")
        self.assertEqual(os.listdir(sentinel_dir), [])


class SymlinkedMediaRootEscape(unittest.TestCase):
    """P1: a symlinked media dir must not let copy/delete reach outside
    the card — even when the card root reports the camera identity."""

    def _card_with_symlinked_media(self):
        card = tempfile.mkdtemp(prefix="card_")
        victim = tempfile.mkdtemp(prefix="victim_")
        write(os.path.join(victim, "precious.jpg"), b"IRREPLACEABLE")
        write(os.path.join(victim, "sub", "precious2.jpg"), b"ALSO")
        os.symlink(victim, os.path.join(card, "Normal_Photos"))
        # A legitimate on-card file so the run has something real to do.
        write(os.path.join(card, "Videos", "clip.mp4"), b"oncard")
        return card, victim

    def test_copy_does_not_follow_symlinked_media_root(self):
        card, victim = self._card_with_symlinked_media()
        dest = tempfile.mkdtemp(prefix="dest_")
        store = FilesystemMediaStore(card, identity_fn=gadget_identity)
        report = media_sync.sync_store(store, dest, log=lambda *_: None)
        self.assertTrue(report.ok, report.failures)
        # The victim tree was never read into dest.
        self.assertFalse(os.path.exists(os.path.join(dest, "Normal_Photos")))
        self.assertTrue(os.path.exists(os.path.join(dest, "Videos", "clip.mp4")))

    def test_delete_never_touches_files_outside_the_card(self):
        card, victim = self._card_with_symlinked_media()
        dest = tempfile.mkdtemp(prefix="dest_")
        store = FilesystemMediaStore(card, identity_fn=gadget_identity)
        media_sync.sync_store(store, dest, delete=True, log=lambda *_: None)
        # The victim files and their directory MUST survive intact.
        self.assertTrue(os.path.exists(os.path.join(victim, "precious.jpg")))
        self.assertTrue(os.path.exists(os.path.join(victim, "sub", "precious2.jpg")))
        self.assertTrue(os.path.isdir(os.path.join(victim, "sub")))

    def test_adapter_delete_self_guards_against_outside_ref(self):
        """Even a direct store.delete() with a doctored ref pointing
        outside the card refuses (the adapter is safe by construction)."""
        card = tempfile.mkdtemp(prefix="card_")
        outside = write(os.path.join(tempfile.mkdtemp(prefix="out_"), "keep.txt"))
        store = FilesystemMediaStore(card, identity_fn=gadget_identity)
        with self.assertRaises(OSError):
            store.delete(MediaEntry(relpath="x", size=1, ref=outside))
        self.assertTrue(os.path.exists(outside))


class IdentitySpoofSurface(unittest.TestCase):
    def test_only_removable_usb_camera_string_matches(self):
        layout = media_store.DWARF_CARD_LAYOUT
        self.assertTrue(media_store.device_identity_matches(layout, GADGET))
        # Same name but NOT removable (an internal volume): rejected.
        self.assertFalse(media_store.device_identity_matches(
            layout, {"media_name": "File-Stor Gadget", "removable": False,
                     "bus_protocol": "USB"}))
        # Same name, removable, but a non-USB bus: rejected.
        self.assertFalse(media_store.device_identity_matches(
            layout, {"media_name": "File-Stor Gadget", "removable": True,
                     "bus_protocol": "SATA"}))
        # A real external drive: rejected.
        self.assertFalse(media_store.device_identity_matches(layout, DRIVE))

    def test_deletion_target_id_surfaces_the_volume_uuid(self):
        card = tempfile.mkdtemp(prefix="card_")
        store = FilesystemMediaStore(card, identity_fn=gadget_identity)
        self.assertIn("TEST-UUID-0001", store.deletion_target_id())


class RelpathCollisionDataLoss(unittest.TestCase):
    """P2: two device files mapping to one local path must never both be
    deleted (the second file's bytes would be lost)."""

    class Transport:
        def __init__(self):
            self.items = [
                {"fileName": "dup.jpg",
                 "filePath": "/sdcard/Normal_Photos/dup.jpg",
                 "fileSize": "16", "mediaType": 1},
                {"fileName": "dup.jpg",
                 "filePath": "/sdcard/Normal_Photos/dup.jpg",
                 "fileSize": "16", "mediaType": 1},
            ]
            self.deleted = []

        def album_media_infos(self, **_):
            return list(self.items)

        def fetch_media_to(self, file_path, local_path):
            with open(local_path, "wb") as fh:
                fh.write(b"A" * 16)

        def album_delete(self, items):
            self.deleted.extend(items)

    def test_colliding_relpaths_are_never_deleted(self):
        dest = tempfile.mkdtemp(prefix="dest_")
        transport = self.Transport()
        store = DwarfAlbumMediaStore("198.51.100.9", transport=transport)
        report = media_sync.sync_store(store, dest, delete=True,
                                       log=lambda *_: None)
        self.assertEqual(report.deleted, 0, "ambiguous mapping = no deletion")
        self.assertEqual(transport.deleted, [])
        self.assertTrue(any("ambiguous" in f for f in report.failures))


class DestInsideSource(unittest.TestCase):
    def test_dest_inside_the_card_is_refused(self):
        card = tempfile.mkdtemp(prefix="card_")
        write(os.path.join(card, "Videos", "clip.mp4"))
        store = FilesystemMediaStore(card, identity_fn=gadget_identity)
        report = media_sync.sync_store(store, os.path.join(card, "mirror"),
                                       log=lambda *_: None)
        self.assertFalse(report.ok)
        self.assertIn("inside the source", report.failures[0])


class UnknownSizeBudget(unittest.TestCase):
    class Transport:
        def album_media_infos(self, **_):
            return [{"fileName": "big.mp4", "filePath": "/sdcard/Videos/big.mp4",
                     "fileSize": "unknown", "mediaType": 2}]

        def fetch_media_to(self, file_path, local_path):
            with open(local_path, "wb") as fh:
                fh.write(b"v")

    def test_unknown_size_is_budgeted_against_the_free_floor(self):
        dest = tempfile.mkdtemp(prefix="dest_")
        store = DwarfAlbumMediaStore("198.51.100.11", transport=self.Transport())
        fake = mock.Mock(free=100 * 1024 * 1024)  # 100MB free, < the pessimistic budget
        with mock.patch.object(media_sync.shutil, "disk_usage", return_value=fake):
            report = media_sync.sync_store(store, dest, log=lambda *_: None)
        self.assertFalse(report.ok)
        self.assertIn("not enough space", report.failures[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
