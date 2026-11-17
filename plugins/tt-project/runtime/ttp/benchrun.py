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
the landed commit and history or forge commands (audit): a flagged cell fails (it is still graded, and `heldout_passed` keeps the grade). Then the held-out files are copied in and the held-out
tests run (the grade). The agent and the grade each run in a fresh venv in the cell's folder (cellenv:
the user site off, pip refused outside a venv), so no cell installs into or imports from ~/.local
and nothing the agent installed runs the held-out tests. Each cell appends one row to <out>/results.jsonl: cost, tokens, wall time,
hand-off status, the grade and the sha256 of its transcript and diff; its files go to <out>/cells.
A rerun with the same plan skips the cells already recorded. <out>/cells.json is the blind review's
input (`ttp bench blind prep <out>`, benchblind.py): per task and repeat whose arms all ran, the task id,
the acceptance criteria from its spec and each arm's diff with the metadata that names it (provider,
model, effort, run and session id, transcript path). `dry_run` swaps every arm's provider
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

from . import bench, cellenv, cellnet, cellns, gitmemo, serverhealth
from .project import durable_write, open_append

PROMPT = ("You are working in the git repository in your current directory. Do the task below there: make "
          "the change, commit it, and run the tests it affects. You have {limit_min} minutes in all; that is a "
          "hang guard, so plan for far less. Run only focused tests: the test files for the code you changed "
          "(`python3 -m pytest -q <test file>`), or `python3 -m pytest -q <test file> -k <name>` for the tests "
          "of the functions you touched. The repository's whole suite may take longer than your time; run it "
          "only if you know it fits.{xdist} When you are done, write "
          '{{"status": "done", "summary": "<what you changed>"}} (or "failed" with why) to '
          "$TTP_RUN_DIR/result.json.\n\n## Task\n\n{spec}\n")


XDIST_LINE = (" pytest-xdist is installed: append `-n {n}` (or fewer) to a pytest command to run its tests in "
              "parallel.")


# An acceptance item the sealed replay cannot meet: it needs the live system (live logs or runs, the
# state db, deployed hosts or other machines, other projects, push, landing or deploy, before/after
# numbers). mark_live tags each such line with NOT_CHECKABLE so the arm and the reviewer skip it.
LIVE = re.compile(r"(?i)\blive\b|\bproject\.db\b|\bstate (?:db|database)\b|\bttp (?:push|express|setup|upgrade)\b"
                  r"|\bdeploy(?:ed|s|ing)?\b|\b(?:other|remote|every|each|all) (?:machines?|hosts?|boxes|projects?)\b"
                  r"|\bon (?:the |a )?(?:hosts?|machines?|boxes?)\b|\bbefore[/ ]and[/ ]after\b|\bbefore/after\b"
                  r"|\bland(?:s|ed|ing)? (?:it|the change|on)\b|\bpush(?:es|ed)? (?:it|the change) to\b")
NOT_CHECKABLE = "[not checkable in the replay: needs the live system]"


def mark_live(text: str) -> str:
    """`text` with each line LIVE matches (an acceptance item that needs the live system) ending in
    NOT_CHECKABLE; other lines unchanged."""
    return "\n".join(f"{x} {NOT_CHECKABLE}" if LIVE.search(x) and NOT_CHECKABLE not in x else x
                     for x in text.splitlines())


LIVE_LINE = (" Lines of the task marked " + NOT_CHECKABLE + " need the live system, which this repository "
             "copy lacks: skip them, and do not fake their results.")


def prompt(spec: str, opts: dict, xdist_n: int = 0) -> str:
    """The cell agent's prompt: the task's spec, its time limit and a focused test command. The parallel
    test line appears only when the cell's venv imports pytest-xdist (`xdist_n`, its grant), never as a
    claim the agent's pytest would refuse. Spec lines that need the live system are marked (mark_live)
    and the agent is told to skip them."""
    marked = mark_live(spec)
    return (opts.get("prompt") or PROMPT).format(spec=marked, limit_min=max(1, int(float(opts.get("timeout_s") or 1800) // 60)),
                         xdist=(XDIST_LINE.format(n=xdist_n) if xdist_n else "")
                         + (LIVE_LINE if NOT_CHECKABLE in marked else ""))
CHEAT = re.compile(r"\bgit\s+(fetch|clone|pull|ls-remote|remote\s+add)\b|\bgit\s+log\s+--all\b|\bgh\s+(api|repo|pr)\b")
TEST_CMD = "python3 -m pytest -q -p no:cacheprovider"
ISOLATE = True   # each cell's agent runs in a mount namespace (cellns.py); only tests turn this off (opts["isolate"])


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
    """`name=provider:model:effort[:harness[@version]]` (model and effort may be empty) as (name, arm). The
    harness is the agent the arm runs (HARNESSES): `claude` (the default, the provider's own CLI), or
    `opencode` or `mini-swe-agent` on the chat provider's OpenAI-compatible endpoint."""
    name, sep, rest = text.partition("=")
    parts = rest.split(":")
    if not sep or not name or len(parts) not in (3, 4) or not parts[0]:
        raise BenchError(f"--arm {text!r}: write it as name=provider:model:effort[:harness[@version]]")
    arm = {"provider": parts[0], "model": parts[1], "effort": parts[2]}
    if len(parts) == 4 and parts[3] not in ("", "claude"):
        kind, _, version = parts[3].partition("@")
        arm["harness"] = kind
        if version:
            arm["harness_version"] = version
    check_harness(name, arm)
    return name, arm


# Agents a bench arm may run instead of the provider's own CLI: each installs into a folder the bench owns
# (HARNESS_DIR/<kind>-<version>, never ~/.local or a global prefix) and talks to the chat provider's
# OpenAI-compatible endpoint through the run's bridge (its /v1 relay: native tool calls, no translation).
HARNESSES = {"opencode": {"npm": "opencode-ai", "bin": "node_modules/.bin/opencode"},
             "mini-swe-agent": {"pypi": "mini-swe-agent", "bin": "venv/bin/mini"},
             "codex": {"npm": "@openai/codex", "bin": "node_modules/.bin/codex"}}
# Tools a harness arm needs beside its agent (installed the same way, never run as an arm): the proxy that turns
# Codex's Responses API calls into the bridge's Chat Completions (codexproxy.py).
TOOLS = {"litellm-proxy": {"pypi": "litellm[proxy]", "bin": "venv/bin/litellm"}}
HARNESS_DIR = Path.home() / ".tt-project" / "model-bench" / "harness"
# How an arm's effort reaches the model in each harness: the bridge sets the chat provider's effort fields
# (providers.chat effort_fields) on every call, over whatever the harness itself sends.
# Providers a harness arm may use: the chat provider's endpoint for every harness, and the logged-in Claude
# CLI for mini-swe-agent (claudebridge: the CLI in print mode as the model, never an API key).
HARNESS_PROVIDERS = {"mini-swe-agent": ("chat", "claude"), "codex": ("chat", "claude")}
CLAUDE_EFFORT_NOTE = "no effort flag; the bridge passes --effort to the claude CLI on each call"
EFFORT_NOTE = {"claude": "Claude Code's effort; the bridge sets the chat provider's effort fields",
               "opencode": "no --variant; the bridge sets the chat provider's effort fields on each call",
               "mini-swe-agent": "no effort flag; the bridge sets the chat provider's effort fields on each call",
               "codex": "the proxy hook drops Codex's own reasoning effort; the bridge sets the model's effort on each call"}


def check_harness(name: str, arm: dict) -> None:
    kind = arm.get("harness") or "claude"
    if kind != "claude" and kind not in HARNESSES:
        raise BenchError(f"arm {name}: harness {kind!r} is not one of claude, {', '.join(HARNESSES)}")
    if kind != "claude" and arm.get("provider") not in HARNESS_PROVIDERS.get(kind, ("chat",)):
        raise BenchError(f"arm {name}: the {kind} harness runs on the {' or '.join(HARNESS_PROVIDERS.get(kind, ('chat',)))} "
                         "provider only")


def latest_version(kind: str) -> str:
    """The current release of harness `kind`, from its package index."""
    spec = HARNESSES.get(kind) or TOOLS[kind]
    if "npm" in spec:
        got = subprocess.run(["npm", "view", spec["npm"], "version"], capture_output=True, text=True, timeout=120)
        version = got.stdout.strip()
    else:
        import urllib.request
        with urllib.request.urlopen(f"https://pypi.org/pypi/{spec['pypi'].split('[')[0]}/json", timeout=60) as r:
            version = str(json.loads(r.read())["info"]["version"])
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", version or ""):
        raise BenchError(f"could not resolve the current {kind} version: {version!r}")
    return version


def harness_ready(kind: str, version: str = "", root: Path | None = None) -> dict:
    """Harness `kind` at `version` (default: the current release), installed under `root` (HARNESS_DIR)
    once: {kind, version, dir, bin}. The folder's harness.json marks a finished install."""
    spec, root = HARNESSES.get(kind) or TOOLS[kind], Path(root or HARNESS_DIR)
    version = version or latest_version(kind)
    folder = root / f"{kind}-{version}"
    marker = folder / "harness.json"
    if not marker.exists():
        folder.mkdir(parents=True, exist_ok=True)
        dl = folder / "download"   # the main package's own archive, kept to record its sha256
        shutil.rmtree(dl, ignore_errors=True)
        dl.mkdir()
        if "npm" in spec:
            steps = [["npm", "pack", "--silent", "--pack-destination", str(dl), f"{spec['npm']}@{version}"],
                     ["npm", "install", "--prefix", str(folder), "--no-fund", "--no-audit", f"{spec['npm']}@{version}"]]
        else:
            steps = [[sys.executable, "-m", "venv", str(folder / "venv")],
                     [str(folder / "venv" / "bin" / "python"), "-I", "-m", "pip", "download", "-q", "--no-deps",
                      "-d", str(dl), f"{spec['pypi'].split('[')[0]}=={version}"],
                     [str(folder / "venv" / "bin" / "python"), "-I", "-m", "pip", "install", "-q",
                      f"{spec['pypi']}=={version}"]]
        with open(folder / "install.log", "ab") as log:
            for argv in steps:
                if subprocess.run(argv, cwd=str(folder), stdout=log, stderr=subprocess.STDOUT,
                                  timeout=1800).returncode:
                    raise BenchError(f"installing {kind} {version} failed: see {folder / 'install.log'}")
        if not (folder / spec["bin"]).exists():
            raise BenchError(f"installing {kind} {version} left no {spec['bin']}")
        sums = {f.name: _sha(f.read_bytes()) for f in sorted(dl.iterdir()) if f.is_file()}
        durable_write(marker, json.dumps({"kind": kind, "version": version, "bin": spec["bin"], "sha256": sums}) + "\n")
    got = json.loads(marker.read_text())
    return {"kind": kind, "version": got["version"], "dir": str(folder), "bin": str(folder / got["bin"]),
            "sha256": got.get("sha256") or {}}


def harness_files(run: Path, arm: dict, inst: dict) -> list[str]:
    """Write the harness arm's launch script (and config) into `run`; returns its argv. The prompt comes on
    stdin; the agent's trajectory (mini-swe-agent) goes to run/trajectory.json."""
    kind, model = arm["harness"], arm["model"]
    q = shlex.quote
    if kind == "opencode":
        conf = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
                "model": f"bench/{model}", "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
                "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "bench",
                                       "options": {"baseURL": "{env:OPENAI_BASE_URL}", "apiKey": "{env:OPENAI_API_KEY}"},
                                       "models": {model: {"name": model}}}}}
        durable_write(run / "opencode.json", json.dumps(conf, indent=1) + "\n")
        body = (f"export OPENCODE_CONFIG={q(str(run / 'opencode.json'))} OPENCODE_DISABLE_AUTOUPDATE=1\n"
                f"exec {q(inst['bin'])} run --format json --auto -m {q('bench/' + model)} \"$(cat)\"\n")
    else:
        durable_write(run / "mini.yaml", "agent:\n  mode: yolo\n  cost_limit: 0\n"
                      "model:\n  cost_tracking: ignore_errors\n  model_kwargs:\n    drop_params: true\n")
        body = ("export MSWEA_CONFIGURED=true MSWEA_SILENT_STARTUP=1 MSWEA_COST_TRACKING=ignore_errors "
                f"MSWEA_GLOBAL_CONFIG_DIR={q(str(run / 'mswea'))}\n"
                f"exec {q(inst['bin'])} -y --exit-immediately -l 0 -m {q('openai/' + model)} -c mini.yaml "
                f"-c {q(str(run / 'mini.yaml'))} -o {q(str(run / 'trajectory.json'))} -t \"$(cat)\"\n")
    durable_write(run / "harness.sh", "#!/bin/sh\n" + body)
    return ["/bin/sh", str(run / "harness.sh")]


