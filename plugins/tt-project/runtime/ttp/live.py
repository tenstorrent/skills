# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp doctor --live <provider>`: one measured parity row for an agent CLI. Opt-in, and it spends
money (a few cents to a few tens of cents), so no test runs it against a real agent.

It makes a scratch project in a scratch git repository and starts its runs the way the daemon does
(Daemon.start_run, the detached runner), then reads them back with the provider's own parser:

- a tiny worker run: it runs one shell command, writes one file in its worktree and tries to write
  one in a sibling dir outside its writable roots, and writes result.json. Right after it starts, steer.md gets an update for it.
- a resume of that run's session, asked for a code word only the first run was told;
- a read-only coordinator turn with the coordinator's structured-output schema.

The scratch folder lives under the tt-project home, not the system temp dir: sandboxes such as
Codex's workspace-write let a worker write /tmp and $TMPDIR, so a probe there would prove nothing.

Each check is pass, fail or unsupported (the adapter does not use the feature at all; the note
says why). A provider that is not installed or is logged out is "not available", with the reason.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .project import durable_write, write_json
from .providers.base import Provider, cli_output, status_check

CHECKS = ("launch", "login", "session", "usage", "structured", "fence", "resume", "steer", "meter")
PASS, FAIL, UNSUPPORTED = "pass", "fail", "unsupported"
WORKER_USD, RESUME_USD, COORD_USD = 0.25, 0.15, 0.15   # per-run caps; a row stays under $0.55
RUN_TIMEOUT_S = 300
POLL_S = 1.0


def cli_version(prov: Provider) -> str:
    b = prov.binary()
    out = cli_output(b, "--version").strip() if b else ""
    return out.splitlines()[-1].strip()[:80] if out else ""


def unavailable(prov: Provider) -> str:
    """Why `prov` cannot be measured here ("" when it can): not installed, or logged out."""
    if not prov.available():
        return "not installed"
    if prov.login_check() is False:
        return f"logged out ({prov.login_hint})"
    return ""


def hook_used(argv: list) -> bool:
    """Whether a run's argv routes tool results through the harness hook (Codex gets it per run,
    only when hooks are on; see providers/codex.py)."""
    return any("ttp.hook PostToolUse" in str(a) for a in argv)


def judge(prov: Provider, ev: dict) -> dict[str, tuple[str, str]]:
    """Each check's (status, note) from what the runs left (`ev`, gathered by measure()). Pure, so
    every path is tested without a real agent."""
    out: dict[str, tuple[str, str]] = {}
    w = ev.get("worker") or {}
    launched = bool(w.get("exit", {}).get("launched", True)) and w.get("exit", {}).get("rc") == 0 \
        and not w.get("error")
    out["launch"] = (PASS, "") if launched else (FAIL, (w.get("error") or f"exit {w.get('exit', {}).get('rc')}")[:200])
    login = ev.get("login")
    out["login"] = (PASS, "") if login is True else \
        (UNSUPPORTED, "no status command: a run finds out") if login is None else (FAIL, "logged out")
    sid, want = w.get("session_id") or "", w.get("assigned_session") or ""
    out["session"] = (FAIL, "the run reported no session id") if not sid else \
        (FAIL, f"reported {sid}, assigned {want}") if want and sid != want else (PASS, "assigned up front" if want else "")
    if w.get("cost_usd", 0) > 0 or w.get("tokens", 0) > 0:
        out["usage"] = (PASS, ("estimated from tokens" if w.get("estimated") else "reported")
                        + f": ${w.get('cost_usd', 0):.4f}, {w.get('tokens', 0)} tokens")
    else:
        out["usage"] = (FAIL, "no cost or tokens parsed")
    c = ev.get("coordinator") or {}
    if c.get("skipped"):
        out["structured"] = (FAIL, c["skipped"])
    elif isinstance((c.get("structured") or {}).get("actions"), list):
        out["structured"] = (PASS, "")
    elif isinstance((c.get("from_text") or {}).get("actions"), list):
        out["structured"] = (PASS, "from the final text, not a schema-checked field")
    else:
        out["structured"] = (FAIL, (c.get("error") or "no actions object in the turn's output")[:200])
    gap = prov.write_fence()
    if gap:
        out["fence"] = (UNSUPPORTED, gap)
    elif not launched:
        out["fence"] = (FAIL, "the worker run did not complete")
    elif ev.get("probe_written"):
        out["fence"] = (FAIL, "the worker wrote outside its writable roots")
    elif not w.get("result"):
        out["fence"] = (FAIL, "the worker could not write result.json in its run dir")
    elif ev.get("inside_written") is False:
        out["fence"] = (FAIL, "the worker could not write a file in its worktree")
    else:
        out["fence"] = (PASS, str((w.get("result") or {}).get("fence") or "")[:120])
    r = ev.get("resume") or {}
    if not prov.resume_args("00000000-0000-4000-8000-000000000000"):
        out["resume"] = (UNSUPPORTED, "the adapter has no resume argv for this CLI")
    elif r.get("skipped"):
        out["resume"] = (FAIL, r["skipped"])
    elif r.get("recalled"):
        out["resume"] = (PASS, "")
    else:
        out["resume"] = (FAIL, (r.get("error") or "the resumed run did not recall the code word")[:200])
    if not (getattr(prov, "steer_hook", False) or ev.get("hook_used")):
        out["steer"] = (UNSUPPORTED, "no hook: the worker reads steer.md between steps")
    elif ev.get("steer_delivered"):
        out["steer"] = (PASS, "" if w.get("steer_word_seen") else "handed over; the agent did not echo it")
    else:
        out["steer"] = (FAIL, "the hook did not hand the update over")
    wins = ev.get("windows") or []
    if wins:
        out["meter"] = (PASS, ", ".join(sorted({str(x) for x in wins})))
    elif type(prov).meter is not Provider.meter:
        out["meter"] = (FAIL, "the meter returned no plan windows")
    else:
        out["meter"] = (UNSUPPORTED, "the run reported no plan windows and the adapter has no meter")
    return {k: out[k] for k in CHECKS}


