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


def test_spl_band_controls_validate_limits_and_preserve_waveform(window, app):
    view, _ = window
    band = view.editor.band
    assert band.value() is None
    assert float(band.high.text()) == 16000
    view._choose_test_tone()
    wait_until(app, lambda: view.worker is None)
    stimulus = view.stimulus
    view.monitor_level.setText("70.00")
    view.level_meter.set_levels(70, 70)
    band.mode.setCurrentIndex(2)
    band.low.setText("100.25")
    band.high.setText("8000.5")
    assert view.editor.setup().measurement.band_hz == (100.25, 8000.5)
    assert view.monitor_spectrum.analysis_band == (100.25, 8000.5)
    assert view.monitor_level.text() == "—"
    assert view.level_meter.level is None
    assert view.stimulus is stimulus
    assert view.playback_start.isEnabled()
    saved = view.editor.setup()
    for low, high in [
        ("-1", "8000"),
        ("500", "500"),
        ("1000", "500"),
        ("0", "17000"),
        ("0", "nan"),
        ("0", ""),
        ("abc", "8000"),
    ]:
        band.low.setText(low)
        band.high.setText(high)
        assert not view.monitor_start.isEnabled()
        assert not view.playback_start.isEnabled()
        with pytest.raises(ValueError):
            view.editor.setup()
    assert view._context()["setup"]["unvalidated"]["measurement.band_hz"]["low_hz"] == "abc"
    view.language.setCurrentIndex(1)
    assert band.info.text() == "Enter band limits in Hz"
    band.mode.setCurrentIndex(1)
    assert band.value() == (20, 20000)  # A preset is never silently clipped to Nyquist.
    assert not view.monitor_start.isEnabled()
    assert "16000" in band.info.text()
    view.editor.fields["daq.sample_rate_hz"].setText("48000")
    assert view.monitor_start.isEnabled()
    assert band.value() == (20, 20000)
    band.mode.setCurrentIndex(0)
    assert float(band.high.text()) == 24000
    assert band.high.isReadOnly()
    view.editor.fields["daq.sample_rate_hz"].setText("44100")
    assert float(band.high.text()) == 22050
    view.editor.set_setup(saved)
    assert band.value() == saved.measurement.band_hz
    assert not band.high.isReadOnly()


