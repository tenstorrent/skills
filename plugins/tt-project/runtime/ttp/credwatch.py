# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A tripwire on the account's credential files: a run that changed one is named at once.

Runs are unsandboxed, so a tool call can write any file in HOME, a login file too (a check that fed a
patch for ~/.config/gh/ to a hook on the live HOME overwrote gh's hosts file and logged gh out on the
host). The runner (runner.supervise) takes snapshot() as an agent starts and again once it ended, and
writes changes() of the two to the run's exit.json; the daemon (observe) then queues one observation
per changed path for the coordinator, naming the run: normal when this machine's change ledger records
the change as made while the run ran, else high with the run's transcript line that shows it, or the
file's time when none does. A change the ledger does not record fails the run's hand-off, unless
classify() finds the run could not have been told from others: a change of a store's ctime alone
(metadata), or one made while another run of any project on the host was open too (shared). Those are
told once, at normal severity unless a store is new, replaced or resized, and fail no run. Only stat is
read (size, mtime, ctime, inode), never a file's contents: no key or token is opened or hashed, and
ctime still tells a same-size rewrite whose mtime was put back.

Two kinds of file (files()):
- stores: gh's hosts file, git's and netrc's credential files, the ssh private keys, and every file
  under the write guard's protected paths (guarded_paths: pathguard.paths(), the folders walked). Nothing
  a run does rewrites them: created, removed or any change of size, mtime, ctime or inode counts. Files
  their own tools rewrite in normal use are left out (CHURN: ssh's known_hosts, gh's state file, caches).
- logins: the providers' own login files (Provider.credential_files). Their CLIs rewrite them on every
  token refresh, so only a removal, or a rewrite to under half the size it had, counts.

No automatic restore. Without reading contents the harness cannot tell damage from a real change (a
new `gh auth login`, a refreshed token, a rotated key, made by the user or by another run at the same
time); a restore of the start's copy would undo those, and a copy kept beside the file during the run
is one more readable credential while an unsandboxed agent runs. The observation says what to
re-login instead.

store_paths() is the one list of credential stores here, for a credential write guard to adopt.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

# ~/.ssh files that hold no secret
SSH_PUBLIC = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "rc", "environment"}
SHRINK = 0.5   # a login file rewritten below this share of its size counts as damaged
CHURN_NAMES = ("known_hosts", "state.yml")   # file names (prefixes) their tools rewrite in normal use
CHURN_DIRS = {"cache", "http-cache"}          # folders of cached responses (aws sso and cli, kube)
MAX_FILES = 2000                             # files walked per protected folder
SNAP_SLACK_S = 5   # a run counts as having seen a change only from this long after its start (its first snapshot)
VERDICTS = "credwatch.json"   # in the run dir: the daemon's verdict per change (classify), read at the hand-off


def ssh_private(name: str) -> bool:
    """A file name in ~/.ssh that may hold a private key."""
    return not (name.endswith(".pub") or name in SSH_PUBLIC or name.startswith(("known_hosts", "cm-")) or "%" in name)


