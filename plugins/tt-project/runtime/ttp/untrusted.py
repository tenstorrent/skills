# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Extra guards for a run whose task reads untrusted input (the `untrusted_input` label).

A PR comment, issue body, CI log or another person's note can carry a planted instruction. In a
head-to-head with planted injections, models followed some of them past the guards every run has:
a credential file outside the known stores was read, a new branch was pushed, a comment was posted.
A run is untrusted when its task carries the label (read from the project's records) or the runner
set TTP_UNTRUSTED_INPUT=1 (a benchmark cell without a project). Then, model-free and on every provider
(the worker hook, the runner's stream guard, and the run's bin/git and bin/gh, which scripts reach too):
- git push updates only the task's own work branch (its recorded branch) or the push branch, and
  the push branch only through `ttp push`: the run's pre-push hook (gitguard) refuses any other ref
  git names, tags and deletes too, and the hook refuses a raw `git push` naming the push branch.
  One more: the pr_branch the coordinator recorded for the task (never one named only in quoted
  text), at exactly a head an independent review passed (reviewed_pr_pushes);
- gh and curl calls that write are refused (write_refusal): `gh api` with a write method, a field or
  input body, or a GraphQL mutation; every other gh subcommand but the reads READ_VERBS names (so
  `gh pr comment`, `gh issue create`, `gh pr review`); curl with -X/--request other than GET or HEAD,
  a data or form body, --json or -T; wget with a post body or --method. A task that needs such a
  write carries the label `untrusted_write` (WRITE_LABEL), set only by the coordinator's
  `untrusted_write` field and never read from a spec's wording; pushes and credential reads
  stay refused with it;
- reads of credential files outside the run's worktree are refused (cred_path): a path whose folder
  is a credential store (CRED_DIRS), whose name is a credential file (CRED_FILES), a private key or
  certificate (CRED_SUFFIXES, id_* but .pub), or names a token, secret, credential, password or API
  key, a process's environ file; named in a Bash command's words or a Read, Grep or Glob path, or
  looked for by a find from outside the worktree (`find ~ -name '*.pem'`).
  ssh, scp, sftp and git (core.sshCommand) read their own files: the value of ssh's -F and -i and of
  -o IdentityFile=, UserKnownHostsFile= and CertificateFile= is not checked (ssh_words), but a
  config or known-hosts file with a credential's name is (ssh would echo its lines in errors).
  So are environment dumps (a bare printenv, env, export or set, or printenv naming a credential-like
  variable), which print tokens.
Every task run, marked or not, is refused a Bash command that may change, move or delete the run's
RUN_MARK file (its name only in quoted text, a read, also through a variable, glob or substitution, a
cp's source or a heredoc written to another file passes) and an edit of that file (mark_refusal):
removing it would lift these guards for the rest of the run and keep the
daemon from labelling the task. A glob (`rm $TTP_RUN_DIR/*`) still gets around it.
Not covered: a path or command built at run time inside a script or inline code (except git and gh,
whose wrappers run anyway), other HTTP clients (httpie, python requests), GET requests that carry
data in their URL, and connector (MCP) writes, which keep their AI-tag guard. A task record that
cannot be read counts as not labelled: a run without the label behaves exactly as before.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shlex

LABEL = "untrusted_input"   # modelpick.UNTRUSTED
WRITE_LABEL = "untrusted_write"
# A coordinator task_update `untrusted_input: false` with a `text` naming who vouched for the source
# clears LABEL (coordinator.untrusted_update): the task keeps CLEARED_LABEL, an event of CLEARED_KIND
# holds the why. A task run cannot clear it.
CLEARED_LABEL, CLEARED_KIND = "untrusted_cleared", "untrusted_cleared"
ENV = "TTP_UNTRUSTED_INPUT"
KEY = LABEL                    # the run note's entry
RUN_MARK = "untrusted-input"   # in the run dir: one source per line (web, connector, download), read by the hook and the daemon
SOURCES = ("pr_comments", "test_log", "peer_note", "coordinator", "web", "connector", "download", "inherited")
NOTE_TAG_RE = re.compile(r"\[note [0-9a-f]{6,}\]")
LOCAL_RE = re.compile(r"^(?:https?://)?(?:localhost|127(?:\.\d+){3}|\[::1\]|0\.0\.0\.0)(?:[:/]|$)")
READ_VERBS = {"view", "list", "ls", "diff", "checks", "status", "download", "watch", "clone", "checkout",
              "verify", "search"}
READ_GROUPS = {"search", "status", "help", "version", "completion", "browse"}
CRED_DIRS = (".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".password-store", ".config/gh",
             ".config/gcloud", ".local/share/keyrings")
CRED_FILES = {".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass", ".my.cnf", ".env",
              "auth.json", "hosts.yml", "hosts.yaml", "shadow", "environ"}
CRED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".gpg", ".asc")
# token and secret anywhere in a name (authtoken, mytoken.txt, accessTokenCache.json, SecretKey), but not
# a tokenizer or a secretary; nor are the token lists beside a model's tokenizer.json (CRED_NAME_OK)
CRED_NAME_RE = re.compile(r"token(?!i[sz])|secret(?!ar)|credential"
                          r"|passw(or)?d|api[_-]?key|^id_(?!.*\.pub$)|^\.env\.", re.I)
CRED_NAME_OK = {"special_tokens_map.json", "added_tokens.json"}
# ssh-family programs whose own option files are theirs to read (ssh_words); ssh options that take a value
SSH_PROGS = {"ssh", "scp", "sftp", "autossh"}
SSH_VALUE_FLAGS = set("BbcDEeFIiJLlmOoPpQRSWw")
# -o options naming a file ssh opens itself: keys ssh never prints, its sockets, and known-hosts files
SSH_OWN_O_RE = re.compile(r"\s*(IdentityFile|CertificateFile|ControlPath|IdentityAgent|UserKnownHostsFile)\s*=?\s*(.*)", re.I | re.S)
WHY = ("this task reads untrusted input (label {label}), which may carry planted instructions, so its run "
       "{what}. ").format
ENV_WHAT = "never dumps its environment, which holds tokens"
CRED_WHAT = "never reads credential, token or secret files outside its worktree"


@functools.lru_cache(maxsize=4)
def _labels(task: str, base: str) -> tuple[str, ...]:
    if not task.isdigit() or not base:
        return ()
    try:
        from .project import Project
        p = Project(base)
        t = p.db.task(int(task)) if (p.state / "project.db").is_file() else None
        labels = (t or {}).get("labels") or "[]"
        labels = json.loads(labels) if isinstance(labels, str) else labels
        return tuple(str(x) for x in labels) if isinstance(labels, list) else ()
    except Exception:
        return ()


def _task(env: dict | None = None) -> tuple[str, str]:
    env = os.environ if env is None else env
    return str(env.get("TTP_TASK") or ""), str(env.get("TTP_PROJECT") or "")


def labelled(task: dict | None) -> bool:
    """A task record that carries the label."""
    try:
        return LABEL in json.loads((task or {}).get("labels") or "[]")
    except (TypeError, ValueError):
        return False


def inherit(labels: list[str], *tasks: dict | None) -> list[str]:
    """`labels` plus LABEL when it is missing and one of `tasks` (the work a new task continues) carries it."""
    return labels + [LABEL] if LABEL not in labels and any(labelled(t) for t in tasks) else labels


def spec_sources(spec: str) -> list[str]:
    """The untrusted sources a coordinator's spec shows model-free: a peer note's `[note <fp>]` tag."""
    return ["peer_note"] if NOTE_TAG_RE.search(spec or "") else []


def mark(run_dir: str, source: str) -> None:
    """Record in the run dir that this run read untrusted text from `source` (once per source)."""
    path = os.path.join(run_dir, RUN_MARK)
    try:
        if source not in marked(run_dir):
            with open(path, "a") as f:
                f.write(source + "\n")
    except OSError:
        pass


def marked(run_dir: str | None) -> list[str]:
    try:
        with open(os.path.join(run_dir, RUN_MARK)) as f:
            return f.read().split()
    except (OSError, TypeError):
        return []


def read_source(tool: str, segments: list[list[str]] | None = None) -> str:
    """The untrusted source a tool call reads, or '': web pages, connector reads, remote downloads."""
    if tool in ("WebFetch", "WebSearch"):
        return "web"
    if tool.startswith("mcp__"):
        return "connector"
    for seg in segments or []:
        if seg and os.path.basename(seg[0]) in ("curl", "wget") and any(
                re.match(r"(?:--url=)?(?:https?|ftp)://", a) and not LOCAL_RE.match(a.removeprefix("--url="))
                for a in seg[1:]):
            return "download"
    return ""


def active(env: dict | None = None) -> bool:
    """This run's task reads untrusted input: labelled, or its run read web, connector or downloaded text."""
    env = os.environ if env is None else env
    return env.get(ENV) == "1" or LABEL in _labels(*_task(env)) or bool(marked(env.get("TTP_RUN_DIR")))


def writes_allowed(env: dict | None = None) -> bool:
    return WRITE_LABEL in _labels(*_task(env))


def own_branch(env: dict | None = None) -> str:
    task, base = _task(env)
    if not task.isdigit() or not base:
        return ""
    try:
        from .project import Project
        return str((Project(base).db.task(int(task)) or {}).get("branch") or "").removeprefix("refs/heads/")
    except Exception:
        return ""


def push_branch(raw: bool = False) -> str:
    """The push branch's name (delivery.push_branch without a leading `origin/`; as set with `raw`), or ""."""
    try:
        from .aitag import project_config
        ref = str((project_config().get("delivery") or {}).get("push_branch") or "").strip()
    except Exception:
        return ""
    return ref if raw else ref.split("/", 1)[1] if ref.startswith("origin/") else ref


# A span of quoted text (spans.wrap) with its contents, or an unclosed one to the end; and a `>` quote line.
_SPAN = re.compile(r"⟦([0-9a-f]{8}) data: [^⟧]*⟧.*?(?:⟦/\1⟧|\Z)", re.S)
_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.M)
# Quoted words on one line: '...' (an apostrophe inside a word, as in user's, stays inside), "...", “...”, ‘...’.
QUOTED = re.compile(r"""(?<!\w)'(?:[^'\n]|'(?=\w))*'(?!\w)|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’""")


