"""Simulator transport driver.

Wraps any module shaped like the gphoto2 module (by default the package
simulator, abstractcamera.sim.gphoto2). This is ALSO the test seam that
replaced the old host-side `camera_control.gp = fake_gp` monkeypatch:
`CameraManager(driver=FakeDriver(fake_module))`.
"""

from __future__ import annotations


class FakeDriver:
    driver_id = "fake"

    def __init__(self, module=None):
        if module is None:
            import abstractcamera.sim.gphoto2 as module
        self._module = module

    @property
    def transport(self):
        return self._module

    def available(self) -> bool:
        return True

    def list_cameras(self) -> list[dict]:
        camera_list = self._module.Camera.autodetect()
        return [
            {
                "id": f"fake:{camera_list.get_value(i)}",
                "transport": "fake",
                "name": str(camera_list.get_name(i)),
                "address": str(camera_list.get_value(i)),
                "name_confidence": "reported",
            }
            for i in range(camera_list.count())
        ]

    def prepare_connect(self, camera_id: str | None) -> None:
        pass  # nothing to release for a simulated transport

    def create_session(self, camera_id: str | None):
        return self._module.Camera()

    def select_adapter(self, model: str | None):
        from abstractcamera.adapters import select_adapter

        return select_adapter(model, self._module)
