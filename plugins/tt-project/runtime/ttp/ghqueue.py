# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""gh through the user's workstation (delivery.gh_route "workstation"), for a lab host with no gh login
whose user's workstation takes no inbound connections. The rule: when gh only works on the user's
workstation, the workstation pulls and runs the queue; never the other way round.

- Here (the box): a gh call the harness cannot run (no login) goes in the host's gh outbox (OUTBOX under
  the tt-project home; each entry names its project and task) with its arguments, repo and a dedupe key,
  and exits HELD_RC. Its caller (a worker's gh, pr-watch, `ttp ci`) retries as for any held call; the
  same call again gets the workstation's answer once it is in: a read's while fresh (READ_FRESH_S), once;
  a write's for WRITE_KEEP_S, so a retried write never posts twice.
- There (the workstation): `ttp bridge gh-pull` logs in to the box (`ttp gh-queue take`), runs each call
  with its own gh as an argument list (never a shell string), only if allowed() passes there too, and
  puts the answers back (`ttp gh-queue put`). `"gh_pull": true` on a box in the bridge config keeps it
  running as a service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import aitag
from .ghroute import HELD_RC, SETTING, WORKSTATION, _with_repo, is_write, local_inputs, mark, repo_of

OUTBOX = "gh-outbox"
READ_FRESH_S = 600            # a read's answer older than this is asked again
WRITE_KEEP_S = 86400          # a write's answer is kept this long: a retry gets it, never a second post
CLAIM_S = 900                 # a taken call with no answer by then is taken again
PENDING_S = {False: 3600, True: 7 * 86400}   # a call nobody took by then is dropped (read, write)
BATCH = 20
OUT_MAX = 4 << 20
GH_TIMEOUT_S = 120
REMOTE = 'PATH="$HOME/.local/bin:$PATH" ttp gh-queue'   # setup's default shim folder, off a non-login PATH
_KEY = re.compile(r"[0-9a-f]{32}")
_READS = {("pr", "view"), ("pr", "list"), ("pr", "checks"), ("issue", "view"), ("issue", "list")}
_POSTS = {("pr", "comment"), ("issue", "comment"), ("pr", "create")}
_API_FIELDS = {"-f", "-F", "--field", "--raw-field"}
_BUNDLED = re.compile(r"-[A-Za-z]\S+")   # -XPOST, -cw, -bText: a bundle or an attached value
_NEVER = {"--web", "-w", "--editor", "-e", "--delete-last"}   # opens a browser or editor there, or deletes
# The only flags each of these upkeep calls may carry (every flag takes a value; anything else is refused)
_EDIT_FLAGS = {"--title": "--title", "-t": "--title", "--body": "--body", "-b": "--body", "--body-file": "--body",
               "-F": "--body", "--repo": "--repo", "-R": "--repo"}
_CLOSE_FLAGS = {"--comment": "--comment", "-c": "--comment", "--repo": "--repo", "-R": "--repo"}
_REPLY_PATH = re.compile(r"/?repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls/[0-9]+/comments/[0-9]+/replies")
_REPLY_FLAGS = {"-X": "method", "--method": "method", "-f": "field", "--raw-field": "field"}


def _body(argv: list[str], stdin: bytes) -> str | None:
    """The post's body: inline (--body/-b) or on stdin (--body-file -/-F -), else None."""
    body = None
    for i, a in enumerate(argv):
        name, eq, val = a.partition("=")
        if name in ("--body", "-b", "--body-file", "-F"):
            v = val if eq else (argv[i + 1] if i + 1 < len(argv) else None)
            body = (stdin.decode("utf-8", "replace") if v == "-" else None) if name in ("--body-file", "-F") else v
    return body


def _parse(rest: list[str], flags: dict) -> tuple[list[str], dict[str, list[str]], str]:
    """(positionals, {canonical flag: values}, why refused) of `rest` against an allowlist of flags that
    each take a value."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        a = rest[i]
        if not a.startswith("-") or a == "-":
            pos.append(a)
            i += 1
            continue
        name, eq, val = a.partition("=")
        if name not in flags:
            return pos, opts, f"`{name}` is not allowed here"
        if not eq:
            if i + 1 >= len(rest):
                return pos, opts, f"`{name}` has no value"
            val, i = rest[i + 1], i + 1
        opts.setdefault(flags[name], []).append(val)
        i += 1
    if any(len(v) > 1 for v in opts.values()):
        return pos, opts, "a flag is given twice"
    return pos, opts, ""


def _tagged(text: str | None) -> bool:
    return text is not None and aitag.ends_with_tag(text, aitag.DEFAULT)


def _upkeep(argv: list[str], stdin: bytes) -> str:
    """Why `gh pr edit` (title and body only) or `gh pr close` (an optional comment) is refused, or ""."""
    edit = argv[1] == "edit"
    pos, opts, why = _parse(argv[2:], _EDIT_FLAGS if edit else _CLOSE_FLAGS)
    if why:
        return f"`gh pr {argv[1]}`: {why}" + (" (only --title, --body or --body-file -)" if edit else
                                                " (only an optional --comment; never --delete-branch)")
    if len(pos) != 1:
        return f"`gh pr {argv[1]}` names exactly one PR"
    if edit:
        if not set(opts) & {"--title", "--body"}:
            return "`gh pr edit` runs only to change the title or body"
        body = opts.get("--body", [None])[0]
        is_file = any(a.split("=", 1)[0] in ("--body-file", "-F") for a in argv[2:])
        if is_file:
            body = stdin.decode("utf-8", "replace") if body == "-" else None
        if "--body" in opts and not _tagged(body):
            return f"the PR body (--body or --body-file -) does not end with the AI-authorship tag {aitag.DEFAULT!r}"
        return ""
    if "--comment" in opts and not _tagged(opts["--comment"][0]):
        return f"the close comment does not end with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


def _reply(argv: list[str]) -> str | None:
    """None unless `gh api` posts to a PR review comment's replies; then why it is refused, or "": one
    `-X POST` to exactly repos/<o>/<r>/pulls/<n>/comments/<id>/replies with one raw field, body, ending
    with the AI tag."""
    if not any(_REPLY_PATH.fullmatch(a) for a in argv[1:]):
        return None
    pos, opts, why = _parse(argv[1:], _REPLY_FLAGS)
    if why:
        return f"a review-comment reply: {why} (only -X POST and -f body=...)"
    if len(pos) != 1 or [m.upper() for m in opts.get("method", [])] != ["POST"]:
        return "a review-comment reply is one `-X POST` to one replies path"
    field = opts.get("field", [""])[0]
    if not field.startswith("body=") or not _tagged(field[5:]):
        return f"a review-comment reply carries one field, body, ending with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


def own_draft(gh: str, argv: list[str]) -> str:
    """Why the workstation must not close the PR `gh pr close argv` names, or "": only a draft its own gh
    user opened, read just before the close."""
    pos, opts, why = _parse(argv[2:], _CLOSE_FLAGS)
    if why or len(pos) != 1:
        return why or "no single PR named"
    repo = [f"--repo={opts['--repo'][0]}"] if "--repo" in opts else []
    try:
        pr = subprocess.run([gh, "pr", "view", pos[0], *repo, "--json", "isDraft,author"], capture_output=True,
                            timeout=GH_TIMEOUT_S)
        me = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, timeout=GH_TIMEOUT_S)
        if pr.returncode or me.returncode:
            return "could not read the PR or this gh user to check it is our own draft"
        v = json.loads(pr.stdout)
        login = me.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "could not read the PR or this gh user to check it is our own draft"
    if not isinstance(v, dict) or v.get("isDraft") is not True:
        return "`gh pr close` closes only a draft PR, and this one is not a draft"
    author = v.get("author") if isinstance(v.get("author"), dict) else {}
    if not login or str(author.get("login") or "").lower() != login.lower():
        return "`gh pr close` closes only a draft this workstation's gh user opened, and this PR is someone else's"
    return ""


def allowed(argv, stdin: bytes = b"") -> str:
    """Why the workstation must not run `gh argv`, or "": the allowlist, checked on both sides. `gh api`
    GET reads, pr view/list/checks, issue view/list, pr and issue comments, `pr create --draft`, a reply
    to a PR review comment (`gh api -X POST .../pulls/<n>/comments/<id>/replies -f body=...`), `pr edit`
    of the title or body only and `pr close` (the workstation also checks the PR is its own draft), each
    post, body or comment ending with the AI-authorship tag; never `pr ready`, a merge, a reviewer, a
    label or anything else."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        return "not a list of plain arguments"
    if bad := next((a for a in argv if _BUNDLED.fullmatch(a)), None):
        return f"`{bad}`: short options go one per word, values apart (so none hides a method, field or --web)"
    if argv[0] == "api":
        if (why := _reply(argv)) is not None:
            return why
        get = any(a.upper() == "--METHOD=GET" or (a in ("-X", "--method") and nxt.upper() == "GET")
                  for a, nxt in zip(argv, [*argv[1:], ""]))
        names = [a.split("=", 1)[0] for a in argv if a.startswith("-")]
        if (is_write(argv) or any(a.lstrip("/").startswith("graphql") for a in argv[1:] if not a.startswith("-"))
                or "--input" in names or (not get and set(names) & _API_FIELDS)
                or any(a.startswith("--method=") and a.upper() != "--METHOD=GET" for a in argv)
                or any("override" in a.lower() for a in argv)):
            return "`gh api` runs GET reads only"
        return ""
    if bad := next((a for a in argv if a.split("=", 1)[0] in _NEVER), None):
        return f"`{bad}` is not run there"
    pair = tuple(argv[:2])
    if pair in _READS:
        return ""
    if pair in (("pr", "edit"), ("pr", "close")):
        return _upkeep(argv, stdin)
    if pair not in _POSTS:
        return f"`gh {' '.join(argv[:2])}` is not on the workstation's allowlist"
    if pair == ("pr", "create") and not any(a in ("--draft", "-d") for a in argv):
        return "`gh pr create` runs only with --draft"
    body = _body(argv, stdin)
    if body is None or not aitag.ends_with_tag(body, aitag.DEFAULT):
        return f"the post's body (--body or --body-file -) does not end with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


def outbox(home=None) -> Path:
    from . import ghauth
    return ghauth._home(home) / OUTBOX


def _load(f: Path) -> dict | None:
    try:
        v = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _write(f: Path, v: dict) -> None:
    from .project import write_json
    write_json(f, v, 0o600)


def prune(d: Path, now: float | None = None) -> None:
    """Drop stale answers and calls nobody took."""
    now = now or time.time()
    for f in d.glob("*.*"):
        if f.suffix not in (".q", ".done"):
            continue
        v = _load(f) or {}
        age = now - float(v.get("at") or v.get("queued_at") or 0)
        limit = (WRITE_KEEP_S if v.get("write") else READ_FRESH_S) if f.suffix == ".done" else PENDING_S[bool(v.get("write"))]
        if age > limit:
            f.unlink(missing_ok=True)


def _project(cwd: str | None) -> str:
    if os.environ.get("TTP_PROJECT"):
        return Path(os.environ["TTP_PROJECT"]).name   # a name, never this host's paths
    try:
        from .project import here
        p = here(cwd)
        return p.name if p else ""
    except Exception:
        return ""


def enqueue(args: list[str], input: bytes | None = None, cwd: str | None = None, home=None) -> int:
    """`gh args` here with no login: the workstation's answer if it is in (its output on ours), else the
    call queued once (HELD_RC)."""
    sent, data, why = local_inputs(args, input, cwd)
    if why:
        print(f"gh (tt-project): held: {why}; it was not queued for the workstation (exit {HELD_RC}).", file=sys.stderr)
        return HELD_RC
    repo = repo_of(cwd)
    sent, data = _with_repo(sent, repo), data or b""
    if why := allowed(sent, data):
        print(f"gh (tt-project): refused: {why} ({SETTING} is {WORKSTATION}).", file=sys.stderr)
        return 1
    key = hashlib.sha256(json.dumps([repo, sent, base64.b64encode(data).decode()]).encode()).hexdigest()[:32]
    d = outbox(home)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    prune(d)
    done = d / f"{key}.done"
    if (got := _load(done)) is not None:
        sys.stdout.write(str(got.get("stdout") or ""))
        sys.stderr.write(str(got.get("stderr") or ""))
        if not got.get("write"):
            done.unlink(missing_ok=True)   # a read is answered once; the next one asks again
        mark(True)
        return int(got.get("rc", 1)) if str(got.get("rc", 1)).lstrip("-").isdigit() else 1
    if not (d / f"{key}.q").exists() and not (d / f"{key}.taken").exists():
        _write(d / f"{key}.q", {"key": key, "argv": sent, "repo": repo, "project": _project(cwd),
                                "task": os.environ.get("TTP_TASK", ""), "write": is_write(sent),
                                "stdin": base64.b64encode(data).decode(), "queued_at": time.time()})
    print(f"gh (tt-project): `gh {' '.join(args[:2])}` is queued for the user's workstation ({SETTING} is "
          f"{WORKSTATION}); the same call again returns its answer once in (exit {HELD_RC}).", file=sys.stderr)
    return HELD_RC


def take(home=None, now: float | None = None) -> list[dict]:
    """Claim up to BATCH queued calls (and any taken more than CLAIM_S ago with no answer)."""
    now = now or time.time()
    d = outbox(home)
    if not d.is_dir():
        return []
    prune(d, now)
    out = []
    for f in sorted([*d.glob("*.q"), *d.glob("*.taken")], key=lambda f: f.stat().st_mtime if f.exists() else 0):
        if len(out) >= BATCH or not _KEY.fullmatch(f.stem):
            continue
        if f.suffix == ".taken" and now - f.stat().st_mtime < CLAIM_S:
            continue
        if f.suffix == ".taken" and (_load(f) or {}).get("write"):   # it may have posted: never sent twice
            _write(d / f"{f.stem}.done", {"rc": 1, "write": True, "stdout": "", "at": now, "stderr":
                   "the workstation took this gh write but sent no answer; it was not sent again (it may have posted)"})
            f.unlink(missing_ok=True)
            continue
        claim = d / f"{f.stem}.taken"
        try:
            os.rename(f, claim)
            os.utime(claim)
        except OSError:
            continue   # another puller took it
        if (v := _load(claim)) is not None:
            out.append({k: v.get(k) for k in ("key", "argv", "repo", "project", "task", "stdin")})
    return out


def put(results, home=None) -> int:
    """Store the workstation's answers to calls taken here; returns how many were stored."""
    d, n = outbox(home), 0
    for r in results if isinstance(results, list) else []:
        key = r.get("key") if isinstance(r, dict) else None
        if not isinstance(key, str) or not _KEY.fullmatch(key) or (v := _load(d / f"{key}.taken")) is None:
            continue
        rc = r.get("rc")
        _write(d / f"{key}.done", {"rc": rc if isinstance(rc, int) else 1, "write": bool(v.get("write")),
                                   "stdout": str(r.get("stdout") or "")[:OUT_MAX],
                                   "stderr": str(r.get("stderr") or "")[:OUT_MAX], "at": time.time()})
        (d / f"{key}.taken").unlink(missing_ok=True)
        n += 1
    return n


# --- the workstation --------------------------------------------------------------------------------

def answer(entry: dict, gh: str | None = None) -> dict | None:
    """Run one taken call with this computer's gh, if allowed() here; its answer."""
    key = entry.get("key")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    try:
        stdin = base64.b64decode(entry.get("stdin") or "", validate=True)
    except (ValueError, TypeError):
        return {"key": key, "rc": 1, "stdout": "", "stderr": "refused on the workstation: stdin is not base64"}
    argv = entry.get("argv")
    if why := allowed(argv, stdin):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused on the workstation: {why}"}
    gh = gh or shutil.which("gh", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin") or "gh"
    if argv[:2] == ["pr", "close"] and (why := own_draft(gh, argv)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused on the workstation: {why}"}
    try:
        p = subprocess.run([gh, *argv], input=stdin, capture_output=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"key": key, "rc": 124, "stdout": "", "stderr": f"gh timed out after {GH_TIMEOUT_S}s on the workstation"}
    except OSError as e:
        return {"key": key, "rc": 127, "stdout": "", "stderr": f"gh could not run on the workstation: {e}"}
    return {"key": key, "rc": p.returncode, "stdout": p.stdout.decode("utf-8", "replace"),
            "stderr": p.stderr.decode("utf-8", "replace")}


def pull(box: str, gh: str | None = None) -> str:
    """One round on `box`: take its queue, answer each call here, put the answers back."""
    from .bridge import _box
    r = _box(box, f"{REMOTE} take", timeout=60)
    if r.returncode != 0:
        return f"{box}: could not take the gh queue: {(r.stderr.strip() or f'exit {r.returncode}')[:200]}"
    try:
        entries = json.loads(r.stdout or "[]")
    except ValueError:
        return f"{box}: the gh queue's answer is not JSON"
    results = [a for e in entries if isinstance(e, dict) and (a := answer(e, gh))] if isinstance(entries, list) else []
    if not results:
        return f"{box}: no gh calls queued"
    p = _box(box, f"{REMOTE} put", timeout=60, inp=json.dumps(results))
    if p.returncode != 0:
        return f"{box}: ran {len(results)} gh call(s) but could not put the answers back: {p.stderr.strip()[:200]}"
    refused = sum(1 for a in results if a["stderr"].startswith("refused on the workstation"))
    return f"{box}: answered {len(results)} gh call(s)" + (f", {refused} refused" if refused else "")


def service_argv(box: str) -> list[str]:
    """The workstation service that keeps pulling `box`'s queue."""
    return ["/usr/bin/env", f"PYTHONPATH={Path(__file__).resolve().parent.parent}", sys.executable, "-m", "ttp",
            "bridge", "gh-pull", box, "--loop"]


def queue_argv(args: list[str]) -> list[str]:
    """The command a caller runs for `gh args` in workstation mode (ghroute.routed)."""
    return ["/usr/bin/env", f"PYTHONPATH={Path(__file__).resolve().parent.parent}", sys.executable, "-m", "ttp",
            "gh-queue", "run", "--", *args]
