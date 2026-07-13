"""Package exceptions."""


class CameraControlError(RuntimeError):
    """A camera operation could not be performed (honest, user-facing text)."""


# Framework-style alias; both names refer to the same class.
CameraError = CameraControlError
