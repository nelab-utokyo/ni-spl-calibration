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
    QSlider,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .calibration import (
    MeasurementCancelled,
    _window_bounds,
    load_reference,
    measurement_bounds,
    validate_reference,
)
from .config import DaqSettings, MeasurementSettings, Setup, load_setup
from .i18n import (
    Message,
    bind,
    error_message,
    retranslate,
    set_cell,
    set_headers,
    set_text,
    set_value,
    translate,
)
from .meter import LiveMeter
from .monitor import monitor_input
from .playback import VolumeControl, run_test_playback
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
    widget = QLabel()
    set_text(widget, text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if muted:
        widget.setObjectName("muted")
    return widget


def button(text, callback, *, primary=False):
    widget = QPushButton()
    set_text(widget, text)
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
            raise ValueError(translate(self, "目標音圧には数値を入力してください。")) from error
        if not math.isfinite(value) or not -100 <= value <= 180:
            raise ValueError(
                translate(self, "目標音圧は−100〜180 dB SPLの有限値で指定してください。")
            )
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
        bind(
            self.fields["daq.sample_rate_hz"],
            "setToolTip",
            "DAQのAI/AO、WAVの変換先、生成関数に共通で使います。変更後は再基準測定が必要です。",
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
                "再生・測定する区間・帯域",
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
        bind(self.fields["measurement.band_hz"], "setPlaceholderText", "例: [5000, 80000]")
        bind(
            self.fields["measurement.stop_s"],
            "setToolTip",
            "校正では指定した開始～終了時刻の区間だけを再生・解析します。",
        )
        bind(
            self.fields["measurement.frequency_correction_db"],
            "setPlaceholderText",
            "例: [[0, 0], [1000, 0], [100000, -2.0]]",
        )
        bind(
            self.fields["measurement.start_scale_v"],
            "setToolTip",
            "探索開始時のV/デジタル値です。初回Vpeakは初期倍率×選択区間のピークで、出力上限を超えません。テスト再生の音量から設定できます。",
        )
        layout.addStretch()
        self.setWidget(content)
        self.set_setup(new_setup())

    def _section(self, title, specs):
        group = QGroupBox()
        bind(group, "setTitle", title)
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
            form.addRow(label(title), widget)
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
                raise ValueError(
                    translate(self, "{key}: 入力値を確認してください", key=key)
                ) from error
            if "." in key:
                section, name = key.split(".")
                payload[section][name] = value
            else:
                payload[key] = value
        payload["daq"] = DaqSettings(**payload["daq"])
        payload["measurement"] = MeasurementSettings(**payload["measurement"])
        return Setup(**payload)


class WaveformView(QWidget):
    def __init__(self, *, empty_text="波形を選択すると、ここに表示します"):
        super().__init__()
        self.empty_text = empty_text
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
                area,
                Qt.AlignmentFlag.AlignCenter,
                translate(self, self.empty_text),
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


class SpectrumView(QWidget):
    """Log-frequency RMS bars; display only, with no filtering of AO samples."""

    def __init__(self):
        super().__init__()
        self.spectrum = None
        self.peaks = None
        self.setMinimumHeight(210)

    def set_spectrum(self, spectrum):
        self.spectrum = spectrum
        levels = np.asarray(spectrum["levels_dbv"], dtype=float)
        if self.peaks is None or len(self.peaks) != len(levels):
            self.peaks = levels.copy()
        else:
            self.peaks = np.maximum(levels, self.peaks - 2)
        self.update()

    def clear(self):
        self.spectrum = None
        self.peaks = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(48, 24, max(1, self.width() - 66), max(1, self.height() - 60))
        painter.fillRect(area, QColor("#132637"))
        low, high = -120.0, 20.0
        for level in range(-120, 21, 20):
            y = area.bottom() - (level - low) / (high - low) * area.height()
            painter.setPen(QColor("#304657"))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor("#64748b"))
            painter.drawText(QPointF(1, y + 4), str(level))
        painter.drawText(
            QPointF(area.left(), 15), translate(self, "マイク入力スペクトラム [dBV RMS]")
        )
        if self.spectrum is None:
            painter.setPen(QColor("#d5e5f3"))
            painter.drawText(
                area,
                Qt.AlignmentFlag.AlignCenter,
                translate(self, "モニターまたはテスト再生を開始してください"),
            )
            return
        levels = np.clip(self.spectrum["levels_dbv"], low, high)
        width = area.width() / len(levels)
        for index, level in enumerate(levels):
            x = area.left() + index * width + 1
            y = area.bottom() - (level - low) / (high - low) * area.height()
            painter.fillRect(QRectF(x, y, max(1, width - 2), area.bottom() - y), QColor("#35b9ae"))
            peak = np.clip(self.peaks[index], low, high)
            peak_y = area.bottom() - (peak - low) / (high - low) * area.height()
            painter.setPen(QPen(QColor("#99ece1"), 2))
            painter.drawLine(QPointF(x, peak_y), QPointF(x + max(1, width - 2), peak_y))
        edges = np.asarray(self.spectrum["edges_hz"])
        last_right = -1
        painter.setPen(QColor("#64748b"))
        for frequency in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
            if not edges[0] <= frequency <= edges[-1] * (1 + 1e-12):
                continue
            x = (
                area.left()
                + np.log(frequency / edges[0]) / np.log(edges[-1] / edges[0]) * area.width()
            )
            text = f"{frequency // 1000}k" if frequency >= 1000 else str(frequency)
            text_width = painter.fontMetrics().horizontalAdvance(text)
            left = min(max(area.left(), x - text_width / 2), area.right() - text_width)
            if left > last_right + 10:
                painter.drawText(QPointF(left, area.bottom() + 19), text)
                last_right = left + text_width
        painter.drawText(QPointF(area.right() - 18, self.height() - 1), "Hz")


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
        self._stimulus_info = None
        self._window_peak_cache = None
        self.last_output = None
        self.last_monitor_output = None
        self._closing = False
        self.activity = ActivityLog()
        self._parent_run = None
        self.volume_control = VolumeControl(0.1)
        self._playback_start_choice = None
        self._pending_start_choice = None
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        title = label("NI SPL Calibration")
        title.setObjectName("title")
        self.mode = QComboBox()
        self.mode.addItems(["シミュレーション", "NI DAQ 実機"])
        bind(self.mode, "setItemText", "シミュレーション", 0)
        bind(self.mode, "setItemText", "NI DAQ 実機", 1)
        self.mode.setCurrentIndex(1)
        self.mode.setMinimumWidth(190)
        self.mode.setMaximumWidth(240)
        self.language = QComboBox()
        self.language.setObjectName("languageSelector")
        self.language.addItem("日本語", "ja")
        self.language.addItem("English", "en")
        self.language.setMaximumWidth(135)
        layout.addWidget(row(title, self.mode, label("言語 / Language"), self.language))
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
        self._playback_tab()
        layout.addWidget(self._microphone_panel())
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
        self.wav_channels.currentIndexChanged.connect(self._source_changed)
        self.language.currentIndexChanged.connect(self._language_changed)
        self._source_changed()
        self._mode_changed()
        self._scan_history()
        self._language_changed()

    @property
    def simulate(self):
        return self.mode.currentIndex() == 0

    @Slot()
    def _language_changed(self):
        retranslate(self)
        self.waveform.update()
        self.monitor_spectrum.update()

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
        bind(self.tabs, "setTabText", title, self.tabs.count() - 1)
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
        layout = self._page("2  マイク基準測定", scroll=True)
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
        for index, source in enumerate(["WAVファイル", "保存波形 (NPZ)", "Python生成関数"]):
            bind(self.source_kind, "setItemText", source, index)
        self.source_kind.setMaximumWidth(180)
        self.source_location = QLineEdit()
        bind(self.source_location, "setPlaceholderText", "再生するモノラルWAVを選択してください")
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
        bind(
            self.parameters_path,
            "setPlaceholderText",
            "省略可。生成関数の引数YAML（SRは設定画面で指定）",
        )
        self.parameters_row = row(
            label("パラメータ"), self.parameters_path, button("YAMLを選択", self._choose_parameters)
        )
        layout.addWidget(self.parameters_row)
        self.preview_button = button("波形を確認", self._prepare)
        self.preview_button.setMaximumWidth(160)
        self.stimulus_info = label("波形が未選択です。", muted=True)
        layout.addWidget(row(self.preview_button, self.stimulus_info))
        self.start_level_info = label(
            "波形を確認すると、探索開始時のピーク電圧を表示します。", muted=True
        )
        layout.addWidget(self.start_level_info)
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
        self.iterations = QTableWidget(0, 5)
        set_headers(
            self.iterations,
            ["探索回", "実測音圧 [dB SPL]", "出力ピーク [V]", "誤差 [dB]", "SNR [dB]"],
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
        layout.addWidget(
            row(
                self.result_file,
                self.open_result_button,
                button("テスト再生へ →", lambda: self.tabs.setCurrentIndex(5)),
            )
        )

    def _show_error(self, error):
        notes = "".join(f"\n{note}" for note in getattr(error, "__notes__", []))
        details = Message("{error}{notes}", {"error": error_message(error), "notes": notes})
        set_text(self.status, "エラー: {details}", details=details)
        QMessageBox.warning(
            self,
            translate(self, "処理を完了できませんでした"),
            translate(self, "{details}", details=details),
        )

    def _history_tab(self):
        layout = self._page("4  履歴・再現", scroll=True)
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
        set_headers(
            self.history, ["実施日時 (UTC)", "状態", "目標 [dB SPL]", "測定モード", "保存フォルダ"]
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
                    item = (
                        set_cell(self.history, index, column, value)
                        if column in (1, 3)
                        else set_cell(self.history, index, column, "{value}", value=value)
                    )
                    item.setData(Qt.ItemDataRole.UserRole, str(record["directory"]))
            set_text(
                self.history_info,
                "{count}件の実行記録。復元時にファイルのSHA-256を照合します。",
                count=len(records),
            )

        self._guard(scan)

    def _restore_selected(self):
        index = self.history.currentRow()
        if index < 0:
            set_text(self.status, "復元する実行記録を選択してください。")
            return
        self._restore(self.history.item(index, 0).data(Qt.ItemDataRole.UserRole))

    def _choose_replay(self):
        path = QFileDialog.getExistingDirectory(
            self, translate(self, "実行記録のフォルダを選択"), self.output_root.text()
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
        layout.addWidget(
            label("マイク入力を周波数ごとのバーで表示します。通常のモニター中はAOを0 Vに保ちます。")
        )
        layout.addWidget(
            label(
                "有効な基準記録があれば、設定帯域の総音圧を表示します（背景音を含みます）。"
                "基準測定・波形校正を行うときは、先にモニターを停止してください。",
                muted=True,
            )
        )
        self.save_monitor = QCheckBox("生のマイク入力も保存する (NPZ)")
        set_text(self.save_monitor, "生のマイク入力も保存する (NPZ)")
        self.save_monitor.setChecked(False)
        layout.addWidget(self.save_monitor)
        self.monitor_status = label("停止中。基準がなくても電圧波形を確認できます。", muted=True)
        layout.addWidget(self.monitor_status)
        layout.addWidget(
            label("入力スペクトラム・電圧・音圧は、どのタブでも画面下部に表示します。")
        )
        layout.addWidget(
            label(
                "横軸は周波数（対数）、縦軸は帯域RMSのdBVです。音声を加工する機能ではありません。",
                muted=True,
            )
        )
        self.monitor_file = label(
            "開始・停止・電圧・音圧の時系列は保存フォルダに記録します。", muted=True
        )
        self.monitor_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_monitor_button = button("記録フォルダを開く", self._open_monitor)
        self.open_monitor_button.setMaximumWidth(180)
        self.open_monitor_button.setEnabled(False)
        layout.addWidget(row(self.monitor_file, self.open_monitor_button))
        layout.addStretch()

    def _microphone_panel(self):
        panel = QFrame()
        panel.setObjectName("microphonePanel")
        panel.setFixedHeight(230)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.monitor_start = button("モニターを開始", self._start_monitor, primary=True)
        self.monitor_start.setMaximumWidth(170)
        self.spectrum_state = label("待機中 · 入力未取得", muted=True)
        self.monitor_rms = label("—")
        self.monitor_peak = label("—")
        self.monitor_level = label("未校正")
        header = row(
            self.monitor_start,
            self.spectrum_state,
            label("入力RMS [V]", muted=True),
            self.monitor_rms,
            label("入力ピーク [V]", muted=True),
            self.monitor_peak,
            label("総音圧 [dB SPL]", muted=True),
            self.monitor_level,
        )
        layout.addWidget(header)
        self.monitor_spectrum = SpectrumView()
        self.monitor_spectrum.setMinimumHeight(160)
        layout.addWidget(self.monitor_spectrum, 1)
        self.monitor_samples = np.empty(0)
        self.monitor_duration_s = 0.0
        return panel

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
            set_text(
                self.monitor_status,
                "モニター中 · 共通SR: {rate:,} Hz",
                rate=setup.daq.sample_rate_hz,
            )
            set_value(self.monitor_file, str(output))
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
        self.monitor_spectrum.set_spectrum(item["spectrum"])
        set_value(self.monitor_rms, f"{item['rms_v']:.5g}")
        set_value(self.monitor_peak, f"{item['peak_v']:.5g}")
        level = item["level_spl_db"]
        set_text(
            self.monitor_level,
            "未校正" if not item["calibrated"] else "無信号" if level is None else f"{level:.2f}",
        )
        if item.get("level_note"):
            set_text(self.monitor_level, "帯域計算不可")
        if item["clipped"]:
            set_text(self.spectrum_state, "入力クリッピング")
            self.spectrum_state.setStyleSheet("color: #a73338;")
            set_text(
                self.monitor_status,
                "入力クリッピング：入力範囲・コンディショナのゲインを確認してください。",
            )
            self.monitor_status.setStyleSheet("color: #a73338;")
            set_text(self.monitor_level, "クリップ")
        else:
            set_text(self.spectrum_state, "取得中 · {rate:,} Hz", rate=rate)
            self.spectrum_state.setStyleSheet("")
            set_text(
                self.monitor_status,
                "モニター中 · {duration:.1f} s · {rate:,} Hz",
                duration=item["duration_s"],
                rate=rate,
            )
            self.monitor_status.setStyleSheet("")

    def _playback_tab(self):
        layout = self._page("6  テスト再生", scroll=True)
        layout.addWidget(
            label("選択した音声をAOから再生し、マイク入力を同時に確認します。基準記録は不要です。")
        )
        self.playback_source = label("再生する音声を選択してください。", muted=True)
        layout.addWidget(
            row(
                self.playback_source,
                button("音声を選択", self._choose_test_source),
                button("1 kHzテスト音を選択", self._choose_test_tone),
            )
        )
        self.wav_channels = QComboBox()
        for source, mode in [
            ("モノラルのみ", "mono"),
            ("左右平均 (L+R)/2", "mix"),
            ("左チャンネル L", "left"),
            ("右チャンネル R", "right"),
        ]:
            index = self.wav_channels.count()
            self.wav_channels.addItem(source, mode)
            bind(self.wav_channels, "setItemText", source, index)
        self.wav_channels.setCurrentIndex(1)
        layout.addWidget(row(label("WAVチャンネル処理"), self.wav_channels))
        self.playback_channel_info = label("AO 1 ch / モノラル波形", muted=True)
        layout.addWidget(self.playback_channel_info)
        self.playback_file_info = label(
            "WAV・NPZを選ぶか、波形校正画面で生成関数を指定してください。", muted=True
        )
        layout.addWidget(self.playback_file_info)
        self.playback_hardware = label(
            "接続機種・AOチャンネル・指令電圧を再生時に表示します。", muted=True
        )
        layout.addWidget(self.playback_hardware)
        self.playback_start = button("テスト再生を開始", self._test_playback, primary=True)
        self.playback_stop = button("再生停止", self._request_stop)
        self.playback_stop.setEnabled(False)
        self.loop_audio = QCheckBox()
        set_text(self.loop_audio, "繰り返し再生")
        self.loop_audio.setChecked(True)
        self.save_playback = QCheckBox()
        set_text(self.save_playback, "AO・マイクの生データを保存")
        self.save_playback.setChecked(False)
        self.mute_button = button("ミュート", lambda: self.volume_fader.setValue(0))
        layout.addWidget(
            row(
                self.playback_start,
                self.playback_stop,
                self.mute_button,
                self.loop_audio,
                self.save_playback,
            )
        )
        self.volume_fader = QSlider(Qt.Orientation.Vertical)
        self.volume_fader.setRange(0, 1000)
        self.volume_fader.setValue(100)
        self.volume_fader.setMinimumHeight(150)
        self.volume_fader.setMaximumWidth(55)
        self.volume_fader.valueChanged.connect(self._volume_changed)
        self.volume_text = label("")
        fader_panel = QWidget()
        fader_layout = QVBoxLayout(fader_panel)
        fader_layout.addWidget(label("音量フェーダー"))
        fader_layout.addWidget(self.volume_text)
        fader_layout.addWidget(self.volume_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        fader_panel.setMaximumWidth(210)
        playback_info = QWidget()
        info_layout = QVBoxLayout(playback_info)
        info_layout.addWidget(
            label("100%は設定したVpeak上限です。校正済み音圧の指定ではありません。", muted=True)
        )
        command, self.playback_peak = metric("AO指令ピーク [V]")
        info_layout.addWidget(command)
        info_layout.addWidget(
            label("入力スペクトラム・電圧・音圧は、どのタブでも画面下部に表示します。", muted=True)
        )
        self.adopt_start_button = button("この音量を探索開始に使う", self._use_playback_as_start)
        self.adopt_start_button.setEnabled(False)
        info_layout.addWidget(self.adopt_start_button)
        info_layout.addStretch()
        layout.addWidget(row(fader_panel, playback_info), 1)
        self.playback_status = label("停止中。音声を選択して再生を開始してください。", muted=True)
        layout.addWidget(self.playback_status)
        layout.addWidget(
            label(
                "小さい音量から調節し、入力がクリップせず目標音圧より低いレベルを目安にします。採用すると再生を停止し、波形校正へ進みます。SNRは校正中に背景音と比較して判定します。",
                muted=True,
            )
        )
        self.playback_open = button("記録フォルダを開く", self._open_monitor)
        self.playback_open.setEnabled(False)
        layout.addWidget(self.playback_open)
        self._volume_changed()

    def _volume_changed(self):
        fraction = self.volume_fader.value() / 1000
        self.volume_control.set(fraction)
        try:
            peak = self.editor.setup().daq.max_peak_v * fraction
        except (ValueError, TypeError):
            peak = 0.0
        set_text(
            self.volume_text,
            "{volume:.1f}%\n上限 {peak:.6g} Vpeak",
            volume=fraction * 100,
            peak=peak,
        )
        self._update_adopt_button()

    def _update_adopt_button(self):
        if not hasattr(self, "adopt_start_button"):
            return
        choice = self._playback_start_choice
        available = (
            choice is not None
            and self._pending_start_choice is None
            and (self.worker is None or self.job_kind == "test_playback")
            and math.isclose(choice["volume_fraction"], self.volume_control.get(), abs_tol=1e-12)
        )
        if available:
            try:
                self._selected_waveform_peak(self.editor.setup())
            except (ValueError, TypeError):
                available = False
        self.adopt_start_button.setEnabled(available)

    def _use_playback_as_start(self):
        def adopt():
            if not self.adopt_start_button.isEnabled():
                return
            choice = self._playback_start_choice
            if self.worker is not None:
                self._pending_start_choice = choice
                self._update_adopt_button()
                self._request_stop()
            else:
                self._apply_start_choice(choice)

        self._guard(adopt)

    def _apply_start_choice(self, choice):
        setup = self.editor.setup()
        if (
            self.stimulus is None
            or self._stimulus_info["sha256"] != choice["stimulus_sha256"]
            or asdict(setup.daq) != choice["daq"]
            or self.simulate != choice["simulate"]
        ):
            raise ValueError(
                translate(self, "波形または設定が変わっています。テスト再生で再確認してください。")
            )
        initial_peak = min(
            choice["scale_v"] * self._selected_waveform_peak(setup), setup.daq.max_peak_v
        )
        self._audit("start_level_selected", **choice, initial_peak_v=initial_peak)
        field = self.editor.fields["measurement.start_scale_v"]
        blocker = QSignalBlocker(field)
        field.setText(repr(choice["scale_v"]))
        del blocker
        self._clear_results()
        self._refresh()
        self.tabs.setCurrentIndex(2)
        area = self.tabs.widget(2)
        if isinstance(area, QScrollArea):
            area.ensureWidgetVisible(self.calibrate_button, 0, 20)
        set_text(
            self.status,
            "探索開始を {peak:.6g} Vpeak（倍率 {scale:.6g} V/デジタル値）に設定しました。目標音圧を指定して校正を開始してください。",
            peak=initial_peak,
            scale=choice["scale_v"],
        )

    def _choose_test_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "テスト再生する音声を選択"), "", "Audio (*.wav *.npz)"
        )
        if path:
            self.source_kind.setCurrentIndex(1 if Path(path).suffix.lower() == ".npz" else 0)
            self.source_location.setText(path)
            self._prepare(playback_preview=True)

    def _choose_test_tone(self):
        self.parameters_path.clear()
        self.function_name.setText("make_test_tone")
        self.source_kind.setCurrentIndex(2)
        self.source_location.setText("ni_spl_calibration.playback")
        self._prepare(playback_preview=True)

    def _test_playback(self):
        self.tabs.setCurrentIndex(5)
        if self.stimulus is None:
            self._prepare(then_playback=True)
            return

        def start():
            setup = self.editor.setup()
            stimulus, simulate = self.stimulus, self.simulate
            if not self._confirm_hardware(setup):
                return
            reference = self.reference
            if reference is not None:
                try:
                    validate_reference(setup, reference)
                    self._check_backend(reference)
                except ValueError:
                    reference = None
            loop, save_capture = self.loop_audio.isChecked(), self.save_playback.isChecked()
            output = self._output_path("playback")
            self.job_output = output
            self._playback_start_choice = None
            self._pending_start_choice = None
            set_value(self.playback_peak, "—")
            self._start(
                "test_playback",
                lambda cancelled, progress: run_test_playback(
                    stimulus,
                    setup=setup,
                    output=output,
                    simulate=simulate,
                    volume=self.volume_control,
                    loop=loop,
                    reference=reference,
                    save_capture=save_capture,
                    cancel_requested=cancelled,
                    on_frame=progress,
                ),
            )
            area = self.tabs.widget(5)
            if isinstance(area, QScrollArea):
                area.ensureWidgetVisible(self.volume_fader, 0, 20)

        self._guard(start)

    def _hardware_info(self, description):
        product = description.get("product_type", description.get("model", description["backend"]))
        setup = self.editor.setup()
        set_text(
            self.playback_hardware,
            "接続: {product}\nAI {input} / AO {output} / {rate:,} Hz / 出力上限 {limit:g} Vpeak",
            product=product,
            input=setup.daq.input_channel,
            output=setup.daq.output_channel,
            rate=setup.daq.sample_rate_hz,
            limit=setup.daq.max_peak_v,
        )

    def _playback_frame(self, item):
        self._monitor_frame(item)
        self._playback_start_choice = (
            {
                "stimulus_sha256": self._stimulus_info["sha256"],
                "daq": asdict(self.editor.setup().daq),
                "simulate": self.simulate,
                "scale_v": float(item["scale_end_v"]),
                "peak_v": float(item["scale_end_v"] * self._stimulus_info["digital_peak"]),
                "volume_fraction": item["volume_fraction"],
                "playback_record": str(self.job_output),
                "first_sample": item["first_sample"],
            }
            if not item["clipped"] and item["volume_fraction"] > 0
            else None
        )
        self._update_adopt_button()
        set_value(self.playback_peak, f"{item['output_peak_v']:.6g}")
        self._hardware_info(item["hardware"])
        set_text(
            self.playback_status,
            "再生中 · {duration:.2f} s · 音量 {volume:.1f}%",
            duration=item["duration_s"],
            volume=item["volume_fraction"] * 100,
        )

    def _apply_replay(self, replay):
        self.editor.set_setup(replay.setup)
        set_value(self.setup_path, str(replay.directory / "setup.json"))
        self.mode.setCurrentIndex(0 if replay.backend == "simulation" else 1)
        self.source_kind.setCurrentIndex(1)
        self.source_location.setText(str(replay.directory / "stimulus.npz"))
        self.target.setValue(replay.target_spl_db)
        self._set_reference(replay.reference, replay.directory / "reference.json")
        self._show_stimulus(replay.stimulus)
        self._parent_run = replay.parent_record
        self.tabs.setCurrentIndex(2)
        self._refresh()
        set_text(
            self.status,
            "条件を復元しました。設定・機器・測定位置を確認し、「校正を開始」で再測定できます。",
        )

    def _audit(self, action, **details):
        root = self.output_root.text().strip()
        if not root:
            raise ValueError(
                translate(self, "操作を記録するため、保存フォルダを指定してください。")
            )
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
            "ui_language": self.language.currentData(),
            "target_spl_db": target,
            "source": {
                "kind": source.kind,
                "location": source.location,
                "parameters": str(source.parameters) if source.parameters else None,
                "wav_channel_mode": source.wav_channel_mode,
            },
            "parent_run": self._parent_run,
            "test_playback": {
                "volume_fraction": self.volume_control.get(),
                "loop": self.loop_audio.isChecked(),
                "save_capture": self.save_playback.isChecked(),
            },
        }

    def _open_activity(self):
        if self.activity.directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.activity.directory)))
        else:
            set_text(self.status, "測定・波形確認などの操作を行うと、操作ログを保存します。")

    def _guard(self, task):
        try:
            return task()
        except Exception as error:
            self._show_error(error)
            return None

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(
            self, translate(self, "結果の保存フォルダ"), self.output_root.text()
        )
        if path:
            self.output_root.setText(path)

    def _new_setup(self):
        self._parent_run = None
        set_text(self.setup_path, "新規セットアップ")
        self.editor.set_setup(new_setup())

    def _load_setup(self):
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "設定を読み込む"), "", "YAML (*.yaml *.yml)"
        )
        if path:

            def load():
                setup = load_setup(path)
                self._audit(
                    "setup_loaded", path=path, file_sha256=file_sha256(path), setup=asdict(setup)
                )
                self._parent_run = None
                self.editor.set_setup(setup)
                set_value(self.setup_path, path)
                set_text(self.status, "設定を読み込みました。")

            self._guard(load)

    def _save_setup(self):
        setup = self._guard(self.editor.setup)
        if setup is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, translate(self, "設定を保存"), "my-setup.yaml", "YAML (*.yaml *.yml)"
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
                set_value(self.setup_path, path)
                set_text(self.status, "設定を保存しました。")

            self._guard(save)

    def _load_reference(self):
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "基準記録を読み込む"), self.output_root.text(), "JSON (*.json)"
        )
        if path:

            def load():
                reference = load_reference(path)
                validate_reference(self.editor.setup(), reference)
                self._check_backend(reference)
                self._audit("reference_loaded", path=path, reference=asdict(reference))
                self._set_reference(reference, Path(path))
                set_text(self.status, "基準記録を読み込みました。波形校正へ進めます。")

            self._guard(load)

    def _set_reference(self, reference, path):
        self._clear_results()
        self.reference = reference
        set_value(self.reference_file, f"{reference.measured_at}\n{path}")
        set_value(self.sensitivity_value, f"{reference.sensitivity_v_per_pa:.6g}")
        set_value(self.frequency_value, f"{reference.estimated_frequency_hz:.2f}")
        set_value(self.stability_value, f"{reference.block_span_db:.3f}")
        self._refresh()

    def _check_backend(self, reference):
        backend = "simulation" if self.simulate else "ni-daqmx"
        if reference.hardware.get("backend") != backend:
            raise ValueError(
                translate(
                    self, "基準記録の測定モードが異なります。同じモードで基準を測り直してください。"
                )
            )

    @Slot()
    def _mode_changed(self):
        set_text(
            self.banner,
            "シミュレーション：合成信号で動作確認します。実機の音圧校正には使えません。"
            if self.simulate
            else "NI DAQ 実機：設定したAI/AOで測定します。開始前に接続と測定位置を確認してください。",
        )
        self._playback_start_choice = None
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
            set_text(value, "—")
        set_text(
            self.result_file, "校正結果と再生用波形を、保存フォルダ内に新しい名前で保存します。"
        )
        self.last_output = None
        self.open_result_button.setEnabled(False)

    def _invalidate_stimulus(self):
        self.stimulus = None
        self._stimulus_info = None
        self._playback_start_choice = None
        self.waveform.set_stimulus(None)
        set_text(self.stimulus_info, "波形を確認するか、そのまま校正を開始してください。")
        self._clear_results()
        set_text(self.playback_channel_info, "AO 1 ch / モノラル波形")
        set_text(
            self.playback_file_info, "WAV・NPZを選ぶか、波形校正画面で生成関数を指定してください。"
        )

    def _refresh(self):
        self._update_adopt_button()
        self._update_start_level_info()
        if self.worker is not None:
            return
        try:
            setup = self.editor.setup()
        except (ValueError, TypeError) as error:
            set_text(
                self.shared_rate, "設定を確認してください: {error}", error=error_message(error)
            )
            self.measure_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.calibrate_button.setEnabled(False)
            set_text(self.reference_status, "設定の入力を確認してください。")
            self.monitor_start.setEnabled(False)
            self.playback_start.setEnabled(False)
            return
        set_text(
            self.shared_rate,
            "共通SR: {rate:,} Hz  ·  WAVはこのレートへ自動変換",
            rate=setup.daq.sample_rate_hz,
        )
        set_text(
            self.reference_instruction,
            "校正器: {level:g} dB SPL / {frequency:g} Hz\n"
            "取得: {duration:g} s  ·  AI: {channel}  ·  共通SR: {rate:,} Hz  ·  AOは0 Vを維持",
            level=setup.reference_level_db,
            frequency=setup.reference_frequency_hz,
            duration=setup.reference_duration_s,
            channel=setup.daq.input_channel,
            rate=setup.daq.sample_rate_hz,
        )
        valid = False
        if self.reference is None:
            set_text(
                self.reference_status, "未測定：基準を測定するか、既存の記録を読み込んでください。"
            )
        else:
            try:
                validate_reference(setup, self.reference)
                self._check_backend(self.reference)
                valid = True
                set_text(
                    self.reference_status, "使用可能：現在の設定・測定モードと一致しています。"
                )
            except ValueError:
                set_text(
                    self.reference_status,
                    "再基準測定が必要：設定または測定モードが変わっています。",
                )
        selected = bool(self.source_location.text().strip())
        if selected:
            set_value(self.playback_source, self._source().location)
        else:
            set_text(self.playback_source, "再生する音声を選択してください。")
        self.playback_start.setEnabled(selected)
        self._volume_changed()
        try:
            self.target.value()
            valid_target = True
        except ValueError:
            valid_target = False
        self.measure_button.setEnabled(True)
        self.monitor_start.setEnabled(True)
        self.preview_button.setEnabled(selected)
        self.calibrate_button.setEnabled(valid and selected and valid_target)
        set_text(
            self.calibration_hint,
            "目標音圧の入力を確認してください。"
            if not valid_target
            else "準備完了"
            if valid and selected
            else "波形を選択してください。"
            if valid
            else "有効なマイク基準記録が必要です。",
        )
        self.open_result_button.setEnabled(self.last_output is not None)
        self.open_monitor_button.setEnabled(self.last_monitor_output is not None)
        self.playback_open.setEnabled(self.last_monitor_output is not None)

    def _choose_source(self):
        index = self.source_kind.currentIndex()
        filters = ["WAV (*.wav)", "保存波形 (*.npz)", "Python (*.py)"]
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "波形・生成関数を選択"), "", translate(self, filters[index])
        )
        if path:
            self.source_location.setText(path)
            self._prepare()

    def _choose_parameters(self):
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "生成関数のパラメータ"), "", "YAML (*.yaml *.yml)"
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
        return StimulusSource(kind, location, parameters, self.wav_channels.currentData())

    def _prepare(self, *, then_calibrate=False, then_playback=False, playback_preview=False):
        def prepare():
            setup = self.editor.setup()
            source = self._source()

            def task(cancelled, progress):
                if cancelled():
                    raise MeasurementCancelled("Measurement cancelled")
                stimulus = source.prepare(setup.daq.sample_rate_hz)
                if not (then_playback or playback_preview):
                    measurement_bounds(stimulus, setup)
                if cancelled():
                    raise MeasurementCancelled("Measurement cancelled")
                return stimulus

            kind = (
                "prepare_playback"
                if then_playback
                else "prepare_test_preview"
                if playback_preview
                else "prepare_calibration"
                if then_calibrate
                else "prepare"
            )
            self._start(kind, task)

        self._guard(prepare)

    def _show_stimulus(self, stimulus):
        self.stimulus = stimulus
        self.waveform.set_stimulus(stimulus)
        info = stimulus.describe()
        self._stimulus_info = info
        conversion = stimulus.provenance.get("resampling")
        rate = f"{stimulus.sample_rate_hz:,} Hz"
        if conversion:
            rate = f"{conversion['source_sample_rate_hz']:,} → {rate}"
        set_value(
            self.stimulus_info,
            f"{rate}  ·  {info['duration_s']:.3f} s  ·  {info['sample_count']:,} samples\n"
            f"RMS {info['digital_rms']:.4f}  /  Peak {info['digital_peak']:.4f}",
        )
        if stimulus.provenance.get("kind") == "wav":
            mode = stimulus.provenance.get("channel_mode", "mono")
            mode_text = {
                "mono": "モノラルのみ",
                "mix": "左右平均 (L+R)/2",
                "left": "左チャンネル L",
                "right": "右チャンネル R",
            }[mode]
            set_text(
                self.playback_channel_info,
                "元WAV {channels} ch → AO 1 ch / {mode}",
                channels=stimulus.provenance.get("source_channels", 1),
                mode=Message(mode_text),
            )
        else:
            set_text(self.playback_channel_info, "AO 1 ch / モノラル波形")
        original_rms = conversion.get("source_digital_rms") if conversion else info["digital_rms"]
        original_peak = (
            conversion.get("source_digital_peak") if conversion else info["digital_peak"]
        )
        if original_rms is not None and original_peak is not None:
            change = 20 * math.log10(info["digital_rms"] / original_rms)
            set_text(
                self.playback_file_info,
                "デジタル振幅: 変換前 RMS {before_rms:.6g} / Peak {before_peak:.6g} → 変換後 RMS {after_rms:.6g} / Peak {after_peak:.6g}（{change:+.3f} dB）",
                before_rms=original_rms,
                before_peak=original_peak,
                after_rms=info["digital_rms"],
                after_peak=info["digital_peak"],
                change=change,
            )
        else:
            set_text(
                self.playback_file_info,
                "変換後のデジタル振幅: RMS {rms:.6g} / Peak {peak:.6g}",
                rms=info["digital_rms"],
                peak=info["digital_peak"],
            )
        self._update_start_level_info()

    def _selected_waveform_peak(self, setup):
        settings = setup.measurement
        if settings.start_s == 0 and settings.stop_s is None:
            return self._stimulus_info["digital_peak"]
        key = (self._stimulus_info["sha256"], settings.start_s, settings.stop_s)
        if self._window_peak_cache is None or self._window_peak_cache[0] != key:
            first, last = _window_bounds(self.stimulus, settings)
            peak = float(np.max(np.abs(self.stimulus.samples[first:last])))
            self._window_peak_cache = (key, peak)
        return self._window_peak_cache[1]

    def _update_start_level_info(self):
        if self.stimulus is None:
            set_text(
                self.start_level_info, "波形を確認すると、探索開始時のピーク電圧を表示します。"
            )
            return
        try:
            setup = self.editor.setup()
            peak = self._selected_waveform_peak(setup)
        except (ValueError, TypeError):
            set_text(self.start_level_info, "設定の入力を確認してください。")
            return
        set_text(
            self.start_level_info,
            "探索開始: {voltage:.6g} Vpeak（初期倍率 {scale:.6g} × 選択区間のピーク {peak:.6g}、上限 {limit:.6g} Vpeak）。テスト再生の音量から開始値を選べます。",
            voltage=min(setup.measurement.start_scale_v * peak, setup.daq.max_peak_v),
            scale=setup.measurement.start_scale_v,
            peak=peak,
            limit=setup.daq.max_peak_v,
        )

    def _output_path(self, kind):
        text = self.output_root.text().strip()
        if not text:
            raise ValueError(translate(self, "保存フォルダを選択してください。"))
        root = Path(text).expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise ValueError(translate(self, "保存先にはフォルダを指定してください。"))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        suffix = ".json" if kind == "reference" else ""
        return root / f"{kind}-{stamp}{suffix}"

    def _confirm_hardware(self, setup, reference_mode=False):
        if self.simulate:
            return True
        instruction = (
            translate(
                self,
                "校正器を装着し、{level:g} dB SPL / {frequency:g} Hzで動作させてください。AOは0 Vです。",
                level=setup.reference_level_db,
                frequency=setup.reference_frequency_hz,
            )
            if reference_mode
            else translate(
                self,
                "校正器を外し、マイクを実際の測定位置に置いてください。スピーカーから音が出ます。",
            )
        )
        message = translate(
            self,
            "{instruction}\n\nAI: {input} / AO: {output}\n"
            "SR: {rate:,} Hz / 出力上限: {peak:g} Vpeak\n\n"
            "動物・聴取者がいないことを確認して開始してください。",
            instruction=instruction,
            input=setup.daq.input_channel,
            output=setup.daq.output_channel,
            rate=setup.daq.sample_rate_hz,
            peak=setup.daq.max_peak_v,
        )
        return (
            QMessageBox.question(
                self,
                translate(self, "実機測定を開始"),
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

            def task(cancelled, progress):
                meter = LiveMeter(setup, None, progress)
                return measure_reference_to_file(
                    setup,
                    output,
                    simulate=simulate,
                    cancel_requested=cancelled,
                    on_samples=meter.push,
                )

            self._start("reference", task)

        self._guard(measure)

    def _calibrate(self):
        if self.stimulus is None:
            self._prepare(then_calibrate=True)
            return

        def search():
            setup = self.editor.setup()
            if self.reference is None:
                raise ValueError(translate(self, "先にマイク基準を測定・読み込みしてください。"))
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
                meter = LiveMeter(setup, reference, progress)
                return calibrate_to_directory(
                    stimulus,
                    setup=setup,
                    reference=reference,
                    target_spl_db=target,
                    output=output,
                    hardware=hardware,
                    on_iteration=progress,
                    on_phase=progress,
                    cancel_requested=cancelled,
                    parent_run=parent_run,
                    on_samples=meter.push,
                )

            self._start("calibrate", task, max_iterations=setup.measurement.max_iterations)

        self._guard(search)

    def _set_busy(self, busy):
        for cls in (QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QPushButton, QCheckBox):
            for widget in self.findChildren(cls):
                if widget not in (self.stop_button, self.language):
                    widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        playing = busy and self.job_kind == "test_playback"
        self.volume_fader.setEnabled(not busy or playing)
        self.mute_button.setEnabled(not busy or playing)
        self.playback_stop.setEnabled(playing)
        self._update_adopt_button()
        set_text(self.stop_button, "停止")
        if not busy:
            self._refresh()

    def _start(self, kind, task, max_iterations=None):
        if self.worker is not None:
            raise RuntimeError(translate(self, "別の処理が実行中です。"))
        self._audit(
            "operation_started",
            operation=kind,
            context=self._context(),
            output=str(self.job_output)
            if kind in {"reference", "calibrate", "monitor", "test_playback"}
            else None,
        )
        self.job_kind = kind
        if kind in {"reference", "calibrate", "monitor", "test_playback"}:
            self.monitor_samples = np.empty(0)
            self.monitor_duration_s = 0
            self.monitor_spectrum.clear()
            set_text(self.spectrum_state, "入力待ち…")
            self.spectrum_state.setStyleSheet("")
            for value in (self.monitor_rms, self.monitor_peak, self.monitor_level):
                set_text(value, "—")
        self.worker = Operation(task, self)
        self.worker.progress.connect(self._iteration)
        self.worker.finished.connect(self._finished)
        self._set_busy(True)
        self.progress.setRange(0, max_iterations or 0)
        self.progress.setValue(0)
        set_text(
            self.status,
            {
                "prepare": "波形を準備しています…",
                "prepare_calibration": "校正用の波形を準備しています…",
                "prepare_playback": "テスト再生用の音声を準備しています…",
                "prepare_test_preview": "テスト再生用の音声を準備しています…",
                "reference": "マイク基準を測定しています…",
                "calibrate": "目標音圧になる電圧倍率を探索しています…",
                "restore": "実行記録を照合して、条件を復元しています…",
                "monitor": "マイク入力をモニターしています。停止ボタンで終了します。",
                "test_playback": "テスト再生中です。フェーダーで調節し、停止ボタンで中断できます。",
            }[kind],
        )
        self.worker.start()

    @Slot(dict)
    def _iteration(self, item):
        if item.get("kind") == "hardware":
            self._hardware_info(item["description"])
            return
        if item.get("kind") == "test_playback":
            self._playback_frame(item)
            return
        if item.get("kind") == "calibration_phase":
            if self.worker is None or self.worker.cancel_event.is_set():
                return
            phase = item["phase"]
            if phase == "saving":
                set_text(self.status, "再生波形と測定条件を保存中…（この間は無音です）")
            elif phase == "background":
                set_text(
                    self.status,
                    "背景音を測定中：{duration:g} 秒（AOは0 V・まだ再生していません）",
                    duration=item["duration_s"],
                )
            else:
                set_text(
                    self.status,
                    "再生中：{iteration}/{maximum} 回目 · 区間 {start:g}～{stop:g} 秒 · 再生 {duration:.3f} 秒 · 出力 {peak:.4g} Vpeak",
                    iteration=item["iteration"],
                    maximum=item["max_iterations"],
                    start=item["start_s"],
                    stop=item["stop_s"],
                    duration=item["duration_s"],
                    peak=item["output_peak_v"],
                )
            return
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
            "—" if item["signal_to_noise_db"] is None else f"{item['signal_to_noise_db']:.1f}",
        ]
        for column, value in enumerate(values):
            cell = (
                set_cell(self.iterations, number, column, "SNR不足")
                if column == 1 and level is None
                else set_cell(self.iterations, number, column, "{value}", value=value)
            )
            cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.iterations.scrollToBottom()
        self.progress.setValue(item["iteration"])
        set_text(self.level_value, "SNR不足" if level is None else f"{level:.2f}")
        set_value(self.peak_value, f"{item['output_peak_v']:.6g}")
        if level is None:
            set_text(
                self.calibration_hint,
                "SNR不足：出力を最大 {step:g} dBずつ上げて探索を続けます。",
                step=self.editor.setup().measurement.max_upward_step_db,
            )
        else:
            set_text(
                self.calibration_hint,
                "SNR条件を満たしています。目標音圧への収束を確認しています。",
            )

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
        if kind in {"reference", "calibrate", "monitor", "test_playback"}:
            set_text(
                self.spectrum_state,
                "停止中 · 最後の入力"
                if self.monitor_spectrum.spectrum is not None
                else "停止中 · 入力未取得",
            )
            self.spectrum_state.setStyleSheet("")
        try:
            extra = {}
            if error is None and kind in {
                "prepare",
                "prepare_calibration",
                "prepare_playback",
                "prepare_test_preview",
            }:
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
                if kind in {"reference", "calibrate", "monitor", "test_playback"}
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
            if isinstance(error, MeasurementCancelled):
                set_text(self.status, "停止しました。")
            else:
                set_text(self.status, "エラー: {details}", details=error_message(error))
            if kind == "calibrate" and self.job_output.is_dir():
                self.last_output = self.job_output
                set_text(self.result_file, "未完了の記録: {path}", path=self.job_output)
                self.open_result_button.setEnabled(True)
            if kind == "monitor":
                set_text(
                    self.monitor_status,
                    "モニターを停止しました。"
                    if isinstance(error, MeasurementCancelled)
                    else "モニターを停止しました。エラーの詳細を確認してください。",
                )
            if kind == "test_playback":
                set_text(
                    self.playback_status, "再生を停止しました。記録とエラーを確認してください。"
                )
                set_text(self.playback_peak, "出力確認")
            if not isinstance(error, MeasurementCancelled) and not self._closing:
                self._show_error(error)
        elif kind in {"prepare", "prepare_calibration", "prepare_playback", "prepare_test_preview"}:
            self._show_stimulus(result)
            set_text(self.status, "波形を確認しました。校正ではこの波形を固定して使います。")
            if kind == "prepare_calibration" and not self._closing:
                self._calibrate()
            if kind == "prepare_playback" and not self._closing:
                self._test_playback()
        elif kind == "reference":
            self._set_reference(result, self.job_output)
            set_text(self.status, "基準を保存しました。校正器を外し、波形校正へ進んでください。")
        elif kind == "restore":
            self._apply_replay(result)
        elif kind == "monitor":
            set_text(
                self.monitor_status,
                "停止しました · 記録時間 {duration:.1f} s",
                duration=result["duration_s"],
            )
            set_text(self.status, "マイクモニターを停止し、記録を保存しました。")
        elif kind == "test_playback":
            set_value(self.playback_peak, "0")
            set_text(
                self.playback_status,
                "再生終了 · {duration:.2f} s · 保存先 {path}",
                duration=result["duration_s"],
                path=self.job_output,
            )
            set_text(self.status, "テスト再生を終了し、AOの停止・0 V指令と記録保存を完了しました。")
        else:
            set_text(self.level_value, f"{result.measured_spl_db:.2f}")
            set_value(self.scale_value, f"{result.scale_v:.7g}")
            set_value(self.peak_value, f"{result.output_peak_v:.6g}")
            self.last_output = self.job_output
            set_value(self.result_file, str(self.job_output / "result.json"))
            self.open_result_button.setEnabled(True)
            set_text(self.status, "校正が完了しました。結果と再生用のstimulus.npzを保存しました。")
        if kind == "calibrate":
            self._scan_history()
        if kind in {"monitor", "test_playback"} and self.job_output.is_dir():
            self.last_monitor_output = self.job_output
            self.open_monitor_button.setEnabled(True)
            self.playback_open.setEnabled(True)
        if kind in {"reference", "calibrate", "test_playback"}:
            set_text(self.monitor_status, "取得終了。最後の入力スペクトラムを表示しています。")
        if kind == "test_playback":
            choice = self._pending_start_choice
            self._pending_start_choice = None
            if error is not None:
                self._playback_start_choice = None
            elif choice is not None and not self._closing:
                self._guard(lambda: self._apply_start_choice(choice))
            self._update_adopt_button()
        if self._closing:
            self.close()

    def _request_stop(self):
        if self.worker is not None:
            self.worker.cancel_event.set()
            try:
                self._audit("stop_requested", operation=self.job_kind)
            except Exception as error:
                set_text(
                    self.status,
                    "停止要求済み。操作ログの保存に失敗しました: {error}",
                    error=error_message(error),
                )
                self.stop_button.setEnabled(False)
                return
            self.stop_button.setEnabled(False)
            set_text(self.stop_button, "停止待ち…")
            set_text(
                self.status,
                "停止要求済み：短い取得単位で中断し、AOを0 Vへ戻します。"
                if self.job_kind in {"reference", "calibrate", "monitor", "test_playback"}
                else "停止要求済み：波形準備の完了後に終了します。",
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
