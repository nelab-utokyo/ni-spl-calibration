# NI SPL Calibration

[日本語](README.md) | [English](README.en.md)

WAV・音生成関数・NumPy配列から、**その波形を目標音圧で再生するための電圧倍率**を求める独立Pythonパッケージです。純音、白色雑音、AM音などを同じ処理で校正できます。実験制御・カメラ・元の実験リポジトリへの依存はありません。

対象は有限長のモノラル波形と、同じNI DAQ上のAI 1チャンネル・AO 1チャンネルです。音響校正器でマイク入力系の感度を測り、実際の刺激を再生して目標音圧に合う倍率を探索します。

```text
音響校正器 → マイク → コンディショナ → DAQ AI  （基準測定）
WAV / 生成関数 / 配列 → DAQ AO → アンプ → スピーカー → マイク → DAQ AI
```

## 導入

Git、Python 3.11以上、[uv](https://docs.astral.sh/uv/)を使用します。リポジトリをcloneし、そのディレクトリで実行してください。

```sh
git clone https://github.com/kmraven/ni-spl-calibration.git
cd ni-spl-calibration
uv sync --locked --extra daq
uv run ni-spl --help
```

実機には、DAQ機種とOSに対応した **NI-DAQmxドライバ**も必要です。Pythonパッケージだけでは動きません。[NI公式ドキュメント](https://nidaqmx-python.readthedocs.io/en/stable/)を参照してください。Windows/Linuxのドライバ対応環境を対象とします。

pipの場合はリポジトリ直下で `python -m pip install ".[daq]"` を実行してインストールできます。リポジトリには `uv.lock` を含み、uvでは同じ依存バージョンを再現できます。

## コマンドと全体の流れ

通常の流れは **設定 → マイク基準測定 → 波形ごとの校正 → 保存した波形を実験で再生** です。

| コマンド | 役割 | 実行するタイミング |
|---|---|---|
| `init my-setup.yaml` | 編集用の設定ファイルを作る | 最初の1回。既存の設定をコピーしてもよい |
| `inspect --setup my-setup.yaml --wav stimulus.wav` | 再生用波形のレート・長さ・振幅・変換履歴を確認する。DAQは操作しない | 任意。測定前の確認用 |
| `reference my-setup.yaml --output …` | 音響校正器でマイク入力系の感度を測る | 初回、およびDAQレート・入力系設定などを変更したとき |
| `calibrate my-setup.yaml --reference … --wav … --target-spl-db … --output …` | 実際の波形を再生して目標音圧になる電圧倍率を求める | 波形・目標音圧・音響経路ごと |

作成済みの設定と有効な基準記録があれば、普段は **`calibrate` だけ**で済みます。`reference` と `calibrate` の間には、校正器を外してマイクを測定位置に置く作業が入ります。両方の測定を毎回やり直す必要はありません。

校正後の実験用再生はPython APIで保存波形に倍率を適用します（[校正結果を再生に使う](#校正結果を再生に使う)）。独立した `play` コマンドはありません。

## サンプルレートは設定ファイルの1か所で指定する

```yaml
daq:
  sample_rate_hz: 200000
```

この値をDAQのAI/AO、WAVの再生用レート、生成関数への `sample_rate_hz` に共通で使います。WAVの元レートはヘッダから読み取るため手入力不要です。異なる場合は校正前にフィルタ付きでリサンプリングし、変換後の波形を校正・保存します。同じレートならサンプルは変更しません。生成関数のパラメータYAMLには `sample_rate_hz` を書きません。

`inspect --setup my-setup.yaml …` も同じレートで準備した波形を表示します。設定なしの `inspect --wav …` は元WAVの確認用です。単独確認には `inspect --sample-rate-hz …` も使えますが、通常は `--setup` で統一してください。設定なしの生成関数の確認は既定の200000 Hzを使います。

レート変更後は基準測定と刺激の校正をやり直します。保存済み `stimulus.npz` は校正済み波形の再現用なので変換せず、設定とレートが異なれば停止します。

## 実機なしで一通り確認する

以下はリポジトリ直下で実行します。`results/` はGit対象外です。

```sh
uv sync --locked --extra dev
uv run ni-spl reference examples/setup.yaml --output results/demo-reference.json --simulate --yes
uv run ni-spl calibrate examples/setup.yaml --reference results/demo-reference.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/demo-am --simulate --yes
uv run pytest
```

`--simulate` は合成したスピーカー・マイクのソフトウェア検証です。すべての記録に `backend: simulation` が入り、実機の基準測定と混用できません。生成波形を使うPython APIの実行例は `uv run python examples/python_api.py` でも確認できます。既存の出力先は上書きしないので、再実行時は新しい名前を指定してください。

## 自分のセットアップで校正する

### 設定と測定位置を固定する

`examples/setup.yaml` をコピーするか、次のコマンドで未測定の設定を作ります。

```sh
uv run ni-spl init my-setup.yaml
```

`setup_id` と `chain_notes` に機器・個体・ゲイン・フィルタ・配線・マイク位置/向きを記録してください。AI/AOチャンネル、入力方式と電圧範囲、サンプルレート、`max_peak_v` は実機に合わせます。BNC接続だけからRSE/差動を決めず、実際の信号源と接地構成を確認します。アンプ側のゲインも固定してください。

NI DAQは、同一デバイス内でAO SampleClockをAIへ配線でき、両方を指定レートでハードウェア駆動できることが必要です。AIを先に待機させ、AOのクロックで取得します。対応しないルートやレートはエラーにし、ソフトウェアタイミングへの切り替えは行いません。DAQが要求レートを別の値へ丸めた場合も停止します。[NIのクロック設定API](https://nidaqmx-python.readthedocs.io/en/stable/task.html)を参照してください。

動物や聴取者のいない状態で実施し、DAQを利用する他のプログラムを閉じます。

### マイク入力系の基準を測る

マイクに音響校正器を装着し、設定した音圧・周波数で動作させます。例の設定は94 dB SPL・1 kHzです。

```sh
uv run ni-spl reference my-setup.yaml --output results/reference-001.json
```

AOを0 Vに保ち、基準信号の周波数・純音成分・時間的安定性・入力クリッピングを確認します。感度はマイク、コンディショナ、ケーブル、DAQ入力をまとめて測った **V/Pa** です。

### 実際に使用する波形を校正する

校正器を外し、マイクを実際の測定位置に置きます。

```sh
uv run ni-spl inspect --setup my-setup.yaml --wav stimulus.wav
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --wav stimulus.wav --target-spl-db 70 --output results/stimulus-001
```

音生成関数の場合は次の形です。関数は `sample_rate_hz` と指定したキーワード引数を受け、`[-1, 1]` 内のモノラル浮動小数点配列を返します。`--generator` は指定したPythonコードを実行します。

```sh
uv run ni-spl calibrate my-setup.yaml --reference results/reference-001.json --generator examples/custom_sound.py:make_sound --parameters examples/parameters.yaml --target-spl-db 70 --output results/generated-001
```

生成関数は1回だけ呼び、探索中はその波形を固定します。WAVはPCM/浮動小数点形式を読み、必要なら `daq.sample_rate_hz` へ変換します。低いレートへの変換では新しいNyquist周波数より高い成分がフィルタで抑えられるため、必要な帯域を含むレートを選んでください。振幅の自動正規化・チャンネル混合は行いません。変換で振幅が `[-1, 1]` を超える場合は停止するので、元WAVに余裕を持たせてください。立ち上がり/立ち下がりランプも元波形に含めます。

## 「目標音圧」の意味を設定する

校正するのは、指定区間・帯域の**背景パワーを差し引いた信号の平均二乗音圧**です。周波数重み付けはありません。マイクの感度補正を設定した場合は、周波数ごとのパワーに反映します。

```yaml
measurement:
  band_hz: [5000, 80000]  # nullならDCを除くNyquistまで
  start_s: 0.1           # 元波形内の開始時刻
  stop_s: 1.9            # nullなら元波形の末尾まで
  acoustic_delay_s: 0.0  # AOに対する音響/機器遅延。必要なら実測して設定
```

- 無音やランプを区間に含めるかで値が変わります。AM音は十分な変調周期を含む区間を指定してください。短い音は遅延と測定窓の選択を特に確認します。
- FFTの各周波数のパワーを積算し、区間全体のエネルギー平均を取ります。ブロックRMSの中央値やdB値の算術平均は使いません。矩形の測定窓なので、帯域境界付近ではスペクトル漏れや周波数分解能の影響があります。
- AM音の意図した変動を不安定とは判定せず、同じ倍率で測定区間全体の音圧が目標内に入ることを2回以上確認します。
- SNRが足りない測定は音圧値として採用しません。測定できない低音圧を、直接測定した値として保存することもありません。背景減算は背景が測定中も安定し、刺激と無相関であることを仮定します。

基準感度に対して、各周波数の加算補正値を `frequency_correction_db: [[Hz, dB], ...]` に設定できます。マイクの感度が基準周波数より+1 dBなら補正は−1 dBです。点間はdBを線形補間し、測定帯域外への外挿は拒否します。空配列は周波数応答を補正しないという意味です。1 kHzの基準測定だけで超音波域までの正確さが保証されるわけではありません。

## 校正結果を再生に使う

出力ディレクトリには以下を保存します。

| ファイル | 内容 |
|---|---|
| `result.json` | 倍率、実測音圧、出力RMS/ピーク、全探索結果、設定、基準、機器/ライブラリ情報 |
| `stimulus.npz` | 探索に使用した波形（WAV変換時は変換後）・サンプルレート・入力元/変換情報を可逆保存 |
| `setup.json` / `reference.json` | 設定と基準測定のスナップショット |
| `failure.json` | 失敗時の理由・取得済み探索履歴。成功記録は作成しない |

```python
from ni_spl_calibration import load_result, load_stimulus, apply_calibration

stimulus = load_stimulus("results/stimulus-001/stimulus.npz")
result = load_result("results/stimulus-001/result.json")
output_v = apply_calibration(stimulus, result)
# 使用したDAQレート・AOチャンネル・信号経路でoutput_vを再生する。
# 再生レートはstimulus.sample_rate_hzから取得する。
# output_vの単位はV。追加のgainや正規化を掛けない。
```

`scale_v` はデジタル値1あたりのVで、再生電圧は `samples * scale_v` です。`apply_calibration` は波形/レートのハッシュと電圧上限を確認します。返す配列は元波形と同じ長さなので、独自の再生処理でも終了時の0 V出力を実装してください。

Pythonからは `Stimulus(samples, setup.daq.sample_rate_hz)`、`load_wav(path, sample_rate_hz=setup.daq.sample_rate_hz)`、`from_generator(fn, sample_rate_hz=setup.daq.sample_rate_hz, ...)` を `calibrate(...)` に渡せます。`load_wav(path)` のようにレートを省略した場合は元WAVをそのまま読み込みます。校正後は、保存した `stimulus.npz` を再生に使ってください。DAQ操作は `NIDaqHardware` に分離し、テストでは `SimulatedHardware` を使えます。全体の例は [examples/python_api.py](examples/python_api.py) にあります。

## 適用範囲と確認状況

この版は**波形ごとの全体レベル校正**です。スピーカーの周波数特性を平坦化する処理ではありません。同じデジタルRMSでもスペクトルが違う音は個別に校正してください。

毎試行異なる雑音を生成する実験では、この結果が固定した1波形に対する値であることを区別し、別seedや代表波形群で音圧のばらつきを確認します。リアルタイムの無限ストリーム、ステレオ、複数DAQの同期は対象外です。

出力にはピーク電圧上限と最大6 dBの上昇幅を適用し、各取得の終了・例外・Ctrl+CでAOの0 V復帰を試みます。入力クリッピングはDCや区間外も含めて両側の電圧範囲で確認します。OS終了、電源断、接続断などではソフトウェアによる復帰を保証できません。

基準記録はセットアップ識別情報とDAQ設定に結び付けます。変更を検出した場合は再基準測定が必要です。ゲインや配線などの物理的変更は自動検出できないため、`chain_notes` を更新してください。スピーカー、測定位置/向き、室内条件など音響経路を変えた場合は刺激も再校正します。JSON/NPZは上書きせず、過去の校正を残します。

自動テストでは基準感度、AMを含む音圧探索、帯域/窓/遅延、出力上限、SNR、クリッピング、記録の再利用、CLIの一連の操作を検証します。**このリポジトリ作成時点でNI DAQ実機による検証は未実施です。** 共有先ではまず既知の基準信号と低い出力でクロック経路・入力方式・0 V復帰・実測値を確認してください。
