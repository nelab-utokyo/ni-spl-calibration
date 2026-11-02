"""Qt desktop interface. All DAQ operations run in a single worker thread."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, replace
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from threading import Event
from uuid import uuid4

import numpy as np
import yaml
from PySide6.QtCore import (
    QItemSelectionModel,
    QPointF,
    QRectF,
    QSignalBlocker,
    Qt,
    QThread,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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
    QSizePolicy,
    QSlider,
    QTableWidget,
    QTabWidget,
    QToolButton,
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
from .jobs import CalibrationJob, load_plan, run_queue, save_plan, starting_context
from .meter import LiveMeter
from .monitor import monitor_input
from .playback import VolumeControl, run_test_playback
from .records import ActivityLog, file_sha256, find_runs, load_replay
from .workflow import (
    StimulusSource,
    make_hardware,
    measure_reference_to_file,
)

STYLE = """
QWidget#pageActions { border-top: 1px solid #dce3ec; }
QWidget { font-size: 13px; color: #1e293b; }
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #f5f7fb; }
QLabel#subtitle, QLabel#muted { color: #596b80; }
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
QTabBar::tab { padding: 11px 20px; color: #64748b; }
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
        if isinstance(widget, (QPushButton, QCheckBox)):
            widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
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


class Page(IntEnum):
    SETUP = 0
    REFERENCE = 1
    PLAYBACK = 2
    CALIBRATION = 3
    HISTORY = 4


class BandInput(QWidget):
    """One shared analysis band for calibration and live SPL, stored in Setup."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.nyquist = None
        self.mode = QComboBox()
        self.mode.setFixedWidth(150)
        for text, value in [("全帯域", "full"), ("20–20,000 Hz", "audio"), ("指定", "custom")]:
            index = self.mode.count()
            self.mode.addItem(text, value)
            bind(self.mode, "setItemText", text, index)
        self.low = QLineEdit("20")
        self.high = QLineEdit("20000")
        for field, name in [(self.low, "音圧帯域の下限 [Hz]"), (self.high, "音圧帯域の上限 [Hz]")]:
            field.setFixedWidth(94)
            field.setAlignment(Qt.AlignmentFlag.AlignRight)
            bind(field, "setAccessibleName", name)
            bind(field, "setToolTip", name)
            field.textChanged.connect(self._edited)
        self.info = label("", muted=True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (
            label("音圧計算帯域", muted=True),
            self.mode,
            self.low,
            label("–"),
            self.high,
            label("Hz", muted=True),
            self.info,
        ):
            layout.addWidget(widget)
        layout.setStretch(6, 1)
        bind(
            self,
            "setToolTip",
            "校正・テスト再生・マイク確認のSPLに共通。DCは除外し、A/C特性は適用しません。再生波形は変わりません。",
        )
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()

    def value(self):
        if self.mode.currentData() == "full":
            return None
        try:
            low, high = float(self.low.text()), float(self.high.text())
        except ValueError as error:
            raise ValueError(translate(self, "帯域にはHz単位の数値を入力してください")) from error
        if not (math.isfinite(low) and math.isfinite(high) and 0 <= low < high):
            raise ValueError(translate(self, "帯域は 0 ≤ 下限 < 上限 で指定してください"))
        return low, high

    def setValue(self, value):
        blocker = QSignalBlocker(self)
        mode = "full" if value is None else "audio" if tuple(value) == (20, 20000) else "custom"
        self.mode.setCurrentIndex(self.mode.findData(mode))
        if value is not None:
            self.low.setText(str(value[0]))
            self.high.setText(str(value[1]))
        self._mode_changed()
        del blocker
        self.changed.emit()

    def set_sample_rate(self, rate):
        self.nyquist = rate / 2 if rate is not None else None
        if self.mode.currentData() == "full":
            self._set_bounds(0, self.nyquist)
        self._update_info()

    def _set_bounds(self, low, high):
        for field, value in [(self.low, low), (self.high, high)]:
            blocker = QSignalBlocker(field)
            field.setText("" if value is None else str(value))
            del blocker

    def _mode_changed(self):
        mode = self.mode.currentData()
        if mode == "full":
            self._set_bounds(0, self.nyquist)
        elif mode == "audio":
            self._set_bounds(20, 20000)
        for field in (self.low, self.high):
            field.setReadOnly(mode != "custom")
        self._edited()

    def _edited(self):
        self._update_info()
        self.changed.emit()

    def _update_info(self):
        error = None
        try:
            band = self.value()
            if self.nyquist is None:
                error = "サンプルレートを確認してください"
            elif band is not None and band[1] > self.nyquist:
                error = "上限は {limit:g} Hz以下"
        except ValueError as exc:
            error = str(exc)
        self.info.setStyleSheet("color: #a73338;" if error else "")
        set_text(
            self.info,
            error or "上限 {limit:g} Hz · A/C補正なし",
            limit=self.nyquist,
        )


def disclosure(title, content):
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    toggle = QToolButton()
    set_text(toggle, title)
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    toggle.setArrowType(Qt.ArrowType.RightArrow)
    toggle.setCheckable(True)
    toggle.setAutoRaise(True)
    content.setVisible(False)

    def expand(checked):
        content.setVisible(checked)
        toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    toggle.toggled.connect(expand)
    layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(content)
    return wrapper


class SetupEditor(QScrollArea):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.fields = {}
        self.types = {}
        self.blocks = {}
        specs = {
            "setup": (
                "機器・測定位置",
                [
                    ("setup_id", "セットアップ名", "str"),
                    ("chain_notes", "機器・ゲイン・測定位置の記録", "notes"),
                ],
            ),
            "daq": (
                "DAQ",
                [
                    ("daq.input_channel", "入力 AI", "str"),
                    ("daq.output_channel", "出力 AO", "str"),
                    ("daq.sample_rate_hz", "サンプルレート [Hz]", "int"),
                    ("daq.terminal_configuration", "入力方式", "terminal"),
                    ("daq.max_peak_v", "出力上限 [Vpeak]", "float"),
                ],
            ),
            "reference": (
                "校正器",
                [
                    ("reference_level_db", "基準音圧 [dB SPL]", "float"),
                    ("reference_frequency_hz", "基準周波数 [Hz]", "float"),
                    ("reference_duration_s", "測定時間 [s]", "float"),
                    ("reference_max_span_db", "安定性の許容幅 [dB]", "float"),
                ],
            ),
            "window": (
                "再生区間",
                [
                    ("measurement.start_s", "開始 [s]", "float"),
                    ("measurement.stop_s", "終了 [s]", "optional_float"),
                ],
            ),
            "search": (
                "探索条件",
                [
                    ("measurement.tolerance_db", "許容誤差 [dB]", "float"),
                    ("measurement.minimum_snr_db", "最低SNR [dB]", "float"),
                    ("measurement.max_upward_step_db", "最大上昇幅 [dB/回]", "float"),
                    ("measurement.max_iterations", "最大探索回数", "int"),
                    ("measurement.confirmations", "確認回数", "int"),
                    ("measurement.acoustic_delay_s", "音響・機器遅延 [s]", "float"),
                    ("measurement.start_scale_v", "初期倍率 [V/デジタル値]", "float"),
                ],
            ),
            "input": (
                "入力系",
                [
                    ("daq.input_min_v", "入力下限 [V]", "float"),
                    ("daq.input_max_v", "入力上限 [V]", "float"),
                    ("measurement.input_clip_fraction", "クリップ判定比率", "float"),
                    (
                        "measurement.frequency_correction_db",
                        "マイク補正 [[Hz, dB], …]",
                        "json_list",
                    ),
                ],
            ),
        }
        for key, (title, fields) in specs.items():
            self.blocks[key] = self._section(title, fields)
        self.band = BandInput()
        self.fields["measurement.band_hz"] = self.band
        self.types["measurement.band_hz"] = "band"
        self.band.changed.connect(self.changed.emit)
        self.fields["daq.sample_rate_hz"].textChanged.connect(self.update_band_limit)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.addWidget(self.blocks["daq"])
        layout.addWidget(self.blocks["setup"])
        layout.addWidget(disclosure("入力系の詳細", self.blocks["input"]))
        layout.addStretch()
        bind(
            self.fields["daq.sample_rate_hz"],
            "setToolTip",
            "DAQのAI/AO、WAVの変換先、生成関数に共通で使います。変更後は再基準測定が必要です。",
        )
        bind(self.fields["measurement.stop_s"], "setPlaceholderText", "末尾まで")
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
        self.setWidget(content)
        self.set_setup(new_setup())

    def _section(self, title, specs):
        group = QGroupBox()
        bind(group, "setTitle", title)
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        position = 0
        for key, title, kind in specs:
            tile = QWidget()
            form = QVBoxLayout(tile)
            form.setContentsMargins(0, 0, 0, 0)
            form.setSpacing(4)
            form.addWidget(label(title, muted=True))
            if kind == "terminal":
                widget = QComboBox()
                widget.addItems(["differential", "rse", "nrse", "pseudo_differential"])
                widget.setFixedWidth(180)
                widget.currentTextChanged.connect(lambda *_: self.changed.emit())
            elif kind == "notes":
                widget = QPlainTextEdit()
                widget.setMaximumHeight(64)
                widget.textChanged.connect(self.changed.emit)
            else:
                widget = QLineEdit()
                width = (
                    112
                    if kind in {"int", "float", "optional_float"}
                    else 210
                    if kind == "json_optional"
                    else 520
                    if kind == "json_list"
                    else 300
                    if key == "setup_id"
                    else 156
                )
                widget.setMaximumWidth(width)
                if kind in {"int", "float", "optional_float"}:
                    widget.setFixedWidth(width)
                    widget.setAlignment(Qt.AlignmentFlag.AlignRight)
                widget.textChanged.connect(lambda *_: self.changed.emit())
            widget.setObjectName(key)
            form.addWidget(widget)
            span = 3 if kind in {"notes", "json_list"} else 1
            if span == 3 and position % 3:
                position += 3 - position % 3
            grid.addWidget(tile, position // 3, position % 3, 1, span)
            position += span
            self.fields[key] = widget
            self.types[key] = kind
        for column in range(3):
            grid.setColumnStretch(column, 1)
        return group

    def set_setup(self, setup):
        blocker = QSignalBlocker(self)
        payload = asdict(setup)
        for key, widget in self.fields.items():
            value = payload
            for part in key.split("."):
                value = value[part]
            kind = self.types[key]
            if kind == "band":
                widget.setValue(value)
            elif kind == "terminal":
                widget.setCurrentText(value)
            elif kind == "notes":
                widget.setPlainText(value)
            elif kind.startswith("json"):
                widget.setText("" if value is None or value == () else json.dumps(value))
            else:
                widget.setText("" if value is None else str(value))
        del blocker
        self.changed.emit()

    def update_band_limit(self):
        try:
            rate = int(self.fields["daq.sample_rate_hz"].text())
        except ValueError:
            rate = 0
        self.band.set_sample_rate(rate if rate >= 2 else None)

    def setup(self):
        payload = {"daq": {}, "measurement": {}}
        for key, widget in self.fields.items():
            kind = self.types[key]
            if kind == "band":
                payload["measurement"]["band_hz"] = widget.value()
                continue
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
        self.setMinimumHeight(92)
        self.samples = None
        self.sample_rate_hz = 1
        self.axis_peak = 1.0
        self.selection = None

    def set_selection(self, start, stop):
        self.selection = (start, stop)
        self.update()

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
        if self.selection is not None:
            duration = len(samples) / self.sample_rate_hz
            start, stop = self.selection
            if 0 <= start < stop <= duration:
                x = area.left() + area.width() * start / duration
                width = area.width() * (stop - start) / duration
                painter.fillRect(QRectF(x, area.top(), width, area.height()), QColor("#dceafa"))
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
        self.nyquist = 1.0
        self.analysis_band = None
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
        painter.drawText(QPointF(area.left(), 15), translate(self, "マイク入力 [dBV]"))
        levels = np.clip(self.spectrum["levels_dbv"], low, high) if self.spectrum else []
        width = area.width() / max(1, len(levels))
        for index, level in enumerate(levels):
            x = area.left() + index * width + 1
            y = area.bottom() - (level - low) / (high - low) * area.height()
            painter.fillRect(QRectF(x, y, max(1, width - 2), area.bottom() - y), QColor("#35b9ae"))
            peak = np.clip(self.peaks[index], low, high)
            peak_y = area.bottom() - (peak - low) / (high - low) * area.height()
            painter.setPen(QPen(QColor("#99ece1"), 2))
            painter.drawLine(QPointF(x, peak_y), QPointF(x + max(1, width - 2), peak_y))
        edges = np.asarray(
            self.spectrum["edges_hz"]
            if self.spectrum
            else [min(20, self.nyquist / 4), self.nyquist]
        )
        if self.analysis_band is not None:
            positions = [
                area.left()
                + np.log(np.clip(bound, edges[0], edges[-1]) / edges[0])
                / np.log(edges[-1] / edges[0])
                * area.width()
                for bound in self.analysis_band
            ]
            for left, right in [(area.left(), positions[0]), (positions[1], area.right())]:
                painter.fillRect(
                    QRectF(left, area.top(), right - left, area.height()), QColor(8, 17, 26, 175)
                )
            painter.setPen(QPen(QColor("#99ece1"), 1, Qt.PenStyle.DashLine))
            for x in positions:
                painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
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


class LevelMeter(QWidget):
    """Compare total input SPL with the target without changing playback gain."""

    def __init__(self):
        super().__init__()
        self.level, self.target, self.clipped = None, 70.0, False
        self.setFixedHeight(40)
        bind(self, "setToolTip", "総音圧（背景音を含む）と目標音圧")

    def set_levels(self, level, target, clipped=False):
        self.level, self.target, self.clipped = level, target, clipped
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(3, 22, max(1, self.width() - 6), 9)
        low, high = min(0, self.target - 20), max(100, self.target + 10)

        def position(value):
            return area.left() + np.clip((value - low) / (high - low), 0, 1) * area.width()

        painter.fillRect(area, QColor("#e2e8f0"))
        if self.level is not None or self.clipped:
            color = (
                "#a73338" if self.clipped else "#c48931" if self.level > self.target else "#269c91"
            )
            painter.fillRect(
                QRectF(
                    area.left(),
                    area.top(),
                    area.width() if self.clipped else position(self.level) - area.left(),
                    area.height(),
                ),
                QColor(color),
            )
            painter.setPen(QColor("#596b80"))
            value = translate(self, "クリップ") if self.clipped else f"{self.level:.1f} dB SPL"
            painter.drawText(QPointF(area.left(), 15), value)
        x = position(self.target)
        painter.setPen(QPen(QColor("#205e96"), 2))
        painter.drawLine(QPointF(x, 19), QPointF(x, 35))
        painter.setPen(QColor("#205e96"))
        text = translate(self, "目標 {value:g}", value=self.target)
        width = painter.fontMetrics().horizontalAdvance(text)
        painter.drawText(
            QPointF(min(max(x - width / 2, area.left()), area.right() - width), 15), text
        )


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
        self._meter_band = None
        self.jobs = []
        self._active_job_id = None
        self._switching_job = False
        self._pause_after = Event()
        self.last_plan = None
        self._queue_setup = None
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        self.mode = QComboBox()
        self.mode.addItems(["シミュレーション", "NI DAQ 実機"])
        bind(self.mode, "setItemText", "シミュレーション", 0)
        bind(self.mode, "setItemText", "NI DAQ 実機", 1)
        self.mode.setCurrentIndex(1)
        self.mode.setFixedWidth(168)
        self.language = QComboBox()
        self.language.setObjectName("languageSelector")
        self.language.addItem("日本語", "ja")
        self.language.addItem("English", "en")
        self.language.setFixedWidth(102)
        self.language.setToolTip("Language / 言語")
        self.language.setAccessibleName("Language / 言語")
        # Discoverable even before the user can read the selected UI language.
        self.language_label = QLabel("Language")
        self.language_label.setObjectName("muted")
        self.language_label.setBuddy(self.language)
        self.output_root = QLineEdit(str(Path.cwd() / "results"))
        browse = button("…", self._choose_output)
        browse.setFixedWidth(38)
        bind(browse, "setToolTip", "保存フォルダを選択")
        toolbar = row(
            self.mode,
            self.language_label,
            self.language,
            label("保存先", muted=True),
            self.output_root,
            browse,
        )
        toolbar.layout().setStretch(4, 1)
        layout.addWidget(toolbar)
        self.tabs = QTabWidget()
        self._scroll_areas = {}
        self._page_layouts = {}
        layout.addWidget(self.tabs, 1)
        self._settings_tab()
        self._reference_tab()
        self._playback_tab()
        self._waveform_tab()
        self._history_tab()
        layout.addWidget(self._microphone_panel())
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = label("設定を確認してください", muted=True)
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
        self.editor.band._update_info()
        self.waveform.update()
        self.monitor_spectrum.update()
        self.level_meter.update()

    def _page(self, title, *, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        if scroll:
            container = QWidget()
            outer = QVBoxLayout(container)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            area.setWidget(page)
            outer.addWidget(area, 1)
            index = self.tabs.count()
            self._scroll_areas[index] = area
            self._page_layouts[index] = outer
            self.tabs.addTab(container, title)
        else:
            self.tabs.addTab(page, title)
        bind(self.tabs, "setTabText", title, self.tabs.count() - 1)
        return layout

    def _page_actions(self, *widgets):
        footer = QWidget()
        footer.setObjectName("pageActions")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 10, 16, 10)
        for widget in widgets[:-1]:
            widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(widget)
        layout.addStretch()
        layout.addWidget(widgets[-1])
        self._page_layouts[self.tabs.count() - 1].addWidget(footer)

    def _page_sidebar(self, page, sidebar):
        outer = self._page_layouts[page]
        area = self._scroll_areas[page]
        outer.removeWidget(area)
        body = QWidget()
        split = QHBoxLayout(body)
        split.setContentsMargins(16, 6, 0, 0)
        split.setSpacing(0)
        side_area = QScrollArea()
        side_area.setFixedWidth(sidebar.width())
        side_area.setWidgetResizable(True)
        side_area.setFrameShape(QFrame.Shape.NoFrame)
        side_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar.setMinimumWidth(0)
        sidebar.setMaximumWidth(16777215)
        side_area.setWidget(sidebar)
        split.addWidget(side_area)
        split.addWidget(area, 1)
        outer.insertWidget(0, body, 1)

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
        self.setup_next = button(
            "マイク基準へ →", lambda: self.tabs.setCurrentIndex(Page.REFERENCE), primary=True
        )
        layout.addWidget(self.setup_next, 0, Qt.AlignmentFlag.AlignRight)

    def _reference_tab(self):
        layout = self._page("2  マイク基準", scroll=True)
        layout.addWidget(self.editor.blocks["reference"])
        self.reference_instruction = label("", muted=True)
        self.reference_status = label("未測定")
        layout.addWidget(
            row(label("校正器を装着"), self.reference_instruction, self.reference_status)
        )
        sensitivity_card, self.sensitivity_value = metric("入力系の感度 [V/Pa]")
        frequency_card, self.frequency_value = metric("基準周波数 [Hz]")
        stability_card, self.stability_value = metric("時間変動の幅 [dB]")
        layout.addWidget(row(sensitivity_card, frequency_card, stability_card))
        self.reference_file = label("—", muted=True)
        self.reference_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.reference_file)
        self.measure_button = button("基準を測定", self._measure_reference, primary=True)
        layout.addStretch()
        self.reference_next = button(
            "音声・開始音量へ →", lambda: self.tabs.setCurrentIndex(Page.PLAYBACK), primary=True
        )
        self._page_actions(
            self.measure_button, button("基準を読み込む", self._load_reference), self.reference_next
        )

    def _waveform_tab(self):
        layout = self._page("4  校正", scroll=True)
        self.calibration_source = label("音声未選択")
        layout.addWidget(
            row(
                self.calibration_source,
                button("← 音声・開始音量", lambda: self.tabs.setCurrentIndex(Page.PLAYBACK)),
            )
        )
        self.start_level_info = label("—", muted=True)
        layout.addWidget(self.start_level_info)
        self.queue_table = self._job_table(
            ["刺激", "目標 [dB SPL]", "状態", "実測 [dB SPL]", "電圧倍率 [V/デジタル値]"]
        )
        layout.addWidget(self.queue_table)
        layout.removeWidget(self.queue_table)
        layout.insertWidget(0, self.queue_table)
        self.execution_scope = QComboBox()
        self.execution_scope.setFixedWidth(160)
        for text, value in [
            ("未完了", "remaining"),
            ("選択した刺激", "selected"),
            ("すべて", "all"),
        ]:
            self.execution_scope.addItem(text, value)
            bind(self.execution_scope, "setItemText", text, self.execution_scope.count() - 1)
        self.execution_scope.currentIndexChanged.connect(self._refresh)
        self.skip_button = button("スキップ", self._skip_jobs)
        self.pause_after = QCheckBox()
        set_text(self.pause_after, "この刺激の完了後に停止")
        self.pause_after.toggled.connect(
            lambda checked: self._pause_after.set() if checked else self._pause_after.clear()
        )
        self.queue_progress = label("", muted=True)
        self.open_plan_button = button("一覧結果を開く", self._open_plan)
        queue_tools = row(
            self.execution_scope,
            self.skip_button,
            self.pause_after,
            self.queue_progress,
            self.open_plan_button,
        )
        queue_tools.layout().setContentsMargins(16, 6, 16, 4)
        self._page_layouts[Page.CALIBRATION].insertWidget(0, queue_tools)
        target_card, self.calibration_target = metric("目標 [dB SPL]")
        level_card, self.level_value = metric("実測 [dB SPL]")
        scale_card, self.scale_value = metric("電圧倍率 [V/デジタル値]")
        peak_card, self.peak_value = metric("出力 [Vpeak]")
        layout.addWidget(row(target_card, level_card, scale_card, peak_card))
        self.calibrate_button = button("校正を開始", self._calibrate, primary=True)
        self.calibration_hint = label("マイク基準が必要です", muted=True)
        self.iterations = QTableWidget(0, 5)
        set_headers(
            self.iterations,
            ["探索回", "実測音圧 [dB SPL]", "出力ピーク [V]", "誤差 [dB]", "SNR [dB]"],
        )
        self.iterations.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.iterations.verticalHeader().hide()
        self.iterations.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.iterations.setMinimumHeight(140)
        layout.addWidget(self.iterations, 1)
        self.result_file = label("—", muted=True)
        self.result_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_result_button = button("結果を開く", self._open_result)
        self.open_result_button.setEnabled(False)
        layout.addWidget(row(self.result_file, self.open_result_button))
        self.job_error = label("", muted=True)
        self.job_error.setWordWrap(True)
        self.job_error.hide()
        layout.addWidget(self.job_error)
        search = QWidget()
        search_layout = QVBoxLayout(search)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.addWidget(self.editor.blocks["search"])
        self.manual_start_button = button("指定した初期倍率を採用", self._adopt_manual_start)
        search_layout.addWidget(self.manual_start_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(disclosure("探索条件・初期倍率を直接指定", search))
        self._page_actions(self.calibrate_button, self.calibration_hint)

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
        layout = self._page("履歴・再現", scroll=True)
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
            result = load_plan(path) if (Path(path) / "plan.json").is_file() else load_replay(path)
            if cancelled():
                raise MeasurementCancelled("Measurement cancelled")
            return result

        self._guard(lambda: self._start("restore", task))

    def _microphone_panel(self):
        panel = QFrame()
        panel.setObjectName("microphonePanel")
        panel.setFixedHeight(246)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.editor.band)
        self.monitor_start = button("マイク確認", self._toggle_monitor)
        self.monitor_start.setMaximumWidth(116)
        self.spectrum_state = label("待機中", muted=True)
        self.monitor_rms = label("—")
        self.monitor_peak = label("—")
        self.monitor_level = label("未校正")
        self.save_capture = QCheckBox()
        set_text(self.save_capture, "入力を保存")
        bind(
            self.save_capture,
            "setToolTip",
            "マイク確認・テスト再生の生入力を保存（テスト再生は出力電圧も保存）",
        )
        self.open_monitor_button = button("記録", self._open_monitor)
        self.open_monitor_button.setMaximumWidth(62)
        self.open_monitor_button.setEnabled(False)
        header = row(
            self.monitor_start,
            self.spectrum_state,
            label("RMS [V]", muted=True),
            self.monitor_rms,
            label("Peak [V]", muted=True),
            self.monitor_peak,
            label("SPL [dB]", muted=True),
            self.monitor_level,
            self.save_capture,
            self.open_monitor_button,
        )
        layout.addWidget(header)
        self.monitor_spectrum = SpectrumView()
        bind(
            self.monitor_spectrum,
            "setToolTip",
            "暗い部分は音圧計算の対象外です。スペクトラムは入力電圧を表示します。",
        )
        self.monitor_spectrum.setMinimumHeight(160)
        layout.addWidget(self.monitor_spectrum, 1)
        self.monitor_samples = np.empty(0)
        self.monitor_duration_s = 0.0
        return panel

    def _toggle_monitor(self):
        if self.worker is not None and self.job_kind == "monitor":
            self._request_stop()
        else:
            self._start_monitor()

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
            simulate, save_capture = self.simulate, self.save_capture.isChecked()
            output = self._output_path("monitor")
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
            set_text(self.monitor_level, "クリップ")
        else:
            set_text(self.spectrum_state, "取得中 · {rate:,} Hz", rate=rate)
            self.spectrum_state.setStyleSheet("")

        try:
            target = self.target.value()
        except ValueError:
            target = self.level_meter.target
        self.level_meter.set_levels(item["level_spl_db"], target, item["clipped"])

    def _playback_tab(self):
        layout = self._page("3  音声・開始音量", scroll=True)
        self.stimulus_table = self._job_table(["刺激", "グループ", "目標", "音量"])
        self.stimulus_table.horizontalHeaderItem(2).setToolTip("dB SPL")
        bind(self.stimulus_table.horizontalHeaderItem(3), "setToolTip", "開始音量", scope=self)
        layout.addWidget(self.stimulus_table)
        self.group_name = QLineEdit()
        self.group_name.setFixedWidth(120)
        bind(self.group_name, "setPlaceholderText", "グループ（任意）")
        self.add_sources_button = button("追加…", lambda: self._choose_sources(append=True))
        self.remove_sources_button = button("削除", lambda: self._guard(self._remove_jobs))
        self.duplicate_sources_button = button("複製", self._duplicate_jobs)
        self.apply_target_button = button("目標を選択行に適用", self._apply_targets)
        self.apply_group_button = button("グループを適用", self._apply_group)
        self.save_plan_button = button("計画を保存", self._save_plan)
        self.load_plan_button = button("計画を読み込む", self._choose_plan)
        self.preparation_count = label("", muted=True)
        queue_tools = row(
            self.add_sources_button,
            self.remove_sources_button,
            self.duplicate_sources_button,
            button("↑", lambda: self._guard(lambda: self._move_job(-1))),
            button("↓", lambda: self._guard(lambda: self._move_job(1))),
            self.save_plan_button,
            self.load_plan_button,
            self.preparation_count,
        )
        queue_tools.layout().setContentsMargins(16, 6, 16, 4)
        sidebar = QWidget()
        sidebar.setFixedWidth(345)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(4)
        self.stimulus_table.setMaximumHeight(16777215)
        self.stimulus_table.setMinimumHeight(70)
        self.stimulus_table.hideColumn(1)  # Group is also shown alongside the name.
        sidebar_layout.addWidget(self.stimulus_table, 1)
        bulk = QWidget()
        bulk_layout = QVBoxLayout(bulk)
        bulk_layout.setContentsMargins(0, 0, 0, 0)
        bulk_layout.addWidget(self.apply_target_button)
        bulk_layout.addWidget(row(self.group_name, self.apply_group_button))
        sidebar_layout.addWidget(disclosure("一括設定", bulk))
        self._page_sidebar(Page.PLAYBACK, sidebar)
        self._page_layouts[Page.PLAYBACK].insertWidget(0, queue_tools)
        self.source_kind = QComboBox()
        self.source_kind.setFixedWidth(140)
        for index, source in enumerate(["WAVファイル", "保存波形 (NPZ)", "Python生成関数"]):
            self.source_kind.addItem(source)
            bind(self.source_kind, "setItemText", source, index)
        self.source_location = QLineEdit()
        bind(self.source_location, "setPlaceholderText", "音声ファイルを選択")
        layout.addWidget(
            row(
                self.source_kind,
                self.source_location,
                button("選択…", self._choose_source),
                button("1 kHz", self._choose_test_tone),
            )
        )
        self.function_name = QLineEdit("make_sound")
        self.function_name.setMaximumWidth(220)
        self.generator_row = row(label("関数名", muted=True), self.function_name)
        self.generator_row.layout().addStretch()
        layout.addWidget(self.generator_row)
        self.parameters_path = QLineEdit()
        bind(self.parameters_path, "setPlaceholderText", "パラメータYAML（任意）")
        self.parameters_row = row(self.parameters_path, button("選択…", self._choose_parameters))
        layout.addWidget(self.parameters_row)
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
        self.wav_channels.setMaximumWidth(170)
        self.preview_button = button("波形を確認", lambda: self._prepare(playback_preview=True))
        self.shared_rate = label("", muted=True)
        self.stimulus_info = label("音声未選択", muted=True)
        layout.addWidget(row(self.wav_channels, self.preview_button, self.stimulus_info))
        self.waveform = WaveformView(empty_text="音声を選択")
        layout.addWidget(self.waveform)
        details = QWidget()
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.playback_channel_info = label("", muted=True)
        self.playback_file_info = label("", muted=True)
        self.playback_hardware = label("", muted=True)
        for widget in (
            self.shared_rate,
            self.playback_channel_info,
            self.playback_file_info,
            self.playback_hardware,
        ):
            detail_layout.addWidget(widget)
        layout.addWidget(disclosure("波形情報・接続", details))
        layout.addWidget(disclosure("再生区間", self.editor.blocks["window"]))
        levels = QFrame()
        level_layout = QHBoxLayout(levels)
        level_layout.setContentsMargins(12, 6, 12, 6)
        target_panel = QWidget()
        target_layout = QVBoxLayout(target_panel)
        target_layout.setContentsMargins(0, 0, 12, 0)
        target_layout.addWidget(label("目標 [dB SPL]", muted=True))
        self.target = TargetInput()
        self.target.setFixedWidth(100)
        self.target.setAlignment(Qt.AlignmentFlag.AlignRight)
        target_layout.addWidget(self.target)
        level_layout.addWidget(target_panel)
        fader_panel = QWidget()
        fader_layout = QVBoxLayout(fader_panel)
        fader_layout.setContentsMargins(0, 0, 12, 0)
        fader_layout.setSpacing(2)
        self.volume_text = label("")
        fader_layout.addWidget(self.volume_text)
        self.volume_fader = QSlider(Qt.Orientation.Horizontal)
        self.volume_fader.setRange(0, 1000)
        self.volume_fader.setValue(100)
        self.volume_fader.setMinimumWidth(200)
        self.volume_fader.valueChanged.connect(self._volume_changed)
        bind(self.volume_fader, "setToolTip", "100%は設定した出力電圧上限")
        fader_layout.addWidget(self.volume_fader)
        self.level_meter = LevelMeter()
        fader_layout.addWidget(self.level_meter)
        level_layout.addWidget(fader_panel, 1)
        command, self.playback_peak = metric("出力 [Vpeak]")
        command.setFixedWidth(160)
        level_layout.addWidget(command)
        levels.setFixedHeight(100)
        level_container = QWidget()
        level_container_layout = QVBoxLayout(level_container)
        level_container_layout.setContentsMargins(16, 0, 16, 4)
        level_container_layout.addWidget(levels)
        self._page_layouts[Page.PLAYBACK].addWidget(level_container)
        self.playback_start = button("テスト再生", self._test_playback, primary=True)
        self.playback_stop = button("停止", self._request_stop)
        self.playback_stop.setEnabled(False)
        self.mute_button = button("ミュート", lambda: self.volume_fader.setValue(0))
        self.loop_audio = QCheckBox()
        set_text(self.loop_audio, "繰り返す")
        self.loop_audio.setChecked(True)
        self.adopt_start_button = button(
            "この音量で校正へ →", self._use_playback_as_start, primary=True
        )
        self.adopt_start_button.setEnabled(False)
        self._page_actions(
            self.playback_start,
            self.playback_stop,
            self.mute_button,
            self.loop_audio,
            self.adopt_start_button,
        )
        layout.addStretch()
        self._volume_changed()

    def _job_table(self, columns):
        table = QTableWidget(0, len(columns))
        set_headers(table, columns)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(90)
        table.setMaximumHeight(155)
        table.currentCellChanged.connect(
            lambda row, column, old_row, old_column: self._select_job_row(table, row)
        )
        table.itemSelectionChanged.connect(self._refresh)
        return table

    def _active_job(self):
        return next((j for j in self.jobs if j.id == self._active_job_id), None)

    def _selected_job_ids(self, table):
        return {
            table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in table.selectionModel().selectedRows()
            if table.item(index.row(), 0) is not None
        }

    def _sync_active_job(self):
        if self._switching_job or self.stimulus is None:
            return self._active_job()
        setup = self.editor.setup()
        target = self.target.value()
        source = asdict(self._source())
        source["parameters"] = str(source["parameters"]) if source["parameters"] else None
        job = self._active_job()
        if job is None:
            job = CalibrationJob(
                Path(
                    source["location"].rpartition(":")[0]
                    if source["kind"] == "generator"
                    else source["location"]
                ).name,
                self.stimulus,
                setup.measurement,
                target,
                source=source,
                parent_run=self._parent_run,
            )
            self.jobs.append(job)
            self._active_job_id = job.id
        else:
            changed = (
                job.stimulus is not self.stimulus
                or job.measurement != setup.measurement
                or job.target_spl_db != target
            )
            if job.stimulus is not self.stimulus or (
                job.measurement.start_scale_v != setup.measurement.start_scale_v
            ):
                job.start_context = None
                job.start_evidence = None
            if changed:
                job.status, job.result, job.error, job.iterations = "pending", None, None, []
                job.output = None
            if job.source != source:
                location = (
                    source["location"].rpartition(":")[0]
                    if source["kind"] == "generator"
                    else source["location"]
                )
                job.name = Path(location).name
            job.stimulus, job.measurement, job.target_spl_db = (
                self.stimulus,
                setup.measurement,
                target,
            )
            job.source = source
            job.parent_run = self._parent_run
        return job

    def _select_job_row(self, table, row):
        if self._switching_job or self.worker is not None or row < 0:
            return
        item = table.item(row, 0)
        if item is not None:
            job_id = item.data(Qt.ItemDataRole.UserRole)
            if job_id != self._active_job_id:
                self._guard(lambda: self._activate_job(job_id, selection_table=table))

    def _activate_job(self, job_id, *, running=False, selection_table=None):
        job = next(j for j in self.jobs if j.id == job_id)
        if not running:
            if (
                self._active_job_id is not None
                and self.stimulus is None
                and self.source_location.text().strip()
            ):
                raise ValueError(translate(self, "先に波形を確認してください"))
            self._sync_active_job()
        setup = self._queue_setup if running else self.editor.setup()
        self._switching_job = True
        try:
            self._active_job_id = job.id
            self._parent_run = job.parent_run
            self.editor.set_setup(replace(setup, measurement=job.measurement))
            source = job.source
            kind = source.get("kind", "npz")
            self.source_kind.setCurrentIndex(["wav", "npz", "generator"].index(kind))
            location = source.get("location", job.name)
            if kind == "generator":
                location, _, function = location.rpartition(":")
                self.function_name.setText(function)
            self.source_location.setText(location)
            self.parameters_path.setText(source.get("parameters") or "")
            self.wav_channels.setCurrentIndex(
                max(0, self.wav_channels.findData(source.get("wav_channel_mode", "mono")))
            )
            self.generator_row.setVisible(kind == "generator")
            self.parameters_row.setVisible(kind == "generator")
            self.wav_channels.setVisible(kind == "wav")
            self.target.setValue(job.target_spl_db)
            self.level_meter.set_levels(None, job.target_spl_db)
            set_text(self.monitor_level, "—")
            self._playback_start_choice = None
            self._pending_start_choice = None
            fraction = (job.start_evidence or {}).get("volume_fraction", 0.1)
            self.volume_fader.setValue(round(fraction * 1000))
            self._clear_results()
            self._show_stimulus(job.stimulus)
            for table in (self.stimulus_table, self.queue_table):
                row_number = self.jobs.index(job)
                blocker = QSignalBlocker(table)
                table.setCurrentCell(row_number, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                if table is not selection_table or not table.selectionModel().hasSelection():
                    table.selectRow(row_number)
                table.scrollToItem(table.item(row_number, 0))
                del blocker
            self._display_job_result(job)
        finally:
            self._switching_job = False
        self._refresh()

    def _display_job_result(self, job):
        self.iterations.setRowCount(0)
        for iteration in job.iterations:
            self._iteration(iteration)
        if job.result:
            set_value(self.level_value, f"{job.result['measured_spl_db']:.2f}")
            set_value(self.scale_value, f"{job.result['scale_v']:.7g}")
            set_value(self.peak_value, f"{job.result['output_peak_v']:.6g}")
        if job.output and job.output.is_dir():
            self.last_output = job.output
            set_value(
                self.result_file,
                str(job.output / ("result.json" if job.result else "failure.json")),
            )
            self.open_result_button.setEnabled(True)
        if job.error:
            set_text(self.calibration_hint, "{error}", error=error_message(ValueError(job.error)))
            self.calibration_hint.setToolTip(job.error)
        self.job_error.setVisible(bool(job.error))
        set_text(self.job_error, "{error}", error=error_message(ValueError(job.error or "")))

    def _update_job_tables(self, setup):
        if self._switching_job:
            return
        states = {
            "pending": "待機",
            "running": "実行中",
            "complete": "成功",
            "failed": "失敗",
            "cancelled": "停止",
            "skipped": "スキップ済み",
        }
        for table in (self.stimulus_table, self.queue_table):
            selected = self._selected_job_ids(table)
            blocker = QSignalBlocker(table)
            table.setRowCount(len(self.jobs))
            for index, job in enumerate(self.jobs):
                ready = setup is not None and job.ready(setup, self.simulate)
                if table is self.stimulus_table:
                    values = [
                        f"{job.group} / {job.name}" if job.group else job.name,
                        job.group or "—",
                        f"{job.target_spl_db:g}",
                        "設定済み" if ready else "未設定",
                    ]
                else:
                    values = [
                        f"{job.group} / {job.name}" if job.group else job.name,
                        f"{job.target_spl_db:g}",
                        states[job.status],
                        f"{job.result['measured_spl_db']:.2f}" if job.result else "—",
                        f"{job.result['scale_v']:.7g}" if job.result else "—",
                    ]
                for column, value in enumerate(values):
                    translated = column == (3 if table is self.stimulus_table else 2)
                    cell = (
                        set_cell(table, index, column, value)
                        if translated
                        else set_cell(table, index, column, "{value}", value=value)
                    )
                    cell.setData(Qt.ItemDataRole.UserRole, job.id)
                    cell.setSelected(job.id in selected)
                    if column == 0:
                        cell.setToolTip(f"{job.group} / {job.name}" if job.group else job.name)
                    if job.error:
                        cell.setToolTip(job.error)
                if job.id == self._active_job_id:
                    table.setCurrentCell(index, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if not selected and self._active_job_id:
                for index, job in enumerate(self.jobs):
                    if job.id == self._active_job_id:
                        table.selectRow(index)
            del blocker

    def _execution_ids(self):
        scope = self.execution_scope.currentData()
        if scope == "selected":
            return self._selected_job_ids(self.queue_table)
        if len(self.jobs) == 1 and self.jobs[0].status == "complete":
            return {self.jobs[0].id}
        return {
            j.id for j in self.jobs if scope == "all" or j.status not in {"complete", "skipped"}
        }

    def _adopt_manual_start(self):
        def adopt():
            if self.stimulus is None:
                raise ValueError(translate(self, "先に波形を確認してください"))
            job = self._sync_active_job()
            setup = self.editor.setup()
            measurement_bounds(self.stimulus, setup)
            job.start_context = starting_context(setup, self.simulate)
            job.start_evidence = {"method": "manual", "scale_v": job.measurement.start_scale_v}
            self._audit("start_level_selected", job_id=job.id, **job.start_evidence)
            self._refresh()

        self._guard(adopt)

    def _apply_targets(self):
        def apply():
            self._sync_active_job()
            value = self.target.value()
            for job in self.jobs:
                if (
                    job.id in self._selected_job_ids(self.stimulus_table)
                    and job.target_spl_db != value
                ):
                    job.target_spl_db = value
                    job.status, job.result, job.output, job.iterations = "pending", None, None, []
            self._refresh()

        self._guard(apply)

    def _apply_group(self):
        for job in self.jobs:
            if job.id in self._selected_job_ids(self.stimulus_table):
                job.group = self.group_name.text().strip()
        self._refresh()

    def _move_job(self, delta):
        self._sync_active_job()
        job = self._active_job()
        if job is None:
            return
        index = self.jobs.index(job)
        destination = index + delta
        if 0 <= destination < len(self.jobs):
            self.jobs[index], self.jobs[destination] = self.jobs[destination], self.jobs[index]
            self._refresh()

    def _remove_jobs(self):
        selected = self._selected_job_ids(self.stimulus_table)
        self.jobs = [job for job in self.jobs if job.id not in selected]
        if self._active_job_id in selected:
            self._active_job_id = None
            self._switching_job = True
            self.source_location.clear()
            self._invalidate_stimulus()
            self._switching_job = False
            if self.jobs:
                self._activate_job(self.jobs[0].id)
        self._refresh()

    def _duplicate_jobs(self):
        def duplicate():
            self._sync_active_job()
            selected = self._selected_job_ids(self.stimulus_table)
            duplicates = [
                replace(
                    job,
                    id=uuid4().hex,
                    status="pending",
                    result=None,
                    output=None,
                    error=None,
                    iterations=[],
                )
                for job in self.jobs
                if job.id in selected
            ]
            self.jobs.extend(duplicates)
            self._refresh()
            if duplicates:
                self._activate_job(duplicates[0].id)

        self._guard(duplicate)

    def _skip_jobs(self):
        for job in self.jobs:
            if job.id in self._selected_job_ids(self.queue_table) and job.status != "complete":
                job.status = "skipped"
        self._guard(
            lambda: self._audit(
                "queue_skipped", job_ids=sorted(self._selected_job_ids(self.queue_table))
            )
        )
        self._refresh()

    def _choose_plan(self):
        path = QFileDialog.getExistingDirectory(
            self, translate(self, "計画のフォルダを選択"), self.output_root.text()
        )
        if path:
            self._restore(path)

    def _save_plan(self):
        def save():
            self._sync_active_job()
            if self.stimulus is None:
                raise ValueError(translate(self, "先に波形を確認してください"))
            setup, reference, simulate = self.editor.setup(), self.reference, self.simulate
            jobs = [replace(job) for job in self.jobs]
            output, parent = self._output_path("plan"), self.last_plan
            self._start(
                "save_plan",
                lambda cancelled, progress: save_plan(
                    output,
                    jobs,
                    setup=setup,
                    reference=reference,
                    simulate=simulate,
                    parent=parent,
                    cancel_requested=cancelled,
                ),
            )

        self._guard(save)

    def _apply_plan(self, plan):
        self._switching_job = True
        try:
            self.jobs = plan.jobs
            self.execution_scope.setCurrentIndex(0)
            self._active_job_id = None
            self.editor.set_setup(plan.setup)
            self.mode.setCurrentIndex(0 if plan.simulate else 1)
            self.reference = plan.reference
            if plan.reference:
                self._set_reference(plan.reference, plan.directory / "reference.json")
            else:
                set_text(self.reference_file, "—")
                for value in (self.sensitivity_value, self.frequency_value, self.stability_value):
                    set_text(value, "—")
            self.last_plan = plan.directory
            self.stimulus = None
        finally:
            self._switching_job = False
        self._update_job_tables(plan.setup)
        selected = next(
            (job for job in self.jobs if job.status not in {"complete", "skipped"}), self.jobs[0]
        )
        self._activate_job(selected.id)
        self.tabs.setCurrentIndex(Page.CALIBRATION)
        self._refresh()

    def _choose_sources(self, *, append):
        paths, _ = QFileDialog.getOpenFileNames(
            self, translate(self, "音声を追加"), "", "Audio (*.wav *.npz)"
        )
        if paths:
            self._import_sources(paths, append=append)

    def _import_sources(self, paths, *, append=True):
        def prepare():
            self._sync_active_job()
            setup, target = self.editor.setup(), self.target.value()
            channel_mode = self.wav_channels.currentData()
            self._import_append = append

            def task(cancelled, progress):
                jobs = []
                for path in paths:
                    if cancelled():
                        raise MeasurementCancelled("Measurement cancelled")
                    source = StimulusSource(
                        "npz" if Path(path).suffix.lower() == ".npz" else "wav",
                        str(path),
                        wav_channel_mode=channel_mode,
                    )
                    stimulus = source.prepare(setup.daq.sample_rate_hz)
                    jobs.append(
                        CalibrationJob(
                            Path(path).name,
                            stimulus,
                            setup.measurement,
                            target,
                            source=asdict(source),
                        )
                    )
                return jobs

            self._start("import_sources", task)

        self._guard(prepare)

    def _volume_changed(self):
        fraction = self.volume_fader.value() / 1000
        self.volume_control.set(fraction)
        try:
            limit = self.editor.setup().daq.max_peak_v
        except (ValueError, TypeError):
            limit = 0.0
        set_text(
            self.volume_text,
            "{volume:.1f}%",
            volume=fraction * 100,
        )
        bind(
            self.volume_fader,
            "setToolTip",
            "100% = {peak:g} Vpeak",
            peak=limit,
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
            or setup.reference_fingerprint != choice["reference_fingerprint"]
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
        job = self._sync_active_job()
        if job is not None:
            job.start_context = starting_context(self.editor.setup(), self.simulate)
            job.start_evidence = dict(choice)
        self._clear_results()
        self._refresh()
        next_job = next(
            (j for j in self.jobs if not j.ready(self.editor.setup(), self.simulate)), None
        )
        if next_job is not None and len(self.jobs) > 1:
            self._activate_job(next_job.id)
        else:
            self.tabs.setCurrentIndex(Page.CALIBRATION)
        set_text(
            self.status,
            "開始音量を設定しました",
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
        self.tabs.setCurrentIndex(Page.PLAYBACK)
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
            loop, save_capture = self.loop_audio.isChecked(), self.save_capture.isChecked()
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
        setup = self.editor.setup()
        self._playback_start_choice = (
            {
                "stimulus_sha256": self._stimulus_info["sha256"],
                "daq": asdict(setup.daq),
                "reference_fingerprint": setup.reference_fingerprint,
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
            self.status,
            "再生中 · {duration:.1f} s · {volume:.1f}%",
            duration=item["duration_s"],
            volume=item["volume_fraction"] * 100,
        )

    def _apply_replay(self, replay):
        self.jobs = []
        self._active_job_id = None
        self.stimulus = None
        self.editor.set_setup(replay.setup)
        set_value(self.setup_path, str(replay.directory / "setup.json"))
        self.mode.setCurrentIndex(0 if replay.backend == "simulation" else 1)
        self.source_kind.setCurrentIndex(1)
        self.source_location.setText(str(replay.directory / "stimulus.npz"))
        self.target.setValue(replay.target_spl_db)
        self._set_reference(replay.reference, replay.directory / "reference.json")
        self._show_stimulus(replay.stimulus)
        self._parent_run = replay.parent_record
        job = self._sync_active_job()
        job.start_context = starting_context(replay.setup, self.simulate)
        job.start_evidence = {"method": "restored", "scale_v": job.measurement.start_scale_v}
        self.tabs.setCurrentIndex(Page.CALIBRATION)
        self._refresh()
        set_text(
            self.status,
            "条件を復元しました",
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
                    key: {
                        "mode": widget.mode.currentData(),
                        "low_hz": widget.low.text(),
                        "high_hz": widget.high.text(),
                    }
                    if isinstance(widget, BandInput)
                    else widget.currentText()
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
                "save_capture": self.save_capture.isChecked(),
            },
            "queue": {
                "parent_plan": str(self.last_plan) if self.last_plan else None,
                "jobs": [
                    {
                        "id": job.id,
                        "name": job.name,
                        "group": job.group,
                        "target_spl_db": job.target_spl_db,
                        "measurement": asdict(job.measurement),
                    }
                    for job in self.jobs
                ],
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
                set_text(self.status, "基準を読み込みました")

            self._guard(load)

    def _set_reference(self, reference, path):
        self._clear_results()
        self.reference = reference
        set_value(self.reference_file, f"{Path(path).name} · {reference.measured_at}")
        self.reference_file.setToolTip(str(path))
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
        if self._switching_job:
            return
        self._playback_start_choice = None
        self._clear_results()
        self._refresh()

    @Slot()
    def _setup_changed(self):
        self.editor.update_band_limit()
        if self._switching_job:
            return
        try:
            setup = self.editor.setup()
        except (ValueError, TypeError):
            setup = None
        band = (
            (setup.measurement.band_hz, setup.measurement.frequency_correction_db)
            if setup is not None
            else None
        )
        if band != self._meter_band:
            self._playback_start_choice = None
        self._meter_band = band
        set_text(self.monitor_level, "—")
        self.level_meter.set_levels(None, self.level_meter.target)
        if (
            setup is not None
            and self.stimulus is not None
            and setup.daq.sample_rate_hz != self.stimulus.sample_rate_hz
        ):
            self._invalidate_stimulus()
        else:
            self._clear_results()
            if self._playback_start_choice and (
                setup is None
                or self._playback_start_choice["reference_fingerprint"]
                != setup.reference_fingerprint
            ):
                self._playback_start_choice = None
        self._refresh()

    @Slot()
    def _target_changed(self):
        if self._switching_job:
            return
        self._clear_results()
        try:
            self.level_meter.set_levels(
                self.level_meter.level, self.target.value(), self.level_meter.clipped
            )
        except ValueError:
            pass
        self._refresh()

    @Slot()
    def _source_changed(self):
        if self._switching_job:
            return
        generator = self.source_kind.currentIndex() == 2
        self.generator_row.setVisible(generator)
        self.parameters_row.setVisible(generator)
        self.wav_channels.setVisible(self.source_kind.currentIndex() == 0)
        self._invalidate_stimulus()
        self._refresh()

    def _clear_results(self):
        self.iterations.setRowCount(0)
        for value in (self.level_value, self.scale_value, self.peak_value):
            set_text(value, "—")
        set_text(self.result_file, "—")
        self.last_output = None
        self.open_result_button.setEnabled(False)
        self.job_error.hide()

    def _invalidate_stimulus(self):
        self.stimulus = None
        self._stimulus_info = None
        self._playback_start_choice = None
        self.waveform.set_stimulus(None)
        set_text(self.stimulus_info, "音声未選択")
        self._clear_results()
        set_text(self.playback_channel_info, "AO 1 ch / モノラル波形")
        set_text(self.playback_file_info, "音声未選択")

    def _refresh(self):
        if self._switching_job:
            return
        self._update_adopt_button()
        if hasattr(self, "jobs") and len(self.jobs) > 1:
            set_text(self.adopt_start_button, "この開始音量を採用")
        else:
            set_text(self.adopt_start_button, "この音量で校正へ →")
        self._update_start_level_info()
        if self.worker is not None:
            return
        try:
            setup = self.editor.setup()
        except (ValueError, TypeError) as error:
            self._update_job_tables(None)
            set_text(
                self.shared_rate, "設定を確認してください: {error}", error=error_message(error)
            )
            self.measure_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.calibrate_button.setEnabled(False)
            set_text(self.reference_status, "設定の入力を確認してください。")
            self.monitor_start.setEnabled(False)
            self.playback_start.setEnabled(False)
            self.setup_next.setEnabled(False)
            self.reference_next.setEnabled(False)
            self.save_plan_button.setEnabled(False)
            self.manual_start_button.setEnabled(False)
            return
        self.setup_next.setEnabled(True)
        self.monitor_spectrum.nyquist = setup.daq.sample_rate_hz / 2
        self.monitor_spectrum.analysis_band = setup.measurement.band_hz
        self.monitor_spectrum.update()
        if self.stimulus is not None:
            duration = len(self.stimulus.samples) / self.stimulus.sample_rate_hz
            self.waveform.set_selection(
                setup.measurement.start_s, setup.measurement.stop_s or duration
            )
        set_text(
            self.shared_rate,
            "SR {rate:,} Hz",
            rate=setup.daq.sample_rate_hz,
        )
        set_text(
            self.reference_instruction,
            "AI {channel}",
            level=setup.reference_level_db,
            frequency=setup.reference_frequency_hz,
            duration=setup.reference_duration_s,
            channel=setup.daq.input_channel,
            rate=setup.daq.sample_rate_hz,
        )
        valid = False
        if self.reference is None:
            set_text(self.reference_status, "未測定")
        else:
            try:
                validate_reference(setup, self.reference)
                self._check_backend(self.reference)
                valid = True
                set_text(self.reference_status, "使用可能")
            except ValueError:
                set_text(
                    self.reference_status,
                    "再基準測定が必要",
                )
        selected = bool(self.source_location.text().strip())
        set_value(
            self.calibration_source,
            Path(self._source().location).name if selected else translate(self, "音声未選択"),
        )
        self.reference_next.setEnabled(valid)
        self.playback_start.setEnabled(selected)
        self._volume_changed()
        try:
            target = self.target.value()
            set_value(self.calibration_target, f"{target:g}")
            valid_target = True
        except ValueError:
            set_value(self.calibration_target, "—")
            valid_target = False
        self.measure_button.setEnabled(True)
        self.monitor_start.setEnabled(True)
        self.preview_button.setEnabled(selected)
        self.calibrate_button.setEnabled(valid and selected and valid_target)
        set_text(
            self.calibration_hint,
            "目標音圧を確認してください"
            if not valid_target
            else "準備完了"
            if valid and selected
            else "音声を選択してください"
            if valid
            else "マイク基準が必要です",
        )
        self.open_result_button.setEnabled(self.last_output is not None)
        self.open_monitor_button.setEnabled(self.last_monitor_output is not None)
        try:
            self._sync_active_job()
        except (ValueError, TypeError):
            pass
        for job in self.jobs:
            if job.result and not job.ready(setup, self.simulate):
                job.status, job.result, job.output, job.error, job.iterations = (
                    "pending",
                    None,
                    None,
                    None,
                    [],
                )
        self._update_job_tables(setup)
        set_text(
            self.preparation_count,
            "準備 {ready} / {count}",
            ready=sum(j.ready(setup, self.simulate) for j in self.jobs),
            count=len(self.jobs),
        )
        set_text(
            self.queue_progress,
            "{done} / {total}件完了",
            done=sum(j.status == "complete" for j in self.jobs),
            total=len(self.jobs),
        )
        ids = self._execution_ids()
        ready = all(j.ready(setup, self.simulate) for j in self.jobs if j.id in ids)
        self.save_plan_button.setEnabled(
            bool(self.jobs) and self.stimulus is not None and valid_target
        )
        self.manual_start_button.setEnabled(self.stimulus is not None and valid_target)
        self.remove_sources_button.setEnabled(bool(self.jobs))
        self.skip_button.setEnabled(bool(self._selected_job_ids(self.queue_table)))
        self.pause_after.setEnabled(False)
        self.open_plan_button.setEnabled(self.last_plan is not None)
        for control in (
            self.execution_scope,
            self.skip_button,
            self.pause_after,
            self.queue_progress,
        ):
            control.setVisible(len(self.jobs) > 1)
        if len(self.jobs) > 1:
            self.calibrate_button.setEnabled(
                valid and bool(ids) and ready and valid_target and self.stimulus is not None
            )
            set_text(self.calibrate_button, "{count}件を開始", count=len(ids))
            set_text(
                self.calibration_hint,
                "開始音量が未設定の刺激があります"
                if not ready
                else "準備完了"
                if ids
                else "実行する刺激を選択してください",
            )
        else:
            set_text(self.calibrate_button, "校正を開始")
            if self.jobs and not ids:
                self.calibrate_button.setEnabled(False)

    def _choose_source(self):
        index = self.source_kind.currentIndex()
        if index != 2:
            self._choose_sources(append=False)
            return
        filters = ["WAV (*.wav)", "保存波形 (*.npz)", "Python (*.py)"]
        path, _ = QFileDialog.getOpenFileName(
            self, translate(self, "波形・生成関数を選択"), "", translate(self, filters[index])
        )
        if path:
            self.source_location.setText(path)
            self._prepare(playback_preview=True)

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
            f"{rate} · {info['duration_s']:.3f} s",
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
        if not self._switching_job:
            self._sync_active_job()
        self._update_start_level_info()
        self._refresh()

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
            set_text(self.start_level_info, "開始音量を選択してください")
            return
        try:
            setup = self.editor.setup()
            peak = self._selected_waveform_peak(setup)
        except (ValueError, TypeError):
            set_text(self.start_level_info, "設定の入力を確認してください。")
            return
        set_text(
            self.start_level_info,
            "区間 {start:g}–{stop:g} s · 開始 {voltage:.6g} Vpeak",
            start=setup.measurement.start_s,
            stop=setup.measurement.stop_s
            or len(self.stimulus.samples) / self.stimulus.sample_rate_hz,
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
            job = self._sync_active_job()
            # Preserve direct single-stimulus use with explicitly configured search
            # settings. A queue requires a checked starting level for every item.
            if len(self.jobs) == 1 and not job.ready(setup, self.simulate):
                measurement_bounds(job.stimulus, setup)
                job.start_context = starting_context(setup, self.simulate)
                job.start_evidence = {
                    "method": "configured",
                    "scale_v": job.measurement.start_scale_v,
                }
            selected_ids = self._execution_ids()
            if not selected_ids:
                raise ValueError(translate(self, "実行する刺激を選択してください"))
            if any(not j.ready(setup, self.simulate) for j in self.jobs if j.id in selected_ids):
                raise ValueError(translate(self, "開始音量が未設定の刺激があります"))
            output = self._output_path("batch")
            if not self._confirm_hardware(setup):
                return
            reference, simulate = self.reference, self.simulate
            jobs, parent = [replace(j) for j in self.jobs], self.last_plan
            self._clear_results()
            self.job_output = output
            self._queue_setup = setup
            self._pause_after.clear()
            self.pause_after.setChecked(False)

            def task(cancelled, progress):
                return run_queue(
                    output,
                    jobs,
                    selected_ids=selected_ids,
                    setup=setup,
                    reference=reference,
                    simulate=simulate,
                    parent=parent,
                    on_progress=progress,
                    cancel_requested=cancelled,
                    pause_after=self._pause_after.is_set,
                    hardware_factory=make_hardware,
                )

            self._start("queue", task, max_iterations=setup.measurement.max_iterations)

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
        self.stimulus_table.setEnabled(not busy)
        self.queue_table.setEnabled(not busy)
        self.pause_after.setEnabled(busy and self.job_kind == "queue")
        monitoring = busy and self.job_kind == "monitor"
        self.monitor_start.setEnabled(not busy or monitoring)
        set_text(self.monitor_start, "停止" if monitoring else "マイク確認")
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
            if kind in {"reference", "calibrate", "queue", "monitor", "test_playback"}
            else None,
        )
        self.job_kind = kind
        if kind in {"reference", "calibrate", "queue", "monitor", "test_playback"}:
            self.monitor_samples = np.empty(0)
            self.monitor_duration_s = 0
            self.monitor_spectrum.clear()
            self.level_meter.set_levels(None, self.level_meter.target)
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
                "calibrate": "校正中…",
                "queue": "条件を保存中…",
                "save_plan": "条件を保存中…",
                "import_sources": "波形を準備しています…",
                "restore": "実行記録を照合して、条件を復元しています…",
                "monitor": "マイク確認中",
                "test_playback": "テスト再生中",
            }[kind],
        )
        self.worker.start()

    @Slot(dict)
    def _iteration(self, item):
        if item.get("kind") == "queue_job":
            self.job_output = Path(item["output"])
            job = next(j for j in self.jobs if j.id == item["job_id"])
            job.status, job.result, job.iterations = "running", None, []
            job.output, job.error = self.job_output, None
            self._activate_job(job.id, running=True)
            self._update_job_tables(self._queue_setup)
            self.progress.setRange(0, job.measurement.max_iterations)
            self.progress.setValue(0)
            set_text(
                self.queue_progress, "{index} / {total}件", index=item["index"], total=item["total"]
            )
            set_value(self.calibration_source, job.name)
            set_value(self.calibration_target, f"{job.target_spl_db:g}")
            self.monitor_samples = np.empty(0)
            self.monitor_spectrum.clear()
            return
        if item.get("kind") == "queue_outcome":
            job = next(j for j in self.jobs if j.id == item["job_id"])
            job.status, job.result, job.error = item["status"], item["result"], item["error"]
            job.output = Path(item["output"])
            self._update_job_tables(self._queue_setup)
            return
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
                set_text(self.status, "条件を保存中…")
            elif phase == "background":
                set_text(
                    self.status,
                    "背景音を測定中 · {duration:g} s",
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
        if self.worker is not None and self.job_kind == "queue":
            job = self._active_job()
            job.iterations.append({k: v for k, v in item.items() if k != "job_id"})
            row_number = self.jobs.index(job)
            set_cell(
                self.queue_table,
                row_number,
                3,
                "SNR不足" if item["measured_spl_db"] is None else "{value}",
                value=f"{item['measured_spl_db']:.2f}"
                if item["measured_spl_db"] is not None
                else "—",
            )
            set_cell(self.queue_table, row_number, 4, "{value}", value=f"{item['scale_v']:.7g}")
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
                "SNR不足 · 最大 +{step:g} dB/回",
                step=self.editor.setup().measurement.max_upward_step_db,
            )
        else:
            set_text(
                self.calibration_hint,
                "収束を確認中",
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
        if kind in {"reference", "calibrate", "queue", "monitor", "test_playback"}:
            set_text(
                self.spectrum_state,
                "停止 · 最終入力" if self.monitor_spectrum.spectrum is not None else "待機中",
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
            elif error is None and kind == "import_sources":
                extra = {
                    "stimuli": [
                        {
                            "job_id": job.id,
                            "source": job.source,
                            "saved_waveform": str(self.activity.save_waveform(job.stimulus)),
                        }
                        for job in result
                    ]
                }
            elif error is None and kind == "restore":
                extra = (
                    {"plan": str(result.directory), "job_ids": [job.id for job in result.jobs]}
                    if hasattr(result, "jobs")
                    else {
                        "restored_from": result.parent_record,
                        "stimulus_sha256": result.stimulus.sha256,
                    }
                )
            self._audit(
                "operation_finished",
                operation=kind,
                status="cancelled"
                if isinstance(error, MeasurementCancelled)
                else "failed"
                if error
                else "held"
                if kind == "queue" and result["status"] == "held"
                else "complete",
                error=str(error) if error else result["error"] if kind == "queue" else None,
                output=str(result["directory"])
                if kind == "queue" and result is not None
                else str(self.job_output)
                if kind in {"reference", "calibrate", "queue", "monitor", "test_playback"}
                else None,
                notes=getattr(error, "__notes__", []) if error else [],
                **extra,
            )
        except Exception as log_error:
            self._show_error(log_error)
            if self._closing:
                self.close()
            return
        if kind == "queue":
            if result is not None:
                self.jobs = result["jobs"]
                self.last_plan = result["directory"]
                self._queue_setup = None
                self._activate_job(self._active_job_id or self.jobs[0].id)
                set_text(
                    self.status,
                    "キューを保留しました"
                    if result["status"] == "held"
                    else "校正完了 · 結果を保存しました",
                )
                if result["error"]:
                    set_text(
                        self.status,
                        "キューを保留しました: {error}",
                        error=error_message(ValueError(result["error"])),
                    )
            else:
                self._queue_setup = None
                set_text(
                    self.status,
                    "停止しました。"
                    if isinstance(error, MeasurementCancelled)
                    else "エラー: {details}",
                    details=error_message(error),
                )
                if not isinstance(error, MeasurementCancelled) and not self._closing:
                    self._show_error(error)
            self._refresh()
            self._scan_history()
            if self._closing:
                self.close()
            return
        if error is None and kind == "save_plan":
            self.last_plan = result
            set_text(self.status, "計画を保存しました: {path}", path=result)
            self._scan_history()
            if self._closing:
                self.close()
            return
        if error is None and kind == "import_sources":
            old = self._active_job()
            if not self._import_append and old is not None:
                index = self.jobs.index(old)
                self.jobs[index : index + 1] = result
            else:
                self.jobs.extend(result)
            self._active_job_id, self.stimulus = None, None
            self._update_job_tables(self.editor.setup())
            self._activate_job(result[0].id)
            set_text(self.status, "音声を準備しました")
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
            if kind == "test_playback":
                set_text(self.status, "再生を停止しました。記録とエラーを確認してください。")
                set_text(self.playback_peak, "出力確認")
            if not isinstance(error, MeasurementCancelled) and not self._closing:
                self._show_error(error)
        elif kind in {"prepare", "prepare_calibration", "prepare_playback", "prepare_test_preview"}:
            self._show_stimulus(result)
            set_text(self.status, "音声を準備しました")
            if kind == "prepare_calibration" and not self._closing:
                self._calibrate()
            if kind == "prepare_playback" and not self._closing:
                self._test_playback()
        elif kind == "reference":
            self._set_reference(result, self.job_output)
            set_text(self.status, "基準を保存しました")
        elif kind == "restore":
            if hasattr(result, "jobs"):
                self._apply_plan(result)
            else:
                self._apply_replay(result)
        elif kind == "monitor":
            set_text(self.status, "モニター記録を保存しました")
        elif kind == "test_playback":
            set_value(self.playback_peak, "0")
            set_text(
                self.status,
                "停止 · {duration:.1f} s",
                duration=result["duration_s"],
                path=self.job_output,
            )
        else:
            set_text(self.level_value, f"{result.measured_spl_db:.2f}")
            set_value(self.scale_value, f"{result.scale_v:.7g}")
            set_value(self.peak_value, f"{result.output_peak_v:.6g}")
            self.last_output = self.job_output
            set_value(self.result_file, str(self.job_output / "result.json"))
            self.open_result_button.setEnabled(True)
            set_text(self.status, "校正完了 · 結果を保存しました")
        if kind == "calibrate":
            self._scan_history()
        if kind in {"monitor", "test_playback"} and self.job_output.is_dir():
            self.last_monitor_output = self.job_output
            self.open_monitor_button.setEnabled(True)
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
            self.monitor_start.setEnabled(False)
            set_text(self.stop_button, "停止待ち…")
            set_text(
                self.status,
                "停止要求済み：短い取得単位で中断し、AOを0 Vへ戻します。"
                if self.job_kind in {"reference", "calibrate", "queue", "monitor", "test_playback"}
                else "停止要求済み：波形準備の完了後に終了します。",
            )

    def _open_result(self):
        if self.last_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def _open_plan(self):
        if self.last_plan is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_plan)))

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
