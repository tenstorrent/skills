# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp` — create, find, talk to and operate tt-project projects.

Every project command takes the project NAME. If the registry says the project lives on another
machine, the command is forwarded over ssh to that machine's copy of the project's own `ttp`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as pysecrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, poll_s
from .db import chat_floor
from . import schedule as sched
from .project import (FOLDER, NAME_RE, Project, hostname, load_registry, load_secrets, register, save_secret,
                      write_json)

RUNTIME = Path(__file__).resolve().parent.parent              # .../runtime (plugin or project copy)
PLUGIN_ROOT = RUNTIME.parent                                   # plugin root, or a project's harness/
MARKER = "tt-project://{name}@{host}:{dir}"


def die(msg: str, code: int = 2) -> None:
    print(f"ttp: {msg}", file=sys.stderr)
    sys.exit(code)


# locating projects --------------------------------------------------------------------------------
def local_project(name: str) -> Project | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") in (None, hostname()) and Path(entry["dir"]).is_dir():
        return Project(entry["dir"])
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        cand = Project(d)
        if cand.exists() and cand.name == name:
            return cand
    return None


def remote_entry(name: str) -> dict | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") and entry["host"] != hostname():
        return entry
    return None


def remote_hosts() -> list[str]:
    """The other machines this user's projects run on (projects created with --host)."""
    here = hostname()
    return sorted({e.get("ssh") or e["host"] for e in load_registry().get("projects", {}).values()
                   if isinstance(e, dict) and e.get("host") and e["host"] != here})


def forward_listen(entry: dict, argv: list[str]) -> int:
    """A remote listener outlives network drops: a laptop changes networks, sleeps and wakes.

    ssh failing (255) means this machine lost the path, not that the project stopped, so wait and
    reconnect. The listener left on the far side exits on its own once its session is gone, and
    the new one replaces it if it has not yet.
    """
    delay, told = 5.0, False
    while True:
        rc = forward(entry, argv, quiet=told)
        if rc != 255:
            return rc
        if not told:
            print("ttp: will keep retrying and deliver messages once the connection is back", file=sys.stderr)
            told = True
        time.sleep(delay)
        delay = min(delay * 2, 120.0)


def forward(entry: dict, argv: list[str], quiet: bool = False) -> int:
    """Run this same command on the project's machine, streaming its output."""
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    cmd = " ".join(shlex.quote(a) for a in [remote_ttp, *argv])
    host = entry.get("ssh") or entry["host"]
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, cmd], stderr=subprocess.PIPE,
                       text=True)
    if r.returncode == 255:   # ssh itself failed: the project is fine, this machine cannot reach it
        if not quiet:
            why = (r.stderr.strip().splitlines() or ["unknown ssh error"])[-1]
            print(f"ttp: cannot reach {host} right now ({why}). The project keeps running there; "
                  f"try again once this machine is back on that network.", file=sys.stderr)
    elif r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


def search_transcripts(name: str) -> list[dict]:
    """Find a project's location markers in agent chat logs (Claude Code, Codex, Cursor)."""
    needle = f"tt-project://{name}@"
    roots = [Path.home() / ".claude" / "projects", Path.home() / ".codex" / "sessions", Path.home() / ".cursor" / "projects"]
    hits: dict[str, dict] = {}
    rx = re.compile(re.escape(needle) + r"([A-Za-z0-9._-]+):(/[^\s\"'\\]+)")
    for root in roots:
        if not root.is_dir():
            continue
        tool = shutil.which("rg")
        cmd = [tool, "-l", "-F", needle, str(root)] if tool else ["grep", "-rlF", needle, str(root)]
        try:
            files = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.split()
        except subprocess.SubprocessError:
            continue
        for f in files:
            try:
                text = Path(f).read_text(errors="replace")
            except OSError:
                continue
            for m in rx.finditer(text):
                hits[f"{m.group(1)}:{m.group(2)}"] = {"host": m.group(1), "dir": m.group(2).rstrip(".,)"),
                                                     "seen_in": f, "mtime": Path(f).stat().st_mtime}
    return sorted(hits.values(), key=lambda h: -h["mtime"])


def resolve(name: str) -> tuple[Project | None, dict | None]:
    p = local_project(name)
    if p:
        return p, None
    entry = remote_entry(name)
    if entry:
        return None, entry
    for hit in search_transcripts(name):
        if hit["host"] == hostname() and Project(hit["dir"]).exists():
            register(name, {"host": hostname(), "dir": str(Project(hit["dir"]).root)})
            return Project(hit["dir"]), None
        if hit["host"] != hostname():
            entry = {"host": hit["host"], "dir": hit["dir"]}
            register(name, entry)
            return None, entry
    return None, None


def need(name: str, argv: list[str]) -> Project:
    p, entry = resolve(name)
    if entry:
        if argv and argv[0] == "listen":
            sys.exit(forward_listen(entry, argv))
        sys.exit(forward(entry, argv))
    if not p:
        die(f"no project named {name!r} on this machine or in the registry; try `ttp find {name}`")
    return p


# creating -----------------------------------------------------------------------------------------
def detect_provider() -> str:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("CURSOR_") for k in os.environ):
        return "cursor"
    return "claude"


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


SOURCE_FILE = "SOURCE_COMMIT"      # in runtime/ttp/: the git commit `ttp setup` installed this runtime from


