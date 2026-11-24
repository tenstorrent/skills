#!/usr/bin/env python3
"""Gather the facts for one overseer tick, read-only. Prints plain text; see overseer.sh.

From the session's Claude Code transcripts: the main session and each subagent (task, age, tool calls
and last action in the window, finished or not) and the background shells each started (command, age,
whether its output and the logs it names grew in the window, exit status). From the host: which
processes hold /dev/tenstorrent/* and whether they work, load, memory, top processes, disk, and the
largest and fastest-growing paths under the run folder. Budget from inputs.json.

    overseer_facts.py --tt /path/to/tt-metal [--inputs inputs.json] [--interval-hours 1]
                      [--project-dir DIR] [--session ID] [--at 2026-11-24T10:00Z] [--started-at EPOCH]

--at reads the transcripts as they were at that time (for testing on a past run); host facts stay live.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import time

BG_START = re.compile(r"Command running in background with ID: (\w+)\. Output is being written to: (\S+?)\.?(?:\s|$)")
NOTE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
TAG = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.S)
LOG_PATH = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s*([^\s;|&<>()]+)|([^\s;|&<>()'\"=]+\.(?:log|out|txt|jsonl))\b")
CD = re.compile(r"\bcd\s+([^\s;&|]+)")


def ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def ago(sec):
    if sec is None:
        return "?"
    sec = int(max(sec, 0))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def gb(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def texts(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    yield b.get("text", "")
                elif b.get("type") == "tool_result":
                    yield from texts(b.get("content"))


def describe(tu):
    i = tu.get("input") or {}
    name = tu.get("name", "?")
    if name == "Bash":
        return f"Bash: {short(i.get('description') or i.get('command', ''), 120)}"
    if name == "Agent":
        return f"Agent: {short(i.get('description', ''), 80)}"
    for k in ("file_path", "path", "pattern", "url", "description"):
        if k in i:
            return f"{name}: {short(i[k], 100)}"
    return name


class Transcript:
    """One jsonl file, cut at `now`."""

    def __init__(self, path, now):
        self.path, self.records = path, []
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = ts(d.get("timestamp", "")) if isinstance(d, dict) else None
                    if t is not None and t <= now:
                        d["_t"] = t
                        self.records.append(d)
        except OSError:
            pass
        self.tool_uses, self.results, self.bg, self.notes, self.handback = {}, {}, [], [], False
        self.first = self.records[0]["_t"] if self.records else None
        self.last = None
        for d in self.records:
            kind, msg = d.get("type"), d.get("message") or {}
            if kind == "assistant":
                self.last = d["_t"]
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        b = dict(b, _t=d["_t"], _cwd=d.get("cwd"))
                        self.tool_uses[b.get("id")] = b
                        self.handback |= b.get("name") == "SubagentHandback"
                        if b.get("name") in ("TaskStop", "KillShell", "KillBash"):
                            i = b.get("input") or {}
                            tid = i.get("task_id") or i.get("shell_id")
                            if tid:
                                self.notes.append({"task-id": tid, "status": "stopped",
                                                   "summary": f"stopped with {b['name']}", "_t": d["_t"]})
            elif kind == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            self.results[b.get("tool_use_id")] = d["_t"]
                            for txt in texts(b.get("content")):
                                for m in BG_START.finditer(txt):
                                    self.bg.append({"id": m.group(1), "out": m.group(2),
                                                    "tool": b.get("tool_use_id"), "t": d["_t"]})
                self._notes(texts(content), d["_t"])
            elif kind == "queue-operation":
                self._notes([d.get("content") or ""], d["_t"])
            elif kind == "attachment":
                self._notes([(d.get("attachment") or {}).get("prompt") or ""], d["_t"])

    def _notes(self, strings, t):
        for txt in strings:
            if not isinstance(txt, str):
                continue
            for m in NOTE.finditer(txt):
                n = {k: v.strip() for k, v in TAG.findall(m.group(1))}
                n["_t"] = t
                self.notes.append(n)

    def calls_since(self, t0):
        return [u for u in self.tool_uses.values() if u["_t"] >= t0]

    def last_action(self):
        if not self.tool_uses:
            return None
        return max(self.tool_uses.values(), key=lambda u: u["_t"])

    def pending(self):
        """Tool calls with no result yet (a foreground call still running)."""
        return [u for k, u in self.tool_uses.items() if k not in self.results and u.get("name") != "SubagentHandback"]


def file_fact(path, now_live, win):
    try:
        st = os.stat(path)
    except OSError:
        return f"{path}: missing"
    last = ""
    try:
        with open(path, "rb") as f:
            f.seek(max(st.st_size - 4096, 0))
            lines = [x for x in f.read().decode(errors="replace").splitlines() if x.strip()]
            last = lines[-1] if lines else ""
    except OSError:
        pass
    grew = "written in window" if now_live - st.st_mtime <= win else f"unchanged for {ago(now_live - st.st_mtime)}"
    return f"{path}: {st.st_size} B, {grew}; last line: {short(last, 140)!r}"


def logs_in(cmd, cwd):
    base = cwd
    for m in CD.finditer(cmd):
        if m.group(1).startswith("/"):
            base = m.group(1)
            break
    seen = []
    for m in LOG_PATH.finditer(cmd):
        p = m.group(1) or m.group(2)
        if not p or "$" in p or p.startswith("/dev/") or p.startswith("&"):
            continue
        p = os.path.expanduser(p)
        if not p.startswith("/") and base:
            p = os.path.join(base, p)
        if p.startswith("/") and p not in seen:
            seen.append(p)
    return seen[:4]


def shells(tr, notes, now, win, live_now, at_mode):
    """Background shells started in this transcript, with their state."""
    out = []
    for b in tr.bg:
        tu = tr.tool_uses.get(b["tool"], {})
        cmd = (tu.get("input") or {}).get("command", "")
        n = notes.get(b["id"])
        if n and n["_t"] < now - win:
            continue  # finished before the window: not news
        line = [f"shell {b['id']} started {ago(now - b['t'])} ago: {short(cmd, 200)!r}"]
        if n:
            line.append(f"  ended {ago(now - n['_t'])} ago: {n.get('status')}: {short(n.get('summary', ''), 80)}")
        else:
            line.append("  still running (no completion notification)")
            for p in [b["out"]] + logs_in(cmd, tu.get("_cwd")):
                f = file_fact(p, live_now, win)
                if at_mode and os.path.exists(p) and os.stat(p).st_mtime > now:
                    f = f"{p}: modified after the --at time; growth unknown"
                line.append("  " + f)
        out.append("\n".join(line))
    return out


def transcript_facts(a, now, win, at_mode):
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    proj = a.project_dir or os.path.join(cfg, "projects", re.sub(r"[^A-Za-z0-9]", "-", a.tt))
    mains = sorted(glob.glob(os.path.join(proj, "*.jsonl")), key=os.path.getmtime)
    if a.session:
        mains = [os.path.join(proj, a.session + ".jsonl")]
    if not mains:
        raise SystemExit(f"no session transcripts in {proj}")
    main_path = mains[-1]
    sid = os.path.basename(main_path)[:-6]
    main = Transcript(main_path, now)
    subs = {}
    for p in sorted(glob.glob(os.path.join(proj, sid, "subagents", "agent-*.jsonl"))):
        t = Transcript(p, now)
        if t.records:
            subs[os.path.basename(p)[6:-6]] = (t, p)
    every = [main] + [t for t, _ in subs.values()]
    agents = {k: u for t in every for k, u in t.tool_uses.items() if u.get("name") == "Agent"}
    notes = {}
    for n in sorted((n for t in every for n in t.notes), key=lambda n: n["_t"]):
        if n.get("task-id"):
            notes[n["task-id"]] = n  # the latest one wins
    done_results = {k for t in every for k in t.results}
    live_now = time.time()

    out = [f"Transcripts: {proj} session {sid}" + (f" (as of {a.at})" if at_mode else "")]
    calls = main.calls_since(now - win)
    la = main.last_action()
    out.append(f"Main session: started {ago(now - main.first)} ago; last activity {ago(now - main.last)} ago; "
               f"{len(calls)} tool calls in window; last action {ago(now - la['_t']) if la else '?'} ago: "
               f"{describe(la) if la else '-'}")
    for u in main.pending():
        if u.get("name") != "Agent":
            out.append(f"  main tool call still running since {ago(now - u['_t'])}: {describe(u)}")
    out += ["  " + s.replace("\n", "\n  ") for s in shells(main, notes, now, win, live_now, at_mode)]

    finished_recent, finished_old = [], 0
    for aid, (t, p) in subs.items():
        meta = {}
        try:
            meta = json.load(open(p[:-6] + ".meta.json"))
        except (OSError, ValueError):
            pass
        tu = agents.get(meta.get("toolUseId"))
        if not tu:
            first = next((x for x in texts((t.records[0].get("message") or {}).get("content"))), "")
            tu = next((u for u in agents.values() if (u.get("input") or {}).get("prompt", "")[:400] == first[:400]), None)
        desc = meta.get("description") or ((tu or {}).get("input") or {}).get("description") or aid
        n = notes.get(aid)
        waiting_note = n and "waiting on" in n.get("summary", "")
        done = t.handback or (n and n.get("status") in ("completed", "failed", "killed", "stopped") and not waiting_note) \
            or (tu and tu.get("id") in done_results and not (tu.get("input") or {}).get("run_in_background")) \
            or (not at_mode and meta.get("finished") and not t.bg and not n)
        if done:
            if (t.last or 0) >= now - win:
                finished_recent.append(desc)
            else:
                finished_old += 1
            continue
        calls = t.calls_since(now - win)
        la = t.last_action()
        s = [f"Subagent {desc!r} ({aid}): running {ago(now - t.first)}; last activity {ago(now - t.last) if t.last else '?'} ago; "
             f"{len(calls)} tool calls in window; last action {ago(now - la['_t']) if la else '?'} ago: {describe(la) if la else '-'}"]
        if waiting_note:
            s.append(f"  told main {ago(now - n['_t'])} ago: {short(n.get('summary'), 100)}")
        for u in t.pending():
            s.append(f"  tool call still running since {ago(now - u['_t'])}: {describe(u)}")
        s += ["  " + x.replace("\n", "\n  ") for x in shells(t, notes, now, win, live_now, at_mode)]
        out.append("\n".join(s))
    out.append(f"Subagents finished in window: {', '.join(repr(d) for d in finished_recent) or 'none'}; "
               f"finished earlier: {finished_old}")
    return out


def proc_cpu():
    cpu = {}
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
                cpu[int(p)] = (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
            except (OSError, IndexError, ValueError):
                pass
    return cpu


def proc_info(pid, run):
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        args = "?"
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = ""
    try:
        rss = int(re.search(r"VmRSS:\s+(\d+)", open(f"/proc/{pid}/status").read()).group(1)) / 1e6
    except (OSError, AttributeError):
        rss = 0.0
    mine = " [run]" if run and (cwd.startswith(run) or run in args) else ""
    return args, rss, mine


def host_facts(a, sample):
    out = []
    run = a.tt
    c0 = proc_cpu()
    time.sleep(sample)
    c1 = proc_cpu()
    rate = {p: (c1[p] - c0.get(p, c1[p])) / sample for p in c1}

    holders = {}
    devs = sorted(glob.glob("/dev/tenstorrent/[0-9]*"))
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{p}/fd"):
                tgt = os.readlink(f"/proc/{p}/fd/{fd}")
                if tgt.startswith("/dev/tenstorrent/"):
                    holders.setdefault(int(p), set()).add(tgt.rsplit("/", 1)[1])
        except OSError:
            continue
    out.append(f"Devices ({len(devs)}: {' '.join(os.path.basename(d) for d in devs)}); holders visible to this user:"
               + ("" if holders else " none"))
    for pid, ds in sorted(holders.items()):
        args, rss, mine = proc_info(pid, run)
        try:
            age = time.time() - os.stat(f"/proc/{pid}").st_mtime
        except OSError:
            age = None
        s = (f"  pid {pid}{mine} holds {','.join(sorted(ds))}; up {ago(age)}; CPU now {rate.get(pid, 0) * 100:.0f}%, "
             f"total CPU {c1.get(pid, 0):.0f} s; RSS {rss:.1f} GB: {short(args, 160)!r}")
        for fd in ("1", "2"):
            try:
                log = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if os.path.isfile(log):
                s += f"\n    log {log}: last written {ago(time.time() - os.path.getmtime(log))} ago"
                try:
                    with open(log, "rb") as f:
                        f.seek(max(os.path.getsize(log) - 65536, 0))
                        tail = f.read().decode(errors="replace").splitlines()
                    run_lines = [x for x in tail if "Running:" in x]
                    if run_lines:
                        s += f"; last 'Running:' line: {short(run_lines[-1], 200)!r}"
                except OSError:
                    pass
                break
        out.append(s)

    load = open("/proc/loadavg").read().split()[:3]
    mem = dict(re.findall(r"(\w+):\s+(\d+)", open("/proc/meminfo").read()))
    out.append(f"Host: load {' '.join(load)} on {os.cpu_count()} CPUs; memory available "
               f"{int(mem.get('MemAvailable', 0)) / 1e6:.0f} of {int(mem.get('MemTotal', 0)) / 1e6:.0f} GB")
    top = sorted(rate, key=rate.get, reverse=True)[:6]
    out.append("  top CPU now: " + "; ".join(
        f"{p}{proc_info(p, run)[2]} {rate[p] * 100:.0f}% {short(proc_info(p, run)[0], 70)!r}" for p in top if rate[p] > 0))
    rss = sorted(((proc_info(p, run), p) for p in c1), key=lambda x: x[0][1], reverse=True)[:5]
    out.append("  top memory: " + "; ".join(f"{p}{i[2]} {i[1]:.1f} GB {short(i[0], 70)!r}" for i, p in rss))
    return out


def disk_facts(a, win, budget_s):
    out = []
    for p in dict.fromkeys([a.tt, os.path.expanduser("~"), "/tmp"]):
        try:
            u = shutil.disk_usage(p)
            out.append(f"Disk {p}: {u.free / 1e9:.0f} GB free of {u.total / 1e9:.0f} GB")
        except OSError:
            pass
    size, recent, start, partial = {}, {}, time.time(), False
    cutoff = time.time() - win
    root_dev = os.stat(a.tt).st_dev
    stack = [(a.tt, ())]
    while stack:
        if time.time() - start > budget_s:
            partial = True
            break
        d, parts = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if e.is_dir(follow_symlinks=False):
                if st.st_dev == root_dev:
                    stack.append((e.path, parts + (e.name,)))
                continue
            keys = [parts[:i] for i in range(1, min(len(parts), 4) + 1)]
            for k in keys:
                size[k] = size.get(k, 0) + st.st_blocks * 512
                if st.st_mtime >= cutoff:
                    recent[k] = recent.get(k, 0) + st.st_size

    def pick(dct, n):
        chosen = []
        for k in sorted(dct, key=dct.get, reverse=True):
            if any(k[: len(c)] == c or c[: len(k)] == k for c in chosen):
                continue
            chosen.append(k)
            if len(chosen) == n:
                break
        return [f"{'/'.join(k)} {gb(dct[k])}" for k in chosen if dct[k] > 0]

    note = " (scan cut short)" if partial else ""
    out.append(f"  largest under {a.tt}{note}: " + "; ".join(pick({k: v for k, v in size.items() if len(k) <= 3}, 6)))
    out.append(f"  written in window under {a.tt}{note}: " + ("; ".join(pick(recent, 5)) or "nothing"))
    return out


def budget_facts(a, now):
    d = {}
    try:
        d = json.load(open(a.inputs))
    except (OSError, ValueError, TypeError):
        pass
    start = ts(d["started_at"]) if isinstance(d.get("started_at"), str) else None
    start = start or a.started_at
    hours = d.get("budget_hours")
    if not (start and hours):
        return [f"Budget: unknown (budget_hours={hours}, started_at={d.get('started_at') or a.started_at})"]
    used = (now - start) / 3600
    return [f"Budget: {used:.1f} h used of {hours} h, {hours - used:.1f} h left"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tt", required=True, help="the tt-metal checkout the session runs in (the run folder)")
    ap.add_argument("--inputs")
    ap.add_argument("--interval-hours", type=float, default=1.0)
    ap.add_argument("--project-dir")
    ap.add_argument("--session")
    ap.add_argument("--at", help="read transcripts as of this UTC time (testing)")
    ap.add_argument("--started-at", type=float, help="epoch seconds, if inputs.json has no started_at")
    ap.add_argument("--sample-seconds", type=float, default=5.0)
    ap.add_argument("--scan-seconds", type=float, default=300.0)
    a = ap.parse_args()
    a.tt = os.path.abspath(a.tt)
    at_mode = bool(a.at)
    now = ts(a.at) if a.at else time.time()
    if now is None:
        raise SystemExit(f"bad --at {a.at}")
    win = a.interval_hours * 3600
    out = [f"Overseer facts at {dt.datetime.fromtimestamp(time.time(), dt.timezone.utc):%Y-%m-%dT%H:%MZ}; "
           f"window: last {a.interval_hours:g} h"]
    out += transcript_facts(a, now, win, at_mode)
    for part in (lambda: host_facts(a, a.sample_seconds), lambda: disk_facts(a, win, a.scan_seconds),
                 lambda: budget_facts(a, now)):
        try:
            out += part()
        except Exception as e:  # host facts are best-effort; transcripts are not
            out.append(f"(could not gather: {type(e).__name__}: {e})")
    if at_mode:
        out.append("Note: devices, host and disk lines are live (now), not as of --at.")
    print("\n".join(out))


if __name__ == "__main__":
    main()
