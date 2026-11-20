"""Blind review for a model comparison (replay bench): each cell's arm diffs are anonymised and
shuffled with a recorded seed, a reviewer scores each against the task's acceptance criteria on a
fixed rubric, and the arm labels are revealed only once every score of the batch is recorded.

    python3 -m ttp.benchblind prep <cells.json | `ttp bench run` folder>... --out <dir> [--seed N]
    (or `ttp bench blind <the same words>`)
    python3 -m ttp.benchblind review <dir> [--provider claude] [--model M] [--effort E] [--budget-usd 2]
    python3 -m ttp.benchblind score <dir> <item> '<{"scores": {...}, "notes": "..."}>'
    python3 -m ttp.benchblind reveal <dir>
    python3 -m ttp.benchblind regrade <dir> --out <dir2>     (same masked items, seal and rubric, no scores: a second reviewer)
    python3 -m ttp.benchblind compare <dir> <dir2>           (two revealed batches: per-arm scores under both, ranking change)

prep with several folders merges their cells per task into one batch (each arm label is unique across them), so
arms from different runs are graded together blind. cells.json: {"cells": [{"task": "<id>", "criteria": "<acceptance criteria>", "arms": {"<label>":
{"diff": "<unified diff>", "<any metadata: model, provider, effort, run_id, transcript, wall_s>": ...}}}]}

Anonymising drops commit headers, trailers and blob ids, masks model and provider names, effort
settings, run ids, session ids, timestamps, elapsed times and transcript paths, and every metadata
value of every arm in the batch; prep refuses a batch where one still shows. `<dir>/batch.json`
holds only the anonymised items, the rubric and the scores; the seed and labels are sealed in
`<dir>/seal.json` (mode 0600), pinned by its sha256 in batch.json. `reveal` checks the seal,
recomputes the shuffle from the seed, writes the labels, seed and per-arm totals into batch.json
and closes it: a score after that is refused. Model-free except `review`."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .project import durable_write
from .benchrun import _sha

RUBRIC = (   # (name, max, what earns it): fixed, so scores of every batch compare
    ("criteria", 4, "does what the acceptance criteria ask, each point"),
    ("correctness", 4, "no bugs: edge cases, error paths and concurrency handled"),
    ("tests", 2, "tests cover the change and would fail without it"),
    ("scope", 2, "minimal and on-topic, readable, matches the surrounding code"),
)
ANSWER_RUBRIC = (   # an answer reviewed against what the project later established (the offline replay)
    ("answered", 5, "answers what the question asked, directly and completely"),
    ("supported", 5, "each conclusion follows from the evidence the question gave"),
    ("agrees", 5, "agrees with the established answer: same cause or verdict, no contradicting claim"),
)


def schema_for(rubric=RUBRIC) -> dict:
    """The reviewer's answer schema for a rubric of (name, max, what) rows (the default rubric's, else a batch's own);
    scores start at the rubric's floor: 0, or 1 for the answer rubric's 1-5 scale."""
    low = 1 if tuple(map(tuple, rubric)) == ANSWER_RUBRIC else 0
    return {"type": "object", "required": ["scores", "notes"], "properties": {
        "scores": {"type": "object", "required": [r[0] for r in rubric],
                   "properties": {r[0]: {"type": "integer", "minimum": low, "maximum": r[1]} for r in rubric}},
        "notes": {"type": "string"}}}


SCHEMA = schema_for()
MASK = "[x]"
_GENERIC = [   # (pattern, replacement): run metadata a diff or its commit can carry
    (r"(?im)^(from [0-9a-f]{7,40}\b|from:|date:|subject:|commit [0-9a-f]{7,40}\b|author:|authordate:"
     r"|commit:|commitdate:|merge:).*\n?", ""),
    (r"(?im)^.*\b(co-authored-by|signed-off-by|generated with|authored by ai|reviewed-by)\b.*\n?", ""),
    (r"(?m)^index [0-9a-f]+\.\.[0-9a-f]+.*\n", ""),
    (r"(?i)\b(anthropic|openai|zhipu|moonshot|claude|opus|sonnet|haiku|fable|glm|qwen|gpt|codex"
     r"|gemini|llama|deepseek|kimi|mistral)[\w.\-]*", MASK),
    (r"(?i)(--)?\b(reasoning[_ ]effort|effort)\b\s*[:=]?\s*\"?(low|medium|high|xhigh|max)\b", MASK),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", MASK),
    (r"(?i)[\w./~-]*(\.jsonl\b|transcripts?/)[\w./-]*", MASK),
    (r"(?i)\b(runs?|run_id)[/ #:=_-]*\d{2,}\b", MASK),
    (r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)?", MASK),
    (r"(?i)\b(wall|elapsed|took|duration|wall_s|cost|spent)\b\s*[:=]?\s*\$?\d+(\.\d+)?\s*(ms|s|sec|min|h|usd)?\b", MASK),
]


_WORDS = frozenset({"low", "medium", "high", "max"})   # effort values masked only beside "effort"


class Refused(Exception):
    """A write the batch's phase forbids, or a leak prep will not seal."""


