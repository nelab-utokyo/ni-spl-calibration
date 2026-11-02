import json
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from ni_spl_calibration import (
    CalibrationError,
    DaqSettings,
    MeasurementSettings,
    Setup,
    SimulatedHardware,
    Stimulus,
    apply_calibration,
    calibrate,
    from_generator,
    load_reference,
    load_result,
    load_stimulus,
    load_wav,
    measure_reference,
    save_stimulus,
)
from ni_spl_calibration.analysis import band_power, validate_capture
from ni_spl_calibration.hardware import output_guard
from ni_spl_calibration.meter import LiveMeter, input_metrics
from ni_spl_calibration.storage import read_json, write_json


@pytest.fixture
def setup():
    return Setup(
        "test-chain", "Synthetic microphone and speaker", daq=DaqSettings(sample_rate_hz=20000)
    )


def tone(fs=20000, duration=0.2):
    return 0.8 * np.sin(2 * np.pi * 2000 * np.arange(round(fs * duration)) / fs)


def reference(setup):
    return measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))


def test_reference_recovers_entire_chain_sensitivity(setup):
    result = reference(setup)
    assert result.sensitivity_v_per_pa == pytest.approx(0.1, rel=1e-5)
    assert result.estimated_frequency_hz == pytest.approx(1000)
    assert result.tone_power_fraction > 0.999


def test_reference_handles_small_offset_from_nominal_calibrator_frequency(setup):
    result = measure_reference(
        setup,
        hardware=SimulatedHardware(setup.daq, reference_mode=True, reference_frequency_hz=1000.37),
    )
    assert result.estimated_frequency_hz == pytest.approx(1000.37, abs=0.01)
    assert result.sensitivity_v_per_pa == pytest.approx(0.1, rel=0.001)


def test_search_interrupt_preserves_iterations_and_resets_output(setup):
    class Tracking(SimulatedHardware):
        resets = 0

        def reset_output(self):
            self.resets += 1

    hardware = Tracking(setup.daq)

    def interrupt(item):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as caught:
        calibrate(
            Stimulus(tone(), 20000),
            setup=setup,
            reference=reference(setup),
            target_spl_db=70,
            hardware=hardware,
            on_iteration=interrupt,
        )
    assert len(caught.value.iterations) == 1
    assert hardware.resets == 2


def test_phases_report_only_the_selected_playback_interval(setup):
    setup = replace(setup, measurement=replace(setup.measurement, stop_s=0.05))
    phases = []

    class CheckingHardware(SimulatedHardware):
        playback_started = False

        def acquire(self, count):
            if not self.playback_started:
                assert phases[-1]["phase"] == "background"
                assert phases[-1]["duration_s"] == count / self.settings.sample_rate_hz
            return super().acquire(count)

        def play_and_record(self, output):
            self.playback_started = True
            assert phases[-1]["phase"] == "playback"
            assert phases[-1]["duration_s"] == len(output) / self.settings.sample_rate_hz
            return super().play_and_record(output)

    result = calibrate(
        Stimulus(tone(), 20000),
        setup=setup,
        reference=reference(setup),
        target_spl_db=70,
        hardware=CheckingHardware(setup.daq),
        on_phase=phases.append,
    )
    assert phases[0]["duration_s"] == 0.05
    assert phases[1]["duration_s"] == pytest.approx(0.05005)
    assert (phases[1]["start_s"], phases[1]["stop_s"]) == (0, 0.05)
    assert len(phases) == len(result.iterations) + 1
    assert [item["iteration"] for item in phases[1:]] == [
        item["iteration"] for item in result.iterations
    ]


