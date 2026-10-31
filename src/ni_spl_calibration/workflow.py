"""Shared source preparation and immutable records for the desktop GUI and CLI."""

from __future__ import annotations

import importlib
import importlib.util
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .calibration import (
    MeasurementCancelled,
    calibrate,
    measure_reference,
    measurement_bounds,
    validate_reference,
)
from .config import DEFAULT_SAMPLE_RATE_HZ, Setup
from .hardware import NIDaqHardware, SimulatedHardware
from .records import seal_run, write_run_manifest
from .stimulus import from_generator, load_stimulus, load_wav, save_stimulus
from .storage import write_json


@dataclass(frozen=True)
class StimulusSource:
    kind: str
    location: str
    parameters: Path | None = None

    def prepare(self, sample_rate_hz: int | None = None):
        if self.parameters and self.kind != "generator":
            raise ValueError("--parameters requires --generator")
        if not self.location.strip():
            raise ValueError("Select a waveform or generator first")
        if self.kind == "wav":
            return load_wav(self.location, sample_rate_hz=sample_rate_hz)
        if self.kind == "npz":
            stimulus = load_stimulus(self.location)
            if sample_rate_hz is not None and stimulus.sample_rate_hz != sample_rate_hz:
                raise ValueError(
                    "Saved stimulus and DAQ/target sample rates differ; NPZ is never resampled. "
                    "Use the original WAV/generator to recalibrate at the new setup rate."
                )
            return stimulus
        if self.kind != "generator":
            raise ValueError(f"Unsupported source kind: {self.kind}")
        parameters = {}
        if self.parameters:
            parameters = yaml.safe_load(self.parameters.read_text(encoding="utf-8"))
            if not isinstance(parameters, dict):
                raise ValueError("Generator parameters must be a YAML mapping")
            if "sample_rate_hz" in parameters:
                raise ValueError(
                    "Set sample_rate_hz in setup.daq, not in generator parameters "
                    "(standalone inspect also accepts --sample-rate-hz)"
                )
        module_name, separator, function_name = self.location.rpartition(":")
        if not separator or not module_name or not function_name:
            raise ValueError("Use --generator module:function or file.py:function")
        if module_name.endswith(".py"):
            path = Path(module_name)
            spec = importlib.util.spec_from_file_location("_ni_spl_user_generator", path)
            if spec is None or spec.loader is None:
                raise ValueError("Cannot import generator file")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        else:
            module = importlib.import_module(module_name)
        function = getattr(module, function_name)
        if not callable(function):
            raise ValueError("Generator must be callable")
        return from_generator(
            function,
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ if sample_rate_hz is None else sample_rate_hz,
            **parameters,
        )


def make_hardware(setup: Setup, *, simulate: bool, reference_mode: bool = False):
    if simulate:
        return SimulatedHardware(
            setup.daq,
            reference_mode=reference_mode,
            reference_frequency_hz=setup.reference_frequency_hz,
            reference_level_db=setup.reference_level_db,
        )
    return NIDaqHardware(setup.daq)


def require_new_output(path: Path):
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Output already exists: {path}")


def measure_reference_to_file(setup, output, *, simulate, cancel_requested=None):
    output = Path(output)
    require_new_output(output)
    if cancel_requested is not None and cancel_requested():
        raise MeasurementCancelled("Measurement cancelled")
    reference = measure_reference(
        setup,
        hardware=make_hardware(setup, simulate=simulate, reference_mode=True),
        cancel_requested=cancel_requested,
    )
    write_json(output, reference)
    return reference


def calibrate_to_directory(
    stimulus,
    *,
    setup,
    reference,
    target_spl_db,
    output,
    hardware,
    on_iteration=None,
    cancel_requested=None,
    parent_run=None,
):
    output = Path(output)
    require_new_output(output)
    if not math.isfinite(target_spl_db) or not -100 <= target_spl_db <= 180:
        raise ValueError("target_spl_db must be finite and between -100 and 180")
    measurement_bounds(stimulus, setup)
    validate_reference(setup, reference, hardware)
    if cancel_requested is not None and cancel_requested():
        raise MeasurementCancelled("Measurement cancelled")
    output.mkdir(parents=True, exist_ok=False)
    save_stimulus(stimulus, output / "stimulus.npz")
    write_json(output / "setup.json", asdict(setup))
    write_json(output / "reference.json", reference)
    write_run_manifest(
        output,
        target_spl_db=target_spl_db,
        stimulus=stimulus,
        hardware=hardware,
        parent_run=parent_run,
    )
    try:
        result = calibrate(
            stimulus,
            setup=setup,
            reference=reference,
            target_spl_db=target_spl_db,
            hardware=hardware,
            on_iteration=on_iteration,
            cancel_requested=cancel_requested,
        )
    except (Exception, KeyboardInterrupt) as error:
        write_json(
            output / "failure.json",
            {
                "schema_version": 1,
                "status": "cancelled" if isinstance(error, MeasurementCancelled) else "failed",
                "error": str(error) or "Interrupted",
                "notes": getattr(error, "__notes__", []),
                "iterations": getattr(error, "iterations", []),
                "stimulus": stimulus.describe(),
                "hardware": hardware.describe(),
            },
        )
        seal_run(output, "failure.json")
        raise
    write_json(output / "result.json", result)
    seal_run(output, "result.json")
    return result
