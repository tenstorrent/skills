"""setup-box agents on a box: `box.py agents check|install|login|editor-defaults`.

- `check <alias>` is one read-only ssh probe: each agent CLI and its version, whether a plain
  `ssh <alias> <cmd>` and a login shell both find it, whether it is logged in, and the Claude Code and
  Codex extensions on every editor server build (Cursor and VS Code) on the box. Each gap comes with
  the command this skill runs to fix it. Exit 0 ready, 1 a gap, 255 ssh failed.
- `install <alias> [--cli ...] [--extensions] --run` installs the CLIs with their official installers
  (idempotent, checked with `--version`), puts ~/.local/bin on PATH for ssh commands and login shells,
  and installs both extensions on every editor server build (checked with `--list-extensions`).
  Without `--run` it prints what it would run.
- `login <alias> <cli>` starts the CLI's own headless login on the box, detached, and prints the
  sign-in link and code for the user. `--code-stdin` passes a code the sign-in page shows back to a
  login that asks for one; `--status` says whether the CLI is logged in. Tokens never leave the box:
  the login's output is shown only up to the sign-in link and code and removed when it ends.
- `editor-defaults [--run]` adds both extensions to `remote.SSH.defaultExtensions` in Cursor's and
  VS Code's user settings on this machine (merged, existing entries kept, the file backed up), so a
  new editor server build on any box gets them. Only on the user's yes: it is their machine.
- Glean: `install` also adds the HTTP MCP server `glean` to each CLI (Claude Code user scope, Codex
  `[mcp_servers.glean]`); an existing `glean` entry is always kept, even on another URL. Its URL comes from
  `--glean-url`, the site file's `mcp.glean` (the box's entry, else top level) or this machine's own
  agent configs, never from a repo; with none it skips with one line. It also adds `glean` to the MCP
  config of each editor server on the box (Cursor `~/.cursor/mcp.json`, VS Code's remote user `mcp.json`).
  `login <alias> <cli> --mcp glean` runs the CLI's OAuth sign-in on the box in a tmux session of its own
  server (`tmux -L ttp-login`), started over ssh in the user's session, never in a sandbox; it hands out
  the link only once the sign-in's localhost callback port listens on the box. `check` reports Glean per
  agent as missing, configured but signed out, or working; `check --search` proves working with a real
  one-shot Glean search per CLI. Agents load MCP servers when a session starts: one open before the
  server was added (an editor's agent panel too) needs a reload to see it.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import platform
import re
import shlex
import sys
from typing import Dict, List, Optional, Sequence

import box

BOX_PY = str(pathlib.Path(__file__).resolve().parent / "box.py")
CLIS: Dict[str, dict] = {
    "claude": {"name": "Claude Code", "install": "curl -fsSL https://claude.ai/install.sh | bash",
               "login": "auth login", "ext": "anthropic.claude-code"},
    "codex": {"name": "Codex", "install": "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
              "login": "login --device-auth", "ext": "openai.chatgpt"},
}
DEFAULT_CLIS = tuple(CLIS)
EXTENSIONS = tuple(c["ext"] for c in CLIS.values())
# Every editor server build a Remote-SSH editor leaves on a box (Cursor's old and new layout, VS Code's).
SERVER_GLOBS = ('"$HOME"/.cursor-server/bin/*/bin/cursor-server',
                '"$HOME"/.cursor-server/bin/*/*/bin/cursor-server',
                '"$HOME"/.vscode-server/bin/*/bin/code-server',
                '"$HOME"/.vscode-server/cli/servers/*/server/bin/code-server')
PATH_MARK = "# tt-project setup-box: agent CLIs on PATH"
PATH_LINE = ('case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac  '
             + PATH_MARK)
LOGIN_DIR = '"$HOME"/.cache/tt-project/agent-login'
SETTING = "remote.SSH.defaultExtensions"
MCP = "glean"
LOGIN_TMUX = "tmux -L ttp-login"
SIGNIN_WAIT = 3600  # seconds a pending MCP sign-in waits for the user's browser (default; `login --wait`)
LOGIN_WAIT = 900  # seconds a CLI's own pending login waits (default; `login --wait`)
# Each editor server's own MCP config on the box: (its server dir, config file, servers key, entry fields).
EDITOR_MCP = {"cursor": (".cursor-server", ".cursor/mcp.json", "mcpServers", {}),
              "vscode": (".vscode-server", ".vscode-server/data/User/mcp.json", "servers", {"type": "http"})}
SEARCH_PROMPT = (f"Call the {MCP} MCP server's search tool (mcp__{MCP}__search) with the query Tenstorrent. Use no "
                 "other tool. Then reply with one line only: GLEAN_RESULTS=<number of results>, or "
                 "GLEAN_ERROR=<short reason> if the tool is missing or fails.")
# Codex defers MCP tools behind its own tool lookup: naming Claude's tool id or forbidding other tools makes it
# answer that the tool is unavailable, so its prompt names only the server.
CODEX_SEARCH_PROMPT = (f"Use the {MCP} MCP server's search tool to search for Tenstorrent. Then reply with one line "
                       "only: GLEAN_RESULTS=<number of results>, or GLEAN_ERROR=<short reason> if the tool is missing "
                       "or fails.")
MCP_URL_HINT = ('set its URL with --glean-url, or in the site file as "mcp": {"glean": "<url>"} (site.md), '
                "or run setup from a machine whose Claude Code, Codex or Cursor already has a `glean` server")


def _clis(arg: Optional[str]) -> List[str]:
    out = [c for c in (arg or ",".join(DEFAULT_CLIS)).split(",") if c and c != "none"]
    bad = [c for c in out if c not in CLIS]
    if bad:
        raise ValueError(f"unknown CLI {', '.join(bad)} (scripted: {', '.join(CLIS)})")
    return out


def _find(cli: str) -> str:
    """Shell: the CLI's path into $b, from PATH or the installers' ~/.local/bin."""
    return (f'b=$(command -v {cli} 2>/dev/null || true); '
            f'[ -z "$b" ] && [ -x "$HOME/.local/bin/{cli}" ] && b="$HOME/.local/bin/{cli}"; ')


def _authed(cli: str) -> str:
    """Shell condition, true when the CLI at $b is logged in; it prints nothing of the status."""
    if cli == "claude":
        return ('$T "$b" auth status --json </dev/null 2>/dev/null | tr -d " " | grep -q \'"loggedIn":true\'')
    return '$T "$b" login status </dev/null >/dev/null 2>&1'


_TIMEOUT = 'T=; command -v timeout >/dev/null 2>&1 && T="timeout 60"; '


def _mcp_read(cli: str) -> str:
    """Shell: the `glean` entry of the CLI at $b into $g (empty when missing), its URL into $u.

    Only the URL, type and status lines are kept: a server's headers never leave the CLI."""
    if cli == "claude":
        return (f'g=$(cd "$HOME" && $T "$b" mcp get {MCP} </dev/null 2>/dev/null | grep -E "^ *(URL|Type|Status):"); '
                'u=$(printf "%s\\n" "$g" | sed -n "s/^ *URL: *//p" | head -n 1); ')
    return (f'g=$($T "$b" mcp get {MCP} --json </dev/null 2>/dev/null | grep -E \'^ *"(name|url|type)":\'); '
            'u=$(printf "%s\\n" "$g" | sed -n \'s/^ *"url": *"\\([^"]*\\)".*/\\1/p\' | head -n 1); ')


