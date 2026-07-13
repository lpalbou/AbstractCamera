"""Canonical wire vocabulary for camera sessions (ADR 0001).

Every CameraSession implementation — the real `gphoto2` module's Camera, the
package simulator (abstractcamera.sim.gphoto2), and the webcam session —
speaks these constants and value shapes. The numeric values are PINNED to
libgphoto2's, which is what makes the three transports interchangeable
without translation (the simulator has relied on this exact equivalence
since its first version).

Evolution rule: ADDITIVE ONLY. Existing names and values never change;
SESSION_PROTOCOL_VERSION bumps when new protocol surface is added.
"""

from __future__ import annotations

SESSION_PROTOCOL_VERSION = 1

# --- event types (libgphoto2 CameraEventType values) -----------------------
GP_EVENT_UNKNOWN = 0
GP_EVENT_TIMEOUT = 1
GP_EVENT_FILE_ADDED = 2
GP_EVENT_FOLDER_ADDED = 3
GP_EVENT_CAPTURE_COMPLETE = 4

# --- widget types (libgphoto2 CameraWidgetType values) ---------------------
GP_WIDGET_WINDOW = 0
GP_WIDGET_SECTION = 1
GP_WIDGET_TEXT = 2
GP_WIDGET_RANGE = 3
GP_WIDGET_TOGGLE = 4
GP_WIDGET_RADIO = 5
GP_WIDGET_MENU = 6
GP_WIDGET_BUTTON = 7
GP_WIDGET_DATE = 8

# --- file types -------------------------------------------------------------
GP_FILE_TYPE_NORMAL = 1


class EventData:
    """FILE_ADDED payload shape: what the manager's announce path reads."""

    __slots__ = ("folder", "name")

    def __init__(self, folder: str, name: str):
        self.folder = folder
        self.name = name


class Abilities:
    """get_abilities() result shape (.model drives adapter selection)."""

    __slots__ = ("model",)

    def __init__(self, model: str):
        self.model = model


class BytesFile:
    """In-memory CameraFile: bytes captured by a session that already holds
    them locally (simulator frames, webcam stills)."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes):
        self._data = data

    def get_data_and_size(self) -> bytes:
        return self._data

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self._data)


class FileBackedFile:
    """Disk-backed CameraFile: the payload already lives in a local temp file
    (webcam movies). save() is a move, so a finished MP4 is never buffered
    in RAM. The manager only calls save() on file_get results, which makes
    this protocol-honest (see CameraSession contract notes)."""

    __slots__ = ("_path",)

    def __init__(self, path: str):
        self._path = path

    def get_data_and_size(self) -> bytes:
        with open(self._path, "rb") as fh:
            return fh.read()

    def save(self, path: str) -> None:
        import os
        import shutil

        try:
            os.replace(self._path, path)
        except OSError:
            # Cross-device capture dir: degrade to copy+remove.
            shutil.copy2(self._path, path)
            os.unlink(self._path)


class ProtocolWidget:
    """Generic single-config widget for non-gphoto2 sessions (the webcam's
    `imagesize`). Mirrors the simulator's widget surface — the adapters'
    `_read_entry` consumes exactly these methods."""

    __slots__ = ("name", "wtype", "value", "choices", "wrange", "readonly")

    def __init__(self, name, wtype, value, choices=None, wrange=None, readonly=False):
        self.name = name
        self.wtype = wtype
        self.value = value
        self.choices = list(choices or [])
        self.wrange = wrange
        self.readonly = readonly

    def get_type(self):
        return self.wtype

    def get_value(self):
        return self.value

    def set_value(self, value):
        if self.wtype == GP_WIDGET_RANGE:
            self.value = float(value)
        elif self.wtype == GP_WIDGET_TOGGLE:
            self.value = int(value)
        else:
            self.value = str(value)

    def get_readonly(self):
        return 1 if self.readonly else 0

    def count_choices(self):
        return len(self.choices)

    def get_choice(self, index):
        return self.choices[index]

    def get_range(self):
        return tuple(self.wrange)
