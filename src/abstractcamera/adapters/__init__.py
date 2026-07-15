"""Camera family adapters: model-string dispatch to the family's behavior.

select_adapter() is the single decision point mapping a detected camera model
to its family adapter. Unknown models get GenericPtpAdapter — the exact
behavior hardware-validated on the Nikon Z6 II.
"""

from __future__ import annotations

from abstractcamera.adapters.base import (
    ActionReceipt,
    CameraAdapter,
    CaptureTiming,
    ClassifiedEvent,
    ConnectDefault,
    EscalationResult,
    GENERIC_CONFIG_WIDGET_NAMES,
    GenericPtpAdapter,
    MovieReceipt,
    WriteReceipt,
)
from abstractcamera.adapters.nikon_z import NikonZAdapter
from abstractcamera.adapters.sony_alpha import SonyAlphaAdapter

__all__ = [
    "ActionReceipt",
    "CameraAdapter",
    "CaptureTiming",
    "ClassifiedEvent",
    "ConnectDefault",
    "EscalationResult",
    "GENERIC_CONFIG_WIDGET_NAMES",
    "GenericPtpAdapter",
    "MovieReceipt",
    "NikonZAdapter",
    "SonyAlphaAdapter",
    "WriteReceipt",
    "select_adapter",
]


def select_adapter(model: str | None, transport_module) -> CameraAdapter:
    """Family dispatch on the abilities model string.

    Substring matching on the vendor is deliberate: libgphoto2 model strings
    vary per body ("Sony DSC-A7r IV (Control)", "Sony Alpha-A7 III",
    "Nikon Z 6II", ...) but always carry the vendor name. Webcam sessions
    self-identify with a "Webcam:" prefix.
    """
    text = (model or "").lower()
    if text.startswith("webcam:"):
        from abstractcamera.adapters.webcam import WebcamAdapter

        return WebcamAdapter(transport_module)
    if "dwarf" in text:
        from abstractcamera.adapters.dwarf import DwarfAdapter

        return DwarfAdapter(transport_module)
    if "sony" in text or "ilce" in text:
        return SonyAlphaAdapter(transport_module)
    if "nikon" in text:
        return NikonZAdapter(transport_module)
    return GenericPtpAdapter(transport_module)
