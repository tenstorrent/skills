"""Offline replay (`ttp bench replay`): finished device and analysis tasks given to the arms as sealed
questions and blind-reviewed against what the project later established.

A sealed question is a fresh one-commit repository holding QUESTION.md (the task's spec) and evidence/
(the tool results the task's own run recorded: logs, command outputs, data files, in time order). The task's
own hand-off and answer, its writes and anything dated after it are left out, so the answer is the
arm's. The established answer is written from later records before any arm runs, kept outside the cells
(<bench home>/replay/established.json), and goes only to the blind reviewer. The cells run through the
daily bench's runner and ledger (benchdaily.py): the same isolation, the same hang guard, a Claude cap.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from . import bench, benchblind, benchdaily, benchnotrun, benchparity, benchrun, gitmemo
from .project import durable_write

CAP_USD = 60.0          # Claude spend of the whole replay (arm runs plus reviews)
CELL_USD = 1.5          # most one arm run may spend (the run's own budget stop)
REVIEW_USD = 0.8        # reserved per question for its blind reviews
LIMIT_S = 1800          # a cell's hang guard
EVIDENCE_CAP = 120_000  # characters of evidence in one question
BLOCK_CAPS = (6000, 3000, 1500, 800)   # a tool result is cut to this many characters, until the evidence fits
SHINGLE = 12            # words in a hand-off shingle
HEAD = "5ea1ed5ea1ed" + "0" * 28   # the "landed commit" of a question that has none: names nothing real
CLASSES = ("device", "analysis")
SKIP_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite", "Agent", "Task", "Skill", "ExitPlanMode"}
OWN = re.compile(r"result\.json|TTP_RUN_DIR|\bttp\s+(note|obligation|handoff|task\s+\S+\s+(add|update|done))\b")
SECRET = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[private key removed]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github" r"_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
                r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"),
     "[token removed]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|passw(?:or)?d|authorization)(\"?\s*[=:]\s*\"?)(?:bearer\s+)?[^\s\"',;]{6,}"),
     r"\1\2[removed]"),
    (re.compile(r"(?m)^(ssh-(?:rsa|ed25519)|ecdsa-sha2-\S+) \S{20,}.*$"), r"\1 [key removed]"),
]


def home() -> Path:
    """The replay's folder, beside the daily bench's (<bench home>/replay): established.json, run/, table.md."""
    return benchdaily.home().parent / "replay"


class ReplayFault(RuntimeError):
    """A question that cannot be built safely (no record, a secret that stays, nothing to read)."""


def redact(text: str) -> str:
    for pat, sub in SECRET:
        text = pat.sub(sub, text)
    return text


def clip(text: str, n: int) -> str:
    """`text` cut to about `n` characters: its start and its end, the cut named."""
    if len(text) <= n:
        return text
    head = n * 2 // 3
    return f"{text[:head]}\n[... {len(text) - n} characters cut ...]\n{text[-(n - head):]}"


def _shingles(text: str) -> set[int]:
    w = re.findall(r"\w+", text.lower())
    return {hash(tuple(w[i:i + SHINGLE])) for i in range(max(0, len(w) - SHINGLE + 1))}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _label(name: str, inp: dict) -> str:
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return str(inp.get("command", "")).strip().splitlines()[0][:150] if str(inp.get("command", "")).strip() else name
    for k in ("file_path", "path", "pattern", "url", "query"):
        if inp.get(k):
            return f"{name} {str(inp[k])[:150]}"
    return name


def read_run(output: Path) -> tuple[list[dict], list[str]]:
    """(blocks, prose) of one run's transcript: the tool results as {tool, label, text} in order (the agent's
    own writes, notes and sub-agent answers aside), and the agent's own words (its messages and final result),
    which a block is checked against so an echo of the answer is not evidence."""
    calls: dict[str, tuple[str, dict]] = {}
    blocks, prose = [], []
    for line in output.read_text(errors="replace").splitlines() if output.exists() else []:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        kind = j.get("type")
        if kind == "result" and isinstance(j.get("result"), str):
            prose.append(j["result"])
        content = (j.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if kind == "assistant" and b.get("type") == "text":
                prose.append(str(b.get("text", "")))
            elif kind == "assistant" and b.get("type") == "tool_use":
                calls[b.get("id", "")] = (str(b.get("name", "")), b.get("input") or {})
            elif kind == "user" and b.get("type") == "tool_result":
                name, inp = calls.get(b.get("tool_use_id", ""), ("", {}))
                if name in SKIP_TOOLS or OWN.search(json.dumps(inp)):
                    continue
                text = _text(b.get("content")).strip()
                if text:
                    blocks.append({"tool": name, "label": _label(name, inp), "text": text})
    return blocks, prose


def echoes(text: str, handoff: set[int]) -> bool:
    """Whether `text` is mostly the hand-off's own words: three or more of its 12-word runs are the hand-off's,
    and they are a quarter or more of all its runs (a log line the hand-off quotes stays evidence)."""
    own = _shingles(text)
    shared = len(own & handoff)
    return bool(own) and shared >= 3 and shared / len(own) >= 0.25


def evidence(blocks: list[dict], handoff_texts: list[str], cap: int = EVIDENCE_CAP) -> tuple[list[dict], dict]:
    """The blocks that may go in a sealed question: redacted, the hand-off's echoes dropped, each cut so the
    whole fits `cap` characters. Returns (kept blocks with `text` cut, {"dropped_echo", "cut", "chars"})."""
    hand = set()
    for t in handoff_texts:
        hand |= _shingles(t)
    kept, dropped = [], 0
    for b in blocks:
        text = redact(b["text"])
        if hand and echoes(text, hand):
            dropped += 1
            continue
        kept.append({**b, "text": text})
    for n in BLOCK_CAPS:
        if sum(min(len(b["text"]), n) + len(b["label"]) + 20 for b in kept) <= cap:
            break
    out, used = [], 0
    for b in kept:
        text = clip(b["text"], n)
        if used + len(text) > cap:
            break
        used += len(text) + len(b["label"]) + 20
        out.append({**b, "text": text})
    return out, {"dropped_echo": dropped, "cut": len(kept) - len(out), "chars": used, "block_cap": n}


def message_of(result) -> str:
    try:
        return str(json.loads(result or "{}").get("message") or "")
    except (ValueError, AttributeError):
        return ""


def handoffs(row, runs: list[dict]) -> list[str]:
    """Everything the task's own runs said as their answer: the task's result message and each run's
    result.json."""
    out = [message_of(row["result"])]
    for r in runs:
        try:
            out.append(str(json.loads((Path(r["dir"]) / "result.json").read_text()).get("message") or ""))
        except (OSError, ValueError, AttributeError):
            pass
    return [t for t in out if t.strip()]


def question(row) -> str:
    return f"# {row['title']}\n\n{row['spec'] or ''}\n"


def dossier(entry: dict, proj: dict, dest: Path, cap: int = EVIDENCE_CAP) -> dict:
    """Build the sealed question for one established entry at `dest` (a new folder) and return its task dict.
    Reads the project's records and run folders read-only. Raises ReplayFault when no evidence can go in."""
    db = benchdaily.open_ro(proj["db"])
    rows = db.q("SELECT * FROM tasks WHERE id=?", (entry["id"],))
    if not rows:
        raise ReplayFault(f"no task {entry['id']} in the project's records")
    row = rows[0]
    runs = db.q("SELECT id, dir FROM runs WHERE task=? ORDER BY id", (entry["id"],))
    blocks, prose = [], []
    for r in runs:
        b, p = read_run(Path(r["dir"]) / "output.jsonl")
        blocks += b
        prose += p
    kept, meta = evidence(blocks, handoffs(row, runs) + prose, cap)
    if not kept:
        raise ReplayFault(f"no recorded evidence is left for task {entry['id']} ({len(blocks)} tool results read)")
    dest.mkdir(parents=True)
    (dest / "evidence").mkdir()
    index = ["# Evidence", "", "Recorded while the question was worked on, in time order.", ""]
    for i, b in enumerate(kept, 1):
        name = f"{i:03d}.txt"
        (dest / "evidence" / name).write_text(f"$ {b['label']}\n\n{b['text']}\n")
        index.append(f"- evidence/{name}: {b['label']}")
    (dest / "evidence" / "INDEX.md").write_text("\n".join(index) + "\n")
    (dest / "QUESTION.md").write_text(redact(question(row)))
    bad = [p.name for p in dest.rglob("*") if p.is_file() and re.search(r"PRIVATE KEY", p.read_text(errors="replace"))]
    if bad:   # a stub header the redaction could not close: never build the question on it
        shutil.rmtree(dest, ignore_errors=True)
        raise ReplayFault(f"a private key header stays in {bad[0]}")
    with gitmemo.scope({}):
        tree = bench.seal(dest)
        base = bench._out(dest, "rev-parse", "HEAD")
    return {"task": "", "base": base, "head": HEAD, "tree": tree, "tests": [], "heldout": {},
            "spec": redact(question(row)), "evidence": meta, "n_evidence": len(kept)}