def _checkout_commit(root: Path) -> str:
    """The commit of the git checkout the plugin at `root` is tracked in ("-dirty" with local edits
    under it), or "unknown" when it is not in one (a plugin cache, a copy, no git)."""
    def run(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
    if not run("ls-files", "--", "runtime/ttp/__init__.py"):      # not a checkout of this plugin
        return "unknown"
    commit = run("rev-parse", "--short=12", "HEAD")
    if not commit:
        return "unknown"
    return commit + ("-dirty" if run("status", "--porcelain", "--", ".") else "")


def recorded_commit(runtime: Path) -> str:
    """The source commit `ttp setup` recorded in an installed runtime, or "unknown"."""
    try:
        return (runtime / "ttp" / SOURCE_FILE).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def source_commit() -> str:
    """The git commit this runtime came from: recorded at install, else read from the plugin checkout."""
    got = recorded_commit(RUNTIME)
    if got == "unknown" and (PLUGIN_ROOT / "template").is_dir():   # a harness copy is not the plugin
        got = _checkout_commit(PLUGIN_ROOT)
    return got


def _runtime_version(runtime: Path) -> str:
    m = re.search(r'__version__ = "([^"]+)"', (runtime / "ttp" / "__init__.py").read_text())
    return m.group(1) if m else "unknown"


def bootstrap(root: Path, name: str, brief: str, provider: str) -> Project:
    p = Project(root)
    if p.exists():
        if p.name != name:
            die(f"{p.base} already holds project {p.name!r}")
        return p
    p.base.mkdir(parents=True, exist_ok=True)
    # The folder ignores itself, so no enclosing repository can ever commit project state.
    (p.base / ".gitignore").write_text("*\n")
    for d in (p.state, p.runs, p.logs, p.worktrees, p.memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    template = PLUGIN_ROOT / "template"
    _copy_tree(RUNTIME, p.harness / "runtime")
    (p.harness / "runtime" / "ttp" / SOURCE_FILE).write_text(source_commit() + "\n")
    _copy_tree(template / "prompts", p.harness / "prompts")
    _copy_tree(template / "bin", p.harness / "bin")
    for f in (p.harness / "bin").iterdir():
        f.chmod(0o755)
    (p.harness / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q", "-b", "upstream", str(p.harness)], check=True)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"tt-project template {__version__}")
    _git(p.harness, "checkout", "-q", "-b", "main")
    charter = (template / "CHARTER.md").read_text().replace("{{NAME}}", name).replace(
        "{{DATE}}", time.strftime("%Y-%m-%d")).replace("{{BRIEF}}", brief.strip() or "(no description given yet)")
    p.charter_path.write_text(charter)
    p.memory_index.write_text("# Memory index\n")
    cfg = {"name": name, "created": time.time(), "root": str(p.root), "host": hostname(),
           "core_provider": provider, "tt_project_version": __version__,
           "id": pysecrets.token_hex(4),
           # New projects only: DEFAULT_CONFIG keeps it off, so existing projects are unchanged.
           "providers": {"claude": {"worker_isolation": True}}}
    sec = load_secrets()
    if (sec.get("slack") or {}).get("bot_token"):
        cfg["notify"] = {"slack": True}
    write_json(p.config_path, cfg)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"project {name}: charter and config")
    db = p.db
    for s in json.loads((template / "recurring.json").read_text()):
        sched.upsert(db, s["name"], s["kind"], s["every"], s.get("at"), s.get("enabled", True),
                     s.get("budget_usd_day"), s.get("description", ""), s.get("payload", {}))
    db.set_meta("name", name)
    db.post("in", "Project created. Brief:\n" + (brief.strip() or "(none)") +
            "\n\nRead the charter, restate the goals, success criteria and restrictions as you understand "
            "them, list what you still need to know, and start the first tasks that do not depend on answers.",
            chat=None, channel="system", kind="user")
    return p


def cmd_new(a) -> None:
    if not NAME_RE.match(a.name):
        die("project names use letters, digits, '.', '_' and '-' (max 63)")
    brief = a.describe or ""
    if a.describe_file:
        brief = sys.stdin.read() if a.describe_file == "-" else Path(a.describe_file).expanduser().read_text()
    if a.host and a.host not in (hostname(), "localhost"):
        return new_remote(a, brief)
    root = Path(a.dir).expanduser().resolve() if a.dir else _default_root()
    p = bootstrap(root, a.name, brief, a.provider or detect_provider())
    register(a.name, {"host": hostname(), "dir": str(p.root)})
    ident = subprocess.run(["git", "-C", str(p.root), "config", "user.email"], capture_output=True, text=True)
    if ident.returncode != 0 or not ident.stdout.strip():
        print("warning: git has no user.email here, so workers cannot commit. Set one "
              "(git config --global user.name/user.email) or tell the coordinator which identity to use.")
    how = "service not installed (--no-service)"
    if not a.no_service:
        from .service import install
        how = install(p)
    _wait_for_daemon(p)
    print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    print(f"created {a.name} in {p.base} · daemon via {how}")
    print(web_line(p))


def _default_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip() if out.returncode == 0 else os.getcwd())


def push_secrets(host: str) -> str:
    """Copy this user's saved keys to another machine (same user), merging with what is there.
    Travels over ssh stdin, lands mode 0600, never on a command line or in a project folder."""
    sec = load_secrets()
    if not sec:
        return "no saved keys to copy"
    merge = ("import json,os,sys;h=os.path.expanduser('~/.tt-project');os.makedirs(h,mode=0o700,exist_ok=True);"
             "p=os.path.join(h,'secrets.json');old={}\n"
             "try: old=json.load(open(p))\nexcept Exception: pass\n"
             "new=json.load(sys.stdin);new.update({k:v for k,v in old.items() if k not in new});"
             "fd=os.open(p+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(fd,json.dumps(new).encode());"
             "os.close(fd);os.replace(p+'.tmp',p);print(','.join(sorted(new)))")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"python3 -c {shlex.quote(merge)}"],
                       input=json.dumps(sec), text=True, capture_output=True)
    return f"copied keys ({r.stdout.strip()}) to {host}" if r.returncode == 0 else f"could not copy keys: {r.stderr[-200:]}"


def ship_runtime(host: str) -> str:
    """Copy this runtime and template to ~/.tt-project/lib/<version> on another machine and make it
    that machine's installed `ttp`. Returns the remote launcher path."""
    stage = f".tt-project/lib/{__version__}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"rm -rf ~/{stage} && mkdir -p ~/{stage}"])
    mac = ["--no-xattrs", "--no-mac-metadata"] if sys.platform == "darwin" else []
    tar = subprocess.Popen(["tar", *mac, "--exclude", "__pycache__", "-C", str(PLUGIN_ROOT), "-czf", "-",
                            "runtime", "template", "bin"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env={**os.environ, "COPYFILE_DISABLE": "1"})
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"tar -C ~/{stage} -xzf -"], stdin=tar.stdout)
    tar.wait()
    mark = f"printf '%s\\n' {shlex.quote(source_commit())} > ~/{stage}/runtime/ttp/{SOURCE_FILE}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"{mark} && ~/{stage}/bin/ttp setup >/dev/null"])
    return f"~/{stage}/bin/ttp"


def new_remote(a, brief: str) -> None:
    """Ship this runtime to the other machine and create the project there."""
    if not a.dir:
        die("--dir is required with --host (the project root on that machine)")
    host = a.host
    stage = f".tt-project/lib/{__version__}"
    ship_runtime(host)
    if load_secrets() and not a.no_secrets:
        print(push_secrets(host))
    from . import machines as mm
    print(mm.push(host))        # its daemon reads the machines list there
    args = [f"~/{stage}/bin/ttp", "new", a.name, "--dir", a.dir, "--provider", a.provider or detect_provider()]
    if a.no_service:
        args.append("--no-service")
    remote = " ".join(shlex.quote(x) if not x.startswith("~/") else x for x in args) + " --describe-file -"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], input=brief, text=True)
    if r.returncode != 0:
        die(f"remote creation on {host} failed", r.returncode)
    register(a.name, {"host": host, "dir": a.dir})


