"""Time-averaged mean-square measurements; preserve intentional modulation."""

from __future__ import annotations

import math

import numpy as np

from .config import DaqSettings, MeasurementSettings


def validate_capture(samples, count: int, daq: DaqSettings, clip_fraction: float) -> np.ndarray:
    values = np.asarray(samples, dtype=float)
    if values.ndim != 1 or len(values) != count or not np.all(np.isfinite(values)):
        raise RuntimeError("Incomplete, non-mono, or non-finite AI capture")
    # Check both rails before removing DC or restricting the measurement window.
    if (
        np.min(values) <= daq.input_min_v * clip_fraction
        or np.max(values) >= daq.input_max_v * clip_fraction
    ):
        raise RuntimeError("AI clipping guard reached; check input range and microphone amplifier")
    return values


def band_power(samples: np.ndarray, sample_rate_hz: int, settings: MeasurementSettings) -> float:
    values = np.asarray(samples, dtype=float)
    if values.ndim != 1 or len(values) < 16 or not np.all(np.isfinite(values)):
        raise ValueError("Analysis requires >= 16 finite mono samples")
    spectrum = np.fft.rfft(values - np.mean(values))
    frequencies = np.fft.rfftfreq(len(values), 1 / sample_rate_hz)
    band = frequencies > 0  # AC sound pressure; DC offset is excluded.
    if settings.band_hz is not None:
        low, high = settings.band_hz
        if high > sample_rate_hz / 2:
            raise ValueError("Analysis band exceeds Nyquist")
        band &= (frequencies >= low) & (frequencies <= high)
    if not np.any(band):
        raise ValueError("Analysis band contains no FFT bins for this measurement window")
    weights = np.full(len(frequencies), 2.0)
    weights[0] = 1.0
    if len(values) % 2 == 0:
        weights[-1] = 1.0
    if settings.frequency_correction_db:
        curve = np.asarray(settings.frequency_correction_db)
        selected = frequencies[band]
        if selected[0] < curve[0, 0] or selected[-1] > curve[-1, 0]:
            raise ValueError("Microphone correction curve must cover the entire analysis band")
        correction = np.interp(selected, curve[:, 0], curve[:, 1])
        weights[band] *= 10 ** (correction / 10)
    power = float(np.sum(weights[band] * np.abs(spectrum[band]) ** 2) / len(values) ** 2)
    if not math.isfinite(power):
        raise ValueError("Non-finite corrected input power")
    return power


def tone_rms(samples: np.ndarray, sample_rate_hz: int, frequency_hz: float) -> float:
    angle = 2 * np.pi * frequency_hz * np.arange(len(samples)) / sample_rate_hz
    design = np.column_stack((np.cos(angle), np.sin(angle), np.ones(len(samples))))
    coefficients, *_ = np.linalg.lstsq(design, samples, rcond=None)
    return float(np.hypot(coefficients[0], coefficients[1]) / np.sqrt(2))