def _mcp_status(cli: str) -> str:
    """Shell: the sign-in state word of the CLI's `glean` server into $a (Claude Code's health check status,
    Codex's auth_status)."""
    if cli == "claude":
        return 'a=$(printf "%s\\n" "$g" | sed -n "s/^ *Status: *//p" | head -n 1); '
    return (f'a=$($T "$b" mcp list --json </dev/null 2>/dev/null | awk \'/"name": *"{MCP}"/{{f=1}} '
            'f && /"auth_status"/{print; exit}\' | sed \'s/.*: *"\\([^"]*\\)".*/\\1/\'); ')


def _mcp_working(cli: str, status: str) -> Optional[bool]:
    """True signed in and working, False signed out, None another state (an error to show)."""
    s = status.strip().lower()
    if cli == "claude":
        return True if "connected" in s else False if "needs auth" in s else None
    return True if s in ("o_auth", "oauth", "bearer_token") else False if s == "not_logged_in" else None


def probe_script(clis: Sequence[str], editors: bool = True, mcp: bool = True) -> str:
    """Read-only shell run on the box; prints key=value lines and never prints a token."""
    s = _TIMEOUT
    for c in clis:
        s += (f'echo "ni_{c}=$(command -v {c} 2>/dev/null)"; '
              f'echo "login_{c}=$("${{SHELL:-/bin/sh}}" -lc \'command -v {c}\' </dev/null 2>/dev/null | tail -n 1)"; '
              + _find(c) + f'echo "bin_{c}=$b"; '
              f'if [ -n "$b" ]; then echo "ver_{c}=$($T "$b" --version </dev/null 2>/dev/null | head -n 1)"; '
              f'if {_authed(c)}; then echo auth_{c}=yes; else echo auth_{c}=no; fi; '
              + ((_mcp_read(c) + f'if [ -z "$g" ]; then echo mcp_{c}=missing; else '
                  + _mcp_status(c) + f'echo "mcp_{c}=configured"; echo "mcpurl_{c}=$u"; echo "mcpauth_{c}=$a"; fi; ')
                 if mcp else "")
              + 'fi; ')
    if mcp:
        s += editor_mcp_script("probe")
    if editors:
        s += (f'for s in {" ".join(SERVER_GLOBS)}; do [ -x "$s" ] || continue; '
              'echo "server=$s|$($T "$s" --list-extensions </dev/null 2>/dev/null | tr "\\n" ",")"; done; ')
    return s + "true"


def server_label(path: str) -> str:
    """`cursor <build>` or `vscode <build>` from a server binary's path."""
    kind = "cursor" if "/.cursor-server/" in path else "vscode"
    parts = path.split("/")
    i = next((n for n, p in enumerate(parts) if p in (".cursor-server", ".vscode-server")), 0)
    build = [p for p in parts[i + 1:] if p not in ("bin", "cli", "servers", "server", "cursor-server", "code-server")]
    return f"{kind} {'/'.join(build) or '?'}"


def read_check(out: str, alias: str, clis: Sequence[str], exts: Sequence[str], glean: Optional[str] = None,
               search: Optional[Dict[str, str]] = None) -> "tuple[List[str], List[str], List[str]]":
    """(report lines, problems, fixes) from the probe's key=value lines; `glean` is the configured URL and
    `search` each CLI's one-shot Glean search answer (`check --search`)."""
    rows = [ln.split("=", 1) for ln in out.splitlines() if "=" in ln]
    kv = {k: v.strip() for k, v in rows}
    lines: List[str] = []
    problems: List[str] = []
    fixes: List[str] = []

    def fix(cmd: str) -> None:
        if cmd not in fixes:
            fixes.append(cmd)
    for c in clis:
        name = CLIS[c]["name"]
        if not kv.get(f"bin_{c}"):
            problems.append(f"{c}: not installed")
            lines.append(f"{c}: not installed")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
            continue
        ver = kv.get(f"ver_{c}") or "?"
        if not kv.get(f"ver_{c}"):
            problems.append(f"{c}: `--version` failed")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
        path_gap = [w for w, k in (("ssh commands", "ni"), ("login shells", "login"))
                    if not kv.get(f"{k}_{c}", "").startswith("/")]
        if path_gap:
            problems.append(f"{c}: not on PATH for {' and '.join(path_gap)}")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
        authed = kv.get(f"auth_{c}") == "yes"
        if not authed:
            problems.append(f"{c}: not logged in")
            fix(f"{BOX_PY} agents login {alias} {c}  (relay its sign-in link and code to the user)")
        lines.append(f"{c}: {ver if name.split()[0].lower() in ver.lower() else f'{name} {ver}'}" + (f", not on PATH for {' and '.join(path_gap)}" if path_gap else "")
                     + (", logged in" if authed else ", not logged in")
                     + _read_mcp(kv, c, alias, glean, problems, fix, (search or {}).get(c)))
    for ed in EDITOR_MCP:
        state = kv.get(f"edmcp_{ed}")
        if state is None or (state == "missing" and not glean):
            continue
        if state == "missing":
            problems.append(f"editor {ed}: {MCP} missing")
            fix(f"{BOX_PY} agents install {alias} --run")
        elif state.startswith("failed"):
            problems.append(f"editor {ed}: {MCP} config {state}")
        lines.append(f"editor {ed}: {MCP} " + ("configured (it signs in from the editor's own MCP view)"
                                              if state == "configured" else state))
    servers = [v.split("|", 1) for k, v in rows if k == "server"]
    if exts and not servers:
        lines.append("editor servers: none on the box yet (an editor's first Remote-SSH connect installs one; "
                     "with `remote.SSH.defaultExtensions` set it brings both extensions, else run the install "
                     "--extensions fix after that connect)")
    for path, listed in servers:
        have = {e.strip().lower() for e in listed.split(",") if e.strip()}
        miss = [e for e in exts if e.lower() not in have]
        label = server_label(path)
        lines.append(f"editor {label}: " + ("extensions ok" if not miss else f"missing {', '.join(miss)}"))
        if miss:
            problems.append(f"editor {label}: missing {', '.join(miss)}")
            fix(f"{BOX_PY} agents install {alias} --cli none --extensions --run")
    return lines, problems, fixes


