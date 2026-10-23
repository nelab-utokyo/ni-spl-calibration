"""Materialize every source once; never normalize or resample implicitly."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class Stimulus:
    samples: np.ndarray
    sample_rate_hz: int
    provenance: dict = field(default_factory=lambda: {"kind": "array"})

    def __post_init__(self):
        if isinstance(self.sample_rate_hz, bool) or not isinstance(self.sample_rate_hz, int):
            raise ValueError("sample_rate_hz must be an integer")
        if self.sample_rate_hz < 2:
            raise ValueError("sample_rate_hz must be >= 2")
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


def load_wav(path: str | Path) -> Stimulus:
    path = Path(path)
    contents = path.read_bytes()
    with sf.SoundFile(BytesIO(contents)) as audio:
        if audio.format not in {"WAV", "WAVEX", "RF64"}:
            raise ValueError("Expected a WAV file")
        if audio.channels != 1:
            raise ValueError("Only mono WAV is supported; select/mix channels explicitly")
        samples = audio.read(dtype="float64")
        rate = audio.samplerate
    return Stimulus(
        samples,
        rate,
        {
            "kind": "wav",
            "name": path.name,
            "file_sha256": hashlib.sha256(contents).hexdigest(),
        },
    )


def from_generator(generator: Callable, *, sample_rate_hz: int, **parameters) -> Stimulus:
    """Call generator(sample_rate_hz=..., **parameters) exactly once."""
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