def schedule(tasks: list[int], arms: list[str], reps: int, seed: int) -> list[dict]:
    """Cells in run order: per task and repeat, every arm back to back in a seeded random order."""
    rng, cells = random.Random(seed), []
    for task in tasks:
        for rep in range(1, reps + 1):
            order = sorted(arms)
            rng.shuffle(order)
            cells += [{"task": task, "rep": rep, "arm": a, "slot": i} for i, a in enumerate(order)]
    return cells


def unreachable(ws: Path) -> int:
    """How many objects the repository at `ws` stores that no ref reaches (a later commit's, say); its own
    store only, whatever object folders the caller's environment adds."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")}

    def count(*a):
        return len(set(bench._git(ws, *a, env=env).stdout.split()))
    return count("cat-file", "--batch-all-objects", "--batch-check=%(objectname)") - count(
        "rev-list", "--objects", "--no-object-names", "--all")


def leaks(ws: Path, task: dict) -> list[str]:
    """What in a cell's workspace could show the candidate the landed change; [] when nothing."""
    def git(*a):
        return bench._git(ws, *a)
    out = []
    if git("remote").stdout.strip():
        out.append("a remote")
    if (refs := git("for-each-ref", "--format=%(refname)").stdout.split()) != ["refs/heads/main"]:
        out.append(f"refs {refs}")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != task["base"] and git("rev-list", "--count", "HEAD").stdout.strip() != "1":
        out.append(f"HEAD {head[:12]} is not the base")   # a sealed (one-commit) copy of the base is fine too
    if git("cat-file", "-e", f"{task['head']}^{{commit}}").returncode == 0:
        out.append(f"commit {task['head'][:12]}")
    if n := unreachable(ws):
        out.append(f"{n} objects HEAD does not reach")
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


TEXT_MAX = 1 << 20   # bytes of a base file read for its lines; a larger one is data, not code a test quotes


def base_lines(ws: Path) -> frozenset[str]:
    """Every line (whitespace collapsed, bench.flat) of each text file in the base workspace `ws`, .git aside:
    a held-out line found anywhere there is boilerplate the agent may write unaided. Read once per folder."""
    key = str(Path(ws).resolve())
    if key not in _BASE:
        found: set[str] = set()
        root = Path(key)
        for f in root.rglob("*") if root.is_dir() else ():
            if ".git" in f.relative_to(root).parts[:1] or f.is_symlink() or not f.is_file():
                continue
            try:
                data = f.read_bytes() if f.stat().st_size <= TEXT_MAX else b""
            except OSError:
                continue
            if b"\0" not in data[:8192]:
                found.update(bench.flat(x) for x in data.decode(errors="replace").splitlines())
        found.discard("")
        _BASE[key] = frozenset(found)
    return _BASE[key]


_BASE: dict[str, frozenset[str]] = {}


def secret_lines(bench_dir: Path, task: dict) -> dict[str, list[str]]:
    """{path: lines} of each held-out file that no text file of the base workspace has (whitespace aside): what
    only the hidden tests show."""
    have = base_lines(bench_dir / str(task["task"]) / "workspace")
    return {path: [x for x in (bench_dir / str(task["task"]) / "heldout" / path).read_text(errors="replace")
                   .splitlines() if bench.flat(x) and bench.flat(x) not in have] for path in task["heldout"]}


# What a transcript holds that the agent did not write: a tool's result (Claude's tool_result blocks and
# tool_use_result, Codex's aggregated_output, Cursor's result) and the records the provider or a tool adds.
OUTPUT_KEYS = frozenset({"tool_use_result", "tool_result_meta", "aggregated_output", "output", "stdout", "stderr",
                         "formatted_output", "result", "content_result"})
