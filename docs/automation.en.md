# CLI, generators and development

[日本語](automation.md) | [English](automation.en.md) | [README](../README.en.md)

## CLI

After [installation](../README.en.md#installation), use the CLI without opening the GUI. API/CLI-only installations can omit the `gui` extra. See `uv run ni-spl --help` and `uv run ni-spl calibrate --help` for arguments.

```sh
uv run ni-spl init my-setup.yaml
uv run ni-spl reference my-setup.yaml --output results/reference-001.json
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --wav stimulus.wav --target-spl-db 70 --output results/stimulus-001
```

Fit the calibrator before `reference`; remove it and place the microphone at the experimental position before `calibrate`. See [examples/setup.yaml](../examples/setup.yaml) for configuration. Use a new output path for each run.

Inspect a waveform without accessing the DAQ:

```sh
uv run ni-spl inspect --setup my-setup.yaml --wav stimulus.wav
```

## Preparing waveforms

`daq.sample_rate_hz` is shared by AI/AO, the WAV conversion target and generator calls. Source WAV rates come from their headers; filtering/resampling occurs only when necessary. Saved `stimulus.npz` files are never resampled and are rejected if their rate differs from the setup.

- Digital amplitudes are floating-point values within `[-1, 1]`. No automatic normalization is applied; out-of-range resampling results are also rejected.
- Downsampling suppresses content above the new Nyquist frequency. Include onset/offset ramps in the source waveform.
- CLI WAV input is mono. In Python, set `load_wav`'s `channel_mode` to `mix`, `left` or `right` for explicit stereo processing. Averaging can cancel anti-phase channels.

Generators accept `sample_rate_hz` and keyword arguments from the parameter YAML, and return a mono array. Do not specify `sample_rate_hz` in that YAML.

```sh
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/generated-001
```

The function runs once; the resulting waveform is reused during the search and restoration. Example [generator](../examples/custom_sound.py) and [parameters](../examples/parameters.yaml) are included.

## Python API

`calibrate(...)` accepts waveforms prepared with `Stimulus(samples, sample_rate_hz)`, `load_wav(...)` or `from_generator(...)`. Omitting the rate in `load_wav(path)` preserves the source rate.

DAQ operations use `NIDaqHardware`; software checks use `SimulatedHardware`. See the [executable example](../examples/python_api.py) and [result playback](../README.en.md#use-the-result-in-an-experiment).

## Check without hardware

Run from the repository root:

```sh
uv sync --locked --extra gui --extra dev
uv run ni-spl reference examples/setup.yaml --output results/demo-reference.json --simulate --yes
uv run ni-spl calibrate examples/setup.yaml --reference results/demo-reference.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/demo-am --simulate --yes
uv run pytest
```

`--simulate` checks software with a synthetic speaker and microphone. Records are marked `backend: simulation` and cannot be mixed with physical references. Run the API example with `uv run python examples/python_api.py`, or headless GUI tests with `QT_QPA_PLATFORM=offscreen uv run pytest`.