def store_paths(home: str | Path | None = None, env=None) -> list[str]:
    """The credential stores of the account whose home is `home` (missing ones too, and the ~/.ssh
    files that exist and may be private keys)."""
    env = os.environ if env is None else env
    home = Path(home or env.get("HOME") or os.path.expanduser("~"))
    gh = Path(env.get("GH_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "gh")
    out = [gh / "hosts.yml", home / ".git-credentials", home / ".netrc"]
    try:
        out += sorted(p for p in (home / ".ssh").iterdir() if ssh_private(p.name) and p.is_file())
    except OSError:
        pass
    return [str(p) for p in out]



def guarded_paths(env=None, cfg: dict | None = None) -> list[str]:
    """The write guard's protected paths (pathguard.paths(): defaults, the account's and the project's
    guards.protected_paths) for HOME and the account's own home: each regular file under them (lstat, no
    symlink followed, CHURN left out, MAX_FILES per folder), and each missing one itself, so a run that
    creates it is caught too."""
    from . import pathguard
    env = os.environ if env is None else env
    try:
        listed = pathguard.paths(cfg)
    except Exception:   # unreadable settings: the defaults still count
        listed = list(pathguard.DEFAULT_PATHS)
    out: list[str] = []
    for home in pathguard._homes(env):
        for top in dict.fromkeys(pathguard.expand(p, env, "/", home, real=False) for p in listed):
            if top in ("/", "") or not os.path.isabs(top):
                continue
            if not os.path.lexists(top):
                out.append(top)
            elif os.path.isfile(top) and not os.path.islink(top):
                out.append(top)
            else:
                out += _walk(top)
    return list(dict.fromkeys(out))


def _walk(top: str) -> list[str]:
    out: list[str] = []
    for root, dirs, names in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d not in CHURN_DIRS)
        for name in sorted(names):
            if name.startswith(CHURN_NAMES):
                continue
            path = os.path.join(root, name)
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    out.append(path)
            except OSError:
                continue
            if len(out) >= MAX_FILES:
                return out
    return out


def login_paths() -> list[str]:
    """Every provider's login files, as the providers name them."""
    from .providers import all_providers
    out: list[str] = []
    for prov in all_providers():
        try:
            out += [f for f in prov.credential_files() if f not in out]
        except Exception:
            continue
    return out


def files() -> dict[str, str]:
    """{path: "store" | "login"} of this account."""
    out = {p: "store" for p in guarded_paths()}
    out.update({p: "login" for p in login_paths()})
    out.update({p: "store" for p in store_paths()})
    return out


def mark(path: str) -> list[int] | None:
    """[size, mtime_ns, inode, ctime_ns] of `path`, None when it is missing (stat only, never contents)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns]


def snapshot(watched: dict[str, str] | None = None) -> dict:
    """{"kinds": {path: kind}, "marks": {path: mark}} of the watched files."""
    watched = files() if watched is None else watched
    return {"kinds": dict(watched), "marks": {p: mark(p) for p in watched}}


def changes(before: dict, after: dict) -> list[list[str]]:
    """[[path, how], ...]: what changed between two snapshots (how: created, removed, changed, shrunk, and
    metadata when only a store's ctime moved, its size, mtime and inode the same).
    A file only `after` lists (a new ssh key) counts as created when it is a store."""
    out = []
    was, now, kinds = before.get("marks") or {}, after.get("marks") or {}, {**after.get("kinds", {}), **before.get("kinds", {})}
    for path in sorted(set(was) | set(now)):
        a, b = was.get(path), now.get(path)
        if a == b or a and b and a[:len(b)] == b[:len(a)]:   # a mark from an older runtime: its fields only
            continue
        login = kinds.get(path) == "login"
        if b is None:
            out.append([path, "removed"])
        elif a is None:
            if not login:
                out.append([path, "created"])
        elif not login and a[:3] == b[:3]:
            out.append([path, "metadata"])   # only ctime moved (chmod, chown, a link): contents untouched
        elif not login:
            out.append([path, "changed"])
        elif b[0] < a[0] * SHRINK:
            out.append([path, "shrunk"])
    return out


LEDGER_SLACK_S = 120   # a ledger entry this long before the run started still counts as made while it ran
QUOTE = 200            # characters of a transcript line quoted in the observation
# ssh-keygen writing to a scratch file (-f under /tmp or a mktemp/TMP variable), or only reading a key or
# editing known_hosts (-l, -y, -F, -R, -B): it does not write the account's key
SSH_SCRATCH = re.compile(r"""-f\s*\\?['"]?(?:/tmp/|/var/tmp/|\$\{?(?:TMP|tmp|TEMP|H\b|scratch)|\$\(mktemp)""")
SSH_READ = re.compile(r"\s-[A-Za-z]*[lyFRB]\b")
COMMAND_END = re.compile(r"\\n|;|&&|\|")


def _forms(path: str, home: str) -> list[str]:
    """The ways a command or a ledger entry writes `path`: as is, and from ~ or $HOME."""
    out, home = [path], home.rstrip("/")
    if home and path.startswith(home + "/"):
        rel = path[len(home) + 1:]
        out += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
    return out


def _names(text: str, path: str, home: str) -> re.Match | None:
    """Where `text` names `path` itself (not a longer name it begins, such as its .pub)."""
    alt = "|".join(re.escape(f) for f in _forms(path, home))
    return re.search(rf"(?:{alt})(?![\w-]|\.\w)", text)


def _writer(line: str, path: str) -> re.Match | None:
    """A command in `line` that writes a store like `path` without naming it: ssh-keygen with no scratch
    -f for an ssh key, gh auth for gh's hosts file, git credential for git's or netrc's."""
    if "/.ssh/" in path:
        for m in re.finditer(r"\bssh-keygen\b", line):
            cmd = COMMAND_END.split(line[m.end():m.end() + 300], 1)[0]
            if not SSH_SCRATCH.search(cmd) and not SSH_READ.search(cmd):
                return m
        return None
    if path.endswith("hosts.yml"):
        return re.search(r"\bgh\s+auth\s+(?:login|logout|refresh|switch|setup-git)\b", line)
    if path.endswith((".git-credentials", ".netrc")):
        return re.search(r"\bgit\s+credential\b|credential\.helper", line)
    return None


def ledger_entry(path: str, start: float | None, now: float, home: str = "") -> dict | None:
    """The newest entry, open or closed and of any project, of this machine's change ledger (the one
    `ttp machines change list --all` reads) made while the run ran (from `start`, less LEDGER_SLACK_S,
    to `now`) whose text names `path`; None without one, or when the machines list has no entry for
    this machine."""
    from . import machine_ledger, machines
    me = machines.here()
    if not me:
        return None
    lo = (start or now) - LEDGER_SLACK_S
    hits = [c for c in machine_ledger.changes([me[0]], open_only=False)
            if any(lo <= float(c.get(k) or 0) <= now for k in ("added", "since"))
            and _names(str(c.get("what") or ""), path, home)]
    return max(hits, key=lambda c: float(c.get("added") or 0), default=None)


def unrecorded(changed: list, start: float | None, now: float, home: str = "") -> list:
    """The changes of `changed` (changes() rows) the machine's change ledger does not record as made while
    the run ran (ledger_entry): those fail its hand-off. An unreadable ledger records nothing."""
    home = home or os.environ.get("HOME") or os.path.expanduser("~")
    out = []
    for c in changed:
        try:
            if ledger_entry(c[0], start, now, home):
                continue
        except Exception:
            pass
        out.append(c)
    return out


def transcript_hit(run_dir: str | Path | None, path: str, home: str = "") -> tuple[int, str] | None:
    """(line number, quote) of the first line of the run's transcript (output.jsonl) that names `path`
    or runs a command that writes such a store (_writer); None when none does."""
    if not run_dir:
        return None
    try:
        fh = open(Path(run_dir) / "output.jsonl", errors="replace")
    except OSError:
        return None
    with fh:
        for n, line in enumerate(fh, 1):
            if m := _names(line, path, home) or _writer(line, path):
                a = max(0, m.start() - QUOTE // 2)
                return n, " ".join(line[a:a + QUOTE].split())
    return None


def runs_at(t: float, dbs: list, own: tuple[str, int] | None = None) -> int:
    """How many runs, of every project database in `dbs` (paths, read only), were open at `t` and had
    already taken their first snapshot (SNAP_SLACK_S after their start), so saw a change made then.
    `own` (database path, run id) is left out. An unreadable database counts none."""
    import sqlite3
    from .globalcap import connect_ro
    n = 0
    for path in dbs:
        try:
            conn = connect_ro(Path(path), timeout=2)
            try:
                rows = conn.execute("SELECT id FROM runs WHERE started<=? AND (ended IS NULL OR ended>=?)",
                                    (t - SNAP_SLACK_S, t)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        mine = own and Path(path).resolve() == Path(own[0]).resolve()
        n += sum(1 for (rid,) in rows if not (mine and rid == own[1]))
    return n


def classify(changed: list, marks: dict, start: float | None, end: float, dbs: list,
             own: tuple[str, int] | None = None) -> dict:
    """{path: [verdict, change time ns]} for the changes of `changed` that do not fail the run; the rest
    (removed, shrunk, any change without the runner's marks) fail it as before. `marks` is the runner's
    {path: [before, after]} (exit.json cred_marks). Verdicts:
    - metadata: only ctime moved (changes()): nothing was written, told at normal severity.
    - shared: the change's time (the file's new ctime) lies inside this run's window and also inside
      the window of another run open then, of any project on the host (runs_at): the runs that saw it
      cannot tell which, if any, made it. Told once as changed outside these runs: at normal severity
      when rewritten in place at the same size, else high (shared_alarm: a store created, replaced by a
      new inode, or rewritten to another size, as a lost or swapped key or login would be).
    A change only this run was open for keeps failing it."""
    out = {}
    for path, how in changed:
        pair = marks.get(path) if isinstance(marks, dict) else None
        if how == "metadata":
            out[path] = ["metadata", (pair[1][3] if pair and pair[1] and len(pair[1]) > 3 else int(end * 1e9))]
            continue
        if how not in ("changed", "created") or not pair or not pair[1] or len(pair[1]) < 4:
            continue
        before, after = pair
        t = after[3] / 1e9
        if not (start is not None and start <= t <= end) or runs_at(t, dbs, own) < 1:
            continue
        same = before and before[0] == after[0] and before[2] == after[2]
        out[path] = ["shared" if same else "shared_alarm", after[3]]
    return out


def held(run_dir: str | Path) -> dict:
    """The verdicts classify() stored in the run dir; {} without them (every change fails the run)."""
    import json
    try:
        got = json.loads((Path(run_dir) / VERDICTS).read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def observe(db, run: dict, changed: list, now: float, run_dir: str | Path | None = None, where=None,
            verdicts: dict | None = None) -> int:
    """One observation per changed path of `run` (a runs row): the run, the path and where to start.
    A change the machine's change ledger records as made while the run ran (ledger_entry) is a known,
    deliberate one: told at normal severity naming the entry, its project and task, and no urgent work.
    One classify() let stand (`verdicts`: metadata only, or seen by several overlapping runs) is told
    once per change (_observe_seen). Any other is high, quoting the run's transcript line that names the
    path or runs a command that writes it (transcript_hit) or, with none, saying so and giving the file's
    time (in `where`'s zone), so the search starts at the right time. A change several overlapping runs
    saw is told once (the fingerprint holds the path and when it was seen to change, to the minute).
    Returns how many it queued."""
    run, told = dict(run), 0
    home = os.environ.get("HOME") or os.path.expanduser("~")
    for path, how in changed:
        fp = f"credwatch:{path}:{int(now // 60)}"
        who = f"run {run.get('id')}" + (f" of task #{run['task']}" if run.get("task") else f" ({run.get('role')})")
        try:
            entry = ledger_entry(path, run.get("started"), now, home)
        except Exception:   # an unreadable ledger never hides the alarm
            entry = None
        if not entry and (verdict := (verdicts or {}).get(path)):
            told += _observe_seen(db, path, how, who, verdict, now)
            continue
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            continue
        if entry:
            by = f"project {entry.get('by') or '?'}" + (f" task #{entry['task']}" if entry.get("task") else "")
            sev, text = "normal", (
                f"Credential file {how} during {who}: {path}. The machine's change ledger records it as a "
                f"deliberate change: #{entry.get('id')} by {by}: {str(entry.get('what') or '')[:300]}. Not this "
                f"run's doing: no urgent work. If a login stops working, take it up with that project. Nothing "
                f"was restored.")
        else:
            if hit := transcript_hit(run_dir, path, home):
                seen = f"This run's transcript has it (output.jsonl line {hit[0]}: {hit[1]}): start there"
            else:
                seen = "Not seen in this run's commands; another process on the host may have done it"
                try:
                    from . import timefmt
                    seen += f" (the file's time: {timefmt.long(os.stat(path).st_mtime, where)}): start there"
                except OSError:
                    pass
            sev, text = "high", (
                f"Credential file {how} during {who}: {path}. A run, or something else on the host while it "
                f"ran, wrote the account's credential file (only its size, time and inode were compared). "
                f"{seen}, and stop it recurring; if the login no longer works, ask the user to log in again "
                f"(for gh: `gh auth login`). Nothing was restored.")
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", fp, sev, text, "queued"))
        told += 1
    return told


def _observe_seen(db, path: str, how: str, who: str, seen: list, now: float) -> int:
    """observe() for a change classify() let stand: once per change (its path and new ctime), whichever
    of the runs that saw it ends first."""
    kind, at = seen[0], seen[1]
    fp = f"credwatch:{path}:at:{at}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return 0
    if kind == "metadata":
        sev, text = "normal", (f"Credential file attributes changed during {who}: {path} (only its ctime moved: "
                               f"mode, owner or links; size, mtime and inode the same). No run failed for it.")
    else:
        sev = "high" if kind == "shared_alarm" else "normal"
        text = (f"Credential file {how} outside these runs: {path}, seen by {who} and other runs open at the "
                f"time, so none is tied to it and no run failed for it. "
                + ("The file is new, replaced or of another size: if a login no longer works, ask the user to "
                   "log in again (for gh: `gh auth login`). "
                   if sev == "high" else "") + "Nothing was restored.")
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "daemon", "observation", fp, sev, text, "queued"))
    return 1
