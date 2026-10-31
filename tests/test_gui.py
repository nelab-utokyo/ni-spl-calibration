import importlib
import json
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
gui = importlib.import_module("ni_spl_calibration._gui")


def wait_until(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)  # Yield the GIL to the Python acquisition worker.
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
    view.mode.setCurrentIndex(0)  # Tests explicitly opt in to simulated hardware.
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
    metric_values = (view.level_value.text(), view.scale_value.text(), view.peak_value.text())
    view.language.setCurrentIndex(1)
    assert view.history.item(0, 1).text() == "Complete"
    assert view.history.item(0, 3).text() == "Simulation"
    assert view.iterations.horizontalHeaderItem(0).text() == "Iteration"
    assert view.last_output == first
    assert metric_values == (
        view.level_value.text(),
        view.scale_value.text(),
        view.peak_value.text(),
    )
    view.language.setCurrentIndex(0)
    assert view.history.item(0, 1).text() == "成功"
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
    assert view.monitor_spectrum.spectrum is not None
    assert view.monitor_rms.text() != "—"
    assert view.monitor_level.text() not in {"—", "未校正"}
    samples_before = view.monitor_samples
    duration_before = view.monitor_duration_s
    view.language.setCurrentIndex(1)
    assert view.language.isEnabled()
    assert view.monitor_samples is samples_before
    assert view.monitor_duration_s == duration_before
    assert view.worker is not None
    assert view.monitor_status.text().startswith("Monitoring")
    assert view.stop_button.text() == "Stop"
    view.language.setCurrentIndex(0)
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

        def play_and_record(self, output, **options):
            entered.set()
            assert release.wait(3)
            # Keep one acquired iteration for this existing stop-history test;
            # native mid-capture cancellation is covered by fake-driver tests.
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


def test_startup_defaults_to_hardware_without_accessing_daq(app, tmp_path, monkeypatch):
    def unexpected_hardware(*args, **kwargs):
        pytest.fail("Opening the GUI must not access hardware")

    workflow = importlib.import_module("ni_spl_calibration.workflow")
    monitor = importlib.import_module("ni_spl_calibration.monitor")
    monkeypatch.setattr(gui, "make_hardware", unexpected_hardware)
    monkeypatch.setattr(workflow, "make_hardware", unexpected_hardware)
    monkeypatch.setattr(monitor, "make_hardware", unexpected_hardware)
    monkeypatch.chdir(tmp_path)
    view = gui.CalibrationWindow()
    try:
        view.show()
        app.processEvents()
        assert not view.simulate
        assert view.mode.currentText() == "NI DAQ 実機"
        assert view.worker is None
        assert view.reference is None
        view.language.setCurrentIndex(1)
        assert view.mode.currentText() == "NI DAQ hardware"
        assert not view.simulate
        assert not (tmp_path / "results").exists()
    finally:
        view.close()


def test_language_switch_preserves_inputs_and_prepared_waveform(window, app, tmp_path):
    view, _ = window
    notes = "新規セットアップ: このメモは翻訳しない {value}"
    view.editor.fields["chain_notes"].setPlainText(notes)
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    wav = tmp_path / "日本語の波形 {value}.wav"
    sf.write(wav, 0.4 * np.sin(2 * np.pi * 2000 * np.arange(6400) / 32000), 32000)
    view.source_location.setText(str(wav))
    view.preview_button.click()
    wait_until(app, lambda: view.worker is None)
    original_setup = view.editor.setup()
    original_stimulus = view.stimulus
    original_reference = view.reference
    info = view.stimulus_info.text()
    view.language.setCurrentIndex(1)
    assert view.tabs.tabText(2) == "3  Waveform calibration"
    assert view.measure_button.text() == "Measure and save reference"
    assert view.calibrate_button.text() == "Start calibration"
    assert view.reference_status.text().startswith("Ready:")
    assert "Common sample rate: 32,000 Hz" in view.shared_rate.text()
    assert "Capture: 2 s" in view.reference_instruction.text()
    assert view.editor.setup() == original_setup
    assert view.stimulus is original_stimulus
    assert view.reference is original_reference
    assert view.source_location.text() == str(wav)
    assert view.stimulus_info.text() == info
    assert view.editor.fields["chain_notes"].toPlainText() == notes
    assert view._context()["ui_language"] == "en"
    # Invalid user input also survives switching, while the validation is translated.
    view.editor.fields["daq.sample_rate_hz"].setText("invalid")
    assert "check the input value" in view.shared_rate.text()
    view.language.setCurrentIndex(0)
    assert "入力値を確認" in view.shared_rate.text()
    assert view.editor.fields["daq.sample_rate_hz"].text() == "invalid"
    view.editor.fields["daq.sample_rate_hz"].setText("32000")
    view.language.setCurrentIndex(1)
    view.target.setValue(float("nan"))
    assert view.calibration_hint.text() == "Check the target SPL."
    assert not view.calibrate_button.isEnabled()
    view.target.setValue(70)


