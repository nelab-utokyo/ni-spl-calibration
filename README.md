# NI SPL Calibration

[日本語](README.md) | [English](README.en.md)

WAV・生成関数・NumPy配列から、**指定した波形・区間を目標音圧で再生するための電圧倍率**を求めます。音響校正器でマイク入力系の感度を測り、NI DAQからの再生と取得で倍率を探索します。対象は有限長のモノラル出力、同一DAQのAI/AO各1チャンネルです。

## 導入

Windows x64版は[ReleasesからZIPをダウンロード](https://github.com/nelab-utokyo/ni-spl-calibration/releases/latest)し、全体を展開して `NI-SPL-Calibration.exe` を起動します。Python・uvは不要です。詳細は[Windows版の配布・ビルド](docs/windows.md)を参照してください。

ソース版はGit、Python 3.11以上、[uv](https://docs.astral.sh/uv/)を使用します。

```sh
git clone https://github.com/kmraven/ni-spl-calibration.git
cd ni-spl-calibration
uv sync --locked --extra gui --extra daq
uv run ni-spl
```

実機には、機種とOSに対応した[NI-DAQmxドライバ](https://nidaqmx-python.readthedocs.io/en/stable/)も必要です。実機用バックエンドはWindows/Linuxのドライバ対応環境を対象とします。

## 校正結果を実験で使う

成功記録の `stimulus.npz` と `result.json` を、実験プログラムから読み込みます。

```python
from pathlib import Path
from ni_spl_calibration import apply_calibration, load_result, load_stimulus

folder = Path("results/batch-実行日時/runs/実行ID")  # 実際の記録フォルダに変更
stimulus = load_stimulus(folder / "stimulus.npz")
result = load_result(folder / "result.json")

output_v = apply_calibration(stimulus, result)
sample_rate_hz = stimulus.sample_rate_hz
output_channel = result.setup["daq"]["output_channel"]
```

`output_v` は、保存された校正区間を切り出して `scale_v` を掛けた**V単位の配列**です。波形・SRのハッシュと出力上限も確認します。この配列を、記録と同じSR・AOチャンネルでDAQへ出力します。追加の正規化や音量倍率は掛けません。戻り値に遅延用の無音や終端0 Vは含まれないため、実験側で終了時の0 V復帰を実装してください。

校正値は、使用した波形・機器・アンプゲイン・音響経路・測定位置に依存します。波形や音響経路を変えたら再校正し、DAQ・入力系の設定を変えたらマイク基準も測り直してください。同じデジタルRMSでも、別の音への倍率の流用は保証されません。

## 音圧と保存記録の意味

校正結果の音圧は、**選択区間・帯域の背景パワーを差し引いた信号音圧**です。入力モニターのSPLは背景を含む総音圧で、スペクトラムのdBVは音圧ではなく帯域RMS電圧を表します。詳しい定義は[測定仕様](docs/measurement.md)を参照してください。

校正の記録は、元WAVや生成コードがなくても同じデジタル入力を再利用できる構成です。

| ファイル | 内容 |
|---|---|
| `result.json` | 再生倍率 `scale_v`（V/デジタル値）、達成音圧 `measured_spl_db`、目標、探索履歴 |
| `stimulus.npz` | 変換・チャンネル処理後の元波形全体、SR、入力元情報。校正区間は設定に保存 |
| `setup.json` / `reference.json` | 設定とマイク基準のスナップショット |
| `run.json` / `checksums.json` | 入力条件・復元元・ファイルのSHA-256 |
| `failure.json` | 失敗・中断時の理由と取得済みの探索履歴。成功結果は作成しない |

マイク確認・テスト再生では `levels.jsonl` に電圧・音圧・スペクトラムを記録し、テスト再生の適用音量も残します。生入力の記録を有効にした場合は `capture-*.npz` に入力波形、テスト再生時は出力電圧も保存します。GUIの操作履歴は `sessions/<セッションID>/events/` に保存します。

保存波形や設定の再利用は、物理的な音場まで再現するものではありません。再測定時も機器・ゲイン・マイク位置を確認してください。

校正キューの記録は `batch-実行日時/` に保存します。`plan.json` と `waveforms/` が順番・刺激ごとの目標音圧・開始倍率・条件・固定波形を保持し、`plan-checksums.json` で整合性を確認します。`summary.json` / `results.csv` が全体の結果、`runs/<実行ID>/` が今回の校正記録、`prior-runs/<刺激ID>/` が引き継いだ記録です。実験プログラムには、成功した行の `output` が指すフォルダ内の `stimulus.npz` と `result.json` を渡します。`output` の相対パスは、このキューの記録フォルダが基準です。

キューはエラー・停止時に残りを保留し、中断された刺激は背景測定から再実行します。計画の復元では音を出さず、再開・再実行は新しい保存先を作ります。計画だけの保存先は `plan-実行日時/` です。元のWAVや生成コードがなくても復元できますが、実機での再測定には同じ機器・音響経路と有効なマイク基準が必要です。

位置比較の記録は `comparison-実行日時/` に保存します。`comparison.json` が位置名と元の校正記録への参照、`calibrations/<ID>/` が固定波形・校正結果・入力系感度のコピー、`measurements/<ID>.json` と `results.csv` が今回の音圧・SNR・差を保持します。差は「今回 − 校正時の実測音圧」で、元と同じ区間・帯域・遅延を使い、その位置で測った背景パワーを差し引きます。SNR不足・クリッピング・中断時の音圧と差は空欄です。出力倍率は固定し、位置名は元のセットアップとは別に保存します。生入力の保存はデフォルトOFFで、有効にした場合は `<ID>-background.npz` / `<ID>-input.npz` に保存します。

## 詳細資料

- [測定仕様・適用範囲](docs/measurement.md)：音圧の定義、区間・帯域・補正、再利用条件
- [CLI・生成関数・開発](docs/automation.md)：GUIを使わない実行、入力仕様、シミュレーションとテスト
- [Python APIの実行例](examples/python_api.py)
