"""Small, content-free performance records; never log task or model bodies."""
from __future__ import annotations

import json
import sys


def emit_timing(stage: str, seconds: float, *, task_id: str | None = None,
                runtime_id: str | None = None) -> None:
    record = {"kind": "performance", "stage": stage, "seconds": max(0.0, seconds)}
    if task_id is not None:
        record["task_id"] = task_id
    if runtime_id is not None:
        record["runtime_id"] = runtime_id
    try:
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # Diagnostics must not turn a completed tool into an uncertain retry,
        # or close a browser that has already started successfully.
        pass
