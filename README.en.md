# NI SPL Calibration

[日本語](README.md) | [English](README.en.md)

Find the **voltage scale needed to play a particular waveform and interval at a target SPL**, starting from WAV files, generators or NumPy arrays. An acoustic calibrator establishes microphone-chain sensitivity; NI DAQ playback and acquisition then determine the scale. The package supports finite mono output and one AI/AO pair on the same DAQ.

## Installation

Use Git, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/kmraven/ni-spl-calibration.git
cd ni-spl-calibration
uv sync --locked --extra gui --extra daq
uv run ni-spl
```

Physical hardware also requires the [NI-DAQmx driver](https://nidaqmx-python.readthedocs.io/en/stable/) appropriate for the device and OS. The hardware backend targets driver-supported Windows/Linux environments.

## Use the result in an experiment

Load `stimulus.npz` and `result.json` from a successful calibration record in your experiment program.

```python
from pathlib import Path
from ni_spl_calibration import apply_calibration, load_result, load_stimulus

folder = Path("results/calibration-timestamp")  # Use the actual record folder
stimulus = load_stimulus(folder / "stimulus.npz")
result = load_result(folder / "result.json")

output_v = apply_calibration(stimulus, result)
sample_rate_hz = stimulus.sample_rate_hz
output_channel = result.setup["daq"]["output_channel"]
```

`output_v` is an **array in volts**, formed by selecting the saved calibration interval and multiplying by `scale_v`. The helper also checks the waveform/rate hash and output limit. Send it to the DAQ at the recorded rate and AO channel, without additional normalization or gain. The array contains no delay padding or final 0 V sample; your experiment must return AO to 0 V after playback.

The calibration depends on the waveform, equipment, amplifier gain, acoustic path and measurement position. Recalibrate after changing the waveform or acoustic path, and acquire a new microphone reference after DAQ/input-chain configuration changes. Matching digital RMS alone does not establish that a scale transfers to another sound.

## Meaning of levels and records

The calibrated level is **signal SPL after background-power subtraction within the selected interval and band**. Monitored SPL includes background; spectrum levels in dBV represent band RMS voltage, not SPL. See the [measurement specification](docs/measurement.en.md) for the definitions.

Calibration records preserve the digital inputs for reuse without the original WAV or generator code.

| File | Contents |
|---|---|
| `result.json` | Playback `scale_v` in V/digital unit, achieved `measured_spl_db`, target and search history |
| `stimulus.npz` | Full source waveform after conversion/channel processing, rate and provenance. The interval is stored in the settings |
| `setup.json` / `reference.json` | Setup and microphone-reference snapshots |
| `run.json` / `checksums.json` | Inputs, parent record and file SHA-256 hashes |
| `failure.json` | Failure/cancellation reason and available iterations; no successful result is created |

Input checks and test playback log voltage, SPL and spectra in `levels.jsonl`; test playback also records applied volume. When raw recording is enabled, `capture-*.npz` contains microphone input and, during test playback, output voltage. GUI activity is stored under `sessions/<session ID>/events/`.

Reusing digital inputs does not reproduce the physical sound field. Check equipment, gain and microphone geometry when repeating a measurement.

## Further reference

- [Measurement specification and scope](docs/measurement.en.md): level definitions, interval, band, corrections and reuse conditions
- [CLI, generators and development](docs/automation.en.md): execution without the GUI, input contracts, simulation and tests
- [Executable Python API example](examples/python_api.py)
