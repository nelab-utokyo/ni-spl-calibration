"""Live input metering shared by monitoring, calibration and test playback."""

from dataclasses import replace

import numpy as np

from .analysis import band_power
from .spectrum import spectrum_bands


def input_metrics(values, setup, reference=None):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or len(values) < 16 or not np.all(np.isfinite(values)):
        raise ValueError("Invalid microphone capture")
    clipped = bool(
        np.min(values) <= setup.daq.input_min_v * setup.measurement.input_clip_fraction
        or np.max(values) >= setup.daq.input_max_v * setup.measurement.input_clip_fraction
    )
    settings = replace(setup.measurement, start_s=0, stop_s=None, acoustic_delay_s=0)
    power = 0.0
    level_note = None
    if reference is not None:
        try:
            power = band_power(values, setup.daq.sample_rate_hz, settings)
        except ValueError as error:
            # A short live frame may not resolve the calibration band. This must
            # never interrupt playback or change the full-window calibration.
            level_note = str(error)
    level = (
        float(10 * np.log10(power / reference.sensitivity_v_per_pa**2 / (20e-6) ** 2))
        if reference is not None and power > 0 and not clipped
        else None
    )
    spectrum = spectrum_bands(values, setup.daq.sample_rate_hz)
    return {
        "rms_v": float(np.sqrt(np.mean(values**2))),
        "peak_v": float(np.max(np.abs(values))),
        "level_spl_db": level,
        "clipped": clipped,
        "calibrated": reference is not None,
        "level_note": level_note,
        "spectrum": {key: value.tolist() for key, value in spectrum.items()},
    }


class LiveMeter:
    def __init__(self, setup, reference, emit):
        self.setup, self.reference, self.emit = setup, reference, emit
        self.pending = np.empty(0)
        self.received = 0
        self.block_size = max(16, round(setup.daq.sample_rate_hz * 0.1))

    def push(self, samples):
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= self.block_size:
            values, self.pending = self.pending[: self.block_size], self.pending[self.block_size :]
            self._emit(values)

    def flush(self):
        """Display the final short frame without mixing background and playback."""
        values, self.pending = self.pending, np.empty(0)
        if len(values) >= 16:
            self._emit(values)
        else:
            self.received += len(values)

    def _emit(self, values):
        self.received += len(values)
        self.emit(
            {
                "kind": "monitor",
                "samples": values,
                "sample_rate_hz": self.setup.daq.sample_rate_hz,
                "duration_s": self.received / self.setup.daq.sample_rate_hz,
                **input_metrics(values, self.setup, self.reference),
            }
        )
