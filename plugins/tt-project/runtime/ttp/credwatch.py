# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A tripwire on the account's credential files: a run that changed one is named at once.

Runs are unsandboxed, so a tool call can write any file in HOME, a login file too (a check that fed a
patch for ~/.config/gh/ to a hook on the live HOME overwrote gh's hosts file and logged gh out on the
host). The runner (runner.supervise) takes snapshot() as an agent starts and again once it ended, and
writes changes() of the two to the run's exit.json; the daemon then decides each change (classify) and
queues observations for the coordinator (observe). A run's hand-off is failed only for a change its own
transcript shows: a command or file edit the run issued that writes the path (a redirect, tee, cp or mv
destination, rm, chmod, sed -i, ssh-keygen -f, an editor tool's file) or a command that writes such a store
(gh auth login, ssh-keygen without a scratch -f, git credential approve); tool output, text and reads such
as ssh -i or cat never count. Not failed, and told once for the whole host (one claim file per
path and change minute in the account's tt-project home, whichever run of any project ends first): a
change of a store's ctime alone (metadata), one this machine's change ledger records as made while the
run ran, and one no transcript shows (the account's user, another run or process). Those are normal
unless a store is new, removed, replaced or resized, and the text says how to record a deliberate change
in the ledger so later runs are not blamed. Accepted gap: a write a run makes through a script whose
command line does not name the path is told, not failed. Only stat is read (size, mtime, ctime, inode),
never a file's contents: no key or token is opened or hashed, and ctime still tells a same-size rewrite
whose mtime was put back.

Two kinds of file (files()):
- stores: gh's hosts file, git's and netrc's credential files, the ssh private keys, and every file
  under the write guard's protected paths (guarded_paths: pathguard.paths(), the folders walked). Nothing
  a run does rewrites them: created, removed or any change of size, mtime, ctime or inode counts. Files
  their own tools rewrite in normal use are left out (CHURN: ssh's host key lists, any name holding
  known_hosts, gh's state file, caches).
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

import hashlib
import json
import os
import re
import stat
from pathlib import Path

# ~/.ssh files that hold no secret
SSH_PUBLIC = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "rc", "environment"}
SHRINK = 0.5   # a login file rewritten below this share of its size counts as damaged
CHURN_NAMES = ("known_hosts", "state.yml")   # file names (prefixes) their tools rewrite in normal use
HOST_LIST = "known_hosts"   # in a file name: ssh's host key lists (known_hosts_<site>, <site>_known_hosts)
                            # hold public host keys that ssh and other sessions add to
CHURN_DIRS = {"cache", "http-cache"}          # folders of cached responses (aws sso and cli, kube)
MAX_FILES = 2000                             # files walked per protected folder
TOLD_DIR = "credwatch-told"   # in the account's tt-project home: one claim file per change told for the host
TOLD_KEEP_S = 86400           # claim files older than this are removed
VERDICTS = "credwatch.json"   # in the run dir: the daemon's verdict per change (classify), read at the hand-off


def ssh_config(name: str) -> bool:
    """A file name in ~/.ssh of ssh's client config (config, <site>_config) or a backup or temp copy of
    one (config.bak-*, config.tmp.*, config~, <site>_config.bak-*): settings, not a credential."""
    base = name.split(".", 1)[0].rstrip("~")
    if name.endswith((".key", ".pem")):   # a key file, whatever its stem
        return False
    return base == "config" or base.endswith("_config") or name.startswith(("config-", "config~"))


def ssh_private(name: str) -> bool:
    """A file name in ~/.ssh that may hold a private key."""
    return not (name.endswith(".pub") or name in SSH_PUBLIC or HOST_LIST in name or name.startswith("cm-") or "%" in name
                or ssh_config(name))


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
            if name.startswith(CHURN_NAMES) or HOST_LIST in name or os.path.basename(root) == ".ssh" and ssh_config(name):
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
SSH_SCRATCH = re.compile(r"""(?:/tmp/|/var/tmp/|\$\{?(?:TMP|tmp|TEMP|H\b|scratch)|\$\(mktemp|\$$)""")
SSH_READ = re.compile(r"\s-[A-Za-z]*[lyFRB]\b")
GH_AUTH_WRITE = {"login", "logout", "refresh", "switch", "setup-git"}
GIT_CRED_WRITE = {"approve", "store", "erase"}
GIT_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin"}
WRITE_ANY = {"rm", "unlink", "shred", "truncate", "chmod", "chown", "touch", "mv", "tee"}   # any operand is written
WRITE_DEST = {"cp", "install", "ln", "rsync", "scp"}                                        # the last operand is
FAILS = "run"   # the one verdict that fails the run's hand-off (classify)


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


def _is(word: str, path: str, home: str) -> bool:
    w = word.strip("'\"")
    return w in _forms(path, home) or (w.startswith("/") and os.path.normpath(w) == path)


def _commands(cmd: str, depth: int = 0) -> list[tuple[list[str], list[str]]]:
    """(words, output redirect targets) of each simple command the shell command `cmd` runs, without its
    leading assignments and wrappers (sudo, env, nohup ...), and of the strings it hands to sh -c or eval.
    Quoted arguments, comments and heredoc bodies no shell reads are words of their command or nothing
    (hook._split), never commands: a writer named in a grep pattern, a commit message or pytest -k is none.
    A quote spanning lines stays one word; a comment ends at its line, and a '#' inside a word is no comment."""
    from . import hook
    run = hook._split(cmd, strip=False)[0]
    flags = hook._quoting(run)
    kept, comment, last = [], False, 0
    for m in re.finditer("[#\n]", run):   # drop '#' comments (a '#' starting an unquoted word) up to their newline
        i, c = m.start(), m[0]
        if not comment:
            kept.append(run[last:i])
        if c == "\n" and not flags[i][0]:
            comment = False
        elif c == "#" and flags[i][0] and (i == 0 or (not flags[i - 1][0] and run[i - 1] in " \t\n;&|(")):
            comment = True
        if not comment:
            kept.append(c)
        last = i + 1
    run = "".join(kept + ([] if comment else [run[last:]]))
    flags = hook._quoting(run)
    run = re.sub("\n", lambda m: "\n" if flags[m.start()][0] else ";", run)
    try:
        words = hook._words(run, ";&|()<>", commenters="")
    except ValueError:
        words = run.split()
    out, argv, targets, redirect = [], [], [], ""
    for w in words + [";"]:
        if redirect and not set(w) <= set(";&|()"):   # a redirect's file: written after >, read after <
            if ">" in redirect:
                targets.append(w)
            redirect = ""
        elif w and set(w) <= set("&<>") and set(w) & set("<>"):
            redirect = w
        elif w and set(w) <= set(";&|()"):
            argv = hook._strip_prefix(argv)
            if argv or targets:
                out.append((argv, targets))
            name = os.path.basename(argv[0]) if argv else ""
            inner = (next((b for a, b in zip(argv[1:], argv[2:]) if re.fullmatch(r"-[a-z]*c[a-z]*", a)), None)
                     if name in hook.SHELLS else " ".join(argv[1:]) if name == "eval" else None)
            if inner and depth < 3:
                out += _commands(inner, depth + 1)
            argv, targets, redirect = [], [], ""
        else:
            argv.append(w)
    return out


def _writes(argv: list[str], targets: list[str], path: str, home: str) -> bool:
    """The simple command `argv` (with its output redirect `targets`) writes `path` itself: a redirect or
    tee target, the destination of cp, install, ln, rsync or scp, an operand of mv, rm, chmod, chown, touch
    or truncate, sed -i or perl -i, dd of= or ssh-keygen -f (but -l, -y and other reads); not when it only
    reads it (ssh -i, cat, grep)."""
    if any(_is(t, path, home) for t in targets):
        return True
    if not argv:
        return False
    verb, args = os.path.basename(argv[0]), argv[1:]
    ops = [a for a in args if not a.startswith("-")]
    return bool(verb in WRITE_ANY and any(_is(a, path, home) for a in ops)
                or verb in WRITE_DEST and (ops and _is(ops[-1], path, home)
                                           or any(a == "-t" and _is(b, path, home) for a, b in zip(args, args[1:])))
                or verb in ("sed", "perl") and any(re.match(r"-\w*i|--in-place", a) for a in args)
                and any(_is(a, path, home) for a in ops)
                or verb == "dd" and any(a.startswith("of=") and _is(a[3:], path, home) for a in args)
                or verb == "ssh-keygen" and not SSH_READ.search(" " + " ".join(args))
                and any(a == "-f" and _is(b, path, home) for a, b in zip(args, args[1:])))


def _writer(argv: list[str], path: str) -> bool:
    """The simple command `argv` writes a store like `path` without naming it: ssh-keygen making a key
    with a -f naming the file's basename, or no -f for a default id_* key; gh auth login, logout, refresh, switch or setup-git for gh's hosts
    file; git credential approve, store or erase, or git config setting credential.helper, for git's or
    netrc's. Only the command's own verb and arguments count, never a writer named inside an argument."""
    if not argv:
        return False
    verb, args = os.path.basename(argv[0]), argv[1:]
    ops = [a for a in args if not a.startswith("-")]
    if "/.ssh/" in path:
        if verb != "ssh-keygen" or SSH_READ.search(" " + " ".join(args)):
            return False
        dest = (next((b for a, b in zip(args, args[1:]) if a == "-f"), None)
                or next((a[2:] for a in args if a.startswith("-f") and len(a) > 2), None))
        name = os.path.basename(path).removesuffix(".pub")
        if dest:   # a key made elsewhere or under another name is not this file's change
            return not SSH_SCRATCH.match(dest) and os.path.basename(dest).removesuffix(".pub") == name
        return name.startswith("id_")   # no -f: ssh-keygen asks for the default id_* key
    if path.endswith("hosts.yml"):
        return verb == "gh" and len(ops) > 1 and ops[0] == "auth" and ops[1] in GH_AUTH_WRITE
    if path.endswith((".git-credentials", ".netrc")) and verb == "git":
        i = 0
        while i < len(args) and args[i].startswith("-"):   # git's own options: -C <dir>, -c <k=v>, --no-pager
            i += 2 if args[i] in ("-C", "-c") else 1
        sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
        if sub in ("credential", "credential-store"):
            return bool(rest) and rest[0] in GIT_CRED_WRITE
        if sub == "config" and not GIT_CONFIG_READ & set(rest):
            vals = [a for a in rest if not a.startswith("-")]
            return any(v == "credential.helper" and k + 1 < len(vals) for k, v in enumerate(vals))
    return False


def issued(run_dir: str | Path) -> list[tuple[int, str, str]]:
    """(line number, 'shell' or 'file', command or path) of each command and file edit the run itself
    issued, in any shipped provider's transcript (output.jsonl): tool calls only, never tool output or
    text, never a forked run's inherited ones (before its task prompt), never a call a guard refused
    before it ran."""
    from . import sidefx
    return [(it["line"], it["kind"], it["what"]) for it in sidefx.items(Path(run_dir), own_only=True)
            if it["status"] != "refused"]


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


_HITS: dict = {}   # transcript_hit's answers by (run dir, path, home, the transcript's and refusal log's stamps)
HITS_KEPT = 512    # answers kept; the oldest go first
# A word each writer's command holds (_writer), looked for as bytes before any line is parsed
WRITER_WORDS = ((lambda p: "/.ssh/" in p, b"ssh-keygen"), (lambda p: p.endswith("hosts.yml"), b"auth"),
                (lambda p: p.endswith((".git-credentials", ".netrc")), b"credential"))


def _needles(path: str) -> list[bytes]:
    """Bytes one of which a transcript holds wherever a call could name `path` (_is) or run a writer for
    it (_writer): its file name (as is and JSON-escaped; every form of the path ends in it) and its
    writer's command word."""
    name = os.path.basename(path.rstrip("/")) or path
    out = {name.encode(), json.dumps(name)[1:-1].encode()}
    out.update(w for has, w in WRITER_WORDS if has(path))
    return sorted(out)


def _stamp(f: Path) -> tuple:
    try:
        st = f.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return ()


def _holds(f: Path, needles: list[bytes]) -> bool:
    """Whether the file holds one of `needles`, read in chunks and never parsed."""
    keep = max(map(len, needles)) - 1
    try:
        with open(f, "rb") as fh:
            tail = b""
            while chunk := fh.read(1 << 20):
                buf = tail + chunk
                if any(x in buf for x in needles):
                    return True
                tail = buf[-keep:] if keep else b""
    except OSError:
        return False
    return False


def transcript_hit(run_dir: str | Path | None, path: str, home: str = "") -> tuple[int, str] | None:
    """(line number, quote) of the first command or file edit the run issued (issued) that writes `path`
    (_writes, an editor tool's file) or a store like it (_writer), as one of its simple commands
    (_commands); None when none does. A transcript and refusal log holding neither the file's name nor
    the writer's command word are answered without parsing a line (_needles), and an answer is kept until
    either file's size or mtime changes: this runs on the daemon's thread as runs end."""
    if not run_dir:
        return None
    rd = Path(run_dir)
    key = (str(rd), path, home, _stamp(rd / "output.jsonl"), _stamp(rd / "refusals.jsonl"))
    if key in _HITS:
        return _HITS[key]
    needles = _needles(path)
    hit = _scan(rd, path, home) if any(_holds(rd / f, needles) for f in ("output.jsonl", "refusals.jsonl")) else None
    while len(_HITS) >= HITS_KEPT:
        _HITS.pop(next(iter(_HITS)))
    _HITS[key] = hit
    return hit


def _scan(run_dir: Path, path: str, home: str) -> tuple[int, str] | None:
    """transcript_hit's answer, read from the run's calls; a shell call holding none of the needles is passed
    over unparsed, as a whole transcript holding none is."""
    needles = [x.decode() for x in _needles(path)]
    for n, kind, text in issued(run_dir):
        if kind == "file":
            if _is(text, path, home):
                return n, text[:QUOTE]
            continue
        if not any(x in text for x in needles):
            continue
        for argv, targets in _commands(text):
            if _writes(argv, targets, path, home) or _writer(argv, path):
                return n, " ".join(argv + [f"> {t}" for t in targets])[:QUOTE]
    return None



def classify(changed: list, marks: dict, start: float | None, end: float, run_dir: str | Path | None = None,
             home: str = "") -> dict:
    """{path: [verdict, change time ns, note]} for each change of `changed` (changes() rows); `marks` is
    the runner's {path: [before, after]} (exit.json cred_marks), the change time the file's new ctime (its
    last one for a removal, else `end`). Verdicts, first that holds:
    - metadata: only ctime moved: nothing was written.
    - ledger: this machine's change ledger records it as made while the run ran (ledger_entry); note: the entry.
    - run (FAILS): a command or file edit the run issued writes the path or such a store (transcript_hit); note: it.
    - outside: neither; written by something the run's commands do not show (the account's user, another
      run or process on the host). outside_alarm when the store is new, removed, shrunk, replaced (another
      inode) or resized, as a lost or swapped key or login would be.
    Only `run` fails the hand-off."""
    home = home or os.environ.get("HOME") or os.path.expanduser("~")
    out = {}
    for path, how in changed:
        pair = marks.get(path) if isinstance(marks, dict) else None
        before, after = (pair if isinstance(pair, list) and len(pair) == 2 else (None, None))
        last = after or before
        at = last[3] if isinstance(last, list) and len(last) > 3 else int(end * 1e9)
        try:
            entry = ledger_entry(path, start, end, home)
        except Exception:   # an unreadable ledger records nothing
            entry = None
        if how == "metadata":
            out[path] = ["metadata", at, ""]
        elif entry:
            by = f"project {entry.get('by') or '?'}" + (f" task #{entry['task']}" if entry.get("task") else "")
            out[path] = ["ledger", at, f"#{entry.get('id')} by {by}: {str(entry.get('what') or '')[:300]}"]
        elif hit := transcript_hit(run_dir, path, home):
            out[path] = [FAILS, at, f"output.jsonl line {hit[0]}: {hit[1]}"]
        else:
            same = before and after and how == "changed" and before[0] == after[0] and before[2] == after[2]
            out[path] = ["outside" if same or _ssh_config_side(path) else "outside_alarm", at, ""]
    return out


def _ssh_config_side(path: str) -> bool:
    """ssh's client config or a temp or backup file its editors leave beside it (config.tmp.*,
    config.bak*, config~, config-*): no key is in them."""
    p = Path(path)
    return p.parent.name == ".ssh" and ssh_config(p.name)


def held(run_dir: str | Path) -> dict:
    """The verdicts classify() stored in the run dir; {} without them (every change fails the run)."""
    import json
    try:
        got = json.loads((Path(run_dir) / VERDICTS).read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def excused(verdicts: dict) -> set:
    """The paths whose change fails no run: every verdict but FAILS."""
    return {p for p, v in verdicts.items() if isinstance(v, list) and v and v[0] != FAILS}


def _claim(key: str, now: float) -> bool:
    """True for the first caller, of any project on the host, to tell `key`: a claim file under the
    account's tt-project home (O_EXCL). True when that folder cannot be written (told per project)."""
    from . import project
    d = project.HOME_DIR / TOLD_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.close(os.open(d / hashlib.sha256(key.encode()).hexdigest()[:24], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return True
    for f in d.iterdir():
        try:
            if f.stat().st_mtime < now - TOLD_KEEP_S:
                f.unlink()
        except OSError:
            pass
    return True


def observe(db, run: dict, changed: list, now: float, verdicts: dict | None = None, where=None) -> int:
    """At most two observations for `run`'s changes (a runs row; `verdicts` from classify, a change without
    one counts as the run's): one high naming the changes the run's transcript shows (they fail its
    hand-off), quoting each line; and one for the rest, made outside the run, holding only the changes no
    run of any project on the host told yet (_claim on path, change minute, verdict and how): high when
    one is new, removed, replaced or resized, else normal, with how to record a deliberate change in the
    ledger. Returns how many it queued."""
    from . import timefmt
    who = f"run {run.get('id')}" + (f" of task #{run['task']}" if run.get("task") else f" ({run.get('role')})")
    mine, rest, sev = [], [], "normal"
    for path, how in changed:
        v = (verdicts or {}).get(path)
        kind, at, note = v if isinstance(v, list) and len(v) == 3 else (FAILS, int(now * 1e9), "")
        if kind == FAILS:
            mine.append(f"{path} {how}" + (f" ({note})" if note else ""))
            continue
        if not _claim(f"credwatch:{path}:{int(at / 1e9 // 60)}:{kind}:{how}", now):
            continue
        sev = "high" if kind == "outside_alarm" else sev
        what = {"metadata": "attributes changed (mode, owner or links; size, mtime and inode the same)",
                "ledger": f"recorded in the machine's change ledger as {note}, no urgent work",
                "outside": "rewritten in place at the same size",
                "outside_alarm": f"{how}: new, removed, replaced or resized"}.get(kind, how)
        rest.append(f"{path} {what}, at {timefmt.long(at / 1e9, where)}")
    told = 0
    if mine:
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", f"credwatch:run:{run.get('id')}:{int(now)}", "high",
              f"Credential files written by {who}, as its transcript shows: {'; '.join(mine)[:1500]}. Its hand-off "
              f"was stored failed. Start at that line and stop it recurring; if a login no longer works, ask "
              f"the user to log in again (for gh: `gh auth login`). Nothing was restored.", "queued"))
        told += 1
    if rest:
        try:
            from . import machines
            alias = (machines.here() or ["<this machine's alias>"])[0]
        except Exception:
            alias = "<this machine's alias>"
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "observation", f"credwatch:host:{run.get('id')}:{int(now)}", sev,
              f"Credential files changed during {who} and not by it (its transcript does not show it; told once "
              f"for the host, no run failed): {'; '.join(rest)[:1500]}. If the user or their agent changed one on "
              f"purpose, record it so later runs are not blamed: `ttp machines change add {alias} \"<what> "
              f"<path>\" --undo 'none: deliberate'`. If a login no longer works, ask the user to log in again "
              f"(for gh: `gh auth login`). Nothing was restored.", "queued"))
        told += 1
    return told