def _wait_for_daemon(p: Project, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (p.db.kv("web") or {}).get("port"):
            return
        time.sleep(0.5)


def web_line(p: Project) -> str:
    from .web import token
    port = (p.db.kv("web") or {}).get("port") or p.config().get("web", {}).get("port")
    if not port:
        return "web app: not running yet (check `ttp status`)"
    return f"web app: http://127.0.0.1:{port}/#token={token(p)}"


# talking ------------------------------------------------------------------------------------------
def cmd_connect(a) -> None:
    p = need(a.name, sys.argv[1:])
    chat = a.chat or f"c{pysecrets.token_hex(3)}"
    p.db.x("INSERT INTO chats(id,created,label,host,last_active,last_read) VALUES(?,?,?,?,?,"
           "(SELECT COALESCE(MAX(id),0) FROM messages)) ON CONFLICT(id) DO UPDATE SET last_active=excluded.last_active",
           (chat, time.time(), a.label or "", os.environ.get("TTP_CLIENT_HOST", ""), time.time()))
    print(MARKER.format(name=p.name, host=hostname(), dir=p.root))
    print(f"chat: {chat}")
    print(status_text(p))
    print(web_line(p))


def cmd_say(a) -> None:
    p = need(a.name, sys.argv[1:])
    text = a.text if a.text != "-" else sys.stdin.read()
    if not text.strip():
        die("empty message")
    mid = p.db.post("in", text.strip(), chat=a.chat or None, channel="chat", kind="user")
    if a.chat:
        p.db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
    print(f"sent (#{mid}); the coordinator answers in this chat when it has decided")


def cmd_listen(a) -> None:
    p = need(a.name, sys.argv[1:])
    db = p.db
    if a.ack is not None:
        # Acknowledged ids only move forward and never past the newest message.
        db.x("UPDATE chats SET last_read=MAX(COALESCE(last_read,0), MIN(?, (SELECT COALESCE(MAX(id),0) "
             "FROM messages))) WHERE id=?", (a.ack, a.chat))
    row = db.one("SELECT last_read, min_severity FROM chats WHERE id=?", (a.chat,))
    if not row:
        die(f"unknown chat {a.chat}; run `ttp connect {a.name}` first")
    floor = chat_floor(row["min_severity"], p.config()["notify"].get("chat_min_severity"))
    after = int(row["last_read"] or 0)
    # One listener per chat, and the newest wins. Two would race for the same messages, and one
    # printing to nowhere (a lost background task, a dropped ssh session) would silently mark them
    # read. Whoever arms a listener last is the one that wants the messages.
    lock = p.state / f"listen-{a.chat}.pid"
    try:
        other = int(lock.read_text())
    except (OSError, ValueError):
        other = 0
    if other and other != os.getpid() and _listener_alive(other, a.chat):
        try:
            os.kill(other, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not _listener_alive(other, a.chat):
                break
            time.sleep(0.1)
        print(f"ttp: replaced an older listener for chat {a.chat} (pid {other})", file=sys.stderr)
    lock.write_text(str(os.getpid()))
    try:
        _listen_loop(p, db, a, after, floor)
    finally:
        try:
            if lock.read_text().strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def _listener_alive(pid: int, chat: str) -> bool:
    """True if pid is a live `ttp listen` for this chat (not a recycled pid)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "listen" in cmd and chat in cmd


def _listen_loop(p: Project, db, a, after: int, floor: str) -> None:
    from .web import cleared
    deadline = time.time() + a.timeout if a.timeout else None
    parent = os.getppid()
    while True:
        if os.getppid() != parent:
            return      # whoever started this listener is gone; nobody would read what it prints
        # The high-water mark comes first: a reply posted after it is left for the next pass,
        # never skipped as if it had been read.
        top = db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"]
        msgs = [m for m in db.unread_for_chat(a.chat, after, floor, upto=top) if not cleared(db, m, time.time())]
        for m in msgs:
            who = "coordinator" if m["chat"] else f"{p.name} ({m['kind']}, {m['severity']})"
            print(f"[#{m['id']} {who}] {m['text']}", flush=True)
        if top > after:
            after = top
            # With --ack, only an acknowledgement marks messages read, so what was printed to a
            # reader that is gone comes back on the next listen.
            if a.ack is None:
                db.x("UPDATE chats SET last_read=? WHERE id=?", (after, a.chat))
        if msgs:
            db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
            if a.once:
                return
        if deadline and time.time() > deadline:
            return
        time.sleep(poll_s(2))


def status_text(p: Project) -> str:
    from .web import at, gate_detail, health, since
    db = p.db
    state = daemon_state(p)
    gates = db.kv("gates", {})
    h = health(p, db, alive=state == "running")
    now = time.time()
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    head = f"{p.name}: daemon {state}" + (" (paused)" if db.kv("paused") else "")
    head += " · tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none yet")
    lines = [head]
    s = h["spend"]
    top = f" · top 7d: {s['top_7d']['source']} ${s['top_7d']['usd']:.2f}" if s["top_7d"] else ""
    live = f" · ~${s['in_flight']:.2f} so far in running work" if s.get("in_flight") else ""
    lines.append(f"spend: ${s['spent_24h']:.2f} last 24h, ${s['spent_7d']:.2f} last 7d{live}{top}")
    for prov, g in gates.items():
        lines.append(f"budget {prov}: {g['level']} · {gate_detail(g)}" + (f" — {'; '.join(g['reasons'])}" if g["reasons"] else ""))
    c = h["coordinator"]
    coord = "coordinator: no turn yet"
    if c["last_turn"]:
        coord = f"coordinator: last turn {int((now - c['last_turn']) // 60)} min ago" + (
            f" ({c['last_status']})" if c["last_status"] else "")
    if c["failures"]:
        coord += f" · {c['failures']} failed in a row"
    if c["backoff_until"]:
        coord += f" · retry at {at(c['backoff_until'], now)}"
    if c["idle_wake"]:
        coord += f" · next idle check {at(c['idle_wake'], now)}"
    lines.append(coord)
    for pp in h["providers_paused"]:
        lines.append(f"{pp['provider']} paused until {at(pp['until'], now)}: {pp['note']} — fix: {pp['fix']}")
    for pr in h["resources_paused"]:
        lines.append(f"resource {pr['resource']} paused since {at(pr['since'], now)} by {pr.get('by') or 'user'}"
                     + (f": {pr['reason']}" if pr.get("reason") else "")
                     + f" — resume: ttp resume {p.name} --resource {pr['resource']}")
    if h["why_idle"]:
        lines.append(f"idle: {h['why_idle']}")
    elif h["held"]:
        lines.append(f"held: {h['held']}")
    for w in h["working"][:8]:
        what = f"#{w['task']} {w['title']}" if w["task"] else w["role"]
        lines.append(f"  running {since(w['started'], now)}: {what}" + (f" — {w['note']}" if w["note"] else ""))
    for t in db.q("SELECT id,title,status,blocked_reason FROM tasks WHERE status IN ('blocked','review') "
                  "ORDER BY status, id LIMIT 8"):
        lines.append(f"  #{t['id']} {t['status']}: {t['title']}" + (f" — {t['blocked_reason']}" if t["blocked_reason"] else ""))
    if h.get("host"):
        lines.append(h["host"])
    disk = db.kv("disk_low")
    if disk:
        lines.append(f"disk: only {disk['free_gb']} GB free under {disk['path']} (guard {disk.get('threshold_gb', '?')} GB); "
                     f"only questions and plans start")
    for t in h["waiting"][:5]:
        why = re.sub(r";? *next try \S+$", "", t["blocked_reason"] or "").strip()
        lines.append(f"  #{t['id']} waiting, next try {at(t['not_before'], now)}: {t['title']}" + (f" — {why}" if why else ""))
    if h["undelivered"]:
        u = h["undelivered"]
        lines.append(f"chat relay: {u['asks']} question(s) not delivered to any chat since {at(u['since'], now)}; "
                     f"is the chat's `ttp listen` running?")
        if u["below_floor"]:
            lines.append(f"  {u['below_floor']} of them are below every chat's severity floor; lower "
                         f"notify.chat_min_severity or the chat's own floor to see them")
    for m in h["asks"]:
        text = " ".join(m["text"].split())
        lines.append(f"  needs you (ask #{m['id']}, {since(m['ts'], now)} ago): {text[:300]}")
    return "\n".join(lines)


def daemon_state(p: Project) -> str:
    """'running', or NOT RUNNING with why: its process is gone, or it stopped completing ticks."""
    from .daemon import HEARTBEAT_STALE_S, _alive, heartbeat
    d = p.db.kv("daemon", {})
    alive = bool(d.get("pid")) and d.get("host") == hostname() and _alive(int(d["pid"]))
    hb = heartbeat(p)
    if not alive:
        return "NOT RUNNING"
    if hb and int(hb.get("pid") or 0) == int(d["pid"]) and hb["age"] > HEARTBEAT_STALE_S:
        return f"NOT RUNNING (process {d['pid']} is up but stuck: no completed tick for {int(hb['age'] // 60)} min)"
    return "running"


def cmd_status(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.json:
        from .web import state_payload
        print(json.dumps(state_payload(p, p.db), default=str, indent=1))
    else:
        print(status_text(p))


def cmd_web(a) -> None:
    p, entry = resolve(a.name)
    if p:
        print(web_line(p))
        return
    if not entry:
        die(f"no project {a.name!r}")
    host = entry.get("ssh") or entry["host"]
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, f"{remote_ttp} web {shlex.quote(a.name)}"],
                       capture_output=True, text=True)
    m = re.search(r"http://127\.0\.0\.1:(\d+)/#token=([0-9a-f]+)", r.stdout)
    if not m:
        die(f"could not read the web address from {host}: {(r.stderr or r.stdout).strip()[-200:]}")
    from .web import free_port
    remote_port, tok = m.group(1), m.group(2)
    local = free_port(int(remote_port) + 100)
    cmd = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-L",
           f"{local}:127.0.0.1:{remote_port}", host]
    if a.tunnel:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print(f"tunnel open (pid in background): localhost:{local} → {host}:{remote_port}")
    else:
        print(f"The project runs on {host}. With the user's OK, open a tunnel:\n  {' '.join(cmd)}")
    print(f"web app: http://127.0.0.1:{local}/#token={tok}")


def cmd_note(a) -> None:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp note only works inside a tt-project run")
    with open(Path(run_dir) / "progress.md", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {a.text}\n")


def cmd_push(a) -> None:
    """Publish this worktree's commits onto the project's target branch, guarded: refuse a dirty
    tree, rebase onto the latest tip, run `delivery.push_checks` on the final head, start over if
    the tip moved meanwhile, and push without force. The target is `delivery.push_branch`, never
    main, master or the remote's default branch. Pushes to one branch take turns; one that waits
    longer than `delivery.push_wait_s` for its turn exits 75. `--free` only tells whether it is
    free (0) or taken (1). Exit codes are in `push.py`."""
    from . import push
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else next((c for d in [Path.cwd(), *Path.cwd().parents]
                                         if (c := Project(d)).exists()), None)
    if not p or not p.exists():
        die("ttp push: no tt-project project here (run it inside a run or a project's worktree)")
    sys.exit(push.free(p, Path.cwd()) if a.free else push.run(p, Path.cwd()))


def cmd_lock(a) -> None:
    """Hold one slot of a shared resource while a command runs: `ttp lock <resource> -- <cmd...>`.

    Parallel tasks share a device or a remote build directory this way: each takes the lock only for
    the commands that touch it, and the rest of the task runs alongside other work. Slots come from
    the project's `resources` config (default 1); a running `exclusive:<resource>` task holds one for
    its whole run, and one waiting for a slot reserves the resource: new commands wait until it has
    started. The lock is held until the command has ended, however it ends.

    Inside a run, waiting is reported in the run's progress (a wait is not a stall) and gives up
    after half the run's stall limit unless --timeout says otherwise (0: wait as long as it takes).
    Giving up exits 75: the task hands back `waiting`. Time spent waiting does not count against
    the run's wall clock, which grows by at most its own length this way.
    """
    from . import locks as lk
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        die("usage: ttp lock <resource> -- <command...>")
    base = os.environ.get("TTP_PROJECT")
    if not base:
        die("ttp lock only works inside a tt-project run (or with TTP_PROJECT set)")
    p = Project(base)

    def _refuse_paused() -> None:
        # Checked before each try, so a pause set while this waits holds too.
        try:
            held = p.db.paused_resources().get(a.resource)
        except Exception as e:   # an unreadable database must not stop device commands
            print(f"ttp lock: could not check for a pause of {a.resource}: {e}", file=sys.stderr, flush=True)
            held = None
        if held is not None:
            if waiting:
                _end_wait()
            why = f" ({held['reason']})" if held.get("reason") else ""
            die(f"{a.resource} is paused{why}; hand the task back as waiting until it is resumed", 75)

    paths = lk.slot_paths(p.state / "locks", a.resource,
                          int((p.config().get("resources") or {}).get(a.resource, 1) or 1))
    who = f"task #{os.environ.get('TTP_TASK') or '?'} (run {os.environ.get('TTP_RUN_ID') or '?'})"
    run_dir = Path(os.environ["TTP_RUN_DIR"]) if os.environ.get("TTP_RUN_DIR") else None
    try:
        spec = json.loads((run_dir / "run.json").read_text()) if run_dir else {}
    except (OSError, ValueError):
        spec = {}
    waiting = False
    if a.resource in {x.get("resource") for x in spec.get("exclusive") or []}:
        # This run's task holds the resource for its whole run already.
        _refuse_paused()
        sys.exit(subprocess.call(cmd))
    timeout = a.timeout
    if timeout is None:
        timeout = float(spec.get("stall_s") or 0) / 2
    mark = lk.reserve_path(p.state / "locks", a.resource)
    started, told = time.time(), 0.0
    wait_key = f"{os.getpid()}:{started}"

    def _end_wait(*_):
        # Cleared first: a signal arriving while this records would otherwise take the record's
        # flock a second time in this process and hang.
        nonlocal waiting
        if waiting:
            waiting = False
            lk.record_wait(run_dir, wait_key, started, time.time())

    while True:
        _refuse_paused()
        reserved = lk.reserved_by(mark)
        f = None if reserved else lk.try_take(paths, who, " ".join(cmd))
        if f:
            _end_wait()
            waited = time.time() - started
            if waited > 5:
                print(f"ttp lock: got {a.resource} after {waited / 60:.1f} min", file=sys.stderr, flush=True)
            # The lock is held until the command itself has ended. A signal to this process (a
            # timeout, a cancel) is passed on to the command, and the lock is released only once
            # the command is gone, so nobody else ever gets the resource while it is still in use.
            proc = subprocess.Popen(cmd)

            def _forward(signum, _frame):
                try:
                    proc.send_signal(signum)
                except OSError:
                    pass

            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, _forward)
            try:
                rc = proc.wait()
            finally:
                f.close()
            sys.exit(rc)
        if run_dir and not waiting:
            # The run's wall clock stops while it waits here; the supervisor reads this record.
            # A signal ends the wait through _end_wait, so the record never stays open.
            lk.record_wait(run_dir, wait_key, started, None)
            waiting = True
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda signum, _f: (_end_wait(), sys.exit(128 + signum)))
        if timeout and time.time() - started > timeout:
            _end_wait()
            die(f"{a.resource} stayed busy for {timeout:.0f} s; hand the task back as waiting", 75)
        if time.time() - told >= 120:
            line = (f"waiting for {a.resource} (reserved for {reserved})" if reserved else
                    f"waiting for {a.resource} (held by {', '.join(lk.holders(paths)) or 'another task'})")
            print(f"ttp lock: {line}", file=sys.stderr, flush=True)
            if run_dir:
                try:
                    with open(run_dir / "progress.md", "a") as pf:
                        pf.write(f"{time.strftime('%H:%M:%S')} {line}\n")
                except OSError:
                    pass
            told = time.time()
        time.sleep(poll_s(3))


# operating ----------------------------------------------------------------------------------------
def cmd_list(a) -> None:
    reg = load_registry().get("projects", {})
    if not reg:
        print("no projects registered on this machine")
    for name, e in sorted(reg.items()):
        print(f"{name}\t{e.get('host')}:{e.get('dir')}")


def cmd_find(a) -> None:
    p, entry = resolve(a.name)
    if p:
        print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    elif entry:
        print(MARKER.format(name=a.name, host=entry["host"], dir=entry["dir"]))
    else:
        die(f"not found in the registry or in this machine's chat logs. Ask the user where it runs, "
            f"then `ttp adopt {a.name} --host HOST --dir DIR`", 1)


def cmd_adopt(a) -> None:
    register(a.name, {"host": a.host or hostname(), "dir": a.dir})
    print(MARKER.format(name=a.name, host=a.host or hostname(), dir=a.dir))


def cmd_task(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.action == "add":
        tid = p.db.add_task(a.title, a.spec or "", kind=a.kind, tier=a.tier, priority=a.priority, origin="user")
        print(f"task #{tid} queued")
    elif a.action == "list":
        for t in p.db.q("SELECT id,status,tier,title FROM tasks ORDER BY id DESC LIMIT 50"):
            print(f"#{t['id']}\t{t['status']}\t{t['tier']}\t{t['title']}")
    elif a.action == "cancel":
        from .runner import stop_runs
        tid = int(a.title)
        p.db.update_task(tid, status="cancelled")
        runs = stop_runs(p.db, p.runs, tid)
        print("cancelled" + (f"; ending its running run{'s' if len(runs) > 1 else ''} "
                             f"{', '.join(map(str, runs))}" if runs else ""))


def cmd_prune(a) -> None:
    """One sweep over finished tasks' worktrees, with the daemon's checks: clear build and cache
    directories, remove the worktree when nothing is lost. Branches stay."""
    from . import worktree
    p = need(a.name, sys.argv[1:])
    before = shutil.disk_usage(p.worktrees).free if p.worktrees.is_dir() else 0
    res = worktree.sweep(p, names=p.config().get("disk", {}).get("cache_dirs"))
    for r in res:
        cleared = f"; cleared {', '.join(r['cleared'][:5])}" if r["cleared"] else ""
        print(f"#{r['task']} ({r['status']}): " + ("removed, branch " + (r["branch"] or "?") + " kept"
                                                   if r["why"] is None else f"kept: {r['why']}") + cleared)
    if not res:
        print("no finished task's worktree to tidy")
    elif before:
        print(f"freed {max(shutil.disk_usage(p.worktrees).free - before, 0) / 1e9:.1f} GB")


def cmd_memory(a) -> None:
    p = need(a.name, sys.argv[1:])
    print(p.add_memory(a.text, kind=a.kind))


def cmd_machines(a) -> None:
    """The user's machines (~/.tt-project/machines.json), shared by all their projects. Each
    project's charter says which of them it may use; its coordinator routes work only to those.
    A project created with --host reads the copy on its machine: changes are copied there, merged."""
    from . import machines as mm
    if a.action == "add":
        try:
            entry = mm.add(a.alias, a.tags, a.note)
        except ValueError as e:
            die(str(e))
        print(f"saved {mm.line(a.alias.strip(), entry)}")
    elif a.action == "remove":
        if not mm.remove(a.alias):
            die(f"no machine {a.alias!r} in {mm.path()}")
        print(f"removed {a.alias}")
    if a.action in ("add", "remove", "push"):
        hosts = [a.host] if getattr(a, "host", None) else remote_hosts()
        if a.action == "push" and not hosts:
            print("no projects on other machines; nothing to copy")
        for host in hosts:
            print(mm.push(host))
    else:
        known = mm.load()
        if a.json:
            print(json.dumps(known, indent=2, sort_keys=True))
            return
        if not known:
            print("no machines yet: ttp machines add <alias> --tags device,... [--note TEXT]")
        for alias in sorted(known):
            print(mm.line(alias, known[alias]))


def cmd_pause(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.resource:
        from .coordinator import pause_resource
        try:
            print(f"{p.name}: " + pause_resource(p, a.resource, a.cmd == "pause", reason=getattr(a, "reason", None) or "", by="user"))
        except ValueError as e:
            die(str(e))
        return
    p.db.set_kv("paused", a.cmd == "pause")
    print(f"{p.name} {'paused: no new model runs start' if a.cmd == 'pause' else 'resumed'}")


def cmd_service(a) -> None:
    p = need(a.name, sys.argv[1:])
    from . import service
    if a.cmd == "start":
        print(service.install(p))
    elif a.cmd == "stop":
        print(service.uninstall(p))
        pid = p.state / "daemon.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 15)
            except (OSError, ValueError):
                pass
        print(stop_workers(p, a.kill))
    else:
        print(service.restart(p))


def stop_workers(p: Project, kill: bool, wait_s: float = 60) -> str:
    """Without kill, running workers finish on their own and the next start records their results.
    With kill, they end now (TERM, then KILL); their tasks go back to the queue and resume later."""
    from .runner import stop_runs
    running = p.db.q("SELECT id FROM runs WHERE status='running'")
    if not running:
        return "no runs in progress"
    if not kill:
        return (f"{len(running)} run(s) in progress keep going; the next start records their results. "
                f"`ttp stop {p.name} --kill` ends them.")
    ids = stop_runs(p.db, p.runs, why="shutdown")
    deadline = time.time() + wait_s
    left = list(ids)
    while left and time.time() < deadline:
        time.sleep(poll_s(1))
        left = [i for i in left if _run_dir_alive(p, i)]
    if left:
        return f"ending {len(ids)} run(s); still ending: {', '.join(map(str, left))}"
    return f"ended {len(ids)} run(s); their tasks resume on the next start"


def _run_dir_alive(p: Project, run_id: int) -> bool:
    row = p.db.one("SELECT dir FROM runs WHERE id=?", (run_id,))
    d = Path(row["dir"]) if row and row["dir"] else p.runs / str(run_id)
    if (d / "exit.json").exists():
        return False
    try:
        return time.time() - (d / "lease").stat().st_mtime <= 180
    except OSError:
        return False


def cmd_config(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.value is None:
        node = p.config()
        for part in a.key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(a.value)
    except ValueError:
        val = a.value
    p.set_config(a.key, val)
    print(f"{a.key} = {json.dumps(val)}")


def cmd_secret(a) -> None:
    if a.kind == "jev":
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Jev key: ")
        from .providers.jev import verify
        ok, msg = verify(a.via, key, a.url)
        if not ok and not a.force:
            die(f"the key did not work ({msg}); not saved. Use --force to save anyway.")
        save_secret("jev", {"via": a.via, "key": key, **({"url": a.url} if a.url else {})})
        print(f"saved Jev key for this user ({a.via}; check: {msg})")
    elif a.kind == "slack":
        tok = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Slack bot token (xoxb-…): ")
        from .slack import Slack
        s = Slack(tok, a.user_id, a.email)
        try:
            s.call("auth.test")
            uid = s.resolve_user()
        except Exception as e:
            die(f"Slack check failed: {e}")
        save_secret("slack", {"bot_token": tok, "user_id": uid, "user_email": a.email})
        print(f"saved Slack bot for user {uid}")
    elif a.kind == "push":
        if not a.host:
            die("--host is required")
        print(push_secrets(a.host))
    elif a.kind == "show":
        sec = load_secrets()
        print(json.dumps({k: sorted(v.keys()) if isinstance(v, dict) else "set" for k, v in sec.items()}))


def cmd_logs(a) -> None:
    p = need(a.name, sys.argv[1:])
    f = p.logs / "daemon.log"
    print(f.read_text()[-a.bytes:] if f.exists() else "(no log yet)")


def cmd_setup(a) -> None:
    """Install this runtime as the user's stable `ttp` (plugin caches move on every update)."""
    from .project import HOME_DIR
    lib = HOME_DIR / "lib" / __version__
    if not (PLUGIN_ROOT / "template").is_dir():
        die(f"{RUNTIME} is a project's harness copy, not the plugin; run `<plugin-root>/bin/ttp setup`", 1)
    commit = source_commit()
    before = recorded_commit(lib / "runtime") if (lib / "runtime").is_dir() else ""
    if RUNTIME.resolve() != (lib / "runtime").resolve():
        for part in ("runtime", "template", "bin"):
            src = PLUGIN_ROOT / part
            if src.exists():
                _copy_tree(src, lib / part)
        for f in (lib / "bin").iterdir():
            f.chmod(0o755)
    (lib / "runtime" / "ttp" / SOURCE_FILE).write_text(commit + "\n")
    cur = HOME_DIR / "lib" / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(lib)
    bindir = Path(a.bin_dir).expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "ttp"
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(cur / 'bin' / 'ttp'))} \"$@\"\n")
    shim.chmod(0o755)
    on_path = str(bindir) in os.environ.get("PATH", "").split(":")
    print(f"ttp {__version__} ({commit}) installed: {shim}" + ("" if on_path else f" (add {bindir} to PATH)"))
    if before and before != commit:
        print(f"replaced ttp {__version__} from commit {before} with commit {commit}")


