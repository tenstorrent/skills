"""End conditions on temporary instructions.

A memory entry or a charter section recorded from temporary words ("while X", "until Y", "for
now") carries an end: `expires` (a time), `until` (a plain-language condition) and/or
`until_probe` (a shell probe like `start_when`: exit 0 = over; 1, 75 or 255 = not yet). The daemon
retires one whose time passed or whose probe exits 0: memory moves to memory/archive/, a charter
section to CHARTER.history.md, and the user gets one low-severity alert. A condition only a model
can judge (`until` alone, or a broken probe) is listed in the digest as possibly over, at most once
a day, and the coordinator decides. Retirements reach the next digest in one line.

Memory keeps the end in its front matter (`expires:`, `until:`, `until_probe:`). A charter section
keeps it in trailing lines of its body (`Expires: `, `Until: `, `Until probe: `), so the user and
every prompt see it next to the text it ends."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project import Project, durable_append, durable_write

CHARTER_HISTORY = "CHARTER.history.md"   # harness file: charter sections retired or replaced
ENDED_KEY = "ended_instructions"   # kv: [{"at", "what", "why"}] retired by their end condition (last 7 days)
LISTED_KEY = "ends_listed"         # kv: {entry key: ts} a possibly-over entry was first seen or last listed
BROKEN_KEY = "ends_probe_broken"   # kv: {entry key: why} end probes that neither pass nor say "not yet"
RECHECK_S = 86400                  # a possibly-over entry is listed again after this long
CHECK_EVERY_S = 60                 # how often the daemon reads memory and charter for ends
PROBE_EVERY_S = 600
PROBE_TIMEOUT_S = 60
NOT_YET_RCS = (1, 75, 255)
UNTIL_CHARS = 300
PROBE_CHARS = 1000
MAX_EXPIRES_S = 365 * 86400
# Trailing charter body lines, in the order they are written.
CHARTER_LINES = (("expires", "Expires: "), ("until", "Until: "), ("probe", "Until probe: "))
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def parse_time(raw: str) -> float | None:
    """A stored `expires` (see stamp) as epoch seconds; None when unreadable."""
    try:
        at = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()


def parse_expires(raw: Any, now: float | None = None) -> float:
    """`expires` from an action: a delay (`6h`, `3d`) from now, or an ISO date or time (local
    unless it names a zone). Must lie ahead, within a year."""
    now = time.time() if now is None else now
    s = str(raw).strip() if isinstance(raw, str) else ""
    m = re.fullmatch(r"([0-9]+) ?([smhdw])", s)
    if m:
        at = now + int(m[1]) * _UNIT[m[2]]
    else:
        iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s.replace(" ", "T", 1).replace("Z", "+00:00"))
        try:
            at = datetime.fromisoformat(iso).timestamp() if s else None
        except ValueError:
            at = None
        if at is None:
            raise ValueError(f"expires {raw!r}: use a delay such as 12h or 3d, or an ISO date or time such as "
                             f"2026-10-05T09:00")
    if at <= now:
        raise ValueError(f"expires {raw!r} is already past: retire it now instead")
    if at > now + MAX_EXPIRES_S:
        raise ValueError(f"expires {raw!r} is more than a year away")
    return at


def from_action(a: dict, now: float | None = None) -> dict:
    """The end an action sets: {"expires": ts, "until": text, "probe": command}, only those given."""
    out: dict = {}
    if a.get("expires"):
        out["expires"] = parse_expires(a["expires"], now)
    until = " ".join(str(a.get("until") or "").split())
    if until:
        if len(until) > UNTIL_CHARS:
            raise ValueError(f"until must be one plain-language condition of at most {UNTIL_CHARS} characters")
        out["until"] = until
    probe = a.get("until_probe")
    if probe:
        if not isinstance(probe, str) or "\n" in probe.strip() or len(probe) > PROBE_CHARS:
            raise ValueError(f"until_probe must be one shell command on one line, at most {PROBE_CHARS} characters")
        out["probe"] = probe.strip()
    return out


def describe(end: dict) -> str:
    """'ends 2026-10-06T12:00Z, or when <until>' for prompts and alerts."""
    parts = ([stamp(end["expires"])] if end.get("expires") else []) \
        + ([f"when {end['until']}"] if end.get("until") else []) \
        + (["when its probe passes"] if end.get("probe") and not end.get("until") else [])
    return "ends " + ", or ".join(parts) if parts else ""


# memory -------------------------------------------------------------------------------------------
def front_matter(end: dict) -> str:
    return "".join(f"{k}: {v}\n" for k, v in (("expires", stamp(end["expires"]) if end.get("expires") else ""),
                                               ("until", end.get("until") or ""),
                                               ("until_probe", end.get("probe") or "")) if v)


def read_front_matter(head: str) -> dict:
    out: dict = {}
    for key, name in (("expires", "expires"), ("until", "until"), ("probe", "until_probe")):
        m = re.search(rf"^{name}:[ \t]*(.+)$", head, re.M)
        if m:
            out[key] = parse_time(m[1]) if key == "expires" else m[1].strip()
    return {k: v for k, v in out.items() if v}


# charter ------------------------------------------------------------------------------------------
def charter_tail(end: dict) -> str:
    """The lines a charter section's text ends with (none when `end` is empty)."""
    vals = {"expires": stamp(end["expires"]) if end.get("expires") else "", "until": end.get("until") or "",
            "probe": end.get("probe") or ""}
    return "".join(f"\n{label}{vals[k]}" for k, label in CHARTER_LINES if vals[k])


