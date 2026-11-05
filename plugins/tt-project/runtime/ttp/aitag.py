# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""AI-authorship disclosure: what a run posts where people other than the user read it ends with a tag
(delivery.ai_tag, default "[authored by AI]"; "" turns the guard off).

Two guards refuse a post without it, model-free; the refusal tells the run to add the tag:

- The worker `gh` wrapper (prguard.check, gh_problem here): the body of `gh issue create`,
  `gh issue comment`, `gh pr comment` and `gh pr review`, and a comment, review or issue written
  through `gh api` (REST POST/PATCH/PUT to an issue, comment or review endpoint; a GraphQL mutation with
  a `body`). A body gh would get some other way (`--editor`, `--web`, a template) is refused with a hint
  to pass `--body-file`. PR bodies and edits of an issue's or PR's own body (`gh pr create`, `gh pr
  edit`, `gh issue edit`, the pulls and issues/<n> endpoints, createPullRequest, updatePullRequest,
  updateIssue) stay as they are: a PR already names the AI tool that made it. So do commit messages.
- The PreToolUse hook (hook.pre_tool_use, tool_problem here) on connector tools (`mcp__<server>__<tool>`)
  whose name says they post: send, post, reply, draft or create a message, comment, issue, page or
  email (Slack, Jira, Confluence, mail). Each text field (text, message, body, comment, description,
  and the like) of such a call must end with the tag.

"Ends with" reads the last line that has text, after trailing HTML tags and comments (a hidden
`<!-- ttp -->` line may follow) and whitespace: the tag anywhere in it, any case, or a similar
disclosure ("Generated with Claude Code", "written by AI").

Messages to the user themselves (the chat relay, `ttp notify`, the web app) are not posts to others
and are never tagged. Routes the harness cannot see (a script posting over HTTP, another CLI) are
left to the prompt rule."""
from __future__ import annotations

import json
import os
import re

KEY = "ai_tag"
SETTING = f"delivery.{KEY}"
DEFAULT = "[authored by AI]"

# The last line of a post discloses AI authorship in other words.
SIMILAR_RE = re.compile(r"(?<![A-Za-z])(?:authored|written|drafted|generated|posted|created|made)\s+(?:by|with|using)\s+"
                        r"(?:\[?\s*)?(?:an?\s+)?(?:AI|Claude|Codex|Copilot|GPT|ChatGPT|Gemini|GLM|LLM|Cursor)(?![A-Za-z])", re.I)
TRAILING_MARKUP_RE = re.compile(r"(?:<!--(?:(?!<!--).)*?-->|</?[A-Za-z][^<>]{0,200}>|\s)+\Z", re.S)

# --- the tag and its check -------------------------------------------------------------------------


def tag(cfg: dict | None) -> str:
    """The project's tag ("" when the guard is off)."""
    v = ((cfg or {}).get("delivery") or {}).get(KEY)
    return DEFAULT if v is None else str(v).strip()


def problems(v) -> list[str]:
    if v is not None and not isinstance(v, str):
        return [f"{SETTING}: not a string (the tag a post to others ends with; \"\" turns it off)"]
    if isinstance(v, str) and "\n" in v:
        return [f"{SETTING}: one line only"]
    return []


