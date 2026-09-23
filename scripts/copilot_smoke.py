#!/usr/bin/env python3
"""Replay candidate skill Markdown in tool-free Copilot sessions; grade outside the agent.

This measures guided answers, not native skill routing or host plugin installation.
Only Markdown under the selected skill directories is supplied to the model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[1]
MAX_BYTES = 60_000


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text())["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("case suite must be a nonempty list")
    ids = set()
    for case in cases:
        if not re.fullmatch(r"[a-z0-9-]+", case["id"]) or case["id"] in ids:
            raise ValueError("case IDs must be unique lowercase slugs")
        ids.add(case["id"])
        if not isinstance(case["prompt"], str) or not case["prompt"].strip():
            raise ValueError("each case needs a prompt")
        if not isinstance(case["skills"], list) or not case["skills"]:
            raise ValueError("each case needs skills")
        if not isinstance(case["expected"], dict) or not case["expected"]:
            raise ValueError("each case needs nonempty expected fields")
        if "required_string_fields" in case:
            fields = case["required_string_fields"]
            if not isinstance(fields, list) or not fields or not all(
                    isinstance(field, str) for field in fields):
                raise ValueError("required_string_fields must be a nonempty list of strings")
    return cases


def skill_context(root: Path, paths: list[str]) -> tuple[str, dict[str, str]]:
    root = root.resolve()
    documents = {}
    for rel in paths:
        path = Path(rel)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"invalid skill path: {rel}")
        directory = root / path
        if any(p.is_symlink() for p in [directory, *directory.parents] if p != root):
            raise ValueError(f"symlink in skill path: {rel}")
        if not (directory / "SKILL.md").is_file():
            raise ValueError(f"missing SKILL.md: {rel}")
        for file in sorted(directory.rglob("*")):
            if file.is_symlink():
                raise ValueError(f"symlink in skill: {file}")
            if file.is_file() and file.suffix == ".md":
                raw = file.read_bytes()
                if len(raw) > MAX_BYTES:
                    raise ValueError("skill context exceeds byte limit")
                documents[file.relative_to(root).as_posix()] = raw
    if sum(len(raw) for raw in documents.values()) > MAX_BYTES:
        raise ValueError("skill context exceeds byte limit")
    digests = {path: hashlib.sha256(raw).hexdigest() for path, raw in documents.items()}
    context = "\n\n".join(f"FILE: {path}\n{raw.decode('utf-8')}" for path, raw in documents.items())
    return context, digests


def grade(answer: object, expected: dict, required_strings: list[str] | None = None) -> list[str]:
    if not isinstance(answer, dict):
        return ["answer must be a JSON object"]
    # JSON comparison distinguishes false from 0, unlike Python equality.
    errors = [f"unexpected or missing field: {key}" for key, value in expected.items()
              if key not in answer or json.dumps(answer[key], sort_keys=True)
              != json.dumps(value, sort_keys=True)]
    for key in required_strings or []:
        value = answer.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"missing or empty required field: {key}")
    return errors


def staged_skill_names(paths: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for rel in paths:
        name = Path(rel).name
        if name in names:
            raise ValueError(f"duplicate staged skill name: {name}")
        names[name] = rel
    return names


def run_case(case: dict, root: Path, executable: str, model: str,
             timeout: int, credits: float) -> dict:
    context, digests = skill_context(root, case["skills"])
    names = staged_skill_names(case["skills"])
    prompt = (
        "Apply the supplied skill instructions to the scenario below. "
        "This is an offline instruction-replay test: do not execute commands. "
        "Return only the requested JSON object, without Markdown fences.\n\n"
        + context + "\n\nSCENARIO:\n" + case["prompt"]
    )
    result = {"id": case["id"], "mode": "instruction-replay", "skills_sha256": digests}
    with tempfile.TemporaryDirectory(prefix="copilot-smoke-") as temp:
        workspace = Path(temp) / "workspace"
        workspace.mkdir()
        staged = {}
        for name, rel in names.items():
            source = root.resolve() / rel
            destination = workspace / ".github/skills" / name
            for file in source.rglob("*.md"):
                target = destination / file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.read_bytes())
            staged[name] = destination
        # Keep credentials needed by the CLI, but not inherited Copilot settings,
        # custom providers, extensions, or permission overrides.
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
                        "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}}
        env["COPILOT_HOME"] = str(Path(temp) / "config")
        try:
            discovery = subprocess.run(
                [executable, "--no-auto-update", "skill", "list", "--json"],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=15, check=True,
            )
            rows = json.loads(discovery.stdout)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid skill inventory")
            for row in rows:
                if row.get("source") != "builtin" and row.get("name") not in staged:
                    raise ValueError("unexpected ambient skill in inventory")
            for name, path in staged.items():
                matches = [row for row in rows if row.get("name") == name]
                if len(matches) != 1:
                    raise ValueError(f"ambiguous skill inventory entries for: {name}")
                row = matches[0]
                if not (row.get("enabled") is True
                        and Path(row.get("path", "")).resolve() == path.resolve()):
                    raise ValueError(f"candidate skill not discovered: {name}")
        except (ValueError, AttributeError, TypeError, subprocess.SubprocessError) as exc:
            return {**result, "status": "error", "reason": f"discovery failed: {exc}"}
        result["discovery_verified"] = sorted(staged)
        cmd = [executable, "--no-auto-update", "--no-custom-instructions",
               "--disable-builtin-mcps", "--no-ask-user", "--no-bash-env",
               "--no-remote", "--no-remote-export", "--available-tools=",
               "--disallow-temp-dir", "--model", model, "--max-ai-credits", str(credits),
               "--silent", "--stream", "off", "-p", prompt]
        try:
            proc = subprocess.run(cmd, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {**result, "status": "error", "reason": "model timeout"}
        if proc.returncode:
            return {**result, "status": "error", "reason": f"CLI exited {proc.returncode}"}
        try:
            answer = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {**result, "status": "error", "reason": "CLI answer was not JSON"}
        errors = grade(answer, case["expected"], case.get("required_string_fields"))
        return {**result, "status": "fail" if errors else "pass",
                "answer": answer, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=REPO)
    parser.add_argument("--cases", type=Path, default=REPO / "evals/copilot/cases.json")
    parser.add_argument("--case", help="run one case by ID")
    parser.add_argument("--copilot", default="copilot")
    parser.add_argument("--model", help="explicit model ID; required for a paid run")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--credits", type=float, default=5, help="soft per-case credit limit")
    parser.add_argument("--output", type=Path, default=Path("copilot-smoke-results.json"))
    parser.add_argument("--validate-only", action="store_true", help="no CLI or model calls")
    args = parser.parse_args()
    try:
        if args.timeout <= 0 or not math.isfinite(args.credits) or not 0 < args.credits <= 100:
            raise ValueError("timeout must be positive and credits must be in (0, 100]")
        cases = load_cases(args.cases)
        if args.case:
            cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            raise ValueError("no matching cases")
        for case in cases:
            skill_context(args.candidate, case["skills"])
            staged_skill_names(case["skills"])
        if args.validate_only:
            print(f"Validated {len(cases)} cases and candidate Markdown; no model calls")
            return 0
        if not args.model or args.model == "auto":
            raise ValueError("specify --model with an explicit model ID")
        executable = shutil.which(args.copilot)
        if not executable:
            raise ValueError("Copilot CLI missing; no evaluations ran")
        if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError(
                "refusing shell-interpreted CLI wrapper (.cmd/.bat/.ps1); "
                "this harness passes candidate content as argv and is POSIX-only"
            )
        version = subprocess.run([executable, "--no-auto-update", "--version"],
                                 capture_output=True, text=True, timeout=15,
                                 check=True).stdout.strip()
        revision = subprocess.run(["git", "-C", str(args.candidate), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        results = [run_case(case, args.candidate, executable, args.model,
                            args.timeout, args.credits) for case in cases]
        report = {"candidate_commit": revision, "cli_version": version,
                  "model": args.model, "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                  "results": results}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{sum(r['status'] == 'pass' for r in results)}/{len(results)} passed; {args.output}")
        return 0 if all(r["status"] == "pass" for r in results) else 1
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Evaluation error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
