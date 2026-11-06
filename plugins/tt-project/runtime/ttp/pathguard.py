# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Credential folders no run writes: ~/.ssh, gh's config dir, the other stores in DEFAULT_PATHS and every
path in guards.protected_paths.

Runs are unsandboxed, so one tool call can overwrite a login (a check routed to a chat model once wrote
over gh's hosts file and logged gh out for every project on the host). refusal() is asked by the worker
hook (hook.pre_tool_use) before every other check, and by a full-tools coordinator (coordtools), for
each tool call; the runner-side tool guard of runs without the hook asks hook.pre_tool_use too. It
refuses any create, change, move, chmod or delete it can read under a protected path:
- Edit, Write, MultiEdit and NotebookEdit naming one, a Codex patch (`*** Add/Update/Delete File:`,
  `*** Move to:`) touching one, and an MCP tool whose name is not read-only given one as an argument;
- in a shell command: a redirect (`>`, `>>`, `&>`) to one, tee, cp, install, rsync, scp and ln targets,
  dd of=, curl and wget output files, unzip -d, sed -i and perl -i, and rm, mv, chmod, chown, touch,
  mkdir, truncate, shred or unlink on one (rm, mv and a recursive chmod or chown also on a folder holding
  one); the commands that rewrite a store themselves (`gh auth login`, `gh config set`, `docker login`,
  `aws configure`, `kubectl config set-*`, `git credential approve`, ssh-keygen writing a key or
  known_hosts); interpreter code (python, perl, node, ruby ...: one-liners, heredocs, the script files a
  command runs) that names one and writes anything; the same inside `sh -c` and shell scripts.
Words are read after ~, $HOME, ${HOME}, $XDG_CONFIG_HOME and $GH_CONFIG_DIR are expanded with the run's
environment and symlinks resolved, and the account's own home counts besides HOME. Reads pass as before.
Known gap: a path a program builds at run time from parts no word names (an argument read from a file).

The list is DEFAULT_PATHS plus guards.protected_paths of the account settings and of the project
(a JSON list; ~ and the variables above expand): a setting adds paths, never removes a default.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex

DEFAULT_PATHS = ("~/.ssh", "~/.config/gh", "~/.git-credentials", "~/.netrc", "~/.aws", "~/.docker/config.json",
                 "~/.kube")
VARS = ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")
VAR_RE = re.compile(r"\$\{?(" + "|".join(VARS) + r")\}?(?!\w)")
# Commands that write every operand, and those (with ANCESTOR) that also reach into a folder they name
WRITE_ALL = {"tee", "rm", "rmdir", "unlink", "shred", "truncate", "touch", "mkdir", "chmod", "chown", "chgrp",
             "chattr", "setfacl", "mv"}
ANCESTOR = {"rm", "mv", "shred"}
RECURSIVE = {"chmod", "chown", "chgrp", "setfacl", "chattr"}
DEST_LAST = {"cp", "install", "ln", "rsync", "scp"}
OUTPUT_OPTS = {"curl": ("-o", "--output"), "wget": ("-O", "--output-document", "-P", "--directory-prefix"),
               "unzip": ("-d",)}
INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|pypy\d*|perl|node|nodejs|deno|bun|ruby|php|tsx|ts-node|lua|Rscript)$")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
REDIRECT_RE = re.compile(r"(?<![<>])(?:&|\d)?>>?\|?\s*([\"']?)([^\s\"';&|<>()]+)\1")
# Code that may write a file: a write call or a mode that writes (perl's '>', '>>', '+<')
WRITE_CODE_RE = re.compile(
    r"\b(?:write\w*|unlink\w*|remove\w*|rename\w*|replace|chmod\w*|chown\w*|rmtree|copy\w*|move|symlink\w*|link"
    r"|mkdirs?\w*|makedirs|truncate\w*|rmdir\w*|touch|dump|save\w*|append\w*|rm_r?f?|cp|mv|ln|sysopen)\b"
    r"|[\"'][rwabxt]*[wax+][rwabxt]*[\"']|[\"'](?:\+?>>?|\+<)[^\"']*[\"']")
