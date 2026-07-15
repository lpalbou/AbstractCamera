"""DWARF wire codec: minimal proto3 encode/decode for the DWARF API v2.

The DWARF 3 control plane is protobuf over WebSocket (port 9900). Message
and command definitions below are transcribed from DwarfLab's PUBLISHED
API v2 documentation (the .proto excerpts in their interface spec) — no
third-party bridge code is used or linked (the known community bridges are
GPL-licensed; this package is MIT, so the protocol layer is implemented
from the spec).

Why a hand-rolled codec instead of the `protobuf` package: the DWARF
messages are tiny (a handful of optional scalar fields), proto3 wire
format is stable and fully documented, and a declarative ~150-line codec
avoids a heavyweight runtime dependency plus codegen for what is, on the
wire, varints and doubles. The codec is general over the field kinds the
DWARF vocabulary uses — not special-cased per message.

Field kinds: "uint32"/"uint64"/"int32"/"int64"/"bool" (varint),
"double" (fixed64), "string"/"bytes" (length-delimited),
("message", SPEC) (embedded message), ("repeated_message", SPEC).

proto3 semantics honored: default-valued fields are omitted on encode;
unknown fields are skipped on decode; int32 negatives ride as 64-bit
two's-complement varints (DWARF error codes are negative int32s).
"""

from __future__ import annotations

import struct

# --- field spec tables (field name -> (number, kind)) -----------------------

WS_PACKET = {
    "major_version": (1, "uint32"),
    "minor_version": (2, "uint32"),
    "device_id": (3, "uint32"),
    "module_id": (4, "uint32"),
    "cmd": (5, "uint32"),
    "type": (6, "uint32"),
    "data": (7, "bytes"),
    "client_id": (8, "string"),
}

COM_RESPONSE = {"code": (1, "int32")}
COM_RES_WITH_INT = {"value": (1, "int32")}

REQ_OPEN_CAMERA = {"binning": (1, "bool"), "rtsp_encode_type": (2, "int32")}
REQ_CLOSE_CAMERA: dict = {}
REQ_PHOTO = {"x": (1, "uint32"), "y": (2, "uint32"), "ratio": (3, "double")}
REQ_BURST_PHOTO = {"count": (1, "int32")}
REQ_STOP_BURST: dict = {}
REQ_START_RECORD = {"encode_type": (1, "int32")}
REQ_STOP_RECORD: dict = {}
REQ_SET_EXP_MODE = {"mode": (1, "int32")}
REQ_SET_EXP = {"index": (1, "int32")}
REQ_SET_GAIN_MODE = {"mode": (1, "int32")}
REQ_SET_GAIN = {"index": (1, "int32")}
REQ_SET_IRCUT = {"value": (1, "int32")}

REQ_GOTO_DSO = {"ra": (1, "double"), "dec": (2, "double"), "target_name": (3, "string")}
REQ_GOTO_SOLAR = {"index": (1, "int32"), "lon": (2, "double"), "lat": (3, "double"),
                  "target_name": (4, "string")}
REQ_STOP_GOTO: dict = {}
REQ_START_CALIBRATION: dict = {}
REQ_STOP_CALIBRATION: dict = {}

REQ_MOTOR_JOYSTICK = {"vector_angle": (1, "double"), "vector_length": (2, "double"),
                      "speed": (3, "double")}
REQ_MOTOR_JOYSTICK_STOP: dict = {}
REQ_MOTOR_STOP = {"id": (1, "int32")}

REQ_MANUAL_SINGLE_STEP_FOCUS = {"direction": (1, "uint32")}
REQ_ASTRO_AUTO_FOCUS = {"mode": (1, "uint32")}
REQ_NORMAL_AUTO_FOCUS = {"mode": (1, "uint32"), "center_x": (2, "uint32"),
                         "center_y": (3, "uint32")}

REQ_SET_MASTER_LOCK = {"lock": (1, "bool")}
REQ_GET_SYSTEM_WORKING_STATE: dict = {}

RES_NOTIFY_HOST_SLAVE_MODE = {"mode": (1, "int32"), "lock": (2, "bool")}
RES_NOTIFY_STATE = {"state": (1, "int32")}  # goto/calibration/tracking states
RES_NOTIFY_STATE_ASTRO_TRACKING = {"state": (1, "int32"), "target_name": (2, "string")}
RES_NOTIFY_TEMPERATURE = {"code": (1, "int32"), "temperature": (2, "int32")}
RES_NOTIFY_SDCARD_INFO = {"available_size": (1, "uint32"), "total_size": (2, "uint32"),
                          "code": (3, "int32")}
