"""Fact-match grading of the replay bench (bench v2 stage 5): each answer is judged against 2-4 checkable facts
written from the project's own records, frozen before any model runs, by two graders with the arm hidden.

The case file (`facts/cases.json` under the replay folder, `ttp bench facts --set <file>`) is JSON:
    {"cases": [{"key": "<project>:<id>",            # the replay question's key (benchreplay.load)
                "project": "<project>", "id": "<id>",
                "class": "device|analysis",
                "facts": [{"source": "<where the fact is in the records>", "text": "<one checkable statement>"}],
                "outcome_type": "...", "outcome_note": "...", "why_hard": "..."}],   # the last three: notes
     "dropped": [{"key": "<project>:<id>", "why": "<why no fact set was written>"}]}
A case has 2 to 4 facts, each a statement an answer either says or does not (a number, a name, a direction).
Frozen: `ttp bench facts --freeze` writes `cases.sha256` (sha256 of the file's bytes) once; grading refuses
a file whose hash differs from it or that has no recorded hash.

Graders: Opus and the open model, both at high effort, never above (blindreview.cap_effort); `bench.facts.graders`
({name: {provider, model, effort}}) replaces them. Each fact gets match, partial, miss or contradicted. The fact
verdict a table counts is the lower of the two graders'; every fact they judged differently is flagged.
A cell no model ran in (benchnotrun) is not graded and sits outside the rates as its own count; a model that
ran and wrote nothing is graded: every fact a miss, no grader call, and it stays in the denominators.
"""
from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import benchblind, benchdaily, benchnotrun, benchreplay, benchv2
from .project import durable_write

VERDICTS = ("contradicted", "miss", "partial", "match")   # worst to best: the lower of two graders counts
SCORE = {"match": 1.0, "partial": 0.5, "miss": 0.0, "contradicted": 0.0}
MIN_FACTS, MAX_FACTS = 2, 4
HIT = 0.5               # a case is a hit when its facts score at least this share (a paired verdict's unit)
ANSWER_CHARS = 14000
ASK_USD = {"claude": 0.5}   # per grader call; any other provider 5.0
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["facts"], "properties": {"facts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["n", "verdict", "why"],
    "properties": {"n": {"type": "integer"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
                   "why": {"type": "string"}}}}}}
PROMPT = """You grade one answer by fact match. Below are the question's title, the answer a model wrote, and {k} numbered facts
written from the project's raw records. For each fact give a verdict:
- match: the answer states the fact, or something that clearly means the same, including the key numbers or names.
- partial: the answer states part of it (right direction, wrong or missing number, or only some of the elements).
- miss: the answer does not state it.
- contradicted: the answer states the opposite or a clearly different value.
Judge only whether the answer says it. Do not reward style or length and do not use outside knowledge to fill gaps.
Return one entry per fact, `why` at most one short sentence.

## Question title
{title}

## Answer
{answer}

## Facts
{facts}
"""


class FactsError(ValueError):
    """A case file that cannot be graded against (broken, changed since it was frozen, never frozen)."""


def home() -> Path:
    return benchreplay.home() / "facts"


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hash_file(path: Path) -> Path:
    return Path(path).with_suffix(".sha256")


