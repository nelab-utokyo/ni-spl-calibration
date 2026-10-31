"""Read-only logarithmic-band spectrum meters in dBV (RMS relative to 1 V)."""

from __future__ import annotations

import numpy as np


def spectrum_bands(samples, sample_rate_hz):
    values = np.asarray(samples, dtype=float)
    if values.ndim != 1 or len(values) < 16 or not np.all(np.isfinite(values)):
        raise ValueError("Spectrum requires >= 16 finite mono samples")
    nyquist = sample_rate_hz / 2
    low = min(20.0, nyquist / 4)
    count = max(1, int(np.ceil(3 * np.log2(nyquist / low))))
    edges = np.geomspace(low, nyquist, count + 1)
    window = np.hanning(len(values))
    transformed = np.fft.rfft((values - values.mean()) * window)
    power = np.abs(transformed) ** 2 / (len(values) * np.sum(window**2))
    power[1:] *= 2
    if len(values) % 2 == 0:
        power[-1] /= 2
    frequencies = np.fft.rfftfreq(len(values), 1 / sample_rate_hz)
    bands, _ = np.histogram(frequencies, bins=edges, weights=power)
    return {
        "edges_hz": edges,
        "centers_hz": np.sqrt(edges[:-1] * edges[1:]),
        "levels_dbv": 10 * np.log10(np.maximum(bands, 1e-24)),
    }