def slug(entry: dict) -> str:
    return f"rp-{benchdaily.slug(entry['project'])}-{entry['id']}"


def load(path: Path | None = None) -> list[dict]:
    """The established answers: [{key, project, id, class, established, sources}], written before any arm runs."""
    path = path or home() / "established.json"
    got = json.loads(path.read_text())
    for e in got:
        if e.get("class") not in CLASSES or not str(e.get("established") or "").strip():
            raise ValueError(f"{e.get('key')}: needs class device|analysis and an established answer")
    return got


def build(entries: list[dict], bench_dir: Path, projects: dict[str, dict], cap: int = EVIDENCE_CAP) -> tuple[dict, list]:
    """Each entry's question under bench_dir, keyed by its replay id. Returns (tasks, faults)."""
    tasks, faults = {}, []
    for e in entries:
        tid = slug(e)
        try:
            proj = projects.get(e["project"])
            if proj is None:
                raise ReplayFault(f"project {e['project']} is not on this host")
            if (bench_dir / tid / "workspace" / "QUESTION.md").exists():
                with gitmemo.scope({}):
                    ws = bench_dir / tid / "workspace"
                    t = {"task": tid, "base": bench._out(ws, "rev-parse", "HEAD"), "head": HEAD,
                         "tree": bench._out(ws, "rev-parse", "HEAD^{tree}"), "tests": [], "heldout": {},
                         "spec": (ws / "QUESTION.md").read_text(), "evidence": {}, "n_evidence": 0}
            else:
                t = dossier(e, proj, bench_dir / tid / "workspace", cap)
        except (ReplayFault, OSError, ValueError, KeyError) as err:
            faults.append({"key": e["key"], "why": f"{type(err).__name__}: {err}"[:300]})
            continue
        tasks[tid] = {**t, "task": tid, "project": e["project"], "source_task": e["id"], "class": e["class"],
                      "repo": "", "source_root": str(proj["root"]), "state": str(proj["db"].parent)}
    return tasks, faults


