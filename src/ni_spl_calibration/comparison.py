"""Measure at a new microphone position with an immutable calibrated AO signal."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import numpy as np

from .analysis import band_power, validate_capture
from .calibration import (
    CalibrationResult,
    _capture,
    _check_cancelled,
    apply_calibration,
    load_result,
    measurement_bounds,
    timestamp,
    validate_reference,
)
from .errors import MeasurementCancelled
from .hardware import output_guard
from .meter import LiveMeter
from .records import INPUT_FILES, Replay, file_sha256, load_replay, seal_run
from .stimulus import save_stimulus
from .storage import read_json, write_json
from .workflow import make_hardware, require_new_output


@dataclass(frozen=True)
class ComparisonSource:
    name: str
    group: str
    replay: Replay
    result: CalibrationResult
    result_sha256: str
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ComparisonRecord:
    directory: Path | None
    sources: list[ComparisonSource]
    rows: list[dict] = field(default_factory=list)


def load_comparison_source(directory, *, name=None, group=""):
    replay = load_replay(directory)
    result = load_result(replay.directory / "result.json")  # A failure is never a baseline.
    name = name or replay.stimulus.provenance.get("name", replay.directory.name)
    return ComparisonSource(
        name, group, replay, result, file_sha256(replay.directory / "result.json")
    )


def validate_sources(sources):
    if (
        not sources
        or len({source.id for source in sources}) != len(sources)
        or any(not re.fullmatch(r"[0-9a-f]{32}", source.id) for source in sources)
    ):
        raise ValueError("Select completed calibrations to compare")
    first = sources[0].replay
    for source in sources:
        replay = source.replay
        if (
            replay.setup.reference_fingerprint != first.setup.reference_fingerprint
            or replay.backend != first.backend
        ):
            raise ValueError("Comparison sources must use the same input chain and DAQ settings")
        validate_reference(replay.setup, replay.reference)
        measurement_bounds(replay.stimulus, replay.setup)
        if source.result.reference != asdict(replay.reference) or source.result.setup != json.loads(
            json.dumps(asdict(replay.setup))
        ):
            raise ValueError("Calibration inputs do not match the baseline")
        apply_calibration(replay.stimulus, source.result)


def _save_baseline(source, directory):
    """Make a portable normal calibration bundle from the already verified inputs."""
    directory.mkdir(parents=True)
    replay = source.replay
    save_stimulus(replay.stimulus, directory / "stimulus.npz")
    write_json(directory / "setup.json", replay.setup)
    write_json(directory / "reference.json", replay.reference)
    write_json(directory / "result.json", source.result)
    write_json(
        directory / "run.json",
        {
            "schema_version": 1,
            "operation": "calibration",
            "created_at": source.result.calibrated_at,
            "target_spl_db": source.result.target_spl_db,
            "backend": replay.backend,
            "stimulus_sha256": replay.stimulus.sha256,
            "stimulus": source.result.stimulus,
            "files": {name: file_sha256(directory / name) for name in INPUT_FILES},
            "parent_run": {
                "directory": str(replay.directory),
                "record_sha256": source.result_sha256,
            },
        },
    )
    seal_run(directory, "result.json")


def _measure(source, hardware, *, cancelled, progress, save_capture, output):
    replay = source.replay
    setup, reference = replay.setup, replay.reference
    settings, fs = setup.measurement, setup.daq.sample_rate_hz
    _, _, delay = measurement_bounds(replay.stimulus, setup)
    signal = apply_calibration(replay.stimulus, source.result)
    count = len(signal)
    voltages = np.concatenate((signal, np.zeros(delay + 1)))
    validate_reference(setup, reference, hardware)
    metrics = {
        "measured_spl_db": None,
        "delta_db": None,
        "signal_to_noise_db": None,
        "background_rms_v": None,
        "background_corrected_rms_v": None,
        "input_peak_v": None,
    }
    meter = LiveMeter(setup, reference, progress)
    background = samples = None
    try:
        with output_guard(hardware):
            _check_cancelled(cancelled)
            progress({"kind": "comparison_phase", "phase": "background", "duration_s": count / fs})
            background = np.asarray(_capture(hardware.acquire, count, cancelled, meter.push))
            meter.flush()
            validate_capture(background, count, setup.daq, settings.input_clip_fraction)
            background_power = band_power(background, fs, settings)
            metrics["background_rms_v"] = math.sqrt(background_power)
            _check_cancelled(cancelled)
            progress(
                {"kind": "comparison_phase", "phase": "playback", "duration_s": len(voltages) / fs}
            )
            samples = np.asarray(
                _capture(hardware.play_and_record, voltages, cancelled, meter.push)
            )
            meter.flush()
            validate_capture(samples, len(voltages), setup.daq, settings.input_clip_fraction)
            _check_cancelled(cancelled)
            metrics["input_peak_v"] = float(np.max(np.abs(samples)))
            power = band_power(samples[delay : count + delay], fs, settings)
            signal_power = max(0.0, power - background_power)
            snr = (
                10 * math.log10(signal_power / background_power)
                if signal_power > 0 and background_power > 0
                else None
            )
            metrics.update(
                signal_to_noise_db=snr, background_corrected_rms_v=math.sqrt(signal_power)
            )
            detectable = signal_power > 0 and (
                background_power == 0 or snr >= settings.minimum_snr_db
            )
            if detectable:
                level = 20 * math.log10(
                    math.sqrt(signal_power) / reference.sensitivity_v_per_pa / 20e-6
                )
                metrics.update(
                    measured_spl_db=level, delta_db=level - source.result.measured_spl_db
                )
                status, error = "complete", None
            else:
                status, error = "insufficient_snr", "Insufficient SNR at the fixed output level"
    except (Exception, KeyboardInterrupt) as error:
        error.comparison_metrics = metrics
        raise
    finally:
        if save_capture:
            for name, values in (("background", background), ("input", samples)):
                if values is not None:
                    path = output / f"{source.id}-{name}.npz"
                    with path.open("xb") as handle:
                        np.savez_compressed(
                            handle,
                            samples=values,
                            sample_rate_hz=fs,
                            **({"output_v": voltages} if name == "input" else {}),
                        )
    return {"status": status, "error": error, **metrics, "hardware": hardware.describe()}


def run_comparison(
    output,
    sources,
    *,
    position="",
    save_capture=False,
    cancel_requested=None,
    on_progress=None,
    hardware_factory=make_hardware,
):
    """Replay each fixed signal once. There is deliberately no gain search or retry."""
    output = Path(output)
    require_new_output(output)
    validate_sources(sources)
    _check_cancelled(cancel_requested)
    progress = on_progress or (lambda item: None)
    output.mkdir(parents=True)
    files, entries = [], []
    for source in sources:
        _check_cancelled(cancel_requested)
        directory = output / "calibrations" / source.id
        _save_baseline(source, directory)
        files.extend(p.relative_to(output).as_posix() for p in directory.iterdir())
        entries.append(
            {
                "id": source.id,
                "name": source.name,
                "group": source.group,
                "calibration_directory": str(source.replay.directory),
                "calibration_sha256": source.result_sha256,
            }
        )
    write_json(
        output / "comparison.json",
        {
            "schema_version": 1,
            "operation": "fixed_gain_comparison",
            "created_at": timestamp(),
            "position": position,
            "backend": sources[0].replay.backend,
            "save_capture": save_capture,
            "sources": entries,
            "files": {name: file_sha256(output / name) for name in files},
        },
    )
    # Seal inputs before touching hardware, even if the process is later interrupted.
    write_json(
        output / "input-checksums.json",
        {"comparison.json": file_sha256(output / "comparison.json")},
    )
    rows = []
    state = "complete"
    for index, source in enumerate(sources, 1):
        if cancel_requested and cancel_requested():
            state = "cancelled"
            break
        row = {
            "schema_version": 1,
            "source_id": source.id,
            "name": source.name,
            "group": source.group,
            "position": position,
            "measured_at": timestamp(),
            "calibration_sha256": source.result_sha256,
            "baseline_spl_db": source.result.measured_spl_db,
            "target_spl_db": source.result.target_spl_db,
            "scale_v": source.result.scale_v,
            "output_peak_v": source.result.output_peak_v,
            "status": "running",
            "measured_spl_db": None,
            "delta_db": None,
        }
        progress(
            {
                "kind": "comparison_source",
                "source_id": source.id,
                "index": index,
                "total": len(sources),
            }
        )
        try:
            _check_cancelled(cancel_requested)
            hardware = hardware_factory(
                source.replay.setup, simulate=source.replay.backend == "simulation"
            )
            row.update(
                _measure(
                    source,
                    hardware,
                    cancelled=cancel_requested,
                    progress=progress,
                    save_capture=save_capture,
                    output=output,
                )
            )
        except (Exception, KeyboardInterrupt) as error:
            status = "cancelled" if isinstance(error, MeasurementCancelled) else "failed"
            if str(error).startswith("AI clipping guard reached"):
                status = "clipped"
            row.update(getattr(error, "comparison_metrics", {}))
            row.update(
                status=status,
                measured_spl_db=None,
                delta_db=None,
                error=str(error) or "Interrupted",
                notes=getattr(error, "__notes__", []),
            )
            state = "failed" if status == "clipped" else status
        row["measured_at"] = timestamp()
        write_json(output / "measurements" / f"{source.id}.json", row)
        rows.append(row)
        progress({"kind": "comparison_result", "row": row})
        if state != "complete":
            break
    summary = {
        "schema_version": 1,
        "status": state,
        "finished_at": timestamp(),
        "position": position,
        "requested_count": len(sources),
        "rows": rows,
    }
    write_json(output / "summary.json", summary)
    columns = [
        "position",
        "name",
        "group",
        "baseline_spl_db",
        "measured_spl_db",
        "delta_db",
        "signal_to_noise_db",
        "status",
        "scale_v",
        "output_peak_v",
        "measured_at",
        "error",
    ]
    with (output / "results.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    paths = [p for p in output.rglob("*") if p.is_file()]
    write_json(
        output / "checksums.json",
        {
            "schema_version": 1,
            "files": {p.relative_to(output).as_posix(): file_sha256(p) for p in paths},
        },
    )
    return {"directory": output, **summary}


def _manifest_paths(files):
    # Version-1 Windows writers used backslashes. Verify the original manifest
    # hash first, then normalize only names that will be checked against allowlists.
    normalized = {}
    for name, digest in files.items():
        canonical = name.replace("\\", "/")
        if canonical in normalized:
            raise ValueError("Duplicate comparison manifest path")
        normalized[canonical] = digest
    return normalized


def load_comparison(directory):
    """Load portable baselines and any completed measurements without using DAQ."""
    directory = Path(directory).resolve()
    payload = read_json(directory / "comparison.json")
    seal = read_json(directory / "input-checksums.json")
    if payload.get("schema_version") != 1 or payload.get("operation") != "fixed_gain_comparison":
        raise ValueError("Unsupported comparison record")
    if seal.get("comparison.json") != file_sha256(directory / "comparison.json"):
        raise ValueError("Comparison record integrity check failed")
    entries = payload["sources"]
    ids = [entry["id"] for entry in entries]
    if (
        not ids
        or len(set(ids)) != len(ids)
        or any(not re.fullmatch(r"[0-9a-f]{32}", value) for value in ids)
    ):
        raise ValueError("Invalid comparison source IDs")
    allowed_inputs = {
        f"calibrations/{value}/{name}"
        for value in ids
        for name in (*INPUT_FILES, "result.json", "run.json", "checksums.json")
    }
    input_files = _manifest_paths(payload["files"])
    if input_files.keys() != allowed_inputs:
        raise ValueError("Invalid comparison file manifest")
    for name, digest in input_files.items():
        if file_sha256(directory / name) != digest:
            raise ValueError("Comparison record integrity check failed")
    if (directory / "checksums.json").is_file():
        hashes = read_json(directory / "checksums.json")
        for name, digest in _manifest_paths(hashes["files"]).items():
            # A record cannot make the loader open arbitrary paths.
            if name not in allowed_inputs | {
                "comparison.json",
                "input-checksums.json",
                "summary.json",
                "results.csv",
            } and not (
                re.fullmatch(r"measurements/[0-9a-f]{32}\.json", name)
                or re.fullmatch(r"[0-9a-f]{32}-(background|input)\.npz", name)
            ):
                raise ValueError("Invalid comparison result path")
            if file_sha256(directory / name) != digest:
                raise ValueError("Comparison record integrity check failed")
    sources = [
        replace(
            load_comparison_source(
                directory / "calibrations" / entry["id"], name=entry["name"], group=entry["group"]
            ),
            id=entry["id"],
        )
        for entry in entries
    ]
    validate_sources(sources)
    rows = [
        read_json(directory / "measurements" / f"{source.id}.json")
        for source in sources
        if (directory / "measurements" / f"{source.id}.json").is_file()
    ]
    by_id = {source.id: source for source in sources}
    for row in rows:
        source = by_id.get(row.get("source_id"))
        if source is None or row["status"] not in {
            "complete",
            "insufficient_snr",
            "failed",
            "clipped",
            "cancelled",
        }:
            raise ValueError("Invalid comparison measurement")
        if (
            row["baseline_spl_db"] != source.result.measured_spl_db
            or row["scale_v"] != source.result.scale_v
        ):
            raise ValueError("Comparison baseline does not match the calibration")
        if row["status"] == "complete":
            if not all(
                isinstance(row[key], (int, float)) and math.isfinite(row[key])
                for key in ("measured_spl_db", "delta_db")
            ) or not math.isclose(
                row["delta_db"], row["measured_spl_db"] - row["baseline_spl_db"], abs_tol=1e-10
            ):
                raise ValueError("Invalid comparison difference")
        elif row["measured_spl_db"] is not None or row["delta_db"] is not None:
            raise ValueError("Invalid measurement cannot contain a comparison SPL")
    return ComparisonRecord(directory, sources, rows)
