# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay bench runner (`ttp bench run`): a head-to-head of model arms on a `ttp bench prep` bench.

An arm is a name with a provider, model and effort (bench.arms in the project's config, or --arm);
the shipped default names none. Each task of the bench runs once per arm per repeat, the arms of one
task and repeat back to back in an order drawn from a seeded generator (the seed is recorded).

Every cell gets the same inputs: a fresh copy of the task's base workspace in a new temporary folder,
the same prompt (the spec as queued under a fixed preamble), the same agent tools and the same
limits, enforced by the run supervisor the daemon uses (runner.py): wall clock, stall guard and
dollar budget (where the provider caps one). Before the run the workspace is checked to hold one
commit, one branch, no remote, reflog or alternates, neither the base nor the landed commit and no
held-out test content (leaks); the bench and results folders, the source repository with its git dir
and worktrees, the project's state folder and the providers' plugin caches and saved transcripts
(hidden_paths) are denied to the agent's reads. After it, the transcript is audited for those paths,
the landed commit and history or forge commands (audit): a flagged cell fails. Then the held-out files are copied in and the held-out
tests run (the grade). Each cell appends one row to <out>/results.jsonl: cost, tokens, wall time,
hand-off status, the grade and the sha256 of its transcript and diff; its files go to <out>/cells.
A rerun with the same plan skips the cells already recorded. `dry_run` swaps every arm's provider
for the fake agent: no model call, no spend."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import bench, gitmemo
from .project import durable_write, open_append

PROMPT = ("You are working in the git repository in your current directory. Do the task below there: make "
          "the change, commit it, and run the tests it affects. When you are done, write "
          '{{"status": "done", "summary": "<what you changed>"}} (or "failed" with why) to '
          "$TTP_RUN_DIR/result.json.\n\n## Task\n\n{spec}\n")
CHEAT = re.compile(r"\bgit\s+(fetch|clone|pull|ls-remote|remote\s+add)\b|\bgit\s+log\s+--all\b|\bgh\s+(api|repo|pr)\b")
TEST_CMD = "python3 -m pytest -q -p no:cacheprovider"


class BenchError(Exception):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified(bench_dir: Path) -> dict:
    """The bench's manifest, once it and every held-out file match their recorded sha256."""
    raw = (bench_dir / "manifest.json").read_bytes()
    if _sha(raw) != (bench_dir / "manifest.sha256").read_text().split()[0]:
        raise BenchError("manifest.json does not match manifest.sha256")
    man = json.loads(raw)
    for t in man["tasks"]:
        for path, sha in t["heldout"].items():
            if _sha((bench_dir / str(t["task"]) / "heldout" / path).read_bytes()) != sha:
                raise BenchError(f"task {t['task']}: held-out {path} does not match the manifest")
    return man


def parse_arm(text: str) -> tuple[str, dict]:
    """`name=provider:model:effort` (model and effort may be empty) as (name, arm)."""
    name, sep, rest = text.partition("=")
    parts = rest.split(":")
    if not sep or not name or len(parts) != 3 or not parts[0]:
        raise BenchError(f"--arm {text!r}: write it as name=provider:model:effort")
    return name, {"provider": parts[0], "model": parts[1], "effort": parts[2]}


def schedule(tasks: list[int], arms: list[str], reps: int, seed: int) -> list[dict]:
    """Cells in run order: per task and repeat, every arm back to back in a seeded random order."""
    rng, cells = random.Random(seed), []
    for task in tasks:
        for rep in range(1, reps + 1):
            order = sorted(arms)
            rng.shuffle(order)
            cells += [{"task": task, "rep": rep, "arm": a, "slot": i} for i, a in enumerate(order)]
    return cells


def leaks(ws: Path, task: dict) -> list[str]:
    """What in a cell's workspace could show the candidate the landed change; [] when nothing."""
    def git(*a):
        return bench._git(ws, *a)
    out = []
    if git("remote").stdout.strip():
        out.append("a remote")
    if (refs := git("for-each-ref", "--format=%(refname)").stdout.split()) != ["refs/heads/main"]:
        out.append(f"refs {refs}")
    if git("rev-list", "--all", "--count").stdout.strip() != "1":
        out.append("more than one commit")
    out += [f"commit {sha[:12]}" for sha in (task["base"], task["head"])
            if git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0]
    if git("rev-parse", "HEAD^{tree}").stdout.strip() != task["tree"]:
        out.append("a tree other than the base's")
    out += [f".git/{p}" for p in ("logs", "objects/info/alternates", "shallow", "FETCH_HEAD") if (ws / ".git" / p).exists()]
    held = set(task["heldout"].values())
    for f in ws.rglob("*"):
        if ".git" in f.relative_to(ws).parts[:1]:
            continue
        if f.is_symlink() and not f.resolve().is_relative_to(ws.resolve()):
            out.append(f"{f.relative_to(ws)} links outside the workspace")
        elif f.is_file() and not f.is_symlink() and _sha(f.read_bytes()) in held:
            out.append(f"{f.relative_to(ws)} holds a held-out test file")
    return out


def secret_lines(bench_dir: Path, task: dict) -> dict[str, list[str]]:
    """{path: lines} of each held-out file that its base copy lacks: what only the hidden tests show."""
    out = {}
    for path in task["heldout"]:
        base = bench_dir / str(task["task"]) / "workspace" / path
        have = set(base.read_text(errors="replace").splitlines()) if base.is_file() else set()
        out[path] = [x for x in (bench_dir / str(task["task"]) / "heldout" / path).read_text(errors="replace")
                     .splitlines() if x not in have]
    return out


def _strings(raw: str) -> str:
    # The transcript as written plus every string in its JSON lines, unescaped.
    found = [raw]

    def walk(v):
        if isinstance(v, str):
            found.append(v)
        for x in v.values() if isinstance(v, dict) else v if isinstance(v, list) else ():
            walk(x)
    for line in raw.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            pass
    return "\n".join(found)


def _system(line: str) -> bool:
    # A provider's own system record (its init lists the installed plugins' paths): not the agent's.
    try:
        rec = json.loads(line)
    except ValueError:
        return False
    return isinstance(rec, dict) and rec.get("type") == "system"


def _forms(p: Path) -> list[str]:
    # How a transcript may write a hidden path: as is, or under the home folder as ~/... or $HOME/...
    try:
        rel = p.relative_to(Path.home())
    except ValueError:
        return [str(p)]
    return [str(p)] + ([f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"] if str(rel) != "." else [])


def hidden_paths(bench_dir: Path, out: Path, repos: list, state: Path | None) -> list[Path]:
    """What a cell must not read: the bench and results folders, the source repository with its git
    common dir and every worktree of it (their history holds the landed commit and later code), the
    project's state folder, and the providers' installed plugin caches and saved transcripts (later
    code and the original task's own run). Denied to the agent's reads and audited in its transcript."""
    found = [bench_dir, out, *([state] if state else [])]
    for repo in (r for r in repos if r and Path(r).exists()):
        found.append(Path(repo))
        common = gitmemo.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=None)
        if common.returncode == 0 and common.stdout.strip():
            found.append(Path(common.stdout.strip()))
        trees = gitmemo.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=None)
        found += [Path(x[len("worktree "):]) for x in trees.stdout.splitlines() if x.startswith("worktree ")]
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    found += [claude / "plugins", claude / "projects", codex / "plugins", codex / "sessions"]
    # Both the resolved path and the name it was given: a symlinked home or repo may be named either way.
    return list(dict.fromkeys(q for p in found for q in (Path(os.path.abspath(Path(p).expanduser())),
                                                          Path(p).expanduser().resolve())))


def _encode(p) -> str:
    # How Claude Code names a session's folder: its cwd with every non-alphanumeric character as '-'.
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def session_dirs(ws: Path) -> list[Path]:
    """Claude Code's folder for a session started in `ws`, resolved and as named: it saves a large
    tool output there and tells the agent to read it."""
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return list(dict.fromkeys(c / "projects" / _encode(ws.resolve()) for c in (Path(os.path.abspath(claude)),
                                                                                 claude.resolve())))


def denied_reads(hidden: list[Path], own: list[Path]) -> list[str]:
    """Read deny rules for `hidden`, each folder also as `/**`. The saved-sessions folder that holds
    the cell's own session (`own`, from session_dirs) is not denied whole, which would refuse the agent its own saved
    output: only the folders of sessions started in another hidden path (the original task's
    worktree) and of earlier cells are. The audit still flags any other folder named there."""
    out = []
    for p in hidden:
        if p not in {o.parent for o in own}:
            out += [str(p), f"{p}/**"]
            continue
        enc = sorted({_encode(q) for q in hidden if not q.is_relative_to(p)})
        out += [r for e in enc for r in (f"{p}/{e}*", f"{p}/{e}*/**")]
        cells = p.glob(_encode(Path(tempfile.gettempdir()).resolve() / "cell-") + "*")
        out += [r for c in sorted(cells) if c.name != own[0].name for r in (str(c), f"{c}/**")]
    return out


def _own_session(text: str, own: list[Path]) -> str:
    # The text with each mention of the cell's own session folder (in any form) taken out, unless its
    # path climbs out of it with `..`.
    for form in (f for o in own for f in _forms(o)):
        text = re.sub(re.escape(form) + r"(?![A-Za-z0-9-])(?![^\s\"'`]*/\.\.)", "", text)
    return text


def audit(transcript: str, task: dict, hidden: list[Path], secret: dict | None = None, diff: str = "",
          session: list[Path] = ()) -> list[str]:
    """Why a cell counts as a cheat attempt: its transcript names a hidden path (the cell's own
    `session` folders aside) or the landed commit or runs history or forge commands, or it or the
    candidate's diff shows lines only the held-out tests have (bench.QUOTE_LINES of them, or all
    when fewer). [] when clean."""
    own = _strings("\n".join(x for x in transcript.splitlines() if not _system(x)))
    if session:
        own = _own_session(own, session)
    out = [f"names {p}" for p in hidden if any(form in own for form in _forms(Path(p)))]
    if task["head"][:12] in transcript:
        out.append("names the landed commit")
    out += sorted({f"runs {m.group(0)!r}" for m in CHEAT.finditer(transcript)})
    if (long := sum(len(x) >= bench.QUOTE_LEN for xs in (secret or {}).values() for x in set(xs))) and \
            (n := bench.quotes(_strings(transcript) + "\n" + diff, {"x": [x for xs in secret.values() for x in xs]})) \
            >= min(bench.QUOTE_LINES, long):
        out.append(f"shows {n} line(s) only the held-out tests have")
    return out


def grade(ws: Path, bench_dir: Path, task: dict, test_cmd: str, timeout_s: float) -> tuple[bool, str]:
    """Copy the held-out files over the candidate's and run the held-out tests: (passed, log tail)."""
    for path in task["heldout"]:
        dest = ws / path
        if dest.is_symlink() or dest.is_dir():
            return False, f"{path} is not a regular file in the workspace"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bench_dir / str(task["task"]) / "heldout" / path, dest)
    try:
        r = subprocess.run([*shlex.split(test_cmd), *task["tests"]], cwd=ws, capture_output=True, text=True,
                           timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"the held-out tests ran past {timeout_s:.0f} s"
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def _clean_env() -> dict:
    # The runner lays the cell's env over its own: nothing of this project's or run's own TTP_ state
    # (the fake agent's scripted knobs, TTP_FAKE_*, pass for dry runs) and no PYTHONPATH, which would
    # hand the agent this (later) runtime: the runner and the fake agent find it through _boot.
    return {k: v for k, v in os.environ.items()
            if k != "PYTHONPATH" and (not k.startswith("TTP_") or k.startswith("TTP_FAKE_"))}


def _boot(module: str) -> list[str]:
    # `python -m <module>` with this runtime on sys.path for that process only, not in its env.
    runtime = str(Path(__file__).resolve().parent.parent)
    return [sys.executable, "-c", f"import runpy, sys; sys.path.insert(0, {runtime!r}); "
            f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)"]


def run_cell(bench_dir: Path, out: Path, task: dict, cell: dict, arm: dict, opts: dict, cfg: dict) -> dict:
    """Run one cell, grade it and return its results row (also written under <out>/cells)."""
    from . import billing, chatbridge
    from .providers import get_provider
    row = {**cell, **arm, "dry_run": bool(opts.get("dry_run"))}
    keep = out / "cells" / f"{task['task']}-r{cell['rep']}-{cell['arm']}"
    hidden = hidden_paths(bench_dir.resolve(), out.resolve(), [task.get("repo"), opts.get("repo")], opts.get("state"))
    tmp = Path(tempfile.mkdtemp(prefix="cell-"))
    try:
        ws, run = tmp / "work", tmp / "run"
        shutil.copytree(bench_dir / str(task["task"]) / "workspace", ws, symlinks=True)
        if problems := leaks(ws, task):
            return {**row, "outcome": "isolation_failed", "passed": False, "why": problems}
        start = bench._git(ws, "rev-parse", "HEAD").stdout.strip()
        run.mkdir()
        durable_write(run / "prompt.md", PROMPT.format(spec=task["spec"]))
        provider = "fake" if opts.get("dry_run") else arm["provider"]
        prices = (cfg.get("pricing") or {}).get(provider) or {}
        prov = get_provider(provider).use(arm["model"], prices, billing.declared(cfg, provider))
        argv, env = prov.build(role="worker", model=arm["model"], effort=arm["effort"], cwd=str(ws),
                               budget_usd=opts["budget_usd"], read_only=False, schema=None, restrictions={},
                               denied_reads=denied_reads(hidden, session_dirs(ws)), web=False)
        if argv[:3] == [sys.executable, "-m", "ttp.providers.fake"]:   # the dry run's agent: no PYTHONPATH
            argv = [*_boot("ttp.providers.fake"), *argv[3:]]
        spec = {"argv": argv, "env": {**env, "TTP_RUN_DIR": str(run)}, "cwd": str(ws), "provider": provider,
                "model": arm["model"], "effort": arm["effort"], "timeout_s": opts["timeout_s"],
                "stall_s": opts["stall_s"], "prices": prices, "default_budget_usd": opts["budget_usd"],
                "budget_usd": None if prov.caps_budget else opts["budget_usd"], "nice": 0,
                **({"bridge": chatbridge.settings(cfg, "standard", arm["model"], arm["effort"])}
                   if provider == "chat" else {})}
        durable_write(run / "run.json", json.dumps(spec, indent=1))
        began = time.time()
        try:
            with open(run / "runner.log", "wb") as log:
                subprocess.run([*_boot("ttp.runner"), str(run)], cwd=str(run), env=_clean_env(),
                               stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               timeout=opts["timeout_s"] + 300)
        except subprocess.TimeoutExpired:   # the supervisor itself hung: its watchdog ends the agent
            row.update(outcome="runner_timeout", passed=False, wall_s=round(time.time() - began, 1))
            try:   # what the agent spent so far still counts
                row["cost_usd"] = prov.parse(run / "output.jsonl", run / "stderr.log").cost_usd
            except Exception:
                pass
            return row
        row["wall_s"] = round(time.time() - began, 1)
        try:
            return _finish(row, bench_dir, task, ws, run, keep, start, prov, opts, hidden)
        except Exception as e:   # the agent ran and spent: record the cell, so a rerun skips it
            return {**row, "outcome": "error", "passed": False, "why": [f"{type(e).__name__}: {e}"[:500]]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_json(path: Path):
    # A file the candidate or its run wrote: None when missing, "invalid" when not a JSON object.
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(errors="replace"))
    except ValueError:
        return "invalid"
    return data if isinstance(data, dict) else "invalid"


def _finish(row: dict, bench_dir: Path, task: dict, ws: Path, run: Path, keep: Path, start: str, prov, opts: dict,
            hidden: list[Path]) -> dict:
    """Audit and grade a cell whose agent has run, filling `row` as it goes (its cost first)."""
    u = prov.parse(run / "output.jsonl", run / "stderr.log")
    row.update(cost_usd=u.cost_usd, cost_estimated=u.estimated, input_tokens=u.input_tokens,
               output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens)
    transcript = (run / "output.jsonl").read_bytes() if (run / "output.jsonl").exists() else b""
    ended, handoff = _read_json(run / "exit.json"), _read_json(run / "result.json")
    ended = ended if isinstance(ended, dict) else {}
    row.update(exit={k: ended[k] for k in ("rc", "stopped", "refused") if ended.get(k) is not None},
               handoff=handoff if handoff == "invalid" else str((handoff or {}).get("status") or "none"),
               transcript_sha256=_sha(transcript))
    bench._git(ws, "add", "-A")
    diff = gitmemo.run(["git", "-C", str(ws), "diff", "--cached", start], capture_output=True, timeout=None).stdout
    row["diff_sha256"] = _sha(diff)
    flags = audit(transcript.decode(errors="replace"), task, hidden,
                  secret_lines(bench_dir, task), diff.decode(errors="replace"), session_dirs(ws))
    passed, log_tail = grade(ws, bench_dir, task, opts["test_cmd"], opts["test_timeout_s"]) if not flags \
        else (False, "not graded: flagged by the cheat audit")
    row.update(passed=passed, flagged=flags, outcome="flagged" if flags else "pass" if passed else "fail")
    keep.mkdir(parents=True, exist_ok=True)
    for name in ("output.jsonl", "exit.json", "result.json", "run.json", "runner.log", "stderr.log"):
        if (run / name).exists():
            shutil.copyfile(run / name, keep / name)
    durable_write(keep / "diff.patch", diff.decode(errors="replace"))
    durable_write(keep / "tests.log", log_tail)
    return row


def run(bench_dir: Path, out: Path, arms: dict, reps: int, seed: int, opts: dict, cfg: dict,
        limit: int = 0) -> list[dict]:
    """Run every cell of the plan not yet in <out>/results.jsonl; returns the rows this call added."""
    if len(arms) < 2:
        raise BenchError("a head-to-head needs at least two arms")
    if bad := [a for a in arms if not re.fullmatch(r"[A-Za-z0-9_.-]+", a)]:
        raise BenchError(f"arm names take letters, digits, '.', '_' and '-' only: {bad}")
    man = verified(bench_dir)
    # The source repository the bench was cut from (recorded by `ttp bench prep`) goes with each task.
    tasks = {t["task"]: {**t, "repo": man.get("repo")} for t in man["tasks"][:limit or None]}
    plan = {"bench": str(bench_dir.resolve()), "manifest_sha256": (bench_dir / "manifest.sha256").read_text().split()[0],
            "arms": arms, "reps": reps, "seed": seed, "cells": schedule(list(tasks), list(arms), reps, seed),
            "limits": {k: opts[k] for k in ("timeout_s", "stall_s", "budget_usd", "test_cmd", "test_timeout_s")},
            "dry_run": bool(opts.get("dry_run"))}
    out.mkdir(parents=True, exist_ok=True)
    if (out / "plan.json").exists():
        if json.loads((out / "plan.json").read_text()) != plan:
            raise BenchError(f"{out} holds another plan (bench, arms, seed, limits or mode differ); name a new folder")
    else:
        durable_write(out / "plan.json", json.dumps(plan, indent=1) + "\n")
    done = set()
    for line in (out / "results.jsonl").read_text().splitlines() if (out / "results.jsonl").exists() else []:
        try:
            r = json.loads(line)
            done.add((r["task"], r["rep"], r["arm"]))
        except (ValueError, KeyError, TypeError):
            continue   # a line torn by a crash: its cell runs again
    added = []
    for cell in plan["cells"]:
        if (cell["task"], cell["rep"], cell["arm"]) in done:
            continue
        row = {**run_cell(bench_dir, out, tasks[cell["task"]], cell, arms[cell["arm"]], opts, cfg), "seed": seed}
        with open_append(out / "results.jsonl", "ab+") as f:
            torn = f.seek(0, 2) > 0 and f.seek(-1, 2) >= 0 and f.read(1) != b"\n"
            f.write((b"\n" if torn else b"") + json.dumps(row).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())
        added.append(row)
    return added