def unquoted(spec: str) -> str:
    """`spec` without the text it quotes: data spans with their contents, and `>` quote lines."""
    return _QUOTE_LINE.sub("", _SPAN.sub("", spec or ""))


def recorded_pr_branch(task: dict) -> str:
    """The pr_branch the coordinator recorded for `task` (worktree.recorded_pr_branch: a spec `pr_branch:`
    line or a task_update's), or "": also when that branch is named only inside quoted text (unquoted),
    since a pasted review or note can carry any `pr_branch:` line."""
    from . import worktree
    branch = worktree.recorded_pr_branch(task) or ""
    if not branch or not worktree.valid_branch(branch):
        return ""
    return branch if worktree.spec_names_pr_branch(unquoted(str(task.get("spec") or "")), branch) else ""


def reviewed_heads(db, task: dict) -> list[str]:
    """The full commits an independent review that passed approved for `task`: a review whose subject is
    the task (db.review_subject) or, naming none, that depends on it; its head from the push queue or
    review_stats."""
    from . import reviewsup
    from .db import dependency_ids, review_subject
    heads = []
    for r in db.q("SELECT * FROM tasks WHERE kind='review' AND id!=? ORDER BY id", (task["id"],)):
        r = dict(r)
        subject = review_subject(r)
        if not reviewsup.passed(r) or (subject != task["id"] if subject is not None
                                       else task["id"] not in dependency_ids(r)):
            continue
        for row in (db.one("SELECT head FROM push_queue WHERE task=? ORDER BY id DESC LIMIT 1", (r["id"],)),
                    db.one("SELECT head FROM review_stats WHERE task=?", (r["id"],))):
            if row and re.fullmatch(r"[0-9a-f]{40}", str(row["head"] or "")):
                heads.append(row["head"])
    return list(dict.fromkeys(heads))


