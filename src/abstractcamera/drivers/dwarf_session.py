"""DwarfSession: a DWARF smart telescope speaking the CameraSession protocol.

Mapping the network device onto the session contract (ADR 0001):

- live view      -> RTSP frames re-encoded to JPEG (capture_preview blocks
                    roughly one frame interval — the stream paces the worker)
- trigger        -> CMD_CAMERA_TELE_PHOTOGRAPH; the shot lands in the
                    DWARF's ALBUM (its microSD), so...
- FILE_ADDED     -> album polling: new album entries newer than a pending
                    capture window announce as (folder, name)
- file_get       -> HTTP download of the album filePath
- config widgets -> the device's OWN parameter tables (exposure/gain gear
                    names fetched from /getDefaultParamsConfig) + the
                    documented DWARF 3 filter wheel positions
- mount/focus    -> dwarf-specific methods the DwarfAdapter maps family
                    actions onto (goto, joystick, calibration, autofocus)

Master lock: the DWARF grants ONE controller. init() requests it and
refuses honestly when the device says another controller (the DWARFLAB
app) holds it — a session that silently ran as a powerless observer would
break every write path downstream.

Threading: protocol methods run on the manager's worker thread only. The
transport's reader thread never touches session state; notifications cross
through a thread-safe deque.
"""

from __future__ import annotations

import time
from collections import deque

from abstractcamera import wire
from abstractcamera.drivers import dwarf_wire as wire3
from abstractcamera.drivers.dwarf_transport import DwarfTransport, RtspFrameSource
from abstractcamera.errors import CameraControlError

PREVIEW_JPEG_QUALITY = 85
ALBUM_POLL_INTERVAL_S = 1.5
ALBUM_POLL_TIMEOUT_S = 4.0
ALBUM_ANNOUNCE_MARGIN_S = 25.0   # command -> album-entry latency allowance
LIVE_OPEN_RETRY_WINDOW_S = 10.0  # the video pipeline warms up after OPEN_CAMERA

# Error codes worth a named refusal (DwarfLab API v2 §error codes).
_ERROR_TEXT = {
    -1: "the DWARF could not parse the command (protocol mismatch)",
    -2: "no SD card is present in the DWARF",
    -3: "the DWARF rejected the parameters",
    -4: "writing to the DWARF's SD card failed (it may be full)",
    -10504: "the telephoto camera failed to open",
    -10507: "the telephoto camera is busy stacking",
    -10511: "the telephoto camera is busy",
    -11501: "the astronomy module is busy",
    -11504: "calibration failed",
    -11505: "GOTO failed (target below horizon or plate solving failed)",
    -11513: "no GOTO has run yet — run a GOTO first",
    -14519: "the mount hit its rotation limit",
}

# Documented DWARF 3 filter positions (SET_IRCUT values).
DWARF3_FILTERS = ("VIS Filter", "Astro Filter", "Dual Band")


def _error_text(code: int) -> str:
    return _ERROR_TEXT.get(code, f"DWARF error code {code}")