def load(path: Path) -> dict[str, dict]:
    """The cases by key. Every case needs a key, and 2 to 4 non-empty facts; a broken file raises FactsError."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise FactsError(f"{path}: cannot read the case file: {e}") from e
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise FactsError(f"{path}: no \"cases\" list")
    got: dict[str, dict] = {}
    for c in cases:
        key = str((c or {}).get("key") or "")
        facts = c.get("facts") if key else None
        if not key or not isinstance(facts, list) or not MIN_FACTS <= len(facts) <= MAX_FACTS \
                or not all(isinstance(f, dict) and str(f.get("text") or "").strip() for f in facts):
            raise FactsError(f"{path}: case {key or '?'}: needs a key and {MIN_FACTS}-{MAX_FACTS} facts, each with text")
        if key in got:
            raise FactsError(f"{path}: case {key} appears twice")
        got[key] = c
    return got


def freeze(path: Path) -> str:
    """Record the file's sha256 beside it, once. A file that already has a different recorded hash is refused:
    the facts never change after a model has been graded against them."""
    load(path)
    d, rec = digest(path), hash_file(path)
    if rec.exists():
        if rec.read_text().split()[0:1] != [d]:
            raise FactsError(f"{path} changed since it was frozen ({rec.name}); grading refuses it")
        return d
    durable_write(rec, f"{d}  {Path(path).name}\n")
    return d


def verify(path: Path) -> dict[str, dict]:
    """The cases, only when the file matches its recorded hash."""
    rec = hash_file(path)
    if not rec.exists():
        raise FactsError(f"{path} is not frozen: run `ttp bench facts --freeze --set {path}` before any model grades it")
    if rec.read_text().split()[0:1] != [digest(path)]:
        raise FactsError(f"{path} changed since it was frozen ({rec.name}): grading refused")
    return load(path)


def graders(cfg: dict | None) -> dict[str, tuple[str, str, str]]:
    """{name: (provider, model, effort)}: Opus high and the open model high, or bench.facts.graders."""
    set_ = (((cfg or {}).get("bench") or {}).get("facts") or {}).get("graders")
    if isinstance(set_, dict) and set_:
        got = {str(n): (str(g.get("provider") or ""), str(g.get("model") or ""), str(g.get("effort") or "high"))
               for n, g in set_.items() if isinstance(g, dict)}
        if len(got) >= 2 and all(p and m for p, m, _ in got.values()):
            return got
    model = benchdaily.GLM_ARM.split("=", 1)[1].split(":")[1]
    return {"opus": ("claude", "opus", "high"), "glm": ("chat", model, "high")}


def prompt(title: str, answer: str, facts: list[dict]) -> str:
    return PROMPT.format(k=len(facts), title=title, answer=benchreplay.clip(answer, ANSWER_CHARS),
                         facts="\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1)))


def no_answer(answer: str) -> bool:
    return not answer.strip() or answer.lstrip().startswith("(no answer")


def verdicts_of(got: dict, k: int) -> list[str]:
    """A grader's answer as k verdicts; a fact it left out is a miss."""
    by = {int(x["n"]): x["verdict"] for x in (got or {}).get("facts") or [] if isinstance(x, dict) and "n" in x}
    return [by.get(i, "miss") if by.get(i) in VERDICTS else "miss" for i in range(1, k + 1)]


def combine(per_grader: dict[str, list[str]]) -> tuple[list[str], list[int]]:
    """(the lower verdict per fact, the 1-based facts the graders judged differently)."""
    lists = list(per_grader.values())
    k = len(lists[0])
    return ([min((v[i] for v in lists), key=VERDICTS.index) for i in range(k)],
            [i + 1 for i in range(k) if len({v[i] for v in lists}) > 1])


def score(verdicts: list[str]) -> float:
    return sum(SCORE[v] for v in verdicts) / len(verdicts) if verdicts else 0.0