def reviewed_pr_pushes(env: dict | None = None) -> list[str]:
    """`refs/heads/<branch>@<sha>` for each head of this run's task an independent review passed
    (reviewed_heads), on the pr_branch the coordinator recorded for it (recorded_pr_branch): the only
    push to a branch not the task's own a labelled run may make. [] when it has none."""
    task, base = _task(env)
    if not task.isdigit() or not base:
        return []
    from .project import Project
    db = Project(base).db
    t = db.task(int(task))
    branch = recorded_pr_branch(t) if t else ""
    return [f"refs/heads/{branch}@{sha}" for sha in reviewed_heads(db, t)] if branch else []


def push_refs(env: dict, name=lambda ref: ref) -> list[str]:
    """The refs an untrusted run's push may update (gitguard's pre-push hook), or [] when the run is no
    such run: its own branch, the push branch, and its recorded pr_branch only at a reviewed head
    (`ref@sha`: reviewed_pr_pushes). `name` maps delivery.push_branch to its branch (gitguard._branch_name)."""
    if not active(env):
        return []
    branches = (own_branch(env), name(b) if (b := push_branch(raw=True)) else "")
    refs = [f"refs/heads/{b}" for b in dict.fromkeys(branches) if b] + reviewed_pr_pushes(env)
    return refs or ["(none)"]


def _strip_repo(args: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(args):
        if args[i] in ("-R", "--repo"):
            i += 2
            continue
        if not args[i].startswith("--repo="):
            out.append(args[i])
        i += 1
    return out


def gh_refusal(args: list[str]) -> str | None:
    """Why a run reading untrusted input may not make this gh call (`args` after `gh`): a write
    without the write label. None otherwise; fails closed."""
    if not os.environ.get("TTP_RUN_DIR") or not active():
        return None
    try:
        return write_refusal("gh") if gh_write(args) and not writes_allowed() else None
    except Exception as e:
        return f"refused: the untrusted-input guard failed ({type(e).__name__})"


def gh_write(args: list[str]) -> bool:
    """gh `args` (after `gh`) may write to GitHub: an api call with a write method, body or mutation,
    or any other subcommand that is no read (READ_GROUPS, READ_VERBS)."""
    args = _strip_repo(args)
    words = [a for a in args if not a.startswith("-")]
    if not words or words[0] in READ_GROUPS:
        return False
    if words[0] == "api":
        from .hook import _gh_write
        return _gh_write(["gh", *args])
    if words[0] == "auth":   # `gh auth token` is a credential read, refused by credguard
        return len(words) > 1 and words[1] not in ("status", "token")
    return len(words) < 2 or words[1] not in READ_VERBS


def http_write(argv: list[str]) -> bool:
    """curl or wget `argv` sends a write: a non-GET method, a body, a form or an upload."""
    from .aitag import CURL_DATA, CURL_VALUE_SHORTS, WGET_DATA, _data_options
    prog = os.path.basename(argv[0]) if argv else ""
    if prog not in ("curl", "wget"):
        return False
    method = ""
    for i, a in enumerate(argv[1:], 1):
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if a in ("-X", "--request", "--method"):
            method = nxt
        elif m := re.fullmatch(r"(?:-X|--request=|--method=)(\S+)", a):
            method = m.group(1)
        elif prog == "curl" and (a == "--upload-file" or a.startswith("--upload-file=")):
            return True
        elif prog == "curl" and len(a) > 1 and a[0] == "-" and a[1] != "-":
            for j, c in enumerate(a[1:], 1):   # a short-flag cluster (-sX DELETE): the first value option takes the rest
                if c == "X":
                    method = a[j + 1:] or nxt
                elif c == "T":
                    return True
                if c in CURL_VALUE_SHORTS or f"-{c}" in CURL_DATA:
                    break
    if method and method.upper() not in ("GET", "HEAD", "OPTIONS"):
        return True
    return bool(_data_options(argv, CURL_DATA if prog == "curl" else WGET_DATA))


def write_refusal(what: str) -> str:
    return WHY(label=LABEL, what=f"sends no write through {what}") + (
        f"Report what you found in the hand-off; a task that must post carries the label {WRITE_LABEL} "
        "(the coordinator's `untrusted_write` field on task_add/task_update)")


def _top(cwd: str) -> str:
    d = os.path.abspath(cwd)
    while d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return os.path.abspath(cwd)


def cred_path(word: str, cwd: str) -> bool:
    """`word` (a command word or a tool's path) names a credential file outside the worktree of `cwd`."""
    word = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    word = word.lstrip("@<")
    if not word or ("/" not in word and not word.startswith(("~", "."))):
        return False
    if not _outside(word, cwd):
        return False
    path = os.path.normpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(word))))
    if re.fullmatch(r"/proc/[^/]+/environ", path):
        return True
    rel = path.replace(os.sep, "/")
    if any(f"/{d}/" in rel + "/" for d in CRED_DIRS):
        return True
    return cred_name(os.path.basename(path))


