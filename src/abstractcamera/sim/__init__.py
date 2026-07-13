"""Simulated camera transports for camera-less development and testing.

abstractcamera.sim.gphoto2 is a module shaped exactly like python-gphoto2's
`gphoto2`, with scriptable bodies (Nikon Z6 II and Sony A7R IV personalities
reproducing hardware-measured quirks). Activate globally with
ABSTRACTCAMERA_FAKE=1, per-manager with
CameraManager(driver=FakeDriver(module)), or per-scenario via
sim.gphoto2.configure(...).
"""
