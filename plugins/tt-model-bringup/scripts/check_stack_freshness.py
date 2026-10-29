#!/usr/bin/env python3
"""Resolve and verify the freshness of the bringup stack.

A bringup run depends on four moving parts: the tt-metal checkout, the
transformers package in its Python environment, the TT vLLM plugin checkout,
and this skills plugin. ``--mode resolve`` fetches each upstream, records the
resolved pins to ``doc/stack_pins.json`` under the model directory, and fails
a new run that starts behind upstream. ``--mode verify`` re-reads that record
at a stage gate and checks that the pinned commits still match the checkouts.
Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
RECORD_PATH = Path("doc/stack_pins.json")
SKILLS_PLUGIN_JSON = "plugins/tt-model-bringup/.claude-plugin/plugin.json"
SKILLS_API = f"https://api.github.com/repos/tenstorrent/skills/contents/{SKILLS_PLUGIN_JSON}?ref=main"
DEFAULT_MAX_AGE_DAYS = 7
EXIT_PASS, EXIT_ADVISORY, EXIT_CRITICAL, EXIT_ERROR = 0, 1, 2, 3


class CheckError(Exception):
    pass


def slugify(hf_model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", hf_model.lower()).strip("_")


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise CheckError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text.split("+")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def resolve_checkout(repo: Path, *, branch: Optional[str], offline: bool, max_age_days: int) -> dict[str, Any]:
    """Compare HEAD with the upstream default branch: commits behind and days behind."""
    info: dict[str, Any] = {"path": str(repo), "head": git(repo, "rev-parse", "HEAD")}
    try:
        info["remote"] = git(repo, "remote", "get-url", "origin")
    except CheckError:
        info["remote"] = None
    info["dirty"] = bool(git(repo, "status", "--porcelain"))
    upstream = branch
    if upstream is None:
        try:
            upstream = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[1]
        except CheckError:
            upstream = "main"
    info["upstream_branch"] = upstream
    fetched = False
    if not offline:
        try:
            git(repo, "fetch", "--quiet", "origin", upstream, timeout=300)
            fetched = True
        except (CheckError, subprocess.TimeoutExpired) as exc:
            info["fetch_error"] = str(exc)
    info["fetched"] = fetched
    try:
        upstream_ref = f"origin/{upstream}"
        info["upstream_head"] = git(repo, "rev-parse", upstream_ref)
        base = git(repo, "merge-base", "HEAD", upstream_ref)
        info["merge_base"] = base
        info["commits_behind"] = int(git(repo, "rev-list", "--count", f"HEAD..{upstream_ref}"))
        info["commits_ahead"] = int(git(repo, "rev-list", "--count", f"{upstream_ref}..HEAD"))
        base_date = int(git(repo, "log", "-1", "--format=%ct", base))
        upstream_date = int(git(repo, "log", "-1", "--format=%ct", upstream_ref))
        info["days_behind"] = round((upstream_date - base_date) / 86400, 1)
    except CheckError as exc:
        info["compare_error"] = str(exc)
        info["days_behind"] = None
        info["commits_behind"] = None
    days = info.get("days_behind")
    if days is None:
        info["verdict"] = "unknown"
    elif days > max_age_days:
        info["verdict"] = "stale"
    else:
        info["verdict"] = "fresh"
    return info


def installed_transformers(python: str) -> Optional[str]:
    result = subprocess.run(
        [python, "-c", "import transformers; print(transformers.__version__)"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def checkpoint_transformers(hf_model: Optional[str], hf_config: Optional[Path]) -> Optional[str]:
    path = hf_config
    if path is None and hf_model:
        path = Path(hf_model) / "config.json" if Path(hf_model).is_dir() else _cached_config(hf_model)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("transformers_version")
    except (OSError, ValueError):
        return None


def _cached_config(hf_model: str) -> Optional[Path]:
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    repo_dir = home / "hub" / ("models--" + hf_model.replace("/", "--"))
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    candidates = sorted(snapshots.glob("*/config.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def resolve_transformers(python: str, hf_model: Optional[str], hf_config: Optional[Path], installed: Optional[str]) -> dict[str, Any]:
    installed = installed or installed_transformers(python)
    required = checkpoint_transformers(hf_model, hf_config)
    info: dict[str, Any] = {"installed": installed, "checkpoint_minimum": required, "python": python}
    if installed is None:
        info["verdict"] = "missing"
    elif required and version_tuple(installed) < version_tuple(required):
        info["verdict"] = "below_checkpoint_minimum"
    else:
        info["verdict"] = "ok"
    return info


def installed_skills_version() -> Optional[str]:
    for host in (".claude-plugin", ".codex-plugin"):
        path = ROOT / host / "plugin.json"
        if path.is_file():
            try:
                return json.loads(path.read_text()).get("version")
            except (OSError, ValueError):
                continue
    return None


def latest_skills_version(offline: bool, latest_file: Optional[Path]) -> tuple[Optional[str], str]:
    if latest_file is not None:
        try:
            return json.loads(latest_file.read_text()).get("version"), str(latest_file)
        except (OSError, ValueError):
            return None, str(latest_file)
    if offline:
        return None, "offline"
    try:
        with urllib.request.urlopen(SKILLS_API, timeout=15) as response:
            payload = json.loads(response.read().decode())
        import base64

        data = json.loads(base64.b64decode(payload["content"]).decode())
        return data.get("version"), SKILLS_API
    except Exception as exc:
        return None, f"unavailable: {exc}"


def resolve_skills_plugin(offline: bool, latest_file: Optional[Path]) -> dict[str, Any]:
    installed = installed_skills_version()
    latest, source = latest_skills_version(offline, latest_file)
    info: dict[str, Any] = {"installed": installed, "latest": latest, "latest_source": source, "root": str(ROOT)}
    if installed is None:
        info["verdict"] = "unknown"
    elif latest is None:
        info["verdict"] = "unverified"
    elif version_tuple(installed) < version_tuple(latest):
        info["verdict"] = "stale"
    else:
        info["verdict"] = "fresh"
    return info


def resolve(args: argparse.Namespace, model_dir: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_age_days": args.max_age_days,
        "tt_metal": resolve_checkout(Path(args.repo).resolve(), branch=args.tt_metal_branch, offline=args.offline, max_age_days=args.max_age_days),
        "transformers": resolve_transformers(args.python, args.hf_model, args.hf_config, args.transformers_version),
        "skills_plugin": resolve_skills_plugin(args.offline, args.skills_latest_file),
    }
    plugin_root = args.plugin_root or os.environ.get("VLLM_TT_PLUGIN_ROOT")
    if plugin_root:
        record["vllm_tt_plugin"] = resolve_checkout(Path(plugin_root).resolve(), branch=args.plugin_branch, offline=args.offline, max_age_days=args.max_age_days)
    else:
        record["vllm_tt_plugin"] = {"verdict": "unresolved", "reason": "VLLM_TT_PLUGIN_ROOT not set; resolve before the serving contract"}
    return record


def judge(record: dict[str, Any], *, new_run: bool) -> tuple[list[str], list[str]]:
    critical: list[str] = []
    advisory: list[str] = []
    severe = critical if new_run else advisory
    for name in ("tt_metal", "vllm_tt_plugin"):
        info = record.get(name) or {}
        verdict = info.get("verdict")
        if verdict == "stale":
            severe.append(
                f"{name} is {info.get('days_behind')} days and {info.get('commits_behind')} commits behind "
                f"origin/{info.get('upstream_branch')}; a new run starts from the upstream head"
            )
        elif verdict == "unknown":
            advisory.append(f"{name} freshness could not be compared ({info.get('compare_error') or info.get('fetch_error')})")
        elif verdict == "unresolved":
            advisory.append(f"{name}: {info.get('reason')}")
        if info.get("fetched") is False and info.get("verdict") not in ("unresolved",):
            advisory.append(f"{name}: upstream was not fetched ({info.get('fetch_error', 'offline')}); the comparison uses the last fetched state")
    tf = record.get("transformers") or {}
    if tf.get("verdict") == "below_checkpoint_minimum":
        critical.append(
            f"transformers {tf.get('installed')} is below the checkpoint's minimum {tf.get('checkpoint_minimum')}"
        )
    elif tf.get("verdict") == "missing":
        critical.append("transformers is not importable in the selected interpreter")
    sk = record.get("skills_plugin") or {}
    if sk.get("verdict") == "stale":
        severe.append(
            f"this skills plugin is {sk.get('installed')} but {sk.get('latest')} is published; update it with the host's plugin "
            "manager and restart the agent (the agent cannot update plugins)"
        )
    elif sk.get("verdict") in ("unverified", "unknown"):
        advisory.append(f"skills plugin version could not be verified ({sk.get('latest_source')})")
    return critical, advisory


def verify(record: dict[str, Any], args: argparse.Namespace) -> tuple[list[str], list[str]]:
    critical: list[str] = []
    advisory: list[str] = []
    for name, path in (("tt_metal", Path(args.repo).resolve()), ("vllm_tt_plugin", Path(args.plugin_root or os.environ.get("VLLM_TT_PLUGIN_ROOT") or "") or None)):
        info = record.get(name) or {}
        pinned = info.get("head")
        if not pinned:
            if name == "tt_metal":
                critical.append("stack_pins.json has no tt_metal pin")
            else:
                advisory.append("stack_pins.json has no vllm_tt_plugin pin")
            continue
        if path is None or not Path(path).is_dir():
            advisory.append(f"{name} checkout {path} is not available to verify")
            continue
        try:
            head = git(Path(path), "rev-parse", "HEAD")
        except CheckError as exc:
            advisory.append(str(exc))
            continue
        if not head.startswith(pinned) and not pinned.startswith(head):
            descends = subprocess.run(
                ["git", "-C", str(path), "merge-base", "--is-ancestor", pinned, "HEAD"],
                text=True, capture_output=True,
            ).returncode == 0
            if not descends:
                critical.append(f"{name} HEAD {head[:12]} is not the recorded pin {pinned[:12]} and does not descend from it")
            else:
                ahead = int(git(Path(path), "rev-list", "--count", f"{pinned}..HEAD"))
                advisory.append(f"{name} HEAD is {ahead} commits past the recorded pin {pinned[:12]} (stage-owned commits)")
    if args.serving_contract:
        contract_path = Path(args.serving_contract)
        if contract_path.is_file():
            try:
                contract_commit = json.loads(contract_path.read_text()).get("plugin", {}).get("commit")
            except (OSError, ValueError):
                contract_commit = None
            pinned = (record.get("vllm_tt_plugin") or {}).get("head")
            if contract_commit and pinned and not contract_commit.startswith(pinned) and not pinned.startswith(contract_commit):
                critical.append(
                    f"serving contract pins plugin {contract_commit[:12]} but stack_pins.json pinned {pinned[:12]}; "
                    "re-resolve the stack or record an explicit migration"
                )
    return critical, advisory


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def main(argv: Optional[list[str]] = None) -> int:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("resolve", "verify"), default="verify")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--hf-model", default=None)
    parser.add_argument("--repo", default=".", help="tt-metal checkout (default: current directory)")
    parser.add_argument("--plugin-root", default=None, help="TT vLLM plugin checkout (default: VLLM_TT_PLUGIN_ROOT)")
    parser.add_argument("--python", default=sys.executable, help="interpreter whose transformers is checked")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--resume", action="store_true", help="resolve for a resumed run: stale upstreams are advisory")
    parser.add_argument("--offline", action="store_true", help="skip fetches and network lookups")
    parser.add_argument("--serving-contract", default=None, help="verify: serving contract whose plugin pin must match")
    parser.add_argument("--tt-metal-branch", default=None)
    parser.add_argument("--plugin-branch", default=None)
    parser.add_argument("--hf-config", type=Path, default=None, help="checkpoint config.json (default: HF cache lookup)")
    parser.add_argument("--transformers-version", default=None, help="override the installed version (tests)")
    parser.add_argument("--skills-latest-file", type=Path, default=None, help="plugin.json to read the latest version from (tests)")
    args = parser.parse_args(argv)

    if args.model_dir:
        model_dir = Path(args.model_dir)
    elif args.hf_model:
        model_dir = Path("models/autoports") / slugify(args.hf_model)
    else:
        print("pass --model-dir or --hf-model", file=sys.stderr)
        return EXIT_ERROR
    record_path = model_dir / RECORD_PATH

    try:
        if args.mode == "resolve":
            record = resolve(args, model_dir)
            critical, advisory = judge(record, new_run=not args.resume)
            record["verdict"] = "critical" if critical else ("advisory" if advisory else "pass")
            record["critical"] = critical
            record["advisory"] = advisory
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            print(f"stack pins written to {record_path}")
        else:
            if not record_path.is_file():
                print(f"critical: missing {record_path}; run check_stack_freshness.py --mode resolve at startup", file=sys.stderr)
                return EXIT_CRITICAL
            record = json.loads(record_path.read_text())
            critical, advisory = verify(record, args)
            if record.get("verdict") == "critical":
                critical.append("stack_pins.json records a critical freshness verdict that was never resolved")
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    for line in critical:
        print(f"critical: {line}", file=sys.stderr)
    for line in advisory:
        print(f"advisory: {line}", file=sys.stderr)
    if critical:
        return EXIT_CRITICAL
    if advisory:
        return EXIT_ADVISORY
    print(f"stack freshness {args.mode} passed for {model_dir}")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