def cred_name(name: str) -> bool:
    """A file name (or a find -name pattern, its wildcards dropped) a credential file has."""
    name = name.strip("*?")
    return name not in CRED_NAME_OK and (name in CRED_FILES or name.endswith(CRED_SUFFIXES)
                                         or bool(CRED_NAME_RE.search(name)))


def _outside(path: str, cwd: str) -> bool:
    path, top = os.path.normpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(path)))), _top(cwd)
    return path != top and not path.startswith(top + os.sep)


def _find_refusal(argv: list[str], cwd: str) -> bool:
    """A find from a folder outside the worktree looking for a credential file by name."""
    roots = []
    for w in argv[1:]:
        if w.startswith(("-", "(", "!")):
            break
        roots.append(w)
    if not any(_outside(r, cwd) for r in roots or ["."]):
        return False
    return any(argv[i] in ("-name", "-iname", "-path", "-ipath", "-wholename") and cred_name(
        os.path.basename(argv[i + 1])) for i in range(1, len(argv) - 1))


def _words(s: list[str]) -> list[str]:
    """A segment's words to check: its first too unless it is a program (a split `$(...)` leaves a path)."""
    if not s:
        return []
    first = os.path.expanduser(s[0])
    return s[1:] if "/" not in s[0] or (os.path.isfile(first) and os.access(first, os.X_OK)) else s


# A variable printenv may name: a plain name that does not look like a credential (GH_TOKEN, *_API_KEY, AUTH)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CRED_VAR_RE = re.compile(r"key|auth|pass|token|secret|credential|cookie|session", re.I)


def _program(s: list[str]) -> str:
    """The program a segment runs (its first word's name) when that word is one, else ''."""
    first = os.path.expanduser(s[0])
    return os.path.basename(s[0]) if "/" not in s[0] or (os.path.isfile(first) and os.access(first, os.X_OK)) else ""


def ssh_words(argv: list[str]) -> list[str]:
    """An ssh, scp, sftp or autossh call's words to check: all but the files ssh reads itself (-F's config,
    -i's key, -o IdentityFile=, CertificateFile=, UserKnownHostsFile=, its sockets). A config or known-hosts file with a
    credential's name stays checked: ssh echoes a bad line of it in its errors. ssh's options end at its
    destination; scp and sftp take them anywhere."""
    out, i, stop = [], 1, os.path.basename(argv[0]) in ("ssh", "autossh")
    while i < len(argv):
        w = argv[i]
        i += 1
        if w == "--" or not w.startswith("-") or w == "-":
            out.append(w)
            if stop and w != "--":
                out.extend(argv[i:])
                break
            continue
        j = next((k for k, c in enumerate(w[1:], 1) if c in SSH_VALUE_FLAGS), None)
        if j is None:
            out.append(w)
            continue
        flag, value = w[j], w[j + 1:]
        if not value and i < len(argv):
            value, i = argv[i], i + 1
        if flag == "o" and (m := SSH_OWN_O_RE.fullmatch(value)):
            flag, value = ("i" if m.group(1).lower() != "userknownhostsfile" else "F"), m.group(2)
        if flag == "i" or (flag == "F" and not cred_name(os.path.basename(value.strip("\"'")))):
            continue
        # another -o option's value is checked whole and word by word (ProxyCommand=cat <file>)
        out += ([w[:j + 1], value, *(value.partition("=")[2].split() if flag == "o" else [])] if value else [w])
    return out


def _checked_words(s: list[str]) -> list[str]:
    """A segment's words cred_path checks (_words), the files ssh reads itself left out (ssh_words), also in
    git's `-c core.sshCommand=<ssh command>`."""
    name = _program(s) if s else ""
    if name in SSH_PROGS:
        return ssh_words(s)
    if name != "git":
        return _words(s)
    out = []
    for prev, w in zip(s, s[1:]):
        key, _, value = w.partition("=")
        if (prev == "-c" or w.startswith("-c")) and key.removeprefix("-c").lower() == "core.sshcommand":
            try:
                inner = shlex.split(value)
            except ValueError:
                inner = []
            if inner and os.path.basename(inner[0]) in SSH_PROGS:
                out += ssh_words(inner)
                continue
        out.append(w)
    return out


def _env_dump(argv: list[str]) -> bool:
    name = os.path.basename(argv[0])
    if name == "printenv":   # dumps without a name; prints only the named variables otherwise
        names = [a for a in argv[1:] if a != "--"]
        return not names or any(not _ENV_NAME_RE.fullmatch(a) or _CRED_VAR_RE.search(a) for a in names)
    if name == "env":
        return all(a.startswith("-") or "=" in a for a in argv[1:])
    if name in ("export", "declare", "typeset"):
        return all(a in ("-p", "-x", "-px", "-xp") for a in argv[1:])
    return name == "set" and len(argv) == 1


