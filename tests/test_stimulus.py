import math

import numpy as np
import pytest
import soundfile as sf

from ni_spl_calibration import load_stimulus, load_wav, save_stimulus


@pytest.mark.parametrize("source_rate,target_rate", [(44100, 200000), (48000, 16000)])
def test_wav_conversion_preserves_timing_tone_and_saved_samples(tmp_path, source_rate, target_rate):
    count = round(source_rate * 0.1) + 1
    samples = 0.4 * np.sin(2 * np.pi * 2000 * np.arange(count) / source_rate)
    path = tmp_path / "tone.wav"
    sf.write(path, samples, source_rate, subtype="DOUBLE")

    original = load_wav(path)
    assert np.array_equal(original.samples, samples)
    assert load_wav(path, sample_rate_hz=source_rate).sha256 == original.sha256
    converted = load_wav(path, sample_rate_hz=target_rate)
    assert converted.sample_rate_hz == target_rate
    assert len(converted.samples) == math.ceil(count * target_rate / source_rate)
    assert abs(len(converted.samples) / target_rate - count / source_rate) < 1 / target_rate
    expected = 0.4 * np.sin(2 * np.pi * 2000 * np.arange(len(converted.samples)) / target_rate)
    assert converted.samples[100:-100] == pytest.approx(expected[100:-100], abs=0.002)
    assert converted.provenance["file_sha256"] == original.provenance["file_sha256"]
    conversion = converted.provenance["resampling"]
    assert conversion["source_sample_rate_hz"] == source_rate
    assert conversion["source_sample_count"] == count
    assert conversion["target_sample_rate_hz"] == target_rate

    save_stimulus(converted, tmp_path / "stimulus.npz")
    loaded = load_stimulus(tmp_path / "stimulus.npz")
    assert loaded.sha256 == converted.sha256
    assert loaded.provenance == converted.provenance


def test_wav_downsampling_suppresses_aliasing(tmp_path):
    source_rate = 48000
    samples = 0.4 * np.sin(2 * np.pi * 12000 * np.arange(4800) / source_rate)
    path = tmp_path / "above-target-nyquist.wav"
    sf.write(path, samples, source_rate, subtype="DOUBLE")
    converted = load_wav(path, sample_rate_hz=16000)
    assert np.sqrt(np.mean(converted.samples[100:-100] ** 2)) < 0.001


def test_wav_conversion_rejects_overshoot_without_normalizing(tmp_path):
    path = tmp_path / "full-scale-step.wav"
    sf.write(path, np.repeat([-1.0, 1.0], 100), 20000, subtype="DOUBLE")
    with pytest.raises(ValueError, match="Resampled WAV exceeds"):
        load_wav(path, sample_rate_hz=40000)


@pytest.mark.parametrize("rate", [True, 0, 1, 44100.5])
def test_wav_target_rate_is_validated_before_loading(tmp_path, rate):
    with pytest.raises(ValueError, match="sample_rate_hz"):
        load_wav(tmp_path / "not-read.wav", sample_rate_hz=rate)