def _wait(run_dir: Path, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s + 60   # the runner enforces timeout_s itself
    while time.time() < deadline:
        try:
            return json.loads((run_dir / "exit.json").read_text())
        except (OSError, ValueError):
            time.sleep(POLL_S)
    (run_dir / "STOP").touch()
    return {"rc": None, "launched": True, "error": "no exit.json in time"}


def _finish(d, prov: Provider, provider: str, run_id: int, timeout_s: float) -> tuple[dict, object]:
    run_dir = d.p.runs / str(run_id)
    exit_info = _wait(run_dir, timeout_s)
    r = d.p.db.q("SELECT * FROM runs WHERE id=?", (run_id,))[0]
    usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
    d._priced(r, usage)
    return exit_info, usage


def measure(provider: str, *, tier: str = "light", timeout_s: float = RUN_TIMEOUT_S,
            base: Path | None = None, max_usd: float | None = None) -> dict:
    """Run the scratch project's three runs on `provider` and return what judge() reads. `max_usd` caps
    the three together: each run's cap is the rest of it, and a run with under a cent left is skipped."""
    from . import project
    from .cli import bootstrap
    from .coordinator import ACTIONS_SCHEMA
    from .daemon import Daemon
    from .hook import OFFSET_FILE, STEER_FILE
    from .prompts import RUN_DIR_MARK
    from .providers import get_provider
    from .providers.base import last_json_object
    prov = get_provider(provider)
    ev: dict = {"login": prov.login_check(), "spend_usd": 0.0}

    def cap(usd: float) -> float:
        return usd if max_usd is None else round(min(usd, max_usd - ev["spend_usd"]), 4)
    home = Path(base or project.HOME_DIR)
    home.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="live-check-", dir=home))
    try:
        repo, outside = tmp / "repo", tmp / "outside"
        outside.mkdir()
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=ttp", "-c", "user.email=ttp@localhost",
                                                     "commit", "-q", "--allow-empty", "-m", "scratch"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        p = bootstrap(repo, "live-check", "Measure one provider.", provider)
        d = Daemon(p.base)
        tid = p.db.add_task("live check", "conformance", kind="code", tier=tier, origin="user")
        word, steer_word, probe = f"W{uuid.uuid4().hex[:6]}", f"S{uuid.uuid4().hex[:6]}", outside / "probe.txt"
        inside = repo / "fence-inside.txt"
        prompt = (
            "This is a harness conformance check. Do exactly these steps and nothing else.\n"
            f"1. Remember the code word {word}; you will be asked for it later.\n"
            f"2. Run this shell command once: echo live-check > {inside}\n"
            f"3. Run this shell command once: echo probe > {probe}\n"
            "   It may be refused; that is expected. Note its outcome in a few words.\n"
            "4. If an update for your task arrived after a tool call, note the code word it gives.\n"
            f"5. Write this JSON to {RUN_DIR_MARK}/result.json, filled in: "
            '{"status": "done", "summary": "live check", "fence": "<outcome of step 3>", '
            '"steer_word": "<code word from the update, or empty>"}\n'
            "Then reply with one short line.")
        rid = d.start_run("worker", prompt, provider, tier, str(repo), task=p.db.task(tid),
                          budget_usd=cap(WORKER_USD), timeout_s=timeout_s)
        run_dir = p.runs / str(rid)
        durable_write(run_dir / STEER_FILE, f"Update for your task: the update's code word is {steer_word}.\n")
        assigned = (p.db.q("SELECT session_id FROM runs WHERE id=?", (rid,))[0] or {}).get("session_id") or ""
        exit_info, u = _finish(d, prov, provider, rid, timeout_s)
        try:
            result = json.loads((run_dir / "result.json").read_text())
        except (OSError, ValueError):
            result = None
        try:
            delivered = int((run_dir / OFFSET_FILE).read_text()) >= (run_dir / STEER_FILE).stat().st_size
        except (OSError, ValueError):
            delivered = False
        if u.auth_failed:
            ev["login"] = False
        ev["spend_usd"] += u.cost_usd or 0
        ev["worker"] = {"exit": exit_info, "error": u.error or exit_info.get("error") or "",
                        "session_id": u.session_id, "assigned_session": assigned, "cost_usd": u.cost_usd or 0,
                        "estimated": u.estimated, "tokens": (u.input_tokens or 0) + (u.output_tokens or 0)
                        + (u.cache_read_tokens or 0), "result": result,
                        "steer_word_seen": steer_word in json.dumps(result or {})}
        ev["probe_written"] = probe.exists()
        ev["inside_written"] = inside.exists()
        ev["steer_delivered"] = delivered
        ev["windows"] = [f"{w.get('window')}" for w in (u.extra.get("windows") or [])]
        if not ev["windows"] and type(prov).meter is not Provider.meter:
            ev["windows"] = [w.window for w in prov.meter()]
        spec = json.loads((run_dir / "run.json").read_text())
        env = spec.get("env") or {}
        ev["hook_used"] = hook_used(spec.get("argv") or [])
        if not prov.resume_args("00000000-0000-4000-8000-000000000000"):
            ev["resume"] = {}
        elif not u.session_id:
            ev["resume"] = {"skipped": "the worker run reported no session to resume"}
        elif not prov.session_saved(u.session_id, str(repo), env):
            ev["resume"] = {"skipped": "the worker's session was not found on disk"}
        elif cap(RESUME_USD) < 0.01:
            ev["resume"] = {"skipped": f"the ${max_usd:.2f} cap was spent before the resume"}
        else:
            rid2 = d.start_run("worker", "What code word did I ask you to remember? Reply with only that word.",
                               provider, tier, str(repo), task=p.db.task(tid), budget_usd=cap(RESUME_USD),
                               timeout_s=timeout_s, resume=u.session_id, handoff_guard=False)
            exit2, u2 = _finish(d, prov, provider, rid2, timeout_s)
            ev["spend_usd"] += u2.cost_usd or 0
            ev["resume"] = {"recalled": word in (u2.final_text or ""),
                            "error": u2.error or exit2.get("error") or ""}
        if cap(COORD_USD) < 0.01:
            ev["coordinator"] = {"skipped": f"the ${max_usd:.2f} cap was spent before the coordinator turn"}
            return ev
        crid = d.start_run("coordinator", 'Reply with exactly this JSON and nothing else: {"actions": [{"type": "noop"}]}',
                           provider, tier, str(p.base), read_only=True, schema=ACTIONS_SCHEMA,
                           system="You are a test coordinator. You only return the requested JSON.",
                           budget_usd=cap(COORD_USD), timeout_s=timeout_s)
        exit3, u3 = _finish(d, prov, provider, crid, timeout_s)
        ev["spend_usd"] += u3.cost_usd or 0
        ev["coordinator"] = {"structured": u3.structured if isinstance(u3.structured, dict) else None,
                             "from_text": last_json_object(u3.final_text or ""),
                             "error": u3.error or exit3.get("error") or ""}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ev


