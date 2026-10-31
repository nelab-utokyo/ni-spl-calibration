from threading import Event

import numpy as np
import pytest

import ni_spl_calibration.monitor as monitor_module
from ni_spl_calibration import DaqSettings, Setup, SimulatedHardware, measure_reference
from ni_spl_calibration.hardware import output_guard
from ni_spl_calibration.monitor import monitor_input
from ni_spl_calibration.records import file_sha256
from ni_spl_calibration.storage import read_json


@pytest.mark.parametrize(
    "calibrated,save_capture,clipped",
    [
        (False, False, False),
        (True, True, False),
        (True, True, True),
    ],
)
def test_monitor_levels_recording_and_cleanup(
    tmp_path, monkeypatch, calibrated, save_capture, clipped
):
    setup = Setup("monitor-test", "Fixed microphone", daq=DaqSettings(sample_rate_hz=20000))
    stop = Event()

    class Hardware(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

        def monitor(self, block_size, *, cancel_requested):
            with output_guard(self):
                while not cancel_requested():
                    amplitude = 10 if clipped else 0.01
                    yield amplitude * np.sin(2 * np.pi * 1000 * np.arange(block_size) / 20000)

    hardware = Hardware(setup.daq)
    monkeypatch.setattr(monitor_module, "make_hardware", lambda *args, **kwargs: hardware)
    reference = (
        measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
        if calibrated
        else None
    )
    updates = []

    def on_frame(frame):
        updates.append(frame)
        if len(updates) == 3:
            stop.set()

    directory = tmp_path / "monitor"
    result = monitor_input(
        setup,
        output=directory,
        simulate=True,
        reference=reference,
        save_capture=save_capture,
        cancel_requested=stop.is_set,
        on_frame=on_frame,
    )
    assert result["captured_samples"] == 6000
    assert result["duration_s"] == pytest.approx(0.3)
    assert hardware.resets == 2
    assert len(updates) == 3
    assert updates[-1]["clipped"] == clipped
    if calibrated and not clipped:
        expected = 20 * np.log10(0.01 / np.sqrt(2) / reference.sensitivity_v_per_pa / 20e-6)
        assert updates[-1]["level_spl_db"] == pytest.approx(expected)
    else:
        assert updates[-1]["level_spl_db"] is None
    captures = list(directory.glob("capture-*.npz"))
    assert len(captures) == (3 if save_capture else 0)
    if captures:
        with np.load(sorted(captures)[0], allow_pickle=False) as data:
            assert np.array_equal(data["samples"], updates[0]["samples"])
            assert int(data["sample_rate_hz"]) == 20000
    assert read_json(directory / "summary.json")["levels_sha256"] == file_sha256(
        directory / "levels.jsonl"
    )


def test_monitor_read_failure_retains_partial_record(tmp_path, monkeypatch):
    setup = Setup("monitor-failure", "Fixed microphone", daq=DaqSettings(sample_rate_hz=20000))

    class Hardware(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

        def monitor(self, block_size, *, cancel_requested):
            with output_guard(self):
                yield np.zeros(block_size)
                raise RuntimeError("Injected AI failure")

    hardware = Hardware(setup.daq)
    monkeypatch.setattr(monitor_module, "make_hardware", lambda *args, **kwargs: hardware)
    directory = tmp_path / "failed-monitor"
    with pytest.raises(RuntimeError, match="Injected"):
        monitor_input(
            setup,
            output=directory,
            simulate=True,
            cancel_requested=lambda: False,
            on_frame=lambda frame: None,
        )
    assert hardware.resets == 2
    assert read_json(directory / "failure.json")["captured_samples"] == 2000
    assert not (directory / "summary.json").exists()
    assert list(directory.glob("capture-*.npz"))
