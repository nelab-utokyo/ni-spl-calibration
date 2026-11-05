"""Frozen calibration plans and a serial, cooperatively cancellable job runner.

Every save/execution creates a new bundle. Events are append-only; an interrupted
process leaves enough information to resume without changing an earlier result.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from .calibration import (
    MeasurementCancelled,
    Reference,
    measurement_bounds,
    timestamp,
    validate_reference,
)
from .config import MeasurementSettings, Setup
from .meter import LiveMeter
from .records import INPUT_FILES, file_sha256, load_replay, setup_from_record
from .stimulus import Stimulus, load_stimulus, save_stimulus
from .storage import read_json, write_json
from .workflow import calibrate_to_directory, make_hardware, require_new_output

STATES = {"pending", "running", "complete", "failed", "cancelled", "skipped"}


def starting_context(setup, simulate):
    """Conditions under which a starting level was checked, excluding target SPL."""
    return {
        "reference_fingerprint": setup.reference_fingerprint,
        "backend": "simulation" if simulate else "ni-daqmx",
        "band_hz": list(setup.measurement.band_hz) if setup.measurement.band_hz else None,
        "frequency_correction_db": [list(p) for p in setup.measurement.frequency_correction_db],
    }


@dataclass
class CalibrationJob:
    name: str
    stimulus: Stimulus
    measurement: MeasurementSettings
    target_spl_db: float
    source: dict = field(default_factory=dict)
    group: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    start_context: dict | None = None
    start_evidence: dict | None = None
    parent_run: dict | None = None
    status: str = "pending"
    result: dict | None = None
    output: Path | None = None
    error: str | None = None
    iterations: list = field(default_factory=list)
    _bounds_cache: tuple | None = field(default=None, init=False, repr=False)

    def ready(self, setup, simulate):
        try:
            current = replace(setup, measurement=self.measurement)
            if self.start_context != starting_context(current, simulate):
                return False
            key = (id(self.stimulus), current.daq.sample_rate_hz, self.measurement)
            if self._bounds_cache is None or self._bounds_cache[0] != key:
                measurement_bounds(self.stimulus, current)
                self._bounds_cache = (key, True)
            return True
        except (ValueError, TypeError):
            return False


@dataclass
class CalibrationPlan:
    directory: Path
    setup: Setup
    reference: Reference | None
    simulate: bool
    jobs: list[CalibrationJob]


def _check_cancelled(cancel_requested):
    if cancel_requested is not None and cancel_requested():
        raise MeasurementCancelled("Measurement cancelled")


def save_plan(directory, jobs, *, setup, reference, simulate, parent=None, cancel_requested=None):
    """Save exact prepared samples, never just paths or executable generators."""
    directory = Path(directory)
    require_new_output(directory)
    if not jobs:
        raise ValueError("Add at least one stimulus")
    ids = [job.id for job in jobs]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[0-9a-f]{32}", i) for i in ids):
        raise ValueError("Invalid or duplicate job ID")
    for job in jobs:
        if not math.isfinite(job.target_spl_db) or not -100 <= job.target_spl_db <= 180:
            raise ValueError("Invalid target SPL")
        if job.status not in STATES:
            raise ValueError("Invalid job status")
    _check_cancelled(cancel_requested)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "setup.json", setup)
    files = ["setup.json"]
    if reference is not None:
        write_json(directory / "reference.json", reference)
        files.append("reference.json")
    entries = []
    for job in jobs:
        _check_cancelled(cancel_requested)
        digest = job.stimulus.sha256
        waveform = f"waveforms/{digest}.npz"
        if waveform not in files:
            (directory / "waveforms").mkdir(exist_ok=True)
            save_stimulus(job.stimulus, directory / waveform)
            files.append(waveform)
        previous_output = None
        if job.output is not None and (job.output / "run.json").is_file():
            # Carry existing outcomes with the plan so a resumed bundle remains
            # portable even when some jobs do not need to be run again.
            load_replay(job.output)
            previous_output = f"prior-runs/{job.id}"
            destination = directory / previous_output
            destination.mkdir(parents=True)
            outcome = "result.json" if (job.output / "result.json").is_file() else "failure.json"
            for name in (*INPUT_FILES, "run.json", "checksums.json", outcome):
                _check_cancelled(cancel_requested)
                if (job.output / name).is_file():
                    shutil.copyfile(job.output / name, destination / name)
        entries.append(
            {
                "id": job.id,
                "name": job.name,
                "group": job.group,
                "stimulus_sha256": digest,
                "stimulus_provenance": job.stimulus.provenance,
                "measurement": asdict(job.measurement),
                "target_spl_db": job.target_spl_db,
                "source": job.source,
                "start_context": job.start_context,
                "start_evidence": job.start_evidence,
                "parent_run": job.parent_run,
                "status": job.status,
                "result": job.result,
                "output": previous_output,
                "error": job.error,
                "iterations": job.iterations,
            }
        )
    _check_cancelled(cancel_requested)
    write_json(
        directory / "plan.json",
        {
            "schema_version": 1,
            "operation": "calibration_queue",
            "created_at": timestamp(),
            "backend": "simulation" if simulate else "ni-daqmx",
            "parent_plan": str(parent) if parent else None,
            "jobs": entries,
            "files": {name: file_sha256(directory / name) for name in files},
        },
    )
    write_json(
        directory / "plan-checksums.json",
        {
            "schema_version": 1,
            "files": {name: file_sha256(directory / name) for name in (*files, "plan.json")},
        },
    )
    return directory


def load_plan(directory):
    """Restore a plan, including the last durable state of an interrupted queue."""
    directory = Path(directory).resolve()
    payload = read_json(directory / "plan.json")
    if payload.get("schema_version") != 1 or payload.get("operation") != "calibration_queue":
        raise ValueError("Unsupported calibration plan")
    if payload.get("backend") not in {"simulation", "ni-daqmx"}:
        raise ValueError("Invalid plan backend")
    files = payload["files"]
    seal = read_json(directory / "plan-checksums.json")
    if seal.get("schema_version") != 1 or seal["files"].get("plan.json") != file_sha256(
        directory / "plan.json"
    ):
        raise ValueError("Plan integrity check failed")
    allowed = {"setup.json", "reference.json"}
    allowed.update(
        f"waveforms/{entry['stimulus_sha256']}.npz"
        for entry in payload["jobs"]
        if re.fullmatch(r"[0-9a-f]{64}", entry["stimulus_sha256"])
    )
    if not {"setup.json"} <= files.keys() or files.keys() - allowed:
        raise ValueError("Invalid plan file manifest")
    for name, digest in files.items():
        if file_sha256(directory / name) != digest:
            raise ValueError(f"保存記録の整合性チェックに失敗しました: {name}")
    setup = setup_from_record(read_json(directory / "setup.json"))
    reference = (
        Reference(**read_json(directory / "reference.json")) if "reference.json" in files else None
    )
    jobs = []
    seen = set()
    waveforms = {}
    for entry in payload["jobs"]:
        job_id = entry["id"]
        digest = entry["stimulus_sha256"]
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or job_id in seen:
            raise ValueError("Invalid or duplicate job ID")
        seen.add(job_id)
        name = f"waveforms/{digest}.npz"
        if name not in files:
            raise ValueError("Missing plan waveform")
        if digest not in waveforms:
            stimulus = load_stimulus(directory / name)
            if stimulus.sha256 != digest:
                raise ValueError("Plan waveform hash mismatch")
            waveforms[digest] = stimulus
        job = CalibrationJob(
            name=entry["name"],
            group=entry["group"],
            id=job_id,
            stimulus=waveforms[digest]
            if waveforms[digest].provenance == entry["stimulus_provenance"]
            else Stimulus(
                waveforms[digest].samples,
                waveforms[digest].sample_rate_hz,
                entry["stimulus_provenance"],
            ),
            measurement=MeasurementSettings(**entry["measurement"]),
            target_spl_db=entry["target_spl_db"],
            source=entry["source"],
            start_context=entry["start_context"],
            start_evidence=entry["start_evidence"],
            parent_run=entry["parent_run"],
            status=entry["status"],
            result=entry["result"],
            output=directory / entry["output"] if entry["output"] else None,
            error=entry["error"],
            iterations=entry["iterations"],
        )
        if (
            not math.isfinite(job.target_spl_db)
            or not -100 <= job.target_spl_db <= 180
            or job.status not in STATES
        ):
            raise ValueError("Invalid saved job")
        jobs.append(job)
        if entry["output"]:
            if entry["output"] != f"prior-runs/{job.id}":
                raise ValueError("Invalid prior result path")
            replay = load_replay(job.output)
            if replay.stimulus.sha256 != job.stimulus.sha256:
                raise ValueError("Prior result waveform does not match the plan")
            if job.status == "complete":
                result = read_json(job.output / "result.json")
                if result != job.result:
                    raise ValueError("Prior result does not match the plan")
    if not jobs:
        raise ValueError("Empty calibration plan")
    by_id = {job.id: job for job in jobs}
    for path in sorted((directory / "events").glob("*.json")):
        event = read_json(path)
        job = by_id[event["job_id"]]
        job.status = event["status"]
        if job.status not in STATES:
            raise ValueError("Invalid queue event")
        output = directory / "runs" / event["attempt"]
        if not re.fullmatch(r"[0-9a-f]{32}", event["attempt"]):
            raise ValueError("Invalid attempt ID")
        job.output = output
        job.error = event.get("error")
        job.result, job.iterations = None, []
        if job.status == "complete":
            # Verify the normal single-stimulus bundle as well as the plan.
            replay = load_replay(output)
            if (
                replay.stimulus.sha256 != job.stimulus.sha256
                or replay.stimulus.provenance != job.stimulus.provenance
                or replay.target_spl_db != job.target_spl_db
                or replay.setup != replace(setup, measurement=job.measurement)
                or reference is None
                or replay.reference != reference
            ):
                raise ValueError("Queue result does not match the plan")
            job.result = read_json(output / "result.json")
            job.iterations = job.result["iterations"]
        elif job.status in {"failed", "cancelled"} and (output / "failure.json").is_file():
            load_replay(output)
            job.iterations = read_json(output / "failure.json")["iterations"]
    for job in jobs:
        if job.status == "running":
            # A crash never counts as successful completion and never auto-restarts AO.
            job.status = "cancelled"
            job.error = "Interrupted before completion"
            job.result = None
    return CalibrationPlan(directory, setup, reference, payload["backend"] == "simulation", jobs)


def run_queue(
    directory,
    jobs,
    *,
    selected_ids,
    setup,
    reference,
    simulate,
    parent=None,
    cancel_requested=None,
    pause_after=None,
    on_progress=None,
    hardware_factory=make_hardware,
):
    """Run selected jobs in order; stop the queue on the first failure/cancellation."""
    directory = Path(directory)
    selected = [job for job in jobs if job.id in selected_ids]
    if not selected or {job.id for job in selected} != set(selected_ids):
        raise ValueError("Select jobs to run")
    for job in selected:
        if not job.ready(setup, simulate):
            raise ValueError("Check the starting level of each selected stimulus")
        current = replace(setup, measurement=job.measurement)
        # Preflight every job before creating tasks or emitting audio.
        measurement_bounds(job.stimulus, current)
        validate_reference(current, reference)
        if reference.hardware.get("backend") != ("simulation" if simulate else "ni-daqmx"):
            raise ValueError("Reference and queue modes differ")
    save_plan(
        directory,
        jobs,
        setup=setup,
        reference=reference,
        simulate=simulate,
        parent=parent,
        cancel_requested=cancel_requested,
    )
    sequence = 0

    def publish(job, status, attempt, error=None):
        nonlocal sequence
        sequence += 1
        event = {
            "job_id": job.id,
            "status": status,
            "attempt": attempt,
            "at": timestamp(),
            "error": str(error) if error else None,
        }
        write_json(directory / "events" / f"{sequence:06d}.json", event)

    emit = on_progress or (lambda item: None)
    state, error_message = "complete", None
    for index, job in enumerate(selected, 1):
        if (cancel_requested and cancel_requested()) or (pause_after and pause_after()):
            state = "held"
            break
        attempt = uuid4().hex
        output = directory / "runs" / attempt
        job.status, job.result, job.output, job.error = "running", None, output, None
        job.iterations = []
        publish(job, "running", attempt)
        emit(
            {
                "kind": "queue_job",
                "job_id": job.id,
                "index": index,
                "total": len(selected),
                "output": str(output),
            }
        )
        current = replace(setup, measurement=job.measurement)

        def progress(item):
            emit({**item, "job_id": job.id})

        try:
            _check_cancelled(cancel_requested)
            hardware = hardware_factory(current, simulate=simulate)
            result = calibrate_to_directory(
                job.stimulus,
                setup=current,
                reference=reference,
                target_spl_db=job.target_spl_db,
                output=output,
                hardware=hardware,
                on_iteration=progress,
                on_phase=progress,
                cancel_requested=cancel_requested,
                parent_run=job.parent_run,
                on_samples=LiveMeter(current, reference, progress).push,
            )
        except (Exception, KeyboardInterrupt) as error:
            job.status = "cancelled" if isinstance(error, MeasurementCancelled) else "failed"
            job.error = str(error) or "Interrupted"
            job.iterations = getattr(error, "iterations", [])
            publish(job, job.status, attempt, job.error)
            state, error_message = "held", job.error
        else:
            job.status, job.result = "complete", asdict(result)
            job.iterations = job.result["iterations"]
            publish(job, "complete", attempt)
        emit(
            {
                "kind": "queue_outcome",
                "job_id": job.id,
                "status": job.status,
                "result": job.result,
                "error": job.error,
                "output": str(output),
            }
        )
        if state == "held":
            break
    summary = {
        "schema_version": 1,
        "status": state,
        "finished_at": timestamp(),
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "group": job.group,
                "target_spl_db": job.target_spl_db,
                "status": job.status,
                "measured_spl_db": job.result["measured_spl_db"] if job.result else None,
                "scale_v": job.result["scale_v"] if job.result else None,
                "output": job.output.relative_to(directory).as_posix()
                if job.output and job.output.is_relative_to(directory)
                else f"prior-runs/{job.id}"
                if (directory / "prior-runs" / job.id).is_dir()
                else str(job.output)
                if job.output
                else None,
                "error": job.error,
            }
            for job in jobs
        ],
    }
    write_json(directory / "summary.json", summary)
    with (directory / "results.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary["jobs"][0]))
        writer.writeheader()
        writer.writerows(summary["jobs"])
    return {"directory": directory, "jobs": jobs, "status": state, "error": error_message}
