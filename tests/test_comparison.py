import csv
import json
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from ni_spl_calibration import (
    DaqSettings,
    MeasurementSettings,
    Setup,
    SimulatedHardware,
    measure_reference,
)
from ni_spl_calibration.calibration import apply_calibration
from ni_spl_calibration.comparison import load_comparison, load_comparison_source, run_comparison
from ni_spl_calibration.records import file_sha256
from ni_spl_calibration.stimulus import Stimulus
from ni_spl_calibration.storage import read_json
from ni_spl_calibration.workflow import calibrate_to_directory


class PositionedHardware(SimulatedHardware):
    """Position changes affect the response, with the same DAQ/microphone identity."""

    def __init__(self, settings, *, response=0.5, delay=0):
        super().__init__(settings)
        self.response, self.delay = response, delay
        self.outputs = []
        self.resets = 0

    def reset_output(self):
        self.resets += 1

    def play_and_record(self, output_v, *, cancel_requested=None, on_chunk=None):
        self.outputs.append(output_v.copy())
        signal = output_v * self.sensitivity_v_per_pa * self.response
        if self.delay:
            signal = np.concatenate((np.zeros(self.delay), signal[: -self.delay]))
        values = signal + self.acquire(len(output_v))
        return self._emit_chunks(values, cancel_requested, on_chunk)


@pytest.fixture
def source(tmp_path):
    setup = Setup(
        "comparison",
        "fixed input chain; calibration position A",
        daq=DaqSettings(sample_rate_hz=8000),
        measurement=MeasurementSettings(
            start_scale_v=0.2, start_s=0.02, stop_s=0.18, band_hz=(600, 2500)
        ),
    )
    reference = measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
    stimulus = Stimulus(0.4 * np.sin(2 * np.pi * 1000 * np.arange(3200) / 8000), 8000)
    output = tmp_path / "original"
    calibrate_to_directory(
        stimulus,
        setup=setup,
        reference=reference,
        target_spl_db=69.3,
        output=output,
        hardware=SimulatedHardware(setup.daq),
    )
    return load_comparison_source(output, name="tone.wav", group="Tones")


@pytest.mark.parametrize("response", [0.5, 2.0])
def test_fixed_gain_selected_interval_and_delta_from_actual_baseline(tmp_path, source, response):
    original_hash = file_sha256(source.replay.directory / "result.json")
    hardware = PositionedHardware(source.replay.setup.daq, response=response)
    result = run_comparison(
        tmp_path / "comparison",
        [source],
        position="20 cm",
        hardware_factory=lambda *a, **kw: hardware,
    )
    assert result["status"] == "complete"
    row = result["rows"][0]
    assert row["status"] == "complete"
    assert row["position"] == "20 cm"
    assert row["scale_v"] == source.result.scale_v
    assert row["baseline_spl_db"] != pytest.approx(source.result.target_spl_db, abs=0.1)
    assert row["delta_db"] == pytest.approx(20 * np.log10(response), abs=0.002)
    assert row["delta_db"] == row["measured_spl_db"] - source.result.measured_spl_db
    assert len(hardware.outputs) == 1
    expected = np.append(apply_calibration(source.replay.stimulus, source.result), 0)
    np.testing.assert_array_equal(hardware.outputs[0], expected)
    assert hardware.resets == 2
    assert file_sha256(source.replay.directory / "result.json") == original_hash
    assert not list((tmp_path / "comparison").glob("*-input.npz"))
    assert (
        "20 cm"
        not in read_json(tmp_path / "comparison/calibrations" / source.id / "setup.json")[
            "chain_notes"
        ]
    )
    with (tmp_path / "comparison/results.csv").open(encoding="utf-8-sig", newline="") as handle:
        csv_row = list(csv.DictReader(handle))[0]
        assert float(csv_row["delta_db"]) == row["delta_db"]