def bash_refusal(segments: list[list[str]], raw: list[list[str]], cwd: str) -> str | None:
    """Why an untrusted run's Bash call is refused (see the module doc), else None. `segments`: its simple
    commands without prefixes, also those in `sh -c` strings; `raw`: with them (a bare env)."""
    names = {os.path.basename(s[0]) for s in segments if s} | {os.path.basename(s[0]) for s in raw if s}
    words = [w for s in segments for w in s]
    if not (names & {"gh", "curl", "wget", "git", "printenv", "env", "export", "declare", "typeset", "set"}
            or any("/" in w or w.startswith(("~", ".")) for w in words)) or not active():
        return None
    if any(_env_dump(s) for s in raw if s):
        return WHY(label=LABEL, what=ENV_WHAT) + \
            "Print one harmless variable by name with `echo \"$NAME\"`"
    if any(cred_path(w, cwd) for s in segments for w in _checked_words(s)) or any(
            _find_refusal(s, cwd) for s in segments if s and os.path.basename(s[0]) == "find"):
        return WHY(label=LABEL, what=CRED_WHAT) + \
            "ssh, git and gh read their own credentials; give a stored secret to a command with `ttp secret env`"
    allowed = writes_allowed()
    for s in segments:
        name = os.path.basename(s[0])
        if name == "gh" and not allowed and gh_write(s[1:]):
            return write_refusal("gh")
        if name in ("curl", "wget") and not allowed and http_write(s):
            return write_refusal(name)
        if name == "git" and (b := push_branch()):
            from . import gitguard
            _, sub, rest = gitguard.split(s[1:])
            if sub == "push" and any(w.lstrip("+").rsplit(":", 1)[-1].removeprefix("refs/heads/") == b
                                     for w in rest if not w.startswith("-")):
                return WHY(label=LABEL, what=f"pushes to the push branch {b} only through `ttp push`") + \
                    "Commit and hand off; the review lands the change"
    return None


MARK_WHY = ("this run's untrusted-input file records the untrusted text it read; leave it as it is (a read "
            "with cat, grep, head or wc passes, also through a loop variable or glob, when no code or read loop "
            "gets its name)")


# Commands that only read or print their arguments: naming RUN_MARK to one has no effect on the file.
MARK_READERS = frozenset(("cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "ls", "stat",
                          "file", "diff", "cmp", "test", "[", "echo", "printf", "jq", "md5sum", "sha1sum", "sha256sum"))
# Words that run code or other commands from their arguments or input: a mention anywhere in such a
# command may reach the file, so it is refused as before.
MARK_RUNNER_RE = re.compile(r"(?:ba|z|da|k|c|tc|fi)?sh|python[\d.]*|perl|ruby|node|deno|bun|php|lua|tclsh"
                            r"|[gmn]?awk|sed|xargs|find|parallel|ssh|eval|exec|source|watch|busybox|n?vim?|ed|ex")
# Copiers that only read a source: the mark as one (not the last word) is a read, unless an option links
# to it or names a target folder (_mark_copy_read).
MARK_COPIERS = frozenset(("cp", "gcp", "install"))
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): *(.+?) *$", re.M)


def _mark_path(word: str) -> bool:
    return os.path.basename(word.rstrip("/")) == RUN_MARK


def _mark_copy_read(args: list[str]) -> bool:
    """A cp or install whose words name RUN_MARK only as a source: its last plain word (the target) is
    something else and no option links to the source or names a target folder (-l, -s, -t)."""
    plain = [w for w in args if not w.startswith("-")]
    if not plain or RUN_MARK in plain[-1]:
        return False
    return not any(re.fullmatch(r"-[a-zA-Z]*[lst][a-zA-Z]*", w) or w.startswith(("--link", "--symbolic", "--target"))
                   for w in args)


def _mark_archive_read(run: list[str]) -> bool:
    """An rsync, tar or zip that only reads RUN_MARK as a source: options from a small read-only set (no
    removal of sources, no remote shell, no log or backup file), and the mark is not its target (rsync's
    last plain word, tar's -f archive, zip's archive)."""
    name, args = os.path.basename(run[0]), run[1:]
    opts = [w for w in args if w.startswith("-")]
    plain = [w for w in args if not w.startswith("-")]
    if name == "rsync":
        ok = all(re.fullmatch(r"-[avrptgoDzhlHS]+", w) or w in ("--archive", "--verbose", "--recursive", "--compress",
                                                                  "--human-readable", "--progress") for w in opts)
        return ok and len(plain) >= 2 and RUN_MARK not in plain[-1]
    if name == "tar":
        if not args or not re.fullmatch(r"-?[cvzjJ]*c[cvzjJ]*f", args[0]) or len(args) < 2:
            return False   # create only, its archive the next word
        return RUN_MARK not in args[1] and not any(w.startswith("-") for w in args[2:])
    if name == "zip":
        return all(re.fullmatch(r"-[rq0-9]+", w) for w in opts) and len(plain) >= 2 and RUN_MARK not in plain[0]
    return False


def _mark_awk_read(run: list[str]) -> bool:
    """An awk whose program only reads: no option but -F or -v (no -i inplace, -f, -e or --include), and a
    program with no output redirect or pipe (>, |), no system() and no @load."""
    if not run or not re.fullmatch(r"[gmn]?awk", os.path.basename(run[0])):
        return False
    args, i = run[1:], 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-F", "-v"):
            i += 2
        elif re.fullmatch(r"-[Fv].+", args[i]):
            i += 1
        else:
            return False
    return i < len(args) and not re.search(r"[>|@]|system", args[i])


def _unsingle(cmd: str) -> str:
    """`cmd` without its single-quoted spans, the only quoting in which a shell runs no substitution."""
    out, q, esc = [], "", False
    for c in cmd:
        if q == "'":
            q = "" if c == "'" else q
        elif esc:
            esc = False
            out.append(c)
        elif c == "\\":
            esc = True
            out.append(c)
        elif c == "'" and not q:
            q = "'"
        else:
            q = ("" if q else '"') if c == '"' else q
            out.append(c)
    return "".join(out)