RES_NOTIFY_RECORD_TIME = {"record_time": (1, "int32")}

COMMON_PARAM = {
    "hasAuto": (1, "bool"),
    "auto_mode": (2, "int32"),
    "id": (3, "int32"),
    "mode_index": (4, "int32"),
    "index": (5, "int32"),
    "continue_value": (6, "double"),
}
RES_NOTIFY_PARAM = {"param": (1, ("repeated_message", COMMON_PARAM))}

# --- module ids (DwarfLab API v2 §protocol) ---------------------------------

MODULE_CAMERA_TELE = 1
MODULE_CAMERA_WIDE = 2
MODULE_ASTRO = 3
MODULE_SYSTEM = 4
MODULE_RGB_POWER = 5
MODULE_MOTOR = 6
MODULE_TRACK = 7
MODULE_FOCUS = 8
MODULE_NOTIFY = 9

# --- message type ids --------------------------------------------------------

TYPE_REQUEST = 0
TYPE_REQUEST_RESPONSE = 1
TYPE_NOTIFICATION = 2
TYPE_NOTIFICATION_RESPONSE = 3

# --- command ids (the subset this package speaks) ----------------------------

CMD_CAMERA_TELE_OPEN_CAMERA = 10000
CMD_CAMERA_TELE_CLOSE_CAMERA = 10001
CMD_CAMERA_TELE_PHOTOGRAPH = 10002
CMD_CAMERA_TELE_BURST = 10003
CMD_CAMERA_TELE_STOP_BURST = 10004
CMD_CAMERA_TELE_START_RECORD = 10005
CMD_CAMERA_TELE_STOP_RECORD = 10006
CMD_CAMERA_TELE_SET_EXP_MODE = 10007
CMD_CAMERA_TELE_SET_EXP = 10009
CMD_CAMERA_TELE_SET_GAIN_MODE = 10011
CMD_CAMERA_TELE_SET_GAIN = 10013
CMD_CAMERA_TELE_SET_IRCUT = 10031
CMD_CAMERA_TELE_GET_SYSTEM_WORKING_STATE = 10039
CMD_CAMERA_TELE_PHOTO_RAW = 10041

CMD_ASTRO_START_CALIBRATION = 11000
CMD_ASTRO_STOP_CALIBRATION = 11001
CMD_ASTRO_START_GOTO_DSO = 11002
CMD_ASTRO_START_GOTO_SOLAR_SYSTEM = 11003
CMD_ASTRO_STOP_GOTO = 11004

CMD_SYSTEM_SET_MASTERLOCK = 13004

CMD_STEP_MOTOR_STOP = 14002
CMD_STEP_MOTOR_SERVICE_JOYSTICK = 14006
CMD_STEP_MOTOR_SERVICE_JOYSTICK_STOP = 14008

CMD_FOCUS_AUTO_FOCUS = 15000
CMD_FOCUS_MANUAL_SINGLE_STEP_FOCUS = 15001
CMD_FOCUS_START_ASTRO_AUTO_FOCUS = 15004

CMD_NOTIFY_ELE = 15201
CMD_NOTIFY_CHARGE = 15202
CMD_NOTIFY_SDCARD_INFO = 15203
CMD_NOTIFY_TELE_RECORD_TIME = 15204
CMD_NOTIFY_STATE_ASTRO_CALIBRATION = 15210
CMD_NOTIFY_STATE_ASTRO_GOTO = 15211
CMD_NOTIFY_STATE_ASTRO_TRACKING = 15212
CMD_NOTIFY_TELE_SET_PARAM = 15213
CMD_NOTIFY_WS_HOST_SLAVE_MODE = 15223
CMD_NOTIFY_POWER_OFF = 15229
CMD_NOTIFY_ALBUM_UPDATE = 15230
CMD_NOTIFY_TEMPERATURE = 15243

# Solar-system goto targets (API v2 enum SolarSystemTarget).
SOLAR_SYSTEM_TARGETS = {
    "mercury": 1, "venus": 2, "mars": 3, "jupiter": 4, "saturn": 5,
    "uranus": 6, "neptune": 7, "moon": 8, "sun": 9,
}

# Astro state codes (API v2 §notifications: shared state vocabulary).
ASTRO_STATES = {0: "idle", 1: "running", 2: "stopping", 3: "stopped",
                4: "success", 5: "failed", 6: "plate solving"}