def row(provider: str, **kw) -> dict:
    """The provider's parity row: {provider, version, date, available, reason, spend_usd, checks}."""
    from .providers import get_provider
    prov = get_provider(provider)
    out = {"provider": provider, "version": cli_version(prov), "date": time.strftime("%Y-%m-%d"),
           "available": False, "reason": unavailable(prov), "spend_usd": 0.0, "checks": {}}
    if out["reason"]:
        return out
    ev = measure(provider, **kw)
    if ev.get("login") is False:
        out["reason"] = f"logged out ({prov.login_hint})"
        out["spend_usd"] = round(ev.get("spend_usd", 0.0), 4)
        return out
    out.update(available=True, spend_usd=round(ev.get("spend_usd", 0.0), 4),
               checks={k: {"status": s, "note": n} for k, (s, n) in judge(prov, ev).items()})
    return out


def format_row(r: dict) -> str:
    head = f"{r['provider']} {r['version'] or '(version unknown)'} · {r['date']}"
    if not r["available"]:
        return f"{head} · not available: {r['reason']}"
    lines = [head + " · " + " · ".join(f"{k} {v['status']}" for k, v in r["checks"].items())
             + f" · spent ${r['spend_usd']:.2f}"]
    lines += [f"  {k}: {v['note']}" for k, v in r["checks"].items() if v["note"]]
    return "\n".join(lines)


