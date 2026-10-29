"""End conditions on temporary instructions.

A memory entry or a charter section recorded from temporary words ("while X", "until Y", "for
now") carries an end: `expires` (a time), `until` (a plain-language condition) and/or
`until_probe` (a shell probe like `start_when`: exit 0 = over; 1, 75 or 255 = not yet, a 255 only
when ssh cannot reach the host it names: netwait.confirm, else the probe is broken). The daemon
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
import tempfile
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import detach, netwait, probespawn, timefmt
from .detach import failed_check
from .project import Project, command_env, durable_append, durable_write, output_tail

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
SH = "/bin/sh"   # what the daemon runs every stored probe with (subprocess shell=True)
MAX_EXPIRES_S = 365 * 86400
# Trailing charter body lines, in the order they are written.
CHARTER_LINES = (("expires", "Expires: "), ("until", "Until: "), ("probe", "Until probe: "))
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def sh_syntax_error(probe: str) -> str:
    """Why /bin/sh cannot parse `probe` (`sh -n`), or "" when it can or the check cannot run."""
    try:
        r = subprocess.run([SH, "-n", "-c", probe], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return "" if r.returncode == 0 else " ".join((r.stderr or f"exit {r.returncode}").split())[:300]


def check_sh(probe: str, what: str) -> None:
    """Refuse a probe to be stored that /bin/sh cannot parse: the daemon would get exit 2 from it forever."""
    err = sh_syntax_error(probe)
    if err:
        raise ValueError(f"{what} is not valid POSIX sh, which the daemon runs probes with ({SH} -n: {err}); "
                         f"write it without bash-only syntax (a here-string <<<, process substitution <(...), "
                         f"arrays) or wrap it in bash -c '...'")


TRIVIAL_WORDS = ("true", ":", "/bin/true", "/usr/bin/true")
_REDIRECT_RE = re.compile(r"^\d*(?:>>|>&|<&|>\||<>|>|<)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _quote_end(s: str, i: int) -> int:
    """The index just past the quote opened at s[i] (', " or `), or len(s) when it never closes."""
    q, j = s[i], i + 1
    while j < len(s):
        if s[j] == "\\" and q != "'":
            j += 2
            continue
        if s[j] == q:
            return j + 1
        j += 1
    return len(s)


def _top_level(probe: str) -> list[tuple[str, str]]:
    """`probe` split at its top-level list and pipeline operators, outside quotes, subshells, $(...)
    and { ...; } groups, without comments: [(the operator before it: "", ";", "&&", "||" or "|", a command)].
    `;`, `&` and newlines all read as ";"."""
    parts, op, buf, depth, i, n = [], "", [], 0, 0, len(probe)
    while i < n:
        ch, prev = probe[i], probe[i - 1] if i else " "
        word_start = prev in " \t\n;&|()"
        if ch == "\\":
            buf.append(probe[i:i + 2])
            i += 2
            continue
        if ch in "'\"`":
            j = _quote_end(probe, i)
            buf.append(probe[i:j])
            i = j
            continue
        if ch == "#" and word_start:
            while i < n and probe[i] != "\n":
                i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch in "{}" and word_start and (i + 1 == n or probe[i + 1] in " \t\n;&|)"):
            depth = depth + 1 if ch == "{" else max(depth - 1, 0)
        elif depth == 0 and ch in ";&|\n" and prev not in "<>":
            two = probe[i:i + 2]
            sep = two if two in ("&&", "||") else "|" if ch == "|" else ";"
            i += 2 if two in ("&&", "||", ";;") else 1
            parts.append((op, "".join(buf).strip()))
            op, buf = sep, []
            continue
        buf.append(ch)
        i += 1
    parts.append((op, "".join(buf).strip()))
    return [(o, c) for o, c in parts if c]


def _trivial_command(cmd: str) -> bool:
    """Whether one simple command or group always exits 0: `true`, `:` or `exit 0` (with assignments or
    redirections), or a ( ... ) / { ...; } group whose list always does."""
    cmd = cmd.strip()
    if cmd.startswith("(") and cmd.endswith(")"):
        return always_succeeds(cmd[1:-1])
    if cmd.startswith("{") and cmd.endswith("}"):
        return always_succeeds(cmd[1:-1])
    words, skip = [], False
    for w in cmd.split():
        if skip:
            skip = False
        elif _REDIRECT_RE.match(w):
            skip = not _REDIRECT_RE.sub("", w)   # `> file`: the target is the next word
        elif not words and _ASSIGN_RE.match(w):
            continue
        else:
            words.append(w)
    return words in ([w] for w in TRIVIAL_WORDS) or words == ["exit", "0"]


def _words(probe: str) -> list[str]:
    """`probe`'s words and operators (`;`, `&`, `|`, `(`, `)`, `{`, `}`, newline), outside comments; each
    quoted string reads as the word "Q"."""
    out, buf, i, n = [], "", 0, len(probe)
    while i < n:
        ch = probe[i]
        if ch == "\\":
            buf += probe[i:i + 2]
            i += 2
        elif ch in "'\"`":
            buf, i = buf + "Q", _quote_end(probe, i)
        elif ch == "#" and not buf:
            while i < n and probe[i] != "\n":
                i += 1
        elif ch in " \t" or ch in ";&|(){}\n":
            out += [buf] if buf else []
            out += [ch] if ch not in " \t" else []
            buf, i = "", i + 1
        else:
            buf, i = buf + ch, i + 1
    return out + ([buf] if buf else [])


def _may_exit_non_zero(probe: str) -> bool:
    """Whether a step anywhere in `probe` can end it non-zero before its last list runs: an `exit` with
    no argument or one other than a literal 0 (`test -e x || exit 1; exit 0`, in a loop or group too), or
    errexit turned on (`set -e`, `set -o errexit`). Read loosely: an `exit 1` in a subshell counts too, so
    a doubtful probe is kept as a real test."""
    words = _words(probe)
    for k, w in enumerate(words):
        nxt = words[k + 1] if k + 1 < len(words) else ""
        if w == "exit" and nxt != "0":
            return True
        if w == "set":
            for j in range(k + 1, len(words)):
                opt = words[j]
                if not opt.startswith("-") or opt == "--":
                    break
                if "e" in opt[1:] or (opt[1:].endswith("o") and words[j + 1:j + 2] == ["errexit"]):
                    return True
    return False


def always_succeeds(probe: str) -> bool:
    """Whether shell probe `probe` exits 0 whatever it tests: it is only `true`, `:` or `exit 0`, or its last
    list ends in one of them after `||`, `;`, `&` or `|` (`test -e x || true`, `check; exit 0`). Such a
    probe says "over" at once, so an end, hold or undo waiting on it fires while its cause is still there.
    A real test (`test -e x || exit 1`, `cmd && true`, `(a || true) && b`) is not flagged, nor one with a
    step that can exit non-zero first (`test -e x || exit 1; exit 0`, `set -e; test -e x; exit 0`)."""
    parts = _top_level(probe or "")
    if not parts or _may_exit_non_zero(probe):
        return False
    start = max((k for k, (op, _) in enumerate(parts) if op == ";"), default=0)
    pipelines: list[list[str]] = []   # [operator before it, its last command] of the last and-or list
    for op, cmd in parts[start:]:
        if op == "|" and pipelines:   # a pipeline exits with its last command's code
            pipelines[-1][1] = cmd
        else:
            pipelines.append([op, cmd])
    always = _trivial_command(pipelines[0][1])
    for op, cmd in pipelines[1:]:
        ok = _trivial_command(cmd)
        always = (always and ok) if op == "&&" else (always or ok)
    return always


def check_not_trivial(probe: str, what: str) -> None:
    """Refuse a probe that always exits 0 (always_succeeds): it would end what waits on it at once."""
    if always_succeeds(probe):
        raise ValueError(f"{what} always exits 0 (it is or ends in `true`, `:` or `exit 0`, e.g. after `|| true`), "
                         f"so it would say \"over\" at once while the cause is still there; make it a real test "
                         f"that ends with `|| exit 1`, e.g. `test -e /path/to/marker || exit 1`")


def hides_test(probe: str) -> bool:
    """always_succeeds, but not only `true`, `:` or `exit 0` steps: a probe that runs a test and then
    drops its answer (`test -e x || true`). A task's bare `true` is a deliberate "now" (`set-when <id>
    true` wakes it); this one would start or wake a task by mistake."""
    return always_succeeds(probe) and not all(_trivial_command(c) for _, c in _top_level(probe))


TRIVIAL_WHY = ("it always exits 0 (it ends in `true`, `:` or `exit 0`, e.g. after `|| true`), so it never tells "
               "over from not yet; restate it ending in `|| exit 1`")


def stamp(ts: float) -> str:
    """The stored form (charter lines, front matter); `shown` is the one people read."""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def shown(ts: float, where=None) -> str:
    """'2026-10-12 05:11 UTC': `ts` in the home zone of `where` (a Project, its settings or a zone
    name), else timefmt.ambient()."""
    return timefmt.long(ts, where if where is not None else timefmt.ambient())


def parse_time(raw: str) -> float | None:
    """A stored `expires` (see stamp) as epoch seconds; None when unreadable."""
    try:
        at = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()


def parse_expires(raw: Any, now: float | None = None, what: str = "expires",
                  max_s: float = MAX_EXPIRES_S, where=None) -> float:
    """`expires` (or `what`) from an action: a delay (`6h`, `3d`) from now, or an ISO date or time. A time
    with an offset or Z keeps it; one without (a date alone is its midnight) is read in the home zone of
    `where` (a Project, settings or zone name; see timefmt.reading_zone), never UTC by default. Must lie
    ahead, within `max_s` (a year)."""
    now = time.time() if now is None else now
    s = str(raw).strip() if isinstance(raw, str) else ""
    m = re.fullmatch(r"([0-9]+) ?([smhdw])", s)
    if m:
        at = now + int(m[1]) * _UNIT[m[2]]
    else:
        iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s.replace(" ", "T", 1).replace("Z", "+00:00"))
        try:
            d = datetime.fromisoformat(iso) if s else None
        except ValueError:
            d = None
        at = (d if d.tzinfo else d.replace(tzinfo=timefmt.reading_zone(where))).timestamp() if d else None
        if at is None:
            raise ValueError(f"{what} {raw!r}: use a delay such as 12h or 3d, or an ISO date or time such as "
                             f"2026-10-05T09:00")
    if at <= now:
        raise ValueError(f"{what} {raw!r} is already past" + (": retire it now instead" if what == "expires" else ""))
    if at > now + max_s:
        raise ValueError(f"{what} {raw!r} is more than " + ("a year" if max_s == MAX_EXPIRES_S
                                                           else f"{max_s / 86400:g} days") + " away")
    return at


