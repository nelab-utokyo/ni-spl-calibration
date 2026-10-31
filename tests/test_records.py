import json
from threading import Event

import numpy as np
import pytest

from ni_spl_calibration import (
    CalibrationError,
    DaqSettings,
    MeasurementCancelled,
    MeasurementSettings,
    Setup,
    SimulatedHardware,
    Stimulus,
    apply_calibration,
    load_result,
    measure_reference,
)
from ni_spl_calibration.records import find_runs, load_replay
from ni_spl_calibration.storage import read_json
from ni_spl_calibration.workflow import calibrate_to_directory


def make_bundle(directory, target=70, *, measurement=None):
    setup = Setup(
        "record-test",
        "Fixed measurement chain",
        daq=DaqSettings(sample_rate_hz=20000),
        measurement=measurement or MeasurementSettings(),
    )
    stimulus = Stimulus(0.4 * np.sin(2 * np.pi * 2000 * np.arange(4000) / 20000), 20000)
    reference = measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
    return calibrate_to_directory(
        stimulus,
        setup=setup,
        reference=reference,
        target_spl_db=target,
        output=directory,
        hardware=SimulatedHardware(setup.daq),
    )


@pytest.mark.parametrize(
    "filename", ["setup.json", "reference.json", "stimulus.npz", "result.json"]
)
def test_modified_bundle_is_rejected(tmp_path, filename):
    directory = tmp_path / "run"
    make_bundle(directory)
    assert load_replay(directory).target_spl_db == 70
    with (directory / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="整合性チェック"):
        load_replay(directory)


def test_failed_search_is_replayable_with_original_target(tmp_path):
    directory = tmp_path / "failed"
    with pytest.raises(CalibrationError):
        make_bundle(directory, target=150)
    replay = load_replay(directory)
    assert replay.target_spl_db == 150
    assert not (directory / "result.json").exists()
    assert read_json(directory / "failure.json")["status"] == "failed"
    assert find_runs(tmp_path)[0]["status"] == "failed"
    with pytest.raises(FileExistsError):
        make_bundle(directory)


def test_legacy_successful_bundle_can_be_restored(tmp_path):
    directory = tmp_path / "legacy"
    result = make_bundle(directory)
    (directory / "run.json").unlink()
    (directory / "checksums.json").unlink()
    replay = load_replay(directory)
    assert replay.stimulus.sha256 == result.stimulus["sha256"]
    # Older bundles lack file hashes, but internal consistency is still checked.
    setup = read_json(directory / "setup.json")
    setup["chain_notes"] = "Changed chain"
    (directory / "setup.json").write_text(json.dumps(setup))
    with pytest.raises(ValueError):
        load_replay(directory)


def test_saved_run_preserves_source_but_replays_only_calibrated_interval(tmp_path):
    directory = tmp_path / "selected-interval"
    make_bundle(directory, measurement=MeasurementSettings(start_s=0.05, stop_s=0.1))
    replay = load_replay(directory)
    result = load_result(directory / "result.json")
    assert len(replay.stimulus.samples) == 4000
    output = apply_calibration(replay.stimulus, result)
    assert len(output) == 1000
    np.testing.assert_array_equal(output, replay.stimulus.samples[1000:2000] * result.scale_v)


def test_reference_cooperative_stop_resets_output():
    event = Event()
    setup = Setup("stop-test", "Synthetic chain", daq=DaqSettings(sample_rate_hz=20000))

    class StoppingHardware(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

        def acquire(self, count, **options):
            values = super().acquire(count, **options)
            event.set()
            return values

    hardware = StoppingHardware(setup.daq, reference_mode=True)
    with pytest.raises(MeasurementCancelled):
        measure_reference(setup, hardware=hardware, cancel_requested=event.is_set)
    assert hardware.resets == 2
