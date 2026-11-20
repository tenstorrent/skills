"""Two escalation switches for runs on the GLM work arm (`route.glm_work`), both off by default.

- `route.glm_work.plan_first` (Opus plans, GLM executes, Opus reviews): the run's first step is
  `ttp advise --plan "<its reading of the task>"`, a read-only Opus call that returns the plan the run
  follows; `ttp push` refuses on such a run until a plan is on record. Reviews stay on Opus as before.
- `route.glm_work.advisor` (ask Opus): the run may ask Opus a read-only question with `ttp advise
  "<question>"`, at most `max_calls` times and `max_minutes` in all per run.

Both are the same one-shot call: the Claude CLI at `model`/`effort`, with read-only tools (Read, Grep,
Glob) in the run's own working folder, no session, its cost booked on the Claude side and its minutes
counted there (glmshare subtracts them from the run's GLM hours). The daemon writes the run's limits to
FILE in its run folder at launch (`arm`); each call appends a line to LOG there, the per-run log of what
the switches did. A run without FILE (the switches off, or not a GLM-arm run) is refused."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

KEY = "glm_assist"        # the run note's field: the switches this run was launched with
FILE = "advisor.json"     # in the run folder: the run's limits, written by the daemon
LOG = "advisor.jsonl"     # in the run folder: one line per call
BIN_ENV = "TTP_ADVISOR_BIN"   # a stand-in for the claude binary (tests)
DEFAULTS = {"max_calls": 3, "max_minutes": 10.0, "model": "opus", "effort": "high", "max_usd": 3.0}
SYSTEM = ("You advise an agent on another model that is doing a software task. Answer only what it asks, "
          "concretely: files, functions, the change to make, the risks. You may read the repository with your "
          "read-only tools; you change nothing and run nothing. If the task needs deeper work than the agent "
          "can do, say so in one line starting 'needs_opus:'.")
PLAN = ("Write a short numbered plan for the task below: the files and functions to change, the change in each, "
        "the tests to add or run, and the risks. Read the repository as needed. If the task should not be done "
        "on this model, answer with one line starting 'needs_opus:' and why.")


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def settings(cfg: dict) -> dict:
    """{"plan_first", "advisor", "max_calls", "max_minutes", "model", "effort", "max_usd"} from route.glm_work."""
    g = (cfg.get("route") or {}).get("glm_work")
    g = g if isinstance(g, dict) else {}
    adv = g.get("advisor")
    row = adv if isinstance(adv, dict) else {"on": adv}
    return {"plan_first": g.get("plan_first") is True, "advisor": row.get("on") is True,
            "max_calls": int(_num(row.get("max_calls"), 0, 20, DEFAULTS["max_calls"])),
            "max_minutes": _num(row.get("max_minutes"), 0, 120, DEFAULTS["max_minutes"]),
            "model": str(row.get("model") or DEFAULTS["model"]),
            "effort": str(row.get("effort") or DEFAULTS["effort"]),
            "max_usd": _num(row.get("max_usd"), 0, 50, DEFAULTS["max_usd"])}


def for_run(cfg: dict) -> dict | None:
    """What a GLM-arm run is launched with (its note's KEY), None with both switches off."""
    s = settings(cfg)
    return s if s["plan_first"] or s["advisor"] else None


def prompt_lines(s: dict | None) -> str:
    """What a GLM-arm run is told about the switches it runs with ('' when none)."""
    if not s:
        return ""
    out = []
    if s.get("plan_first") and not s.get("device"):   # a device run's plan lines: deviceplan.glm_lines
        out.append('Before your first edit, run `ttp advise --plan "<the task in your own words, and the files you '
                   'expect to touch>"` and follow the plan it prints; `ttp push` refuses until a plan is on '
                   "record. If the plan starts with needs_opus:, hand the task back to Opus.")
    if s.get("advisor"):
        out.append(f'When you are stuck or unsure, ask Opus with `ttp advise "<question>"` (read-only, at most '
                   f"{s['max_calls']} questions and {s['max_minutes']:g} minutes in all for this run).")
    return "\n\n" + "\n".join(out)


def arm(run_dir, s: dict | None) -> None:
    """The daemon, at launch: the run's limits in its run folder (nothing when both switches are off)."""
    if not s:
        return
    from .project import durable_write
    durable_write(Path(run_dir) / FILE, json.dumps(s, sort_keys=True))


def limits(run_dir) -> dict | None:
    try:
        got = json.loads((Path(run_dir) / FILE).read_text())
    except (OSError, ValueError, TypeError):
        return None
    return got if isinstance(got, dict) else None


def rows(path) -> list[dict]:
    """The JSON object lines of a jsonl file, oldest first; unreadable lines are skipped."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def calls(run_dir) -> list[dict]:
    """The run's calls so far (LOG), oldest first."""
    return rows(Path(run_dir) / LOG)


def hours(run_dir) -> float:
    """The run's advisor hours (they count on the Claude side, not the GLM side)."""
    return sum(float(r.get("minutes") or 0) for r in calls(run_dir)) / 60


def planned(run_dir) -> bool:
    return any(r.get("kind") == "plan" and r.get("ok") for r in calls(run_dir))


def push_refusal(run_dir) -> str | None:
    """Why a plan-first run may not push yet, else None."""
    lim = limits(run_dir) if run_dir else None
    if not lim or not lim.get("plan_first") or planned(run_dir):
        return None
    return ('this run is plan-first (route.glm_work.plan_first): run `ttp advise --plan "<the task in your '
            'words>"` and follow its plan before `ttp push`')


def refusal(run_dir, plan: bool, step=None) -> str | None:
    """Why the run may not make this call now, else None."""
    lim = limits(run_dir) if run_dir else None
    if not lim:
        return "`ttp advise` runs only in a GLM-arm run launched with route.glm_work.advisor or plan_first on"
    done = calls(run_dir)
    used = sum(float(r.get("minutes") or 0) for r in done)
    if used >= float(lim.get("max_minutes") or 0):
        return f"this run's advisor minutes are used up ({used:.1f} of {lim.get('max_minutes')}); decide yourself"
    if step is not None:   # a device run's Opus check before a reset, power cycle, flash or firmware step
        from . import deviceplan
        if not lim.get("device") or not planned(run_dir):
            return "`--check-step` checks a step of a device run's plan: run `ttp advise --plan` first"
        steps = (deviceplan.plan(run_dir) or {}).get("steps") or []
        cap = sum(1 for st in steps if deviceplan.risky(st["command"])) + 2
        if sum(1 for r in done if r.get("kind") == "check") >= cap:
            return f"this run has used its {cap} step checks; hand the task back to Opus"
        return None
    if plan:
        if not lim.get("plan_first"):
            return "plan-first is off for this run; ask a question with `ttp advise \"<question>\"` instead"
        if planned(run_dir):
            return "this run already has its plan (see advisor.jsonl in the run folder); follow it"
        return None
    if not lim.get("advisor"):
        return "the ask-Opus advisor is off for this run"
    asked = sum(1 for r in done if r.get("kind") == "ask")
    if asked >= int(lim.get("max_calls") or 0):
        return f"this run has asked Opus {asked} times, its limit; decide yourself or hand the task back"
    return None


def _run(argv: list[str], text: str, timeout: float, cwd: str) -> dict:
    out = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    if out.returncode != 0 or data.get("is_error"):
        raise RuntimeError(f"exit {out.returncode}: {str(data.get('result') or out.stderr)[-200:]}")
    return data


def _opus_call(run, argv: list[str], text: str, timeout: float, cwd: str, model: str, usage: dict) -> tuple[str, float]:
    """One CLI call: its answer and dollars; its token counts add into `usage`."""
    data = run(argv, text, timeout, cwd)
    got = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    for k in ("input_tokens", "output_tokens"):
        usage[k] = int(usage.get(k) or 0) + int(got.get(k) or 0)
    usd = float(data.get("total_cost_usd") or 0)
    if not usd and got:
        from . import localspend
        usd = localspend.price(model, got)[0]
    return str(data.get("result") or "").strip(), usd


def _binary() -> str:
    if got := os.environ.get(BIN_ENV):
        return got
    from .providers.base import find_binary
    return find_binary("claude") or ""


def ask(run_dir, question: str, *, cwd: str, plan: bool = False, db=None, source: str = "advisor",
        run=None, binary: str | None = None, now=time.time, step=None) -> dict:
    """One call: {"ok", "answer", "minutes", "cost_usd"} (or {"ok": False, "error"}), logged in LOG and
    booked in `db` on the Claude side. Raises ValueError when refusal() refuses it. A device run's plan
    (limits "device") is kept by deviceplan.store; `step` asks Opus to check that plan step ("verdict")."""
    from . import deviceplan
    if why := refusal(run_dir, plan, step):
        raise ValueError(why)
    lim = limits(run_dir) or {}
    used = sum(float(r.get("minutes") or 0) for r in calls(run_dir))
    timeout = max(30.0, (float(lim.get("max_minutes") or 0) - used) * 60)
    exe = binary if binary is not None else _binary()
    model = str(lim.get("model") or DEFAULTS["model"])
    argv = [exe, "-p", "--model", model, "--effort", str(lim.get("effort") or DEFAULTS["effort"]),
            "--output-format", "json", "--tools", "Read,Grep,Glob", "--no-session-persistence",
            "--strict-mcp-config", "--setting-sources", "", "--max-budget-usd",
            f"{float(lim.get('max_usd') or DEFAULTS['max_usd']):.2f}", "--system-prompt", SYSTEM]
    device = plan and lim.get("device")
    block = deviceplan.source_block(deviceplan.copy_sources(run_dir)) if device else ""
    text = (deviceplan.DEVICE_PLAN + "\n\n" + block + "--- task ---\n" + str(lim.get("task") or "")[:6000] + "\n\n--- the agent's "
            "words ---\n" if device else PLAN + "\n\n--- task ---\n" if plan else "") + question.strip()
    if step is not None:
        text = deviceplan.check_text(run_dir, step) or ""
        if not text:
            raise ValueError(f"the plan has no step {step}")
    t0 = now()
    row = {"kind": "check" if step is not None else "plan" if plan else "ask", "q": question.strip()[:500]}
    answer, usd, usage = "", 0.0, {}
    try:
        if not exe:
            raise RuntimeError("no claude binary on this host")
        answer, usd = _opus_call(run or _run, argv, text, timeout, cwd, model, usage)
        left = timeout - (now() - t0)   # the fix-up never runs past max_minutes
        if device and answer and not answer.lower().startswith("needs_opus:") \
                and (why := deviceplan.parse_why(answer)[1]):
            # A bad plan never reaches GLM: Opus fixes its own block once, in this plan step.
            row.update(plan_error=why[:300])
            if left < 30.0:
                raise RuntimeError(f"the plan does not parse ({why[:200]}) and no time is left to fix it")
            got, more = _opus_call(run or _run, argv, deviceplan.FIX.format(why=why) + answer, left, cwd, model, usage)
            answer, usd = got or answer, usd + more
        left = timeout - (now() - t0)
        if device and answer and not answer.lower().startswith("needs_opus:") and left >= 30.0 \
                and (why := deviceplan.source_why(run_dir, answer)):
            # A step not copied from the project's passing runs or scripts goes back to Opus once; store hands
            # back a plan still unsourced.
            row.update(source_error=why[:300])
            got, more = _opus_call(run or _run, argv, deviceplan.SOURCE_FIX.format(why=why, sources=block) + answer,
                                   left, cwd, model, usage)
            answer, usd = got or answer, usd + more
        row.update(ok=bool(answer), answer=answer[:4000])
        if step is not None:
            row.update(step=str(step), verdict="ok" if answer.lower().startswith("ok") else "stop")
    except Exception as e:   # a failed call still spends its minutes; a failed fix-up keeps the first plan
        row.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
    if device and answer:
        if why := deviceplan.store(run_dir, answer, cwd, task=str(lim.get("task") or "")):   # it hands the run back to Opus
            row.update(ok=False, error=row.get("error") or f"the plan fails a check: {why}"[:300], handed_back=True)
    row.update(ts=round(t0, 1), minutes=round(max(0.0, now() - t0) / 60, 3), cost_usd=round(usd, 4), model=model)
    with open(Path(run_dir) / LOG, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    if row.get("verdict") == "stop":
        from . import glmguard
        glmguard.hit(run_dir, f"Opus's check stopped step {step}: {answer[:240]}", kind="check")
    if db is not None and usd:
        db.spend("claude", usd, source, account=model, tokens_in=int(usage.get("input_tokens") or 0),
                 tokens_out=int(usage.get("output_tokens") or 0))
    return row