def test_reference_rejects_noise_wrong_frequency_clipping_and_drift(setup):
    with pytest.raises(RuntimeError):
        measure_reference(setup, hardware=SimulatedHardware(setup.daq))
    with pytest.raises(RuntimeError, match="frequency"):
        measure_reference(
            setup,
            hardware=SimulatedHardware(setup.daq, reference_mode=True, reference_frequency_hz=2000),
        )
    with pytest.raises(RuntimeError, match="clipping"):
        measure_reference(
            setup,
            hardware=SimulatedHardware(setup.daq, reference_mode=True, sensitivity_v_per_pa=10),
        )

    class Drifting(SimulatedHardware):
        def acquire(self, count):
            return super().acquire(count) * np.linspace(0.5, 1.5, count)

    with pytest.raises(RuntimeError, match="unstable"):
        measure_reference(setup, hardware=Drifting(setup.daq, reference_mode=True))


@pytest.mark.parametrize("modulated", [False, True])
@pytest.mark.parametrize("step_db", [6, 10])
def test_search_recovers_voltage_with_am_and_bounded_steps(setup, modulated, step_db):
    setup = replace(setup, measurement=replace(setup.measurement, max_upward_step_db=step_db))
    samples = tone()
    if modulated:
        samples *= (1 + np.sin(2 * np.pi * 20 * np.arange(len(samples)) / 20000)) / 2
    stimulus = Stimulus(samples, 20000)

    class Recording(SimulatedHardware):
        def __init__(self):
            super().__init__(setup.daq, noise_rms_v=0)
            self.outputs = []
            self.resets = 0

        def play_and_record(self, output):
            self.outputs.append(output.copy())
            return super().play_and_record(output)

        def reset_output(self):
            self.resets += 1

    hardware = Recording()
    # Use the same simulator identity as the reference (noise floor included).
    ref = measure_reference(
        setup, hardware=SimulatedHardware(setup.daq, reference_mode=True, noise_rms_v=0)
    )
    result = calibrate(stimulus, setup=setup, reference=ref, target_spl_db=70, hardware=hardware)
    expected = (20e-6 * 10 ** (70 / 20)) / np.sqrt(np.mean(samples**2))
    assert result.scale_v == pytest.approx(expected, rel=1e-8)
    assert result.measured_spl_db == pytest.approx(70, abs=1e-6)
    assert hardware.resets >= 2
    assert result.iterations[-1]["scale_v"] == result.iterations[-2]["scale_v"]
    upward_steps = [
        20 * np.log10(current["scale_v"] / previous["scale_v"])
        for previous, current in zip(result.iterations, result.iterations[1:])
    ]
    assert max(upward_steps) == pytest.approx(step_db)
    for output in hardware.outputs:
        assert np.max(np.abs(output)) <= setup.daq.max_peak_v
        assert output[-1] == 0
        assert output[:-1] / np.max(np.abs(output)) == pytest.approx(
            samples / np.max(np.abs(samples))
        )
    assert apply_calibration(stimulus, result) == pytest.approx(samples * result.scale_v)


def test_unreachable_target_stops_at_peak_limit_and_preserves_history(setup):
    with pytest.raises(CalibrationError, match="limit") as caught:
        calibrate(
            Stimulus(tone(), 20000),
            setup=setup,
            reference=reference(setup),
            target_spl_db=110,
            hardware=SimulatedHardware(setup.daq),
        )
    assert caught.value.iterations[-1]["output_peak_v"] == pytest.approx(0.5)
    assert all(x["output_peak_v"] <= 0.5 for x in caught.value.iterations)


def test_low_snr_is_not_reported_as_measured_spl(setup):
    hw = SimulatedHardware(setup.daq, noise_rms_v=0.2)
    ref = replace(reference(setup), hardware=hw.describe())
    with pytest.raises(CalibrationError) as caught:
        calibrate(
            Stimulus(tone(), 20000), setup=setup, reference=ref, target_spl_db=70, hardware=hw
        )
    assert all(x["measured_spl_db"] is None for x in caught.value.iterations)


