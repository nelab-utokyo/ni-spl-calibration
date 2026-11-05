Windows x64 portable build. Extract the entire ZIP and launch `NI-SPL-Calibration.exe`; keep `_internal` beside the executable. Python and uv are not required.

NI実機を使う場合は、対応するNI-DAQmxドライバを別途インストールしてください。ZIPをすべて展開し、`NI-SPL-Calibration.exe` を起動します。Python・uvは不要です。

Includes single/multi-stimulus calibration, fixed-gain position comparisons, Japanese/English UI and portable calibration records. Raw microphone recording defaults to OFF.

The build runs the automated tests on Windows and a simulation-based smoke test inside the packaged executable. Physical DAQ behavior requires verification on the actual setup. This executable is unsigned.

See `START-HERE.txt`, `build-info.json`, `smoke-test.json` and the included dependency notices. A `.sha256` file is attached for download integrity checks.