def cmd_upgrade(a) -> None:
    """Merge the installed template into a project's harness. The harness repo keeps pristine
    template snapshots on its `upstream` branch, so this is an ordinary three-way merge."""
    entry = remote_entry(a.name)
    if entry and not local_project(a.name):
        from . import machines as mm
        ship_runtime(entry.get("ssh") or entry["host"])      # the newer runtime becomes that machine's ttp
        print(mm.push(entry.get("ssh") or entry["host"]))
        sys.exit(forward(entry, sys.argv[1:]))
    p = need(a.name, sys.argv[1:])
    from .project import HOME_DIR
    src = HOME_DIR / "lib" / "current"
    if not (src / "runtime").is_dir():
        die("no installed template; run `ttp setup` from the plugin first")
    h = p.harness
    new_v, new_c = _runtime_version(src / "runtime"), recorded_commit(src / "runtime")
    old_v, old_c = _runtime_version(h / "runtime"), recorded_commit(h / "runtime")
    if old_v != new_v:
        print(f"upgrading the harness from ttp {old_v} ({old_c}) to {new_v} ({new_c})")
    elif old_c != new_c:
        print(f"same version {new_v}, new source commit: {old_c} -> {new_c}")
    else:
        print(f"installed template: ttp {new_v} ({new_c})")
    ident = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
    if _git(h, "status", "--porcelain"):
        _git(h, "add", "-A")
        _git(h, *ident, "commit", "-q", "-m", "local harness changes before template upgrade")
    tmp = p.state / "upgrade-wt"
    if tmp.exists():
        shutil.rmtree(tmp)
    _git(h, "worktree", "add", "-q", str(tmp), "upstream")
    try:
        for part, dst in (("runtime", "runtime"), ("template/prompts", "prompts"), ("template/bin", "bin")):
            if (tmp / dst).exists():
                shutil.rmtree(tmp / dst)
            _copy_tree(src / part, tmp / dst)
        _git(tmp, "add", "-A")
        if _git(tmp, "status", "--porcelain"):
            _git(tmp, *ident, "commit", "-q", "-m", f"tt-project template {new_v} ({new_c})")
    finally:
        _git(h, "worktree", "remove", "--force", str(tmp))
    merged, problem = _merge_upstream(h, p.state / "upgrade-merge", ident)
    if problem:
        tid = p.db.add_task(
            "Finish the tt-project template upgrade", _UPGRADE_TASK.format(problem=problem, name=p.name),
            kind="harness", tier="standard", priority=2, origin="user")
        print(f"upgrade not applied; the running harness is unchanged. {problem}\nQueued harness task #{tid} to "
              f"finish it.")
        sys.exit(1)
    r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--ff-only", merged], capture_output=True, text=True)
    if r.returncode != 0:   # the daemon committed charter or memory meanwhile: those touch other files
        r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--no-edit", merged], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "-C", str(h), "merge", "--abort"], capture_output=True)
            die(f"could not apply the checked upgrade to {h}: {r.stdout[-500:]}", 1)
    print("harness up to date with the installed template; restarting the daemon")
    from . import service
    print(service.restart(p))