def _read_mcp(kv: Dict[str, str], c: str, alias: str, glean: Optional[str], problems: List[str], fix,
              search: Optional[str] = None) -> str:
    """The Glean part of a CLI's check line: missing, configured but signed out, working (with `search`, only
    when the real search returned results), or the CLI's own error status; each gap goes to problems with its
    fix. Missing with no URL configured is not a gap."""
    state = kv.get(f"mcp_{c}")
    if state is None:
        return ""
    install = f"{BOX_PY} agents install {alias} --cli {c} --run"
    if state == "missing":
        if not glean:
            return f", {MCP} not set up (no URL configured: {MCP_URL_HINT})"
        problems.append(f"{c}: {MCP} missing")
        fix(install)
        return f", {MCP} missing"
    url, auth = kv.get(f"mcpurl_{c}", ""), kv.get(f"mcpauth_{c}", "")
    other = f" (kept on another URL: {url or 'none'})" if glean and url != glean else ""
    working = _mcp_working(c, auth)
    found = re.match(r"GLEAN_RESULTS=(\d+)$", search or "")
    if working and (search is None or (found and int(found.group(1)) > 0)):
        return f", {MCP} working{other}" + (f" (search: {found.group(1)} results)" if found else "")
    if working:
        problems.append(f"{c}: {MCP} search failed ({search})")
        return f", {MCP} configured{other} but its search failed ({search})"
    problems.append(f"{c}: {MCP} " + ("signed out" if working is False else f"not working ({auth or 'no status'})"))
    fix(f"{BOX_PY} agents login {alias} {c} --mcp {MCP}  (relay its sign-in link to the user)")
    return (f", {MCP} configured{other} but " + ("signed out" if working is False
                                                 else f"not working ({auth or 'no status'})"))


def search_script(clis: Sequence[str]) -> str:
    """Shell: one non-interactive Glean search per CLI, `search_<cli>=` its one answer line (GLEAN_RESULTS=<n> or
    GLEAN_ERROR=<reason>), or `no answer`. Only that line leaves the box."""
    s = (f'T=; command -v timeout >/dev/null 2>&1 && T="timeout 300"; Q={shlex.quote(SEARCH_PROMPT)}; '
         f'QC={shlex.quote(CODEX_SEARCH_PROMPT)}; ')
    for c in clis:
        run = (f'"$b" -p "$Q" --allowedTools=mcp__{MCP}' if c == "claude"
               else '"$b" exec --skip-git-repo-check "$QC"')
        s += (_find(c) + 'if [ -n "$b" ]; then r=$(cd "$HOME" && $T ' + run + ' </dev/null 2>/dev/null '
              '| grep -oE "GLEAN_(RESULTS=[0-9]+|ERROR=.*)" | tail -n 1 | cut -c1-160); '
              f'echo "search_{c}=${{r:-no answer}}"; fi; ')
    return s + "true"


def _mcp_add(cli: str) -> str:
    """Shell: add the `glean` server at $G to the CLI at $b (Claude Code in user scope)."""
    if cli == "claude":
        return f'(cd "$HOME" && $T "$b" mcp add --scope user --transport http {MCP} "$G" </dev/null >/dev/null 2>&1); '
    # Codex starts the OAuth sign-in right after adding and waits for its callback: the entry is written
    # first, the wait is cut short, and `login --mcp glean` signs in.
    return f'$TA "$b" mcp add {MCP} --url "$G" </dev/null >/dev/null 2>&1; '


_EDITOR_MCP_PY = r"""
import json, os, shutil, sys
path, key, url, extra, mode = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]
try:
    d = json.load(open(path)) if os.path.exists(path) else {}
except ValueError:
    print("failed (not JSON, left as it is)"); sys.exit(0)
servers = d.get(key) if isinstance(d.get(key), dict) else {}
g = servers.get("glean")
if isinstance(g, dict):
    u = g.get("url") or ""
    print("configured" if mode == "probe" else "present" if u == url else "kept (on another URL: %s)" % (u or "none"))
    sys.exit(0)
if mode == "probe":
    print("missing"); sys.exit(0)
servers["glean"] = dict(extra, url=url)
d[key] = servers
os.umask(0o077)
os.makedirs(os.path.dirname(path), exist_ok=True)
if os.path.exists(path) and not os.path.exists(path + ".ttp-bak"):
    shutil.copy2(path, path + ".ttp-bak")
open(path + ".ttp-tmp", "w").write(json.dumps(d, indent=2) + "\n")
os.replace(path + ".ttp-tmp", path)
print("added")
"""


def editor_mcp_script(mode: str, url: str = "") -> str:
    """Shell: per editor server on the box, its `glean` MCP entry: `edmcp_<editor>=` added, present, kept (on
    another URL), missing or configured (probe), or failed. Nothing for an editor with no server there."""
    s = f"PY={shlex.quote(_EDITOR_MCP_PY)}; "
    for ed, (server, rel, key, extra) in EDITOR_MCP.items():
        s += (f'if [ -d "$HOME/{server}" ]; then if command -v python3 >/dev/null 2>&1; then '
              f'echo "edmcp_{ed}=$(python3 -c "$PY" "$HOME/{rel}" {key} {shlex.quote(url or "-")} '
              f'{shlex.quote(json.dumps(extra))} {mode} 2>/dev/null || echo failed)"; '
              f'else echo "edmcp_{ed}=failed (no python3)"; fi; fi; ')
    return s


def mcp_install_script(clis: Sequence[str], url: str) -> str:
    """Shell: the `glean` server at `url` for each CLI; an existing `glean` entry is kept as it is."""
    s = (f'G={shlex.quote(url)}; '
         'TA=; command -v timeout >/dev/null 2>&1 && TA="timeout 20"; ')
    for c in clis:
        s += (_find(c) + f'if [ -z "$b" ]; then echo "mcp_{c}=failed (no {c})"; fail=1; else '
              + _mcp_read(c) + f'if [ "$u" = "$G" ]; then echo "mcp_{c}=present"; '
              f'elif [ -n "$g" ]; then echo "mcp_{c}=kept (on another URL: ${{u:-none}})"; else ' + _mcp_add(c) + _mcp_read(c)
              + f'if [ "$u" = "$G" ]; then echo "mcp_{c}=added"; else echo "mcp_{c}=failed"; fail=1; fi; fi; fi; ')
    s += editor_mcp_script("add", url)
    return s