# A perl or shell redirect (or perl open mode) and its target; a comparison's right side names no path
TARGET_RE = re.compile(r">>?[&|]?\s*[\"']?([^\s\"'`;,()<>|&]+)")
# Code that runs a command: it writes only what that command writes
RUN_CODE_RE = re.compile(r"\b(?:system|exec\w*|popen\w*|spawn\w*|subprocess|check_call|check_output|run)\b|`")
STR_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`([^`]*)`")
# Commands in an argv list that write the paths they name (login tools write their store: read as argv)
ARGV_WRITERS = WRITE_ALL | DEST_LAST | set(OUTPUT_OPTS) | {"dd", "sed", "perl"}
STORE_TOOLS = {"gh", "docker", "aws", "kubectl", "git", "ssh-keygen"}
# ssh and scp read the files their -i and -F name: options before one that take no value, those whose value
# is skipped (-p's only when it is a port), and the words a path expression handed to one may use
SSH_TOOLS = {"ssh", "scp"}
SSH_FLAGS_RE = re.compile(r"-[46ACNTXYaqrvtpBO3]+")
SSH_VALUE_OPTS = ("-o", "-l", "-J", "-P")
PATH_WORDS = {"os", "path", "pathlib", "Path", "expanduser", "expandvars", "join", "home", "homedir", "environ",
              "getenv", "get", "str", "fspath", "abspath", "realpath", "resolve", "ENV", "HOME", "Dir", "File",
              "require", "process", "env"}
# A run call that is a whole statement: its result is not kept, so nothing else can reach its arguments
RUN_STMT_RE = re.compile(r"(?:^|[;\n])\s*(?:[\w.]+\.)?(?:system|run|call|check_call|check_output|Popen|exec\w*"
                         r"|spawn\w*)\s*\(")
HOME_REF_RE = re.compile(r"~|\bHOME\b|home\(\)|expanduser|homedir|getpwuid|Dir\.home|\$ENV\{|os\.homedir")
# Codex patches: the files one adds, changes, deletes or moves to (Codex trims a header line, so blanks
# around it and its path do not hide one)
PATCH_FILE_RE = re.compile(r"^\s*\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$", re.M)
MCP_READ_RE = re.compile(r"(?:^|_)(?:read|get|list|search|find|view|stat|info|tree|show|fetch|query|describe|"
                         r"grep|glob|cat|head|tail|lookup|status)(?:_|$)", re.I)
MCP_WRITE_RE = re.compile(r"write|edit|create|move|rename|delete|remove|copy|put|patch|append|chmod|mkdir|upload|"
                          r"save|set|update", re.I)
# The plugin's own shipped scripts (setup-box's write ~/.ssh on a remote box, in ssh command text):
# reviewed code, so the script-file read skips them; a script a run wrote is read
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
WHY = ("a run never creates, changes, moves, chmods or deletes anything under the account's credential folders "
       "({hit} is one; guards.protected_paths lists them): a login a run overwrote is gone for every project on "
       "the host. Read it as before, test against a scratch copy under `mktemp -d` named by another variable "
       "(H=$(mktemp -d); ... \"$H/.ssh/...\"), and leave logins and keys to the user")


def paths(cfg: dict | None = None) -> list[str]:
    """The protected list: the defaults, then the account's and the project's guards.protected_paths."""
    from .project import load_account_settings
    out = list(DEFAULT_PATHS)
    if cfg is None:
        from .aitag import project_config
        cfg = project_config()
    for src in (load_account_settings(), cfg or {}):
        extra = (src.get("guards") or {}).get("protected_paths") if isinstance(src.get("guards"), dict) else None
        out += [p for p in extra if isinstance(p, str) and p.strip()] if isinstance(extra, list) else []
    return list(dict.fromkeys(out))


def _homes(env) -> list[str]:
    homes = [env.get("HOME") or os.path.expanduser("~")]
    try:
        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        pass
    return list(dict.fromkeys(h for h in homes if h))


def expand(word: str, env, cwd: str, home: str | None = None, real: bool = True) -> str:
    """`word` as a real path: ~ and VARS expanded (with `home` for HOME), relative to `cwd`, symlinks
    resolved (`real`)."""
    home = home or env.get("HOME") or os.path.expanduser("~")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    vals = {"HOME": home, "XDG_CONFIG_HOME": xdg, "GH_CONFIG_DIR": env.get("GH_CONFIG_DIR") or os.path.join(xdg, "gh")}
    word = VAR_RE.sub(lambda m: vals[m[1]], word.replace('"', "").replace("'", ""))
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    word = os.path.join(cwd, os.path.expanduser(word))
    return os.path.realpath(word) if real else os.path.normpath(word)


def roots(env=None, cfg: dict | None = None) -> list[str]:
    """Every protected path, real and as written, for HOME and the account's own home; gh's dir as the
    environment moves it (GH_CONFIG_DIR, XDG_CONFIG_HOME) too."""
    env = os.environ if env is None else env
    out = []
    for p in paths(cfg) + ["$GH_CONFIG_DIR"]:
        for home in _homes(env):
            for e in (env, {}):
                out += [expand(p, e, "/", home), expand(p, e, "/", home, real=False)]
    return [r for r in dict.fromkeys(out) if r not in ("/", "") and os.path.isabs(r)]


def _inside(path: str, rts: list[str]) -> str | None:
    return next((r for r in rts if path == r or path.startswith(r.rstrip("/") + "/")), None)


def _holds(path: str, rts: list[str]) -> str | None:
    """The protected path `path` is a folder of (or is)."""
    return _inside(path, rts) or next((r for r in rts if r.startswith(path.rstrip("/") + "/")), None)


def _hit(word: str, ctx, ancestor: bool = False) -> str | None:
    if not word or word.startswith("-"):
        return None
    p = expand(word, ctx[0], ctx[1])
    return (_holds if ancestor else _inside)(p, ctx[2])


def _operands(args: list[str]) -> list[str]:
    out, opts = [], True
    for a in args:
        if opts and a == "--":
            opts = False
        elif not (opts and a.startswith("-") and a != "-"):
            out.append(a)
    return out


def _opt_values(args: list[str], names: tuple[str, ...]) -> list[str]:
    out = []
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                out.append(args[i + 1])
            elif n.startswith("--") and a.startswith(n + "="):
                out.append(a[len(n) + 1:])
            elif len(n) == 2 and a.startswith(n) and len(a) > 2 and not a.startswith("--"):
                out.append(a[2:])
    return out


def _store_write(argv: list[str]) -> str | None:
    """The store a command rewrites itself, by its home-relative path (None: not such a command)."""
    name, rest = os.path.basename(argv[0]), [a for a in argv[1:] if not a.startswith("-")]
    sub, sub2 = (rest + ["", ""])[:2]
    if name == "gh" and (sub == "auth" and sub2 in ("login", "logout", "refresh", "switch")
                         or sub == "config" and sub2 in ("set", "clear-cache")):
        return "$GH_CONFIG_DIR"
    if name == "docker" and sub in ("login", "logout"):
        return "~/.docker/config.json"
    if name == "aws" and sub == "configure" and sub2 not in ("get", "list", "list-profiles"):
        return "~/.aws"
    if name == "kubectl" and sub == "config" and sub2 not in ("view", "current-context", "get-contexts",
                                                             "get-clusters", "get-users"):
        return "~/.kube"
    if name == "git" and sub == "credential" and sub2 in ("approve", "reject") or name == "git-credential-store":
        return "~/.git-credentials"
    if name == "ssh-keygen" and not _keygen_reads(argv[1:]) and not _keygen_file(argv[1:]):
        return "~/.ssh"
    return None


def _keygen_reads(args: list[str]) -> bool:
    """ssh-keygen only prints (-l, -y, -F, -L, -B, -e, -Q)."""
    return bool(set("lyFLBeQ") & set("".join(a[1:] for a in args if re.fullmatch(r"-[a-zA-Z]+", a))))


def _keygen_file(args: list[str]) -> list[str]:
    """ssh-keygen's -f values, also at the end of a flag cluster (-lf FILE) or joined (-fFILE)."""
    out = []
    for i, a in enumerate(args):
        if re.fullmatch(r"-[a-zA-Z]*f", a) and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith("-f") and len(a) > 2:
            out.append(a[2:])
    return out


def _argv_refusal(argv: list[str], ctx, cmd_env=None) -> str | None:
    """`cmd_env`: the environment the command itself gets (its NAME=value prefix): where a tool keeps its store."""
    name, args = os.path.basename(argv[0]), argv[1:]
    ops = _operands(args)
    store = _store_write(argv)
    if store and _inside(expand(store, cmd_env or ctx[0], "/"), ctx[2]):
        return store
    if name == "ssh-keygen":
        return None if _keygen_reads(args) else next((h for w in _keygen_file(args) if (h := _hit(w, ctx))), None)
    if name in WRITE_ALL or name in ("sed", "perl") and any(re.match(r"-[a-zA-Z]*i|--in-place", a) for a in args):
        anc = name in ANCESTOR or name in RECURSIVE and any(re.match(r"-[a-zA-Z]*R|--recursive", a) for a in args)
        words = ops if name != "perl" else [a for a in ops if not a.startswith(("print", "s/", "$"))]
        return next((h for w in words if (h := _hit(w, ctx, anc))), None)
    if name in DEST_LAST:
        dest = _opt_values(args, ("-t", "--target-directory")) + ops[-1:]
        return next((h for w in dest if (h := _hit(w, ctx))), None)
    if name == "dd":
        return next((h for a in args if a.startswith("of=") and (h := _hit(a[3:], ctx))), None)
    if name in OUTPUT_OPTS:
        return next((h for w in _opt_values(args, OUTPUT_OPTS[name]) if (h := _hit(w, ctx))), None)
    return None


def _code_refusal(code: str, ctx, depth: int = 0) -> str | None:
    """Interpreter code that writes a protected path: a write call or mode with one named in the code
    (anywhere: a variable may carry it), a redirect or open mode with one as its target, or a command
    the code runs (os.system, subprocess, exec, backticks) that writes one. Reads pass."""
    ms = list(STR_RE.finditer(code))
    lits = [m[1] or m[2] or m[3] or "" for m in ms]
    # The code with each literal blanked: what lies between literals is code (a variable, a call)
    bare = STR_RE.sub(lambda m: m[0][0] + "x" * (len(m[0]) - 2) + m[0][-1], code)
    # The whole-code checks skip a key or config file that ssh or scp only reads (-i, -F)
    rest = _ssh_reads(code, bare, ms)
    if WRITE_CODE_RE.search(code) and (h := _names(rest, ctx)):
        return h
    for m in TARGET_RE.finditer(code):
        if h := _names(m[1], ctx):
            return h
    if not RUN_CODE_RE.search(code):
        return None
    for i, (m, lit) in enumerate(zip(ms, lits)):
        words = re.findall(r"[^\s|;&<>()\"'`]+", lit)
        names, built = {os.path.basename(w) for w in words}, _built(bare, m, lit)
        if names & ARGV_WRITERS and (h := _names(rest, ctx)):   # a write command: its target may be built
            return h
        item = os.path.basename(lit) in STORE_TOOLS   # an argv list item: its args are the literals after it
        if item and (h := _argv_refusal(lits[i:], ctx)):
            return h
        # A login tool with an argument built in code may write its store: unless it only prints a key
        reads = "ssh-keygen" in names and _keygen_reads(lits[i + 1:] if item else words)
        if names & STORE_TOOLS and built and not reads and (h := _names(rest, ctx)):
            return h
        if " " in lit and depth < 2 and (h := shell_refusal(lit, *ctx, depth + 1)):   # a command line
            return h
        # A redirect whose target the code builds: 'echo x >' + p, f'echo x > {p}', 'cat > %s' % p (2>&1: none)
        joined = re.match(r"\s*[^\s,\])\n;]", bare[m.end():])   # code goes on right after it: + p, % p
        for r in re.finditer(r"(?<![<>])>>?\|?(&)?\s*([^\s;&|<>()]*)", lit):
            if r[1] and re.fullmatch(r"\d+-?|-", r[2].rstrip("'\"`")):
                continue
            if (not r[2] or re.search(r"[{}%$]", r[2]) or joined and r.end() == len(lit)) and (
                    h := _names(rest, ctx)):
                return h
    return None


