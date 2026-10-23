import json
from pathlib import Path

import soundfile as sf

from ni_spl_calibration import load_stimulus
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
