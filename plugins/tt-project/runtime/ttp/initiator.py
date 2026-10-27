# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who or what started the shutdown that ended the previous boot: best effort, model-free.

The daemon asks once per boot (daemon._note_boot) and keeps the answer in the boot record, so a
question such as "did tt-project reboot this host?" is answered by the record itself. Sources, in
order; each is bounded and never raises:

1. The previous boot's journal (`journalctl -b <boot> -o json`, its last TAIL_ENTRIES entries), read
   as this user, else through `sudo -n` (bounded, never prompts) when that showed only the user's own
   entries and `sudo -n true` works: a sudo or pkexec COMMAND that runs reboot, shutdown, poweroff or
   halt (or `systemctl reboot|poweroff|halt|kexec`), or systemd-logind's 'Power key pressed'; the last
   one wins. Else the shutdown marker (logind's 'System is rebooting/powering down', systemd reaching
   the reboot or poweroff target, a 'shutdown scheduled' line) and, within WINDOW_S before it, the
   nearest system unit line that names a power cycle, reboot or shutdown: '<unit> (<message>)'.
2. A tt-project host-cycle record (`ttp drain --reboot`) from any project on this host that ended that
   boot (hostcycle.planned).
3. An entry in the shared machine-change ledger for this host's machine alias, recorded from
   LEDGER_WINDOW_S before the shutdown (the boot's last heartbeat) up to this boot, that names a power
   cycle or a reboot: the latest one, with its entry id and the project that recorded it.

Else the unit started or logging nearest before the marker, a bare logind line, or for a boot whose
journal ends with no marker 'no clean shutdown logged (...)' and its last unit lines. Else 'unknown',
or 'unknown (journal not readable: <why>)'. Tests set TTP_TEST_NO_JOURNAL so the real journalctl and
sudo never run; they stub run_journal and run_sudo_journal instead.
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
SUDO_TIMEOUT_S = 10   # sudo -n journalctl; `sudo -n true` before it takes SUDO_CHECK_S
SUDO_CHECK_S = 5
TAIL_ENTRIES = 3000   # the last entries of the boot: its shutdown and the minutes before it
WINDOW_S = 600        # how far before the shutdown marker a unit line may name the initiator
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


def _clip(text, chars: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= chars else text[:chars - 1] + "…"


def _journalctl(boot: str | None) -> list[str]:
    hexid = (boot or "").replace("-", "")
    which = hexid if re.fullmatch(r"[0-9a-f]{32}", hexid) else "-1"
    return ["journalctl", "-b", which, "--no-pager", "-o", "json", "-n", str(TAIL_ENTRIES),
            f"--output-fields={FIELDS}"]


def _run(argv: list[str], timeout: float) -> tuple[int, str, str] | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.returncode, r.stdout or "", r.stderr or ""


def run_journal(boot: str | None) -> tuple[int, str, str] | None:
    """(exit code, stdout, stderr) of journalctl as this user for boot `boot` (the previous boot when it
    is no journal boot id): the boot's last TAIL_ENTRIES entries as JSON, only the fields parse() reads;
    None when it could not run."""
    if os.environ.get("TTP_TEST_NO_JOURNAL"):
        return None
    return _run(_journalctl(boot), JOURNAL_TIMEOUT_S)


def run_sudo_journal(boot: str | None) -> tuple[int, str, str] | bool | None:
    """The same read through `sudo -n` (never prompts, stores nothing); False when `sudo -n true` fails
    (no passwordless sudo here), None when the read could not run."""
    if os.environ.get("TTP_TEST_NO_JOURNAL"):
        return False
    ok = _run(["sudo", "-n", "true"], SUDO_CHECK_S)
    if not ok or ok[0] != 0:
        return False
    return _run(["sudo", "-n", *_journalctl(boot)], SUDO_TIMEOUT_S)


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
    return ""


def read(boot: str | None, journal=None, sudo=None) -> tuple[tuple[int, str, str] | None, str, str]:
    """(the read to parse, how it was read: 'user', 'sudo' or '' when none showed the system's entries,
    why none did). As this user first, then `sudo -n` when that saw only the user's own entries.
    `journal`/`sudo`: run_journal's/run_sudo_journal's answers, read here when None."""
    got = run_journal(boot) if journal is None else journal
    if not got:
        return None, "", "journalctl did not answer, missing or timed out"
    why = _limited(got)
    if not why:
        return got, "user", ""
    if why != "user not in systemd-journal":
        return got, "", why
    s = run_sudo_journal(boot) if sudo is None else sudo
    if s is False:
        return got, "", f"{why} and no passwordless sudo"
    if not s or _limited(s):
        return got, "", f"{why}, and sudo -n journalctl did not read it either"
    return s, "sudo", ""


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


def parse(out: str, full: bool = False) -> tuple[str | None, str | None]:
    """(initiator, fallback) from the previous boot's journal. Initiator: the last sudo/pkexec stop
    command or power-key press, else the system unit line nearest before the shutdown marker (within
    WINDOW_S) that names a power cycle, reboot or shutdown. Fallback: the unit started or logging
    nearest before the marker, a logind 'System is ...' line that names nobody, or with `full` (a read
    that shows the system's entries) and no marker, NO_SHUTDOWN and the last unit lines before the gap."""
    rows = _entries(out)
    found = fallback = None
    for e in rows:
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
    marks = [i for i, e in enumerate(rows) if _marker(e)]
    if not marks:
        if full and rows:
            last = [f"{_service(e)}: {_clip(e['msg'], 60)}" for e in rows if _service(e) and e["msg"]][-2:]
            return None, NO_SHUTDOWN + (f"; last unit lines: {'; '.join(last)}" if last else "")
        return None, fallback
    # The shutdown's first marker: a 'shutdown scheduled' line may come minutes before logind's.
    end = rows[marks[-1]]["ts"]
    first = next(i for i in marks if end is None or rows[i]["ts"] is None or rows[i]["ts"] >= end - WINDOW_S)
    at = rows[first]["ts"]
    near = start = unit_line = None
    for e in reversed(rows[:first]):
        if at is not None and e["ts"] is not None and e["ts"] < at - WINDOW_S:
            break
        unit = _service(e)
        if unit and e["msg"] and _STOP_WORDS.search(e["msg"]):
            near = f"{unit} ({_clip(e['msg'])})"
            break
        if start is None and e["ident"] == "systemd":
            m = _UNIT_START.match(e["msg"])
            u = e["about"] or (m["unit"] if m else "")
            if m and not u.startswith(("systemd-", "user@")):
                start = f"{u} ({_clip(e['msg'])})"
        if unit_line is None and unit and e["msg"]:
            unit_line = f"{unit} ({_clip(e['msg'])})"
    if near:
        return near, fallback
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
    `journal`/`sudo`: run_journal's/run_sudo_journal's answers, read here when None. `unreadable`: why
    no read showed the system's entries (ttp doctor says it once; the notice only in its text)."""
    from . import hostcycle, machines
    fallback, why, how = None, "", ""
    try:
        got, how, why = read(boot, journal, sudo)
        if got:
            found, fallback = parse(got[1], full=bool(how))
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