def test_english_dialogs_and_validation(window, monkeypatch):
    view, _ = window
    view.language.setCurrentIndex(1)
    view.mode.setCurrentIndex(1)
    calls = []

    def question(*args):
        calls.append(args)
        return widgets.QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(widgets.QMessageBox, "question", question)
    view.measure_button.click()
    assert calls[0][1] == "Start hardware measurement"
    assert "Fit the calibrator" in calls[0][2]
    assert "Sample rate: 32,000 Hz" in calls[0][2]
    assert view.worker is None
    file_dialogs = []
    monkeypatch.setattr(
        widgets.QFileDialog, "getOpenFileName", lambda *args: file_dialogs.append(args) or ("", "")
    )
    view._load_setup()
    assert file_dialogs[0][1] == "Load setup"
    view.target.setText("invalid")
    with pytest.raises(ValueError, match="numeric target SPL"):
        view.target.value()


def test_translation_templates_have_matching_parameters():
    from string import Formatter

    from ni_spl_calibration.i18n import ENGLISH

    def parameters(text):
        return {name for _, name, _, _ in Formatter().parse(text) if name is not None}

    for japanese, english in ENGLISH.items():
        assert parameters(japanese) == parameters(english), japanese


def test_test_playback_fader_stereo_monitor_mute_and_mid_file_stop(window, app, tmp_path):
    view, _ = window
    source_rate = 44100
    t = np.arange(source_rate * 2) / source_rate
    wav = tmp_path / "music-stereo.wav"
    sf.write(
        wav,
        np.column_stack((0.8 * np.sin(2 * np.pi * 1000 * t), 0.4 * np.sin(2 * np.pi * 3000 * t))),
        source_rate,
        subtype="DOUBLE",
    )
    view.source_location.setText(str(wav))
    view.tabs.setCurrentIndex(5)
    assert view.reference is None
    assert view.playback_start.isEnabled()
    view.playback_start.click()
    wait_until(
        app,
        lambda: (
            view.worker is not None
            and view.job_kind == "test_playback"
            and view.monitor_spectrum.spectrum is not None
        ),
    )
    assert view.volume_fader.isEnabled()
    assert view.mute_button.isEnabled()
    assert not view.measure_button.isEnabled()
    assert view.stimulus.provenance["source_channels"] == 2
    assert view.stimulus.provenance["channel_mode"] == "mix"
    assert view.stimulus.sample_rate_hz == 32000
    assert "元WAV 2 ch" in view.playback_channel_info.text()
    assert "変換前 RMS" in view.playback_file_info.text()
    assert view.monitor_rms.text() != "—"
    directory = view.job_output
    view.volume_fader.setValue(800)
    wait_until(app, lambda: "80.0%" in view.playback_status.text())
    view.language.setCurrentIndex(1)
    assert view.playback_start.text() == "Start test playback"
    assert view.volume_fader.isEnabled()
    view.mute_button.click()
    wait_until(app, lambda: view.playback_peak.text() == "0")
    view.volume_fader.setValue(500)
    wait_until(app, lambda: "50.0%" in view.playback_status.text())
    view.playback_stop.click()
    wait_until(app, lambda: view.worker is None)
    summary = read_json(directory / "summary.json")
    assert summary["status"] == "stopped"
    assert summary["duration_s"] < 2  # Stopped inside the original two-second file.
    assert view.playback_peak.text() == "0"
    assert view.measure_button.isEnabled()
    assert not view.playback_stop.isEnabled()
    records = [json.loads(line) for line in (directory / "levels.jsonl").read_text().splitlines()]
    assert {0.1, 0.8, 0.0, 0.5} <= {item["volume_fraction"] for item in records}
    assert all(item["output_peak_v"] <= view.editor.setup().daq.max_peak_v for item in records)
    assert records[-1]["spectrum"]["edges_hz"][-1] == 16000


def test_built_in_tone_selection_and_one_shot_test_playback(window, app):
    view, _ = window
    view._choose_test_tone()
    wait_until(app, lambda: view.worker is None)
    assert view.stimulus.provenance["callable"].endswith(":make_test_tone")
    assert view.stimulus.sample_rate_hz == view.editor.setup().daq.sample_rate_hz
    view.loop_audio.setChecked(False)
    view.playback_start.click()
    wait_until(app, lambda: view.worker is None, timeout=8)
    assert read_json(view.job_output / "summary.json")["status"] == "complete"
    assert view.playback_peak.text() == "0"
    assert view.monitor_spectrum.spectrum is not None
