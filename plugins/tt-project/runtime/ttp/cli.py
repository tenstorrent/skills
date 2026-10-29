# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp` — create, find, talk to and operate tt-project projects.

Every project command takes the project NAME. If the registry says the project lives on another
machine, the command is forwarded over ssh to that machine's copy of the project's own `ttp`.
"""
from __future__ import annotations

import argparse
import atexit
import datetime as _dt
import getpass
import hashlib
import json
import math
import os
import re
import secrets as pysecrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, poll_s
from .db import CHANGES_NEEDED, chat_floor, task_outcome
from . import outbox
from .devq import unmask_devices
from . import scriptwatch, sshcmd, timefmt
from . import schedule as sched
from .project import (NAME_RE, Project, hostname, load_registry, load_secrets, register, save_secret,
                      durable_append, durable_write, write_json, ACCOUNT_KEYS, DEFAULT_CONFIG, deep_merge,
                      load_account_settings, set_account_setting, zombie, device_timeout_max, command_env,
                      NOTE_RUN_ENV, in_run, replace_copy, sync_all, MEMORY_AUDIENCES)

RUNTIME = Path(__file__).resolve().parent.parent              # .../runtime (plugin or project copy)
PLUGIN_ROOT = RUNTIME.parent                                   # plugin root, or a project's harness/
MARKER = "tt-project://{name}@{host}:{dir}"


def die(msg: str, code: int = 2) -> None:
    print(f"ttp: {msg}", file=sys.stderr)
    sys.exit(code)


# locating projects --------------------------------------------------------------------------------
_HERE: Project | None = None   # the project _project_name_argv put in a command's empty name slot


def local_project(name: str) -> Project | None:
    if _HERE is not None and _HERE.name == name:
        return _HERE
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


LISTEN_GAVE_UP = 75   # `ttp listen --timeout` on another machine: still unreachable when the timeout ran out
LISTEN_GRACE_S = 60     # a listener reconnecting within this says nothing about the drop
LISTEN_CONNECTED_S = 25  # an ssh session that lasted this long had connected (ConnectTimeout is 20 s)
LISTEN_REPLACED = 3   # a newer listener for the same chat took over; this one stops and does not reconnect
REPLACED_MARK = "ttp: replaced by a newer listener"   # its stderr line, read even when ssh reports a drop
_MSG_LINE = re.compile(r"\[#(\d+) ")


def _dropped(rc: int) -> bool:
    """True if a forwarded command ended because the connection did, not by its own exit.

    ssh exits 255 when it fails. A status of 128 or more is a signal ending the session (the far
    side's shell, a shared ssh connection, the network stack): `ttp listen` itself exits only
    0, 1, 2 or LISTEN_REPLACED. Below 0, ssh here was killed by a signal."""
    return rc == 255 or rc >= 128 or rc < 0


def _listen_timeout(argv: list[str]) -> float:
    for i, a in enumerate(argv):
        v = a.split("=", 1)[1] if a.startswith("--timeout=") else (argv[i + 1] if a == "--timeout" and i + 1 < len(argv) else None)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                return 0.0
    return 0.0


def _with_timeout(argv: list[str], left: float) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--timeout":
            skip = True
        elif not a.startswith("--timeout="):
            out.append(a)
    return out + [f"--timeout={max(left, 1.0):.0f}"]


def forward_listen(entry: dict, argv: list[str], name: str | None = None) -> int:
    """A remote listener outlives network drops: a laptop changes networks, sleeps and wakes.

    A dropped connection (see _dropped) means the path was lost (this machine's network, or the
    host restarting), not that the project stopped, so wait (5 s, doubling to 2 min) and reconnect.
    A reconnect within LISTEN_GRACE_S says nothing; a longer outage is explained once, from the
    side ssh's error points to (sshfail.kind). An outage that began with the host dropping a working
    connection keeps that wording while it reboots (its retries time out); only this machine losing
    its own path (sshfail.offline) switches to the network advice. The listener left on the far side
    exits on its own once its session is gone, and the new one replaces it if it has not yet.
    Each (re)connect first sends what is queued here. A message is printed once per listener:
    one shown before a drop is not shown again after the reconnect, and with --once, a batch that
    arrived before the drop ends the listener (exit 0) like a clean finish. Acknowledging stays
    with --ack, so nothing a reader missed is lost. With --timeout, it stops retrying once the
    timeout has run out and exits LISTEN_GAVE_UP; every other exit is the far side's own.
    A listener replaced by a newer one for the same chat ends with LISTEN_REPLACED and is not
    retried: reconnecting would displace the newer one in turn, and the two would take turns.
    """
    from . import sshfail
    once = "--once" in argv
    limit = _listen_timeout(argv)
    deadline = time.time() + limit if limit > 0 else None
    shown: set[int] = set()
    delay, told, held, tries = 5.0, False, False, 0
    down_since, connected, drop = None, False, None
    while True:
        start = time.time()
        failed = flush_outbox(name, entry, quiet=held) if name else None
        held = held or failed is not None
        if failed is not None and failed.returncode == 255:
            rc, err = 255, failed.stderr
        else:
            left = deadline - time.time() if deadline else 0
            errs: list[str] = []
            rc = forward(entry, _with_timeout(argv, left) if deadline and tries else argv, quiet=True, shown=shown,
                         failure=errs)
            err = errs[-1] if errs else None
        tries += 1
        if not _dropped(rc):
            return rc
        if once and shown:
            return 0        # the batch got here before the drop; --ack picks up from it
        now = time.time()
        if now - start >= LISTEN_CONNECTED_S or (rc != 255 and rc >= 128):
            connected, down_since, told, delay = True, None, False, 5.0   # it was up: a new outage starts now
        if down_since is None:   # the error that started this outage, if a working session dropped
            down_since, drop = now, ((err, rc) if connected else None)
        if drop and sshfail.kind(drop[0], True) == "host" and not sshfail.offline(err):
            err, rc = drop      # a host rebooting times out while down: still its side, not this network
        if deadline and now + delay >= deadline:
            if not told:
                _unreachable(entry, err, rc, connected)
            print(f"ttp: still cannot reach {entry.get('ssh') or entry['host']} and --timeout ran out; "
                  f"stopping (exit {LISTEN_GAVE_UP}). Nothing is lost: unacknowledged messages come "
                  f"back on the next listen", file=sys.stderr)
            return LISTEN_GAVE_UP
        if not told and now - down_since >= LISTEN_GRACE_S:   # a quick reconnect says nothing
            _unreachable(entry, err, rc, connected, " until --timeout runs out" if deadline else "")
            told = True
        time.sleep(delay)
        delay = min(delay * 2, 120.0)


def _ssh_cmd(entry: dict, argv: list[str]) -> list[str]:
    cmd = sshcmd.remote_cmd(entry, argv)
    try:
        host = sshcmd.host(entry)
    except sshcmd.BadHost as e:
        die(str(e))
    # Keepalives: a connection that died quietly (a laptop asleep, a changed network) ends within
    # about a minute instead of hanging until TCP gives up.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", "--", host, cmd]


def _ssh(entry: dict, argv: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(_ssh_cmd(entry, argv), stdin=subprocess.DEVNULL if capture else None,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE, text=True)


def _ssh_relay(entry: dict, argv: list[str], shown: set[int]) -> subprocess.CompletedProcess:
    """Run argv there, passing its output through line by line, minus messages already in `shown`
    (a message line starts `[#<id> `; the lines after it belong to it)."""
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(_ssh_cmd(entry, argv), stdout=subprocess.PIPE, stderr=err, text=True)
        skip = False
        for line in proc.stdout:
            m = _MSG_LINE.match(line)
            if m:
                skip = int(m.group(1)) in shown
                shown.add(int(m.group(1)))
            if not skip:
                sys.stdout.write(line)
                sys.stdout.flush()
        rc = proc.wait()
        err.seek(0)
        return subprocess.CompletedProcess(proc.args, rc, None, err.read())


def _unreachable(entry: dict, stderr: str | None, rc: int = 255, connected: bool = False,
                 retrying: str | None = None) -> None:
    """Say why ssh to the project's host failed, from the user's side (sshfail.kind), naming the ssh
    alias and never an address. `retrying`: the listener keeps retrying (the text after "retrying")."""
    from . import machines as mm, sshfail
    host = entry.get("ssh") or entry["host"]
    why, kind = sshfail.why(stderr, host, rc), sshfail.kind(stderr, connected)
    if kind == "host":
        head = (f"{host} dropped the connection" if connected else f"{host} is not accepting connections")
        line = f"ttp: {head} (probably restarting; {why})."
        tail = (f" Retrying{retrying}; messages are delivered once it is back." if retrying is not None
                else " Try again in a minute or two.")
    elif kind == "network":
        line = f"ttp: cannot reach {host} right now ({why}). The project keeps running there;"
        tail = (f" retrying{retrying}; messages are delivered once this machine can reach {host} again."
                if retrying is not None else f" try again once this machine can reach {host} again.")
    else:
        line = f"ttp: cannot reach {host} right now ({why})."
        tail = (f" Retrying{retrying}; messages are delivered once the connection is back." if retrying is not None
                else " The project keeps running there.")
    print(line + tail, file=sys.stderr)
    if hint := mm.auth_failed_hint(stderr):
        print(f"ttp: {hint}", file=sys.stderr)


def forward(entry: dict, argv: list[str], quiet: bool = False, shown: set[int] | None = None,
            failure: list[str] | None = None) -> int:
    """Run this same command on the project's machine, streaming its output. With `shown` (a
    listener), message lines already printed are not printed again. `failure` collects the stderr
    of a dropped connection (_dropped) instead of printing it: that caller explains the drop itself."""
    from . import sshfail
    r = _ssh(entry, argv) if shown is None else _ssh_relay(entry, argv, shown)
    if shown is not None and REPLACED_MARK in (r.stderr or ""):
        # The far side said so before it went: whatever ssh reports (255, 143), this is no drop.
        sys.stderr.write(r.stderr)
        return LISTEN_REPLACED
    if failure is not None and _dropped(r.returncode):   # the caller explains the drop, if it lasts
        failure.append(r.stderr or "")
    elif r.returncode == 255:   # ssh itself failed: the project's state is kept there
        if not quiet:
            _unreachable(entry, r.stderr)
    elif r.stderr:
        sys.stderr.write(sshfail.scrub(r.stderr, entry.get("ssh") or entry["host"]))
    return r.returncode


def _send_say(entry: dict, argv: list[str], client_id: str, capture: bool = False) -> subprocess.CompletedProcess:
    """`ttp say` on the project's machine, tagged with the client id it deduplicates on."""
    r = _ssh(entry, [argv[0], f"--client-id={client_id}", *argv[1:]], capture)
    if r.returncode == 2 and "--client-id" in (r.stderr or ""):   # an older runtime there: send it untagged
        r = _ssh(entry, argv, capture)
    if r.returncode == 2 and "--broadcast" in (r.stderr or "") and "--broadcast" in argv:
        return _send_say(entry, _fold_broadcast(argv), client_id, capture)
    if r.returncode == 2 and "--context" in (r.stderr or "") and "--context" in argv:
        return _send_say(entry, _fold_context(argv), client_id, capture)
    return r


def _fold_broadcast(argv: list[str]) -> list[str]:
    """The `ttp say` argv for a runtime without `--broadcast`: the broadcast id goes into the
    relayer's context, so that project still learns it and never relays the words on."""
    i = argv.index("--broadcast")
    bid, rest = argv[i + 1], argv[:i] + argv[i + 2:]
    note = f"broadcast {bid}: the user sent these words to every project. Do not relay them to other projects."
    if "--context" in rest:
        j = rest.index("--context") + 1
        rest[j] = f"{rest[j]}\n{note}"
        return rest
    j = rest.index("--")
    return rest[:j] + ["--context", note] + rest[j:]


def _fold_context(argv: list[str]) -> list[str]:
    """The `ttp say` argv for a runtime without `--context`: the relayer's context goes after a
    relay.MARK line in the text, which that runtime keeps and a newer one splits off again."""
    from . import relay
    i = argv.index("--context")
    rest = argv[:i] + argv[i + 2:]
    return rest[:-1] + [f"{rest[-1]}\n\n{relay.MARK}\n{argv[i + 1]}"]


def flush_outbox(name: str, entry: dict, quiet: bool = False) -> subprocess.CompletedProcess | None:
    """Send what is queued on this machine for `name`, oldest first, each removed only once the
    project confirmed it. Returns the failed attempt (that message and the ones behind it stay
    queued), else None. Only a 255 means the project is unreachable; any other failure is that
    message's own problem and must not stop the caller's command."""
    if not outbox.entries(name):
        return None
    failed, sent = None, 0
    with outbox.locked(name):
        for e in outbox.entries(name):
            r = _send_say(entry, e["argv"], e["id"], capture=True)
            if r.returncode == 0:
                outbox.drop(name, e["id"])
                sent += 1
                continue
            why = ((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]
            if r.returncode == 2:     # the project refused it: set it aside, do not block the rest
                outbox.drop(name, e["id"], rejected=why)
                print(f"ttp: {name} refused queued message #{e['id']} ({why}); it is kept in "
                      f"{outbox.folder() / (name + '.rejected.jsonl')}", file=sys.stderr)
                continue
            failed = r
            if r.returncode != 255 and not quiet:
                print(f"ttp: queued messages for {name} stay queued ({why})", file=sys.stderr)
            break
    if sent:
        print(f"ttp: delivered {sent} queued message(s) to {name}", file=sys.stderr)
    return failed


def say_remote(name: str, entry: dict, text: str, chat: str | None, context: str = "",
               broadcast: str | None = None) -> int:
    """Send a message to a project on another machine. If that machine cannot be reached, or older
    messages are still queued for it, queue this one here: it is never dropped, and goes out in order."""
    return _say_remote(name, entry, text, chat, context, broadcast)[0]


def _say_remote(name: str, entry: dict, text: str, chat: str | None, context: str = "",
                broadcast: str | None = None, finding: bool = False) -> tuple[int, str]:
    """say_remote's exit code and where the message went: `client:<id>` (delivered) or
    `queued:<host>`. A broadcast's client id is its broadcast id: a resend is a no-op there.
    `finding`: `context` is a relayer's own findings, sent with no user's words."""
    argv = (["say", name, *(["--chat", chat] if chat else []), "--finding", context, "--", ""] if finding else
            ["say", name, *(["--chat", chat] if chat else []), *(["--context", context] if context else []),
             *(["--broadcast", broadcast] if broadcast else []), "--", text])
    cid = broadcast or outbox.new_id()
    if flush_outbox(name, entry) is None and not outbox.entries(name):
        r = _send_say(entry, argv, cid)
        if r.returncode != 255:
            if r.stderr:
                sys.stderr.write(r.stderr)
            return r.returncode, f"client:{cid}"
    outbox.add(name, argv, cid, chat)
    print(f"queued on this machine; delivered once {name} is reachable (#{cid})")
    return 0, f"queued:{entry.get('host')}"


CHAT_LOG_ROOTS = (".claude/projects", ".codex/sessions", ".cursor/projects")
_mono = time.monotonic   # the search cap's clock, and the miss cache's: tests replace them
_now = time.time
_last_search_cut = False   # whether search_transcripts' last run stopped at its cap
_GREP_BATCH = 1000   # files per grep: few processes, and the cap is checked between them


def _env_s(var: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(var, default)))
    except ValueError:
        return default


def _chat_logs(deadline: float | None) -> list[Path]:
    """Every chat-log file, newest first. A walk past `deadline` returns what it has."""
    found: list[tuple[float, str]] = []
    stack = [str(Path.home() / r) for r in CHAT_LOG_ROOTS]
    while stack:
        if deadline is not None and _mono() >= deadline:
            break
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file():
                            found.append((e.stat().st_mtime, e.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return [Path(f) for _, f in sorted(found, reverse=True)]


def search_transcripts(name: str, cap_s: float | None = None) -> list[dict]:
    """Find a project's location markers in agent chat logs (Claude Code, Codex, Cursor), newest
    logs first. With `cap_s`, stop after that many seconds in all; `_last_search_cut` says whether
    it did."""
    global _last_search_cut
    needle = f"tt-project://{name}@"
    hits: dict[str, dict] = {}
    rx = re.compile(re.escape(needle) + r"([A-Za-z0-9._-]+):(/[A-Za-z0-9._/~-]+)")
    deadline = None if cap_s is None else _mono() + cap_s
    files = _chat_logs(deadline)
    matched: list[str] = []
    _last_search_cut = False
    for i in range(0, len(files), _GREP_BATCH):
        left = 120.0 if deadline is None else deadline - _mono()
        if left <= 0:
            _last_search_cut = True
            break
        try:
            out = subprocess.run(["grep", "-lF", "--null", "-e", needle, "--", *map(str, files[i:i + _GREP_BATCH])],
                                 capture_output=True, text=True, timeout=left).stdout
        except subprocess.TimeoutExpired:
            if deadline is not None:
                break
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        matched += [f for f in out.split("\0") if f]
    if deadline is not None and _mono() >= deadline:
        _last_search_cut = True
    for f in matched:
        try:
            text = Path(f).read_text(errors="replace")
            mtime = Path(f).stat().st_mtime
        except OSError:
            continue
        for m in rx.finditer(text):
            if m.group(1).startswith("-"):   # ssh would read it as an option
                continue
            hits.setdefault(f"{m.group(1)}:{m.group(2)}", {"host": m.group(1), "dir": m.group(2).rstrip(".,)"),
                                                          "seen_in": f, "mtime": mtime})
    return sorted(hits.values(), key=lambda h: -h["mtime"])


def _miss_cache_path() -> Path:
    from .project import registry_path
    return registry_path().with_name("chatlog-misses.json")


def _cached_miss(name: str) -> bool:
    try:
        at = json.loads(_miss_cache_path().read_text()).get(name)
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(at, (int, float)) and 0 <= _now() - at < _env_s("TTP_CHATLOG_MISS_S", 600)


def _cache_miss(name: str) -> None:
    """Remember that the chat logs had no marker for `name` (only names that passed NAME_RE, at
    most 200, expired ones dropped). A failed write only costs the next search."""
    from .project import write_json
    ttl, now = _env_s("TTP_CHATLOG_MISS_S", 600), _now()
    try:
        data = json.loads(_miss_cache_path().read_text())
        data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and 0 <= now - v < ttl}
    except (OSError, ValueError, AttributeError):
        data = {}
    data[name] = now
    try:
        write_json(_miss_cache_path(), dict(sorted(data.items(), key=lambda kv: -kv[1])[:200]), mode=0o600)
    except OSError:
        pass


def _found_in_chat_log(name: str, host: str, folder: str, seen_in: str) -> None:
    from .project import registry_path
    print(f"ttp: registered {name} at {host}:{folder} (found in the chat log {seen_in}; "
          f"remove it from {registry_path()} if that is wrong)", file=sys.stderr)


_quick_miss: dict[str, str] = {}   # name -> why resolve()'s chat-log search was not a full one


def resolve(name: str, full: bool = False) -> tuple[Project | None, dict | None]:
    """The project called `name`: here, in the registry, else from a chat-log marker (registered
    then). A mistyped name costs at most TTP_CHATLOG_SEARCH_S seconds (newest logs first) and then
    nothing for TTP_CHATLOG_MISS_S seconds; `full` (ttp find) searches every log, uncached."""
    p = local_project(name)
    if p:
        return p, None
    entry = remote_entry(name)
    if entry:
        return None, entry
    if not full and _cached_miss(name):
        _quick_miss[name] = "cached"
        return None, None
    cap = None if full else _env_s("TTP_CHATLOG_SEARCH_S", 10)
    hits = search_transcripts(name) if cap is None else search_transcripts(name, cap_s=cap)
    for hit in hits:
        if hit["host"] == hostname() and Project(hit["dir"]).exists():
            register(name, {"host": hostname(), "dir": str(Project(hit["dir"]).root)})
            _found_in_chat_log(name, hostname(), str(Project(hit["dir"]).root), hit["seen_in"])
            return Project(hit["dir"]), None
        if hit["host"] != hostname():
            entry = {"host": hit["host"], "dir": hit["dir"]}
            register(name, entry)
            _found_in_chat_log(name, hit["host"], hit["dir"], hit["seen_in"])
            return None, entry
    if NAME_RE.match(name):
        _cache_miss(name)
    if cap is not None and _last_search_cut:
        _quick_miss[name] = f"cut after {cap:g}s"
    return None, None


def need(name: str, argv: list[str]) -> Project:
    p, entry = resolve(name)
    if entry:
        if argv and argv[0] == "listen":
            sys.exit(forward_listen(entry, argv, name))
        failed = flush_outbox(name, entry)
        if failed is None or failed.returncode != 255:   # a message stuck for its own reason: run the command anyway
            rc = forward(entry, argv)
        else:
            _unreachable(entry, failed.stderr)
            rc = 255
        n = outbox.count(name)
        if n and argv[:1] == ["status"] and "--json" not in argv:
            print(f"{n} message(s) queued here for {name}; they go out once it is reachable")
        sys.exit(rc)
    if not p:
        import difflib
        close = difflib.get_close_matches(name, list(load_registry().get("projects", {})), n=3)
        why = _quick_miss.get(name)
        die(f"no project named {name!r} on this machine or in the registry"
            + (f"; did you mean {', '.join(close)}?" if close else "")
            + (f" (chat-log search {why}; `ttp find {name}` searches every log)" if why else ""))
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
    """Copy `src` over `dst`, each file renamed into place (replace_copy): an installed script a shell
    is running is never rewritten under it."""
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    copy_function=replace_copy)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


SOURCE_FILE = "SOURCE_COMMIT"      # in runtime/ttp/: the git commit (or content hash) `ttp setup` installed this runtime from


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


def content_hash(runtime: Path) -> str:
    """'sha-<12 hex>' over the sorted relative paths and bytes of the files under `runtime` (no
    __pycache__, *.pyc or SOURCE_COMMIT): the identity of a runtime installed from no git checkout."""
    h = hashlib.sha256()
    for f in sorted(runtime.rglob("*"), key=lambda x: x.relative_to(runtime).as_posix()):
        rel = f.relative_to(runtime)
        if "__pycache__" in rel.parts or f.suffix == ".pyc" or rel.as_posix() == f"ttp/{SOURCE_FILE}" \
                or not f.is_file():
            continue
        data = f.read_bytes()
        h.update(f"{rel.as_posix()}\0{len(data)}\0".encode() + data)
    return "sha-" + h.hexdigest()[:12]


def source_commit() -> str:
    """The git commit this runtime came from: recorded at install, else read from the plugin checkout,
    else (a plugin cache or archive, no git) a content hash of the runtime."""
    got = recorded_commit(RUNTIME)
    if got == "unknown" and (PLUGIN_ROOT / "template").is_dir():   # a harness copy is not the plugin
        got = _checkout_commit(PLUGIN_ROOT)
        if got == "unknown":
            got = content_hash(RUNTIME)
    return got


# Template files installed at the harness root. pytest.ini isolates harness-local test runs from an
# enclosing repository's pytest config and conftest.py (pytest stops looking at the first config).
HARNESS_ROOT_FILES = ("pytest.ini",)
# Files that hold a pytest config at a directory's root, and the section that makes them one
_PYTEST_CONFIGS = {"pytest.ini": "", ".pytest.ini": "", "pytest.toml": "", ".pytest.toml": "",
                   "pyproject.toml": "[tool.pytest", "tox.ini": "[pytest]", "setup.cfg": "[tool:pytest]"}


def _own_pytest_config(h: Path) -> str:
    """The pytest config file main holds at the harness root that upstream never shipped, or "":
    the project's own, which an upgrade never overrides with the template's pytest.ini."""
    def show(ref: str, name: str) -> str | None:
        r = subprocess.run(["git", "-C", str(h), "show", f"{ref}:{name}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    if show("upstream", "pytest.ini") is not None:
        return ""
    for name, section in _PYTEST_CONFIGS.items():
        text = show("HEAD", name)
        if text is not None and section in text:
            return name
    return ""


def _runtime_version(runtime: Path) -> str:
    m = re.search(r'__version__ = "([^"]+)"', (runtime / "ttp" / "__init__.py").read_text())
    return m.group(1) if m else "unknown"


def bootstrap(root: Path, name: str, brief: str, provider: str, home_tz: str | None = None,
              home_from: str = "") -> Project:
    p = Project(root)
    if p.exists():
        if p.name != name:
            die(f"{p.base} already holds project {p.name!r}")
        return p
    p.base.mkdir(parents=True, exist_ok=True)
    # The folder ignores itself, so no enclosing repository can ever commit project state.
    durable_write(p.base / ".gitignore", "*\n")
    for d in (p.state, p.runs, p.logs, p.worktrees, p.memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    template = PLUGIN_ROOT / "template"
    _copy_tree(RUNTIME, p.harness / "runtime")
    (p.harness / "runtime" / "ttp" / SOURCE_FILE).write_text(source_commit() + "\n")
    _copy_tree(template / "prompts", p.harness / "prompts")
    _copy_tree(template / "bin", p.harness / "bin")
    for f in (p.harness / "bin").iterdir():
        f.chmod(0o755)
    for f in HARNESS_ROOT_FILES:
        shutil.copy2(template / f, p.harness / f)
    (p.harness / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q", "-b", "upstream", str(p.harness)], check=True)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"tt-project template {__version__}")
    _git(p.harness, "checkout", "-q", "-b", "main")
    _ensure_git_ident(p.harness)
    from . import release
    release.guard_harness(p.harness)
    charter = (template / "CHARTER.md").read_text().replace("{{NAME}}", name).replace(
        "{{DATE}}", time.strftime("%Y-%m-%d")).replace("{{BRIEF}}", brief.strip() or "(no description given yet)")
    durable_write(p.charter_path, charter)
    durable_write(p.memory_index, "# Memory index\n")
    cfg = {"name": name, "created": time.time(), "root": str(p.root), "host": hostname(),
           "core_provider": provider, "tt_project_version": __version__,
           "id": pysecrets.token_hex(4),
           # New projects only: DEFAULT_CONFIG keeps it off, so existing projects are unchanged.
           "providers": {"claude": {"worker_isolation": True}}}
    # The user's zone: the workstation's when it sent one (new_remote), else this machine's. In an ssh
    # login with none sent this machine's is a server's: the fallback zone, set by no machine, so no
    # spend push moves it and the workstation's next connect sets it.
    if timefmt.valid(home_tz):
        cfg.update({timefmt.KEY: timefmt.valid(home_tz), timefmt.FROM_KEY: home_from or hostname()})
    elif timefmt.in_ssh_login():
        cfg[timefmt.KEY] = timefmt.fallback()[0]
    else:
        cfg.update({timefmt.KEY: timefmt.detect_local(), timefmt.FROM_KEY: hostname()})
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
                     s.get("budget_usd_day"), s.get("description", ""), s.get("payload", {}), cfg[timefmt.KEY])
    sched.write_file(p, "schedules: from the template", create=True)
    db.set_meta("name", name)
    db.post("in", "Project created. Brief:\n" + (brief.strip() or "(none)") +
            "\n\nRead the charter, restate the goals, success criteria and restrictions as you understand "
            "them, list what you still need to know, and start the first tasks that do not depend on answers.",
            chat=None, channel="system", kind="user", provenance="system")
    return p


def _usd(v) -> str:
    return f"${float(v):g}"


def defaults_lines(budget: dict, jev_key: bool, name: str = "<name>") -> list[str]:
    """The spending and Jev defaults `ttp setup` and `ttp new` state, from the budget they apply."""
    b, reserve = budget, float(budget.get("reserve_pct", 10))
    out = [f"spending caps (usage-billed accounts): {_usd(b.get('daily_usd'))}/day and {_usd(b.get('weekly_usd'))}/week "
           f"per project, {_usd(b.get('global_daily_usd'))}/day across all projects "
           f"(budget.daily_usd, budget.weekly_usd, budget.global_daily_usd; 0 turns the global cap off)",
           f"subscription plans: used up to {100 - reserve:g}% of each window (budget.reserve_pct {reserve:g}); "
           f"if you share the plan with your own chats, only that {reserve:g}% is left for them: raise "
           f"budget.reserve_pct to keep more",
           "free Codex window resets: used automatically, at most one per window (budget.reset_credits_auto)",
           f"change one: ttp config {name} KEY VALUE"]
    if jev_key:
        out.append("Jev (optional add-on): on, a key is saved. It sends log and alert text to an outside paid "
                   f"service; `ttp config {name} jev.enabled false` turns it off for a project")
    else:
        out.append("Jev (optional add-on): off. It turns on only once you save a key (`ttp secret jev`), and then "
                   "sends log and alert text to an outside paid service")
    return out


def cmd_new(a) -> None:
    if not NAME_RE.match(a.name):
        die("project names use letters, digits, '.', '_' and '-' (max 63)")
    brief = a.describe or ""
    if a.describe_file:
        brief = sys.stdin.read() if a.describe_file == "-" else Path(a.describe_file).expanduser().read_text()
    from . import machines
    if a.host and not machines.names_here(a.host):   # an alias for this machine creates it here
        return new_remote(a, brief)
    root = Path(a.dir).expanduser().resolve() if a.dir else _default_root()
    p = bootstrap(root, a.name, brief, a.provider or detect_provider(), a.home_tz, a.home_from or "")
    register(a.name, {"host": hostname(), "dir": str(p.root)})
    from . import coordinator as coord
    if a.coordinator_effort != coord.DEFAULT_EFFORT:   # before the daemon starts, so its first turn has it
        p.set_config("coordinator.effort", a.coordinator_effort)
    ident = subprocess.run(["git", "-C", str(p.root), "config", "user.email"], capture_output=True, text=True)
    if ident.returncode != 0 or not ident.stdout.strip():
        print("warning: git has no user.email here, so workers cannot commit. Set one "
              "(git config --global user.name/user.email) or tell the coordinator which identity to use.")
    how = "service not installed (--no-service)"
    if not a.no_service:
        from .service import install
        how = install(p)
        _wait_for_daemon(p)    # with no service nothing starts it: no wait
    print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    print(f"created {a.name} in {p.base} · daemon via {how}")
    for ln in defaults_lines(p.config()["budget"], bool((load_secrets().get("jev") or {}).get("key")), a.name):
        print(ln)
    print(coord.route_line(p.config()) + f" (change it: ttp config {a.name} coordinator.effort LEVEL)")
    if a.no_service:
        print(f"daemon: not started (--no-service). Start it by hand with `{hand_start_cmd(p)}`, "
              f"or `ttp start {a.name}` installs a service that keeps it running")
        return
    from . import weblink
    line, rc = weblink.local(p, weblink.link(p))
    print(line)
    if rc:
        sys.exit(rc)


def hand_start_cmd(p: Project) -> str:
    """The shell command that starts a project's daemon without a service, as its service would: the
    harness's own runtime, from the project folder, logging to daemon.out, surviving the shell's exit."""
    q = shlex.quote
    return (f"cd {q(str(p.base))} && nohup {q(str(p.harness / 'bin' / 'ttp'))} daemon {q(str(p.base))} "
            f">> {q(str(p.logs / 'daemon.out'))} 2>&1 &")


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


def remote_version(host: str) -> str:
    """The version of the `ttp` installed on another machine ("" when none or unreadable)."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "cat ~/.tt-project/lib/current/runtime/ttp/__init__.py"],
                       capture_output=True, text=True)
    m = re.search(r'__version__ = "([^"]+)"', r.stdout) if r.returncode == 0 else None
    return m.group(1) if m else ""


def ship_runtime(host: str) -> str:
    """Copy this runtime and template to ~/.tt-project/lib/<version> on another machine and make it
    that machine's installed `ttp`. Returns the remote launcher path. A machine that already has
    this version or a newer one keeps its own: an older runtime never replaces a newer install."""
    from .release import is_newer
    there = remote_version(host)
    if there and not is_newer(__version__, there):
        return "~/.tt-project/lib/current/bin/ttp"
    stage = f".tt-project/lib/{__version__}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"rm -rf ~/{stage} && mkdir -p ~/{stage}"])
    mac = ["--no-xattrs", "--no-mac-metadata"] if sys.platform == "darwin" else []
    parts = [d for d in ("runtime", "template", "bin", "skills") if (PLUGIN_ROOT / d).exists()]
    tar = subprocess.Popen(["tar", *mac, "--exclude", "__pycache__", "-C", str(PLUGIN_ROOT), "-czf", "-",
                            *parts], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
    launcher = ship_runtime(host)
    if load_secrets() and not a.no_secrets:
        print(push_secrets(host))
    from . import machines as mm
    print(mm.push(host))        # its daemon reads the machines list there
    args = [launcher, "new", a.name, "--dir", a.dir, "--provider", a.provider or detect_provider()]
    # This workstation's zone, never the box's (that runtime is at least this one: ship_runtime). In an
    # ssh login here it is a server's, so none: the box falls back and the next workstation connect sets it.
    if not timefmt.in_ssh_login():
        args += ["--home-tz", timefmt.detect_local(), "--home-from", hostname()]
    if a.no_service:
        args.append("--no-service")
    from . import coordinator as coord
    if a.coordinator_effort != coord.DEFAULT_EFFORT:
        args += ["--coordinator-effort", a.coordinator_effort]
    remote = " ".join(shlex.quote(x) if not x.startswith("~/") else x for x in args) + " --describe-file -"
    from . import weblink
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], input=brief, text=True, stdout=subprocess.PIPE)
    _echo_without_web(r.stdout)
    if r.returncode not in (0, weblink.UNVERIFIED):   # UNVERIFIED: created, but its web app failed the check there
        die(f"remote creation on {host} failed", r.returncode)
    entry = {"host": host, "dir": a.dir}
    register(a.name, entry)
    if a.no_service:        # no daemon there, so no web app yet: its output said how to start one
        return
    line, rc = remote_web(a.name, entry, r.stdout)
    print(line)
    if rc:
        sys.exit(rc)


def _echo_without_web(out: str | None) -> None:
    """Print a remote command's output without its web app line: that link is for the far machine's
    own localhost, and the caller prints the one that works here."""
    for ln in (out or "").splitlines():
        if not ln.startswith("web app:"):
            print(ln)


def remote_web(name: str, entry: dict, out: str | None) -> tuple[str, int]:
    """The checked local link to a remote project's web app, read from the web line its own `ttp new`
    or `ttp connect` printed there (or asked for when an older runtime printed none)."""
    from . import weblink
    host = entry.get("ssh") or entry["host"]
    there = next((ln for ln in (out or "").splitlines() if ln.startswith("web app:")), "")
    if "NOT AVAILABLE" in there:    # its own check and repair on that machine failed: nothing to tunnel to
        return f"{there} (on {host})", weblink.UNVERIFIED
    m = weblink.LINK.search(there)
    if not m:
        r = _ssh(entry, ["web", name], capture=True)
        m = weblink.LINK.search(r.stdout or "")
        if not m:
            why = ((r.stderr or r.stdout or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]
            return weblink.bad_line("web", f"could not read the web address from {host} ({why})", []), weblink.UNVERIFIED
    return weblink.remote(name, entry, int(m.group(1)), m.group(2))


def _wait_for_daemon(p: Project, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (p.db.kv("web") or {}).get("port"):
            return
        time.sleep(0.5)


def web_line(p: Project) -> str:
    from .weblink import link
    url = link(p)
    return f"web app: {url}" if url else "web app: not running yet (check `ttp status`)"


# talking ------------------------------------------------------------------------------------------
def cmd_connect(a) -> None:
    p, entry = resolve(a.name)
    if entry:
        argv = ["connect", a.name, *(["--chat", a.chat] if a.chat else []), *(["--label", a.label] if a.label else [])]
        sys.exit(connect_remote(a.name, entry, argv))
    p = need(a.name, sys.argv[1:])
    # The home zone follows the user: the zone a workstation sent, or this machine's own when the
    # user types here (an ssh login here is a server's zone, not the user's).
    tz, src = a.home_tz, a.home_from or ""
    if not tz and not timefmt.in_ssh_login():
        tz, src = timefmt.detect_local(), hostname()
    if tz:
        timefmt.set_home(p, tz, src, f"ttp connect from {src}" if src else "ttp connect")
    chat = a.chat or f"c{pysecrets.token_hex(3)}"
    p.db.x("INSERT INTO chats(id,created,label,host,last_active,last_read) VALUES(?,?,?,?,?,"
           "(SELECT COALESCE(MAX(id),0) FROM messages)) ON CONFLICT(id) DO UPDATE SET last_active=excluded.last_active",
           (chat, time.time(), a.label or "", os.environ.get("TTP_CLIENT_HOST", ""), time.time()))
    print(MARKER.format(name=p.name, host=hostname(), dir=p.root))
    print(f"chat: {chat}")
    print(status_text(p))
    from . import weblink
    line, rc = weblink.local(p, weblink.link(p))
    print(line)
    if rc:
        sys.exit(rc)


def connect_remote(name: str, entry: dict, argv: list[str]) -> int:
    """`ttp connect` on the project's machine, then the checked link through the kept local forward."""
    from . import weblink
    failed = flush_outbox(name, entry)
    if failed is not None and failed.returncode == 255:
        _unreachable(entry, failed.stderr)
        return 255
    # The user's zone only from their workstation: in an ssh login here it is a server's, so none.
    home = [] if timefmt.in_ssh_login() else ["--home-tz", timefmt.detect_local(), "--home-from", hostname()]
    r = _ssh(entry, [*argv, *home], capture=True)
    if home and r.returncode == 2 and "--home-tz" in (r.stderr or ""):   # an older runtime there: connect without it
        r = _ssh(entry, argv, capture=True)
    if r.returncode == 255:
        _unreachable(entry, r.stderr)
        return 255
    if r.stderr:
        sys.stderr.write(r.stderr)
    _echo_without_web(r.stdout)
    if r.returncode not in (0, weblink.UNVERIFIED):
        return r.returncode
    line, rc = remote_web(name, entry, r.stdout)
    print(line)
    return rc


def cmd_say(a) -> None:
    from . import locks
    if in_run() or locks.under_run_agent():
        # A message posted here counts as the user's (pr_approve reads approvals from it), so a run
        # must not post one. Runs report through `ttp note` and their hand-off. The run's agent
        # among this process's ancestors counts too: a run can unset its environment.
        die("ttp say posts a message as the user and is refused inside a run; use `ttp note` or the hand-off")
    from . import broadcast, relay
    finding = (getattr(a, "finding", "") or "").strip()
    if getattr(a, "all", False):
        if finding:
            die("--finding goes to one project: ttp say <project> --finding <text>")
        return _say_all(a)
    if getattr(a, "except_", None):
        die("--except goes with --all")
    p, entry = resolve(a.name)
    # Only the user's verbatim words are the message; what a relayer adds is kept apart (relay.py).
    text, context = relay.split(a.text if a.text != "-" else sys.stdin.read(), a.context)
    if finding and text:   # findings sent with the user's words are more of the relayer's context
        context, finding = "\n".join(x for x in (context, finding) if x), ""
    elif finding:   # the relayer's own findings alone: data, never the user's word
        context = "\n".join(x for x in (finding, context) if x)
    if not text and not finding:
        die("empty message" if not context else NO_WORDS)
    bid = getattr(a, "broadcast", None)
    if entry:
        rc, ref = _say_remote(a.name, entry, text, a.chat, context, bid, finding=bool(finding))
        if rc == 0 and not finding:
            broadcast.note_delivery(text, a.name, ref, bid)
        sys.exit(rc)
    p = p or need(a.name, sys.argv[1:])
    seen, mid = _say_local_or_die(p, a, text, context, bid, kind="finding" if finding else "user")
    if finding:
        print(f"{'already received' if seen else 'sent'} (#{mid}) as {relay.FINDING}")
        return
    # A plain say is recorded too: telling the projects one at a time builds the same record.
    broadcast.note_delivery(text, a.name, f"#{mid}", bid)
    if seen:
        print(f"already received (#{mid})")
    else:
        print(f"sent (#{mid}); the coordinator answers in this chat when it has decided")


NO_WORDS = ("no user's words: --context alone is not a message; send a relayer's own findings alone with "
            "`ttp say <project> --finding <text>`")


def _say_local_or_die(p: Project, a, text: str, context: str, bid: str | None = None,
                      kind: str = "user") -> tuple[dict | None, int]:
    import sqlite3
    from .db import is_busy
    try:
        return _say_local(p, a, text, context, bid, kind)
    except sqlite3.OperationalError as e:   # db.tx already waited and retried
        if not is_busy(e):
            raise
        die(f"the database stayed busy ({e}); the message was not saved. Send it again"
            + (f" with the same --client-id {a.client_id}" if a.client_id else ""), 75)


SAY_ALL = "*all*"   # the project slot of `ttp say --all <text>` (no project name can be this)


def _say_all_argv(argv: list[str]) -> list[str]:
    """`ttp say --all <text>` names no project: put SAY_ALL in the project slot, so `say` keeps both
    positionals required (an optional one would swallow the text of `say <p> --chat c -- <text>`)."""
    if argv[:1] != ["say"] or "--all" not in argv[:argv.index("--") if "--" in argv else len(argv)]:
        return argv
    return ["say", SAY_ALL, *argv[1:]]


_SECRET_VALUED = ("--keys", "--host", "--via", "--url", "--email", "--user-id", "--secret")
VALUE_GIVEN = "<value>"


def _secret_value_argv(argv: list[str]) -> list[str]:
    """`ttp secret set|generate <name> <value...>`: argparse would echo a stray word ("unrecognized
    arguments: <value>"), so each word after the name that is no option of `ttp secret` becomes
    VALUE_GIVEN, after the name and before the options, and cmd_secret_custom refuses it."""
    if argv[:1] != ["secret"] or argv[1:2] not in (["set"], ["generate"]):
        return argv
    pos, opts, stray, it = argv[1:2], [], [], iter(argv[2:])
    for w in it:
        if w == "--":
            stray.append(VALUE_GIVEN)
            break
        elif w in _SECRET_VALUED:
            opts += [w, *(v for v in [next(it, None)] if v is not None)]
        elif w in ("--force", "-h", "--help") or w.split("=", 1)[0] in _SECRET_VALUED:
            opts.append(w)
        elif len(pos) == 1 and not w.startswith("-"):
            pos.append(w)
        elif len(pos) == 1:
            opts.append(w)   # an unknown option before any name: argparse refuses it (not a value)
        else:
            stray.append(VALUE_GIVEN)
    return ["secret", *pos, *stray[:1], *opts]


def _secret_env_argv(argv: list[str]) -> list[str]:
    """`ttp secret env [alias] --secret <name>[:<key>]=<VAR> ... -- <command...>`: argparse takes no
    positional after an option and drops a '--' inside the command, so the options go first and each
    command word rides exactly in a hidden --cmd-word=:<word> (the ':' keeps argparse from dropping a
    '--' word)."""
    if argv[:2] != ["secret", "env"]:
        return argv
    end = argv.index("--") if "--" in argv else len(argv)
    if not any(w == "--secret" or w.startswith("--secret=") for w in argv[2:end]):
        return argv
    opts, pos, it = [], [], iter(argv[2:end])
    for w in it:
        if w == "--secret":
            opts += [w, *(v for v in [next(it, None)] if v is not None)]
        elif w.startswith("-"):
            opts.append(w)
        else:
            pos.append(w)
    return ["secret", *opts, "env", *pos, *(f"--cmd-word=:{w}" for w in argv[end + 1:])]


_SAY_VALUED = ("--chat", "--context", "--finding", "--except", "--broadcast", "--client-id")


def _say_finding_argv(argv: list[str]) -> list[str]:
    """`ttp say <p> --finding <text>` has no user's words: give the required text an empty one (an
    optional positional would swallow the text of `say <p> --chat c -- <text>`)."""
    if argv[:1] != ["say"] or "--" in argv or not any(x == "--finding" or x.startswith("--finding=") for x in argv):
        return argv
    i, positional = 1, 0
    while i < len(argv):
        positional += not argv[i].startswith("-")
        i += 2 if argv[i] in _SAY_VALUED else 1
    return [*argv, "--", ""] if positional == 1 else argv


def _say_all(a) -> None:
    """`ttp say --all`: the user's words to every project `ttp list` shows, once each. The words'
    broadcast (broadcast.py) skips the projects that already got them within its reuse window, and
    each project gets the broadcast id, so none relays them on."""
    from . import broadcast, relay
    if a.chat or a.client_id:
        die("--all goes to every project without a chat: --chat names one project's chat")
    if a.name != SAY_ALL:
        die("ttp say --all takes only the message: ttp say --all [--except p,q] [--context ..] <text>")
    text, context = relay.split(a.text if a.text != "-" else sys.stdin.read(), a.context)
    if not text:
        die("empty message" if not context else NO_WORDS)
    reg = load_registry().get("projects", {})
    skip = {n.strip() for n in (a.except_ or "").split(",") if n.strip()}
    for n in sorted(skip - set(reg)):
        print(f"ttp: --except {n}: no such project in `ttp list`", file=sys.stderr)
    b = broadcast.claim(text)
    bid, sent, had, failed = b["id"], 0, 0, []
    for name, entry in sorted(reg.items()):
        if name in skip or not isinstance(entry, dict):
            continue
        if name in b["delivered"]:
            print(f"{name}: already has it ({b['delivered'][name]})")
            had += 1
            continue
        try:
            if entry.get("host") and entry["host"] != hostname():
                rc, ref = _say_remote(name, entry, text, None, context, bid)
                if rc:
                    failed.append(f"{name} (exit {rc})")
                    continue
                print(f"{name}: {'queued' if ref.startswith('queued:') else 'sent'} ({ref})")
            else:
                if not Project(entry["dir"]).exists():
                    failed.append(f"{name} (no project at {entry['dir']})")
                    continue
                seen, mid = _say_local(Project(entry["dir"]), argparse.Namespace(chat=None, client_id=bid),
                                       text, context, bid)
                ref = f"#{mid}"
                print(f"{name}: {'already received' if seen else 'sent'} ({ref})")
        except Exception as e:   # one project's failure must not stop the others
            failed.append(f"{name} ({type(e).__name__}: {e})")
            continue
        sent += 1
        try:
            broadcast.record(bid, name, ref)
        except OSError as e:   # delivered all the same; a resend within the window may repeat it
            print(f"ttp: could not record {name} in {broadcast.path()} ({e})", file=sys.stderr)
    print(f"broadcast {bid}: sent to {sent} project(s), {had} already had it"
          + (f", {len(skip & set(reg))} left out" if skip & set(reg) else "")
          + (f"; not delivered: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


def _say_local(p: Project, a, text: str, context: str, bid: str | None = None,
               kind: str = "user") -> tuple[dict | None, int]:
    """`kind` 'finding': a relayer's own findings, in `context` with no user's words (`text` '')."""
    seen = None
    with p.db.tx():   # a client id makes a resent message (its confirmation was lost) a no-op
        if a.client_id:
            seen = p.db.one("SELECT id FROM messages WHERE direction='in' AND ref=?", (f"client:{a.client_id}",))
        mid = seen["id"] if seen else p.db.post("in", text.strip(), chat=a.chat or None, channel="chat",
                                                 kind=kind, ref=f"client:{a.client_id}" if a.client_id else None,
                                                 provenance="cli-legacy", context=context, broadcast=bid)
        if a.chat:
            p.db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
    return seen, mid


def cmd_listen(a) -> None:
    p = need(a.name, sys.argv[1:])
    db = p.db
    if a.ack is not None:
        # Acknowledged ids only move forward and never past the newest message. Best effort: the
        # next acknowledgement catches up, so a busy database never ends the listener.
        _listen_write(db, "UPDATE chats SET last_read=MAX(COALESCE(last_read,0), MIN(?, (SELECT COALESCE(MAX(id),0) "
                      "FROM messages))) WHERE id=?", (a.ack, a.chat), f"acknowledging #{a.ack}")
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
    # Claim the chat before stopping the older listener: while it shuts down, a third listener
    # must find this one in the lock, and the older one's cleanup must not remove it.
    tmp = lock.with_name(f"{lock.name}.{os.getpid()}")
    tmp.write_text(str(os.getpid()))
    os.replace(tmp, lock)
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
    _on_replaced(lock, a.chat)
    try:
        _listen_loop(p, db, a, after, floor)
    finally:
        try:
            if lock.read_text().strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def _on_replaced(lock: Path, chat: str) -> None:
    """On SIGTERM from a newer listener (it claimed the lock first), say so on stderr and exit
    LISTEN_REPLACED, so a forwarder on another machine stops instead of reconnecting and taking
    the chat back. Any other SIGTERM ends the listener as before."""
    def handler(signum, frame):
        try:
            newer = int(lock.read_text())
        except (OSError, ValueError):
            newer = 0
        if newer and newer != os.getpid() and _pid_alive(newer):
            print(f"{REPLACED_MARK} for chat {chat} (pid {newer}); this one stops without reconnecting "
                  f"(exit {LISTEN_REPLACED})", file=sys.stderr, flush=True)
            sys.exit(LISTEN_REPLACED)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    signal.signal(signal.SIGTERM, handler)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _listener_alive(pid: int, chat: str) -> bool:
    """True if pid is a live `ttp listen` for this chat (not a recycled pid)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if zombie(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
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
            kind = f"ask {m['id']}" if m["kind"] == "ask" else m["kind"]   # "ask N": "#N" reads as a task
            who = "coordinator" if m["chat"] else f"{p.name} ({kind}, {m['severity']})"
            print(f"[#{m['id']} {who}] {m['text']}", flush=True)
        if top > after:
            after = top
            # With --ack, only an acknowledgement marks messages read, so what was printed to a
            # reader that is gone comes back on the next listen.
            if a.ack is None:
                _listen_write(db, "UPDATE chats SET last_read=? WHERE id=?", (after, a.chat), f"marking #{after} read")
        if msgs:
            _listen_write(db, "UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat), "noting activity")
            if a.once:
                return
        if deadline and time.time() > deadline:
            return
        time.sleep(poll_s(2))


def _listen_write(db, sql: str, args: tuple, what: str) -> None:
    """A listener's own bookkeeping write (db.x retries a busy database). Still busy after that: say so
    on stderr and go on listening. Each of these writes is idempotent and the next one catches up."""
    import sqlite3
    from .db import is_busy
    try:
        db.x(sql, args)
    except sqlite3.OperationalError as e:
        if not is_busy(e):
            raise
        print(f"ttp: {what} skipped, the database stayed busy ({e}); the next update catches up",
              file=sys.stderr, flush=True)


STATUS_WIDTH = 400   # `ttp status` lines end by here, at a word (quoted texts end sooner)


def clip(text: str, width: int = STATUS_WIDTH) -> str:
    """`text` cut at the last word boundary within `width`, with an ellipsis when cut."""
    if len(text) <= width:
        return text
    cut = text[:width - 1]
    if (sp := cut.rfind(" ")) > width // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—-") + "…"


def status_text(p: Project, verbose: bool = False) -> str:
    """One screen: what needs the user now first, then the budget and the work, then information.
    `verbose` adds the diagnostic lines (coordinator cache, checks slot waits, unacked upstream fixes)."""
    from .alerts import feed
    from .web import at, attention, health, since
    db = p.db
    state = daemon_state(p)
    h = health(p, db, alive=state.startswith("running"))
    now = time.time()
    tz = timefmt.zone(p)
    counts = db.status_counts()
    from . import projpause
    pause = projpause.current(db, now)
    head = f"{p.name}: daemon {state}" + (f" ({projpause.describe(pause, p, now)})" if pause else "")
    head += " · tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none yet")
    head += " · " + timefmt.long(now, p)
    lines = [head]
    if h["runtime"]["line"]:
        lines.append(h["runtime"]["line"])
    from . import hostcycle
    if cyc := hostcycle.hold(p, now):
        lines.append(hostcycle.hold_line(cyc, p, now))
    from . import lease
    if lease_line := lease.status_line(p, p.config(), now):
        lines.append(lease_line)
    for m in attention(db, now)[:6]:
        text = " ".join(m["text"].split())
        what = f"ask {m['id']}" if m["kind"] == "ask" else "alert"
        lines.append(f"  needs you ({what}, {since(m['ts'], now)} ago): {clip(text, 300)}")
    # Blocked tasks: those that wait on the user join the top; the rest are the project's to move.
    from . import waits
    wy = waits.on_user(db, now, tz)
    you, stuck = wy["tasks"], wy["held"]
    for t in wy["gated"][:6]:
        lines.append(f"  needs you (#{t['id']}, gated on an open ask): {t['title']}")
    for t in you[:6]:
        lines.append(f"  needs you (#{t['id']}, {t['wait']['age_text']}): {t['title']} — {t['wait']['text']}")
    lines.append("budget: " + h["spend"]["headline"])
    c = h["coordinator"]
    coord = "coordinator: no turn yet"
    if c["last_turn"]:
        coord = f"coordinator: last turn {int((now - c['last_turn']) // 60)} min ago" + (
            f" ({c['last_status']})" if c["last_status"] else "")
    if c["failures"]:
        coord += f" · {c['failures']} failed in a row"
    if c["backoff_until"]:
        coord += f" · retry at {at(c['backoff_until'], now, tz)}"
    if c["idle_wake"]:
        coord += f" · next idle check {at(c['idle_wake'], now, tz)}"
    elif c["idle_held"]:
        coord += f" · idle check {c['idle_held']}"
    lines.append(coord)
    lines.append(c["route_line"])
    from . import budget
    cache = budget.coordinator_cache(db, now)["24h"] if verbose else {}
    if cache.get("turns"):
        lines.append(budget.coordinator_cache_line(cache))
    from . import pushq
    pq = pushq.status_line(p, now)
    if pq:
        lines.append(pq)
    from . import replica
    rl = replica.status_line(p, p.config(), now)
    if rl:
        lines.append(rl)
    for b in h.get("breakers") or []:
        lines.append(b["line"])
    for pp in h["providers_paused"]:
        lines.append(f"{pp['provider']} paused until {at(pp['until'], now, tz)}: {pp['note']} — fix: {pp['fix']}")
    from . import fleethold
    for hold in sorted(fleethold.active().values(), key=lambda x: x.get("id") or 0):
        lines.append(fleethold.line(hold, p))
    for pr in h["resources_paused"]:
        if pr.get("fleet"):
            continue   # one line per hold above
        lines.append(f"resource {pr['resource']} paused since {at(pr['since'], now, tz)} by {pr.get('by') or 'user'}"
                     + (f" in {pr['project']} (shared: holds in every project)" if pr.get("shared") else "")
                     + (f": {pr['reason']}" if pr.get("reason") else "") + (f"; {pr['end']}" if pr.get("end") else "")
                     + f" — resume: ttp resume {p.name} --resource {pr['resource']}")
    from . import checkcap, locks, shared, testslots
    if host := checkcap.line():
        lines.append(host)
    lines.extend(testslots.use_lines())
    from . import checklane, checkspill
    if suite := testslots.suite_line(p.name, now, checklane.slots(p.config())):
        lines.append(suite)
    lines.extend(checklane.long_wait_lines(p.name, now))
    lines.extend(checkspill.status_lines(p.name, now))
    if verbose and (slot_waits := checkcap.wait_line(p.state, now)):
        lines.append(slot_waits)
    for res in sorted(shared.names(p.config())):
        who = locks.held(shared.root() / res, p)
        lines.append(f"shared {res}: " + ("; ".join(w.split(": ", 1)[-1] for w in who) if who else "free"))
    for res, got in shared.mismatches(p).items():
        lines.append(f"shared {res}: projects give different slot counts ("
                     + ", ".join(f"{k} {n}" for k, n in sorted(got.items())) + f"); all use {min(got.values())}")
    if h["why_idle"]:
        lines.append(f"idle: {h['why_idle']}")
    elif h["held"]:
        lines.append(f"held: {h['held']}")
    for w in h["working"][:8]:
        what = f"#{w['task']} {w['title']}" if w["task"] else w["role"]
        lines.append(f"  running {since(w['started'], now)}: {what}" + (f" ({w['wake']} wake)" if w.get("wake") else "")
                     + (f" — {w['note']}" if w["note"] else ""))
    from . import push
    lines.extend(push.status_lines(p))
    in_review = db.review_since()
    if stuck:
        lines.append("stuck, the project is on it:")
    for t in stuck[:8]:
        lines.append(f"  #{t['id']} {t['wait']['age_text']}: {t['title']} — {t['wait']['text']}")
    for d in h.get("drivers_stalled") or []:
        lines.append(f"  {d}")
    for t in db.q("SELECT id,title,status,blocked_reason FROM tasks WHERE status IN ('review','pushing') "
                  "ORDER BY status, id LIMIT 8"):
        age = f" {since(in_review[t['id']], now)}" if t["id"] in in_review else ""
        lines.append(f"  #{t['id']} {t['status']}{age}: {t['title']}" + (f" — {t['blocked_reason']}" if t["blocked_reason"] else ""))
    if h.get("schedules_broken"):
        lines.append(h["schedules_broken"])
    if h.get("schedules_waiting"):
        lines.append(h["schedules_waiting"])
    if h.get("health"):
        lines.append(h["health"])
    if h.get("coverage"):
        lines.append(h["coverage"])
    lines.extend(h.get("obligations") or [])
    from . import mitigate
    lines.extend(mitigate.status_lines(db, now, tz))
    from . import sweepskip
    lines.extend(sweepskip.status_lines(db, now, tz))
    lines.extend(h.get("outages") or [])
    from . import screen as scr
    for m in scr.mutes(db, now):
        lines.append(f"muted: {scr.mute_line(m, now, p)}")
    if h.get("host"):
        lines.append(h["host"])
    if h.get("idle_sleep"):
        lines.append(h["idle_sleep"])
    if h.get("release"):
        lines.append(h["release"])
    if h.get("mcp_gap"):
        lines.append(h["mcp_gap"])
    if h.get("local_only"):
        lines.append(h["local_only"])
    if h.get("uncommitted"):
        lines.append(h["uncommitted"])
    from . import upstream
    cfg = p.config()
    ups = "; ".join(x for x in (upstream.status_line(db, cfg), verbose and upstream.fixes_line(p.name, cfg, now),
                                upstream.forward_line(now)) if x)
    if ups:
        lines.append(ups)
    disk = db.kv("disk_low")
    if disk:
        lines.append(f"disk: only {disk['free_gb']} GB free under {disk['path']} (guard {disk.get('threshold_gb', '?')} GB); "
                     f"only questions and plans start")
    for t in h["logged_out"][:5]:
        lines.append(f"  #{t['id']} held: logged out: {t['title']}")
    for t in h.get("net_held", [])[:5]:
        lines.append(f"  #{t['id']} held: network: {t['title']}")
    for t in h["waiting"][:5]:
        why = re.sub(r";? *next try \S+$", "", t["blocked_reason"] or "").strip()
        lines.append(f"  #{t['id']} waiting, next try {at(t['not_before'], now, tz)}: {t['title']}" + (f" — {why}" if why else ""))
        lines += [f"    job {j['name']}: {j['line']}" for j in (db.kv("job_use", {}) or {}).values() if j.get("task") == t["id"]]
    from .coordinator import BROKEN_START
    for t in h["deferred"][:5]:
        broken = t["starts"].startswith(BROKEN_START + ":")
        lines.append(f"  #{t['id']} {t['starts'] if broken else 'deferred, ' + t['starts']}: {t['title']}"
                     + (f" — {t['gate']['text']}" if t.get("gate") else ""))
    if h["undelivered"]:
        u = h["undelivered"]
        lines.append(f"chat relay: {u['asks']} question(s) not delivered to any chat since {at(u['since'], now, tz)}; "
                     f"is the chat's `ttp listen` running?")
        if u["below_floor"]:
            lines.append(f"  {u['below_floor']} of them are below every chat's severity floor; lower "
                         f"notify.chat_min_severity or the chat's own floor to see them")
    recent = feed(db, now, limit=3)
    if recent:
        lines.append("recent:")
    for m in recent:
        text = " ".join(m["text"].split())
        tag = f"cleared {at(m['cleared_at'], now, tz)}" if m.get("cleared_at") else m["state"]
        lines.append(f"  {at(m['ts'], now, tz)} ({tag}) {clip(text, 160)}")
    return "\n".join(clip(ln) for ln in lines)


def daemon_state(p: Project) -> str:
    """'running', or NOT RUNNING with why: its process is gone, or it stopped completing ticks."""
    from . import identity
    s = identity.probe(p.state, p.db.kv("daemon", {}), hostname())
    if s["state"] == "running":
        return "running" if s["source"] == "pid" else f"running (heartbeat {int(s['age'])}s ago)"
    if s["state"] == "unknown":
        return "status unknown from this sandbox"
    if s["state"] == "stale":
        return f"NOT RUNNING (process {s['pid']} is up but stuck: no completed tick for {int(s['age'] // 60)} min)"
    return "NOT RUNNING"


def here() -> Project | None:
    """The project this process runs in: its run's project, else the one at the folder it was started
    in or above it (its git top level included), else, in a git worktree, the one at its main checkout."""
    if os.environ.get("TTP_PROJECT") and Project(os.environ["TTP_PROJECT"]).exists():
        return Project(os.environ["TTP_PROJECT"])
    if p := next((c for d in [Path.cwd(), *Path.cwd().parents] if (c := Project(d)).exists()), None):
        return p
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = (Path.cwd() / r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    if common and common.name == ".git" and (c := Project(common.parent)).exists():
        return c
    return None


# Commands whose first positional is the project: with none given, it is the one here().
PROJECT_CMDS = frozenset({"web", "connect", "logs", "prune", "say", "listen", "task", "memory", "schedules", "pause",
                          "resume", "drain", "start", "stop", "restart", "config", "upgrade", "alerts"})


def _positionals(sp: argparse.ArgumentParser, args: list[str]) -> list[str]:
    """The positional words of `args` for subcommand parser `sp`, skipping its options and their values."""
    out, i = [], 0
    while i < len(args):
        t = args[i]
        i += 1
        if t == "--":
            return out + args[i:]
        if not t.startswith("-") or t == "-":
            out.append(t)
            continue
        act = sp._option_string_actions.get(t)
        if act is None or act.nargs == 0:
            continue
        if act.nargs in ("*", "+"):
            while i < len(args) and not args[i].startswith("-"):
                i += 1
        elif act.nargs == "?":
            i += i < len(args) and not args[i].startswith("-")
        else:
            i += act.nargs if isinstance(act.nargs, int) else 1
    return out


def _project_name_argv(sub, argv: list[str]) -> list[str]:
    """`ttp <command>` without its project name: put the project here() (the run's, else the current
    folder's) in the name slot. The slot counts as empty when no positional is given, or when the first
    is no known project, a later slot is free and the word fits the next slot's choices (`task list`).
    With no project here, argv is unchanged and argparse asks for the name as before."""
    global _HERE
    _HERE = None
    if not argv or argv[0] not in PROJECT_CMDS or argv[0] not in sub.choices:
        return argv
    if argv[0] == "config" and ("--account" in argv or "--chat-settable" in argv):   # its first word is the key
        return argv
    sp = sub.choices[argv[0]]
    slots = [x for x in sp._actions if not x.option_strings]
    if not slots or slots[0].dest != "name":
        return argv
    room = sum(1 if x.nargs in (None, "?") else x.nargs if isinstance(x.nargs, int) else 99 for x in slots)
    pos = _positionals(sp, argv[1:])
    if pos and (len(pos) >= room or local_project(pos[0]) or remote_entry(pos[0])
                or (slots[1].choices and pos[0] not in slots[1].choices)):
        return argv
    p = here()
    if not p:
        return argv
    _HERE = p
    return [argv[0], p.name, *argv[1:]]


def cmd_status(a) -> None:
    if a.name:
        p = need(a.name, sys.argv[1:])
    else:
        p = here()
        if not p:
            die("no tt-project project here; name one: `ttp status <name>`")
    if a.json:
        from .web import state_payload
        print(json.dumps(state_payload(p, p.db), default=str, indent=1))
    else:
        print(status_text(p, verbose=a.verbose))
        from . import notifier
        for ln in notifier.status_lines():   # this machine's notifier: on old code, or failing to deliver
            print(ln)
    from . import upstream
    upstream.maybe_fallback()   # the daemon cannot reach a forward target: send its notes from this shell


def cmd_stats(a) -> None:
    """Context re-read (cache-read) tokens per run and per $, by role, kind, tier and effort."""
    from . import budget as bud
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp stats <name>`")
    if a.tools:
        s = bud.tool_output_stats(p.db, p.runs, a.days)
        print(json.dumps(s, indent=1) if a.json else bud.tool_output_text(s))
        return
    tz = timefmt.home(p)
    since, until, at = (_stats_time(v, tz, flag) for v, flag in ((a.since, "--since"), (a.until, "--until"),
                                                                 (a.split_at, "--split-at")))
    if a.runs or a.tier:
        from . import route
        rows = route.runs(p.db, a.tier, since if since is not None else time.time() - a.days * 86400, until)
        print(json.dumps(rows, indent=1) if a.json else route.runs_text(rows))
        return
    if at is not None:
        span = a.days * 86400
        s = bud.reread_split(p.db, at, since if since is not None else at - span,
                             until if until is not None else min(time.time(), at + span), top=a.top)
        print(json.dumps(s, indent=1) if a.json else bud.reread_split_text(s, tz))
        return
    s = bud.reread_stats(p.db, a.days, top=a.top, since=since, until=until)
    print(json.dumps(s, indent=1) if a.json else bud.reread_text(s, tz if since or until else None))


def _stats_time(v: str | None, tz: str, flag: str) -> float | None:
    """A `ttp stats` time: epoch seconds, or an ISO date or date-time in the project's home zone
    unless it names its own offset ('2026-10-12', '2026-10-12 14:00', '2026-10-12T21:00Z')."""
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    try:
        d = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        die(f"{flag}: {v!r} is not a time; give epoch seconds or an ISO date, e.g. 2026-10-12 14:00")
    return (d if d.tzinfo else d.replace(tzinfo=timefmt.tzinfo(tz))).timestamp()


def cmd_replicate(a) -> None:
    """Standby state snapshots (replica.py)."""
    from . import replica
    if a.restore:
        if not a.into:
            die("--restore needs --into PATH: where the project folder is made")
        try:
            r = replica.restore(a.restore, a.into, a.seq)
        except replica.SnapshotError as e:
            die(f"not restored: {e}")
        print(json.dumps(r, indent=1) if a.json else
              f"restored snapshot #{r['seq']} into {r['project_dir']} (harness at {r['harness_head'][:10]}; "
              f"to apply next: {r['import']})")
        return
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp replicate <name>`")
    cfg = p.config()
    if not replica.settings(cfg):
        print("standby snapshots are off: set failover.standby to a machine alias or an absolute directory")
        return
    if a.verify:
        v = replica.verify(cfg, replica.identity(p, cfg))
        if a.json:
            print(json.dumps(v, indent=1))
        elif "error" in v:
            print(f"could not verify: {v['error']}")
        else:
            for x in v["snapshots"] or [{"seq": None}]:
                print("no snapshots" if x["seq"] is None else f"#{x['seq']}: " + ("ok" if x["ok"] else f"BAD: {x['why']}"))
        snaps = v.get("snapshots") or []
        sys.exit(0 if snaps and snaps[-1]["ok"] else 1)
    if a.now:
        r = replica.snapshot(p, cfg)
        print(json.dumps(r, indent=1) if a.json else f"snapshot: {r['outcome']}" + (
            f" ({r.get('error')})" if r.get("error") else f" #{r['seq']}" if r["outcome"] == "ok" else ""))
        sys.exit(1 if r["outcome"] == "failed" else 0)
    print(json.dumps(replica.load_state(p), indent=1) if a.json else replica.status_line(p, cfg, time.time()))


def cmd_audit(a) -> None:
    """The daily review's self-efficiency audit of this project's last `--hours`, model-free."""
    from . import audit
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp audit <name>`")
    hours = float((p.config().get("review") or {}).get("blocked_long_h", audit.BLOCKED_H))
    if a.json:
        print(json.dumps(audit.report(p.db, window_s=a.hours * 3600, blocked_h=hours, p=p), indent=1, default=str))
    else:
        print("\n".join(f"- {line}" for line in audit.lines(p.db, window_s=a.hours * 3600, blocked_h=hours, p=p)))


def cmd_ttr(a) -> None:
    """Time to recover per machine (ttr.py): incidents, median, p90 and max TTR, open ones, the last one."""
    from . import ttr
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp ttr <name>`")
    if a.json:
        print(json.dumps({"zone": timefmt.home(p), "days": a.days,
                          "machines": ttr.report(p.db, a.alias, a.days, project=p.name)}, indent=1))
        return
    got = ttr.lines(p.db, timefmt.home(p), a.alias, a.days, project=p.name)
    print("\n".join(got) if got else f"no machine incidents in {a.days:g} d"
          + (f" on {a.alias}" if a.alias else ""))


def cmd_push_audit(a) -> None:
    """Done tasks of the last days whose claimed pushes are not on the remote (pushclaim.audit), with
    counts: confirmed, missing, unknown (and unverifiable, shared when any). Read-only: it only fetches the remote's tracking refs."""
    from . import pushclaim
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp push-audit <name>`")
    got = pushclaim.audit(p, days=a.days)
    if a.json:
        print(json.dumps(got, indent=1))
        return
    for g in got["gaps"]:
        print(f"#{g['task']} {g['state']}: {g['why']}")
    print(pushclaim.summary_line({**got, "gaps": []}))


def cmd_spend_today(a) -> None:
    """This machine's projects' spend in [since, until) by provider and account key, and its other
    Claude Code sessions as estimated (globalcap.answer): what another machine's global daily total
    asks for over ssh (globalcap.fetch). The account itself stays here.
    `--receive` keeps what a machine that cannot be asked pushes here instead (globalcap.push)."""
    from . import globalcap as gcap
    now = time.time()
    if a.receive:
        ack, rc = gcap.receive(sys.stdin.buffer.read(gcap.RECEIVE_BYTES + 1), a.via or "", now)
        if rc == 0 and ack.get("tz"):
            # The sender's zone moves the home zone of this machine's projects that it set last.
            import sqlite3
            for e in load_registry().get("projects", {}).values():
                if e.get("host") in (hostname(), "localhost") and e.get("dir") and Project(e["dir"]).exists():
                    try:
                        timefmt.follow_push(Project(e["dir"]), ack["tz"], ack.get("from") or "")
                    except (OSError, RuntimeError, sqlite3.Error):
                        pass
            ack = {k: v for k, v in ack.items() if k not in ("tz", "from")}
        print(json.dumps(ack, sort_keys=True))
        raise SystemExit(rc)
    b = deep_merge(DEFAULT_CONFIG["budget"], load_account_settings().get("budget") or {})
    if a.since is None:
        a.since, a.until, _ = gcap.window(b, now)
    t = gcap.answer(a.since, a.until if a.until is not None else now + gcap.DAY, now=now)
    if a.json:
        print(json.dumps(t))
        return
    o = t.get(gcap.SESSIONS)
    other = (f", other Claude Code sessions {gcap.money(o['usd'])} (estimated)" if o
             else ", other Claude Code sessions unknown")
    print(f"{t['host']}: {len(t['projects'])} projects, {gcap.money(sum(r['usd'] for r in t['rows']))}{other} since "
          f"{timefmt.long(a.since, gcap.zone(b)[0])}" +
          "".join(f"\n  could not read {e}" for e in t["errors"]))


def cmd_web(a) -> None:
    from . import consent, tunnel, weblink
    if a.unkeep:
        print(tunnel.unkeep(a.name))
        return
    if a.keep and not a.tunnel:
        die("--keep goes with --tunnel: `ttp web NAME --tunnel --keep`")
    p, entry = resolve(a.name)
    if p:
        if a.keep:
            print(f"{a.name} runs on this computer: there is no tunnel to keep")
        line, rc = weblink.local(p, weblink.link(p))
        print(line)
        if rc:
            sys.exit(rc)
        return
    if not entry:
        die(f"no project {a.name!r}")
    try:
        host = sshcmd.host(entry)
    except sshcmd.BadHost as e:
        die(str(e))
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "--", host, sshcmd.remote_cmd(entry, ["web", a.name])],
                       capture_output=True, text=True)
    if r.returncode == 255:
        _unreachable(entry, r.stderr)
        sys.exit(255)
    m = weblink.LINK.search(r.stdout)
    if not m:
        die(f"could not read the web address from {host}: {(r.stderr or r.stdout).strip()[-200:]}")
    from .web import free_port
    remote_port, tok = int(m.group(1)), m.group(2)
    kept = tunnel.installed(a.name)
    if a.keep:
        line, rc = weblink.remote(a.name, entry, remote_port, tok)
        print(line)
        if rc:
            sys.exit(rc)
        return
    opened = False
    if kept and kept["host"] == host and kept["remote"] == remote_port and kept["local"]:
        local, opened = kept["local"], True   # the kept tunnel already forwards; a second one would only clash
        print(f"the kept tunnel forwards localhost:{local} → {host}:{remote_port} ({kept['file']})")
    else:
        local = free_port(remote_port + 100)
        cmd = tunnel.ssh_argv(host, local, remote_port, ssh="ssh")
        why = a.tunnel and consent.gate("tunnel", f"ttp web {a.name} --tunnel")
        if why:
            print(f"tunnel not opened: {why}\n  {' '.join(cmd)}")
            sys.exit(consent.NEEDS_ANSWER)
        if a.tunnel:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            print(f"tunnel open (pid in background): localhost:{local} → {host}:{remote_port}")
            opened = True
        else:
            print(f"The project runs on {host}. `ttp web {a.name} --tunnel --keep` opens this local forward and "
                  f"keeps it up:\n  {' '.join(cmd)}")
    url = f"http://127.0.0.1:{local}/#token={tok}"
    kind, why = weblink.verify(url, a.name) if opened else ("", "")
    if kind:
        print(weblink.bad_line(kind, why, []))
        sys.exit(weblink.UNVERIFIED)
    print(weblink.ok_line(url) if opened else f"web app (once that forward is open): {url}")


def cmd_note(a) -> None:
    # Model-free completion commands get only note context, never a full worker context.
    run_dir = os.environ.get("TTP_RUN_DIR") or os.environ.get(NOTE_RUN_ENV)
    if getattr(a, "status", None) is not None:
        return _note_status(a, run_dir)
    if a.text is None:
        die("ttp note: the note's text is required", 2)
    if not run_dir:
        die("ttp note only works inside a tt-project run")
    if getattr(a, "reply", None) and a.to:
        die("ttp note: --reply answers the note's sender by its own route; leave out --to", 2)
    if a.to or getattr(a, "reply", None):
        _note_to(a, run_dir)
    durable_append(Path(run_dir) / "progress.md", f"{timefmt.clock()} {a.text}\n")
    from . import upstream
    upstream.maybe_fallback()   # the daemon cannot reach a forward target: send its notes from this run


def cmd_notify(a) -> None:
    """`ttp notify`: a worker's message for the user. The run cannot reach the daemon's database, so
    it is appended to the run's notify file, and the daemon posts it as an alert when the run ends
    (daemon.relay_worker_notifies): low or normal severity, a few per run, an identical text once.
    High goes to the coordinator, not the user: it is synced to the run's escalation file, which the
    daemon reads while the run lives and again at its end (daemon.relay_worker_escalations). Critical
    stays the coordinator's."""
    from .daemon import ESCALATE_FILE, ESCALATIONS_PER_RUN, NOTIFY_FILE
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp notify only works inside a tt-project run")
    if a.severity == "critical":
        die("ttp notify: critical is the coordinator's; use --severity high to escalate to it")
    text = a.text.strip()
    if not text:
        die("empty notify")
    if a.severity == "high":
        durable_append(Path(run_dir) / ESCALATE_FILE, json.dumps({"ts": time.time(), "severity": "high",
                                                                 "text": text}) + "\n")
        durable_append(Path(run_dir) / "progress.md", f"{timefmt.clock()} escalate: {text[:200]}\n")
        print(f"escalated to the coordinator as a high event within a minute (the first {ESCALATIONS_PER_RUN} "
              f"distinct texts per run); the user is not paged directly. Keep working")
        return
    durable_append(Path(run_dir) / NOTIFY_FILE, json.dumps({"ts": time.time(), "severity": a.severity,
                                                           "text": text}) + "\n")
    durable_append(Path(run_dir) / "progress.md", f"{timefmt.clock()} notify: {text[:200]}\n")
    print("queued for the user: it is sent when this run ends; say so in the hand-off")


def _note_to(a, run_dir: str) -> None:
    """`ttp note --to <project>`: a worker's note for another project, filed in the user's upstream
    inbox with this run's project and task (upstream.send); one on another machine is sent on there by
    the daemon (upstream.forward). Nothing of the other project's is written; its daemon reads the
    note as untrusted data from this worker. A note past upstream.SPEC_CHARS once whitespace is joined
    is refused whole, never cut; a filed or queued one prints the stored length and a short hash. A name
    this machine's registry does not know is looked up on the forward targets (upstream.find_peer), and
    the note goes to the one that runs it. `--reply <fp>` and `--to <project>@<machine>` take the route a
    received note came in by (upstream.reply_route, upstream.peer_route), so a sender this machine does
    not know is still answered; with no route the note is refused, never dropped."""
    from . import upstream
    base = os.environ.get("TTP_PROJECT")
    me = Project(base) if base else None
    if not me or not me.exists():
        die("ttp note --to: this run has no project (TTP_PROJECT)")
    at = back = None
    routed = bool(getattr(a, "reply", None)) or "@" in a.to
    if routed:
        try:
            a.to, at, back = (upstream.reply_route(me, a.reply) if getattr(a, "reply", None)
                              else upstream.peer_route(me, *a.to.split("@", 1)))
        except ValueError as e:
            die(f"not sent: {e}. Nothing was filed; put the note in the hand-off instead")
    if a.to == me.name:
        die("ttp note --to names this run's own project; report to it with `ttp note` and the hand-off")
    far = None if routed else remote_entry(a.to)
    targets = upstream.forward_to()
    if far and targets is not None and (far.get("ssh") or far["host"]) not in targets:
        die(f"project {a.to} runs on another machine, and upstream.forward_to does not send notes there. "
            f"Put the note in the hand-off as a follow-up titled `upstream: ...` instead")
    if not routed and not far and not local_project(a.to) and a.to == upstream.MAINTAINERS:
        return _note_upstream(a, me)
    if not a.text.strip():
        die("empty note")
    if not routed and not far and not local_project(a.to):
        checked = []
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}", a.to):
            at, checked = upstream.find_peer(a.to)
        if not at:
            got = upstream.senders(me, a.to)
            how = (f". {a.to} sent this project note {got[0][0]}"
                   + (f" from {got[0][1]['via']}" if got[0][1].get("via") else "")
                   + f": answer it with `ttp note --reply {got[0][0]} \"<text>\"`, which takes the route it came by"
                   + (f" (or `--to {a.to}@{got[0][1]['via']}`)" if got[0][1].get("via") else "")) if got else ""
            die(f"unknown project {a.to}: not on this machine (`ttp list`), "
                + (f"nor on the machines notes are sent on to: {'; '.join(checked)}" if checked
                   else "and there is no other machine to ask (`ttp upstream --forward-status`)") + how)
    try:
        text = upstream.note_text(a.text)
    except upstream.NoteTooLong as e:
        die(f"not sent: {e}. Nothing was filed or cut. Shorten it, with the question or request first, "
            f"or put the whole text in the hand-off as a follow-up")
    from . import taskfacts
    # The tasks it names, as the records show them now: what it states of them may be stale.
    text = taskfacts.note_suffix(me, text, upstream.SPEC_CHARS)
    sent = f"; stored {upstream.receipt(text)}"
    task = os.environ.get("TTP_TASK") or ""
    try:
        got, place = upstream.send_or_queue(me.state, me.name, int(task) if task.isdigit() else None, a.to,
                                            text, a.severity, at=at, back=back)
    except OSError as e:
        # A sandbox that cannot write the inbox (read-only home): the run's directory keeps the note,
        # and this project's daemon files it when the run ends (upstream.send_queued).
        upstream.queue(Path(run_dir), a.to, text, a.severity, at=at, back=back)
        print(f"note for {a.to} queued in this run's directory ({e.strerror or e}); this project's daemon files "
              f"it in its inbox as {upstream.note_id(a.to, text)} when the run ends{sent}")
        return
    if got == "full":
        die(f"not sent: {upstream.OUTBOX_MAX} notes of this project already wait to go to other projects. "
            f"Put it in the hand-off instead and keep working")
    if got == "queued":
        print(f"note for {a.to} queued: number {place} in this project's outbox (other projects get at most "
              f"{upstream.NOTES_PER_HOUR} of its notes an hour); its daemon files it as "
              f"{upstream.note_id(a.to, text)} when the limit allows{sent}. Nothing to wait for: keep working")
        return
    print(f"note for {a.to} " + ("already in its inbox" if got == "duplicate" else "filed in its inbox")
          + f" as {upstream.note_id(a.to, text)}" + sent
          + (f"; this machine cannot reach {back}, so it waits here until {back}'s daemon takes it on its next "
             f"call (it asks at least hourly for a week after sending notes here)" if back else "")
          + (f"; this machine's daemon sends it on to {(far.get('ssh') or far['host']) if far else at}" if far or at else "")
          + f"; `ttp note --status {upstream.note_id(a.to, text)}` shows whether {a.to} read it")
    target = (far.get("ssh") or far["host"]) if far else at
    if target and target in upstream.failing():
        print(f"warning: {upstream.forward_line()}. The note stays queued and is sent once the target is "
              f"reached; this run also tries to send it. Nothing to wait for: keep working")


def _note_status(a, run_dir: str | None) -> None:
    """`ttp note --status <id>`: where a `ttp note --to` note stands (upstream.note_status). Exit 0 once
    the project it is for read it, 1 while it is on its way, 2 when it cannot tell: a retry_when."""
    from . import upstream
    if a.text is not None or a.to or getattr(a, "reply", None):
        die("ttp note --status takes only the note's id", 2)
    base = os.environ.get("TTP_PROJECT")
    try:
        rc, line = upstream.note_status(a.status, Project(base).state if base else None,
                                        Path(run_dir) if run_dir else None)
    except ValueError as e:
        die(str(e), 2)
    print(line)
    raise SystemExit(rc)


def _note_upstream(a, me) -> None:
    """`ttp note --to tt-project` (no project of that name here): a lesson for tt-project's maintainers,
    filed in the user's upstream inbox as an `upstream: ...` follow-up of this run would be."""
    from . import upstream
    if not a.text.strip():
        die("empty note")
    try:
        text = upstream.note_text(a.text)
    except upstream.NoteTooLong as e:
        die(f"not sent: {e}. Nothing was filed or cut. Shorten it, with the question or request first, "
            f"or put the whole text in the hand-off as a follow-up titled `upstream: ...`")
    task = os.environ.get("TTP_TASK") or ""
    note = {"title": "upstream: " + upstream.cut_title(text), "spec": text}
    try:
        got = upstream.append(me.name, int(task) if task.isdigit() else None, [note])
    except OSError as e:
        die(f"not sent: this run cannot write the upstream inbox ({e.strerror or e}). Put the note in the "
            f"hand-off as a follow-up titled `upstream: ...`; this project's daemon files it")
    print(f"note for tt-project's maintainers " + ("filed in" if got else "already in")
          + f" the upstream inbox as {upstream.fingerprint(note['title'], text)}; stored {upstream.receipt(text)}")


def cmd_upstream(a) -> None:
    """The user's upstream inbox across machines: `--receive` files notes another machine sends over
    ssh (JSON lines on stdin; prints one JSON ack line), `--forward-status` shows each target this
    machine sends its notes on to, and `--forward-to` sets those targets (`default`: the machines of
    the remote projects; `none`: send nothing). Closing a note: `--resolve <fp> --fixed-in <version>`
    records the maintainers' fix, `--fixed <fp>` is the read-only probe a task waiting on it uses
    (0 once this project's running daemon is at or above that version, 1 before, 2 unknown, 3 when the
    note was closed as not a plugin bug: no fix ships), `--resolve <fp> --wontfix "<reason>"` closes a
    not-a-bug note without a release, `--ack <fp>
    "<evidence>"` closes it for this project, `--regress <fp> "<reason>"` records that a shipped fix did not
    hold (its filer's note then never closes by silence, nor as filer unreachable), `--fp "<title>" "<spec>"` prints the id an `upstream: ...`
    follow-up gets, and `--mine` / `--resolved` list this project's notes / every resolution. With no flag it
    lists the inbox's notes with each sender (project, host, task), open first, then the forward status;
    `--resolve` prints the sender and sends the answer to it once when it is another project on this machine."""
    from . import upstream
    if a.has_project is not None:
        print(json.dumps({"project": a.has_project, "here": upstream.has_project(a.has_project)}))
        return
    if a.note_state is not None:
        try:
            print(json.dumps(upstream.note_state(a.note_state), sort_keys=True))
        except ValueError as e:
            die(str(e), 2)
        return
    if a.fp is not None:
        title, spec = a.fp
        task, me = os.environ.get("TTP_TASK") or "", here()
        # As the daemon files it (upstream.append): a spec past the limit is cut naming the hand-off.
        print(upstream.fingerprint(title.strip()[:upstream.TITLE_CHARS], upstream.cut(
            spec.strip(), f"the hand-off of {me.name if me else '?'} #{task}" if task else "its hand-off")))
        return
    if a.resolve or a.fixed or a.ack or a.regress or a.mine or a.resolved or a.wontfix is not None:
        try:
            if a.wontfix is not None and not a.resolve:
                die("ttp upstream --wontfix needs --resolve <fp>", 2)
            if a.resolve and a.wontfix is not None:
                if a.fixed_in:
                    die("ttp upstream --wontfix closes a note without a release: drop --fixed-in", 2)
                me = here()
                try:
                    r = upstream.wontfix(a.resolve, a.wontfix, me.name if me else "")
                except OSError as e:
                    if not os.environ.get("TTP_RUN_DIR"):
                        raise
                    upstream.queue_wontfix(Path(os.environ["TTP_RUN_DIR"]), a.resolve, a.wontfix)
                    print(f"wontfix of note {a.resolve.strip().lower()} queued in this run's directory "
                          f"({e.strerror or e}); this project's daemon records it when the run ends")
                    _answer_sender({"kind": "wontfix", "fp": a.resolve.strip().lower(),
                                    "note": " ".join(a.wontfix.split())}, me)
                    return
                print(f"note {r['fp']} closed without a release (wontfix): {r['note']}")
                _answer_sender(r, me)
                return
            if a.resolve:
                if not a.fixed_in:
                    die("ttp upstream --resolve needs --fixed-in <version> or --wontfix \"<reason>\"")
                me = here()
                try:
                    r = upstream.resolve(a.resolve, a.fixed_in, a.note or "", me.name if me else "")
                except OSError as e:
                    if not os.environ.get("TTP_RUN_DIR"):
                        raise
                    # A sandbox that cannot write the user's files: this project's daemon records it at the run's end.
                    upstream.queue_resolve(Path(os.environ["TTP_RUN_DIR"]), a.resolve, a.fixed_in, a.note or "")
                    print(f"resolution of note {a.resolve.strip().lower()} queued in this run's directory "
                          f"({e.strerror or e}); this project's daemon records it when the run ends")
                    _answer_sender({"kind": "resolve", "fp": a.resolve.strip().lower(),
                                    "fixed_in": a.fixed_in.strip().lstrip("v")}, me)
                    return
                print(f"note {r['fp']} resolved: fixed in {r['fixed_in']}. The project that filed it is told once its "
                      f"daemon runs that version; it closes the note with `ttp upstream --ack {r['fp']} \"<evidence>\"`")
                _answer_sender(r, me)
                return
            if a.resolved:
                for line in upstream.listing(None) or ["no upstream note is resolved"]:
                    print(line)
                return
            p = here()
            if not p:
                die("no tt-project project here", 2)
            if a.fixed:
                rc, words = upstream.fixed_status(p, a.fixed)
                print(words)
                raise SystemExit(rc)
            if a.regress:
                fp, reason = a.regress
                # The filer's regression blocks its close by silence; a maintainer's ingesting project may also
                # record one for a note no project here filed, so it is never closed as filer unreachable.
                if (fp.strip().lower() not in upstream.filed(p.name)
                        and not (p.config().get("upstream") or {}).get("ingest")):
                    die(f"note {fp} was not filed by {p.name}", 2)
                try:
                    upstream.regress(fp, reason, p.name)
                except OSError as e:
                    if not os.environ.get("TTP_RUN_DIR"):
                        raise
                    upstream.queue_regress(Path(os.environ["TTP_RUN_DIR"]), fp, reason)
                    print(f"regression of note {fp.strip().lower()} queued in this run's directory ({e.strerror or e}); "
                          f"this project's daemon records it when the run ends")
                    return
                print(f"regression of note {fp.strip().lower()} recorded for {p.name}: it no longer closes by silence "
                      f"or as filer unreachable")
                return
            if a.ack:
                fp, evidence = a.ack
                try:
                    upstream.ack(fp, p.name, evidence)
                except OSError as e:
                    if not os.environ.get("TTP_RUN_DIR"):
                        raise
                    # A sandbox that cannot write the user's files: this project's daemon records it at the run's end.
                    upstream.queue_ack(Path(os.environ["TTP_RUN_DIR"]), fp, evidence)
                    print(f"ack of note {fp.strip().lower()} queued in this run's directory ({e.strerror or e}); this "
                          f"project's daemon records it when the run ends")
                    return
                print(f"note {fp.strip().lower()} closed for {p.name}")
                return
            for line in upstream.listing(p.name) or [f"{p.name} filed no upstream note on this machine"]:
                print(line)
            return
        except ValueError as e:
            die(str(e), 2)
    if a.receive:
        ack, rc = upstream.receive(sys.stdin.buffer.read(upstream.RECEIVE_BYTES + 1), a.via or "")
        print(json.dumps(ack, sort_keys=True))
        raise SystemExit(rc)
    if a.forward_now:
        # The fallback for a daemon that cannot reach a target (upstream.maybe_fallback): this context's
        # ssh, the same lock and cursor, only the targets failing past upstream.FALLBACK_AFTER_S.
        print(f"{upstream.forward(context='run')} upstream note(s) sent on")
        return
    if a.forward_to is not None:
        v = a.forward_to.strip()
        try:
            upstream.set_forward_to(None if v == "default" else [] if v == "none"
                                    else [x for x in v.replace(",", " ").split() if x])
        except ValueError as e:
            die(str(e))
        got = upstream.forward_to()
        print("upstream notes go to " + ("the machines of the remote projects" if got is None
                                         else ", ".join(got) if got else "no other machine"))
        return
    for line in ([] if a.forward_status else upstream.inbox_lines()) + upstream.forward_status():
        print(line)


def _answer_sender(rec: dict, me) -> None:
    """After `ttp upstream --resolve`: print who sent the note and, when that is another project on this
    machine, send it the answer once through the note path (a repeat is a duplicate). Never fails the
    resolve: a send that cannot be made says why."""
    from . import upstream
    n, to, line = upstream.answer_target(rec["fp"], me.name if me else "")
    if not to:
        print(line)
        return
    text, task = upstream.answer_text(n, rec), os.environ.get("TTP_TASK") or ""
    source, task = (me.name if me else upstream.MAINTAINERS), (int(task) if task.isdigit() else None)
    try:
        got = (upstream.send_or_queue(me.state, source, task, to, text)[0] if me
               else upstream.send(source, task, to, text))
    except OSError as e:
        if not os.environ.get("TTP_RUN_DIR"):
            print(f"{line}; answer not sent: {e.strerror or e}")
            return
        upstream.queue(Path(os.environ["TTP_RUN_DIR"]), to, text)
        got = "queued in this run's directory"
    words = {"sent": "answer sent", "duplicate": "answer already sent",
             "queued": "answer queued in this project's outbox (the hourly limit)",
             "limited": "answer not sent: this machine's hourly note limit", "full": "answer not sent: the outbox is full"}
    print(f"{line}; {words.get(got, 'answer ' + got)} to {to} as {upstream.note_id(to, text)}")

def cmd_push(a) -> None:
    """Publish this worktree's commits onto the project's target branch, guarded: refuse a dirty
    tree, rebase onto the latest tip, run `delivery.push_checks` on the final head, start over if
    the tip moved meanwhile, and push without force. The target is `delivery.push_branch`, never
    main, master or the remote's default branch. Pushes to one branch take turns; one that waits
    longer than `delivery.push_wait_s` (unset: twice the last check run, 900 s to 2 h) for its
    turn exits 75. With `delivery.version_bump` it bumps the version after each rebase. `--free` only tells whether it is
    free (0) or taken (1). `--detach` runs the push in a process of its own and prints its marker and
    a `--result <marker>` probe (0 once finished or dead, 1 while running). `--own` publishes the
    task's own `ttp/t<id>-...` branch under its own name instead, as it is: checks run on HEAD, no
    rebase or bump, never the push branch or a shared one; with no push branch set, that is also the
    default once the branch is on the remote. `--own-branch` publishes the task's own branch even when
    it carries a pr_branch. Inside a run, a plain push of the task's own branch onto the push branch is
    refused unless its spec asks it to land or `--land` says so. Inside a run, a worktree on another task's branch is
    refused (push.resolve), and so is publishing onto another task's branch the task only inherited. `--queue` lists the push queue
    (`delivery.push_queue`). Exit codes are in `push.py`."""
    from . import push
    if a.result:
        sys.exit(push.result(Path(a.result), pushed_only=a.pushed))
    if a.own_branch:   # read by push.own_target, also in the detached process (inherited, or QUEUE_ENV)
        a.own = True
        os.environ[push.OWN_BRANCH_ENV] = "1"
    if a.land:   # read by push._resolve, also in the detached process (inherited, or QUEUE_ENV)
        if a.own:
            die("ttp push: --land lands on delivery.push_branch; --own publishes the own branch: pick one")
        os.environ[push.LAND_ENV] = "1"
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else next((c for d in [Path.cwd(), *Path.cwd().parents]
                                         if (c := Project(d)).exists()), None)
    if not p or not p.exists():
        die("ttp push: no tt-project project here (run it inside a run or a project's worktree)")
    if a.queue:
        from . import pushq
        print(pushq.queue_text(p))
        return
    if a.free:
        sys.exit(push.free(p, Path.cwd()))
    from . import worktree
    worktree.prepare(p, Path.cwd())   # a worktree a reviewer made itself gets the .venv its checks may run
    if a.marker:
        sys.exit(push.run_detached(p, Path.cwd(), Path(a.marker), a.own))
    if a.batch:
        from . import batch
        sys.exit(batch.run_batch(p, Path(a.batch)))
    sys.exit(push.detach(p, Path.cwd(), a.own) if a.detach else push.run(p, Path.cwd(), a.own))


CHECK_PASSES = "check_passes.json"   # under the project's state: {"passes": [{"tree", "commands", "prefixes", "run", "ts"}]}
CHECK_PASSES_MAX = 200                # the newest entries kept


_PLAIN_WORD = re.compile(r"[\w@%+=:,./ -]+")


def _command_key(c) -> str:
    """How a recorded pass names a check: a command line made only of plain words is compared as its
    words, so `pytest -q 'a.py'`, `pytest  -q a.py` and the argv `pytest -q a.py` are one command. Any
    shell syntax (quotes around `$`, globs, `;`, pipes, ...) can change what runs, so such a line, or a
    structured check, is compared as written."""
    if not isinstance(c, str):
        return str(c)
    from . import push
    if host := push.remote_host(c):   # another machine's pass is not this one's, nor one with other env
        return json.dumps([str(c), host, dict(getattr(c, "env", ()) or ())])
    if getattr(c, "env", ()):
        return json.dumps([str(c), dict(c.env)])
    if getattr(c, "cwd", ""):   # a pass in the authoring worktree is not a clean checkout's
        return json.dumps([str(c), {"cwd": c.cwd}])
    try:
        words = shlex.split(c)
    except ValueError:
        return c
    if words and all(_PLAIN_WORD.fullmatch(w) for w in words):
        return shlex.join(words)
    return c


def _same_run_key(c, py: str = "") -> str:
    """_command_key, with a pytest run's program made one word where push.venv_cmd runs them alike:
    bare `pytest`, `py.test` and `python[3] -m pytest`, and `<py> -m pytest` for `py` the venv python
    venv_cmd puts in their place (venv_python). Any other interpreter (`/usr/bin/python3 -m pytest`,
    `.venv/bin/pytest`) runs as written, so stays distinct. Only for telling an extra command already run."""
    key = _command_key(c)
    if not isinstance(c, str):
        return key
    try:
        words = shlex.split(key)
    except ValueError:
        return key
    if not words or not all(_PLAIN_WORD.fullmatch(w) for w in words):
        return key   # shell syntax: compared as written
    if words[0] in ("pytest", "py.test"):
        return shlex.join(["pytest", *words[1:]])
    if len(words) > 2 and (words[0] in ("python", "python3") or (py and words[0] == py)) \
            and words[1:3] == ["-m", "pytest"]:
        return shlex.join(["pytest", *words[3:]])
    return key


def _commands_hash(cmds: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([_command_key(c) for c in cmds]).encode()).hexdigest()


def _recorded_pass(p: Project | None, tree: str, cmds: list, purpose: str = "candidate",
                   envs: list[str] | None = None) -> dict | None:
    """The recorded pass of exactly these commands, in this order, on this tree, for this purpose, or
    None. A pass of more commands that start with these is one of these too: they ran first, in order,
    and passed (the daemon's pre-started checks run the project's checks alone; a worker ran them with
    its own after `--`). A pass for the other purpose never counts (a record without one is candidate).
    With `envs` (pushpass.check_envs of the commands), only a pass recorded under the same
    environment for each of them counts; one recorded without it never does."""
    if not p or not tree:
        return None
    try:
        passes = json.loads((p.state / CHECK_PASSES).read_text()).get("passes")
    except (OSError, ValueError, AttributeError):
        return None
    key = _commands_hash(cmds)
    for e in reversed(passes if isinstance(passes, list) else []):
        if isinstance(e, dict) and e.get("tree") == tree and e.get("purpose", "candidate") == purpose and (
                e.get("commands") == key or key in (e.get("prefixes") or ())) and (
                envs is None or (isinstance(e.get("envs"), list) and e["envs"][:len(envs)] == list(envs))):
            return e
    return None


def _env_miss(p: Project | None, tree: str, cmds: list, parts: list[dict], purpose: str = "candidate") -> str:
    """When _recorded_pass found no pass of `cmds` on `tree` under their environment (`parts`:
    pushpass.check_env_parts) but one under another: which run recorded it and the parts of the
    environment it differs in; else ""."""
    if not p or not tree:
        return ""
    try:
        passes = json.loads((p.state / CHECK_PASSES).read_text()).get("passes")
    except (OSError, ValueError, AttributeError):
        return ""
    from . import pushpass
    key, new = _commands_hash(cmds), [pushpass.part_digests(x) for x in parts]
    for e in reversed(passes if isinstance(passes, list) else []):
        if isinstance(e, dict) and e.get("tree") == tree and e.get("purpose", "candidate") == purpose and (
                e.get("commands") == key or key in (e.get("prefixes") or ())) and isinstance(e.get("env_parts"), list):
            why = sorted({part for old, now in zip(e["env_parts"], new)
                          for part in pushpass.differs(old, now).split(", ") if part})
            if why:
                return f"the pass `ttp checks` recorded in run {e.get('run') or '?'} on this tree ran in another " \
                       f"environment: {', '.join(why)} differ"
    return ""


def _check_envs(top: str | Path, cmds: list, run_env: dict) -> tuple[list[str] | None, list[dict] | None]:
    """(pushpass.check_envs, their pushpass.check_env_parts) of `cmds`, or (None, None) without `run_env`."""
    if not run_env:
        return None, None
    from . import pushpass
    parts = pushpass.check_env_parts(Path(top), cmds, run_env)
    return [pushpass.digest(x) for x in parts], parts


def _record_pass(p: Project | None, tree: str, cmds: list, purpose: str = "candidate", run_dir: str | Path = "",
                 envs: list[str] | None = None, parts: list[dict] | None = None) -> None:
    """Only passes are recorded, with `envs` (pushpass.check_envs) when given, and the part_digests of
    their `parts` (pushpass.check_env_parts) to say what differs on a miss. A failed write is said
    and never fails the checks."""
    if not p or not tree:
        return
    import fcntl
    from . import pushpass
    path = p.state / CHECK_PASSES
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)   # parallel runs of one project share the file
            try:
                passes = json.loads(path.read_text()).get("passes")
            except (OSError, ValueError, AttributeError):
                passes = None
            passes = [e for e in passes if isinstance(e, dict)] if isinstance(passes, list) else []
            passes.append({"tree": tree, "commands": _commands_hash(cmds),
                           "prefixes": [_commands_hash(cmds[:n]) for n in range(1, len(cmds))],
                           "run": os.environ.get("TTP_RUN_ID") or "?", "ts": time.time(),
                           **({"log": str(Path(run_dir) / "checks.log")} if run_dir else {}),
                           **({"envs": list(envs)} if envs is not None and len(envs) == len(cmds) else {}),
                           **({"env_parts": [pushpass.part_digests(x) for x in parts]}
                              if parts is not None and len(parts) == len(cmds) else {}),
                           **({"purpose": purpose} if purpose != "candidate" else {})})
            write_json(path, {"passes": passes[-CHECK_PASSES_MAX:]})
    except OSError as e:
        print(f"ttp checks: could not record the pass in {path}: {e}", file=sys.stderr)


CHECKS_ENV = "checks_env.json"   # under the project's state: the last check that failed at import, for doctor


def _note_checks_env(p: Project | None, cmd: str | None, problem: str | None, head: str,
                     source: str = "delivery.push_checks", also: bool = False) -> None:
    """Keep the last import-time failure of a check for `ttp doctor`; any other outcome clears it.
    `source` is where the command came from (delivery.push_checks, or `ttp checks --`); `also`, that
    tests failed in the same run as well, so the head's code is not cleared."""
    if not p:
        return
    path = p.state / CHECKS_ENV
    try:
        if problem:
            write_json(path, {"command": cmd, "problem": problem, "head": head, "source": source,
                              **({"also": True} if also else {}),
                              "run": os.environ.get("TTP_RUN_ID") or "?", "ts": time.time()})
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def checks_env_line(p: Project) -> str | None:
    """`ttp doctor`'s line for a check that last failed at import (CHECKS_ENV), or None."""
    try:
        e = json.loads((p.state / CHECKS_ENV).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(e, dict) or not e.get("problem"):
        return None
    when = timefmt.long(float(e.get("ts") or 0), p)
    what = ("tests failed, and also an environment problem" if e.get("also")
            else "an environment problem, not the code")
    return (f"{e.get('source') or 'delivery.push_checks'}: {e.get('command')!r} failed at import in run "
            f"{e.get('run') or '?'} at {when} ({what}): {e['problem']}")


def _extra_checks(extra: list[str], own: list, py: str = "") -> tuple[list[str], list[str]]:
    """(the commands given after `--`, the ones dropped). One word is a shell command line as
    written (`ttp checks -- 'FOO=1 pytest -q'`). Several words that each hold whitespace are one
    command each (`-- 'pytest -q tests/a.py' 'ruff check .'`); any other several words are argv,
    quoted so each stays one word (`-- pytest -k 'a and b'`). A command the same as one of the
    project's checks that runs on every head (no `if_exists`/`if_changed`), or as an earlier one
    here, is dropped: it would run twice (a pytest run in any spelling the venv `py` runs alike,
    _same_run_key). One that
    only a scoped check matches stays, as that check may be skipped on this head (_ran_again drops
    it once that check applies)."""
    if not extra:
        return [], []
    if len(extra) == 1 or all(any(ch.isspace() for ch in w) for w in extra):
        given = [w for w in extra if w.strip()]
    else:
        given = [shlex.join(extra)]
    seen = {_same_run_key(c, py) for c in own if not (getattr(c, "if_exists", "") or getattr(c, "if_changed", ()))}
    mine, dropped = [], []
    for c in given:
        key = _same_run_key(c, py)
        (dropped if key in seen else mine).append(c)
        seen.add(key)
    return mine, dropped


def _ran_again(todo: list, own: list, py: str = "") -> tuple[list, list]:
    """(the applicable checks to run, the extras dropped): an extra command that a scoped project
    check applicable on this head (in `todo`) also runs is dropped, so it runs once."""
    keys = {_same_run_key(c, py) for c in todo if any(c is o for o in own)}
    keep, dropped = [], []
    for c in todo:
        (dropped if not any(c is o for o in own) and _same_run_key(c, py) in keys else keep).append(c)
    return keep, dropped


def _warn_broken_venvs(p: Project | None, top: str, say) -> None:
    """Warn about each venv the checks may use whose python does not run, with the command that
    makes it again (push.broken_venvs): its checks would otherwise fail at import, unexplained."""
    from . import push, worktree
    also = [p.root / n for n in worktree.VENV_NAMES] if p else []
    if os.environ.get("VIRTUAL_ENV"):
        also.append(Path(os.environ["VIRTUAL_ENV"]))
    for venv, why in push.broken_venvs(top, also):
        say(f"warning: the venv {venv} is broken ({why}), so checks run without its packages; "
            f"make it again with: {push.venv_rebuild_cmd(venv)}")


def _prestage_skip(a, p: Project | None, top: str, head: str, tree: str, cmds: list, own: list, vpy: str,
                   purpose: str) -> str:
    """Why `ttp checks --detach` skips delivery.check_prestage on this head, or "": the detached checks
    would only reuse a recorded pass, run none of them, or take the fast path past the project's
    checks (as the foreground path decides; `--fresh` and `--full` turn those off)."""
    from . import push
    if a.fresh:
        return ""
    base = _checks_base(p, Path(top), cmds)
    try:
        todo, _ = push.applicable(Path(top), head, cmds, lambda m: None, base, candidate=purpose == "candidate")
    except push.ScopeError:
        return ""
    todo, _ = _ran_again(todo, own, vpy)
    if not todo:
        return "no check applies on this head"
    if _recorded_pass(p, tree, todo, purpose):
        return "these checks already passed on this tree and the detached run reuses that pass; `--fresh` runs it"
    fcfg = p.config() if p else None
    if not a.full and not any(getattr(c, "if_changed", ()) for c in todo) \
            and push.fast_path(Path(top), base, head, push.fast_globs(fcfg), push.fast_exclude(fcfg)):
        return "the fast path skips the project's checks on this docs/data-only diff; `--full` runs it"
    return ""


def cmd_checks(a) -> None:
    """(inside a run) Run the local checks on this worktree's commit and record the result in the
    run's directory: the harness's gh opens a PR (even a draft) only after they passed on HEAD. The
    checks are the project's `delivery.push_checks`, plus the commands given after `--`.

    A pass is also recorded in the project's state, keyed on HEAD's tree and the ordered list of
    the commands that apply there. When the same commands, or more that start with them, already
    passed on the same tree (a reviewer checking a worker's commit, a rerun after a rebase that changed
    nothing), that pass is reused and nothing runs again; `--fresh` always runs them. Failures are never recorded, so a
    failure is never served as a pass. `ttp push` and the push queue never read this record and
    run their own checks on the exact commit they push (the queue skips only a check it recorded
    passing itself on the same tree, pushpass): a forged record here can at most skip a local re-run
    or an own-branch push's checks, never let a change onto the push branch. A pass also records the
    environment of each command (pushpass.check_envs); `ttp push --own` runs nothing again that
    passed here on exactly HEAD's tree with the same commands and environment. Where every project check is skipped on a commit,
    `ttp push --own` takes the commands given after `--` that this run recorded passing on exactly that
    commit and runs them itself before it pushes.

    Once they passed, the tests the head adds compared with the push target (addedtests) run again,
    ROUNDS times each, through the first check that is one plain local pytest run: one that fails a
    round fails the checks, named with its pass count; past a cap the rerun is reported skipped,
    never passed. With no added tests nothing more runs.

    `--detach` runs them in a session of their own that writes checks.rc in the run's directory
    however it ends, and prints a `--result` probe for `retry_when` that also answers once they were
    killed outright. Starting it again stops the run's earlier detached checks for the same worktree
    and purpose; while ones for another worktree or purpose still run, it refuses (exit 75) and
    prints their `retry_when`, as does a foreground run that would hand over to them.

    `--purpose harness` qualifies a harness-preparation head: it runs the harness-tagged and untagged
    project checks plus the commands after `--`, lists candidate-tagged ones as not run, and fails
    unless a harness-tagged check applies to the head and ran or reused a recorded pass. With no
    harness-tagged check configured it runs the default harness plan (push.default_harness_checks). Its pass is recorded as such and never counts for a PR,
    an on_pass hand-off or `ttp push --own`; its failures open no check gap."""
    from . import addedtests, checkcap, checkdirs, checkprestage, cleantree, prguard, push, pushpass, slowtests
    if getattr(a, "gap", None):
        from . import checkgaps
        p = here()
        if not p:
            die("no project found for the check gap probe")
        sys.exit(0 if checkgaps.ready(p, a.gap, getattr(a, "gap_config", None)) else 1)
    if a.result:
        sys.exit(_checks_result(Path(a.result)))
    if getattr(a, "slot_status", False) or a.cmd in (["status"], ["status", "--json"]):
        sys.exit(_checks_lane_status(bool(getattr(a, "json", False)) or a.cmd == ["status", "--json"]))
    if getattr(a, "env_ok", None) is not None:
        sys.exit(_checks_env_ok(here(), a.env_ok))
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp checks only works inside a tt-project run")
    if a.rc:
        sys.exit(_checks_child(a))
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else None
    cfg = p.config() if p and p.exists() else {}
    if not (p and p.exists()):
        p = None
    from . import hostcycle
    if cyc := hostcycle.hold(p):   # a planned cycle of this host would cut them short
        print(f"ttp checks: not started, {hostcycle.hold_line(cyc, p)}. Commit and hand off `waiting` with a "
              "`retry_when` that exits 0 once this host is back, and run them after the cycle", file=sys.stderr)
        sys.exit(75)
    purpose = getattr(a, "purpose", None) or "candidate"
    d = cfg.get("delivery") or {}
    checks_host = d.get("checks_host")   # where checks without a host run
    # The checks run at the top of the worktree being checked, as `ttp push` runs them, wherever in
    # it this was started: a repo-relative check started in a subdirectory would test the wrong place.
    top = subprocess.run(["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    git = ["git", "-C", top or str(Path.cwd())]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if not top or not head:
        die("ttp checks: run it in the change's git worktree")
    # The repo being checked picks its list (push.repo_checks): the harness repo never runs the code repo's.
    configured, source = push.repo_checks(d, Path(top), p)
    own = push.with_host(push.check_list(configured), checks_host)
    harness_unset = source == push.HARNESS_UNSET
    if harness_unset:
        print(f"ttp checks: {push.HARNESS_UNSET} (delivery.{push.BY_REPO}.{push.HARNESS_KEY}), so the code "
              "repo's delivery.push_checks do not run here, only their purpose harness entries"
              + ("" if purpose == "harness" else "; running the built-in harness default (a whitespace check, "
                 "and pytest of " + " and ".join(push.HARNESS_TEST_DIRS) + " where they exist)")
              + ". Its pass is this repo's own, never a pass of the code checks")
        source = "delivery.push_checks (purpose harness entries)" if purpose == "harness" \
            else "the built-in harness default"
    not_run: list[str] = []
    default_harness = False
    if purpose == "harness":
        if bad := push.check_problems(d.get("push_checks") if harness_unset else configured):
            die(f"ttp checks --purpose harness: {source} is malformed, so nothing is qualified: "
                + "; ".join(bad), 1)
        default_harness = not any(c.purpose == "harness" for c in own)
        not_run = [f"not run: candidate publication gate: {c}" for c in own if c.purpose == "candidate"]
        own = [c for c in own if c.purpose != "candidate"]
    elif harness_unset:
        own = push.with_host(push.default_harness_checks(Path(top), _checks_base(p, Path(top), own), ""),
                             checks_host)
    defaults = push.default_harness_checks(Path(top), _checks_base(p, Path(top), own)) if default_harness else []
    if defaults:   # no configured harness plan: the built-in one qualifies the head
        own += defaults
        print(f"ttp checks: {source} has no purpose harness check; running the default harness plan "
              "(a whitespace check, and pytest of " + " and ".join(push.HARNESS_TEST_DIRS) + " where they exist)")
    if p:
        from . import worktree
        worktree.prepare(p, top)   # a worktree a reviewer made itself gets the .venv its checks may run
    # Every check, extras too, runs in the project's environment: its venv active (VIRTUAL_ENV, PATH)
    # when this shell has none, as a run has (worktree.project_venv).
    venv_vars = {}
    if p and not os.environ.get("VIRTUAL_ENV") and (pv := worktree.project_venv(p, top)):
        venv_vars = worktree.venv_env(pv, os.environ.get("PATH", ""))
    # The python a bare pytest runs with (push.venv_cmd): an extra spelled with it runs the same tests.
    vpy = str(push.venv_python(top, {**os.environ, **venv_vars}) or "")
    mine, dropped = _extra_checks(a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd, own, vpy)
    mine = push.with_host(mine, checks_host)
    wrapped, wrap = set(), ()
    if mine:   # checks.wrap_cmd: the project's own lock or queue wrapper (never the project checks)
        try:
            wrap = push.wrap_of(cfg)
        except ValueError as e:
            die(f"ttp checks: {e}; the commands after `--` were not run", 1)
        mine = push.with_wrap(mine, wrap)
        wrapped = {str(c) for c in mine if getattr(c, "wrap", None)}   # their run takes the lock's lane
        if wrap and any(not push.remote_host(c) for c in mine):
            if why := push.wrap_problem(wrap, top, {**os.environ, **venv_vars}):
                die(f"ttp checks: {why}", 1)
            print(f"ttp checks: the commands after `--` run behind {push.WRAP_KEY}: {shlex.join(wrap)}")
        if wrap and (remote := [c for c in mine if push.remote_host(c)]):
            print(f"ttp checks: {', '.join(map(repr, map(str, remote)))} run on their check host without "
                  f"{push.WRAP_KEY}, which wraps only commands run on this machine")
    if dropped:
        print(f"ttp checks: not running {', '.join(map(repr, dropped))} after `--` again: "
              f"{source} already runs it")
    cmds = own + mine
    if not cmds:
        die(f"ttp checks: the project sets no {source} for this repo; give the repository's test commands after "
            "`--`, e.g. ttp checks -- pytest -q")
    own_harness = bool(p) and Path(top).resolve() == p.harness.resolve()
    dirty, runtime_written, state = _checks_dirty(git, purpose == "harness", own_harness)
    if dirty:
        die("ttp checks: commit first; the checks are recorded for a commit, and this worktree has changes")
    if runtime_written:
        print("ttp checks: not counting runtime-written changes to tracked files the repository ignores: "
              + ", ".join(runtime_written))
    if state:
        print("ttp checks: not counting uncommitted harness state files the runtime or other tasks write: "
              + ", ".join(state))
    if runtime_written or state or (untracked := _untracked(git)):
        what = "uncommitted changes" if runtime_written or state else f"{untracked} untracked file(s)"
        print(f"ttp checks: {what} in this worktree are not checked: the checks run on a clean checkout of "
              f"commit {head[:12]}")
    tree = subprocess.run([*git, "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    if a.detach:
        try:
            checkdirs.writable(run_dir)
            # The project's short pre-stage runs first, in the foreground (checkprestage): its failure
            # starts nothing, so it is fixed in this run, not after the whole detached suite. Unset, it is
            # derived from the project's frequent detached checks failures (checkprestage.derived).
            say = lambda m: print(f"ttp checks: {m}", flush=True)
            pre = d.get(checkprestage.KEY)
            if purpose == "candidate" and not harness_unset and not getattr(a, "no_prestage", False) \
                    and not _checks_owner(Path(run_dir), top, purpose):
                ids = checkprestage.derived(p, own, say) if pre is None else None
                if pre is not None or ids:
                    pre_env = {**push.check_env("checks"), "PWD": top, **venv_vars}
                    if why := _prestage_skip(a, p, top, head, tree, cmds, own, vpy, purpose):
                        print(f"ttp checks: pre-stage (delivery.{checkprestage.KEY}) skipped: {why}", flush=True)
                    elif checkprestage.run(pre, top, pre_env, say) if pre is not None else \
                            checkprestage.run_derived(p, ids, own, top, pre_env, say, run_dir):
                        sys.exit(1)
            _checks_detach(a, Path(run_dir), p, top, head)
        except OSError as e:
            die(f"ttp checks: {e}")
        return
    try:
        checkdirs.writable(getattr(a, "check_run_dir", None) or run_dir)
    except OSError as e:
        die(f"ttp checks: {e}")
    log = Path(run_dir) / "checks.log"
    passed, failed, failure, rc, scope_error = True, None, None, None, None
    clean_note = ""   # why a check found no clean checkout, or what only this worktree has
    fast_skipped, fast_cmds = [], []
    executed, selected, rerun = [], [], None
    timed_out, deadline, ran_since = None, None, 0.0   # the wall-time cap (checkwall) and its outcome
    spilled = None   # the spill host's claim when the checks run there (checkspill)
    run_env: dict = {}   # what the checks run in, for the pass's envs (pushpass.check_envs)
    base = _checks_base(p, Path(top), cmds)
    (Path(run_dir) / "check_failure.json").unlink(missing_ok=True)
    (Path(run_dir) / slowtests.FILE).unlink(missing_ok=True)   # this run's slowest tests only
    start = log.stat().st_size if log.exists() else 0
    with open(log, "a") as out:
        out.write(f"ttp checks: in {top} on {head[:12]}" + (" (purpose harness)" if purpose == "harness" else "")
                  + "\n")
        for line in not_run:
            print(f"ttp checks: {line}")
            out.write(f"{line}\n")
        _warn_broken_venvs(p, top, lambda m: (print(f"ttp checks: {m}"), out.write(f"{m}\n")))
        try:
            todo, skipped = push.applicable(Path(top), head, cmds, lambda m: (print(f"ttp checks: {m}"),
                                                                               out.write(f"{m}\n")),
                                            base, candidate=purpose == "candidate")
            todo, again = _ran_again(todo, own, vpy)
            if again:
                said = (f"not running {', '.join(map(repr, again))} after `--` again: a scoped "
                        f"{source} entry runs it on this head")
                print(f"ttp checks: {said}")
                out.write(f"{said}\n")
            fcfg = p.config() if p else None
            if todo and purpose == "candidate" and not a.full and not a.fresh \
                    and not any(getattr(c, "if_changed", ()) for c in todo) \
                    and not _recorded_pass(p, tree, todo, purpose) \
                    and push.fast_path(Path(top), base, head, push.fast_globs(fcfg), push.fast_exclude(fcfg)):
                own_s = {str(c) for c in own}
                fast_skipped = [str(c) for c in todo if str(c) in own_s]
                fast_cmds = push.fast_checks(base)
                todo = [c for c in todo if str(c) not in own_s] + fast_cmds
                said = ("FAST PATH: the diff changes only docs/data paths, so the project's checks were NOT run; "
                        "only the whitespace and CRLF checks run; `ttp checks --full` runs them all (delivery.checks_fast_globs sets the paths, "
                        "delivery.checks_fast_exclude the paths that never take it)"
                        + (f"; commands after `--` still run" if len(todo) > len(fast_cmds) else ""))
                print(f"ttp checks: {said}")
                out.write(f"{said}\n")
            selected = list(todo)
        except push.ScopeError as e:
            out.write(f"{e}\n")
            todo, skipped, passed, failed, scope_error = [], [], False, str(e.check), e.check
            selected = list(cmds)   # no check was even judged, so none of them ran
        # A harness run qualifies the head only through a harness check: one skipped by if_exists or
        # if_changed leaves the untagged checks and extras (or nothing) to pass, which is no qualification.
        unqualified = (purpose == "harness" and passed
                       and not any(getattr(c, "purpose", "") == "harness" for c in todo))
        if unqualified:
            failed = HARNESS_SKIPPED
            out.write(f"{failed}\n")
            passed, todo = False, []
        # A continuation's if_changed checks that passed at its lineage's marker, with nothing under their
        # scope changed since, are verified through it (verified.py); `ttp push` never reads the marker.
        # A recorded pass of every check on this tree comes first: the marker only covers what it lacks.
        hit = _recorded_pass(p, tree, todo, purpose) if todo and not a.fresh else None
        mark, verified_cmds = _verified_scope(p, Path(top), head, todo, lambda m: (print(f"ttp checks: {m}"),
                                                                                    out.write(f"{m}\n")),
                                              a.fresh) \
            if passed and todo and purpose == "candidate" and not hit else (None, [])
        todo = [c for c in todo if c not in verified_cmds]
        if verified_cmds and todo and not a.fresh:
            hit = _recorded_pass(p, tree, todo, purpose)
        if not todo and passed and verified_cmds:
            said = f"every applicable check was {mark['line']}"
            out.write(f"{said}\n")
            print(f"ttp checks: {said}")
        elif not todo and passed and push.outside_scope(cmds, skipped):
            out.write(f"{push.OUT_OF_SCOPE}\n")
            print(f"ttp checks: {push.OUT_OF_SCOPE}")
        elif not todo and passed:
            out.write(f"{push.NONE_APPLY}\n")
            passed, failed = False, push.NONE_APPLY
        elif hit:
            when = timefmt.long(float(hit.get("ts") or 0), p)
            rl = str(hit.get("log") or "")
            rec_out = (f"full output: {rl}" if rl and Path(rl).is_file() else
                       "no commands ran in this call and the recorded run's output is not available; "
                       "--fresh produces it")
            said = (f"ttp checks: {len(todo)} check(s) passed on {tree[:12]} in run {hit.get('run') or '?'} "
                    f"at {when} (recorded); --fresh runs them again ({rec_out})")
            out.write(f"{said}\n")
        # A stale coverage digest fails a harness check-plan at once: found here, it fails before the wait
        # for a checks slot, not after it (the check needs no slot to fail).
        from . import checkplan
        for c in todo if purpose == "harness" and not hit else []:
            if why := checkplan.stale(Path(top), str(c)):
                said = f"not queued for a checks slot: {c} would fail: {why}"
                print(f"ttp checks: {said}")
                out.write(f"{said}\n")
                passed, failed, todo = False, c, []
                break
        # A runner the host lacks (pytest wiped from the python a check runs with) is an environment
        # fault, not this head's: wait for it, counting nothing against the change (envfault).
        from . import envfault
        pre_env = {**push.check_env("checks", run_dir=getattr(a, "check_run_dir", None)), "PWD": top, **venv_vars}
        if todo and not hit and (fault := envfault.precheck(todo, top, pre_env)):
            envfault.record(p, fault, os.environ.get("TTP_TASK"))
            envfault.write_marker(run_dir, fault)
            said = f"{envfault.line(fault)}. Hand off `waiting`; retry_when: {envfault.retry_when(p, fault)}"
            out.write(f"ttp checks: {said}\n")
            write_json(Path(run_dir) / prguard.CHECKS_FILE, {"head": head, "worktree": top, "passed": False,
                                                             "commands": todo, "skipped": skipped, "ts": time.time(),
                                                             "purpose": purpose, "env_fault": fault["sig"]})
            print(f"ttp checks: {said}", file=sys.stderr)
            sys.exit(75)
        if p:
            envfault.recheck(p)   # a runner back: its open fault closes
        arrived = _checks_arrived()
        from . import checklane, checkspill
        # A rerun here of checks that did not pass on a spill host never spills again (checkspill).
        no_spill = bool(os.environ.pop(checkspill.OFF_ENV, ""))
        spill_ok = not no_spill and checkspill.spillable(todo)
        fresh_ok = not a.fresh
        slot = None
        while todo and not hit:
            slot = _checks_slot(p, Path(run_dir), out, head, bool(getattr(a, "own_group", False)), arrived,
                                _checks_key(p, tree, todo, purpose) if fresh_ok else "",
                                (lambda: bool(_recorded_pass(p, tree, todo, purpose))) if fresh_ok else None,
                                todo, checklane.all_light(todo, cfg, mine if getattr(a, "light", False) else ()),
                                str(top), checklane.wrap_key(wrap) if any(str(c) in wrapped for c in todo) else "",
                                spill_ok)
            # Checks of the same tree may have passed while these waited (the same checks running beside
            # them, a review's pre-started ones): that pass serves, and the slot goes to the next waiter.
            if slot is not None and fresh_ok and (again := _recorded_pass(p, tree, todo, purpose)):
                slot.close()
                hit, slot = again, None
                said = (f"ttp checks: {len(todo)} check(s) passed on {tree[:12]} in run {hit.get('run') or '?'} "
                        f"at {timefmt.long(float(hit.get('ts') or 0), p)} (recorded while these waited for a "
                        f"checks slot); --fresh runs them again (full output: {hit.get('log') or 'not kept'})")
                out.write(f"{said}\n")
            # A wait a recorded pass ended holds no slot: with that pass unreadable now, queue again
            # (same arrival time), never run the checks uncapped.
            if slot is None or not slot.served:
                break
        # Every host-wide slot stayed busy: a tool call must not block for the whole queue.
        handover = bool(todo) and not hit and slot is None
        # The slots here stayed busy and a spill host took the run: its checks run there (remotecheck).
        spilled = getattr(slot, "spill", None) if slot is not None and not hit else None
        if spilled:
            todo = push.with_host(todo, spilled.host)
        child_env = {"PWD": top, **venv_vars, **(slot.env() if slot else {})}
        run_env = {**push.check_env("checks", run_dir=getattr(a, "check_run_dir", None)), **child_env}
        for line in [] if hit or handover else push.sandbox_lines(todo):
            print(f"ttp checks: {line}", flush=True)
            out.write(f"ttp checks: {line}\n")
        # Local checks run in a clean checkout of `head` (cleantree), never in this worktree, whose
        # ignored, untracked or uncommitted files the recorded tree lacks; a remote one copies `head`
        # itself (remotecheck), one with "cwd": "authoring" runs here and is labelled so.
        clean = None
        reap_tok, reap_rec, stop_handlers = "", None, {}
        try:
            if not (hit or handover) and any(not _in_place(c) for c in todo):
                try:
                    clean = cleantree.Checkout(top, head)
                    if p:
                        worktree.prepare(p, clean.path)   # the .venv a check may run, as the push queue's
                    said = f"running in a clean checkout of {head[:12]}: files only on disk in {top} are not seen"
                    print(f"ttp checks: {said}", flush=True)
                    out.write(f"ttp checks: {said}\n")
                except (cleantree.Error, OSError, subprocess.SubprocessError) as e:
                    failed = next(c for c in todo if not _in_place(c))
                    out.write(f"$ {failed}\n")
                    out.flush()
                    check_start = out.tell()
                    clean_note = f"it did not run: no clean checkout of {head[:12]} to run it in: {e}"
                    out.write(f"ttp checks: {clean_note}\n")
                    passed, rc = False, 1
            if not (hit or handover) and passed and todo:
                # Each check carries this run's token, so what it leaves running ends with it (checkreap).
                from . import checkreap
                reap_tok, _REAPED["procs"] = checkreap.token(), 0
                if p:
                    if any(swept := checkreap.sweep(p.state)):
                        out.write(f"{checkreap.said(*swept)} of earlier check runs\n")
                    try:
                        reap_rec = checkreap.Record(p.state, reap_tok, str(clean.dir) if clean else "", top)
                    except OSError:
                        reap_rec = None
                if not getattr(a, "own_group", False):
                    stop_handlers = _foreground_stop()
                # The run's wall-time cap from its first command on (checkwall); the status line shows it.
                from . import checkwall, testslots
                wall, wall_how = checkwall.cap(p)
                ran_since = time.time()
                deadline = ran_since + wall if wall > 0 else None
                testslots.suite_running(getattr(slot, "suite", None), wall)
                if p and (last := checkwall.last_timeout(p.state, os.environ.get("TTP_TASK"))):
                    said_last = (f"this task's last checks run (run {last.get('run') or '?'} on "
                                 f"{str(last.get('head') or '')[:12]}) was stopped at the wall-time cap: a timeout, "
                                 f"not a pass")
                    print(f"ttp checks: {said_last}", flush=True)
                    out.write(f"ttp checks: {said_last}\n")
            for c in [] if hit or handover or not passed else todo:
                executed.append(c)
                cwd = top if _in_place(c) else str(clean.path)
                note = AUTHORING_NOTE if cleantree.authoring(c) and not push.remote_host(c) else ""
                # A progress line per check: the commands' own output goes only to checks.log.
                print(f"ttp checks: running {len(executed)}/{len(todo)}: {c}{push.where(c)}{note}", flush=True)
                out.write(f"$ {c}{push.where(c)}{note}\n")
                out.flush()
                check_start = out.tell()
                # A detached run gives each check a group of its own, so a stop ends it with ttp checks.
                group = bool(getattr(a, "own_group", False))
                env = {**push.check_env("checks", run_dir=getattr(a, "check_run_dir", None)), **child_env,
                       "PWD": cwd, **({checkreap.MARKER: reap_tok} if reap_tok else {})}
                if pushpass.resumable(str(c)) and not push.remote_host(c):   # one plain local pytest run
                    env |= slowtests.env(Path(run_dir) / slowtests.FILE, env)
                def said(line: str) -> None:   # a script it runs was rewritten as it ran: once more
                    nonlocal check_start
                    print(f"ttp checks: {line}", flush=True)
                    out.write(f"ttp checks: {line}\n")
                    out.flush()
                    check_start = out.tell()
                began = time.monotonic()
                try:
                    rc = scriptwatch.rerun(
                        lambda: _run_check(_low_priority(push.check_argv(c, cwd, env)), group, record=reap_rec, cwd=cwd,
                                           stdout=out,
                                           stderr=subprocess.STDOUT, env=env, timeout=checkwall.deadline_left(deadline),
                                           pass_fds=(a.tmp_fd,) if group and getattr(a, "tmp_fd", None) is not None else ()),
                        bool, None if push.remote_host(c) else str(c), cwd, env, said)
                except subprocess.TimeoutExpired:
                    # Past the wall-time cap: _run_check ended its group and what it left; never a pass.
                    passed, failed, rc = False, c, checkwall.RC
                    timed_out = {"check": str(c), "cap_s": wall, "how": wall_how, "head": head,
                                 "run": os.environ.get("TTP_RUN_ID") or "?"}
                    out.write(f"ttp checks: {checkwall.said(str(c), wall, wall_how, head)}\n")
                    out.write(_check_took(c, rc, began))
                    break
                if rc != 0:
                    passed, failed = False, c
                    if cwd != top:   # what only this worktree has may be what the check missed
                        out.flush()
                        with open(log, "rb") as f:
                            f.seek(check_start)
                            output = f.read().decode("utf-8", errors="replace")
                        if clean_note := cleantree.hint(cleantree.authoring_only(top, cwd, str(c), output)):
                            out.write(f"ttp checks: {clean_note}\n")
                    out.write(_check_took(c, rc, began))
                    break
                out.write(_check_took(c, rc, began))
            # The tests the diff adds, once more several times each: a timing-flaky one fails here.
            py_check = next((c for c in executed if not push.remote_host(c) and addedtests.eligible(str(c))), None)
            if passed and py_check is not None and (new_tests := addedtests.added(top, base, head)):
                cwd = top if _in_place(py_check) else str(clean.path)
                tmp_fd = bool(getattr(a, "own_group", False)) and getattr(a, "tmp_fd", None) is not None
                env = {**push.check_env("checks", run_dir=getattr(a, "check_run_dir", None)), **child_env,
                       "PWD": cwd, **({checkreap.MARKER: reap_tok} if reap_tok else {})}
                print(f"ttp checks: rerunning {len(new_tests)} added test(s) {addedtests.ROUNDS} times each "
                      f"with: {py_check}", flush=True)
                out.write(f"$ {py_check} (rerun of the added tests: {', '.join(map('::'.join, new_tests))})\n")
                out.flush()
                check_start = out.tell()
                wall_hit = []   # the rerun ended at the run's wall-time cap, not its own

                def rerun_check(e, t):
                    left = checkwall.deadline_left(deadline)
                    try:
                        return _run_check(_low_priority(push.check_argv(py_check, cwd, e)), True, record=reap_rec,
                                          cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=e,
                                          timeout=left if left is not None and (t is None or left < t) else t,
                                          pass_fds=(a.tmp_fd,) if tmp_fd else ())
                    except subprocess.TimeoutExpired:
                        if left is not None and (t is None or left < t):
                            wall_hit.append(1)
                        raise
                rerun = addedtests.run(new_tests, str(py_check), rerun_check, Path(run_dir) / addedtests.FILE, env)
                for line in addedtests.lines(rerun):
                    print(f"ttp checks: {line}", flush=True)
                    out.write(f"ttp checks: {line}\n")
                if rerun["status"] in ("flaky", "failed"):
                    passed, failed, rc = False, py_check, rerun.get("rc") or 1
                elif wall_hit:
                    passed, failed, rc = False, py_check, checkwall.RC
                    timed_out = {"check": str(py_check), "cap_s": wall, "how": wall_how, "head": head,
                                 "run": os.environ.get("TTP_RUN_ID") or "?"}
                    out.write(f"ttp checks: {checkwall.said(str(py_check), wall, wall_how, head)}\n")
        finally:
            if reap_tok:
                _checks_reaped(out, reap_tok, reap_rec, clean, rc)
            elif clean is not None:
                clean.close()
            for sig, h in stop_handlers.items():
                signal.signal(sig, h)
            if stop_handlers:
                _CHECK_STOP["stopped"] = None
        if ran_since and p:
            if timed_out and spilled:
                pass   # a spill host's wall time (a slower host, a stalled link) is no timeout of the task's: rerun here
            elif timed_out:
                checkwall.record_timeout(p.state, os.environ.get("TTP_TASK"), timed_out)
            elif executed:
                checkwall.record_run(p.state, time.time() - ran_since, passed,
                                     _commands_hash(todo) if passed and executed == todo else "")
                # A completed run no longer reports the task's old wall-time timeout (TIMEOUTS entry).
                checkwall.clear_timeout(p.state, os.environ.get("TTP_TASK"))
        if slot and not passed:
            slot.close()
        # A spill host's own environment never fails a change (a fault there, a missing tool): what did
        # not pass there (a timeout included) runs again here, in its place in the queue, and only that
        # verdict counts.
        if spilled and not passed and (executed or timed_out) and not _CHECK_STOP.get("stopped"):
            how = f"past the {timed_out['cap_s']:g} s wall-time cap" if timed_out else f"exit {rc}"
            said = (f"the checks did not pass on spill host {spilled.host} ({how}); running them again on "
                    "this machine, whose result counts")
            out.write(f"ttp checks: {said}\n")
            out.flush()
            print(f"ttp checks: {said}", flush=True)
            os.environ[checkcap.ARRIVED_ENV] = str(arrived)
            os.environ[checkspill.OFF_ENV] = "1"
            return cmd_checks(a)
        # A check host ssh could not reach (or that could not take the tree) is the environment, as a
        # missing runner is: wait for it, counting nothing against the change (envfault).
        if not passed and (fault := push.remote_fault(failed, rc, top)):
            envfault.record(p, fault, os.environ.get("TTP_TASK"))
            envfault.write_marker(run_dir, fault)
            said = f"{envfault.line(fault)}. Hand off `waiting`; retry_when: {envfault.retry_when(p, fault)}"
            out.write(f"ttp checks: {said}\n")
            write_json(Path(run_dir) / prguard.CHECKS_FILE, {"head": head, "worktree": top, "passed": False,
                                                             "commands": todo, "skipped": skipped, "ts": time.time(),
                                                             "purpose": purpose, "env_fault": fault["sig"],
                                                             "hosts": push.hosts_of(todo)})
            print(f"ttp checks: {said}", file=sys.stderr)
            sys.exit(75)
    if handover:
        try:
            _checks_detach(a, Path(run_dir), p, top, head, arrived, no_spill)
        except OSError as e:
            die(f"ttp checks: {e}")
        # Told only once they were started: a failed start says so, never that they run.
        with open(log, "a") as out:
            out.write(f"ttp checks: {CHECKS_HANDOVER}\n")
        print(f"ttp checks: {CHECKS_HANDOVER}", flush=True)
        sys.exit(75)
    env_problem, also, not_executed = None, False, []
    # The checks that ran (or whose recorded pass came) from this worktree, not a clean checkout.
    in_authoring = [str(c) for c in todo if cleantree.authoring(c) and not push.remote_host(c)]
    if not passed:
        # The applicable checks a failure or refusal left unrun stay listed as such, never as passed;
        # those verified through the lineage marker were checked, not left unrun.
        not_executed = [c for c in selected if c not in executed and c not in verified_cmds]
        with open(log, "rb") as f:   # this call's part of the log
            f.seek(start)
            text = f.read().decode("utf-8", errors="replace")
        env_problem = push.env_problem(text, top) if failed in todo else None
        also = bool(env_problem) and push.test_failures(text)
        ran, output = failed in todo and rc is not None, text
        if ran:
            with open(log, "rb") as f:
                f.seek(check_start)
                output = f.read().decode("utf-8", errors="replace")
        check = scope_error or (push.Check(failed) if ran and not isinstance(failed, push.Check) else
                                failed if ran else None)
        if ran and purpose == "candidate":   # a harness run opens no check gap
            failure = push.applicability_failure(Path(top), head, check, rc, output, base)
        # Every failed run leaves its receipt (a refusing selector or any failing check too), so a
        # missing one never reads as no failure; only the gap kinds above can open a check gap.
        kind = ("check_failed" if ran else "scope_error" if scope_error else "harness_skipped" if unqualified
                else "none_apply")
        write_json(Path(run_dir) / "check_failure.json", {
            **(failure or {"kind": kind, "sig": push.check_sig(check) if check else "",
                           "check": check.config() if check else None, "targets": [], "head": head,
                           "base": base, "worktree": top, "rc": rc if ran else None,
                           "output": output[-8000:], "unrelated": False}),
            "purpose": purpose, "not_executed": not_executed,
            **({"timed_out": timed_out} if timed_out else {}),
            **({"env_problem": env_problem} if env_problem else {}),
            **({"flaky": rerun.get("flaky") or []} if rerun and failed is py_check else {})})
    write_json(Path(run_dir) / prguard.CHECKS_FILE, {"head": head, "worktree": top, "passed": passed,
                                                     "commands": todo, "skipped": skipped, "ts": time.time(),
                                                     **({"verified": verified_cmds, "verified_through": mark["sha"]}
                                                        if verified_cmds else {}),
                                                     "purpose": purpose, "checks_from": source,
                                                     **({"fast_path": True, "skipped_project_checks": fast_skipped}
                                                        if fast_cmds else {}),
                                                     **({"not_run": not_run} if not_run else {}),
                                                     **({"not_executed": not_executed} if not passed else {}),
                                                     **({"timed_out": timed_out} if timed_out else {}),
                                                     **({"env_problem": env_problem} if env_problem else {}),
                                                     **({"hosts": hosts} if (hosts := push.hosts_of(todo)) else {}),
                                                     **({"spilled_to": spilled.host} if spilled else {}),
                                                     **({"authoring_tree": in_authoring} if in_authoring else {}),
                                                     **({"added_tests": rerun} if rerun else {})})
    source = ("the default harness plan" if any(str(c) == str(failed) for c in defaults)
              else source if any(str(c) == str(failed) for c in own) else "ttp checks --")
    _note_checks_env(p, str(failed) if env_problem else None, env_problem, head, source, also)
    if not passed:
        from . import trim
        if unqualified:
            die(f"ttp checks --purpose harness: {HARNESS_SKIPPED} on {head[:12]}, so nothing qualifies this "
                f"head; the purpose harness check must apply to it (full output: {log})", 1)
        print(trim.summary(text))   # a test run's failures, else head and tail
        if timed_out:
            die(f"ttp checks: {checkwall.said(str(failed), timed_out['cap_s'], timed_out['how'], head)} "
                f"(full output: {log})", 1)
        if also:
            die(f"ttp checks: {failed!r} failed on {head[:12]}: tests failed, and there is also an environment "
                f"problem: {env_problem} (full output: {log})", 1)
        if env_problem:
            die(f"ttp checks: {failed!r} failed at import on {head[:12]}: an environment problem, not the "
                f"head's code: {env_problem} (full output: {log})", 1)
        if rerun and failed is py_check:
            die(f"ttp checks: {addedtests.summary(rerun)} on {head[:12]}, rerun with {failed!r} "
                f"(full output: {log})", 1)
        die(f"ttp checks: {failed!r} failed on {head[:12]}" + (f"; {clean_note}" if clean_note else "")
            + f" (full output: {log})", 1)
    if fast_cmds:   # a reduced pass verifies nothing: no marker, and only the extras are recorded
        extras = [c for c in todo if str(c) not in set(map(str, fast_cmds))]
        if extras:
            _record_pass(p, tree, extras, purpose, run_dir, *_check_envs(top, extras, run_env))
        if slot:
            slot.close()
        print(f"ttp checks: FAST-PATH pass on {head[:12]} (whitespace and CRLF checks"
              + (f" and {len(extras)} command(s) after `--`" if extras else "")
              + f"); the project's checks did NOT run, so this is not a full pass and does not count for a PR "
              f"(full output: {log})")
        return
    if purpose == "candidate":
        _mark_verified(p, head, tree, list(todo) + list(verified_cmds))
    if hit:
        print(said)
        return
    if todo:
        _record_pass(p, tree, todo, purpose, run_dir, *_check_envs(top, todo, run_env))
    # Released only once the pass is recorded: a waiter for the same checks finds it the moment it may run.
    if slot:
        slot.close()
    more = f", {len(skipped)} skipped as not applicable" if skipped else ""
    more += f", {len(not_run)} candidate gate(s) not run" if not_run else ""
    if in_authoring:
        more += (f"; {len(in_authoring)} ran in the authoring worktree, NOT a clean checkout (\"cwd\": "
                 f"\"authoring\"): {', '.join(map(repr, in_authoring))}")
    if rerun and rerun["status"] == "skipped":
        more += f"; the rerun of the added tests was SKIPPED ({rerun['why']}), not passed"
    if harness_unset:
        more += f"; {push.HARNESS_UNSET}: the built-in harness default ran, not the code repo's checks"
    print(f"ttp checks: {len(todo)} check(s) passed on {head[:12]}{more}; "
          + ("recorded as a harness qualification only: it never counts for a PR, an on_pass hand-off or a push"
             if purpose == "harness" else "recorded for the draft PR") + f" (full output: {log})")


HARNESS_SKIPPED = "every purpose harness check was skipped as not applicable"
AUTHORING_NOTE = " (in the authoring worktree, not a clean checkout)"


def _in_place(c) -> bool:
    """Whether `ttp checks` runs check `c` in the worktree itself: it opted out of the clean checkout
    ("cwd": "authoring"), or it runs on a check host, which gets a copy of the commit (remotecheck)."""
    from . import cleantree, push
    return cleantree.authoring(c) or bool(push.remote_host(c))


def _untracked(git: list[str]) -> int:
    """How many untracked files (not ignored) the worktree has: they are not checked."""
    r = subprocess.run([*git, "status", "--porcelain=v1", "-z", "--untracked-files=normal"], capture_output=True,
                       text=True)
    return sum(1 for e in r.stdout.split("\0") if e.startswith("?? "))


def _run_task(p: Project | None) -> dict | None:
    task = os.environ.get("TTP_TASK") or ""
    try:
        return p.db.task(int(task)) if p and task.isdigit() else None
    except Exception:
        return None


def _verified_scope(p: Project | None, top: Path, head: str, todo: list, say,
                    fresh: bool = False) -> tuple[dict | None, list]:
    """(the run's task's inherited checks marker, the if_changed checks in `todo` it verifies): each passed
    at the marker, and the commits after it change nothing under its scope. A marker at `head` itself (a
    rerun on the same head) verifies the scoped checks it recorded. `fresh` ignores the marker, at
    `head` or an ancestor: every check runs again. A marker that is no
    longer an ancestor of `head` (rewritten history) verifies none: every check runs, and that is said.
    Checks without an if_changed scope always run, as they cover the whole tree."""
    from . import push, verified
    t = _run_task(p)
    try:
        m = verified.scope(p.db, t, top, head, "checks", include_self=True) if t else None
    except Exception:
        return None, []
    if not m or fresh:
        return m, []
    if not m["ok"]:
        say(m["line"])
        return m, []
    files = [] if m["sha"] == head else verified.changed_since(top, m["sha"], head)
    if files is None:
        say(f"cannot tell what changed since {m['sha'][:12]}: full verification")
        return m, []
    passed = {str(c) for c in m.get("commands") or []}
    mine = [c for c in todo if getattr(c, "if_changed", ()) and str(c) in passed
            and not any(push.matches(files, g) for g in c.if_changed)]
    for c in mine:
        say(f"Verified: {c} ({m['line']}: nothing under {', '.join(c.if_changed)} changed since)")
    if not mine:
        say(f"{m['line']}: every check runs, as each covers what changed since")
    return m, mine


def _mark_verified(p: Project | None, head: str, tree: str, cmds: list) -> None:
    """Record this run's task as verified through `head` (verified.py); a failure never fails the checks."""
    from . import verified
    t = _run_task(p)
    if not t:
        return
    try:
        verified.record(p.db, t["id"], "checks", head, tree=tree, commands=[str(c) for c in cmds],
                        run=os.environ.get("TTP_RUN_ID") or "?")
    except Exception as e:
        print(f"ttp checks: could not record the verified-through marker: {e}", file=sys.stderr)


# Files in the project's own harness repo the runtime and other tasks write while a harness task runs.
HARNESS_STATE_FILES = ("project.json", "schedules.json", "MEMORY.md", "settled_areas.json")


def _harness_state_path(path: str, own_harness: bool) -> bool:
    """Whether `path` is harness state no worker's change owns: an append-only `.jsonl` record or a
    file under a `state/` directory anywhere, and in the project's own harness repo its config,
    schedules, charter and memory, which the coordinator and daemon write and commit themselves."""
    parts = path.split("/")
    if path.endswith(".jsonl") or "state" in parts[:-1]:
        return True
    return own_harness and (path in HARNESS_STATE_FILES or parts[0] == "memory"
                            or (len(parts) == 1 and path.startswith("CHARTER")))


def _checks_dirty(git: list[str], harness: bool = False,
                  own_harness: bool = False) -> tuple[bool, list[str], list[str]]:
    """Whether the worktree has changes that block `ttp checks`, and the tracked files it tolerates:
    unstaged edits to tracked files the repository's ignore rules match (a log a running watcher keeps
    appending to), which no check is meant to read and which would otherwise force a fresh worktree
    for every run. With `harness` (`--purpose harness`), also unstaged edits to harness state
    (_harness_state_path), returned apart: a worker must not commit files another writer owns to
    qualify its head, and the harness check binds every input it reads to the commit anyway.
    A staged edit, or any other change, still blocks."""
    out = subprocess.run([*git, "status", "--porcelain=v1", "-z", "--untracked-files=no"], capture_output=True,
                         text=True)
    if out.returncode:
        return True, [], []
    ignored = set(subprocess.run([*git, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard"],
                                 capture_output=True, text=True).stdout.split("\0")) - {""}
    blocking, tolerated, state, items = False, [], [], iter(out.stdout.split("\0"))
    for e in items:
        if len(e) <= 3:
            continue
        if e[0] in "RC":
            next(items, None)   # a rename's or copy's source follows its new path
        if e[0] == " " and e[3:] in ignored:
            tolerated.append(e[3:])
        elif e[0] == " " and harness and _harness_state_path(e[3:], own_harness):
            state.append(e[3:])
        else:
            blocking = True
    return blocking, sorted(tolerated), sorted(state)


def _harness_purpose(a) -> dict:
    """What a detached or relaunched `ttp checks` keeps of `--purpose`: only harness, so a restart
    after a reboot qualifies the head for the same purpose."""
    return {"purpose": "harness"} if getattr(a, "purpose", None) == "harness" else {}


CHECKS_HANDOVER = ("every host-wide checks slot stayed busy, so they did not run in this call; started them "
                   "detached instead, in the same place in the queue (exit 75): hand off waiting with the "
                   "retry_when above")
CHECKS_SLOT_WAIT = "checks-slot.wait"   # in the run's directory while its checks wait for a host-wide slot
# The daemon's request that the run's checks stop waiting for a slot ({pid, started}): their task ended
# (Daemon.reap_waiting_checks). Only a waiter with that pid and start time honours it, and only while it waits.
CHECKS_SLOT_CANCEL = "checks-slot.cancel"
# (not "checks.queued": detach.queued(checks.rc) is the daemon's launch marker)


def _checks_arrived() -> float:
    """When these checks joined the host-wide queue: the time a foreground run handed over with
    (checkcap.ARRIVED_ENV, taken out of the environment so no check command inherits it), else now."""
    from . import checkcap
    now = time.time()
    try:
        t = float(os.environ.pop(checkcap.ARRIVED_ENV, ""))
    except ValueError:
        return now
    return t if math.isfinite(t) and 0 < t <= now else now


def _checks_key(p: Project | None, tree: str, cmds: list, purpose: str) -> str:
    """What makes two `ttp checks` the same run (checkcap.acquire's `same`): project, tree, commands
    and purpose, as _recorded_pass matches them; "" without a project or tree."""
    if not p or not tree:
        return ""
    return hashlib.sha256(f"{p.name}\0{tree}\0{_commands_hash(cmds)}\0{purpose}".encode()).hexdigest()[:24]


def _low_priority(argv: list) -> list:
    """A check command's argv at a lower CPU and IO priority (testslots.low_priority)."""
    from . import testslots
    return testslots.low_priority(argv)


def _checks_cancelled(run_dir: Path) -> str:
    """Why this process should stop waiting for a checks slot (CHECKS_SLOT_CANCEL names it), or ""."""
    from .runner import proc_start
    try:
        req = json.loads((run_dir / CHECKS_SLOT_CANCEL).read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(req, dict) or req.get("pid") != os.getpid() or req.get("started") != proc_start(os.getpid()):
        return ""
    return str(req.get("why") or "the task ended")


def _checks_lane_status(as_json: bool) -> int:
    """`ttp checks status` (also --slot-status): the project's checks lanes, their holders and waiters
    (checklane.lane_status). Read only: it never queues, takes or writes a lock, or starts a check."""
    from . import checklane, testslots
    p = here()
    if not p:
        die("ttp checks status: no project found")
    top = subprocess.run(["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    here_repo = testslots.repo_key(top) if top else ""
    from . import checkspill
    if as_json:
        print(json.dumps({"project": p.name, "slots": checklane.slots(p.config()),
                          "project_max": checklane.project_cap(p.config()), "this_repo": here_repo,
                          "repos": checklane.lane_status(p.name), "spilled": checkspill.holders(p.name)}, indent=2))
    else:
        print(checklane.status_text(p.name, p.config(), here_repo))
        for line in checkspill.status_lines(p.name):
            print(f"  {line}")
    return 0


def _checks_slot(p: Project | None, run_dir: Path, out, head: str, detached: bool = False,
                 arrived: float | None = None, same: str = "", done=None, cmds: list | None = None,
                 light: bool = False, top: str = "", lock: str = "", spill: bool = False):
    """Wait for a host-wide checks slot (checkcap). The wait is told in the log, the run's notes
    (progress.md) and CHECKS_SLOT_WAIT, which the `--result` probe and `ttp status` read. In the
    foreground it is recorded like a `ttp lock` wait, so it does not count toward the run's wall
    clock, and it ends after checkcap.FOREGROUND_WAIT_S: None then means the caller hands over to
    detached checks. With no project there is nobody to start them detached, so it waits on. `arrived`
    is the place in the queue (default now); `same` and `done` as for checkcap.acquire. A cancel request
    for this process (CHECKS_SLOT_CANCEL) ends the wait and the run (exit 1). Slots it cannot write end
    the run (exit 2), never uncapped. Light checks (`light`: checklane.all_light) run at once, outside
    every slot; full checks queue for one of the lanes of the project's repository at `top` (checklane:
    checks.slots, checks.max_wait_s), or with `lock` (checklane.wrap_key: commands behind checks.wrap_cmd)
    for that wrapper lock's one lane, at most checklane.project_cap of the project's at once, told with
    the holder and the queue position each minute. With `spill` and delivery.checks_spill_hosts set, a
    wait past delivery.checks_spill_after_s tries those hosts (checkspill.pick) and keeps its place here
    when none can take the run: a slot whose `spill` claim names the host the checks then run on."""
    from . import checkcap, checklane, checkspill, locks as lk
    foreground = not detached and not os.environ.get("TTP_DETACHED")
    key = f"checks:{os.getpid()}:{time.time()}"

    def tell(line: str) -> None:
        out.write(f"ttp checks: {line}\n")
        out.flush()
        print(f"ttp checks: {line}", flush=True)
        try:
            durable_append(run_dir / "progress.md", f"{timefmt.clock()} checks: {line}\n")
        except OSError:
            pass

    def waiting(on: bool, suite: str = "", limit: int = 0) -> None:
        # The wait is recorded before the marker shows it, so whoever sees the marker sees both. `suite`:
        # the project's suite lock it waits on (testslots.suite_hold), which the `--result` probe names
        # with its `limit` (lanes at once).
        if foreground:
            lk.record_wait(run_dir, key, start, None if on else time.time())
        try:
            if on:
                write_json(run_dir / CHECKS_SLOT_WAIT, {"since": time.time(), "pid": os.getpid(), "head": head,
                                                        **({"suite": suite} if suite else {}),
                                                        **({"limit": limit} if suite and limit else {})})
            else:
                (run_dir / CHECKS_SLOT_WAIT).unlink(missing_ok=True)
        except OSError:
            pass

    start = time.time()
    if light:
        tell("light checks only (syntax, lint, format or validation: checklane, checks.light_cmds, --light): "
             "running now, outside the project's checks slot")
        return checkcap.Slot()
    prio, owns = checkcap.task_rank(p)
    cfg = p.config() if p else {}
    from . import testslots
    repo = testslots.repo_key(top) if p else ""
    cmds = cmds or []
    what = " && ".join(str(c) for c in cmds)[:300] or "full checks"
    # One of the repository's checks lanes (testslots.suite_hold; checks.slots of them, checklane), or with
    # `lock` its wrapper lock's one lane, taken before the checks slot so a run never holds one of the
    # host's slots while it waits. Its wait is told, recorded and cancellable as the checks slot's is; the
    # foreground wait for all of them together ends after FOREGROUND_WAIT_S.
    spill_to = bool(spill and p and checkspill.hosts(cfg))
    told: set[str] = set()
    while True:
        limit = max(0.0, checkcap.FOREGROUND_WAIT_S - (time.time() - start)) if foreground and p else None
        if spill_to:   # a bounded wait here, then the spill hosts, then wait again in the same place
            limit = checkspill.after_s(cfg) if limit is None else min(limit, checkspill.after_s(cfg))
        slot = _checks_slot_take(p, run_dir, out, head, tell, waiting, limit, arrived, same, done, prio, owns,
                                 cfg, repo, cmds, what, top, lock)
        if slot is not None or not spill_to:
            return slot
        # The same hosts skipped for the same reasons are told once, not after every bound.
        if (claim := checkspill.pick(p, cfg, lambda line: line in told or (told.add(line), tell(line)),
                                     what)) is not None:
            slot = checkcap.Slot()
            slot.spill = claim
            return slot
        if foreground and time.time() - start >= checkcap.FOREGROUND_WAIT_S:
            return None


def _checks_slot_take(p, run_dir: Path, out, head: str, tell, waiting, limit: float | None, arrived, same: str,
                      done, prio, owns, cfg: dict, repo: str, cmds: list, what: str, top: str = "",
                      lock: str = ""):
    """One wait of _checks_slot for the repository's lane (or `lock`'s), a host-wide checks slot and
    test-worker slots, at most `limit` seconds (None: no limit): the slot, or None once `limit` passed."""
    from . import checkcap, checklane, cleantree, push, testslots
    t0 = time.time()
    # Concurrent runs are isolated (own clean checkout, scratch, output folder and port block); what is
    # not keeps a lock: checks run in the authoring worktree also hold its one lane (testslots.tree_key).
    local = [c for c in cmds if not push.remote_host(c)]
    in_tree = bool(p and top and any(cleantree.authoring(c) for c in local))
    suite = tree = None

    def hold(key: str, n: int, **lane):
        left = max(0.0, limit - (time.time() - t0)) if limit is not None else None
        on_key = f"w{lane['lock']}" if lane.get("lock") else key
        return testslots.suite_hold(p.name if p else None, tell, left,
                                    lambda on: waiting(on, str(testslots.suite_path(p.name, on_key)),
                                                       1 if lane.get("lock") else n),
                                    lambda: _checks_cancelled(run_dir), f"run {run_dir.name}", prio, arrived,
                                    slots=n, repo=key,
                                    cost=checklane.cost(p.state if p else None, _commands_hash(cmds), cmds, cfg),
                                    max_wait=checklane.max_wait_s(cfg) if key == repo else 0.0,
                                    state=p.state if p else None, what=what, **lane)
    try:
        if in_tree:
            if (tree := hold(testslots.tree_key(top), 1)) is None:
                return None
        if (suite := hold(repo, checklane.slots(cfg), lock=lock, cap=checklane.project_cap(cfg),
                          worktree=top, headroom=checklane.headroom)) is None:
            if tree is not None:
                tree.close()
            return None
        if tree is not None:
            suite.files += tree.files
            tree = None
        left = max(0.0, limit - (time.time() - t0)) if limit is not None else None
        slot = checkcap.acquire(checkcap.label(p.name if p else None), f"ttp checks on {head[:12]}", tell, waiting,
                                left, arrived,
                                p.name if p else None, prio, same, done, lambda: _checks_cancelled(run_dir),
                                p.state if p else None, owns)
        if slot is None or slot.served:
            suite.close()
            return slot
        # Then the run's share of the host's test-worker slots (testslots), held with the checks slot;
        # in the foreground none free by the end of the same wait hands over to detached checks too.
        left = max(0.0, limit - (time.time() - t0)) if limit is not None else None
        slot.tests = testslots.acquire(checkcap.label(p.name if p else None), tell, left,
                                       p.config() if p else {}, p.name if p else "")
        if slot.tests is None:
            slot.close()
            suite.close()
            return None
        slot.tests.files += suite.files   # the suite lock ends with the run's test slots
        slot.suite = suite   # its label shows the run's wall-time cap (testslots.suite_running)
        if not os.environ.get(testslots.HELD_ENV) and (ports := testslots.port_block(checkcap.label(p.name if p else None))):
            slot.tests.files.append(ports[0])   # the run's own ports, freed with its test slots
            slot.tests.extra.update(ports[1])
        return slot
    except checkcap.Cancelled as e:
        for g in (suite, tree):
            if g is not None:
                g.close()
        said = ("not run: stopped waiting for "
                + ("this project's other full checks" if suite is None else "a host-wide checks slot") + f": {e}")
        out.write(f"ttp checks: {said}\n")
        out.flush()
        die(f"ttp checks: {said}", 1)
    except checkcap.Unavailable as e:
        for g in (suite, tree):
            if g is not None:
                g.close()
        out.write(f"ttp checks: {e}\n")
        out.flush()
        die(f"ttp checks: {e}")


def _checks_base(p: Project | None, top: Path, cmds: list) -> str:
    """What `ttp checks` compares `if_changed` scopes with: the push target as last fetched, or ""
    (no project, no push target or no copy of it here), and then those checks run."""
    from . import push
    if not p:
        return ""
    try:
        remote, branch = push.target(p, top)
    except ValueError:
        return ""
    r = subprocess.run(["git", "-C", str(top), "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


CHECKS_RC, CHECKS_PID, CHECKS_OUT = "checks.rc", "checks.pid", "checks.out"   # in the run's directory

CHECKS_TMP, CHECKS_TMP_LOCK = "checks-tmp", "checks-tmp.lock"


def _checks_tmp_cleanup(run_dir: Path) -> None:
    """Remove abandoned scratch only while its lifetime lock is free. The launcher, checks
    process and check commands inherit the same lock, so recovery never removes live storage,
    even between launch and checks.pid or after the checks supervisor was killed."""
    from . import checkdirs
    try:
        with checkdirs.directory(run_dir):
            info = _read_checks_pid(run_dir)
        base = os.environ.get("TTP_PROJECT")
        output = info.get("check_run_dir")
        if base and output and Path(output).parent == Project(base).state / checkdirs.FOLDER:
            checkdirs.cleanup(Path(output), whole=True)
    except OSError:
        pass


# A stop signal that lands while a check command starts or is being ended waits for that to finish.
# `stopped` keeps the first stop for the rest of the run, even if its SystemExit is lost on the way.
_CHECK_STOP: dict = {"defer": False, "pending": None, "stopped": None}
_CHECK_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _checks_stop(sig, _frame):
    """The detached checks' stop handler: exit 128+sig, deferred while a check starts or is ended."""
    _CHECK_STOP["stopped"] = _CHECK_STOP["stopped"] or 128 + sig
    if _CHECK_STOP["defer"]:
        _CHECK_STOP["pending"] = 128 + sig
        return
    raise SystemExit(128 + sig)


def _check_took(c, rc: int, began: float) -> str:
    """checks.log's line for one check's wall-clock seconds, so a slow suite shows in the log."""
    return f"ttp checks: {c} took {time.monotonic() - began:.1f} s (exit {rc})\n"


def _run_check(argv: list, group: bool, record=None, timeout: float | None = None, **kw) -> int:
    """Run one check command to its exit code. In its own group (`group`) a stop always ends it:
    a stop during Popen would otherwise leave the started command running. Once its exit code is
    known, and when it is stopped or fails, every process it left (checkreap: tied by the run's
    token in its environment, or by its group and a working directory in its tree) is ended too;
    the check log says how many. `record` (checkreap.Record) learns the check's group. Past `timeout`
    seconds it is ended and subprocess.TimeoutExpired raised."""
    proc = None
    _CHECK_STOP.update(defer=True, pending=None)
    try:
        if _CHECK_STOP["stopped"]:   # stopped checks start no further command
            raise SystemExit(_CHECK_STOP["stopped"])
        proc = subprocess.Popen(argv, start_new_session=group, **kw)
        if group and record is not None:
            record.group(proc.pid)
        _CHECK_STOP["defer"] = False
        if _CHECK_STOP["pending"]:
            raise SystemExit(_CHECK_STOP["pending"])
        rc = proc.wait(timeout=timeout)
        _reap_check(proc, group, kw)
        return rc
    except BaseException:
        _CHECK_STOP["defer"] = True
        try:
            if group and proc is not None:
                _end_check(proc)
            elif proc is not None and proc.poll() is None:
                _end_check(proc, group=False)
            if proc is not None:
                _reap_check(proc, group, kw)
        finally:
            _CHECK_STOP["defer"] = False
        if _CHECK_STOP["pending"]:
            raise SystemExit(_CHECK_STOP["pending"])
        raise
    finally:
        _CHECK_STOP["defer"] = False


def _end_check(proc: subprocess.Popen, group: bool = True) -> None:
    """End a check run in its own group (or, not in one, the command itself): TERM, then KILL if it
    is still there after 5 s."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig) if group else proc.send_signal(sig)
        except OSError:
            break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            pass


_REAPED = {"procs": 0}   # leftover processes this run's checks left, ended as each check ended


def _foreground_stop() -> dict:
    """A foreground run's SIGTERM or SIGHUP ends its running check and what that left, as a
    detached run's does (_checks_stop); the handlers it replaced, to restore. Only where the
    default handler is in place, from the main thread."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        return {}
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is signal.SIG_DFL:
            old[sig] = signal.signal(sig, _checks_stop)
    if old:
        _CHECK_STOP["stopped"] = None
    return old


def _checks_reaped(out, tok: str, record, clean, rc) -> None:
    """The end of a run's checks, however it ends: record their exit code (exit.json), end what is
    still tied to the run, remove the clean tree, say what was reaped, then drop the record unless
    a process would not end (the sweep tries again later)."""
    from . import checkreap
    _CHECK_STOP["defer"] = True
    found, left = 0, 1
    try:
        if record is not None:
            record.exit(rc)
        found, left = checkreap.reap(tok, record.data["pgids"] if record is not None else (),
                                     str(clean.dir) if clean is not None else "")
    except OSError:
        pass
    finally:
        try:
            if clean is not None:
                clean.close()
            if procs := _REAPED["procs"] + found:
                out.write(f"{checkreap.said(procs, 1 if clean is not None else 0)}"
                          + (f"; {left} would not end" if left else "") + "\n")
            if record is not None and not left:
                record.forget()
        except (OSError, ValueError):
            pass
        _CHECK_STOP["defer"] = False
        if _CHECK_STOP["pending"]:
            raise SystemExit(_CHECK_STOP["pending"])


def _reap_check(proc: subprocess.Popen, group: bool, kw: dict) -> None:
    """End what the finished check `proc` left running (checkreap) and say so in its log."""
    from . import checkreap
    tok = (kw.get("env") or {}).get(checkreap.MARKER, "")
    if not tok:
        return
    found, left = checkreap.reap(tok, (proc.pid,) if group else (), str(kw.get("cwd") or ""))
    _REAPED["procs"] += found
    out = kw.get("stdout")
    if found and hasattr(out, "write"):
        try:
            out.write(f"ttp checks: ended {found} process{'es' if found != 1 else ''} the check left running"
                      + (f"; {left} would not end" if left else "") + "\n")
            out.flush()
        except (OSError, ValueError):
            pass


def _checks_alive(info: dict) -> bool:
    """The detached checks recorded in `info` still run: same pid and start, not a zombie."""
    from .runner import proc_start
    pid, started = info.get("pid"), info.get("started")
    return isinstance(pid, int) and bool(started) and proc_start(pid) == started and not zombie(pid)


def _checks_elsewhere(info: dict) -> bool:
    """This process cannot judge the recorded checks' pid: it runs boxed (push._boxed), or in a PID
    namespace other than the one that started them."""
    from . import push
    ns = push.pid_ns()
    return push._boxed() or bool(info.get("pidns") and ns and ns != info["pidns"])


def _checks_old_boot(info: dict) -> bool:
    """The recorded checks were started in an earlier boot of this host. A missing or unknown boot
    id, recorded or current, never says so."""
    from .runner import boot_id, same_boot
    b = info.get("boot")
    return isinstance(b, str) and b not in ("", "unknown") and boot_id() != "unknown" and not same_boot(b)


def _read_checks_pid(run_dir: Path) -> dict:
    try:
        info = json.loads((run_dir / CHECKS_PID).read_text())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _checks_scope(worktree, purpose) -> tuple:
    """What a run dir's detached checks are for: their worktree and purpose."""
    return (os.path.realpath(worktree) if worktree else "", "harness" if purpose == "harness" else "")


def _checks_owner(run_dir: Path, top: str, purpose) -> str:
    """The run's detached checks for another worktree or purpose that still own `run_dir`'s files
    (queued for the daemon, or started and without checks.rc), told as '<worktree> (purpose ...)';
    "" when there are none, or they are for the same worktree and purpose (a rerun replaces them).
    Where their pid cannot be judged (a sandbox), they count as running until checks.rc is written."""
    from . import detach
    rc = run_dir / CHECKS_RC
    info: dict = {}
    if detach.pending(rc):
        q, _ = detach._marker(rc)
        try:
            info = json.loads(q.read_text()).get("checks") or {}
        except (OSError, ValueError, AttributeError):
            info = {}
    else:
        pid_info = _read_checks_pid(run_dir)
        if pid_info and not rc.exists() and (_checks_alive(pid_info) or (
                _checks_elsewhere(pid_info) and not _checks_old_boot(pid_info))):
            info = pid_info
    if not isinstance(info, dict) or not info.get("worktree"):
        return ""
    theirs = _checks_scope(info.get("worktree"), info.get("purpose"))
    if theirs == _checks_scope(top or os.getcwd(), purpose):
        return ""
    return theirs[0] + (f" (purpose {theirs[1]})" if theirs[1] else "")


def _checks_detach(a, run_dir: Path, p: Project | None, top: str = "", head: str = "",
                   arrived: float | None = None, no_spill: bool = False) -> None:
    """Start `ttp checks` in a session of its own. It writes checks.rc however it ends (a stop by
    TERM, HUP or INT included); the printed `--result` probe also wakes the task when it was killed
    outright. This run's earlier detached checks for the same worktree and purpose are stopped
    first, so nobody needs `pkill`; while ones for another worktree or purpose still own the run
    dir, it refuses (exit 75) and prints their probe.
    A tool sandbox queues the launch and cancellation through the daemon (detach.tend).
    checks.pid also keeps the worktree, head and command, so the daemon can start the same checks
    again, model-free, after a reboot killed them (Daemon._relaunch_checks). `arrived`: a foreground
    run's place in the host-wide queue, which they keep."""
    from . import checkcap, detach, push
    rc = run_dir / CHECKS_RC
    owner = _checks_owner(run_dir, top, getattr(a, "purpose", None))
    if owner:
        # One run dir holds one detached checks job's files: replacing another worktree's or
        # purpose's checks would lose them without a word. Wait for them instead.
        print(f"retry_when: {push._own_ttp(p)} checks --result {shlex.quote(str(run_dir))}")
        die(f"ttp checks: not started: this run's detached checks for {owner} still own {run_dir} "
            "(exit 75): hand off waiting with the retry_when above, then run these checks again", 75)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    if arrived is not None:
        env[checkcap.ARRIVED_ENV] = repr(arrived)
    if no_spill:   # the rerun here of checks that did not pass on a spill host keeps off the spill hosts
        from . import checkspill
        env[checkspill.OFF_ENV] = "1"
    detach.lost(rc).unlink(missing_ok=True)
    # A request the daemon has not finished starting is replaced through the queue too, so the daemon
    # never starts an older request after this one.
    if push._boxed() or (p and detach.pending(rc)):
        if not p:
            die("ttp checks: sandboxed detached checks need a project daemon; set TTP_PROJECT")
        detach.enqueue(p.state, rc, run_dir / CHECKS_OUT, [], top or os.getcwd(), env,
                       checks={"worktree": top, "head": head, "cmd": list(a.cmd), "fresh": bool(a.fresh),
                               "full": bool(getattr(a, "full", False)), **_harness_purpose(a)})
        print("ttp checks: queued; the daemon starts them outside this command's sandbox")
    else:
        detach.withdraw(rc)   # a marker left without a request (no daemon to take it)
        pid = _checks_start(a, run_dir, top, head, env)
        print(f"ttp checks: started in the background (pid {pid}); output in {run_dir / CHECKS_OUT}, "
              f"exit code in {rc}")
    print(f"retry_when: {push._own_ttp(p)} checks --result {shlex.quote(str(run_dir))}")
    print("on_pass: if nothing is left after them but recording the result, put that final hand-off "
          "(done or needs_review) in the waiting hand-off's on_pass; a pass records it without a model run")


def _checks_place(run_dir: Path, old: dict, env: dict) -> float:
    """The arrival time a launch queues at for a host-wide checks slot: the earlier of the handed-over
    one (checkcap.ARRIVED_ENV in `env`) and, when the checks it restarts or replaces were still waiting
    for a slot (CHECKS_SLOT_WAIT names their pid), theirs (checks.pid's arrived); else now."""
    from . import checkcap
    now = time.time()
    seen = []
    try:
        seen.append(float(env.get(checkcap.ARRIVED_ENV, "")))
    except ValueError:
        pass
    try:
        wait = json.loads((run_dir / CHECKS_SLOT_WAIT).read_text())
    except (OSError, ValueError):
        wait = {}
    if isinstance(wait, dict) and isinstance(old.get("pid"), int) and wait.get("pid") == old.get("pid"):
        try:
            seen.append(float(old.get("arrived") or old.get("ts") or ""))
        except (TypeError, ValueError):
            pass
    seen = [t for t in seen if math.isfinite(t) and 0 < t <= now]
    return min(seen) if seen else now


def _checks_start(a, run_dir: Path, top: str, head: str, env: dict, req: str = "") -> int:
    """Launch outside the tool sandbox, cancelling the earlier host process before replacing its
    metadata. The child runs the checks only on this launcher's `go`, sent once checks.pid durably
    names it (or, if the launcher died after that write, once it finds itself named there): a
    launcher that dies before the write leaves a child that starts nothing. `req` is the queued
    request this launch serves (detach.tend). Checks it restarts or replaces while they still wait for a
    host-wide slot hand their place in the queue to this launch (_checks_place)."""
    from .push import pid_ns
    from .runner import boot_id, proc_start
    from . import checkcap, checkdirs
    checkdirs.writable(str(run_dir))
    old = _read_checks_pid(run_dir)
    arrived = _checks_place(run_dir, old, env)
    if _checks_alive(old):
        try:
            os.killpg(old["pid"], signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 15
        while _checks_alive(old) and time.time() < deadline:
            time.sleep(0.1)
        if _checks_alive(old):
            try:
                os.killpg(old["pid"], signal.SIGKILL)
            except OSError:
                pass
    base = env.get("TTP_PROJECT")
    if not base:
        raise OSError("TTP_RUN_DIR for detached checks requires a project state directory (TTP_PROJECT)")
    # Refuse a linked legacy lock, and preserve the old commands' lifetime on upgrade.
    previous = Path(old["check_run_dir"]) if old.get("check_run_dir") else run_dir
    if previous != run_dir and previous.parent != Project(base).state / checkdirs.FOLDER:
        raise OSError("TTP_RUN_DIR in checks.pid is outside the project's check output directory")
    old_guard = checkdirs.take(previous) if previous.exists() or previous.is_symlink() else False
    if old_guard is None:
        raise OSError("earlier check commands still own their temporary storage; not starting replacement checks")
    if old_guard:
        old_guard.close()
        if previous != run_dir:
            checkdirs.finish(previous)
    output = checkdirs.new(Project(base).state, run_dir / CHECKS_PID)
    guard = checkdirs.take(output)
    launched = False
    try:
        env = checkdirs.environment(output, env)
        # The supervisor records checks.log/rc in the original run; commands get the report dir.
        env['TTP_RUN_DIR'] = str(run_dir)
        env['TTP_CHECKS_TMP_FD'] = str(guard.fileno())
        env[checkcap.ARRIVED_ENV] = repr(arrived)
        (run_dir / CHECKS_RC).unlink(missing_ok=True)
        (run_dir / CHECKS_SLOT_WAIT).unlink(missing_ok=True)
        (run_dir / CHECKS_SLOT_CANCEL).unlink(missing_ok=True)
        with open(run_dir / CHECKS_OUT, "wb") as out:
            child = subprocess.Popen([sys.executable, "-m", "ttp", "checks", "--rc", str(run_dir / CHECKS_RC),
                                      *(["--fresh"] if a.fresh else []), *(["--full"] if getattr(a, "full", False) else []),
                                      *(["--light"] if getattr(a, "light", False) else []),
                                      *(["--purpose", a.purpose] if getattr(a, "purpose", None) else []), *a.cmd],
                                     cwd=top or Path.cwd(), env=env, stdin=subprocess.PIPE, stdout=out,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(guard.fileno(),))
        try:
            write_json(run_dir / CHECKS_PID, {"pid": child.pid, "started": proc_start(child.pid), "ts": time.time(),
                                              "boot": boot_id(), "worktree": top, "head": head, "cmd": list(a.cmd),
                                              "fresh": bool(a.fresh), "full": bool(getattr(a, "full", False)),
                                              "light": bool(getattr(a, "light", False)),
                                              **_harness_purpose(a), "req": req, "pidns": pid_ns(), "arrived": arrived,
                                              "check_run_dir": str(output), "check_report_dir": str(checkdirs.reports(output))})
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)   # this launch's own child, still waiting for its go
            except OSError:
                pass
            child.wait()
            raise
        try:
            child.stdin.write(b"go\n")
            child.stdin.close()
        except OSError:
            pass   # it died: the probe reports that
        launched = True
        return child.pid
    finally:
        guard.close()
        if launched:
            checkdirs.cleanup(output, whole=True)
        else:
            checkdirs.finish(output)


def _checks_released(go: bytes, run_dir: Path) -> bool:
    """The detached child may run: its launcher sent `go`, or died after checks.pid named this
    very process (pid and start), so its identity is durable either way."""
    from .runner import proc_start
    if go.startswith(b"go"):
        return True
    info = _read_checks_pid(run_dir)
    return info.get("pid") == os.getpid() and bool(info.get("started")) and info.get("started") == proc_start(os.getpid())


def _checks_child(a) -> int:
    """The detached `ttp checks`: run them, then write the exit code to `--rc` however it ends."""
    rc_file = Path(a.rc)
    tmp_fd = int(os.environ["TTP_CHECKS_TMP_FD"]) if "TTP_CHECKS_TMP_FD" in os.environ else None
    released = False
    code: int | str = 1

    _CHECK_STOP["stopped"] = None
    for sig in _CHECK_STOP_SIGNALS:
        signal.signal(sig, _checks_stop)
    try:
        # A stop signal blocked in the launcher stays blocked here and in the check commands: the
        # checks could then never be stopped, and a stop would record their exit code instead.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _CHECK_STOP_SIGNALS)
        released = _checks_released(sys.stdin.buffer.read(), rc_file.parent)
        if not released:
            # A newer launch may own checks.rc: this unrecorded child leaves it alone.
            print("ttp checks: not started: the launcher ended before it recorded this process", flush=True)
            return 1
        from . import checkdirs
        output = checkdirs.writable(os.environ.get("TTP_CHECK_OUTPUT_DIR"))
        cmd_checks(argparse.Namespace(**{**vars(a), "rc": None, "detach": False, "own_group": True,
                                         "check_run_dir": output, "tmp_fd": tmp_fd}))
        code = 0
    except OSError as e:
        print(f"ttp checks: {e}", file=sys.stderr, flush=True)
        code = 1
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code > 128:
            with open(rc_file.parent / "checks.log", "a") as log:
                log.write(f"ttp checks: stopped by signal {code - 128}\n")
    finally:
        stopped = _CHECK_STOP["stopped"]
        if released and stopped and code != stopped:
            # A stop whose SystemExit something caught on the way still ends the checks as stopped.
            code = stopped
            with open(rc_file.parent / "checks.log", "a") as log:
                log.write(f"ttp checks: stopped by signal {code - 128}\n")
        if tmp_fd is not None:
            os.close(tmp_fd)
            from . import checkdirs
            info = _read_checks_pid(rc_file.parent)
            if info.get('pid') == os.getpid() and info.get('check_run_dir'):
                checkdirs.finish(Path(info['check_run_dir']))
            _checks_tmp_cleanup(rc_file.parent)
        if released:
            durable_write(rc_file, f"{code}\n")
    return code if isinstance(code, int) else 1


def _checks_env_ok(p: Project | None, sigs: list | None = None) -> int:
    """`retry_when` probe of a check run that stopped at a runner fault (envfault): 0 once the
    faults named by `sigs` (every open one when none is named) have their runner back (each
    closes), 1 while one is still missing. Another task's open fault never holds a named wait."""
    from . import envfault
    still = envfault.recheck(p, sigs=sigs) if p else {}
    for f in still.values():
        print(f"ttp checks: still missing: {envfault.line(f)}")
    if not still:
        print("ttp checks: no check runner is missing")
    return 1 if still else 0


def _checks_result(run_dir: Path) -> int:
    """`retry_when` probe of detached checks: 0 once checks.rc exists or the checks died without
    writing it (it then writes `killed` there), 1 while they run."""
    rc_file = run_dir / CHECKS_RC
    from . import detach, envfault
    _checks_tmp_cleanup(run_dir)
    if detach.pending(rc_file):
        print("ttp checks: queued for the daemon, which starts them outside the sandbox")
        return 1
    if detach.lost(rc_file).exists():
        durable_write(rc_file, "killed\n")
        print(f"ttp checks: not started: {detach.lost(rc_file).read_text().strip()}")
        return 0
    info = _read_checks_pid(run_dir)
    if info and not rc_file.exists() and _checks_old_boot(info):
        # Checked before the namespace fallback: no process of an earlier boot still runs.
        durable_write(rc_file, "killed\n")
        print(f"ttp checks: the detached checks (pid {info.get('pid')}) were started before this host last "
              "rebooted and ended without an exit code: the reboot interrupted them; run `ttp checks --detach` again")
        return 0
    if info and not rc_file.exists() and _checks_elsewhere(info):
        # Its pid means nothing in this PID namespace: never call it dead (nor write `killed`) from
        # here. The daemon's own probe of retry_when sees the host's processes and settles it.
        print(f"ttp checks: started outside this sandbox (pid {info.get('pid')}); running unless the daemon's "
              "probe finds otherwise")
        return 1
    alive = _checks_alive(info)
    if not rc_file.exists() and alive:
        try:
            wait = json.loads((run_dir / CHECKS_SLOT_WAIT).read_text())
        except (OSError, ValueError):
            wait = None
        if isinstance(wait, dict) and wait.get("suite"):
            from . import checkcap, locks as lk
            try:
                held = checkcap._some(lk.held_labels([Path(wait["suite"])]))
            except OSError:
                held = ""
            n = wait.get("limit") if isinstance(wait.get("limit"), int) else 1
            print(f"ttp checks: waiting (pid {info['pid']}, not running) for this project's other full checks "
                  f"to end, {'one' if n == 1 else n} at a time per project and repository (checks.slots sets "
                  f"it): held by {held or 'a run that just ended'}")
        elif wait is not None or (run_dir / CHECKS_SLOT_WAIT).exists():
            from . import checkcap
            print(f"ttp checks: queued for a host-wide checks slot (pid {info['pid']}); "
                  + (checkcap.line() or "they start once one is free"))
        else:
            print(f"ttp checks: running (pid {info['pid']})")
        return 1
    if rc_file.exists() and rc_file.read_text().strip() == "75" and (fault := envfault.marker_open(run_dir)):
        # Stopped at a runner the host lacks: asleep until it is back, then the task runs them again.
        print(f"ttp checks: waiting: {envfault.line(fault)}")
        return 1
    if rc_file.exists():
        print(f"ttp checks: finished with exit code {rc_file.read_text().strip()}")
        print(f"ttp checks: full output ('$ cmd' lines, test results) in {run_dir / 'checks.log'}")
        from . import slowtests
        for line in slowtests.lines(run_dir / slowtests.FILE):
            print(f"ttp checks: {line}")
    elif not info:
        print(f"ttp checks: no detached checks recorded in {run_dir}")
        return 0
    else:
        durable_write(rc_file, "killed\n")
        print(f"ttp checks: the detached checks (pid {info.get('pid')}) ended without an exit code: something "
              "killed them; run `ttp checks --detach` again")
    try:
        tail = (run_dir / CHECKS_OUT).read_text(errors="replace").splitlines()[-20:]
    except OSError:
        tail = []
    if tail:
        print("\n".join(tail))
    return 0


def cmd_clip(a) -> None:
    """Run one command with its whole output in a file, and print only what a model needs: a test
    run's failures, else the head and tail, plus the file's path. Exits with the command's code.
    Inside a run the file goes in the run's folder (out/), else in a temporary one. A build (make, ninja,
    cmake --build) first takes this run's share of the host's build slots (testslots.BUILDS) and runs
    with that many jobs, at a lower priority; none free within CLIP_WAIT_S: one job."""
    from . import testslots, trim
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        die("usage: ttp clip -- <command>")
    line, env, grant, argv = shlex.join(cmd) if len(cmd) > 1 else cmd[0], None, None, None
    if testslots.builds(line):
        def tell(msg: str) -> None:
            print(f"ttp clip: {msg}", flush=True)
        grant = testslots.acquire(f"ttp clip (task #{os.environ.get('TTP_TASK') or '?'}, pid {os.getpid()}): "
                                  f"{line[:80]}", tell, testslots.CLIP_WAIT_S, kind=testslots.BUILDS)
        if grant is None:
            tell(f"no host build slot free after {testslots.CLIP_WAIT_S:.0f} s: building with 1 job")
            grant = testslots.Grant(1, kind=testslots.BUILDS)
        if grant.k:
            if (capped := testslots.clamp_build(line, grant.k)) != line:
                tell(f"build jobs capped at -j {grant.k} (this run's share of the host's build slots): {capped}")
            line, env = capped, {**os.environ, **grant.env()}
            argv = testslots.low_priority(["/bin/sh", "-c", line])
    run_dir = os.environ.get("TTP_RUN_DIR")
    folder = Path(run_dir) / "out" if run_dir else Path(tempfile.mkdtemp(prefix="ttp-clip-"))
    folder.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for _ in folder.glob("clip-*.log"))
    log = folder / f"clip-{n}.log"
    try:
        with open(log, "w") as out:
            rc = subprocess.run(argv or line, shell=argv is None, stdout=out, stderr=subprocess.STDOUT,
                                env=env).returncode
    finally:
        if grant is not None:
            grant.close()
    text = log.read_text(errors="replace")
    print(trim.summary(text, str(log), a.lines))
    print(f"(exit {rc})")
    sys.exit(rc)


def skill_dirs() -> list[Path]:
    """Where this release's skills are: the plugin's own, else the copy `ttp setup` installed with this
    version, else the current install (a project's harness copy has none of its own)."""
    from .project import HOME_DIR
    return [PLUGIN_ROOT / "skills", HOME_DIR / "lib" / __version__ / "skills", HOME_DIR / "lib" / "current" / "skills"]


def cmd_skill(a) -> None:
    """Print the path of an installed tt-project skill's SKILL.md (no name: list them), so a worker in
    any project, on any agent, can read the skill without finding the plugin cache."""
    for d in skill_dirs():
        names = sorted(f.parent.name for f in d.glob("*/SKILL.md")) if d.is_dir() else []
        if not names:
            continue
        if not a.name:
            for n in names:
                print(f"{n}\t{d / n / 'SKILL.md'}")
            return
        if a.name in names:
            print(d / a.name / "SKILL.md")
            return
        die(f"no skill {a.name!r} in {d}; there are: {', '.join(names)}", 1)
    die("no installed tt-project skills found; run `<plugin-root>/bin/ttp setup`", 1)


def cmd_broker_released(a) -> None:
    """Probe: exit 0 once a device broker's hold is released, 1 while not (brokerhold.released), from
    /health JSON and log text in the files named, or on stdin."""
    from . import brokerhold
    text = "".join(Path(f).read_text(errors="replace") for f in a.files) if a.files else sys.stdin.read()
    ok, why = brokerhold.released(text)
    print(f"ttp broker-released: {'released' if ok else 'not released'} ({why})")
    sys.exit(0 if ok else 1)


def cmd_sandbox(a) -> None:
    """Run one command in a process sandbox (sandbox.py): its own PID namespace and /proc, so it
    cannot see or signal any other process on the host, and nothing it started outlives it. Where
    the host allows no sandbox it runs as it is and says why. Exits with the command's code."""
    from . import sandbox
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        die("usage: ttp sandbox -- <command...>")
    sys.exit(sandbox.run(cmd))


def cmd_hours(a) -> None:
    """`ttp hours`: the user's usual active hours, from the times of their own messages over the last
    days in the project's home zone (hours.py). Before starting a time-limited step for the user (a
    sign-in link, a device code), `--check` exits 0 when now is a good time (inside them, or no pattern
    yet), 1 when not: hand off waiting with it as `retry_when`."""
    import sqlite3
    from . import hours
    p = here()
    if p is None:
        die("ttp hours: no project here (run it in a project or its run)")
    now = time.time()
    try:
        times = hours.user_times(p.state / "project.db", now - a.days * 86400)
    except sqlite3.Error as e:
        die(f"ttp hours: cannot read the project's messages ({e})", 75)
    r = hours.assess(times, timefmt.home(p), now=now, need_s=a.need)
    if a.json:
        z = r["zone"]
        print(json.dumps({**r, **{f"{k}_text": timefmt.long(r[k], z) for k in ("until", "next_start") if r[k]},
                          "summary": hours.describe(r, now)}))
    else:
        print(hours.describe(r, now))
    if a.check:
        sys.exit(0 if r["ok"] else 1)


def cmd_killscan(a) -> None:
    """Check scripts for kills by name or pattern (`pkill`, `killall`, `pgrep`/`pidof`, `ps | grep`) before
    running them: such a kill can match the worker's own tool shell. Exits 1 when one is found.
    `--shim <dir>` writes logging stand-ins for those commands; put <dir> first on PATH to run the
    script without them killing anything."""
    from . import killscan
    if a.shim:
        folder = Path(a.shim).resolve()
        killscan.write_shims(folder)
        print(f"shims for {', '.join(killscan.SHIMMED)} in {folder}; calls are logged to "
              f"{folder / 'killscan.log'}. Run the script with: PATH={shlex.quote(str(folder))}:\"$PATH\" <script>")
    hits = 0
    for name in a.files:
        try:
            text = Path(name).read_text(errors="replace")
        except OSError as e:
            die(f"ttp killscan: cannot read {name}: {e.strerror or e}")
        for n, reason, line in killscan.scan(text):
            hits += 1
            print(f"{name}:{n}: {reason}: {line}")
    if a.files:
        print(f"ttp killscan: {hits} kill(s) by name or pattern found" if hits else
              "ttp killscan: no kills by name or pattern found")
    elif not a.shim:
        die("usage: ttp killscan <script>... [--shim <dir>]")
    sys.exit(1 if hits else 0)


def cmd_leftover_procs(a) -> None:
    """A hand-off check (worker.handoff_checks): exit 1 while a done hand-off's task still has live
    processes in its own worktree (leftoverprocs.py), 0 when it has none, 2 when it cannot tell."""
    from . import leftoverprocs
    path = a.result or os.environ.get("TTP_RESULT_JSON") or ""
    tid = a.task if a.task is not None else os.environ.get("TTP_TASK_ID", "")
    if not path or not str(tid).isdigit():
        die("usage: ttp leftover-procs <result.json> (the task from TTP_TASK_ID, or --task)")
    p = here()
    if p is None:
        die("ttp leftover-procs: no project here; run it from the project root")
    try:
        result = leftoverprocs.load(path)
    except (OSError, ValueError) as e:
        die(f"ttp leftover-procs: cannot read {path}: {e}")
    rc, msg = leftoverprocs.check(result, p.worktrees / f"t{int(tid)}")
    if msg:
        print(msg)
    sys.exit(rc)


def cmd_svcscan(a) -> None:
    """Check scripts for service unit changes (`systemctl stop|disable|mask|start|restart|...`, also
    under `sudo -n`) before running them on a shared machine. Exits 1 when one is found. `--shim <dir>`
    writes logging stand-ins for systemctl, service and sudo; put <dir> first on PATH to exercise the
    script without changing a real unit."""
    from . import svcscan
    if a.shim:
        folder = Path(a.shim).resolve()
        svcscan.write_shims(folder)
        print(f"shims for {', '.join(svcscan.SHIMMED)} in {folder}; calls are logged to "
              f"{folder / 'svcscan.log'}. Run the script with: PATH={shlex.quote(str(folder))}:\"$PATH\" <script>")
    hits = 0
    for name in a.files:
        try:
            text = Path(name).read_text(errors="replace")
        except OSError as e:
            die(f"ttp svcscan: cannot read {name}: {e.strerror or e}")
        for n, reason, line in svcscan.scan(text):
            hits += 1
            print(f"{name}:{n}: {reason}: {line}")
    if a.files:
        print(f"ttp svcscan: {hits} service unit change(s) found" if hits else
              "ttp svcscan: no service unit changes found")
    elif not a.shim:
        die("usage: ttp svcscan <script>... [--shim <dir>]")
    sys.exit(1 if hits else 0)


def cmd_ssh_preflight(a) -> None:
    """Exit 0 once a host answers over ssh, 255 while it does not (for retry_when; 12 with
    --classified), 10 when its host key does not verify, 11 when authentication fails (both wake a
    waiting task). ssh runs with strict host-key checking and no connection sharing. `@<resource>`
    finds the host in the project's config `device.hosts` (or `device.runners`) each time it runs,
    so a probe follows its resource to another box; an unknown resource exits 2."""
    from . import hostmap
    target = a.target
    if target.startswith("@"):
        base = os.environ.get("TTP_PROJECT") or \
            next((str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
        if not base:
            die("ttp ssh-preflight @<resource> only works in a tt-project (or with TTP_PROJECT set)")
        try:
            host = hostmap.resolve(Project(base).config(), target)
        except KeyError as e:
            die(f"ssh-preflight: {e.args[0]}", hostmap.UNKNOWN_RC)
    else:
        host = target
    rc, out = hostmap.preflight(host, a.timeout, a.classified)
    shown = f"{host} ({target})" if host != target else host
    why = {hostmap.HOST_KEY_RC: "host key not verified (changed, or not in known_hosts)",
           hostmap.AUTH_RC: "authentication failed"}.get(rc, "not reachable")
    if rc == 0:
        print(f"ssh-preflight: {shown} reachable")
    else:
        print(f"ssh-preflight: {shown} {why} (exit {rc})" + (f": {out}" if out else ""),
              file=sys.stderr)
    sys.exit(rc)


def cmd_goal(a) -> None:
    """Run one stock goal check (suite_green, artifact_fresh, service_up); see goalchecks."""
    from . import goalchecks
    sys.exit(goalchecks.run(a))


LOCK_NA = 3   # `ttp lock --probe` outside a project, of a lock only a project keeps: no answer here


def _machine_lock_answer(resource: str, shared_mode: bool, as_json: bool = False) -> tuple[int, str | dict]:
    """(exit code, line) of `ttp lock --probe` outside any project (see _probe_machine_lock); the line
    is lockprobe.report's dict when `as_json`."""
    from . import fleethold, lockprobe, locks as lk, shared
    if resource not in shared.names({}):
        said = f"{resource}: n/a (no tt-project project here, and `ttp machines` does not share it)"
        return LOCK_NA, {"resource": resource, "na": True, "line": said} if as_json else said
    hold = fleethold.covering(resource)
    held = shared.read_pause(resource) or (fleethold.entry(hold) if hold else None)
    if held is not None:
        said = f"{resource}: paused" + (f" ({held['reason']})" if held.get("reason") else "")
        return 75, lockprobe.report(resource, said, False, paused=held) if as_json else said
    where = shared.root() / resource
    paths = lk.slot_paths(where, resource, shared.machine_slots(resource))
    free = lk.probe(where, resource, paths, shared=shared_mode and not resource.endswith("-device"))
    said = lockprobe.line(resource, free, where, resource, paths)
    return (0 if free else 75), lockprobe.report(resource, said, free, where, resource, paths) if as_json else said


def _probe_machine_lock(resource: str, shared_mode: bool, as_json: bool = False) -> None:
    """`ttp lock --probe` outside any project. A resource shared through `ttp machines` keeps its lock
    under ~/.tt-project/locks, where every project takes it: that lock answers, as the plain probe does
    (0 free, 75 busy, paused or reserved). Any other lock lives in some project's state, so there is
    nothing here to read: it says n/a and exits LOCK_NA. --json prints lockprobe.report instead."""
    rc, said = _machine_lock_answer(resource, shared_mode, as_json)
    print(json.dumps(said) if as_json else said)
    sys.exit(rc)


def _lock_answers(a, answers: list[tuple[str, int, str | dict]]) -> None:
    """Print `ttp lock --probe` answers of several resources ((name, exit code, line) each), as lines or
    with --json as a list of their lockprobe.report rows, each with a state (free, busy, paused or n/a),
    and exit: 75 when any is busy or paused, else LOCK_NA when any has no answer here, else 0."""
    if a.json:
        print(json.dumps([{**said, "state": {0: "free", LOCK_NA: "n/a"}.get(rc) or (
            "paused" if said.get("paused") else "busy")} for _, rc, said in answers], indent=1))
    else:
        for _, _, said in answers:
            print(said)
    rcs = {rc for _, rc, _ in answers}
    sys.exit(75 if 75 in rcs else LOCK_NA if LOCK_NA in rcs else 0)


def cmd_lock(a) -> None:
    """Hold one slot of a shared resource while a command runs: `ttp lock <resource> -- <cmd...>`.

    Parallel tasks share a device or a remote build directory this way: each takes the lock only for
    the commands that touch it, and the rest of the task runs alongside other work. Slots come from
    the project's `resources` config (default 1); a running `exclusive:<resource>` task holds one for
    its whole run, and one waiting for a slot reserves the resource: new commands wait until it has
    started, except that task's own commands (its detached driver, same TTP_TASK), which take the slot
    and clear the reservation as its run would. The lock is held until the command has ended, however it ends. Every name in config
    `device.locks` is the one device lock. Waiters are served in arrival order. Each take and release
    goes to the project's activity log (activity.py, `ttp machines activity`); `--disruptive` marks a
    command that may reset, power-cycle or reboot what it holds.

    `ttp lock A,B,C -- <cmd...>` (the names in one word) holds several at once, all or nothing: it queues on each at the same
    arrival time, so later waiters on any of them never pass it, and takes them, in name order, only
    once it is in turn on all of them; one that is not ready means it takes none and keeps waiting, so
    it never holds a subset while it waits. Every one shows as held by the same holder until the
    command ends. Each lock keeps its own rules (shared, device, nested, paused, reserved). A `ttp lock` run
    under a hold already (an enclosing one, or an exclusive task's run) does not wait behind such a
    waiter: it may be waiting for that very hold.

    `ttp lock --shared <resource> -- <cmd...>` is for read-only work: shared holders run together,
    while an exclusive holder (a plain `ttp lock`, or an exclusive task's whole-run hold) and shared
    ones exclude each other. A queued exclusive waiter is not jumped by later shared ones. Not for a
    device lock (one in config `device.locks` or ending in `-device`): at most one command at a time
    may be in the device phase, so `--shared` on one exits 2 at once and runs nothing. `--probe
    --shared` on one answers as the plain probe does.

    Nested: a `ttp lock` inside one that holds the same lock in the same or a stronger mode (or
    inside an exclusive task's run while its lineage holds it, hold.covers; a hold recorded lost does
    not) just runs, saying so on stderr. An exclusive one inside a shared
    hold of the lock exits 2 at once: it could only wait for itself.

    Inside a run, waiting is reported in the run's progress (a wait is not a stall), does not count
    toward the run's wall clock (which grows by at most its own length this way), and gives up after
    half the run's stall limit (a longer --timeout is capped to that; 0 means that limit too). Giving
    up exits 75: the task hands back `waiting` with retry_when `ttp lock --probe <resource>`. A paused
    resource exits 75 as well. In a `ttp detach` job, or a background (`nohup ... &`) driver that no longer
    runs under the run's agent, there is no cap.
    `ttp lock --probe <resource>` exits 0 when the resource is free, not paused, not reserved and
    nobody queues for it, else 75; with --shared, when no exclusive holder or waiter has it and it is
    neither paused nor reserved. Outside any project, `--probe` answers for a resource `ttp machines`
    shares (its lock is machine-wide) and says n/a, exit 3, for any other. `--json` (a probe) prints the
    answer as JSON: free, paused, waiting, reserved_for and each live holder with its pid and whether it
    lives; a slot an exclusive run handed to the keeper of its detached jobs names the keeper (pid, via
    "keeper") and the run supervisor that took it (taker_pid). `--probe A,B` answers for each and exits
    75 when any is busy or paused; with `--json` it prints a list of those answers, each with a state.
    Inside an exclusive task's run, or a detached job of it, a resource held by that run's own lineage
    (hold.covers: the run itself, a later run of the task, or the keeper whose hold lists this job)
    probes free (exit 0, held_for_caller in --json) unless paused: its `ttp lock` runs under that hold.
    """
    from . import hold, lockprobe, locks as lk
    cmd = list(a.command or [])
    while cmd and cmd[0] == "--disruptive":   # given after the resource, where the command's words start
        a.disruptive, cmd = True, cmd[1:]
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if a.json and cmd:
        die("ttp lock --json lists the locks and runs nothing: drop the command or --json", 2)
    a.probe = a.probe or a.json
    if not cmd and not a.probe:
        die("usage: ttp lock [--disruptive] <resource> -- <command...>  |  ttp lock --probe <resource>")
    names = list(dict.fromkeys(n for n in a.resource.split(",") if n))
    if not names:
        die("usage: ttp lock [--disruptive] <resource>[,<resource>...] -- <command...>", 2)
    base = os.environ.get("TTP_PROJECT")
    if not base and a.probe:
        base = next((str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
    if not base and a.probe:
        if len(names) == 1:
            return _probe_machine_lock(names[0], a.shared, a.json)
        return _lock_answers(a, [(n, *_machine_lock_answer(n, a.shared, a.json)) for n in names])
    if not base:
        die("ttp lock only works inside a tt-project run (or with TTP_PROJECT set)")
    p = Project(base)

    def _refuse_paused() -> None:
        # Checked before each try, so a pause set while this waits holds too. A pause of any of the
        # device's names holds them all; a fleet hold, every name on its machine.
        for L in todo:
            held = lockprobe.pause(p, cfg, L["name"])
            if held is not None:
                if waiting:
                    _end_wait()
                why = f" ({held['reason']})" if held.get("reason") else ""
                die(f"{L['name']} is paused{why}; hand the task back as waiting until it is resumed", 75)

    from . import shared
    cfg = p.config()
    todo: list[dict] = []   # the locks to take, one per canonical lock, in name order (see the docstring)
    for n in names:
        res = lk.canonical(cfg, n)
        if all(L["res"] != res for L in todo):
            todo.append({"name": n, "res": res, "where": shared.locks_dir(p, res, cfg),
                         "device": res in lk.device_locks(cfg) or res.endswith("-device") or n.endswith("-device")})
    todo.sort(key=lambda L: L["res"])
    dev = next((L for L in todo if L["device"]), None)
    if a.shared and dev and not a.probe:   # a probe answers for the plain lock: a retry_when keeps working
        die(f"{dev['name']} is a device lock: one command at a time may use the device, so it cannot be "
            f"held shared. Use a plain `ttp lock {dev['name']} -- <command>`", 2)

    def _unwritable(e: OSError, L: dict) -> None:
        # Never a private lock instead: other holders would not see it. Say what is wrong.
        die(f"cannot take the lock of {L['name']}: {L['where']} is not writable here ({e.strerror or e}). "
            f"The run's sandbox or file permissions must allow writing it; hand the task back as blocked "
            f"and name this directory")

    for L in todo:
        try:
            L["paths"] = lk.slot_paths(L["where"], L["res"], shared.slots(p, L["res"], cfg))
        except OSError as e:
            _unwritable(e, L)
    if a.probe:   # a paused `ttp lock` exits 75, so its probe must too
        from . import pregrant
        # A free lock is free for a task only once the project's pre-grant check grants it too (asked
        # only for a known task: the probe of a task's retry_when names it with --task).
        ptask = str(getattr(a, "task", None) or os.environ.get("TTP_TASK") or "").strip()
        check = pregrant.command(cfg) if ptask else ""
        # What `ttp lock` runs under without waiting: this run's whole-run hold, or the keeper kept for
        # this detached job (hold.covers). Busy there would leave a driver that probes before each try
        # waiting on the very hold kept for it.
        try:
            rspec = json.loads((Path(os.environ["TTP_RUN_DIR"]) / "run.json").read_text())
        except (KeyError, OSError, ValueError):
            rspec = {}
        mytask = str(os.environ.get("TTP_TASK") or "").strip()
        whole = {lk.canonical(cfg, x.get("resource") or "") for x in (rspec.get("exclusive") or [])
                 if isinstance(x, dict)} if mytask and ptask == mytask else set()

        def _answer(name: str) -> tuple[int, str | dict]:
            rc, said = lockprobe.answer(p, cfg, name, a.shared, a.json)
            L = next(x for x in todo if x["res"] == lk.canonical(cfg, name))
            if rc != 0 and L["res"] in whole and lockprobe.pause(p, cfg, name) is None and (
                    cov := hold.covers(L["paths"], shared.holder(p, L["res"], hold.who(mytask), cfg), rspec)):
                line = f"{name}: free for this run (held by {cov}; its ttp lock runs under that hold)"
                rc, said = 0, ({**said, "free": True, "line": line, "held_for_caller": cov} if a.json else line)
            if rc != 0 or not check:
                return rc, said
            got = pregrant.check(check, p.root, name, ptask, p.state, pregrant.timeout_s(cfg))
            if got.granted:
                return rc, said
            return 75, ({**said, "free": False, "line": got.line, "pre_grant": got.line} if a.json else got.line)
        if len(names) == 1:
            rc, said = _answer(names[0])
            print(json.dumps(said) if a.json else said)
            sys.exit(rc)
        return _lock_answers(a, [(L["name"], *_answer(L["name"])) for L in todo])
    label = hold.label(hold.who(os.environ.get("TTP_TASK")), os.environ.get("TTP_RUN_ID"))
    own = os.environ.get("TTP_TASK") or ""
    for L in todo:
        L["who"] = shared.holder(p, L["res"], label, cfg)
        # What the daemon's reservation for this task names (_resources_free): its own driver may pass it.
        L["own"] = shared.holder(p, L["res"], hold.who(own), cfg) if own.strip() else None
    held_locks = [x for x in os.environ.get("TTP_LOCKS_HELD", "").split(",") if x]
    shared_locks = [x for x in os.environ.get("TTP_LOCKS_SHARED", "").split(",") if x]
    inner = [L for L in todo if L["res"] in held_locks or (L["res"] in shared_locks and a.shared)]
    todo = [L for L in todo if L not in inner]
    for L in inner:
        # An enclosing `ttp lock` of this command holds it already, in this mode or a stronger one.
        print(f"ttp lock: {L['name']} is held already by an enclosing ttp lock "
              f"({'exclusive' if L['res'] in held_locks else 'shared'}); "
              f"{'taking the rest' if todo else 'running the command under it'}", file=sys.stderr, flush=True)
    if not todo:
        sys.exit(subprocess.call(cmd))
    if bad := next((L for L in todo if L["res"] in shared_locks), None):
        die(f"cannot lock {bad['name']} exclusively inside a shared `ttp lock` of it: it would wait for itself. "
            f"Take the exclusive lock around the whole command instead", 2)
    mine = [L["res"] for L in todo]
    child_env = {**os.environ, **({"TTP_LOCKS_SHARED": ",".join(shared_locks + mine)} if a.shared
                                  else {"TTP_LOCKS_HELD": ",".join(held_locks + mine)})}
    if any(L["device"] for L in todo):
        child_env["TTP_NO_DEVICE"] = "0"   # a device lock is the sanctioned way to a device
        child_env = unmask_devices(child_env)   # and sees the devices a run's own commands do not
    detached = bool(os.environ.get("TTP_DETACHED"))
    run_dir = Path(os.environ["TTP_RUN_DIR"]) if os.environ.get("TTP_RUN_DIR") else None
    try:
        spec = json.loads((run_dir / "run.json").read_text()) if run_dir else {}
    except (OSError, ValueError):
        spec = {}
    waiting = False
    from . import activity
    from .runner import boot_id

    def _logged(mode: str, ls: list[dict], run) -> int:
        # Each take and release goes to the project's activity log (`ttp machines activity`).
        ids = [activity.new_id() for _ in ls]
        for aid, L in zip(ids, ls):
            activity.append(p.state, activity.base(p.name, ev="take", id=aid, resource=L["res"], mode=mode,
                                                   cmd=activity.head(cmd), start=time.time(),
                                                   **{k: v for k, v in lk.taker().items() if k != "node"},
                                                   boot=boot_id(), **({"disruptive": True} if a.disruptive else {}),
                                                   **({"job_rc": j} if (j := lk.job_fields().get("job_rc")) else {})))
        rc = 1
        try:
            rc = run()
            return rc
        finally:
            for aid in ids:
                activity.append(p.state, {"ev": "release", "id": aid, "end": time.time(), "rc": rc})

    whole = {lk.canonical(cfg, x.get("resource") or "") for x in spec.get("exclusive") or []}
    # run.json alone is not the hold: one recorded lost (its keeper died, another task took the slot)
    # still lists it. Only a slot the run's own lineage holds now covers the command (hold.covers).
    if run_held := [L for L in todo if L["res"] in whole and own.strip() and
                    hold.covers(L["paths"], shared.holder(p, L["res"], hold.who(own), cfg), spec)]:
        # This run's task holds the resource for its whole run already, exclusively even for a --shared
        # command: a nested plain `ttp lock` of it inside the command just runs too.
        _refuse_paused()
        todo = [L for L in todo if L not in run_held]
        if a.shared:
            child_env.update(TTP_LOCKS_SHARED=",".join(shared_locks + [L["res"] for L in todo]),
                             TTP_LOCKS_HELD=",".join(held_locks + [L["res"] for L in run_held]))
        for L in run_held:
            print(f"ttp lock: {L['name']} is held already by this task's whole run; "
                  f"{'taking the rest' if todo else 'running the command under it'}", file=sys.stderr, flush=True)
        if not todo:
            sys.exit(_logged("exclusive", run_held, lambda: subprocess.call(cmd, env=child_env)))
    what = ",".join(L["name"] for L in todo)   # what the messages and the retry_when name
    from . import pregrant
    from .runner import PRE_GRANT_FRESH_S
    check, asked_at, tries = pregrant.command(cfg), 0.0, 0

    def _pre_grant_refusal() -> str | None:
        # The project's own holds (pregrant.py): the refusal line when the check does not grant every lock.
        nonlocal asked_at
        asked_at = time.time()
        for L in todo:
            got = pregrant.check(check, p.root, L["name"], own, p.state, pregrant.timeout_s(cfg))
            if not got.granted:
                return (f"{got.line}; hand the task back as waiting with retry_when "
                        f"`ttp lock --probe {what}{f' --task {own}' if own.strip() else ''}`")
        return None
    if check and (why := _pre_grant_refusal()):
        die(why, 75)   # asked before queueing, so nothing waits on a held lock
    # A waiter that holds locks already never waits behind one for several resources at once: that one
    # may wait for a lock this waiter's holder has, and neither would ever go on.
    past_multi = bool(held_locks or shared_locks or whole)
    timeout = a.timeout
    cap = float(spec.get("stall_s") or 0) / 2
    capped = False
    if cap and not detached:
        if timeout is not None and (timeout == 0 or timeout > cap):
            print(f"ttp lock: --timeout {timeout:.0f} capped to {cap:.0f} s inside a run; if it runs out, "
                  f"hand off `waiting` with retry_when `ttp lock --probe{' --shared' if a.shared else ''} {what}`",
                  file=sys.stderr, flush=True)
        capped = not timeout or timeout > cap
        timeout = cap if timeout is None or timeout == 0 else min(timeout, cap)
    for L in todo:
        L["mark"] = lk.reserve_path(L["where"], L["res"])
    wait_dir = None if detached else run_dir
    started, told = time.time(), 0.0
    wait_key = f"{os.getpid()}:{started}"

    def _end_wait(*_):
        # Cleared first: a signal arriving while this records would otherwise take the record's
        # flock a second time in this process and hang.
        nonlocal waiting
        if waiting:
            waiting = False
            lk.record_wait(wait_dir, wait_key, started, time.time())

    job = lk.job_fields()   # inside a `ttp detach` job (its --lock): the label names the job and its pid
    tickets: list = []
    atexit.register(lambda: [lk.dequeue(t) for t in tickets])   # however this ends; a dead process's ticket is dropped too
    for L in todo:
        try:   # one arrival time on every lock: no two waiters can be ahead of each other on different ones
            L["ticket"] = lk.enqueue(L["where"], L["res"], L["who"], since=started, shared=a.shared,
                                     multi=len(todo) > 1)
        except OSError as e:
            _unwritable(e, L)
        tickets.append(L["ticket"])

    def _take() -> tuple[list | None, list]:
        # (the held slots of every lock, []) once all are this waiter's, else (None, [(lock, reserved for)]
        # of the ones it waits for). Several are taken only when the turn has come on each, and a slot
        # lost in the instant between is let go with the others: no subset is ever kept.
        # A lock reserved for this task (TTP_TASK) is its own driver's to take now, ahead of the queue the
        # reservation holds off, as the task's run would take it (runner._take_exclusive).
        out = []
        for L in todo:
            by = lk.reserved_by(L["mark"])
            L["mine"] = bool(by) and by == L["own"]
            by = None if L["mine"] else by
            if by or (len(todo) > 1 and not L["mine"]
                      and not lk.turn(L["where"], L["res"], L["ticket"], L["paths"], past_multi=past_multi)):
                out.append((L, by))
        if out:
            return None, out
        fs = []
        for L in todo:
            try:
                if L["mine"]:
                    f = (lk.try_take_shared if a.shared else lk.try_take)(L["paths"], L["who"], " ".join(cmd),
                                                                         extra=job)
                else:
                    f = lk.take_in_turn(L["where"], L["res"], L["ticket"], L["paths"], L["who"], " ".join(cmd),
                                        past_multi=past_multi, extra=job)
            except OSError as e:
                for g in fs:
                    g.close()
                _end_wait()
                _unwritable(e, L)
            if not f:
                for g in fs:
                    g.close()
                return None, [(L, None)]
            fs.append(f)
        return fs, []

    while True:
        _refuse_paused()
        fs, blocked = _take()
        tries += 1
        if fs and check and (tries > 1 or time.time() - asked_at >= PRE_GRANT_FRESH_S) \
                and (why := _pre_grant_refusal()):
            # Asked again holding the slots: a hold that began while this waited is never run over.
            for f in fs:
                f.close()
            _end_wait()
            for t in tickets:
                lk.dequeue(t)
            tickets.clear()
            die(why, 75)
        if fs:
            _end_wait()
            for t in tickets:
                lk.dequeue(t)
            tickets.clear()
            for L in todo:   # as the task's run does once it has its slot (runner._take_exclusive)
                if L["mine"]:
                    lk.unreserve(L["mark"], L["own"])
            waited = time.time() - started
            if waited > 5:
                print(f"ttp lock: got {what}{' (shared)' if a.shared else ''} after {waited / 60:.1f} min",
                      file=sys.stderr, flush=True)
            # The lock is held until the command itself has ended. A signal to this process (a
            # timeout, a cancel) is passed on to the command, and the lock is released only once
            # the command is gone, so nobody else ever gets the resource while it is still in use.
            proc = subprocess.Popen(cmd, env=child_env)

            def _forward(signum, _frame):
                try:
                    proc.send_signal(signum)
                except OSError:
                    pass

            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, _forward)

            def _wait() -> int:
                try:
                    return proc.wait()
                finally:
                    for f in fs:
                        f.close()

            sys.exit(_logged("shared" if a.shared else "lock", todo, _wait))
        if wait_dir and not waiting:
            # The run's wall clock stops while it waits here; the supervisor reads this record.
            # A signal ends the wait through _end_wait, so the record never stays open.
            lk.record_wait(wait_dir, wait_key, started, None)
            waiting = True
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda signum, _f: (_end_wait(), sys.exit(128 + signum)))
        if capped and timeout and time.time() - started > timeout and not lk.in_run(run_dir):
            # A driver started in the background (`nohup ... &`) outlives the run: the run's cap is not its own,
            # and its wait is no longer the run's.
            capped, timeout = False, a.timeout
            _end_wait()
            wait_dir = None
            print(f"ttp lock: not under the run's agent; waiting for {what} without the run's cap",
                  file=sys.stderr, flush=True)
        # Noted before any give-up: a wait that ends at once under load still shows as a wait, not a stall.
        if time.time() - told >= 120:
            def _why(L: dict, by) -> str:
                if by:
                    return f"{L['name']} (reserved for {by})"
                ahead = len(lk.queued(L["where"], L["res"])) - 1
                return (f"{L['name']}{' (shared)' if a.shared else ''} (held by "
                        f"{', '.join(lk.holders_now(L['paths'], p)) or 'another task'}"
                        f"{f'; {ahead} ahead in queue' if ahead > 0 else ''})")
            line = "waiting for " + "; ".join(_why(L, by) for L, by in blocked)
            print(f"ttp lock: {line}", file=sys.stderr, flush=True)
            if run_dir:
                try:
                    with open(run_dir / "progress.md", "a") as pf:
                        pf.write(f"{timefmt.clock()} {line}\n")
                except OSError:
                    pass
            told = time.time()
        if timeout and time.time() - started > timeout:
            _end_wait()
            die(f"{what} stayed busy for {timeout:.0f} s. Hand the task back now: result.json status "
                f"`waiting`, waiting_for `{what}`, retry_when "
                f"`ttp lock --probe{' --shared' if a.shared else ''} {what}`", 75)
        time.sleep(poll_s(3))

def cmd_land(a) -> None:
    """Land a reviewed head on a push branch that is a local branch of a code repo with no git remote
    (land.py): 0 landed, 3 a conflict, any other exit lands nothing."""
    from . import land
    p = here()
    if not p:
        die("ttp land: no project here (run it in a project or with TTP_PROJECT set)")
    sys.exit(land.run(p, a))


def cmd_landed(a) -> None:
    """Probe for start_when/retry_when: exit 0 once a commit, or a task's landing (`--task <id>`),
    is on the push branch, also as rebased commits; 1 while not yet; 2 when it cannot tell. The
    daemon runs a `landed:#<id>` probe as `ttp landed --task <id>` (see landed.py)."""
    from . import landed
    p = here()
    if not p:
        print("ttp landed: no project here (run it in a project or with TTP_PROJECT set)")
        sys.exit(landed.CANNOT)
    sys.exit(landed.run(p, a))


def cmd_ack_wait(a) -> None:
    """`ttp ack-wait probe|settle --from <project> --hash <h>`: wait model-free for another project's
    reply to a note (ackwait.py). probe: 0 once the reply is in this machine's inbox, 1 while not, 2
    when it cannot tell. settle (an on_pass_cmd): write the hand-off quoting the reply to $TTP_RESULT."""
    from . import ackwait
    p = here()
    if not p:
        print("ttp ack-wait: no project here (run it in a project or with TTP_PROJECT set)")
        sys.exit(ackwait.CANNOT)
    if a.frm == p.name or not (local_project(a.frm) or remote_entry(a.frm)):
        print(f"ttp ack-wait: {a.frm!r} is not another project of this user's; `ttp list` shows them")
        sys.exit(ackwait.CANNOT)
    try:
        n = ackwait.find(p.name, a.frm, a.hash)
    except ValueError as e:
        print(f"ttp ack-wait: {e}")
        sys.exit(ackwait.CANNOT)
    except OSError as e:
        print(f"ttp ack-wait: cannot read the inbox: {e}")
        sys.exit(ackwait.CANNOT)
    if not n:
        print(f"no reply from {a.frm} naming {a.hash.strip().lower()} yet")
        sys.exit(ackwait.WAITING)
    if a.action == "probe":
        print(f"{a.frm} replied: note {n['fp']}")
        sys.exit(ackwait.FOUND)
    out = os.environ.get("TTP_RESULT")
    if not out:
        die("ttp ack-wait settle is an on_pass_cmd: it writes the hand-off to $TTP_RESULT, which is not set",
            ackwait.CANNOT)
    write_json(Path(out), ackwait.handoff(n, a.frm, a.hash.strip().lower(), a.status))
    run_dir = os.environ.get("TTP_RUN_DIR") or os.environ.get(NOTE_RUN_ENV)
    if run_dir:
        durable_append(Path(run_dir) / "progress.md",
                       f"{time.strftime('%H:%M:%S')} {a.frm} replied to {a.hash.strip().lower()}: note {n['fp']}\n")
    print(f"{a.frm} replied (note {n['fp']}); hand-off {a.status} written to {out}")


def cmd_progress(a) -> None:
    """`ttp progress report <kind> --key <key> [--resource <name>] [--holder <text>] "<reason>"`: one JSON
    line in the project's state/progress.jsonl (progress.py). A refusal naming --resource with no --holder
    records the lock's holders now (progress.holder_text, "unknown" when unreadable). No daemon needed;
    exit 0 once given a project and a key, even when the line cannot be written, so a driver under
    `set -e` keeps going."""
    from . import progress
    ap = argparse.ArgumentParser(prog="ttp progress report")
    ap.add_argument("kind", choices=progress.KINDS)
    ap.add_argument("reason", nargs="?", default="")
    ap.add_argument("--key", default=a.key)
    ap.add_argument("--resource", default=getattr(a, "resource", None) or "")
    ap.add_argument("--holder", default=getattr(a, "holder", None) or "")
    a = ap.parse_intermixed_args(a.words or [])
    p = here()
    if not p:
        die("ttp progress report: no project here (run it in a project or with TTP_PROJECT set)", 2)
    key = a.key
    if not key and (job := os.environ.get(progress.JOB_ENV)):
        key = f"{os.environ.get('TTP_TASK') or 'job'}/{Path(job).stem}"
    if not (key or "").strip():
        die("ttp progress report: --key is required outside a `ttp detach` job", 2)
    holder = a.holder
    if not holder.strip() and a.kind == "refusal" and a.resource.strip():
        holder = progress.holder_text(p, a.resource.strip())
    rec = progress.record(a.kind, key, a.reason, resource=a.resource, holder=holder)
    try:
        progress.append(p.state, rec)
    except OSError as e:
        print(f"ttp progress report: not recorded ({e})", file=sys.stderr)


def _job_base_env(state: Path | None) -> dict | None:
    """The environment a worker's detached job inherits without asking: the daemon's own plus the caps
    the daemon gives every worker (testslots.worker_env: MAKEFLAGS, the test slot share), so the
    harness's build cap is no core request (jobuse.requested). None outside a run or when the daemon's
    environment cannot be read."""
    from . import jobuse, testslots
    base = jobuse.daemon_env(state / "daemon.pid") if state is not None else None
    if base is None:
        return None
    try:
        cfg = Project(state.parent).config()
    except Exception:
        cfg = None
    return {**base, **testslots.worker_env(cfg)}


def cmd_detach(a) -> None:
    """Start a long job that outlives this run: `ttp detach <name> -- <command...>`.

    Output goes to $TTP_RUN_DIR/<name>.log and the exit code to $TTP_RUN_DIR/<name>.rc once it
    ends. Hand off `waiting` with the retry_when it prints, `ttp detach --check <rc path>`: it exits 0
    once the job wrote exit code 0, 4 (detach.FAILED_RC) once it wrote another one or its process is
    gone without one (a kill, a reboot: it says why, see detach.state), else 1. On 4 the daemon runs
    the hand-off's on_pass_cmd model-free to decide, or wakes the task with the job's exit code. With
    `--rows <file> --after <n>` it exits 0 also once that file has more than n lines, and 0 (never 4)
    once a job ended or is gone, else 1: one probe that wakes on a new result row, the end or a dead
    driver; a path no `ttp detach` recorded is never gone then, only a new row wakes it. A series
    driver's own dir (<dir>/pid, optional <dir>/EXIT) runs while its pid lives and ends at EXIT or a dead pid. From a command a sandbox would end with everything it started,
    the daemon starts the job (detach.py).
    A run that ends without a hand-off after a detach is also brought back as waiting on its jobs. The
    resources an `exclusive:` task holds for its whole run stay held until its jobs end (hold.py), or
    until the host a `--remote-host` job drives reset or its remote pid died (detach.remote_verdict).
    `--resumable` records the job's argv, cwd and environment (no secrets) so the daemon can start it
    again after a host reboot cut it short, while its task still waits on it (daemon._relaunch_jobs).
    `--lock <resource>` (repeatable, or A,B) runs the job's command under one `ttp lock` of them all
    (_lock_job): the job takes them all or nothing before its command starts and holds them until the
    command ends, before its .rc is written, or its processes are gone (a job found lost).
    `--results <glob>` (relative to the job's working dir) declares the files the job writes per finished
    unit: kept in its detached.json entry (a relaunch keeps it), each new match counts as a result report
    from the job for its task's obligations (obligations._job_results). A task that owns an open
    obligation and starts a job without it is reminded to report results (_results_reminder).
    `--spend-cmd <cmd> --spend-cap <usd> [--spend-every <s>]` (together) has the daemon read a paid-API
    job's spend model-free and stop it at the cap (spendcap.py)."""
    from . import detach, jobuse
    from . import locks as lk
    from .push import _boxed, _own_ttp
    from .runner import boot_id
    if a.check is not None:   # `--check --host <alias> <path>` leaves the paths to the positionals
        a.check = [*a.check, *([a.name] if a.name else []), *(a.command or [])]
        _check_options(a)
        if not a.check:
            die("usage: ttp detach --check <rc path...>  |  ttp detach --check --host <ssh alias> <dir>/<name>...")
        rows, after = getattr(a, "rows", None), getattr(a, "after", None)
        if getattr(a, "mirror", None) and not a.host:
            die("--mirror goes with --host: it keeps a remote job's exit code and log tail here")
        if after is not None and rows is None:
            die("--after goes with --rows <file>: the check wakes once that file has more than --after lines")
        if (after or 0) < 0:
            die("--after must be 0 or more")
        if a.host:
            mirror = getattr(a, "mirror", None) or os.environ.get("TTP_RUN_DIR") or None
            sys.exit(_detach_check_remote(a.host, a.check, rows, after or 0, mirror))
    if getattr(a, "reap", None):
        sys.exit(_detach_reap(a.reap))
    if getattr(a, "stop", None):
        sys.exit(_detach_stop(a.stop))
    if a.check is None:
        _start_options(a)
    if a.check is None and (getattr(a, "rows", None) is not None or getattr(a, "after", None) is not None):
        die("--rows and --after go with --check")
    if a.check is None and getattr(a, "mirror", None):
        die("--mirror goes with --check --host")
    job_locks = list(dict.fromkeys(n for x in getattr(a, "lock", None) or [] for n in x.split(",") if n))
    if getattr(a, "lock", None) and (a.remote or a.check is not None):
        die("--lock is for a local job: it holds the locks here while the job runs")
    if getattr(a, "lock", None) and not job_locks:
        die("usage: ttp detach <name> --lock <resource> [--lock <resource>...] -- <command...>")
    if getattr(a, "resumable", False) and (a.remote or a.check is not None):
        die("--resumable is for a local job: a --remote one already outlives a reboot here")
    results = getattr(a, "results", None)
    if results is not None and (a.remote or a.check is not None):
        die("--results is for a local job: the daemon reads its result files here")
    if results is not None and (not results.strip() or Path(results).is_absolute()):
        die("--results needs a glob relative to the job's working dir, e.g. 'out/*.json'")
    from . import spendcap
    spend_opts = (getattr(a, "spend_cmd", None), getattr(a, "spend_cap", None), getattr(a, "spend_every", None))
    if any(x is not None for x in spend_opts) and (a.remote or a.check is not None):
        die("--spend-cmd, --spend-cap and --spend-every are for a local job: the daemon here reads its spend")
    if why := spendcap.options_error(*spend_opts):
        die(why)
    rhost = getattr(a, "remote_host", None)
    if (getattr(a, "remote_pid", None) or getattr(a, "remote_marker", None)) and not rhost:
        die("--remote-pid and --remote-marker go with --remote-host <ssh alias>")
    if rhost and (a.remote or a.check is not None):
        die("--remote-host is for a local job that drives another host; --remote starts the job there itself")
    if a.remote:
        _detach_remote(a, _own_ttp())
        return
    if a.check is not None:
        ended, failed, one_ended = True, False, False
        for rc in map(Path, a.check):
            rec = detach.recorded(rc)
            ser = None if rec else detach.series(rc)   # a series driver's own dir, read by its pid and EXIT
            if ser is None and rows is not None and not rec:   # no driver to read: only a new row wakes it
                ended = False
                print(f"{rc.stem}: no detached job recorded at {rc}; waking only on a new row in {rows}")
                continue
            st, why = ser or detach.state(rc)
            if st == "ended":
                failed = failed or why != "0"
                print(f"{rc.stem}: job finished rc={why}")
            elif st == "lost":
                failed = True
                print(f"{rc.stem}: gone without an exit code ({why})" + ("" if ser else f"; see {rc.with_suffix('.log')}"))
            else:
                ended = False
                print(f"{rc.stem}: {'queued for the daemon, which starts it outside the sandbox' if st == 'queued' else 'running'}")
            one_ended = one_ended or st in ("ended", "lost")
        if rows is not None:
            sys.exit(_rows_verdict(_count_rows(Path(rows)), rows, after or 0, one_ended))
        sys.exit(1 if not ended else detach.FAILED_RC if failed else 0)
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not cmd or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.name or ""):
        die("usage: ttp detach <name> -- <command...>  |  ttp detach --check <rc path...>   "
            "(name: letters, digits, . _ -)")
    if not run_dir:
        die("ttp detach only works inside a tt-project run")
    rd = Path(run_dir).resolve()
    rc, logf = rd / f"{a.name}.rc", rd / f"{a.name}.log"
    if any(f.exists() for f in (rc, logf, detach.queued(rc), detach.lost(rc))):
        die(f"a detached job named {a.name} already ran in this run; pick another name")
    asked = cmd
    if job_locks:
        cmd = _lock_job(job_locks, cmd)
    pid = None
    from . import activity
    job_env, aid = dict(os.environ), activity.new_id()
    from .progress import JOB_ENV
    job_env[JOB_ENV] = str(rc)   # its `ttp progress report` lines name the job (progress.py)
    state = rd.parent.parent if rd.parent.name == "runs" else None
    if state is not None:   # the wrapper appends the job's end to the project's activity log
        job_env.update({activity.ENV_LOG: str(activity.log_path(state)), activity.ENV_ID: aid})
    if getattr(a, "resumable", False):   # before it starts: a reboot right after still finds it
        detach.save_resume(rc, logf, cmd, os.getcwd(), job_env, boot_id())
    if _boxed() and state is not None:   # this command's sandbox would end the job: the daemon starts it
        detach.enqueue(state, rc, logf, cmd, os.getcwd(), job_env)
    else:
        # The job inherits this lock and holds it while any of its processes lives, so a job gone
        # without an .rc is told apart from one still running (detach.state).
        lock = lk.try_take([lk.job_lock(rc)], f"detached job {a.name}", " ".join(cmd))
        if lock is None:
            die(f"a detached job named {a.name} is still running in this run; pick another name")
        try:
            pid = detach.start(rc, logf, cmd, os.getcwd(), job_env, lock)
        finally:
            lock.close()
    remote = None
    if rhost:   # after the start: a slow or dead host never delays the job (detach.remote_verdict)
        remote = {"host": rhost, "boot": detach.remote_start(rhost), "pid": getattr(a, "remote_pid", None) or "",
                  "marker": getattr(a, "remote_marker", None) or ""}
        if not remote["boot"]:
            print(f"ttp detach: {rhost} did not answer; its boot id is recorded at the first answer", file=sys.stderr)
    if state is not None:
        activity.append(state, activity.base(Project(state.parent).name, ev="job", id=aid, job=a.name,
                                             rcfile=str(rc), cmd=activity.head(cmd), start=time.time()))
    with detach.registry(rd) as jobs:
        jobs.append({"name": a.name, "rc": str(rc), "log": str(logf), "pid": pid, "started": time.time(),
                 "boot": boot_id(), "queued": pid is None, "status": "running" if pid else "queued",
                 "command": " ".join(cmd)[:300],
                 **({"resumable": True} if getattr(a, "resumable", False) else {}),
                 **({"remote": remote} if remote else {}),
                 **({"locks": job_locks} if job_locks else {}),
                 **({"results": results, "cwd": os.getcwd()} if results else {}),
                 **({"spend": spendcap.entry(*spend_opts)} if spend_opts[0] is not None else {}),
                 "requested": jobuse.requested(asked, os.environ, getattr(a, "cores", None),
                                               getattr(a, "devices", None), getattr(a, "mem_gb", None),
                                               _job_base_env(state))})
    how = (f"(pid {pid})" if pid else
           "(queued: this command's sandbox would end it, so the daemon starts it within seconds)")
    print(f"detached {a.name} {how}\nlog: {logf}\nrc:  {rc}\n"
          + (f"locks: {','.join(job_locks)} (taken all or nothing before the command starts, held until it "
             f"ends; a paused one ends the job with exit 75)\n" if job_locks else "")
          + (f"spend cap: ${spend_opts[1]:.2f}, read every {spendcap.entry(*spend_opts)['every']} s by the daemon\n"
             if spend_opts[0] is not None else "")
          + (f"results: each new file matching {results} under {os.getcwd()} counts as a result report\n"
             if results else _results_reminder(state))
          + f"hand off: status waiting, retry_when \"{lk.job_probe([rc], _own_ttp())}\"")


def _results_reminder(state: Path | None) -> str:
    """The start notice's reminder for a task that owns an open obligation and started a job with no
    --results: a live job that reports no result leaves the obligation overdue. "" otherwise."""
    task = os.environ.get("TTP_TASK") or ""
    if state is None or not task.isdigit():
        return ""
    try:
        from . import obligations
        owned = obligations.owned_open(Project(state.parent).db, int(task))
    except Exception:   # an unreadable database only loses the reminder
        return ""
    if not owned:
        return ""
    return (f"obligation {', '.join(owned)}: this job keeps it on time only while it reports results. Have "
            f"it run `ttp progress report result --key <k> \"<what>\"` per finished unit, or start it with "
            f"`--results <glob>` matching the file it writes per unit\n")


def _detach_reap(what: str) -> int:
    """`ttp detach --reap <name or .rc path>`: close the job's record now, as the daemon's sweep would
    (detach.close): 0 once it is closed (again: a closed one stays as it was), 1 while its driver is alive
    or queued, 2 when no record names it. A name is a job of $TTP_RUN_DIR."""
    from . import detach
    rc = Path(what) if what.endswith(".rc") or "/" in what else \
        Path(os.environ.get("TTP_RUN_DIR") or die("--reap <name> needs $TTP_RUN_DIR; give the .rc path")) / f"{what}.rc"
    rc = rc.resolve()
    if not detach._entry(rc):
        print(f"ttp detach --reap: no detached job is recorded at {rc}", file=sys.stderr)
        return 2
    e = detach.close(rc)
    if e is None:
        st, _ = detach.state(rc)
        print(f"ttp detach --reap: {rc.stem} is still {st}; its record closes once it is over", file=sys.stderr)
        return 1
    ex = "?" if e.get("exit") is None else e["exit"]
    print(f"{rc.stem}: {e['status']} rc={ex} at {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(e['ended']))}"
          + (f" ({e['why']})" if e.get("why") else ""))
    return 0


def _detach_stop(what: str) -> int:
    """`ttp detach --stop <rc>`: stop a running job as the daemon stops those of ended tasks (jobstop.end):
    0 once stopped or when it no longer runs, 2 when no record names it, 3 when refused (jobstop.may_stop).
    A sandboxed caller, which cannot signal it, queues the stop for the daemon."""
    from . import detach, jobstop
    from .push import _boxed
    rc = Path(what).expanduser().resolve()
    if not detach._entry(rc):
        print(f"ttp detach --stop: no detached job is recorded at {rc}", file=sys.stderr)
        return 2
    base = rc.parent.parent.parent.parent if rc.parent.parent.name == "runs" else None
    if base is None or not (base / "state").is_dir():
        print(f"ttp detach --stop: {rc} is not a job of a tt-project run", file=sys.stderr)
        return 2
    p = Project(base)
    if not jobstop.running(rc):
        print(f"{rc.stem}: not running; nothing to stop")
        return 0
    caller = os.environ.get("TTP_TASK") or ""
    ok, why = jobstop.may_stop(p, rc, caller)
    if not ok:
        print(f"ttp detach --stop: {why}", file=sys.stderr)
        return 3
    if _boxed():
        jobstop.request(p, rc, caller)
        print(f"{rc.stem}: queued; the daemon stops it within seconds ({why})")
        return 0
    t = jobstop.task_of(p, rc)
    r = jobstop.end(p, rc, f"stopped by `ttp detach --stop` ({why})", t["id"] if t else None)
    print(r["text"] if r else f"{rc.stem}: not running; nothing to stop")
    return 0


def _lock_job(names: list, cmd: list) -> list:
    """`ttp detach --lock A --lock B`: the job's command as `ttp lock A,B -- <command...>`, so the job takes
    them by ttp lock's rules unchanged (all or nothing, in arrival order, device, reserved and nested rules;
    a detached job's wait has no run cap) and holds them for the command's life; its label names the task,
    run and job (locks.job_fields). A paused one refuses the job here at once, exit 75, as ttp lock does."""
    from . import lockprobe
    base = os.environ.get("TTP_PROJECT")
    if not base:
        die("ttp detach --lock only works inside a tt-project run (or with TTP_PROJECT set)")
    p = Project(base)
    cfg = p.config()
    for n in names:
        if (held := lockprobe.pause(p, cfg, n)) is not None:
            why = f" ({held['reason']})" if held.get("reason") else ""
            die(f"{n} is paused{why}, so the job did not start; hand the task back as waiting, retry_when "
                f"`ttp lock --probe {','.join(names)}`", 75)
    return [sys.executable, str(Path(__file__).resolve().parents[2] / "bin" / "ttp"), "lock", ",".join(names), "--", *cmd]


# Sets $b to the host's boot id, as the remote side of `ttp detach --remote` records it (below) and
# detach.remote_liveness reads it for a local job's --remote-host.
REMOTE_BOOT_SH = r'''b=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
[ -n "$b" ] || b=$(sysctl -n kern.bootsessionuuid 2>/dev/null)
[ -n "$b" ] || b=$(sysctl -n kern.boottime 2>/dev/null | sed -n "s/^{ sec = \([0-9]*\),.*/\1/p")
'''

# The remote side of `ttp detach --remote`, run by POSIX sh over ssh. The driver's wrapper records its
# own pid and its process start (field 22 of /proc/<pid>/stat; without /proc, as on macOS, ps's lstart),
# next to the host's boot_id (else kern.bootsessionuuid, else kern.boottime's seconds, which moves when the
# clock is stepped): a pid alive with another start, or a boot that changed, is a driver that is gone even
# with no .rc. A .boot holding kern.boottime's seconds, as older versions wrote, still names its boot.
# A driver gone with no .rc reads "boot" (it died with the host reboot), "gone same" (same boot: it
# crashed or was killed) or "gone unknown" (no boot id recorded or readable now: either).
_REMOTE_START = r'''d=$1 n=$2; shift 2
case $d in "~"|"~/"*) d=$HOME${d#\~} ;; esac
mkdir -p "$d" || exit 3
p=$d/$n
if [ -e "$p.log" ] || [ -e "$p.rc" ] || [ -e "$p.pid" ]; then echo "exists $p"; exit 4; fi
''' + REMOTE_BOOT_SH + r'''if [ -n "$b" ]; then echo "$b"; fi >"$p.boot"
setsid nohup sh -c 'p=$1; shift
s=$(sed "s/.*) //" /proc/$$/stat 2>/dev/null | cut -d" " -f20)
[ -n "$s" ] || s=$(ps -o lstart= -p $$ 2>/dev/null | tr -s " " _)
echo "$(date +%s) $s" >"$p.start"
echo $$ >"$p.pid.tmp"; mv "$p.pid.tmp" "$p.pid"
"$@" >"$p.log" 2>&1 </dev/null; c=$?; echo $c >"$p.rc.tmp"; mv "$p.rc.tmp" "$p.rc"' sh "$p" "$@" \
  >/dev/null 2>&1 </dev/null &
i=0; while [ ! -s "$p.pid" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
[ -s "$p.pid" ] || { echo "nopid $p"; exit 5; }
echo "started $(cat "$p.pid") $p"
'''

_REMOTE_CHECK = r'''p=$1
case $p in "~"|"~/"*) p=$HOME${p#\~} ;; esac
p=${p%/}; p=${p%.rc}
if [ -e "$p.rc" ]; then echo "rc $(cat "$p.rc")"; exit 0; fi
if [ ! -s "$p.pid" ] && [ -d "$p" ] && { [ -e "$p/EXIT" ] || [ -s "$p/pid" ]; }; then
  if [ -e "$p/EXIT" ]; then
    c=$(tr -d " \t\r\n" <"$p/EXIT" 2>/dev/null | head -c 16)
    case $c in ""|*[!0-9]*) echo "rc" ;; *) echo "rc $c" ;; esac; exit 0
  fi
  pid=$(cat "$p/pid")
  if kill -0 "$pid" 2>/dev/null || ps -p "$pid" >/dev/null 2>&1; then echo "running $pid"; else echo "series"; fi
  exit 0
fi
if [ ! -s "$p.pid" ]; then echo "none $p"; exit 0; fi
k=unknown
if [ -s "$p.boot" ]; then
  r=$(cat "$p.boot") s=
  b=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
  if [ -z "$b" ]; then
    b=$(sysctl -n kern.bootsessionuuid 2>/dev/null)
    s=$(sysctl -n kern.boottime 2>/dev/null | sed -n "s/^{ sec = \([0-9]*\),.*/\1/p")
  fi
  if [ -n "$b$s" ]; then
    if [ "$r" != "${b:-$s}" ] && [ "$r" != "$s" ]; then echo "boot"; exit 0; fi
    k=same
  fi
fi
pid=$(cat "$p.pid")
if kill -0 "$pid" 2>/dev/null; then
  st=$(sed "s/.*) //" /proc/$pid/stat 2>/dev/null | cut -d" " -f20)
  [ -n "$st" ] || st=$(ps -o lstart= -p "$pid" 2>/dev/null | tr -s " " _)
  want=$(cut -d" " -f2 "$p.start" 2>/dev/null)
  if [ -z "$st" ] || [ -z "$want" ] || [ "$st" = "$want" ]; then echo "running $pid"; exit 0; fi
fi
if [ -e "$p.rc" ]; then echo "rc $(cat "$p.rc")"; exit 0; fi
echo "gone $k"
'''


def _start_options(a) -> None:
    """`ttp detach <name> --devices 1 -- <command...>`: argparse leaves the options given after the
    name among the command's words; take them back out up to the first `--` or the first word that is
    no option, so they count as if they came before the name. Any other option there is refused, so it
    never runs as the command."""
    words = list(a.command or [])
    known = {"--cores": int, "--devices": int, "--mem-gb": float, "--resumable": None, "--lock": list,
             **({"--dir": str} if a.remote else {"--remote-host": str, "--remote-pid": str, "--remote-marker": str,
                                                  "--results": str, "--spend-cmd": str, "--spend-cap": float,
                                                  "--spend-every": int})}
    while words and words[0].startswith("-") and words[0] != "--":
        w = words.pop(0)
        key, eq, val = w.partition("=")
        if key not in known:
            die(f"ttp detach: unknown option {w} before the command (put the command after --)")
        kind = known[key]
        if kind is None:
            if eq:
                die(f"{key} takes no value")
            a.resumable = True
            continue
        if not eq:
            if not words or words[0] == "--":
                die(f"{key} needs a value")
            val = words.pop(0)
        if kind is list:   # repeatable
            a.lock = [*(getattr(a, "lock", None) or []), val]
            continue
        try:
            setattr(a, key[2:].replace("-", "_"), kind(val))
        except ValueError:
            die(f"{key} needs a {'whole ' if kind is int else ''}number, not {val!r}")
    a.command = words


def _check_options(a) -> None:
    """`ttp detach --check`: argparse leaves the options given after the first path (`--rows`,
    `--after`, `--host`, `--mirror`) among the paths; take them back out, so they count wherever they appear."""
    paths, words = [], list(a.check)
    while words:
        w = words.pop(0)
        key, eq, val = w.partition("=")
        if key in ("--rows", "--after", "--host", "--mirror"):
            if not eq:
                if not words:
                    die(f"{key} needs a value")
                val = words.pop(0)
            if key == "--after":
                try:
                    val = int(val)
                except ValueError:
                    die(f"--after needs a whole number, not {val!r}")
            setattr(a, key[2:], val)
        elif w.startswith("-"):
            die(f"ttp detach --check: unknown option {w}")
        else:
            paths.append(w)
    a.check = paths


# Run before _REMOTE_CHECK with `--rows`: the line count of the rows file ($2), "rows none" without one.
_REMOTE_ROWS = r'''f=$2
case $f in "~"|"~/"*) f=$HOME${f#\~} ;; esac
if [ -e "$f" ]; then echo "rows $(awk 'END{print NR}' "$f" 2>/dev/null)"; else echo "rows none"; fi
'''


def _count_rows(path: Path) -> int | None:
    """The lines in a rows file (a last line without a newline counts), None when it does not exist."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _rows_verdict(n: int | None, rows: str, after: int, one_ended: bool, where: str = "") -> int:
    """`ttp detach --check --rows <file> --after <n>`: 0 once the file has more than `after` lines or a
    job it checks ended or is gone (its driver dead: killed, the host restarted), else 1."""
    have = "no file yet" if n is None else f"{n} line{'s' if n != 1 else ''}"
    print(f"rows: {have} in {rows}{where} (wakes above {after})")
    return 0 if one_ended or (n or 0) > after else 1


def _ssh_sh(host: str, script: str, args: list, timeout: float = 60) -> subprocess.CompletedProcess | None:
    """Run `script` with sh on `host` over ssh, with `args` as its $1...; None when ssh cannot run."""
    remote = "sh -c " + " ".join(shlex.quote(x) for x in (script, "sh", *map(str, args)))
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, remote],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _detach_remote(a, ttp: str) -> None:
    """`ttp detach --remote <ssh alias> [--dir <remote dir>] <name> -- <command...>`: start the driver
    on that host with setsid nohup, so it outlives this run, ssh and a reboot of this host. <name>.log,
    .rc, .pid, .boot and .start go to the remote folder (default ~/.ttp-detach/<run>); the retry_when
    it prints, `ttp detach --check --host <alias> <dir>/<name>`, exits 0 once the .rc holds 0, 4 once it
    holds another exit code or the driver is gone (pid dead, or the host restarted: the .boot recorded at
    launch tells a host reboot from a crash, _GONE_WHY), 1 while it runs and 255 when ssh fails."""
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.name or "") or a.remote.startswith("-"):
        die("usage: ttp detach --remote <ssh alias> [--dir <remote dir>] <name> -- <command...>   "
            "(name: letters, digits, . _ -)")
    run = Path(os.environ.get("TTP_RUN_DIR") or "").name or time.strftime("manual-%Y%m%d-%H%M%S")
    rdir = (a.dir or f"~/.ttp-detach/{run}").rstrip("/") or "/"
    r = _ssh_sh(a.remote, _REMOTE_START, [rdir, a.name, *cmd])
    if r is None or r.returncode == 255:
        die(f"ssh to {a.remote} failed{': ' + r.stderr.strip()[-300:] if r and r.stderr.strip() else ''}", 255)
    out = (r.stdout.strip().splitlines() or [""])[-1].split(" ", 2)
    if r.returncode == 4:
        die(f"a detached job named {a.name} already ran in {rdir} on {a.remote}; pick another name")
    if r.returncode or out[0] != "started" or len(out) < 3:
        die(f"could not start {a.name} on {a.remote} (exit {r.returncode}): "
            f"{(r.stdout + r.stderr).strip()[-300:]}")
    pid, prefix = out[1], out[2]
    from . import activity, detach
    mirror = ""
    if os.environ.get("TTP_RUN_DIR") and Path(os.environ["TTP_RUN_DIR"]).is_dir():   # its rc and log tail come back here
        rd = Path(os.environ["TTP_RUN_DIR"]).resolve()
        try:
            detach.mirror_save(rd, a.name, a.remote, prefix)
            mirror = f" --mirror {shlex.quote(str(rd))}"
        except OSError:
            pass
    if p := here():   # its end is known only on that host: the log keeps the start
        activity.append(p.state, activity.base(p.name, ev="job", id=activity.new_id(), job=a.name,
                                               machine=a.remote, cmd=activity.head(cmd), start=time.time()))
    print(f"detached {a.name} on {a.remote} (pid {pid})\nlog: {prefix}.log\nrc:  {prefix}.rc\n"
          f"hand off: status waiting, survives_reboot true, retry_when "
          f"\"{ttp} detach --check --host {shlex.quote(a.remote)}{mirror} {shlex.quote(prefix)}\"")


# Why a remote driver is gone without an exit code, by what its host's boot id says (_REMOTE_CHECK).
_GONE_WHY = {"boot": "driver died with the host reboot: the host's boot id changed since launch",
             "same": "the driver crashed or was killed: same boot, so not a host reboot",
             "unknown": "killed, or the host restarted: the boot id could not be read, so which is unknown"}


def _detach_check_remote(host: str, prefixes: list, rows: str | None = None, after: int = 0, mirror=None) -> int:
    """`ttp detach --check --host <alias> <dir>/<name>...`: 0 once every job ended with exit 0, 4
    (detach.FAILED_RC) once every one ended or is gone but one failed or left no exit code, 1 while
    any runs, 255 when ssh fails (retry_when keeps the task asleep), 2 on an answer it cannot read.
    With `rows` (a file on that host): 0 once it has more than `after` lines or one job ended or is
    gone, else 1 (_rows_verdict); a path with no driver recorded (no .rc, no .pid) is never read as
    gone then, only a new row wakes it; ssh failing is still 255. A series driver's own dir
    (<dir>/pid, optional <dir>/EXIT holding its exit code) is read by those: running while the pid
    lives, ended once EXIT exists, gone once the pid is dead. With `mirror` (a local folder) each answer's
    log tail and end are copied there (detach.mirror_probe), and a job whose end was mirrored reads as
    that end when ssh fails, not 255."""
    from .detach import FAILED_RC, mirror_end, mirror_name, mirror_probe, mirror_read, mirror_save
    ended, failed, one_ended, n = True, False, False, None
    for i, pre in enumerate(prefixes):
        name = mirror_name(pre)
        count = rows is not None and i == 0   # the rows file is read once, in the first job's ssh call
        r, lines = mirror_probe(host, pre, mirror, rows if count else None)
        if r is None or r.returncode == 255:
            print(f"{name}: ssh to {host} failed{': ' + r.stderr.strip()[-200:] if r and r.stderr.strip() else ''}")
            kept = mirror_read(mirror, name, host, pre) if mirror else None
            end = (kept or {}).get("end") or {}
            if not end:
                if kept and (Path(mirror) / f"{name}.remote.log").exists():
                    print(f"{name}: its last log tail mirrored here: {Path(mirror) / f'{name}.remote.log'}")
                return 255
            failed = failed or end.get("rc") != "0"
            one_ended = True
            print(f"{name}: " + (f"job finished rc={end['rc']}" if "rc" in end else f"gone without an exit code ({end.get('gone')})")
                  + f", as mirrored before {host} stopped answering; log tail: {Path(mirror) / f'{name}.remote.log'}")
            continue
        if count:
            got = (lines[0] if lines else "").split(" ", 1)
            if got[0] != "rows" or len(got) < 2:
                print(f"{name}: unreadable answer from {host} (exit {r.returncode}): {(r.stdout + r.stderr).strip()[-200:]}")
                return 2
            n = None if got[1].strip() == "none" else int(got[1]) if got[1].strip().isdigit() else 0
        out = (lines or [""])[-1].split(" ", 1)
        if r.returncode or out[0] not in ("rc", "none", "boot", "running", "gone", "series"):
            print(f"{name}: unreadable answer from {host} (exit {r.returncode}): {(r.stdout + r.stderr).strip()[-200:]}")
            return 2
        if mirror and (end := mirror_end(lines[-1])):
            try:
                mirror_save(mirror, name, host, pre, end=end)
            except OSError:
                pass
        if out[0] == "rc":
            code = out[1].strip() if len(out) > 1 and out[1].strip() else "?"
            failed = failed or code != "0"
            print(f"{name}: job finished rc={code}")
        elif out[0] == "running":
            ended = False
            print(f"{name}: running on {host}")
        elif out[0] == "none" and rows is not None:   # no driver to read: only a new row wakes it
            ended = False
            print(f"{name}: no driver recorded at {pre} on {host}; waking only on a new row in {rows}")
            continue
        elif out[0] == "none":
            failed = True
            print(f"{name}: gone without an exit code: no driver was recorded at {pre} on {host}")
        elif out[0] == "series":
            failed = True
            print(f"{name}: series driver gone without an EXIT (its pid is dead); see {pre.removesuffix('.rc')}/log on {host}")
        else:
            failed = True
            why = _GONE_WHY.get(out[-1].strip(), _GONE_WHY["unknown"])   # "boot", "gone same", "gone unknown"
            print(f"{name}: gone without an exit code ({why}); see {pre}.log on {host}")
        one_ended = one_ended or out[0] != "running"
    if rows is not None:
        return _rows_verdict(n, rows, after, one_ended, f" on {host}")
    return 1 if not ended else FAILED_RC if failed else 0


def cmd_devq(a) -> None:
    """The project's serial device-job runners (config `device.runners.<name>`; see devq.py).

    `ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] [--workdir <dir>] [--input <path>]...
    [--cold-start <s>] [--artifact <path>] [--wedge-pattern <regex>]... [--no-wedge-check] [--no-lint] -- <command>`
    lints the
    command first (devq.lint: bash -n, the scripts it calls exist on the host, a task id not this task's;
    --no-lint skips it; --wedge-pattern replaces the runner's wedge_patterns for this job and
    --no-wedge-check turns them off), then
    queues one job on the runner's host (whose job guard, devq.guard, may refuse it; it checks the --input
    paths are readable there and warns on a --cold-start longer than the job's limit; it stamps the job with the
    workdir's commit, dirty flag and the built --artifact, else the runner's build_artifact: devq.build_stamp)
    and starts the runner if it is down (with runner.device_timeout_max_s
    set, it refuses a longer --timeout and gives a job without one that ceiling); it prints the retry_when,
    `ttp devq probe <runner> <id>`, which exits 0 once the job has its done marker, 1 while it runs and
    3 while it waits with no runner alive (the daemon then runs `ttp devq start <runner>` itself, model-free,
    and the task sleeps on: daemon._restart_runner). `status <runner> [<id>]` shows the queue or one job's marker; `clear <runner> <config>`
    allows a config skipped for its drops again; `list` names the runners."""
    from . import devq
    p = here()
    runners = (((p.config().get("device") or {}).get("runners")) or {}) if p else {}
    if a.op == "list":
        if not runners:
            print("no device runners configured (config device.runners)")
        for name, rc in sorted(runners.items()):
            s = devq.settings(rc)
            print(f"{name}\t{s['host'] or 'this machine'}:{devq.runner_dir(name, s)}")
        return
    if not p:
        die("no tt-project project here")
    if not a.runner or a.runner not in runners:
        die(f"no device runner named {a.runner!r}; configured: {', '.join(sorted(runners)) or 'none'}")
    rc = runners[a.runner]
    bad = devq.config_problems(a.runner, rc)
    if bad:
        die("; ".join(bad))
    cfg_json = json.dumps(devq.settings(rc))
    rest = list(a.rest or [])
    if a.op == "submit":
        usage = ("usage: ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] [--workdir <dir>] "
                 "[--input <path>]... [--cold-start <s>] [--artifact <path>] [--wedge-pattern <regex>]... "
                 "[--no-wedge-check] "
                 "[--no-lint] -- <command...>")
        opts, rest = (rest[:rest.index("--")], rest[rest.index("--") + 1:]) if "--" in rest else (rest, [])
        sp = argparse.ArgumentParser(prog="ttp devq submit", add_help=False)
        for flag in ("--id", "--config", "--workdir", "--artifact"):
            sp.add_argument(flag, default="")
        sp.add_argument("--timeout", type=int, default=0)
        sp.add_argument("--input", action="append", default=[])
        sp.add_argument("--cold-start", type=int, default=0)
        sp.add_argument("--no-lint", action="store_true")
        sp.add_argument("--wedge-pattern", action="append", default=[])
        sp.add_argument("--no-wedge-check", action="store_true")
        try:
            o, extra = sp.parse_known_args(opts)
        except SystemExit:
            die(usage)
        if not o.id or not rest or extra:
            die(usage)
        a.id = o.id
        timeout = o.timeout
        ceiling = device_timeout_max(p.config())[0]
        if ceiling and timeout > ceiling:
            die(f"devq submit: --timeout {timeout} is above the project's device-job ceiling of {ceiling} s "
                f"(runner.device_timeout_max_s); split the run into shorter jobs")
        if ceiling and not timeout and not 0 < float(devq.settings(rc)["job_timeout_s"] or 0) <= ceiling:
            timeout = ceiling
            print(f"devq submit: no --timeout; the job gets the project's device-job ceiling of {ceiling} s")
        spec = {"id": o.id, "config": o.config or o.id, "cmd": rest[0] if len(rest) == 1 else shlex.join(rest),
                "task": os.environ.get("TTP_TASK", ""), "workdir": o.workdir, "timeout_s": timeout,
                "inputs": o.input, "cold_start_s": o.cold_start}
        if o.artifact:
            spec["artifact"] = o.artifact
        if o.wedge_pattern and o.no_wedge_check:
            die("devq submit: --wedge-pattern and --no-wedge-check exclude each other")
        if o.wedge_pattern or o.no_wedge_check:
            spec["wedge_patterns"] = o.wedge_pattern
            bad = devq.pattern_problems(o.wedge_pattern)
            if bad:
                die("devq submit: --wedge-pattern: " + "; ".join(bad))
        if not o.no_lint:
            errors, warnings = devq.lint(spec["cmd"], rc, o.id, o.workdir, spec["task"])
            for w in warnings:
                print(f"devq submit: warning: {w}", file=sys.stderr)
            if errors:
                die("devq submit: refused, the job would fail at once: " + "; ".join(errors)
                    + " (fix the command, or pass --no-lint if this check is wrong)")
        args, install, limit = [cfg_json, json.dumps(spec)], True, 120
    elif a.op == "start":
        args, install, limit = [cfg_json], True, 120
    elif a.op in ("probe", "clear"):
        if len(rest) != 1:
            die(f"usage: ttp devq {a.op} <runner> <{'id' if a.op == 'probe' else 'config'}>")
        args, install, limit = rest, False, 50 if a.op == "probe" else 120
    else:
        args, install, limit = rest[:1], False, 120
    argv = devq.host_call(a.runner, rc, a.op, args, install)
    try:
        r = subprocess.run(argv, input=devq.source(), text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"ttp devq: the runner's host did not answer within {limit} s", file=sys.stderr)
        sys.exit(75 if a.op == "probe" else 1)
    if a.op == "submit" and r.returncode == 0:
        remote = bool(devq.settings(rc)["host"])
        print(f"hand off: status waiting, retry_when \"{_own_ttp_word(p)} devq probe {a.runner} {a.id}\", "
              f"wake_tier light{', survives_reboot true' if remote else ''}")
    sys.exit(r.returncode)


def _own_ttp_word(p: Project | None) -> str:
    from .push import _own_ttp
    return _own_ttp(p)


# operating ----------------------------------------------------------------------------------------
def cmd_ci(a) -> None:
    """CI probe for `retry_when`: exit 0 once the commit's GitHub Actions runs completed or one of
    their jobs hung (in progress past 3x its recent median), 1 while they run, 75 when gh cannot
    answer. Prints one line per run (`done:`, `hung:`, `running:`); see ciwait."""
    from . import ciwait
    rc, lines = ciwait.probe(repo=a.repo, branch=a.branch, sha=a.sha, workflow=a.workflow, run=a.run,
                             hang=not a.no_hang, factor=a.factor, floor_min=a.floor_min, default_min=a.default_min)
    for line in lines:
        print(line)
    sys.exit(rc)


def cmd_list(a) -> None:
    if getattr(a, "status", False):
        return cmd_overview(a)
    reg = load_registry().get("projects", {})
    if not reg:
        print("no projects registered on this machine")
    for name, e in sorted(reg.items()):
        print(f"{name}\t{e.get('host')}:{e.get('dir')}")


def cmd_overview(a) -> None:
    """Every project on this machine at a glance: daemon, harness version, asks, waiting, running,
    spend today; other projects are only read (overview.py)."""
    from . import overview as ov
    data = ov.overview()
    print(json.dumps(data, indent=1, default=str) if getattr(a, "json", False) else ov.text(data))


def cmd_deployed(a) -> None:
    """Probe for a deploy (and a retry_when): exit 0 once every project `ttp list` shows on this machine
    runs the target version and commit by its daemon's own receipt and completed a tick since; 1 while
    any is pending (a deferred merge, a restart not yet done), one line each with owner and resume time;
    2 when it cannot tell. Read only unless --after-push records owned pending merges and alerts
    in the reporting project (done/pending exit 0, failed exit 1)."""
    from . import overview as ov
    names = [n.strip() for n in (a.projects or "").split(",") if n.strip()] or None
    data = ov.deployed(a.version, a.commit, names, a.fresh_s, allow_newer=a.after_push)
    if a.after_push:
        from . import deploy
        p = here()
        if not p:
            die("--after-push needs a reporting project (TTP_PROJECT or the current folder)")
        data = deploy.record(p, data)
    print(json.dumps(data, indent=1, default=str) if a.json else data["text"])
    sys.exit(data["rc"])


def cmd_find(a) -> None:
    p, entry = resolve(a.name, full=True)
    if p:
        print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    elif entry:
        print(MARKER.format(name=a.name, host=entry["host"], dir=entry["dir"]))
    else:
        die(f"not found in the registry or in this machine's chat logs. Ask the user where it runs, "
            f"then `ttp adopt {a.name} --host HOST --dir DIR`", 1)


def cmd_adopt(a) -> None:
    from . import machines
    # Another name for this machine (an alias, its FQDN) is stored as its own short name.
    host = a.host if a.host and not machines.names_here(a.host) else hostname()
    register(a.name, {"host": host, "dir": a.dir})
    print(MARKER.format(name=a.name, host=host, dir=a.dir))


TASK_LIST_LIMIT = 50   # newest tasks `ttp task list` shows when given no --status, --all or --limit


def list_tasks(p, a) -> tuple[list[dict], int, int | None]:
    """The rows `ttp task list` shows, newest first, how many matched and the cap applied (None: none).
    --status filters on the shown status (changes_needed included) before any cap, and with no --limit
    returns every match; with no filter only the newest TASK_LIST_LIMIT are shown unless --all."""
    cols = "SELECT id,status,kind,result,tier,title,labels FROM tasks"
    want = {s.strip() for s in (a.status or "").split(",") if s.strip()}
    limit = None if a.all else a.limit if a.limit is not None else None if want else TASK_LIST_LIMIT
    if want:
        stored = sorted(want | ({"failed"} if CHANGES_NEEDED in want else set()))
        rows = [t for t in p.db.q(f"{cols} WHERE status IN ({','.join('?' * len(stored))}) ORDER BY id DESC",
                                  stored) if task_outcome(t) in want]
        return rows[:limit] if limit is not None else rows, len(rows), limit
    total = p.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    return p.db.q(f"{cols} ORDER BY id DESC" + (" LIMIT ?" if limit is not None else ""),
                  [limit] if limit is not None else []), total, limit


def cmd_task(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.action != "add" and a.start_when is not None or a.action not in ("add", "rejudge") and a.why is not None:
        die("--start-when and --why go with add (--why also with rejudge); for a queued task use set-when")
    if a.action != "rejudge" and a.dry_run:
        die("--dry-run goes with rejudge")
    if a.action not in ("add", "set-when") and a.start_after is not None:
        die("--start-after goes with add or set-when")
    if a.action != "add" and a.budget is not None:
        die("--budget goes with add")
    if a.action != "set-when" and a.on_pass_cmd is not None:
        die("--on-pass-cmd goes with set-when")
    if a.action != "superseded" and (a.ref is not None or a.repo is not None):
        die("--ref and --repo go with superseded")
    if a.action != "list" and (a.status, a.all, a.limit) != (None, False, None):
        die("--status, --all and --limit go with list")
    if a.all and a.limit is not None:
        die("--all and --limit do not go together")
    if a.limit is not None and a.limit < 1:
        die("--limit needs a number of 1 or more")
    if a.action == "add":
        try:
            tid, note = add_task(p, a)
        except ValueError as e:
            die(str(e))
        print(f"task #{tid} queued" + note)
    elif a.action == "list":
        rows, total, limit = list_tasks(p, a)
        if limit is not None and total > limit:   # never cut silently: a caller would miss older tasks
            print(f"showing newest {limit} of {total} tasks; use --all, --limit N or --status", file=sys.stderr)
        if a.json:
            print(json.dumps([{"id": t["id"], "status": task_outcome(t), "kind": t["kind"], "tier": t["tier"],
                               "title": t["title"], **task_timing(t)} for t in rows], indent=2))
            return
        for t in rows:
            print(f"#{t['id']}\t{task_outcome(t)}\t{t['tier']}\t{t['title']}")
    elif a.action == "show":
        if not a.title.strip().isdigit():
            die(f"show needs a task id, not {a.title!r}")
        t = p.db.task(int(a.title))
        if not t:
            die(f"no task #{int(a.title)} in {p.name}", 1)
        from .landed import abandoned, superseded_rows
        gone = abandoned(p.db, t["id"])
        sup = superseded_rows(p, t["id"])
        if a.json:
            print(json.dumps({**t, **({"abandoned": {"why": gone[0], "ts": gone[1]}} if gone else {}),
                              **({"superseded": sup} if sup else {})}, indent=2, default=str))
        else:
            lines = [f"superseded by {r['sha'][:10]} ({'on' if r['on_push_branch'] else 'not on' if r['on_push_branch'] is False else 'cannot read'}"
                     f" the push branch), recorded {timefmt.long(r['ts'], p)}" for r in sup]
            if gone:
                lines.append(f"abandoned {timefmt.long(gone[1], p)}: {gone[0]}")
            print(show_task(t) + ("\n\n" + "\n".join(lines) if lines else ""))
    elif a.action == "cancel":
        from .runner import stop_runs
        if not a.title.strip().isdigit():
            die(f"cancel needs a task id, not {a.title!r}")
        tid = int(a.title)
        why = p.db.cancel(tid)
        if why:
            die(why)
        runs = stop_runs(p.db, p.runs, tid)
        print("cancelled" + (f"; ending its running run{'s' if len(runs) > 1 else ''} "
                             f"{', '.join(map(str, runs))}" if runs else ""))
    elif a.action == "requeue":
        if not a.title.strip().isdigit():
            die(f"requeue needs a task id, not {a.title!r}")
        tid = int(a.title)
        why = p.db.requeue(tid)
        if why:
            die(why)
        nb = (p.db.task(tid) or {}).get("not_before")
        # requeue keeps a deferral (often set on purpose); say so, and how to lift it
        print(f"task #{tid} queued" + (f"; it still waits until {timefmt.long(nb, p)}; to start it sooner: "
                                      f"ttp task {p.name} set-when {tid} --start-after now"
                                      if nb and nb > time.time() else ""))
    elif a.action == "set-when":
        if not a.title.strip().isdigit():
            die(f"set-when needs a task id, not {a.title!r}")
        try:
            print(set_when(p.db, int(a.title), a.probe, p, on_pass_cmd=a.on_pass_cmd, start_after=a.start_after))
        except ValueError as e:
            die(str(e))
    elif a.action == "rejudge":
        from .rejudge import rejudge
        if not a.title.strip().isdigit():
            die(f"rejudge needs a task id, not {a.title!r}")
        base = os.environ.get("TTP_PROJECT")
        if base and Project(base).base != p.base:
            die("rejudge from a run of another project is refused: it changes only its own project's tasks")
        try:
            lines, rc = rejudge(p, p.config(), int(a.title), dry_run=a.dry_run, why=a.why or "")
        except ValueError as e:
            die(str(e))
        print("\n".join(lines))
        if rc:
            raise SystemExit(rc)
    elif a.action == "superseded":
        from .landed import record_superseded
        if not a.title.strip().isdigit() or not a.probe:
            die("superseded needs a task id and the commit that carries its work (on the push branch, or --ref)")
        try:
            print(record_superseded(p, int(a.title), a.probe.strip(), a.ref, a.repo))
        except ValueError as e:
            die(str(e))
    elif a.action == "abandon":
        from .landed import abandon
        if not a.title.strip().isdigit():
            die(f'abandon needs a task id and why its work will never land: ttp task {p.name} abandon <id> "<why>"')
        try:
            print(abandon(p, int(a.title), a.probe or ""))
        except ValueError as e:
            die(str(e))


def task_timing(t: dict) -> dict:
    """A task's start_when, start_after (epoch seconds), why and retry_when, only those set."""
    from .db import deferral, load_result
    d = deferral(t)
    out = {"start_when": d.get("when"), "start_after": d.get("after"), "start_why": d.get("why"),
           "retry_when": load_result(t["result"]).get("retry_when")}
    return {k: v for k, v in out.items() if v}


def show_task(t: dict) -> str:
    """`ttp task show`: a task's title, kind, status, spec and stored hand-off (summary, follow-ups, notes,
    review findings). Read-only."""
    from .db import load_result
    r = load_result(t["result"])
    why = t.get("tier_source")   # NULL on tasks older than the record
    queued = t.get("assigned_tier")
    src = (f" ({why}" + (f", queued at {queued}" if queued and queued != t["tier"] else "") + ")") if why else ""
    out = [f"#{t['id']} {t['title']}", f"kind: {t['kind']}   status: {task_outcome(t)}   tier: {t['tier']}{src}",
           "", "spec:", t["spec"] or "(none)"]
    for key, v in task_timing(t).items():
        out.insert(2, f"{key}: {v}")
    for key in ("summary", "question", "followups", "notes", "findings"):
        v = r.get(key)
        if v:
            out += ["", f"{key}:", v if isinstance(v, str) else json.dumps(v, indent=2)]
    return "\n".join(out)


def add_task(p, a) -> tuple[int, str]:
    """`ttp task add`: the task and its deferral (--start-after, --start-when, --why) in one insert, so
    the daemon can never start it before its probe is set. Same checks as the coordinator's task_add."""
    from .coordinator import _start_args, defer_labels, marker_only_probe, start_why
    if not a.title.strip():
        raise ValueError("add needs a title")
    d = {"start_after": a.start_after, "start_when": a.start_when, "why": a.why}
    if a.why is not None and a.start_when is None:
        raise ValueError("--why explains a --start-when probe; give one")
    budget = getattr(a, "budget", None)
    if budget is not None and budget <= 0:
        raise ValueError("--budget is a dollar amount above 0")
    after, when = _start_args(d, {}, p)
    from .budget import task_default_usd
    tid = p.db.add_task(a.title, a.spec or "", kind=a.kind, tier=a.tier, priority=a.priority, origin="user",
                        budget_usd=budget if budget is not None else task_default_usd(p.config(), a.tier),
                        labels=defer_labels(after, when, start_why(d, {}, when)), not_before=after)
    stale = marker_only_probe(when, p.root)
    return tid, (f"; starts when: {when}" if when else "") \
        + (f"; starts after {timefmt.long(after, p)}" if after else "") + (f"; flagged: {stale}" if stale else "")


class _ProbeRefused(ValueError):
    """set_when's refusal of an always-passing retry_when under a kept on_pass_cmd; `key` names the wait."""
    def __init__(self, text: str, key: str):
        super().__init__(text)
        self.key = key


def set_when(db, tid: int, probe: str | None, p=None, on_pass_cmd: str | None = None,
             start_after: str | None = None) -> str:
    """_set_when; an always-passing retry_when it refuses is also recorded as one set_when_refused event
    per wait, so a refused model-free caller (a watcher that drops stderr) still shows."""
    try:
        return _set_when(db, tid, probe, p, on_pass_cmd, start_after)
    except _ProbeRefused as e:
        fp = f"set_when_refused:{e.key}"
        if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (time.time(), "cli", "set_when_refused", fp, "low", str(e), "handled", tid))
        raise ValueError(str(e)) from None


def _set_when(db, tid: int, probe: str | None, p=None, on_pass_cmd: str | None = None,
              start_after: str | None = None) -> str:
    """Re-point the probe of a task that has not started: a waiting task's `retry_when`, else its
    `start_when` deferral. An empty probe clears it. `start_after` (a delay, a time read in the home
    zone, or `now`/\"\" to clear) sets its start_after deferral (not_before), alone or with the probe.
    Same checks as the coordinator's task_update, through the same coordinator.deferral_update
    (the push branch's log check needs `p`). `on_pass_cmd` sets (\"\" clears) a waiting task's
    model-free completion command; `probe` None then keeps its retry_when. The first rewrite keeps
    what the run's result.json says as retry_when_orig / on_pass_cmd_orig, so the daemon still
    takes that result.json as the hand-off the task waits on (daemon._same_probe). An always-passing
    probe (`true`) while the hand-off's on_pass_cmd stays is refused: that command was written to run
    only once the hand-off's own wait ended."""
    from .coordinator import check_probe, check_task_refs, deferral_update, marker_only_probe, missing_file_probe
    from .db import TERMINAL_TASK_STATES, deferral, dump_result, load_result
    if probe is None and on_pass_cmd is None and start_after is None:
        raise ValueError("set-when needs the probe command (\"\" clears it), --start-after or --on-pass-cmd")
    with db.tx():   # BEGIN IMMEDIATE: the daemon cannot start the task between the check and the write
        task = db.task(tid)
        if not task:
            raise ValueError(f"no task #{tid}")
        if task["status"] in ("running", *TERMINAL_TASK_STATES):
            raise ValueError(f"task #{tid} is {task['status']}: only a task that has not started can have "
                             f"its probe or start_after changed; add a new one instead")
        prev = load_result(task["result"])
        retry = prev.get("status") == "waiting" and not deferral(task).get("when")   # the probe is its retry_when
        if on_pass_cmd is not None and not retry:
            raise ValueError(f"task #{tid} is not waiting on a hand-off: only a waiting task has an on_pass_cmd")
        said = []
        d = {**({"start_when": probe} if probe is not None and not retry else {}),
             **({"start_after": start_after} if start_after is not None else {})}
        if d:
            cur = deferral(task)
            upd, after, when = deferral_update(db, task, d, p)
            db.update_task(tid, **upd)
            if "start_when" in d:
                stale = marker_only_probe(when, p.root if p else None) if when != cur.get("when") else None
                said.append("start_when " + (f"set: {when}" if when else "cleared") + (f"; flagged: {stale}" if stale else ""))
            if start_after is not None:
                said.append("start_after " + (f"set: {timefmt.long(after, p)}" if after else "cleared"))
        if retry and (probe is not None or on_pass_cmd is not None):
            if probe is not None:
                check_probe(probe, "retry_when")
                check_task_refs(db, probe, "retry_when")
                from .waitheal import always_passes
                if on_pass_cmd is None and prev.get("on_pass_cmd") is not None and always_passes(probe):
                    # A caller acting on an older view of the task (a watcher that read the hand-off before
                    # the latest one) would run the hand-off's on_pass_cmd before the wait it was written for.
                    raise _ProbeRefused(
                        f"task #{tid} rejected: its hand-off's on_pass_cmd runs once its retry_when "
                        f"`{str(prev.get('retry_when') or '')[:200]}` passes; `{probe.strip()}` passes at once, "
                        f"so it would run now, before that wait ended. The probe stays. To run it now anyway, "
                        f"also give --on-pass-cmd with that command; to wake the task for a run instead, "
                        f"give --on-pass-cmd \"\"", f"{tid}:{prev.get('waiting_since')}")
            if on_pass_cmd is not None:
                check_probe(on_pass_cmd, "on_pass_cmd")
            for key, value in (("retry_when", probe), ("on_pass_cmd", on_pass_cmd)):
                if value is None:
                    continue
                prev.setdefault(f"{key}_orig", prev.get(key))
                if value.strip():
                    prev[key] = value.strip()
                else:
                    prev.pop(key, None)
            db.update_task(tid, result=dump_result(prev))
            if probe is not None:
                from .waitheal import probe_flag
                end = probe_flag(probe) or missing_file_probe(probe, p.root if p else None, "retry_when")
                said.insert(0, "retry_when " + (f"set: {probe.strip()}" if probe.strip()
                                                else "cleared; it wakes at its retry timer")
                            + (f"; flagged: {end}" if end else ""))
                from . import worktree
                if p and (held := worktree.probe_hold_text(tid, probe)):
                    from .daemon import log
                    log(p, held)
                    said.insert(1, held.removeprefix(f"task {tid} retry_when "))
            if on_pass_cmd is not None:
                said.append("on_pass_cmd " + (f"set: {on_pass_cmd.strip()}" if on_pass_cmd.strip()
                                              else "cleared; a passed probe wakes it"))
        return f"task #{tid} " + "; ".join(said)


def cmd_prune(a) -> None:
    """One sweep over finished tasks' worktrees and the worktrees workers left in finished runs'
    directories (state/runs/<id>/...), with the daemon's checks: clear build and cache
    directories, archive hand-off artifacts and large untracked files (state/archive/tasks/<id>/),
    move small untracked leftovers to the task's last run directory, clear a stale `git worktree
    add` lock, and remove the worktree when nothing is lost. Branches stay. Prints each worktree's
    outcome (the reason for each one kept), how many went and the space freed. --dry-run changes
    nothing and lists what would happen. --archived <task> lists a task's archive; with --restore
    <dir> it extracts the archived files there."""
    from . import archive, worktree
    p = need(a.name, sys.argv[1:])
    if getattr(a, "archived", None) is not None:
        m = archive.manifest(p, a.archived)
        if not m:
            sys.exit(f"task #{a.archived} has no archive under {archive.task_dir(p, a.archived)}")
        if getattr(a, "restore", None):
            got = archive.restore(p, a.archived, Path(a.restore).expanduser())
            print(f"restored {len(got)} file(s) of task #{a.archived} to {a.restore}")
            return
        print(f"task #{a.archived}: branch {m.get('branch') or '?'} at {str(m.get('head') or '?')[:12]}, "
              f"archive {archive.task_dir(p, a.archived)}")
        for e in m.get("files") or []:
            where = archive.where(p, a.archived, e)
            print(f"  {e['path']}  {e['size'] / 1e6:.1f} MB  {e['stored']}"
                  + ("  (hand-off artifact)" if e.get("named") else "")
                  + (f"  {where}" if where else ""))
        for e in m.get("patches") or []:
            print(f"  tracked changes ({len(e.get('paths') or [])} file(s)): "
                  f"git apply {archive.task_dir(p, a.archived) / e['file']}  (on {str(m.get('head') or '?')[:12]})")
        return
    dry = getattr(a, "dry_run", False)
    disk = p.config().get("disk", {})
    before = shutil.disk_usage(p.worktrees).free if p.worktrees.is_dir() else 0
    from .daemon import disk_guard_bytes
    try:
        reserve = disk_guard_bytes(disk, p.state)   # an archive leaves the disk guard's threshold free
    except OSError:
        reserve = 0.0
    res = worktree.sweep(p, names=disk.get("cache_dirs"), leftovers_max_mb=disk.get("worktree_leftovers_max_mb"),
                         archive_max_mb=disk.get("worktree_archive_max_mb"), dry_run=dry,
                         patch_tracked=getattr(a, "patch_tracked", False), reserve_bytes=reserve)
    for r in res:
        cleared = f"; cleared {', '.join(r['cleared'][:5])}" if r["cleared"] else ""
        mv, ar = r.get("moved"), r.get("archived")
        moved = (f"{'would move' if dry else 'moved'} {mv['files']} untracked file(s) ({mv['bytes'] / 1e6:.1f} MB) "
                 f"to {mv['to']}; " if mv else "")
        if ar:
            moved += (f"{'would archive' if dry else 'archived'} {ar['files']} file(s) ({ar['bytes'] / 1e6:.1f} MB)"
                      + (f" to {ar['dir']}" if ar.get("dir") else "")
                      + (f", {'would drop' if dry else 'dropped'} {ar['dropped']} over the cap "
                         f"({ar['dropped_bytes'] / 1e6:.1f} MB, listed in the manifest)" if ar["dropped"] else "")
                      + "; ")
        if r.get("patched"):
            moved += f"{'would save' if dry else 'saved'} its {len(r['patched'])} changed tracked file(s) as a verified patch; "
        if r.get("unlocked") is not None:
            moved += f"{'would clear' if dry else 'cleared'} its stale lock ({r['unlocked'] or 'no reason'}); "
        who = (f"stray worktree {r['path']} (#{r['task']})" if r.get("stray") else
               f"run {r['run'] or '?'}'s worktree {r['path']}" + (f" (#{r['task']})" if r["task"] else "")
               if "run" in r else f"#{r['task']}")
        print(f"{who} ({r['status']}): " + (moved + ("would be removed" if dry else "removed") + ", branch "
                                                   + (r["branch"] or "?") + " kept"
                                                   if r["why"] is None else f"kept: {r['why']}") + cleared)
    if not res:
        print("no finished task's worktree to tidy")
        return
    gone = sum(1 for r in res if r["why"] is None)
    line = f"{'would remove' if dry else 'removed'} {gone} worktree(s), kept {len(res) - gone}"
    if before and not dry:
        line += f"; freed {max(shutil.disk_usage(p.worktrees).free - before, 0) / 1e9:.1f} GB"
    print(line)


def _memory_room(p, dry: str = "", added: bool = True) -> None:
    """After a memory write: once an entry was `added` the pinned worker-memory total, then the pinned
    coordinator-memory total, each with a warning once it is over its room. In a dry run (dry is its
    prefix) nothing was added or retired, so it is the total as it stands."""
    from .coordinator import coordinator_pinned_total, worker_pinned_total
    now = " (as it stands: the dry run changed nothing)" if dry else ""
    for total, over in ([worker_pinned_total(p)] if added else []) + [coordinator_pinned_total(p)]:
        print(f"{dry}{total}{now}")
        if over:
            print(f"{dry}warning: {over}", file=sys.stderr)


def cmd_memory(a) -> None:
    from .coordinator import memory_budget_check, plan_memory_audience, worker_room_after
    p = need(a.name, sys.argv[1:])
    if a.search:
        found = p.search_memory(a.search)
        for e in found:
            tags = ", ".join([e["kind"]] + ["standing"] * e["standing"] + ["archived"] * e["archived"])
            print(f"[{e['name']}] ({tags}) {' '.join(e['text'].split())[:300]}")
        print(f"{len(found)} entr{'y' if len(found) == 1 else 'ies'} found")
        return
    if a.settle is not None:
        from . import settled
        if a.text or a.forget or a.supersedes:
            sys.exit("--settle takes only entry names (or none: every review-area settlement in memory)")
        try:
            recs, errors = settled.migrate(p, a.settle, dry_run=a.dry_run)
        except ValueError as e:
            sys.exit(str(e))
        for r in recs:
            print(f"{'would move' if a.dry_run else 'moved'} [{r['source']}] -> {settled.record_text(r, False)}")
        print(f"{len(recs)} entr{'y' if len(recs) == 1 else 'ies'} {'would move' if a.dry_run else 'moved'} to "
              f"{settled.FILE}" + ("" if a.dry_run or not recs else " and archived from memory"))
        if not a.dry_run:
            memory_budget_check(p)
        if errors:
            sys.exit("\n".join(errors))
        return
    if not a.text and not a.forget:
        sys.exit("give the memory's text, or --forget <entry>, or --search <words>")
    if (a.title or a.audience) and not a.text:
        sys.exit("--title and --audience go with the memory's text")
    if a.permanent and not a.supersedes:
        sys.exit("--permanent goes with --supersedes")
    dry = "dry run, nothing written or committed: " if a.dry_run else ""
    if a.supersedes:
        if not a.text or a.forget:
            sys.exit("--supersedes needs the merged entry's text and no --forget: "
                     f"ttp memory {p.name} \"<text>\" --kind K --supersedes <entry> ...")
        try:
            added, done = p.supersede_memory(a.text, a.supersedes, kind=a.kind, standing=a.standing,
                                             title=a.title, audience=a.audience, permanent=a.permanent,
                                             dry_run=a.dry_run)
        except ValueError as e:
            sys.exit(str(e))
        print(f"{dry}would add {added}" if dry else added)
        for dst in done:
            print(f"{dry}would retire to {dst}" if dry else f"retired to {dst}")
        room = "" if a.dry_run else worker_room_after(p, added.stem)
        if not a.dry_run:
            memory_budget_check(p)
        _memory_room(p, dry)
        if room:
            print(f"warning: {room}", file=sys.stderr)
        return
    room = ""
    try:
        if a.text:
            audience, room = plan_memory_audience(p, a.text, a.kind, a.standing, a.audience, a.title,
                                                  without=a.forget or ())
            path = p.add_memory(a.text, kind=a.kind, title=a.title, standing=a.standing, audience=audience,
                                dry_run=a.dry_run)
            print(f"{dry}would add {path}" if dry else path)
        done, errors = p.forget_memories(a.forget, why=a.why, dry_run=a.dry_run) if a.forget else ([], [])
    except ValueError as e:   # a forbidden-data refusal: it names the pattern, never the text
        sys.exit(str(e))
    for dst in done:
        print(f"{dry}would retire to {dst}" if dry else f"retired to {dst}")
    if not a.dry_run:
        memory_budget_check(p)
    if a.text or a.forget:
        _memory_room(p, dry, added=bool(a.text))
    if a.text and room:
        print(f"{dry}warning: {room}", file=sys.stderr)
    if spaced := [n for n in a.forget or [] if len(n.split()) > 1]:
        # --forget takes every word after it, so text written after it was read as an entry name.
        errors.append(f"{spaced[0]!r} is no entry name (names have no spaces): --forget takes every word "
                      f"after it as a name, so put the memory's text before it: "
                      f"ttp memory {p.name} \"<text>\" --forget <entry>")
    if errors:
        sys.exit("\n".join(errors))


def cmd_schedules(a) -> None:
    """Read-only by default (list, show). Only --export writes: it moves the schedules into
    harness/schedules.json and commits it, so it is confirmed first."""
    p = need(a.name, sys.argv[1:])
    path = sched.file_path(p)
    if a.export:
        if path.exists():
            die(f"{path} already exists: it holds the schedules; edit it, the daemon applies it")
        if not a.yes:
            note = (f"--export moves the schedules into {path} and commits it: from then on that file, "
                    "not the database, holds them and every change is a harness commit. "
                    "To only read them, use `ttp schedules {} show <schedule>`.".format(p.name))
            if not sys.stdin.isatty():
                die(note + " Add --yes to export anyway.")
            print(note)
            if input("export? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not exported")
                return
        sched.write_file(p, "schedules: exported from the database", create=True)
        print(f"moved the schedules into {path} and committed it; from now on it holds them "
              "and every change to them is a harness commit")
        return
    if a.action == "show":
        if not a.schedule:
            die("usage: ttp schedules <name> show <schedule>")
        r = p.db.one("SELECT * FROM schedules WHERE name=?", (a.schedule,))
        if not r:
            die(f"no schedule {a.schedule!r}")
        r = dict(r)
        e = sched.entry(r)
        state = {k: r[k] for k in r if k not in ("name", "kind", "every_s", "at", "enabled", "budget_usd_day",
                                                  "description", "payload")}
        state["recent_skips"] = [{"at": timefmt.short(float(x["ts"]), p), "reason": x["reason"]}
                                 for x in sched.skips(p.db, a.schedule)]
        if hint := sched.suggest_cap(p.db, r):
            state["suggested_budget_usd_day"] = {**hint, "text": sched.cap_text(hint)}
        print(json.dumps({**e, "state": state}, indent=2, default=str))
        return
    from . import heal
    print(f"schedules from {path}" if path.exists() else
          f"schedules from the database only (`ttp schedules {p.name} --export` moves them into {path})")
    home = timefmt.home(p)
    for r in p.db.q("SELECT * FROM schedules ORDER BY name"):
        e = sched.entry(r)
        t = e["payload"].get("timeout_s") if isinstance(e["payload"], dict) else None
        pre = str(e["payload"].get("precheck") or "").strip() if isinstance(e["payload"], dict) else ""
        over = sched.over_caps(e["payload"]) if r["kind"] != "watcher" and isinstance(e["payload"], dict) else []
        at = sched.at_text(r, home)
        spec = heal.of(e["payload"]) if r["kind"] == "command" and isinstance(e["payload"], dict) else None
        slow = heal.interval(p.db, r, spec) if spec else 0
        if slow > int(r["every_s"]):   # a heal check backing off while healthy
            at = (at + " " if at else "") + f"(now {heal._secs(slow)}: healthy)"
        cap = "" if r["kind"] != "llm" else \
            f" cap ${float(r['budget_usd_day']):.2f}/day" if r["budget_usd_day"] is not None else " no daily cap"
        if cap and (hint := sched.cap_text(sched.suggest_cap(p.db, r))):
            cap += f" ({hint})"
        print(f"  {e['name']:24} {e['kind']:8} every {e['every']}{' ' + at if at else ''}{cap}"
              f"{f' timeout {t}s' if t else ''}"
              f"{f' (runs at the cap: {sched.over_cap_text(over)})' if over else ''}"
              f"{f' precheck: {pre[:60]}' if pre else ''}"
              f"{'' if e['enabled'] else ' (off)'}  {r['last_status'] or ''}"
              f"{f' ({n} skip(s) in 24 h)' if (n := len(sched.skips(p.db, r['name'], sched.SKIP_KEEP, time.time() - 86400))) else ''}")


def cmd_mute(a) -> None:
    """Observation mutes: `list` shows a project's active mutes; `end <source> <match>` ends one now, without
    a coordinator turn, with its usual end summary and who ended it."""
    from . import screen
    p = need(a.project, sys.argv[1:])
    own = os.environ.get("TTP_PROJECT")
    if a.action == "end" and os.environ.get("TTP_RUN_DIR") and own and Path(own).resolve() != Path(p.base).resolve():
        die(f"a run may end only its own project's mutes, not {a.project}'s")
    if a.action == "list":
        ms = screen.mutes(p.db)
        if not ms:
            print("no active mutes")
        for m in ms:
            print(screen.mute_line(m, where=p))
        return
    task = os.environ.get("TTP_TASK")
    who = f"task #{task}" if os.environ.get("TTP_RUN_DIR") and task else "the user (ttp mute end)"
    m = screen.end_mute_by(p.db, a.source, a.match, who, where=p)
    if m is None:
        live = "; ".join(f"{x['source']} {x['match']!r}" for x in screen.mutes(p.db)) or "none"
        die(f"no active mute of {a.source} {a.match!r} (source and match as `ttp mute list` shows them; active: {live})")
    print(f"ended the mute of {m['source']} {m['match']!r} ({m['count']} muted); the coordinator gets its summary")


def cmd_issues(a) -> None:
    """Daemon and watcher issues: `list` shows the open ones (--all: closed too), `close <id-or-key> --why`
    closes one without a DB write by hand. A condition still live reopens at its next detection."""
    from . import screen
    if a.project:
        p = need(a.project, sys.argv[1:])
    else:
        base = os.environ.get("TTP_PROJECT") or next(
            (str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
        if not base:
            die("no project here: run it inside a project or a run, or pass --project NAME")
        p = Project(base)
    if a.action == "list":
        rows = p.db.q("SELECT id, source, title, severity, status, count, last_seen, watch_key, closed, cleared_why "
                      "FROM issues" + ("" if a.all else " WHERE status='open'") + " ORDER BY id DESC LIMIT ?",
                      (max(1, a.limit),))
        if a.json:
            print(json.dumps(rows))
            return
        if not rows:
            print("no open issues" if not a.all else "no issues")
        for r in rows:
            key = f" key={r['watch_key']}" if r["watch_key"] else ""
            end = f" closed {timefmt.short(float(r['closed']), p)}: {r['cleared_why']}" if r["status"] == "fixed" \
                and r["closed"] else ""
            print(f"#{r['id']} [{r['severity']}/{r['status']}] {r['source']}{key} x{r['count']} last "
                  f"{timefmt.short(float(r['last_seen'] or 0), p)}: {(r['title'] or '')[:120]}{end}")
        return
    own = os.environ.get("TTP_PROJECT")
    if os.environ.get("TTP_RUN_DIR") and (not own or Path(own).resolve() != Path(p.base).resolve()):
        die(f"a run may close only its own project's issues, not {p.name or p.base}'s")
    if a.why is None or not a.why.strip():
        die("ttp issues close: --why <reason> is required and must not be empty")
    task = os.environ.get("TTP_TASK")
    who = f"task #{task}" if os.environ.get("TTP_RUN_DIR") and task else "the user (ttp issues close)"
    try:
        row = screen.close_issue(p.db, a.issue, a.why, who)
    except ValueError as e:
        die(f"ttp issues close: {e}")
    if row.get("already"):
        print(f"issue #{row['id']} was already closed: {row['cleared_why'] or 'no reason recorded'}")
        return
    print(f"closed issue #{row['id']} ({row['source']}); a new sighting of its condition reopens it")


def cmd_heal(a) -> None:
    """Heal checks (command schedules with a `heal` block): `list` shows each with its state,
    `test <name>` runs its check once as a dry run (no fix, no state change)."""
    from . import heal, responsibilities
    if a.project:
        p = need(a.project, sys.argv[1:])
    else:
        base = os.environ.get("TTP_PROJECT") or next(
            (str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
        if not base:
            die("no project here: run it inside a project or a run, or pass --project NAME")
        p = Project(base)
    found = heal.checks(p.db)
    if a.action == "list":
        if not found:
            print("no heal checks (a command schedule's `heal` block declares one)")
        try:
            items = responsibilities.inventory(p)
        except Exception:
            items = None
        cover = responsibilities.check_cover(items or [])
        for r, spec in found:
            print(heal.describe(r, spec, p.db))
            if items is not None:
                hint = "" if cover.get(r["name"]) else responsibilities.uncovered_hint(items)
                print(f"    covers: {', '.join(cover.get(r['name']) or ['no inventory item'])}"
                      + (f" ({hint})" if hint else ""))
        line = heal.line(p.db)
        if line:
            print(line)
        return
    match = [(r, spec) for r, spec in found if r["name"] == a.check]
    if not match:
        die(f"no enabled heal check named {a.check!r}; `ttp heal list` shows them")
    r, spec = match[0]
    rc, out = heal.check(spec, dict(heal.state(p.db, r["name"])), str(p.root), command_env(p))
    word = heal.classify(rc, out)
    print(f"{r['name']}: {word} (exit {rc})" + (f"\n{out}" if out else ""))
    sys.exit(1 if heal.recovery_off(out) else 0 if rc == 0 or heal.known_run(rc, out) else 1 if rc == 1 or heal.finding(rc, out) else 75)


def cmd_obligation(a) -> None:
    """`ttp obligation add|attach|progress|receipt|drop|list`: urgent deliverables owed to the user (obligations.py).
    Inside a run, progress and receipts go to the run's file and the daemon files them for its task; so
    do a task's on_pass_cmd's (note-only context: the waiting run's directory)."""
    from . import obligations as ob
    if a.milestone and a.action != "receipt":
        die("--milestone is for a checked milestone receipt")
    run_dir = os.environ.get("TTP_RUN_DIR") or os.environ.get(NOTE_RUN_ENV)
    if run_dir and a.action in ("progress", "receipt"):
        task = os.environ.get("TTP_TASK") or ""
        if not task.isdigit():
            die(f"ttp obligation {a.action}: this run has no task (TTP_TASK)")
        if len(a.args) != 2 or not a.args[1].strip():
            die(f"usage: ttp obligation {a.action} <id> \"<evidence>\"")
        ob.append_run(Path(run_dir), a.args[0], a.action, a.args[1], task, milestone=a.milestone)
        print(f"{a.action} on {a.args[0]} recorded for task #{task}; the daemon files it within "
              f"{ob.CHECK_EVERY_S // 60} min (only if this task owns {a.args[0]})")
        return
    if in_run():
        die(f"ttp obligation {a.action} is for the user or the coordinator, not inside a run; an owner "
            f"records `progress` and `receipt`")
    p = need(a.project, sys.argv[1:]) if a.project else here()
    if not p:
        die("no tt-project project here; name one with --project")
    try:
        if a.action == "add":
            if len(a.args) != 1:
                die('usage: ttp obligation add "<what is owed>" [--owner <task>]...')
            oid = ob.add(p, a.args[0], [int(t) for t in a.owner or []])
            print(f"obligation {oid} recorded" + (f"; owners: {', '.join('#' + str(t) for t in a.owner)}"
                                                  if a.owner else "; attach its owner tasks with `ttp obligation attach`"))
        elif a.action == "attach":
            if len(a.args) < 2 or not all(x.lstrip("#").isdigit() for x in a.args[1:]):
                die("usage: ttp obligation attach <id> <task>...")
            new = ob.attach(p, a.args[0], [int(x.lstrip("#")) for x in a.args[1:]])
            print(f"attached to {', '.join('#' + str(t) for t in new)}" if new else "already attached")
        elif a.action in ("progress", "receipt"):
            if len(a.args) != 2:
                die(f"usage: ttp obligation {a.action} <id> \"<evidence>\" [--task <owner>]")
            print(ob.record(p, a.args[0], a.action, a.args[1], a.task, milestone=a.milestone))
        elif a.action == "drop":
            if len(a.args) != 2:
                die('usage: ttp obligation drop <id> "<why it is no longer owed>"')
            print(ob.drop(p, a.args[0], a.args[1]))
        elif a.json:
            print(json.dumps(ob.items(p.db), indent=1))
        else:
            print("\n".join(ob.lines(p.db, where=p)) or "no obligations")
    except ValueError as e:
        die(str(e))


def cmd_machines(a) -> None:
    """The user's machines (~/.tt-project/machines.json), shared by all their projects. Each
    project's charter says which of them it may use; its coordinator routes work only to those.
    A project created with --host reads the copy on its machine: changes are copied there, merged."""
    from . import machines as mm
    if a.action == "change":
        return _machine_change(a)
    if a.action == "outage":
        return _machine_outage(a)
    if a.action == "activity":
        return _machine_activity(a)
    if a.action == "status":
        from . import responsibilities
        view = responsibilities.machine_status(a.alias)
        if a.json:
            print(json.dumps(view, indent=2, sort_keys=True))
        elif not view:
            print(f"no machines in {mm.path()} (ttp machines add <alias>)")
        for rec in ([] if a.json else view):
            print("\n".join(responsibilities.machine_lines(rec)))
        return
    if a.action == "set":
        a.action = "add"
    if a.action == "add":
        recovery = a.owner is not None or a.fallback is not None
        try:
            if not recovery or any(v is not None for v in (a.tags, a.note, a.until, a.min_free_gb, a.hostname,
                                                            a.shared)) or a.unshared:
                entry = mm.add(a.alias, a.tags, a.note,
                               ... if a.min_free_gb is None else a.min_free_gb, a.hostname,
                               False if a.unshared else a.shared, a.until, where=here())
            if recovery:   # only an owner or fallback: the machine must be known already
                entry = mm.set_recovery(a.alias.strip(), a.owner, a.fallback)
        except ValueError as e:
            die(str(e))
        print(f"saved {mm.line(a.alias.strip(), entry)}")
    elif a.action == "remove":
        try:
            if not mm.remove(a.alias):
                die(f"no machine {a.alias!r} in {mm.path()}")
        except ValueError as e:
            die(str(e))
        print(f"removed {a.alias}")
    if a.action in ("add", "remove", "push", "set"):
        named = getattr(a, "to", None) or ([a.host] if getattr(a, "host", None) else [])
        hosts = named or remote_hosts()
        if a.action == "push" and not hosts:
            print("no projects on other machines; nothing to copy")
        if not named:   # a host named on purpose is tried whatever its state
            skips = mm.push_skips(hosts)
            for host in hosts:
                if host in skips:
                    print(f"skipped {host}: {skips[host]}")
            hosts = [h for h in hosts if h not in skips]
        if a.action == "push":
            for host in hosts:
                print(mm.push(host), flush=True)
            if getattr(a, "log", False):
                print(mm.PUSH_DONE, flush=True)
        else:
            # A change never waits for hosts that are down: the copy runs on in the background.
            for line in mm.push_failures(mark=True):
                print(f"last background copy: {line}")
            if hosts:
                try:
                    log = mm.push_background(hosts)
                except OSError as e:     # the local change is saved; only the copy did not start
                    print(f"could not start copying the machines list to {', '.join(hosts)}: {e}")
                    return
                print(f"copying the machines list to {', '.join(hosts)} in the background (results: {log}; "
                      f"`ttp machines push` copies in the foreground)")
    else:
        known = mm.load()
        if a.json:
            print(json.dumps(known, indent=2, sort_keys=True))
            return
        if not known:
            print("no machines yet: ttp machines add <alias> --tags device,... [--note TEXT]")
        from . import fleethold
        holds = fleethold.active()
        for alias in sorted(known):
            print(mm.line(alias, known[alias]))
            if alias in holds:
                print("  " + fleethold.line(holds[alias]))


def _machine_activity(a) -> None:
    """`ttp machines activity <alias> [--since] [--json]`: who holds, waits for or runs on the machine
    now, and the locks and detached jobs every project here logged on it since then (activity.py). Read only."""
    from . import activity
    now = time.time()
    try:
        since = activity.parse_since(a.since, now, where=here())
    except ValueError as e:
        die(str(e))
    view = activity.machine_view(a.alias, since, now)
    if a.json:
        print(json.dumps(view, indent=2, sort_keys=True))
        return
    p = here()
    print("\n".join(activity.lines(view, p)))


def _machine_outage(a) -> None:
    """`ttp machines outage`: a known outage (hardware repair, an external ticket) that pages nobody."""
    from . import machine_ledger as ml
    p = here()
    by = a.by or (p.name if p else "user")
    if a.clear:
        if not a.alias:
            die("name the machine whose outage is over")
        if ml.clear_outage(a.alias, by):
            left = ml.outages().get(a.alias)
            print(f"{a.alias}: {by}'s known outage cleared"
                  + (f"; still out: {ml.outage_line(a.alias, left, p)}" if left else ""))
            return
        others = sorted((ml.outage_entries().get(a.alias) or {}))
        print(f"{a.alias}: no known outage from {by}"
              + (f"; open from {', '.join(others)} (clear one with --by <project>)" if others else ""))
        return
    if not a.why:
        got = ml.outages()
        if a.json:
            print(json.dumps({k: o for k, o in got.items() if not a.alias or k == a.alias}, indent=2, sort_keys=True))
            return
        for alias, o in sorted(got.items()):
            if not a.alias or alias == a.alias:
                print(ml.outage_line(alias, o, p))
        if not got:
            print("no known outages")
        return
    try:
        rec = ml.set_outage(a.alias, a.why, by, a.expires, owner=a.owner or "", covers=a.covers, where=p)
    except ValueError as e:
        die(str(e))
    print(ml.outage_line(a.alias, ml.outages().get(a.alias) or rec, p))


def _machine_change(a) -> None:
    """`ttp machines change add|list|close|edit`: the ledger of live changes to shared machines (machine_ledger.py)."""
    from . import machine_ledger as ml
    if a.change == "list":
        got = ml.changes([a.alias] if a.alias else None, open_only=not a.all)
        if a.json:
            print(json.dumps(got, indent=2, sort_keys=True))
            return
        if not got:
            print("no open machine changes")
        p = here()
        for c in got:
            print(ml.change_line(c, where=p) + (f" (closed {ml.ends.shown(c['closed'], p)}: {c.get('closed_why')})"
                                                if c.get("closed") else ""))
            for r in c.get("auto_undo_runs") or []:
                tail = " ".join(str(r.get("tail") or "").split())[-200:]
                print(f"  auto-undo try {r.get('try')} at {ml.ends.shown(float(r.get('at') or 0), p)}: "
                      f"{r.get('outcome')}" + (f"; output: {tail}" if tail else ""))
        for r in ml.pending(p.state) if p else []:
            print(f"queued for this project's daemon (request {r['queued']}): {r['op']} "
                  + (f"#{r['fields'].get('id')}" if r["op"] == "close" else
                     f"{r['fields'].get('alias')}: {r['fields'].get('what')}"))
        return
    p = here()
    by = a.by or (p.name if p else "")
    queue = p.state if p else None   # a sandbox that keeps the shared ledger read-only: queued for the daemon
    if a.change == "close":
        try:
            c = ml.close_change(a.id, a.why or "", by or "user", queue=queue)
        except ValueError as e:
            die(str(e))
        print(_queued_line(c) if c.get("queued") else f"closed machine change #{c['id']} on {c['alias']}")
        return
    if a.change == "edit":
        flag_only = a.test is not None and not any((a.what, a.undo, a.expires, a.until_probe, a.until_task)) \
            and a.auto_undo is None
        if a.allow_met_probe and not a.until_probe:
            die("--allow-met-probe goes with --until-probe")
        try:
            if a.test is not None:
                c = ml.set_test(a.id, by, a.test, queue=queue)
                if c.get("queued"):
                    print(f"queued the test flag of machine change #{a.id} (request {c['queued']}): the shared "
                          f"machine ledger is read-only here, so this project's daemon sets it on its next tick")
                else:
                    print(f"machine change #{a.id} is {'now' if a.test else 'no longer'} flagged as a test: "
                          f"`ttp ttr` labels the incidents inside its window afresh")
            if flag_only:
                return
            owner = None
            if a.until_task or a.until_probe:
                c = next((c for c in ml.changes() if c.get("id") == a.id), None)
                owner = c.get("by") if c else by
            if a.until_task:
                _until_task_ok(p, owner, a.until_task)
            c = ml.edit_change(a.id, by or "user", a.what, a.undo, a.expires, a.until_probe, until_task=a.until_task,
                               where=p, auto_undo=a.auto_undo, allow_met_probe=a.allow_met_probe,
                               project=(p if p and p.name == owner else local_project(owner) or p)
                               if a.until_probe else None)
        except ValueError as e:
            die(str(e))
        except OSError as e:
            if e.errno not in ml.UNWRITABLE:
                raise
            die(f"the shared machine ledger is read-only here ({e.strerror}), and an edit (except --test or "
                f"--no-test) is not queued: "
                f"`ttp machines change close {a.id}` and `ttp machines change add` a new entry (both are queued "
                f"for this project's daemon)")
        print(f"edited {ml.change_line(c, where=p)}")
        return
    if not by:
        die("run it inside a project, or say whose change it is: --by <project>")
    task = os.environ.get("TTP_TASK")
    tid = int(task) if task and task.isdigit() else None
    if a.power_cycle_at and (a.undo or a.expires or a.until_probe or a.until_task or a.auto_undo):
        die("a power cycle needs no --undo, --expires, --until-probe, --until-task or --auto-undo: it closes itself "
            "after its time")
    if a.expires and a.until_task:
        die("give --until-task without --expires: it ends with the task, at the latest after 7 days")
    if not a.power_cycle_at and not a.undo:
        die("give the --undo command (or --power-cycle-at <t> for a planned power cycle)")
    if not a.power_cycle_at and not (a.expires or a.until_probe or a.until_task):
        die(ml.END_HINT)
    if a.since and a.power_cycle_at:
        die("a power cycle cannot be backdated: --since is for a change already made")
    if a.allow_met_probe and not a.until_probe:
        die("--allow-met-probe goes with --until-probe")
    try:
        since = ml.parse_since(a.since, where=p) if a.since else None
        if a.until_task:
            _until_task_ok(p, by, a.until_task)
        c = ml.add_power_cycle(a.alias, a.power_cycle_at, by, a.what, tid, queue=queue,
                                extra={"test": True} if a.test else None, where=p) if a.power_cycle_at else \
            ml.add_change(a.alias, a.what, a.undo, by, a.expires, a.until_probe, tid, expects=a.expect,
                          until_task=a.until_task, queue=queue, test=a.test, since=since, where=p,
                          auto_undo=a.auto_undo, allow_met_probe=a.allow_met_probe,
                          project=p if p and p.name == by else local_project(by) or p)
    except ValueError as e:
        die(str(e))
    print(_queued_line(c, p) if c.get("queued") else f"recorded {ml.change_line(c, where=p)}")


def _queued_line(req: dict, where=None) -> str:
    """What `ttp machines change add|close` says when it queued the request (machine_ledger: Queued requests);
    an added change's end is shown in the zone of `where`."""
    from . import ends
    end = req["fields"].get("expires")
    what = f"close of machine change #{req['fields']['id']}" if req["op"] == "close" else \
        f"machine change on {req['fields']['alias']}: {req['fields']['what']}" \
        + (f" (ends {ends.shown(float(end), where)} at the latest)" if isinstance(end, (int, float)) else "")
    return (f"queued {what} (request {req['queued']}): the shared machine ledger is read-only here, so this "
            f"project's daemon records it on its next tick, within a few minutes while it runs; "
            f"`ttp machines change list` shows it. Running the command again adds nothing.")


def _until_task_ok(p: Project | None, by: str, tid: int) -> None:
    """Refuse an until-task unless it is an open task of the project the entry is by, run inside it: only
    that project's daemon reads its tasks' states."""
    from . import machine_ledger as ml
    if p is None or p.name != by:
        raise ValueError(f"--until-task names a task of the project the change is by ({by or '?'}): run it "
                         f"inside that project")
    ml.check_task(p, tid)


def _pause_by(p: Project) -> str:
    """Whom a `ttp pause --resource` is stamped for: the user only when they run it themselves; inside
    a run, the coordinator (its own turn in this project) or a worker, whose pauses the coordinator may lift."""
    from .project import in_run
    if not in_run():
        return "user"
    run, mine = os.environ.get("TTP_RUN_ID"), os.environ.get("TTP_PROJECT")
    if run and not os.environ.get("TTP_TASK") and mine and Path(mine).resolve() == Path(p.base).resolve():
        row = p.db.one("SELECT role FROM runs WHERE id=?", (run,))
        if row and row["role"] == "coordinator":
            return "coordinator"
    return "worker"


def cmd_pause(a) -> None:
    p = need(a.name, sys.argv[1:])
    if (a.resource or getattr(a, "machine", None)) and getattr(a, "until_reboot", False):
        die("--until-reboot pauses the whole project; a resource pause ends with --until or --end-when")
    if getattr(a, "machine", None):
        if a.resource:
            die("--machine holds every resource on the machine: give it or --resource, not both")
        from .coordinator import pause_machine
        from .project import in_run
        try:
            from . import pauseends
            end = pauseends.from_action({"until": a.until, "end_when": a.end_when, "report_from": a.report_from},
                                        where=p) \
                if a.cmd == "pause" else None
            # Only the user's own command outside a run sets or lifts a hold as theirs.
            print(f"{p.name}: " + pause_machine(p, a.machine, a.cmd == "pause", reason=getattr(a, "reason", None) or "",
                                                by=_pause_by(p), users_word=not in_run(), end=end))
        except ValueError as e:
            die(str(e))
        return
    if a.resource:
        from .coordinator import pause_resource
        try:
            from . import pauseends
            end = pauseends.from_action({"until": a.until, "end_when": a.end_when, "report_from": a.report_from},
                                        where=p) \
                if a.cmd == "pause" else None
            print(f"{p.name}: " + pause_resource(p, a.resource, a.cmd == "pause", reason=getattr(a, "reason", None) or "",
                                                 by=_pause_by(p), end=end))
        except ValueError as e:
            die(str(e))
        return
    boot = None
    if a.cmd == "pause" and getattr(a, "until_reboot", False):
        from .runner import boot_id
        boot = boot_id()
        if boot == "unknown":
            die("--until-reboot: this host's boot id cannot be read (no /proc/sys/kernel/random/boot_id "
                "or kern.bootsessionuuid or kern.boottime); use a plain pause and resume it after the reboot")
    from . import projpause
    by = projpause.caller(p)
    try:
        p.db.set_paused(a.cmd == "pause", boot, by=by, via="CLI", reason=getattr(a, "reason", None) or ("host power cycle (until reboot)" if boot else ""))
    except ValueError as e:
        die(f"{e}: pass --reason")
    if a.cmd != "pause":
        from . import hostcycle
        for cid in hostcycle.release(p):
            print(f"planned host cycle withdrawn (machine change #{cid}): its drain recorded none")
    print(f"{p.name} " + ("resumed" if a.cmd != "pause" else "paused: no new model runs start"
                          + (" until the host reboots; the daemon lifts it on its first tick after boot" if boot else "")))


def _draining_runs(p: Project) -> list[dict]:
    """Runs and pushes in flight in project p, apart from the run calling (TTP_RUN_ID of this project)."""
    me = os.environ.get("TTP_RUN_ID") if os.environ.get("TTP_PROJECT") and \
        Path(os.environ["TTP_PROJECT"]).resolve() == Path(p.base).resolve() else None
    runs = [dict(r) for r in p.db.q("SELECT id,task,role FROM runs WHERE status='running' ORDER BY id")
            if str(r["id"]) != str(me)]
    return runs + [{"id": None, "task": t["id"], "role": "push"}
                   for t in p.db.q("SELECT id FROM tasks WHERE status='pushing' AND COALESCE(result,'') NOT LIKE '%\"push_verify\"%' ORDER BY id")]


def cmd_lease(a) -> None:
    """`ttp lease check`: whether the configured lease store may be used and answers (lease.py).
    `ttp lease show`: this host's last view of the lease (state/lease.json)."""
    from . import lease
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp lease check <name>`")
    if a.action == "show":
        print(lease.status_line(p, p.config()) or "no lease configured (failover.lease_remote unset)")
        return
    ok, text = lease.check(p, p.config())
    print(f"ttp lease check: {text}")
    if not ok:
        sys.exit(1)


def cmd_drain(a) -> None:
    """Pause the project (no new runs) and wait until the runs in flight end; running workers are kept.
    Its `ttp detach` jobs still running here are listed (a reboot ends them); with --jobs it waits for
    them too. With --reboot it records the planned host cycle (hostcycle), so the next boot does not
    count the jobs it cuts as the tasks' reboot losses. --reboot also announces the cycle in the machine
    ledger as it starts (hostcycle.open_intent): every project on this host then starts no new worker
    or reviewer run, checks or push, and their running workers are told to hand off; once it records the
    cycle, the intent's time moves to then (hostcycle.cycle_now). Its wait also lasts while other
    projects' runs (but their coordinators) and pushes here are in flight, and lists them all; only this
    project's decide the exit."""
    from . import hostcycle
    p = need(a.name, sys.argv[1:])
    if not p.db.kv("paused", False):
        from . import projpause
        p.db.set_paused(True, by=projpause.caller(p), via="ttp drain",
                        reason=f"drain before a planned host cycle: {a.reboot}" if a.reboot
                        else "drain: waiting for the runs in flight to end")
        print(f"{p.name} paused: no new model runs start")
    intent, wrote, since = None, False, time.time()
    if a.reboot:
        intent, wrote, line = hostcycle.open_intent(p, a.reboot, a.wait, now=since)
        print(line, file=sys.stdout if intent else sys.stderr)
    end = time.time() + max(0.0, a.wait)
    while True:
        left, jobs = _draining_runs(p), hostcycle.running_jobs(p)
        others = hostcycle.host_runs(p) if a.reboot else []
        if not left and not (a.jobs and jobs) and all(r["role"] == "coordinator" for _, r in others) \
                or time.time() >= end:
            break
        time.sleep(min(poll_s(2.0), max(0.1, end - time.time())))
    if a.reboot:
        try:
            rec = hostcycle.record(p, a.reboot, left, jobs, intent=intent["id"] if intent else None,
                                   intent_was=hostcycle.before(intent, p.name, since) if intent and not wrote else None,
                                   since=since)
        except OSError as e:
            if wrote:   # no record, no planned cycle: other projects here are not held for it
                from . import machine_ledger as ml
                ml.close_change(intent["id"], "ttp drain could not record the planned host cycle", by=p.name)
            die(f"could not record the planned host cycle: {e}")
        if intent:
            hostcycle.cycle_now(p, intent, since=since)
        print(f"planned host cycle recorded: {rec}")
    if others:
        print(f"other projects on this host: {len(others)} run(s) or push(es) in flight, workers told to hand off "
              "before the cycle; a reboot ends them: "
              + ", ".join(f"{n} push of task #{r['task']}" if r["role"] == "push" else
                          f"{n} run {r['id']} ({r['role']}" + (f", task #{r['task']})" if r["task"] else ")")
                          for n, r in others))
    if jobs:
        print(f"{p.name}: {len(jobs)} detached job(s) still running on this host; a reboot ends them"
              + (" (recorded as interrupted by the planned cycle)" if a.reboot else "") + f": {hostcycle.describe(jobs)}",
              file=sys.stderr if a.jobs else sys.stdout)
    if not left and not (a.jobs and jobs):
        print(f"{p.name} drained: no runs in flight")
        return
    what = ", ".join(f"push of task #{r['task']}" if r["role"] == "push" else
                     f"run {r['id']} ({r['role']}" + (f", task #{r['task']})" if r["task"] else ")") for r in left)
    if a.jobs and jobs:
        what = ", ".join(x for x in (what, f"{len(jobs)} detached job(s)") if x)
    print(f"{p.name} not drained after {a.wait:g} s: {len(left) + (len(jobs) if a.jobs else 0)} in flight: {what}"
          + f". The project stays paused; `ttp resume {p.name}` lifts it", file=sys.stderr)
    sys.exit(1)


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
        _exit_for_restart(service.restart(p))


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


def cmd_account_config(key: str, value: str | None) -> None:
    """Read or set an account-level setting (project.ACCOUNT_KEYS) in ~/.tt-project/settings.json."""
    if value is None:
        node = deep_merge(DEFAULT_CONFIG, load_account_settings())
        for part in key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(value)
    except ValueError:
        val = value
    from .globalcap import setting_problems
    sec, _, k = key.partition(".")
    bad = setting_problems({k: val}) if sec == "budget" and val not in ("", None) else []
    if bad:
        die(bad[0])
    try:
        set_account_setting(key, None if val == "" else val)
    except ValueError as e:
        die(str(e))
    print(f"{key} = {json.dumps(val)} (account-level, every project on this machine)" if val != ""
          else f"{key} removed (account-level); the default applies")


def _user_only_change(raw: dict, key: str, val) -> str | None:
    """The user-only setting (coordinator.NEEDS_USER) that writing `val` at `key` into the project.json
    `raw` would change, or None. A parent key (`budget`, `delivery`) counts through what it holds, and
    turning a SAFE_WHEN_OFF switch off is the safe direction, as in the coordinator's own gate."""
    import copy
    from .coordinator import NEEDS_USER, SAFE_WHEN_OFF

    def at(node, dotted):
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    after = copy.deepcopy(raw)
    node = after
    for part in key.split(".")[:-1]:
        nxt = node.get(part) if isinstance(node, dict) else None
        node = node.setdefault(part, {}) if isinstance(nxt, dict) or nxt is None else None
        if node is None:   # set_config fails on a scalar parent; nothing user-only changes here
            return None
    node[key.split(".")[-1]] = val
    for k in NEEDS_USER:
        if k != key and not k.startswith(key + ".") and not key.startswith(k + "."):
            continue
        new = at(after, k)
        if new != at(raw, k) and not (k in SAFE_WHEN_OFF and new in (False, "", [])):
            return k
    return None


def _refuse_user_only_in_run(key: str) -> None:
    die(f"{key} is a setting only the user may change, and `ttp config` refuses to set it inside a run; "
        f"reading it still works. Name the value you need in the hand-off (a followup or the question) "
        f"so the coordinator can ask the user")


def cmd_chat_settable(key: str, value: str | None) -> None:
    """`ttp config --chat-settable KEY [VALUE]`: whether a coordinator config_set accepts the key (and the
    value, checked as config_set checks it), without setting anything; `list` prints every accepted key.
    Exit 0 when accepted, 1 when refused."""
    from . import chatset
    from .coordinator import USER_SETTABLE, settable
    if key == "list":
        print("\n".join([*sorted(USER_SETTABLE), *chatset.PATTERNS]))
        return
    try:
        conv = settable(key)
        if value is not None:
            try:
                val = json.loads(value)
            except ValueError:
                val = value
            val = conv(val)
    except (ValueError, TypeError) as e:
        print(f"refused: {e}" if str(e).startswith(key) else f"{key}: refused: {e}")
        sys.exit(1)
    print(f"{key}: accepted" + (f" as {json.dumps(val)}" if value is not None else ""))


def cmd_config(a) -> None:
    run = in_run()
    if a.chat_settable:
        return cmd_chat_settable(a.name, a.key)
    if a.account:
        if a.value is not None:
            die("usage: ttp config --account KEY [VALUE]")
        from .coordinator import NEEDS_USER
        if run and a.key is not None and a.name in NEEDS_USER:
            # A run can be steered by text it reads; spend caps change only on the user's word.
            _refuse_user_only_in_run(a.name)
        return cmd_account_config(a.name, a.key)
    from .project import DEFAULT_CONFIG
    if a.name.split(".")[0] in DEFAULT_CONFIG and a.value is None and not local_project(a.name) and here():
        # `ttp config KEY [VALUE]` in a run or project folder: the project is the one it runs in, as for `ttp stats`
        p, a.key, a.value = here(), a.name, a.key
    else:
        p = need(a.name, sys.argv[1:])
    if a.value is None:
        node = p.config()   # the effective config: project.json over the defaults
        for part in (a.key or "").split(".") if a.key else []:
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(a.value)
    except ValueError:
        val = a.value
    from .project import unknown_key_hint
    hint = unknown_key_hint(a.key)
    if hint:
        die(hint)
    if a.key == "runner.pin_verify":
        from .project import pin_verify_mode
        try:
            val = pin_verify_mode(val)
        except ValueError as e:
            die(str(e))
    if a.key in ("coordinator.provider_failover_after_h", "coordinator.provider_failover_to"):
        from . import failover
        try:
            val = (failover.after_h_of(val) if a.key.endswith("_after_h")
                   else failover.to_of(val, p.config().get("providers")))
        except ValueError as e:
            die(str(e))
    if a.key.startswith("heal.outage_") and a.key.endswith("_min"):
        from .outagetask import minutes
        try:
            minutes(a.key.split(".", 1)[1], val)
        except ValueError as e:
            die(str(e).replace("; the default is used", ""))
    if a.key in ("delivery.push_checks", "delivery.push_checks_by_repo"):
        from .push import checks_by_repo_of, checks_of
        try:
            val = checks_of(val) if a.key == "delivery.push_checks" else checks_by_repo_of(val)
        except ValueError as e:
            die(str(e) if a.key == "delivery.push_checks" else f"{a.key}: {e}")
    if run and (k := _user_only_change(p.raw_config(), a.key, val)):
        # A run can be steered by text it reads; these change only in a coordinator turn that carries
        # the user's yes (coordinator.NEEDS_USER), as `ttp say` is refused inside a run.
        _refuse_user_only_in_run(k)
    try:
        p.set_config(a.key, val)
    except (RuntimeError, ValueError) as e:   # project.json unreadable with no last good copy; a section given one value
        die(str(e))
    print(f"{a.key} = {json.dumps(val)}")


SECRET_KINDS = ("jev", "slack", "endpoint", "show", "check", "env", "push", "set", "generate")
SECRET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SECRET_MAX_BYTES = 64 << 10


def cmd_secret(a) -> None:
    if a.alias is not None and a.kind not in ("endpoint", "check", "env", "set", "generate"):
        die(f"`ttp secret {a.kind}` takes no alias (only endpoint, check, env, set and generate do)")
    if a.keys is not None and a.kind != "generate":
        die("--keys goes only with `ttp secret generate <name> --keys a,b,c`")
    if (a.secret or a.cmd_word) and a.kind != "env":
        die("--secret goes only with `ttp secret env --secret <name>[:<key>]=<VAR> -- <command>`")
    if a.kind in ("set", "generate"):
        return cmd_secret_custom(a)
    if a.command and a.kind != "env":
        die(f"`ttp secret {a.kind}` runs no command (only `ttp secret env <alias> -- <command>` does)")
    if a.kind == "jev":
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Jev key: ")
        from .providers.jev import verify
        ok, msg = verify(a.via, key, a.url)
        if not ok and not a.force:
            die(f"the key did not work ({msg}); not saved. Use --force to save anyway.")
        save_secret("jev", {"via": a.via, "key": key, **({"url": a.url} if a.url else {})})
        print(f"saved Jev key for this user ({a.via}; check: {msg})")
        print("Jev (optional add-on) is now on in your projects on this machine: it sends log and alert text "
              "to an outside paid service. `ttp config <name> jev.enabled false` turns it off for a project.")
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
    elif a.kind == "endpoint":
        from . import endpoint
        try:
            name = endpoint.slot(a.alias or "")
            url = endpoint.base_url(a.url or "")
        except ValueError as e:
            die(str(e) if a.alias else "give an alias: `ttp secret endpoint <alias> --url <base url>`")
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass(f"API key for {a.alias}: ")
        if not key:
            die("no key given; not saved")
        ok, msg = endpoint.verify(url, key)
        if not ok and not a.force:
            die(f"the key did not work at {url}/models ({msg}); not saved. Use --force to save anyway.")
        endpoint.save(a.alias, url, key, ok, msg)
        print(f"saved {name} for this user ({url}; check: {msg})")
    elif a.kind == "push":
        if not a.host:
            die("--host is required")
        print(push_secrets(a.host))
    elif a.kind == "check":
        # Tests a saved key without ever showing it: reachable / auth ok / auth failed.
        from . import endpoint
        aliases = [a.alias] if a.alias else sorted(endpoint.saved())
        if not aliases:
            die("no endpoint key saved: `ttp secret endpoint <alias> --url <base url>`")
        bad = 0
        for alias in aliases:
            try:
                ok, msg = endpoint.check(alias)
            except ValueError as e:
                die(str(e))
            print(msg)
            bad += not ok
        sys.exit(1 if bad else 0)
    elif a.kind == "env":
        cmd_secret_env(a)
    elif a.kind == "show":
        from . import endpoint
        sec = load_secrets()
        print(json.dumps({k: sorted(v.keys()) if isinstance(v, dict) else "set" for k, v in sec.items()
                          if not k.startswith(endpoint.PREFIX)}))
        for line in endpoint.show_lines():
            print(line)


def _secret_name(name: str | None, usage: str) -> str:
    """A custom secret's name, or die: lowercase letters, digits, '-' and '_', never a built-in one."""
    if not name:
        die(f"give a name: `ttp secret {usage}`")
    if not SECRET_NAME_RE.fullmatch(name):
        die(f"bad secret name {name!r}: lowercase letters, digits, '-' and '_' (up to 64, starting "
            "with a letter or digit)")
    from . import redact
    if name in SECRET_KINDS or not redact.custom(name):
        die(f"secret name {name!r} is taken by `ttp secret {name}`; pick another name")
    return name


def cmd_secret_custom(a) -> None:
    """`ttp secret set <name>` (the value from stdin) and `ttp secret generate <name> --keys a,b,c`
    (a fresh random token per key, stored as a JSON object). Neither prints a value."""
    from . import redact
    for opt in ("host", "url", "email", "user_id"):
        if getattr(a, opt):   # never echo it: it may be the value
            die(f"`ttp secret {a.kind}` takes no --{opt.replace('_', '-')}")
    if a.kind == "set":
        name = _secret_name(a.alias, "set <name> < file-with-the-value")
        if a.command:   # never echo it back: it is in argv already
            die("the value is read from stdin, never the command line (that lands in shell history and "
                f"logs): `ttp secret set {name} < file`, or type it at the prompt")
        if sys.stdin.isatty():
            value = getpass.getpass(f"value for {name}: ")
        else:
            value = sys.stdin.read(SECRET_MAX_BYTES + 1)
            value = value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith("\n") else value
        if len(value.encode()) > SECRET_MAX_BYTES:
            die(f"the value is over {SECRET_MAX_BYTES >> 10} KiB; not saved")
        if not value.strip():
            die("empty value on stdin; not saved")
        if len(value.strip()) < redact.MIN_LEN:
            die(f"the value has fewer than {redact.MIN_LEN} characters, too short to be masked in logs; "
                "not saved")
        old = name in load_secrets()
        save_secret(name, value)
        print(f"stored secret {name}" + (" (replaced the earlier value)" if old else ""))
        return
    name = _secret_name(a.alias, "generate <name> --keys a,b,c")
    if a.command:
        die("`ttp secret generate` takes no value: it makes the tokens itself")
    keys = [k.strip() for k in (a.keys or "").split(",")]
    if keys == [""]:
        die(f"give the key names: `ttp secret generate {name} --keys a,b,c`")
    for k in keys:
        if not SECRET_NAME_RE.fullmatch(k):
            die(f"bad key name {k!r}: lowercase letters, digits, '-' and '_'")
    if len(set(keys)) != len(keys):
        die("a key name is listed twice")
    if name in load_secrets() and not a.force:
        die(f"secret {name} exists; --force replaces it with new tokens")
    save_secret(name, {k: pysecrets.token_urlsafe(32) for k in keys})
    print(f"stored secret {name} with a new random token for each of: {', '.join(keys)}")


ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _secret_env_vars(specs: list[str]) -> dict[str, str]:
    """{VAR: stored value} for each `--secret <name>[:<key>]=<VAR>` (custom secrets only). Errors name
    the secret and key only once they are valid names, and never a value."""
    from . import redact
    store, out, by = load_secrets(), {}, {}
    for spec in specs:
        ref, eq, var = spec.partition("=")
        name, colon, key = ref.partition(":")
        if not eq:
            die("--secret takes <name>[:<key>]=<VAR>")
        if not SECRET_NAME_RE.fullmatch(name):
            die("bad secret name in --secret: lowercase letters, digits, '-' and '_'")
        if colon and not SECRET_NAME_RE.fullmatch(key):
            die(f"bad key name for secret {name}: lowercase letters, digits, '-' and '_'")
        label = f"{name}:{key}" if colon else name
        if name in SECRET_KINDS or not redact.custom(name):
            die(f"secret {name} is built in (`ttp secret {name}`); --secret gives only secrets made with "
                "`ttp secret set` or `ttp secret generate`")
        if not ENV_VAR_RE.fullmatch(var):
            die(f"bad variable name for {label}: letters, digits and '_', not starting with a digit")
        if var in by:
            die(f"{by[var]} and {label} set the same variable")
        if name not in store:
            die(f"no secret {name}: store it with `ttp secret set {name}` or `ttp secret generate {name} --keys ...`")
        v = store[name]
        if colon and not isinstance(v, dict):
            die(f"secret {name} is a single value, not keys: use --secret {name}=<VAR>")
        if colon and key not in v:
            die(f"secret {name} has no key {key} (its keys: {', '.join(sorted(map(str, v)))})")
        if not colon and isinstance(v, dict):
            die(f"secret {name} holds keys ({', '.join(sorted(map(str, v)))}): "
                f"pick one with --secret {name}:<key>=<VAR>")
        v = v[key] if colon else v
        if not isinstance(v, str) or "\0" in v:
            die(f"secret {label} is not a text value")
        out[var], by[var] = v, label
    return out


def cmd_secret_env(a) -> None:
    """`ttp secret env <alias> -- <command>`: runs the command with the endpoint's key and URL in its
    environment (OPENAI_API_KEY, OPENAI_BASE_URL), its output passed on with every stored secret masked.
    `--secret <name>[:<key>]=<VAR>` (repeatable, with or without an alias) adds a custom secret as VAR."""
    from . import endpoint, redact
    if a.secret:
        argv = [w[1:] for w in a.cmd_word or []]
        if a.command:   # more than one word before `--`
            argv = []
    else:
        argv = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not (a.alias or a.secret) or not argv:
        die("usage: ttp secret env <alias> -- <command...>, or "
            "ttp secret env [alias] --secret <name>[:<key>]=<VAR> ... -- <command...>")
    env = dict(os.environ)
    if a.alias:
        try:
            env.update(endpoint.env_for(a.alias))
        except ValueError as e:
            die(f"{e}: `ttp secret endpoint {a.alias} --url <base url>`")
    given = _secret_env_vars(a.secret or [])
    env.update(given)
    # A multi-line value is masked line by line too: the output is passed on one line at a time.
    parts = sorted({(p.strip(), spec.partition("=")[0].partition(":")[0])
                    for spec, v in zip(a.secret or [], given.values())
                    for p in v.splitlines() if "\n" in v.strip() and len(p.strip()) >= redact.MIN_LEN},
                   key=lambda x: -len(x[0]))
    try:
        child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        die(f"cannot run {argv[0]}: {e.strerror or e}")
    try:
        for line in iter(child.stdout.readline, b""):
            line = redact.scrub(line.decode(errors="replace"))
            for p, name in parts:
                line = line.replace(p, redact.mask(name))
            sys.stdout.write(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        child.terminate()
    rc = child.wait()
    sys.exit(128 - rc if rc < 0 else rc)


def cmd_logs(a) -> None:
    p = need(a.name, sys.argv[1:])
    f = p.logs / "daemon.log"
    print(f.read_text()[-a.bytes:] if f.exists() else "(no log yet)")


def stale_install_warning(cwd: Path | None = None) -> str:
    """Warn when an installed ttp runs setup inside a checkout that holds a newer plugin."""
    from .project import HOME_DIR
    from .release import is_newer, runtime_version
    try:
        if not RUNTIME.resolve().is_relative_to((HOME_DIR / "lib").resolve()):
            return ""
    except OSError:
        return ""
    here = (cwd or Path.cwd()).resolve()
    for d in (here, *here.parents):
        plugin = d / "plugins" / "tt-project"
        if (plugin / "runtime").is_dir() and (plugin / "bin" / "ttp").exists():
            newer = runtime_version(plugin / "runtime")
            if is_newer(newer, __version__):
                msg = (f"warning: this is the installed ttp {__version__}, but this checkout has {newer}; "
                       f"to install the checkout run `{plugin / 'bin' / 'ttp'} setup`")
                print(msg, file=sys.stderr)
                return msg
            return ""
    return ""


def new_project_defaults_lines() -> list[str]:
    """The defaults a new project gets, with the account-level budget settings applied."""
    from .project import DEFAULT_CONFIG
    budget = deep_merge(DEFAULT_CONFIG["budget"], load_account_settings().get("budget") or {})
    return defaults_lines(budget, bool((load_secrets().get("jev") or {}).get("key")))


def shim_text(python: str, launcher: Path) -> str:
    """The `ttp` on PATH: the interpreter setup ran under, else (its venv or conda env removed) python3 from
    PATH, with a hint on stderr."""
    py, hint = shlex.quote(python), shlex.quote(f"ttp: {python} is gone; using python3 from PATH "
                                                "(rerun `ttp setup` to pin an interpreter)")
    return (f"#!/bin/sh\npy={py}\n"
            f"if [ ! -x \"$py\" ]; then echo {hint} >&2; py=python3; fi\n"
            f"exec \"$py\" {shlex.quote(str(launcher))} \"$@\"\n")


def cmd_setup(a) -> None:
    """Install this runtime as the user's stable `ttp` (plugin caches move on every update), and the plain
    `/tt-project` command for Claude Code (shortcut.py)."""
    from .project import HOME_DIR
    from .release import forced_mark, forced_version, is_newer, runtime_version
    lib = HOME_DIR / "lib" / __version__
    from . import cyclenotice
    _cycle_hold(cyclenotice.host_paths(), a)
    if not (PLUGIN_ROOT / "template").is_dir():
        die(f"{RUNTIME} is a project's harness copy, not the plugin; run `<plugin-root>/bin/ttp setup`", 1)
    installed = runtime_version(HOME_DIR / "lib" / "current" / "runtime")
    if is_newer(installed, __version__) and not a.force:
        die(f"ttp {installed} is installed, newer than this ttp {__version__}: not downgrading it. "
            f"Run setup from the newer plugin, or add --force to install {__version__} anyway", 1)
    stale_install_warning()
    _recover_setup()
    commit = source_commit()
    before = recorded_commit(lib / "runtime") if (lib / "runtime").is_dir() else ""
    cur = HOME_DIR / "lib" / "current"
    jr = HOME_DIR / "lib" / SETUP_JOURNAL
    if RUNTIME.resolve() != (lib / "runtime").resolve():
        from .runner import boot_id
        lib.parent.mkdir(parents=True, exist_ok=True)
        durable_write(jr, json.dumps({"pid": os.getpid(), "boot": boot_id(), "source": str(PLUGIN_ROOT),
                                      "lib": str(lib), "commit": commit, "existed": lib.exists(),
                                      "prev": os.readlink(cur) if cur.is_symlink() else ""}))
        _install_parts(PLUGIN_ROOT, lib)
    durable_write(lib / "runtime" / "ttp" / SOURCE_FILE, commit + "\n", sync=False)
    sync_all()    # a reboot now must not leave empty files that upgrades take in as upstream
    # The marker goes first: a daemon check between the two steps must not undo a deliberate downgrade.
    if is_newer(installed, __version__):    # a deliberate downgrade: daemons must not undo it
        durable_write(forced_mark(), __version__ + "\n")
    elif forced_version() != __version__:  # re-running setup of the forced version keeps it forced
        forced_mark().unlink(missing_ok=True)
    if cur.exists() and not cur.is_symlink():
        shutil.rmtree(cur) if cur.is_dir() else cur.unlink()
    from .release import point_current
    point_current(lib)       # in one step: a `ttp` starting meanwhile finds the old release or this one
    jr.unlink(missing_ok=True)
    bindir = Path(a.bin_dir).expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "ttp"
    durable_write(shim, shim_text(sys.executable, cur / "bin" / "ttp"), mode=0o755)
    on_path = str(bindir) in os.environ.get("PATH", "").split(":")
    print(f"ttp {__version__} ({commit}) installed: {shim}" + ("" if on_path else f" (add {bindir} to PATH)"))
    from .service import plan
    print(f"daemon service for new projects here: {plan()}")
    if before and before != commit:
        print(f"replaced ttp {__version__} from commit {before} with commit {commit}")
    from . import shortcut
    line = shortcut.remove() if a.no_shortcut else shortcut.install()
    if line:
        print(line)
    from . import notifier
    for ln in notifier.refresh(send_test=True, always=True):   # an installed notifier runs this code from now on
        print(ln)
    from .project import DEFAULT_CONFIG
    print("defaults for new projects:")
    for ln in new_project_defaults_lines():
        print("  " + ln)


def cmd_uninstall(a) -> None:
    """Remove ttp's services, crontab lines, shortcut, shim and lib for this user (uninstall.py); keep projects."""
    if os.environ.get("TTP_RUN_DIR"):
        die("ttp uninstall: refused inside a run; the user runs it", 2)
    from . import uninstall
    bin_dir = Path(a.bin_dir).expanduser()
    acts = uninstall.plan(bin_dir)
    print(("would " if a.dry_run else "") + ("remove:" if acts else "remove nothing: no ttp service, shortcut, shim, lib or PATH line here"))
    for what, _ in acts:
        print(f"  {what}")
    if acts and not a.dry_run:
        if not a.yes:
            try:
                ok = input("go ahead? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                ok = False
            if not ok:
                die("ttp uninstall: nothing changed (add --yes to skip the question)", 1)
        left = [f"{what.splitlines()[0]}: {err}" for what, step in acts if (err := step())]
        for ln in left:
            print(f"  not done: {ln}", file=sys.stderr)
        print("done" + (f", {len(left)} step(s) not done" if left else ""))
    print("left in place (remove by hand if you want them gone):")
    for ln in uninstall.leftovers(bin_dir):
        print(f"  {ln}")


def cyclenotice_paths(p: Project) -> list[str]:
    from . import cyclenotice
    return cyclenotice.project_paths(p)


def _cycle_hold(paths: list[str], a, where=None, auto_rec: Project | None = None) -> None:
    """Exit 75 (busy), changing nothing, while a device broker announces a power cycle of this host
    (cyclenotice.py); with --ignore-cycle-notice go on with a warning. `auto_rec`: the daemon's own
    upgrade, recorded as held (no attempt spent; the daemon retries once the notice is gone)."""
    from . import cyclenotice
    h = cyclenotice.held(paths)
    if not h:
        return
    why = cyclenotice.line(h, where)
    if getattr(a, "ignore_cycle_notice", False):
        print(f"warning: {why}; going ahead (--ignore-cycle-notice)", file=sys.stderr)
        return
    if auto_rec is not None:
        from . import release
        release.finish(auto_rec, "held", why=why)
    die(f"held: {why}; nothing was changed. It goes ahead once the notice is gone "
        f"(--ignore-cycle-notice proceeds anyway)", cyclenotice.BUSY)


SETUP_JOURNAL = ".setup.journal"     # in HOME_DIR/lib while `ttp setup` copies a release in (_recover_setup)
SETUP_PARTS = ("runtime", "template", "bin", "skills")    # skills: readable by workers of any project


def _install_parts(src_root: Path, lib: Path) -> None:
    for part in SETUP_PARTS:
        src = src_root / part
        if src.exists():
            _copy_tree(src, lib / part)
    for f in (lib / "bin").iterdir():
        f.chmod(0o755)


def _recover_setup() -> str:
    """A `ttp setup` cut short (a reboot mid-copy) leaves its journal: the next `ttp` command finishes it
    (the copy is redone from the same plugin folder, then lib/current points at it) or, with that folder
    gone or updated to another version, rolls it back (lib/current points at the release it did before, and a release folder the run
    created is removed). Returns the line it printed ("" when there was nothing to do)."""
    from .project import HOME_DIR
    from .release import point_current, runtime_version
    from .runner import same_boot
    jr = HOME_DIR / "lib" / SETUP_JOURNAL
    try:
        rec = json.loads(jr.read_text())
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        rec = {}
    rec = rec if isinstance(rec, dict) else {}
    pid = rec.get("pid")
    if same_boot(rec.get("boot")) and isinstance(pid, int) and pid != os.getpid():
        try:
            os.kill(pid, 0)
            return ""       # it still runs
        except ProcessLookupError:
            pass
        except PermissionError:
            return ""
    lib, src = Path(rec.get("lib") or "/nonexistent"), Path(rec.get("source") or "/nonexistent")
    # The plugin folder updated meanwhile holds another version than the folder setup named: roll back.
    if lib.parent == HOME_DIR / "lib" and (src / "runtime" / "ttp").is_dir() and (src / "template").is_dir() \
            and runtime_version(src / "runtime") == lib.name:
        _install_parts(src, lib)
        durable_write(lib / "runtime" / "ttp" / SOURCE_FILE, str(rec.get("commit") or "unknown") + "\n", sync=False)
        sync_all()
        point_current(lib)
        what = f"finished: {lib.name} installed again from {src}"
    else:
        prev = Path(str(rec.get("prev") or ""))
        prev = prev if prev.is_absolute() else HOME_DIR / "lib" / prev
        if str(rec.get("prev") or "") and prev.is_dir():
            point_current(prev)
        if lib.parent == HOME_DIR / "lib" and not rec.get("existed") and lib.is_dir() and lib != prev:
            shutil.rmtree(lib, ignore_errors=True)
        what = (f"rolled back to {prev.name}" if str(rec.get("prev") or "") else "rolled back") + \
            "; rerun `ttp setup` from the plugin"
    jr.unlink(missing_ok=True)
    line = f"an earlier `ttp setup` was cut short (a reboot?); {what}"
    print(line, file=sys.stderr)
    return line


def cmd_upgrade(a) -> None:
    """Merge the installed template into a project's harness. The harness repo keeps pristine
    template snapshots on its `upstream` branch, so this is an ordinary three-way merge. `--auto` is
    the daemon's own call (release.py): it records the outcome and sends one low notify when applied.
    Exit 75 = deferred, nothing changed and the project finishes it itself (another upgrade, a harness
    task or the daemon landing a harness worktree holds it, a push is in flight, or a conflicting merge waits for a harness task or for the daemon's
    retry, which comes only for a newer version under upgrade.auto). Exit 1 = failed, including a conflict
    nothing will retry (upgrade.auto off, or the same version from another commit). After main moved, the
    restart decides: exit RESTART_DEFERRED (76) = main is applied and the restart deferred (the old daemon
    runs on and restarts when it takes the request a sandboxed caller left it), exit 1 = it failed or the
    runtime was rolled back. 75 never means main moved."""
    from . import locks, release
    if a.project_dir:      # the daemon names its own folder: never another project of the same name
        p = Project(a.project_dir)
        if not p.exists() or p.name != a.name:
            die(f"{a.project_dir} does not hold project {a.name!r}")
    else:
        entry = remote_entry(a.name)
        if entry and not local_project(a.name):
            from . import machines as mm
            ship_runtime(entry.get("ssh") or entry["host"])      # the newer runtime becomes that machine's ttp
            print(mm.push(entry.get("ssh") or entry["host"]))
            sys.exit(forward(entry, sys.argv[1:]))
        p = need(a.name, sys.argv[1:])
    _cycle_hold(cyclenotice_paths(p), a, p, auto_rec=p if a.auto else None)
    from . import notifier
    for ln in notifier.refresh(send_test=not a.auto):   # this machine's notifier on the installed code too
        print(ln)
    held = locks.try_take([release.upgrade_lock(p)], f"ttp upgrade (pid {os.getpid()})", "ttp upgrade")
    if held is None:
        who = locks.holders_now([release.upgrade_lock(p)], p)
        landing = bool(who) and "landing harness worktrees" in who[0]   # the daemon's (harnesstree)
        if a.auto:
            release.finish(p, "held", why="the daemon is landing harness worktrees" if landing
                           else "another upgrade is running")
        what = (f"the daemon is landing harness worktrees of {p.name}" if landing
                else f"another upgrade of {p.name} is running")
        die(f"upgrade refused: {what}" + (f" ({who[0]})" if who else "") + "; nothing was changed"
            + ("; main is as it was and no restart is coming: rerun this --apply once it ends"
               if a.apply is not None else ""), UPGRADE_BUSY)
    from . import upjournal
    try:
        upjournal.recover(p, restart=_restart_daemon)    # an earlier run cut short: finish or roll it back
        upjournal.begin(p, "apply" if a.apply is not None else "upgrade", a.auto)
        own = _own_run(p)
        if a.apply is not None and own and release.fenced(p, os.environ.get("TTP_TASK") or ""):
            die(f"--apply refused: a host-side upgrade took the template merge over from task "
                f"#{os.environ['TTP_TASK']} (its finish-lock generation is older); nothing was changed", 1)
        # Read under the lock: an upgrade that just ended may have queued it. A stale record goes here.
        holder = release.finish_holder(p, clean=True)
        tid = holder["task"] if holder else None
        mine = bool(tid) and os.environ.get("TTP_TASK") == str(tid)
        took, parked = False, None
        if tid and not mine and a.apply is None and not a.auto and not own:
            # A blocked or idle task cannot hold off a host-side upgrade, paused or not: the pause stays and
            # a conflict queues the merge task again, held by the pause like any other task.
            took = _take_over_finish(p, holder, overdue=getattr(a, "take_over_from", None) == tid)
            if took:
                holder, tid = None, None
        if tid and not mine and a.apply is None and not a.auto and not own and p.db.kv("paused", False):
            # A paused project's merge task cannot run: the merge is tried here, files only, and the task
            # is superseded only once it merged cleanly; a conflict leaves it to finish after the resume.
            parked, holder, tid = holder, None, None
        if tid and not mine and a.apply is None:    # the open task takes this release on: one merge, not two
            release.guard_harness(p.harness)
            _retarget_upgrade_task(p, holder, a.auto)
        if tid and not mine:      # its worker is resolving the merge: a second resolution would race it
            release.note_deferred(p, task=tid)
            if a.auto:
                release.finish(p, "held", why=f"harness task #{tid} finishes an earlier upgrade")
            die(f"upgrade refused: harness task #{tid} is finishing an earlier template upgrade of {p.name}; "
                f"nothing was changed ({release.holder_line(p, holder)}).", 75)
        if a.apply is None and mine:
            die(f"task #{tid} finishes the merge in its own worktree and applies it with "
                f"`ttp upgrade {p.name} --apply <commit>`", 2)
        release.guard_harness(p.harness)
        if a.apply is not None:
            _apply_upgrade(p, a.apply)
        else:
            _upgrade(p, a.auto, explicit=not a.auto and not own, took_over=took, parked=parked)
        upjournal.end(p)
    except SystemExit as e:
        upjournal.end(p)        # it refused, failed or finished on purpose: whatever it changed is whole
        if a.auto and e.code and (p.db.kv(release.KV_AUTO) or {}).get("outcome") == "running":
            release.finish(p, "failed", why=f"exit {e.code}; see logs/upgrade.log")
        raise
    except Exception as e:
        if a.auto:
            release.finish(p, "failed", why=f"{type(e).__name__}: {str(e)[:200]}")
        raise
    finally:
        held.close()


def _restart_daemon(p: Project):
    from . import service
    return service.restart(p)


def _own_run(p: Project) -> bool:
    """This command runs inside one of `p`'s own runs (a task or coordinator turn of that project)."""
    base = os.environ.get("TTP_PROJECT")
    try:
        return bool(base) and Path(base).resolve() == Path(p.base).resolve()
    except OSError:
        return False


def _take_over_finish(p: Project, holder: dict, overdue: bool = False) -> bool:
    """An explicit host-side upgrade while finish task holder["task"] is blocked or idle past
    release.STALE_S, or (`overdue`: `--take-over-from` names it) past the takeover deadline a deploy gave
    it: fence that task (cancelled, superseded; its `--apply` refused from now on) so this upgrade
    proceeds. True when it took the lock over."""
    from . import release, sweep
    from .project import HOME_DIR
    why = release.stale_holder(p.db, holder) or ("past its deploy takeover deadline" if overdue else "")
    src = HOME_DIR / "lib" / "current" / "runtime"
    if not why or not src.is_dir():
        return False
    tid = holder["task"]
    target = f"{_runtime_version(src)} ({recorded_commit(src)})"
    gen = release.fence(p, tid, target)
    sweep.signal(p.db, f"host upgrade took the template merge over from harness task #{tid} ({why})")
    print(f"harness task #{tid} held the template merge but is {why}; this host-side upgrade takes it over: "
          f"task #{tid} is cancelled (superseded by host upgrade to {target}) and its `--apply` is refused "
          f"(finish-lock generation {gen})")
    return True


def _upgrade(p: Project, auto: bool = False, explicit: bool = False, took_over: bool = False,
             parked: dict | None = None) -> None:
    """`explicit`: run by hand on the host, not --auto and not from one of the project's runs. A conflict
    then queues the merge task once the last one ended upgrade.merge_retry_h ago (`took_over`: at once,
    the last one was just superseded), not only once a day. `parked`: the project is paused and this finish
    task holds the merge: a clean merge supersedes it and lands (the daemon restarts, still paused); a
    conflict retargets it and exits 75, the task finishing it once the project is resumed."""
    from . import release, upjournal
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
    _ensure_git_ident(h)        # harness tasks commit here with plain `git commit`
    _refuse_unfinished_merge(p, auto)
    emptied = _emptied(h, None, "HEAD")
    if emptied:      # a crash cut these short: they are damage, not local edits to commit and keep
        _git(h, "checkout", "HEAD", "--", *emptied)
        print("restored files a crash left empty: " + ", ".join(emptied))
    _restore_cut_runtime(h)
    left = _commit_local_changes(p, ident)
    base = _git(h, "rev-parse", "HEAD")     # the merge is checked against this; main must not move meanwhile
    upjournal.step(p, "committed", base=base)
    _snapshot_upstream(p, src, new_v, new_c, ident)
    kept: dict[str, str] = {}
    merged, problem = _merge_upstream(h, p.state / "upgrade-merge", ident, base, kept)
    if problem and parked:      # the open task is retargeted (exit 75); nothing is queued while paused
        print(f"{p.name} paused; upgrades when resumed: the merge needs harness task #{parked['task']}, which "
              f"runs once {p.name} is resumed. Nothing else was changed.")
        _retarget_upgrade_task(p, parked, False)
        sys.exit(75)
    if problem:
        holder = release.finish_holder(p)
        tid = holder["task"] if holder else None
        last = None if tid else release.recent_upgrade_task(p)
        if last and release.landed(p.db, last["id"], old_v, old_c):
            last = None     # it landed its release: this conflict is the next release's, queued at once
        sooner = None       # an explicit host-side upgrade may queue the merge task this soon
        if last and explicit:
            ended = release.task_ended(p.db, last["id"])
            sooner = None if ended is None else ended + release.merge_retry_s(p.config())
            if took_over or (sooner is not None and time.time() >= sooner):
                last = None
        if last:        # at most one model task a day: each release would otherwise queue its own
            until = float(last["created"]) + release.TASK_EVERY_S
            release.note_deferred(p, problem, last["id"])
            release.defer(p, auto, f"{new_v} {new_c}", f"{old_v} ({old_c})", f"{new_v} ({new_c})", until,
                          problem[:300])
            at = timefmt.long(until, p)
            took = (f"upgrade not applied; the running harness is unchanged. {problem}\nHarness task "
                    f"#{last['id']} took on a template merge less than a day ago, so none is queued now; ")
            # Exit 75 (deferred) only when the project takes it on itself: its daemon retries after `until`.
            if sooner is not None and sooner < until:
                at = timefmt.long(sooner, p)    # a host-side rerun queues it before the daemon's daily retry
                took += f"a host-side `ttp upgrade {p.name}` queues one from {at}; "
            if release.retried({"newer": release.is_newer(new_v, old_v)}, p.config()):
                print(took + f"upgrade deferred: owner: this project's daemon (upgrade.auto), which tries again "
                             f"after the day is up; resumes at {timefmt.long(until, p)}.")
                sys.exit(75)
            if p.db.kv("paused", False):    # deferred, not failed: a takeover after the resume queues the merge
                print(took + f"{p.name} paused; upgrades when resumed: a host-side `ttp upgrade {p.name}` after "
                             f"the resume queues the merge task from {at}.")
                sys.exit(75)
            why = ("upgrade.auto is off" if release.is_newer(new_v, old_v)
                   else f"the daemon retries only a newer version, and this is {new_v} again from another commit")
            print(took + f"{why}, so nothing retries it: rerun `ttp upgrade {p.name}` after {at} "
                         f"to queue a harness task for the merge.")
            sys.exit(1)
        target = f"{new_v} ({new_c})"
        tid = tid or p.db.add_task(
            release.UPGRADE_TASK_TITLE, _UPGRADE_TASK.format(problem=problem, name=p.name, target=target,
                                                             state=shlex.quote(str(p.state))),
            kind="harness", tier="standard", priority=2, origin="user")
        release.take_finish_lock(p, tid, f"{new_v} {new_c}", target)     # it holds the live harness until it ends
        release.note_deferred(p, problem, tid)
        if auto:
            release.finish(p, "conflict", task=tid, why=problem[:300])
        print(f"upgrade not applied; the running harness is unchanged. {problem}\nHarness task #{tid} "
              f"finishes it (upgrade deferred to that task)."
              + (f"\n{p.name} paused; upgrades when resumed: task #{tid} runs once {p.name} is resumed "
                 f"(the pause was left as it is)." if p.db.kv("paused", False) else ""))
        sys.exit(75)        # deferred: the project's own harness task takes it on
    if auto and release.push_in_flight(p):   # a push started while this merged: never swap under it
        release.finish(p, "held", why="a push is in flight")
        print("upgrade held: a push is in flight; the running harness is unchanged and the daemon retries later")
        sys.exit(75)
    moved = _moved_template(h, base)
    if moved:
        if auto:
            release.finish(p, "failed", why=moved[:300])
        die(f"upgrade not applied; the running harness is unchanged. {moved}", 1)
    genv = {**os.environ, release.GUARD_ENV: "1"}
    upjournal.step(p, "merging", base=base, merged=_git(h, "rev-parse", merged))
    r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--ff-only", merged], capture_output=True, text=True,
                       env=genv)
    if r.returncode != 0:   # the daemon committed charter or memory meanwhile: those touch other files
        r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--no-edit", merged], capture_output=True,
                           text=True, env=genv)
        if r.returncode != 0:
            subprocess.run(["git", "-C", str(h), "merge", "--abort"], capture_output=True)
            die(f"could not apply the checked upgrade to {h}: {(r.stdout + r.stderr)[-500:]}"
                + (f"; files a running harness task changed were left for it to commit ({', '.join(left[:5])}): "
                   f"rerun `ttp upgrade {p.name}` once it has committed them" if left else ""), 1)
    sync_all()       # a reboot right after must not leave the files git just wrote empty
    upjournal.step(p, "merged")
    if parked:      # merged without its worker: the paused project's finish task has nothing left to do
        release.fence(p, parked["task"], f"{new_v} ({new_c})")
        print(f"{p.name} is paused: merged its files without harness task #{parked['task']}, which is cancelled "
              f"(superseded); the daemon restarts and the project stays paused")
    release.clear_stuck(p)
    keep_note = _keep_light_coordinator(p)
    if keep_note:
        print(keep_note)
    if ttl_note := _auto_cache_ttl(p):
        print(ttl_note)
    if caps_note := _migrate_load_caps(p):
        print(caps_note)
    print("harness up to date with the installed template; restarting the daemon")
    from . import service
    upjournal.step(p, "restarting")
    res = service.restart(p)
    if not auto:
        _exit_for_restart(res)
        return
    print(res)
    if (_runtime_version(h / "runtime"), recorded_commit(h / "runtime")) != (new_v, new_c):
        release.finish(p, "failed", why="the daemon did not start with it, so the runtime was rolled back")
        return
    release.finish(p, "applied", **({"kept_lost": _cut_list(kept)} if kept else {}))
    p.db.post("out", f"tt-project harness upgraded from {old_v} ({old_c}) to {new_v} ({new_c}); the daemon "
              f"restarted and running work was kept."
              + (f" Kept this project's runtime edits that drop names upstream ships: {_cut_list(kept)}." if kept
                 else ""), chat=None, kind="alert", severity="low")


def _keep_light_coordinator(p: Project) -> str:
    """Since coordinator.model_tier an unset coordinator.model follows the standard tier. A project that
    overrides a provider's light-tier model to make its coordinator cheaper keeps that model: model_tier
    is set to "" for it (once; an explicit model_tier is the project's own choice). Returns the note."""
    try:
        raw = p.raw_config()
    except Exception:
        return ""
    coord = raw.get("coordinator")
    if isinstance(coord, dict) and "model_tier" in coord:
        return ""
    provs = raw.get("providers")
    if not isinstance(provs, dict) or not any(
            isinstance(v, dict) and isinstance(((v.get("tiers") or {}).get("light") or {}), dict)
            and ((v.get("tiers") or {}).get("light") or {}).get("model") for v in provs.values()):
        return ""
    try:
        p.set_config("coordinator.model_tier", "")
    except Exception:
        return ""
    return ("upgrade note: this project overrides the light-tier model, so coordinator.model_tier was set to "
            "\"\" and its coordinator keeps the light-tier model; `ttp config %s coordinator.model_tier "
            "standard` makes it follow the standard tier" % p.name)


def _auto_cache_ttl(p: Project) -> str:
    """An empty coordinator.cache_ttl, left by an older template, is set to "auto" (the runtime already reads
    it so; an explicit value is the project's own choice). Returns the note."""
    try:
        coord = p.raw_config().get("coordinator")
        if not (isinstance(coord, dict) and "cache_ttl" in coord and not str(coord["cache_ttl"] or "").strip()):
            return ""
        p.set_config("coordinator.cache_ttl", "auto")
    except Exception:
        return ""
    return 'upgrade note: coordinator.cache_ttl was "" (from an older template) and is now "auto"'


def _migrate_load_caps(p: Project) -> str:
    """Each loadcaps.OLD_DEFAULTS key project.json still holds at its old fixed default is removed, so the
    default sized to the project's load applies; any other value is the project's own and stays, and no
    other key is touched. A second run changes nothing. Returns the note."""
    from . import loadcaps
    try:
        raw = p.raw_config()
        if p.config_status == "unavailable":
            return ""
        new, done = loadcaps.migrate(raw)
        if not done:
            return ""
        write_json(p.config_path, new)
    except Exception:
        return ""
    return "upgrade note: defaults now sized to the project's load: " + "; ".join(done)


def _running_harness_start(p: Project) -> tuple[float, int] | None:
    """(start, task) of the oldest run of this project's harness tasks still running, leaving out the
    run this command runs in (its worker commits its own files before it upgrades); None when none runs."""
    me = os.environ.get("TTP_RUN_ID") if _own_run(p) else None
    r = p.db.one("SELECT r.started, r.task FROM runs r JOIN tasks t ON t.id=r.task WHERE r.status='running' "
                 "AND t.kind='harness' AND r.started IS NOT NULL AND CAST(r.id AS TEXT) IS NOT ? "
                 "ORDER BY r.started LIMIT 1", (me,))
    return (float(r["started"]), int(r["task"])) if r else None


def _commit_local_changes(p: Project, ident: list[str]) -> list[str]:
    """Commit the harness's local changes before a template upgrade merges into it. While harness tasks of
    the project run, they edit this same checkout and commit with their own message: only changes older
    than the oldest such run's start are committed here, and newer ones (and deletions, whose age is
    unknown) are left for their worker. Waiting for the run instead could hold an upgrade for hours.
    Returns the files left uncommitted."""
    h = p.harness
    if not _git(h, "status", "--porcelain"):
        return []

    def names(*args: str) -> set[str]:
        out = subprocess.run(["git", "-C", str(h), *args], capture_output=True, text=True, check=True).stdout
        return {n for n in out.split("\0") if n}

    msg = "local harness changes before template upgrade"
    running = _running_harness_start(p)
    kept_out = _keep_out_runtime_data(h, running[0] if running else None)
    if not running:
        _git(h, "add", "-A")
        files = sorted(names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
        if plans := _refresh_plan_digests(p, files):
            _git(h, "--literal-pathspecs", "add", "--", *plans)
            files = sorted(names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
        if files:
            _commit_listing(p, ident, msg, files, kept_out)
        return []
    since, tid = running

    changed = (names("ls-files", "-z", "--modified", "--others", "--deleted", "--exclude-standard")
               | names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
    old, left = [], []
    for n in sorted(changed):
        try:
            (old if (h / n).lstat().st_mtime <= since else left).append(n)
        except OSError:
            left.append(n)
    if old:
        # A plan file a running task edited stays its own: its digests are not refreshed here.
        old += [n for n in _refresh_plan_digests(p, old, set(left)) if n not in old]
        spec = p.state / "upgrade-commit-paths"
        spec.write_text("\0".join(old) + "\0")
        try:
            files = ["--pathspec-from-file", str(spec), "--pathspec-file-nul"]
            _git(h, "--literal-pathspecs", "add", "-A", *files)
            # --only: the worker's own staged files stay staged and out of this commit
            done = sorted(names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD") & set(old))
            if done:
                _commit_listing(p, ident, msg, done, kept_out, ["--literal-pathspecs"], ["--only", *files])
        finally:
            spec.unlink(missing_ok=True)
    if left:
        print(f"left {len(left)} file(s) changed since harness task #{tid}'s run started for it to commit: "
              f"{', '.join(left[:10])}" + (f" and {len(left) - 10} more" if len(left) > 10 else ""))
    return left


def _refresh_plan_digests(p: Project, files: list[str], busy: set[str] = frozenset()) -> list[str]:
    """The project's harness check plans pin inputs by digest; a pre-upgrade commit of `files` that
    changes one would fail the next harness task's checks on a digest that task never touched. A plan
    read at HEAD (--definition-at-head) gets their digests refreshed in the working copy, to go in the
    same commit; one in `busy` (a running task's uncommitted edit) or at a pinned definition commit
    cannot be, and is named. Returns the plan files rewritten."""
    from . import checkplan, push
    h, plans = p.harness, []
    try:
        checks = push.all_checks(p.config().get("delivery") or {})
    except Exception:
        return []
    for c in checks:
        args = checkplan.harness_args(str(c)) if getattr(c, "purpose", "") == "harness" else None
        if not args or args[1] in plans:
            continue
        pinned, path = args
        if pinned or path in busy:
            try:
                pins = json.loads((h / path).read_text())["coverage"]["paths"]
            except (OSError, ValueError, TypeError, KeyError):
                continue
            hit = sorted(n for n in files if isinstance(pins, dict) and n in pins and pins[n] != checkplan.COMMITTED)
            if hit:
                why = (f"its definition is pinned at commit {pinned[:12]}" if pinned
                       else "a running harness task has uncommitted edits to it")
                print(f"upgrade note: this commit changes {', '.join(hit)}, which harness check plan {path} pins "
                      f"by digest; {why}, so the digest was not refreshed and harness checks fail on it until a "
                      f"harness task refreshes it")
            continue
        done = checkplan.refresh(h, path, files)
        if done:
            plans.append(path)
            print(f"refreshed the digests of {', '.join(done)} in harness check plan {path}: this commit changes them")
    return plans


# Untracked files with these names are runtime data a harness's tools write as they run (append-only logs,
# queues, pid and lock files, databases), not edits: the pre-upgrade commit leaves them out.
_RUNTIME_DATA = ("*.jsonl", "*.ndjson", "*.log", "*.pid", "*.lock", "*.db", "*.db-wal", "*.db-shm", "*.sqlite")


def _keep_out_runtime_data(h: Path, since: float | None) -> list[str]:
    """Runtime data files (_RUNTIME_DATA) git does not track yet, or only staged: unstaged and added to
    the harness's local ignore rules (.git/info/exclude), so neither the pre-upgrade commit nor a later
    `git add -A` sweeps them in. A tracked file stays tracked; one changed after `since` (a running harness
    task's start) is left to that task. Returns the files kept out."""
    import fnmatch

    def names(*args: str) -> list[str]:
        out = subprocess.run(["git", "-C", str(h), *args], capture_output=True, text=True, check=True).stdout
        return [n for n in out.split("\0") if n]

    new = set(names("ls-files", "-z", "--others", "--exclude-standard"))
    staged = set(names("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=A", "-z", "HEAD"))
    def mine(n: str) -> bool:
        try:
            return since is None or (h / n).lstat().st_mtime <= since
        except OSError:
            return False

    data = sorted(n for n in new | staged
                  if any(fnmatch.fnmatch(Path(n).name, g) for g in _RUNTIME_DATA) and mine(n))
    if not data:
        return []
    if staged & set(data):
        _git(h, "--literal-pathspecs", "rm", "-q", "--cached", "--", *sorted(staged & set(data)))
    exclude = h / _git(h, "rev-parse", "--git-path", "info/exclude")
    cur = exclude.read_text() if exclude.exists() else ""
    have = set(cur.splitlines())
    lines = ["/" + re.sub(r"([\\*?\[])", r"\\\1", n) for n in data]
    add = [ln for ln in lines if ln not in have]
    if add:
        durable_write(exclude, cur + ("" if not cur or cur.endswith("\n") else "\n")
                      + "# runtime data kept out of commits by `ttp upgrade`\n" + "\n".join(add) + "\n")
    return data


def _listed(names: list[str], cap: int = 40) -> str:
    return "\n".join(f"  {n}" for n in names[:cap]) + (f"\n  ... and {len(names) - cap} more" if len(names) > cap else "")


def _commit_listing(p: Project, ident: list[str], msg: str, files: list[str], kept_out: list[str],
                    pre: list[str] | None = None, post: list[str] | None = None) -> str:
    """Commit `files` (what is staged, or `post`'s paths) with a message that lists them and the runtime
    data kept out, then tell the open harness tasks that may own those edits. Returns the sha."""
    h = p.harness
    body = f"Files committed ({len(files)}):\n{_listed(files)}"
    if kept_out:
        body += (f"\n\nRuntime data kept out (in .git/info/exclude; `git add -f` to commit one):\n"
                 f"{_listed(kept_out)}")
    _git(h, *(pre or []), *ident, "commit", "-q", *(post or []), "-m", msg, "-m", body)
    sha = _git(h, "rev-parse", "HEAD")
    print(f"committed the harness's local changes as {sha[:12]} ({len(files)} file(s): "
          f"{', '.join(files[:10])}{f' and {len(files) - 10} more' if len(files) > 10 else ''})")
    _tell_harness_tasks(p, sha, files)
    return sha


def _tell_harness_tasks(p: Project, sha: str, files: list[str]) -> None:
    """A harness task whose run ended mid-edit (an auth failure, a kill) left its work uncommitted in the
    live harness; the pre-upgrade commit took it. Every open harness task with a run that started before
    the newest of those edits may own them: its spec, and the steer file of a run still going, name the
    commit so its next attempt continues from it instead of redoing or hunting for the work."""
    from .coordinator import _append_update
    h = p.harness
    newest = 0.0
    for n in files:
        try:
            newest = max(newest, (h / n).lstat().st_mtime)
        except OSError:
            newest = time.time()       # deleted: its age is unknown
    me = os.environ.get("TTP_RUN_ID") if _own_run(p) else None
    mine = p.db.one("SELECT task FROM runs WHERE CAST(id AS TEXT)=?", (me,)) if me else None
    shown = ", ".join(files[:10]) + (f" and {len(files) - 10} more" if len(files) > 10 else "")
    upd = (f"The template upgrade committed the live harness's uncommitted changes as {sha[:12]} "
           f"(\"local harness changes before template upgrade\": {shown}). If they are an earlier attempt's "
           f"edits of this task, continue from that commit (`git show {sha[:12]}`) instead of redoing them.")
    for t in p.db.q("SELECT id, spec FROM tasks WHERE kind='harness' AND status NOT IN "
                    "('done','failed','cancelled','review') AND EXISTS (SELECT 1 FROM runs r WHERE r.task=tasks.id "
                    "AND r.started IS NOT NULL AND r.started <= ?) ORDER BY id", (newest,)):
        if mine and t["id"] == mine["task"]:
            continue        # the run doing this upgrade made the commit itself
        p.db.x("UPDATE tasks SET spec=? WHERE id=?", (f"{(t['spec'] or '').rstrip()}\n\n## Update\n{upd}\n", t["id"]))
        for r in p.db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (t["id"],)):
            if r["dir"]:
                _append_update(Path(r["dir"], "steer.md"), upd, f"pre-upgrade-{sha[:12]}")
        print(f"told harness task #{t['id']} that {sha[:12]} holds the harness's uncommitted changes")


def _snapshot_upstream(p: Project, src: Path, new_v: str, new_c: str, ident: list[str]) -> None:
    """Commit the installed template onto the harness's `upstream` branch (pristine snapshots); main is
    not touched. No commit when upstream already holds it."""
    h = p.harness
    tmp = p.state / "upgrade-wt"
    if tmp.exists():
        shutil.rmtree(tmp)
    own = _own_pytest_config(h)
    _git(h, "worktree", "add", "-q", str(tmp), "upstream")
    try:
        for part, dst in (("runtime", "runtime"), ("template/prompts", "prompts"), ("template/bin", "bin")):
            if (tmp / dst).exists():
                shutil.rmtree(tmp / dst)
            _copy_tree(src / part, tmp / dst)
        for f in HARNESS_ROOT_FILES:
            if (src / "template" / f).is_file() and not (f == "pytest.ini" and own):
                shutil.copy2(src / "template" / f, tmp / f)
        _git(tmp, "add", "-A")
        if _git(tmp, "status", "--porcelain"):
            _git(tmp, *ident, "commit", "-q", "-m", f"tt-project template {new_v} ({new_c})")
    finally:
        _git(h, "worktree", "remove", "--force", str(tmp))


_TARGET_RE = re.compile(r"^Target release: .*$", re.M)


def _retarget_upgrade_task(p: Project, holder: dict, auto: bool) -> None:
    """A `ttp upgrade` while harness task holder["task"] finishes a template merge: the installed
    release goes onto `upstream` (main and the live harness stay as they are) and that task takes it
    on, so one task finishes the merge for every release meanwhile instead of one task each. A running
    worker is told through its steer.md; its `--apply` of a merge without the new upstream is refused.
    Exits 75 (deferred): the open task lands it."""
    from . import release
    from .coordinator import _append_update
    from .project import HOME_DIR
    src = HOME_DIR / "lib" / "current"
    tid = holder["task"]
    if not (src / "runtime").is_dir():
        return
    h = p.harness
    new_v, new_c = _runtime_version(src / "runtime"), recorded_commit(src / "runtime")
    key, target = f"{new_v} {new_c}", f"{new_v} ({new_c})"
    before = _git(h, "rev-parse", "upstream")
    _snapshot_upstream(p, src, new_v, new_c, ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"])
    moved = _git(h, "rev-parse", "upstream") != before
    if holder.get("key") != key:
        task = p.db.task(tid) or {}
        spec = task.get("spec") or ""
        line = f"Target release: tt-project {target}"
        spec = _TARGET_RE.sub(line, spec, 1) if _TARGET_RE.search(spec) else f"{spec.rstrip()}\n\n{line}\n"
        if moved:
            upd = (f"Retargeted to tt-project {target}: `upstream` now holds it. Merge `upstream` again in your "
                   f"worktree (`--apply` refuses a merge without it), resolve, check, then apply.")
            spec += f"\n## Update\n{upd}\n"
            for r in p.db.q("SELECT id, dir FROM runs WHERE task=? AND status='running'", (tid,)):
                if r["dir"]:
                    _append_update(Path(r["dir"], "steer.md"), upd, f"retarget-{tid}-{new_c}")
        # Not through update_task: a retarget is not the task's own progress, and its `updated` time is
        # what tells a stuck finish task (release.stale_holder) from one that moves.
        p.db.x("UPDATE tasks SET spec=? WHERE id=?", (spec, tid))
        release.take_finish_lock(p, tid, key, target)
    release.note_deferred(p, task=tid)
    if auto:
        release.finish(p, "conflict", task=tid, why=f"harness task #{tid} takes this release on")
    print(f"upgrade not applied; the running harness is unchanged. Harness task #{tid} is finishing the "
          f"template merge" + (f" and now targets {target}" if moved or holder.get("key") != key else "")
          + f" (upgrade deferred to that task; {release.holder_line(p, holder)}).")
    sys.exit(75)


def _moved_template(h: Path, base: str) -> str:
    """Why main may not take the checked merge ("" = it may): since `base` someone else changed the
    template's files on it, or merged upstream into it. Only charter, memory and config commits of the
    daemon may land meanwhile; the merge then keeps them. Anything else would be overwritten."""
    head = _git(h, "rev-parse", "HEAD")
    if head == base:
        return ""
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", base, head]).returncode != 0:
        return f"main moved from {base[:12]} to {head[:12]} and no longer contains it"
    touched = _git(h, "diff", "--name-only", base, head, "--", "runtime", "prompts", "bin").split()
    if touched:
        return (f"main moved from {base[:12]} to {head[:12]} while the template was merged, changing "
                f"{', '.join(touched[:5])}; rerun `ttp upgrade` to merge again")
    return ""


def _apply_upgrade(p: Project, commit: str) -> None:
    """`ttp upgrade <name> --apply <commit>`: the last step of an upgrade task. Fast-forwards main to the
    task's checked merge, under the upgrade lock, only when it takes in the current template and main
    has not moved past it; then restarts the daemon like any upgrade."""
    from . import release, upjournal
    h = p.harness
    r = subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", commit + "^{commit}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"--apply: {commit} is not a commit of {h}", 2)
    merged = r.stdout.strip()
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", "upstream", merged]).returncode != 0:
        die(f"--apply refused: {commit} does not contain the current template (upstream); merge it again", 1)
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", "HEAD", merged]).returncode != 0:
        die(f"--apply refused: main moved and {commit} does not contain it; nothing was changed. In the "
            f"worktree: `git merge main`, check again, commit, then rerun with the new commit.", 1)
    upjournal.step(p, "merging", base=_git(h, "rev-parse", "HEAD"), merged=merged)
    r = subprocess.run(["git", "-C", str(h), "merge", "--ff-only", merged], capture_output=True, text=True,
                       env={**os.environ, release.GUARD_ENV: "1"})
    if r.returncode != 0:
        die(f"--apply: could not fast-forward main to {commit}: {(r.stderr or r.stdout).strip()[-400:]}", 1)
    sync_all()
    upjournal.step(p, "merged")
    release.clear_stuck(p)
    print(f"harness main is now {merged[:12]}; restarting the daemon")
    from . import service
    upjournal.step(p, "restarting")
    _exit_for_restart(service.restart(p))


UPGRADE_BUSY = 75       # `ttp upgrade` deferred or refused: nothing was changed (EX_TEMPFAIL, as for `ttp lock`)
RESTART_DEFERRED = 76   # main (or the runtime) is applied; the old daemon restarts when it takes restart.request


def _exit_for_restart(res) -> None:
    """Print a restart's report; exit RESTART_DEFERRED when it is deferred (the old daemon runs on and
    restarts when it takes the request) and 1 when it failed or rolled back, never 0 for either. Never 75:
    that is `ttp upgrade`'s "nothing was changed", and a worker that read it as "applied" would wait on a
    restart.request that never comes."""
    print(res)
    code = {"deferred": RESTART_DEFERRED, "failed": 1, "rolled_back": 1}.get(getattr(res, "outcome", "running"), 0)
    if code:
        print(f"restart {res.outcome.replace('_', ' ')} (exit {code})", file=sys.stderr)
        sys.exit(code)


HARNESS_MERGE_WAIT_S = 15   # how long `ttp upgrade` waits for the daemon's harness worktree merge to end


def _refuse_unfinished_merge(p: Project, auto: bool) -> None:
    """A hand-run `git merge` the guard hook refused leaves MERGE_HEAD and the merged files in the live
    harness. Committing them as local changes is that same merge, so the hook would refuse it again:
    stop here and say what is wrong and how to undo it."""
    from . import harnesstree, release
    h = p.harness
    end = time.time() + HARNESS_MERGE_WAIT_S
    while harnesstree.landing_merge(h) and time.time() < end:   # the daemon lands a harness task's commits
        time.sleep(0.5)
    if harnesstree.landing_merge(h):
        if auto:
            release.finish(p, "held", why="a harness worktree merge is in progress; the daemon retries")
        die(f"upgrade deferred: harness merge in progress in {h}, daemon retries; nothing was changed", 75)
    if subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                      capture_output=True).returncode != 0:
        return
    files = _git(h, "diff", "--name-only", "HEAD").splitlines()
    shown = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
    msg = (f"upgrade not applied: an unfinished merge is in the live harness {h} (MERGE_HEAD is set)"
           + (f"; files: {shown}" if shown else "")
           + f". `git -C {shlex.quote(str(h))} merge --abort` restores HEAD; then rerun `ttp upgrade {p.name}`.")
    if auto:            # stderr already goes to logs/upgrade.log; the status line shows the reason
        release.finish(p, "failed", why="an unfinished merge is in the live harness; `git merge --abort` "
                                        "there restores HEAD")
    else:
        p.logs.mkdir(parents=True, exist_ok=True)
        with open(p.logs / "upgrade.log", "a") as log:
            log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} ttp upgrade\n{msg}\n")
    die(msg, 1)


_UPGRADE_TASK = """`ttp upgrade` could not apply the new tt-project template on its own: {problem}

Target release: tt-project {target}

The live harness was left untouched, and this task holds it until it ends: a newer release meanwhile
moves `upstream` and retargets this task (merge `upstream` again) instead of queuing another.
In this harness repo:
1. `git worktree add --detach <tmp> main`, then in <tmp>: `git merge upstream`.
2. Resolve each conflict keeping this project's intent and taking upstream's fixes. Put this project's
   own prompt rules between `<!-- ttp:local -->` and `<!-- /ttp:local -->` lines, so later upgrades keep
   them without a merge.
3. Check in <tmp>: `python3 -m compileall -q runtime` and `PYTHONPATH=runtime python3 -c "import ttp.daemon, ttp.cli"`.
4. Commit, then in <tmp>: `git merge main` (main may have moved meanwhile; resolve and check again).
5. `ttp upgrade {name} --apply <commit>`, run on its own (never piped or masked). It fast-forwards main
   to <commit> under the upgrade lock and restarts the daemon (it rolls the runtime back if the daemon
   does not start). Never merge upstream into main any other way: the harness refuses it.
   Where the service manager is out of reach (a sandbox), it asks the running daemon to restart itself.
   Its exit code says what happened; act on it alone:
   - Exit 0: done, remove <tmp>.
   - Exit 75: busy, nothing was changed (the daemon is landing harness worktrees, or another upgrade
     runs): main is as it was and no restart is coming, so never wait on restart.request. Keep <tmp> and
     rerun this step once the landing ends (a minute or two), or hand off `waiting` with
     retry_after_s 300 and retry_when `ttp lock --probe harness-upgrade` (tell it from exit 76 by its
     "nothing was changed" line).
   - Exit 76: main is applied and the restart deferred; the old daemon runs on and restarts when it
     takes the request: remove <tmp> and hand off `waiting` with retry_when
     `test ! -e {state}/restart.request -a ! -e {state}/restart.request.taken`. When it wakes you, read
     {state}/restart.result: its `outcome` (running, busy, failed, rolled_back) and `text` say how the
     restart went; report a failure or a rollback with that text.
   - Exit 1 or other: keep <tmp> and hand off with its path and the error (it says whether the restart
     was unavailable or the new daemon failed).
"""


def _ensure_git_ident(h: Path) -> None:
    """Give the harness repo a local commit identity (tt-project <tt-project@localhost>) for any key
    its own .git/config lacks, so a plain `git merge` or `git commit` in it works for any run, even
    where no global identity is set. A locally configured name or email is never overwritten."""
    for key, val in (("user.name", "tt-project"), ("user.email", "tt-project@localhost")):
        if subprocess.run(["git", "-C", str(h), "config", "--local", key], capture_output=True,
                          text=True).stdout.strip() == "":
            _git(h, "config", "--local", key, val)


def _emptied(h: Path, have: str | None, ref: str) -> list[str]:
    """Template files (runtime, prompts, bin) that `have` (a commit, or None for the files on disk)
    holds empty while `ref` has them with content. A crash or reboot right after a write leaves
    exactly this, and the merge would then keep the empty file as if it were a local edit."""
    def sizes(rev: str) -> dict[str, int]:
        out = {}
        for entry in _git(h, "ls-tree", "-r", "-l", "-z", rev, "--", "runtime", "prompts", "bin").split("\0"):
            meta, _, path = entry.partition("\t")
            if path and meta.split()[-1].isdigit():
                out[path] = int(meta.split()[-1])
        return out
    full = {f for f, n in sizes(ref).items() if n}
    if have is None:
        return sorted(f for f in full if (h / f).is_file() and not (h / f).is_symlink()
                      and (h / f).stat().st_size == 0)
    return sorted(f for f, n in sizes(have).items() if n == 0 and f in full)


def _blob(h: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(h), "show", f"{rev}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _top_names(src: bytes) -> set[str] | None:
    """Top-level names a Python file defines, or None when it does not parse."""
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _why_cut(f: str, got: bytes | None, want: bytes, keep: set[str] | None = None) -> str:
    """How `got` lost the runtime file `want` ("" when it did not): deleted, emptied, no longer parsing,
    or missing top-level names `want` defines (only those in `keep`, when given)."""
    if got is None:
        return "deleted"
    if not got.strip():
        return "empty"
    if not f.endswith(".py") or (need := _top_names(want)) is None:
        return ""
    names = _top_names(got)
    if names is None:
        return "cut short"
    lost = need & (need if keep is None else keep) - names
    return "lost " + ", ".join(sorted(lost)[:5]) if lost else ""


def _cut_runtime(h: Path, have: str | None, ref: str) -> dict[str, str]:
    """runtime/ files `ref` ships with content that `have` (a commit, or None for the files on disk)
    lost, mapped to how (see _why_cut; on disk only names HEAD defines too count). A reboot partway
    through an upgrade leaves this; committing it as a local change breaks `import ttp.daemon`
    (e.g. `cannot import name 'poll_s'`)."""
    diff = ["diff", "--name-only", "--no-renames"] + ([have, ref] if have else ["HEAD"]) + ["--", "runtime"]
    cut = {}
    for f in _git(h, *diff).splitlines():
        want = _blob(h, ref, f)
        if not want or not want.strip():
            continue        # not in `ref`, or empty there: nothing to lose
        if have is None:
            p = h / f
            got = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            why = _why_cut(f, got, want, _top_names(_blob(h, "HEAD", f) or b""))
        else:
            why = _why_cut(f, _blob(h, have, f), want)
        if why:
            cut[f] = why
    return cut


def _cut_list(cut: dict[str, str]) -> str:
    return ", ".join(f"{f} ({why})" for f, why in sorted(cut.items()))


def _restore_cut_runtime(h: Path) -> None:
    """Before the upgrade commits local changes: put back runtime/ files that upstream still ships but
    are deleted or cut short on disk, from HEAD (or upstream when HEAD's copy is cut too). Refuses the
    upgrade, with nothing committed, when they cannot be put back."""
    cut = _cut_runtime(h, None, "upstream")
    if not cut:
        return
    for f in cut:
        src = "upstream" if _why_cut(f, _blob(h, "HEAD", f), _blob(h, "upstream", f) or b"") else "HEAD"
        r = subprocess.run(["git", "-C", str(h), "checkout", src, "--", f], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"upgrade refused: {f} is {cut[f]} and could not be restored from {src}: "
                f"{r.stderr.strip()[-300:]}. Nothing was committed; restore it, then rerun `ttp upgrade`.", 1)
    left = _cut_runtime(h, None, "upstream")
    if left:
        die("upgrade refused: runtime files upstream ships are still deleted or cut short after restoring "
            f"them: {_cut_list(left)}. Nothing was committed; restore them, then rerun `ttp upgrade`.", 1)
    print("warning: restored runtime files a crash deleted or cut short instead of committing them: "
          + _cut_list(cut))


def _runtime_problem(tmp: Path) -> str:
    """Why the runtime in worktree `tmp` does not compile or import ("" when it does)."""
    env = {**os.environ, "PYTHONPATH": str(tmp / "runtime")}
    for check in ([sys.executable, "-m", "compileall", "-q", "runtime"],
                  [sys.executable, "-c", "import ttp.daemon, ttp.cli"]):
        c = subprocess.run(check, cwd=str(tmp), env=env, capture_output=True, text=True, timeout=300)
        if c.returncode != 0:
            return f"the merged runtime fails `{' '.join(check[1:])}`: " + (c.stderr or c.stdout).strip()[-400:]
    return ""


def _restore_from_upstream(tmp: Path, ident: list[str], cut: dict[str, str]) -> str:
    """Check out and commit `cut`'s runtime files from upstream in worktree `tmp` ("" when done)."""
    if not cut:
        return ""
    r = subprocess.run(["git", "-C", str(tmp), "checkout", "upstream", "--", *cut], capture_output=True, text=True)
    if r.returncode != 0:
        return (f"runtime files upstream ships are deleted or cut short on main and could not be "
                f"restored: {_cut_list(cut)}: {r.stderr.strip()[-300:]}")
    _git(tmp, *ident, "commit", "-q", "-m", "restore runtime files main lost: " + _cut_list(cut))
    print("warning: restored runtime files main had deleted or cut short: " + _cut_list(cut))
    return ""


LOCAL_OPEN, LOCAL_CLOSE = "<!-- ttp:local -->", "<!-- /ttp:local -->"


def _settle_merge(tmp: Path, ident: list[str], files: list[str]) -> str:
    """Finish the stopped merge of upstream in worktree `tmp` without a model when no conflict needs
    judgment (batch.settle): both sides only added lines at one spot, they changed different lines,
    or upstream already makes the project's change (comments and spacing aside); never when a line
    one side added nearly repeats one the other added (batch._near). A template prompt
    the project only added to takes upstream's text with the project's blocks put back
    (_keep_local_blocks). Commits it and returns ""; else names the files left (nothing is committed)."""
    from .batch import _show, settle
    left, kinds, blocks = [], set(), []
    for f in files:
        if settle(tmp, f, [], taken_in=True, hunks=True, superseded=True, near=True, kinds=kinds):
            continue
        sides = [_show(tmp, n, f) for n in (2, 1, 3)]
        merged = None
        if f.startswith("prompts/") and f.endswith(".md") and None not in sides:
            try:
                merged = _keep_local_blocks(*(s.decode() for s in sides))
            except UnicodeDecodeError:
                pass
        if merged is None:
            left.append(f)
        else:
            durable_write(tmp / f, merged)
            blocks.append(f)
    if left or not files:
        return "the merge conflicts in " + ", ".join(left)
    _git(tmp, "add", "--", *files)
    _git(tmp, *ident, "commit", "-q", "--no-edit", "--no-verify")
    print("settled the template merge's conflicts, which needed no judgment: " + ", ".join(files))
    if "superseded" in kinds:
        print("took upstream's version where it already makes the project's change; the project's own "
              "wording stays in the merge's first parent")
    if blocks:
        print(f"kept this project's own blocks in upstream's text, fenced as {LOCAL_OPEN}: " + ", ".join(blocks))
    return ""


def _keep_local_blocks(ours: str, base: str, theirs: str) -> str | None:
    """A template prompt merged as upstream's text (`theirs`) plus this project's own blocks, when the
    project only added to it: its text outside `<!-- ttp:local -->` fences is `base` with lines inserted.
    Each block goes back after the base line it followed (after upstream's rewrite of that spot, if
    any); added lines not yet fenced get the fence lines, so the next upgrade finds them as the
    project's (the prompts leave the fence lines out). None when the project changed or removed
    template text, a fence is not closed, or a line the project added nearly repeats one upstream
    added or changed at the same spot (batch._near: two versions of one line): that needs judgment."""
    import difflib

    from .batch import _near
    items: list[tuple[str, object]] = []       # ("t", template line) or ("b", fenced block lines)
    block = None
    for line in ours.splitlines(keepends=True):
        s = line.strip()
        opens, closes = (s.startswith(m[:-4]) and s.endswith("-->") for m in (LOCAL_OPEN, LOCAL_CLOSE))
        if block is not None:
            block.append(line)
            if closes:
                items.append(("b", block))
                block = None
        elif opens:
            block = [line]
        elif closes:
            return None
        else:
            items.append(("t", line))
    if block is not None:
        return None
    old = base.splitlines(keepends=True)
    tmpl = [x for kind, x in items if kind == "t"]
    added = [False] * len(tmpl)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, tmpl, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            return None
        if tag == "insert":
            added[j1:j2] = [True] * (j2 - j1)
    groups: list[tuple[int, list[str]]] = []    # (base lines before it, the block's lines)
    at, j, new = 0, 0, []

    def put(lines: list[str]) -> None:
        if groups and groups[-1][0] == at:
            groups[-1][1].extend(lines)
        else:
            groups.append((at, list(lines)))

    def fence() -> None:
        if new:
            body = [x if x.endswith("\n") else x + "\n" for x in new]
            put([LOCAL_OPEN + "\n", *body, LOCAL_CLOSE + "\n"])
            new.clear()
    for kind, x in items:
        if kind == "b":
            fence()
            put([y if y.endswith("\n") else y + "\n" for y in x])
        elif added[j]:
            new.append(x)
            j += 1
        else:
            fence()
            at, j = at + 1, j + 1
    fence()
    if not groups:
        return None
    out = theirs.splitlines(keepends=True)
    ops = difflib.SequenceMatcher(None, old, out, autojunk=False).get_opcodes()
    for i, lines in groups:                     # only upstream's lines added or changed at the block's spot
        mine = [x for x in lines if x.strip() not in (LOCAL_OPEN, LOCAL_CLOSE)]
        if _near(mine, [x for tag, i1, i2, j1, j2 in ops if tag != "equal" and i1 <= i <= i2
                        for x in out[j1:j2]], False):
            return None

    def where(i: int) -> int:
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal" and i1 <= i <= i2:
                return j1 + i - i1
            if tag != "equal" and i == i1:
                return j1
            if tag != "equal" and i1 < i <= i2:
                return j2
        return len(out)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for i, _, lines in sorted(((where(i), k, lines) for k, (i, lines) in enumerate(groups)), reverse=True):
        out[i:i] = lines
    return "".join(out)


def _merge_upstream(h: Path, tmp: Path, ident: list[str], base: str = "main",
                    kept: dict[str, str] | None = None) -> tuple[str, str]:
    """Merge `upstream` into a scratch worktree of main and check the result compiles and imports.
    Returns (merge commit, "") or ("", what went wrong); the live harness is never touched here.
    Runtime files kept although they lack names upstream ships are added to `kept` (file -> how)."""
    subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    subprocess.run(["git", "-C", str(h), "worktree", "prune"], capture_output=True)
    _git(h, "worktree", "add", "-q", "--detach", str(tmp), base)
    try:
        r = subprocess.run(["git", "-C", str(tmp), *ident, "merge", "--no-edit", "upstream"], capture_output=True,
                           text=True)
        if r.returncode != 0:
            files = subprocess.run(["git", "-C", str(tmp), "diff", "--name-only", "--diff-filter=U"],
                                   capture_output=True, text=True).stdout.split()
            why = _settle_merge(tmp, ident, files)
            if why:
                return "", (why if files else "the merge failed: " + (r.stderr or r.stdout).strip()[-400:])
        emptied = _emptied(tmp, "HEAD", "upstream")
        if emptied:     # committed as "local changes" after a crash emptied them; nobody empties these on purpose
            _git(tmp, "checkout", "upstream", "--", *emptied)
            _git(tmp, *ident, "commit", "-q", "-m", "restore template files a crash left empty: " + ", ".join(emptied))
        cut = _cut_runtime(tmp, "HEAD", "upstream")
        # Deleted, empty or unparseable files are damage. A file that only lacks top-level names upstream
        # ships may be a deliberate local edit (a helper removed on purpose): it is put back only when
        # the merged runtime does not compile or import without it, so its other local edits survive.
        lost = {f: why for f, why in cut.items() if why.startswith("lost ")}
        why = _restore_from_upstream(tmp, ident, {f: w for f, w in cut.items() if f not in lost})
        if why:
            return "", why
        problem = _runtime_problem(tmp)
        if problem and lost:
            why = _restore_from_upstream(tmp, ident, lost)
            if why:
                return "", why
            lost, problem = {}, _runtime_problem(tmp)
        if problem:
            return "", problem
        if lost:
            print("warning: kept runtime files main changed although they lack names upstream ships (the merged "
                  "runtime compiles and imports without them): " + _cut_list(lost))
            if kept is not None:
                kept.update(lost)
        return _git(tmp, "rev-parse", "HEAD"), ""
    finally:
        subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)


def cmd_alerts(a) -> None:
    if (a.ack is not None) != (a.why is not None) or a.why is not None and not a.why.strip():
        die("ttp alerts: --ack <message id> requires a non-empty --why <text>")
    p = need(a.name, sys.argv[1:])
    if a.ack is not None:
        from . import alerts
        try:
            ep = alerts.ack(p.db, a.ack, a.why)
        except ValueError as e:
            die(f"ttp alerts: {e}")
        from .daemon import release_claims
        release_claims(p, ep)   # closed now: a machine-wide claim goes as it would at the sweep
        print(json.dumps(ep) if a.json else f"Acknowledged alert #{a.ack}: " + (
            ep["cleared_why"] if ep["cleared"] else f"{ep['acked']['why']} (kept in the feed until it clears)"))
        return
    from .notifier import alerts_since, health
    rows = alerts_since(p, a.after, a.floor)
    if a.json:
        print(json.dumps({"alerts": rows, "health": health(p)} if a.health else rows))
    else:
        for r in rows:
            if not r["cleared"]:
                print(f"#{r['id']} [{r['severity']}] {r['text']}")


def cmd_consent(a) -> None:
    from . import consent
    if a.answer:
        if not a.topic:
            die("name the topic: `ttp consent notifier|tunnel yes|no`")
        consent.record(a.topic, a.answer)
        print(f"recorded {a.topic}: {a.answer} (never asked again; {consent.path()})")
        return
    for t in [a.topic] if a.topic else list(consent.TOPICS):
        got = consent.answer(t)
        print(f"{t}: {got}" + (f" (ask the user once: {consent.TOPICS[t]})" if got == "ask" else ""))


def cmd_notifier(a) -> None:
    from . import consent, notifier
    if a.action == "install":
        why = consent.gate("notifier", "ttp notifier install")
        if why:
            die(f"notifier not installed: {why}", consent.NEEDS_ANSWER)
        print(notifier.install())
        ok, lines = notifier.test_once()
        print("\n".join(lines))
    elif a.action == "run":
        sys.exit(notifier.main())
    elif a.action == "restart":
        if not notifier.agent_file():
            die("no desktop notifier installed here; `ttp notifier install` installs one")
        print(notifier.install())
    elif a.action == "uninstall":   # stays off: setup and upgrade restart only an installed, enabled agent
        print(notifier.uninstall())
    elif a.action == "test" and a.slack:
        from .slack import send_test
        ok, text = send_test()
        print(text)
        if not ok:
            sys.exit(1)
    elif a.action == "test":
        ask = None
        if sys.stdin.isatty() and sys.stdout.isatty():   # the user at a terminal can say what macOS cannot
            ask = lambda q: input(q).strip().lower() in ("y", "yes")
        ok, lines = notifier.test_once(ask=ask)
        print("\n".join(lines))
        if not ok:
            sys.exit(1)


def cmd_daemon(a) -> None:
    from .daemon import Daemon
    sys.exit(Daemon(a.dir).run())


def cmd_doctor(a) -> None:
    if getattr(a, "live", None):
        # Measures the agent CLI itself, in a scratch project: no project needed, and real spend.
        from . import live
        r = live.row(a.live)
        print(json.dumps(r, indent=1) if a.json else live.format_row(r))
        sys.exit(1 if any(c["status"] == live.FAIL for c in r["checks"].values()) else 0)
    if getattr(a, "loop", None):
        # One tiny code task through dispatch, review and push, in a scratch project: real spend.
        from . import loopcheck
        r = loopcheck.run(a.loop)
        if a.json:
            print(json.dumps(r, indent=1))
        sys.exit(0 if not r["available"] or r["outcome"] == "pass" else 1)
    if getattr(a, "scheduler", False):
        # The installed runtime's command-watcher lifecycle, in a scratch project: model-free, $0.
        from . import schedcheck
        rows = schedcheck.run()
        for what, ok, why in rows:
            print(f"{'pass' if ok else 'FAIL'}  {what}" + (f": {why}" if why else ""))
        sys.exit(0 if rows and all(ok for _, ok, _ in rows) else 1)
    if not a.name:
        die("ttp doctor: name a project, --live <provider>, --loop <provider> or --scheduler")
    p = need(a.name, sys.argv[1:])
    from .providers import all_providers
    print(status_text(p))
    for prov in all_providers():
        if prov.name == "fake" or (prov.name == "chat" and "chat" not in p.config()["providers"]):
            continue
        print(f"provider {prov.name}: {'found ' + prov.binary() if prov.available() else 'not installed'}"
              + (f" · account {prov.account()}" if prov.available() and prov.account() else ""))
    gaps = [f"{prov.name} ({prov.write_fence()})" for prov in all_providers()
            if prov.name != "fake" and prov.available() and prov.write_fence()]
    if gaps:
        print("unfenced providers (their workers can write anywhere, project.db included): " + ", ".join(gaps))
    claude_cfg = p.config()["providers"].get("claude", {})
    from .coordinator import name_list
    from . import mcpgap   # a gap is already a status line above
    g = mcpgap.gap(claude_cfg, mcpgap.read(p.state))
    if g["state"] == "unknown":
        print(f"mcp servers on this host: unknown ({g['error']}); the daemon runs `claude mcp list` again within "
              f"{mcpgap.RETRY_S // 60} min")
    names = name_list(claude_cfg.get("mcp_servers") or [])
    if names:
        from .providers import get_provider
        prov = get_provider("claude")
        dirs = [str(p.root), str(p.root.resolve())]
        _, unknown = prov.mcp_servers([n for n in names if not prov.is_connector(n)], dirs)
        print(f"mcp servers for workers: {', '.join(names)}"
              + (f" · not defined in your Claude config: {', '.join(unknown)} (servers from a plugin "
                 "cannot be listed; add them with `claude mcp add`)" if unknown else "")
              + ("" if claude_cfg.get("worker_isolation") else " · worker_isolation is off, so workers load "
                 "all your servers; a claude.ai name here keeps your claude.ai connectors too"))
        if claude_cfg.get("worker_isolation") and any(prov.is_connector(n) for n in names):
            from .coordinator import dir_list
            plugin_dirs = [str(Path(os.path.expanduser(d))) for d in dir_list(claude_cfg.get("plugin_dirs") or [])]
            plan = mcpgap.plan(claude_cfg, mcpgap.read(p.state), prov.mcp_server_names(dirs), plugin_dirs)
            print(f"claude.ai connectors for isolated workers: {', '.join(plan['connectors'])}, every other server "
                  f"kept out ({len(plan['deny'])} by name)" if not plan["why"] else
                  f"claude.ai connectors for isolated workers: none, workers stay strict ({plan['why']})")
    sec = load_secrets()
    print(f"jev: {'key saved' if (sec.get('jev') or {}).get('key') else 'no key (rules-only screening)'}"
          f" · enabled in project: {p.config()['jev'].get('enabled')}")
    from .daemon import SLACK_ERROR_KEY
    from .slack import status as slack_status
    print(slack_status(p.config(), p.db.kv(SLACK_ERROR_KEY))["line"] + " · `ttp notifier test --slack` sends a test")
    print(web_line(p))
    from .service import service_line
    print(service_line(p))
    from . import sandbox
    print(f"process sandbox for checks: {sandbox.notice() or 'on (own PID namespace per check command)'}")
    from .project import config_problems
    for line in config_problems(p.raw_config(), p.root):
        print(f"project.json: {line}")
    from . import push as _push
    _d = p.config().get("delivery") or {}
    if _push.lands_locally(_d.get("push_branch"), p.root, _push.allow_protected(_d)):
        print(f"project.json: {_push.push_branch_problem(_d.get('push_branch'), p.root, _push.allow_protected(_d))}")
    checks = _push.check_list((p.config().get("delivery") or {}).get("push_checks"))
    if checks and (p.config().get("delivery") or {}).get("push_branch"):
        try:
            remote, branch = _push.target(p, p.root)
            ref, missing = _push.unmatched_paths(p.root, remote, branch, checks)
        except (ValueError, OSError):
            ref, missing = "", []
        for m in missing:
            print(f"delivery.push_checks: {m} matches no file on {ref}; every push would fail on it")
    if line := checks_env_line(p):
        print(line)
    from . import initiator
    if line := initiator.doctor_line(p):
        print(line)
    from . import upstream
    for line in upstream.doctor_lines(p):
        print(line)
    from .coordinator import pinned_memory_line, worker_pinned_line
    print(pinned_memory_line(p))
    if line := worker_pinned_line(p):
        print(line)
    for c in _push.unexcluded_log_checks(checks):
        print(f"delivery.push_checks: {c!r} also checks committed *.log output, whose captured lines keep "
              f"trailing whitespace; add the pathspec `-- . {_push.LOG_EXCLUDE}`")


PROVIDER_CHOICES = ("claude", "codex", "cursor")
TASK_KINDS = ("question", "code", "review", "plan", "harness", "work")   # as coordinator.TASK_KINDS (not imported: slow)
TASK_TIERS = ("light", "standard", "deep")                              # as chaincap.TIERS

# `ttp --help` lists the commands in these groups; a command in none is listed under "other".
COMMAND_GROUPS = (
    ("everyday", ("new", "connect", "web", "status", "say", "listen", "list", "overview", "find", "task",
                  "memory", "obligation", "config", "pause", "resume", "drain", "alerts", "issues", "mute",
                  "machines", "logs", "doctor")),
    ("inside a run", ("note", "notify", "progress", "checks", "lock", "detach", "devq", "push", "land", "clip", "sandbox",
                      "skill", "killscan", "svcscan", "leftover-procs")),
    ("probes (retry_when, start_when)", ("landed", "ci", "ack-wait", "ssh-preflight", "broker-released",
                                         "deployed", "goal", "hours")),
    ("admin", ("setup", "upgrade", "start", "stop", "restart", "adopt", "prune", "secret", "consent",
               "notifier", "schedules", "heal", "upstream", "lease", "replicate", "check-plan", "stats", "audit",
               "push-audit", "ttr", "spend-today", "uninstall")),
)


class _TopHelp(argparse.HelpFormatter):
    def _split_lines(self, text, width):   # never wrap inside a hyphenated word (--start-after)
        import textwrap
        return textwrap.wrap(self._whitespace_matcher.sub(" ", text).strip(), width, break_on_hyphens=False)


class _TopParser(argparse.ArgumentParser):
    """The top-level parser: its help lists the commands by COMMAND_GROUPS instead of one long block."""
    def __init__(self, **kw):
        super().__init__(formatter_class=_TopHelp, **kw)

    def format_help(self) -> str:
        f = self._get_formatter()
        f.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        f.add_text(self.description)
        sub = next(a for a in self._actions if isinstance(a, argparse._SubParsersAction))
        rows = {c.dest: c for c in sub._choices_actions}   # commands added without help (daemon) have none
        for heading, names in (*COMMAND_GROUPS, ("other", tuple(rows))):
            listed = [rows.pop(n) for n in names if n in rows]
            if listed:
                f.start_section(heading)
                f.add_arguments(listed)
                f.end_section()
        f.start_section(self._optionals.title)
        f.add_arguments(self._optionals._group_actions)
        f.end_section()
        f.add_text(self.epilog)
        return f.format_help()


class _Version(argparse.Action):
    """`ttp --version`: the source commit is looked up only when asked for (it may run git)."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(f"ttp {__version__} ({source_commit()})")
        parser.exit()


def main(argv: list[str] | None = None) -> None:
    ap = _TopParser(prog="ttp", description="tt-project: long-running, self-driving projects",
                    epilog="`ttp COMMAND --help` shows a command's options.")
    ap.add_argument("--version", action=_Version, nargs=0, help="show the version and source commit, then exit")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND", help="the command to run",
                            parser_class=argparse.ArgumentParser)   # not _TopParser: commands have no groups

    s = sub.add_parser("new", help="create a project; ends with its checked web app link (exit 3: the link "
                       "did not answer and the line says what is broken)")
    s.add_argument("name", help="the project's name")
    s.add_argument("--dir", help="project root (default: git top level of the current directory)")
    s.add_argument("--host", help="machine to run on (default: this one)")
    s.add_argument("--describe", help="the brief, inline")
    s.add_argument("--describe-file", help="the brief, from a file ('-' for stdin)")
    # "fake" (the test stand-in agent) is accepted but listed only when TTP_TEST_FAKE_PROVIDER is set.
    shown = [*PROVIDER_CHOICES, *(["fake"] if os.environ.get("TTP_TEST_FAKE_PROVIDER") else [])]
    s.add_argument("--provider", choices=[*PROVIDER_CHOICES, "fake"], metavar="{" + ",".join(shown) + "}",
                   help="the agent CLI that runs the project (default: the one found on this machine)")
    s.add_argument("--no-service", action="store_true", help="do not install a boot-time service")
    # coordinator.DEFAULT_EFFORT and EFFORT_ORDER, spelt out: importing coordinator here slows every command.
    s.add_argument("--coordinator-effort", default="xhigh", choices=("minimal", "low", "medium", "high", "xhigh", "max"),
                   help="the effort the coordinator's turns run at (default: xhigh; each provider "
                        "takes its nearest level; later: ttp config <name> coordinator.effort LEVEL)")
    s.add_argument("--no-secrets", action="store_true", help="with --host: do not copy your saved keys there")
    s.add_argument("--home-tz", help=argparse.SUPPRESS)     # with --host there: the workstation's zone
    s.add_argument("--home-from", help=argparse.SUPPRESS)   # and its name
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("web", help="web app link (for a remote project: tunnel command and link)")
    s.add_argument("name", help="the project")
    s.add_argument("--tunnel", action="store_true", help="open the local forward now (it only lets this machine "
                   "view the web app; asked once per user: `ttp consent tunnel`)")
    s.add_argument("--keep", action="store_true", help="with --tunnel: keep it up as a user service across drops "
                   "and reboots (adopts or replaces an existing com.tt-project.tunnel.<name> service)")
    s.add_argument("--unkeep", action="store_true", help="stop and remove the kept tunnel")
    s.set_defaults(fn=cmd_web)
    for name, fn, hlp in (("connect", cmd_connect, "attach this chat to a project; ends with its checked web app link "
                           "(exit 3: the link did not answer and the line says what is broken)"),
                          ("status", cmd_status, "one-screen status"),
                          ("logs", cmd_logs, "daemon log tail"), ("doctor", cmd_doctor, "diagnose setup"),
                          ("prune", cmd_prune, "tidy finished tasks' worktrees now (branches are kept)")):
        s = sub.add_parser(name, help=hlp)
        # `ttp status` alone, inside a project's folder (or a run), is that project's status.
        s.add_argument("name", nargs="?" if name in ("status", "doctor") else None,
                       help="the project" + (" (default: the one this folder or run belongs to)"
                                             if name in ("status", "doctor") else ""))
        if name == "connect":
            s.add_argument("--chat", help="reuse this chat id (default: a new one)")
            s.add_argument("--label", help="a name for the chat, shown in the web app")
            s.add_argument("--home-tz", help=argparse.SUPPRESS)     # the connecting workstation's zone
            s.add_argument("--home-from", help=argparse.SUPPRESS)   # and its name
        if name == "status":
            s.add_argument("--json", action="store_true", help="print the status as JSON")
            s.add_argument("--verbose", action="store_true",
                           help="also the coordinator cache, checks slot waits and unacked upstream fix lines")
        if name == "prune":
            s.add_argument("--dry-run", action="store_true", help="change nothing; list what a sweep would do")
            s.add_argument("--archived", type=int, metavar="TASK",
                           help="list what the sweep archived from this task's worktree (and where)")
            s.add_argument("--restore", metavar="DIR", help="with --archived: extract the task's archived files here")
            s.add_argument("--patch-tracked", action="store_true",
                           help="also free done or cancelled tasks' worktrees with uncommitted tracked changes, "
                                "saving them as a verified patch in the archive")
        if name == "logs":
            s.add_argument("--bytes", type=int, default=6000, help="how much of the log's end to print (default 6000)")
        if name == "doctor":
            s.add_argument("--live", choices=["claude", "codex", "cursor"], metavar="PROVIDER",
                           help="measure one agent CLI with real runs in a scratch project (spends up to "
                           "about $0.55; a missing or logged-out CLI is reported, exit 0)")
            s.add_argument("--loop", choices=["claude", "codex", "cursor"], metavar="PROVIDER",
                           help="run one tiny code task end to end (dispatch, hand-off, review, push to a local "
                           "bare remote) in a scratch project, ticking one daemon of its own (spends up to "
                           "about $1, at most 15 minutes; a missing or logged-out CLI is reported, exit 0)")
            s.add_argument("--json", action="store_true", help="with --live or --loop: the result as JSON")
            s.add_argument("--scheduler", action="store_true",
                           help="check the command-watcher scheduler's lifecycle in a scratch project, each tick in "
                           "a fresh process (no model, $0; exit 1 on a failed step)")
        s.set_defaults(fn=fn)

    s = sub.add_parser("say", help="send a message to the coordinator")
    s.add_argument("name", help="the project (none with --all)")
    s.add_argument("text", help="the user's words, verbatim, or '-' for stdin")
    s.add_argument("--chat", help="the chat it comes from (replies go there)")
    s.add_argument("--context", default="", help="a relayer's own context (data, never the user's word); "
                   "kept apart from the user's words")
    s.add_argument("--finding", default="", help="a relayer's own findings, sent with no user's words; shown to "
                   "the coordinator as data, never the user's word")
    s.add_argument("--all", action="store_true", help="send to every project `ttp list` shows, once each: "
                   "a resend within 24 h reaches only those that did not get it yet")
    s.add_argument("--except", dest="except_", metavar="P,Q", help="with --all: projects to leave out")
    s.add_argument("--broadcast", help=argparse.SUPPRESS)   # set by `ttp say --all` on another machine
    s.add_argument("--client-id", help=argparse.SUPPRESS)   # set by a forwarding machine; resends are skipped
    s.set_defaults(fn=cmd_say)

    s = sub.add_parser("listen", help="print messages for a chat as they arrive", description=(
        "Print messages for a chat as they arrive. For a project on another machine the listen runs "
        "there over ssh; a dropped connection (ssh exit 255, or a signal ending the session) is "
        "retried after 5 s, doubling to 2 min, and each message is printed once per listener. "
        "Exit codes: 0 messages shown (--once) or --timeout reached; 2 the project refused it "
        "(unknown chat or project); 3 a newer listener for the chat replaced this one (not retried); "
        "75 the project stayed unreachable until --timeout ran out; "
        "any other code is the project's own ttp failing."))
    s.add_argument("name", help="the project")
    s.add_argument("--chat", required=True, help="the chat id `ttp connect` printed")
    s.add_argument("--once", action="store_true", help="exit after the first batch")
    s.add_argument("--timeout", type=float, default=0,
                   help="stop after this many seconds (0: never); also bounds reconnecting")
    s.add_argument("--ack", type=int, metavar="ID",
                   help="mark messages up to ID read; later ones stay unread until acknowledged")
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("note", help="append a progress note, or send one to another project")
    s.add_argument("text", nargs="?", help="the note, one line")
    s.add_argument("--to", metavar="PROJECT",
                   help="also file the note in the inbox of another project, as this worker's; tt-project: an "
                        "`upstream: ...` note for its maintainers; PROJECT@MACHINE: the project on that machine, "
                        "by a forward target or the route its notes came by")
    s.add_argument("--reply", metavar="FP",
                   help="answer note FP (its `[note <fp>]`) from another project, over the route it came by, "
                        "even when that project is not in `ttp list`")
    s.add_argument("--severity", choices=["low", "normal", "high"], default="normal",
                   help="with --to: the severity of the event the other project's coordinator gets")
    s.add_argument("--status", metavar="ID",
                   help="where note ID (printed by --to) stands: queued, filed-local, forwarded (in the inbox of the "
                        "machine that runs the project) or delivered (the project read it); exit 0 once delivered, "
                        "1 while on its way, 2 unknown, so it serves as a retry_when")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("notify", help="tell the user something; sent when the run ends")
    s.add_argument("text", help="the message")
    s.add_argument("--severity", choices=["low", "normal", "high", "critical"], default="normal",
                   help="high: an urgent report for the coordinator, relayed while the run lives; "
                        "critical is refused")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("progress", help="report a detached or looping driver's results, refusals, stalls and "
                                        "failures to the daemon (one line from bash; works with the daemon down)")
    ps = s.add_subparsers(dest="progress_cmd", required=True, help="what to do")
    m = ps.add_parser("report", help="append one report to the project's progress.jsonl; exit 0 even when it "
                                     "cannot be written (a warning on stderr), so a driver never stops on it")
    m.add_argument("--key", help="what the report is about, the same each loop (default inside a `ttp detach` "
                                 "job: its task and name)")
    m.add_argument("--resource", help="a refusal's busy resource (lock name); with no --holder, its holders now "
                                      "are recorded from the lock state (`ttp lock --probe --json`), else unknown")
    m.add_argument("--holder", help="who holds the resource (a task, run, pid or external label), as the driver "
                                    "knows it; shown in the stall observation")
    m.add_argument("words", nargs=argparse.REMAINDER, metavar="KIND [REASON]",
                   help="KIND: result (a counted result; ends a stall), refusal, stall or failure (count toward "
                        "one); REASON: why, one line (an exit code, the refusal's text); --key may come anywhere")
    m.set_defaults(fn=cmd_progress)

    s = sub.add_parser("upstream", help="upstream notes across machines: the inbox "
                       "with each note's sender (project, host, task), receive, forwarding status and targets")
    s.add_argument("--receive", action="store_true", help="file notes sent over ssh (JSON lines on stdin); prints an ack")
    s.add_argument("--via", metavar="ALIAS", help="with --receive: the sending machine's alias")
    s.add_argument("--forward-status", action="store_true", help="per target: cursor, last ok, last error (the default)")
    s.add_argument("--forward-now", action="store_true", help="send the pending notes from here to the machines "
                   "the daemon has failed to reach for a while (started in the background by `ttp note` and `ttp status`)")
    s.add_argument("--forward-to", metavar="ALIASES",
                   help="machines to send this machine's notes on to (comma-separated), 'default' or 'none'")
    s.add_argument("--resolve", metavar="FP", help="record that note FP is fixed (with --fixed-in); prints its sender "
                   "and sends the answer once to a sending project on this machine")
    s.add_argument("--fixed-in", metavar="VERSION", help="with --resolve: the release with the fix")
    s.add_argument("--wontfix", metavar="REASON", help="with --resolve: close note FP without a release (not a "
                                                       "plugin bug); the reason is required and recorded")
    s.add_argument("--note", help="with --resolve: what changed, for the project that filed it")
    s.add_argument("--fixed", metavar="FP", help="probe: print the note, its state and the running version; exit 0 once "
                                                  "this project's running daemon has note FP's fix, 1 before, 2 when "
                                                  "unknown, 3 when closed as not a bug (wontfix)")
    s.add_argument("--ack", nargs=2, metavar=("FP", "EVIDENCE"), help="close note FP: the fix works here")
    s.add_argument("--regress", nargs=2, metavar=("FP", "REASON"), help="record that note FP's shipped fix did not "
                                                                        "hold: blocks its close by silence")
    s.add_argument("--fp", nargs=2, metavar=("TITLE", "SPEC"), help="print the id an `upstream: ...` follow-up gets")
    s.add_argument("--mine", action="store_true", help="this project's notes and where each stands")
    s.add_argument("--resolved", action="store_true", help="every resolved note and where it stands")
    s.add_argument("--has-project", metavar="NAME", help="print whether project NAME runs on this machine (JSON); "
                                                         "`ttp note --to` asks the forward targets this over ssh")
    s.add_argument("--note-state", metavar="FP", help="print whether this machine's inbox holds note FP and whether "
                                                      "the project it is for read it (JSON); `ttp note --status` asks this over ssh")
    s.set_defaults(fn=cmd_upstream)

    s = sub.add_parser("push", help="guarded push of this worktree to delivery.push_branch")
    s.add_argument("--free", action="store_true",
                   help="push nothing: exit 0 when no other push to the target branch is running, 1 while one is")
    s.add_argument("--detach", action="store_true",
                   help="push in a process of its own; print its marker and a --result probe, and return at once")
    s.add_argument("--result", metavar="MARKER",
                   help="push nothing: report a detached push; exit 0 once it finished (or died), 1 while it runs")
    s.add_argument("--pushed", action="store_true",
                   help="with --result: exit 0 only if the push landed, 1 while it runs, 3 if it finished "
                        "without pushing (for an on_pass_cmd; no grep needed)")
    s.add_argument("--own", action="store_true",
                   help="publish the checked-out branch under its name, as it is (checks, no rebase): this task's "
                        "ttp/t<id>-... branch, or another named branch as a fast-forward only; "
                        "never delivery.push_branch, main or the default branch")
    s.add_argument("--own-branch", action="store_true",
                   help="as --own, but publish this task's own ttp/t<id>-... branch under its own name, never "
                        "onto a pr_branch it carries: the route when --own may not create that pr_branch "
                        "(delivery.own_push unset lets a code task create its own branch). Publish before handing off: "
                        "the daemon's hand-off lint flags a follow-up that leaves this push to another run")
    s.add_argument("--land", action="store_true",
                   help="inside a run: land this task's own ttp/t<id>-... branch on delivery.push_branch even "
                        "though its spec has no landing step (without it such a push is refused)")
    s.add_argument("--queue", action="store_true",
                   help="push nothing: list the push queue's entries and its last 10 batches")
    s.add_argument("--marker", help=argparse.SUPPRESS)   # the detached process itself
    s.add_argument("--batch", metavar="MARKER", help=argparse.SUPPRESS)   # the push queue's batch process (batch.py)
    s.set_defaults(fn=cmd_push)

    from . import checkplan
    s = sub.add_parser("check-plan", help="run the checks a reviewed definition file lists and verify their inputs "
                                          "against its recorded digests (no skips, no cached passes)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--definition-commit", help="full commit ID of the independently reviewed definition")
    g.add_argument("--definition-at-head", action="store_true",
                   help="harness only: read the definition from HEAD's commit, so covered inputs and their "
                        "digests change in one commit")
    s.add_argument("--definition-path", required=True, help="exact repository path of its JSON file")
    s.add_argument("--purpose", required=True, choices=["harness", "candidate"],
                   help="harness: checks for the project's own harness; candidate: checks for a change "
                        "to deliver (needs --definition-commit)")
    s.set_defaults(fn=checkplan.command)

    s = sub.add_parser("checks", help="run the local checks on HEAD and record the result")
    s.add_argument("--gap", metavar="SIG", help="read-only probe: exit 0 when a missing check's config changed")
    s.add_argument("--gap-config", metavar="FINGERPRINT",
                   help="with --gap: the push_checks fingerprint the waiter failed under")
    s.add_argument("--fresh", action="store_true",
                   help="run the checks even when they already passed on this tree")
    s.add_argument("--full", action="store_true",
                   help="run the project's checks even for a docs/data-only diff (no fast path)")
    s.add_argument("--light", action="store_true",
                   help="the commands after -- are light checks (syntax, lint, format, validation): with only "
                        "light checks to run they run at once, without waiting for a checks slot (checklane)")
    s.add_argument("--purpose", choices=["candidate", "harness"], default="candidate",
                   help="harness: qualify a harness-preparation head with the purpose harness (else the default "
                        "harness plan) and untagged checks; "
                        "candidate gates are not run and the pass never counts for a PR or a push")
    s.add_argument("--detach", action="store_true",
                   help="run them in a process of their own; print a retry_when that exits 0 once they finished or died")
    s.add_argument("--no-prestage", action="store_true",
                   help="with --detach: skip delivery.check_prestage, the short foreground run before the full checks")
    s.add_argument("--result", metavar="RUN_DIR",
                   help="run nothing: exit 0 once a run's detached checks finished or died, 1 while they run")
    s.add_argument("--env-ok", nargs="*", metavar="SIG", default=None,
                   help="run nothing: exit 0 once the named environment faults (or, with none named, every "
                        "open one) no longer hit a missing check runner, 1 while one does")
    s.add_argument("--slot-status", action="store_true",
                   help="run nothing (also `ttp checks status`): print each repository's checks lanes, their "
                        "holders (pid, task, run, age, alive) and the waiters in queue order; never queues")
    s.add_argument("--json", action="store_true", help="with --slot-status or status: as JSON")
    s.add_argument("--rc", help=argparse.SUPPRESS)   # the detached process itself: where it writes its exit code
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="extra check commands after --: one quoted string runs through the shell "
                        "(ttp checks -- 'FOO=1 pytest -q && ruff check'); several quoted strings run as "
                        "one check each (-- 'pytest -q' 'ruff check .'); plain words run as argv "
                        "(-- pytest -q tests/a.py). One that delivery.push_checks already runs is dropped")
    s.set_defaults(fn=cmd_checks)

    s = sub.add_parser("stats", help="context re-read (cache-read) tokens per run and per $, by kind and tier")
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    s.add_argument("--days", type=float, default=7, help="the window, or each side's span with --split-at")
    s.add_argument("--since", help="start: epoch seconds or ISO date/time (home zone unless it has an offset)")
    s.add_argument("--until", help="end, the same way (default now)")
    s.add_argument("--split-at", help="compare runs before and after this time (a change landing)")
    s.add_argument("--top", type=int, default=10, help="how many of the costliest runs to list (default 10)")
    s.add_argument("--tools", action="store_true", help="tool output by category instead: the file-read share")
    s.add_argument("--runs", action="store_true",
                   help="worker runs instead: provider, model, tier, outcome (with any route fallback), wall time, cost")
    s.add_argument("--tier", help="with --runs: only this tier's runs (implies --runs)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("audit", help="the daily review's self-efficiency audit: asks, wasted runs, idle wakes, "
                                     "$ per outcome, review loops, stuck work and overrides held past their end")
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    s.add_argument("--hours", type=float, default=24, help="the window to audit, in hours (default 24)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("replicate", help="backup copies of the project's state on a standby machine "
                                         "(failover.standby): status, take one now, verify them, or restore one "
                                         "into a project folder", description=(
        "Without flags: the snapshot status. --now: take and ship one now. --verify: check every snapshot at "
        "the standby (checksums and chains). --restore DIR --into PATH: on the standby, make PATH/tt-project "
        "from the newest complete snapshot in DIR (or --seq N)."))
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--now", action="store_true", help="take a snapshot and send it to the standby now")
    g.add_argument("--verify", action="store_true", help="check every snapshot at the standby (checksums and chains)")
    g.add_argument("--restore", metavar="DIR", help="on the standby: restore from the snapshots in DIR")
    s.add_argument("--into", metavar="PATH", help="with --restore: make the project folder PATH/tt-project")
    s.add_argument("--seq", type=int, help="with --restore: this snapshot number instead of the newest complete one")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_replicate)

    s = sub.add_parser("push-audit", help="find done tasks that say they pushed a commit the remote does not have")
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    s.add_argument("--days", type=float, default=2, help="done tasks updated within this many days")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_push_audit)

    s = sub.add_parser("ttr", help="how long each machine took to recover from problems: incidents, median, "
                                   "90th percentile and longest time, and the ones still open")
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    s.add_argument("alias", nargs="?", help="only this machine")
    s.add_argument("--days", type=float, default=30, help="the window: incidents open now or ended within it")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_ttr)

    s = sub.add_parser("spend-today", help="this machine's tt-project spend today, by provider and account "
                                           "(for the global daily cap)")
    s.add_argument("--since", type=float, help="from this Unix time (default: the budget day's start)")
    s.add_argument("--until", type=float, help="up to this Unix time (default: the budget day's end)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.add_argument("--receive", action="store_true",
                   help="keep the spend another machine pushes on stdin (it runs this over ssh)")
    s.add_argument("--via", help="with --receive: the sending machine's alias")
    s.set_defaults(fn=cmd_spend_today)

    s = sub.add_parser("clip", help="run a command, keep its full output in a file and print a short "
                                    "excerpt (a test run's failures) plus the path")
    s.add_argument("--lines", type=int, default=40, help="at most this many lines of an ordinary excerpt")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    s.set_defaults(fn=cmd_clip)

    s = sub.add_parser("sandbox", help="run a command (a test suite) in its own PID namespace, where it cannot see "
                                       "or signal other processes; nothing it starts outlives it")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    s.set_defaults(fn=cmd_sandbox)

    s = sub.add_parser("skill", help="print the path of an installed tt-project skill's SKILL.md (no name: list them)")
    s.add_argument("name", nargs="?", help="e.g. tt-project-harness")
    s.set_defaults(fn=cmd_skill)

    s = sub.add_parser("broker-released", help="exit 0 once a device broker's hold is released (/health "
                                               "held false with a healthy fsm, or a 'hold released' or "
                                               "device_released log line), 1 while not")
    s.add_argument("files", nargs="*", help="/health JSON and log text to read (default: stdin)")
    s.set_defaults(fn=cmd_broker_released)

    s = sub.add_parser("hours", help="the user's usual active hours, from their own messages in the home zone; "
                                     "--check exits 0 when now is a good time for a step they must act on, 1 not")
    s.add_argument("--json", action="store_true", help="print the window and its data as JSON")
    s.add_argument("--check", action="store_true", help="exit 0 inside the hours (or with no pattern yet), 1 outside")
    s.add_argument("--for", dest="need", type=float, default=0, metavar="SECONDS",
                   help="also need this much of the window left (a step's time to act)")
    s.add_argument("--days", type=float, default=14, help="look back this many days (default 14)")
    s.set_defaults(fn=cmd_hours)

    s = sub.add_parser("killscan", help="flag kills by name or pattern (pkill, killall, pgrep/pidof, ps | grep into kill) "
                                        "in scripts before running them; --shim writes stand-ins that only log")
    s.add_argument("files", nargs="*", help="scripts to check; exits 1 if one kills by name or pattern, 2 if one cannot be read")
    s.add_argument("--shim", metavar="DIR", help="write logging stand-ins for pkill, killall, pgrep and pidof "
                                                 "into DIR (put DIR first on PATH)")
    s.set_defaults(fn=cmd_killscan)

    s = sub.add_parser("svcscan", help="flag service unit changes (systemctl stop/disable/mask/start/restart/"
                                       "enable/unmask, also under sudo) in scripts before running them; "
                                       "--shim writes stand-ins that only log", description=(
        "Check a driver with `ttp svcscan <script>` before running it on a shared machine: never stop, "
        "restart, disable or mask a shared service unless your task says to. It flags any systemctl verb but "
        "a read-only query (also under sudo -n, in Python argv lists and across backslash-continued lines) "
        "and `service <unit> stop|restart|...`. To exercise the driver without changing a real unit, run "
        "`ttp svcscan --shim <dir>`, then `PATH=<dir>:\"$PATH\" <script>`: the stand-ins log each change to "
        "<dir>/svcscan.log and pass only read-only queries to the real systemctl. A call by path "
        "(/usr/bin/systemctl, ./systemctl) or over ssh is flagged as such: a local shim does not stop it, so "
        "edit it out. Exit codes: 0 none found, 1 a unit change found, 2 a script cannot be read."))
    s.add_argument("files", nargs="*", help="scripts to check; exits 1 if one changes a unit, 2 if one cannot be read")
    s.add_argument("--shim", metavar="DIR", help="write logging stand-ins for systemctl, service and sudo "
                                                 "into DIR (put DIR first on PATH)")
    s.set_defaults(fn=cmd_svcscan)

    s = sub.add_parser("leftover-procs", help="hand-off check: exit 1 while a done task still has live processes "
                                              "in its own worktree", description=(
        "For worker.handoff_checks: `ttp leftover-procs {result}`. A process is the task's only when its "
        "resolved working directory is the task's own worktree (<project>/tt-project/worktrees/t<id>) or below "
        "it, by whole path components: t39 never claims t390, and another project's t<id> is never this "
        "one's. A pid the hand-off lists in leftover_procs [{pid, stop, ends}] is not reported. Read-only; "
        "Linux /proc (none found elsewhere). Exit codes: 0 none, 1 some found, 2 usage or an unreadable hand-off."))
    s.add_argument("result", nargs="?", help="the hand-off's result.json (default: TTP_RESULT_JSON)")
    s.add_argument("--task", type=int, help="the task id (default: TTP_TASK_ID)")
    s.set_defaults(fn=cmd_leftover_procs)

    s = sub.add_parser("goal", help="stock goal checks, one JSON line per observation; exit 0 met, 1 not, "
                                    "75 could not check")
    gs = s.add_subparsers(dest="goal", required=True, help="the goal to check")
    g = gs.add_parser("suite_green", help="a test command exits 0 at the current head")
    g.add_argument("--name", help="the suite's name (its key; default: the command)")
    g.add_argument("--cwd", default=".", help="run in this folder (its HEAD is reported)")
    g.add_argument("--min-tests", type=int, default=0, help="fail unless at least this many tests ran")
    g.add_argument("--count-regex", help="regex whose first group is the test count (default: pytest, "
                                         "unittest and ctest summaries)")
    g.add_argument("--timeout", type=float, default=3600, help="seconds; past it the check could not run")
    g.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    g = gs.add_parser("artifact_fresh", help="a file or glob's newest match exists, is not trivially small "
                                             "and is newer than the bounds given")
    g.add_argument("path", help="a path or glob (** allowed)")
    g.add_argument("--max-age-h", type=float, help="no older than this many hours")
    g.add_argument("--newer-than-commit", metavar="REV", help="newer than this commit's commit time")
    g.add_argument("--repo", default=".", help="the repository of --newer-than-commit")
    g.add_argument("--newer-than", metavar="PATH", help="newer than this input file")
    g.add_argument("--min-bytes", type=int, default=1, help="at least this size (default 1)")
    g = gs.add_parser("service_up", help="a systemd unit is active and/or a URL answers an expected status")
    g.add_argument("--unit", help="a systemd unit")
    g.add_argument("--user", action="store_true", help="a user unit (systemctl --user)")
    g.add_argument("--host", help="check the unit on this ssh alias")
    g.add_argument("--url", help="an http(s) URL")
    g.add_argument("--expect", type=int, nargs="+", help="accepted status codes (default: any 2xx or 3xx)")
    g.add_argument("--body-contains", help="text the answer must contain")
    g.add_argument("--timeout", type=float, default=30, help="seconds per probe")
    s.set_defaults(fn=cmd_goal)

    s = sub.add_parser("lock", help="hold a shared resource while one command runs", description=(
        "Hold a shared resource while one command runs; waiters are served in arrival order, and exit 75 "
        "means busy or paused (retry_when `ttp lock --probe <resource>`). --shared is for read-only "
        "commands: shared holders run together, while an exclusive holder (a plain `ttp lock`, or an "
        "exclusive task's whole-run hold) and shared ones exclude each other; a queued exclusive waiter is "
        "not jumped by later shared ones. Not for a device lock (one in config `device.locks` or ending in "
        "`-device`): --shared on one exits 2 and runs nothing, and `--probe --shared` on one answers as the "
        "plain probe does. Nested: a `ttp lock` inside one that holds the same lock in the same or a "
        "stronger mode (or inside an exclusive task's run) just runs; an exclusive one inside a shared hold "
        "of the same lock exits 2 at once, as it could only wait for itself. Several resources, named in one word (`ttp lock "
        "A,B -- <cmd>`, `--probe A,B`), are taken all or nothing: it keeps its place in every queue and holds none "
        "of them until it can take them all."))
    s.add_argument("--probe", action="store_true",
                   help="exit 0 if the resource is free, not paused and nobody queues for it, else 75 (for retry_when)")
    s.add_argument("--shared", action="store_true",
                   help="read mode: run alongside other shared holders; exclusive holders and these exclude each other "
                        "(not for device locks)")
    s.add_argument("--task", default=None,
                   help="probe for this task id: a free lock then also needs config locks.pre_grant_check to "
                        "grant it (default: TTP_TASK)")
    s.add_argument("--json", action="store_true",
                   help="probe, printing JSON: free, paused, waiting and each live holder with its pid and whether "
                        "it lives (a keeper of detached jobs: via \"keeper\", the taker's pid in taker_pid); "
                        "several resources: a list of these (runs nothing)")
    s.add_argument("resource", help="the resource's name (a device, machine or build folder); several "
                                    "in one word (`ttp lock A,B -- <cmd>`) are held all or nothing")
    s.add_argument("--timeout", type=float, default=None,
                   help="give up after this many seconds (exit 75); 0 waits as long as it takes; "
                        "inside a run: at most half its stall limit")
    s.add_argument("--disruptive", action="store_true",
                   help="the command may reset, power-cycle or reboot what it holds: `ttp machines activity` "
                        "and the resume note after a reboot list it first")
    s.add_argument("command", nargs=argparse.REMAINDER, help="the command to run while holding it (after --)")
    s.set_defaults(fn=cmd_lock)

    s = sub.add_parser("ssh-preflight", help="exit 0 once a host answers over ssh, 255 while not (for "
                                             "retry_when), 10 host key not verified, 11 auth failed; "
                                             "@<resource> takes the host from config device.hosts")
    s.add_argument("target", help="an ssh host, or @<resource> (config device.hosts or device.runners)")
    s.add_argument("--timeout", type=float, default=30.0, help="give up on ssh after this many seconds")
    s.add_argument("--classified", action="store_true",
                   help="exit 12 instead of 255 when the host is not reachable (not for retry_when: "
                        "only 255 keeps a waiting task asleep)")
    s.set_defaults(fn=cmd_ssh_preflight)

    s = sub.add_parser("land", help="land a reviewed head on a local push branch (code repo with no git remote): "
                                    "replay its commits onto the tip, run the push checks (delivery.push_checks) on the "
                                    "replayed head, fast-forward; 0 landed, 3 conflict, other exits land nothing")
    s.add_argument("head", help="the reviewed commit (or branch) to land")
    s.add_argument("--task", type=int, help="the task to record the landing for (default: this run's task)")
    s.add_argument("--root", metavar="REPO", help="land in this git repo instead of the current directory's "
                   "(e.g. a temporary clone with its remote removed, to try it first); a landing in a "
                   "repo other than the project's is not recorded, and there a main/master push branch needs no "
                   "delivery.allow_protected_push_branch (the repo has no remote, so nothing is published)")
    s.add_argument("--dry-run", action="store_true", help="write nothing and move no ref: report the replay, "
                   "a conflict (exit 3), the push checks it would run (without running them) and the records "
                   "it would write; exits as a landing would")
    s.set_defaults(fn=cmd_land)

    s = sub.add_parser("landed", help="exit 0 once a commit or a task's landing is on the push branch, even "
                                      "rebased (same patch-id or author/date/subject), 1 while not yet, 2 if it "
                                      "cannot tell (for start_when; `landed:#<id>` runs it)")
    s.add_argument("ref", nargs="?", help="a commit, or #<id> for a task's landing")
    s.add_argument("--task", type=int, help="the task whose landing to look for")
    s.add_argument("--onto", help="the branch to look on (default: delivery.push_branch)")
    s.set_defaults(fn=cmd_landed)

    s = sub.add_parser("ack-wait", help="wait model-free for another project's reply to a `ttp note --to`: probe "
                                        "(a retry_when: 0 once replied, 1 while not, 2 if it cannot tell) or settle "
                                        "(an on_pass_cmd: writes the hand-off quoting the reply to $TTP_RESULT)")
    s.add_argument("action", choices=["probe", "settle"], help="probe: check for the reply; settle: write the hand-off")
    s.add_argument("--from", dest="frm", required=True, metavar="PROJECT", help="the project that replies")
    s.add_argument("--hash", required=True, help="the note's id or sha256 prefix, as `ttp note --to` printed it; "
                                                 "the reply names it")
    s.add_argument("--status", choices=["done", "needs_review"], default="done", help="settle: the hand-off's status")
    s.set_defaults(fn=cmd_ack_wait)

    s = sub.add_parser("detach", help="start a job that outlives the run; writes <name>.rc")
    s.add_argument("--check", nargs="*", metavar="RC",
                   help="exit 0 once every job of these .rc paths ended with exit 0, 4 once all ended or are "
                   "gone but one failed or left no exit code, else 1 (for retry_when)")
    s.add_argument("--remote", metavar="HOST",
                   help="start the job on this ssh host with setsid nohup; it survives this run and a reboot here. It "
                   "records that host's boot id, so a driver gone without an exit code reads as died with the "
                   "host reboot (boot id changed), crashed or killed (same boot), or unknown (unreadable)")
    s.add_argument("--dir", help="with --remote: the remote folder for its files (default ~/.ttp-detach/<run>)")
    s.add_argument("--remote-host", metavar="HOST",
                   help="a local job that drives a job on this ssh host: records its boot id, and an exclusive "
                   "run's hold keeper ends the job and lets its hold go once that host reset or --remote-pid is "
                   "gone without --remote-marker (an unreachable host keeps the hold)")
    s.add_argument("--remote-pid", metavar="PID|FILE", help="with --remote-host: the remote driver's pid, or a "
                   "file on that host holding it")
    s.add_argument("--remote-marker", metavar="PATH", help="with --remote-host: the file on that host the remote "
                   "side writes once it finished; while it exists the job is never ended as dead")
    s.add_argument("--host", help="with --check: the ssh host the jobs run on; the paths are <dir>/<name>; "
                   "255 when ssh fails")
    s.add_argument("--mirror", metavar="DIR",
                   help="with --check --host: the local folder (the launching run's) that keeps each job's exit code "
                   "and last log lines as <name>.remote.rc/.remote.log/.remote.json, so a result outlives the host "
                   "answering; when ssh fails, a mirrored end reads as 0 or 4, not 255 (default: $TTP_RUN_DIR)")
    s.add_argument("--rows", metavar="FILE",
                   help="with --check (and --host: a file on that host): also exit 0 once this file has more "
                   "than --after lines; with it, 0 also once a job ended or its driver is gone (killed, a "
                   "reboot or power cycle), else 1, so one probe wakes on a new result row, the end or a dead driver. A long "
                   "device run appends each step's result to it and fsyncs it as the step ends, not only an "
                   "end-of-run summary, so a reboot or crash keeps the steps already done")
    s.add_argument("--after", type=int, metavar="N", help="with --rows: the line count already seen (default 0)")
    s.add_argument("--lock", action="append", metavar="RESOURCE",
                   help="repeatable (or A,B): the job runs its command under one `ttp lock` of them all, so it takes "
                   "them all or nothing by ttp lock's rules before the command starts and holds them until it ends "
                   "(its .rc) or its processes are gone; a paused one exits 75 at once and starts nothing")
    s.add_argument("--reap", metavar="NAME|RC",
                   help="close the record of a job that is over now, as the daemon does each minute: ended "
                   "with its exit code, or died when gone without one; refuses (exit 1) while it runs; idempotent")
    s.add_argument("--stop", metavar="RC",
                   help="stop a running job (its group, its matching broker jobs, its lock slots), logged; allowed "
                   "when its task has ended, or for an open priority-1 task whose resources its locks hold; "
                   "refused (exit 3) otherwise; idempotent")
    s.add_argument("--resumable", action="store_true",
                   help="the job is safe to rerun from the start: after a host reboot cuts it short, the daemon "
                   "starts it again while the task still waits on it, with no model run (up to "
                   "budget.max_job_relaunches times, default 3)")
    s.add_argument("--results", metavar="GLOB",
                   help="files the job writes per finished unit, relative to its working dir (e.g. 'out/*.json'): "
                   "each new match counts as a result report from the job, keeping an obligation its task owns on "
                   "time with no `ttp progress report result` in the driver")
    s.add_argument("--spend-cmd", metavar="CMD",
                   help="with --spend-cap, for a job that calls a paid API: a shell command printing one number, the "
                   "job's spend so far in USD; while the job runs the daemon runs it model-free from the project "
                   "root every --spend-every s, stops the job's process group once a read reaches the cap and raises "
                   "one event; 3 failed reads in a row raise one event and leave the job running. Only the number "
                   "and a failure reason are logged, never its output or environment (it may use `ttp secret env`)")
    s.add_argument("--spend-cap", type=float, metavar="USD", help="with --spend-cmd: the job's spend cap in USD")
    s.add_argument("--spend-every", type=int, metavar="S",
                   help="with --spend-cmd: seconds between spend reads (default 300, at least 60)")
    s.add_argument("--cores", type=int, help="cores the job is meant to keep busy (default: read from -j N, "
                   "--jobs/--threads/--workers N, OMP_NUM_THREADS); the daemon flags sustained under-use")
    s.add_argument("--devices", type=int, help="devices the job is meant to use")
    s.add_argument("--mem-gb", type=float, help="memory the job asked for (recorded only)")
    s.add_argument("name", nargs="?", help="the job's name (its .log and .rc files are named after it)")
    s.add_argument("command", nargs=argparse.REMAINDER, help="the command to run (after --)")
    s.set_defaults(fn=cmd_detach)

    from . import ciwait
    s = sub.add_parser("ci", help="exit 0 once a commit's CI runs completed or a job hung, 1 while they run, "
                                  "75 if gh cannot answer (for retry_when)")
    s.add_argument("--repo", help="owner/repo (default: the repository of the current directory)")
    s.add_argument("--branch", help="the branch whose newest commit's runs to wait for")
    s.add_argument("--sha", help="wait for this commit's runs instead of the branch's newest")
    s.add_argument("--workflow", help="only this workflow")
    s.add_argument("--run", help="wait for this one run id")
    s.add_argument("--no-hang", action="store_true", help="wait for completion only, never wake on a hung job")
    s.add_argument("--factor", type=float, default=ciwait.FACTOR, help="a job is hung past this many times its median")
    s.add_argument("--floor-min", type=float, default=ciwait.FLOOR_MIN, help="never call a job hung before this many minutes")
    s.add_argument("--default-min", type=float, default=ciwait.DEFAULT_MIN,
                   help="the limit in minutes when no finished run of the workflow is known")
    s.set_defaults(fn=cmd_ci)

    s = sub.add_parser("devq", help="queue jobs on the project's serial device-job runner (config device.runners)")
    s.add_argument("op", choices=["submit", "probe", "start", "status", "clear", "list"], help="what to do")
    s.add_argument("runner", nargs="?", help="the runner's name in config device.runners")
    s.add_argument("rest", nargs=argparse.REMAINDER,
                   help="submit: --id <unique id> [--config <drop-rule key; default the id>] [--timeout <s>] "
                        "[--workdir <dir on the host>] [--no-lint] -- <command>; probe: <id>; status: [<id>]; clear: <config>")
    s.set_defaults(fn=cmd_devq)

    s = sub.add_parser("list", help="projects registered on this machine")
    s.add_argument("--status", action="store_true", help="one status line per project, as `ttp overview`")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("overview", help="every project on this machine: daemon, version, asks, waiting, running, "
                                        "spend today; other projects are only read")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_overview)
    s = sub.add_parser("deployed", help="exit 0 once every project on this machine runs a release by its daemon's "
                                        "own receipt, 1 while any is pending (with owner and resume time), 2 if "
                                        "it cannot tell; read only (for retry_when)")
    s.add_argument("version", nargs="?", help="the release to look for (default: the installed one)")
    s.add_argument("--commit", help="its commit (default: the installed one's, when the versions match)")
    s.add_argument("--projects", help="only these projects, comma-separated (default: every one `ttp list` shows here)")
    s.add_argument("--fresh-s", type=float, default=300, help="a completed tick at most this old (default 300)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.add_argument("--after-push", action="store_true",
                   help="accept owned merges with takeover times, record follow-ups in this project, "
                        "and alert on failures; exit 0 for done/pending, 1 for failed")
    s.set_defaults(fn=cmd_deployed)
    s = sub.add_parser("find", help="locate a project by name (registry, then chat logs)")
    s.add_argument("name", help="the project")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("adopt", help="record where a project lives")
    s.add_argument("name", help="the project")
    s.add_argument("--host", help="the machine it runs on (default: this one)")
    s.add_argument("--dir", required=True, help="its project root on that machine")
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("task", help="add, list, show, cancel or requeue tasks by hand (requeue: a blocked or "
                                    "stopped task runs again; add may wait for --start-when or --start-after); "
                                    "set-when re-points a task not yet started; superseded records a done task's "
                                    "work as landed under another commit (also a code task in review whose work "
                                    "landed, or a harness task in review whose commit is merged into the harness: "
                                    "it is then closed as done); abandon records that a done code task's "
                                    "work will never land (its branch is kept), so local-only, push-audit and landed "
                                    "checks leave it out; rejudge reruns the daemon's hand-off checks on a done or "
                                    "failed task's stored hand-off and corrects a wrong partial mark")
    s.add_argument("name", help="the project")
    s.add_argument("action", choices=["add", "list", "show", "cancel", "requeue", "set-when", "superseded",
                                          "abandon", "rejudge"],
                   help="what to do")
    s.add_argument("title", nargs="?", default="", help="add: the title; show/cancel/requeue/set-when/superseded/abandon/rejudge: the task id")
    s.add_argument("probe", nargs="?", help="set-when: the new retry_when/start_when command (\"\" clears it); "
                   "superseded: the commit on the push branch that carries the task's work (for a harness task: "
                   "the commit merged into the harness); abandon: why its work will never land")
    s.add_argument("--ref", help="superseded: the ref that holds the commit (default: the push branch; in a "
                   "second repository the task worked in, any of its branches); for a task with no branch on record, "
                   "required: the branch of a second repository that holds its work, or a ref the commit is on; a commit already on that "
                   "local or remote-tracking ref skips the file-overlap check")
    s.add_argument("--repo", help="superseded: a repository path to search first for the branch and the commit "
                   "(then the repository of the current directory, the repositories the task's runs worked in, the project root)")
    s.add_argument("--json", action="store_true", help="list: JSON rows with start_when/retry_when; show: the task record")
    s.add_argument("--status", help="list: only tasks in these statuses, comma-separated (queued,running,waiting,"
                   "blocked,review,done,failed,changes_needed,cancelled, ...); filtered before any cap, so every match "
                   "shows unless --limit is given")
    s.add_argument("--all", action="store_true", help="list: every task, not only the newest 50")
    s.add_argument("--limit", type=int, help="list: at most this many, newest first (default 50 with no --status; "
                   "a cut list says so on stderr)")
    s.add_argument("--spec", help="add: what the task should do, in full (the worker reads only this)")
    s.add_argument("--kind", default="work",
                   help="add: one of " + ", ".join(TASK_KINDS) + " (default work)")
    s.add_argument("--tier", default="standard",
                   help="add: one of " + ", ".join(TASK_TIERS) + ", the model's effort (default standard)")
    s.add_argument("--priority", type=int, default=3, help="add: 1 (most urgent) to 5 (default 3)")
    s.add_argument("--budget", type=float, help="add: its per-run dollar cap (default: budget.task_default_usd for its tier)")
    s.add_argument("--start-when", help="add: a probe; the task starts only once it exits 0 (e.g. landed:#<id>)")
    s.add_argument("--start-after", help="add, set-when: a delay (2h, 3d) or ISO time (home zone unless it names "
                   "one) before which it does not start; set-when: now or \"\" clears it")
    s.add_argument("--why", help="add: plain words for --start-when, shown instead of the probe; rejudge: "
                   "the reason logged with the change")
    s.add_argument("--dry-run", action="store_true", help="rejudge: show what would change, write nothing")
    s.add_argument("--on-pass-cmd", help="set-when: a waiting task's model-free completion command, run once its "
                   "retry_when passes (\"\" clears it); without a probe its retry_when stays; an "
                   "always-passing probe alone is refused while the hand-off's on_pass_cmd stays")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("obligation", help="urgent deliverables owed to the user: add, attach owner tasks, "
                                          "record progress and receipts, drop one recorded by mistake, list")
    s.add_argument("action", choices=["add", "attach", "progress", "receipt", "drop", "list"], help="what to do")
    s.add_argument("args", nargs="*", help="add: the text; attach: <id> <task>...; progress/receipt: <id> <evidence>; "
                                           "drop: <id> <why>")
    s.add_argument("--owner", type=int, action="append", help="add: a required owner task (repeatable)")
    s.add_argument("--milestone", help="receipt: the milestone key; leaves the obligation open")
    s.add_argument("--task", type=int, help="progress/receipt outside a run: the owner task it is for")
    s.add_argument("--project", help="the project (default: the one here)")
    s.add_argument("--json", action="store_true", help="list: print JSON")
    s.set_defaults(fn=cmd_obligation)

    s = sub.add_parser("memory", help="add a memory, retire one with --forget, merge with --supersedes, or --search")
    s.add_argument("name", help="the project")
    s.add_argument("text", nargs="?", help="the memory to add, one entry")
    s.add_argument("--kind", default="fact",
                   help="restriction, preference, resource, decision or fact (default fact)")
    s.add_argument("--forget", action="extend", nargs="+", metavar="ENTRY",
                   help="retire entries (names in [brackets]) to memory/archive/: several names after one "
                        "--forget, or the flag repeated, make one harness commit; an unknown name is "
                        "reported and the others are still retired. It takes every word after it, so "
                        "give the text first: ttp memory <name> \"<text>\" --forget <entry>")
    s.add_argument("--standing", action="store_true",
                   help="an instruction that applies every time its case comes up (kept as a preference); "
                        "one finished instance never retires it")
    s.add_argument("--title", help="with the text: the entry's title (its MEMORY.md line and file name; "
                                   "default its first line, cut to 80 chars)")
    s.add_argument("--audience", choices=(*MEMORY_AUDIENCES, "default"),
                   help="with the text: who gets it. workers: pinned in worker prompts; coordinator: kept "
                        "out of them (a restriction still reaches them). Default: by kind and wording. With --supersedes the default is the "
                        "audience the replaced entries share; 'default' clears it in the same write")
    s.add_argument("--why", help="with --forget: why it ends; required for a standing entry (its end condition "
                                  "or the user's words ending it)")
    s.add_argument("--search", nargs="+", metavar="WORD",
                   help="list live and archived entries holding every word (any case); changes nothing")
    s.add_argument("--supersedes", action="extend", nargs="+", metavar="ENTRY",
                   help="with the text: add it as a merged entry and archive the entries it replaces, in one "
                        "harness commit. Refused whole if a name is unknown or archived, a standing entry would "
                        "be replaced by one not --standing, a restriction, preference or resource by another "
                        "kind, or the quoted (user's verbatim) words of a restriction, preference or standing "
                        "entry are missing from the text. The new entry keeps the earliest end (expires, until, "
                        "until_probe) of the entries it replaces")
    s.add_argument("--permanent", action="store_true",
                   help="with --supersedes: the new entry drops the replaced entries' end and never expires")
    s.add_argument("--settle", nargs="*", metavar="ENTRY",
                   help="move decision entries into the settled-areas register (harness/settled_areas.json) and "
                        "archive them from memory, in one harness commit; with no names, every decision that "
                        "records a review-area-cap settlement. Each entry's whole text stays as the record's "
                        "decision")
    s.add_argument("--dry-run", action="store_true",
                   help="show what would be added, retired or moved (--settle); write and commit nothing")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("schedules", help="list or show schedules (read-only); --export moves them into the harness")
    s.add_argument("name", help="the project")
    s.add_argument("action", nargs="?", choices=["list", "show"], default="list",
                   help="list (default) or show <schedule>: read-only, writes nothing")
    s.add_argument("schedule", nargs="?", help="the schedule to show")
    s.add_argument("--export", action="store_true",
                   help="MOVES the schedules into harness/schedules.json and COMMITS it; that file then holds "
                        "them. Asks first; needs --yes when not interactive. To only read, use show")
    s.add_argument("--yes", action="store_true", help="confirm --export without a prompt")
    s.set_defaults(fn=cmd_schedules)

    s = sub.add_parser("heal", help="heal checks: list them, or test one's check (dry run)")
    s.add_argument("action", choices=("list", "test"), help="list the heal checks, or test one")
    s.add_argument("check", nargs="?", help="test: the heal check (its schedule's name)")
    s.add_argument("--project", help="the project (default: the one this folder or run belongs to)")
    s.set_defaults(fn=cmd_heal)

    s = sub.add_parser("issues", help="daemon and watcher issues: list them, or close a stale one with a reason "
                       "(list/close)", description=(
        "Issues are the conditions the daemon and command watchers observed (daemon:schedule-gap, "
        "watcher:<name> and the rest). `close` ends one whose condition is over but that nothing closes by "
        "itself (say a schedule gap after a host move); the reason and time go in its history and the "
        "activity feed. A condition that is still live is seen again at its next detection and reopens."))
    iss = s.add_subparsers(dest="action", required=True, help="what to do")
    m = iss.add_parser("list", help="the project's open issues (--all: closed ones too), newest first")
    m.add_argument("--project", help="the project (default: the one this folder or run belongs to)")
    m.add_argument("--all", action="store_true", help="include closed and ignored issues")
    m.add_argument("--limit", type=int, default=50, help="at most this many (default 50)")
    m.add_argument("--json", action="store_true", help="print JSON")
    m = iss.add_parser("close", help="close an issue with a reason; its condition, if still live, reopens it")
    m.add_argument("issue", metavar="ID-OR-KEY", help="the issue's id (as `ttp issues list` shows), fingerprint or key")
    m.add_argument("--why", help="the reason (required, non-empty)")
    m.add_argument("--project", help="the project (default: the one this folder or run belongs to)")
    s.set_defaults(fn=cmd_issues)

    s = sub.add_parser("mute", help="a project's observation mutes: list them, or end one now (list/end)")
    ms = s.add_subparsers(dest="action", required=True, help="what to do")
    m = ms.add_parser("list", help="the project's active mutes")
    m.add_argument("project", help="the project")
    m = ms.add_parser("end", help="end a mute now, without a coordinator turn; its end summary still comes")
    m.add_argument("project", help="the project")
    m.add_argument("source", help="the mute's source as `ttp mute list` shows it (e.g. watcher:<name>)")
    m.add_argument("match", help="the mute's match text (any case)")
    s.set_defaults(fn=cmd_mute)

    s = sub.add_parser("machines", help="your machines, shared by all your projects (add/list/status/remove/push)")
    ms = s.add_subparsers(dest="action", required=True, help="what to do")
    m = ms.add_parser("add", aliases=["set"], help="add a machine, or change its tags, note or recovery owner")
    m.add_argument("alias", help="a short name, also used as the resource name in tasks (e.g. box-a)")
    m.add_argument("--tags", help="what it offers, comma-separated (e.g. device,x86)")
    m.add_argument("--note", help="one line for the coordinator (no secrets)")
    m.add_argument("--until", help="when the note stops being true (a delay such as 3d, or an ISO time, home zone "
                                   "unless it names one); past "
                                   "it the coordinator sees it as stale (\"\" = no end)")
    m.add_argument("--min-free-gb", dest="min_free_gb",
                   help="disk guard threshold on this machine's filesystem, overriding the projects' "
                        "disk.min_free_gb there (0 = off; \"\" = back to the projects' own)")
    m.add_argument("--hostname", help="its short host name, when that is not the alias (\"\" = none)")
    m.add_argument("--shared", nargs="?", const="", metavar="NAMES",
                   help="resources on it that all your projects share: one set of `ttp lock` slots and one "
                        "pause across projects (comma-separated; default: the alias itself)")
    m.add_argument("--unshared", action="store_true", help="its resources are per project again")
    m.add_argument("--owner", help="the project that recovers it when it stays down or held (\"\" = none)")
    m.add_argument("--fallback", help="the project that takes over when the owner cannot act (\"\" = none)")
    m = ms.add_parser("list", help="list your machines")
    m.add_argument("--json", action="store_true", help="print JSON")
    m = ms.add_parser("status", help="per machine: the projects using it, its recovery owner, pauses, open changes, "
                                     "heal checks and problems in 24 h (read only)")
    m.add_argument("alias", nargs="?", help="only this machine")
    m.add_argument("--json", action="store_true", help="print JSON")
    m = ms.add_parser("activity", help="who holds, waits for or runs on a machine now, and the locks and detached "
                                       "jobs logged on it, from all your projects here (read only)")
    m.add_argument("alias", help="the machine")
    m.add_argument("--since", help="window start: a delay back (30m, 6h, 2d) or an ISO time (home zone unless it has an offset; default 24h)")
    m.add_argument("--json", action="store_true", help="print JSON")
    m = ms.add_parser("remove", help="remove a machine")
    m.add_argument("alias", help="the machine")
    m = ms.add_parser("push", help="copy the list to the machines your --host projects run on, merged")
    m.add_argument("--host", help="only this machine")
    m.add_argument("--to", action="append", help=argparse.SUPPRESS)    # the background copy's hosts
    m.add_argument("--log", action="store_true", help=argparse.SUPPRESS)   # it ends its log with PUSH_DONE
    m = ms.add_parser("outage", help="a known outage (hardware repair, an external ticket): nobody is paged "
                                     "about the machine until it ends; the daily review lists it")
    m.add_argument("alias", nargs="?", help="the machine (none: list the known outages)")
    m.add_argument("why", nargs="?", help="why it is out, one line (none: show it)")
    m.add_argument("--owner", help="who has the repair: a person's role or a ticket, not a project")
    m.add_argument("--expires", help="when it ends: a delay (3d) or an ISO time, home zone unless it names one "
                                     "(required to record one)")
    m.add_argument("--by", help="the project recording or clearing it (default: the project you are in); "
                                "each project keeps its own entry per machine")
    m.add_argument("--covers", action="append",
                   help="what it explains, comma-separated: host, power, network, tray, chip, fabric or "
                        "a service's name (none: the whole machine). A heal check stays quiet only when "
                        "its name, resource or unit names one, never because of its output (host, power or "
                        "network: also while it cannot reach the box), so a check named after a covered "
                        "part stays quiet even when it fails for another reason; co-tenant watchers read "
                        "it from --json")
    m.add_argument("--json", action="store_true", help="list the known outages as JSON")
    m.add_argument("--clear", action="store_true", help="the outage is over: ends only this project's (or --by's) "
                                                         "entry; the machine stays out while another's is open")
    m = ms.add_parser("change", help="the ledger of live changes to shared machines, seen by every co-tenant")
    cs = m.add_subparsers(dest="change", required=True, help="what to do")
    c = cs.add_parser("add", help="record a live change: what, how to undo it and when it ends")
    c.add_argument("alias", help="the machine")
    c.add_argument("what", help="what changed and why, one line")
    c.add_argument("--undo", help="the command that undoes it (required, except for --power-cycle-at)")
    c.add_argument("--expires", help="when it ends: a delay (6h, 3d) or an ISO time (home zone unless it names one, "
                                     "e.g. 2026-10-26T15:30 or 2026-10-26T15:30-07:00); with --until-probe, at the latest")
    end = c.add_mutually_exclusive_group()
    end.add_argument("--until-probe", dest="until_probe",
                     help="a command that exits 0 once the change should be undone (checked every 10 min); it "
                          "must still fail when you record the change: add runs it once and refuses one that "
                          "passes already")
    end.add_argument("--until-task", dest="until_task", type=int, metavar="ID",
                     help="use it when a staged artifact (such as a build tree) must outlive its builder only "
                          "while this task of the project is open: it ends once that task is done, failed "
                          "or cancelled, at the latest after 7 days")
    end.add_argument("--power-cycle-at", dest="power_cycle_at", metavar="T",
                     help="a planned power cycle at T (a delay, or an ISO time in the home zone unless it "
                          "names one): running workers on the machine are "
                          "told to commit and hand off shortly before; the entry closes itself after")
    c.add_argument("--by", help="the project that owns the change (default: the project you are in)")
    c.add_argument("--expect", action="append", metavar="FINGERPRINT|CONDITION",
                   help="an alert the change is known to cause (an event fingerprint, or down/held on this "
                        "machine): it keeps the coordinator's turn routine while the change is open; repeatable")
    c.add_argument("--test", action="store_true",
                   help="the change is made for a test: `ttp ttr` labels this project's incidents on the machine "
                        "that start while it is open as tests, outside its TTR stats")
    c.add_argument("--auto-undo", dest="auto_undo", action="store_true",
                   help="this project's daemon runs the undo itself, unattended, once the end passes (the until-probe "
                        "passed twice, or --expires), and closes the entry on exit 0; for quick, idempotent "
                        "undos safe to run alone (remove a scratch dir, clear a hold), never a kill by name")
    c.add_argument("--allow-met-probe", dest="allow_met_probe", action="store_true",
                   help="record it even though its --until-probe passes already (the entry is overdue at once); "
                        "prints a warning")
    c.add_argument("--since", metavar="T",
                   help="backdate the change's start, for one already made: a delay back (2h) or an ISO time "
                        "(home zone unless it names one); not in the future, at most 7 days back")
    c = cs.add_parser("list", help="the open changes (--all: closed ones too, with who or which auto-close rule "
                                   "closed them: a no-op undo such as `none: <why>` closes itself once its end passes)")
    c.add_argument("alias", nargs="?", help="only this machine")
    c.add_argument("--all", action="store_true", help="closed ones too")
    c.add_argument("--json", action="store_true", help="print JSON")
    c = cs.add_parser("close", help="the change was undone")
    c.add_argument("id", type=int, help="the change's id, as `ttp machines change list` shows it")
    c.add_argument("--why", help="how it ended")
    c.add_argument("--by", help="who closes it (default: the project you are in)")
    c = cs.add_parser("edit", help="correct an open change in place (same id): its text, undo command or end; "
                                   "or flag one, open or closed, as a test")
    c.add_argument("id", type=int, help="the change's id, as `ttp machines change list` shows it")
    c.add_argument("--what", help="the new text: what changed and why, one line")
    c.add_argument("--undo", help="the new command that undoes it")
    c.add_argument("--expires", help="a new end: a delay (6h, 3d) or an ISO time (home zone unless it names "
                                     "one); with --until-probe, at the latest")
    end = c.add_mutually_exclusive_group()
    end.add_argument("--until-probe", dest="until_probe",
                     help="a new end: a command that exits 0 once the change should be undone; the same "
                          "one again is judged afresh; it must still fail now: edit runs it once and refuses one "
                          "that passes already")
    end.add_argument("--until-task", dest="until_task", type=int, metavar="ID",
                     help="a new end: once this open task ends; a continuation task gives its own id "
                          "to re-extend the entry (again 7 days at the latest)")
    flag = c.add_mutually_exclusive_group()
    flag.add_argument("--test", dest="test", action="store_true", default=None,
                      help="flag the change, open or closed, as made for a test after the fact: `ttp ttr` labels "
                           "the incidents inside its window as tests; only the project that recorded it or the "
                           "machine's recovery owner may")
    flag.add_argument("--no-test", dest="test", action="store_false",
                      help="clear its test flag: its incidents count in TTR again")
    auto = c.add_mutually_exclusive_group()
    auto.add_argument("--auto-undo", dest="auto_undo", action="store_true", default=None,
                      help="let this project's daemon run its undo once its end passes (as `change add --auto-undo`)")
    auto.add_argument("--no-auto-undo", dest="auto_undo", action="store_false",
                      help="page for its undo again instead")
    c.add_argument("--allow-met-probe", dest="allow_met_probe", action="store_true",
                   help="store the new --until-probe even though it passes already (the entry is overdue at once); "
                        "prints a warning")
    c.add_argument("--by", help="who edits it (default: the project you are in)")
    s.set_defaults(fn=cmd_machines)

    for name in ("pause", "resume"):
        s = sub.add_parser(name, help=f"{name} the project, or with --resource one shared resource")
        s.add_argument("name", help="the project")
        s.add_argument("--resource", help=f"{name} only this resource: tasks using it wait, `ttp lock` refuses it"
                       if name == "pause" else f"{name} only this resource")
        s.add_argument("--machine", metavar="ALIAS",
                       help="hold every resource on this machine (from `ttp machines list`) for all your projects on "
                            "this host: one record they all honour; ends like --resource"
                       if name == "pause" else "lift the machine-wide hold of this machine (one you set needs you, "
                                               "outside a run)")
        if name == "pause":
            s.add_argument("--reason", help="why, shown to workers, the coordinator and in status; required "
                                            "for a project pause from a run")
            s.add_argument("--until", help="with --resource: when it should end, a delay (2d) or an ISO time (home "
                                           "zone unless it names one), at "
                                           "most 7 days ahead; this or --end-when is required")
            s.add_argument("--end-when", dest="end_when", metavar="PROBE",
                           help="with --resource: a read-only shell probe that exits 0 once the pause can end")
            s.add_argument("--report-from", dest="report_from", metavar="PROJECT",
                           help="with --resource: the project whose report it waits on; its notes re-prompt the coordinator")
            s.add_argument("--until-reboot", dest="until_reboot", action="store_true",
                           help="the whole project until this host reboots: the daemon lifts it on its first tick "
                                "on a new boot")
        s.set_defaults(fn=cmd_pause)
    s = sub.add_parser("lease", help="the lock that keeps a project running on one machine at a time (failover): "
                                     "check where it is kept, or show this machine's view of it")
    s.add_argument("action", choices=["check", "show"],
                   help="check: whether the lease store answers; show: this machine's last view")
    s.add_argument("name", nargs="?", help="the project (default: the one here)")
    s.set_defaults(fn=cmd_lease)
    s = sub.add_parser("drain", help="pause the project and wait until its runs in flight end (they are not killed)")
    s.add_argument("name", help="the project")
    s.add_argument("--wait", type=float, default=0, metavar="S",
                   help="seconds to wait; exits 0 once no run or push is in flight, 1 at the timeout (default 0: check once)")
    s.add_argument("--jobs", action="store_true",
                   help="also wait for the project's `ttp detach` jobs running on this host (listed either way)")
    s.add_argument("--reboot", metavar="REASON",
                   help="record a planned reboot or power cycle of this host, about to happen: on the next boot "
                        "the detached jobs it ends do not count as the tasks' reboot losses. It also holds every "
                        "project on this host (no new worker runs, checks or pushes) and waits for their runs too")
    s.set_defaults(fn=cmd_drain)
    for name in ("start", "stop", "restart"):
        s = sub.add_parser(name, help=f"{name} the project's daemon service (running workers are kept)")
        s.add_argument("name", help="the project")
        if name == "stop":
            s.add_argument("--kill", action="store_true", help="also end running workers; their tasks resume on start")
        s.set_defaults(fn=cmd_service)

    s = sub.add_parser("config", help="read or set a config key (dotted); NAME defaults to the run's project; "
                                      "a section or no key prints the effective values, defaults included; with --account, the account-level "
                                      "value every project on this machine reads: ttp config --account KEY [VALUE]")
    s.add_argument("name", help="the project (inside a run: may be left out)")
    s.add_argument("key", nargs="?", help="the dotted key or section (none: every value)")
    s.add_argument("value", nargs="?", help="the new value (none: print the current one)")
    s.add_argument("--account", action="store_true",
                   help="the account-level setting (" + ", ".join(
                       f"{sec}.{k}" for sec, ks in ACCOUNT_KEYS.items() for k in sorted(ks)) + "); '' removes it")
    s.add_argument("--chat-settable", action="store_true",
                   help="check, without setting it, whether a coordinator config_set accepts KEY (and VALUE): "
                        "ttp config --chat-settable KEY [VALUE]; KEY `list` prints every accepted key")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("secret", help="store per-user credentials (read from stdin)")
    s.add_argument("kind", choices=SECRET_KINDS,
                   help="jev: a key for Jev, the optional add-on that sends log and alert text to an "
                        "outside paid service; slack: a Slack bot token; "
                        "endpoint <alias> --url <base url>: an API key for an OpenAI-compatible endpoint, "
                        "checked with GET <base url>/models and saved with its URL as endpoint/<alias>; "
                        "show: list what is saved (never the keys); check [alias]: test saved endpoint keys "
                        "(reachable, auth ok or auth failed; never the key); env <alias> -- <command>: run "
                        "a command with the key in OPENAI_API_KEY and the URL in OPENAI_BASE_URL, its output "
                        "masked; env --secret <name>[:<key>]=<VAR> -- <command>: the same with a secret of your own "
                        "in VAR; push: copy your saved keys to --host; set <name>: a value of your own, "
                        "read from stdin; generate <name> --keys a,b,c: a fresh random token per key, "
                        "stored as a JSON object. No command prints a saved key. "
                        "Keys go to secrets.json in your ttp home (~/.tt-project, mode 0600)")
    s.add_argument("alias", nargs="?", help="with endpoint: a name for it (letters, digits, '-' and '_'); "
                                            "with check or env: the saved endpoint; with set or generate: "
                                            "the secret's name (lowercase letters, digits, '-' and '_')")
    s.add_argument("command", nargs="*", help="with env: -- then the command to run")
    s.add_argument("--host", help="with push: the machine to copy your saved keys to")
    s.add_argument("--via", default="typesafe", choices=["typesafe", "openrouter"],
                   help="with jev: the service the key is for (default typesafe)")
    s.add_argument("--url", help="with jev: the service's address, if not its usual one; with endpoint "
                                 "(required): its base URL, e.g. https://api.example.com/v1")
    s.add_argument("--keys", help="with generate: the key names, comma-separated")
    s.add_argument("--email", help="with slack: your Slack email, to find your user")
    s.add_argument("--user-id", help="with slack: your Slack user id (instead of --email)")
    s.add_argument("--secret", action="append", metavar="NAME[:KEY]=VAR",
                   help="with env (repeatable): put a secret made with set or generate (one key of it with "
                        ":KEY) in the command's environment as VAR; the command line carries only names")
    s.add_argument("--cmd-word", action="append", help=argparse.SUPPRESS)
    s.add_argument("--force", action="store_true",
                   help="with jev or endpoint: save the key even if the check fails; with generate: "
                        "replace an existing secret")
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser("setup", help="install ttp for this user (stable copy + shim on PATH)")
    s.add_argument("--bin-dir", default="~/.local/bin", help="where the ttp command goes (default ~/.local/bin)")
    s.add_argument("--force", action="store_true", help="install even over a newer installed ttp")
    s.add_argument("--ignore-cycle-notice", action="store_true",
                   help="go ahead although a device broker announced a power cycle of this host (a warning is printed)")
    s.add_argument("--no-shortcut", action="store_true",
                   help="remove (and do not install) the plain /tt-project command for Claude Code")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("uninstall", help="remove ttp for this user on this machine: stops and removes its services, "
                                         "its own crontab lines, the /tt-project shortcut, the ttp command and the "
                                         "installed lib; keeps project folders and linger, and prints what it left")
    s.add_argument("--bin-dir", default="~/.local/bin", help="where setup put the ttp command (default ~/.local/bin)")
    s.add_argument("--dry-run", action="store_true", help="list every step and change nothing")
    s.add_argument("--yes", action="store_true", help="do not ask before removing")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("upgrade", help="merge the installed tt-project template into a project's harness")
    s.add_argument("name", help="the project")
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)          # the daemon's own upgrade
    s.add_argument("--apply", metavar="COMMIT",
                   help="an upgrade task's last step: fast-forward main to its checked merge and restart")
    s.add_argument("--project-dir", help=argparse.SUPPRESS)
    s.add_argument("--ignore-cycle-notice", action="store_true",
                   help="go ahead although a device broker announced a power cycle of this host (a warning is printed)")
    s.add_argument("--take-over-from", metavar="TASK", type=int,
                   help="host-side takeover: harness task TASK missed its takeover deadline, so this upgrade "
                        "takes the template merge over from it even while it still shows activity")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("alerts", help="list broadcast alerts or acknowledge a keyed alert")
    s.add_argument("name", help="the project")
    s.add_argument("--after", type=int, default=0, help="only alerts after this message id (default 0: all)")
    s.add_argument("--floor", default="high", help="the lowest severity listed: low, normal, high or critical "
                                                   "(default high)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.add_argument("--health", action="store_true", help="with --json: {alerts, health: {heartbeat_age_s, service, "
                   "paused}}, the heartbeat's age taken on this host (the desktop notifier's daemon-down check)")
    s.add_argument("--ack", type=int, metavar="ID", help="acknowledge the keyed alert of this message id: it closes, or moves to the feed while it holds")
    s.add_argument("--why", help="reason for acknowledging the alert (required with --ack)")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("consent", help="the user's one answer per question asked once (desktop notifier, local "
                       "view tunnels): show it, or record it")
    s.add_argument("topic", nargs="?", choices=["notifier", "tunnel"], help="the question (none: show every answer)")
    s.add_argument("answer", nargs="?", choices=["yes", "no"], help="record this answer (none: show it)")
    s.set_defaults(fn=cmd_consent)

    s = sub.add_parser("notifier", help="desktop notifications on this machine for all your projects")
    s.add_argument("action", choices=["install", "restart", "run", "test", "uninstall"],
                   help="install: start it at login and send a test notification; restart: on the installed code; "
                        "uninstall: stop and remove it (setup and upgrade then leave it off); "
                        "run: run it here; test: send one test notification and say whether it got through "
                        "(exit 1 when not) and how to fix it")
    s.add_argument("--slack", action="store_true",
                   help="with test: send one test message to your Slack DM instead (optional; set up with `ttp secret slack`)")
    s.set_defaults(fn=cmd_notifier)

    s = sub.add_parser("daemon")   # the service runs it; no help, so --help does not list it
    s.add_argument("dir", help="the project's tt-project folder")
    s.set_defaults(fn=cmd_daemon)

    a = ap.parse_args(_project_name_argv(sub, _say_finding_argv(_say_all_argv(_secret_value_argv(_secret_env_argv(
        sys.argv[1:] if argv is None else list(argv)))))))
    dry_run_stub(a)
    try:
        _recover_setup()     # a `ttp setup` a reboot cut short: finished or rolled back before anything else
    except Exception as e:
        print(f"ttp: could not recover an interrupted `ttp setup`: {e}", file=sys.stderr)
    a.fn(a)


def dry_run_stub(a) -> None:
    """Under an on_pass_cmd's dry run (onpassdry.ENV), `ttp note`, `notify`, `push` and `land` do nothing
    and exit 0, and what would start work or touch a device (`ttp lock`, `devq submit|start|clear`,
    `detach` without --check) is refused (exit 75, as a busy resource), so nothing leaves the run and no
    device or shared machine is touched."""
    from .onpassdry import ENV
    cmd = a.cmd
    if os.environ.get(ENV) != "1":
        return
    if cmd == "lock" or (cmd == "devq" and a.op in ("submit", "start", "clear")) \
            or (cmd == "detach" and a.check is None):
        print(f"ttp {cmd}: refused in an on_pass_cmd dry run ({ENV}=1)", file=sys.stderr)
        sys.exit(75)
    if cmd in ("note", "notify", "push", "land"):
        print(f"ttp {cmd}: skipped in an on_pass_cmd dry run ({ENV}=1)", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
