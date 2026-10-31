"""Volume-controlled playback and lossless duplex recordings for hardware checks."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from threading import Lock

import numpy as np

from .calibration import timestamp, validate_reference
from .hardware import check_stop
from .meter import input_metrics
from .records import file_sha256
from .stimulus import save_stimulus
from .storage import write_json
from .workflow import make_hardware, require_new_output


class VolumeControl:
    def __init__(self, fraction=0.1):
        self._lock = Lock()
        self.set(fraction)

    def set(self, fraction):
        value = float(fraction)
        if not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Volume must be between 0 and 1")
        with self._lock:
            self._value = value

    def get(self):
        with self._lock:
            return self._value


def make_test_tone(*, sample_rate_hz):
    """A one-second 1 kHz mono tone for A/B checks against WAV playback."""
    if sample_rate_hz <= 2000:
        raise ValueError("The 1 kHz test tone requires a sample rate above 2000 Hz")
    samples = 0.8 * np.sin(2 * np.pi * 1000 * np.arange(sample_rate_hz) / sample_rate_hz)
    ramp = np.sin(np.linspace(0, np.pi / 2, max(1, round(sample_rate_hz * 0.005)))) ** 2
    samples[: len(ramp)] *= ramp
    samples[-len(ramp) :] *= ramp[::-1]
    return samples


def voltage_blocks(stimulus, *, max_peak_v, block_size, volume, loop):
    """Keep source samples fixed; ramp changes in V/digital unit over <= 10 ms."""
    if block_size < 2 or not np.isfinite(max_peak_v) or max_peak_v <= 0:
        raise ValueError("Invalid playback block size or peak limit")
    samples = stimulus.samples
    maximum_scale = max_peak_v / np.max(np.abs(samples))
    cursor = 0
    previous = 0.0
    ramp_count = min(block_size, max(1, round(stimulus.sample_rate_hz * 0.01)))
    while loop or cursor < len(samples) + 1:
        fraction = volume.get()
        requested = fraction * maximum_scale
        scales = np.full(block_size, requested)
        scales[:ramp_count] = np.linspace(previous, requested, ramp_count + 1)[1:]
        previous = requested
        if loop:
            source = samples[(np.arange(block_size) + cursor) % len(samples)]
        else:
            source = np.zeros(block_size)
            valid = min(block_size, max(0, len(samples) - cursor))
            source[:valid] = samples[cursor : cursor + valid]
        output = source * scales
        yield {
            "output": output,
            "source_first_sample": cursor,
            "volume_fraction": fraction,
            "scale_start_v": float(scales[0]),
            "scale_end_v": requested,
            "output_peak_v": float(np.max(np.abs(output))),
        }
        cursor += block_size


def run_test_playback(
    stimulus,
    *,
    setup,
    output,
    simulate,
    volume,
    loop=True,
    reference=None,
    save_capture=True,
    cancel_requested,
    on_frame,
):
    """Uncalibrated voltage-controlled playback; never labels it a calibrated stimulus."""
    output = Path(output)
    require_new_output(output)
    check_stop(cancel_requested)
    if stimulus.sample_rate_hz != setup.daq.sample_rate_hz:
        raise ValueError("Stimulus and DAQ sample rates differ")
    block_size = max(16, round(setup.daq.sample_rate_hz * 0.05))
    total_samples = (
        None if loop else math.ceil((len(stimulus.samples) + 1) / block_size) * block_size
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "setup.json", asdict(setup))
    save_stimulus(stimulus, output / "stimulus.npz")
    if reference is not None:
        write_json(output / "reference.json", reference)
    stream = None
    captured = 0
    hardware = None
    try:
        hardware = make_hardware(setup, simulate=simulate)
        if reference is not None:
            validate_reference(setup, reference, hardware)
        write_json(
            output / "playback.json",
            {
                "schema_version": 1,
                "started_at": timestamp(),
                "operation": "test_playback",
                "hardware": hardware.describe(),
                "stimulus": stimulus.describe(),
                "loop": loop,
                "initial_volume_fraction": volume.get(),
                "block_size": block_size,
                "save_capture": save_capture,
                "gain_definition": "volume * max_peak_v / source_digital_peak; 10 ms ramp",
                "files": {
                    name: file_sha256(output / name)
                    for name in ("setup.json", "stimulus.npz")
                    + (("reference.json",) if reference is not None else ())
                },
            },
        )
        on_frame({"kind": "hardware", "description": hardware.describe()})
        blocks = voltage_blocks(
            stimulus,
            max_peak_v=setup.daq.max_peak_v,
            block_size=block_size,
            volume=volume,
            loop=loop,
        )
        stream = hardware.duplex(
            blocks, block_size, cancel_requested=cancel_requested, total_samples=total_samples
        )
        with (output / "levels.jsonl").open("x", encoding="utf-8") as log:
            try:
                for block in stream:
                    values = np.asarray(block["samples"], dtype=float)
                    if values.shape != (block_size,):
                        raise RuntimeError("Incomplete playback microphone capture")
                    metrics = input_metrics(values, setup, reference)
                    item = {
                        key: value
                        for key, value in block.items()
                        if key not in {"output", "samples"}
                    }
                    item.update(
                        at=timestamp(), first_sample=captured, sample_count=block_size, **metrics
                    )
                    if save_capture:
                        path = output / f"capture-{captured // block_size + 1:06d}.npz"
                        with path.open("xb") as handle:
                            np.savez_compressed(
                                handle,
                                samples=values,
                                output_v=block["output"],
                                sample_rate_hz=setup.daq.sample_rate_hz,
                                first_sample=captured,
                            )
                        item.update(capture_file=path.name, capture_sha256=file_sha256(path))
                    captured += block_size
                    log.write(json.dumps(item, allow_nan=False) + "\n")
                    log.flush()
                    on_frame(
                        {
                            "kind": "test_playback",
                            "samples": values.copy(),
                            "sample_rate_hz": setup.daq.sample_rate_hz,
                            "duration_s": captured / setup.daq.sample_rate_hz,
                            "hardware": hardware.describe(),
                            **item,
                        }
                    )
            finally:
                stream.close()
                stream = None
    except (Exception, KeyboardInterrupt) as error:
        write_json(
            output / "failure.json",
            {
                "schema_version": 1,
                "status": "failed",
                "at": timestamp(),
                "error": str(error),
                "notes": getattr(error, "__notes__", []),
                "captured_samples": captured,
                "hardware": hardware.describe() if hardware is not None else None,
            },
        )
        raise
    finally:
        if stream is not None:
            stream.close()
    summary = {
        "schema_version": 1,
        "status": "stopped" if cancel_requested() else "complete",
        "finished_at": timestamp(),
        "captured_samples": captured,
        "duration_s": captured / setup.daq.sample_rate_hz,
        "hardware": hardware.describe(),
        "levels_sha256": file_sha256(output / "levels.jsonl"),
    }
    write_json(output / "summary.json", summary)
    return summary