def from_action(a: dict, now: float | None = None, where=None) -> dict:
    """The end an action sets: {"expires": ts, "until": text, "probe": command}, only those given. A zone-less
    `expires` is read in the home zone of `where` (parse_expires)."""
    out: dict = {}
    if a.get("expires"):
        out["expires"] = parse_expires(a["expires"], now, where=where)
    until = " ".join(str(a.get("until") or "").split())
    if until:
        if len(until) > UNTIL_CHARS:
            raise ValueError(f"until must be one plain-language condition of at most {UNTIL_CHARS} characters")
        out["until"] = until
    probe = a.get("until_probe")
    if probe:
        if not isinstance(probe, str) or "\n" in probe.strip() or len(probe) > PROBE_CHARS:
            raise ValueError(f"until_probe must be one shell command on one line, at most {PROBE_CHARS} characters")
        check_sh(probe.strip(), "until_probe")
        check_not_trivial(probe.strip(), "until_probe")
        out["probe"] = probe.strip()
    return out


def describe(end: dict, where=None) -> str:
    """'ends 2026-10-06 05:00 UTC, or when <until>' for prompts and alerts, in the home zone of `where`
    (a Project, its settings or a zone name); without one the stored UTC stamp."""
    when = (timefmt.long(end["expires"], where) if where is not None else stamp(end["expires"])) \
        if end.get("expires") else ""
    parts = ([when] if when else []) \
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
        self._errs: dict[int, Any] = {}   # id(proc) -> its stderr file
        self._probed: dict[str, float] = {}
        self._resume: str | None = None   # the last entry done when a pass stopped on its time budget
        self._carry: dict = {}   # and the verdicts read for the entries after it

    def tick(self, now: float | None = None, over=None) -> list[str]:
        """Returns what was retired this time, as lines. `over(what)`, asked before each entry, says
        the caller's time budget is spent: the pass stops there (after at least one entry) and the
        next tick resumes after the last one done, with the verdicts already read."""
        now = time.time() if now is None else now
        verdicts, self._carry = {**self._carry, **self._reap(now)}, {}
        if now - self._checked < CHECK_EVERY_S and not verdicts and self._resume is None:
            return []
        self._checked = now
        retired, broken = [], dict(self.p.db.kv(BROKEN_KEY) or {})
        items, at = temporaries(self.p), self._resume
        self._resume = None
        cut = next((n + 1 for n, x in enumerate(items) if x["key"] == at), 0)
        items = items[cut:] + items[:cut]
        for n, item in enumerate(items):
            if over is not None and over(item["what"]) and n:
                self._resume = items[n - 1]["key"]
                self._carry = {x["key"]: verdicts[x["key"]] for x in items[n:] if x["key"] in verdicts}
                break
            end, key = item["end"], item["key"]
            rc = verdicts.get(key, (None, "", "")) if not always_succeeds(end.get("probe") or "") else (None, "", "")
            why = ""
            if end.get("expires") and end["expires"] <= now:
                why = f"expired {shown(end['expires'], self.p)}"
            elif end.get("probe") and rc[1] == end["probe"] and rc[0] == 0:
                why = "its until_probe passed"
            elif end.get("probe") and rc[1] == end["probe"] and failed_check(end["probe"], rc[0]):
                why = f"the detached job its until_probe waits on ended but failed or left no exit code ({detach.failed_why(end['probe'], rc[0], self.p.root)})"
            if not why and end.get("probe") and always_succeeds(end["probe"]):
                broken[key] = TRIVIAL_WHY   # stored before it was refused: never run, only its expires ends it
                continue
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
                    if str(rc[2]).startswith("timed out"):
                        self.log(f"end probe of {item['what']} {rc[2]}"[:400])
                else:
                    code = rc[0] if isinstance(rc[0], str) else f"exit {rc[0]}"
                    broken[key] = f"{code}: {rc[2]}" if rc[2] else code   # with its stderr tail
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
            self._procs[key] = (spawn_probe(self.p, probe, self._errs), now, probe)
        except OSError as e:
            self.log(f"end probe for {key} could not start: {e}")

    def _reap(self, now: float) -> dict:
        return reap_probes(self._procs, self._errs, now, self.p)

    def stop(self) -> None:
        stop_probes(self._procs, self._errs)


