"""Shared writer for eval-readiness probe results.

Every eval-readiness probe (precision on reasoning text, harness requests,
chat conformance, long-answer load, long context) writes one JSON file through
``write_result`` so the runner-side gate ``scripts/check_eval_readiness.py``
can read them all the same way, and prints the same short summary.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
RESULT_DIR = Path("doc/eval_readiness")
MAX_SUMMARY_LINES = 12


def check(
    check_id: str,
    passed: Optional[bool],
    *,
    severity: str = "critical",
    detail: str = "",
    value: Any = None,
    threshold: Any = None,
    waivable: bool = True,
) -> dict:
    """Build one check row. ``passed=None`` records a skip; give a detail."""
    status = "skip" if passed is None else ("pass" if passed else "fail")
    if severity not in ("critical", "advisory"):
        raise ValueError(f"severity must be critical or advisory, got {severity!r}")
    row = {"id": check_id, "status": status, "severity": severity, "waivable": waivable, "detail": detail}
    if value is not None:
        row["value"] = value
    if threshold is not None:
        row["threshold"] = threshold
    return row


def result_path(model_dir: Path | str, probe: str) -> Path:
    return Path(model_dir) / RESULT_DIR / f"{probe}.json"


def write_result(
    model_dir: Path | str,
    probe: str,
    checks: Iterable[dict],
    *,
    summary_lines: Iterable[str] = (),
    data: Optional[dict] = None,
    command: Optional[list[str]] = None,
    output: Optional[Path | str] = None,
) -> Path:
    """Write the probe result and return its path.

    ``data`` holds probe-specific measurements; keep it compact (no raw
    completions) so a reader can open the file without flooding context.
    Raw artifacts belong next to it under ``doc/eval_readiness/<probe>/``.
    """
    rows = list(checks)
    if not rows:
        raise ValueError("a probe result needs at least one check")
    path = Path(output) if output else result_path(model_dir, probe)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "probe": probe,
        "schema_version": SCHEMA_VERSION,
        "created_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command if command is not None else [sys.executable, *sys.argv],
        "checks": rows,
        "summary_lines": list(summary_lines)[:MAX_SUMMARY_LINES],
    }
    if data:
        body["data"] = data
    path.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n")
    return path


def print_summary(path: Path | str) -> int:
    """Print at most about 40 lines for the agent; return 0 pass, 1 advisory, 2 critical."""
    body = json.loads(Path(path).read_text())
    rows = body["checks"]
    failed_critical = [r for r in rows if r["status"] == "fail" and r["severity"] == "critical"]
    failed_advisory = [r for r in rows if r["status"] == "fail" and r["severity"] == "advisory"]
    verdict = "critical" if failed_critical else ("advisory" if failed_advisory else "pass")
    print(f"{body['probe']}: {verdict} ({len(rows)} checks) -> {path}")
    for line in body.get("summary_lines", [])[:MAX_SUMMARY_LINES]:
        print(f"  {line}")
    shown = 0
    for row in failed_critical + failed_advisory:
        if shown >= 20:
            print(f"  ... more failures in {path}")
            break
        print(f"  {row['severity'].upper()} {row['id']}: {row.get('detail', '')}")
        shown += 1
    return 2 if failed_critical else (1 if failed_advisory else 0)