PROMPT = ("You are given a sealed question. The repository in your current directory holds QUESTION.md (the task "
          "as it was given) and evidence/ (the logs, command outputs and data files recorded while it was "
          "worked on, in time order; evidence/INDEX.md lists them). You have no network, no device and no other "
          "record: do not try to run what the question asks for; work from the evidence only. Answer the "
          "question: write your answer to ANSWER.md in this repository (the conclusion first, then the evidence "
          "it rests on, then what the evidence cannot settle) and commit it. You have {limit_min} minutes in all; "
          "that is a hang guard, so plan for far less. When you are done, write "
          '{{"status": "done", "summary": "<one line>"}} to $TTP_RUN_DIR/result.json.\n\n## Question\n\n{spec}\n')


def answer_of(keep: Path) -> str:
    """An arm's answer: ANSWER.md as its diff added it, else the agent's final message, else a note."""
    diff = (keep / "diff.patch").read_text(errors="replace") if (keep / "diff.patch").exists() else ""
    m = re.search(r"^\+\+\+ b/ANSWER\.md\n(.*?)(?=^diff --git |\Z)", diff, re.S | re.M)
    if m:
        body = [x[1:] for x in m.group(1).splitlines() if x.startswith("+") and not x.startswith("+++")]
        if (text := "\n".join(body).strip()):
            return text
    last = ""
    for line in (keep / "output.jsonl").read_text(errors="replace").splitlines() if (keep / "output.jsonl").exists() else []:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("type") == "result" and isinstance(j.get("result"), str):
            last = j["result"]
    return last.strip() or "(no answer was written)"