class DwarfSession:
    """One DWARF unit, addressed by host, speaking the session protocol.

    transport_factory/live_source_factory are the test seams (ADR 0007):
    a fake transport + numpy frame source run the whole suite offline.
    """

    def __init__(self, host: str, label: str = "DWARF 3",
                 transport_factory=None, live_source_factory=None):
        self.host = host
        self._label = label
        self._transport_factory = transport_factory or DwarfTransport
        self._live_source_factory = live_source_factory or RtspFrameSource
        self._transport = None
        self._live = None
        self._events: deque = deque()
        self._seen_files: set[str] = set()
        self._pending_deadlines: list[float] = []
        self._album_last_poll = 0.0
        self._album_backoff_s = ALBUM_POLL_INTERVAL_S
        self._exposure_table: list[tuple[int, str]] = []
        self._gain_table: list[tuple[int, str]] = []
        self._exposure_value = ""
        self._gain_value = ""
        self._filter_value = ""
        self._battery = ""
        self._temperature = ""
        self._manual_exposure_sent = False
        self._manual_gain_sent = False

    # -- lifecycle ----------------------------------------------------------------
    def init(self) -> None:
        transport = self._transport_factory(self.host)
        transport.connect()
        try:
            self._handshake(transport)
        except Exception:
            transport.close()
            raise
        self._transport = transport

    def _handshake(self, transport) -> None:
        # Working-state query first: it wakes the notification stream (and
        # is how existing tooling primes the device).
        try:
            transport.send_request(
                wire3.MODULE_CAMERA_TELE,
                wire3.CMD_CAMERA_TELE_GET_SYSTEM_WORKING_STATE,
                timeout_s=8.0)
        except CameraControlError:
            pass  # some firmware answers only with notifications — not fatal

        self._acquire_master_lock(transport)
        self._load_parameter_tables(transport)

        # Open the telephoto camera (idempotent: "already on" is success).
        code = transport.send_command(
            wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_OPEN_CAMERA,
            wire3.REQ_OPEN_CAMERA, {"binning": False, "rtsp_encode_type": 0},
            timeout_s=15.0)
        if code not in (0, -10500):
            raise CameraControlError(
                f"The DWARF refused to open its camera — {_error_text(code)}.")

        self._open_live_view()
        self._seed_seen_files(transport)

    def _acquire_master_lock(self, transport) -> None:
        payload = wire3.encode_message(wire3.REQ_SET_MASTER_LOCK, {"lock": True})
        try:
            response = transport.send_request(
                wire3.MODULE_SYSTEM, wire3.CMD_SYSTEM_SET_MASTERLOCK, payload,
                alias_keys=((wire3.MODULE_NOTIFY, wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE),
                            (wire3.MODULE_SYSTEM, wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE)),
                timeout_s=8.0)
        except CameraControlError as exc:
            raise CameraControlError(
                f"Master-lock negotiation with the DWARF failed: {exc}")
        if int(response.get("_cmd", 0)) == wire3.CMD_NOTIFY_WS_HOST_SLAVE_MODE:
            decoded = wire3.decode_message(wire3.RES_NOTIFY_HOST_SLAVE_MODE,
                                           response.get("data", b""))
            if not decoded.get("lock", False):
                raise CameraControlError(
                    "The DWARF granted only observer access — another "
                    "controller (usually the DWARFLAB app) holds the master "
                    "lock. Close the app (or release control there) and "
                    "reconnect.")
        else:
            decoded = wire3.decode_message(wire3.COM_RESPONSE,
                                           response.get("data", b""))
            code = int(decoded.get("code", 0))
            if code != 0:
                raise CameraControlError(
                    f"The DWARF refused the master lock — {_error_text(code)}.")

    def _load_parameter_tables(self, transport) -> None:
        """The device's own exposure/gain vocabularies. Absence is not fatal
        (dials are then absent, honestly), but a telescope without its
        exposure table is barely usable — surface a status note."""
        try:
            payload = transport.default_params_config()
        except CameraControlError as exc:
            self._events.append((wire.GP_EVENT_UNKNOWN,
                                 f"parameter tables unavailable ({exc}) — "
                                 "exposure/gain dials are disabled"))
            return
        self._exposure_table, self._gain_table = self._parse_params_config(payload)

    @staticmethod
    def _parse_params_config(payload: dict) -> tuple[list, list]:
        """Extract (index, name) gear tables for the TELE camera's exposure
        and gain from the getDefaultParamsConfig JSON. Defensive walk: the
        exact nesting varies across firmware, but entries are always dicts
        carrying gear values lists of {index, name}."""
        def gear_options(param: dict) -> list[tuple[int, str]]:
            options: list[tuple[int, str]] = []
            gear = param.get("gearMode")
            values = gear.get("values") if isinstance(gear, dict) else None
            for entry in values if isinstance(values, list) else []:
                if not isinstance(entry, dict):
                    continue
                try:
                    options.append((int(entry["index"]), str(entry.get("name", ""))))
                except (KeyError, TypeError, ValueError):
                    continue
            return options

        exposure: list[tuple[int, str]] = []
        gain: list[tuple[int, str]] = []
        data = payload.get("data")
        cameras = data.get("cameras") if isinstance(data, dict) else None
        for camera in cameras if isinstance(cameras, list) else []:
            if not isinstance(camera, dict):
                continue
            name = str(camera.get("name", "")).lower()
            if cameras and len(cameras) > 1 and "tele" not in name and name:
                # Wide-angle tables exist too; v1 pilots the tele camera.
                if "wide" in name:
                    continue
            for param in camera.get("supportParams") or []:
                if not isinstance(param, dict):
                    continue
                param_name = str(param.get("name", "")).lower()
                if "exposure" in param_name and not exposure:
                    exposure = gear_options(param)
                elif "gain" in param_name and not gain:
                    gain = gear_options(param)
        return exposure, gain

    def _open_live_view(self) -> None:
        live = self._live_source_factory(self._rtsp_url())
        deadline = time.time() + LIVE_OPEN_RETRY_WINDOW_S
        while True:
            try:
                live.open()
                break
            except CameraControlError:
                if time.time() >= deadline:
                    raise
                time.sleep(1.0)
        self._live = live

    def _rtsp_url(self) -> str:
        transport = self._transport
        if transport is not None and hasattr(transport, "rtsp_url"):
            return transport.rtsp_url(0)
        return f"rtsp://{self.host}:554/ch0/stream0"

    def _seed_seen_files(self, transport) -> None:
        """Pre-existing album files never announce as new captures."""
        try:
            for entry in transport.album_media_infos(page_size=16):
                name = str(entry.get("fileName", ""))
                if name:
                    self._seen_files.add(name)
        except CameraControlError:
            pass  # empty album / album service still starting

    def exit(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.close()
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                # We opened the camera; close it so the device idles clean.
                transport.send_command(
                    wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_CLOSE_CAMERA,
                    timeout_s=5.0)
            except CameraControlError:
                pass
            transport.close()

    def get_abilities(self):
        return wire.Abilities(model=f"{self._label} ({self.host})")

    # -- streaming -------------------------------------------------------------------
    def capture_preview(self):
        import cv2

        if self._live is None:
            raise CameraControlError("The DWARF live stream is closed.")
        frame = self._live.read()  # raises when the stream dies (watchdog)
        ok, encoded = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
        if not ok:
            raise CameraControlError("JPEG encoding of the preview frame failed.")
        return wire.BytesFile(encoded.tobytes())

    # -- capture ---------------------------------------------------------------------
    def _command(self, module_id: int, cmd: int, spec: dict | None = None,
                 values: dict | None = None, *, timeout_s: float = 10.0,
                 accept: tuple = ()) -> None:
        transport = self._transport
        if transport is None:
            raise CameraControlError("The DWARF session is closed.")
        code = transport.send_command(module_id, cmd, spec, values,
                                      timeout_s=timeout_s)
        if code != 0 and code not in accept:
            raise CameraControlError(_error_text(code))

    def _expect_album_files(self, extra_window_s: float = 0.0) -> None:
        self._pending_deadlines.append(
            time.time() + ALBUM_ANNOUNCE_MARGIN_S + self.nominal_exposure_s()
            + extra_window_s)

    def trigger_capture(self) -> None:
        self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_PHOTOGRAPH,
                      wire3.REQ_PHOTO, {})
        self._expect_album_files()

    def start_burst(self, count: int) -> None:
        self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_BURST,
                      wire3.REQ_BURST_PHOTO, {"count": int(count)})
        per_frame = max(1.0, self.nominal_exposure_s() + 1.0)
        self._expect_album_files(extra_window_s=per_frame * int(count))

    def stop_burst(self) -> None:
        self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_STOP_BURST)

    def start_record(self) -> None:
        self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_START_RECORD,
                      wire3.REQ_START_RECORD, {"encode_type": 0})

    def stop_record(self) -> None:
        self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_STOP_RECORD)
        self._expect_album_files()  # the finished MP4 appears in the album

    # -- mount / astro -----------------------------------------------------------------
    def goto_dso(self, ra_deg: float, dec_deg: float, target_name: str = "") -> None:
        self._command(wire3.MODULE_ASTRO, wire3.CMD_ASTRO_START_GOTO_DSO,
                      wire3.REQ_GOTO_DSO,
                      {"ra": float(ra_deg), "dec": float(dec_deg),
                       "target_name": target_name or "target"},
                      timeout_s=15.0)

    def goto_solar(self, target: str, lon: float = 0.0, lat: float = 0.0) -> None:
        index = wire3.SOLAR_SYSTEM_TARGETS.get(target.strip().lower())
        if index is None:
            known = ", ".join(sorted(wire3.SOLAR_SYSTEM_TARGETS))
            raise CameraControlError(
                f"Unknown solar-system target '{target}' — known targets: {known}.")
        self._command(wire3.MODULE_ASTRO, wire3.CMD_ASTRO_START_GOTO_SOLAR_SYSTEM,
                      wire3.REQ_GOTO_SOLAR,
                      {"index": index, "lon": float(lon), "lat": float(lat),
                       "target_name": target},
                      timeout_s=15.0)

    def stop_goto(self) -> None:
        self._command(wire3.MODULE_ASTRO, wire3.CMD_ASTRO_STOP_GOTO)

    def start_calibration(self) -> None:
        self._command(wire3.MODULE_ASTRO, wire3.CMD_ASTRO_START_CALIBRATION,
                      timeout_s=15.0)

    def joystick(self, angle_deg: float, length: float, speed_dps: float) -> None:
        self._command(wire3.MODULE_MOTOR, wire3.CMD_STEP_MOTOR_SERVICE_JOYSTICK,
                      wire3.REQ_MOTOR_JOYSTICK,
                      {"vector_angle": float(angle_deg),
                       "vector_length": max(0.0, min(1.0, float(length))),
                       "speed": max(0.1, min(30.0, float(speed_dps)))})

    def joystick_stop(self) -> None:
        self._command(wire3.MODULE_MOTOR,
                      wire3.CMD_STEP_MOTOR_SERVICE_JOYSTICK_STOP)

    def astro_autofocus(self) -> None:
        self._command(wire3.MODULE_FOCUS, wire3.CMD_FOCUS_START_ASTRO_AUTO_FOCUS,
                      wire3.REQ_ASTRO_AUTO_FOCUS, {"mode": 0}, timeout_s=15.0)

    def normal_autofocus(self) -> None:
        self._command(wire3.MODULE_FOCUS, wire3.CMD_FOCUS_AUTO_FOCUS,
                      wire3.REQ_NORMAL_AUTO_FOCUS, {"mode": 0}, timeout_s=15.0)

    def manual_focus_step(self, direction: int) -> None:
        self._command(wire3.MODULE_FOCUS, wire3.CMD_FOCUS_MANUAL_SINGLE_STEP_FOCUS,
                      wire3.REQ_MANUAL_SINGLE_STEP_FOCUS,
                      {"direction": 1 if direction >= 0 else 0})

    # -- events / files ---------------------------------------------------------------
    def wait_for_event(self, timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            self._drain_notifications()
            self._poll_album_if_due()
            if self._events:
                return self._events.popleft()
            if time.time() >= deadline:
                return (wire.GP_EVENT_TIMEOUT, None)
            time.sleep(0.002)

    def _drain_notifications(self) -> None:
        transport = self._transport
        if transport is None:
            return
        while True:
            try:
                packet = transport.notifications.popleft()
            except IndexError:
                return
            self._consume_notification(packet)

    def _consume_notification(self, packet: dict) -> None:
        cmd = int(packet.get("_cmd", packet.get("cmd", 0)))
        data = packet.get("data", b"")
        try:
            if cmd == wire3.CMD_NOTIFY_ELE:
                value = wire3.decode_message(wire3.COM_RES_WITH_INT, data)["value"]
                self._battery = f"{value}%"
            elif cmd == wire3.CMD_NOTIFY_TEMPERATURE:
                decoded = wire3.decode_message(wire3.RES_NOTIFY_TEMPERATURE, data)
                self._temperature = f"{decoded['temperature']}\N{DEGREE SIGN}C"
            elif cmd == wire3.CMD_NOTIFY_ALBUM_UPDATE:
                self._album_last_poll = 0.0  # poll on the next pump
                if not self._pending_deadlines:
                    # Externally-initiated capture (device buttons/app):
                    # still announce it — the file exists either way.
                    self._pending_deadlines.append(time.time() + 10.0)
            elif cmd == wire3.CMD_NOTIFY_STATE_ASTRO_GOTO:
                state = wire3.decode_message(wire3.RES_NOTIFY_STATE, data)["state"]
                self._events.append((wire.GP_EVENT_UNKNOWN,
                                     f"GOTO {wire3.ASTRO_STATES.get(state, state)}"))
            elif cmd == wire3.CMD_NOTIFY_STATE_ASTRO_CALIBRATION:
                state = wire3.decode_message(wire3.RES_NOTIFY_STATE, data)["state"]
                self._events.append((wire.GP_EVENT_UNKNOWN,
                                     f"calibration {wire3.ASTRO_STATES.get(state, state)}"))
            elif cmd == wire3.CMD_NOTIFY_STATE_ASTRO_TRACKING:
                decoded = wire3.decode_message(
                    wire3.RES_NOTIFY_STATE_ASTRO_TRACKING, data)
                target = decoded.get("target_name") or "target"
                state = wire3.ASTRO_STATES.get(decoded["state"], decoded["state"])
                self._events.append((wire.GP_EVENT_UNKNOWN,
                                     f"tracking {target}: {state}"))
            elif cmd == wire3.CMD_NOTIFY_POWER_OFF:
                self._events.append((wire.GP_EVENT_UNKNOWN,
                                     "the DWARF reports it is powering off"))
        except (ValueError, KeyError):
            pass  # malformed notification: skip, keep the session alive

    def _poll_album_if_due(self) -> None:
        transport = self._transport
        if transport is None or not self._pending_deadlines:
            return
        now = time.time()
        if now - self._album_last_poll < self._album_backoff_s:
            return
        self._album_last_poll = now
        try:
            entries = transport.album_media_infos(page_size=8)
            self._album_backoff_s = ALBUM_POLL_INTERVAL_S
        except CameraControlError:
            # Album service hiccup: back off, never kill the preview loop.
            self._album_backoff_s = min(10.0, self._album_backoff_s * 2)
            return
        # Expired windows drop AFTER their poll ran, so every window gets a
        # final post-deadline look (album entries can lag the capture).
        self._pending_deadlines = [d for d in self._pending_deadlines if d > now]
        for entry in reversed(entries):  # oldest-first announce order
            name = str(entry.get("fileName", ""))
            path = str(entry.get("filePath", ""))
            if not name or name in self._seen_files:
                continue
            self._seen_files.add(name)
            folder = path[: -len(name)].rstrip("/") if path.endswith(name) else path
            self._events.append((wire.GP_EVENT_FILE_ADDED,
                                 wire.EventData(folder or "/", name)))
        if len(self._seen_files) > 4096:
            # Bounded memory across very long sessions; old names can never
            # announce again anyway (polls only look at the newest page).
            self._seen_files = set(list(self._seen_files)[-2048:])

    def file_get(self, folder: str, name: str, file_type: int):
        transport = self._transport
        if transport is None:
            raise CameraControlError("The DWARF session is closed.")
        path = f"{folder.rstrip('/')}/{name}" if folder and folder != "/" else name
        return wire.BytesFile(transport.fetch_media(path))

    # -- single-config widget I/O --------------------------------------------------
    def nominal_exposure_s(self) -> float:
        from abstractcamera.sequences import parse_shutter_speed_seconds

        try:
            return float(parse_shutter_speed_seconds(self._exposure_value))
        except Exception:
            return 0.0

    def get_single_config(self, name: str):
        if name == "shutterspeed":
            if not self._exposure_table:
                raise CameraControlError("Widget not found: shutterspeed")
            return wire.ProtocolWidget(
                "shutterspeed", wire.GP_WIDGET_RADIO, self._exposure_value,
                choices=[label for _, label in self._exposure_table])
        if name == "gain":
            if not self._gain_table:
                raise CameraControlError("Widget not found: gain")
            return wire.ProtocolWidget(
                "gain", wire.GP_WIDGET_RADIO, self._gain_value,
                choices=[label for _, label in self._gain_table])
        if name == "ircut":
            return wire.ProtocolWidget(
                "ircut", wire.GP_WIDGET_RADIO, self._filter_value,
                choices=list(DWARF3_FILTERS))
        if name == "battery":
            return wire.ProtocolWidget("battery", wire.GP_WIDGET_TEXT,
                                       self._battery, readonly=True)
        if name == "temperature":
            return wire.ProtocolWidget("temperature", wire.GP_WIDGET_TEXT,
                                       self._temperature, readonly=True)
        raise CameraControlError(f"Widget not found: {name}")

    @staticmethod
    def _lookup(table: list[tuple[int, str]], label: str) -> int:
        wanted = str(label).strip()
        for index, name in table:
            if name == wanted:
                return index
        known = ", ".join(name for _, name in table[:12])
        raise CameraControlError(
            f"'{wanted}' is not one of this DWARF's values (device offers: {known}...).")

    def set_single_config(self, name: str, widget) -> None:
        value = str(widget.get_value())
        if name == "shutterspeed":
            index = self._lookup(self._exposure_table, value)
            if not self._manual_exposure_sent:
                self._command(wire3.MODULE_CAMERA_TELE,
                              wire3.CMD_CAMERA_TELE_SET_EXP_MODE,
                              wire3.REQ_SET_EXP_MODE, {"mode": 1})
                self._manual_exposure_sent = True
            self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_EXP,
                          wire3.REQ_SET_EXP, {"index": index})
            self._exposure_value = value
            return
        if name == "gain":
            index = self._lookup(self._gain_table, value)
            if not self._manual_gain_sent:
                self._command(wire3.MODULE_CAMERA_TELE,
                              wire3.CMD_CAMERA_TELE_SET_GAIN_MODE,
                              wire3.REQ_SET_GAIN_MODE, {"mode": 1})
                self._manual_gain_sent = True
            self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_GAIN,
                          wire3.REQ_SET_GAIN, {"index": index})
            self._gain_value = value
            return
        if name == "ircut":
            filters = {label.lower(): position
                       for position, label in enumerate(DWARF3_FILTERS)}
            position = filters.get(value.strip().lower())
            if position is None:
                raise CameraControlError(
                    f"Unknown filter '{value}' — this DWARF offers: "
                    f"{', '.join(DWARF3_FILTERS)}.")
            self._command(wire3.MODULE_CAMERA_TELE, wire3.CMD_CAMERA_TELE_SET_IRCUT,
                          wire3.REQ_SET_IRCUT, {"value": position})
            self._filter_value = DWARF3_FILTERS[position]
            return
        raise CameraControlError(
            f"'{self._label}' exposes no manual control over {name}.")
