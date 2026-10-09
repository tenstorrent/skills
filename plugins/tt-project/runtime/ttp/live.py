# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp doctor --live <provider>`: one measured parity row for an agent CLI. Opt-in, and it spends
money (a few cents to a few tens of cents), so no test runs it against a real agent.

It makes a scratch project in a scratch git repository and starts its runs the way the daemon does
(Daemon.start_run, the detached runner), then reads them back with the provider's own parser:

- a tiny worker run: it runs one shell command, tries to write one file outside its writable
  roots, and writes result.json. Right after it starts, steer.md gets an update for it.
- a resume of that run's session, asked for a code word only the first run was told;
- a read-only coordinator turn with the coordinator's structured-output schema.

The scratch folder lives under the tt-project home, not the system temp dir: sandboxes such as
Codex's workspace-write let a worker write /tmp and $TMPDIR, so a probe there would prove nothing.

Each check is pass, fail or unsupported (the adapter does not use the feature at all; the note
says why). A provider that is not installed or is logged out is "not available", with the reason.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .providers.base import Provider, cli_output

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
    if not getattr(prov, "steer_hook", False):
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
            base: Path | None = None) -> dict:
    """Run the scratch project's three runs on `provider` and return what judge() reads."""
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
        prompt = (
            "This is a harness conformance check. Do exactly these steps and nothing else.\n"
            f"1. Remember the code word {word}; you will be asked for it later.\n"
            "2. Run this shell command once: echo live-check\n"
            f"3. Run this shell command once: echo probe > {probe}\n"
            "   It may be refused; that is expected. Note its outcome in a few words.\n"
            "4. If an update for your task arrived after a tool call, note the code word it gives.\n"
            f"5. Write this JSON to {RUN_DIR_MARK}/result.json, filled in: "
            '{"status": "done", "summary": "live check", "fence": "<outcome of step 3>", '
            '"steer_word": "<code word from the update, or empty>"}\n'
            "Then reply with one short line.")
        rid = d.start_run("worker", prompt, provider, tier, str(repo), task=p.db.task(tid),
                          budget_usd=WORKER_USD, timeout_s=timeout_s)
        run_dir = p.runs / str(rid)
        (run_dir / STEER_FILE).write_text(f"Update for your task: the update's code word is {steer_word}.\n")
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
        ev["steer_delivered"] = delivered
        ev["windows"] = [f"{w.get('window')}" for w in (u.extra.get("windows") or [])]
        if not ev["windows"] and type(prov).meter is not Provider.meter:
            ev["windows"] = [w.window for w in prov.meter()]
        env = json.loads((run_dir / "run.json").read_text()).get("env") or {}
        if not prov.resume_args("00000000-0000-4000-8000-000000000000"):
            ev["resume"] = {}
        elif not u.session_id:
            ev["resume"] = {"skipped": "the worker run reported no session to resume"}
        elif not prov.session_saved(u.session_id, str(repo), env):
            ev["resume"] = {"skipped": "the worker's session was not found on disk"}
        else:
            rid2 = d.start_run("worker", "What code word did I ask you to remember? Reply with only that word.",
                               provider, tier, str(repo), task=p.db.task(tid), budget_usd=RESUME_USD,
                               timeout_s=timeout_s, resume=u.session_id)
            exit2, u2 = _finish(d, prov, provider, rid2, timeout_s)
            ev["spend_usd"] += u2.cost_usd or 0
            ev["resume"] = {"recalled": word in (u2.final_text or ""),
                            "error": u2.error or exit2.get("error") or ""}
        crid = d.start_run("coordinator", 'Reply with exactly this JSON and nothing else: {"actions": [{"type": "noop"}]}',
                           provider, tier, str(p.base), read_only=True, schema=ACTIONS_SCHEMA,
                           system="You are a test coordinator. You only return the requested JSON.",
                           budget_usd=COORD_USD, timeout_s=timeout_s)
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