def _ssh_reads(code: str, bare: str, ms: list) -> str:
    """`code` with each path blanked that a run call (a whole statement) hands ssh or scp as -i or -F
    before the first operand: in its argv list, its command line, or its GIT_SSH_COMMAND value."""
    for a, e in _ssh_spans(code, bare, ms):
        code = code[:a] + " " * (e - a) + code[e:]
    return code


def _ssh_spans(code: str, bare: str, ms: list, depth: int = 0) -> list:
    starts, spans = {m.start(): m for m in ms}, []
    if depth < 1:   # code quoted in a command line (python3 -c "..."): its own run calls
        for m in ms:
            if RUN_STMT_RE.search(lit := m[1] or m[2] or ""):
                inner = list(STR_RE.finditer(lit))
                ibare = STR_RE.sub(lambda i: i[0][0] + "x" * (len(i[0]) - 2) + i[0][-1], lit)
                spans += [(a + m.start() + 1, e + m.start() + 1) for a, e in _ssh_spans(lit, ibare, inner, 1)]

    def lit_at(pos):
        return starts.get(pos) or bare[pos:pos + 1] in ("f", "F") and starts.get(pos + 1)

    def line_items(m):   # a command line literal: ssh's words in it, and code joined right after
        lit, c, out, cmd, stop = m[1] or m[2] or m[3] or "", m.start() + 1, [], True, None
        groups = r"\{([^{}]*)\}" if code[m.start() - 1:m.start()] in ("f", "F") else r"\$\{([^{}]*)\}" if m[3] else None
        for t in re.finditer(r"[^\s;&|]+|[;&|]+", lit):
            word = t[0].strip("'\"")
            if stop is not None and t.start() > stop:   # past X='ssh ...': the command comes next
                spans.extend(_ssh_values(out))
                out, stop = [], None
            if re.fullmatch(r"[;&|]+", t[0]):   # the next command
                spans.extend(_ssh_values(out))
                out, cmd, stop = [], True, None
            elif out:
                left = re.sub(groups, lambda g: "" if _path_expr(g[1]) else "\0", t[0]) if groups else t[0]
                bad = r"`|\$\(" + (r"|[\0{}]" if groups else "")   # a command substitution, code left in a format
                out.append((word, (c + t.start(), c + t.end()), not re.search(bad, left)))
            elif cmd:   # NAME=value words, then the command; ssh as a quoted value (X='ssh -i k') ends with it
                v = re.match(r"\w+=(['\"]?)", t[0])
                tool = os.path.basename(word[v.end():] if v else word) in SSH_TOOLS
                if v and v[1] and tool:
                    stop = q if (q := lit.find(v[1], t.start() + v.end())) >= 0 else len(lit)
                cmd, out = bool(v), [("", None, False)] if tool and (not v or v[1]) else []
        if out and (j := re.compile(r"\s*\+\s*").match(bare, m.end())):   # 'ssh -i ' + expanduser('~/.ssh/k')
            a = j.end()
            e = _expr_end(bare, a, "+,;\n")
            out.append((None, (a, e), _path_expr(code[a:e])))
        spans.extend(_ssh_values(out))

    for call in RUN_STMT_RE.finditer(bare):
        end = _expr_end(bare, call.end(), "")
        if end >= len(bare) or not re.compile(r"\s*(?:[;\n]|$)").match(bare, end + 1) or "$_" in code[call.end():end]:
            continue
        a = re.match(r"\s*(\[?)\s*", bare[call.end():end])
        pos = call.end() + a.end()
        m = lit_at(pos)
        first = m and (m[1] or m[2] or "")
        if first and os.path.basename(first) in SSH_TOOLS and (a[1] or re.compile(r"\s*,").match(bare, m.end())):
            items, pos = [("", None, False)], m.end()   # an argv list (perl: the call's own arguments)
            while j := re.compile(r"\s*,\s*").match(bare, pos):
                pos = j.end()
                if (m := lit_at(pos)) and m[3] is None and not (bare[m.start() - 1] in "fF" and "{" in m[0]):
                    items.append((m[1] or m[2] or "", (m.start() + 1, m.end() - 1), True))
                    pos = m.end()
                else:
                    e = _expr_end(bare, pos, ",")
                    items.append((None, (pos, e), _path_expr(code[pos:e])))
                    pos = e
            spans.extend(_ssh_values(items))
        elif m:
            line_items(m)
        if code.count("GIT_SSH_COMMAND") == 1:
            for g in re.finditer(r"\bGIT_SSH_COMMAND\s*=\s*|[\"']GIT_SSH_COMMAND[\"']\s*:\s*", code[call.end():end]):
                if m := lit_at(call.end() + g.end()):
                    line_items(m)
    return spans


