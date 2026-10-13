"""Save a self-contained evaluation report without a separate report database."""

import json
from pathlib import Path
from uuid import uuid4

from tool_agent_lab.evaluation.metrics import aggregate


def build_report(metadata: dict, cases: list[dict]) -> dict:
    attempts = [a for c in cases for a in c["attempts"]]
    return {"format_version": "evaluation-v1", **metadata, "cases": cases,
            "metrics": aggregate(cases, attempts)}


def write_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
