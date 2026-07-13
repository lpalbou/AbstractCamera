"""NULL-guarded widget reads for python-gphoto2.

The crash class (observed 2026-07-12, packaged app, Sony A7R IV connect):
`CameraWidget.get_value()` on a string-typed widget whose C value is NULL
runs `PyUnicode_FromString(NULL)` → `strlen(NULL)` → SIGSEGV. A Python
try/except cannot catch it — the process dies. Bodies CAN return NULL
string values transiently (mid-wake, menu open, priority handoffs), so
every string read from a real camera goes through this module: the value
pointer is fetched via ctypes straight from libgphoto2 and NULL-checked
BEFORE any Python string is built. NULL reads surface as None (callers
treat them as absent values — the config cache skips them honestly).

Non-swig widgets (the simulator's plain-Python widgets, the webcam
session's ProtocolWidget) take the normal `get_value()` path untouched.
"""

from __future__ import annotations

import ctypes
import glob
import os
import threading

from abstractcamera import wire

_STRING_WIDGET_TYPES = (wire.GP_WIDGET_TEXT, wire.GP_WIDGET_RADIO, wire.GP_WIDGET_MENU)
_lock = threading.Lock()
_lib: ctypes.CDLL | None = None
_lib_failed = False


def _libgphoto2() -> ctypes.CDLL | None:
    """The libgphoto2 already loaded into this process (the python-gphoto2
    wheel ships it in gphoto2/.dylibs on macOS; system lib elsewhere)."""
    global _lib, _lib_failed
    with _lock:
        if _lib is not None or _lib_failed:
            return _lib
        candidates: list[str] = []
        try:
            import gphoto2

            package_dir = os.path.dirname(gphoto2.__file__)
            candidates += glob.glob(os.path.join(package_dir, ".dylibs", "libgphoto2.*.dylib"))
            candidates += glob.glob(os.path.join(package_dir, "libgphoto2*.so*"))
        except Exception:
            pass
        candidates += ["libgphoto2.so.6", "libgphoto2.dylib"]
        for candidate in candidates:
            try:
                lib = ctypes.CDLL(candidate)
                lib.gp_widget_get_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                lib.gp_widget_get_value.restype = ctypes.c_int
                lib.gp_widget_get_choice.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                lib.gp_widget_get_choice.restype = ctypes.c_int
                _lib = lib
                return _lib
            except Exception:
                continue
        _lib_failed = True
        return None


def _swig_pointer(widget) -> int | None:
    this = getattr(widget, "this", None)
    if this is None:
        return None
    try:
        return int(this)
    except Exception:
        return None


def widget_value(widget):
    """`widget.get_value()` with the NULL trap removed for string types on
    real (swig) widgets. Returns None where the C value is NULL."""
    try:
        widget_type = widget.get_type()
    except Exception:
        return widget.get_value()
    if widget_type not in _STRING_WIDGET_TYPES:
        return widget.get_value()  # numeric/toggle: no char* path, no trap
    pointer_value = _swig_pointer(widget)
    lib = _libgphoto2() if pointer_value is not None else None
    if lib is None:
        return widget.get_value()  # sim/tests (plain objects) — trusted
    out = ctypes.c_void_p()
    if lib.gp_widget_get_value(ctypes.c_void_p(pointer_value), ctypes.byref(out)) != 0:
        return None
    if not out.value:
        return None  # THE crash case: NULL char* — absent, not a segfault
    return ctypes.cast(out, ctypes.c_char_p).value.decode("utf-8", "replace")


def widget_choices(widget) -> list[str]:
    """Choice list with the same NULL guard (gp_widget_get_choice can also
    hand back NULL); NULL entries are skipped."""
    try:
        count = widget.count_choices()
    except Exception:
        return []
    pointer_value = _swig_pointer(widget)
    lib = _libgphoto2() if pointer_value is not None else None
    if lib is None:
        return [widget.get_choice(i) for i in range(count)]
    choices: list[str] = []
    for index in range(count):
        out = ctypes.c_void_p()
        if lib.gp_widget_get_choice(ctypes.c_void_p(pointer_value), index,
                                    ctypes.byref(out)) != 0 or not out.value:
            continue
        choices.append(ctypes.cast(out, ctypes.c_char_p).value.decode("utf-8", "replace"))
    return choices