def test_band_power_is_energy_mean_not_median_of_blocks():
    values = np.concatenate((np.zeros(3000), tone(duration=0.05)))
    settings = MeasurementSettings()
    assert band_power(values, 20000, settings) == pytest.approx(np.mean(values**2))
    settings = replace(settings, band_hz=(1000, 3000))
    time = np.arange(4000) / 20000
    samples = np.sin(2 * np.pi * 2000 * time) + 2 * np.sin(2 * np.pi * 6000 * time)
    assert band_power(samples, 20000, settings) == pytest.approx(0.5)
    corrected = replace(settings, frequency_correction_db=((1000, 6), (3000, 6)))
    assert band_power(samples, 20000, corrected) == pytest.approx(0.5 * 10**0.6)
    with pytest.raises(ValueError, match="cover"):
        band_power(
            samples, 20000, replace(settings, frequency_correction_db=((1500, 0), (2500, 0)))
        )


def test_audible_band_excludes_ultrasound_from_live_spl_and_calibration():
    rate = 200000
    setup = Setup(
        "band-test", "Synthetic known acoustic chain", daq=DaqSettings(sample_rate_hz=rate)
    )
    band_setup = replace(setup, measurement=replace(setup.measurement, band_hz=(20, 20000)))
    ref = measure_reference(
        setup, hardware=SimulatedHardware(setup.daq, reference_mode=True, noise_rms_v=0)
    )
    t = np.arange(rate // 5) / rate
    # The 50 kHz component has 100 times the power of the audible component.
    pressure = (
        np.sqrt(2)
        * 20e-6
        * (
            10 ** (30 / 20) * np.sin(2 * np.pi * 1000 * t)
            + 10 ** (50 / 20) * np.sin(2 * np.pi * 50000 * t)
        )
    )
    volts = pressure * ref.sensitivity_v_per_pa
    assert input_metrics(volts, setup, ref)["level_spl_db"] == pytest.approx(50.0432137378)
    assert input_metrics(volts, band_setup, ref)["level_spl_db"] == pytest.approx(30)
    frames = []
    LiveMeter(band_setup, ref, frames.append).push(volts)
    assert len(frames) == 2
    assert all(frame["level_spl_db"] == pytest.approx(30) for frame in frames)
    # Restricting SPL analysis must not filter the output waveform.
    samples = 0.01 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * 50000 * t)
    stimulus = Stimulus(samples, rate)
    band_setup = replace(band_setup, measurement=replace(band_setup.measurement, start_scale_v=1))
    result = calibrate(
        stimulus,
        setup=band_setup,
        reference=ref,
        target_spl_db=50,
        hardware=SimulatedHardware(setup.daq, noise_rms_v=0),
    )
    output = apply_calibration(stimulus, result)
    assert np.array_equal(output, samples * result.scale_v)
    captured = output * ref.sensitivity_v_per_pa
    assert input_metrics(captured, band_setup, ref)["level_spl_db"] == pytest.approx(50)
    assert input_metrics(captured, setup, ref)["level_spl_db"] == pytest.approx(70.0432137378)


def test_window_and_acoustic_delay_measure_only_selected_samples(setup):
    stimulus = Stimulus(np.concatenate((np.zeros(2000), tone(duration=0.1))), 20000)
    setup = replace(
        setup, measurement=MeasurementSettings(start_s=0.1, stop_s=0.2, acoustic_delay_s=0.005)
    )

    class Delayed(SimulatedHardware):
        def play_and_record(self, output):
            base = super().play_and_record(output)
            return np.concatenate((np.zeros(100), base[:-100]))

    hw = Delayed(setup.daq, noise_rms_v=0)
    ref = measure_reference(
        setup, hardware=SimulatedHardware(setup.daq, reference_mode=True, noise_rms_v=0)
    )
    result = calibrate(stimulus, setup=setup, reference=ref, target_spl_db=70, hardware=hw)
    assert result.scale_v == pytest.approx(
        (20e-6 * 10**3.5) / np.sqrt(np.mean(tone() ** 2)), rel=1e-8
    )