def charter_end(body: list[str]) -> dict:
    """The end a charter section's trailing lines give, if any."""
    out: dict = {}
    for line in reversed(body):
        s = line.strip()
        if not s:
            continue
        hit = next(((k, s[len(label):].strip()) for k, label in CHARTER_LINES if s.startswith(label)), None)
        if not hit:
            break
        out.setdefault(hit[0], hit[1])
    if "expires" in out:
        out["expires"] = parse_time(out["expires"])
    return {k: v for k, v in out.items() if v}


def section_name(heading: str) -> str:
    return " ".join(heading[3:].split())


def move_to_history(p: Project, heading: str, body: list[str], note: str) -> Path:
    """Append a charter section to CHARTER_HISTORY with `note`, once per section and note."""
    hist = p.harness / CHARTER_HISTORY
    if not hist.exists():
        durable_append(hist, "# Charter history\n\nSections replaced or retired in CHARTER.md, oldest first.\n")
    try:
        done = f"\n{heading}\n{note}\n" in hist.read_text()
    except FileNotFoundError:
        done = False
    if not done:   # a retried turn or tick moves it once
        durable_append(hist, f"\n{heading}\n{note}\n" + "\n".join(body).strip("\n") + "\n")
    return hist


def retire_section(p: Project, heading: str, why: str) -> bool:
    """Move the charter section headed exactly `heading` to the history; False when it is gone."""
    from .prompts import charter_sections
    sections = charter_sections(p.charter_path.read_text())
    idx = [i for i, (h, _) in enumerate(sections) if h == heading]
    if not idx:
        return False
    hist = move_to_history(p, heading, sections[idx[0]][1], f"(retired {time.strftime('%Y-%m-%d')}: {why})")
    kept = "\n".join(line for i, (h, b) in enumerate(sections) if i != idx[0] for line in ([h] if h else []) + b)
    durable_write(p.charter_path, kept.rstrip() + "\n")
    p.commit_harness([p.charter_path, hist], f"charter: retired \"{section_name(heading)}\" ({why})"[:200])
    return True


# both ---------------------------------------------------------------------------------------------
def temporaries(p: Project) -> list[dict]:
    """Every live memory entry and charter section that carries an end: key, kind, name, what, end."""
    out = []
    for e in p._memory_entries():
        if e.get("end"):
            out.append({"key": f"memory:{e['name']}", "kind": "memory", "name": e["name"],
                        "what": f"memory [{e['name']}]", "end": e["end"]})
    if p.charter_path.exists():
        from .prompts import charter_sections
        for head, body in charter_sections(p.charter_path.read_text()):
            end = charter_end(body) if head and not section_name(head).lower().startswith("brief") else {}
            if end:
                out.append({"key": f"charter:{section_name(head)}", "kind": "charter", "name": head,
                            "what": f"charter section \"{section_name(head)}\"", "end": end})
    return out


def retire(p: Project, item: dict, why: str, now: float) -> None:
    if item["kind"] == "memory":
        p.forget_memory(item["name"], why=why)
    elif not retire_section(p, item["name"], why):
        return
    db = p.db
    done = [x for x in db.kv(ENDED_KEY, []) or [] if float(x.get("at") or 0) > now - 7 * 86400]
    db.set_kv(ENDED_KEY, done + [{"at": now, "what": item["what"], "why": why}])
    for k in (LISTED_KEY, BROKEN_KEY):
        seen = db.kv(k) or {}
        if item["key"] in seen:
            seen.pop(item["key"])
            db.set_kv(k, seen or None)