def spawn_probe(p: Project, probe: str, errs: dict[int, Any], stdout: bool = False) -> probespawn.Pending:
    """Start shell probe `probe` in a session of its own, in its project's context (cwd p.root,
    project.command_env), off the caller's thread (probespawn: opening its file or forking can stall).
    Its stderr (with `stdout`, its stdout too) goes to a file (a pipe nobody reads could fill), read
    through `errs` (_err_tail). A probe that cannot start polls "could not start"."""
    proc = probespawn.Pending(probe, cwd=str(p.root), env=command_env(p), stdout=stdout)
    errs[id(proc)] = proc
    return proc


def _spawn_now(p: Project, probe: str, errs: dict[int, Any], stdout: bool = False) -> subprocess.Popen:
    """spawn_probe on the caller's thread, for a caller that waits on the probe anyway (probe_once)."""
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(probe, shell=True, cwd=str(p.root), env=command_env(p), stdin=subprocess.DEVNULL,
                                stdout=err if stdout else subprocess.DEVNULL, stderr=err, start_new_session=True)
    except OSError:
        err.close()
        raise
    errs[id(proc)] = err
    return proc


def probe_once(p: Project, probe: str, timeout_s: float) -> tuple[Any, str]:
    """Run shell probe `probe` once as the daemon does (spawn_probe), waiting at most `timeout_s`:
    (its exit code, "timeout" (killed) or "cannot run: <why>", its output tail)."""
    errs: dict[int, Any] = {}
    try:
        proc = _spawn_now(p, probe, errs, stdout=True)
    except OSError as e:
        return f"cannot run: {e}", ""
    try:
        rc: Any = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill(proc)
        rc = "timeout"
    except BaseException:   # interrupted: leave no probe running
        _kill(proc)
        _err_tail(errs, proc)
        raise
    return rc, _err_tail(errs, proc)


