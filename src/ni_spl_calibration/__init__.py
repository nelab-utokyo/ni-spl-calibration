"""Calibrate any finite mono waveform through one NI DAQ AI/AO pair."""

from .calibration import (
    CalibrationError,
    CalibrationResult,
    MeasurementCancelled,
    Reference,
    apply_calibration,
    calibrate,
    load_reference,
    load_result,
    measure_reference,
)
from .config import DaqSettings, MeasurementSettings, Setup, load_setup
from .hardware import NIDaqHardware, SimulatedHardware
from .stimulus import Stimulus, from_generator, load_stimulus, load_wav, save_stimulus

__all__ = [
    "CalibrationError",
    "CalibrationResult",
    "MeasurementCancelled",
    "Reference",
    "apply_calibration",
    "calibrate",
    "load_reference",
    "load_result",
    "measure_reference",
    "DaqSettings",
    "MeasurementSettings",
    "Setup",
    "load_setup",
    "NIDaqHardware",
    "SimulatedHardware",
    "Stimulus",
    "from_generator",
    "load_stimulus",
    "load_wav",
    "save_stimulus",
]
