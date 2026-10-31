from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .analysis import band_power, tone_rms, validate_capture
from .config import Setup
from .hardware import Hardware, NIDaqHardware, output_guard
from .stimulus import Stimulus


def timestamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Reference:
    measured_at: str
    setup_sha256: str
    sensitivity_v_per_pa: float
    rms_v: float
    estimated_frequency_hz: float
    block_span_db: float
    tone_power_fraction: float
    hardware: dict
    setup: dict
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1 or len(self.setup_sha256) != 64:
            raise ValueError("Unsupported or invalid reference record")
        if not all(
            math.isfinite(v)
            for v in (
                self.sensitivity_v_per_pa,
                self.rms_v,
                self.estimated_frequency_hz,
                self.block_span_db,
                self.tone_power_fraction,
            )
        ):
            raise ValueError("Reference metrics must be finite")
        if (
            self.sensitivity_v_per_pa <= 0
            or self.rms_v <= 0
            or not 0 < self.tone_power_fraction <= 1
        ):
            raise ValueError("Invalid reference metrics")
        object.__setattr__(self, "setup", json.loads(json.dumps(self.setup, allow_nan=False)))


class CalibrationError(RuntimeError):
    def __init__(self, message, iterations=()):
        super().__init__(message)
        self.iterations = list(iterations)


class MeasurementCancelled(CalibrationError):
    """A cooperative stop requested between finite hardware operations."""


def _check_cancelled(cancel_requested, iterations=()):
    if cancel_requested is not None and cancel_requested():
        raise MeasurementCancelled("Measurement cancelled", iterations)


@dataclass(frozen=True)
class CalibrationResult:
    calibrated_at: str
    target_spl_db: float
    measured_spl_db: float
    scale_v: float
    output_peak_v: float
    output_rms_v: float
    stimulus: dict
    setup: dict
    reference: dict
    hardware: dict
    iterations: list[dict]
    status: str = "complete"
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1 or self.status != "complete":
            raise ValueError("Expected a completed version-1 calibration result")
        if not all(
            math.isfinite(v)
            for v in (
                self.target_spl_db,
                self.measured_spl_db,
                self.scale_v,
                self.output_peak_v,
                self.output_rms_v,
            )
        ):
            raise ValueError("Result metrics must be finite")
        if min(self.scale_v, self.output_peak_v, self.output_rms_v) <= 0:
            raise ValueError("Result voltages must be positive")
        for name in ("stimulus", "setup", "reference", "hardware", "iterations"):
            object.__setattr__(
                self, name, json.loads(json.dumps(getattr(self, name), allow_nan=False))
            )


def _identity(description):
    return {key: value for key, value in description.items() if key != "runtime"}


def validate_reference(setup: Setup, reference: Reference, hardware: Hardware | None = None):
    if reference.setup_sha256 != setup.reference_fingerprint:
        raise ValueError("Reference belongs to a different setup; acquire a new reference")
    if hardware is not None and _identity(reference.hardware) != _identity(hardware.describe()):
        raise ValueError(
            "Reference hardware/backend differs; acquire a new reference on this hardware"
        )
    if hardware is not None and hardware.settings != setup.daq:
        raise ValueError("Hardware DAQ settings differ from the setup")


