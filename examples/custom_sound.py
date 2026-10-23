"""A finite AM tone; replace this function with your own stimulus generator."""

import numpy as np


def make_sound(*, sample_rate_hz, duration_s=2.0, carrier_hz=10000.0, am_hz=8.0, seed=42):
    # seed is accepted so this interface can also serve stochastic generators.
    if duration_s <= 0 or not 0 < carrier_hz - am_hz < carrier_hz + am_hz < sample_rate_hz / 2:
        raise ValueError("Positive duration and carrier/sidebands below Nyquist are required")
    t = np.arange(round(duration_s * sample_rate_hz)) / sample_rate_hz
    envelope = (1 + np.sin(2 * np.pi * am_hz * t)) / 2
    waveform = 0.8 * envelope * np.sin(2 * np.pi * carrier_hz * t)
    ramp_count = min(round(0.01 * sample_rate_hz), len(t) // 2)
    if ramp_count:
        ramp = np.sin(np.linspace(0, np.pi / 2, ramp_count)) ** 2
        waveform[:ramp_count] *= ramp
        waveform[-ramp_count:] *= ramp[::-1]
    return waveform