@pytest.mark.parametrize("start,stop", [(0, 0.05), (0.05, 0.15), (0.1, None)])
@pytest.mark.parametrize("delay_s", [0, 0.005])
def test_only_selected_samples_are_played_and_reused(setup, start, stop, delay_s):
    setup = replace(
        setup,
        measurement=MeasurementSettings(
            start_s=start, stop_s=stop, acoustic_delay_s=delay_s, start_scale_v=3
        ),
    )
    fs = setup.daq.sample_rate_hz
    samples = tone()
    first, last = round(start * fs), len(samples) if stop is None else round(stop * fs)
    samples[first:last] *= 0.25  # Larger unplayed samples must not limit the selected output.
    stimulus = Stimulus(samples, fs)
    selected = samples[first:last]
    delay = round(delay_s * fs)

    class RecordingHardware(SimulatedHardware):
        def __init__(self):
            super().__init__(setup.daq, noise_rms_v=0)
            self.outputs = []
            self.acquired_counts = []

        def acquire(self, count):
            self.acquired_counts.append(count)
            return super().acquire(count)

        def play_and_record(self, output):
            self.outputs.append(output.copy())
            captured = super().play_and_record(output)
            return np.concatenate((np.zeros(delay), captured[: len(captured) - delay]))

    hardware = RecordingHardware()
    ref = measure_reference(
        setup, hardware=SimulatedHardware(setup.daq, reference_mode=True, noise_rms_v=0)
    )
    result = calibrate(stimulus, setup=setup, reference=ref, target_spl_db=70, hardware=hardware)
    assert hardware.acquired_counts[0] == len(selected)
    assert result.iterations[0]["output_peak_v"] == pytest.approx(setup.daq.max_peak_v)
    for output, iteration in zip(hardware.outputs, result.iterations, strict=True):
        assert len(output) == len(selected) + delay + 1
        np.testing.assert_array_equal(output[: len(selected)], selected * iteration["scale_v"])
        np.testing.assert_array_equal(output[len(selected) :], np.zeros(delay + 1))
    expected = selected * result.scale_v
    assert result.measured_spl_db == pytest.approx(70, abs=1e-6)
    assert result.output_peak_v == pytest.approx(np.max(np.abs(expected)))
    assert result.output_rms_v == pytest.approx(np.sqrt(np.mean(expected**2)))
    np.testing.assert_array_equal(apply_calibration(stimulus, result), expected)
    np.testing.assert_array_equal(stimulus.samples, samples)


@pytest.mark.parametrize("constant", [0, 0.25])
def test_silent_selected_interval_is_rejected_before_hardware_access(setup, monkeypatch, constant):
    from ni_spl_calibration import calibration

    setup = replace(setup, measurement=MeasurementSettings(stop_s=0.1))
    stimulus = Stimulus(np.concatenate((np.full(2000, constant), tone(duration=0.1))), 20000)
    ref = reference(setup)
    monkeypatch.setattr(
        calibration, "NIDaqHardware", lambda *_: pytest.fail("Silent interval must not access DAQ")
    )
    with pytest.raises(ValueError, match="silent or constant"):
        calibrate(stimulus, setup=setup, reference=ref, target_spl_db=70)


def test_stale_reference_backend_and_rate_fail_before_capture(setup):
    ref = reference(setup)
    stimulus = Stimulus(tone(), 20000)
    with pytest.raises(ValueError, match="different setup"):
        calibrate(
            stimulus,
            setup=replace(setup, chain_notes="Changed amplifier gain"),
            reference=ref,
            target_spl_db=70,
            hardware=SimulatedHardware(setup.daq),
        )
    with pytest.raises(ValueError, match="backend"):
        calibrate(
            stimulus,
            setup=setup,
            reference=replace(ref, hardware={"backend": "ni-daqmx"}),
            target_spl_db=70,
            hardware=SimulatedHardware(setup.daq),
        )
    with pytest.raises(ValueError, match="sample rates"):
        calibrate(Stimulus(tone(), 40000), setup=setup, reference=ref, target_spl_db=70)
    with pytest.raises(ValueError, match="outside"):
        calibrate(
            stimulus,
            setup=replace(setup, measurement=MeasurementSettings(stop_s=2)),
            reference=ref,
            target_spl_db=70,
        )