def arm_tokens(cells: list[dict]) -> list[str]:
    """Every arm label and metadata value of the batch: the literal tokens no item may show."""
    def flat(v):   # the values of nested metadata too, never its keys
        if isinstance(v, (dict, list, tuple)):
            return [x for y in (v.values() if isinstance(v, dict) else v) for x in flat(y)]
        return [] if v is None else [v]
    out = set()
    for c in cells:
        for label, arm in (c.get("arms") or {}).items():
            vals = [label, *(x for k, v in arm.items() if k != "diff" for x in flat(v))]
            for v in vals:
                s = str(v).strip()
                if len(s) >= (3 if s.isdigit() else 2) and s.lower() not in _WORDS:
                    out.add(s)
    return sorted(out, key=len, reverse=True)


def anonymise(diff: str, tokens: list[str]) -> str:
    for pat, rep in _GENERIC:
        diff = re.sub(pat, rep, diff)
    for t in tokens:
        diff = re.sub(rf"(?i)(?<![\w]){re.escape(t)}(?![\w])", MASK, diff)
    return diff


def leaks(text: str, tokens: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in tokens if re.search(rf"(?<![\w]){re.escape(t.lower())}(?![\w])", low)]


def shuffle(cells: list[dict], seed: int) -> list[dict]:
    """The presented order and the sealed labels, reproducible from the seed alone: per cell the
    arms (sorted by label, then shuffled) take letters A, B, ..., and the items are shuffled."""
    rng = random.Random(seed)
    items = []
    for n, c in enumerate(sorted(cells, key=lambda c: str(c["task"])), 1):
        labels = sorted(c["arms"])
        rng.shuffle(labels)
        items += [{"id": f"{n}{chr(65 + i)}", "task": str(c["task"]), "label": lab} for i, lab in enumerate(labels)]
    rng.shuffle(items)
    return items


def merge_cells(lists: list[list[dict]]) -> list[dict]:
    """One cell list from several runs: cells of one task join their arms (criteria and reference from the first
    that has them); an arm label named twice for one task is refused, so rename an arm before merging."""
    by_task: dict = {}
    for cells in lists:
        for c in cells:
            t = str(c["task"])
            got = by_task.setdefault(t, {**c, "arms": {}})
            for k in ("criteria", "reference"):
                if not got.get(k) and c.get(k):
                    got[k] = c[k]
            for label, arm in c["arms"].items():
                if label in got["arms"]:
                    raise Refused(f"arm {label!r} of task {t} is in two of the folders; rename one")
                got["arms"][label] = arm
    return list(by_task.values())


