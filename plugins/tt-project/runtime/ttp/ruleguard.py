# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Rule checks built from a project's own settings, asked before every tool call of every model.

Each rule reads structured settings only, never charter wording, and ships empty: it acts once a
project names what it protects. The worker hook (hook.pre_tool_use, so Claude Code, Codex and Cursor
alike) and the GLM shell (glmenv.refusal) ask it. RULES, with the setting each reads:
- protected_paths (guards.protected_paths): no write under a listed path, here or in text ssh runs
  on another host. pathguard.py enforces it beside the credential folders it always guards.
- slurm_nodes (guards.slurm): srun, sbatch or salloc naming nodes (-w, --nodelist) that squeue shows
  holding another user's job, or sinfo shows drained, down or reserved; --jobid or --dependency onto
  another user's job; scancel of another user's job (by id or -u); and scontrol update of a node's
  state are refused, also inside ssh to a login host (the same ssh runs the query there). -u $USER
  (or ${USER}, $LOGNAME, $(whoami)) is the caller; a job id squeue no longer knows has no live
  holder. Any other query that fails or times out refuses: the rule never guesses. Set guards.slurm to true on a project that
  uses Slurm; guards.slurm_queue_pinned true also lets an sbatch without --immediate name busy or
  drained nodes, since it only queues. Calls inside wrapper scripts are not seen.
- landing_push (delivery.push_branch, delivery.fast_forward_also, delivery.mirror_remotes and
  delivery.channel_tags): a raw `git push` to one of them is refused; `ttp push` is the route, which
  pushes only what the push queue approved.
- user_undo (the shared machine-change ledger): a command that replays the undo of an entry recorded
  as user-ordered (`ttp machines change add ... --user-msg <id>`) is refused, naming the entries.
guards.off, a list of rule names, turns rules off for one project; only the user's own word sets it
(coordinator.NEEDS_USER). Each refusal is one line naming its rule and setting, and is counted in the
run folder (FIRES) by rule and model; `ttp stats --guards` sums them and `ttp guards` lists the rules.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

RULES = ("protected_paths", "slurm_nodes", "landing_push", "user_undo")
OFF_KEY = "guards.off"
SLURM_KEY = "guards.slurm"
FIRES = "ruleguard.jsonl"
SLURM_PROGS = {"srun", "sbatch", "salloc", "scancel", "scontrol"}
SCANCEL_VALUE_OPTS = {"-A", "-M", "-n", "-p", "-q", "-R", "-s", "-t", "-u", "-w"}
QUERY_TIMEOUT_S = 15
CALLER = {"$USER", "${USER}", "$LOGNAME", "${LOGNAME}", "$(whoami)", "`whoami`", "$(id -un)", "`id -un`"}   # -u forms naming the caller
CALLER_SUBST = re.compile(r"(?:-u\s*|--user[=\s]+)\$\((?:whoami|id -un)\)")   # unquoted: split off as "$"
_ANY_SUBST = re.compile(r"(?:-u\s*|--user[=\s]+)\$\([^)]*\)")
HELD_STATES = ("drain", "drng", "down", "resv", "reserved", "maint")
PUSH_VALUE_OPTS = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
UNDO_MIN_CHARS = 8


def off(cfg: dict) -> set[str]:
    v = (cfg.get("guards") or {}).get("off") if isinstance(cfg.get("guards"), dict) else None
    return {str(x) for x in v} if isinstance(v, list) else set()


def on(rule: str, cfg: dict) -> bool:
    return rule not in off(cfg)


def fire(rule: str, why: str) -> None:
    """Count one refusal of `rule` in the run folder (its run row names the model); never raises."""
    if not (run := os.environ.get("TTP_RUN_DIR")):
        return
    rec = {"ts": round(time.time(), 3), "rule": rule, "why": why[:300]}
    try:
        with open(Path(run) / FIRES, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def fires(run_dir: Path) -> list[dict]:
    try:
        lines = (Path(run_dir) / FIRES).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("rule") in RULES:
            out.append(rec)
    return out


def counts(p, since: float, until: float) -> dict:
    """{rule: {model: fires}} over the runs started in [since, until)."""
    out: dict = {r: {} for r in RULES}
    rows = p.db.q("SELECT id, model, dir FROM runs WHERE started>=? AND started<?", (since, until))
    for r in rows:
        for rec in fires(Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])):
            m = r["model"] or "unknown"
            out[rec["rule"]][m] = out[rec["rule"]].get(m, 0) + 1
    return out


def counts_text(c: dict) -> str:
    return "\n".join(f"{rule}: " + (", ".join(f"{m} {n}" for m, n in sorted(v.items())) or "0") for rule, v in c.items())