def grade_run(run: Path, cases: dict[str, dict], cfg: dict | None, asks: dict | None = None, parallel: int = 4,
              progress=None) -> dict:
    """Grade every answer of a replay run folder (results.jsonl, cells/, bench/<task>/workspace/QUESTION.md)
    against the frozen cases. Resumable: grades/<grader>/<task>-<arm>.json are kept. `asks`: {grader: ask(text)}
    (tests; default the real graders). Returns {"cells": [...], "not_run": [...], "ungraded": [...], "spend": {}}."""
    say = progress or (lambda s: print(s, file=sys.stderr, flush=True))
    rows = benchdaily.read_rows(run / "results.jsonl")
    by_task = {benchreplay.slug(c): c for c in cases.values()}
    specs = graders(cfg)
    spend: dict = {g: {} for g in specs}
    if asks is None:
        asks = {g: benchblind.provider_ask(p, m, e, ASK_USD.get(p, 5.0), cfg, spend[g], schema=SCHEMA)
                for g, (p, m, e) in specs.items()}
    items, not_run = [], []
    for (tid, rep, arm), r in sorted(rows.items(), key=lambda kv: str(kv[0])):
        case = by_task.get(tid)
        if case is None or rep != 0:
            continue
        if benchnotrun.is_not_run(r) or r.get("endpoint_fault"):
            not_run.append({"task": tid, "key": case["key"], "arm": arm,
                            "why": r.get("not_run_reason") or "; ".join(r.get("server_reason") or []) or "not run"})
        else:
            items.append((tid, arm, case))

    def one(item) -> dict:
        tid, arm, case = item
        keep = run / "cells" / f"{tid}-r0-{arm}"
        answer = benchreplay.answer_of(keep)
        k = len(case["facts"])
        rec = {"task": tid, "key": case["key"], "arm": arm, "empty": no_answer(answer), "graders": {}}
        if rec["empty"]:   # a model ran and wrote nothing: failed, every fact missed, no grader call
            rec["graders"] = {g: ["miss"] * k for g in specs}
            return rec
        q = run / "bench" / tid / "workspace" / "QUESTION.md"
        title = (q.read_text(errors="replace").splitlines() or [""])[0] if q.exists() else case["key"]
        text = prompt(title, answer, case["facts"])
        for g in specs:
            f = run / "grades" / g / f"{tid}-{arm}.json"
            if f.exists():
                try:
                    rec["graders"][g] = json.loads(f.read_text())["verdicts"]
                    continue
                except (OSError, ValueError, KeyError):
                    pass
            try:
                got = asks[g](text)
            except Exception as e:   # a grader that failed leaves the cell ungraded, never a guessed verdict
                rec.setdefault("errors", {})[g] = str(e)[:150]
                continue
            rec["graders"][g] = v = verdicts_of(got, k)
            f.parent.mkdir(parents=True, exist_ok=True)
            durable_write(f, json.dumps({"task": tid, "key": case["key"], "arm": arm, "grader": g, "verdicts": v,
                                         "why": got.get("facts")}, indent=1) + "\n")
            say(f"bench facts: {g} {tid} {arm} {v}")
        return rec

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        recs = list(pool.map(one, items))
    cells, ungraded = [], []
    for rec in recs:
        if len(rec["graders"]) < len(specs):
            ungraded.append({"task": rec["task"], "arm": rec["arm"], "why": rec.get("errors") or "a grader gave no answer"})
            continue
        rec["verdicts"], rec["disagree"] = combine(rec["graders"])
        rec["score"] = round(score(rec["verdicts"]), 4)
        rec["hit"] = rec["score"] >= HIT
        rec["scores"] = {g: round(score(v), 4) for g, v in rec["graders"].items()}
        cells.append(rec)
    return {"cells": cells, "not_run": not_run, "ungraded": ungraded, "graders": {g: list(s) for g, s in specs.items()},
            "spend": {g: s for g, s in spend.items() if s}}


def summary(res: dict, cases: dict[str, dict]) -> dict:
    """Per arm: cells graded, not run (its own count), fact verdict counts, mean fact-match score (with each
    grader's), hits with their Wilson 95%, and the paired verdict of every arm pair on the cases they share."""
    arms: dict[str, dict] = {}
    for c in res["cells"]:
        a = arms.setdefault(c["arm"], {"cells": [], "not_run": 0, "empty": 0})
        a["cells"].append(c)
        a["empty"] += bool(c["empty"])
    for n in res["not_run"]:
        arms.setdefault(n["arm"], {"cells": [], "not_run": 0, "empty": 0})["not_run"] += 1
    out: dict = {"arms": {}, "pairs": []}
    for arm, a in sorted(arms.items()):
        cells = a["cells"]
        counts = {v: sum(x["verdicts"].count(v) for x in cells) for v in VERDICTS}
        scores = {g: round(sum(x["scores"][g] for x in cells) / len(cells), 4) if cells else None for g in res["graders"]}
        out["arms"][arm] = {
            "n": len(cells), "not_run": a["not_run"], "empty": a["empty"], "facts": counts,
            "score": round(sum(x["score"] for x in cells) / len(cells), 4) if cells else None, "by_grader": scores,
            "hits": benchv2.pass_rate([{"task": x["key"], "hit": x["hit"]} for x in cells], "hit"),
            "disagreements": sum(len(x["disagree"]) for x in cells)}
    names = sorted(out["arms"])
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            rows = {arm: [{"task": c["key"], "hit": c["hit"]} for c in res["cells"] if c["arm"] == arm] for arm in (x, y)}
            out["pairs"].append({"a": x, "b": y, "paired": (d := benchv2.paired(rows[x], rows[y], "hit")),
                                 "verdict": benchv2.verdict(d, x, y)})
    out["disagreements"] = [{"task": c["task"], "key": c["key"], "arm": c["arm"], "fact": n, "text": cases[c["key"]]["facts"][n - 1]["text"],
                             "graders": {g: v[n - 1] for g, v in c["graders"].items()}}
                            for c in res["cells"] for n in c["disagree"]]
    return out