def prep(cells: list[dict], out: Path, seed: int | None = None, rubric=RUBRIC) -> dict:
    """Seal a batch. `rubric`: the rows (name, max, what) its reviewer scores; a cell's `reference` (the
    established answer, which names no arm) goes to the reviewer with its item."""
    if out.exists():
        raise Refused(f"{out} exists; name a new folder")
    seen: set = set()
    dup = sorted({t for t in (str(c["task"]) for c in cells) if t in seen or seen.add(t)})
    if dup:   # cells are keyed by task: repeats would show one cell's diffs twice and never reveal
        raise Refused(f"task id(s) {', '.join(dup[:10])} name more than one cell; give each repeat its own task id")
    seed = secrets.randbits(63) if seed is None else int(seed)
    tokens = arm_tokens(cells)
    by_task = {str(c["task"]): c for c in cells}
    items, order = [], shuffle(cells, seed)
    for it in order:
        c = by_task[it["task"]]
        diff = anonymise(str(c["arms"][it["label"]].get("diff") or ""), tokens)
        bad = leaks(diff, tokens)
        if bad:
            raise Refused(f"item {it['id']} still shows {len(bad)} arm token(s) after anonymising")
        items.append({"id": it["id"], "task": it["task"], "criteria": str(c.get("criteria") or ""),
                      **({"reference": str(c["reference"])} if c.get("reference") else {}),
                      "diff": diff, "sha256": _sha(diff.encode())})
    seal = json.dumps({"seed": seed, "labels": {it["id"]: it["label"] for it in order}},
                      sort_keys=True)
    out.mkdir(parents=True)
    durable_write(out / "seal.json", seal, mode=0o600)
    batch = {"phase": "scoring", "rubric": [list(r) for r in rubric], "seal_sha256": _sha(seal.encode()),
             "items": items, "scores": {}}
    durable_write(out / "batch.json", json.dumps(batch, indent=1))
    return batch


@contextmanager
def _locked(d: Path):
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield json.loads((d / "batch.json").read_text())
    finally:
        os.close(fd)


def _check(got: dict, rubric=RUBRIC) -> dict:
    s = got.get("scores") if isinstance(got, dict) else None
    if not isinstance(s, dict) or set(s) != {r[0] for r in rubric}:
        raise Refused(f"scores must name exactly {[r[0] for r in rubric]}")
    low = 1 if tuple(map(tuple, rubric)) == ANSWER_RUBRIC else 0
    for n, m, *_ in rubric:
        if not isinstance(s[n], int) or isinstance(s[n], bool) or not low <= s[n] <= m:
            raise Refused(f"{n} must be an integer {low}..{m}")
    return {"scores": dict(s), "total": sum(s.values()), "notes": str(got.get("notes") or "")[:2000]}


def score(d: Path, item: str, got: dict) -> dict:
    """Record one item's score; refused once revealed, for an unknown item or a second score."""
    with _locked(d) as b:
        rec = _check(got, b.get("rubric") or RUBRIC)
        if b["phase"] != "scoring":
            raise Refused("labels are revealed: the batch takes no more scores")
        if item not in {i["id"] for i in b["items"]}:
            raise Refused(f"no item {item}")
        if item in b["scores"]:
            raise Refused(f"item {item} is already scored")
        b["scores"][item] = rec
        durable_write(d / "batch.json", json.dumps(b, indent=1))
    return rec


def reveal(d: Path) -> dict:
    with _locked(d) as b:
        if b["phase"] != "scoring":
            raise Refused("already revealed")
        missing = [i["id"] for i in b["items"] if i["id"] not in b["scores"]]
        if missing:
            raise Refused(f"{len(missing)} item(s) unscored: {', '.join(missing[:10])}")
        raw = (d / "seal.json").read_bytes()
        if _sha(raw) != b["seal_sha256"]:
            raise Refused("seal.json does not match its pinned sha256")
        seal = json.loads(raw)
        cells: dict = {}
        for i in b["items"]:
            cells.setdefault(i["task"], {"task": i["task"], "arms": {}})["arms"][seal["labels"][i["id"]]] = {}
        again = {it["id"]: it["label"] for it in shuffle(list(cells.values()), seal["seed"])}
        if again != seal["labels"]:
            raise Refused("the sealed labels do not reproduce from the sealed seed")
        arms: dict = {}
        for i in b["items"]:
            a = arms.setdefault(seal["labels"][i["id"]], {"items": 0, "total": 0})
            a["items"] += 1
            a["total"] += b["scores"][i["id"]]["total"]
        b.update(phase="revealed", seed=seal["seed"], labels=seal["labels"], arms=arms)
        durable_write(d / "batch.json", json.dumps(b, indent=1))
    return b


