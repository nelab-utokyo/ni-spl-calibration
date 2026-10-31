from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

DEFAULT_SAMPLE_RATE_HZ = 200_000


@dataclass(frozen=True)
class DaqSettings:
    input_channel: str = "Dev1/ai0"
    output_channel: str = "Dev1/ao0"
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    terminal_configuration: str = "differential"
    input_min_v: float = -10.0
    input_max_v: float = 10.0
    max_peak_v: float = 0.5

    def __post_init__(self):
        for channel, kind in [(self.input_channel, "ai"), (self.output_channel, "ao")]:
            parts = channel.split("/")
            if (
                len(parts) != 2
                or not parts[0]
                or not parts[1].startswith(kind)
                or not parts[1][2:].isdigit()
            ):
                raise ValueError(f"Invalid {kind} channel: {channel}")
        if self.input_channel.split("/")[0] != self.output_channel.split("/")[0]:
            raise ValueError("AI and AO must be on the same DAQ device")
        if self.terminal_configuration not in {
            "differential",
            "rse",
            "nrse",
            "pseudo_differential",
        }:
            raise ValueError("Unsupported terminal_configuration")
        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or self.sample_rate_hz < 2
        ):
            raise ValueError("sample_rate_hz must be an integer >= 2")
        if not all(math.isfinite(v) for v in (self.input_min_v, self.input_max_v, self.max_peak_v)):
            raise ValueError("DAQ voltage limits must be finite")
        if not self.input_min_v < 0 < self.input_max_v or not 0 < self.max_peak_v <= 10:
            raise ValueError("Invalid DAQ voltage limits")


@dataclass(frozen=True)
class MeasurementSettings:
    band_hz: tuple[float, float] | None = None
    start_s: float = 0.0
    stop_s: float | None = None
    acoustic_delay_s: float = 0.0
    # Additive microphone corrections relative to the acoustic-reference frequency.
    frequency_correction_db: tuple[tuple[float, float], ...] = ()
    tolerance_db: float = 0.5
    minimum_snr_db: float = 10.0
    max_upward_step_db: float = 10.0
    max_iterations: int = 20
    confirmations: int = 2
    start_scale_v: float = 0.001
    input_clip_fraction: float = 0.98

    def __post_init__(self):
        for name in (
            "start_s",
            "acoustic_delay_s",
            "tolerance_db",
            "minimum_snr_db",
            "max_upward_step_db",
            "start_scale_v",
            "input_clip_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.stop_s is not None and (
            not math.isfinite(self.stop_s) or self.stop_s <= self.start_s
        ):
            raise ValueError("stop_s must be greater than start_s")
        if self.band_hz is not None:
            if (
                len(self.band_hz) != 2
                or not all(math.isfinite(x) for x in self.band_hz)
                or not 0 <= self.band_hz[0] < self.band_hz[1]
            ):
                raise ValueError("band_hz must contain increasing nonnegative frequencies")
            object.__setattr__(self, "band_hz", tuple(self.band_hz))
        points = tuple(tuple(point) for point in self.frequency_correction_db)
        if points and (
            len(points) < 2
            or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points)
        ):
            raise ValueError("frequency_correction_db needs at least two finite [Hz, dB] points")
        if points and (points[0][0] < 0 or any(a[0] >= b[0] for a, b in zip(points, points[1:]))):
            raise ValueError("Correction frequencies must be nonnegative and strictly increasing")
        object.__setattr__(self, "frequency_correction_db", points)
        for name in ("max_iterations", "confirmations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.confirmations < 2 or self.max_iterations < self.confirmations:
            raise ValueError("Require at least two confirmations within max_iterations")
        if not 0 < self.input_clip_fraction < 1 or not 0 < self.max_upward_step_db <= 10:
            raise ValueError("Require 0 < input_clip_fraction < 1 and 0 < max_upward_step_db <= 10")
        if self.tolerance_db <= 0 or self.start_scale_v <= 0:
            raise ValueError("tolerance_db and start_scale_v must be positive")


@dataclass(frozen=True)
class Setup:
    setup_id: str
    chain_notes: str
    daq: DaqSettings = field(default_factory=DaqSettings)
    measurement: MeasurementSettings = field(default_factory=MeasurementSettings)
    reference_level_db: float = 94.0
    reference_frequency_hz: float = 1000.0
    reference_duration_s: float = 2.0
    reference_max_span_db: float = 0.2

    def __post_init__(self):
        if not self.setup_id.strip() or not self.chain_notes.strip():
            raise ValueError("Record setup_id and chain_notes before calibration")
        if not all(
            math.isfinite(v)
            for v in (
                self.reference_level_db,
                self.reference_frequency_hz,
                self.reference_duration_s,
                self.reference_max_span_db,
            )
        ):
            raise ValueError("Reference settings must be finite")
        if not 0 < self.reference_frequency_hz < self.daq.sample_rate_hz / 2:
            raise ValueError("Reference frequency must be below Nyquist")
        if not -100 <= self.reference_level_db <= 180:
            raise ValueError("Reference level must be between -100 and 180 dB SPL")
        if self.reference_duration_s < 0.5 or self.reference_max_span_db <= 0:
            raise ValueError("Reference needs >= 0.5 s and a positive stability limit")
        if self.measurement.band_hz and self.measurement.band_hz[1] > self.daq.sample_rate_hz / 2:
            raise ValueError("Analysis band exceeds Nyquist")

    @property
    def reference_fingerprint(self) -> str:
        payload = asdict(self)
        del payload["measurement"]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()


def load_setup(path: str | Path) -> Setup:
    with Path(path).open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Setup must be a YAML mapping")
    payload = dict(payload)
    return Setup(
        **{
            **payload,
            "daq": DaqSettings(**payload.get("daq", {})),
            "measurement": MeasurementSettings(**payload.get("measurement", {})),
        }
    )
