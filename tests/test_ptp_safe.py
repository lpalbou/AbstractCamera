"""ptp_safe: the NULL-guarded widget reader (the 2026-07-12 Sony segfault
class). True NULL values need a real body mid-wake — these tests pin the
DISPATCH rules (what goes through ctypes, what falls back) and the sim
compatibility; the hardware validation script covers the live path."""

import unittest

from abstractcamera import ptp_safe, wire


class PlainWidget:
    """Simulator-shaped widget: plain Python, no swig `.this`."""

    def __init__(self, widget_type, value, choices=()):
        self._type = widget_type
        self._value = value
        self._choices = list(choices)

    def get_type(self):
        return self._type

    def get_value(self):
        return self._value

    def count_choices(self):
        return len(self._choices)

    def get_choice(self, index):
        return self._choices[index]


class Dispatch(unittest.TestCase):
    def test_plain_string_widget_falls_back(self):
        widget = PlainWidget(wire.GP_WIDGET_RADIO, "1/60", ["1/60", "1/125"])
        self.assertEqual(ptp_safe.widget_value(widget), "1/60")
        self.assertEqual(ptp_safe.widget_choices(widget), ["1/60", "1/125"])

    def test_numeric_widget_never_routes_through_ctypes(self):
        widget = PlainWidget(wire.GP_WIDGET_RANGE, 5500.0)
        self.assertEqual(ptp_safe.widget_value(widget), 5500.0)

    def test_broken_get_type_degrades_to_get_value(self):
        class NoType:
            def get_type(self):
                raise RuntimeError("no type")

            def get_value(self):
                return "fallback"

        self.assertEqual(ptp_safe.widget_value(NoType()), "fallback")

    def test_swig_pointer_extraction(self):
        class FakeThis:
            def __int__(self):
                return 0xDEADBEEF

        class SwigLike:
            this = FakeThis()

        self.assertEqual(ptp_safe._swig_pointer(SwigLike()), 0xDEADBEEF)
        self.assertIsNone(ptp_safe._swig_pointer(PlainWidget(wire.GP_WIDGET_TEXT, "x")))


class SimIntegration(unittest.TestCase):
    def test_simulator_widgets_read_identically(self):
        import abstractcamera.sim.gphoto2 as sim

        sim.reset()
        camera = sim.Camera()
        camera.init()
        widget = camera.get_single_config("iso")
        self.assertEqual(ptp_safe.widget_value(widget), widget.get_value())
        self.assertEqual(ptp_safe.widget_choices(widget),
                         [widget.get_choice(i) for i in range(widget.count_choices())])
        camera.exit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
