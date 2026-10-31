import json
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

from ni_spl_calibration import apply_calibration, load_result, load_stimulus, load_wav
from ni_spl_calibration.cli import main


def test_cli_simulated_reference_and_generator_then_reuse_as_wav(tmp_path, capsys):
    setup = tmp_path / "setup.yaml"
    reference = tmp_path / "reference.json"
    assert main(["init", str(setup)]) == 0
    assert main(["reference", str(setup), "--output", str(reference), "--simulate", "--yes"]) == 0
    generator = Path(__file__).resolve().parents[1] / "examples" / "custom_sound.py"
    output = tmp_path / "generated"
    assert (
        main(
            [
                "calibrate",
                str(setup),
                "--reference",
                str(reference),
                "--target-spl-db",
                "70",
                "--generator",
                f"{generator}:make_sound",
                "--output",
                str(output),
                "--simulate",
                "--yes",
            ]
        )
        == 0
    )
    record = json.loads((output / "result.json").read_text())
    assert record["hardware"]["backend"] == "simulation"
    assert record["measured_spl_db"] > 69.5
    stimulus = load_stimulus(output / "stimulus.npz")
    assert stimulus.sha256 == record["stimulus"]["sha256"]
    wav = tmp_path / "input.wav"
    sf.write(wav, stimulus.samples, stimulus.sample_rate_hz, subtype="DOUBLE")
    assert main(["inspect", "--wav", str(wav)]) == 0
    wav_output = tmp_path / "wav-result"
    assert (
        main(
            [
                "calibrate",
                str(setup),
                "--reference",
                str(reference),
                "--target-spl-db",
                "70",
                "--wav",
                str(wav),
                "--output",
                str(wav_output),
                "--simulate",
                "--yes",
            ]
        )
        == 0
    )
    assert json.loads((wav_output / "result.json").read_text())["scale_v"] == record["scale_v"]
    assert main(["reference", str(setup), "--output", str(reference), "--simulate", "--yes"]) == 2


def test_failed_search_saves_history_and_never_creates_success_record(tmp_path):
    setup = tmp_path / "setup.yaml"
    reference = tmp_path / "reference.json"
    assert main(["init", str(setup)]) == 0
    assert main(["reference", str(setup), "--output", str(reference), "--simulate", "--yes"]) == 0
    generator = Path(__file__).resolve().parents[1] / "examples" / "custom_sound.py"
    output = tmp_path / "failed"
    assert (
        main(
            [
                "calibrate",
                str(setup),
                "--reference",
                str(reference),
                "--target-spl-db",
                "150",
                "--generator",
                f"{generator}:make_sound",
                "--output",
                str(output),
                "--simulate",
                "--yes",
            ]
        )
        == 2
    )
    assert not (output / "result.json").exists()
    failure = json.loads((output / "failure.json").read_text())
    assert failure["status"] == "failed"
    assert failure["iterations"]
    assert (output / "stimulus.npz").is_file()


def test_setup_rate_controls_inspection_conversion_and_calibration(tmp_path, capsys):
    setup = tmp_path / "setup.yaml"
    assert main(["init", str(setup)]) == 0
    payload = yaml.safe_load(setup.read_text())
    payload["daq"]["sample_rate_hz"] = 32000
    setup.write_text(yaml.safe_dump(payload))
    generator = Path(__file__).resolve().parents[1] / "examples" / "custom_sound.py"
    capsys.readouterr()
    assert main(["inspect", "--setup", str(setup), "--generator", f"{generator}:make_sound"]) == 0
    assert json.loads(capsys.readouterr().out)["sample_rate_hz"] == 32000

    wav = tmp_path / "source.wav"
    sf.write(wav, 0.4 * np.sin(2 * np.pi * 2000 * np.arange(8820) / 44100), 44100)
    assert main(["inspect", "--wav", str(wav)]) == 0
    assert json.loads(capsys.readouterr().out)["sample_rate_hz"] == 44100
    assert main(["inspect", "--setup", str(setup), "--wav", str(wav)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["sample_rate_hz"] == 32000
    assert main(["inspect", "--sample-rate-hz", "32000", "--wav", str(wav)]) == 0
    assert json.loads(capsys.readouterr().out) == preview

    reference = tmp_path / "reference.json"
    assert main(["reference", str(setup), "--output", str(reference), "--simulate", "--yes"]) == 0
    output = tmp_path / "converted"
    assert (
        main(
            [
                "calibrate",
                str(setup),
                "--reference",
                str(reference),
                "--wav",
                str(wav),
                "--target-spl-db",
                "70",
                "--output",
                str(output),
                "--simulate",
                "--yes",
            ]
        )
        == 0
    )
    assert "WAV resampled: 44100 -> 32000 Hz" in capsys.readouterr().out
    saved = load_stimulus(output / "stimulus.npz")
    result = load_result(output / "result.json")
    assert result.stimulus == preview == saved.describe()
    assert result.setup["daq"]["sample_rate_hz"] == saved.sample_rate_hz == 32000
    assert 69.5 < result.measured_spl_db < 70.5
    assert np.array_equal(apply_calibration(saved, result), saved.samples * result.scale_v)
    assert "resampling" not in load_wav(wav).provenance
    assert "scipy" in result.hardware["runtime"]

    assert main(["inspect", "--setup", str(setup), "--npz", str(output / "stimulus.npz")]) == 0
    assert json.loads(capsys.readouterr().out) == preview
    payload["daq"]["sample_rate_hz"] = 20000
    setup.write_text(yaml.safe_dump(payload))
    assert main(["inspect", "--setup", str(setup), "--npz", str(output / "stimulus.npz")]) == 2
    assert "NPZ is never resampled" in capsys.readouterr().err


def test_generator_parameters_cannot_override_shared_rate(tmp_path, capsys):
    parameters = tmp_path / "parameters.yaml"
    parameters.write_text("sample_rate_hz: 44100\n")
    assert (
        main(
            [
                "inspect",
                "--generator",
                "unused:make_sound",
                "--parameters",
                str(parameters),
            ]
        )
        == 2
    )
    assert "Set sample_rate_hz in setup.daq" in capsys.readouterr().err