def regrade(src: Path, out: Path) -> dict:
    """A second, independent grading of the same batch: `out` gets src's masked items, rubric and seal (so the
    labels and shuffle are the same) with no scores, for another reviewer to score and reveal. Refused for a
    folder that exists or a seal that does not match its pin."""
    if out.exists():
        raise Refused(f"{out} exists; name a new folder")
    b = json.loads((src / "batch.json").read_text())
    raw = (src / "seal.json").read_bytes()
    if _sha(raw) != b["seal_sha256"]:
        raise Refused("seal.json does not match its pinned sha256")
    out.mkdir(parents=True)
    durable_write(out / "seal.json", raw, mode=0o600)
    nb = {"phase": "scoring", "rubric": b["rubric"], "seal_sha256": b["seal_sha256"], "items": b["items"], "scores": {}}
    durable_write(out / "batch.json", json.dumps(nb, indent=1))
    return nb


def landed_by_score(rubric, scores: dict) -> bool:
    """The score-only verdict: criteria and correctness at 3 or more of 4 (an answer: agrees at 4 or more of 5)."""
    if tuple(map(tuple, rubric)) == ANSWER_RUBRIC:
        return int(scores.get("agrees", 0)) >= 4
    return int(scores.get("criteria", 0)) >= 3 and int(scores.get("correctness", 0)) >= 3


def compare(a: Path, b: Path) -> dict:
    """Two revealed gradings of one batch: per arm the items, mean total and landed count under each, the
    ranking (by mean total, ties by landed) under each, and whether the order of the arms changed."""
    ba, bb = (json.loads((d / "batch.json").read_text()) for d in (a, b))
    for x in (ba, bb):
        if x["phase"] != "revealed":
            raise Refused("both batches must be revealed")
    if ba["labels"] != bb["labels"] or [i["sha256"] for i in ba["items"]] != [i["sha256"] for i in bb["items"]]:
        raise Refused("the two batches are not the same items and seal")
    rub = ba.get("rubric") or RUBRIC
    arms: dict = {}
    for i in ba["items"]:
        arm = ba["labels"][i["id"]]
        r = arms.setdefault(arm, {"items": 0, "total_a": 0, "total_b": 0, "landed_a": 0, "landed_b": 0})
        r["items"] += 1
        for k, x in (("a", ba), ("b", bb)):
            sc = x["scores"][i["id"]]
            r[f"total_{k}"] += sc["total"]
            r[f"landed_{k}"] += landed_by_score(rub, sc["scores"])
    for r in arms.values():
        r["mean_a"], r["mean_b"] = (round(r[f"total_{k}"] / r["items"], 2) for k in ("a", "b"))

    def rank(k: str) -> list[str]:
        return sorted(arms, key=lambda n: (-arms[n][f"mean_{k}"], -arms[n][f"landed_{k}"], n))
    ra, rb = rank("a"), rank("b")
    return {"arms": arms, "rank_a": ra, "rank_b": rb, "ranking_changed": ra != rb}