def bash_refusal(command: str, segments: list[list[str]], cfg: dict, cwd: str = "", env=None) -> str | None:
    """Why the shell `command` (its simple commands `segments`) breaks one of the rules, else None;
    the refusal is counted (fire)."""
    env = os.environ if env is None else env
    for rule, check in (("slurm_nodes", _slurm), ("landing_push", _push), ("user_undo", _undo)):
        if not on(rule, cfg):
            continue
        try:
            why = check(command, segments, cfg, cwd or os.getcwd(), env)
        except Exception as e:
            why = f"{rule}: the check could not run ({type(e).__name__}), so it refuses" if rule == "slurm_nodes" else None
        if why:
            fire(rule, why)
            return why
    return None


# -- slurm_nodes ---------------------------------------------------------------------------------------

def _opt(args: list[str], short: str, long: str) -> list[str]:
    out = []
    for i, a in enumerate(args):
        if a in (short, long) and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith(long + "="):
            out.append(a.split("=", 1)[1])
        elif short and a.startswith(short) and len(a) > len(short) and not a.startswith("--"):
            out.append(a[len(short):])
    return out


def _slurm_calls(segments: list[list[str]]):
    """(ssh prefix or [], the slurm call's words) for each SLURM_PROGS call, also inside ssh's command."""
    from . import hook, pathguard
    for s in segments:
        name = os.path.basename(s[0]) if s else ""
        if name in SLURM_PROGS:
            yield [], s
        elif name == "ssh" and (remote := pathguard._ssh_remote(s[1:])):
            prefix = s[:len(s) - len(remote)]
            for r in hook._segments(" ".join(remote)):
                if r and os.path.basename(r[0]) in SLURM_PROGS:
                    yield prefix, r


def _query(prefix: list[str], script: str, env) -> str:
    argv = [*prefix, script] if prefix else ["sh", "-c", script]
    got = subprocess.run(argv, capture_output=True, text=True, timeout=QUERY_TIMEOUT_S, env=dict(env))
    if got.returncode:
        raise RuntimeError("query failed")
    return got.stdout


def _scancel_callers(command) -> bool | None:
    """Whether every unquoted `-u $(...)` inside a scancel's own text (a command's text ends at
    ; && || | or a line break) is the caller's own name; None when there is none. Read over the whole
    command: the shell parse keeps only a call's words before its first substitution. A substitution of
    another command (`squeue -u $(whoami)`) is no scancel's."""
    found = [bool(CALLER_SUBST.fullmatch(m.group(0)))
             for text in re.split(r"\s*(?:;|&&|\|\||\||\n)\s*", str(command)) if re.search(r"\bscancel\b", text)
             for m in _ANY_SUBST.finditer(text)]
    return all(found) if found else None


def _slurm(command, segments, cfg, cwd, env) -> str | None:
    g = cfg.get("guards") or {}
    if not g.get("slurm"):
        return None
    own = _scancel_callers(command)
    for prefix, words in _slurm_calls(segments):
        prog, args = os.path.basename(words[0]), words[1:]
        if prog == "scontrol":
            low = [a.lower() for a in args]
            if low[:1] == ["update"] and any(a.startswith("nodename=") for a in low) and any(a.startswith("state=") for a in low):
                return ("slurm_nodes (guards.slurm): scontrol update of a node's state changes it for every user; "
                        "leave node states to the cluster's admins")
            continue
        queued = prog == "sbatch" and not any(a == "-I" or a.startswith("--immediate") for a in args)
        nodes = "" if queued and g.get("slurm_queue_pinned") else ",".join(_opt(args, "-w", "--nodelist"))
        if prog == "scancel":
            users = [u for u in _opt(args, "-u", "--user")
                     if u not in CALLER and not (u == "$" and own)] + ["$"] * (own is False)
            jobs = [a for i, a in enumerate(args) if re.match(r"\d", a) and (i == 0 or args[i - 1] not in SCANCEL_VALUE_OPTS)]
            nodes = ""
        else:
            users = []
            jobs = _opt(args, "", "--jobid") + [j for d in _opt(args, "-d", "--dependency") for j in re.findall(r":(\d+)", d)]
        if not nodes and not jobs and not users:
            continue
        q = shlex.quote
        script = "id -un && echo @@"
        if nodes:
            script += f" && squeue -h -w {q(nodes)} -o '%u %i' && echo @@ && sinfo -h -N -n {q(nodes)} -o '%N %T'"
        for j in jobs:
            # a job that ended is no longer listed: squeue -j exits 1 with 'Invalid job id', no live holder
            script += (f" && echo @@ && {{ squeue -h -j {q(j)} -o '%u %i' 2>/dev/null"
                       f" || squeue -h -j {q(j)} 2>&1 | grep -q 'Invalid job id'; }}")
        try:
            parts = _query(prefix, script, env).split("@@")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return (f"slurm_nodes (guards.slurm): squeue or sinfo could not tell who holds {nodes or ', '.join(jobs or users)}, "
                    "so the call is refused; name free nodes the queue can confirm, or let the scheduler pick")
        me, rest = parts[0].strip(), parts[1:]
        node_jobs, states, job_rows = (rest[0], rest[1], rest[2:]) if nodes else ("", "", rest)
        rows = [ln.split() for part in (node_jobs, *job_rows) for ln in part.splitlines()]
        others = sorted({r[1] for r in rows if len(r) >= 2 and r[0] != me})
        held = sorted({r[0] for r in (ln.split() for ln in states.splitlines())
                       if len(r) >= 2 and any(h in r[1].lower() for h in HELD_STATES)})
        strangers = sorted({u for u in users if u != me})
        what = [f"jobs {', '.join(others)} of another user"] * bool(others) + \
               [f"nodes {', '.join(held)} drained, down or reserved"] * bool(held) + \
               [f"the jobs of user {', '.join(strangers)}"] * bool(strangers)
        if what:
            return (f"slurm_nodes (guards.slurm): {prog} onto {'; '.join(what)}; never take nodes or jobs "
                    "others hold: ask the scheduler for free nodes")
    return None


