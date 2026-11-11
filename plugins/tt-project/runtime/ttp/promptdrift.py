"""Worker prompt drift: a project's own harness/prompts/worker.md against the plugin template's.

A project's copy grows with its own rules and keeps sections the template has since dropped; every
worker run pays for each byte. This check, without a model call, flags a copy that is much larger
than the template (coordinator.prompt_drift_ratio, default 1.5; 0: off), lacks a `## ` section the
template has, or still has a section the template dropped. `line` feeds `ttp doctor` and the daily
review; `check` (daemon tick) queues one low event to the project's own coordinator per changed copy,
suggesting it merge its copy toward the template. The harness never edits a project's prompts: each
coordinator owns its own (user msg #1653)."""
from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from . import gitmemo
from .project import Project

KEY = "worker_prompt_drift"   # kv: {"stat", "hash": the copy's last checked hash, "flagged": its last flagged hash}
RATIO = 1.5
EVERY_S = 3600   # the daemon looks at most this often


def _git_show(harness: Path, *args: str) -> str | None:
    try:
        r = gitmemo.run(["git", "-C", str(harness), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def template_text(p: Project) -> str | None:
    """The template worker.md the project last upgraded to (its harness's `upstream` snapshot), else the
    installed release's, else this runtime's own plugin copy."""
    text = _git_show(p.harness, "show", "upstream:prompts/worker.md")
    if text:
        return text
    from . import release
    for path in (release.installed() / "template" / "prompts" / "worker.md",
                 Path(__file__).resolve().parents[2] / "template" / "prompts" / "worker.md"):
        try:
            return path.read_text()
        except OSError:
            continue
    return None


def headings(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.startswith("## ")]


def dropped(p: Project, template: str) -> set[str]:
    """`## ` sections the template once had (its upstream snapshots' history) and has since dropped."""
    log = _git_show(p.harness, "log", "-p", "--format=", "upstream", "--", "prompts/worker.md") or ""
    ever = {x[1:].strip() for x in log.splitlines() if x.startswith("-## ") or x.startswith("+## ")}
    return ever - set(headings(template))


def ratio_setting(p: Project) -> float:
    try:
        return float((p.config().get("coordinator") or {}).get("prompt_drift_ratio", RATIO))
    except (TypeError, ValueError):
        return RATIO


def report(p: Project, copy: str | None = None) -> dict | None:
    """{size, template, ratio, limit, big, missing, stale}, or None without a copy or a template."""
    if copy is None:
        try:
            copy = (p.harness / "prompts" / "worker.md").read_text()
        except OSError:
            return None
    template = template_text(p)
    if not template:
        return None
    size, base = len(copy.encode()), len(template.encode())
    limit = ratio_setting(p)
    own = set(headings(copy))
    return {"size": size, "template": base, "ratio": round(size / base, 2) if base else 0.0, "limit": limit,
            "big": bool(limit > 0 and base and size > limit * base),
            "missing": [h for h in headings(template) if h not in own],
            "stale": sorted(own & dropped(p, template))}


def flagged(r: dict | None) -> bool:
    return bool(r and (r["big"] or r["missing"] or r["stale"]))


def line(p: Project, r: dict | None = None) -> str:
    """One line for doctor and the daily review; "" when the copy is fine."""
    r = r if r is not None else report(p)
    if not flagged(r):
        return ""
    why = []
    if r["big"]:
        why.append(f"{r['size'] // 1024} KB, {r['ratio']}x the template's {r['template'] // 1024} KB "
                   f"(limit {r['limit']}x)")
    if r["missing"]:
        why.append("lacks the template's " + ", ".join(f"'{h}'" for h in r["missing"]))
    if r["stale"]:
        why.append("keeps sections the template dropped: " + ", ".join(f"'{h}'" for h in r["stale"]))
    return ("worker prompt: harness/prompts/worker.md " + "; ".join(why)
            + ". Merge it toward the template (the harness's upstream branch): every worker run pays for each byte.")


def check(p: Project, now: float | None = None) -> str:
    """Daemon tick step: one queued low event per changed copy that is flagged. Returns its text or ""."""
    now = time.time() if now is None else now
    path = p.harness / "prompts" / "worker.md"
    state = p.db.kv(KEY) or {}
    try:
        st = path.stat()
    except OSError:
        return ""
    stat = [st.st_mtime_ns, st.st_size]
    if state.get("stat") == stat and now - state.get("at", 0) < EVERY_S:
        return ""
    copy = path.read_text()
    digest = hashlib.sha256(copy.encode()).hexdigest()
    state.update(stat=stat, at=now, hash=digest)
    text = ""
    if state.get("flagged") != digest:
        text = line(p, report(p, copy))
        if text:
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                   (now, "harness", "worker_prompt_drift", "low",
                    text + " Your coordinator owns this copy: fold project rules that every task needs into "
                    "short lines, move the rest to memory or the charter, drop sections the template dropped. "
                    "The harness never edits it. Not repeated while the copy is unchanged.", "queued"))
            state["flagged"] = digest
    p.db.set_kv(KEY, state)
    return text
