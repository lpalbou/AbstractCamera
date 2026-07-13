"""Widget trees for the simulated cameras (backend/fake_gphoto2.py).

Two profiles:
- z6ii: mirrors a Nikon Z6 II (from a real `gphoto2 --list-all-config` dump)
  — moved verbatim from fake_gphoto2._build_z6ii_widgets.
- a7r4: mirrors a Sony A7R IV (from the hardware probe of the REAL body,
  2026-07-12, untracked/sony_probe/sony_relevant_widgets.txt): Sony value
  vocabularies ('1/60', '13/10', 'card+sdram', 'Auto ISO' as an iso choice),
  Sony action toggles (autofocus/manualfocus/capture/bulb/movie), the
  prioritymode gate, readonly EV in M — and NO isoauto / burstnumber /
  liveviewsize / movieprohibit / recordingmedia / viewfinder widgets.

The builders take the widget class and a config getter so this module stays
import-cycle-free (fake_gphoto2 imports it, not the other way around).
"""

from __future__ import annotations

GP_WIDGET_TEXT = 2
GP_WIDGET_RANGE = 3
GP_WIDGET_TOGGLE = 4
GP_WIDGET_RADIO = 5


def build_z6ii_widgets(widget_cls, cfg) -> dict:
    """Widget names, choice strings, and ranges mirroring a Nikon Z6 II."""
    w = {}

    def add(name, wtype, value, choices=None, wrange=None, readonly=False):
        w[name] = widget_cls(name, wtype, value, choices, wrange, readonly)

    add("iso", GP_WIDGET_RADIO, "800",
        ["100", "200", "400", "800", "1600", "3200", "6400", "12800", "25600", "51200"])
    add("isoauto", GP_WIDGET_RADIO, "Off", ["On", "Off"])
    add("shutterspeed", GP_WIDGET_RADIO, "0.0333s",
        ["Bulb", "Time", "30s", "25s", "20s", "15s", "13s", "10s", "8s", "6s", "5s",
         "4s", "3s", "2s", "1.6s", "1.3s", "1s", "0.7692s", "0.6250s", "0.5000s",
         "0.4000s", "0.3333s", "0.2500s", "0.2000s", "0.1666s", "0.1250s", "0.1000s",
         "0.0769s", "0.0666s", "0.0500s", "0.0400s", "0.0333s", "0.0250s", "0.0200s",
         "0.0166s", "0.0125s", "0.0100s", "0.0080s", "0.0062s", "0.0050s", "0.0040s",
         "0.0031s", "0.0025s", "0.0020s", "0.0015s", "0.0012s", "0.0010s", "0.0008s",
         "0.0006s", "0.0005s", "0.0004s", "0.0003s", "0.0002s"])
    add("f-number", GP_WIDGET_RADIO, "f/4",
        ["f/1.8", "f/2", "f/2.2", "f/2.5", "f/2.8", "f/3.2", "f/3.5", "f/4",
         "f/4.5", "f/5", "f/5.6", "f/6.3", "f/7.1", "f/8", "f/9", "f/10", "f/11",
         "f/13", "f/14", "f/16"])
    add("whitebalance", GP_WIDGET_RADIO, "Automatic",
        ["Automatic", "Natural light auto", "Daylight", "Fluorescent",
         "Incandescent", "Cloudy", "Shade", "Color Temperature", "Preset"])
    add("colortemperature", GP_WIDGET_RANGE, 5000.0, wrange=[2500.0, 10000.0, 10.0])
    add("exposurecompensation", GP_WIDGET_RADIO, "0",
        ["-5", "-4", "-3", "-2", "-1", "0", "1", "2", "3", "4", "5"])
    add("expprogram", GP_WIDGET_RADIO, "M", ["M", "P", "A", "S"])
    add("usermode", GP_WIDGET_RADIO, "U1", ["U1", "U2", "U3"])
    add("capturemode", GP_WIDGET_RADIO, "Single Shot",
        ["Single Shot", "Burst", "Timer", "Quiet"])
    add("burstnumber", GP_WIDGET_RANGE, 3.0, wrange=[1.0, 200.0, 1.0])
    add("shootingspeed", GP_WIDGET_RADIO, "5.5fps", ["12fps", "10fps", "5.5fps", "4fps"])
    add("capturetarget", GP_WIDGET_RADIO, "Memory card", ["Internal RAM", "Memory card"])
    add("imagequality", GP_WIDGET_RADIO, "NEF (Raw)",
        ["JPEG Basic", "JPEG Normal", "JPEG Fine", "NEF (Raw)",
         "NEF+Basic", "NEF+Normal", "NEF+Fine"])
    add("imagesize", GP_WIDGET_RADIO, "Large", ["Large", "Medium", "Small"])
    add("liveviewsize", GP_WIDGET_RADIO, "VGA", ["QVGA", "VGA", "XGA"])
    add("focusmode", GP_WIDGET_RADIO, "AF-S", ["Manual", "AF-S", "AF-C", "AF-F"])
    add("exposuremetermode", GP_WIDGET_RADIO, "Matrix",
        ["Matrix", "Center-Weighted", "Spot", "Highlight-weighted"])
    add("batterylevel", GP_WIDGET_TEXT, "82%", readonly=True)
    add("autofocusdrive", GP_WIDGET_TOGGLE, 0)
    add("manualfocusdrive", GP_WIDGET_RANGE, 0.0, wrange=[-32767.0, 32767.0, 1.0])
    add("movie", GP_WIDGET_TOGGLE, 0)
    add("viewfinder", GP_WIDGET_TOGGLE, 1)
    add("recordingmedia", GP_WIDGET_RADIO, "Card", ["Card", "SDRAM"])
    add("movieprohibit", GP_WIDGET_TEXT, cfg("movie_prohibit_text"), readonly=True)
    # Scenario hook: start a session with specific widget values (e.g. a
    # body arriving with isoauto=On to exercise the connect-time default).
    for name, value in (cfg("widget_overrides") or {}).items():
        if name in w:
            w[name].value = value
    return w