def answer_cells(entries: dict[str, dict]):
    """A `build` for benchdaily.blind_review: one cell per question whose arms all ended, each arm's answer in
    the place of a diff and the established answer as the reference."""
    def build_cells(out: Path, tasks: dict, arms: dict) -> dict:
        rows = benchdaily.read_rows(out / "results.jsonl")
        cells = []
        for tid, task in tasks.items():
            if any((tid, 0, a) not in rows or rows[(tid, 0, a)].get("endpoint_fault") for a in arms):
                continue
            # an arm whose cell no model ran in has no answer to review (benchnotrun.py): it is left out, never graded
            # as an empty answer; a model that worked and wrote nothing is graded ("(no answer was written)") and fails
            live = [a for a in sorted(arms) if not benchnotrun.is_not_run(rows[(tid, 0, a)])]
            if not live:
                continue
            side = {}
            for a in live:
                keep = out / "cells" / f"{tid}-r0-{a}"
                side[a] = {"diff": answer_of(keep), **{k: arms[a].get(k, "") for k in ("provider", "model", "effort")},
                           "run_id": keep.name, "session_id": rows[(tid, 0, a)].get("session_id") or "",
                           "transcript": str(keep / "output.jsonl")}
            cells.append({"task": tid, "criteria": clip(str(task["spec"]), 4000),
                          "reference": entries[tid]["established"], "arms": side})
        return {"cells": cells}
    return build_cells


