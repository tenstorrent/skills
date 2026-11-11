# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""gh and pushes on a host with no GitHub login: the normal case on a lab host, where no GitHub login
or token is kept. Nobody is ever asked to log gh in there.

- gh calls (the gh wrapper prguard.main, pr-watch's watchers._spawn, `ttp ci`'s ciwait._gh) run on
  the machine named by the project's delivery.gh_route (an ssh alias of a machine where gh is logged
  in; unset by default) whenever this host's gh check (ghauth) does not say logged in: over ssh, with
  the repo passed explicitly (-R) and `gh api`'s {owner}/{repo} filled in here, after every local guard
  ran on the same arguments. Unset or down (ssh exit 255): a `gh api` GET of a public repo is read
  without a login (public); a write is held with HELD_RC, for its caller to retry once the route is
  back (held); other reads fail as before.
- A push to an https GitHub remote with no https login here goes over its ssh form
  (git@github.com:<owner>/<repo>.git) with the forwarded ssh agent: a `-c url...pushInsteadOf` on
  the git command line (push_argv), never an edit of the stored remote.
- The ask and notify gates refuse a text telling the user to log gh in on a remote host (login_ask).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request

KEY = "gh_route"         # delivery.gh_route
SETTING = f"delivery.{KEY}"
STATE = "gh-route.json"   # the route's last answer, host-wide, under the tt-project home
HELD_RC = 75             # a write held until the route is back: retry later
SSH_RC = 255             # ssh's own failure: the route is down
API = "https://api.github.com"
# The subcommands gh takes -R on: the routed call has no checkout to read the repo from.
_REPO_FLAG = {"pr", "run", "issue", "workflow", "release", "cache", "label", "secret", "variable", "ruleset"}
_GH_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_SSH_URL = re.compile(r"^(?:ssh://)?git@github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_WRITE_SUBS = {"create", "edit", "comment", "ready", "close", "reopen", "merge", "review", "rerun", "cancel",
               "delete", "lock", "unlock", "update-branch", "run", "enable", "disable", "set", "upload"}
_memo: dict = {}


def _cached(key, ttl: float, fn):
    hit = _memo.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _memo[key] = (time.time(), v)
    return v


def problem(v) -> str:
    """Why `v` cannot be delivery.gh_route, or ""."""
    from .remotecheck import HOST_RE
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or not HOST_RE.fullmatch(v.strip()) or v.strip() == "local":
        return f"{v!r}: {SETTING} is the ssh alias of a machine where gh is logged in"
    return ""


def route(cfg: dict | None = None, cwd: str | None = None) -> str:
    """The project's delivery.gh_route ("" when unset), from `cfg` or the project at `cwd`."""
    if cfg is None:
        def find():
            from .project import here
            try:
                p = here(cwd)
                return p.config() if p else {}
            except Exception:
                return {}
        cfg = _cached(("cfg", cwd or os.getcwd(), os.environ.get("TTP_PROJECT")), 60, find)
    v = ((cfg or {}).get("delivery") or {}).get(KEY)
    return v.strip() if isinstance(v, str) and not problem(v) else ""


def local_ok() -> bool:
    """Whether this host's gh is known to be logged in (the host-wide check, ghauth)."""
    from . import ghauth
    return ghauth.read().get("ok") is True


def repo_of(cwd: str | None) -> str:
    """owner/repo of the checkout at `cwd` (its origin's GitHub URL), or ""."""
    def read():
        try:
            r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd or None, capture_output=True,
                               text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return ""
        m = _GH_URL.match(r.stdout.strip()) or _SSH_URL.match(r.stdout.strip())
        return f"{m[1]}/{m[2]}" if r.returncode == 0 and m else ""
    return _cached(("repo", cwd or os.getcwd()), 300, read)


def _with_repo(args: list[str], repo: str) -> list[str]:
    """`args` naming `repo` explicitly: -R for the subcommands that take it, {owner}/{repo} filled in a
    `gh api` path. Args that name a repo already, or with nothing to name, stay as they are."""
    if not repo or any(a in ("-R", "--repo") or a.startswith("--repo=") for a in args):
        return list(args)
    if args[:1] == ["api"]:
        owner, name = repo.split("/", 1)
        return [a.replace("{owner}", owner).replace("{repo}", name) for a in args]
    if args[:1] and args[0] in _REPO_FLAG:
        cut = args.index("--") if "--" in args else len(args)
        return [*args[:cut], "-R", repo, *args[cut:]]
    return list(args)


