# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's own PR body rules as a model-free guard (delivery.pr_body_check, off by default).

When the project names a check, the worker `gh` guard (prguard) runs it on the PR body before
`gh pr create`, and before `gh pr edit` when the edit sets the body (`-b/--body`, `-F/--body-file`).
The check is a shell command run in the caller's directory within its timeout (boundedchild). It
gets the body on stdin and as a temp file whose path is in TTP_PR_BODY_FILE, the repo (`owner/name`,
"" when unknown) in TTP_PR_REPO and `create` or `edit` in TTP_PR_ACTION. Exit 0 lets gh run; any
other exit refuses the call and shows the check's output, trimmed.

The setting is a command string, or {"run", "timeout_s", "repos", "skip_repos"}: `repos` (globs on
`owner/name`, case-insensitive) limits the check to the repos it matches, `skip_repos` leaves
matching repos out. A repo that cannot be told is checked. A body gh would get some other way
(`--fill`, `--editor`, `--web`, a template, a prompt) cannot be read beforehand: while a check
applies, such a call is refused and asked to pass `--body-file`. A check that cannot run (no such
command, a timeout) refuses too, naming the setting and the error.

A body written through `gh api` is checked too (api_problem): a REST POST or PATCH to
repos/<owner>/<repo>/pulls[/<n>] carrying a `body` field (`-f`, `-F`, `-F body=@file`) or an --input
JSON "body", and a GraphQL createPullRequest or updatePullRequest whose body is inline in the query or
in a variable (`-f`/`-F` fields, --input "variables"). Its repo comes from the REST path; a GraphQL
call's repo is not told (checked). A body that cannot be read (stdin gh would get, an unreadable file, a
variable not given) refuses the call. `repositories/<id>/...` paths are read like `repos/<owner>/<repo>/...`
(their repo is not told).