def test_clipping_checks_asymmetric_input_rails():
    daq = DaqSettings(input_min_v=-1, input_max_v=10)
    with pytest.raises(RuntimeError, match="clipping"):
        validate_capture(np.array([-0.99, 0, 0, 0]), 4, daq, 0.98)


def test_interrupt_and_failed_reset_preserve_primary_error():
    class Hardware:
        count = 0

        def reset_output(self):
            self.count += 1
            if self.count == 2:
                raise RuntimeError("Device unplugged")

    hardware = Hardware()
    with pytest.raises(KeyboardInterrupt) as caught:
        with output_guard(hardware):
            raise KeyboardInterrupt
    assert hardware.count == 2
    assert "AO reset also failed" in caught.value.__notes__[0]


def test_wav_generator_and_saved_samples_preserve_exact_waveform(tmp_path):
    calls = []

    def generator(*, sample_rate_hz, seed):
        calls.append(seed)
        return np.random.default_rng(seed).uniform(-0.5, 0.5, 4000)

    stimulus = from_generator(generator, sample_rate_hz=20000, seed=7)
    assert calls == [7]
    assert stimulus.samples.flags.writeable is False
    save_stimulus(stimulus, tmp_path / "stimulus.npz")
    loaded = load_stimulus(tmp_path / "stimulus.npz")
    assert loaded.sha256 == stimulus.sha256
    sf.write(tmp_path / "stimulus.wav", stimulus.samples, 20000, subtype="DOUBLE")
    assert load_wav(tmp_path / "stimulus.wav").sha256 == stimulus.sha256
    sf.write(tmp_path / "stereo.wav", np.column_stack((stimulus.samples, stimulus.samples)), 20000)
    with pytest.raises(ValueError, match="mono"):
        load_wav(tmp_path / "stereo.wav")
    with pytest.raises(FileExistsError):
        save_stimulus(stimulus, tmp_path / "stimulus.npz")


@pytest.mark.parametrize(
    "samples",
    [
        np.zeros(20),
        np.ones(20),
        np.full(20, np.nan),
        np.full(20, np.inf),
        tone() * 2,
        np.zeros((20, 2)),
    ],
)
def test_invalid_stimuli_are_rejected(samples):
    with pytest.raises(ValueError):
        Stimulus(samples, 20000)


def test_records_round_trip_and_waveform_changes_invalidate_result(setup, tmp_path):
    stimulus = Stimulus(tone(), 20000)
    ref = reference(setup)
    result = calibrate(
        stimulus,
        setup=setup,
        reference=ref,
        target_spl_db=70,
        hardware=SimulatedHardware(setup.daq),
    )
    write_json(tmp_path / "reference.json", ref)
    write_json(tmp_path / "result.json", result)
    assert load_reference(tmp_path / "reference.json") == ref
    assert load_result(tmp_path / "result.json") == result
    with pytest.raises(ValueError, match="changed"):
        apply_calibration(Stimulus(tone() * 0.5, 20000), result)
    with pytest.raises(FileExistsError):
        write_json(tmp_path / "result.json", {"overwrite": True})
    with pytest.raises(ValueError):
        write_json(tmp_path / "invalid.json", {"x": float("nan")})
    assert not (tmp_path / "invalid.json").exists()
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "complete"
    (tmp_path / "nan.json").write_text('{"x": NaN}')
    with pytest.raises(ValueError):
        read_json(tmp_path / "nan.json")