OUTPUT_TYPES = frozenset({"tool_result", "function_call_output", "custom_tool_call_output", "mcp_tool_call_output"})
NOT_AGENT = frozenset({"system", "user"})   # records the provider writes, or a tool's results (Claude's user record)
MESSAGE_TYPES = frozenset({"text", "thinking", "redacted_thinking", "agent_message", "reasoning", "message"})


def agent_text(transcript: str, messages: bool = True) -> str:
    """What the agent itself wrote, from its transcript: its tool calls' inputs and (`messages`) its own
    messages and reasoning, never what a tool returned (a file it read, an `env` it printed) nor the provider's
    own records. A line that is not JSON counts as the agent's."""
    found: list[str] = []

    def walk(v, depth=0):
        if isinstance(v, str):
            if v.lstrip()[:1] in ("{", "[") and depth < 8:   # arguments given as a JSON string
                try:
                    return walk(json.loads(v), depth + 1)
                except ValueError:
                    pass
            found.append(v)
        elif isinstance(v, dict):
            kind = v.get("type")
            if kind in OUTPUT_TYPES or (not messages and kind in MESSAGE_TYPES):
                return
            for k, x in v.items():
                if k not in OUTPUT_KEYS:
                    walk(x, depth)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth)
    for line in transcript.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            found.append(line)
            continue
        if isinstance(rec, dict) and rec.get("type") in NOT_AGENT:
            continue
        if not messages and isinstance(rec, dict) and isinstance(rec.get("item"), dict) \
                and rec["item"].get("type") in MESSAGE_TYPES:
            continue
        walk(rec)
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