def _err_tail(errs: dict[int, Any], proc: subprocess.Popen) -> str:
    """The bounded end of a finished probe's stderr, once (its file is closed after)."""
    f = errs.pop(id(proc), None)
    if isinstance(f, probespawn.Pending):
        f = f.take_file()
    if f is None:
        return ""
    try:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4 * 2000))
        return output_tail(f.read())
    except (OSError, ValueError):
        return ""
    finally:
        f.close()


class _Confirming:
    """A probe that exited 255, while netwait.confirm asks ssh whether the hosts it names answer."""

    def __init__(self, fut: Future, err: str):
        self.fut, self.err = fut, err


_confirm_slots = threading.BoundedSemaphore(4)   # at most 4 host checks at once, across projects


def _confirm(probe: str, err: str, p: Project | None) -> Future:
    """netwait.confirm off the daemon's thread: in an outage each host check waits out its timeout.
    Daemon threads, so a check still running never holds up the daemon's exit."""
    env = command_env(p) if p is not None else None
    fut: Future = Future()

    def run() -> None:
        with _confirm_slots:
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(netwait.confirm(probe, err, lambda h: netwait.answers(h, env)))
            except BaseException as e:
                fut.set_exception(e)
    threading.Thread(target=run, name="ttp-end-confirm", daemon=True).start()
    return fut