def install_script(clis: Sequence[str], exts: Sequence[str], glean: Optional[str] = None) -> str:
    """Idempotent shell run on the box: CLIs, PATH lines, the Glean server, editor extensions; key=value lines."""
    s = ("set -u; fail=0; umask 022; mkdir -p \"$HOME/.local/bin\" \"$HOME/.cache/tt-project\"; "
         'T=; TL=; command -v timeout >/dev/null 2>&1 && { T="timeout 60"; TL="timeout 600"; }; ')
    if clis:
        s += (f"LINE={shlex.quote(PATH_LINE)}; MARK={shlex.quote(PATH_MARK)}; "
              'put() { f=$1; if [ -f "$f" ] && grep -qF "$MARK" "$f"; then echo "path=$f already"; return; fi; '
              '[ -f "$f" ] && [ ! -e "$f.ttp-bak" ] && cp -p "$f" "$f.ttp-bak"; t=$(mktemp) || return 1; '
              'if [ "$2" = top ]; then { printf "%s\\n" "$LINE"; cat "$f" 2>/dev/null; } >"$t"; '
              'else { cat "$f" 2>/dev/null; printf "%s\\n" "$LINE"; } >"$t"; fi; '
              'cat "$t" >"$f"; rm -f "$t"; echo "path=$f added"; }; '
              # ~/.bashrc first line: Debian-style ones return early for the shell an ssh command runs in.
              'put "$HOME/.bashrc" top; '
              'if [ -f "$HOME/.bash_profile" ]; then put "$HOME/.bash_profile" end; else put "$HOME/.profile" end; fi; '
              'case "${SHELL:-}" in */zsh) put "$HOME/.zshenv" top ;; esac; '
              'PATH="$HOME/.local/bin:$PATH"; export PATH; ')
    for c in clis:
        log = f'"$HOME/.cache/tt-project/agents-install-{c}.log"'
        s += (_find(c) + f'if [ -n "$b" ] && v=$($T "$b" --version </dev/null 2>/dev/null) && [ -n "$v" ]; then '
              f'echo "cli_{c}=present $v"; else '
              f'( {CLIS[c]["install"]} ) >{log} 2>&1; ' + _find(c)
              + f'if [ -n "$b" ] && v=$($T "$b" --version </dev/null 2>/dev/null) && [ -n "$v" ]; then '
              f'echo "cli_{c}=installed $v"; else echo "cli_{c}=failed"; fail=1; '
              f'tail -n 5 {log} | sed "s/^/log_{c}=/"; fi; fi; ')
    if clis and glean:
        s += mcp_install_script(clis, glean)
    if exts:
        s += (f'n=0; for s in {" ".join(SERVER_GLOBS)}; do [ -x "$s" ] || continue; n=$((n+1)); '
              'have=$($T "$s" --list-extensions </dev/null 2>/dev/null); '
              f'for e in {" ".join(exts)}; do '
              'if printf "%s\\n" "$have" | grep -qix "$e"; then echo "ext=$s|$e|present"; '
              'elif $TL "$s" --install-extension "$e" </dev/null >/dev/null 2>&1 && '
              '$T "$s" --list-extensions </dev/null 2>/dev/null | grep -qix "$e"; then echo "ext=$s|$e|installed"; '
              'else echo "ext=$s|$e|failed"; fail=1; fi; done; done; [ "$n" = 0 ] && echo "ext=none"; ')
    return s + 'exit "$fail"'


def read_install(out: str) -> "tuple[List[str], bool]":
    lines, ok, added = [], True, False
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k.startswith("cli_"):
            lines.append(f"{k[4:]}: {v}")
            ok = ok and not v.startswith("failed")
        elif k.startswith("log_"):
            lines.append(f"  {k[4:]} installer: {v}")
        elif k == "path":
            lines.append(f"PATH line: {v}")
        elif k.startswith("mcp_") or k.startswith("edmcp_"):
            lines.append(("editor " if k.startswith("ed") else "") + f"{k.split('_', 1)[1]}: {MCP} {v}")
            ok = ok and not v.startswith("failed")
            added = added or v == "added"
        elif k == "ext" and v == "none":
            lines.append("editor servers: none on the box yet (an editor's first Remote-SSH connect installs one)")
        elif k == "ext":
            path, ext, state = (v.split("|") + ["", ""])[:3]
            lines.append(f"editor {server_label(path)}: {ext} {state}")
            ok = ok and state != "failed"
    if added:
        lines.append(f"reload (tell the user once): agent sessions already open, an editor's agent panel too, load "
                     f"{MCP} only when they start: restart `claude` or `codex`, or run `Developer: Reload Window` "
                     "in the editor")
    return lines, ok


def _login_key(cli: str, mcp: Optional[str]) -> str:
    return f"{cli}-{mcp}" if mcp else cli


def _login_ready(cli: str, mcp: Optional[str]) -> str:
    """Shell: exit 0 with a state line when there is nothing to sign in to (no CLI or server, or signed in)."""
    s = _find(cli) + 'if [ -z "$b" ]; then echo state=no-cli; exit 0; fi; '
    if not mcp:
        return s + f'if {_authed(cli)}; then echo state=logged-in; exit 0; fi; '
    return (s + _mcp_read(cli) + 'if [ -z "$g" ]; then echo state=no-mcp; exit 0; fi; ' + _mcp_status(cli)
            + 'case "$a" in *Connected*|o_auth|oauth|bearer_token) echo state=logged-in; exit 0 ;; esac; ')


def _session(cli: str, mcp: str) -> str:
    return f"ttp-login-{_login_key(cli, mcp)}"


def _tmux(cli: str, mcp: str) -> str:
    """Shell: $X the sign-in tmux server, $S the session, $SS the path of `ss` (empty without it)."""
    return f'X="{LOGIN_TMUX}"; S={_session(cli, mcp)}; SS=$(command -v ss 2>/dev/null); '


# Shell: the pane's text into $o, the localhost callback port its sign-in link names into $p (empty when the
# link has none), and the address listening on that port on the box into $l (empty when nothing listens).
_CAPTURE = 'o=$($X capture-pane -p -J -t "=$S:" -S -200 2>/dev/null); '
_CALLBACK = ('p=$(printf "%s\\n" "$o" | grep -ioE "redirect_uri=https?%3A%2F%2F[^&]*%3A[0-9]+" | head -n 1 '
             '| sed "s/.*%3[aA]//"); ')
_LISTEN = ('l=; if [ -n "$p" ] && [ -n "$SS" ]; then l=$($SS -Hltn 2>/dev/null | awk -v p=":$p" '
           '\'{a=$4; if (substr(a, length(a) - length(p) + 1) == p) {print a; exit}}\'); fi; ')
_DEAD = '[ -n "$p" ] && [ -n "$SS" ] && [ -z "$l" ]'