# --- proto3 primitives --------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    if value < 0:
        value &= 0xFFFFFFFFFFFFFFFF  # two's complement, 64-bit (proto3 int32/int64)
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    if value >= 1 << 63:
        value -= 1 << 64
    if -(1 << 31) <= value < 1 << 31:
        return value
    return value  # out-of-range int32 survives as int64 (defensive, not expected)


_VARINT_KINDS = ("uint32", "uint64", "int32", "int64", "bool")


def _wire_type(kind) -> int:
    if isinstance(kind, tuple):
        return 2
    if kind in _VARINT_KINDS:
        return 0
    if kind == "double":
        return 1
    if kind in ("string", "bytes"):
        return 2
    raise ValueError(f"unknown field kind: {kind}")


def encode_message(spec: dict, values: dict) -> bytes:
    """Encode `values` against `spec` (proto3: defaults are omitted)."""
    out = bytearray()
    for name, (number, kind) in spec.items():
        if name not in values:
            continue
        value = values[name]
        if isinstance(kind, tuple):
            mode, sub_spec = kind
            items = value if mode == "repeated_message" else [value]
            for item in items:
                payload = encode_message(sub_spec, item)
                out += _encode_varint((number << 3) | 2)
                out += _encode_varint(len(payload))
                out += payload
            continue
        if kind in _VARINT_KINDS:
            number_value = int(value)
            if number_value == 0:
                continue
            out += _encode_varint((number << 3) | 0)
            out += _encode_varint(number_value)
        elif kind == "double":
            if float(value) == 0.0:
                continue
            out += _encode_varint((number << 3) | 1)
            out += struct.pack("<d", float(value))
        elif kind == "string":
            payload = str(value).encode("utf-8")
            if not payload:
                continue
            out += _encode_varint((number << 3) | 2)
            out += _encode_varint(len(payload))
            out += payload
        elif kind == "bytes":
            payload = bytes(value)
            if not payload:
                continue
            out += _encode_varint((number << 3) | 2)
            out += _encode_varint(len(payload))
            out += payload
    return bytes(out)


def decode_message(spec: dict, data: bytes) -> dict:
    """Decode `data` against `spec`. Absent fields get proto3 defaults;
    unknown fields are skipped (forward compatibility with new firmware)."""
    by_number = {number: (name, kind) for name, (number, kind) in spec.items()}
    result: dict = {}
    for name, (_, kind) in spec.items():
        if isinstance(kind, tuple) and kind[0] == "repeated_message":
            result[name] = []
        elif isinstance(kind, tuple):
            result[name] = None
        elif kind in _VARINT_KINDS:
            result[name] = False if kind == "bool" else 0
        elif kind == "double":
            result[name] = 0.0
        else:
            result[name] = "" if kind == "string" else b""
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        number, wire = tag >> 3, tag & 0x7
        if wire == 0:
            raw, pos = _decode_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data):
                raise ValueError("truncated fixed64")
            raw = data[pos:pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated length-delimited field")
            raw = data[pos:pos + length]
            pos += length
        elif wire == 5:
            if pos + 4 > len(data):
                raise ValueError("truncated fixed32")
            raw = data[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        entry = by_number.get(number)
        if entry is None:
            continue  # unknown field: skipped, forward-compatible
        name, kind = entry
        if isinstance(kind, tuple):
            mode, sub_spec = kind
            decoded = decode_message(sub_spec, raw)
            if mode == "repeated_message":
                result[name].append(decoded)
            else:
                result[name] = decoded
        elif kind == "bool":
            result[name] = bool(raw)
        elif kind in ("int32", "int64"):
            result[name] = _signed32(raw) if kind == "int32" else raw
        elif kind in ("uint32", "uint64"):
            result[name] = raw
        elif kind == "double":
            result[name] = struct.unpack("<d", raw)[0]
        elif kind == "string":
            result[name] = raw.decode("utf-8", errors="replace")
        else:
            result[name] = raw
    return result


def encode_packet(module_id: int, cmd: int, payload: bytes, *,
                  client_id: str, device_id: int = 1,
                  packet_type: int = TYPE_REQUEST,
                  major_version: int = 1, minor_version: int = 2) -> bytes:
    """One control-plane frame: a WsPacket wrapping an encoded request."""
    return encode_message(WS_PACKET, {
        "major_version": major_version,
        "minor_version": minor_version,
        "device_id": device_id,
        "module_id": module_id,
        "cmd": cmd,
        "type": packet_type,
        "data": payload,
        "client_id": client_id,
    })


def decode_packet(frame: bytes) -> dict:
    return decode_message(WS_PACKET, frame)
