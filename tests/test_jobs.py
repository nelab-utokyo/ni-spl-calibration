from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from ni_spl_calibration import DaqSettings, Setup, SimulatedHardware, measure_reference
from ni_spl_calibration.jobs import (
    CalibrationJob,
    load_plan,
    run_queue,
    save_plan,
    starting_context,
)
from ni_spl_calibration.records import file_sha256, find_runs, load_replay
from ni_spl_calibration.stimulus import Stimulus
from ni_spl_calibration.storage import read_json, write_json


@pytest.fixture
def inputs():
    setup = Setup("queue-test", "fixed chain", daq=DaqSettings(sample_rate_hz=8000))
    reference = measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
    jobs = []
    for index, (amplitude, frequency, target) in enumerate(
        [(0.3, 1000, 65), (0.7, 2000, 70), (0.2, 1500, 60)]
    ):
        stimulus = Stimulus(
            amplitude * np.sin(2 * np.pi * frequency * np.arange(1600) / 8000), 8000
        )
        settings = replace(setup.measurement, start_scale_v=0.02 + index * 0.01)
        jobs.append(
            CalibrationJob(
                f"sound-{index}",
                stimulus,
                settings,
                target,
                start_context=starting_context(replace(setup, measurement=settings), True),
                group="music" if index < 2 else "noise",
            )
        )
    return setup, reference, jobs


def test_plan_roundtrip_preserves_frozen_waveforms_and_per_stimulus_settings(tmp_path, inputs):
    setup, reference, jobs = inputs
    jobs[1].measurement = replace(jobs[1].measurement, start_s=0.02, stop_s=0.12)
    jobs[2].start_context = None
    directory = tmp_path / "plan"
    save_plan(directory, jobs, setup=setup, reference=reference, simulate=True)
    restored = load_plan(directory)
    assert restored.setup == setup
    assert restored.reference == reference
    assert restored.simulate is True
    assert [j.id for j in restored.jobs] == [j.id for j in jobs]
    for original, copied in zip(jobs, restored.jobs):
        np.testing.assert_array_equal(original.stimulus.samples, copied.stimulus.samples)
        assert copied.measurement == original.measurement
        assert copied.target_spl_db == original.target_spl_db
        assert copied.group == original.group
    assert [j.ready(setup, True) for j in restored.jobs] == [True, True, False]
    with pytest.raises(FileExistsError):
        save_plan(directory, jobs, setup=setup, reference=reference, simulate=True)


@pytest.mark.parametrize("filename", ["plan.json", "setup.json", "waveform"])
def test_plan_integrity_checks(tmp_path, inputs, filename):
    setup, reference, jobs = inputs
    save_plan(tmp_path / "plan", jobs, setup=setup, reference=reference, simulate=True)
    directory = tmp_path / "plan"
    path = (
        next((directory / "waveforms").glob("*.npz"))
        if filename == "waveform"
        else directory / filename
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="integrity|整合性"):
        load_plan(directory)


def test_serial_execution_results_and_portable_restore(tmp_path, inputs):
    setup, reference, jobs = inputs
    events, starts = [], []

    def hardware_factory(current, **options):
        starts.append(current.measurement.start_scale_v)
        return SimulatedHardware(current.daq)

    output = tmp_path / "batch"
    result = run_queue(
        output,
        jobs,
        selected_ids={j.id for j in jobs},
        setup=setup,
        reference=reference,
        simulate=True,
        hardware_factory=hardware_factory,
        on_progress=events.append,
    )
    assert result["status"] == "complete"
    assert starts == [0.02, 0.03, 0.04]
    assert [e["job_id"] for e in events if e.get("kind") == "queue_job"] == [j.id for j in jobs]
    assert all(j.status == "complete" for j in jobs)
    for job in jobs:
        replay = load_replay(job.output)
        assert replay.stimulus.sha256 == job.stimulus.sha256
        assert replay.target_spl_db == job.target_spl_db
        assert replay.setup.measurement == job.measurement
    assert (output / "results.csv").is_file()
    assert len(find_runs(tmp_path)) == 1
    relocated = tmp_path / "relocated"
    output.rename(relocated)
    restored = load_plan(relocated)
    assert all(j.status == "complete" for j in restored.jobs)
    assert all(j.output.is_relative_to(relocated) for j in restored.jobs)


def test_pause_after_current_and_resume_only_remaining_without_overwrite(tmp_path, inputs):
    setup, reference, jobs = inputs
    pause = Event()

    def progress(item):
        if item.get("kind") == "queue_job":
            pause.set()

    first = tmp_path / "first"
    result = run_queue(
        first,
        jobs,
        selected_ids={j.id for j in jobs},
        setup=setup,
        reference=reference,
        simulate=True,
        pause_after=pause.is_set,
        on_progress=progress,
    )
    assert result["status"] == "held"
    assert [j.status for j in jobs] == ["complete", "pending", "pending"]
    old_result_hash = file_sha256(jobs[0].output / "result.json")
    plan = load_plan(first)
    remaining = {j.id for j in plan.jobs if j.status != "complete"}
    second = tmp_path / "second"
    run_queue(
        second,
        plan.jobs,
        selected_ids=remaining,
        setup=setup,
        reference=reference,
        simulate=True,
        parent=first,
    )
    assert len(list((second / "runs").glob("*/result.json"))) == 2
    assert all(j.status == "complete" for j in plan.jobs)
    assert file_sha256(jobs[0].output / "result.json") == old_result_hash
    assert read_json(second / "plan.json")["parent_plan"] == str(first)
    first.rename(tmp_path / "old-record-moved-away")
    portable = tmp_path / "portable-resume"
    second.rename(portable)
    restored = load_plan(portable)
    assert all(j.status == "complete" and j.output.is_relative_to(portable) for j in restored.jobs)


