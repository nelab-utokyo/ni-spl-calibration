"""Japanese/English GUI messages and state-preserving widget translation.

Bindings describe display text only. User input, file paths and record data are
never used as translation keys or changed when the display language changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

ENGLISH = {
    "言語 / Language": "Language",
    "シミュレーション": "Simulation",
    "NI DAQ 実機": "NI DAQ hardware",
    "設定 → マイク基準測定 → 波形の音圧校正": "Setup → Microphone reference → Waveform calibration",
    "保存フォルダ": "Output folder",
    "フォルダを選択": "Browse folder",
    "設定を確認して、マイク基準測定へ進んでください。": "Check the setup, then measure or load a microphone reference.",
    "停止": "Stop",
    "停止待ち…": "Stopping…",
    "1  設定": "1  Setup",
    "2  マイク基準測定": "2  Microphone reference",
    "3  波形校正": "3  Waveform calibration",
    "4  履歴・再現": "4  History / restore",
    "5  マイクモニター": "5  Microphone monitor",
    "新規セットアップ": "New setup",
    "新規": "New",
    "設定を読み込む": "Load setup",
    "設定を保存": "Save setup",
    "マイク基準測定へ →": "Microphone reference →",
    "セットアップ": "Setup",
    "セットアップ名": "Setup name",
    "機器・ゲイン・測定位置の記録": "Equipment, gain and measurement geometry",
    "DAQと再生の共通設定": "DAQ and playback",
    "共通サンプルレート [Hz]": "Common sample rate [Hz]",
    "入力 AI チャンネル": "AI input channel",
    "出力 AO チャンネル": "AO output channel",
    "入力方式": "Input terminal configuration",
    "出力ピーク電圧上限 [V]": "Output peak voltage limit [V]",
    "DAQのAI/AO、WAVの変換先、生成関数に共通で使います。変更後は再基準測定が必要です。": "Used for DAQ AI/AO, WAV conversion and generators. A rate change requires a new reference.",
    "SRの指定はこの1か所です。WAVの元SRは自動取得し、必要に応じて変換します。": "Set the sample rate here once. WAV source rates are read automatically and converted when needed.",
    "音響校正器の基準": "Acoustic calibrator reference",
    "基準音圧 [dB SPL]": "Reference level [dB SPL]",
    "基準周波数 [Hz]": "Reference frequency [Hz]",
    "取得時間 [s]": "Capture duration [s]",
    "安定性の許容幅 [dB]": "Maximum stability span [dB]",
    "測定する区間・帯域": "Measurement window and band",
    "開始時刻 [s]": "Start time [s]",
    "終了時刻 [s]・空欄で末尾まで": "Stop time [s]; blank = waveform end",
    "帯域 [下限Hz, 上限Hz]・空欄で全帯域": "Band [low Hz, high Hz]; blank = full band",
    "音響・機器遅延 [s]": "Acoustic / equipment delay [s]",
    "目標音圧の許容誤差 [dB]": "Target tolerance [dB]",
    "探索・入力系の詳細": "Search and input details",
    "入力電圧の下限 [V]": "Input minimum [V]",
    "入力電圧の上限 [V]": "Input maximum [V]",
    "最低SNR [dB]": "Minimum SNR [dB]",
    "1回の最大上昇幅 [dB]": "Maximum upward step [dB]",
    "最大探索回数": "Maximum iterations",
    "目標内での確認回数": "Required confirmations",
    "初期倍率 [V / デジタル値]": "Initial scale [V / digital unit]",
    "入力クリップ判定比率": "Input clipping threshold fraction",
    "マイク補正 [[Hz, dB], …]": "Microphone corrections [[Hz, dB], …]",
    "例: [5000, 80000]": "Example: [5000, 80000]",
    "例: [[0, 0], [1000, 0], [100000, -2.0]]": "Example: [[0, 0], [1000, 0], [100000, -2.0]]",
    "目標音圧には数値を入力してください。": "Enter a numeric target SPL.",
    "目標音圧は−100〜180 dB SPLの有限値で指定してください。": "Target SPL must be finite and between -100 and 180 dB SPL.",
    "{key}: 入力値を確認してください": "{key}: check the input value",
    "波形を選択すると、ここに表示します": "Select a waveform to preview it here",
    "モニターを開始すると、ここに入力波形を表示します": "Start the monitor to see microphone input here",
    "校正器をマイクに装着して、入力系の感度を測定します。": "Fit the acoustic calibrator to the microphone to measure input-chain sensitivity.",
    "有効な基準記録を読み込めば、同じ設定で再利用できます。": "Load a compatible reference to reuse it with the same setup.",
    "未測定": "Not measured",
    "入力系の感度 [V/Pa]": "Input-chain sensitivity [V/Pa]",
    "時間変動の幅 [dB]": "Temporal span [dB]",
    "基準記録を読み込むか、新しく測定してください。": "Load an existing reference or measure a new one.",
    "基準を測定して保存": "Measure and save reference",
    "基準記録を読み込む": "Load reference",
    "マイク入力を確認": "View microphone input",
    "波形校正へ →": "Waveform calibration →",
    "WAVファイル": "WAV file",
    "保存波形 (NPZ)": "Saved waveform (NPZ)",
    "Python生成関数": "Python generator",
    "再生するモノラルWAVを選択してください": "Select a mono WAV to play",
    "ファイルを選択": "Browse file",
    "関数名": "Function name",
    "指定したPythonコードを実行します。": "Runs the selected Python code.",
    "省略可。生成関数の引数YAML（SRは設定画面で指定）": "Optional generator parameters YAML (set the sample rate in Setup)",
    "パラメータ": "Parameters",
    "YAMLを選択": "Browse YAML",
    "波形を確認": "Preview waveform",
    "波形が未選択です。": "No waveform selected.",
    "校正を開始": "Start calibration",
    "先に基準記録を測定・読み込みしてください。": "Measure or load a reference first.",
    "目標音圧 [dB SPL]": "Target SPL [dB SPL]",
    "実測音圧 [dB SPL]": "Measured SPL [dB SPL]",
    "電圧倍率 [V / デジタル値]": "Scale [V / digital unit]",
    "出力ピーク [V]": "Output peak [V]",
    "探索回": "Iteration",
    "誤差 [dB]": "Error [dB]",
    "校正結果と再生用波形を、保存フォルダ内に新しい名前で保存します。": "Results and playback samples are saved under a new name in the output folder.",
    "結果フォルダを開く": "Open result folder",
    "エラー: {details}": "Error: {details}",
    "処理を完了できませんでした": "Unable to complete the operation",
    "過去の実行から、設定・波形・基準・目標音圧をまとめて復元できます。": "Restore setup, waveform, reference and target SPL together from a previous run.",
    "保存波形をそのまま使うので、乱数を含む生成関数も再実行しません。復元後に「校正を開始」を押すと、新しいフォルダへ再測定結果を保存します。": "Saved samples are reused without rerunning random generators. After restoring, click Start calibration to save a new measurement in a new folder.",
    "履歴を更新": "Refresh history",
    "記録フォルダを選んで復元": "Restore from folder",
    "操作ログを開く": "Open activity log",
    "実施日時 (UTC)": "Date/time (UTC)",
    "状態": "Status",
    "目標 [dB SPL]": "Target [dB SPL]",
    "測定モード": "Mode",
    "保存フォルダ内の実行履歴を表示します。": "Run history in the output folder.",
    "選択した実行の条件を復元": "Restore selected run",
    "成功": "Complete",
    "失敗": "Failed",
    "停止しました。": "Stopped.",
    "未完了": "Incomplete",
    "読込不可": "Unreadable",
    "{count}件の実行記録。復元時にファイルのSHA-256を照合します。": "{count} runs. SHA-256 hashes are verified when restoring.",
    "復元する実行記録を選択してください。": "Select a run to restore.",
    "実行記録のフォルダを選択": "Select a run folder",
    "マイク入力を約100 msごとに更新します。AOは0 Vに保ちます。": "Microphone input updates approximately every 100 ms. AO stays at 0 V.",
    "有効な基準記録があれば、設定帯域の総音圧を表示します（背景音を含みます）。基準測定・波形校正を行うときは、先にモニターを停止してください。": "With a valid reference, total SPL in the configured band is shown, including background. Stop monitoring before measuring a reference or calibrating.",
    "モニターを開始": "Start monitor",
    "生のマイク入力も保存する (NPZ)": "Save raw microphone input (NPZ)",
    "停止中。基準がなくても電圧波形を確認できます。": "Stopped. Voltage can be monitored without a reference.",
    "入力RMS [V]": "Input RMS [V]",
    "入力ピーク [V]": "Input peak [V]",
    "総音圧 [dB SPL]": "Total SPL [dB SPL]",
    "マイク入力の直近1秒 [V] · 縦軸は自動スケール": "Latest second of microphone input [V] · vertical axis autoscales",
    "開始・停止・電圧・音圧の時系列は保存フォルダに記録します。": "Start/stop events and voltage/SPL time series are saved in the output folder.",
    "記録フォルダを開く": "Open recording folder",
    "未校正": "Uncalibrated",
    "無信号": "No signal",
    "モニター中 · 共通SR: {rate:,} Hz": "Monitoring · common sample rate: {rate:,} Hz",
    "入力クリッピング：入力範囲・コンディショナのゲインを確認してください。": "Input clipping: check the voltage range and conditioner gain.",
    "クリップ": "Clipping",
    "モニター中 · {duration:.1f} s · {rate:,} Hz": "Monitoring · {duration:.1f} s · {rate:,} Hz",
    "条件を復元しました。設定・機器・測定位置を確認し、「校正を開始」で再測定できます。": "Conditions restored. Check the setup, equipment and geometry, then click Start calibration to remeasure.",
    "操作を記録するため、保存フォルダを指定してください。": "Select an output folder to record operations.",
    "測定・波形確認などの操作を行うと、操作ログを保存します。": "Activity is logged when you perform operations such as measurement or waveform preview.",
    "結果の保存フォルダ": "Output folder",
    "設定を読み込みました。": "Setup loaded.",
    "設定を保存しました。": "Setup saved.",
    "基準記録を読み込みました。波形校正へ進めます。": "Reference loaded. You can proceed to waveform calibration.",
    "基準記録の測定モードが異なります。同じモードで基準を測り直してください。": "Reference mode differs. Measure a new reference in the current mode.",
    "シミュレーション：合成信号で動作確認します。実機の音圧校正には使えません。": "Simulation: synthetic signals for software checks. Results cannot be used for physical calibration.",
    "NI DAQ 実機：設定したAI/AOで測定します。開始前に接続と測定位置を確認してください。": "NI DAQ hardware: measurements use the configured AI/AO. Check connections and geometry before starting.",
    "波形を確認するか、そのまま校正を開始してください。": "Preview the waveform or start calibration directly.",
    "設定を確認してください: {error}": "Check the setup: {error}",
    "設定の入力を確認してください。": "Check the setup fields.",
    "共通SR: {rate:,} Hz  ·  WAVはこのレートへ自動変換": "Common sample rate: {rate:,} Hz  ·  WAV files are converted to this rate",
    "校正器: {level:g} dB SPL / {frequency:g} Hz\n取得: {duration:g} s  ·  AI: {channel}  ·  共通SR: {rate:,} Hz  ·  AOは0 Vを維持": "Calibrator: {level:g} dB SPL / {frequency:g} Hz\nCapture: {duration:g} s  ·  AI: {channel}  ·  Rate: {rate:,} Hz  ·  AO stays at 0 V",
    "未測定：基準を測定するか、既存の記録を読み込んでください。": "Not measured: measure or load a reference.",
    "使用可能：現在の設定・測定モードと一致しています。": "Ready: matches the current setup and mode.",
    "再基準測定が必要：設定または測定モードが変わっています。": "New reference required: setup or mode has changed.",
    "目標音圧の入力を確認してください。": "Check the target SPL.",
    "準備完了": "Ready",
    "波形を選択してください。": "Select a waveform.",
    "有効なマイク基準記録が必要です。": "A valid microphone reference is required.",
    "保存波形 (*.npz)": "Saved waveform (*.npz)",
    "波形・生成関数を選択": "Select waveform or generator",
    "生成関数のパラメータ": "Generator parameters",
    "保存フォルダを選択してください。": "Select an output folder.",
    "保存先にはフォルダを指定してください。": "The output path must be a folder.",
    "校正器を装着し、{level:g} dB SPL / {frequency:g} Hzで動作させてください。AOは0 Vです。": "Fit the calibrator and set it to {level:g} dB SPL / {frequency:g} Hz. AO stays at 0 V.",
    "校正器を外し、マイクを実際の測定位置に置いてください。スピーカーから音が出ます。": "Remove the calibrator and position the microphone for measurement. The speaker will emit sound.",
    "{instruction}\n\nAI: {input} / AO: {output}\nSR: {rate:,} Hz / 出力上限: {peak:g} Vpeak\n\n動物・聴取者がいないことを確認して開始してください。": "{instruction}\n\nAI: {input} / AO: {output}\nSample rate: {rate:,} Hz / Output limit: {peak:g} Vpeak\n\nStart only after confirming no animals or listeners are exposed.",
    "実機測定を開始": "Start hardware measurement",
    "先にマイク基準を測定・読み込みしてください。": "Measure or load a microphone reference first.",
    "別の処理が実行中です。": "Another operation is running.",
    "波形を準備しています…": "Preparing waveform…",
    "校正用の波形を準備しています…": "Preparing the calibration waveform…",
    "マイク基準を測定しています…": "Measuring microphone reference…",
    "目標音圧になる電圧倍率を探索しています…": "Searching for the output scale at the target SPL…",
    "実行記録を照合して、条件を復元しています…": "Verifying the run and restoring conditions…",
    "マイク入力をモニターしています。停止ボタンで終了します。": "Monitoring microphone input. Click Stop to finish.",
    "SNR不足": "Insufficient SNR",
    "未完了の記録: {path}": "Incomplete run: {path}",
    "モニターを停止しました。": "Monitor stopped.",
    "モニターを停止しました。エラーの詳細を確認してください。": "Monitor stopped. Check the error details.",
    "波形を確認しました。校正ではこの波形を固定して使います。": "Waveform ready. Calibration will use these exact samples.",
    "基準を保存しました。校正器を外し、波形校正へ進んでください。": "Reference saved. Remove the calibrator and proceed to waveform calibration.",
    "停止しました · 記録時間 {duration:.1f} s": "Stopped · recorded {duration:.1f} s",
    "マイクモニターを停止し、記録を保存しました。": "Microphone monitor stopped and recording saved.",
    "校正が完了しました。結果と再生用のstimulus.npzを保存しました。": "Calibration complete. Results and playback stimulus.npz saved.",
    "停止要求済み。操作ログの保存に失敗しました: {error}": "Stop requested. Activity log could not be saved: {error}",
    "停止要求済み：現在の取得・波形準備が終わり次第、安全に終了します。": "Stop requested: waiting for the current acquisition or waveform preparation to finish.",
    "未対応の実行記録です。": "Unsupported run record.",
    "保存記録の整合性チェックに失敗しました: {name}": "Record integrity check failed: {name}",
    "未対応の整合性記録です。": "Unsupported integrity record.",
    "保存記録の目標音圧が不正です。": "Invalid target SPL in the saved run.",
    "保存記録の測定モードが不正です。": "Invalid backend in the saved run.",
    "保存記録と基準記録の測定モードが一致しません。": "Run and reference modes do not match.",
    "保存波形とDAQのSRが一致しません。": "Saved waveform and DAQ sample rates do not match.",
    "保存波形のハッシュが実行記録と一致しません。": "Waveform hash does not match the run record.",
    "保存された結果と入力条件が一致しません。": "Saved results and inputs do not match.",
    "保存済みプレビュー波形のハッシュが一致しません。": "Saved preview waveform hash does not match.",
    "取得終了。最後の入力スペクトラムを表示しています。": "Capture finished. Showing the last input spectrum.",
    "入力スペクトラム・電圧・音圧は、どのタブでも画面下部に表示します。": "Input spectrum, voltage and SPL are always visible below the tabs.",
    "待機中 · 入力未取得": "Idle · no input yet",
    "入力待ち…": "Waiting for input…",
    "入力クリッピング": "Input clipping",
    "取得中 · {rate:,} Hz": "Live · {rate:,} Hz",
    "停止中 · 最後の入力": "Stopped · last input",
    "停止中 · 入力未取得": "Stopped · no input",
    "モノラルのみ": "Mono only",
    "左右平均 (L+R)/2": "Average (L+R)/2",
    "左チャンネル L": "Left channel L",
    "右チャンネル R": "Right channel R",
    "WAVチャンネル処理": "WAV channels",
    "元WAV {channels} ch → AO 1 ch / {mode}": "Source WAV {channels} ch → AO 1 ch / {mode}",
    "AO 1 ch / モノラル波形": "AO 1 ch / mono waveform",
    "マイク入力スペクトラム [dBV RMS]": "Microphone spectrum [dBV RMS]",
    "モニターまたはテスト再生を開始してください": "Start monitoring or test playback",
    "テスト再生へ →": "Test playback →",
    "マイク入力を周波数ごとのバーで表示します。通常のモニター中はAOを0 Vに保ちます。": "Microphone input is displayed as frequency bars. Standalone monitoring keeps AO at 0 V.",
    "横軸は周波数（対数）、縦軸は帯域RMSのdBVです。音声を加工する機能ではありません。": "Frequency is logarithmic; levels are band RMS in dBV. This display does not alter the audio.",
    "帯域計算不可": "Band unavailable",
    "6  テスト再生": "6  Test playback",
    "選択した音声をAOから再生し、マイク入力を同時に確認します。基準記録は不要です。": "Play the selected waveform through AO and monitor the microphone simultaneously. No reference is required.",
    "再生する音声を選択してください。": "Select audio to play.",
    "音声を選択": "Select audio",
    "1 kHzテスト音を選択": "Select 1 kHz test tone",
    "WAV・NPZを選ぶか、波形校正画面で生成関数を指定してください。": "Choose WAV/NPZ, or select a generator in Waveform calibration.",
    "接続機種・AOチャンネル・指令電圧を再生時に表示します。": "Device, AO channel and commanded voltage are displayed during playback.",
    "テスト再生を開始": "Start test playback",
    "再生停止": "Stop playback",
    "繰り返し再生": "Loop",
    "AO・マイクの生データを保存": "Save raw AO and microphone data",
    "ミュート": "Mute",
    "音量フェーダー": "Volume fader",
    "100%は設定したVpeak上限です。校正済み音圧の指定ではありません。": "100% corresponds to the configured Vpeak limit. This is not a calibrated SPL setting.",
    "AO指令ピーク [V]": "Commanded AO peak [V]",
    "停止中。音声を選択して再生を開始してください。": "Stopped. Select audio and start playback.",
    "{volume:.1f}%\n上限 {peak:.6g} Vpeak": "{volume:.1f}%\nLimit {peak:.6g} Vpeak",
    "テスト再生する音声を選択": "Select audio for test playback",
    "接続: {product}\nAI {input} / AO {output} / {rate:,} Hz / 出力上限 {limit:g} Vpeak": "Connected: {product}\nAI {input} / AO {output} / {rate:,} Hz / Limit {limit:g} Vpeak",
    "再生中 · {duration:.2f} s · 音量 {volume:.1f}%": "Playing · {duration:.2f} s · Volume {volume:.1f}%",
    "デジタル振幅: 変換前 RMS {before_rms:.6g} / Peak {before_peak:.6g} → 変換後 RMS {after_rms:.6g} / Peak {after_peak:.6g}（{change:+.3f} dB）": "Digital amplitude: before RMS {before_rms:.6g} / Peak {before_peak:.6g} → after RMS {after_rms:.6g} / Peak {after_peak:.6g} ({change:+.3f} dB)",
    "変換後のデジタル振幅: RMS {rms:.6g} / Peak {peak:.6g}": "Converted digital amplitude: RMS {rms:.6g} / Peak {peak:.6g}",
    "テスト再生用の音声を準備しています…": "Preparing audio for test playback…",
    "テスト再生中です。フェーダーで調節し、停止ボタンで中断できます。": "Test playback is running. Adjust the fader or press Stop.",
    "再生を停止しました。記録とエラーを確認してください。": "Playback stopped. Check the recording and error details.",
    "出力確認": "Check output",
    "再生終了 · {duration:.2f} s · 保存先 {path}": "Playback finished · {duration:.2f} s · Saved to {path}",
    "テスト再生を終了し、AOの停止・0 V指令と記録保存を完了しました。": "Test playback finished. AO was stopped, 0 V commanded and records saved.",
    "停止要求済み：短い取得単位で中断し、AOを0 Vへ戻します。": "Stop requested: interrupting at the next short acquisition boundary and returning AO to 0 V.",
    "停止要求済み：波形準備の完了後に終了します。": "Stop requested: waiting for waveform preparation to finish.",
}


@dataclass(frozen=True)
class Message:
    source: str
    values: dict = field(default_factory=dict)

    def render(self, language):
        template = ENGLISH.get(self.source, self.source) if language == "en" else self.source
        values = {
            key: value.render(language) if isinstance(value, Message) else value
            for key, value in self.values.items()
        }
        return template.format(**values) if values else template


def error_message(error):
    source = str(error)
    source = next((ja for ja, en in ENGLISH.items() if en == source), source)
    prefix = "保存記録の整合性チェックに失敗しました: "
    if source.startswith(prefix):
        return Message(
            "保存記録の整合性チェックに失敗しました: {name}", {"name": source[len(prefix) :]}
        )
    suffix = ": 入力値を確認してください"
    if source.endswith(suffix):
        return Message("{key}: 入力値を確認してください", {"key": source[: -len(suffix)]})
    suffix = ": check the input value"
    if source.endswith(suffix):
        return Message("{key}: 入力値を確認してください", {"key": source[: -len(suffix)]})
    return Message(source)


def language_of(widget):
    window = widget.window()
    selector = getattr(window, "language", None)
    return selector.currentData() if selector is not None else "ja"


def translate(widget, source, **values):
    return Message(source, values).render(language_of(widget))


def bind(widget, setter, source, *args, scope=None, **values):
    bindings = getattr(widget, "_text_bindings", None)
    if bindings is None:
        bindings = {}
        widget._text_bindings = bindings
    message = Message(source, values)
    bindings[setter, args] = message
    language = language_of(scope if scope is not None else widget)
    getattr(widget, setter)(*args, message.render(language))


def set_text(widget, source, **values):
    bind(widget, "setText", source, **values)


def set_value(widget, value):
    # Protect user-provided names/paths from coinciding with a translation key.
    set_text(widget, "{value}", value=value)


def set_headers(table, sources):
    for index, source in enumerate(sources):
        item = QTableWidgetItem()
        bind(item, "setText", source, scope=table)
        table.setHorizontalHeaderItem(index, item)


def set_cell(table, row, column, source, **values):
    item = QTableWidgetItem()
    bind(item, "setText", source, scope=table, **values)
    table.setItem(row, column, item)
    return item


def _retranslate_object(obj, language):
    for (setter, args), message in getattr(obj, "_text_bindings", {}).items():
        getattr(obj, setter)(*args, message.render(language))


def retranslate(window):
    language = language_of(window)
    for widget in (window, *window.findChildren(QObject)):
        _retranslate_object(widget, language)
    for table in window.findChildren(QTableWidget):
        for column in range(table.columnCount()):
            item = table.horizontalHeaderItem(column)
            if item is not None:
                _retranslate_object(item, language)
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    _retranslate_object(item, language)
