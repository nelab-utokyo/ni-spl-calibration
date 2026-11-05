# Windows distribution

Extract the entire Windows x64 ZIP and run `NI-SPL-Calibration.exe`. Keep `_internal` alongside the executable. Python and uv are not required; physical NI hardware requires a compatible NI-DAQmx driver.

The default output folder is Windows Documents / NI SPL Calibration / results. WAV, NPZ and generators using bundled libraries are supported. Use the source distribution for generators requiring additional Python dependencies.

## Build

Run on 64-bit Windows:

```powershell
uv sync --locked --extra gui --extra daq --extra dev --group build
uv run --no-sync pytest
uv run --no-sync python packaging/build_windows.py
```

The ZIP, SHA-256 file and executable smoke-test report are written to `dist/`. `build-info.json` records the source commit and dependency versions; `smoke-test.json` records checks performed inside the packaged executable. The executable is unsigned.

## GitHub Actions

The `Windows binary` workflow tests and builds on Windows, then runs simulation checks inside the executable. Download the ZIP from the successful run's Artifacts.

Runs without a tag produce test builds. Pushing a `v` tag matching the project version (for example, `v0.1.0`) publishes a GitHub Release only after the build succeeds. To update the version, edit `pyproject.toml` and update `uv.lock`.

The NI-DAQmx driver is not bundled. CI never accesses physical hardware; it verifies the packaged GUI, WAV loading, resampling, calibration queue, comparisons and record restoration. Verify physical calibration with the actual DAQ, input chain and acoustic path.

PyInstaller hooks collect Qt, NumPy, SciPy and libsndfile. The build additionally collects dynamically imported application modules, the NI Python package and distribution metadata used in experiment records. Distribution license files are included under `licenses/`.