def test_delay_and_band_match_calibration_and_optional_raw_recording(tmp_path, source):
    setup = replace(
        source.replay.setup,
        measurement=replace(source.replay.setup.measurement, acoustic_delay_s=0.01),
    )
    original = tmp_path / "delayed"
    calibrate_to_directory(
        source.replay.stimulus,
        setup=setup,
        reference=source.replay.reference,
        target_spl_db=69.3,
        output=original,
        hardware=PositionedHardware(setup.daq, response=1, delay=80),
    )
    delayed = load_comparison_source(original)

    class OutOfBand(PositionedHardware):
        def play_and_record(self, output_v, **options):
            values = super().play_and_record(output_v, **options)
            # An out-of-band signal must not affect the final comparison SPL.
            return values + 0.2 * np.sin(2 * np.pi * 3000 * np.arange(len(values)) / 8000)

    hardware = OutOfBand(setup.daq, response=0.5, delay=80)
    output = tmp_path / "measurement"
    result = run_comparison(
        output, [delayed], save_capture=True, hardware_factory=lambda *a, **kw: hardware
    )
    assert result["rows"][0]["delta_db"] == pytest.approx(-6.0206, abs=0.002)
    assert len(hardware.outputs[0]) == 1280 + 80 + 1
    assert np.all(hardware.outputs[0][-81:] == 0)
    with np.load(next(output.glob("*-input.npz"))) as raw:
        assert raw["sample_rate_hz"] == 8000
        np.testing.assert_array_equal(raw["output_v"], hardware.outputs[0])


def test_snr_failure_never_increases_gain_or_returns_an_apparent_difference(tmp_path, source):
    class NoSignal(PositionedHardware):
        def acquire(self, count, **options):
            return 0.01 * np.sin(2 * np.pi * 1000 * np.arange(count) / 8000)

    hardware = NoSignal(source.replay.setup.daq, response=0)
    result = run_comparison(
        tmp_path / "low-snr", [source], hardware_factory=lambda *a, **kw: hardware
    )
    row = result["rows"][0]
    assert row["status"] == "insufficient_snr"
    assert row["measured_spl_db"] is row["delta_db"] is None
    assert row["background_rms_v"] > 0
    assert len(hardware.outputs) == 1
    assert max(abs(hardware.outputs[0])) == pytest.approx(source.result.output_peak_v)


@pytest.mark.parametrize("background", [True, False])
def test_clipping_is_invalid_and_holds_remaining_stimuli(tmp_path, source, background):
    class Clipped(PositionedHardware):
        def acquire(self, count, **options):
            if background:
                return np.full(count, 10.0)
            return super().acquire(count, **options)

        def play_and_record(self, output_v, **options):
            self.outputs.append(output_v.copy())
            return np.full(len(output_v), 10.0)

    hardware = Clipped(source.replay.setup.daq)
    result = run_comparison(
        tmp_path / "clipped",
        [source, replace(source, id="a" * 32)],
        hardware_factory=lambda *a, **kw: hardware,
    )
    assert result["status"] == "failed"
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["status"] == "clipped"
    assert row["measured_spl_db"] is row["delta_db"] is None
    assert len(hardware.outputs) == (0 if background else 1)
    assert hardware.resets == 2


def test_stop_records_cancelled_attempt_and_does_not_start_remaining(tmp_path, source):
    stop = Event()
    hardware = PositionedHardware(source.replay.setup.daq)

    def progress(item):
        if item.get("kind") == "comparison_phase" and item["phase"] == "playback":
            stop.set()

    result = run_comparison(
        tmp_path / "cancelled",
        [source, replace(source, id="a" * 32)],
        cancel_requested=stop.is_set,
        on_progress=progress,
        hardware_factory=lambda *a, **kw: hardware,
    )
    assert result["status"] == "cancelled"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["status"] == "cancelled"
    assert result["rows"][0]["delta_db"] is None
    assert hardware.resets == 2
    assert load_comparison(tmp_path / "cancelled").rows[0]["status"] == "cancelled"


def test_reset_failure_does_not_publish_a_valid_measurement(tmp_path, source):
    class ResetFailure(PositionedHardware):
        def reset_output(self):
            super().reset_output()
            if self.resets == 2:
                raise RuntimeError("Reset failed")

    hardware = ResetFailure(source.replay.setup.daq)
    row = run_comparison(
        tmp_path / "reset-error", [source], hardware_factory=lambda *a, **kw: hardware
    )["rows"][0]
    assert row["status"] == "failed"
    assert row["measured_spl_db"] is row["delta_db"] is None