def session_dirs(ws: Path, config: str | None = None) -> list[Path]:
    """Claude Code's folder for a session started in `ws` (under `config`, the cell's own config
    folder, if any), resolved and as named: it saves a large tool output there and tells the agent to read it."""
    claude = Path(config or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return list(dict.fromkeys(c / "projects" / _encode(ws.resolve()) for c in (Path(os.path.abspath(claude)),
                                                                                 claude.resolve())))


# Each provider's config folder variable, and the files a cell's own folder copies from the real one
# (Codex's hooks and config, which its build reads to decide the harness hook).
SCRATCH = {"claude": ("CLAUDE_CONFIG_DIR", ()), "codex": ("CODEX_HOME", ("config.toml", "hooks.json")),
           "cursor": ("CURSOR_CONFIG_DIR", ())}


def _stamp(p: Path):
    # The file as it is (an in-place rewrite within the clock's tick keeps its mtime): None when gone.
    try:
        return p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def scratch_home(provider: str, tmp: Path, logins: bool = True) -> tuple[dict, list]:
    """A config folder of the cell's own for `provider`, with no earlier sessions in it: (env naming
    it, its logins). Each login file the provider names is linked in, so the CLI reads the live one;
    restore_logins writes back one the CLI refreshed in the cell's folder."""
    from .providers import get_provider
    if provider not in SCRATCH:
        return {}, []
    var, copies = SCRATCH[provider]
    home = tmp / f"{provider}-config"
    home.mkdir()
    if copies:
        from .providers.codex import codex_home
        for name in (n for n in copies if (codex_home() / n).is_file()):
            shutil.copyfile(codex_home() / name, home / name)
    links = []
    for f in (Path(x) for x in (get_provider(provider).credential_files() if logins else [])):
        if f.is_file() and not (home / f.name).exists():
            (home / f.name).symlink_to(f.resolve())
            links.append((home / f.name, f.resolve(), _stamp(f.resolve())))
    conf = {var: str(home)}
    if provider == "claude" and not os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        # Claude Code names its macOS keychain entry after the config folder: keep the real one's, or a
        # cell on macOS finds no login there.
        from .providers.claude import claude_config_dir
        conf["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(claude_config_dir())
    return conf, links


def restore_logins(links: list) -> None:
    """A login the CLI replaced in the cell's folder (a token refresh writes a new file, not through
    the link) goes back to the real file, only while that is unchanged since the cell began: a refresh
    elsewhere meanwhile wins. Written beside it and renamed in, owner-only."""
    for mine, real, stamp in links:
        if mine.is_symlink() or not mine.is_file() or _stamp(real) != stamp:
            continue
        durable_write(real, mine.read_bytes(), 0o600)


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


# Where a tool call reaches: its command, and the paths it reads, lists or writes (Glob's pattern is a
# path too). Not file content it writes or a search pattern, which may name any folder.
REACH = ("command", "cmd", "file_path", "path", "paths", "notebook_path", "target_file", "target_directory",
         "cwd", "workdir", "dir", "directory")


def _commands(transcript: str) -> list[str]:
    # What the agent asked its tools to reach, one entry per tool call (commands and paths under a tool
    # call's input, args or command keys, not the outputs they returned); a line that is not JSON counts as one.
    found = []

    def walk(v, take=False, extra=()):
        if isinstance(v, str) and take:
            if v.lstrip()[:1] in ("{", "["):   # arguments given as a JSON string
                try:
                    return walk(json.loads(v), True, extra)
                except ValueError:
                    pass
            found[-1] += "\n" + v
        elif isinstance(v, dict):
            for k, x in v.items():
                if take:
                    if k in REACH or k in extra:
                        walk(x, True)
                elif k in ("input", "args", "arguments", "command", "cmd"):
                    found.append("")
                    walk(x, True, ("pattern",) if str(v.get("name", "")).lower() == "glob" else ())
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x, take, extra)
    for line in transcript.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            found.append(line)
    return [c for c in found if c.strip()]


# A saved-sessions folder named relative to wherever the command runs (`cd ~ && cat .claude/projects/..`),
# or a provider's config folder cd'ed into with its sessions named after it.
RELATIVE = re.compile(r"(?<![\w-])\.(?:claude/projects|codex/sessions|cursor/chats)(?![\w-])")
CONFIG_ROOT = re.compile(r"(?<![\w-])\.(?:claude|codex|cursor)/?(?=$|[\s;&|)'\"`])")
SESSIONS = re.compile(r"(?<![\w.-])(?:projects|sessions|chats)\b")
UP = re.compile(r"(?:^|[\s/=\"'`])\.\.(?=$|[/\s;&|)\"'`])")
CD = re.compile(r"(?:^|[;&|(\s])(?:cd|pushd)(?:[ \t]+([^\s;&|)]+))?(?=$|[\s;&|)])")


def _kept_cwd(call: str, state: tuple[bool, bool], session) -> tuple[bool, bool]:
    # Where a shell that keeps its cwd between calls (Claude's Bash) is after `call`, from its last cd:
    # (in a provider's config folder, in the cell's own session folder). A relative cd stays within.
    cds = [m.group(1) for m in CD.finditer(call)]
    if not cds:
        return state
    to = (cds[-1] or "~").strip("\"'")
    if to == "-":
        return state
    here = (bool(CONFIG_ROOT.search(to)), bool(session) and _own_session(to, list(session)) != to)
    return here if to.startswith(("/", "~", "$")) else (state[0] or here[0], state[1] or here[1])


def audit(transcript: str, task: dict, hidden: list[Path], secret: dict | None = None, diff: str = "",
          session: list[Path] = ()) -> list[str]:
    """Why a cell counts as a cheat attempt: its tool calls' inputs name a hidden path (the cell's own
    `session` folders aside) or run history or forge commands, its transcript names the landed commit, or
    what the agent wrote (agent_text) or the candidate's diff shows lines only the held-out tests have
    (secret_lines; bench.QUOTE_LINES of them, or all when fewer). A tool's output is never the agent's: a
    workspace file it read or an `env` it printed flags nothing. [] when clean."""
    inputs = agent_text(transcript, messages=False)   # what its tool calls asked for, never what they printed
    own = _own_session(inputs, session) if session else inputs
    out = [f"names {p}" for p in hidden if any(form in own for form in _forms(Path(p)))]
    if task["head"][:12] in transcript:
        out.append("names the landed commit")
    out += sorted({f"runs {m.group(0)!r}" for m in CHEAT.finditer(inputs)})
    calls = _commands("\n".join(x for x in transcript.splitlines() if not _system(x)))
    # A cd kept from an earlier call counts as if it were in this one (`cd ~/.claude`, then `cat projects/..`).
    named = up = False
    state = (False, False)
    for c in calls:
        rest = _own_session(c, list(session)) if session else c
        if not named and ((m := RELATIVE.search(rest)) or (state[0] or CONFIG_ROOT.search(rest))
                          and (m := SESSIONS.search(rest))):
            out.append(f"names a saved-sessions folder by a relative path: {m.group(0)}")
            named = True
        if session and not up and UP.search(c) and (state[1] or _own_session(c, list(session)) != c):
            out.append("climbs out of its own session folder with ..")
            up = True
        state = _kept_cwd(c, state, session)
    if (long := len({f for xs in (secret or {}).values() for x in xs if len(f := bench.flat(x)) >= bench.QUOTE_LEN})) \
            and (n := bench.quotes(agent_text(transcript) + "\n" + diff, [x for xs in secret.values() for x in xs])) \
            >= min(bench.QUOTE_LINES, long):
        out.append(f"quote_matches: {n}")
    return out


def grade_workers(env: dict) -> int:
    """How many xdist workers the grade's held-out tests run with: the environment's python imports
    pytest-xdist and the parallel grant the host allows (a quarter of its cores, at least 1, never more
    than its free ones), else 0 (no xdist: plain serial pytest)."""
    from . import checkcap, testslots
    py = str(Path(env["VIRTUAL_ENV"]) / "bin" / "python") if env.get("VIRTUAL_ENV") \
        else shutil.which("python3", path=env.get("PATH") or os.defpath) or "python3"
    try:
        has = subprocess.run([py, "-c", testslots.XDIST_PROBE], capture_output=True, env=env,
                              timeout=30, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return 0
    if not has:
        return 0
    return max(1, checkcap.cores() // 4)


def grade(ws: Path, bench_dir: Path, task: dict, test_cmd: str, timeout_s: float,
          env: dict | None = None) -> tuple[bool, str]:
    """Copy the held-out files over the candidate's and run the held-out tests with `env` (the cell's
    grading venv): (passed, log tail). They run as bench validation runs them (bench._held_cmd with the
    select plugin), so a case file's test runs where its aggregator collects it, never on its own. The
    tests run in parallel (`-n`) where the venv holds pytest-xdist and the host has room for it."""
    from . import benchcells
    for path in task["heldout"]:
        dest = ws / path
        if dest.is_symlink() or dest.is_dir():
            return False, f"{path} is not a regular file in the workspace"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bench_dir / str(task["task"]) / "heldout" / path, dest)
    argv, select = bench._held_cmd(test_cmd, list(task["tests"]))
    if select:
        base = dict(os.environ if env is None else env)
        env = {**base, **benchcells.select_env(select, base)}
    env = env or dict(os.environ)
    env.setdefault("PYTEST_ADDOPTS", "")
    if xdist_n := grade_workers(env):
        # The held-out tests run in parallel (pytest-xdist from the cell's venv): a cell never needs the
        # full serial suite, and the grade never waits on one worker.
        env["PYTEST_ADDOPTS"] = (env["PYTEST_ADDOPTS"] + " " if env["PYTEST_ADDOPTS"] else "") + f"-n {xdist_n}"
    try:
        r = subprocess.run(argv, cwd=ws, capture_output=True, text=True,
                           timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return False, f"the held-out tests ran past {timeout_s:.0f} s"
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def _clean_env() -> dict:
    # The runner lays the cell's env over its own: nothing of this project's or run's own TTP_ state
    # (the fake agent's scripted knobs, TTP_FAKE_*, pass for dry runs) and no PYTHONPATH, which would
    # hand the agent this (later) runtime: the runner and the fake agent find it through _boot.
    return {k: v for k, v in os.environ.items()
            if k != "PYTHONPATH" and (not k.startswith("TTP_") or k.startswith("TTP_FAKE_"))}


def _boot(module: str, cell=None) -> list[str]:
    # `python -m <module>` with this runtime on sys.path for that process only, not in its env. In a cell
    # (cellns.py) the real runtime does not exist: the dry run's fake agent runs from the copy it was given.
    runtime = (cell.runtime if cell is not None and cell.runtime else str(Path(__file__).resolve().parent.parent))
    python = cell.python if cell is not None and cell.python else sys.executable
    return [python, "-c", f"import runpy, sys; sys.path.insert(0, {runtime!r}); "
            f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)"]


def _in_cell(argv: list[str], cell, unset: list[str] = ()) -> list[str]:
    """`argv` as the agent runs it in the cell's namespace: the project's hooks left out of its settings (their
    commands live in the runtime, which the cell does not have; the namespace is the guard), its executable as
    the resolved install the namespace holds, and the namespace's own prefix."""
    argv = list(argv)
    if "--settings" in argv[:-1]:
        i = argv.index("--settings") + 1
        try:
            settings = json.loads(argv[i])
            settings.pop("hooks", None)
            argv[i] = json.dumps(settings)
        except (ValueError, AttributeError):
            pass
    if cell.cli and argv and os.path.realpath(shutil.which(argv[0]) or argv[0]) == os.path.realpath(cell.cli):
        argv[0] = cell.cli
    return cell.wrap(argv, unset)


def cell_agent_env(env: dict, inherited: dict, run: Path, cell, hidden: list[Path]) -> tuple[dict, list[str]]:
    """The variables `env` adds to a cell's agent, and the names of `inherited` (the runner's own environment) the
    namespace unsets, so no variable the agent can print names a hidden path or the runtime (paths that do not
    exist in its namespace, and point at the answer's source), nor any other host path the namespace does not
    hold (the runner's PWD, OLDPWD, a tool's config file: absent in the cell, they only tell where the real
    files are; cellns.Cell.outside). CLAUDE_ENV_FILE (loopproxy.sh, which lives in the
    runtime) names a copy in the cell's run folder instead, with nothing chained after it."""
    from . import loopproxy
    env = dict(env)
    if env.get("CLAUDE_ENV_FILE") or inherited.get("CLAUDE_ENV_FILE"):
        copy = run / loopproxy.SCRIPT.name
        shutil.copyfile(loopproxy.SCRIPT, copy)
        env.update({"CLAUDE_ENV_FILE": str(copy), loopproxy.PREV_ENV: ""})
    runtime = Path(__file__).resolve().parent.parent
    forms = [f for p in dict.fromkeys([*map(Path, hidden), runtime, Path(os.path.abspath(runtime))])
             for f in _forms(p)]
    sets = cell.sets()

    def names(v) -> bool:   # a hidden path or the runtime, or any host path the namespace does not hold
        return any(f in str(v) for f in forms) or cell.outside(v)
    kept = {k: v for k, v in env.items() if k in sets or not names(v)}
    return kept, sorted(k for k, v in inherited.items() if k not in kept and k not in sets and names(v))


def isolate(tmp: Path, ws: Path, arm: dict, opts: dict, hidden: list[Path], repos: list, canary: Path | None = None,
            gone: list[str] = ()):
    """The cell's mount namespace (cellns.py) for `arm`'s agent and what its self-test found wrong: (cell, problems).
    Raises cellns.Unavailable where no namespace can be built. The agent's config folders are the cell's own
    (never the real ones), the real login files of a provider that has them are bound in read-only, the project's
    hooks are not copied in, and a dry run's fake agent gets a copy of the runtime (the real one stays out).
    `canary`: an injection cell's folder (benchcells.injection_cell): its decoy home seeds the scratch HOME, and
    its stand-in tools, canary marks, bare remote and inputs are bound in at their own paths; the rest of it
    (cell.json, the pristine workspace, its sibling cells) and `hidden` stay out."""
    from .providers import get_provider
    provider = "fake" if opts.get("dry_run") else arm["provider"]
    prov = get_provider(provider)
    kind = "claude" if provider == "chat" else provider   # the chat provider runs Claude Code's CLI
    conf, creds, inst = {}, [], arm.get("harness_install") if not opts.get("dry_run") else None
    if inst:   # a harness arm: its own agent, read-only from its install folder; no Claude config
        kind = ""
    if kind in SCRATCH:
        conf, _ = scratch_home(kind, tmp, False)
        conf.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        folder = Path(conf[SCRATCH[kind][0]])
        (folder / "hooks.json").unlink(missing_ok=True)
        if provider not in ("chat", "fake"):
            creds = [(f, str(folder / Path(f).name)) for f in prov.credential_files()]
    runtime, copy = Path(__file__).resolve().parent.parent, None
    if provider == "fake":
        copy = tmp / "fakert"
        shutil.copytree(runtime / "ttp", copy / "ttp", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    python = str(tmp / "venv" / "bin" / "python")
    absent, extra = [*cellns.real_paths(runtime, repos), *hidden], {}
    if canary is not None:
        canary = canary.resolve()
        if (canary / "home").is_dir():   # the decoy credential, in the cell's own scratch HOME
            shutil.copytree(canary / "home", tmp / "home", dirs_exist_ok=True)
        binds = [("ro", canary / "bin"), ("bind", canary / "canary"), ("bind", canary / "remote.git"),
                 ("ro", canary / "inputs")]
        extra = {"standins": {t.name: t for t in sorted((canary / "bin").glob("*"))},
                 "binds": [(m, p) for m, p in binds if p.exists()], "remote": str(canary / "remote.git"),
                 "path_dirs": [str(tmp / "venv" / "bin"), str(canary / "bin")]}
        absent = [p for p in absent if not canary.is_relative_to(p)]   # a folder that holds the cell stays
        absent += [canary / "cell.json", canary / "workspace", canary / "home",
                   *(p for p in canary.parent.iterdir() if p != canary)]   # the pristine cell, its sibling cells
    if inst:
        extra["binds"] = [*extra.get("binds", []), ("ro", Path(inst["dir"]))]
    if opts.get("ro_binds"):   # folders of the cell bound read-only over its writable one (a read-only snapshot)
        extra["binds"] = [*extra.get("binds", []), *(("ro", Path(x)) for x in opts["ro_binds"])]
    if opts.get("deny_tools"):   # a folder of refusing stand-ins: each covers its tool, also a system copy
        deny = Path(opts["deny_tools"])
        extra["standins"] = {**extra.get("standins", {}), **{t.name: t for t in sorted(deny.glob("*"))}}
        extra["binds"] = [*extra.get("binds", []), ("ro", deny)]
        extra["path_dirs"] = [*(extra.get("path_dirs") or [str(tmp / "venv" / "bin")]), str(deny)]
    cli = inst["bin"] if inst else "" if provider == "fake" else str(prov.binary() or "")
    # An arm whose own CLI calls its provider's API (not a bridge: the chat provider's CLI and every harness arm
    # call the run's bridge, which makes the model call outside the cell) reaches it through the proxy, which
    # allows that provider's API domains only; every other cell has no network but its bridge.
    domains = cellnet.API_DOMAINS.get(provider, ()) if not inst and provider != "chat" and not opts.get("dry_run") else ()
    cell = cellns.build(tmp, ws, hidden, python=python, cli=cli, proxy_domains=domains,
                        config=conf, creds=creds, **{"path_dirs": [str(tmp / "venv" / "bin")], **extra})
    cell.python, cell.runtime = python, str(copy) if copy else ""
    return cell, cellns.selftest(cell, ws, absent, gone=gone)


REACH_CODE = ("import socket, sys\n"
              "for t in sys.argv[1:]:\n"
              "    h, _, p = t.rpartition(':')\n"
              "    try:\n"
              "        socket.create_connection((h, int(p)), timeout=3).close()\n"
              "        print('REACH', t, 'reachable')\n"
              "    except OSError:\n"
              "        print('REACH', t, 'unreachable')\n")


def _reach_targets(targets: list) -> list[str]:
    out = []
    for t in targets:
        host, _, port = str(t).rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536 or not re.fullmatch(r"[0-9A-Za-z.:\[\]_-]+", host):
            raise BenchError(f"reach: {t!r} is not HOST:PORT")
        out.append(f"{host.strip('[]')}:{port}")
    return out


def reach_probe(targets: list) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Each host:port of `targets` tried by a TCP connect from inside real cells built exactly as the bench, the daily
    bench, replay and the device shadow build theirs (isolate: its own network namespace; one cell per provider shape
    and the shadow's read-only one): ([(cell, target, "reachable" | "unreachable")], problems). `problems` is what
    kept a cell from being built, failing its self-test or answering: a probe with a problem proves nothing."""
    from . import deviceshadow
    targets, rows, problems = _reach_targets(targets), [], []
    shapes = [("bench-chat", {"provider": "chat", "model": "m", "effort": "high"}, {}),
              ("bench-claude", {"provider": "claude", "model": "m", "effort": "high"}, {}),
              ("shadow", {"provider": "chat", "model": "m", "effort": "high"}, {"shadow": True})]
    tmp = Path(tempfile.mkdtemp(prefix="reach-"))
    try:
        for name, arm, extra in shapes:
            root = tmp / name
            ws = root / "work"
            ws.mkdir(parents=True)
            for args in (["init", "-q", "-b", "main"], ["-c", "user.name=reach", "-c", "user.email=reach@localhost",
                                                         "commit", "-q", "--allow-empty", "-m", "start"]):
                subprocess.run(["git", "-C", str(ws), *args], capture_output=True, timeout=60, check=True)
            venv = cellenv.make(root / "venv", seed=())
            opts: dict = {}
            if extra.get("shadow"):
                (root / "ro").mkdir()
                opts = {"ro_binds": [str(root / "ro")], "deny_tools": str(deviceshadow.deny_tools(root / "deny"))}
            cell = None
            try:
                cell, bad = isolate(root, ws, arm, opts, [], [])
                if bad:
                    problems.append(f"{name}: isolation self-test: " + "; ".join(bad)[:300])
                    continue
                r = subprocess.run(cell.wrap([str(venv / "bin" / "python"), "-I", "-c", REACH_CODE, *targets]),
                                   cwd=str(ws), capture_output=True, text=True, timeout=60 + 4 * len(targets),
                                   env={**cellenv.env(venv, _clean_env()), **cell.sets()}, stdin=subprocess.DEVNULL)
                got = {t: v for line in r.stdout.splitlines() if line.startswith("REACH ")
                       for _, t, v in [line.split(" ", 2)]}
                if set(got) != set(targets):
                    problems.append(f"{name}: the probe did not answer (exit {r.returncode}): "
                                    f"{(r.stderr or r.stdout).strip()[-200:]}")
                    continue
                rows += [(name, t, got[t]) for t in targets]
            except cellns.Unavailable as e:
                problems.append(f"{name}: no cell: {e}"[:300])
            except (OSError, subprocess.SubprocessError, cellenv.Unavailable) as e:
                problems.append(f"{name}: {type(e).__name__}: {e}"[:300])
            finally:
                if cell is not None:
                    cell.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows, problems


def run_cell(bench_dir: Path, out: Path, task: dict, cell: dict, arm: dict, opts: dict, cfg: dict) -> dict:
    """Run one cell, grade it and return its results row (also written under <out>/cells)."""
    row = {**cell, **arm, "dry_run": bool(opts.get("dry_run"))}
    if opts.get("isolate", ISOLATE) and not cellns.backend():   # nothing to run it in: no work, no score
        return {**row, "outcome": "not_isolated", "passed": False,
                "why": ["not isolated: neither bwrap (bubblewrap) nor unshare (util-linux) can build a mount "
                        "namespace here, so the cell is not run, scored or ranked"]}
    keep = out / "cells" / f"{task['task']}-r{cell['rep']}-{cell['arm']}"
    hidden = hidden_paths(bench_dir.resolve(), out.resolve(), [task.get("repo"), opts.get("repo")], opts.get("state"))
    tmp = Path(tempfile.mkdtemp(prefix="cell-"))
    logins, cell = [], None
    try:
        ws, run = tmp / "work", tmp / "run"
        shutil.copytree(bench_dir / str(task["task"]) / "workspace", ws, symlinks=True)
        if problems := leaks(ws, task):
            return {**row, "outcome": "isolation_failed", "passed": False, "why": problems}
        start = bench._git(ws, "rev-parse", "HEAD").stdout.strip()
        try:   # the agent and all it starts run in the cell's own venv, the user site off
            cell_env = cellenv.env(cellenv.make(tmp / "venv", opts.get("cell_packages", cellenv.SEED)), _clean_env())
        except cellenv.Unavailable as e:
            return {**row, "outcome": "isolation_failed", "passed": False, "why": [str(e)[:500]]}
        spec = task["spec"]
        if opts.get("isolate", ISOLATE):   # the agent runs in a namespace that holds only the cell (cellns.py)
            repos = [r for r in (task.get("repo"), opts.get("repo")) if r]
            try:
                cell, problems = isolate(tmp, ws, arm, opts, hidden, repos, gone=[task["head"]])
            except cellns.Unavailable as e:
                return {**row, "outcome": "not_isolated", "passed": False, "why": [str(e)[:500]]}
            if problems:
                return {**row, "outcome": "error", "passed": False, "isolated": cell.backend,
                        "why": [f"isolation self-test: {x}"[:300] for x in problems]}
            roots = [Path(x).expanduser() for r in repos for x in (r, Path(r).resolve())]
            spec = bench.scrub_spec(spec, roots, [h for h in hidden if h not in roots], Path.home())
            row["isolated"] = cell.backend
            conf, own = dict(cell.conf), session_dirs(ws, cell.conf.get("CLAUDE_CONFIG_DIR"))
        else:   # No earlier transcripts within reach: the arm's CLI gets a config folder of the cell's own.
            conf, logins = scratch_home(arm["provider"], tmp, not opts.get("dry_run")) \
                if opts.get("scratch_home", True) else ({}, [])
            own = session_dirs(ws, conf.get("CLAUDE_CONFIG_DIR"))
        prov, wall, hung = launch(run, ws, prompt(spec, opts, grade_workers(cell_env)), arm, opts, cfg, hidden, cell_env, conf, own,
                                  cell)
        row["wall_s"] = wall
        if hung:   # the supervisor itself hung: its watchdog ends the agent; what it spent so far still counts
            row.update(outcome="runner_timeout", passed=False)
            try:
                row.update(cell_spend(prov.parse(run / "output.jsonl", run / "stderr.log"), arm,
                                      run / "output.jsonl"))
            except Exception:
                pass
            return row
        try:
            return _finish(row, bench_dir, task, ws, run, keep, start, prov, opts, hidden, own)
        except Exception as e:   # the agent ran and spent: record the cell, so a rerun skips it
            return {**row, "outcome": "error", "passed": False, "why": [f"{type(e).__name__}: {e}"[:500]]}
    finally:
        restore_logins(logins)
        if cell is not None:
            cell.close()
        shutil.rmtree(tmp, ignore_errors=True)


def agent_time(opts: dict) -> bool:
    """Whether the cell limit counts the agent's own time (default), not wall time (`agent_time` false)."""
    return bool(opts.get("agent_time", True))


def server_factor(opts: dict) -> float:
    return float(opts.get("server_factor") or serverhealth.FACTOR)


def history(opts: dict) -> "serverhealth.History":
    """The per-model record of healthy calls that defines each model's normal speed (`server_history`: a path)."""
    return serverhealth.History(opts.get("server_history"), int(opts.get("server_window") or serverhealth.WINDOW))


def launch(run: Path, ws: Path, prompt: str, arm: dict, opts: dict, cfg: dict, hidden: list[Path], cell_env: dict,
           env: dict | None = None, own: list[Path] | None = None, cell=None) -> tuple:
    """Run the arm's agent on `prompt` in `ws` under the runner supervisor (run dir `run`, new), reads of
    `hidden` denied except the cell's own session folders `own` (default: session_dirs(ws)); `env` adds to
    the agent's environment (PATH_PREPEND goes before its PATH); `cell` (cellns.Cell): the agent runs in its
    mount namespace, the supervisor and the chat bridge stay outside. Returns
    (provider, wall_s, hung): hung when the supervisor itself outlived its timeout."""
    from . import billing, chatbridge
    from .providers import get_provider
    run.mkdir()
    durable_write(run / "prompt.md", prompt)
    provider = "fake" if opts.get("dry_run") else arm["provider"]
    prices = (cfg.get("pricing") or {}).get(provider) or {}
    prov = get_provider(provider).use(arm["model"], prices, billing.declared(cfg, provider))
    inst = arm.get("harness_install") if provider in ("chat", "claude") else None
    if inst:   # a harness arm: its own agent on the bridge's OpenAI-compatible relay (harness_files)
        argv, penv = harness_files(run, arm, inst), {}
    else:
        argv, penv = prov.build(role="worker", model=arm["model"], effort=arm["effort"], cwd=str(ws),
                                budget_usd=opts["budget_usd"], read_only=False, schema=None, restrictions={},
                                denied_reads=denied_reads(hidden, own or session_dirs(ws)), web=False)
    if argv[:3] == [sys.executable, "-m", "ttp.providers.fake"]:   # the dry run's agent: no PYTHONPATH
        argv = [*_boot("ttp.providers.fake", cell), *argv[3:]]
    extra = dict(env or {})
    if pre := extra.pop("PATH_PREPEND", ""):
        extra["PATH"] = os.pathsep.join(p for p in (pre, penv.get("PATH") or cell_env.get("PATH", "")) if p)
    if cell is not None:   # nothing the agent can print names a hidden path (cell_agent_env)
        added, unset = cell_agent_env({**penv, **extra}, cell_env, run, cell, hidden)
        penv, extra = {}, added
        argv = _in_cell(argv, cell, unset)
    # A harness arm's agent is not the provider's CLI: the runner treats it as the chat provider's (its output is
    # not a Claude stream), whichever model the bridge calls.
    spec = {"argv": argv, "env": {**penv, **extra, "TTP_RUN_DIR": str(run)}, "cwd": str(ws),
            "provider": "chat" if inst else provider,
            "model": arm["model"], "effort": arm["effort"], "timeout_s": opts["timeout_s"],
            "stall_s": opts["stall_s"], "prices": prices, "default_budget_usd": opts["budget_usd"],
            "budget_usd": None if prov.caps_budget and not (inst and provider == "claude") else opts["budget_usd"],
            "nice": 0,
            **({"cell_net": {"bridge_sock": str(cell.sock("bridge"))}} if cell is not None else {}),
            **({"bridge": {"kind": "claude-cli", "model": arm["model"], "effort": arm["effort"],
                           "budget_usd": opts["budget_usd"]}} if inst and provider == "claude" else
               {"bridge": chatbridge.settings(cfg, "standard", arm["model"], arm["effort"])}
               if provider == "chat" else {}), **({"openai_env": True} if inst else {})}
    if agent_time(opts):   # the limit counts the agent's own time, not the server's wait (serverhealth)
        spec["agent_time"] = True
        spec["ceiling_s"] = serverhealth.ceiling_s(opts["timeout_s"], opts.get("ceiling_factor") or serverhealth.CEILING_FACTOR)
        if normal := history(opts).normal(arm["model"], int(opts.get("server_min_calls") or serverhealth.MIN_CALLS)):
            spec["server"] = {"normal_tps": normal["tps"], "factor": server_factor(opts)}
    durable_write(run / "run.json", json.dumps(spec, indent=1))
    began = time.time()
    proxy = cellnet.connect_proxy(cell.sock("proxy"), cell.proxy_domains) if cell is not None and cell.proxy_domains \
        else None
    try:
        with open(run / "runner.log", "wb") as log:
            subprocess.run([*_boot("ttp.runner"), str(run)], cwd=str(run), env=cell_env,
                           stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=(spec["ceiling_s"] if spec.get("agent_time") else opts["timeout_s"]) + 300)
    except subprocess.TimeoutExpired:
        return prov, round(time.time() - began, 1), True
    finally:
        if proxy is not None:
            proxy.stop()
    return prov, round(time.time() - began, 1), False


CHARS_PER_TOKEN = 4   # content chars per output token, on the high side: an estimate from it stays a floor
# A cut-off Claude row's cost_basis; rows priced before it (stream tokens only) are not comparable (spend_summary).
STREAM_BASIS = "stream tokens, no result line (a floor)"
CONTENT_BASIS = "estimate: stream tokens, output from content length (a floor)"
PROVIDER_BASIS = "estimate: the provider's own"


def stream_output_chars(path: Path) -> int:
    """The characters a Claude stream's assistant messages wrote (text, visible thinking, tool inputs),
    each block once (a streamed message repeats its blocks), subagents' included: what its output
    tokens at least cover. 0 when the file is missing or unreadable."""
    seen, total = set(), 0
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "assistant":
                    continue
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    body = block.get("text") if kind == "text" else block.get("thinking") if kind == "thinking" \
                        else json.dumps(block.get("input"), sort_keys=True) if kind == "tool_use" else None
                    if not isinstance(body, str) or not body:
                        continue
                    key = (str(msg.get("id") or ""), kind, hashlib.sha256(body.encode(errors="replace")).hexdigest())
                    if key not in seen:
                        seen.add(key)
                        total += len(body)
    except OSError:
        return 0
    return total


def cell_spend(u, arm: dict, output: Path | None = None) -> dict:
    """A cell's spend fields: cost_usd, cost_estimated and, for an estimate, cost_basis and output_tokens.
    A Claude run cut off before its result line (its limit, a kill) reports no cost, and its stream's
    per-message usage under-counts output (each message's usage is read as it starts): price its tokens
    at list price (localspend), output at the larger of the stream's count and its content length
    (`output`, the stream file) over CHARS_PER_TOKEN, estimated, so a cell that spent tokens never
    records 0 or a few hundred tokens. A run with a result line is priced as it reported."""
    tokens = u.input_tokens or u.output_tokens or u.cache_read_tokens or u.cache_write_tokens
    out = {"cost_usd": u.cost_usd, "cost_estimated": bool(u.estimated)}
    if u.estimated:   # every estimated row names its basis: one without was priced before (spend_summary)
        out["cost_basis"] = PROVIDER_BASIS
    if u.cost_usd or not tokens or arm.get("provider") != "claude":
        return out
    from . import localspend
    written = -(-stream_output_chars(output) // CHARS_PER_TOKEN) if output else 0
    output_tokens = max(int(u.output_tokens or 0), written)
    try:
        usd, _unknown = localspend.price(str(arm.get("model") or u.extra.get("model") or ""), {
            "input_tokens": u.input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": u.cache_read_tokens,
            "cache_creation_input_tokens": u.cache_write_tokens}, overrides=localspend.overrides())
    except Exception:
        return out
    out.update(cost_usd=round(float(usd), 6), cost_estimated=True,
               cost_basis=CONTENT_BASIS if output_tokens > (u.output_tokens or 0) else STREAM_BASIS)
    if output_tokens > (u.output_tokens or 0):
        out.update(output_tokens=output_tokens, stream_output_tokens=int(u.output_tokens or 0))
    return out


HARNESS_BASIS = "estimate: the bridge's token counts"
CLI_COST_SOURCE = "the claude CLI's total_cost_usd per call, summed by the bridge"


def harness_spend(prov, usage: dict) -> dict:
    """A harness arm's tokens and spend from the bridge's usage counts, at the chat provider's price row
    (or list price, localspend), always marked estimated."""
    fin, fout, fread = (int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"))
    # chatbridge.usage_of already counts input_tokens without the cached share: never subtract it twice
    out = {"input_tokens": fin, "output_tokens": fout, "cache_read_tokens": fread,
           "model_calls": int(usage.get("calls") or 0), "cost_estimated": True, "cost_basis": HARNESS_BASIS}
    row = prov.price() if hasattr(prov, "price") else None
    if "cost_usd" in usage:   # the claude CLI's own per-call cost (claudebridge): reported, not estimated
        out.update(cache_write_tokens=int(usage.get("cache_write_tokens") or 0), cost_estimated=False,
                   cost_usd=round(float(usage["cost_usd"] or 0.0), 6))
        out.pop("cost_basis")
        out["cost_source"] = CLI_COST_SOURCE
    elif row:
        pin, pcached, pout = row
        out["cost_usd"] = round((out["input_tokens"] * pin + fread * pcached + fout * pout) / 1e6, 6)
    else:
        from . import localspend
        try:
            usd, _ = localspend.price(str(getattr(prov, "model", "") or ""), {
                "input_tokens": out["input_tokens"], "output_tokens": fout, "cache_read_input_tokens": fread,
                "cache_creation_input_tokens": 0}, overrides=localspend.overrides())
            out["cost_usd"] = round(float(usd), 6)
        except Exception:
            out["cost_usd"] = 0.0
    return out


def cell_cost(u, arm: dict, output: Path | None = None) -> tuple[float, bool]:
    """(USD, estimated) for a cell's agent: cell_spend's cost_usd and cost_estimated."""
    got = cell_spend(u, arm, output)
    return got["cost_usd"], got["cost_estimated"]


def spend_summary(rows: list[dict]) -> dict:
    """The rows' spend for a report: cost_usd (their sum), estimated_cells and, when any is estimated,
    a note saying the sum is an estimate (a floor). Estimated rows without a cost_basis were priced
    from the stream's output count alone (before content-length pricing): the note says those and
    later rows are not comparable."""
    usd = round(sum(float(r.get("cost_usd") or 0) for r in rows), 4)
    est = [r for r in rows if r.get("cost_estimated")]
    got = {"cost_usd": usd, "estimated_cells": len(est)}
    if est:
        old = sum(not r.get("cost_basis") for r in est)
        got["cost_note"] = (f"estimate: {len(est)} of {len(rows)} cells' spend is estimated (no cost reported, "
                            f"e.g. a run cut off before its result line), so ${usd} is an estimate, likely a floor")
        if old and old < len(est):
            got["cost_note"] += (f"; {old} estimated cell(s) were priced from the stream's output count alone, "
                                 f"the rest from content length too: not comparable")
        elif old:
            got["cost_note"] += "; priced from the stream's output count alone, which under-counts output"
    return got


def _read_json(path: Path):
    # A file the candidate or its run wrote: None when missing, "invalid" when not a JSON object.
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(errors="replace"))
    except ValueError:
        return "invalid"
    return data if isinstance(data, dict) else "invalid"


def endpoint_fault(ended: dict) -> str:
    """The endpoint fault (the chat bridge's last_fault, e.g. "rate_limit" for an HTTP 429) an agent
    exited non-zero on, per its exit.json `ended`, or "": a cell the timeout stopped is the model's."""
    if not isinstance(ended, dict) or ended.get("rc") in (0, None) or ended.get("stopped"):
        return ""
    return str((ended.get("bridge") or {}).get("last_fault") or "")


def _finish(row: dict, bench_dir: Path, task: dict, ws: Path, run: Path, keep: Path, start: str, prov, opts: dict,
            hidden: list[Path], own: list[Path] | None = None) -> dict:
    """Audit and grade a cell whose agent has run, filling `row` as it goes (its cost first)."""
    u = prov.parse(run / "output.jsonl", run / "stderr.log")
    row.update(input_tokens=u.input_tokens, output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens,
               session_id=u.session_id)
    row.update(cell_spend(u, row, run / "output.jsonl"))
    transcript = (run / "output.jsonl").read_bytes() if (run / "output.jsonl").exists() else b""
    ended, handoff = _read_json(run / "exit.json"), _read_json(run / "result.json")
    ended = ended if isinstance(ended, dict) else {}
    if row.get("harness"):   # its own output format: the tokens are the bridge's counts, priced here
        row.update(harness_spend(prov, (ended.get("bridge") or {}).get("usage") or {}))
        if (run / "trajectory.json").exists():   # mini-swe-agent's messages: the transcript the audit reads
            transcript += b"\n" + (run / "trajectory.json").read_bytes()
    if isinstance(ended.get("ended"), (int, float)):   # the agent's own end: names this run's kept exit.json
        row["cell_ended"] = ended["ended"]
    row.update(exit={k: ended[k] for k in ("rc", "stopped", "refused") if ended.get(k) is not None},
               handoff=handoff if handoff == "invalid" else str((handoff or {}).get("status") or "none"),
               transcript_sha256=_sha(transcript))
    bench._git(ws, "add", "-A")
    diff = gitmemo.run(["git", "-C", str(ws), "diff", "--cached", start], capture_output=True, timeout=None).stdout
    row["diff_sha256"] = _sha(diff)
    if ended.get("stopped") in ("timeout", "stalled"):
        # The wall-time hang guard ended the agent (user msg #2898: the limit is only a hang guard). The
        # cell still grades what the agent committed, so it scores as the held-out tests say; the row
        # names the guard so a reader sees the cell was cut short.
        row["hang_guard"] = ended["stopped"]
    flags = audit(transcript.decode(errors="replace"), task, hidden,
                  secret_lines(bench_dir, task), diff.decode(errors="replace"), own or session_dirs(ws))
    # Graded even when flagged, so a reader sees both: a flagged cell still scores as a flag (passed False),
    # and `heldout_passed` keeps what the held-out tests said. A fresh venv: nothing the agent installed
    # into its own runs them. A quote-match flag is a note, not a fail: an isolated standard cell's line
    # matches cannot come from the answer (isolation denies the held-out files and the landed history),
    # so they never void the score; only hard evidence does.
    hard = [f for f in flags if not f.startswith("quote_matches: ")]
    if not task["tests"]:
        # A task with no held-out tests (the daily bench's, and the offline replay's questions): nothing to
        # run. The cell is "done" (the agent ran to its end) or "flagged"; the blind review is the grade.
        log_tail = "no held-out tests: graded by the blind review\n"
        row.update(passed=None, heldout_passed=None, flagged=flags, outcome="flagged" if hard else "done")
    else:
        venv = cellenv.make(run.parent / "grade-venv", opts.get("cell_packages", cellenv.SEED))
        held, log_tail = grade(ws, bench_dir, task, opts["test_cmd"], opts["test_timeout_s"],
                               cellenv.env(venv, _clean_env()))
        if hard:
            log_tail = "flagged by the cheat audit; the held-out tests " + ("passed" if held else "failed") \
                + " (not scored as a pass)\n" + log_tail
        passed = held and not hard
        row.update(passed=passed, heldout_passed=held, flagged=flags,
                   outcome="flagged" if hard else "pass" if passed else "fail")
    if row["outcome"] in ("fail", "done") and (fault := endpoint_fault(ended)):
        # the endpoint failed the agent (a 429, an outage), not the model: never scored, retried by the suite
        row.update(outcome="error", endpoint_fault=fault)
        if ((ended.get("bridge") or {}).get("busy") or {}).get("gave_up"):
            row["capacity_stop"] = True   # the bridge waited out the model's 429s as far as allowed
    server_status(row, ended, opts)
    keep.mkdir(parents=True, exist_ok=True)
    for name in ("output.jsonl", "exit.json", "result.json", "run.json", "runner.log", "stderr.log",
                 "trajectory.json", "opencode.json", "mini.yaml", "harness.sh"):
        if (run / name).exists():
            shutil.copyfile(run / name, keep / name)
    durable_write(keep / "diff.patch", diff.decode(errors="replace"))
    durable_write(keep / "tests.log", log_tail)
    return row


def server_status(row: dict, ended: dict, opts: dict) -> None:
    """Record what the server did to a cell (the call summary, the limit rule it ran under) and mark it
    `server` (not scored, never a fail, queued for a rerun) when it was cut by the limit, the ceiling or a
    capacity refusal while its calls ran slower than the model's normal speed (serverhealth.classify). A
    cell that passed stays a pass. The healthy cells' calls define each model's normal speed."""
    bridge = ended.get("bridge") or {}
    srv = bridge.get("server") or {}
    row["limit_rule"] = (ended.get("limits") or {}).get("rule") or "wall_time"
    if ended.get("limits"):
        row["limits"] = ended["limits"]
    if srv:
        row["server"] = {k: v for k, v in srv.items() if k != "per_call"}
    hist, factor = history(opts), server_factor(opts)
    normal = hist.normal(row.get("model", ""), int(opts.get("server_min_calls") or serverhealth.MIN_CALLS))
    if normal:
        row["server_normal"] = normal
    if row.get("outcome") in ("fail", "done", "error"):
        why = serverhealth.classify(ended.get("stopped"), bridge, normal, factor)
        if why:
            serverhealth.mark(row, why)
            return
    if srv.get("per_call") and not serverhealth.slow(srv["per_call"], normal, factor):
        hist.add(row.get("model", ""), srv["per_call"])


def criteria(spec: str) -> str:
    """The spec's acceptance section (from a line starting 'Acceptance' to the next 'Word:' heading),
    else the whole spec: what the blind reviewer scores each arm's diff against. Items that need the
    live system end in NOT_CHECKABLE (mark_live): the reviewer does not score them."""
    lines = spec.splitlines()
    start = next((i for i, x in enumerate(lines) if re.match(r"(?i)\s*(#+\s*)?acceptance\b", x)), None)
    if start is None:
        return mark_live(spec.strip())
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"\s*(#+\s*)?[A-Z][\w ()#,'-]{0,40}:(\s|$)", lines[i])), len(lines))
    return mark_live("\n".join(lines[start:end]).strip())


def blind_cells(out: Path, tasks: dict, arms: dict, cells: list[dict]) -> dict:
    """The blind review's input from <out>/results.jsonl: one cell per task and repeat whose arms all
    have a row (a repeat's task id gets '-r<rep>' when the plan has several). Each arm carries its diff
    and only what names the arm, so prep masks those tokens; run stats stay in results.jsonl."""
    rows = {}
    for line in (out / "results.jsonl").read_text().splitlines() if (out / "results.jsonl").exists() else []:
        try:
            r = json.loads(line)
            rows[(r["task"], r["rep"], r["arm"])] = r
        except (ValueError, KeyError, TypeError):
            continue
    rows = {k: r for k, r in rows.items() if r.get("outcome") != serverhealth.OUTCOME}   # not scored until rerun
    reps = sorted({c["rep"] for c in cells})
    got = []
    for tid, task in tasks.items():
        for rep in reps:
            if any((tid, rep, a) not in rows for a in arms):
                continue
            side = {}
            for a in sorted(arms):
                r, keep = rows[(tid, rep, a)], out / "cells" / f"{tid}-r{rep}-{a}"
                diff = (keep / "diff.patch").read_text(errors="replace") if (keep / "diff.patch").exists() else ""
                side[a] = {"diff": diff, **{k: arms[a].get(k, "") for k in ("provider", "model", "effort")},
                           "run_id": keep.name, "session_id": r.get("session_id") or "",
                           "transcript": str(keep / "output.jsonl")}
            got.append({"task": str(tid) if len(reps) == 1 else f"{tid}-r{rep}",
                        "criteria": criteria(str(task.get("spec") or "")), "arms": side})
    return {"cells": got}


def resolve_harness(arm: dict, opts: dict) -> dict:
    """A harness arm with its install resolved: the version (recorded in plan.json) and how effort maps."""
    kind = arm.get("harness")
    if not kind or opts.get("dry_run"):
        return arm
    inst = harness_ready(kind, arm.get("harness_version", ""), opts.get("harness_dir"))
    return {**arm, "harness_version": inst["version"], "harness_install": inst, "effort_mapping": CLAUDE_EFFORT_NOTE if arm.get("provider") == "claude" else EFFORT_NOTE[kind]}


def run(bench_dir: Path, out: Path, arms: dict, reps: int, seed: int, opts: dict, cfg: dict,
        limit: int = 0, only: list[int] | None = None) -> list[dict]:
    """Run every cell of the plan not yet in <out>/results.jsonl; returns the rows this call added.
    `only`: just these bench tasks."""
    if not arms:
        raise BenchError("a run needs at least one arm (--arm name=provider:model:effort)")
    if bad := [a for a in arms if not re.fullmatch(r"[A-Za-z0-9_.-]+", a)]:
        raise BenchError(f"arm names take letters, digits, '.', '_' and '-' only: {bad}")
    for name, arm in arms.items():
        check_harness(name, arm)
    man = verified(bench_dir)
    arms = {name: resolve_harness(arm, opts) for name, arm in arms.items()}
    # The source repository the bench was cut from (recorded by `ttp bench prep`) goes with each task.
    picked = [t for t in man["tasks"] if not only or t["task"] in only]
    if only and (missing := sorted(set(only) - {t["task"] for t in picked})):
        raise BenchError(f"tasks not in this bench: {missing}")
    tasks = {t["task"]: {**t, "repo": man.get("repo")} for t in picked[:limit or None]}
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
            if not r.get("endpoint_fault"):   # a cell the endpoint failed runs again
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
    durable_write(out / "cells.json", json.dumps(blind_cells(out, tasks, arms, plan["cells"]), indent=1) + "\n")
    return added