def routed(args: list[str], cfg: dict | None = None, cwd: str | None = None) -> list[str] | None:
    """The ssh command line running `gh args` on delivery.gh_route, or None (no route, or gh here is
    logged in)."""
    alias = route(cfg, cwd)
    if not alias or local_ok():
        return None
    remote = shlex.join(["gh", *_with_repo(args, repo_of(cwd))])
    # A non-login shell there may lack the usual install folders on PATH.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, "--",
            f'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin" {remote}']


def command(real: str, args: list[str], cfg: dict | None = None, cwd: str | None = None) -> list[str]:
    """What runs for `gh args`: the routed ssh command, else the real gh here."""
    return routed(args, cfg, cwd) or [real, *args]


def is_write(args: list[str]) -> bool:
    """Whether `gh args` changes something on GitHub."""
    if args[:1] == ["api"]:
        method = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), "")
        method = method or next((a.split("=", 1)[1] for a in args if a.startswith("--method=")), "")
        if method:
            return method.upper() != "GET"
        return any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args) and "graphql" not in args
    return len(args) > 1 and args[1] in _WRITE_SUBS


def held(args: list[str], cfg: dict | None = None, cwd: str | None = None) -> str:
    """Why a write is held here (no login, and no route), or ""."""
    if not is_write(args) or local_ok():
        return ""
    return (f"held: gh has no login on this host and {SETTING} " + ("is down" if route(cfg, cwd) else "is unset")
            + f"; `gh {' '.join(args[:2])}` was not sent. Retry once {SETTING} works (exit {HELD_RC}).")


def _logged_out() -> bool:
    from . import ghauth
    return ghauth.down() is not None


def public(args: list[str], cwd: str | None = None, timeout: float = 20) -> tuple[int, str] | None:
    """(0, body) of a `gh api` GET of a REST path read with no login (a public repo), else None."""
    if args[:1] != ["api"] or len(args) < 2 or is_write(args) or "graphql" in args:
        return None
    rest = [a for a in _with_repo(args, repo_of(cwd))[1:] if a not in ("-X", "--method", "GET", "--method=GET")]
    if len(rest) != 1 or rest[0].startswith("-") or "{" in rest[0]:
        return None   # --jq, --paginate, headers, fields: not mirrored here
    path = rest[0]
    req = urllib.request.Request(f"{API}/{path.lstrip('/')}", headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https host)
            return 0, r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wanted(args: list[str], cfg: dict | None = None, cwd: str | None = None) -> bool:
    """Whether `gh args` takes the no-login path here: routed, else held or read publicly."""
    return not local_ok() and bool(route(cfg, cwd) or _logged_out())


def run(real: str, args: list[str], input: bytes | None = None, cfg: dict | None = None,
        cwd: str | None = None) -> int:
    """`gh args` with no login here, its output on ours: on the route; route unset or down, a write is
    held (HELD_RC), a public `gh api` read answered without a login, any other read run here as before."""
    import sys
    via = routed(args, cfg, cwd)
    if via:
        rc = subprocess.run(via, input=input, cwd=cwd).returncode
        mark(rc != SSH_RC)
        if rc != SSH_RC:
            return rc
    if why := held(args, cfg, cwd):
        print(f"gh (tt-project): {why}", file=sys.stderr)
        return HELD_RC
    if got := public(args, cwd):
        sys.stdout.write(got[1])
        return got[0]
    return subprocess.run([real, *args], input=input, cwd=cwd).returncode


def mark(ok: bool, home=None) -> None:
    """Record the route's last answer, host-wide (STATE), for the gh-auth alert."""
    from . import ghauth
    try:
        from .project import write_json
        f = ghauth._home(home) / STATE
        f.parent.mkdir(parents=True, exist_ok=True)
        write_json(f, {"ok": ok, "at": time.time()}, 0o600)
    except OSError:
        pass


