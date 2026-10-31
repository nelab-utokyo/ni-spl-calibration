"""Qt desktop interface. All DAQ operations run in a single worker thread."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Event

import numpy as np
import yaml
from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .calibration import (
    MeasurementCancelled,
    load_reference,
    measurement_bounds,
    validate_reference,
)
from .config import DaqSettings, MeasurementSettings, Setup, load_setup
from .monitor import monitor_input
from .records import ActivityLog, file_sha256, find_runs, load_replay
from .workflow import (
    StimulusSource,
    calibrate_to_directory,
    make_hardware,
    measure_reference_to_file,
)

STYLE = """
QWidget { font-size: 13px; color: #1e293b; }
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #f5f7fb; }
QLabel#title { font-size: 25px; font-weight: 700; color: #132d46; }
QLabel#subtitle, QLabel#muted { color: #596b80; }
QLabel#banner { background: #e7f0fa; color: #1f5688; padding: 10px; border-radius: 6px; }
QLabel#metric { font-size: 25px; font-weight: 600; color: #164e80; }
QFrame#metricCard { background: #f0f5fc; border: 1px solid #dbe5f2; border-radius: 8px; }
QGroupBox { background: white; border: 1px solid #dce3ec; border-radius: 8px;
            margin-top: 12px; padding: 16px 12px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox {
    background: white; border: 1px solid #cbd5e1; border-radius: 5px; padding: 7px;
    selection-background-color: #c8ddf6;
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #327aba; }
QPushButton { background: white; border: 1px solid #cbd5e1; border-radius: 5px;
              padding: 8px 14px; font-weight: 500; }
QPushButton:hover { background: #eaf1fa; border-color: #7b9cbe; }
QPushButton#primary { color: white; background: #205e96; border-color: #205e96; }
QPushButton#primary:hover { background: #174c7d; }
QPushButton#stop { color: #a73338; border-color: #d5a4a6; }
QPushButton:disabled { color: #94a3b8; background: #f1f4f8; border-color: #e0e6ee; }
QPushButton#primary:disabled, QPushButton#stop:disabled {
    color: #94a3b8; background: #f1f4f8; border-color: #e0e6ee;
}
QLineEdit:disabled, QComboBox:disabled { color: #718096; background: #f1f4f8; }
QTabWidget::pane { border: 1px solid #dce3ec; background: #f5f7fb; border-radius: 6px; }
QTabBar::tab { padding: 12px 28px; color: #64748b; }
QTabBar::tab:selected { color: #164e80; background: #e4edf8; font-weight: 600; }
QTableWidget { background: white; border: 1px solid #dce3ec; border-radius: 5px;
               gridline-color: #edf1f6; }
QHeaderView::section { background: #edf2f8; padding: 7px; border: none; color: #52657c; }
QProgressBar { border: none; background: #e2e8f0; border-radius: 4px; max-height: 8px; }
QProgressBar::chunk { background: #327aba; border-radius: 4px; }
"""


def label(text, *, muted=False):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if muted:
        widget.setObjectName("muted")
    return widget


def button(text, callback, *, primary=False):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    if primary:
        widget.setObjectName("primary")
    return widget


def row(*widgets):
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    return container


def metric(title):
    card = QFrame()
    card.setObjectName("metricCard")
    card.setMinimumHeight(78)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.addWidget(label(title, muted=True))
    value = label("—")
    value.setObjectName("metric")
    layout.addWidget(value)
    return card, value


def new_setup():
    return Setup("my-setup", "マイク・校正器の個体、ゲイン、配線、スピーカー、測定位置を記録")


class TargetInput(QLineEdit):
    valueChanged = Signal()

    def __init__(self):
        super().__init__("70")
        self.textChanged.connect(lambda *_: self.valueChanged.emit())

    def value(self):
        try:
            value = float(self.text())
        except ValueError as error:
            raise ValueError("目標音圧には数値を入力してください。") from error
        if not math.isfinite(value) or not -100 <= value <= 180:
            raise ValueError("目標音圧は−100〜180 dB SPLの有限値で指定してください。")
        return value

    def setValue(self, value):
        self.setText(str(value))


class SetupEditor(QScrollArea):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.fields = {}
        self.types = {}
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.addWidget(
            self._section(
                "セットアップ",
                [
                    ("setup_id", "セットアップ名", "str"),
                    ("chain_notes", "機器・ゲイン・測定位置の記録", "notes"),
                ],
            )
        )
        layout.addWidget(
            self._section(
                "DAQと再生の共通設定",
                [
                    ("daq.sample_rate_hz", "共通サンプルレート [Hz]", "int"),
                    ("daq.input_channel", "入力 AI チャンネル", "str"),
                    ("daq.output_channel", "出力 AO チャンネル", "str"),
                    ("daq.terminal_configuration", "入力方式", "terminal"),
                    ("daq.max_peak_v", "出力ピーク電圧上限 [V]", "float"),
                ],
            )
        )
        self.fields["daq.sample_rate_hz"].setToolTip(
            "DAQのAI/AO、WAVの変換先、生成関数に共通で使います。変更後は再基準測定が必要です。"
        )
        layout.addWidget(
            label(
                "SRの指定はこの1か所です。WAVの元SRは自動取得し、必要に応じて変換します。",
                muted=True,
            )
        )
        layout.addWidget(
            self._section(
                "音響校正器の基準",
                [
                    ("reference_level_db", "基準音圧 [dB SPL]", "float"),
                    ("reference_frequency_hz", "基準周波数 [Hz]", "float"),
                    ("reference_duration_s", "取得時間 [s]", "float"),
                    ("reference_max_span_db", "安定性の許容幅 [dB]", "float"),
                ],
            )
        )
        layout.addWidget(
            self._section(
                "測定する区間・帯域",
                [
                    ("measurement.start_s", "開始時刻 [s]", "float"),
                    ("measurement.stop_s", "終了時刻 [s]・空欄で末尾まで", "optional_float"),
                    ("measurement.band_hz", "帯域 [下限Hz, 上限Hz]・空欄で全帯域", "json_optional"),
                    ("measurement.acoustic_delay_s", "音響・機器遅延 [s]", "float"),
                    ("measurement.tolerance_db", "目標音圧の許容誤差 [dB]", "float"),
                ],
            )
        )
        layout.addWidget(
            self._section(
                "探索・入力系の詳細",
                [
                    ("daq.input_min_v", "入力電圧の下限 [V]", "float"),
                    ("daq.input_max_v", "入力電圧の上限 [V]", "float"),
                    ("measurement.minimum_snr_db", "最低SNR [dB]", "float"),
                    ("measurement.max_upward_step_db", "1回の最大上昇幅 [dB]", "float"),
                    ("measurement.max_iterations", "最大探索回数", "int"),
                    ("measurement.confirmations", "目標内での確認回数", "int"),
                    ("measurement.start_scale_v", "初期倍率 [V / デジタル値]", "float"),
                    ("measurement.input_clip_fraction", "入力クリップ判定比率", "float"),
                    (
                        "measurement.frequency_correction_db",
                        "マイク補正 [[Hz, dB], …]",
                        "json_list",
                    ),
                ],
            )
        )
        self.fields["measurement.band_hz"].setPlaceholderText("例: [5000, 80000]")
        self.fields["measurement.frequency_correction_db"].setPlaceholderText(
            "例: [[0, 0], [1000, 0], [100000, -2.0]]"
        )
        layout.addStretch()
        self.setWidget(content)
        self.set_setup(new_setup())

    def _section(self, title, specs):
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(10)
        for key, title, kind in specs:
            if kind == "terminal":
                widget = QComboBox()
                widget.addItems(["differential", "rse", "nrse", "pseudo_differential"])
                widget.currentTextChanged.connect(lambda *_: self.changed.emit())
            elif kind == "notes":
                widget = QPlainTextEdit()
                widget.setMaximumHeight(72)
                widget.textChanged.connect(self.changed.emit)
            else:
                widget = QLineEdit()
                widget.textChanged.connect(lambda *_: self.changed.emit())
            widget.setObjectName(key)
            form.addRow(title, widget)
            self.fields[key] = widget
            self.types[key] = kind
        return group

    def set_setup(self, setup):
        blocker = QSignalBlocker(self)
        payload = asdict(setup)
        for key, widget in self.fields.items():
            value = payload
            for part in key.split("."):
                value = value[part]
            kind = self.types[key]
            if kind == "terminal":
                widget.setCurrentText(value)
            elif kind == "notes":
                widget.setPlainText(value)
            elif kind.startswith("json"):
                widget.setText("" if value is None or value == () else json.dumps(value))
            else:
                widget.setText("" if value is None else str(value))
        del blocker
        self.changed.emit()

    def setup(self):
        payload = {"daq": {}, "measurement": {}}
        for key, widget in self.fields.items():
            kind = self.types[key]
            if kind == "terminal":
                text = widget.currentText()
            elif kind == "notes":
                text = widget.toPlainText().strip()
            else:
                text = widget.text().strip()
            try:
                if kind == "int":
                    value = int(text)
                elif kind in {"float", "optional_float"}:
                    value = None if kind == "optional_float" and not text else float(text)
                elif kind.startswith("json"):
                    value = json.loads(text) if text else (None if kind == "json_optional" else [])
                else:
                    value = text
            except (ValueError, TypeError) as error:
                raise ValueError(f"{key}: 入力値を確認してください") from error
            if "." in key:
                section, name = key.split(".")
                payload[section][name] = value
            else:
                payload[key] = value
        payload["daq"] = DaqSettings(**payload["daq"])
        payload["measurement"] = MeasurementSettings(**payload["measurement"])
        return Setup(**payload)


class WaveformView(QWidget):
    def __init__(self):
        super().__init__()
        self.stimulus = None
        self.setMinimumHeight(135)
        self.samples = None
        self.sample_rate_hz = 1
        self.axis_peak = 1.0

    def set_stimulus(self, stimulus):
        self.stimulus = stimulus
        self.samples = stimulus.samples if stimulus is not None else None
        self.sample_rate_hz = stimulus.sample_rate_hz if stimulus is not None else 1
        self.axis_peak = 1.0
        self.update()

    def set_input(self, samples, sample_rate_hz):
        self.samples = samples
        self.sample_rate_hz = sample_rate_hz
        self.axis_peak = max(1e-6, float(np.max(np.abs(samples))) * 1.1)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left = max(46, painter.fontMetrics().horizontalAdvance(f"{-self.axis_peak:.2g}") + 10)
        area = QRectF(left, 12, max(1, self.width() - left - 16), self.height() - 40)
        painter.fillRect(area, QColor("#f3f7fc"))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawLine(area.bottomLeft(), area.bottomRight())
        painter.drawLine(
            QPointF(area.left(), area.center().y()), QPointF(area.right(), area.center().y())
        )
        painter.setPen(QColor("#64748b"))
        painter.drawText(QPointF(2, area.top() + 10), f"{self.axis_peak:.2g}")
        painter.drawText(QPointF(8, area.center().y() + 4), "0")
        painter.drawText(QPointF(2, area.bottom()), f"{-self.axis_peak:.2g}")
        if self.samples is None:
            painter.drawText(
                area, Qt.AlignmentFlag.AlignCenter, "波形を選択すると、ここに表示します"
            )
            return
        samples = self.samples
        bins = min(len(samples), max(1, int(area.width())))
        edges = np.linspace(0, len(samples), bins + 1, dtype=int)
        maxima = np.maximum.reduceat(samples, edges[:-1])
        minima = np.minimum.reduceat(samples, edges[:-1])
        painter.setPen(QPen(QColor("#327aba"), 1))
        for index in range(bins):
            x = area.left() + (index + 0.5) * area.width() / bins
            high = area.center().y() - float(maxima[index]) / self.axis_peak * area.height() / 2
            low = area.center().y() - float(minima[index]) / self.axis_peak * area.height() / 2
            painter.drawLine(QPointF(x, high), QPointF(x, low + 0.5))
        painter.setPen(QColor("#64748b"))
        painter.drawText(QPointF(area.left(), self.height() - 6), "0 s")
        duration = len(samples) / self.sample_rate_hz
        painter.drawText(QPointF(area.right() - 90, self.height() - 6), f"{duration:.3f} s")


class Operation(QThread):
    progress = Signal(dict)

    def __init__(self, task, parent):
        super().__init__(parent)
        self.task = task
        self.cancel_event = Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.task(self.cancel_event.is_set, self.progress.emit)
        except (Exception, KeyboardInterrupt) as error:
            self.error = error


class CalibrationWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NI SPL Calibration")
        self.resize(1160, 890)
        self.setMinimumSize(960, 720)
        self.worker = None
        self.reference = None
        self.stimulus = None
        self.last_output = None
        self.last_monitor_output = None
        self._closing = False
        self.activity = ActivityLog()
        self._parent_run = None
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        title = label("NI SPL Calibration")
        title.setObjectName("title")
        self.mode = QComboBox()
        self.mode.addItems(["シミュレーション", "NI DAQ 実機"])
        self.mode.setMinimumWidth(190)
        self.mode.setMaximumWidth(240)
        layout.addWidget(row(title, self.mode))
        layout.addWidget(label("設定 → マイク基準測定 → 波形の音圧校正", muted=True))
        self.banner = label("")
        self.banner.setObjectName("banner")
        layout.addWidget(self.banner)
        self.output_root = QLineEdit(str(Path.cwd() / "results"))
        layout.addWidget(
            row(
                label("保存フォルダ"),
                self.output_root,
                button("フォルダを選択", self._choose_output),
            )
        )
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._settings_tab()
        self._reference_tab()
        self._waveform_tab()
        self._history_tab()
        self._monitor_tab()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = label("設定を確認して、マイク基準測定へ進んでください。", muted=True)
        self.stop_button = button("停止", self._request_stop)
        self.stop_button.setObjectName("stop")
        self.stop_button.setMaximumWidth(120)
        self.stop_button.setEnabled(False)
        layout.addWidget(row(self.status, self.stop_button))
        self.editor.changed.connect(self._setup_changed)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.source_kind.currentIndexChanged.connect(self._source_changed)
        self.source_location.textChanged.connect(self._source_changed)
        self.function_name.textChanged.connect(self._source_changed)
        self.parameters_path.textChanged.connect(self._source_changed)
        self.target.valueChanged.connect(self._target_changed)
        self._source_changed()
        self._mode_changed()
        self._scan_history()

    @property
    def simulate(self):
        return self.mode.currentIndex() == 0

    def _page(self, title, *, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            area.setWidget(page)
            self.tabs.addTab(area, title)
        else:
            self.tabs.addTab(page, title)
        return layout

    def _settings_tab(self):
        layout = self._page("1  設定")
        self.setup_path = label("新規セットアップ", muted=True)
        layout.addWidget(
            row(
                self.setup_path,
                button("新規", self._new_setup),
                button("設定を読み込む", self._load_setup),
                button("設定を保存", self._save_setup),
            )
        )
        self.editor = SetupEditor()
        layout.addWidget(self.editor, 1)
        layout.addWidget(
            button("マイク基準測定へ →", lambda: self.tabs.setCurrentIndex(1), primary=True)
        )

    def _reference_tab(self):
        layout = self._page("2  マイク基準測定")
        layout.addWidget(label("校正器をマイクに装着して、入力系の感度を測定します。"))
        layout.addWidget(
            label("有効な基準記録を読み込めば、同じ設定で再利用できます。", muted=True)
        )
        self.reference_instruction = label("")
        layout.addWidget(self.reference_instruction)
        self.reference_status = label("未測定")
        layout.addWidget(self.reference_status)
        sensitivity_card, self.sensitivity_value = metric("入力系の感度 [V/Pa]")
        frequency_card, self.frequency_value = metric("基準周波数 [Hz]")
        stability_card, self.stability_value = metric("時間変動の幅 [dB]")
        layout.addWidget(row(sensitivity_card, frequency_card, stability_card))
        self.reference_file = label("基準記録を読み込むか、新しく測定してください。", muted=True)
        self.reference_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.reference_file)
        self.measure_button = button("基準を測定して保存", self._measure_reference, primary=True)
        layout.addWidget(
            row(
                self.measure_button,
                button("基準記録を読み込む", self._load_reference),
                button("マイク入力を確認", lambda: self.tabs.setCurrentIndex(4)),
            )
        )
        layout.addStretch()
        layout.addWidget(button("波形校正へ →", lambda: self.tabs.setCurrentIndex(2), primary=True))

    def _waveform_tab(self):
        layout = self._page("3  波形校正", scroll=True)
        self.shared_rate = label("")
        layout.addWidget(self.shared_rate)
        self.source_kind = QComboBox()
        self.source_kind.addItems(["WAVファイル", "保存波形 (NPZ)", "Python生成関数"])
        self.source_kind.setMaximumWidth(180)
        self.source_location = QLineEdit()
        self.source_location.setPlaceholderText("再生するモノラルWAVを選択してください")
        layout.addWidget(
            row(
                self.source_kind,
                self.source_location,
                button("ファイルを選択", self._choose_source),
            )
        )
        self.function_name = QLineEdit("make_sound")
        self.generator_row = row(
            label("関数名"),
            self.function_name,
            label("指定したPythonコードを実行します。", muted=True),
        )
        layout.addWidget(self.generator_row)
        self.parameters_path = QLineEdit()
        self.parameters_path.setPlaceholderText("省略可。生成関数の引数YAML（SRは設定画面で指定）")
        self.parameters_row = row(
            label("パラメータ"), self.parameters_path, button("YAMLを選択", self._choose_parameters)
        )
        layout.addWidget(self.parameters_row)
        self.preview_button = button("波形を確認", self._prepare)
        self.preview_button.setMaximumWidth(160)
        self.stimulus_info = label("波形が未選択です。", muted=True)
        layout.addWidget(row(self.preview_button, self.stimulus_info))
        self.waveform = WaveformView()
        layout.addWidget(self.waveform)
        self.target = TargetInput()
        self.target.setMinimumWidth(160)
        self.target.setMaximumWidth(180)
        self.calibrate_button = button("校正を開始", self._calibrate, primary=True)
        self.calibration_hint = label("先に基準記録を測定・読み込みしてください。", muted=True)
        layout.addWidget(
            row(
                label("目標音圧 [dB SPL]"),
                self.target,
                self.calibrate_button,
                self.calibration_hint,
            )
        )
        level_card, self.level_value = metric("実測音圧 [dB SPL]")
        scale_card, self.scale_value = metric("電圧倍率 [V / デジタル値]")
        peak_card, self.peak_value = metric("出力ピーク [V]")
        layout.addWidget(row(level_card, scale_card, peak_card))
        self.iterations = QTableWidget(0, 4)
        self.iterations.setHorizontalHeaderLabels(
            ["探索回", "実測音圧 [dB SPL]", "出力ピーク [V]", "誤差 [dB]"]
        )
        self.iterations.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.iterations.verticalHeader().hide()
        self.iterations.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.iterations.setMinimumHeight(115)
        layout.addWidget(self.iterations, 1)
        self.result_file = label(
            "校正結果と再生用波形を、保存フォルダ内に新しい名前で保存します。", muted=True
        )
        self.result_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_result_button = button("結果フォルダを開く", self._open_result)
        self.open_result_button.setMaximumWidth(180)
        self.open_result_button.setEnabled(False)
        layout.addWidget(row(self.result_file, self.open_result_button))

    def _show_error(self, error):
        details = "\n".join([str(error), *getattr(error, "__notes__", [])])
        self.status.setText(f"エラー: {details}")
        QMessageBox.warning(self, "処理を完了できませんでした", details)

    def _history_tab(self):
        layout = self._page("4  履歴・再現")
        layout.addWidget(
            label("過去の実行から、設定・波形・基準・目標音圧をまとめて復元できます。")
        )
        layout.addWidget(
            label(
                "保存波形をそのまま使うので、乱数を含む生成関数も再実行しません。"
                "復元後に「校正を開始」を押すと、新しいフォルダへ再測定結果を保存します。",
                muted=True,
            )
        )
        layout.addWidget(
            row(
                button("履歴を更新", self._scan_history),
                button("記録フォルダを選んで復元", self._choose_replay),
                button("操作ログを開く", self._open_activity),
            )
        )
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(
            ["実施日時 (UTC)", "状態", "目標 [dB SPL]", "測定モード", "保存フォルダ"]
        )
        self.history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.verticalHeader().hide()
        layout.addWidget(self.history, 1)
        self.history_info = label("保存フォルダ内の実行履歴を表示します。", muted=True)
        layout.addWidget(self.history_info)
        layout.addWidget(button("選択した実行の条件を復元", self._restore_selected, primary=True))

    def _scan_history(self):
        def scan():
            records = find_runs(Path(self.output_root.text()).expanduser())
            self.history.setRowCount(0)
            states = {
                "complete": "成功",
                "failed": "失敗",
                "cancelled": "停止",
                "incomplete": "未完了",
                "invalid": "読込不可",
            }
            for record in records:
                index = self.history.rowCount()
                self.history.insertRow(index)
                values = [
                    record.get("created_at", "—"),
                    states.get(record["status"], record["status"]),
                    str(record.get("target_spl_db", "—")),
                    "シミュレーション" if record.get("backend") == "simulation" else "NI DAQ 実機",
                    record["directory"].name,
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, str(record["directory"]))
                    self.history.setItem(index, column, item)
            self.history_info.setText(
                f"{len(records)}件の実行記録。復元時にファイルのSHA-256を照合します。"
            )

        self._guard(scan)

    def _restore_selected(self):
        index = self.history.currentRow()
        if index < 0:
            self.status.setText("復元する実行記録を選択してください。")
            return
        self._restore(self.history.item(index, 0).data(Qt.ItemDataRole.UserRole))

    def _choose_replay(self):
        path = QFileDialog.getExistingDirectory(
            self, "実行記録のフォルダを選択", self.output_root.text()
        )
        if path:
            self._restore(path)

    def _restore(self, path):
        def task(cancelled, progress):
            if cancelled():
                raise MeasurementCancelled("Measurement cancelled")
            result = load_replay(path)
            if cancelled():
                raise MeasurementCancelled("Measurement cancelled")
            return result

        self._guard(lambda: self._start("restore", task))

    def _monitor_tab(self):
        layout = self._page("5  マイクモニター", scroll=True)
        layout.addWidget(label("マイク入力を約100 msごとに更新します。AOは0 Vに保ちます。"))
        layout.addWidget(
            label(
                "有効な基準記録があれば、設定帯域の総音圧を表示します（背景音を含みます）。"
                "基準測定・波形校正を行うときは、先にモニターを停止してください。",
                muted=True,
            )
        )
        self.monitor_start = button("モニターを開始", self._start_monitor, primary=True)
        self.save_monitor = QCheckBox("生のマイク入力も保存する (NPZ)")
        self.save_monitor.setChecked(True)
        layout.addWidget(row(self.monitor_start, self.save_monitor))
        self.monitor_status = label("停止中。基準がなくても電圧波形を確認できます。", muted=True)
        layout.addWidget(self.monitor_status)
        rms_card, self.monitor_rms = metric("入力RMS [V]")
        peak_card, self.monitor_peak = metric("入力ピーク [V]")
        level_card, self.monitor_level = metric("総音圧 [dB SPL]")
        layout.addWidget(row(rms_card, peak_card, level_card))
        layout.addWidget(label("マイク入力の直近1秒 [V] · 縦軸は自動スケール", muted=True))
        self.monitor_waveform = WaveformView()
        self.monitor_waveform.setMinimumHeight(220)
        layout.addWidget(self.monitor_waveform, 1)
        self.monitor_file = label(
            "開始・停止・電圧・音圧の時系列は保存フォルダに記録します。", muted=True
        )
        self.monitor_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_monitor_button = button("記録フォルダを開く", self._open_monitor)
        self.open_monitor_button.setMaximumWidth(180)
        self.open_monitor_button.setEnabled(False)
        layout.addWidget(row(self.monitor_file, self.open_monitor_button))
        self.monitor_samples = np.empty(0)
        self.monitor_duration_s = 0.0

    def _start_monitor(self):
        def start():
            setup = self.editor.setup()
            reference = self.reference
            if reference is not None:
                try:
                    validate_reference(setup, reference)
                    self._check_backend(reference)
                except ValueError:
                    reference = None
            simulate, save_capture = self.simulate, self.save_monitor.isChecked()
            output = self._output_path("monitor")
            self.monitor_samples = np.empty(0)
            self.monitor_duration_s = 0
            self.monitor_level.setText("—" if reference is not None else "未校正")
            self.monitor_status.setText(f"モニター中 · 共通SR: {setup.daq.sample_rate_hz:,} Hz")
            self.monitor_file.setText(str(output))
            self.job_output = output
            self._start(
                "monitor",
                lambda cancelled, progress: monitor_input(
                    setup,
                    output=output,
                    simulate=simulate,
                    reference=reference,
                    save_capture=save_capture,
                    cancel_requested=cancelled,
                    on_frame=progress,
                ),
            )

        self._guard(start)

    def _monitor_frame(self, item):
        rate = item["sample_rate_hz"]
        self.monitor_samples = np.concatenate((self.monitor_samples, item["samples"]))[-rate:]
        self.monitor_duration_s = item["duration_s"]
        self.monitor_waveform.set_input(self.monitor_samples, rate)
        self.monitor_rms.setText(f"{item['rms_v']:.5g}")
        self.monitor_peak.setText(f"{item['peak_v']:.5g}")
        level = item["level_spl_db"]
        self.monitor_level.setText(
            "未校正" if not item["calibrated"] else "無信号" if level is None else f"{level:.2f}"
        )
        if item["clipped"]:
            self.monitor_status.setText(
                "入力クリッピング：入力範囲・コンディショナのゲインを確認してください。"
            )
            self.monitor_status.setStyleSheet("color: #a73338;")
            self.monitor_level.setText("クリップ")
        else:
            self.monitor_status.setText(f"モニター中 · {item['duration_s']:.1f} s · {rate:,} Hz")
            self.monitor_status.setStyleSheet("")

    def _apply_replay(self, replay):
        self.editor.set_setup(replay.setup)
        self.setup_path.setText(str(replay.directory / "setup.json"))
        self.mode.setCurrentIndex(0 if replay.backend == "simulation" else 1)
        self.source_kind.setCurrentIndex(1)
        self.source_location.setText(str(replay.directory / "stimulus.npz"))
        self.target.setValue(replay.target_spl_db)
        self._set_reference(replay.reference, replay.directory / "reference.json")
        self._show_stimulus(replay.stimulus)
        self._parent_run = replay.parent_record
        self.tabs.setCurrentIndex(2)
        self._refresh()
        self.status.setText(
            "条件を復元しました。設定・機器・測定位置を確認し、「校正を開始」で再測定できます。"
        )

    def _audit(self, action, **details):
        root = self.output_root.text().strip()
        if not root:
            raise ValueError("操作を記録するため、保存フォルダを指定してください。")
        return self.activity.record(root, action, **details)

    def _context(self):
        source = self._source()
        try:
            setup = asdict(self.editor.setup())
        except (ValueError, TypeError):
            setup = {
                "unvalidated": {
                    key: widget.currentText()
                    if isinstance(widget, QComboBox)
                    else widget.toPlainText()
                    if isinstance(widget, QPlainTextEdit)
                    else widget.text()
                    for key, widget in self.editor.fields.items()
                }
            }
        try:
            target = self.target.value()
        except ValueError:
            target = {"unvalidated": self.target.text()}
        return {
            "setup": setup,
            "reference": asdict(self.reference) if self.reference else None,
            "backend": "simulation" if self.simulate else "ni-daqmx",
            "target_spl_db": target,
            "source": {
                "kind": source.kind,
                "location": source.location,
                "parameters": str(source.parameters) if source.parameters else None,
            },
            "parent_run": self._parent_run,
        }

    def _open_activity(self):
        if self.activity.directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.activity.directory)))
        else:
            self.status.setText("測定・波形確認などの操作を行うと、操作ログを保存します。")

    def _guard(self, task):
        try:
            return task()
        except Exception as error:
            self._show_error(error)
            return None

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "結果の保存フォルダ", self.output_root.text())
        if path:
            self.output_root.setText(path)

    def _new_setup(self):
        self._parent_run = None
        self.setup_path.setText("新規セットアップ")
        self.editor.set_setup(new_setup())

    def _load_setup(self):
        path, _ = QFileDialog.getOpenFileName(self, "設定を読み込む", "", "YAML (*.yaml *.yml)")
        if path:

            def load():
                setup = load_setup(path)
                self._audit(
                    "setup_loaded", path=path, file_sha256=file_sha256(path), setup=asdict(setup)
                )
                self._parent_run = None
                self.editor.set_setup(setup)
                self.setup_path.setText(path)
                self.status.setText("設定を読み込みました。")

            self._guard(load)

    def _save_setup(self):
        setup = self._guard(self.editor.setup)
        if setup is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "設定を保存", "my-setup.yaml", "YAML (*.yaml *.yml)"
        )
        if path:

            def save():
                Path(path).write_text(
                    yaml.safe_dump(asdict(setup), sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
                self._audit(
                    "setup_saved", path=path, file_sha256=file_sha256(path), setup=asdict(setup)
                )
                self.setup_path.setText(path)
                self.status.setText("設定を保存しました。")

            self._guard(save)

    def _load_reference(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "基準記録を読み込む", self.output_root.text(), "JSON (*.json)"
        )
        if path:

            def load():
                reference = load_reference(path)
                validate_reference(self.editor.setup(), reference)
                self._check_backend(reference)
                self._audit("reference_loaded", path=path, reference=asdict(reference))
                self._set_reference(reference, Path(path))
                self.status.setText("基準記録を読み込みました。波形校正へ進めます。")

            self._guard(load)

    def _set_reference(self, reference, path):
        self._clear_results()
        self.reference = reference
        self.reference_file.setText(f"{reference.measured_at}\n{path}")
        self.sensitivity_value.setText(f"{reference.sensitivity_v_per_pa:.6g}")
        self.frequency_value.setText(f"{reference.estimated_frequency_hz:.2f}")
        self.stability_value.setText(f"{reference.block_span_db:.3f}")
        self._refresh()

    def _check_backend(self, reference):
        backend = "simulation" if self.simulate else "ni-daqmx"
        if reference.hardware.get("backend") != backend:
            raise ValueError(
                "基準記録の測定モードが異なります。同じモードで基準を測り直してください。"
            )

    @Slot()
    def _mode_changed(self):
        self.banner.setText(
            "シミュレーション：合成信号で動作確認します。実機の音圧校正には使えません。"
            if self.simulate
            else "NI DAQ 実機：設定したAI/AOで測定します。開始前に接続と測定位置を確認してください。"
        )
        self._clear_results()
        self._refresh()

    @Slot()
    def _setup_changed(self):
        self._invalidate_stimulus()
        self._refresh()

    @Slot()
    def _target_changed(self):
        self._clear_results()
        self._refresh()

    @Slot()
    def _source_changed(self):
        generator = self.source_kind.currentIndex() == 2
        self.generator_row.setVisible(generator)
        self.parameters_row.setVisible(generator)
        self._invalidate_stimulus()
        self._refresh()

    def _clear_results(self):
        self.iterations.setRowCount(0)
        for value in (self.level_value, self.scale_value, self.peak_value):
            value.setText("—")
        self.result_file.setText("校正結果と再生用波形を、保存フォルダ内に新しい名前で保存します。")
        self.last_output = None
        self.open_result_button.setEnabled(False)

    def _invalidate_stimulus(self):
        self.stimulus = None
        self.waveform.set_stimulus(None)
        self.stimulus_info.setText("波形を確認するか、そのまま校正を開始してください。")
        self._clear_results()

    def _refresh(self):
        if self.worker is not None:
            return
        try:
            setup = self.editor.setup()
        except (ValueError, TypeError) as error:
            self.shared_rate.setText(f"設定を確認してください: {error}")
            self.measure_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.calibrate_button.setEnabled(False)
            self.reference_status.setText("設定の入力を確認してください。")
            self.monitor_start.setEnabled(False)
            return
        self.shared_rate.setText(
            f"共通SR: {setup.daq.sample_rate_hz:,} Hz  ·  WAVはこのレートへ自動変換"
        )
        self.reference_instruction.setText(
            f"校正器: {setup.reference_level_db:g} dB SPL / {setup.reference_frequency_hz:g} Hz\n"
            f"取得: {setup.reference_duration_s:g} s  ·  AI: {setup.daq.input_channel}  ·  "
            f"共通SR: {setup.daq.sample_rate_hz:,} Hz  ·  AOは0 Vを維持"
        )
        valid = False
        if self.reference is None:
            self.reference_status.setText(
                "未測定：基準を測定するか、既存の記録を読み込んでください。"
            )
        else:
            try:
                validate_reference(setup, self.reference)
                self._check_backend(self.reference)
                valid = True
                self.reference_status.setText("使用可能：現在の設定・測定モードと一致しています。")
            except ValueError:
                self.reference_status.setText(
                    "再基準測定が必要：設定または測定モードが変わっています。"
                )
        selected = bool(self.source_location.text().strip())
        try:
            self.target.value()
            valid_target = True
        except ValueError:
            valid_target = False
        self.measure_button.setEnabled(True)
        self.monitor_start.setEnabled(True)
        self.preview_button.setEnabled(selected)
        self.calibrate_button.setEnabled(valid and selected and valid_target)
        self.calibration_hint.setText(
            "目標音圧の入力を確認してください。"
            if not valid_target
            else "準備完了"
            if valid and selected
            else "波形を選択してください。"
            if valid
            else "有効なマイク基準記録が必要です。"
        )
        self.open_result_button.setEnabled(self.last_output is not None)
        self.open_monitor_button.setEnabled(self.last_monitor_output is not None)

    def _choose_source(self):
        index = self.source_kind.currentIndex()
        filters = ["WAV (*.wav)", "保存波形 (*.npz)", "Python (*.py)"]
        path, _ = QFileDialog.getOpenFileName(self, "波形・生成関数を選択", "", filters[index])
        if path:
            self.source_location.setText(path)
            self._prepare()

    def _choose_parameters(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "生成関数のパラメータ", "", "YAML (*.yaml *.yml)"
        )
        if path:
            self.parameters_path.setText(path)

    def _source(self):
        kind = ["wav", "npz", "generator"][self.source_kind.currentIndex()]
        location = self.source_location.text().strip()
        parameters = None
        if kind == "generator":
            location += ":" + self.function_name.text().strip()
            text = self.parameters_path.text().strip()
            parameters = Path(text) if text else None
        return StimulusSource(kind, location, parameters)

    def _prepare(self, *, then_calibrate=False):
        def prepare():
            setup = self.editor.setup()
            source = self._source()

            def task(cancelled, progress):
                if cancelled():
                    raise MeasurementCancelled("Measurement cancelled")
                stimulus = source.prepare(setup.daq.sample_rate_hz)
                measurement_bounds(stimulus, setup)
                if cancelled():
                    raise MeasurementCancelled("Measurement cancelled")
                return stimulus

            self._start("prepare_calibration" if then_calibrate else "prepare", task)

        self._guard(prepare)

    def _show_stimulus(self, stimulus):
        self.stimulus = stimulus
        self.waveform.set_stimulus(stimulus)
        info = stimulus.describe()
        conversion = stimulus.provenance.get("resampling")
        rate = f"{stimulus.sample_rate_hz:,} Hz"
        if conversion:
            rate = f"{conversion['source_sample_rate_hz']:,} → {rate}"
        self.stimulus_info.setText(
            f"{rate}  ·  {info['duration_s']:.3f} s  ·  {info['sample_count']:,} samples\n"
            f"RMS {info['digital_rms']:.4f}  /  Peak {info['digital_peak']:.4f}"
        )

    def _output_path(self, kind):
        text = self.output_root.text().strip()
        if not text:
            raise ValueError("保存フォルダを選択してください。")
        root = Path(text).expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise ValueError("保存先にはフォルダを指定してください。")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        suffix = ".json" if kind == "reference" else ""
        return root / f"{kind}-{stamp}{suffix}"

    def _confirm_hardware(self, setup, reference_mode=False):
        if self.simulate:
            return True
        instruction = (
            f"校正器を装着し、{setup.reference_level_db:g} dB SPL / "
            f"{setup.reference_frequency_hz:g} Hzで動作させてください。AOは0 Vです。"
            if reference_mode
            else "校正器を外し、マイクを実際の測定位置に置いてください。スピーカーから音が出ます。"
        )
        message = (
            f"{instruction}\n\n"
            f"AI: {setup.daq.input_channel} / AO: {setup.daq.output_channel}\n"
            f"SR: {setup.daq.sample_rate_hz:,} Hz / 出力上限: {setup.daq.max_peak_v:g} Vpeak\n\n"
            "動物・聴取者がいないことを確認して開始してください。"
        )
        return (
            QMessageBox.question(
                self,
                "実機測定を開始",
                message,
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Ok
        )

    def _measure_reference(self):
        def measure():
            setup = self.editor.setup()
            output = self._output_path("reference")
            simulate = self.simulate
            if not self._confirm_hardware(setup, reference_mode=True):
                return
            self.job_output = output
            self._start(
                "reference",
                lambda cancelled, progress: measure_reference_to_file(
                    setup,
                    output,
                    simulate=simulate,
                    cancel_requested=cancelled,
                ),
            )

        self._guard(measure)

    def _calibrate(self):
        if self.stimulus is None:
            self._prepare(then_calibrate=True)
            return

        def search():
            setup = self.editor.setup()
            if self.reference is None:
                raise ValueError("先にマイク基準を測定・読み込みしてください。")
            validate_reference(setup, self.reference)
            self._check_backend(self.reference)
            measurement_bounds(self.stimulus, setup)
            output = self._output_path("calibration")
            if not self._confirm_hardware(setup):
                return
            stimulus, reference = self.stimulus, self.reference
            target, simulate = self.target.value(), self.simulate
            parent_run = self._parent_run
            self._clear_results()
            self.job_output = output

            def task(cancelled, progress):
                if cancelled():
                    raise MeasurementCancelled("Measurement cancelled")
                hardware = make_hardware(setup, simulate=simulate)
                return calibrate_to_directory(
                    stimulus,
                    setup=setup,
                    reference=reference,
                    target_spl_db=target,
                    output=output,
                    hardware=hardware,
                    on_iteration=progress,
                    cancel_requested=cancelled,
                    parent_run=parent_run,
                )

            self._start("calibrate", task, max_iterations=setup.measurement.max_iterations)

        self._guard(search)

    def _set_busy(self, busy):
        for cls in (QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QPushButton, QCheckBox):
            for widget in self.findChildren(cls):
                if widget is not self.stop_button:
                    widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.stop_button.setText("停止")
        if not busy:
            self._refresh()

    def _start(self, kind, task, max_iterations=None):
        if self.worker is not None:
            raise RuntimeError("別の処理が実行中です。")
        self._audit(
            "operation_started",
            operation=kind,
            context=self._context(),
            output=str(self.job_output) if kind in {"reference", "calibrate", "monitor"} else None,
        )
        self.job_kind = kind
        self.worker = Operation(task, self)
        self.worker.progress.connect(self._iteration)
        self.worker.finished.connect(self._finished)
        self._set_busy(True)
        self.progress.setRange(0, max_iterations or 0)
        self.progress.setValue(0)
        self.status.setText(
            {
                "prepare": "波形を準備しています…",
                "prepare_calibration": "校正用の波形を準備しています…",
                "reference": "マイク基準を測定しています…",
                "calibrate": "目標音圧になる電圧倍率を探索しています…",
                "restore": "実行記録を照合して、条件を復元しています…",
                "monitor": "マイク入力をモニターしています。停止ボタンで終了します。",
            }[kind]
        )
        self.worker.start()

    @Slot(dict)
    def _iteration(self, item):
        if item.get("kind") == "monitor":
            self._monitor_frame(item)
            return
        number = self.iterations.rowCount()
        self.iterations.insertRow(number)
        level = item["measured_spl_db"]
        error = item["error_db"]
        values = [
            str(item["iteration"]),
            "SNR不足" if level is None else f"{level:.3f}",
            f"{item['output_peak_v']:.6g}",
            "—" if error is None else f"{error:+.3f}",
        ]
        for column, value in enumerate(values):
            cell = QTableWidgetItem(value)
            cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.iterations.setItem(number, column, cell)
        self.iterations.scrollToBottom()
        self.progress.setValue(item["iteration"])
        self.level_value.setText("SNR不足" if level is None else f"{level:.2f}")
        self.peak_value.setText(f"{item['output_peak_v']:.6g}")

    @Slot()
    def _finished(self):
        worker, kind = self.worker, self.job_kind
        self.worker = None
        worker.wait()
        error, result = worker.error, worker.result
        worker.deleteLater()
        self.progress.setRange(0, 1)
        self.progress.setValue(0 if error else 1)
        self._set_busy(False)
        try:
            extra = {}
            if error is None and kind in {"prepare", "prepare_calibration"}:
                path = self.activity.save_waveform(result)
                extra = {"stimulus": result.describe(), "saved_waveform": str(path)}
            elif error is None and kind == "reference":
                extra = {"reference": asdict(result)}
            elif error is None and kind == "restore":
                extra = {
                    "restored_from": result.parent_record,
                    "stimulus_sha256": result.stimulus.sha256,
                }
            self._audit(
                "operation_finished",
                operation=kind,
                status="cancelled"
                if isinstance(error, MeasurementCancelled)
                else "failed"
                if error
                else "complete",
                error=str(error) if error else None,
                output=str(self.job_output)
                if kind in {"reference", "calibrate", "monitor"}
                else None,
                notes=getattr(error, "__notes__", []) if error else [],
                **extra,
            )
        except Exception as log_error:
            self._show_error(log_error)
            if self._closing:
                self.close()
            return
        if error is not None:
            self.status.setText(
                "停止しました。" if isinstance(error, MeasurementCancelled) else f"エラー: {error}"
            )
            if kind == "calibrate" and self.job_output.is_dir():
                self.last_output = self.job_output
                self.result_file.setText(f"未完了の記録: {self.job_output}")
                self.open_result_button.setEnabled(True)
            if kind == "monitor":
                self.monitor_status.setText(
                    "モニターを停止しました。"
                    if isinstance(error, MeasurementCancelled)
                    else "モニターを停止しました。エラーの詳細を確認してください。"
                )
            if not isinstance(error, MeasurementCancelled) and not self._closing:
                self._show_error(error)
        elif kind in {"prepare", "prepare_calibration"}:
            self._show_stimulus(result)
            self.status.setText("波形を確認しました。校正ではこの波形を固定して使います。")
            if kind == "prepare_calibration" and not self._closing:
                self._calibrate()
        elif kind == "reference":
            self._set_reference(result, self.job_output)
            self.status.setText("基準を保存しました。校正器を外し、波形校正へ進んでください。")
        elif kind == "restore":
            self._apply_replay(result)
        elif kind == "monitor":
            self.monitor_status.setText(f"停止しました · 記録時間 {result['duration_s']:.1f} s")
            self.status.setText("マイクモニターを停止し、記録を保存しました。")
        else:
            self.level_value.setText(f"{result.measured_spl_db:.2f}")
            self.scale_value.setText(f"{result.scale_v:.7g}")
            self.peak_value.setText(f"{result.output_peak_v:.6g}")
            self.last_output = self.job_output
            self.result_file.setText(str(self.job_output / "result.json"))
            self.open_result_button.setEnabled(True)
            self.status.setText("校正が完了しました。結果と再生用のstimulus.npzを保存しました。")
        if kind == "calibrate":
            self._scan_history()
        if kind == "monitor" and self.job_output.is_dir():
            self.last_monitor_output = self.job_output
            self.open_monitor_button.setEnabled(True)
        if self._closing:
            self.close()

    def _request_stop(self):
        if self.worker is not None:
            self.worker.cancel_event.set()
            try:
                self._audit("stop_requested", operation=self.job_kind)
            except Exception as error:
                self.status.setText(f"停止要求済み。操作ログの保存に失敗しました: {error}")
                self.stop_button.setEnabled(False)
                return
            self.stop_button.setEnabled(False)
            self.stop_button.setText("停止待ち…")
            self.status.setText(
                "停止要求済み：現在の取得・波形準備が終わり次第、安全に終了します。"
            )

    def _open_result(self):
        if self.last_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def _open_monitor(self):
        if self.last_monitor_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_monitor_output)))

    def closeEvent(self, event):
        if self.worker is not None:
            self._closing = True
            self._request_stop()
            event.ignore()
        else:
            event.accept()


def run():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("NI SPL Calibration")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = CalibrationWindow()
    window.show()
    return app.exec()
