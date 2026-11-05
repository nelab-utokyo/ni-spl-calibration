"""Exercise bundled imports, native DLLs, GUI workers and records without a DAQ."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


def run():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import numpy as np
    import soundfile as sf
    from PySide6.QtWidgets import QApplication

    from ni_spl_calibration import DaqSettings, Setup
    from ni_spl_calibration._gui import STYLE, CalibrationWindow
    from ni_spl_calibration.comparison import load_comparison
    from ni_spl_calibration.hardware import runtime_versions
    from ni_spl_calibration.records import load_replay
    from ni_spl_calibration.workflow import StimulusSource

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: errors.append(str(value))
    view = CalibrationWindow()
    view._show_error = lambda error: errors.append(str(error))
    assert not view.simulate and view.worker is None  # Startup must not load the NI driver.

    def wait():
        deadline = time.monotonic() + 30
        while view.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        if view.worker is not None:
            view._request_stop()
            raise RuntimeError("Frozen GUI smoke test timed out")
        if errors:
            raise RuntimeError("; ".join(errors))

    with tempfile.TemporaryDirectory(prefix="ni-spl-frozen-") as temporary:
        folder = Path(temporary)
        try:
            view.mode.setCurrentIndex(0)
            view.editor.set_setup(
                Setup(
                    "frozen-smoke", "Synthetic input chain", daq=DaqSettings(sample_rate_hz=32000)
                )
            )
            view.output_root.setText(str(folder / "results"))
            view.show()
            # Exercise dynamic imports too; this module is selected by name in the GUI.
            view._choose_test_tone()
            wait()
            assert view.stimulus is not None
            app_dir = (
                Path(sys.executable).parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1]
            )
            generated = StimulusSource(
                "generator", str(app_dir / "examples/custom_sound.py") + ":make_sound"
            ).prepare(32000)
            assert generated.sample_rate_hz == 32000
            values = 0.4 * np.sin(2 * np.pi * 1000 * np.arange(8820) / 44100)
            wav = folder / "stereo.wav"
            sf.write(wav, np.column_stack((values, values)), 44100)
            view.source_kind.setCurrentIndex(0)
            view.source_location.setText(str(wav))
            view._prepare(playback_preview=True)
            wait()
            assert view.stimulus.sample_rate_hz == 32000
            view._measure_reference()
            wait()
            view._calibrate()
            wait()
            assert view.jobs[0].status == "complete"
            load_replay(view.last_output)
            view._open_comparison()
            wait()
            view.comparison_dialog.start_button.click()
            wait()
            record = load_comparison(view.comparison_dialog.last_output)
            assert record.rows[0]["status"] == "complete"
            assert abs(record.rows[0]["delta_db"]) < 0.01
            view.language.setCurrentIndex(1)
            assert view.comparison_dialog.windowTitle() == "Position comparison"
            versions = runtime_versions()
            assert all(
                versions.get(name)
                for name in ("numpy", "scipy", "soundfile", "nidaqmx", "PySide6-Essentials")
            )
            return {
                "status": "complete",
                "frozen": bool(getattr(sys, "frozen", False)),
                "runtime": versions,
                "checks": [
                    "NI imports without driver",
                    "Qt GUI",
                    "built-in generator",
                    "external generator",
                    "stereo WAV / libsndfile",
                    "resampling / SciPy",
                    "calibration queue",
                    "fixed-gain comparison",
                    "record verification",
                    "English UI",
                ],
            }
        finally:
            if view.worker is not None:
                view._request_stop()
                view.worker.wait(30000)
                app.processEvents()
            view.close()
            app.processEvents()
            sys.excepthook = original_hook