class Ends:
    """The daemon's side: retire what has ended, run end probes in the background."""

    def __init__(self, p: Project, log=lambda msg: None):
        self.p, self.log = p, log
        self._checked = 0.0
        self._procs: dict[str, tuple[subprocess.Popen, float, str]] = {}
        self._probed: dict[str, float] = {}

    def tick(self, now: float | None = None) -> list[str]:
        """Returns what was retired this time, as lines."""
        now = time.time() if now is None else now
        verdicts = self._reap(now)
        if now - self._checked < CHECK_EVERY_S and not verdicts:
            return []
        self._checked = now
        retired, broken = [], dict(self.p.db.kv(BROKEN_KEY) or {})
        for item in temporaries(self.p):
            end, key = item["end"], item["key"]
            rc = verdicts.get(key, (None, ""))
            why = ""
            if end.get("expires") and end["expires"] <= now:
                why = f"expired {stamp(end['expires'])}"
            elif end.get("probe") and rc[1] == end["probe"] and rc[0] == 0:
                why = "its until_probe passed"
            if why:
                try:
                    retire(self.p, item, why, now)
                except (OSError, ValueError) as e:
                    self.log(f"could not retire {item['what']}: {e}")
                    continue
                retired.append(f"{item['what']} ({why})")
                broken.pop(key, None)
                continue
            if end.get("probe") and rc[1] == end["probe"] and rc[0] is not None:
                if rc[0] in NOT_YET_RCS:
                    broken.pop(key, None)
                else:
                    broken[key] = rc[0] if isinstance(rc[0], str) else f"exit {rc[0]}"
            if end.get("probe") and key not in self._procs and now - self._probed.get(key, 0) >= PROBE_EVERY_S:
                self._start(key, end["probe"], now)
        live = {i["key"] for i in temporaries(self.p)} if retired else None
        broken = {k: v for k, v in broken.items() if live is None or k in live}
        if broken != (self.p.db.kv(BROKEN_KEY) or {}):
            self.p.db.set_kv(BROKEN_KEY, broken or None)
        if retired:
            text = "Retired, its end condition passed: " + "; ".join(retired)
            self.log(text)
            self.p.db.post("out", text, chat=None, kind="alert", severity="low")
        return retired

    def _start(self, key: str, probe: str, now: float) -> None:
        self._probed[key] = now
        try:
            proc = subprocess.Popen(probe, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.log(f"end probe for {key} could not start: {e}")
            return
        self._procs[key] = (proc, now, probe)

    def _reap(self, now: float) -> dict:
        out = {}
        for key, (proc, started, probe) in list(self._procs.items()):
            rc = proc.poll()
            if rc is None and now - started < PROBE_TIMEOUT_S:
                continue
            del self._procs[key]
            if rc is None:
                _kill(proc)
            out[key] = ("timeout" if rc is None else rc, probe)
        return out

    def stop(self) -> None:
        for proc, _, _ in self._procs.values():
            _kill(proc)
        self._procs.clear()


def _kill(proc: subprocess.Popen) -> None:
    """A probe runs in a session of its own: end it and whatever it started."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


# digest -------------------------------------------------------------------------------------------
def digest_lines(p: Project, since: float, now: float | None = None) -> list[str]:
    """The digest's line of entries retired since `since` (the previous turn), and the entries
    whose end only a model can judge, each listed at most once per RECHECK_S."""
    now = time.time() if now is None else now
    db = p.db
    lines = []
    done = [x for x in db.kv(ENDED_KEY, []) or [] if float(x.get("at") or 0) > since]
    if done:
        lines.append("## Retired (end condition passed; nothing to do): "
                     + "; ".join(f"{x['what']} ({x['why']})" for x in done))
    listed, broken = dict(db.kv(LISTED_KEY) or {}), db.kv(BROKEN_KEY) or {}
    due, live = [], set()
    for item in temporaries(p):
        end, key = item["end"], item["key"]
        if not (end.get("until") and not end.get("probe") or key in broken):
            continue
        live.add(key)
        if key not in listed:
            listed[key] = now   # just recorded: first listed a day from now
        elif now - float(listed[key]) >= RECHECK_S:
            listed[key] = now
            cond = end.get("until") or "its probe passes"
            note = f"; its until_probe is broken ({broken[key]}): fix it with a restated entry" if key in broken else ""
            due.append(f"- {item['what']}: until {cond}{note}")
    listed = {k: v for k, v in listed.items() if k in live}
    if listed != (db.kv(LISTED_KEY) or {}):
        db.set_kv(LISTED_KEY, listed or None)
    if due:
        lines.append("## Temporary instructions possibly over (retire each that clearly is: `memory_forget`, or "
                     "`charter_update` `replaces` + `over`; the rest come back in a day)")
        lines += due
    return lines