# -- landing_push --------------------------------------------------------------------------------------

def targets(cfg: dict) -> tuple[set[str], set[str]]:
    """(landing branch names, channel tags) a raw git push may not reach."""
    from . import channeltag
    from .push import fast_forward_list, mirror_list
    d = cfg.get("delivery") or {}
    pb = str(d.get("push_branch") or "").strip()
    branches = {pb, pb.partition("/")[2]} if pb else set()
    branches |= set(fast_forward_list(d.get("fast_forward_also")))
    branches |= {m["branch"] for m in mirror_list(d.get("mirror_remotes")) if m.get("branch")}
    return {b for b in branches if b}, set(channeltag.channel_tags(cfg))


def _push(command, segments, cfg, cwd, env) -> str | None:
    from . import gitguard
    branches, tags = targets(cfg)
    if not branches and not tags:
        return None
    for s in segments:
        if os.path.basename(s[0]) != "git":
            continue
        glob_, sub, rest = gitguard.split(s[1:])
        if sub != "push":
            continue
        pos, flags, i = [], [], 0
        while i < len(rest):
            a = rest[i]
            if a in PUSH_VALUE_OPTS:
                i += 1
            elif a.startswith("-"):
                flags.append(a)
            else:
                pos.append(a)
            i += 1
        dests = [r.lstrip("+").rpartition(":")[2] for r in pos[1:]]
        if not pos[1:]:
            dests = [_current_branch(glob_, cwd, env)]
        hit = [d for d in dests if d.removeprefix("refs/heads/") in branches or d.removeprefix("refs/tags/") in tags]
        if "--all" in flags or "--mirror" in flags:
            hit += sorted(branches)
        if "--tags" in flags and tags:
            hit += sorted(tags)
        if hit:
            return (f"landing_push (delivery.push_branch and its landing branches and channel tags): a raw git push "
                    f"to {hit[0]} skips the push queue's review gate; land it with `ttp push`")
    return None


def _current_branch(glob_: list[str], cwd: str, env) -> str:
    try:
        got = subprocess.run(["git", *glob_, "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True,
                             text=True, timeout=10, env=dict(env))
        return got.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# -- user_undo -----------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"']", "", text)).strip()


def _undo(command, segments, cfg, cwd, env) -> str | None:
    from . import machine_ledger
    cmd = _norm(command)
    ids = [str(c.get("id")) for c in machine_ledger.changes(open_only=False)
           if c.get("user_msg") and len(u := _norm(str(c.get("undo") or ""))) >= UNDO_MIN_CHARS and u in cmd]
    if ids:
        return (f"user_undo (the machine-change ledger): this replays the undo of user-ordered change "
                f"{', '.join(ids)}, which reverts what the user ordered; only the user's word undoes it")
    return None


def listing(cfg: dict) -> list[str]:
    """One line per rule: on or off, and what it reads (`ttp guards`)."""
    from . import machine_ledger, pathguard
    dead = off(cfg)
    extra = [x for x in pathguard.paths(cfg) if x not in pathguard.DEFAULT_PATHS]
    branches, tags = targets(cfg)
    try:
        ordered = [str(c.get("id")) for c in machine_ledger.changes(open_only=False) if c.get("user_msg")]
    except Exception:
        ordered = []
    src = {"protected_paths": f"guards.protected_paths: {', '.join(extra) or 'none'} (credential folders always)",
           "slurm_nodes": f"guards.slurm: {'on' if (cfg.get('guards') or {}).get('slurm') else 'unset'}"
                          + ("; slurm_queue_pinned: on" if (cfg.get('guards') or {}).get('slurm_queue_pinned') else ""),
           "landing_push": "delivery.push_branch, fast_forward_also, mirror_remotes, channel_tags: "
                           + (", ".join(sorted(branches | tags)) or "none"),
           "user_undo": f"user-ordered ledger entries: {', '.join(ordered) or 'none'}"}
    return [f"{r}: {'off (guards.off)' if r in dead else 'on'}; {src[r]}" for r in RULES]