# The CLI-upgrade watch (Daemon.watch_cli_versions). A flag probe (Provider.supports) cannot see a CLI
# that changed what it does, so once a day at most, a provider whose CLI version changed gets one live
# row. Its state is host-wide under the tt-project home, so one row per upgrade serves every project on
# the host: versions.json keeps each provider's {version, checked_at, measured}, and each run has a
# folder <provider>-<local date> (its mkdir is the day's claim, atomic across daemons) with claim.json,
# out.json (the row), log and rc. Only the daemon that claimed a run reports it.
WATCH_DIR, VERSIONS = "cli_watch", "versions.json"
WATCHED = ("claude", "codex", "cursor")   # the providers `ttp doctor --live` measures
WATCH_EVERY_S = 3600   # each provider's `--version` is read at most this often, host-wide
WATCH_USD = 0.50       # the spend cap of the live run a version change starts
WATCH_TIMEOUT_S = 3 * RUN_TIMEOUT_S + 300   # the whole detached run: its three runs and the scratch setup
# argv: run dir, timeout, command...; writes out.json (stdout), log (stderr), then rc (124 once timed out)
# own session: the live run is killed whole (its group) once past the timeout
WATCH_RUNNER = r'''
import os, signal, subprocess, sys
d, limit, argv = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
with open(os.path.join(d, "out.json"), "wb") as out, open(os.path.join(d, "log"), "wb") as err:
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            rc = child.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            rc = 124
    except OSError:
        rc = 127
    out.flush()
    os.fsync(out.fileno())
with open(os.path.join(d, "rc.tmp"), "w") as f:
    f.write(f"{rc}\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(os.path.join(d, "rc.tmp"), os.path.join(d, "rc"))
'''


def watch_dir(home: Path | None = None) -> Path:
    from . import project
    return Path(home or project.HOME_DIR) / WATCH_DIR


