# Windows配布

Windows x64のポータブルZIPを使用します。ZIP全体を展開し、`NI-SPL-Calibration.exe` を起動します。`_internal` も同じフォルダに保持してください。Python・uvは不要ですが、DAQ実機には対応するNI-DAQmxドライバが必要です。

既定の記録先はWindowsの「ドキュメント / NI SPL Calibration / results」です。WAV・NPZと、同梱ライブラリを使う生成関数に対応します。追加のPythonライブラリが必要な生成関数にはソース版を使用してください。

## ビルド

64ビットWindows上で実行します。

```powershell
uv sync --locked --extra gui --extra daq --extra dev --group build
uv run --no-sync pytest
uv run --no-sync python packaging/build_windows.py
```

`dist/` にZIP、SHA-256、起動確認の結果を出力します。`build-info.json` はソースのcommitと依存バージョン、`smoke-test.json` はパッケージ内で実施した検証を保持します。実行ファイルは未署名です。

## GitHub Actions

`Windows binary` ワークフローでWindows上のテスト・ビルド・実行ファイルのシミュレーション検証を行います。成功した実行のArtifactsからZIPを取得できます。

タグを付けない実行は動作確認用です。プロジェクトのバージョンと一致する `v` タグ（例：`v0.1.0`）をpushした場合だけ、成功後にGitHub Releaseを公開します。バージョンを更新する場合は `pyproject.toml` と `uv.lock` を更新してください。

NI-DAQmxドライバは同梱しません。CIの検証は実機へアクセスせず、パッケージ内のGUI・WAV読み込み・SR変換・校正キュー・比較測定・記録の復元を確認します。実機での校正は使用するDAQ・入力系・音響経路で確認してください。

PyInstallerの標準フックでQt・NumPy・SciPy・libsndfileを収集し、動的に読み込むアプリのモジュールとNI Pythonパッケージ、実行記録用の配布メタデータも収集します。依存パッケージのライセンス文書は `licenses/` に保存します。
