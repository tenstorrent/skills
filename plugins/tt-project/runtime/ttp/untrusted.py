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
  write carries the label `untrusted_write` (WRITE_LABEL), set by whoever writes the task, never
  read from its wording; pushes and credential reads stay refused with it;
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
Every task run, marked or not, is refused a Bash command naming the run's RUN_MARK file and an edit
of that file (mark_refusal): removing it would lift these guards for the rest of the run and keep the
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


def guards_live() -> bool:
    """Whether this harness ships the untrusted-input guards (for routing such runs to other models)."""
    return True


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
        f"Report what you found in the hand-off; a task that must post carries the label {WRITE_LABEL}")


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


MARK_WHY = "this run's untrusted-input file records the untrusted text it read; leave it as it is"


def secret_refusal(why: str) -> bool:
    """Whether a refusal guards a secret (a credential read, an environment dump, the run marker): once
    such a call ran, the secret is in the agent's context (streamguard.must_stop)."""
    return any(w in why for w in (ENV_WHAT, CRED_WHAT, MARK_WHY))


def mark_refusal(tool: str, args: dict) -> str | None:
    """Refuse a task run's Bash command naming RUN_MARK, or an edit or patch of that file."""
    if tool == "Bash":
        return MARK_WHY if RUN_MARK in str(args.get("command") or "") else None
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        return MARK_WHY if os.path.basename(path.rstrip("/")) == RUN_MARK else None
    if tool == "apply_patch":
        return MARK_WHY if RUN_MARK in json.dumps(args) else None
    return None


def tool_refusal(tool: str, args: dict, cwd: str) -> str | None:
    """Why an untrusted run's Read, Grep or Glob call is refused: its path names a credential file
    outside the worktree (cred_path), else None."""
    path = str(args.get("file_path") or args.get("path") or "")
    if tool not in ("Read", "Grep", "Glob") or not path or not cred_path(path, cwd) or not active():
        return None
    return WHY(label=LABEL, what=CRED_WHAT).rstrip()