def mcp_login_script(cli: str, mcp: str, wait: int = SIGNIN_WAIT) -> str:
    """Start the CLI's sign-in to an MCP server on the box in a tmux session, unless it is signed in or one
    is pending whose callback still listens; print its link only once the callback port listens there.

    The session lives in a tmux server of its own, started by this ssh command in the user's session on the
    box: never inside a sandbox, whose own network namespace a browser's localhost callback cannot reach. A
    pending sign-in whose callback no longer listens (it timed out or died) is replaced. The pane is a real
    terminal: the redirect URL the browser ends on can be pasted back (`--code-stdin`). It waits up to
    `wait` seconds; the session keeps when that ends (`until=`, epoch seconds) for a later call.
    """
    return (_TIMEOUT + 'umask 077; ' + _login_ready(cli, mcp)
            # From the `mcp` listing: Claude Code (Commander) exits 0 for `mcp <unknown> --help`.
            + 'if ! $T "$b" mcp --help </dev/null 2>/dev/null | grep -Eq "^[[:space:]]+login([[:space:]]|$)"; then '
            'echo state=no-login-cmd; exit 0; fi; '
            'command -v tmux >/dev/null 2>&1 || { echo state=no-tmux; exit 0; }; ' + _tmux(cli, mcp)
            + 'if $X has-session -t "=$S" 2>/dev/null; then ' + _CAPTURE + _CALLBACK + _LISTEN
            + f'if {_DEAD}; then $X kill-session -t "=$S" 2>/dev/null; echo "replaced=$p"; fi; fi; '
            'if $X has-session -t "=$S" 2>/dev/null; then st=pending; else '
            f'TL=; command -v timeout >/dev/null 2>&1 && TL="timeout {int(wait)}"; '
            f'$X new-session -d -s "$S" -x 400 -y 50 -c "$HOME" "exec $TL \\"$b\\" mcp login {mcp} --no-browser" '
            '</dev/null >/dev/null 2>&1 || { echo state=failed; exit 0; }; st=started; '
            f'[ -n "$TL" ] && $X set-option -t "=$S" @ttp_until "$(( $(date +%s) + {int(wait)} ))" 2>/dev/null; fi; '
            'i=0; while :; do ' + _CAPTURE + 'case "$o" in *https://*) break ;; esac; '
            '[ $i -ge 30 ] && break; $X has-session -t "=$S" 2>/dev/null || break; sleep 1; i=$((i+1)); done; '
            + _CALLBACK + 'i=0; while [ -n "$p" ] && [ -n "$SS" ]; do ' + _LISTEN
            + '[ -n "$l" ] && break; [ $i -ge 10 ] && break; sleep 1; i=$((i+1)); done; '
            f'if {_DEAD}; then $X kill-session -t "=$S" 2>/dev/null; echo "nolisten=$p"; echo state=no-listener; exit 0; fi; '
            'echo "state=$st"; echo "session=$S"; [ -n "$l" ] && echo "listen=$l"; '
            'u=$($X show-options -v -t "=$S" @ttp_until 2>/dev/null); [ -n "$u" ] && echo "until=$u"; '
            '[ -n "$p" ] && [ -z "$SS" ] && echo "listen=unknown"; '
            'printf "%s\\n" "$o" | sed -n "1,40p" | sed "s/^/out=/"; true')


def login_script(cli: str, mcp: Optional[str] = None, wait: Optional[int] = None) -> str:
    """Start the CLI's headless login on the box, detached, unless it is logged in or one is pending.

    Its stdin is a fifo held open, so a code the sign-in page shows can be passed back (`--code-stdin`);
    output goes to an owner-only log that is removed when the login ends. It waits up to `wait` seconds
    (LOGIN_WAIT by default); when that ends is kept beside it (`until=`). An MCP sign-in runs in tmux
    instead (`mcp_login_script`).
    """
    if mcp:
        return mcp_login_script(cli, mcp, SIGNIN_WAIT if wait is None else wait)
    wait = int(LOGIN_WAIT if wait is None else wait)
    c = _login_key(cli, mcp)
    cmd = f'$TL $b {CLIS[cli]["login"]}'
    return (_TIMEOUT + f'umask 077; d={LOGIN_DIR}; mkdir -p "$d"; chmod 700 "$d"; ' + _login_ready(cli, mcp)
            + f'pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo state=pending; else '
            f'rm -f "$d/{c}.in" "$d/{c}.log" "$d/{c}.until"; mkfifo "$d/{c}.in" || {{ echo state=failed; exit 0; }}; '
            f'TL=; command -v timeout >/dev/null 2>&1 && TL="timeout {wait}"; '
            'nohup setsid sh -c \'exec 3<>"$1"; eval "$2" <&3 >"$3" 2>&1; exec 3<&-; sleep 5; rm -f "$1" "$3" "$4" "$5"\' sh '
            f'"$d/{c}.in" "{cmd}" "$d/{c}.log" "$d/{c}.pid" "$d/{c}.until" </dev/null >/dev/null 2>&1 & '
            f'echo $! >"$d/{c}.pid"; [ -n "$TL" ] && echo "$(( $(date +%s) + {wait} ))" >"$d/{c}.until"; '
            'echo state=started; fi; '
            f'u=$(cat "$d/{c}.until" 2>/dev/null); [ -n "$u" ] && echo "until=$u"; '
            f'i=0; while [ $i -lt 30 ] && ! grep -q "https://" "$d/{c}.log" 2>/dev/null; do sleep 1; i=$((i+1)); done; '
            f'sed -n "1,40p" "$d/{c}.log" 2>/dev/null | sed "s/^/out=/"; true')


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_TOKENISH = re.compile(r"\b[A-Za-z0-9_\-.]{40,}\b")


def relay_lines(out: str) -> List[str]:
    """The login's own instructions, link and code; anything token-like outside a link is redacted."""
    keep = []
    for ln in out.splitlines():
        if not ln.startswith("out="):
            continue
        t = _ANSI.sub("", ln[4:]).rstrip()
        if not t.strip() or t.lstrip().startswith("WARNING"):
            continue
        if "https://" not in t:
            t = _TOKENISH.sub("<redacted>", t)
        keep.append(t)
    return keep


def code_script(cli: str, mcp: Optional[str] = None) -> str:
    """Pass one line from stdin (the code the sign-in page showed, or the redirect URL) to the pending login."""
    if mcp:  # typed into the sign-in's tmux pane through a buffer, never on a command line
        return ('IFS= read -r code || true; ' + _tmux(cli, mcp)
                + 'if ! command -v tmux >/dev/null 2>&1 || ! $X has-session -t "=$S" 2>/dev/null; then '
                'echo state=no-login; exit 1; fi; '
                'if printf "%s" "$code" | $X load-buffer -b ttp-login-code - && '
                '$X paste-buffer -d -b ttp-login-code -t "=$S:" && $X send-keys -t "=$S:" Enter; then echo state=sent; '
                'else $X delete-buffer -b ttp-login-code 2>/dev/null; echo state=failed; exit 1; fi')
    c = _login_key(cli, mcp)
    return (f'd={LOGIN_DIR}; IFS= read -r code || true; '
            f'pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null || [ ! -p "$d/' + c + '.in" ]; then '
            'echo state=no-login; exit 1; fi; '
            'printf "%s\\n" "$code" >"$d/' + c + '.in"; echo state=sent')


def status_script(cli: str, mcp: Optional[str] = None) -> str:
    """`state=` logged-in, logged-out, pending, or (MCP) no-listener: a pending sign-in whose callback died."""
    c = _login_key(cli, mcp)
    st = 'st=$( (' + _TIMEOUT + _login_ready(cli, mcp) + 'echo state=logged-out) | sed -n "s/^state=//p" | head -n 1); '
    if mcp:
        return (st + _tmux(cli, mcp) + 'if [ "$st" = logged-out ] && command -v tmux >/dev/null 2>&1 && '
                '$X has-session -t "=$S" 2>/dev/null; then ' + _CAPTURE + _CALLBACK + _LISTEN
                + f'if {_DEAD}; then st=no-listener; else st=pending; fi; fi; echo "state=$st"')
    return (st +
            f'd={LOGIN_DIR}; pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ "$st" = logged-out ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then st=pending; fi; '
            'echo "state=$st"')