def prompt(item: dict, rubric=RUBRIC) -> str:
    """The reviewer's prompt for one item. Criteria marked not checkable (benchrun.mark_live: they need
    the live system the sealed replay lacks) are named as not to be scored. An item with a `reference`
    (the established answer) is an answer reviewed against it."""
    from .benchrun import NOT_CHECKABLE
    if item.get("reference"):
        scale = "\n".join(f"- {r[0]} (1-{r[1]}): {r[2]}" for r in rubric)
        return (f"Score this answer to a question. You see only the answer; who or what wrote it is hidden and "
                f"does not matter. 5 is best, 1 worst. The established answer is what the project later "
                f"found out for certain; the answer under review did not see it.\n\nRubric (integers):\n{scale}"
                f"\n\nQuestion:\n{item['criteria']}\n\nEstablished answer:\n{item['reference']}\n\n"
                f"Answer under review:\n```\n{item['diff']}\n```\n\n"
                f"Answer as JSON: {{\"scores\": {{...}}, \"notes\": \"one short paragraph\"}}.")
    rubric = "\n".join(f"- {r[0]} (0-{r[1]}): {r[2]}" for r in rubric)
    skip = (f" Criteria marked {NOT_CHECKABLE} need the live system, which the replay that made this change "
            f"lacked: do not score them, and do not count them as missed; score the rest."
            if NOT_CHECKABLE in item["criteria"] else "")
    return (f"Score this change against its task's acceptance criteria. You see only the change; who or "
            f"what wrote it is hidden and does not matter.{skip}\n\nRubric (integers):\n{rubric}\n\n"
            f"Acceptance criteria:\n{item['criteria']}\n\nChange:\n```diff\n{item['diff']}\n```\n\n"
            f"Answer as JSON: {{\"scores\": {{...}}, \"notes\": \"one short paragraph\"}}.")


# A grader that can run commands (an agent CLI without a no-exec mode) gets the blind flag: its `ttp` then
# refuses every command that could show which model made an arm (blindreview).
BLIND_ENV = {"TTP_BLIND": "1"}