@pytest.mark.parametrize("band", [None, (1500, 2500)])
def test_gui_calibration_and_exact_replay_without_original_source(window, app, tmp_path, band):
    view, _ = window
    if band is not None:
        view.editor.band.mode.setCurrentIndex(2)
        view.editor.band.low.setText(str(band[0]))
        view.editor.band.high.setText(str(band[1]))
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.reference is not None
    wav = tmp_path / "input.wav"
    sf.write(wav, 0.4 * np.sin(2 * np.pi * 2000 * np.arange(8820) / 44100), 44100)
    view.source_location.setText(str(wav))
    target = 70.12345678901234
    view.target.setValue(target)
    view.tabs.setCurrentIndex(gui.Page.CALIBRATION)
    assert view.calibrate_button.isEnabled()
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    first = view.last_output
    assert first is not None
    replay = load_replay(first)
    assert replay.setup.measurement.band_hz == band
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
    assert view.editor.band.value() == band
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
    view.editor.band.setValue((200, 8000))
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    view.tabs.setCurrentIndex(gui.Page.SETUP)
    view.monitor_start.click()
    wait_until(app, lambda: view.monitor_duration_s >= 0.2)
    assert not view.measure_button.isEnabled()
    assert not view.calibrate_button.isEnabled()
    assert not view.mode.isEnabled()
    assert not view.editor.band.mode.isEnabled()
    assert not view.editor.band.low.isEnabled()
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
    assert view.spectrum_state.text().startswith("Live")
    assert view.stop_button.text() == "Stop"
    view.language.setCurrentIndex(0)
    directory = view.job_output
    view.stop_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.measure_button.isEnabled()
    assert read_json(directory / "summary.json")["captured_samples"] >= 6400
    assert not list(directory.glob("capture-*.npz"))
    assert read_json(directory / "monitor.json")["save_capture"] is False
    assert read_json(directory / "setup.json")["measurement"]["band_hz"] == [200, 8000]
    assert "停止" in view.spectrum_state.text()
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
        assert "再生中：" in view.status.text()
        view.language.setCurrentIndex(1)
        assert view.status.text().startswith("Playing:")
        view.language.setCurrentIndex(0)
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
        assert not view.save_capture.isChecked()
        assert view.editor.setup().measurement.max_upward_step_db == 10
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
    assert view.tabs.tabText(gui.Page.PLAYBACK) == "3  Audio / starting level"
    assert view.measure_button.text() == "Measure reference"
    assert view.calibrate_button.text() == "Start calibration"
    assert view.reference_status.text() == "Ready"
    assert "Rate 32,000 Hz" in view.shared_rate.text()
    assert "AI Dev1/ai0" in view.reference_instruction.text()
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
    assert view.calibration_hint.text() == "Check target SPL"
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
    view.editor.band.setValue((500, 5000))
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
    view.tabs.setCurrentIndex(gui.Page.PLAYBACK)
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
    assert not view.editor.band.mode.isEnabled()
    assert view.stimulus.provenance["source_channels"] == 2
    assert view.stimulus.provenance["channel_mode"] == "mix"
    assert view.stimulus.sample_rate_hz == 32000
    assert "元WAV 2 ch" in view.playback_channel_info.text()
    assert "変換前 RMS" in view.playback_file_info.text()
    assert view.monitor_rms.text() != "—"
    directory = view.job_output
    view.volume_fader.setValue(800)
    wait_until(app, lambda: "80.0%" in view.status.text())
    view.language.setCurrentIndex(1)
    assert view.playback_start.text() == "Test playback"
    assert view.volume_fader.isEnabled()
    view.mute_button.click()
    wait_until(app, lambda: view.playback_peak.text() == "0")
    view.volume_fader.setValue(500)
    wait_until(app, lambda: "50.0%" in view.status.text())
    view.playback_stop.click()
    wait_until(app, lambda: view.worker is None)
    summary = read_json(directory / "summary.json")
    assert summary["status"] == "stopped"
    assert summary["duration_s"] < 2  # Stopped inside the original two-second file.
    assert not list(directory.glob("capture-*.npz"))
    assert read_json(directory / "playback.json")["save_capture"] is False
    assert read_json(directory / "setup.json")["measurement"]["band_hz"] == [500, 5000]
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