def _state(out: str) -> str:
    return next((ln.split("=", 1)[1].strip() for ln in out.splitlines() if ln.startswith("state=")), "")


def _ssh(alias: str, script: str, timeout: float, input: Optional[str] = None):
    return box.ssh_run(alias, ["sh", "-c", script], timeout=timeout, input=input)


def _ssh_failed(alias: str, r) -> Optional[int]:
    if r.returncode in (255, 124):
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"ssh: {alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
        return 255 if r.returncode == 255 else 1
    return None


def _site_entry(args) -> "tuple[dict, dict]":
    """(site file, the box's entry), each empty when missing."""
    try:
        site = box.load_site(args.site)
    except box.SiteError:
        return {}, {}
    try:
        return site, box.box_entry(site, args.alias)
    except box.SiteError:
        return site, {}


def local_mcp_url(home: Optional[pathlib.Path] = None, name: str = MCP) -> "tuple[Optional[str], str]":
    """(url, file) of the HTTP server `name` in this machine's own Claude Code, Codex or Cursor config."""
    home = home or pathlib.Path.home()
    for rel in (".claude.json", ".cursor/mcp.json"):
        try:
            srv = (json.loads((home / rel).read_text(encoding="utf-8")).get("mcpServers") or {}).get(name) or {}
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(srv, dict) and isinstance(srv.get("url"), str) and srv["url"].startswith("https://"):
            return srv["url"], f"~/{rel}"
    try:
        text = (home / ".codex/config.toml").read_text(encoding="utf-8")
    except OSError:
        return None, ""
    m = re.search(r'^\[mcp_servers\.' + re.escape(name) + r'\]\s*$(.*?)(?=^\[|\Z)', text, re.M | re.S)
    u = m and re.search(r'^\s*url\s*=\s*"(https://[^"]+)"', m.group(1), re.M)
    return (u.group(1), "~/.codex/config.toml") if u else (None, "")


def glean_url(args) -> "tuple[Optional[str], str]":
    """(url, where it came from): --glean-url, the site file's mcp.glean (box entry, then top level), then this
    machine's own agent configs. Never a default: the URL is the user's company's."""
    if getattr(args, "glean_url", None):
        return args.glean_url, "--glean-url"
    site, entry = _site_entry(args)
    for where, d in (("the box's site entry", entry), ("the site file", site)):
        u = (d.get("mcp") or {}).get(MCP) if isinstance(d.get("mcp"), dict) else None
        if isinstance(u, str) and u:
            return u, where
    return local_mcp_url()


def _wanted(args) -> "tuple[List[str], List[str]]":
    """CLIs and extensions to check: --cli, else the box's saved setup answers, else both."""
    answers = (_site_entry(args)[1].get("setup") or {}).get("answers") or {}
    if args.cli:
        clis = _clis(args.cli)
    elif "agent_clis" in answers:
        clis = [c for c in answers["agent_clis"] if c in CLIS]
    else:
        clis = list(DEFAULT_CLIS)
    return clis, list(EXTENSIONS) if answers.get("agents.extensions", "yes") != "no" else []


def cmd_check(args) -> int:
    try:
        clis, exts = _wanted(args)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    r = _ssh(args.alias, probe_script(clis, bool(exts)), timeout=300)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    search = None
    if getattr(args, "search", False):
        kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        ready = [c for c in clis if kv.get(f"mcp_{c}") == "configured" and _mcp_working(c, kv.get(f"mcpauth_{c}", ""))]
        if ready:
            rs = _ssh(args.alias, search_script(ready), timeout=900)
            bad = _ssh_failed(args.alias, rs)
            if bad is not None:
                return bad
            search = {ln.split("=", 1)[0][7:]: ln.split("=", 1)[1].strip()
                      for ln in rs.stdout.splitlines() if ln.startswith("search_")}
    lines, problems, fixes = read_check(r.stdout, args.alias, clis, exts, glean_url(args)[0], search)
    if "claude" in clis and "auth_claude=yes" in r.stdout.splitlines() and not getattr(args, "no_connectors", False):
        lines, problems, fixes = connector_findings(args, lines, problems, fixes)
    if args.json:
        print(json.dumps({"alias": args.alias, "lines": lines, "problems": problems, "fixes": fixes}))
    else:
        print("\n".join(lines + ["verdict: " + ("problems: " + "; ".join(problems) if problems else "ok")]
                        + (["fix (the skill runs these; a login needs the user only to enter its code):"]
                           + [f"  {f}" for f in fixes] if fixes else [])))
    return 1 if problems else 0


def connector_findings(args, lines: List[str], problems: List[str], fixes: List[str]):
    """The box's claude.ai account connectors (`box.py connectors check <alias>`, `connectors.md`) as more findings:
    a different account than this machine's, and each connector that needs authentication or is missing."""
    import box_connectors
    try:
        fams = box_connectors.families(_site_entry(args)[0])
    except ValueError as e:
        return lines, problems + [f"connectors: {e}"], fixes
    ln, pr, fx, _ = box_connectors.inspect([args.alias], fams, here=False)
    return (lines + [f"connectors {x}" for x in ln], problems + [f"connectors {x}" for x in pr],
            fixes + [f for f in fx if f not in fixes])


def cmd_install(args) -> int:
    try:
        clis = _clis(args.cli)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    exts = list(EXTENSIONS) if args.extensions else []
    if not clis and not exts:
        print("error: nothing to install: name --cli and/or --extensions")
        return 1
    url, where = glean_url(args) if clis else (None, "")
    script = install_script(clis, exts, url)
    skip = [f"{MCP}: skipped, no URL configured: {MCP_URL_HINT}"] if clis and not url else []
    if not args.run:
        print(f"dry run: on {args.alias}, as your own user (no root):")
        for c in clis:
            print(f"- {c}: if `{c} --version` fails, `{CLIS[c]['install']}`, then `{c} --version`")
        if clis:
            print("- ~/.local/bin on PATH for ssh commands (first line of ~/.bashrc; ~/.zshenv for zsh) and login "
                  "shells (~/.bash_profile or ~/.profile), each file backed up once to <file>.ttp-bak")
        if clis and url:
            print(f"- the {MCP} MCP server ({url}, from {where}) in each CLI that has no `glean` entry; an "
                  "existing one is kept; then `agents login <box-alias> <cli> --mcp glean`")
        if exts:
            print(f"- {', '.join(exts)} on every Cursor and VS Code server build there, checked with --list-extensions")
        print("\n".join(skip + ["rerun with --run"]))
        return 0
    r = _ssh(args.alias, script, timeout=1800)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    lines, ok = read_install(r.stdout)
    print("\n".join(lines + skip + [f"verdict: {'ok' if ok and r.returncode == 0 else 'failed'}; "
                             f"then `{BOX_PY} agents check {args.alias}`"]))
    return 0 if ok and r.returncode == 0 else 1