def measure_reference(
    setup: Setup, *, hardware: Hardware | None = None, cancel_requested=None
) -> Reference:
    _check_cancelled(cancel_requested)
    hardware = hardware if hardware is not None else NIDaqHardware(setup.daq)
    if hardware.settings != setup.daq:
        raise ValueError("Hardware DAQ settings differ from the setup")
    fs = setup.daq.sample_rate_hz
    count = round(setup.reference_duration_s * fs)
    with output_guard(hardware):
        _check_cancelled(cancel_requested)
        values = validate_capture(
            hardware.acquire(count), count, setup.daq, setup.measurement.input_clip_fraction
        )
        _check_cancelled(cancel_requested)
        frequencies = np.fft.rfftfreq(count, 1 / fs)
        spectrum = np.abs(np.fft.rfft((values - np.mean(values)) * np.hanning(count)))
        spectrum[0] = 0
        peak = int(np.argmax(spectrum))
        estimated = float(frequencies[peak])
        if 0 < peak < len(spectrum) - 1:
            left, center, right = np.log(np.maximum(spectrum[peak - 1 : peak + 2], 1e-300))
            denominator = left - 2 * center + right
            if denominator != 0:
                estimated += float(0.5 * (left - right) / denominator * fs / count)
        if abs(estimated - setup.reference_frequency_hz) > max(
            2, setup.reference_frequency_hz * 0.01
        ):
            raise RuntimeError(
                "Reference dominant frequency does not match the acoustic calibrator"
            )
        # A real calibrator can differ slightly from nominal 1 kHz. Fitting its
        # measured frequency avoids attenuating the estimate through beating.
        rms = tone_rms(values, fs, estimated)
        total_power = float(np.var(values))
        fraction = min(1.0, rms**2 / total_power) if total_power > 0 else 0.0
        if fraction < 0.9 or rms <= 0:
            raise RuntimeError(
                "Reference has insufficient pure-tone power; check the acoustic calibrator"
            )
        block = max(16, round(fs * 0.1))
        levels = np.array(
            [
                tone_rms(values[i : i + block], fs, estimated)
                for i in range(0, count - block + 1, block)
            ]
        )
        if len(levels) < 2 or np.min(levels) <= 0:
            raise RuntimeError("Reference needs at least two valid stability blocks")
        low, high = np.percentile(levels, [5, 95])
        span = float(20 * np.log10(high / low))
        if span > setup.reference_max_span_db:
            raise RuntimeError(f"Acoustic reference is unstable ({span:.3f} dB span)")
        pressure = 20e-6 * 10 ** (setup.reference_level_db / 20)
        _check_cancelled(cancel_requested)
        return Reference(
            timestamp(),
            setup.reference_fingerprint,
            rms / pressure,
            rms,
            estimated,
            span,
            fraction,
            hardware.describe(),
            asdict(setup),
        )


def measurement_bounds(stimulus: Stimulus, setup: Setup) -> tuple[int, int, int]:
    if stimulus.sample_rate_hz != setup.daq.sample_rate_hz:
        raise ValueError(
            "Stimulus and DAQ sample rates differ; resample explicitly and recalibrate"
        )
    settings = setup.measurement
    fs = stimulus.sample_rate_hz
    duration = len(stimulus.samples) / fs
    stop = duration if settings.stop_s is None else settings.stop_s
    if stop > duration + 1e-12 or settings.start_s >= stop:
        raise ValueError("Measurement window is outside the stimulus")
    first, last = round(settings.start_s * fs), round(stop * fs)
    if last - first < 16:
        raise ValueError("Measurement window must contain at least 16 samples")
    # Preflight bin selection and response-curve coverage before any hardware access.
    band_power(np.zeros(last - first), fs, settings)
    return first, last, round(settings.acoustic_delay_s * fs)