def _ssh_values(items: list) -> list:
    """The spans of the -i and -F values among ssh's words (word, span, a plain path?), up to its first operand."""
    out, want, skip = [], False, False
    for i, (word, span, ok) in enumerate(items[1:], 1):
        if want:
            if not ok:
                break
            out.append(span)
            want = False
        elif skip:
            skip = False
        elif word in ("-i", "-F"):
            want = True
        elif word and word[:2] in ("-i", "-F") and ok:
            out.append((span[0] + 2, span[1]) if span[1] - span[0] == len(word) else span)
        elif word in SSH_VALUE_OPTS or word == "-p" and i + 1 < len(items) and (items[i + 1][0] or "").isdigit():
            skip = True
        elif not (word and SSH_FLAGS_RE.fullmatch(word)):
            break
    return out


def _expr_end(bare: str, pos: int, stops: str) -> int:
    """Where the expression at `pos` ends: a `stops` character or a closing bracket outside brackets."""
    depth = 0
    for i in range(pos, len(bare)):
        ch = bare[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and ch in stops:
            return i
    return len(bare)


def _path_expr(text: str) -> bool:
    """Code that only builds a path (from the home folder, the environment, literals): nothing it runs or keeps."""
    t = STR_RE.sub("''", re.sub(r"\\([\"'])", r"\1", text))   # its literals (quotes escaped in a command line too)
    return bool(t.strip()) and not re.search(r"[=;`]", t) and set(re.findall(r"[A-Za-z_]\w*", t)) <= PATH_WORDS


def _built(bare: str, m, lit: str) -> bool:
    """The literal at `m` is joined with code: a format, template, concatenation or an argv list
    holding a variable (`bare`: the code with its literals blanked)."""
    if "{" in lit and bare[m.start() - 1:m.start()] in ("f", "F") or m[3] and "${" in lit:
        return True
    after = re.split(r"[\])\n;]", bare[m.end():], maxsplit=1)[0]
    before = re.split(r"[\[(\n;]", bare[:m.start()])[-1]
    return bool(re.sub(r"([\"'`])x*\1|[\s,]", "", after + before))


def _names(code: str, ctx) -> str | None:
    """The protected path `code` names: as a path, or as the home folder joined with its parts."""
    for m in re.finditer(r"(?:~|\$\{?(?:HOME|XDG_CONFIG_HOME|GH_CONFIG_DIR)\}?|/)[^\s\"'`;,()\[\]{}<>|&]*", code):
        if m[0] not in ("~", "/") and (h := _hit(m[0], ctx)):
            return h
    if HOME_REF_RE.search(code):
        for home in _homes(ctx[0]):
            for r in ctx[2]:
                rel = os.path.relpath(r, home)
                parts = rel.split("/")
                if not rel.startswith("..") and rel != "." and (
                        rel in code or all(re.search(r"[\"'/]" + re.escape(p) + r"[\"'/]", code) for p in parts)):
                    return r
    return None


def shell_refusal(cmd: str, env, cwd: str, rts: list[str], depth: int = 0) -> str | None:
    """The protected path a shell command line (or script) writes, as far as its words show."""
    from . import hook
    ctx = (env, cwd, rts)
    commands, scan = hook._split(cmd)
    # Redirects: in the line as written and in scan (_split drops an echo's quoted words, a quoted
    # redirect target too), where the `>` is not quoted: quoted text is an argument (ssh's remote
    # command, a message); `sh -c` text is read below
    for text in (cmd, scan):
        flags = hook._quoting(text)
        for m in REDIRECT_RE.finditer(text):
            if not flags[m.start()][0] and (h := _hit(m[2], ctx)):
                return h
    if "*** Begin Patch" in cmd and (h := patch_refusal(cmd, ctx)):
        return h
    segments = hook._all_segments(commands)
    prefixed = {tuple(hook._strip_prefix(raw)): _assigned(raw, env) for raw in hook._segments(commands, strip=False)}
    for argv in segments:
        if h := _argv_refusal(argv, ctx, prefixed.get(tuple(argv))):
            return h
    for argv in segments:   # sh -c '...' and eval '...': their redirects too
        argv = argv[1:] if argv[0] == "builtin" and len(argv) > 1 else argv
        name = os.path.basename(argv[0])
        inner = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
                     None) if name in SHELLS else " ".join(argv[1:]) if name == "eval" else None
        if inner and depth < 2 and (
                h := shell_refusal(inner, env, cwd, rts, depth + 1)):
            return h
        # eval parses its words again: an escaped redirect (eval echo x \> f) is one then
        if name == "eval" and "\\" in cmd and depth < 2 and (
                h := shell_refusal(re.sub(r"\\(.)", r"\1", cmd), env, cwd, rts, depth + 1)):
            return h
    names = {os.path.basename(a[0]) for a in segments}
    if any(INTERPRETER_RE.match(n) for n in names):
        # The line (heredocs too), and each -c/-e code as the interpreter gets it (its string literals unquoted)
        inline = [a[i + 1] for a in segments if INTERPRETER_RE.match(os.path.basename(a[0]))
                  for i in range(1, len(a) - 1) if a[i] in ("-c", "-e", "-E", "--eval", "-p", "--print")]
        if h := next((h for c in [scan, *inline] if (h := _code_refusal(c, ctx, depth))), None):
            return h
    if depth < 2:
        for f in hook._scripts(segments, cwd):
            if os.path.realpath(f).startswith(PLUGIN_ROOT + os.sep):
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            first = text.split("\n", 1)[0]
            h = (shell_refusal(text, env, cwd, rts, depth + 1) if f.suffix == ".sh" or any(s in first for s in SHELLS)
                 else _code_refusal(text, ctx, depth + 1))
            if h:
                return h
    return None


