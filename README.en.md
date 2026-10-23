# NI SPL Calibration

[日本語](README.md) | [English](README.en.md)

A standalone Python package that finds the **voltage scale needed to play a particular waveform at a target sound pressure level (SPL)**. It accepts WAV files, sound generator functions, and NumPy arrays, using the same calibration procedure for pure tones, white noise, and amplitude-modulated (AM) sounds. It has no dependencies on the original experiment repository, experiment controllers, or cameras.

The package supports finite mono waveforms and one analog input (AI) and one analog output (AO) channel on the same NI DAQ device. It measures the microphone input chain's sensitivity using an acoustic calibrator, then plays the actual stimulus and searches for the voltage scale that reaches the target SPL.

```text
Acoustic calibrator → microphone → conditioner → DAQ AI  (reference measurement)
WAV / generator / array → DAQ AO → amplifier → speaker → microphone → DAQ AI
```

## Installation

Use Git, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/). Clone the repository and run the commands from its root directory:

```sh
git clone https://github.com/kmraven/ni-spl-calibration.git
cd ni-spl-calibration
uv sync --locked --extra daq
uv run ni-spl --help
```

Physical hardware also requires the **NI-DAQmx driver** appropriate for your DAQ model and operating system. Installing the Python package alone is insufficient. See the [official NI documentation](https://nidaqmx-python.readthedocs.io/en/stable/). The hardware backend targets Windows/Linux environments supported by the driver.

For pip, run `python -m pip install ".[daq]"` from the repository root. The repository includes `uv.lock` so uv can reproduce the dependency versions.

## Try the complete workflow without hardware

Run the following from the repository root. The `results/` directory is excluded from Git.

```sh
uv sync --locked --extra dev
uv run ni-spl reference examples/setup.yaml --output results/demo-reference.json --simulate --yes
uv run ni-spl calibrate examples/setup.yaml --reference results/demo-reference.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/demo-am --simulate --yes
uv run pytest
```

`--simulate` checks the software using a synthetic speaker and microphone. Every record is marked `backend: simulation`, and simulated reference measurements cannot be mixed with physical hardware measurements. To try the Python API with a generated waveform, run `uv run python examples/python_api.py`. Existing output paths are never overwritten; choose a new name when repeating an example.

## Calibrate your setup

### Fix the configuration and measurement position

Copy `examples/setup.yaml`, or create an unmeasured setup template:

```sh
uv run ni-spl init my-setup.yaml
```

Use `setup_id` and `chain_notes` to record equipment models and unit identifiers, gains, filters, wiring, and microphone position/orientation. Set the AI/AO channels, input terminal configuration, voltage ranges, sample rate, and `max_peak_v` for your hardware. Determine RSE versus differential input from the actual signal source and grounding arrangement; a BNC connection alone does not determine the correct mode. Keep the external amplifier gain fixed.

The NI DAQ must support hardware-timed AI and AO at the requested rate and internal routing of the AO SampleClock to AI on the same device. AI is armed first and acquires samples using the AO clock. Unsupported routes or rates cause an error; there is no fallback to software timing. The package also stops if the DAQ coerces the requested sample rate to another value. See the [NI clock configuration API](https://nidaqmx-python.readthedocs.io/en/stable/task.html).

Perform calibration without animals or listeners exposed, and close other programs that use the DAQ.

### Measure the microphone input chain reference

Fit the acoustic calibrator to the microphone and operate it at the configured level and frequency. The example configuration uses 94 dB SPL at 1 kHz.

```sh
uv run ni-spl reference my-setup.yaml --output results/reference-001.json
```

AO remains at 0 V while the package checks the reference frequency, pure-tone content, temporal stability, and input clipping. The resulting sensitivity, in **V/Pa**, covers the microphone, conditioner, cable, and DAQ input as one measurement chain.

### Calibrate the waveform you will use

Remove the calibrator and place the microphone at the intended measurement position.

```sh
uv run ni-spl inspect --wav stimulus.wav
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --wav stimulus.wav --target-spl-db 70 --output results/stimulus-001
```

To use a sound generator function, provide a callable that accepts `sample_rate_hz` and the specified keyword parameters and returns a mono floating-point array within `[-1, 1]`. The `--generator` option executes the specified Python code.

```sh
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/generated-001
```

The generator is called once, and that waveform remains fixed throughout the search. PCM and floating-point WAV files are supported. Amplitude normalization, channel mixing, and sample-rate conversion are never performed implicitly. If the WAV and DAQ rates differ, explicitly convert the waveform and calibrate the converted version. Include any onset/offset ramps in the source waveform.

## Define what the target SPL means

Calibration uses the **time-averaged mean-square signal pressure after background-power subtraction**, within the selected time interval and frequency band. No perceptual frequency weighting is applied. If microphone response corrections are configured, they are applied to the power at each frequency.

```yaml
measurement:
  band_hz: [5000, 80000]  # null: all AC bins up to Nyquist
  start_s: 0.1           # Start time within the source waveform
  stop_s: 1.9            # null: end of the source waveform
  acoustic_delay_s: 0.0  # Acoustic/equipment delay relative to AO; measure if needed
```

- Including silence or ramps changes the measured level. For AM sounds, select an interval containing enough modulation cycles. For short sounds, check the delay and measurement window carefully.
- FFT-bin powers are integrated to obtain the energy average over the entire interval. The calculation does not use a median of block RMS values or an arithmetic average of dB values. The rectangular measurement window introduces spectral leakage and frequency-resolution limitations near band boundaries.
- Intentional AM variation is not classified as instability. Instead, the level over the whole measurement interval must fall within the target tolerance at least twice at the same voltage scale.
- Measurements below the required SNR are not accepted as SPL readings. A level hidden below the measurement floor is not saved as a directly measured result. Background subtraction assumes that background noise remains stable during measurement and is uncorrelated with the stimulus.

Specify additive microphone response corrections relative to the reference sensitivity as `frequency_correction_db: [[Hz, dB], ...]`. If microphone sensitivity at a frequency is +1 dB relative to the reference frequency, the correction is −1 dB. Corrections are interpolated linearly in dB between frequency points; a curve that does not cover the analysis band is rejected. An empty list means no frequency-response correction. A 1 kHz reference measurement alone does not establish accuracy throughout the ultrasonic range.

## Use the calibration result for playback

The output directory contains:

| File | Contents |
|---|---|
| `result.json` | Voltage scale, measured SPL, output RMS/peak voltage, complete search history, setup, reference, and hardware/library information |
| `stimulus.npz` | Losslessly saved waveform, sample rate, and source information used in the search |
| `setup.json` / `reference.json` | Snapshots of the configuration and reference measurement |
| `failure.json` | Failure reason and available search history; no success record is created on failure |

```python
from ni_spl_calibration import load_result, load_stimulus, apply_calibration

stimulus = load_stimulus("results/stimulus-001/stimulus.npz")
result = load_result("results/stimulus-001/result.json")
output_v = apply_calibration(stimulus, result)
# Play output_v using the calibrated DAQ rate, AO channel, and signal chain.
# output_v is in volts. Do not apply another gain or normalization.
```

`scale_v` is volts per digital unit; the playback voltage is `samples * scale_v`. `apply_calibration` checks the waveform/sample-rate hash and voltage limit. The returned array has the same length as the source waveform, so your own playback implementation must also return AO to 0 V at the end.

From Python, pass a `Stimulus(samples, sample_rate_hz)`, the result of `load_wav(...)`, or the result of `from_generator(...)` to `calibrate(...)`. DAQ operations are separated into `NIDaqHardware`; tests can use `SimulatedHardware`. See [examples/python_api.py](examples/python_api.py) for a complete example.

## Scope and validation status

This version calibrates the **overall level of each waveform**. It does not flatten the speaker's frequency response. Calibrate sounds with different spectra separately, even when their digital RMS values match.

If your experiment generates new noise on every trial, distinguish this result for one fixed waveform from the distribution across stimuli. Check level variability using other seeds or a representative waveform set. Infinite real-time streams, stereo, and synchronization across multiple DAQ devices are outside this version's scope.

The output is constrained by a peak-voltage limit and upward search steps of at most 6 dB. The package attempts to return AO to 0 V after each acquisition, on exceptions, and on Ctrl+C. Input clipping is checked against both voltage rails, including DC offsets and samples outside the analysis window. Software cannot guarantee output reset after operating-system termination, power loss, or disconnection.

Reference records are tied to the setup identity and DAQ configuration. Detected changes require a new reference measurement. Physical changes such as gain or wiring changes cannot be detected automatically; update `chain_notes`. Recalibrate the stimuli after changing the speaker, microphone position/orientation, room conditions, or other material parts of the acoustic path. JSON/NPZ records are never overwritten, preserving earlier calibrations.

Automated tests cover reference sensitivity, level searches including AM sounds, frequency bands, time windows and delays, output limits, SNR, clipping, record reuse, and the CLI workflow. **Physical NI DAQ validation had not been performed when this repository was created.** At the receiving setup, first use a known reference signal and a low output level to check clock routing, input configuration, return to 0 V, and measured levels.
