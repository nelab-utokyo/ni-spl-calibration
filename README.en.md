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
uv sync --locked --extra gui --extra daq
uv run ni-spl
```

Physical hardware also requires the **NI-DAQmx driver** appropriate for your DAQ model and operating system. Installing the Python package alone is insufficient. See the [official NI documentation](https://nidaqmx-python.readthedocs.io/en/stable/). The hardware backend targets Windows/Linux environments supported by the driver.

The repository includes `uv.lock` so uv can reproduce the dependency versions. API-only installations can omit the `gui` extra.

## Desktop GUI

**Launch the desktop GUI with uv.** Run this from the repository root:

```sh
uv run ni-spl
```

Before the first launch, install the dependencies with `uv sync --locked --extra gui --extra daq` as shown above.

The application starts in **NI DAQ hardware mode**. Launching the GUI does not access the DAQ; a measurement or monitor must be started explicitly. Select Simulation at the top to check the software without hardware.

Use **Language** at the top to switch between Japanese and English. Settings, selected files, prepared samples, reference records and results remain intact. Record keys, units and user-entered equipment notes are not translated.

| Tab | Workflow |
|---|---|
| **1 設定 (Setup)** | Edit chain identity, AI/AO, common sample rate, voltage limits and all measurement parameters. Load/save setup YAML |
| **2 マイク基準測定 (Microphone reference)** | Fit the calibrator, measure and save a reference, or load a compatible existing JSON |
| **3 波形校正 (Waveform calibration)** | Remove the calibrator and position the microphone. Select WAV/NPZ/generator, preview the waveform and resampling, set the target and start calibration |
| **4 履歴・再現 (History and restore)** | Restore conditions from successful, failed or cancelled runs, including runs saved elsewhere |
| **5 マイクモニター (Microphone monitor)** | Configure raw input recording and inspect capture status and recording folders |
| **6 テスト再生 (Test playback)** | Play WAV/generated audio through AO with a live volume fader and simultaneous microphone monitoring; no reference required |

Set the sample rate once in the setup tab. Measurement runs in a worker thread while the GUI displays iterations and levels. Stop and closing the window are supported during playback. NI reads are split into approximately 20 ms chunks: cancellation stops AO, then AI, closes the tasks and commands 0 V. The full waveform need not finish. USB, OS and driver latency still apply; this is not a hardware cutoff with a guaranteed response time. Every run receives a new output path. Changes to rate, hardware/input settings or backend require a compatible reference.

### Records and reproducibility

Before calibration, `run.json`, setup, reference and the exact playback samples are saved. Inputs and target level remain available on failure/cancellation. `checksums.json` records SHA-256 hashes of completed record files; restore verifies them.

Select a run in History and click `選択した実行の条件を復元` to restore setup, backend, target, reference and saved stimulus. The original WAV or generator is unnecessary: restoration uses `stimulus.npz`, without rerunning random generators. Restore does not start hardware. Starting calibration then creates a new output bundle that links back to its parent run. Reproducing digital inputs does not reproduce physical room conditions or equipment state; verify the recorded gain, equipment and geometry before measurement. Older successful bundles can also be restored when all input snapshots exist, using internal consistency checks because they lack file-level hashes.

GUI operation starts, completion, failures, stop requests and setup/reference file loads/saves are recorded as timestamped JSON events under `sessions/<session ID>/events/`. Preview waveforms are also saved in that session. Open the activity folder using `操作ログを開く`. The log captures complete conditions at operation start rather than individual keystrokes.

### Live microphone monitoring

The spectrum, RMS/peak voltage and total SPL are **always visible in the shared panel below the tabs**, regardless of tab selection or scrolling. Click Start monitor in that panel from any tab. Configure raw input recording and open recording folders in the Microphone monitor tab.

During capture, the spectrum updates approximately every 100 ms. Frequency is logarithmic, up to Nyquist; bars show approximately third-octave RMS voltage levels in dBV (relative to 1 V). The panel shows an idle state before acquisition and labels the last input after stopping. This is a display, not an equalization filter. With a valid reference, **total SPL including background** is also shown in the configured measurement band; it differs from calibration's background-subtracted signal level. Clipping is flagged and the frame's SPL is not reported. Reference, calibration and test playback captures also feed this shared display.

AO stays at 0 V. Monitoring and reference/calibration tasks are mutually exclusive. Use Stop before continuing measurements.

Each `monitor-<timestamp>/` folder stores setup, reference, hardware information, `levels.jsonl` and a final `summary.json` (or a failure record). Raw input recording is **OFF by default**. When enabled, consecutive `capture-*.npz` files contain `samples` in volts, `sample_rate_hz` and `first_sample`; their SHA-256 hashes are recorded in the level log. Leaving raw captures off preserves the level time series and activity log.

### Test playback

Select audio or the built-in 1 kHz tone in Test playback. Generators selected in Waveform calibration are also supported. Stereo WAVs can be averaged as (L+R)/2 (the GUI default), or the left/right channel selected. The resulting mono samples and channel policy are recorded; pre/post-resampling RMS and peak are displayed. Anti-phase channels can cancel when averaged; select the left or right channel when needed.

Start playback, then adjust the live fader. 100% corresponds to `max_peak_v`; the default 10% gives 0.05 Vpeak with a 0.5 V limit. This is not a calibrated SPL setting. Mute, mid-file Stop, looping and simultaneous microphone frequency bars are available without a reference.

The device, AI/AO channels and commanded AO peak are displayed during playback. The commanded voltage is the program's output value, not a measurement at the analog terminal.

Each `playback-<timestamp>/` records setup, stimulus, optional reference, `playback.json`, `levels.jsonl` and a summary/failure record. Applied fader settings, 10 ms gain ramps and spectra are logged. Raw AO/microphone recording is **OFF by default**. When enabled, `capture-*.npz` contains microphone `samples`, the AO `output_v` written, sample rate and starting sample position. The source waveform, setup and applied volume history are retained with raw recording off.

Preview emits no audio; standalone microphone monitoring holds AO at 0 V. Use Test playback for an audible check.

### Choose the initial calibration level

`start_scale_v` is the initial **volts per digital unit**. The first peak voltage is `min(start_scale_v * waveform_peak, max_peak_v)`. For example, a waveform peak of 0.5 and an initial scale of 0.01 produce 0.005 Vpeak. Waveform calibration displays this initial Vpeak.

To choose it using playback:

1. Measure the microphone reference and test-play the waveform you will calibrate.
2. Start low and adjust the fader while watching the input and SPL. Use a level below the target without clipping the microphone input as a starting point.
3. Click **Use this volume to start calibration**. Playback stops before the applied gain is saved as the initial scale and Waveform calibration opens. Muted or clipped frames and pending fader changes cannot be adopted. The exact prepared waveform is retained, and the selected gain and source playback record are logged.
4. Check the initial Vpeak, set the target and start calibration. Test playback's total SPL is a guide; the calibration search checks SNR against a separate background measurement.

The iteration table also displays SNR. Insufficient SNR continues the search with bounded upward steps. The default for new setups and the allowed maximum are **10 dB per step** (approximately 3.16 times the voltage). The peak-voltage limit still applies. If SNR remains insufficient at that limit, increasing only the initial scale will not help; check the microphone input, gain and analysis band. Existing setup files and restored runs retain their saved step size.

## Using commands

The usual flow is **configure → measure the microphone reference → calibrate each waveform → play the saved waveform in your experiment**.

| Command | Purpose | When to run |
|---|---|---|
| `init my-setup.yaml` | Create a configuration template to edit | Once, or copy an existing setup |
| `inspect --setup my-setup.yaml --wav stimulus.wav` | Preview the playback waveform's rate, length, amplitude and conversion history without DAQ access | Optional, before measurement |
| `reference my-setup.yaml --output …` | Measure microphone input sensitivity with an acoustic calibrator | Initially and after changes such as DAQ rate or input-chain settings |
| `calibrate my-setup.yaml --reference … --wav … --target-spl-db … --output …` | Play the waveform and find the voltage scale for the target SPL | For each waveform, target level and acoustic path |

With an existing setup and valid reference, routine use requires **only `calibrate`**. Between `reference` and `calibrate`, remove the calibrator and place the microphone at the measurement position. You do not need to repeat both measurements every time.

For experimental playback, apply the scale to the saved waveform using the Python API ([Use the calibration result for playback](#use-the-calibration-result-for-playback)). There is no separate `play` command.

## Set the sample rate in one place

```yaml
daq:
  sample_rate_hz: 200000