def table(out: Path, tasks: dict, arms: dict, graded: dict, faults: list, skipped: list, limit_s: int | None = None) -> dict:
    """Per arm and class (device, analysis) and overall: N, mean 1-5 scores, share that agrees (4 or 5), $ and
    minutes per question, hang-guard stops; plus spend by provider kind and each arm's parity row
    (benchdaily.parity_row: its scored questions as the task set, the hang guard as the limit)."""
    rows = benchdaily.read_rows(out / "results.jsonl")
    idle = benchnotrun.tally(list(rows.values()))   # per arm: cells no model ran in, outside every N and rate
    cells = []
    for _, (tid, arm, score) in sorted(graded["scored"].items()):
        r = rows.get((tid, 0, arm)) or {}
        s = score["scores"]
        cells.append({"task": tid, "key": f"{tasks[tid]['project']}:{tasks[tid]['source_task']}",
                      "class": tasks[tid]["class"], "arm": arm, "claude": benchdaily.is_claude(arms[arm]),
                      "answered": int(s["answered"]), "supported": int(s["supported"]), "agrees": int(s["agrees"]),
                      "cost_usd": float(r.get("cost_usd") or 0), "minutes": round(float(r.get("wall_s") or 0) / 60, 2),
                      "stopped": bool(r.get("hang_guard")) or r.get("outcome") in ("timeout", "stalled")})

    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        if not n:
            return {}
        mean = lambda k: round(sum(c[k] for c in rs) / n, 2)
        return {"n": n, "answered": mean("answered"), "supported": mean("supported"), "agrees": mean("agrees"),
                "agree_4plus": sum(1 for c in rs if c["agrees"] >= 4), "usd_per_task": mean("cost_usd"),
                "min_per_task": round(sum(c["minutes"] for c in rs) / n, 1), "stops": sum(c["stopped"] for c in rs)}
    sec = graded.get("second")
    if sec:
        two = {(t, a): sc["scores"] for t, a, sc in sec["scored"].values()}
        for c in cells:
            if (c["task"], c["arm"]) in two:
                c["second_agrees"] = int(two[(c["task"], c["arm"])]["agrees"])
    spend = benchdaily.review_spent(out)
    claude = sum(c["cost_usd"] for c in cells if c["claude"])
    return {"cells": cells,
            **({"second_grader": {"reviewer": sec["reviewer"], "compare": sec["compare"], "unscored": sec["unscored"],
                                  "agree_4plus": {a: sum(1 for c in cells if c["arm"] == a and c.get("second_agrees", 0) >= 4)
                                                  for a in sorted(arms)}}} if sec else {}),
            "arms": {a: {cls: agg([c for c in cells if c["arm"] == a and c["class"] == cls]) for cls in (*CLASSES, "all")
                         if cls == "all" or any(c["class"] == cls for c in cells)} | {
                "all": agg([c for c in cells if c["arm"] == a])} for a in sorted(arms)},
            "spend": {"claude_arms_usd": round(claude, 4), "review_usd": round(spend, 4),
                      "claude_usd": round(claude + spend, 4),
                      "glm_usd": round(sum(c["cost_usd"] for c in cells if not c["claude"]), 4)},
            **({"not_run": {"per_arm": idle, "cells": [
                {"task": r["task"], "arm": r["arm"], "why": benchnotrun.cell_record(r)["why"]}
                for r in sorted(rows.values(), key=lambda r: (str(r["task"]), str(r["arm"]))) if benchnotrun.is_not_run(r)]}}
               if idle else {}),
            "faults": faults, "skipped": skipped, "unscored": graded.get("unscored", []),
            "parity": [benchdaily.parity_row(":".join([str(arms[a].get(f) or "") for f in ("provider", "model", "effort")]
                                                      + ([arms[a]["harness"]] if arms[a].get("harness") else [])),
                                             [{"day": "replay", "task": c["task"]} for c in cells if c["arm"] == a],
                                             {"replay": limit_s} if limit_s else {}) | {"arm": a} for a in sorted(arms)]}


HEAD_ROW = ("| arm | N | answered (1-5) | supported (1-5) | agrees (1-5) | agrees 4+ | $ per question | "
            "minutes per question | hang-guard stops | not run |\n|---|---|---|---|---|---|---|---|---|---|")


def table_md(rec: dict, names: dict[str, str]) -> str:
    """The replay table as markdown: one block per class, then all questions."""
    lines = ["# Offline replay: sealed questions blind-reviewed against what the project later established", "",
             "Scores are the blind Opus review's, 1 (worst) to 5. `agrees` compares with the established answer, "
             "written from later records before any arm ran. $ per question is each arm's own spend. A model that worked and "
             "wrote no answer failed the question. Not run = no model ran in the cell (never launched, zero model "
             "calls, or the server broke it): its own count per arm, outside N and every rate.", ""]
    for cls in (*CLASSES, "all"):
        have = {a: v[cls] for a, v in rec["arms"].items() if v.get(cls)}
        if not have:
            continue
        lines += [f"## {cls}", "", HEAD_ROW]
        for a, v in have.items():
            lines.append(f"| {names.get(a, a)} | {v['n']} | {v['answered']} | {v['supported']} | {v['agrees']} | "
                         f"{v['agree_4plus']}/{v['n']} | ${v['usd_per_task']} | {v['min_per_task']} | {v['stops']} | "
                         f"{(rec.get('not_run') or {}).get('per_arm', {}).get(a, 0) if cls == 'all' else ''} |")
        lines.append("")
    if sg := rec.get("second_grader"):
        cmp = sg.get("compare") or {}
        lines += ["## Second grader", "",
                  "The same masked answers graded again by a second model with the same rubric. Agrees 4+ per arm: "
                  + ", ".join(f"{names.get(a, a)} {n}" for a, n in sg["agree_4plus"].items())
                  + (f". Ranking {'changed' if cmp.get('ranking_changed') else 'unchanged'}: "
                     f"{' > '.join(names.get(a, a) for a in cmp.get('rank_a', []))}; second grader: "
                     f"{' > '.join(names.get(a, a) for a in cmp.get('rank_b', []))}." if cmp else "."), ""]
    sp = rec["spend"]
    lines += [f"Spend: Claude ${sp['claude_usd']:.2f} (arms ${sp['claude_arms_usd']:.2f}, reviews ${sp['review_usd']:.2f}); "
              f"GLM ${sp['glm_usd']:.2f}."]
    for title, items in (("Not built", rec["faults"]), ("Skipped", rec["skipped"]), ("Not scored", rec["unscored"]),
                         ("Not run (no model ran)", [{**x, "task": f"{x['task']} {names.get(x['arm'], x['arm'])}"}
                                                     for x in (rec.get("not_run") or {}).get("cells", [])])):
        if items:
            lines += ["", f"{title}:"] + [f"- {x.get('key') or x.get('task') or x.get('item')}: {x['why']}" for x in items]
    if rec.get("parity"):
        lines += [""] + benchparity.section([r | {"arm": names.get(r["arm"], r["arm"])} for r in rec["parity"]],
                                            fields=benchdaily.PARITY_FIELDS)
    return "\n".join(lines) + "\n"