def works(home=None, within_s: float = 3600) -> bool:
    """Whether a routed gh call succeeded here within `within_s` and none failed since."""
    from . import ghauth
    try:
        st = json.loads((ghauth._home(home) / STATE).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(st, dict) and st.get("ok") is True and time.time() - float(st.get("at") or 0) < within_s


def quiet(error: str) -> str:
    """gh's own error with its 'run gh auth login' advice cut: nobody logs in on a lab host."""
    out = re.sub(r"[\s.:;,-]*(?:to (?:log in|get started)[^:]*:?\s*|please run:?\s*|run:?\s*)*`?gh auth login`?\.?",
                 "", str(error or ""), flags=re.I).strip()
    return out or "not logged in"


def down_text(host: str, when: str, error: str, cfg: dict | None = None) -> str:
    """The harness condition while gh has no login here and no route works: what is skipped, the
    setting that routes it; no login step for anyone."""
    how = (f"{SETTING} is set but its last gh call failed" if route(cfg) else f"{SETTING} is unset")
    return (f"gh has no login on {host} (since {when}; {quiet(error)}), as is normal for a lab host, and {how}. "
            f"PR watching runs degraded on public reads only; CI waits, PR edits, comments and other gh writes "
            f"of projects here are held and retried once a route works. To route them, set {SETTING} to the "
            f"ssh alias of a machine where gh is logged in. Pushes go over ssh with the forwarded agent.")


# --- pushes ---------------------------------------------------------------------------------

def _https_login(repo: str | None) -> bool:
    """Whether git has a credential helper for https://github.com at `repo` that can answer: none, or
    gh's own while gh here is logged out, is no login."""
    def read():
        try:
            r = subprocess.run(["git", *(["-C", repo] if repo else []), "config", "--get-urlmatch",
                                "credential.helper", "https://github.com"], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return True   # unknown: leave the push as it is
        return r.stdout.strip()
    helper = _cached(("helper", repo), 300, read)
    if helper is True:
        return True
    return bool(helper) and not ("gh auth git-credential" in helper and not local_ok())


def value_of(v) -> str:
    """A `delivery.gh_route` value: an ssh alias, or "" for none."""
    v = "" if v is None else str(v).strip()
    if v and (why := problem(v)):
        raise ValueError(why)
    return v


def push_argv(argv: list | tuple, cwd: str | None = None) -> list | tuple:
    """`argv` (a git command line) with https GitHub pushes sent over their ssh form when there is no
    https login here; any other command as it is."""
    from .gitmemo import git_args
    try:
        args = git_args(argv)
    except Exception:
        return argv
    if not argv or os.path.basename(str(argv[0])) != "git" or not args or args[0] != "push":
        return argv
    repo = next((str(argv[i + 1]) for i, a in enumerate(argv[:-1]) if a == "-C"), cwd)
    if _https_login(repo):
        return argv
    return [argv[0], "-c", "url.git@github.com:.pushInsteadOf=https://github.com/", *argv[1:]]


def push_hint(err: str) -> str:
    """A failed push's note when it failed for want of a GitHub login, else ""."""
    if not re.search(r"could not read Username|Permission denied \(publickey\)|terminal prompts disabled", err or ""):
        return ""
    return ("no GitHub login here (none is kept on lab hosts; gh calls go on delivery.gh_route): the push went "
            "over ssh (git@github.com:...), which needs a forwarded ssh agent with a GitHub key; it waits and "
            "retries. Nobody logs in on this host.")


# --- the ask gate ---------------------------------------------------------------------------

_LOGIN_RE = re.compile(r"\bgh\s+auth\s+(?:login|refresh)\b|\blog\s+(?:gh|github(?:\s+cli)?)\s+(?:back\s+)?in\b|"
                       r"\b(?:gh|github(?:\s+cli)?)\s+(?:login|needs?\s+(?:a\s+)?(?:login|logging\s+in))\b|"
                       r"\bgh\s+(?:is\s+)?logged\s+out\b.{0,80}\b(?:run|log\s+in|login)\b", re.I | re.S)
_OWN_RE = re.compile(r"\byour\s+(?:own\s+)?(?:laptop|mac|desktop|workstation|own\s+machine)\b", re.I)


def login_ask(text: str) -> str:
    """Why an ask or notify telling the user to log gh in on a remote host is refused, or ""."""
    if not _LOGIN_RE.search(text or "") or _OWN_RE.search(text or ""):
        return ""
    return (f"no GitHub login or token is kept on lab hosts, and nobody is asked to log gh in there: gh calls go "
            f"through {SETTING} (an ssh alias of a machine where gh is logged in), else read-only checks run and "
            f"writes wait. Set the route or report what was skipped as a harness condition")