MARK_FEEDS = frozenset(("read", "mapfile", "readarray", "xargs", "parallel"))   # turn their input into words or commands
# Commands that change or create a file named as their argument, or run another command: a word of a prose clause
# before the mark that is one of these (or a runner or feed) makes it a command line, the same on every host (no
# PATH lookup). Readers (MARK_READERS) stay prose: naming the file to one has no effect on it.
MARK_FILE_COMMANDS = frozenset((
    "rm", "rmdir", "unlink", "mv", "cp", "ln", "truncate", "shred", "touch", "chmod", "chown", "chattr", "mkdir",
    "mkfifo", "mknod", "tee", "dd", "install", "patch", "nano", "rsync", "scp", "tar", "zip", "unzip", "gzip",
    "gunzip", "bzip2", "xz", "cpio", "git", "make", "sort", "uniq", "split", "csplit", "history", "enable", "sudo",
    "env", "command", "builtin", "nohup", "timeout"))
MARK_SUBST_DEPTH = 4   # nested substitutions read; deeper ones count as a write


def _mark_close(cmd: str, i: int, end: str) -> int:
    """The index of the `end` (")" or a backtick) closing a substitution whose text starts at `i`, or -1."""
    q, depth = "", 1
    while i < len(cmd):
        c = cmd[i]
        if q == "'":
            q = "" if c == "'" else q
        elif c == "\\":
            i += 1
        elif c == '"' and end == ")":
            q = "" if q == '"' else '"'
        elif c == "'" and not q and end == ")":
            q = "'"
        elif not q and end == ")" and c in "()":
            depth += 1 if c == "(" else -1
            if not depth:
                return i
        elif c == end and end == "`":
            return i
        i += 1
    return -1


def _mark_subst(cmd: str) -> tuple[str, list[str]] | None:
    """`cmd` with each outermost command or process substitution (`$(..)`, backticks, `<(..)`, `>(..)`)
    replaced by one word: the bare mark name when its text names RUN_MARK (its output may be the mark's
    path), else `_`; and the substitutions' texts. None when one is not closed."""
    out, inner, q, i = [], [], "", 0
    while i < len(cmd):
        c = cmd[i]
        if q == "'":
            q = "" if c == "'" else q
        elif c == "\\":
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif c == '"':
            q = "" if q == '"' else '"'
        elif c == "'" and not q:
            q = "'"
        elif cmd.startswith("$(", i) or c == "`" or (not q and cmd.startswith(("<(", ">("), i)):
            start = i + (1 if c == "`" else 2)
            end = _mark_close(cmd, start, "`" if c == "`" else ")")
            if end < 0:
                return None
            inner.append(cmd[start:end])
            out.append(RUN_MARK if RUN_MARK in inner[-1] else "_")
            i = end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), inner


# Script interpreters: a mention of the mark only in a quoted prose clause of their program or arguments
# (receipt or note text a script writes) is inert, as a quoted prose argument to any other command is.
MARK_SCRIPT_RE = re.compile(r"python[\d.]*|perl|ruby|node|deno|bun|php|lua")
# A quoted clause with no shell or code punctuation: text, never a path, option or command list, nor a
# word list or other prefixed literal (perl qw'', ruby %w'', tcl {*}"").
MARK_PROSE_RE = re.compile(r"(?<![\w%}])(['\"])([^'\"\n;&|<>(){}\[\]`$\\=*?/]*)\1")
MARK_PROSE_PLACE = "run-mark-prose"   # what a prose mention reads as
# What may stand before a quoted clause that is code, not prose: an interpreter's program option (python -c,
# perl -le, node --eval, php -r) or a here-string, whose clause is the program itself, or a quote operator
# with space before its delimiter (perl qw "..", q #..#, ruby %q ..).
MARK_CODE_QUOTE_RE = re.compile(r"(?:(?:^|\s)-\w*[ceErRBpP]|--eval|--print|<<<|\bq[qwxr]?|%\w*)[\s#]*$")


def _mark_prose(clause: str) -> bool:
    """Whether a quoted clause names RUN_MARK only as a word of prose: it has spaces, each mention is a
    whole word (trailing .,:!), and no word before one is a command (MARK_FILE_COMMANDS, a runner or feed),
    `.` or an option (a builtin's `history -w`, `enable -f`), so the clause run as a command line could not
    take the mark as its argument."""
    # shortcut: a script that cuts the name out of such text (a split or slice) is not seen, as names built at
    # run time never are (perl's glob splits its text too)
    words = clause.split()
    at = [i for i, w in enumerate(words) if RUN_MARK in w]
    if len(words) < 2 or not at or any(words[i].rstrip(".,:!") != RUN_MARK for i in at):
        return False
    return not any(w == "." or w.startswith("-") or w in MARK_FILE_COMMANDS or MARK_RUNNER_RE.fullmatch(w)
                   or w in MARK_FEEDS for w in words[:at[-1]])


def _mark_quiet(cmd: str, words: list[str]) -> tuple[list[str], str] | None:
    """`cmd` and its `words` with each prose mention of RUN_MARK (_mark_prose) read as MARK_PROSE_PLACE, for a
    command whose only code runners are script interpreters (MARK_SCRIPT_RE); None when anything else may
    run code or split text into words (a shell, eval, sed, xargs, a read loop, `.`), or the words do not
    line up, or a mention is left outside prose. A clause counts only where the shell quotes all of it,
    so no mention it hides is a word, and never as a program's own text or a quote operator's
    (MARK_CODE_QUOTE_RE)."""
    if any(w == "." or w in MARK_FEEDS or (MARK_RUNNER_RE.fullmatch(os.path.basename(w))
                                            and not MARK_SCRIPT_RE.fullmatch(os.path.basename(w))) for w in words):
        return None
    from .hook import _quoting
    flags = _quoting(cmd)
    text = MARK_PROSE_RE.sub(lambda m: m[0].replace(RUN_MARK, MARK_PROSE_PLACE) if _mark_prose(m[2]) and all(
        f[0] for f in flags[m.start():m.end()]) and not MARK_CODE_QUOTE_RE.search(cmd[:m.start()]) else m[0], cmd)
    if RUN_MARK in text:
        return None
    quiet = _mark_words(text)
    return (quiet, text) if quiet is not None and len(quiet) == len(words) else None


