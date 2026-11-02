# NI SPL Calibration

[日本語](README.md) | [English](README.en.md)

WAV・生成関数・NumPy配列から、**指定した波形・区間を目標音圧で再生するための電圧倍率**を求めます。音響校正器でマイク入力系の感度を測り、NI DAQからの再生と取得で倍率を探索します。対象は有限長のモノラル出力、同一DAQのAI/AO各1チャンネルです。

## 導入

Git、Python 3.11以上、[uv](https://docs.astral.sh/uv/)を使用します。

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

folder = Path("results/calibration-実行日時")  # 実際の記録フォルダに変更
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

## 詳細資料

- [測定仕様・適用範囲](docs/measurement.md)：音圧の定義、区間・帯域・補正、再利用条件
- [CLI・生成関数・開発](docs/automation.md)：GUIを使わない実行、入力仕様、シミュレーションとテスト
- [Python APIの実行例](examples/python_api.py)
