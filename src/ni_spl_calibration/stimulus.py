"""Materialize sources once; convert WAV only to an explicitly supplied target rate."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def _validate_sample_rate(sample_rate_hz: int) -> None:
    if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int):
        raise ValueError("sample_rate_hz must be an integer")
    if sample_rate_hz < 2:
        raise ValueError("sample_rate_hz must be >= 2")


@dataclass(frozen=True)
class Stimulus:
    samples: np.ndarray
    sample_rate_hz: int
    provenance: dict = field(default_factory=lambda: {"kind": "array"})

    def __post_init__(self):
        _validate_sample_rate(self.sample_rate_hz)
        samples = np.array(self.samples, dtype="<f8", copy=True)
        if samples.ndim != 1 or samples.size < 16:
            raise ValueError("Provide at least 16 mono samples")
        if not np.all(np.isfinite(samples)):
            raise ValueError("Samples must be finite")
        if np.max(np.abs(samples)) > 1:
            raise ValueError("Samples must be within [-1, 1]; normalize explicitly if intended")
        if np.std(samples) <= np.finfo(float).eps:
            raise ValueError("Silent or constant waveforms cannot be calibrated")
        samples.flags.writeable = False
        object.__setattr__(self, "samples", samples)
        # Make metadata JSON-serializable and independent of the caller's objects.
        object.__setattr__(
            self, "provenance", json.loads(json.dumps(self.provenance, allow_nan=False))
        )

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256(str(self.sample_rate_hz).encode() + b"\0")
        digest.update(self.samples.tobytes())
        return digest.hexdigest()

    def describe(self) -> dict:
        return {
            "sha256": self.sha256,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": len(self.samples),
            "duration_s": len(self.samples) / self.sample_rate_hz,
            "digital_rms": float(np.sqrt(np.mean(self.samples**2))),
            "digital_peak": float(np.max(np.abs(self.samples))),
            "provenance": self.provenance,
        }


def load_wav(
    path: str | Path, *, sample_rate_hz: int | None = None, channel_mode: str = "mono"
) -> Stimulus:
    """Read WAV; optionally select/mix stereo and resample without normalization.

    Omit sample_rate_hz to retain the original samples and rate. A matching target
    also preserves the samples exactly. Conversion uses an anti-aliasing FIR filter
    and records the original rate/count; the returned samples are what to calibrate.
    The default channel_mode='mono' rejects stereo. 'mix', 'left', and 'right'
    explicitly select the one-channel waveform sent to AO.
    """
    if sample_rate_hz is not None:
        _validate_sample_rate(sample_rate_hz)
    if channel_mode not in {"mono", "mix", "left", "right"}:
        raise ValueError("Invalid WAV channel mode")
    path = Path(path)
    contents = path.read_bytes()
    with sf.SoundFile(BytesIO(contents)) as audio:
        if audio.format not in {"WAV", "WAVEX", "RF64"}:
            raise ValueError("Expected a WAV file")
        if audio.channels != 1 and channel_mode == "mono":
            raise ValueError("Only mono WAV is supported; select/mix channels explicitly")
        if audio.channels > 2:
            raise ValueError("Select a mono or stereo WAV")
        if channel_mode == "right" and audio.channels < 2:
            raise ValueError("Right-channel selection requires a stereo WAV")
        channels = audio.channels
        original = audio.read(dtype="float64", always_2d=True)
        samples = (
            original.mean(axis=1)
            if channel_mode == "mix"
            else original[:, 1]
            if channel_mode == "right"
            else original[:, 0]
        )
        rate = audio.samplerate
    stimulus = Stimulus(
        samples,
        rate,
        {
            "kind": "wav",
            "name": path.name,
            "file_sha256": hashlib.sha256(contents).hexdigest(),
            "source_channels": channels,
            "channel_mode": channel_mode,
            "file_digital_rms": float(np.sqrt(np.mean(original**2))),
            "file_digital_peak": float(np.max(np.abs(original))),
        },
    )
    if sample_rate_hz is None or sample_rate_hz == rate:
        return stimulus
    divisor = math.gcd(rate, sample_rate_hz)
    samples = resample_poly(
        stimulus.samples,
        sample_rate_hz // divisor,
        rate // divisor,
        window=("kaiser", 5.0),
        padtype="constant",
    )
    if np.max(np.abs(samples)) > 1:
        raise ValueError(
            "Resampled WAV exceeds [-1, 1]; reduce the source amplitude explicitly "
            "before calibration"
        )
    return Stimulus(
        samples,
        sample_rate_hz,
        {
            **stimulus.provenance,
            "resampling": {
                "method": "scipy.signal.resample_poly",
                "source_sample_rate_hz": rate,
                "source_sample_count": len(stimulus.samples),
                "source_digital_rms": float(np.sqrt(np.mean(stimulus.samples**2))),
                "source_digital_peak": float(np.max(np.abs(stimulus.samples))),
                "target_sample_rate_hz": sample_rate_hz,
                "window": ["kaiser", 5.0],
                "padtype": "constant",
            },
        },
    )


def from_generator(generator: Callable, *, sample_rate_hz: int, **parameters) -> Stimulus:
    """Call generator(sample_rate_hz=..., **parameters) exactly once."""
    _validate_sample_rate(sample_rate_hz)
    if not callable(generator):
        raise TypeError("generator must be callable")
    module = getattr(generator, "__module__", type(generator).__module__)
    name = getattr(generator, "__qualname__", type(generator).__qualname__)
    metadata = {
        "kind": "generator",
        "callable": f"{module}:{name}",
        "parameters": parameters,
    }
    json.dumps(metadata, allow_nan=False)  # Reject unsupported metadata before running code.
    samples = generator(sample_rate_hz=sample_rate_hz, **parameters)
    return Stimulus(samples, sample_rate_hz, metadata)


def save_stimulus(stimulus: Stimulus, path: str | Path) -> None:
    """Losslessly store the exact digital samples used in the search."""
    with Path(path).open("xb") as handle:
        np.savez_compressed(
            handle,
            samples=stimulus.samples,
            sample_rate_hz=stimulus.sample_rate_hz,
            provenance=json.dumps(stimulus.provenance),
        )


def load_stimulus(path: str | Path) -> Stimulus:
    with np.load(path, allow_pickle=False) as data:
        return Stimulus(
            data["samples"], int(data["sample_rate_hz"]), json.loads(str(data["provenance"]))
        )
