"""Continuous microphone monitoring with optional lossless capture recording."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from .analysis import band_power
from .calibration import MeasurementCancelled, timestamp, validate_reference
from .meter import input_metrics
from .records import file_sha256
from .storage import write_json
from .workflow import make_hardware, require_new_output


def monitor_input(
    setup,
    *,
    output,
    simulate,
    reference=None,
    save_capture=False,
    cancel_requested,
    on_frame,
    block_duration_s=0.1,
):
    output = Path(output)
    require_new_output(output)
    if cancel_requested():
        raise MeasurementCancelled("Measurement cancelled")
    block_size = max(16, round(setup.daq.sample_rate_hz * block_duration_s))
    settings = replace(setup.measurement, start_s=0, stop_s=None, acoustic_delay_s=0)
    # Check band/correction coverage without accessing hardware.
    band_power(np.zeros(block_size), setup.daq.sample_rate_hz, settings)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "setup.json", asdict(setup))
    if reference is not None:
        write_json(output / "reference.json", reference)
    count = 0
    frames = None
    try:
        hardware = make_hardware(setup, simulate=simulate)
        if reference is not None:
            validate_reference(setup, reference, hardware)
        write_json(
            output / "monitor.json",
            {
                "schema_version": 1,
                "started_at": timestamp(),
                "sample_rate_hz": setup.daq.sample_rate_hz,
                "block_size": block_size,
                "save_capture": save_capture,
                "hardware": hardware.describe(),
                "setup_sha256": file_sha256(output / "setup.json"),
                "reference_sha256": file_sha256(output / "reference.json") if reference else None,
                "level_definition": "Total band-limited input pressure; no background subtraction",
            },
        )
        frames = hardware.monitor(block_size, cancel_requested=cancel_requested)
        with (output / "levels.jsonl").open("x", encoding="utf-8") as log:
            try:
                for values in frames:
                    values = np.asarray(values, dtype=float)
                    if values.shape != (block_size,) or not np.all(np.isfinite(values)):
                        raise ValueError("Invalid microphone monitor capture")
                    item = {
                        "at": timestamp(),
                        "first_sample": count * block_size,
                        "sample_count": block_size,
                        **input_metrics(values, setup, reference),
                    }
                    count += 1
                    if save_capture:
                        path = output / f"capture-{count:06d}.npz"
                        with path.open("xb") as handle:
                            np.savez_compressed(
                                handle,
                                samples=values,
                                sample_rate_hz=setup.daq.sample_rate_hz,
                                first_sample=item["first_sample"],
                            )
                        item.update(capture_file=path.name, capture_sha256=file_sha256(path))
                    log.write(json.dumps(item, allow_nan=False) + "\n")
                    log.flush()
                    on_frame(
                        {
                            "kind": "monitor",
                            "samples": values.copy(),
                            "sample_rate_hz": setup.daq.sample_rate_hz,
                            "duration_s": count * block_size / setup.daq.sample_rate_hz,
                            **item,
                        }
                    )
                    if cancel_requested():
                        break
            finally:
                frames.close()
                frames = None
    except (Exception, KeyboardInterrupt) as error:
        write_json(
            output / "failure.json",
            {
                "schema_version": 1,
                "status": "failed",
                "at": timestamp(),
                "error": str(error),
                "notes": getattr(error, "__notes__", []),
                "captured_samples": count * block_size,
            },
        )
        raise
    finally:
        if frames is not None:
            frames.close()
    summary = {
        "schema_version": 1,
        "status": "stopped",
        "stopped_at": timestamp(),
        "captured_samples": count * block_size,
        "duration_s": count * block_size / setup.daq.sample_rate_hz,
        "levels_sha256": file_sha256(output / "levels.jsonl"),
    }
    write_json(output / "summary.json", summary)
    return summary
