"""Build an x64 Windows portable ZIP from the locked uv environment."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "NI-SPL-Calibration"
PACKAGES = [
    "ni-spl-calibration",
    "numpy",
    "scipy",
    "soundfile",
    "nidaqmx",
    "PySide6-Essentials",
    "shiboken6",
    "PyYAML",
]


def main():
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("Run this build on 64-bit Windows (locally or with GitHub Actions).")
    import PyInstaller.__main__

    args = [
        str(ROOT / "packaging/windows_entry.py"),
        "--name",
        APP_NAME,
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build/pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--paths",
        str(ROOT / "src"),
        "--paths",
        str(ROOT / "packaging"),
        "--collect-submodules",
        "ni_spl_calibration",
        "--collect-all",
        "nidaqmx",
    ]
    for name in PACKAGES:
        args += ["--copy-metadata", name]
    PyInstaller.__main__.run(args)
    folder = ROOT / "dist" / APP_NAME
    for name in ("README.md", "README.en.md"):
        shutil.copy2(ROOT / name, folder / name)
    shutil.copytree(ROOT / "docs", folder / "docs", dirs_exist_ok=True)
    examples = folder / "examples"
    examples.mkdir(exist_ok=True)
    for name in ("custom_sound.py", "setup.yaml", "parameters.yaml", "python_api.py"):
        shutil.copy2(ROOT / "examples" / name, examples / name)
    shutil.copy2(ROOT / "packaging/WINDOWS-README.txt", folder / "START-HERE.txt")
    versions = {name: importlib.metadata.version(name) for name in PACKAGES}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (folder / "build-info.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": versions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    licenses = folder / "licenses"
    licenses.mkdir(exist_ok=True)
    notices = [
        "Distribution notices from the locked build environment (including build-only tools).\n"
    ]
    for name in ("LICENSE.txt", "LICENSE"):
        python_license = Path(sys.base_prefix) / name
        if python_license.is_file():
            shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
            break
    # Preserve distribution-supplied notices alongside replaceable DLLs.
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name", "unknown")
        notices.append(
            f"{name} {dist.version}\n{dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See distribution notices'}\n"
        )
        for entry in dist.files or ():
            if any(
                part.lower().startswith(("license", "copying", "notice")) for part in entry.parts
            ):
                path = Path(dist.locate_file(entry))
                if path.is_file():
                    target = licenses / name / str(entry).replace("..", "_").replace(":", "_")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    (folder / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(sorted(notices)), encoding="utf-8")
    # Test the actual executable, not imports from the build environment.
    report = ROOT / "dist" / "windows-smoke-test.json"
    subprocess.run(
        [str(folder / f"{APP_NAME}.exe"), "--smoke-test", str(report)], check=True, timeout=180
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    if result.get("status") != "complete" or not result.get("frozen"):
        raise RuntimeError(f"Frozen smoke test failed: {report}")
    shutil.copy2(report, folder / "smoke-test.json")
    archive_name = f"ni-spl-calibration-{versions['ni-spl-calibration']}-windows-x64"
    archive = Path(
        shutil.make_archive(str(ROOT / "dist" / archive_name), "zip", ROOT / "dist", APP_NAME)
    )
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(archive)


if __name__ == "__main__":
    main()