def _last_line(body: str) -> str:
    text = body[-20000:]   # a long body's end is all that is read
    text = TRAILING_MARKUP_RE.sub("", text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def ends_with_tag(body: str, the_tag: str) -> bool:
    last = _last_line(body)
    return the_tag.lower() in last.lower() or bool(SIMILAR_RE.search(last))


def ok(body: str, the_tag: str) -> bool:
    """Whether a post's body may go: empty, or it ends with the tag."""
    return not the_tag or not body.strip() or ends_with_tag(body, the_tag)


def refusal(the_tag: str, what: str = "this post") -> str:
    return (f"refused: {what} goes to people other than the user, so it ends with the AI-authorship tag "
            f"{the_tag!r} ({SETTING}). Add it as the body's last line (a hidden `<!-- ttp -->` line may "
            f"follow) and run it again")


# --- gh ---------------------------------------------------------------------------------------------

BODY_HOW = "write the body to a file and pass it with --body-file <path>"
COMMENT_VALUES = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
ISSUE_CREATE_VALUES = {"-a", "--assignee", "-b", "--body", "-F", "--body-file", "-l", "--label", "-m",
                       "--milestone", "-p", "--project", "-R", "--repo", "-t", "--title", "-T", "--template",
                       "--recover"}
# (command, sub) -> (its options that take a value, whether a body is required)
GH_POSTS = {("issue", "create"): (ISSUE_CREATE_VALUES, True),
            ("issue", "comment"): (COMMENT_VALUES, True),
            ("pr", "comment"): (COMMENT_VALUES, True),
            ("pr", "review"): (COMMENT_VALUES, False)}   # an approval without a body posts no text

# REST endpoints that post text others read: a new issue, issue and commit comments, PR review
# comments and reviews. A PR's or an issue's own body (pulls, issues/<n>) is left alone.
_REPO = r"^/?(?:repos/[^/]+/[^/]+|repositories/\d+)/"
API_POST_RE = re.compile(_REPO + r"(?:issues/\d+/comments|issues/comments/\d+|pulls/\d+/comments(?:/\d+/replies)?"
                         r"|pulls/comments/\d+|pulls/\d+/reviews(?:/\d+(?:/events)?)?|commits/[^/]+/comments"
                         r"|comments/\d+|issues)/?$")
GQL_BODY_EDIT_RE = re.compile(r"\b(?:createPullRequest|updatePullRequest|updateIssue|createCommitOnBranch)\b")
# Mutations that post text others read: a query naming one is checked even beside a body edit.
GQL_POST_RE = re.compile(r"\b(?:add\w+|submit\w+|create(?:Issue|Discussion)|update\w*Comment|"
                         r"update(?:PullRequestReview|Discussion))\s*\(")


def gh_problem(cmd: str, rest: list[str], cfg: dict, stdin_text: str | None = None,
               cwd: str | None = None) -> str | None:
    """Why `gh <cmd> <rest>` must not run for want of the tag, or None."""
    the_tag = tag(cfg)
    if not the_tag or not rest:
        return None
    if cmd == "api":
        return _api_problem(rest, the_tag, stdin_text, cwd)
    spec = GH_POSTS.get((cmd, rest[0]))
    if spec is None:
        return None
    values, required = spec
    more = rest[1:]
    if rest[0] == "comment" and "--delete-last" in more:
        return None
    from . import prbody
    opts, _ = prbody._scan(more, values)
    files = [opts[k] for k in ("--body-file", "-F") if k in opts]
    inline = [opts[k] for k in ("--body", "-b") if k in opts]
    what = f"`gh {cmd} {rest[0]}`"
    if files:
        try:
            body = prbody._read(files[-1], stdin_text, cwd, "--body-file")
        except prbody._Unread as e:
            return f"refused: cannot read {e} to check it for the AI-authorship tag ({SETTING}); {BODY_HOW}"
    elif inline:
        body = inline[-1]
    elif required:
        return (f"refused: {what} posts a body this guard cannot read beforehand (--editor, --web, a template "
                f"or a prompt), and posts to others end with the AI-authorship tag {the_tag!r} ({SETTING}); "
                f"{BODY_HOW}, ending with the tag")
    else:
        return None
    return None if ok(body, the_tag) else refusal(the_tag, what)


def _api_problem(args: list[str], the_tag: str, stdin_text: str | None, cwd: str | None) -> str | None:
    from . import prbody
    from .prguard import _api_target
    endpoint, method = _api_target(args)
    graphql = endpoint.strip("/").lower() == "graphql"
    if not graphql and not (API_POST_RE.match(endpoint) and method in ("POST", "PATCH", "PUT")):
        return None
    try:
        if graphql:
            bodies = prbody.graphql_bodies_any(   # a query only editing a PR's or an issue's own body is left
                args, stdin_text, cwd, keep=lambda q: GQL_POST_RE.search(q) or not GQL_BODY_EDIT_RE.search(q))
        else:
            bodies = prbody._rest_bodies(args, stdin_text, cwd)
    except prbody._Unread as e:
        return (f"refused: cannot read {e} to check it for the AI-authorship tag ({SETTING}); pass the body "
                f"as a field (-F body=@<file>) or in an --input file")
    for body in dict.fromkeys(bodies):
        if not ok(body, the_tag):
            return refusal(the_tag, "this `gh api` post")
    return None


# --- connector tools (Claude Code PreToolUse) --------------------------------------------------------

# A connector tool's own name (after `mcp__<server>__`) that posts: a verb, then what it posts.
POST_TOOL_RE = re.compile(r"(?:send|post|reply|forward|schedule|draft|create|add|update|edit|write|publish)"
                          r"[\w-]*?(?:message|comment|issue|page|e?mail|draft|reply|post|worklog|thread)", re.I)
_READS = r"(?:get|list|search|read|fetch|find|lookup|delete|remove)"
NOT_POST_RE = re.compile(rf"(?:^|_){_READS}(?:_|$)|^{_READS}(?=[A-Z_]|$)")
# A field of its input that carries the text people read (key words compared without case, `_` or `-`).
TEXT_KEYS = ("text", "message", "body", "comment", "description", "markdown", "html", "content")


def _text_key(key: str) -> bool:
    k = re.sub(r"[_\-\s]", "", key).lower()
    return k == "content" or any(k.endswith(w) for w in TEXT_KEYS if w != "content")


def _leaves(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _leaves(x)]
    if isinstance(v, list):
        return [s for x in v for s in _leaves(x)]
    return []


def _texts(tool_input, depth: int = 0) -> list[str]:
    """The text fields of a tool's input: a string field under a text key, or a structured one (rich
    text, a document tree) as its last text leaf."""
    out = []
    if isinstance(tool_input, dict) and depth < 4:
        for k, v in tool_input.items():
            if _text_key(str(k)):
                leaves = [s for s in _leaves(v) if s.strip()]
                if isinstance(v, str):
                    out += [v] if v.strip() else []
                elif leaves:
                    out.append(leaves[-1])
            elif isinstance(v, (dict, list)):
                out += _texts(v, depth + 1)
    elif isinstance(tool_input, list) and depth < 4:
        for v in tool_input:
            out += _texts(v, depth + 1)
    return out


def posts(tool_name: str) -> bool:
    """Whether a connector tool posts text that others read, by its name."""
    if not tool_name.startswith("mcp__"):
        return False
    own = tool_name.split("__", 2)[-1]
    return bool(POST_TOOL_RE.search(own)) and not NOT_POST_RE.search(own)


def tool_problem(tool_name: str, tool_input, cfg: dict | None) -> str | None:
    """Why a connector tool call must not run for want of the tag, or None."""
    the_tag = tag(cfg)
    if not the_tag or not posts(str(tool_name or "")):
        return None
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            tool_input = {"text": tool_input}
    for text in _texts(tool_input):
        if not ends_with_tag(text, the_tag):
            return refusal(the_tag, f"what {tool_name.split('__', 2)[-1]} posts") + \
                " (each text field: message, body, description)"
    return None


def project_config() -> dict:
    """The run's project settings ({} outside a project)."""
    if not os.environ.get("TTP_PROJECT"):
        return {}
    from .prguard import _project_config
    return _project_config()
