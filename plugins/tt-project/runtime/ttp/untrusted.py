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
  git names, tags and deletes too, and the hook refuses a raw `git push` naming the push branch;
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
  So are environment dumps (printenv, a bare env, export or set), which print tokens.
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

LABEL = "untrusted_input"   # modelpick.UNTRUSTED
WRITE_LABEL = "untrusted_write"
ENV = "TTP_UNTRUSTED_INPUT"
READ_VERBS = {"view", "list", "ls", "diff", "checks", "status", "download", "watch", "clone", "checkout",
              "verify", "search"}
READ_GROUPS = {"search", "status", "help", "version", "completion", "browse"}
CRED_DIRS = (".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".password-store", ".config/gh",
             ".config/gcloud", ".local/share/keyrings")
CRED_FILES = {".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass", ".my.cnf", ".env",
              "auth.json", "hosts.yml", "hosts.yaml", "shadow", "environ"}
CRED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".gpg", ".asc")
# token and secret only as a whole word (api_token.txt, access-tokens.json, accessToken), so a model's
# tokenizer.json is no credential
CRED_NAME_RE = re.compile(r"token(?!i[sz])|secret|credential"   # anywhere in the name, but not a tokenizer
                          r"|passw(or)?d|api[_-]?key|^id_(?!.*\.pub$)|^\.env\.", re.I)
WHY = ("this task reads untrusted input (label {label}), which may carry planted instructions, so its run "
       "{what}. ").format


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


def active(env: dict | None = None) -> bool:
    """This run's task reads untrusted input."""
    env = os.environ if env is None else env
    return env.get(ENV) == "1" or LABEL in _labels(*_task(env))


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


def push_refs(env: dict, name=lambda ref: ref) -> list[str]:
    """The refs an untrusted run's push may update (gitguard's pre-push hook), or [] when the run is no
    such run. `name` maps delivery.push_branch to its branch (gitguard._branch_name)."""
    if not active(env):
        return []
    branches = (own_branch(env), name(b) if (b := push_branch(raw=True)) else "")
    return [f"refs/heads/{b}" for b in dict.fromkeys(branches) if b] or ["(none)"]


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
    from .aitag import CURL_DATA, WGET_DATA, _data_options
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
        elif prog == "curl" and (m := re.fullmatch(r"-[A-Za-z0-9#:]+X(\S*)", a)):   # a short-flag cluster: -sX DELETE
            method = m.group(1) or nxt
        elif prog == "curl" and (a in ("-T", "--upload-file") or a.startswith(("--upload-file=", "-T"))):
            return True
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
    return name in CRED_FILES or name.endswith(CRED_SUFFIXES) or bool(CRED_NAME_RE.search(name))


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


def _env_dump(argv: list[str]) -> bool:
    name = os.path.basename(argv[0])
    if name == "printenv":
        return True
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
        return WHY(label=LABEL, what="never dumps its environment, which holds tokens") + \
            "Print one harmless variable by name with `echo \"$NAME\"`"
    if any(cred_path(w, cwd) for s in segments for w in _words(s)) or any(
            _find_refusal(s, cwd) for s in segments if s and os.path.basename(s[0]) == "find"):
        return WHY(label=LABEL, what="never reads credential, token or secret files outside its worktree") + \
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


def tool_refusal(tool: str, args: dict, cwd: str) -> str | None:
    """Why an untrusted run's Read, Grep or Glob call is refused: its path names a credential file
    outside the worktree (cred_path), else None."""
    path = str(args.get("file_path") or args.get("path") or "")
    if tool not in ("Read", "Grep", "Glob") or not path or not cred_path(path, cwd) or not active():
        return None
    return WHY(label=LABEL, what="never reads credential, token or secret files outside its worktree").rstrip()
