"""Blind review for a model comparison (replay bench): each cell's arm diffs are anonymised and
shuffled with a recorded seed, a reviewer scores each against the task's acceptance criteria on a
fixed rubric, and the arm labels are revealed only once every score of the batch is recorded.

    python3 -m ttp.benchblind prep <cells.json | `ttp bench run` folder> --out <dir> [--seed N]
    (or `ttp bench blind <the same words>`)
    python3 -m ttp.benchblind review <dir> [--provider claude] [--model M] [--effort E] [--budget-usd 2]
    python3 -m ttp.benchblind score <dir> <item> '<{"scores": {...}, "notes": "..."}>'
    python3 -m ttp.benchblind reveal <dir>

cells.json: {"cells": [{"task": "<id>", "criteria": "<acceptance criteria>", "arms": {"<label>":
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
import hashlib
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

RUBRIC = (   # (name, max, what earns it): fixed, so scores of every batch compare
    ("criteria", 4, "does what the acceptance criteria ask, each point"),
    ("correctness", 4, "no bugs: edge cases, error paths and concurrency handled"),
    ("tests", 2, "tests cover the change and would fail without it"),
    ("scope", 2, "minimal and on-topic, readable, matches the surrounding code"),
)
SCHEMA = {"type": "object", "required": ["scores", "notes"], "properties": {
    "scores": {"type": "object", "required": [n for n, _, _ in RUBRIC],
               "properties": {n: {"type": "integer", "minimum": 0, "maximum": m} for n, m, _ in RUBRIC}},
    "notes": {"type": "string"}}}
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


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def prep(cells: list[dict], out: Path, seed: int | None = None) -> dict:
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
                      "diff": diff, "sha256": _sha(diff.encode())})
    seal = json.dumps({"seed": seed, "labels": {it["id"]: it["label"] for it in order}},
                      sort_keys=True)
    out.mkdir(parents=True)
    durable_write(out / "seal.json", seal, mode=0o600)
    batch = {"phase": "scoring", "rubric": [list(r) for r in RUBRIC], "seal_sha256": _sha(seal.encode()),
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


def _check(got: dict) -> dict:
    s = got.get("scores") if isinstance(got, dict) else None
    if not isinstance(s, dict) or set(s) != {n for n, _, _ in RUBRIC}:
        raise Refused(f"scores must name exactly {[n for n, _, _ in RUBRIC]}")
    for n, m, _ in RUBRIC:
        if not isinstance(s[n], int) or isinstance(s[n], bool) or not 0 <= s[n] <= m:
            raise Refused(f"{n} must be an integer 0..{m}")
    return {"scores": dict(s), "total": sum(s.values()), "notes": str(got.get("notes") or "")[:2000]}


def score(d: Path, item: str, got: dict) -> dict:
    """Record one item's score; refused once revealed, for an unknown item or a second score."""
    rec = _check(got)
    with _locked(d) as b:
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


def prompt(item: dict) -> str:
    rubric = "\n".join(f"- {n} (0-{m}): {what}" for n, m, what in RUBRIC)
    return (f"Score this change against its task's acceptance criteria. You see only the change; who or "
            f"what wrote it is hidden and does not matter.\n\nRubric (integers):\n{rubric}\n\n"
            f"Acceptance criteria:\n{item['criteria']}\n\nChange:\n```diff\n{item['diff']}\n```\n\n"
            f"Answer as JSON: {{\"scores\": {{...}}, \"notes\": \"one short paragraph\"}}.")


def provider_ask(provider: str, model: str, effort: str, budget_usd: float):
    """A decision-only run on the named provider (no tools, schema-checked answer) per prompt."""
    from .providers import get_provider
    prov = get_provider(provider).use(model)

    def ask(text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            argv, env = prov.build(role="review", model=model, effort=effort, cwd=tmp, budget_usd=budget_usd,
                                   read_only=True, schema=SCHEMA, restrictions={})
            argv = prov.read_only_args(argv)   # decision-only: unsandboxed, as the daemon's
            out = Path(tmp) / "output.jsonl"
            r = subprocess.run(argv, input=text.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               cwd=tmp, env={**os.environ, **env}, timeout=1800)
            durable_write(out, r.stdout, sync=False)
            u = prov.parse(out)
        if not u.structured:
            raise Refused(f"no scored answer ({u.error or 'empty'})")
        return u.structured
    return ask


def review(d: Path, ask) -> list[str]:
    """Score every unscored item through `ask(prompt) -> dict`; the reviewer sees each item's
    criteria and anonymised diff only. Returns the ids scored."""
    b = json.loads((d / "batch.json").read_text())
    done = []
    for it in b["items"]:
        if it["id"] not in b["scores"]:
            score(d, it["id"], ask(prompt(it)))
            done.append(it["id"])
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ttp.benchblind", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prep")
    s.add_argument("cells")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int)
    s = sub.add_parser("review")
    s.add_argument("dir")
    s.add_argument("--provider", default="claude")
    s.add_argument("--model", default="")
    s.add_argument("--effort", default="")
    s.add_argument("--budget-usd", type=float, default=2.0)
    s = sub.add_parser("score")
    s.add_argument("dir")
    s.add_argument("item")
    s.add_argument("json")
    s = sub.add_parser("reveal")
    s.add_argument("dir")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prep":
            src = Path(a.cells) / "cells.json" if Path(a.cells).is_dir() else Path(a.cells)   # a `ttp bench run` folder
            b = prep(json.loads(src.read_text())["cells"], Path(a.out), a.seed)
            print(f"benchblind: {len(b['items'])} item(s) sealed in {a.out}")
        elif a.cmd == "review":
            got = review(Path(a.dir), provider_ask(a.provider, a.model, a.effort, a.budget_usd))
            print(f"benchblind: scored {len(got)} item(s)")
        elif a.cmd == "score":
            print(json.dumps(score(Path(a.dir), a.item, json.loads(a.json))))
        else:
            b = reveal(Path(a.dir))
            print(json.dumps({"seed": b["seed"], "arms": b["arms"]}))
    except Refused as e:
        print(f"benchblind: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
