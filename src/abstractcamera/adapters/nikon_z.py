"""Nikon Z family adapter.

The mechanics ARE GenericPtpAdapter — that code was extracted verbatim from
the controller after a year of hardware hardening on a Z6 II. This subclass
only claims the family identity so status()/capabilities can say "Nikon Z"
and future Nikon-specific divergences have a named home.
"""

from __future__ import annotations

from abstractcamera.adapters.base import GenericPtpAdapter


class NikonZAdapter(GenericPtpAdapter):
    family = "nikon_z"
    display_name = "Nikon Z"
    # Hardware fact (Z6 II): preview frames FAIL during a still exposure and
    # those failures would count toward the disconnect watchdog.
    preview_survives_exposure = False

    def capabilities(self, config_cache: dict) -> dict:
        caps = super().capabilities(config_cache)
        caps["family"] = self.family
        caps["display_name"] = self.display_name
        # Human labels for the Save To dial (value -> label); generic bodies
        # fall back to the frontend's regex-based labels.
        caps["save_to"]["labels"] = {
            "Internal RAM": "Camera buffer → this computer only",
            "Memory card": "Memory card (recommended)",
        }
        return caps