def callback_forward(listen: str) -> Optional[str]:
    """`<port>:<address>:<port>` for `ssh -L`, from the address the sign-in's callback listens on (as `ss` shows
    it): the forward goes to that address, never to `localhost`, which may resolve to another one on the box."""
    m = re.match(r"^(\[[0-9a-fA-F:.]+\]|[0-9.]+|\*):(\d+)$", listen.strip())
    if not m:
        return None
    host = "127.0.0.1" if m.group(1) in ("*", "0.0.0.0") else "[::1]" if m.group(1) == "[::]" else m.group(1)
    return f"{m.group(2)}:{host}:{m.group(2)}"


def home_zone(env: Optional[dict] = None):
    """The tzinfo times for the user are shown in: the home zone of the project in TTP_PROJECT
    (`home_timezone` in its project.json), else this machine's."""
    env = os.environ if env is None else env
    try:
        cfg = json.loads((pathlib.Path(env["TTP_PROJECT"]) / "harness" / "project.json").read_text())
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg["home_timezone"])
    except Exception:
        return datetime.datetime.now().astimezone().tzinfo


def until_text(epoch, tz=None, now: Optional[float] = None) -> str:
    """'01:07 PDT' (with the weekday when not within 20 h): when a sign-in's window ends, in the home zone
    with its abbreviation; '' when the box did not say."""
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return ""
    now = datetime.datetime.now().timestamp() if now is None else now
    fmt = "%H:%M %Z" if abs(ts - now) < 20 * 3600 else "%a %H:%M %Z"
    return datetime.datetime.fromtimestamp(ts, tz or home_zone()).strftime(fmt)


def cmd_login(args) -> int:
    cli, mcp = args.cli, args.mcp
    what = f"{cli} {mcp} sign-in" if mcp else f"{cli} login"
    again = f"{BOX_PY} agents login {args.alias} {cli}" + (f" --mcp {mcp}" if mcp else "")
    if args.status:
        r = _ssh(args.alias, status_script(cli, mcp), timeout=150)
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout) or "unknown"
        print(f"{cli}{f' {mcp}' if mcp else ''} on {args.alias}: {st}"
              + (f" (its callback no longer listens: the link is dead; `{again}` starts a new one)"
                 if st == "no-listener" else ""))
        return 0 if st == "logged-in" else 1
    if args.code_stdin:
        r = _ssh(args.alias, code_script(cli, mcp), timeout=60, input=sys.stdin.readline())
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout)
        print(f"passed to the pending {what}; then `{again} --status`"
              if st == "sent" else f"no pending {what} on {args.alias}: start one with `{again}`")
        return 0 if st == "sent" else 1
    wait = getattr(args, "wait", None)
    if wait is not None and wait < 60:
        print(f"--wait {wait}: give the user at least 60 s to act")
        return 2
    r = _ssh(args.alias, login_script(cli, mcp, wait), timeout=240)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    st = _state(r.stdout)
    if st == "logged-in":
        print(f"{cli}{f' {mcp}' if mcp else ''} on {args.alias}: already signed in")
        return 0
    if st == "no-cli":
        print(f"{cli} is not installed on {args.alias}: `{BOX_PY} agents install {args.alias} --cli {cli} --run` first")
        return 1
    if st == "no-mcp":
        print(f"{cli} on {args.alias} has no {mcp} server: `{BOX_PY} agents install {args.alias} --cli {cli} --run` "
              f"first ({MCP_URL_HINT})")
        return 1
    if st == "no-login-cmd":
        print(f"{cli} on {args.alias} is too old for `{cli} mcp login`: update it on the box"
              + (f" (`claude update`), or the user signs in once by hand: ssh to the box, run `claude`, type `/mcp`, "
                 f"pick {mcp} and choose Authenticate" if cli == "claude" else ""))
        return 1
    kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln and not ln.startswith("out="))
    if st == "no-tmux":
        print(f"{what} on {args.alias} needs tmux on the box (the sign-in waits there in a session of its own): "
              "install it with the box's package manager, then rerun")
        return 1
    if st == "no-listener":
        print(f"{what} on {args.alias}: its callback port {kv.get('nolisten', '?')} is not listening on the box, so "
              "its link would not work; not handing it out (stopped it). Sign-ins run over ssh in the box's own "
              f"tmux server; if this repeats, look for what blocks localhost listeners on the box, then `{again}`")
        return 1
    shown = relay_lines(r.stdout)
    if st not in ("started", "pending") or not any("https://" in ln for ln in shown):
        print(f"{what} on {args.alias} did not show a sign-in link ({st or 'no answer'})"
              + ("".join(f"\n  {ln}" for ln in shown)))
        return 1
    if mcp and "replaced" in kv:
        print(f"(an earlier pending {what} whose callback port {kv['replaced']} no longer listened was replaced)")
    ends = until_text(kv.get("until"))
    print(f"{what} on {args.alias} is waiting for the user (tell them, word for word):")
    print("\n".join(f"  {ln}" for ln in shown))
    if ends:
        print(f"  This link works until {ends}.")
    if mcp:
        fwd = callback_forward(kv.get("listen", ""))
        print((f"Its callback listens on the box at {kv['listen']}. " if fwd else "")
              + ("Its callback could not be checked (no `ss` on the box). " if kv.get("listen") == "unknown" else "")
              + "Either way completes it: "
              + (f"(a) before opening the link, the user forwards that port from the machine their browser runs "
                 f"on: `ssh -N -L {fwd} <their ssh alias for {args.alias}>`, and the page completes by itself; or (b) "
                 if fwd else "")
              + "after signing in, the browser ends on a localhost page that may not load: the user copies that "
              f"page's full address and gives it to you; pass it on with `{again} --code-stdin` (on stdin; it only "
              "completes this pending sign-in and is never stored).")
        span = f"up to {(wait or SIGNIN_WAIT) // 60} min"   # a pending one keeps the wait it started with
        span = (f"{span} (until {ends})" if st == "started" else f"until {ends}") if ends else span
        print(f"It waits {span} in tmux session {kv.get('session', _session(cli, mcp))} on the "
              f"box (`ssh -t {args.alias} {LOGIN_TMUX} attach -t {kv.get('session', _session(cli, mcp))}` shows it).")
    elif cli == "claude":
        print(f"If the page then shows a code to paste back, pass it on with: "
              f"`{again} --code-stdin` (the code on stdin; it only completes "
              "this box's pending login and is never stored)")
    print(f"then: `{again} --status`  (exit 0 signed in)")
    return 0


# ---- editor-defaults: the user's own machine ----