def table_md(res: dict, summ: dict, names: dict[str, str] | None = None) -> str:
    names = names or {}
    gs = list(res["graders"])
    head = ["arm", "answers graded", "not run", "empty answers", "facts match", "partial", "miss", "contradicted",
            "fact-match score", *[f"score by {g}" for g in gs], "cases hit (95%)", "grader disagreements"]
    lines = ["# Replay fact-match grading", "",
             f"Graders: {', '.join(f'{g} ({p}:{m}:{e})' for g, (p, m, e) in res['graders'].items())}; the fact "
             "verdict counted is the lower of the two. A case is a hit when its facts score at least "
             f"{HIT:.0%} (match 1, partial 0.5). Not-run cells (no model ran) are their own count, outside every rate; "
             "an empty answer from a model that ran is graded and fails.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for arm, s in summ["arms"].items():
        h = s["hits"]
        rate = f"{h['passed']}/{h['n']} ({h['low']:.2f}-{h['high']:.2f})" if h["n"] else "-"
        sc = lambda v: "-" if v is None else f"{v:.2f}"
        lines.append("| " + " | ".join([names.get(arm, arm), str(s["n"]), str(s["not_run"]), str(s["empty"]),
                                        *[str(s["facts"][v]) for v in ("match", "partial", "miss", "contradicted")],
                                        sc(s["score"]), *[sc(s["by_grader"][g]) for g in gs], rate,
                                        str(s["disagreements"])]) + " |")
    if summ["pairs"]:
        lines += ["", "## Pairs (cases both arms answered)", ""] + [f"- {p['verdict']}" for p in summ["pairs"]]
    if res["not_run"]:
        lines += ["", "## Not run (no model ran)", ""] + [f"- {n['key']} / {names.get(n['arm'], n['arm'])}: {n['why']}"
                                                          for n in res["not_run"]]
    if res["ungraded"]:
        lines += ["", "## Ungraded (a grader gave no answer)", ""] + [f"- {u['task']} / {names.get(u['arm'], u['arm'])}: {u['why']}"
                                                                      for u in res["ungraded"]]
    if summ["disagreements"]:
        lines += ["", "## Grader disagreements (flagged)", ""] + [
            f"- {d['key']} / {names.get(d['arm'], d['arm'])}, fact {d['fact']}: "
            + ", ".join(f"{g} {v}" for g, v in d["graders"].items()) + f" - {d['text'][:140]}" for d in summ["disagreements"]]
    return "\n".join(lines) + "\n"


def run(cases_path: Path, run_dir: Path, cfg: dict | None, out: Path | None = None, asks: dict | None = None,
        parallel: int = 4, names: dict | None = None, progress=None) -> dict:
    """Verify the frozen cases, grade the run folder and write facts.json and facts.md (in `out`, default the run
    folder's parent). Returns the result with its summary."""
    cases = verify(cases_path)
    res = grade_run(run_dir, cases, cfg, asks, parallel, progress)
    res["summary"] = summ = summary(res, cases)
    res["cases_sha256"] = digest(cases_path)
    out = out or run_dir.parent
    durable_write(out / "facts.json", json.dumps(res, indent=1) + "\n")
    durable_write(out / "facts.md", table_md(res, summ, names))
    return res
