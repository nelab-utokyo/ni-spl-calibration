"""Portable run bundles, integrity checks and a timestamped desktop activity log."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .calibration import load_reference, load_result, timestamp, validate_reference
from .config import DaqSettings, MeasurementSettings, Setup
from .hardware import runtime_versions
from .stimulus import load_stimulus, save_stimulus
from .storage import read_json, write_json

INPUT_FILES = ("setup.json", "reference.json", "stimulus.npz")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def setup_from_record(payload):
    return Setup(
        **{
            **payload,
            "daq": DaqSettings(**payload["daq"]),
            "measurement": MeasurementSettings(**payload["measurement"]),
        }
    )


def write_run_manifest(output, *, target_spl_db, stimulus, hardware, parent_run=None):
    write_json(
        output / "run.json",
        {
            "schema_version": 1,
            "operation": "calibration",
            "created_at": timestamp(),
            "target_spl_db": target_spl_db,
            "backend": hardware.describe()["backend"],
            "stimulus_sha256": stimulus.sha256,
            "stimulus": stimulus.describe(),
            "files": {name: file_sha256(output / name) for name in INPUT_FILES},
            "parent_run": parent_run,
        },
    )


def seal_run(output, outcome):
    names = (*INPUT_FILES, "run.json", outcome)
    write_json(
        output / "checksums.json",
        {
            "schema_version": 1,
            "created_at": timestamp(),
            "files": {name: file_sha256(output / name) for name in names},
        },
    )


def _verify_files(directory, hashes, names):
    # Only fixed bundle filenames are opened; a manifest cannot select arbitrary paths.
    for name in names:
        expected = hashes.get(name)
        if not isinstance(expected, str) or file_sha256(directory / name) != expected:
            raise ValueError(f"保存記録の整合性チェックに失敗しました: {name}")


@dataclass(frozen=True)
class Replay:
    directory: Path
    setup: Setup
    reference: object
    stimulus: object
    target_spl_db: float
    backend: str
    record_sha256: str

    @property
    def parent_record(self):
        return {"directory": str(self.directory), "record_sha256": self.record_sha256}


def load_replay(directory):
    """Restore inputs, never execute the original generator or start hardware."""
    directory = Path(directory).resolve()
    manifest_path = directory / "run.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else None
    if manifest is not None:
        if manifest.get("schema_version") != 1 or manifest.get("operation") != "calibration":
            raise ValueError("未対応の実行記録です。")
        _verify_files(directory, manifest.get("files", {}), INPUT_FILES)
        target = manifest["target_spl_db"]
        backend = manifest["backend"]
        digest = file_sha256(manifest_path)
        outcome = "result.json" if (directory / "result.json").is_file() else "failure.json"
        if (directory / outcome).is_file():
            seal = read_json(directory / "checksums.json")
            if seal.get("schema_version") != 1:
                raise ValueError("未対応の整合性記録です。")
            _verify_files(directory, seal.get("files", {}), (*INPUT_FILES, "run.json", outcome))
    else:
        # Successful records created before run manifests can also be restored.
        result = load_result(directory / "result.json")
        target, backend = result.target_spl_db, result.hardware["backend"]
        digest = file_sha256(directory / "result.json")
    if not math.isfinite(target) or not -100 <= target <= 180:
        raise ValueError("保存記録の目標音圧が不正です。")
    if backend not in {"simulation", "ni-daqmx"}:
        raise ValueError("保存記録の測定モードが不正です。")
    setup = setup_from_record(read_json(directory / "setup.json"))
    reference = load_reference(directory / "reference.json")
    stimulus = load_stimulus(directory / "stimulus.npz")
    validate_reference(setup, reference)
    if backend != reference.hardware.get("backend"):
        raise ValueError("保存記録と基準記録の測定モードが一致しません。")
    if stimulus.sample_rate_hz != setup.daq.sample_rate_hz:
        raise ValueError("保存波形とDAQのSRが一致しません。")
    if manifest is not None and stimulus.sha256 != manifest["stimulus_sha256"]:
        raise ValueError("保存波形のハッシュが実行記録と一致しません。")
    if (directory / "result.json").is_file():
        result = load_result(directory / "result.json")
        if (
            result.stimulus != stimulus.describe()
            or result.setup != json.loads(json.dumps(asdict(setup)))
            or result.reference != asdict(reference)
            or result.target_spl_db != target
            or result.hardware.get("backend") != backend
        ):
            raise ValueError("保存された結果と入力条件が一致しません。")
    return Replay(directory, setup, reference, stimulus, target, backend, digest)


def find_runs(root):
    root = Path(root)
    directories = {
        path.parent for pattern in ("*/run.json", "*/result.json") for path in root.glob(pattern)
    }
    directories.update(path.parent for path in root.glob("*/plan.json"))
    records = []
    for directory in sorted(directories, reverse=True):
        try:
            plan_path = directory / "plan.json"
            if plan_path.is_file():
                plan = read_json(plan_path)
                summary = directory / "summary.json"
                records.append(
                    {
                        "directory": directory,
                        "created_at": plan["created_at"],
                        "status": "complete"
                        if summary.is_file() and read_json(summary)["status"] == "complete"
                        else "incomplete",
                        "target_spl_db": ", ".join(
                            f"{v:g}" for v in sorted({job["target_spl_db"] for job in plan["jobs"]})
                        ),
                        "backend": plan["backend"],
                    }
                )
                continue
            manifest = directory / "run.json"
            payload = read_json(manifest if manifest.is_file() else directory / "result.json")
            status = "incomplete"
            if (directory / "result.json").is_file():
                status = "complete"
            elif (directory / "failure.json").is_file():
                status = read_json(directory / "failure.json")["status"]
            records.append(
                {
                    "directory": directory,
                    "created_at": payload.get("created_at", payload.get("calibrated_at", "")),
                    "status": status,
                    "target_spl_db": payload["target_spl_db"],
                    "backend": payload.get("backend", payload.get("hardware", {}).get("backend")),
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            records.append({"directory": directory, "status": "invalid"})
    return records


class ActivityLog:
    """One immutable JSON event per action; no keystrokes or external telemetry."""

    def __init__(self):
        self.session_id = uuid4().hex
        self.started_at = timestamp()
        self.sequence = 0
        self.directory = None

    def record(self, root, action, **details):
        directory = Path(root).expanduser().resolve() / "sessions" / self.session_id
        if not (directory / "session.json").exists():
            write_json(
                directory / "session.json",
                {
                    "schema_version": 1,
                    "session_id": self.session_id,
                    "started_at": self.started_at,
                    "runtime": runtime_versions(),
                },
            )
        self.directory = directory
        self.sequence += 1
        path = directory / "events" / f"{self.sequence:06d}-{action}.json"
        write_json(path, {"schema_version": 1, "at": timestamp(), "action": action, **details})
        return path

    def save_waveform(self, stimulus):
        if self.directory is None:
            raise RuntimeError("Start an activity log before saving a preview")
        path = self.directory / "waveforms" / f"{stimulus.sha256}.npz"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if load_stimulus(path).sha256 != stimulus.sha256:
                raise ValueError("保存済みプレビュー波形のハッシュが一致しません。")
        else:
            save_stimulus(stimulus, path)
        return path