_UPGRADE_TASK = """`ttp upgrade` could not apply the new tt-project template on its own: {problem}

The live harness was left untouched. In this harness repo:
1. `git worktree add --detach <tmp> main`, then in <tmp>: `git merge upstream`.
2. Resolve each conflict keeping this project's intent and taking upstream's fixes.
3. Check in <tmp>: `python3 -m compileall -q runtime` and `PYTHONPATH=runtime python3 -c "import ttp.daemon, ttp.cli"`.
4. Commit, then in the harness: `git merge --ff-only <commit>`; remove <tmp>.
5. `ttp restart {name}` (it rolls the runtime back if the daemon does not start).
"""


def _merge_upstream(h: Path, tmp: Path, ident: list[str]) -> tuple[str, str]:
    """Merge `upstream` into a scratch worktree of main and check the result compiles and imports.
    Returns (merge commit, "") or ("", what went wrong); the live harness is never touched here."""
    subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    subprocess.run(["git", "-C", str(h), "worktree", "prune"], capture_output=True)
    _git(h, "worktree", "add", "-q", "--detach", str(tmp), "main")
    try:
        r = subprocess.run(["git", "-C", str(tmp), *ident, "merge", "--no-edit", "upstream"], capture_output=True,
                           text=True)
        if r.returncode != 0:
            files = subprocess.run(["git", "-C", str(tmp), "diff", "--name-only", "--diff-filter=U"],
                                   capture_output=True, text=True).stdout.split()
            return "", ("the merge conflicts in " + ", ".join(files) if files else
                        "the merge failed: " + (r.stderr or r.stdout).strip()[-400:])
        env = {**os.environ, "PYTHONPATH": str(tmp / "runtime")}
        for check in ([sys.executable, "-m", "compileall", "-q", "runtime"],
                      [sys.executable, "-c", "import ttp.daemon, ttp.cli"]):
            c = subprocess.run(check, cwd=str(tmp), env=env, capture_output=True, text=True, timeout=300)
            if c.returncode != 0:
                return "", f"the merged runtime fails `{' '.join(check[1:])}`: " + (c.stderr or c.stdout).strip()[-400:]
        return _git(tmp, "rev-parse", "HEAD"), ""
    finally:
        subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)


