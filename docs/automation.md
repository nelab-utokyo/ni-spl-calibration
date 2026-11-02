# CLI・生成関数・開発

[日本語](automation.md) | [English](automation.en.md) | [README](../README.md)

## CLI

[導入](../README.md#導入)後、GUIを使わずに実行できます。API/CLIだけなら `gui` extraを省略できます。詳細な引数は `uv run ni-spl --help`、`uv run ni-spl calibrate --help` で確認します。

```sh
uv run ni-spl init my-setup.yaml
uv run ni-spl reference my-setup.yaml --output results/reference-001.json
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --wav stimulus.wav --target-spl-db 70 --output results/stimulus-001
```

`reference` 前に校正器を装着し、`calibrate` 前に外してマイクを実際の測定位置に置きます。設定は [examples/setup.yaml](../examples/setup.yaml) を参照してください。出力先は毎回新しい名前にします。

DAQにアクセスせず波形を確認する場合:

```sh
uv run ni-spl inspect --setup my-setup.yaml --wav stimulus.wav
```

## 波形の準備

`daq.sample_rate_hz` をAI/AO、WAVの変換先、生成関数への共通レートとして使います。WAVの元レートはヘッダから読み取り、必要な場合だけフィルタ付きで変換します。保存済み `stimulus.npz` は変換せず、設定とレートが異なれば拒否します。

- 振幅は `[-1, 1]` の浮動小数点値です。自動正規化せず、SR変換で範囲を超えた場合も拒否します。
- 低いSRへの変換では新しいNyquistより高い成分が抑えられます。ランプは入力波形に含めます。
- CLIのWAV入力はモノラルです。Pythonでは `load_wav` の `channel_mode` に `mix`・`left`・`right` を指定してステレオを明示的に処理できます。左右平均では逆相成分が打ち消し合います。

生成関数は `sample_rate_hz` とパラメータYAMLのキーワード引数を受け、モノラル配列を返します。YAMLに `sample_rate_hz` は書きません。

```sh
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/generated-001
```

関数は1回だけ実行し、探索中と記録の復元ではその波形を再利用します。[生成関数](../examples/custom_sound.py)と[パラメータ](../examples/parameters.yaml)の例があります。

## Python API

`calibrate(...)` は、`Stimulus(samples, sample_rate_hz)`、`load_wav(...)`、`from_generator(...)` で用意した波形を受け取ります。`load_wav(path)` のレート省略時は元WAVのレートを保ちます。

DAQ操作は `NIDaqHardware`、ソフトウェア検証は `SimulatedHardware` に分離しています。[実行例](../examples/python_api.py)と[校正結果の再生](../README.md#校正結果を実験で使う)を参照してください。

## 実機なしで確認する

リポジトリ直下で実行します。

```sh
uv sync --locked --extra gui --extra dev
uv run ni-spl reference examples/setup.yaml --output results/demo-reference.json --simulate --yes
uv run ni-spl calibrate examples/setup.yaml --reference results/demo-reference.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/demo-am --simulate --yes
uv run pytest
```

`--simulate` は合成したスピーカー・マイクのソフトウェア検証です。記録は `backend: simulation` として保存し、実機の基準とは混用できません。APIの例は `uv run python examples/python_api.py`、GUIテストのヘッドレス実行は `QT_QPA_PLATFORM=offscreen uv run pytest` です。
