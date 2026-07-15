# Acknowledgments

AbstractCamera stands on the shoulders of excellent open-source projects and communities.

## Base runtime dependencies

- **NumPy** (frame buffers and detection math): [`pyproject.toml`](pyproject.toml)
- **OpenCV (opencv-python)** (JPEG encode/decode, detection, RTSP live-view reading): [`pyproject.toml`](pyproject.toml)
- **PyObjC (AVFoundation / Quartz / libdispatch)** (native macOS webcam capture by device uniqueID, ADR 0009): [`src/abstractcamera/drivers/avf_capture.py`](src/abstractcamera/drivers/avf_capture.py), [`src/abstractcamera/drivers/avf_enum.py`](src/abstractcamera/drivers/avf_enum.py)

## Optional runtime dependencies (declared as extras)

- **python-gphoto2 / libgphoto2** (tethered PTP camera control — Nikon Z, Sony Alpha, generic bodies): [`src/abstractcamera/drivers/gphoto2_driver.py`](src/abstractcamera/drivers/gphoto2_driver.py) (declared in the `gphoto2` extra)
- **PyAV / FFmpeg** (MP4 movie recording and rolling-clip encoding): [`src/abstractcamera/clips.py`](src/abstractcamera/clips.py), [`src/abstractcamera/drivers/webcam_session.py`](src/abstractcamera/drivers/webcam_session.py) (declared in the `clips` extra)
- **rawpy / LibRaw** (embedded-JPEG thumbnails for RAW captures): [`src/abstractcamera/raw_thumbs.py`](src/abstractcamera/raw_thumbs.py) (declared in the `raw` extra)
- **websocket-client** (WebSocket transport for the DWARF smart-telescope control plane): [`src/abstractcamera/drivers/dwarf_transport.py`](src/abstractcamera/drivers/dwarf_transport.py) (declared in the `dwarf` extra)

## Protocol documentation

- **DwarfLab DWARF API v2** (published interface specification — module,
  command and message definitions — from which the vendored minimal
  protobuf codec in [`src/abstractcamera/drivers/dwarf_wire.py`](src/abstractcamera/drivers/dwarf_wire.py)
  is implemented): <https://help.dwarflab.com/>
- The community DWARF integrations (dwarfAlp, dwarf_python_api) were
  studied as protocol references only — no code is copied or linked (they
  are GPL-licensed; this package is MIT).
- **libgphoto2** documentation and the PTP specification informed the
  session wire vocabulary ([`src/abstractcamera/wire.py`](src/abstractcamera/wire.py) — event/widget
  constants pinned to libgphoto2's values).

## Packaging

- **setuptools** and **wheel** (build system): [`pyproject.toml`](pyproject.toml)

## Community and contributors

Thanks to everyone who reports issues, suggests improvements, and contributes fixes or documentation updates.