def reap_probes(procs: dict, errs: dict[int, Any], now: float,
                p: Project | None = None) -> dict[str, tuple[Any, str, str]]:
    """{key: (exit code or "timeout", probe, stderr tail)} of the probes in `procs` that ended or
    ran past PROBE_TIMEOUT_S (killed: 1, "not yet", until it timed out probespawn.TIMEOUTS_BROKEN runs
    in a row, then "timeout"); the rest keep running. A 255 is "host unreachable" only once
    netwait.confirm finds every host the probe names silent (the key stays in `procs` meanwhile);
    otherwise its verdict is why it is broken (a string), never "not yet"."""
    out = {}
    for key, (proc, started, probe) in list(procs.items()):
        if isinstance(proc, _Confirming):
            if proc.fut.done():
                del procs[key]
                try:
                    why = proc.fut.result()
                except Exception:   # the check itself failed: keep the old reading
                    why = ""
                out[key] = (why or netwait.UNREACHABLE_RC, probe, proc.err)
            continue
        rc = proc.poll()
        if rc is None and now - started < PROBE_TIMEOUT_S:
            continue
        del procs[key]
        if rc is None:
            _kill(proc)
        err = _err_tail(errs, proc)
        if rc is None:   # killed on its timeout: "not yet" unless it keeps timing out (probespawn.timed_out)
            rc = probespawn.timed_out(probe)
            if rc == 1:
                err = f"timed out after {PROBE_TIMEOUT_S} s, counted as not yet" + (f": {err}" if err else "")
        else:
            probespawn.answered(probe)
        if rc == netwait.UNREACHABLE_RC:
            procs[key] = (_Confirming(_confirm(probe, err, p), err), started, probe)
            continue
        out[key] = (rc, probe, err)
    return out


def stop_probes(procs: dict, errs: dict[int, Any]) -> None:
    for proc, _, _ in procs.values():
        if isinstance(proc, _Confirming):
            proc.fut.cancel()   # a check already running ends within its own timeout
            continue
        _kill(proc)
        _err_tail(errs, proc)
    procs.clear()


def _kill(proc) -> None:
    """A probe runs in a session of its own: end it and whatever it started (probespawn.kill)."""
    probespawn.kill(proc)


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
            note = f"; its until_probe is broken ({broken[key][-500:]}): fix it with a restated entry" if key in broken else ""
            due.append(f"- {item['what']}: until {cond}{note}")
    listed = {k: v for k, v in listed.items() if k in live}
    if listed != (db.kv(LISTED_KEY) or {}):
        db.set_kv(LISTED_KEY, listed or None)
    if due:
        lines.append("## Temporary instructions possibly over (retire each that clearly is: `memory_forget`, or "
                     "`charter_update` `replaces` + `over`; the rest come back in a day)")
        lines += due
    return lines