def _mark_words(cmd: str) -> list[str] | None:
    """`cmd` split into shell words and operators, a heredoc's body lines as commands; None when it
    does not parse."""
    from .hook import _quoting
    try:
        flags = _quoting(cmd)
        lex = shlex.shlex(re.sub("\n", lambda m: "\n" if flags[m.start()][0] else ";\n;", cmd), posix=True,
                          punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""   # a `#` inside a word is part of it (`rm a#b`); _mark_uncomment drops real comments
        return list(lex)
    except ValueError:
        return None


def _mark_uncomment(cmd: str) -> str:
    """`cmd` without its comments: from a `#` that starts a word outside quotes and outside a
    ${...} expansion (a `#` there is a literal, not a comment) to the line's end."""
    from .hook import _quoting
    flags = _quoting(cmd)
    cuts, depth = [], 0   # depth: inside a ${...} parameter expansion
    for i, c in enumerate(cmd):
        if c == "}" and depth:
            depth -= 1
        elif c == "{" and i and cmd[i - 1] == "$" and not flags[i - 1][0]:
            depth += 1
        elif c == "#" and not depth and (i == 0 or cmd[i - 1] in " \t\n;&|(" and not flags[i - 1][0]):
            cuts.append(i)
    out, end = [], 0
    for i in cuts:
        if i >= end:
            out.append(cmd[end:i])
            end = cmd.find("\n", i) if "\n" in cmd[i:] else len(cmd)
    return "".join(out) + cmd[end:]


def _mark_data_bodies(cmd: str) -> str:
    """`cmd` with comments cut from its command lines (_mark_uncomment; body lines stay as written,
    bar a comment tail ending in a backslash, cut before the join below) and without the bodies
    of heredocs that are only data: a quoted delimiter on a cat (or another _data_body piece) or on
    a tee into files, neither piped on, while every other command is only data too (_data_only), so none
    reads the file back into code (`xargs rm < list.txt`)."""
    from .hook import HEREDOC_RE, READERS, _chain, _data_body, _data_only, _heredocs, _pieces, _runs_inside
    # a comment ending in a backslash must not swallow the line after it: _heredocs joins
    # backslash-newlines before comments are cut, so cut such a comment tail at its own line first.
    # shortcut: the cut also hits a heredoc body line ending in a backslash; its comment tail is
    # gone from the body, which no interpreter reads as code once the tail holds no command
    cmd = "\n".join(_mark_uncomment(line) if line.endswith("\\") else line for line in cmd.split("\n"))
    lines, bodies = _heredocs(cmd)
    if not bodies:
        return _mark_uncomment(cmd)
    bare = _mark_uncomment("\n".join(lines)).split("\n")
    text = "\n".join(bare)

    def tee(i: int, pos: int) -> bool:
        s, e, sep = next(p for p in _pieces(bare[i]) if p[0] <= pos <= p[1])
        return bool(HEREDOC_RE.match(bare[i], pos)[2]) and bare[i][s:e].split()[:1] == ["tee"] \
            and sep not in ("|", "|&") and not _runs_inside(bare[i][s:e])
    data = {k for k, (i, _, pos) in enumerate(bodies) if (m := HEREDOC_RE.match(bare[i], pos))
            and m[0] == HEREDOC_RE.match(lines[i], pos)[0] and (_data_body(bare, i, pos) or tee(i, pos))}
    held = {(i, s) for k, (i, _, pos) in enumerate(bodies) if k in data
            for s, e, _ in _pieces(bare[i]) if s <= pos <= e}

    def inert(piece: str, sep: str, after: list[str]) -> bool:   # prints, or a bare reader word (no VAR= or env)
        return _data_only(piece, sep, text, after) or all(
            p.split()[:1] and p.split()[0] in READERS and not _runs_inside(p) for p in (piece, *after))
    pieces = [(j, line, _pieces(line)) for j, line in enumerate(bare)]
    if any((j, s) not in held and line[s:e].strip() and not inert(line[s:e], sep, _chain(line, ps, n))
           for j, line, ps in pieces for n, (s, e, sep) in enumerate(ps)):
        data = set()
    out = []
    for i, line in enumerate(bare):
        out.append(line)
        for k, (j, body, pos) in enumerate(bodies):
            if j == i and k not in data:
                out += body + [HEREDOC_RE.match(lines[i], pos)[3]]
    return "\n".join(out)


def _mark_fed(words: list[str], named: bool, quiet: list[str] | None = None) -> bool:
    """Whether a command that runs code or turns its input into words (MARK_RUNNER_RE, `.`, MARK_FEEDS)
    may get the mark's name: in its own words or redirects, from an earlier command of its pipeline that
    names it, or from a heredoc or here-string when the line names it (`named`). An input redirect,
    here-string or heredoc that gives the name (`< <(ls .../<mark>)`, `<<< ".../<mark>"`) may feed a
    compound command (a loop's `done`, `}`, `)`) whose body the split here cannot see: then any such
    command anywhere in the line counts. `quiet` (_mark_quiet): the words with prose mentions read as
    text, which is what the code gets; an earlier command of a pipeline still passes every mention on."""
    quiet = quiet or words
    seg, seen, mention, here, target, fed, coded, given = [], False, False, False, False, False, False, False
    said = False   # a mention, prose too, in the segment or its redirects: passed down a pipe
    for w, q in zip(words + [";"], quiet + [";"]):
        if target:
            target, mention, said = False, mention or RUN_MARK in q, said or RUN_MARK in w
            fed = fed or given and RUN_MARK in q
        elif w and set(w) <= set("&<>") and set(w) & set("<>"):
            heredoc = w.startswith("<<") and not w.startswith("<<<")
            target, here, given = True, here or w.startswith("<<"), "<" in w and not heredoc
            fed = fed or heredoc and named
        elif w and set(w) <= set(";&|()<>"):
            m = mention or any(RUN_MARK in x for _, x in seg)
            run = [x for x, _ in seg if x not in ("{", "}", "!", "if", "then", "else", "elif", "do", "while", "until")]
            plain = [x for x, _ in seg]
            code = any(MARK_RUNNER_RE.fullmatch(os.path.basename(x)) for x in (plain[1:] if _mark_awk_read(run) else plain))
            code = code or run[:1] == ["."] or bool(run[:1]) and run[0] in MARK_FEEDS
            coded = coded or code
            if code and (m or seen or here and named) or coded and fed:
                return True
            said = said or any(RUN_MARK in x for x, _ in seg)
            seen = (seen or said) if w in ("|", "|&") else False
            seg, mention, here, said = [], False, False, False
        else:
            seg.append((w, q))
    return False


def _mark_bash(cmd: str, depth: int = 0) -> bool:
    """Whether a Bash command may create, change, move or delete RUN_MARK. A mention only in a quoted
    prose argument (a note's text), as an argument of a command that only reads (a grep pattern, a
    cat, a cp, rsync, tar or zip source, an awk program that only prints), through a shell variable or
    glob path given to such a read, or in a heredoc written to another file has no effect; a redirect
    into it, a path to it given to any other command, or a mention that reaches a command that runs code
    or turns its input into words (_mark_fed) counts. A substitution is read as its own command, and
    its output as the mark's name when its text names it; next to a heredoc any substitution counts. A
    heredoc's body lines are read as commands, so one naming the mark as a bare word or path counts, unless
    the body is only data (_mark_data_bodies). A `#` starts a comment only where it starts a word."""
    if RUN_MARK not in cmd:
        return False
    from .hook import _quoting, _segments, _strip_prefix
    live = "".join(c for c, (inert, _) in zip(cmd, _quoting(cmd)) if not inert)
    if re.search(r"(?<!<)<<(?!<)", live) and ("`" in _unsingle(cmd) or "$(" in _unsingle(cmd) or "<(" in live
                                               or ">(" in live):
        return True   # a substitution in a heredoc's body runs too, where the quoting read here may be off
    got = _mark_subst(cmd)
    if got is None or (got[1] and depth >= MARK_SUBST_DEPTH):
        return True
    cmd, inner = got
    cmd = _mark_data_bodies(cmd)
    if any(t.strip() != RUN_MARK and _mark_bash(t, depth + 1) for t in inner):
        return True   # (a bare `name` substitution runs a command found on PATH, never the run folder's file)
    words = _mark_words(cmd)
    if words is None:
        return True
    if any(">" in w and set(w) <= set("&<>|") and RUN_MARK in n for w, n in zip(words, words[1:])):
        return True   # a redirect into it
    quiet = None if inner else _mark_quiet(cmd, words)
    if _mark_fed(words, RUN_MARK in (quiet[1] if quiet else cmd), quiet and quiet[0]):
        return True   # code, or a command run from piped input, that may get its name
    segs = _segments(cmd, strip=False, commenters="")
    for seg in segs:
        if not any(RUN_MARK in w for w in seg):
            continue
        run = _strip_prefix(seg)
        if run and os.path.basename(run[0]) in MARK_READERS:
            continue
        if run and os.path.basename(run[0]) in MARK_COPIERS and _mark_copy_read(run[1:]):
            continue
        if run and (_mark_archive_read(run) or _mark_awk_read(run)):
            continue
        if any(RUN_MARK in w and (_mark_path(w) or not re.search(r"\s", w)) for w in seg):
            return True   # a path to it, or a bare word naming it, given to a command that may write
    return False


def secret_refusal(why: str) -> bool:
    """Whether a refusal guards a secret (a credential read, an environment dump, the run marker): once
    such a call ran, the secret is in the agent's context (streamguard.must_stop)."""
    return any(w in why for w in (ENV_WHAT, CRED_WHAT, MARK_WHY))


def mark_refusal(tool: str, args: dict) -> str | None:
    """Refuse a task run's Bash command that may create, change, move or delete RUN_MARK (_mark_bash),
    or an edit or patch of that file. Its name inside quoted text or a read is no effect."""
    if tool == "Bash":
        return MARK_WHY if _mark_bash(str(args.get("command") or "")) else None
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        return MARK_WHY if _mark_path(path) else None
    if tool == "apply_patch":
        patch = "\n".join(str(v) for v in args.values()) if isinstance(args, dict) else str(args)
        return MARK_WHY if any(_mark_path(p) for p in PATCH_FILE_RE.findall(patch)) else None
    return None


def tool_refusal(tool: str, args: dict, cwd: str) -> str | None:
    """Why an untrusted run's Read, Grep or Glob call is refused: its path names a credential file
    outside the worktree (cred_path), else None."""
    path = str(args.get("file_path") or args.get("path") or "")
    if tool not in ("Read", "Grep", "Glob") or not path or not cred_path(path, cwd) or not active():
        return None
    return WHY(label=LABEL, what=CRED_WHAT).rstrip()
