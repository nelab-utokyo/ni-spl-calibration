import json
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from ni_spl_calibration import DaqSettings, Setup, Stimulus, load_wav
from ni_spl_calibration.playback import (
    VolumeControl,
    make_test_tone,
    run_test_playback,
    voltage_blocks,
)
from ni_spl_calibration.records import file_sha256
from ni_spl_calibration.spectrum import spectrum_bands
from ni_spl_calibration.storage import read_json


@pytest.mark.parametrize("rate", [44100, 48000, 192000])
@pytest.mark.parametrize("frequency", [1000, 10000])
def test_audio_rate_conversion_does_not_attenuate_in_band_music_tones(tmp_path, rate, frequency):
    samples = 0.8 * np.sin(2 * np.pi * frequency * np.arange(rate // 5) / rate)
    path = tmp_path / "tone.wav"
    sf.write(path, samples, rate, subtype="DOUBLE")
    converted = load_wav(path, sample_rate_hz=200000)
    source = samples[round(rate * 0.02) : -round(rate * 0.02)]
    target = converted.samples[4000:-4000]
    gain_db = 20 * np.log10(np.sqrt(np.mean(target**2)) / np.sqrt(np.mean(source**2)))
    assert abs(gain_db) < 0.03
    assert converted.provenance["resampling"]["source_digital_peak"] == pytest.approx(
        np.max(np.abs(samples))
    )
    assert converted.provenance["resampling"]["source_digital_rms"] == pytest.approx(
        np.sqrt(np.mean(samples**2))
    )


def test_stereo_selection_is_explicit_and_downmix_is_preserved(tmp_path):
    rate = 44100
    time = np.arange(4410) / rate
    left = 0.6 * np.sin(2 * np.pi * 1000 * time)
    right = 0.2 * np.sin(2 * np.pi * 3000 * time)
    path = tmp_path / "stereo.wav"
    sf.write(path, np.column_stack((left, right)), rate, subtype="DOUBLE")
    with pytest.raises(ValueError, match="mono"):
        load_wav(path)
    assert np.array_equal(load_wav(path, channel_mode="left").samples, left)
    assert np.array_equal(load_wav(path, channel_mode="right").samples, right)
    mixed = load_wav(path, channel_mode="mix")
    assert np.array_equal(mixed.samples, (left + right) / 2)
    assert mixed.provenance["source_channels"] == 2
    assert mixed.provenance["channel_mode"] == "mix"
    # Anti-phase stereo genuinely cancels under averaging, rather than an SR defect.
    sf.write(path, np.column_stack((left, -left)), rate, subtype="DOUBLE")
    with pytest.raises(ValueError, match="Silent"):
        load_wav(path, channel_mode="mix")
    assert np.max(np.abs(load_wav(path, channel_mode="left").samples)) > 0.5


def test_spectrum_reports_rms_voltage_per_log_band():
    rate = 200000
    values = 0.4 * np.sin(2 * np.pi * 1000 * np.arange(20000) / rate)
    spectrum = spectrum_bands(values + 3, rate)
    assert spectrum["edges_hz"][-1] == rate / 2
    band = int(np.argmax(spectrum["levels_dbv"]))
    assert spectrum["edges_hz"][band] < 1000 < spectrum["edges_hz"][band + 1]
    power = np.sum(10 ** (spectrum["levels_dbv"] / 10))
    assert power == pytest.approx(0.4**2 / 2, rel=0.001)
    assert np.max(spectrum_bands(np.full(2000, 5), 20000)["levels_dbv"]) < -200


def test_volume_blocks_ramp_and_never_exceed_peak_cap():
    stimulus = Stimulus(make_test_tone(sample_rate_hz=20000), 20000)
    volume = VolumeControl(0.25)
    blocks = voltage_blocks(stimulus, max_peak_v=0.5, block_size=1000, volume=volume, loop=True)
    first = next(blocks)
    assert first["output_peak_v"] == pytest.approx(0.125, abs=1e-6)
    volume.set(1)
    loud = next(blocks)
    assert loud["output_peak_v"] <= 0.5
    assert loud["scale_start_v"] < loud["scale_end_v"]
    volume.set(0)
    fading = next(blocks)
    muted = next(blocks)
    assert fading["scale_start_v"] > fading["scale_end_v"] == 0
    assert not np.any(muted["output"])
    with pytest.raises(ValueError):
        volume.set(1.01)


@pytest.mark.parametrize("loop", [True, False])
def test_playback_records_ao_ai_and_applied_volume_without_reference(tmp_path, loop):
    setup = Setup("test-playback", "Fixed microphone chain", daq=DaqSettings(sample_rate_hz=20000))
    tone = make_test_tone(sample_rate_hz=20000)[:2050]
    stimulus = Stimulus(tone, 20000)
    control = VolumeControl(0.1)
    stop = Event()
    frames = []

    def update(item):
        if item["kind"] != "test_playback":
            return
        frames.append(item)
        if len(frames) == 1:
            control.set(0.6)
        if loop and len(frames) == 4:
            stop.set()

    directory = tmp_path / "playback"
    summary = run_test_playback(
        stimulus,
        setup=setup,
        output=directory,
        simulate=True,
        volume=control,
        loop=loop,
        cancel_requested=stop.is_set,
        on_frame=update,
    )
    assert summary["status"] == ("stopped" if loop else "complete")
    assert summary["duration_s"] <= 0.2
    assert frames[0]["volume_fraction"] == 0.1
    assert any(frame["volume_fraction"] == 0.6 for frame in frames[1:])
    assert all(frame["output_peak_v"] <= 0.5 for frame in frames)
    assert all(frame["level_spl_db"] is None for frame in frames)
    log = [json.loads(line) for line in (directory / "levels.jsonl").read_text().splitlines()]
    assert len(log) == len(frames)
    for row in log:
        capture = directory / row["capture_file"]
        assert file_sha256(capture) == row["capture_sha256"]
        with np.load(capture, allow_pickle=False) as data:
            assert np.max(np.abs(data["output_v"])) <= 0.5
            assert data["samples"].shape == data["output_v"].shape
    if not loop:
        with np.load(directory / log[-1]["capture_file"], allow_pickle=False) as data:
            assert data["output_v"][-1] == 0
    metadata = read_json(directory / "playback.json")
    assert metadata["stimulus"]["sha256"] == stimulus.sha256
    assert read_json(directory / "summary.json")["levels_sha256"] == file_sha256(
        directory / "levels.jsonl"
    )
