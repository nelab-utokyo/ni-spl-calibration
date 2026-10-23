from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path


def read_json(path):
    def invalid(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=invalid)


def write_json(path, payload):
    """Publish a complete JSON file exclusively; existing records are immutable."""
    path = Path(path)
    content = (
        json.dumps(
            asdict(payload) if is_dataclass(payload) else payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ni-spl-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
