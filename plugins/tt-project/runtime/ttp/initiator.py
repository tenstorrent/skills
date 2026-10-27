# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who or what started the shutdown that ended the previous boot: best effort, model-free.

The daemon asks once per boot (daemon._note_boot) and keeps the answer in the boot record, so a
question such as "did tt-project reboot this host?" is answered by the record itself. Sources, in
order; each is bounded and never raises:

1. The previous boot's journal as JSON, read as this user, else through `sudo -n` (bounded, never
   prompts, stores nothing) when that showed only the user's own entries and `sudo -n true` works. Three
   bounded reads the same way: the boot's last MARK_ENTRIES lines of logind and of systemd about the
   shutdown targets, which the shutdown's own Stopping/Stopped lines cannot push out; the boot's last
   MARK_ENTRIES lines of sudo and pkexec; then the WINDOW_S before the shutdown marker found in the
   first (logind's 'System is rebooting/powering down', systemd reaching the reboot or poweroff target,
   a 'shutdown scheduled' line). A sudo or
   pkexec COMMAND that runs reboot, shutdown, poweroff or halt (or `systemctl reboot|poweroff|halt|
   kexec`), or logind's 'Power key pressed': the last one wins. Else the line nearest before the marker
   of a system unit, or of systemd starting one, that names a power cycle, reboot or shutdown:
   '<unit> (<message>)'. A unit systemd only started is named only when its own lines (the last
   UNIT_ENTRIES with its _SYSTEMD_UNIT, read the same way) show it logged after its start: one armed just
   before the request (a shutdown watchdog) that logged nothing is 'armed, not fired' after the request.
2. A tt-project host-cycle record (`ttp drain --reboot`) from any project on this host that ended that
   boot (hostcycle.planned).
