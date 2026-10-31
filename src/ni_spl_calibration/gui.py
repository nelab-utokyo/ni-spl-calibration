"""Desktop entry point; the core Python API does not require Qt."""

import sys


def main():
    try:
        from ._gui import run
    except ModuleNotFoundError as error:
        if error.name != "PySide6":
            raise
        print(
            "GUIを使用するには uv sync --locked --extra gui を実行してください。\n"
            'For pip: python -m pip install ".[gui]"',
            file=sys.stderr,
        )
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
