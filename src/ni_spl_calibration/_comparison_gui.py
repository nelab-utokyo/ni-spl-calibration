"""A modeless, compact window for repeated measurements at fixed calibrated gain."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QVBoxLayout,
)

from .i18n import (
    Message,
    bind,
    error_message,
    set_cell,
    set_headers,
    set_text,
    set_value,
)


class ComparisonDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        # Use the application's language without adding another language selector.
        self.owner, self.language = owner, owner.language
        self.sources, self.rows = [], []
        self.last_output = None
        self.close_after_stop = False
        self.sequence = 0
        self._latest = {}
        bind(self, "setWindowTitle", "比較測定")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(860, 400)
        self.setMinimumSize(740, 360)
        from ._gui import button, label, metric, row

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.source_selector = QComboBox()
        self.source_selector.setMinimumWidth(180)
        self.fixed_output = label("—", muted=True)
        layout.addWidget(row(self.source_selector, self.fixed_output))
        self.position = QLineEdit()
        self.position.setMaximumWidth(260)
        bind(self.position, "setPlaceholderText", "位置名（任意）")
        self.conditions = label("", muted=True)
        layout.addWidget(row(self.position, self.conditions))
        baseline, self.baseline = metric("校正時 [dB SPL]")
        current, self.current = metric("今回 [dB SPL]")
        difference, self.difference = metric("差 [dB]")
        layout.addWidget(row(baseline, current, difference))
        self.history = QTableWidget(0, 6)
        set_headers(
            self.history, ["位置", "刺激", "校正時 [dB SPL]", "今回 [dB SPL]", "差 [dB]", "状態"]
        )
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history.verticalHeader().hide()
        self.history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history.setMinimumHeight(85)
        layout.addWidget(self.history, 1)
        self.start_button = button(
            "再生・測定", lambda: owner._guard(owner._start_comparison), primary=True
        )
        self.stop_button = button("停止", owner._request_stop)
        self.save_capture = QCheckBox()
        set_text(self.save_capture, "入力を保存")
        self.open_button = button("記録を開く", self.open_output)
        actions = row(self.start_button, self.stop_button, self.save_capture, self.open_button)
        actions.layout().insertStretch(3)
        layout.addWidget(actions)
        self.status = label("準備完了", muted=True)
        layout.addWidget(self.status)
        self.source_selector.currentIndexChanged.connect(self.show_source)
        self.history.cellClicked.connect(self.show_row)
        self.set_busy(False)

    def set_sources(self, sources, *, rows=(), output=None):
        self.sources = sources
        self.rows = []
        self.history.setRowCount(0)
        self.sequence = 0
        self.last_output = output
        self.source_selector.clear()
        for source in sources:
            self.source_selector.addItem(
                f"{source.group} / {source.name}" if source.group else source.name
            )
        self._latest = {}
        set_text(
            self.start_button,
            "再生・測定" if len(sources) == 1 else "{count}件を再生・測定",
            count=len(sources),
        )
        for row in rows:
            self.append_result(row)
        self.show_source()
        self.set_busy(False)

    def show_source(self):
        index = self.source_selector.currentIndex()
        if not 0 <= index < len(self.sources):
            return
        source = self.sources[index]
        result, setup = source.result, source.replay.setup
        set_text(self.fixed_output, "出力固定 {peak:.6g} Vpeak", peak=result.output_peak_v)
        set_text(
            self.conditions,
            "{mode} · {channel} · {rate:,} Hz",
            mode=Message(
                "シミュレーション" if source.replay.backend == "simulation" else "NI DAQ 実機"
            ),
            channel=setup.daq.output_channel,
            rate=setup.daq.sample_rate_hz,
        )
        set_value(self.baseline, f"{result.measured_spl_db:.2f}")
        self._show_values(self._latest.get(source.id))

    def _show_values(self, row):
        value = row.get("measured_spl_db") if row else None
        delta = row.get("delta_db") if row else None
        set_value(self.current, "—" if value is None else f"{value:.2f}")
        set_value(self.difference, "—" if delta is None else f"{delta:+.2f}")

    def set_busy(self, busy):
        measuring = busy and self.owner.job_kind == "comparison"
        compatible = False
        if self.sources:
            try:
                compatible = self.owner.editor.setup().reference_fingerprint == self.sources[
                    0
                ].replay.setup.reference_fingerprint and self.owner.simulate == (
                    self.sources[0].replay.backend == "simulation"
                )
            except (ValueError, TypeError):
                pass
        self.start_button.setEnabled(not busy and compatible)
        bind(
            self.start_button,
            "setToolTip",
            "" if compatible else "測定モード・入力系・DAQ設定を校正時と合わせてください",
        )
        self.stop_button.setEnabled(measuring)
        self.position.setEnabled(not busy)
        self.save_capture.setEnabled(not busy)
        self.source_selector.setEnabled(not busy)
        self.history.setEnabled(not busy)
        self.open_button.setEnabled(
            not busy and self.last_output is not None and self.last_output.is_dir()
        )

    def begin(self, output):
        self.sequence += 1
        self.last_output = output
        for source in self.sources:
            self._latest.pop(source.id, None)
        self._show_values(None)
        set_text(self.status, "条件を保存中…")

    def progress(self, item):
        kind = item.get("kind")
        if kind == "comparison_source":
            index = next(
                i for i, source in enumerate(self.sources) if source.id == item["source_id"]
            )
            self.source_selector.setCurrentIndex(index)
            set_text(
                self.status, "比較測定 {index} / {total}", index=item["index"], total=item["total"]
            )
        elif kind == "comparison_phase":
            set_text(
                self.status,
                "背景音を測定中 · {duration:g} s"
                if item["phase"] == "background"
                else "固定倍率で再生中 · {duration:g} s",
                duration=item["duration_s"],
            )
        elif kind == "comparison_result":
            self.append_result(item["row"])

    def append_result(self, row):
        self.rows.append(row)
        self._latest[row["source_id"]] = row
        number = self.history.rowCount()
        self.history.insertRow(number)
        status = {
            "complete": "成功",
            "insufficient_snr": "SNR不足",
            "cancelled": "停止",
            "failed": "失敗",
            "clipped": "クリップ",
        }[row["status"]]
        values = [
            row["position"],
            row["name"],
            f"{row['baseline_spl_db']:.2f}",
            "—" if row["measured_spl_db"] is None else f"{row['measured_spl_db']:.2f}",
            "—" if row["delta_db"] is None else f"{row['delta_db']:+.2f}",
            status,
        ]
        for column, value in enumerate(values):
            cell = (
                set_cell(self.history, number, column, value)
                if column == 5
                else set_cell(self.history, number, column, "{value}", value=value)
            )
            cell.setToolTip(row.get("error") or row["measured_at"])
        self.history.scrollToBottom()
        self._show_values(row)

    def show_row(self, index, column):
        row = self.rows[index]
        source_index = next(
            i for i, source in enumerate(self.sources) if source.id == row["source_id"]
        )
        self.source_selector.setCurrentIndex(source_index)
        set_value(self.baseline, f"{row['baseline_spl_db']:.2f}")
        self._show_values(row)
        if row.get("error"):
            set_text(self.status, "{error}", error=error_message(ValueError(row["error"])))

    def finish(self, result, error=None):
        if result is not None:
            self.last_output = result["directory"]
            set_text(
                self.status,
                "比較測定を保存しました"
                if result["status"] == "complete"
                else "比較測定を中断しました",
            )
            invalid = next((row for row in result["rows"] if row.get("error")), None)
            if invalid is not None:
                set_text(self.status, "{error}", error=error_message(ValueError(invalid["error"])))
        elif error is not None:
            set_text(self.status, "{error}", error=error_message(error))
        self.set_busy(False)
        if self.close_after_stop:
            self.close_after_stop = False
            self.close()

    def open_output(self):
        if self.last_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.owner.worker is not None and self.owner.job_kind == "comparison":
            self.close_after_stop = True
            self.owner._request_stop()
            event.ignore()
        else:
            event.accept()