3. An entry in the shared machine-change ledger for this host's machine alias, recorded from
   LEDGER_WINDOW_S before the shutdown (the boot's last heartbeat) up to this boot, that names a power
   cycle or a reboot: the latest one, with its entry id and the project that recorded it.

Else the unit started or logging nearest before the marker, a bare logind line, or for a boot whose
journal ends with no marker 'no clean shutdown logged (...)' and its last unit lines (a marker read that
came back full says only that no marker was in it). Else 'unknown',
or 'unknown (journal not readable: <why>)'. Tests set TTP_TEST_NO_JOURNAL so the real journalctl and
sudo never run; they stub run_journal (extra, sudo) and sudo_ok instead.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

JOURNAL_TIMEOUT_S = 5
SUDO_TIMEOUT_S = 8    # each `sudo -n journalctl`; `sudo -n true` before them takes SUDO_CHECK_S
SUDO_CHECK_S = 3
MARK_ENTRIES = 5000   # the marker read (logind, systemd's shutdown-target lines) and the command read (sudo, pkexec)
WINDOW_S = 600        # the window read: this long before the shutdown marker, at most WINDOW_ENTRIES
WINDOW_ENTRIES = 5000
TAIL_ENTRIES = 300    # a boot with no marker: its last lines
UNIT_ENTRIES = 20     # a started unit's own last lines: whether it logged after its start
STOP_TARGETS = ("reboot.target", "poweroff.target", "halt.target", "kexec.target")
COMMAND_IDENTS = ("sudo", "pkexec")
FIELDS = "MESSAGE,_SYSTEMD_UNIT,UNIT,SYSLOG_IDENTIFIER,_COMM,_UID"
LEDGER_WINDOW_S = 1800
TEXT_CHARS = 120
_LINE = re.compile(r"\s(?P<ident>[\w.@-]+)(?:\[\d+\])?:\s(?P<msg>.*)$")
_DENIED = re.compile(r"insufficient permissions|not seeing messages from other users|permission denied", re.I)
_NO_STORE = re.compile(r"no persistent journal|no journal files were found|is not available", re.I)
_STOPS = {"reboot", "shutdown", "poweroff", "halt"}
_SYSTEMCTL_STOPS = {"reboot", "poweroff", "halt", "kexec"}
_LEDGER_WORDS = re.compile(r"power[ _-]?cycl|reboot|power[ _-]?off|shut[ _-]?down", re.I)
_STOP_WORDS = re.compile(r"power[ _-]?cycl|\breboot|power[ _-]?off|\bshutdown\b|\bhalt\b|kexec|watchdog", re.I)
_MARKER_LOGIND = re.compile(r"^System is (rebooting|powering down|halting)|shutdown scheduled"
                            r"|system will (reboot|power ?off|halt|shut ?down)", re.I)
_MARKER_SYSTEMD = re.compile(r"^Reached target .*\b(reboot|power-?off|halt|kexec)\b", re.I)
_UNIT_START = re.compile(r"^Start(?:ed|ing) (?P<unit>[\w.@:-]+\.(?:service|scope|timer))\b")
NO_SHUTDOWN = "no clean shutdown logged (power loss, hard reset or watchdog)"
NO_MARKER = f"no shutdown marker in the journal's last {MARK_ENTRIES} logind and shutdown-target lines read"
EMPTY = "no system entries visible for the previous boot"


def _clip(text, chars: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= chars else text[:chars - 1] + "…"


def _journalctl(boot: str | None, extra) -> list[str]:
    hexid = (boot or "").replace("-", "")
    which = hexid if re.fullmatch(r"[0-9a-f]{32}", hexid) else "-1"
    return ["journalctl", "-b", which, "--no-pager", "-o", "json", f"--output-fields={FIELDS}", *extra]


def _run(argv: list[str], timeout: float) -> tuple[int, str, str] | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.returncode, r.stdout or "", r.stderr or ""


def marker_args() -> list[str]:
    """logind's lines, or systemd's about a shutdown target: few after the marker however many units stop."""
    return ["-n", str(MARK_ENTRIES), "SYSLOG_IDENTIFIER=systemd-logind", "+", "SYSLOG_IDENTIFIER=systemd",
            *(f"UNIT={t}" for t in STOP_TARGETS)]


def command_args() -> list[str]:
    return ["-n", str(MARK_ENTRIES), *(f"SYSLOG_IDENTIFIER={i}" for i in COMMAND_IDENTS)]


def window_args(at: float | None) -> list[str]:
    """The lines before the shutdown marker at `at`, newest first (with --since, -n alone would keep the
    oldest), or with no marker the boot's last lines."""
    if at is None:
        return ["-n", str(TAIL_ENTRIES)]
    return ["--since", f"@{int(at - WINDOW_S)}", "--until", f"@{int(at) + 1}", "-r", "-n", str(WINDOW_ENTRIES)]


def unit_args(unit: str) -> list[str]:
    """The last lines unit `unit` logged itself (systemd's lines about it are init.scope's)."""
    return ["-n", str(UNIT_ENTRIES), f"_SYSTEMD_UNIT={unit}"]


def end_args() -> list[str]:
    """The boot's last line: when its journal ended."""
    return ["-n", "1"]


def run_journal(boot: str | None, extra=(), sudo: bool = False) -> tuple[int, str, str] | None:
    """(exit code, stdout, stderr) of journalctl for boot `boot` (the previous boot when it is no journal
    boot id) as JSON, only the fields parse() reads, `extra` selecting the lines; through `sudo -n` with
    `sudo` (never prompts, stores nothing). None when it could not run."""
    if os.environ.get("TTP_TEST_NO_JOURNAL"):
        return None
    if sudo:
        return _run(["sudo", "-n", *_journalctl(boot, extra)], SUDO_TIMEOUT_S)
    return _run(_journalctl(boot, extra), JOURNAL_TIMEOUT_S)


def sudo_ok() -> bool:
    """Whether `sudo -n` works here (passwordless): checked once per reboot, silently."""
    if os.environ.get("TTP_TEST_NO_JOURNAL"):
        return False
    r = _run(["sudo", "-n", "true"], SUDO_CHECK_S)
    return bool(r) and r[0] == 0


def _entries(out: str) -> list[dict]:
    """{ident, unit, msg, ts, uid} per line: journalctl's JSON, or the short format (older reads, tests)."""
    rows = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            msg = e.get("MESSAGE")
            if isinstance(msg, list):   # a binary message comes as its bytes
                msg = bytes(b for b in msg if isinstance(b, int) and 0 <= b < 256).decode("utf-8", "replace")
            try:
                ts = int(e.get("__REALTIME_TIMESTAMP")) / 1e6
            except (TypeError, ValueError):
                ts = None
            rows.append({"ident": str(e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or ""),
                         "unit": str(e.get("_SYSTEMD_UNIT") or ""), "about": str(e.get("UNIT") or ""),
                         "msg": str(msg or "").strip(), "ts": ts, "uid": e.get("_UID"), "json": True})
            continue
        m = _LINE.search(line)
        if m:
            rows.append({"ident": m["ident"], "unit": "", "about": "", "msg": m["msg"].strip(), "ts": None,
                         "uid": None, "json": False})
    return rows


def _limited(got: tuple[int, str, str]) -> str:
    """Why a read does not show the system's entries, or '' when it does."""
    rc, out, err = got
    # journalctl's hints go to stderr; a logged message saying 'permission denied' is no hint.
    if _DENIED.search(err or ""):
        return "user not in systemd-journal"
    if _NO_STORE.search(err or ""):
        return "no persistent journal kept for the previous boot"
    if rc != 0:
        tail = (err or "").strip().splitlines()
        return f"journalctl exited {rc}" + (f": {_clip(tail[-1], 60)}" if tail else "")
    rows = [e for e in _entries(out) if e["json"]]
    me = str(os.getuid())
    if rows and all(str(e["uid"]) == me for e in rows):
        return "user not in systemd-journal"   # only this user's own entries came back
    if not (out or "").strip():
        return EMPTY
    return ""


def read(boot: str | None, journal=None, sudo=None):
    """(the marker read, ask, how, why): ask(extra) reads more the same way, how is 'user', 'sudo' or ''
    when no read showed the system's entries (ask is then None), why says why not. As this user first,
    then through `sudo -n` when that showed only the user's own entries or none. `journal`/`sudo`: fixed answers
    (tests; sudo False: no passwordless sudo), read here when None."""
    as_user = (lambda extra: journal) if journal is not None else (lambda extra: run_journal(boot, extra))
    got = as_user(marker_args())
    if not got:
        return None, None, "", "journalctl did not answer, missing or timed out"
    why = _limited(got)
    if not why:
        return got, as_user, "user", ""
    if why not in ("user not in systemd-journal", EMPTY):
        return got, None, "", why
    if sudo is False or sudo is None and not sudo_ok():
        return got, None, "", f"{why} and no passwordless sudo"
    as_root = (lambda extra: sudo) if sudo is not None else (lambda extra: run_journal(boot, extra, sudo=True))
    s = as_root(marker_args())
    if not s or _limited(s):
        return got, None, "", f"{why}, and sudo -n journalctl did not read it either"
    return s, as_root, "sudo", ""


def _stop_command(cmd: str) -> str | None:
    """`cmd` shortened to its program and arguments when it stops the host, else None."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    if not words:
        return None
    prog = Path(words[0]).name
    if prog in _STOPS or (prog == "systemctl" and _SYSTEMCTL_STOPS & set(words[1:])):
        return _clip(" ".join([prog, *words[1:]]), 60)
    return None


def _service(e: dict) -> str:
    """The system unit that logged `e` when it may name an initiator: not systemd's own (pid 1, logind,
    journald, udev ...) nor a user manager."""
    unit = e["unit"]
    if not unit or unit == "init.scope" or unit.startswith(("systemd-", "user@")) \
            or e["ident"] in ("systemd", "systemd-logind", "kernel"):
        return ""
    return unit


def _marker(e: dict) -> bool:
    return (e["ident"] == "systemd-logind" and bool(_MARKER_LOGIND.search(e["msg"]))) \
        or (e["ident"] == "systemd" and bool(_MARKER_SYSTEMD.search(e["msg"])))


def marker_at(out: str) -> float | None:
    """The time of the first shutdown marker in the marker read, None with none (or no time)."""
    rows = _entries(out)
    marks = [e for e in rows if _marker(e)]
    if not marks or marks[-1]["ts"] is None:
        return None
    end = marks[-1]["ts"]
    return next(e["ts"] for e in marks if e["ts"] is not None and e["ts"] >= end - WINDOW_S)


def _armed(unit: str, e: dict, own) -> str | None:
    """'<unit> (<description>) armed, not fired: ...' when systemd started `unit` at `e` and its own lines
    (own(unit): (its read, the boot's last line), either None when not read) show nothing after that;
    None when it logged after its start, or that is unknown."""
    got = own(unit) if own else None
    if not got or got[0] is None:
        return None
    lines, end = got
    t0 = e["ts"]
    if any(r["unit"] == unit and r["msg"] and (t0 is None or r["ts"] is None or r["ts"] >= t0)
           for r in _entries(lines) if r["json"]):
        return None
    m = re.match(r"Start(?:ed|ing) (?P<what>.*?)\.*$", e["msg"])
    what = m["what"] if m else ""
    if what.startswith(unit):
        what = what[len(unit):].lstrip(" -")
    ends = [r["ts"] for r in _entries(end or "") if r["ts"] is not None]
    later = f" {max(ends) - t0:.0f} s later" if ends and t0 is not None and max(ends) >= t0 else ""
    name = f"{unit} ({_clip(what, 60)})" if what and what != unit else unit
    return f"{name} armed, not fired: it logged nothing of its own before the boot's journal ended{later}"


def parse(out: str, window: str | None = None, full: bool = False,
          commands: str | None = None, own=None) -> tuple[str | None, str | None]:
    """(initiator, fallback) from the previous boot's journal: `out` the marker read, `window` the lines
    before its marker (out itself when None), `commands` the sudo and pkexec read (or in out). A marker
    read with MARK_ENTRIES lines and no marker says NO_MARKER, never NO_SHUTDOWN. Initiator: the last
    sudo/pkexec stop command or power-key
    press, else the line nearest before the marker (within WINDOW_S) of a system unit, or of systemd
    starting one, that names a power cycle, reboot or shutdown; a unit systemd started that `own` (see
    _armed) shows logged nothing after its start is no initiator: the next such line before it, else the
    logind request, is, with '; <unit> (...) armed, not fired: ...' after it. Fallback: the unit started or logging
    nearest before the marker, a logind 'System is ...' line that names nobody, or with `full` (a read
    that shows the system's entries) and no marker, NO_SHUTDOWN and the last unit lines before the gap."""
    rows = _entries(out)
    scan = (_entries(commands) if commands else []) + rows
    if commands and all(e["ts"] is not None for e in scan):
        scan.sort(key=lambda e: e["ts"])
    found = fallback = None
    for e in scan:
        ident, msg = e["ident"], e["msg"]
        if ident == "sudo" and "COMMAND=" in msg \
                and not re.search(r"incorrect password|not allowed|not in sudoers", msg, re.I):
            cmd = _stop_command(msg.split("COMMAND=", 1)[1])
            if cmd:
                found = f"{_clip(msg.split(':', 1)[0], 40)} ran `{cmd}` via sudo (journal)"
        elif ident == "pkexec" and "Executing command" in msg and "[COMMAND=" in msg:
            cmd = _stop_command(msg.split("[COMMAND=", 1)[1].rsplit("]", 1)[0])
            if cmd:
                found = f"{_clip(msg.split(':', 1)[0], 40)} ran `{cmd}` via pkexec (journal)"
        elif ident == "systemd-logind" and "Power key pressed" in msg:
            found = "the power key (journal)"
        elif ident == "systemd-logind" and re.match(r"System is (rebooting|powering down|halting)", msg):
            fallback = f"a request to systemd-logind that names no user (journal: {_clip(msg, 60)})"
    if found:
        return found, fallback
    near = rows if window is None else _entries(window)
    marks = [e for e in rows if _marker(e)]
    if not marks:
        if sum(1 for line in (out or "").splitlines() if line.strip()) >= MARK_ENTRIES:
            return None, NO_MARKER   # the marker may lie before a full bounded read: no claim about it
        if full and near:
            last = [f"{_service(e)}: {_clip(e['msg'], 60)}" for e in near if _service(e) and e["msg"]][-2:]
            return None, NO_SHUTDOWN + (f"; last unit lines: {'; '.join(last)}" if last else "")
        return None, fallback
    at = marker_at(out)
    first = next((k for k, e in enumerate(near) if _marker(e) and (at is None or e["ts"] is None or e["ts"] >= at)),
                 None)
    before = near[:first] if first is not None else [e for e in near if at is None or (e["ts"] or 0) < at]
    start = unit_line = armed = None
    for e in reversed(before):
        if at is not None and e["ts"] is not None and e["ts"] < at - WINDOW_S:
            break
        unit = _service(e)
        m = _UNIT_START.match(e["msg"]) if e["ident"] == "systemd" else None
        started = (e["about"] or (m["unit"] if m else "")) if m or e["about"] and e["msg"].startswith("Start") else ""
        if started.startswith(("systemd-", "user@")):
            started = ""
        if (unit or started) and _STOP_WORDS.search(e["msg"]):
            quiet = None if unit else _armed(started, e, own)
            if quiet is None:
                hit = f"{unit or started} ({_clip(e['msg'])})"
                return (f"{hit}; {armed}" if armed else hit), fallback
            armed = armed or quiet
            continue
        if start is None and started:
            start = f"{started} ({_clip(e['msg'])})"
        if unit_line is None and unit and e["msg"]:
            unit_line = f"{unit} ({_clip(e['msg'])})"
    if armed:
        # The request itself names nobody: a fallback, so a host-cycle record or ledger entry still wins.
        return None, f"{fallback or 'a shutdown request that names no one (journal)'}; {armed}"
    return None, start or unit_line or fallback


def _ledger(alias: str | None, start: float, end: float) -> dict | None:
    from . import machine_ledger
    if not alias:
        return None
    hits = []
    for c in machine_ledger.changes([alias], open_only=False):
        # Recorded in the window, or a power-cycle intent planned for a time in it.
        times = [c.get("added")] + ([c.get("at")] if c.get("kind") == machine_ledger.POWER_CYCLE else [])
        inside = [t for t in times if isinstance(t, (int, float)) and start <= t <= end]
        if inside and _LEDGER_WORDS.search(str(c.get("what") or "")):
            hits.append((max(inside), c))
    if not hits:
        return None
    _, c = max(hits, key=lambda h: h[0])
    who = f" by project {_clip(c['by'], 40)}" if c.get("by") else ""
    return {"source": "ledger", "by": str(c.get("by") or ""), "entry": c.get("id"),
            "text": f"machine-change ledger entry #{c.get('id')}{who}: {_clip(c.get('what'), 80)}"}


def find(p, boot: str | None, boot_at: float | None, last_heartbeat: float | None,
         journal=None, sudo=None) -> dict:
    """{source: journal|host-cycle|ledger|unknown, text, ...} for the shutdown that ended boot `boot`;
    `boot_at`: this boot's start; `last_heartbeat`: the daemon's last sign of life on `boot`.
    `journal`/`sudo`: fixed answers to every read as this user/through sudo -n, read here when None.
    `unreadable`: why no read showed the system's entries (ttp doctor says it once; the notice only in
    its text)."""
    from . import hostcycle, machines
    fallback, why, how = None, "", ""
    try:
        got, ask, how, why = read(boot, journal, sudo)
        if got:
            window = commands = None
            if ask:
                c = ask(command_args())
                commands = c[1] if c and c[0] == 0 else None
                args = window_args(marker_at(got[1]))
                w = ask(args)
                window = w[1] if w and w[0] == 0 else None
                if window is not None and "-r" in args:
                    window = "\n".join(reversed(window.splitlines()))   # read newest first
            seen = {}

            def own(unit):   # once per unit: its 'Starting' and 'Started' lines both ask
                if unit not in seen:
                    u, n = ask(unit_args(unit)), ask(end_args())
                    seen[unit] = (u[1] if u and u[0] == 0 else None), (n[1] if n and n[0] == 0 else None)
                return seen[unit]
            found, fallback = parse(got[1], window, full=bool(how) and window is not None, commands=commands,
                                    own=own if ask else None)
            if found:
                return {"source": "journal", "text": found, **({"read": how} if how == "sudo" else {})}
    except Exception:
        why = why or "the journal read failed"
    extra = {"unreadable": why} if why else {}
    try:
        rec = hostcycle.planned(p, boot, boot_at)
        if rec:
            rid = rec.get("id") or Path(rec["path"]).parent.name
            return {"source": "host-cycle", "project": rec.get("project") or "", "record": rec["path"],
                    "text": f"`ttp drain --reboot` in project {rec.get('project') or '?'} "
                            f"(host-cycle record {rid}: {_clip(rec.get('reason') or 'no reason recorded', 80)})",
                    **extra}
    except Exception:
        pass
    try:
        end = boot_at or time.time()
        here = machines.here()
        hit = _ledger(here[0] if here else None, (last_heartbeat or end) - LEDGER_WINDOW_S, end)
        if hit:
            return {**hit, **extra}
    except Exception:
        pass
    if fallback:
        return {"source": "journal", "text": fallback, **({"read": how} if how == "sudo" else {}), **extra}
    return {"source": "unknown", "text": f"unknown (journal not readable: {why})" if why else "unknown", **extra}


def doctor_line(p) -> str:
    """One line for ttp doctor when the last reboot's journal could not be read, with what fixes it."""
    try:
        ini = (p.db.kv("boot_prev") or {}).get("initiator") or {}
    except Exception:
        return ""
    why = ini.get("unreadable") if isinstance(ini, dict) else None
    if not why:
        return ""
    fix = (" Adding this user to the systemd-journal group (root, once; tt-project:setup-box offers it) lets "
           "reboot notices name the cause." if "systemd-journal" in why else "")
    return f"reboot causes: the last reboot's journal was not readable ({why}).{fix}"