def cmd_alerts(a) -> None:
    p = need(a.name, sys.argv[1:])
    from .notifier import alerts_since
    rows = alerts_since(p, a.after, a.floor)
    if a.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            if not r["cleared"]:
                print(f"#{r['id']} [{r['severity']}] {r['text']}")


def cmd_notifier(a) -> None:
    from . import notifier
    if a.action == "install":
        print(notifier.install())
    elif a.action == "run":
        sys.exit(notifier.main())
    elif a.action == "test":
        notifier.show("tt-project", "Test notification: desktop alerts work on this machine.")
        print("sent a test notification")


def cmd_daemon(a) -> None:
    from .daemon import Daemon
    sys.exit(Daemon(a.dir).run())


def cmd_doctor(a) -> None:
    p = need(a.name, sys.argv[1:])
    from .providers import all_providers
    print(status_text(p))
    for prov in all_providers():
        if prov.name == "fake":
            continue
        print(f"provider {prov.name}: {'found ' + prov.binary() if prov.available() else 'not installed'}"
              + (f" · account {prov.account()}" if prov.available() and prov.account() else ""))
    claude_cfg = p.config()["providers"].get("claude", {})
    from .coordinator import name_list
    names = name_list(claude_cfg.get("mcp_servers") or [])
    if names:
        from .providers import get_provider
        _, unknown = get_provider("claude").mcp_servers(names, [str(p.root), str(p.root.resolve())])
        print(f"mcp servers for workers: {', '.join(names)}"
              + (f" · not defined in your Claude config: {', '.join(unknown)} (servers from a plugin "
                 "cannot be listed; add them with `claude mcp add`)" if unknown else "")
              + ("" if claude_cfg.get("worker_isolation") else " · unused: worker_isolation is off, "
                 "so workers load all your servers"))
    sec = load_secrets()
    print(f"jev: {'key saved' if (sec.get('jev') or {}).get('key') else 'no key (rules-only screening)'}"
          f" · enabled in project: {p.config()['jev'].get('enabled')}")
    print(f"slack: {'bot saved' if (sec.get('slack') or {}).get('bot_token') else 'not configured'}"
          f" · enabled in project: {p.config()['notify'].get('slack')}")
    print(web_line(p))