def _assigned(raw: list[str], env) -> dict:
    """`env` with the NAME=value words before a command (also after `env`)."""
    out = dict(env)
    for w in raw:
        if m := re.fullmatch(r"([A-Za-z_]\w*)=(.*)", w):
            out[m[1]] = m[2]
        elif os.path.basename(w) != "env" and not w.startswith("-"):
            break
    return out


def patch_refusal(text: str, ctx) -> str | None:
    return next((h for m in PATCH_FILE_RE.finditer(text) if (h := _hit(m[1], ctx))), None)


def _strings(v, depth: int = 0):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict) and depth < 4:
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif isinstance(v, list) and depth < 4:
        for x in v:
            yield from _strings(x, depth + 1)


def refusal(payload: dict, env=None, cfg: dict | None = None) -> str | None:
    """Why this tool call may not run (it writes under a protected path), or None. Any error refuses
    when the call names a protected path."""
    env = os.environ if env is None else env
    try:
        rts = roots(env, cfg)
    except Exception:
        rts = roots(env, {})
    try:
        hit = _refusal(payload, env, rts)
    except Exception:
        text = json.dumps(payload.get("tool_input") or {})
        hit = next((p for p in DEFAULT_PATHS + tuple(rts) if p.lstrip("~/") in text), None)
    return WHY.format(hit=hit) if hit else None


def _refusal(payload: dict, env, rts: list[str]) -> str | None:
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    inp = inp if isinstance(inp, dict) else {"input": inp}
    ctx = (env, str(payload.get("cwd") or os.getcwd()), rts)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _hit(str(inp.get("file_path") or inp.get("notebook_path") or ""), ctx)
    if tool in ("Bash", "shell", "local_shell", "exec_command"):
        cmd = inp.get("command") or inp.get("cmd") or ""
        cmd = shlex.join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)   # an argv: keep `-c` text one word
        return shell_refusal(cmd, env, ctx[1], rts)
    if tool == "apply_patch" or "*** Begin Patch" in json.dumps(inp):
        return next((h for s in _strings(inp) if (h := patch_refusal(s, ctx))), None)
    if tool.startswith("mcp__"):
        verb = tool.rsplit("__", 1)[-1]
        if MCP_READ_RE.search(verb) and not MCP_WRITE_RE.search(verb):
            return None
        return next((h for s in _strings(inp) if re.match(r"[~/$]", s) and "\n" not in s
                     and (h := _hit(s, ctx, True))), None)
    return None