def build_a7r4_widgets(widget_cls, cfg) -> dict:
    """Sony A7R IV widget surface (names/choices/RO flags from the real
    body). Note what is ABSENT versus Nikon: isoauto, burstnumber,
    liveviewsize, movieprohibit, recordingmedia, viewfinder."""
    w = {}

    def add(name, wtype, value, choices=None, wrange=None, readonly=False):
        w[name] = widget_cls(name, wtype, value, choices, wrange, readonly)

    # actions (probe: toggles idle at 2; manualfocus is a step CODE ±1..7)
    add("autofocus", GP_WIDGET_TOGGLE, 2)
    add("manualfocus", GP_WIDGET_RANGE, 0.0, wrange=[-7.0, 7.0, 1.0])
    add("capture", GP_WIDGET_TOGGLE, 2)
    add("bulb", GP_WIDGET_TOGGLE, 2)
    add("movie", GP_WIDGET_TOGGLE, 2)
    # settings
    add("prioritymode", GP_WIDGET_RADIO, "Camera", ["Camera", "Application"])
    add("capturetarget", GP_WIDGET_RADIO, "card+sdram", ["sdram", "card+sdram", "card"])
    # status
    add("cameramodel", GP_WIDGET_TEXT, "ILCE-7RM4", readonly=True)
    add("batterylevel", GP_WIDGET_TEXT, "100%", readonly=True)
    add("focusindication", GP_WIDGET_RADIO, "Unlock",
        ["Unlock", "Focus Locked", "No Focus - Low Contrast", "Tracking Acquire",
         "Tracking Focused", "Tracking No Focus - Low Contrast", "Unpause", "Pause"],
        readonly=True)
    add("focalposition", GP_WIDGET_RANGE, 86.0, wrange=[0.0, 100.0, 1.0], readonly=True)
    # imgsettings
    add("imagesize", GP_WIDGET_RADIO, "Large", ["Large", "Medium", "Small"])
    add("iso", GP_WIDGET_RADIO, "800",
        ["Auto ISO", "50 Multi Frame Noise Reduction", "100", "125", "160", "200",
         "250", "320", "400", "500", "640", "800", "1000", "1250", "1600", "2000",
         "2500", "3200", "4000", "5000", "6400", "8000", "10000", "12800", "16000",
         "20000", "25600", "32000"])
    add("colortemperature", GP_WIDGET_RANGE, 6200.0, wrange=[2500.0, 9900.0, 100.0])
    add("whitebalance", GP_WIDGET_RADIO, "Automatic",
        ["Automatic", "Daylight", "Shade", "Cloudy", "Tungsten",
         "Fluorescent: Warm White", "Fluorescent: Cold White",
         "Fluorescent: Day White", "Fluorescent: Daylight", "Flash",
         "Underwater: Auto", "Choose Color Temperature", "Preset 1", "Preset 2",
         "Preset 3"])
    # capturesettings (EV is READONLY in M on this body — hardware fact)
    add("exposurecompensation", GP_WIDGET_RADIO, "0",
        ["5", "4.7", "4.3", "4", "3.7", "3.3", "3", "2.7", "2.3", "2", "1.7",
         "1.3", "1", "0.7", "0.3", "0", "-0.3", "-0.7", "-1", "-1.3", "-1.7",
         "-2", "-2.3", "-2.7", "-3", "-3.3", "-3.7", "-4", "-4.3", "-4.7", "-5"],
        readonly=True)
    add("flashmode", GP_WIDGET_RADIO, "Fill flash",
        ["Flash off", "Automatic Flash", "Fill flash", "Slow Sync", "Rear Curtain Sync"])
    add("f-number", GP_WIDGET_RADIO, "f/5.6",
        ["f/4", "f/4.5", "f/5", "f/5.6", "f/6.3", "f/7.1", "f/8", "f/9", "f/10",
         "f/11", "f/13", "f/14", "f/16", "f/18", "f/20", "f/22"])
    add("imagequality", GP_WIDGET_RADIO, "RAW", ["RAW", "RAW+JPEG", "JPEG"])
    add("jpegquality", GP_WIDGET_RADIO, "Std", ["X.Fine", "Fine", "Std"])
    add("focusmode", GP_WIDGET_RADIO, "Automatic",
        ["Automatic", "AF-A", "AF-C", "DMF", "Manual"])
    add("expprogram", GP_WIDGET_RADIO, "M",
        ["P", "A", "S", "M", "Movie (P)", "Movie (A)", "Movie (S)", "Movie (M)",
         "Intelligent Auto"], readonly=True)
    add("aspectratio", GP_WIDGET_RADIO, "3:2", ["3:2", "4:3", "16:9", "1:1"])
    add("capturemode", GP_WIDGET_RADIO, "Single Shot",
        ["Single Shot", "Continuous Shooting Lo", "Continuous Shooting Mid",
         "Continuous Shooting Hi", "Continuous Shooting Hi+",
         "Self Timer 10 Sec.", "Self Timer 5 Sec.", "Self Timer 2 Sec."])
    add("exposuremetermode", GP_WIDGET_RADIO, "Multi",
        ["Multi", "Center", "Spot Standard", "Spot Large", "Entire Screen Avg.",
         "Highlight"])
    add("shutterspeed", GP_WIDGET_RADIO, "1/60",
        ["0/0", "30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10",
         "25/10", "2", "16/10", "13/10", "1", "8/10", "6/10", "5/10", "4/10",
         "1/3", "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15", "1/20",
         "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125",
         "1/160", "1/200", "1/250", "1/320", "1/400", "1/500", "1/640", "1/800",
         "1/1000", "1/1250", "1/1600", "1/2000", "1/2500", "1/3200", "1/4000",
         "1/5000", "1/6400", "1/8000", "Bulb"])
    add("focusarea", GP_WIDGET_RADIO, "Center",
        ["Zone", "Center", "Flexible Spot: S", "Expand Flexible Spot"])
    add("dro", GP_WIDGET_RADIO, "Off",
        ["Off", "DRO Auto", "DRO Lv1", "DRO Lv2", "DRO Lv3", "DRO Lv4", "DRO Lv5"])
    for name, value in (cfg("widget_overrides") or {}).items():
        if name in w:
            w[name].value = value
    return w