GitHub treats a PR as an issue, so an issue body edit may rewrite a PR's body: a REST POST or PATCH to
repos/<owner>/<repo>/issues/<n> with a body, `gh issue edit <n>` with `--body` or `--body-file`
(issue_problem) and a GraphQL updateIssue. Each is checked when its target is a PR, looked up with the
real gh (`gh api .../pulls/<n>` answers, a GraphQL node's __typename is PullRequest), or when the lookup
cannot tell (offline, no auth, a timeout). A plain issue (a 404, an Issue node) is not checked.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import subprocess
import tempfile
import textwrap

KEY = "pr_body_check"
SETTING = f"delivery.{KEY}"
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0
CANNOT_RUN = (126, 127)   # the shell found no such command or could not run it: a setting fault
SHOW_LINES, SHOW_CHARS = 20, 2000
BODY_FILE_HOW = "write the body to a file and pass it with --body-file <path>"
LOOKUP_TIMEOUT_S = 20.0   # telling a PR from a plain issue; a lookup that does not answer in time checks the body

# gh pr create / gh pr edit options that take a value (the others are flags), so a value is never
# read as an option: `-t -b` sets the title to "-b".
CREATE_VALUES = {"-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head",
                 "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-T",
                 "--template", "-t", "--title", "-R", "--repo", "--recover"}
EDIT_VALUES = {"--add-assignee", "--add-label", "--add-project", "--add-reviewer", "--attach", "-B", "--base",
               "-b", "--body", "-F", "--body-file", "-m", "--milestone", "--remove-assignee", "--remove-label",
               "--remove-project", "--remove-reviewer", "-t", "--title", "-R", "--repo"}


def parse(v) -> dict | None:
    """{"run", "timeout_s", "repos", "skip_repos"} from the setting; None when unset or empty;
    ValueError when malformed."""
    if v is None or v is False or v == "" or v == {}:
        return None
    if isinstance(v, str):
        return {"run": v, "timeout_s": DEFAULT_TIMEOUT_S, "repos": [], "skip_repos": []} if v.strip() else None
    if not isinstance(v, dict):
        raise ValueError('not a command string or a {"run", "timeout_s", "repos", "skip_repos"} object')
    if extra := set(v) - {"run", "timeout_s", "repos", "skip_repos"}:
        raise ValueError(f"unknown key(s) {', '.join(sorted(map(str, extra)))}")
    run, t = v.get("run"), v.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(run, str) or not run.strip():
        raise ValueError('"run" is not a non-empty command string')
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 < t <= MAX_TIMEOUT_S:
        raise ValueError(f'"timeout_s" is not a number of seconds in (0, {MAX_TIMEOUT_S:.0f}]')
    globs = {}
    for k in ("repos", "skip_repos"):
        g = v.get(k) or []
        if not isinstance(g, list) or not all(isinstance(x, str) and x.strip() for x in g):
            raise ValueError(f'"{k}" is not a list of owner/name glob strings')
        globs[k] = [x.strip().lower() for x in g]
    return {"run": run, "timeout_s": float(t), **globs}


def problems(v) -> list[str]:
    try:
        parse(v)
    except ValueError as e:
        return [f"{SETTING}: {e}"]
    return []


def _scan(args: list[str], values: set[str]) -> tuple[dict[str, str], list[str]]:
    """The last value of each option in `values` that `args` sets (under its own spelling: `-b` and
    `--body` apart), and the positionals. Joined short values (`-bx`) and `--opt=x` count."""
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in values:
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else None
                if val is not None:
                    opts[name] = val
        elif a.startswith("-") and a != "-":
            for j, c in enumerate(a[1:], 1):   # a cluster of flags, up to the first that takes a value
                if f"-{c}" in values:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = args[i] if i < len(args) else None
                    if val is not None:
                        opts[f"-{c}"] = val
                    break
        else:
            pos.append(a)
        i += 1
    return opts, pos


def _repo_name(text: str) -> str:
    """`owner/name` from a -R value (`owner/name`, `host/owner/name`) or a PR or repo URL."""
    text = re.sub(r"^[a-z]+://", "", (text or "").strip(), flags=re.I).split("/pull/")[0].rstrip("/")
    parts = [x for x in text.split("/") if x]
    if len(parts) >= 3 and "." in parts[0]:
        parts = parts[1:]
    return "/".join(parts[:2]).removesuffix(".git").lower() if len(parts) >= 2 else ""


def _repo(opts: dict, pos: list[str], real: str | None) -> str:
    for k in ("--repo", "-R"):
        if opts.get(k):
            return _repo_name(opts[k])
    url = next((x for x in pos if re.match(r"https?://[^/\s]+/[^/\s]+/[^/\s]+/pull/\d+", x, re.I)), None)
    if url:
        return _repo_name(url)
    if os.environ.get("GH_REPO"):
        return _repo_name(os.environ["GH_REPO"])
    if real:
        from .prguard import _gh
        return _repo_name(_gh(real, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"))
    return ""


def applies(conf: dict, repo: str) -> bool:
    """Whether the check covers `repo` (`owner/name`, "" when unknown: always covered)."""
    if not repo:
        return True
    if conf["repos"] and not any(fnmatch.fnmatchcase(repo, g) for g in conf["repos"]):
        return False
    return not any(fnmatch.fnmatchcase(repo, g) for g in conf["skip_repos"])


def _shown(text: str) -> str:
    lines = (text or "").strip().splitlines()
    out = "\n".join(lines[:SHOW_LINES])
    if len(lines) > SHOW_LINES:
        out += f"\n... ({len(lines) - SHOW_LINES} more lines)"
    return out if len(out) <= SHOW_CHARS else out[:SHOW_CHARS] + " ..."


def problem(sub: str, args: list[str], cfg: dict, real: str | None = None, stdin_text: str | None = None,
            cwd: str | None = None) -> str | None:
    """Why `gh pr <sub> <args>` must not run under the project's PR body check, or None."""
    if sub not in ("create", "edit"):
        return None
    conf, why = _conf(cfg)
    if conf is None:
        return why
    opts, pos = _scan(args, CREATE_VALUES if sub == "create" else EDIT_VALUES)
    if sub == "edit" and not _sets_body(opts):
        return None   # the body is not changed
    # gh is asked for the repo only when a glob needs it.
    repo = _repo(opts, pos, real if conf["repos"] or conf["skip_repos"] else None)
    if not applies(conf, repo):
        return None
    return _check_body(conf, opts, sub, repo, stdin_text, cwd)


def _sets_body(opts: dict) -> bool:
    return any(k in opts for k in ("--body", "-b", "--body-file", "-F"))


def _check_body(conf: dict, opts: dict, sub: str, repo: str, stdin_text: str | None, cwd: str | None) -> str | None:
    """The check's verdict on the body a `gh pr`/`gh issue` call's options give."""
    files = [opts[k] for k in ("--body-file", "-F") if k in opts]
    bodies = [opts[k] for k in ("--body", "-b") if k in opts]
    if files and bodies:
        return f"refused: pass the PR body once, with --body or --body-file, for {SETTING} to check it"
    if files:
        if files[-1] == "-":
            if stdin_text is None:
                return f"refused: cannot read the PR body from stdin for {SETTING}; {BODY_FILE_HOW}"
            body = stdin_text
        else:
            try:
                with open(os.path.join(cwd or "", files[-1]), encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError as e:
                return f"refused: cannot read the --body-file {files[-1]!r} for {SETTING} ({e.strerror or e})"
    elif bodies:
        body = bodies[-1]
    else:
        return (f"refused: the project checks each PR body ({SETTING}), and this call gives gh no body to "
                f"check beforehand (--fill, --editor, --web, a template or a prompt); {BODY_FILE_HOW}")
    return _run(conf, body, sub, repo, cwd)


ISSUE_URL_RE = re.compile(r"^https?://([^/\s]+)/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)", re.I)


def issue_problem(args: list[str], cfg: dict, real: str | None = None, stdin_text: str | None = None,
                  cwd: str | None = None) -> str | None:
    """Why `gh issue edit <args>` must not run under the project's PR body check, or None: it sets the
    body of an issue that is a PR, or of one the lookup cannot tell from a plain issue."""
    conf, why = _conf(cfg)
    if conf is None:
        return why
    opts, pos = _scan(args, EDIT_VALUES)   # gh issue edit's options are a subset of gh pr edit's
    if not _sets_body(opts):
        return None
    given = next((opts[k] for k in ("--repo", "-R") if opts.get(k)), "") or os.environ.get("GH_REPO", "")
    for target in pos or [""]:   # gh issue edit takes several issues
        url = ISSUE_URL_RE.match(target)
        if url:
            host, repo, number, pr = url[1], f"{url[2]}/{url[3]}".lower(), url[5], url[4].lower() == "pull"
        else:
            host, repo, number, pr = _repo_host(given), _repo_name(given), target.lstrip("#"), False
            if not repo and (conf["repos"] or conf["skip_repos"]):
                repo = _repo({}, [], real)
        if not applies(conf, repo):
            continue
        path = f"repos/{repo}" if repo else "repos/{owner}/{repo}"   # gh fills these from the current repo
        if pr or is_pull(real, path, number, ["--hostname", host] if host else []) is not False:
            return _check_body(conf, opts, "edit", repo, stdin_text, cwd)
    return None


def _repo_host(text: str) -> str:
    """The host a -R value or URL names (`host/owner/name`, a URL), else ""."""
    parts = [x for x in re.sub(r"^[a-z]+://", "", (text or "").strip(), flags=re.I).split("/") if x]
    return parts[0] if len(parts) >= 3 and "." in parts[0] else ""


def is_pull(real: str | None, repo_path: str, number: str, host: list[str] = ()) -> bool | None:
    """Whether issue `number` of `repo_path` (`repos/<owner>/<repo>`, `repositories/<id>`) is a PR: True
    when the real gh finds it under pulls/, False on a 404, None when it cannot tell (no gh, offline,
    no auth, a timeout): the caller checks the body then."""
    if not real or not str(number).isdigit():
        return None
    try:
        r = subprocess.run([real, "api", *host, f"{repo_path}/pulls/{int(number)}", "--jq", ".number"],
                           capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0 and r.stdout.strip() == str(int(number)):
        return True
    return False if r.returncode != 0 and "HTTP 404" in (r.stderr or "") else None


def node_type(real: str | None, node: str, host: list[str] = ()) -> str | None:
    """A GraphQL node's __typename as the real gh reports it (None when it cannot tell)."""
    if not real or not node:
        return None
    try:
        r = subprocess.run([real, "api", *host, "graphql", "-f", "query=query($id:ID!){node(id:$id){__typename}}",
                            "-f", f"id={node}", "--jq", ".data.node.__typename"],
                           capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _conf(cfg: dict) -> tuple[dict | None, str | None]:
    """The project's parsed setting (None when unset or malformed) and the refusal when malformed."""
    try:
        return parse((cfg.get("delivery") or {}).get(KEY)), None
    except ValueError as e:
        return None, f"refused: {SETTING} is malformed ({e}); fix it in the project's settings"


# --- PR bodies written through gh api -----------------------------------------------------------

# repos/<owner>/<repo>/pulls[/<n>], or repositories/<id>/pulls[/<n>] (GitHub serves both).
API_PULLS_RE = re.compile(r"^/?(?:repos/([^/]+)/([^/]+)|repositories/(\d+))/pulls(?:/(\d+))?/?$")
API_ISSUE_RE = re.compile(r"^/?(?:repos/([^/]+)/([^/]+)|repositories/(\d+))/issues/(\d+)/?$")
GQL_PR_RE = re.compile(r"\b(createPullRequest|updatePullRequest|updateIssue)\b")
GQL_PR_ONLY_RE = re.compile(r"\b(createPullRequest|updatePullRequest)\b")
GQL_ID_RE = re.compile(r'\bid\s*:\s*(?:"((?:[^"\\]|\\.)*)"|\$(\w+))')
# A `body:` argument: a block string, a string or a variable.
GQL_BODY_RE = re.compile(r'\bbody\s*:\s*(?:"""((?:[^"\\]|\\.|"(?!""))*)"""|"((?:[^"\\\n]|\\.)*)"|\$(\w+))')
GQL_INPUT_VAR_RE = re.compile(r"\binput\s*:\s*\$(\w+)")


class _Unread(Exception):
    pass


def api_writes(endpoint: str, method: str) -> bool:
    """Whether a `gh api` call may write a PR body (api_problem decides): a REST POST or PATCH on a
    pulls or issue endpoint (a PR is an issue), or any GraphQL call (`/graphql` too: gh posts it as
    REST, GitHub serves it)."""
    return (endpoint.strip("/").lower() == "graphql"
            or bool(API_PULLS_RE.match(endpoint) or API_ISSUE_RE.match(endpoint)) and method in ("POST", "PATCH"))


def api_problem(endpoint: str, method: str, args: list[str], cfg: dict, real: str | None = None,
                stdin_text: str | None = None, cwd: str | None = None) -> str | None:
    """Why `gh api <args>` (its endpoint and method already read) must not run under the project's PR
    body check, or None. Nothing is read while the setting is unset."""
    if not api_writes(endpoint, method):
        return None
    conf, why = _conf(cfg)
    if conf is None:
        return why
    from .prguard import _opts
    host = [x for h in _opts(args, ("--hostname",))[-1:] for x in ("--hostname", h)]
    issue = API_ISSUE_RE.match(endpoint)
    rest = API_PULLS_RE.match(endpoint) or issue
    repo = ""
    if rest:
        owner, name, rid, number = rest.groups()
        if rid:
            repo = ""   # a repository id: not told (checked)
        elif "{" in owner or "{" in name:   # gh fills {owner}/{repo} from the current repo
            repo = _repo({}, [], real if conf["repos"] or conf["skip_repos"] else None)
        else:
            repo = f"{owner}/{name}".lower()
        if not applies(conf, repo):
            return None
    try:
        if rest:
            action, bodies = "create" if number is None else "edit", _rest_bodies(args, stdin_text, cwd)
            if issue and bodies and is_pull(real, endpoint.strip("/").rsplit("/issues/", 1)[0], number, host) is False:
                return None   # a plain issue's body
        else:
            action, bodies = _graphql_bodies(args, stdin_text, cwd, real, host)
    except _Unread as e:
        return (f"refused: cannot read {e} for {SETTING} to check the PR body; pass the body as a field "
                f"(-F body=@<file>) or in an --input file")
    for body in dict.fromkeys(bodies):
        if why := _run(conf, body, action, repo, cwd):
            return why
    return None


def _read(path: str, stdin_text: str | None, cwd: str | None, what: str) -> str:
    if path == "-":
        if stdin_text is None:
            raise _Unread(f"the {what} from stdin")
        return stdin_text
    try:
        with open(os.path.join(cwd or "", path), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise _Unread(f"the {what} {path!r} ({e.strerror or e})") from None


def _api_fields(args: list[str], stdin_text: str | None, cwd: str | None) -> list[tuple[str, object]]:
    """The fields a `gh api` call sends, as (name, value getter): a typed field's `@file` is read when
    its value is asked for."""
    from .prguard import _opts
    out = []
    for typed, names in ((False, ("-f", "--raw-field")), (True, ("-F", "--field"))):
        for f in _opts(args, names):
            k, _, v = f.partition("=")
            if typed and v.startswith("@"):
                out.append((k, lambda p=v[1:]: _read(p, stdin_text, cwd, "field file")))
            else:
                out.append((k, lambda v=v: v))
    return out


def _api_input(args: list[str], stdin_text: str | None, cwd: str | None) -> dict | None:
    """The --input JSON object (None without --input or when it is not an object)."""
    from .prguard import _opts
    inputs = _opts(args, ("--input",))
    if not inputs:
        return None
    try:
        data = json.loads(_read(inputs[-1], stdin_text, cwd, "--input"))
    except ValueError:
        raise _Unread(f"the --input {inputs[-1]!r} as JSON") from None
    return data if isinstance(data, dict) else None


def _text(v) -> str:
    return v if isinstance(v, str) else "" if v is None else json.dumps(v)


def _rest_bodies(args: list[str], stdin_text: str | None, cwd: str | None) -> list[str]:
    bodies = [get() for k, get in _api_fields(args, stdin_text, cwd) if k == "body"]
    data = _api_input(args, stdin_text, cwd)
    if data is not None and "body" in data:
        bodies.append(_text(data["body"]))
    return bodies


def _graphql_bodies(args: list[str], stdin_text: str | None, cwd: str | None, real: str | None = None,
                    host: list[str] = ()) -> tuple[str, list[str]]:
    """The action and the PR bodies a GraphQL createPullRequest or updatePullRequest sets, and an
    updateIssue's unless every node it names is a plain issue."""
    fields = _api_fields(args, stdin_text, cwd)
    queries = [get() for k, get in fields if k == "query"]
    data = _api_input(args, stdin_text, cwd) or {}
    if isinstance(data.get("query"), str):
        queries.append(data["query"])
    queries = [q for q in queries if GQL_PR_RE.search(q)]
    if not queries:
        return "edit", []
    variables = dict(data.get("variables") or {}) if isinstance(data.get("variables"), dict) else {}
    for k, get in fields:   # every other field is a variable; `input[body]=x` nests one level
        if k in ("query", "operationName"):
            continue
        m = re.fullmatch(r"(\w+)\[(\w+)\]", k)
        if m:
            node = variables.get(m.group(1))
            node = variables[m.group(1)] = node if isinstance(node, dict) else {}
            node[m.group(2)] = get
        else:
            variables[k] = get

    def value(v):
        return v() if callable(v) else v

    def variable(name: str):
        if name not in variables:
            raise _Unread(f"the GraphQL variable ${name}")
        return value(variables[name])
    action = "create" if any("createPullRequest" in q for q in queries) else "edit"
    bodies = []
    for q in queries:
        if not GQL_PR_ONLY_RE.search(q) and _plain_issues(q, variable, value, real, host):
            continue
        for block, string, var in GQL_BODY_RE.findall(q):
            if var:
                bodies.append(_text(variable(var)))
            elif string or not block:
                try:
                    bodies.append(json.loads(f'"{string}"'))
                except ValueError:
                    bodies.append(string)
            else:
                bodies.append(textwrap.dedent(block.replace('\\"""', '"""')).strip("\n"))
        for var in GQL_INPUT_VAR_RE.findall(q):   # the whole input object passed as a variable
            v = variable(var)
            if isinstance(v, dict) and "body" in v:
                bodies.append(_text(value(v["body"])))
    return action, bodies


def _plain_issues(q: str, variable, value, real: str | None, host: list[str]) -> bool:
    """Every node an updateIssue query names (`id:` inline, in a variable or in its input object) is a
    plain issue, as the real gh tells it. None named, or one not told: False (the body is checked)."""
    nodes = []
    try:
        nodes += [_text(variable(var)) if var else string for string, var in GQL_ID_RE.findall(q)]
        for var in GQL_INPUT_VAR_RE.findall(q):
            v = variable(var)
            nodes.append(_text(value(v.get("id"))) if isinstance(v, dict) else "")
    except _Unread:
        return False
    return bool(nodes) and all(node_type(real, n, host) == "Issue" for n in nodes)


def _run(conf: dict, body: str, sub: str, repo: str, cwd: str | None) -> str | None:
    from . import boundedchild
    fd, path = tempfile.mkstemp(prefix="ttp-pr-body-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        env = {**os.environ, "TTP_PR_BODY_FILE": path, "TTP_PR_REPO": repo, "TTP_PR_ACTION": sub}
        r = boundedchild.run(conf["run"], conf["timeout_s"], shell=True, cwd=cwd, env=env, input=body,
                             stderr="stdout")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = _shown(r.out if isinstance(r.out, str) else "")
    if r.rc is None or r.rc in CANNOT_RUN:
        why = r.error or f"exit {r.rc}"
        return (f"refused: the PR body check ({SETTING}) could not run ({why}); fix the setting or the "
                f"command it names" + (f":\n{out}" if out else ""))
    if r.rc != 0:
        return (f"refused: the PR body failed the project's check ({SETTING}, exit {r.rc}); fix the body "
                f"and retry" + (f":\n{out}" if out else ""))
    return None