def run_replay(arms: dict, cfg: dict, cap: float = CAP_USD, parallel: int = benchdaily.PARALLEL, root: Path | None = None,
               established: Path | None = None, only: list[str] | None = None, opts: dict | None = None, ask=None,
               progress=None, projects: dict | None = None, seed: int | None = None, ask2=None) -> dict:
    """Build the questions, run every arm under the Claude cap, review blind, write <root>/table.md and
    replay.json. Resumable: built questions, results.jsonl and the blind folder are reused."""
    say = progress or (lambda s: print(s, file=sys.stderr, flush=True))
    root = root or home()
    out, bench_dir = root / "run", root / "run" / "bench"
    out.mkdir(parents=True, exist_ok=True)
    entries = load(established or root / "established.json")
    if only:
        entries = [e for e in entries if e["key"] in only or e["class"] in only]
    projects = projects if projects is not None else {p["name"]: p for p in benchdaily.projects(None)}
    for name, arm in arms.items():
        benchrun.check_harness(name, arm)
    arms = {name: benchrun.resolve_harness(arm, opts or {}) for name, arm in arms.items()}
    tasks, faults = build(entries, bench_dir, projects)
    say(f"bench replay: {len(tasks)} question(s) built, {len(faults)} not built")
    by_id = {slug(e): e for e in entries}
    order = sorted(tasks, key=lambda t: (hashlib.sha256(f"{seed or 0}:{t}".encode()).hexdigest()))
    ropts = {"timeout_s": LIMIT_S, "stall_s": 900, "budget_usd": CELL_USD, "review_usd": REVIEW_USD, "prompt": PROMPT,
             "test_cmd": benchrun.TEST_CMD, "test_timeout_s": 900, "dry_run": False, "scratch_home": True, **(opts or {})}
    done = benchdaily.run_cells("replay", tasks, arms, out, bench_dir, ropts, cfg, cap, parallel, order, say)
    graded = benchdaily.blind_review(out, tasks, arms, cfg, ask, seed, build=answer_cells(by_id),
                                     rubric=benchblind.ANSWER_RUBRIC, ask2=ask2)
    rec = table(out, tasks, arms, graded, faults, done["skipped"], ropts["timeout_s"])
    rec["arms_def"] = {k: {f: v.get(f, "") for f in ("provider", "model", "effort", "harness")} for k, v in arms.items()}
    rec["evidence"] = {t: {"n": tasks[t]["n_evidence"], **tasks[t]["evidence"]} for t in tasks}
    names = {a: f"{v.get('model')} {v.get('effort')}" + (f" ({v['harness']})" if v.get("harness") else "")
             for a, v in rec["arms_def"].items()}
    durable_write(root / "replay.json", json.dumps(rec, indent=1) + "\n")
    durable_write(root / "table.md", table_md(rec, names))
    return rec
