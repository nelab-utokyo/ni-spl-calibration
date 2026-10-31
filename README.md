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
uv sync --locked --extra gui --extra daq
uv run python -m ni_spl_calibration
```

実機には、DAQ機種とOSに対応した **NI-DAQmxドライバ**も必要です。Pythonパッケージだけでは動きません。[NI公式ドキュメント](https://nidaqmx-python.readthedocs.io/en/stable/)を参照してください。Windows/Linuxのドライバ対応環境を対象とします。

pipの場合はリポジトリ直下で `python -m pip install ".[gui,daq]"` を実行してインストールできます。リポジトリには `uv.lock` を含み、uvでは同じ依存バージョンを再現できます。GUI不要のPython API利用では `gui` extraを省略できます。

## GUIで使う

**PythonコマンドでデスクトップGUIを起動します。** パッケージをインストールした仮想環境のPythonで実行してください。

```sh
python -m ni_spl_calibration
```

uvで仮想環境を管理している場合は、リポジトリ直下で `uv run python -m ni_spl_calibration` を実行します。

初期状態は**シミュレーション**です。実機を使う場合は画面右上で「NI DAQ 実機」に切り替えてください。

| 画面 | 操作 |
|---|---|
| **1 設定** | 機器・ゲイン・位置を記録し、AI/AO・共通SR・電圧上限などを入力。既存YAMLの読込・保存も可能 |
| **2 マイク基準測定** | 校正器を装着して「基準を測定して保存」。有効な基準JSONは読込・再利用可能 |
| **3 波形校正** | 校正器を外してマイクを測定位置へ置き、WAV/NPZ/生成関数を選択。波形とSR変換を確認し、目標音圧を指定して「校正を開始」 |
| **4 履歴・再現** | 過去の成功・失敗・停止記録から、条件をまとめて復元。別の保存場所の記録フォルダも選択可能 |
| **5 マイクモニター** | 連続AI取得による入力波形・RMS電圧・ピーク電圧・総音圧を表示。生入力の保存も選択可能 |

SRは「1 設定」の**共通サンプルレート**だけで指定します。測定処理は別スレッドで実行し、探索履歴と実測値を表示します。「停止」やウィンドウを閉じる操作では、現在の有限長取得が終わってからAOの0 V復帰を試み、取得済みの履歴を残します。停止要求は即時のハードウェア遮断ではありません。

保存先には毎回新しいフォルダ・ファイル名を付けます。設定したSR、機器、入力系の条件、モードと基準が合わない場合は再基準測定が必要です。

### 実施内容を保存して、同じ条件を復元する

校正では測定前に `run.json` と設定・基準・実際の再生波形を保存します。失敗・停止でも目標音圧を含む入力条件が残ります。完了時の `checksums.json` にはファイルのSHA-256を保存し、復元時に照合します。

「履歴・再現」で実行を選択して**「選択した実行の条件を復元」**を押すと、設定、モード、目標音圧、基準、保存波形を読み込みます。元WAVや生成コードが手元になくても、保存した `stimulus.npz` を使って同じデジタル波形を再測定できます。乱数生成関数は再実行しません。復元だけでは測定を開始せず、「校正を開始」で新しい記録を作ります。再測定の記録には復元元も残します。

GUI操作の開始・完了・失敗・停止要求、設定/基準の読込・保存は、保存フォルダの `sessions/<セッションID>/events/` に時刻付きJSONで保存します。波形確認時の波形も同セッション内に保存します。「操作ログを開く」から確認できます。入力欄の1文字ごとの編集は記録せず、処理開始時の条件全体を記録します。

同じ条件と波形の復元は、物理的な音場や機器状態まで再現するものではありません。実機で再測定するときは、保存した機器・ゲイン・マイク位置を確認してください。旧版の成功記録も、結果と設定・基準・波形が揃っていれば復元できます。旧版にはファイル単位のSHA-256記録がないため、内部の整合性のみ確認します。

### マイク入力をリアルタイムに確認する

「マイクモニター」で「モニターを開始」を押します。約100 msごとに更新し、直近1秒の電圧波形を表示します。SRは設定画面と共通です。基準なしでは電圧を表示し、有効な基準があるときは設定帯域の**背景音を含む総音圧**を表示します。校正探索で使う背景差引き後の信号音圧とは定義が異なります。入力クリッピング時は警告し、そのフレームの音圧値は採用しません。

連続取得中はAOを0 Vに保ち、基準測定・波形校正との同時実行を禁止します。画面下の「停止」でモニターを終了すると測定操作を再開できます。

`monitor-日時/` に設定・基準・機器情報、電圧/音圧の時系列 `levels.jsonl`、終了記録 `summary.json` を保存します。「生のマイク入力も保存する」は既定で有効で、連続ブロックを `capture-*.npz` に可逆保存します。各ファイルには `samples`（V）、`sample_rate_hz`、`first_sample` が入り、時系列には対応ファイルのSHA-256も記録します。無効にした場合もレベルと操作履歴は残ります。

## コマンドを使う場合

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
uv sync --locked --extra gui --extra dev
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
| `run.json` | 測定前に保存する入力条件・目標音圧・測定モード・波形ハッシュ・復元元 |
| `checksums.json` | 完了した記録ファイルのSHA-256。GUIの復元時に照合 |
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

自動テストでは基準感度、AMを含む音圧探索、帯域/窓/遅延、出力上限、SNR、クリッピング、GUIの操作・条件復元・停止・ライブモニター、保存記録の整合性、CLIの一連の操作を検証します。GUIのテストは `gui` extraが必要で、ヘッドレス環境では `QT_QPA_PLATFORM=offscreen uv run pytest` を使えます。**NI DAQ実機による検証は未実施です。** 共有先ではまず既知の基準信号と低い出力でクロック経路・入力方式・0 V復帰・実測値を確認してください。