```

This value controls DAQ AI/AO, the WAV playback rate and the `sample_rate_hz` passed to generators. The source WAV rate is read from its header. When it differs, the WAV is resampled with an anti-aliasing filter before calibration, and the converted samples are calibrated and saved. Matching rates preserve the samples exactly. Do not put `sample_rate_hz` in the generator parameter YAML.

Use `inspect --setup my-setup.yaml …` to preview the same prepared waveform. Without a setup, `inspect --wav …` inspects the original WAV. Standalone inspection also accepts `inspect --sample-rate-hz …`; use `--setup` for the normal workflow. Standalone generator inspection defaults to 200000 Hz.

After changing the rate, measure a new reference and recalibrate the stimulus. Saved `stimulus.npz` files preserve a calibrated waveform exactly: they are never resampled, and a mismatch with the setup rate is rejected.

## Try the complete workflow without hardware

Run the following from the repository root. The `results/` directory is excluded from Git.

```sh
uv sync --locked --extra gui --extra dev
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
uv run ni-spl inspect --setup my-setup.yaml --wav stimulus.wav
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --wav stimulus.wav --target-spl-db 70 --output results/stimulus-001
```

To use a sound generator function, provide a callable that accepts `sample_rate_hz` and the specified keyword parameters and returns a mono floating-point array within `[-1, 1]`. The `--generator` option executes the specified Python code.

```sh
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/generated-001
```

The generator is called once, and that waveform remains fixed throughout the search. PCM and floating-point WAV files are supported and converted to `daq.sample_rate_hz` when necessary. Downsampling filters out components above the new Nyquist frequency, so choose a rate that includes the required bandwidth. Amplitude normalization and channel mixing are never performed automatically. Conversion that exceeds `[-1, 1]` is rejected; leave amplitude headroom in the source WAV. Include any onset/offset ramps in the source waveform.

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
| `stimulus.npz` | Losslessly saved waveform used in the search (after WAV conversion, if any), sample rate, source and conversion information |
| `setup.json` / `reference.json` | Snapshots of the configuration and reference measurement |
| `run.json` | Inputs, target level, backend, waveform hash and parent run, saved before measurement |
| `checksums.json` | SHA-256 hashes of completed record files, verified when restoring |
| `failure.json` | Failure reason and available search history; no success record is created on failure |

```python
from ni_spl_calibration import load_result, load_stimulus, apply_calibration