class _Version(argparse.Action):
    """`ttp --version`: the source commit is looked up only when asked for (it may run git)."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(f"ttp {__version__} ({source_commit()})")
        parser.exit()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ttp", description="tt-project: long-running, self-driving projects")
    ap.add_argument("--version", action=_Version, nargs=0, help="show the version and source commit, then exit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create a project")
    s.add_argument("name")
    s.add_argument("--dir", help="project root (default: git top level of the current directory)")
    s.add_argument("--host", help="machine to run on (default: this one)")
    s.add_argument("--describe", help="the brief, inline")
    s.add_argument("--describe-file", help="the brief, from a file ('-' for stdin)")
    s.add_argument("--provider", choices=["claude", "codex", "cursor", "fake"])
    s.add_argument("--no-service", action="store_true", help="do not install a boot-time service")
    s.add_argument("--no-secrets", action="store_true", help="with --host: do not copy your saved keys there")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("web", help="web app link (for a remote project: tunnel command and link)")
    s.add_argument("name")
    s.add_argument("--tunnel", action="store_true", help="open the ssh tunnel now (ask the user first)")
    s.set_defaults(fn=cmd_web)
    for name, fn, hlp in (("connect", cmd_connect, "attach this chat to a project"),
                          ("status", cmd_status, "one-screen status"),
                          ("logs", cmd_logs, "daemon log tail"), ("doctor", cmd_doctor, "diagnose setup"),
                          ("prune", cmd_prune, "tidy finished tasks' worktrees now (branches are kept)")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("name")
        if name == "connect":
            s.add_argument("--chat")
            s.add_argument("--label")
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "logs":
            s.add_argument("--bytes", type=int, default=6000)
        s.set_defaults(fn=fn)

    s = sub.add_parser("say", help="send a message to the coordinator")
    s.add_argument("name")
    s.add_argument("text", help="message, or '-' for stdin")
    s.add_argument("--chat")
    s.set_defaults(fn=cmd_say)

    s = sub.add_parser("listen", help="print messages for a chat as they arrive")
    s.add_argument("name")
    s.add_argument("--chat", required=True)
    s.add_argument("--once", action="store_true", help="exit after the first batch")
    s.add_argument("--timeout", type=float, default=0)
    s.add_argument("--ack", type=int, metavar="ID",
                   help="mark messages up to ID read; later ones stay unread until acknowledged")
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("note", help="(inside a run) append a progress note")
    s.add_argument("text")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("push", help="guarded push of this worktree to delivery.push_branch")
    s.add_argument("--free", action="store_true",
                   help="push nothing: exit 0 when no other push to the target branch is running, 1 while one is")
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("lock", help="(inside a run) hold a shared resource while one command runs")
    s.add_argument("resource")
    s.add_argument("--timeout", type=float, default=None,
                   help="give up after this many seconds (exit 75); 0 waits as long as it takes; "
                        "default inside a run: half its stall limit")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_lock)

    for name, fn in (("list", cmd_list),):
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("find", help="locate a project by name (registry, then chat logs)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("adopt", help="record where a project lives")
    s.add_argument("name")
    s.add_argument("--host")
    s.add_argument("--dir", required=True)
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("task", help="add/list/cancel tasks by hand")
    s.add_argument("name")
    s.add_argument("action", choices=["add", "list", "cancel"])
    s.add_argument("title", nargs="?", default="")
    s.add_argument("--spec")
    s.add_argument("--kind", default="work")
    s.add_argument("--tier", default="standard")
    s.add_argument("--priority", type=int, default=3)
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("memory", help="add a memory")
    s.add_argument("name")
    s.add_argument("text")
    s.add_argument("--kind", default="fact")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("machines", help="your machines, shared by all your projects (add/list/remove/push)")
    ms = s.add_subparsers(dest="action", required=True)
    m = ms.add_parser("add", help="add a machine, or change its tags or note")
    m.add_argument("alias", help="a short name, also used as the resource name in tasks (e.g. box-a)")
    m.add_argument("--tags", help="what it offers, comma-separated (e.g. device,x86)")
    m.add_argument("--note", help="one line for the coordinator (no secrets)")
    m = ms.add_parser("list", help="list your machines")
    m.add_argument("--json", action="store_true")
    m = ms.add_parser("remove", help="remove a machine")
    m.add_argument("alias")
    m = ms.add_parser("push", help="copy the list to the machines your --host projects run on, merged")
    m.add_argument("--host", help="only this machine")
    s.set_defaults(fn=cmd_machines)

    for name in ("pause", "resume"):
        s = sub.add_parser(name, help=f"{name} the project, or with --resource one shared resource")
        s.add_argument("name")
        s.add_argument("--resource", help=f"{name} only this resource: tasks using it wait, `ttp lock` refuses it"
                       if name == "pause" else f"{name} only this resource")
        if name == "pause":
            s.add_argument("--reason", help="why, shown to workers, the coordinator and in status")
        s.set_defaults(fn=cmd_pause)
    for name in ("start", "stop", "restart"):
        s = sub.add_parser(name, help=f"{name} the project's daemon service (running workers are kept)")
        s.add_argument("name")
        if name == "stop":
            s.add_argument("--kill", action="store_true", help="also end running workers; their tasks resume on start")
        s.set_defaults(fn=cmd_service)

    s = sub.add_parser("config", help="read or set a config key (dotted)")
    s.add_argument("name")
    s.add_argument("key")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("secret", help="store per-user credentials (read from stdin)")
    s.add_argument("kind", choices=["jev", "slack", "show", "push"])
    s.add_argument("--host", help="with push: the machine to copy your saved keys to")
    s.add_argument("--via", default="typesafe", choices=["typesafe", "openrouter"])
    s.add_argument("--url")
    s.add_argument("--email")
    s.add_argument("--user-id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser("setup", help="install ttp for this user (stable copy + shim on PATH)")
    s.add_argument("--bin-dir", default="~/.local/bin")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("upgrade", help="merge the installed tt-project template into a project's harness")
    s.add_argument("name")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("alerts", help="broadcast alerts after a message id")
    s.add_argument("name")
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--floor", default="high")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("notifier", help="desktop notifications on this machine for all your projects")
    s.add_argument("action", choices=["install", "run", "test"])
    s.set_defaults(fn=cmd_notifier)

    s = sub.add_parser("daemon", help=argparse.SUPPRESS)
    s.add_argument("dir")
    s.set_defaults(fn=cmd_daemon)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
