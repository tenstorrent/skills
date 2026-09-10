#!/usr/bin/env python3
"""Local, versioned bring-up records. No network transport or model execution."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import uuid

SCHEMA_VERSION = 1
FEEDBACK_FIELDS = (
    "outdated_apis", "papercuts", "workarounds", "suggested_skill_improvements",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_checkpoint(repo: Path) -> dict[str, Any]:
    """Capture HEAD and dirt without storing source diffs or filenames."""
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10,
        ).stdout

    result: dict[str, Any] = {
        "commit": None, "describe": None, "dirty": None,
        "status_sha256": None, "capture_error": None,
    }
    try:
        if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != repo.resolve():
            result["capture_error"] = "NotRepositoryRoot"
            return result
        result["commit"] = git("rev-parse", "--verify", "HEAD").decode().strip()
        result["describe"] = git("describe", "--always", "--tags", "--long").decode().strip()
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
        result["dirty"] = bool(status)
        result["status_sha256"] = hashlib.sha256(status).hexdigest()
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not copy stderr, paths or command output into the record.
        result["capture_error"] = type(exc).__name__
    return result


def atomic_json(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)  # Atomic create; concurrent submissions cannot overwrite feedback.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RunRecord:
    """One invocation per UUID; resuming never overwrites an earlier verdict."""
    def __init__(self, repo: Path, log_dir: Path, *, model_id: str | None,
                 requested_revision: str | None, resume_stage: int | None, dry_run: bool):
        self.repo = repo
        self.started = time.monotonic()
        attempt_id = str(uuid.uuid4())
        self.path = log_dir / "telemetry" / attempt_id / "run.json"
        # Snapshot before creating telemetry files inside the target checkout.
        checkpoint = git_checkpoint(repo)
        self.data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "kind": "bringup_run",
            "plugin_version": json.loads((Path(__file__).resolve().parents[1] / ".codex-plugin/plugin.json").read_text())["version"],
            "attempt_id": attempt_id, "started_at": now(), "finished_at": None,
            "elapsed_seconds": None, "status": "running", "exit_code": None,
            "error_type": None, "dry_run": dry_run, "resume_stage": resume_stage,
            "model_checkpoint": {
                "model_id": model_id, "requested_revision": requested_revision,
                "resolved_revision": None, "resolution_evidence": None,
            },
            "tt_metal": {"start": checkpoint, "finish": None},
            "stages": [], "offboarding_file": "offboarding.json",
        }
        self.save()
        reference = Path(__file__).resolve().parents[1] / "skills/model-bringup/references/offboarding.md"
        (self.path.parent / "OFFBOARDING.md").write_text(
            "# Bring-up off-boarding\n\n"
            "After this runner exits or is confirmed stopped, the coordinating agent must "
            "complete the local feedback step. Preserve the recorded outcome and all artifacts.\n\n"
            f"Read the off-boarding instructions: {reference}\n\n"
            f"Attempt record: {self.path.resolve()}\n\n"
            "Report outdated APIs, papercuts/problems, workarounds and suggested skill improvements. "
            "Use observed evidence; do not invent feedback or rewrite skills. "
            "An empty list means none observed; missing feedback means not collected.\n",
            encoding="utf-8",
        )

    def save(self) -> None:
        atomic_json(self.path, self.data)

    def start_stage(self, index: int, prompt: Path) -> None:
        self.data["stages"].append({
            "index": index, "name": prompt.stem, "started_at": now(), "finished_at": None,
            "status": "running", "exit_code": None, "goal_status": None, "check_status": None,
            "checkpoint": {"start": git_checkpoint(self.repo), "finish": None},
        })
        self.save()

    def finish_stage(self, code: int | None, *, goal_status: str | None, check_status: str | None,
                     error: BaseException | None = None) -> None:
        stage = self.data["stages"][-1]
        status = ("interrupted" if isinstance(error, KeyboardInterrupt) else "error" if error
                  else "dry_run" if self.data["dry_run"] else "completed" if code == 0 else "stopped")
        stage.update(status=status,
                     exit_code=code, goal_status=goal_status, check_status=check_status,
                     finished_at=now())
        stage["checkpoint"]["finish"] = git_checkpoint(self.repo)
        self.save()

    def finish(self, code: int | None, *, error: BaseException | None = None) -> None:
        status = ("interrupted" if isinstance(error, KeyboardInterrupt) else "error" if error
                  else "dry_run" if self.data["dry_run"] else "completed" if code == 0 else "stopped")
        for stage in self.data["stages"]:
            if stage["status"] == "running":
                stage.update(status=status, finished_at=now())
                stage["checkpoint"]["finish"] = git_checkpoint(self.repo)
        self.data.update(status=status, exit_code=code, error_type=type(error).__name__ if error else None,
                         finished_at=now(), elapsed_seconds=round(time.monotonic() - self.started, 3))
        self.data["tt_metal"]["finish"] = git_checkpoint(self.repo)
        self.save()


def validate_feedback(value: Any) -> dict[str, Any]:
    """Allow curated feedback only; status/provenance overrides are rejected."""
    if not isinstance(value, dict) or set(value) != set(FEEDBACK_FIELDS) | {"model_checkpoint"}:
        raise ValueError("feedback must contain model_checkpoint and exactly the four feedback categories")
    checkpoint = value["model_checkpoint"]
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"resolved_revision", "resolution_evidence"}:
        raise ValueError("model_checkpoint requires resolved_revision and resolution_evidence")
    for text in checkpoint.values():
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 4000):
            raise ValueError("checkpoint values must be null or nonempty strings of at most 4000 characters")
    if (checkpoint["resolved_revision"] is None) != (checkpoint["resolution_evidence"] is None):
        raise ValueError("a resolved weights revision must include its observed evidence; otherwise use null for both")
    for key in FEEDBACK_FIELDS:
        entries = value[key]
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError(f"{key} must be a list with at most 50 entries")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 4000 for text in entries):
            raise ValueError(f"{key} entries must be nonempty strings of at most 4000 characters")
    return value


def submit_feedback(record: Path, feedback: Path, *, abandoned_reason: str | None = None) -> Path:
    run = json.loads(record.read_text(encoding="utf-8"))
    if run.get("schema_version") != SCHEMA_VERSION or run.get("kind") != "bringup_run":
        raise ValueError("unsupported run record schema")
    if run["status"] == "running" and not abandoned_reason:
        raise ValueError("runner has no terminal record; confirm it stopped and supply --abandoned-reason")
    if abandoned_reason is not None and (not abandoned_reason.strip() or len(abandoned_reason) > 4000):
        raise ValueError("abandoned reason must be nonempty and at most 4000 characters")
    if abandoned_reason is not None and run["status"] != "running":
        raise ValueError("abandonment applies only to an unfinished record; its terminal outcome must be preserved")
    payload = validate_feedback(json.loads(feedback.read_text(encoding="utf-8")))
    result = {
        "schema_version": SCHEMA_VERSION, "kind": "bringup_offboarding",
        "attempt_id": run["attempt_id"], "collected_at": now(),
        "observed_run_status": run["status"],
        "outcome": "abandoned" if abandoned_reason else run["status"],
        "abandoned_reason": abandoned_reason, "feedback": payload,
    }
    output = record.parent / "offboarding.json"
    if output.exists():
        raise ValueError("offboarding already exists; preserve the original feedback")
    atomic_json(output, result, replace=False)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--feedback", type=Path, required=True)
    parser.add_argument("--abandoned-reason", help="Explicit observation that an unfinished runner has stopped")
    args = parser.parse_args()
    try:
        print(submit_feedback(args.record, args.feedback, abandoned_reason=args.abandoned_reason))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"offboarding: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