stimulus = load_stimulus("results/stimulus-001/stimulus.npz")
result = load_result("results/stimulus-001/result.json")
output_v = apply_calibration(stimulus, result)
# Read the playback rate from stimulus.sample_rate_hz.
# Play output_v using the calibrated DAQ rate, AO channel, and signal chain.
# output_v is in volts. Do not apply another gain or normalization.
```

`scale_v` is volts per digital unit; the playback voltage is `samples * scale_v`. `apply_calibration` checks the waveform/sample-rate hash and voltage limit. The returned array has the same length as the source waveform, so your own playback implementation must also return AO to 0 V at the end.

From Python, pass `Stimulus(samples, setup.daq.sample_rate_hz)`, `load_wav(path, sample_rate_hz=setup.daq.sample_rate_hz)`, or `from_generator(fn, sample_rate_hz=setup.daq.sample_rate_hz, ...)` to `calibrate(...)`. Omitting the rate in `load_wav(path)` preserves the original WAV. After calibration, use the saved `stimulus.npz` for playback. DAQ operations are separated into `NIDaqHardware`; tests can use `SimulatedHardware`. See [examples/python_api.py](examples/python_api.py) for a complete example.

## Scope and validation status

This version calibrates the **overall level of each waveform**. It does not flatten the speaker's frequency response. Calibrate sounds with different spectra separately, even when their digital RMS values match.

If your experiment generates new noise on every trial, distinguish this result for one fixed waveform from the distribution across stimuli. Check level variability using other seeds or a representative waveform set. Calibration supports finite mono waveforms; stereo WAVs can be explicitly mixed/selected to mono. Stereo output and multiple-DAQ synchronization remain outside scope. Test-playback looping is for output checks, not infinite-stream SPL calibration.

The output is constrained by a peak-voltage limit and upward search steps of at most 10 dB. The package attempts to return AO to 0 V after each acquisition, on exceptions, and on Ctrl+C. Input clipping is checked against both voltage rails, including DC offsets and samples outside the analysis window. Software cannot guarantee output reset after operating-system termination, power loss, or disconnection.

Reference records are tied to the setup identity and DAQ configuration. Detected changes require a new reference measurement. Physical changes such as gain or wiring changes cannot be detected automatically; update `chain_notes`. Recalibrate the stimuli after changing the speaker, microphone position/orientation, room conditions, or other material parts of the acoustic path. JSON/NPZ records are never overwritten, preserving earlier calibrations.

Automated tests cover reference sensitivity, level searches including AM sounds, frequency bands, windows and delays, output limits, SNR, clipping, GUI operations/restoration/cancellation/live monitoring, record integrity and the CLI workflow. GUI tests require the `gui` extra; use `QT_QPA_PLATFORM=offscreen uv run pytest` in headless environments. **Physical NI DAQ validation has not been performed.** At the receiving setup, first use a known reference signal and a low output level to check clock routing, input configuration, return to 0 V, and measured levels.
