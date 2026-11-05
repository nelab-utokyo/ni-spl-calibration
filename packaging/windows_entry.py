"""Windowed executable entry point; the source/uv launcher stays unchanged."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        report = Path(sys.argv[2]).resolve()
        try:
            from smoke_windows import run

            result = run()
        except BaseException:
            report.write_text(
                json.dumps({"status": "failed", "traceback": traceback.format_exc()}, indent=2),
                encoding="utf-8",
            )
            return 1
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0

    from ni_spl_calibration.gui import main as gui_main

    try:
        return gui_main()
    except Exception:
        # No console is attached to the Windows GUI executable.
        import ctypes
        import os

        details = traceback.format_exc()
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NI-SPL-Calibration"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "startup-error.log"
        log.write_text(details, encoding="utf-8")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Could not start NI SPL Calibration.\n\n{log}\n\n{details}",
                "NI SPL Calibration",
                0x10,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