def test_portable_multi_source_restore_can_repeat_without_original_calibration(tmp_path, source):
    other = replace(source, id="a" * 32, name="second.wav")
    output = tmp_path / "comparison"
    result = run_comparison(output, [source, other], position="B")
    source.replay.directory.rename(tmp_path / "original-moved-away")
    relocated = tmp_path / "relocated"
    output.rename(relocated)
    restored = load_comparison(relocated)
    assert [s.id for s in restored.sources] == [source.id, other.id]
    assert [row["source_id"] for row in restored.rows] == [source.id, other.id]
    assert restored.rows == result["rows"]
    assert all(s.replay.directory.is_relative_to(relocated) for s in restored.sources)
    repeated = run_comparison(tmp_path / "repeated", restored.sources, position="C")
    assert [row["position"] for row in repeated["rows"]] == ["C", "C"]
    assert result["rows"][0]["position"] == "B"
    with pytest.raises(FileExistsError):
        run_comparison(relocated, restored.sources)


@pytest.mark.parametrize(
    "file", ["comparison.json", "summary.json", "results.csv", "measurement", "baseline"]
)
def test_comparison_integrity_checks(tmp_path, source, file):
    output = tmp_path / "comparison"
    run_comparison(output, [source])
    path = output / file
    if file == "measurement":
        path = output / "measurements" / f"{source.id}.json"
    if file == "baseline":
        path = output / "calibrations" / source.id / "result.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="integrity"):
        load_comparison(output)


def test_changed_input_chain_rejected_before_hardware_access(tmp_path, source):
    wrong = replace(
        source,
        id="a" * 32,
        replay=replace(
            source.replay,
            setup=replace(
                source.replay.setup, daq=replace(source.replay.setup.daq, max_peak_v=0.2)
            ),
        ),
    )
    calls = []
    with pytest.raises(ValueError, match="input chain"):
        run_comparison(
            tmp_path / "bad", [source, wrong], hardware_factory=lambda *a, **kw: calls.append(True)
        )
    assert calls == []
    assert not (tmp_path / "bad").exists()


def test_short_stimulus_also_updates_the_input_spectrum(tmp_path, source):
    setup = replace(
        source.replay.setup, measurement=replace(source.replay.setup.measurement, stop_s=0.04)
    )
    path = tmp_path / "short-calibration"
    calibrate_to_directory(
        source.replay.stimulus,
        setup=setup,
        reference=source.replay.reference,
        target_spl_db=69.3,
        output=path,
        hardware=SimulatedHardware(setup.daq),
    )
    events = []
    run_comparison(
        tmp_path / "short-comparison", [load_comparison_source(path)], on_progress=events.append
    )
    frames = [event for event in events if event.get("kind") == "monitor"]
    assert len(frames) == 2  # Background and playback are displayed separately.
    assert all(frame["spectrum"]["levels_dbv"] for frame in frames)
    assert len(frames[-1]["samples"]) == 161


def test_windows_comparison_manifests_remain_portable(tmp_path, source):
    output = tmp_path / "comparison"
    run_comparison(output, [source], position="Windows record")
    manifest = output / "comparison.json"
    payload = read_json(manifest)
    assert all("\\" not in name for name in payload["files"])
    # Reproduce the older Windows writer's path spelling and valid checksums.
    payload["files"] = {
        name.replace("/", "\\"): digest for name, digest in payload["files"].items()
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (output / "input-checksums.json").write_text(
        json.dumps({"comparison.json": file_sha256(manifest)}), encoding="utf-8"
    )
    seal = read_json(output / "checksums.json")
    seal["files"] = {name.replace("/", "\\"): file_sha256(output / name) for name in seal["files"]}
    (output / "checksums.json").write_text(json.dumps(seal), encoding="utf-8")
    restored = load_comparison(output)
    assert restored.rows[0]["position"] == "Windows record"
    assert restored.rows[0]["status"] == "complete"