def _read(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read_version(prov: Provider) -> str:
    """The CLI's `--version`, read now: cli_version's answer is cached for the process, and a daemon
    outlives an upgrade. "" when it cannot be read."""
    b = prov.binary()
    r = status_check([b, "--version"], 20) if b else None
    out = r[1].strip() if r and r[0] == 0 else ""
    return out.splitlines()[-1].strip()[:80] if out else ""


def live_argv(provider: str) -> list[str]:
    return [sys.executable, "-m", "ttp", "doctor", "--live", provider, "--json", "--max-usd", f"{WATCH_USD:.2f}"]


def check_versions(owner: str, providers: list[Provider], *, home: Path | None = None,
                   now: float | None = None, argv=live_argv) -> list[str]:
    """Read each installed, not logged-out provider's CLI version (at most every WATCH_EVERY_S) and start
    one detached live row for each whose version is not the one last measured, at most one per provider
    per local day: a change on a day that already had its run is measured the next day. A provider seen
    for the first time is only recorded. Returns the providers a run was started for."""
    now = time.time() if now is None else now
    d = watch_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    seen, started = _read(d / VERSIONS), []
    for prov in providers:
        rec = seen.get(prov.name) if isinstance(seen.get(prov.name), dict) else {}
        if now - float(rec.get("checked_at") or 0) < WATCH_EVERY_S:
            continue
        if not prov.available() or prov.login_check() is False:
            continue   # not recorded: a later install or login is still compared with the last measured version
        version = read_version(prov)
        if not version:
            continue
        rec = {**rec, "version": version, "checked_at": now}
        rec.setdefault("measured", version)
        if rec["measured"] != version and start(prov.name, version, owner, home=home, now=now, argv=argv):
            rec["measured"] = version
            started.append(prov.name)
        seen[prov.name] = rec
    write_json(d / VERSIONS, seen)
    return started


def start(provider: str, version: str, owner: str, *, home: Path | None = None, now: float | None = None,
          argv=live_argv) -> bool:
    """Claim today's live run for `provider` and start it detached; False when today's is taken."""
    now = time.time() if now is None else now
    run = watch_dir(home) / f"{provider}-{time.strftime('%Y-%m-%d', time.localtime(now))}"
    try:
        run.mkdir(parents=True)
    except FileExistsError:
        return False
    write_json(run / "claim.json", {"provider": provider, "version": version, "owner": owner, "at": now})
    runtime = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": runtime + os.pathsep + os.environ.get("PYTHONPATH", "")}
    try:   # own session: the detached watch runner outlives this daemon tick
        subprocess.Popen([sys.executable, "-c", WATCH_RUNNER, str(run), str(WATCH_TIMEOUT_S), *argv(provider)],
                         cwd=runtime, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        durable_write(run / "log", f"did not start: {e}\n")
        write_json(run / "rc", 127)
    return True


def harvest(owner: str, *, home: Path | None = None, now: float | None = None) -> list[tuple[str, str]]:
    """Each finished live run `owner` claimed and has not reported, once: its row is kept as
    <provider>.last.json, and the run is marked reported. A run with no rc WATCH_TIMEOUT_S plus ten
    minutes after its claim was killed (a reboot) and counts as finished without a row. Returns
    (key, text) of an observation for each run with a failing check or no row."""
    now = time.time() if now is None else now
    d, out = watch_dir(home), []
    for run in sorted(d.glob("*-*-*-*")) if d.is_dir() else []:
        claim = _read(run / "claim.json")
        if claim.get("owner") != owner or (run / "reported").exists():
            continue
        if not (run / "rc").exists() and now - float(claim.get("at") or 0) < WATCH_TIMEOUT_S + 600:
            continue
        try:
            rc = int((run / "rc").read_text().strip())
        except (OSError, ValueError):
            rc = -1   # killed before it wrote one
        r = _read(run / "out.json")
        provider, version = str(claim.get("provider")), str(claim.get("version"))
        head = f"{provider} CLI changed to {version}: its live check (`ttp doctor --live {provider}`)"
        if r.get("checks") is not None:
            write_json(d / f"{provider}.last.json", r)
        failing = {k: v for k, v in (r.get("checks") or {}).items() if v.get("status") == FAIL}
        if failing:
            out.append((f"cli-live:{provider}:{version}", f"{head} failed " + "; ".join(
                f"{k}: {v.get('note') or 'fail'}"[:200] for k, v in failing.items()) + f". Row: {run / 'out.json'}"))
        elif rc not in (0, 1) or r.get("checks") is None:
            try:
                tail = " | ".join((run / "log").read_text(errors="replace").strip().splitlines()[-3:])[-300:]
            except OSError:
                tail = ""
            out.append((f"cli-live:{provider}:{version}", f"{head} gave no row (exit {rc}): {tail or 'no log'}. "
                        f"Folder: {run}"))
        durable_write(run / "reported", f"{rc}\n")
    return out