def settings_paths(home: Optional[pathlib.Path] = None, system: Optional[str] = None) -> Dict[str, pathlib.Path]:
    """Each editor's user settings.json here, for editors whose user folder exists."""
    home = home or pathlib.Path.home()
    system = system or platform.system()
    if system == "Darwin":
        base = home / "Library" / "Application Support"
    elif system == "Windows":
        base = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    found = {}
    for name, folder in (("Cursor", "Cursor"), ("VS Code", "Code")):
        user = base / folder / "User"
        if user.is_dir():
            found[name] = user / "settings.json"
    return found


def _blank_jsonc(text: str) -> str:
    """`text` with comments and trailing commas turned into spaces: same offsets, plain JSON."""
    out = list(text)
    i, n, in_str = 0, len(text), False
    last_comma = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str, last_comma = True, None
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in text[i:j]]
            i = j
            continue
        elif ch == ",":
            last_comma = i
        elif ch in "]}":
            if last_comma is not None:
                out[last_comma] = " "
            last_comma = None
        elif not ch.isspace():
            last_comma = None
        i += 1
    return "".join(out)


def _top_key_value_span(blank: str, key: str) -> Optional["tuple[int, int]"]:
    """(start, end) of the top-level `key`'s value in blanked JSON, or None."""
    depth, i, n = 0, 0, len(blank)
    while i < n:
        ch = blank[i]
        if ch == '"':
            j = i + 1
            while j < n and blank[j] != '"':
                j += 2 if blank[j] == "\\" else 1
            if depth == 1 and json.loads(blank[i:j + 1]) == key and blank[j + 1:].lstrip().startswith(":"):
                k = blank.index(":", j + 1) + 1
                while blank[k].isspace():
                    k += 1
                _, end = json.JSONDecoder().raw_decode(blank, k)
                return k, end
            i = j + 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return None


def merge_defaults(text: str, exts: Sequence[str] = EXTENSIONS) -> "tuple[Optional[str], List[str]]":
    """(new text or None when nothing changes, the extensions added); ValueError when it cannot merge.

    Comments and everything else in the file stay as they are: only the setting's list is written.
    """
    blank = _blank_jsonc(text) if text.strip() else "{}"
    try:
        doc = json.loads(blank)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON with comments ({e.msg} at line {e.lineno})")
    if not isinstance(doc, dict):
        raise ValueError("the settings file is not a JSON object")
    have = doc.get(SETTING)
    if have is not None and not (isinstance(have, list) and all(isinstance(x, str) for x in have)):
        raise ValueError(f"{SETTING} is not a list of extension ids")
    have = have or []
    add = [e for e in exts if e.lower() not in {h.lower() for h in have}]
    if not add:
        return None, []
    new_list = json.dumps(have + add)
    if not text.strip():
        return "{\n    " + json.dumps(SETTING) + ": " + new_list + "\n}\n", add
    span = _top_key_value_span(blank, SETTING)
    if span:
        return text[:span[0]] + new_list + text[span[1]:], add
    brace = blank.index("{")
    comma = "," if doc else ""
    return text[:brace + 1] + "\n    " + json.dumps(SETTING) + ": " + new_list + comma + text[brace + 1:], add


def cmd_editor_defaults(args) -> int:
    paths = {f"file {p}": pathlib.Path(p).expanduser() for p in args.settings} if args.settings else settings_paths()
    if not paths:
        print("no Cursor or VS Code user settings on this machine: it is not where the editor runs; the box's "
              f"servers get the extensions from `{BOX_PY} agents install <box-alias> --cli none --extensions --run`, "
              "rerun after an editor update")
        return 0
    rc = 0
    for name, path in paths.items():
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            new, add = merge_defaults(text)
        except (OSError, ValueError) as e:
            print(f"{name}: left unchanged: {e} ({path})")
            rc = 1
            continue
        if new is None:
            print(f"{name}: {SETTING} already has {', '.join(EXTENSIONS)} ({path})")
            continue
        if not args.run:
            print(f"{name}: would add {', '.join(add)} to {SETTING} ({path}); rerun with --run")
            continue
        if path.exists():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.ttp-bak-{stamp}")
            backup.write_bytes(path.read_bytes())
        else:
            backup = None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.ttp-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        print(f"{name}: added {', '.join(add)} to {SETTING} ({path}"
              + (f"; backup {backup.name})" if backup else ")"))
    return rc


def register(sub) -> None:
    ag = sub.add_parser("agents", help="agent CLIs, logins and editor extensions on a box") \
        .add_subparsers(dest="agents_cmd", required=True)
    c = ag.add_parser("check", help="read-only: CLIs, PATH, logins and editor extensions, each gap with its fix")
    c.add_argument("alias")
    c.add_argument("--cli", help="comma-separated CLIs to check (default: the box's setup answer, else claude,codex)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--glean-url", help="the Glean MCP URL to expect (default as for install)")
    c.add_argument("--search", action="store_true",
                   help="prove Glean works: one real one-shot Glean search per signed-in CLI (a model call each)")
    c.add_argument("--no-connectors", action="store_true",
                   help="skip the claude.ai account connectors (`box.py connectors check`)")
    c.set_defaults(func=cmd_check)
    i = ag.add_parser("install", help="CLIs, PATH lines and editor extensions (dry run unless --run)")
    i.add_argument("alias")
    i.add_argument("--cli", default=",".join(DEFAULT_CLIS), help="comma-separated, or none (default: claude,codex)")
    i.add_argument("--extensions", action="store_true", help="also install the extensions on every editor server")
    i.add_argument("--glean-url", help="the Glean MCP URL (default: the site file's mcp.glean, else this machine's "
                                       "own agent configs)")
    i.add_argument("--run", action="store_true")
    i.set_defaults(func=cmd_install)
    lg = ag.add_parser("login", help="start a CLI's headless login on the box and show its sign-in link and code")
    lg.add_argument("alias")
    lg.add_argument("cli", choices=tuple(CLIS))
    lg.add_argument("--mcp", choices=(MCP,), help="sign the CLI in to this MCP server instead of the CLI's own login")
    how = lg.add_mutually_exclusive_group()
    how.add_argument("--status", action="store_true", help="exit 0 logged in, 1 not (or pending)")
    how.add_argument("--code-stdin", action="store_true", help="pass the code from stdin to the pending login")
    lg.add_argument("--wait", type=int, metavar="SECONDS",
                    help=f"how long a new sign-in waits for the user (default {SIGNIN_WAIT} for --mcp, "
                         f"else {LOGIN_WAIT}); a pending one keeps its own")
    lg.set_defaults(func=cmd_login)
    e = ag.add_parser("editor-defaults", help="add both extensions to remote.SSH.defaultExtensions on this machine "
                                              "(dry run unless --run; only on the user's yes)")
    e.add_argument("--settings", action="append", help="a settings.json path (default: Cursor's and VS Code's here)")
    e.add_argument("--run", action="store_true")
    e.set_defaults(func=cmd_editor_defaults)