@pytest.mark.parametrize("windowed", [False, True])
def test_playback_volume_is_applied_to_the_first_calibration_output(
    window, app, tmp_path, windowed
):
    view, _ = window
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    reference = view.reference
    assert view.reference_next.isEnabled()
    view.reference_next.click()
    assert view.tabs.currentIndex() == gui.Page.PLAYBACK
    if windowed:
        rate = view.editor.setup().daq.sample_rate_hz
        samples = 0.8 * np.sin(2 * np.pi * 1000 * np.arange(rate) / rate)
        samples[rate // 4 : 3 * rate // 4] *= 0.25
        path = tmp_path / "varying-level.wav"
        sf.write(path, samples, rate, subtype="DOUBLE")
        view.source_location.setText(str(path))
        view._prepare(playback_preview=True)
    else:
        view._choose_test_tone()
    wait_until(app, lambda: view.worker is None)
    stimulus = view.stimulus
    if windowed:
        view.editor.fields["measurement.start_s"].setText("0.25")
        view.editor.fields["measurement.stop_s"].setText("0.75")
        assert view.stimulus is stimulus
        assert view.waveform.selection == (0.25, 0.75)
    view.playback_start.click()
    wait_until(app, lambda: view.adopt_start_button.isEnabled())
    view.volume_fader.setValue(150)
    # An outstanding fader request is not accepted until hardware plays it.
    assert not view.adopt_start_button.isEnabled()
    wait_until(app, lambda: view.adopt_start_button.isEnabled())
    choice = view._playback_start_choice
    assert choice["volume_fraction"] == 0.15
    assert choice["peak_v"] == pytest.approx(0.075)
    initial_peak = 0.01875 if windowed else 0.075
    view.language.setCurrentIndex(1)
    assert view.adopt_start_button.text() == "Use this level →"
    view.adopt_start_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.tabs.currentIndex() == gui.Page.CALIBRATION
    assert view.stimulus is stimulus
    assert view.reference is reference
    assert view.editor.setup().measurement.start_scale_v == pytest.approx(choice["scale_v"])
    assert f"{initial_peak:g} Vpeak" in view.start_level_info.text()
    assert read_json(Path(choice["playback_record"]) / "summary.json")["status"] == "stopped"
    events = [read_json(path) for path in view.activity.directory.glob("events/*.json")]
    selected = next(event for event in events if event["action"] == "start_level_selected")
    assert selected["scale_v"] == choice["scale_v"]
    assert selected["initial_peak_v"] == pytest.approx(initial_peak)
    view.target.setValue(70)
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    result = read_json(view.last_output / "result.json")
    assert result["iterations"][0]["scale_v"] == pytest.approx(choice["scale_v"])
    assert result["iterations"][0]["output_peak_v"] == pytest.approx(initial_peak)
    assert result["setup"]["measurement"]["start_scale_v"] == choice["scale_v"]
    view.source_location.setText("a-different-waveform.wav")
    assert not view.adopt_start_button.isEnabled()


def test_multiple_stimuli_prepare_save_restore_run_and_rerun_selected(window, app, tmp_path):
    view, _ = window
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    files = []
    for index, amplitude in enumerate([0.3, 0.7]):
        path = tmp_path / f"sound-{index}.wav"
        sf.write(
            path,
            amplitude * np.sin(2 * np.pi * (1000 + index * 1000) * np.arange(8820) / 44100),
            44100,
        )
        files.append(path)
    view.tabs.setCurrentIndex(gui.Page.PLAYBACK)
    view._import_sources(files)
    wait_until(app, lambda: view.worker is None)
    assert len(view.jobs) == 2
    assert view.stimulus_table.rowCount() == view.queue_table.rowCount() == 2
    assert not view.calibrate_button.isEnabled()
    hashes = [job.stimulus.sha256 for job in view.jobs]
    scales = []
    for index in range(2):
        assert view._active_job_id == view.jobs[index].id
        view.target.setValue(65 + index * 5)
        view.volume_fader.setValue(100 + index * 50)
        view.playback_start.click()
        wait_until(app, lambda: view.adopt_start_button.isEnabled())
        scales.append(view._playback_start_choice["scale_v"])
        assert view.adopt_start_button.text() == "この開始音量を採用"
        view.adopt_start_button.click()
        wait_until(app, lambda: view.worker is None)
        assert view.tabs.currentIndex() == (
            gui.Page.PLAYBACK if index == 0 else gui.Page.CALIBRATION
        )
    assert [j.measurement.start_scale_v for j in view.jobs] == pytest.approx(scales)
    assert view.calibrate_button.isEnabled()
    view._save_plan()
    wait_until(app, lambda: view.worker is None)
    saved = view.last_plan
    for path in files:
        path.unlink()
    view._restore(saved)
    wait_until(app, lambda: view.worker is None)
    assert [j.stimulus.sha256 for j in view.jobs] == hashes
    view.language.setCurrentIndex(1)
    assert view.calibrate_button.text() == "Start 2 jobs"
    assert view.stimulus_table.item(0, 3).text() == "Checked"
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    assert [j.status for j in view.jobs] == ["complete", "complete"]
    assert [j.result["target_spl_db"] for j in view.jobs] == [65, 70]
    assert [j.result["setup"]["measurement"]["start_scale_v"] for j in view.jobs] == pytest.approx(
        scales
    )
    first_outputs = [j.output for j in view.jobs]
    contents = [p.joinpath("result.json").read_bytes() for p in first_outputs]
    batch = view.last_plan
    assert (batch / "results.csv").is_file()
    assert not list(batch.rglob("capture-*.npz"))
    assert not view.calibrate_button.isEnabled()  # No unfinished jobs.
    view.queue_table.selectRow(0)
    assert view.level_value.text() == f"{view.jobs[0].result['measured_spl_db']:.2f}"
    view.execution_scope.setCurrentIndex(1)
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.jobs[0].output != first_outputs[0]
    assert view.jobs[1].output == first_outputs[1]
    assert [p.joinpath("result.json").read_bytes() for p in first_outputs] == contents


def test_queue_bulk_target_group_duplicate_order_and_parameter_isolation(window, app, tmp_path):
    view, _ = window
    paths = []
    for index in range(2):
        path = tmp_path / f"tone-{index}.wav"
        sf.write(path, 0.4 * np.sin(2 * np.pi * 1000 * np.arange(6400) / 32000), 32000)
        paths.append(path)
    view._import_sources(paths)
    wait_until(app, lambda: view.worker is None)
    view.editor.fields["measurement.start_s"].setText("0.03")
    view._adopt_manual_start()
    view._activate_job(view.jobs[1].id)
    assert view.editor.setup().measurement.start_s == 0
    view._adopt_manual_start()
    view.stimulus_table.setRangeSelected(widgets.QTableWidgetSelectionRange(0, 0, 1, 3), True)
    view.target.setValue(62.5)
    view._apply_targets()
    assert [j.target_spl_db for j in view.jobs] == [62.5, 62.5]
    view.group_name.setText("music")
    view._apply_group()
    assert [j.group for j in view.jobs] == ["music", "music"]
    view.stimulus_table.selectRow(0)
    view._duplicate_jobs()
    assert len(view.jobs) == 3
    duplicate = view._active_job()
    assert duplicate.id != view.jobs[0].id
    assert duplicate.stimulus is view.jobs[0].stimulus
    assert duplicate.ready(view.editor.setup(), True)
    view.target.setValue(80)
    assert view.jobs[0].target_spl_db == 62.5
    view._move_job(-1)
    assert view.jobs[1].id == duplicate.id
    view.editor.fields["daq.max_peak_v"].setText("0.3")
    assert not any(j.ready(view.editor.setup(), True) for j in view.jobs)
    view.stimulus_table.selectRow(1)
    view._remove_jobs()
    assert len(view.jobs) == 2


@pytest.mark.parametrize("immediate", [True, False])
def test_gui_queue_stop_and_resume(window, app, tmp_path, monkeypatch, immediate):
    view, _ = window
    entered, release = Event(), Event()
    view.measure_button.click()
    wait_until(app, lambda: view.worker is None)
    paths = []
    for index in range(2):
        path = tmp_path / f"tone-{index}.wav"
        sf.write(path, 0.4 * np.sin(2 * np.pi * 1000 * np.arange(6400) / 32000), 32000)
        paths.append(path)
    view._import_sources(paths)
    wait_until(app, lambda: view.worker is None)
    for job in view.jobs:
        view._activate_job(job.id)
        view._adopt_manual_start()
    factory = gui.make_hardware

    class BlockingHardware(SimulatedHardware):
        def play_and_record(self, output, **options):
            entered.set()
            assert release.wait(3)
            return super().play_and_record(output, **options)

    monkeypatch.setattr(gui, "make_hardware", lambda current, **kw: BlockingHardware(current.daq))
    view.calibrate_button.click()
    try:
        wait_until(app, entered.is_set)
        assert not view.stimulus_table.isEnabled()
        assert view.pause_after.isEnabled()
        if immediate:
            view.stop_button.click()
        else:
            view.pause_after.setChecked(True)
    finally:
        release.set()
    wait_until(app, lambda: view.worker is None)
    assert [j.status for j in view.jobs] == ["cancelled" if immediate else "complete", "pending"]
    previous = view.last_plan
    monkeypatch.setattr(gui, "make_hardware", factory)
    view.execution_scope.setCurrentIndex(1)
    view._restore(previous)
    wait_until(app, lambda: view.worker is None)
    assert view.execution_scope.currentData() == "remaining"
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    assert [j.status for j in view.jobs] == ["complete", "complete"]
    assert view.last_plan != previous
    events = [read_json(path) for path in view.activity.directory.glob("events/*.json")]
    assert any(
        event["action"] == "operation_finished"
        and event.get("operation") == "queue"
        and event["status"] == "held"
        for event in events
    )


def test_single_stimulus_can_be_recalibrated_without_changing_execution_scope(window, app):
    view, _ = window
    view._choose_test_tone()
    wait_until(app, lambda: view.worker is None)
    view._measure_reference()
    wait_until(app, lambda: view.worker is None)
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    first = view.last_output
    assert view.execution_scope.currentData() == "remaining"
    assert view.calibrate_button.isEnabled()
    view.calibrate_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.last_output != first
    assert (first / "result.json").is_file()
    view.editor.fields["daq.max_peak_v"].setText("0.3")
    assert view.jobs[0].result is None
    assert view.jobs[0].status == "pending"
    assert not view.jobs[0].ready(view.editor.setup(), True)


def calibrate_comparison_source(view, app):
    view._choose_test_tone()
    wait_until(app, lambda: view.worker is None)
    view._measure_reference()
    wait_until(app, lambda: view.worker is None)
    view._calibrate()
    wait_until(app, lambda: view.worker is None)
    assert view.compare_button.isEnabled()


def test_fixed_gain_comparison_repeated_positions_language_and_original_result(
    window, app, monkeypatch
):
    from ni_spl_calibration.calibration import apply_calibration, load_result
    from ni_spl_calibration.comparison import load_comparison

    view, _ = window
    assert not view.compare_button.isEnabled()
    calibrate_comparison_source(view, app)
    original_output = view.last_output
    original_bytes = (original_output / "result.json").read_bytes()
    original_setup, original_waveform = view.editor.setup(), view.stimulus
    calibration_metrics = (view.level_value.text(), view.scale_value.text(), view.peak_value.text())
    response, outputs = [0.5], []

    class Moved(SimulatedHardware):
        def play_and_record(self, output, **options):
            outputs.append(output.copy())
            return response[0] * super().play_and_record(output, **options)

    def no_hardware(*args, **kwargs):
        pytest.fail("Opening a comparison window must not access DAQ")

    monkeypatch.setattr(gui, "make_hardware", no_hardware)
    view.compare_button.click()
    wait_until(app, lambda: view.worker is None)
    dialog = view.comparison_dialog
    assert dialog.isVisible()
    assert not dialog.save_capture.isChecked()
    assert not hasattr(dialog, "volume_fader")
    monkeypatch.setattr(gui, "make_hardware", lambda setup, **kw: Moved(setup.daq))
    dialog.position.setText("20 cm {value}")
    dialog.start_button.click()
    wait_until(app, lambda: view.worker is None)
    first = dialog.last_output
    assert dialog.rows[-1]["delta_db"] == pytest.approx(-6.0206, abs=0.002)
    assert dialog.difference.text() == "-6.02"
    np.testing.assert_array_equal(
        outputs[0],
        np.append(
            apply_calibration(original_waveform, load_result(original_output / "result.json")), 0
        ),
    )
    assert dialog.rows[-1]["position"] == "20 cm {value}"
    assert not list(first.glob("*-input.npz"))
    assert (
        load_comparison(first).rows[-1]["scale_v"]
        == load_result(original_output / "result.json").scale_v
    )
    view.language.setCurrentIndex(1)
    assert dialog.windowTitle() == "Position comparison"
    assert dialog.start_button.text() == "Play / measure"
    assert dialog.position.text() == "20 cm {value}"
    assert dialog.history.item(0, 5).text() == "Complete"
    response[0] = 2
    dialog.position.setText("30 cm")
    dialog.start_button.click()
    wait_until(app, lambda: view.worker is None)
    assert dialog.last_output != first
    assert dialog.history.rowCount() == 2
    assert dialog.difference.text() == "+6.02"
    np.testing.assert_array_equal(outputs[0], outputs[1])
    assert view.editor.setup() == original_setup
    assert view.stimulus is original_waveform
    assert (
        view.level_value.text(),
        view.scale_value.text(),
        view.peak_value.text(),
    ) == calibration_metrics
    assert (original_output / "result.json").read_bytes() == original_bytes
    assert view.jobs[0].status == "complete"
    view.editor.fields["chain_notes"].setPlainText("Different microphone and input gain")
    assert not dialog.start_button.isEnabled()


def test_multi_stimulus_comparison_and_settings_guard(window, app, tmp_path):
    view, _ = window
    view._measure_reference()
    wait_until(app, lambda: view.worker is None)
    paths = []
    for index in range(2):
        path = tmp_path / f"compare-{index}.wav"
        sf.write(
            path, 0.4 * np.sin(2 * np.pi * (1000 + 500 * index) * np.arange(6400) / 32000), 32000
        )
        paths.append(path)
    view._import_sources(paths)
    wait_until(app, lambda: view.worker is None)
    for job in view.jobs:
        view._activate_job(job.id)
        view._adopt_manual_start()
    view._calibrate()
    wait_until(app, lambda: view.worker is None)
    view.queue_table.selectAll()
    assert view.compare_button.isEnabled()
    view.compare_button.click()
    wait_until(app, lambda: view.worker is None)
    dialog = view.comparison_dialog
    assert len(dialog.sources) == 2
    assert dialog.start_button.text() == "2件を再生・測定"
    for path in paths:
        path.unlink()
    dialog.start_button.click()
    wait_until(app, lambda: view.worker is None)
    assert dialog.history.rowCount() == 2
    assert [r["name"] for r in dialog.rows] == ["compare-0.wav", "compare-1.wav"]
    assert all(abs(r["delta_db"]) < 0.01 for r in dialog.rows)
    view.editor.fields["daq.max_peak_v"].setText("0.3")
    assert not dialog.start_button.isEnabled()
    assert not view.compare_button.isEnabled()
    view.editor.fields["daq.max_peak_v"].setText("0.5")
    assert dialog.start_button.isEnabled()
    view.mode.setCurrentIndex(1)
    assert not dialog.start_button.isEnabled()


def test_comparison_records_restore_from_history_without_audio(window, app, monkeypatch):
    view, _ = window
    calibrate_comparison_source(view, app)
    view._open_comparison()
    wait_until(app, lambda: view.worker is None)
    view.comparison_dialog.position.setText("B")
    view.comparison_dialog.start_button.click()
    wait_until(app, lambda: view.worker is None)
    directory = view.comparison_dialog.last_output
    view.comparison_dialog.close()
    view.editor.fields["daq.sample_rate_hz"].setText("48000")
    view.mode.setCurrentIndex(1)

    def unexpected_hardware(*args, **kwargs):
        pytest.fail("Restoring a comparison record must not play audio")

    factory = gui.make_hardware
    monkeypatch.setattr(gui, "make_hardware", unexpected_hardware)
    view._restore(directory)
    wait_until(app, lambda: view.worker is None)
    assert view.comparison_dialog.isVisible()
    assert view.simulate
    assert view.editor.setup().daq.sample_rate_hz == 32000
    assert view.comparison_dialog.start_button.isEnabled()
    assert (
        view.comparison_dialog.history.item(view.comparison_dialog.history.rowCount() - 1, 0).text()
        == "B"
    )
    assert view.comparison_dialog.current.text() != "—"
    monkeypatch.setattr(gui, "make_hardware", factory)
    view.comparison_dialog.position.setText("C")
    view.comparison_dialog.start_button.click()
    wait_until(app, lambda: view.worker is None)
    assert view.comparison_dialog.last_output != directory
    assert view.comparison_dialog.rows[-1]["position"] == "C"


@pytest.mark.parametrize("close_main", [False, True])
def test_comparison_close_during_playback_cancels_and_resets(window, app, monkeypatch, close_main):
    view, _ = window
    calibrate_comparison_source(view, app)
    view._open_comparison()
    wait_until(app, lambda: view.worker is None)
    entered, release = Event(), Event()

    class Blocking(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

        def play_and_record(self, output, **options):
            entered.set()
            assert release.wait(3)
            return super().play_and_record(output, **options)

    hardware = Blocking(view.editor.setup().daq)
    monkeypatch.setattr(gui, "make_hardware", lambda *a, **kw: hardware)
    dialog = view.comparison_dialog
    dialog.start_button.click()
    try:
        wait_until(app, entered.is_set)
        output = view.job_output
        assert not view.calibrate_button.isEnabled()
        assert not view.mode.isEnabled()
        assert dialog.stop_button.isEnabled()
        assert not view.volume_fader.isEnabled()
        view.language.setCurrentIndex(1)
        assert "fixed gain" in view.status.text()
        if close_main:
            view.close()
        else:
            dialog.reject()  # Includes the Escape-key path.
        assert view.worker.cancel_event.is_set()
    finally:
        release.set()
    wait_until(app, lambda: view.worker is None)
    assert hardware.resets == 2
    assert not dialog.isVisible()
    assert view.isVisible() is not close_main
    row = read_json(output / "summary.json")["rows"][0]
    assert row["status"] == "cancelled"
    assert row["delta_db"] is None


def test_calibration_history_opens_comparison_with_saved_settings(window, app):
    view, _ = window
    calibrate_comparison_source(view, app)
    view.editor.fields["daq.sample_rate_hz"].setText("48000")
    view.history.selectRow(0)
    view._compare_history()
    wait_until(app, lambda: view.worker is None)
    assert view.editor.setup().daq.sample_rate_hz == 32000
    assert view.comparison_dialog.isVisible()
    assert view.comparison_dialog.start_button.isEnabled()