@pytest.mark.parametrize("cancel", [False, True])
def test_error_or_mid_playback_cancel_holds_rest_and_preserves_failure(tmp_path, inputs, cancel):
    setup, reference, jobs = inputs
    stop = Event()

    class Hardware(SimulatedHardware):
        def play_and_record(self, output, **options):
            if cancel:
                stop.set()
                return super().play_and_record(output, **options)
            raise RuntimeError("Disconnected DAQ")

    result = run_queue(
        tmp_path / "batch",
        jobs,
        selected_ids={j.id for j in jobs},
        setup=setup,
        reference=reference,
        simulate=True,
        cancel_requested=stop.is_set,
        hardware_factory=lambda current, **kw: Hardware(current.daq),
    )
    assert result["status"] == "held"
    assert [j.status for j in jobs] == ["cancelled" if cancel else "failed", "pending", "pending"]
    failure = read_json(jobs[0].output / "failure.json")
    assert failure["status"] == jobs[0].status
    restored = load_plan(tmp_path / "batch")
    assert [j.status for j in restored.jobs] == [j.status for j in jobs]


def test_interrupted_process_is_restored_as_cancelled_without_starting_hardware(tmp_path, inputs):
    setup, reference, jobs = inputs
    directory = tmp_path / "plan"
    save_plan(directory, jobs, setup=setup, reference=reference, simulate=True)
    write_json(
        directory / "events/000001.json",
        {
            "job_id": jobs[0].id,
            "status": "running",
            "attempt": "a" * 32,
        },
    )
    restored = load_plan(directory)
    assert [j.status for j in restored.jobs] == ["cancelled", "pending", "pending"]
    assert not (directory / "runs").exists()


def test_changed_hardware_band_or_sample_rate_requires_rechecking_start_level(inputs):
    setup, reference, jobs = inputs
    job = jobs[0]
    assert job.ready(setup, True)
    assert not job.ready(setup, False)
    assert not job.ready(replace(setup, chain_notes="moved microphone"), True)
    assert not job.ready(replace(setup, daq=replace(setup.daq, max_peak_v=0.3)), True)
    job.measurement = replace(job.measurement, band_hz=(500, 2500))
    assert not job.ready(setup, True)


def test_all_selected_jobs_are_validated_before_any_output_or_record(tmp_path, inputs):
    setup, reference, jobs = inputs
    jobs[-1].start_context = None
    accessed = []
    with pytest.raises(ValueError, match="starting level"):
        run_queue(
            tmp_path / "bad",
            jobs,
            selected_ids={j.id for j in jobs},
            setup=setup,
            reference=reference,
            simulate=True,
            hardware_factory=lambda *a, **kw: accessed.append(True),
        )
    assert not accessed
    assert not (tmp_path / "bad").exists()


def test_rerun_failure_cannot_display_the_previous_success_as_a_current_result(tmp_path, inputs):
    setup, reference, jobs = inputs
    run_queue(
        tmp_path / "first",
        jobs,
        selected_ids={j.id for j in jobs},
        setup=setup,
        reference=reference,
        simulate=True,
    )

    def disconnected(*args, **kwargs):
        raise RuntimeError("Device unavailable")

    run_queue(
        tmp_path / "second",
        jobs,
        selected_ids={jobs[0].id},
        setup=setup,
        reference=reference,
        simulate=True,
        hardware_factory=disconnected,
    )
    restored = load_plan(tmp_path / "second")
    assert restored.jobs[0].status == "failed"
    assert restored.jobs[0].result is None
    assert restored.jobs[0].iterations == []
    assert restored.jobs[1].status == "complete"
    assert (tmp_path / "second/prior-runs" / jobs[0].id / "result.json").is_file()


def test_identical_samples_keep_each_sources_provenance(tmp_path, inputs):
    setup, reference, jobs = inputs
    jobs[1].stimulus = Stimulus(
        jobs[0].stimulus.samples, 8000, {"kind": "array", "name": "another-source"}
    )
    save_plan(tmp_path / "plan", jobs, setup=setup, reference=reference, simulate=True)
    plan = load_plan(tmp_path / "plan")
    assert plan.jobs[0].stimulus.sha256 == plan.jobs[1].stimulus.sha256
    assert plan.jobs[0].stimulus.provenance == jobs[0].stimulus.provenance
    assert plan.jobs[1].stimulus.provenance == jobs[1].stimulus.provenance