def calibrate(
    stimulus: Stimulus,
    *,
    setup: Setup,
    reference: Reference,
    target_spl_db: float,
    hardware: Hardware | None = None,
    on_iteration=None,
    cancel_requested=None,
) -> CalibrationResult:
    """Find volts per digital unit. Measure the exact same waveform each time."""
    if not math.isfinite(target_spl_db) or not -100 <= target_spl_db <= 180:
        raise ValueError("target_spl_db must be finite and between -100 and 180")
    first, last, delay = measurement_bounds(stimulus, setup)
    validate_reference(setup, reference)
    _check_cancelled(cancel_requested)
    hardware = hardware if hardware is not None else NIDaqHardware(setup.daq)
    validate_reference(setup, reference, hardware)
    settings = setup.measurement
    peak = float(np.max(np.abs(stimulus.samples)))
    maximum_scale = setup.daq.max_peak_v / peak
    scale = min(settings.start_scale_v, maximum_scale)
    history = []
    confirmations = 0
    fs = stimulus.sample_rate_hz
    try:
        with output_guard(hardware):
            _check_cancelled(cancel_requested, history)
            count = last - first
            background = validate_capture(
                hardware.acquire(count), count, setup.daq, settings.input_clip_fraction
            )
            background_power = band_power(background, fs, settings)
            for number in range(1, settings.max_iterations + 1):
                _check_cancelled(cancel_requested, history)
                # Include the delayed acoustic tail and a final 0 V sample.
                output = np.concatenate((stimulus.samples * scale, np.zeros(delay + 1)))
                samples = validate_capture(
                    hardware.play_and_record(output),
                    len(output),
                    setup.daq,
                    settings.input_clip_fraction,
                )
                captured = samples[first + delay : last + delay]
                measured_power = band_power(captured, fs, settings)
                signal_power = max(0.0, measured_power - background_power)
                snr = (
                    float(10 * math.log10(signal_power / background_power))
                    if signal_power > 0 and background_power > 0
                    else None
                )
                detectable = signal_power > 0 and (
                    background_power == 0 or snr >= settings.minimum_snr_db
                )
                level = (
                    float(
                        20
                        * math.log10(
                            math.sqrt(signal_power) / reference.sensitivity_v_per_pa / 20e-6
                        )
                    )
                    if detectable
                    else None
                )
                error = target_spl_db - level if level is not None else None
                item = {
                    "iteration": number,
                    "scale_v": scale,
                    "output_peak_v": scale * peak,
                    "measured_spl_db": level,
                    "error_db": error,
                    "signal_to_noise_db": snr,
                    "background_corrected_rms_v": math.sqrt(signal_power),
                    "background_rms_v": math.sqrt(background_power),
                    "input_peak_v": float(np.max(np.abs(samples))),
                }
                history.append(item)
                if on_iteration is not None:
                    on_iteration(dict(item))
                _check_cancelled(cancel_requested, history)
                if error is not None and abs(error) <= settings.tolerance_db:
                    confirmations += 1
                    if confirmations >= settings.confirmations:
                        return CalibrationResult(
                            calibrated_at=timestamp(),
                            target_spl_db=target_spl_db,
                            measured_spl_db=level,
                            scale_v=scale,
                            output_peak_v=scale * peak,
                            output_rms_v=scale * float(np.sqrt(np.mean(stimulus.samples**2))),
                            stimulus=stimulus.describe(),
                            setup=asdict(setup),
                            reference=asdict(reference),
                            hardware=hardware.describe(),
                            iterations=history,
                        )
                    continue  # Confirm at exactly the same voltage, never renormalize.
                confirmations = 0
                adjustment = (
                    settings.max_upward_step_db
                    if error is None
                    else min(error, settings.max_upward_step_db)
                )
                next_scale = min(maximum_scale, scale * 10 ** (adjustment / 20))
                if next_scale <= 0 or math.isclose(next_scale, scale, rel_tol=1e-12, abs_tol=0):
                    raise CalibrationError(
                        "Target/SNR not reached at the output voltage limit", history
                    )
                scale = next_scale
            raise CalibrationError("Search did not converge within max_iterations", history)
    except KeyboardInterrupt as error:
        error.iterations = list(history)
        raise
    except Exception as error:
        if isinstance(error, CalibrationError):
            raise
        wrapped = CalibrationError(str(error), history)
        for note in getattr(error, "__notes__", ()):
            wrapped.add_note(note)
        raise wrapped from error


def apply_calibration(stimulus: Stimulus, result: CalibrationResult) -> np.ndarray:
    """Return volts for the exact calibrated waveform, without adding padding."""
    if stimulus.sha256 != result.stimulus["sha256"]:
        raise ValueError("Waveform/sample rate changed; recalibrate this stimulus")
    output = stimulus.samples * result.scale_v
    if np.max(np.abs(output)) > result.setup["daq"]["max_peak_v"] * (1 + 1e-12):
        raise ValueError("Calibrated waveform exceeds the stored peak-voltage limit")
    return output


def load_reference(path: str | Path) -> Reference:
    from .storage import read_json

    return Reference(**read_json(path))


def load_result(path: str | Path) -> CalibrationResult:
    from .storage import read_json

    return CalibrationResult(**read_json(path))
