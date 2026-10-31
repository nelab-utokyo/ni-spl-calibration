import importlib
import os
import sys
import time
from pathlib import Path
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from ni_spl_calibration import DaqSettings, Setup, SimulatedHardware, load_setup, measure_reference
from ni_spl_calibration.cli import main
from ni_spl_calibration.records import load_replay
from ni_spl_calibration.storage import read_json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
widgets = pytest.importorskip("PySide6.QtWidgets")
qt_test = importlib.import_module("PySide6.QtTest")
gui = importlib.import_module("ni_spl_calibration._gui")


def wait_until(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        qt_test.QTest.qWait(10)
    app.processEvents()
    assert predicate(), "GUI operation timed out"


@pytest.fixture(scope="module")
def app():
    application = widgets.QApplication.instance() or widgets.QApplication([])
    application.setQuitOnLastWindowClosed(False)
    application.setStyle("Fusion")
    application.setStyleSheet(gui.STYLE)
    return application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, traceback: errors.append(value))
    view = gui.CalibrationWindow()
    monkeypatch.setattr(view, "_show_error", errors.append)
    view.output_root.setText(str(tmp_path / "results"))
    view.editor.set_setup(
        Setup("gui-test", "Test microphone chain", daq=DaqSettings(sample_rate_hz=32000))
    )
    view.show()
    app.processEvents()
    yield view, errors
    if view.worker is not None:
        view._request_stop()
        wait_until(app, lambda: view.worker is None)
    view.close()
    app.processEvents()
    assert errors == []


def test_setup_form_roundtrip_and_save_load(window, tmp_path, monkeypatch):
    view, _ = window
    example = Path(__file__).resolve().parents[1] / "examples/setup.yaml"
    setup = load_setup(example)
    view.editor.set_setup(setup)
    assert view.editor.setup() == setup
    path = tmp_path / "saved.yaml"
    monkeypatch.setattr(widgets.QFileDialog, "getSaveFileName", lambda *args: (str(path), "YAML"))
    view._save_setup()
    assert load_setup(path) == setup
    view.editor.fields["daq.sample_rate_hz"].setText("32000")
    assert "32,000" in view.shared_rate.text()
    assert view.editor.setup().daq.sample_rate_hz == 32000
    monkeypatch.setattr(widgets.QFileDialog, "getOpenFileName", lambda *args: (str(path), "YAML"))
    view._load_setup()
    assert view.editor.setup() == setup
    view.editor.fields["daq.sample_rate_hz"].setText("invalid")
    assert not view.measure_button.isEnabled()
    assert not view.monitor_start.isEnabled()


def test_gui_calibration_and_exact_replay_without_original_source(window, app, tmp_path):
    view, _ = window
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.reference is not None
    wav = tmp_path / "input.wav"
    sf.write(wav, 0.4 * np.sin(2 * np.pi * 2000 * np.arange(8820) / 44100), 44100)
    view.source_location.setText(str(wav))
    target = 70.12345678901234
    view.target.setValue(target)
    view.tabs.setCurrentIndex(2)
    assert view.calibrate_button.isEnabled()
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    first = view.last_output
    assert first is not None
    replay = load_replay(first)
    assert replay.stimulus.sample_rate_hz == 32000
    assert view.iterations.rowCount() >= 2
    assert "44,100 → 32,000 Hz" in view.stimulus_info.text()
    assert read_json(first / "run.json")["target_spl_db"] == target
    assert view.history.rowCount() == 1
    wav.unlink()
    view.editor.fields["daq.sample_rate_hz"].setText("invalid")
    view._restore(first)
    wait_until(app, lambda: view.worker is None)
    assert view.stimulus.sha256 == replay.stimulus.sha256
    assert view.source_kind.currentIndex() == 1
    assert view.editor.setup() == replay.setup
    assert view.target.value() == replay.target_spl_db
    assert view.last_output is None
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    second = view.last_output
    assert second != first
    assert load_replay(second).stimulus.sha256 == replay.stimulus.sha256
    assert read_json(second / "run.json")["parent_run"] == replay.parent_record
    assert view.history.rowCount() == 2
    events = [read_json(path) for path in sorted(view.activity.directory.glob("events/*.json"))]
    assert any(
        event.get("operation") == "restore" and event["action"] == "operation_finished"
        for event in events
    )
    assert len(list(view.activity.directory.glob("waveforms/*.npz"))) == 1
    view.editor.fields["daq.sample_rate_hz"].setText("20000")
    assert not view.calibrate_button.isEnabled()
    assert "再基準測定" in view.reference_status.text()


def test_monitor_updates_records_and_excludes_other_daq_operations(window, app):
    view, _ = window
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    view.tabs.setCurrentIndex(4)
    view.monitor_start.click()
    wait_until(app, lambda: view.monitor_duration_s >= 0.2)
    assert not view.measure_button.isEnabled()
    assert not view.calibrate_button.isEnabled()
    assert not view.mode.isEnabled()
    assert view.monitor_waveform.samples is not None
    assert view.monitor_rms.text() != "—"
    assert view.monitor_level.text() not in {"—", "未校正"}
    directory = view.job_output
    view.stop_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.measure_button.isEnabled()
    assert read_json(directory / "summary.json")["captured_samples"] >= 6400
    assert list(directory.glob("capture-*.npz"))
    assert "停止しました" in view.monitor_status.text()
    events = [read_json(path) for path in view.activity.directory.glob("events/*.json")]
    assert any(
        item["action"] == "stop_requested" and item["operation"] == "monitor" for item in events
    )


@pytest.mark.parametrize("close_window", [False, True])
def test_stop_or_close_during_capture_preserves_history_and_resets_output(
    window, app, tmp_path, monkeypatch, close_window
):
    view, _ = window
    entered, release = Event(), Event()
    setup = view.editor.setup()

    class BlockingHardware(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

        def play_and_record(self, output):
            entered.set()
            assert release.wait(3)
            return super().play_and_record(output)

    hardware = BlockingHardware(setup.daq)
    monkeypatch.setattr(gui, "make_hardware", lambda *args, **kwargs: hardware)
    reference = measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
    view._set_reference(reference, tmp_path / "reference.json")
    wav = tmp_path / "tone.wav"
    sf.write(wav, 0.4 * np.sin(2 * np.pi * 2000 * np.arange(6400) / 32000), 32000)
    view.source_location.setText(str(wav))
    view.calibrate_button.click()
    try:
        wait_until(app, entered.is_set)
        directory = view.job_output
        if close_window:
            view.close()
            assert view.worker is not None
        else:
            view.stop_button.click()
        assert not view.mode.isEnabled()
    finally:
        release.set()
    wait_until(app, lambda: view.worker is None)
    assert hardware.resets == 2
    assert not (directory / "result.json").exists()
    failure = read_json(directory / "failure.json")
    assert failure["status"] == "cancelled"
    assert len(failure["iterations"]) == 1
    assert load_replay(directory).stimulus.sample_rate_hz == 32000
    if close_window:
        assert not view.isVisible()


def test_real_hardware_can_be_cancelled_before_access(window, monkeypatch):
    view, _ = window
    view.mode.setCurrentIndex(1)
    monkeypatch.setattr(
        widgets.QMessageBox, "question", lambda *args: widgets.QMessageBox.StandardButton.Cancel
    )
    view.measure_button.click()
    assert view.worker is None
    assert not Path(view.output_root.text()).exists()


def test_no_command_opens_gui(monkeypatch):
    launcher = importlib.import_module("ni_spl_calibration.gui")
    monkeypatch.setattr(launcher, "main", lambda: 17)
    assert main([]) == 17
    assert main(["gui"]) == 17
