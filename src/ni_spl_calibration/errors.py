"""Shared errors for calibration, playback and hardware cancellation."""


class CalibrationError(RuntimeError):
    def __init__(self, message, iterations=()):
        super().__init__(message)
        self.iterations = list(iterations)


class MeasurementCancelled(CalibrationError):
    """A stop requested while hardware work is in progress."""