def provider_ask(provider: str, model: str, effort: str, budget_usd: float, cfg: dict | None = None,
                 spend: dict | None = None, bridge_env: dict | None = None, schema: dict | None = None):
    """A decision-only run on the named provider (no tools, schema-checked answer) per prompt. A
    chat provider's reviewer runs through the chat bridge this wrapper starts, like a chat run's
    own agent: the CLI asks Anthropic otherwise, and the open model's id never reaches it. `spend`:
    a dict the caller owns, filled per call with the reviewer's measured cost (calls, cost_usd,
    cost_estimated) so a suite can report its review spend."""
    from .blindreview import cap_effort
    from .providers import get_provider
    prov = get_provider(provider).use(model)
    effort = cap_effort(effort) if effort else effort   # a grader never runs above high

    def note(u) -> None:
        if spend is not None:
            spend["calls"] = spend.get("calls", 0) + 1
            spend["cost_usd"] = round(spend.get("cost_usd", 0.0) + float(u.cost_usd or 0), 4)
            spend["cost_estimated"] = bool(u.estimated) or bool(spend.get("cost_estimated"))

    def ask(text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            argv, env = prov.build(role="review", model=model, effort=effort, cwd=tmp, budget_usd=budget_usd,
                                   read_only=True, schema=schema or SCHEMA, restrictions={})
            argv = prov.read_only_args(argv)   # decision-only: no restrictions, so no OS sandbox
            out = Path(tmp) / "output.jsonl"
            if provider == "chat":
                # A chat reviewer runs through the chat bridge: either one the caller holds open for
                # the whole review (bridge_env, its env dict), or one started per ask here.
                with (_constant(bridge_env) if bridge_env is not None else _bridge(cfg, model, effort)) as bridge:
                    r = subprocess.run(argv, input=text.encode(), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=tmp,
                                       env={**os.environ, **env, **bridge, **BLIND_ENV}, timeout=1800)
            else:
                r = subprocess.run(argv, input=text.encode(), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=tmp,
                                   env={**os.environ, **env, **BLIND_ENV}, timeout=1800)
            durable_write(out, r.stdout, sync=False)
            u = prov.parse(out)
            note(u)
        if not u.structured:
            raise Refused(f"no scored answer ({u.error or 'empty'})")
        return u.structured
    return ask


def sub_parser_review(sub):
    """The review subparser: a chat reviewer needs the project's settings, so the CLI's
    `bench blind review --config <project>` passes them through the words after the first two."""
    s = sub.add_parser("review")
    s.add_argument("dir")
    s.add_argument("--provider", default="claude")
    s.add_argument("--model", default="")
    s.add_argument("--effort", default="")
    s.add_argument("--budget-usd", type=float, default=2.0)
    s.add_argument("--config", default="")
    return s


def _review_cfg(argv: list[str]) -> dict:
    """The project config named by `--config <project-or-path>`, else {} (an Anthropic reviewer
    needs none). A name resolves through the project registry; a path is the project itself."""
    for i, w in enumerate(argv):
        if w == "--config" and i + 1 < len(argv):
            from .project import Project, here, load_registry
            given = argv[i + 1]
            base = None
            for candidate in (given, str(Path(given).resolve())):
                prj = Project(candidate)
                if prj.exists():
                    base = prj
                    break
            if base is None:
                for name, row in ((load_registry() or {}).get("projects") or {}).items():
                    if name == given or str(row.get("path", "")) == given:
                        base = Project(row["path"])
                        break
            if base is None and (found := here(given)):
                base = found
            if base is None or not base.exists():
                raise Refused(f"--config {given!r} names no project")
            return base.config()
    return {}


def _bridge(cfg: dict, model: str, effort: str):
    """The chat bridge env for an open-model reviewer: one Bridge for the whole review, from the
    project's providers.chat settings, with the run's env (ANTHROPIC_BASE_URL and its token) laid
    over the CLI's own. No settings (a null config): Bridge.from_settings refuses with the reason."""
    from .chatbridge import Bridge, settings
    br = Bridge.from_settings(settings(cfg or {}, "review", model, effort))
    br.start()
    return _closing(br)


@contextmanager
def _closing(br):
    try:
        yield br.env()
    finally:
        br.stop()


@contextmanager
def _constant(env: dict):
    yield env


def review(d: Path, ask) -> list[str]:
    """Score every unscored item through `ask(prompt) -> dict`; the reviewer sees each item's
    criteria and anonymised diff only. Returns the ids scored."""
    b = json.loads((d / "batch.json").read_text())
    done = []
    for it in b["items"]:
        if it["id"] not in b["scores"]:
            score(d, it["id"], ask(prompt(it, b.get("rubric") or RUBRIC)))
            done.append(it["id"])
    return done


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv   # the module form: _review_cfg reads it too
    ap = argparse.ArgumentParser(prog="python3 -m ttp.benchblind", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prep")
    s.add_argument("cells", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int)
    s = sub_parser_review(sub)
    s = sub.add_parser("score")
    s.add_argument("dir")
    s.add_argument("item")
    s.add_argument("json")
    s = sub.add_parser("reveal")
    s.add_argument("dir")
    s = sub.add_parser("regrade")
    s.add_argument("dir")
    s.add_argument("--out", required=True)
    s = sub.add_parser("compare")
    s.add_argument("dir")
    s.add_argument("dir2")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prep":
            srcs = [Path(c) / "cells.json" if Path(c).is_dir() else Path(c) for c in a.cells]   # `ttp bench run` folders
            b = prep(merge_cells([json.loads(x.read_text())["cells"] for x in srcs]), Path(a.out), a.seed)
            print(f"benchblind: {len(b['items'])} item(s) sealed in {a.out}")
        elif a.cmd == "review":
            rub = json.loads((Path(a.dir) / "batch.json").read_text()).get("rubric") or RUBRIC
            got = review(Path(a.dir), provider_ask(a.provider, a.model, a.effort, a.budget_usd, _review_cfg(argv),
                                                    schema=schema_for(rub)))
            print(f"benchblind: scored {len(got)} item(s)")
        elif a.cmd == "regrade":
            b = regrade(Path(a.dir), Path(a.out))
            print(f"benchblind: {len(b['items'])} item(s) copied unscored to {a.out}")
        elif a.cmd == "compare":
            print(json.dumps(compare(Path(a.dir), Path(a.dir2)), indent=1))
        elif a.cmd == "score":
            print(json.dumps(score(Path(a.dir), a.item, json.loads(a.json))))
        else:
            b = reveal(Path(a.dir))
            print(json.dumps({"seed": b["seed"], "arms": b["arms"]}))
    except (Refused, OSError, ValueError, KeyError) as e:   # a missing folder or a malformed file too
        print(f"benchblind: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
