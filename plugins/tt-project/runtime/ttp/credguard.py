# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Credential stores a run never reads, and GitHub writes that skip `git push`.

Runs are unsandboxed: they can read all of HOME and reach any host. A credential a tool call prints
stays in the run's log and transcript, so the stores are kept out of tool calls model-free:
- the daemon gives Claude Code runs a Read deny rule for each file credential_files() names (gh's
  hosts file, git's and netrc's credential files, the agents' own login files, and every file in
  ~/.ssh but the known public ones, see ssh_public);
- the worker hook (hook.py) refuses a Bash call that names one by path (bash_refusal), or prints a
  token (`gh auth token`, `gh auth status --show-token`, `git credential fill`). ssh, git and gh
  read them themselves: a key given to ssh by `-i` or `-o IdentityFile=`, a key path handed to
  ssh-add or ssh-keygen, mkdir, chmod, chown, touch and test of them, and ls or stat of the ~/.ssh
  dir itself pass. The words of a command ssh runs on another host are checked the same way, quotes
  are dropped before the check (~/.ss''h), and a find whose name test matches a private key or
  credential file is refused. Known gap: a path built at run time inside a script or inline code.
Secrets stay reachable only through `ttp secret env` (hook.secret_read).

Pushes go only through `ttp push` and the push queue. The run's bin/gh (prguard.check) refuses, inside
a run, `gh api` calls that create, move or delete refs or tags, write file contents, merge branches or
write releases (ref_write), `gh release create`, `gh release delete --cleanup-tag` and `gh repo sync`
of a named repo; the hook refuses the same requests sent by curl and the like (hook.api_ref_write).
PR merges stay under the charter's merge policy and are not refused here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ~/.ssh files that hold no secret
SSH_PUBLIC = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "rc", "environment"}
# A path under .ssh (the dir alone has no name), ending where a shell word ends
SSH_PATH_RE = re.compile(r"(?<![\w.-])\.ssh(?:/+([^\s\"'`;&|<>()]*))?(?![\w.-])")
# The other stores by name, or by the variable that moves them
STORE_RE = re.compile(r"(?:(?<![\w.-])gh|GH_CONFIG_DIR\}?)/+hosts\.ya?ml\b|(?<![\w.-])\.git-credentials\b"
                      r"|(?<![\w.-])\.netrc\b|(?<![\w.-])\.credentials\.json\b"
                      r"|(?:(?<![\w.-])\.codex|CODEX_HOME\}?)/+auth\.json\b")
SSH_FAMILY = {"ssh", "scp", "sftp", "autossh", "ssh-copy-id"}
# ssh -o options that only name a key or a socket ssh opens itself (never ProxyCommand: it runs a command)
SSH_KEY_OPTION_RE = re.compile(r"(?:IdentityFile|CertificateFile|ControlPath|IdentityAgent)[\s=]", re.I)
# Commands that never print a file's contents or name; ls and stat may name the ~/.ssh dir only
SILENT = {"mkdir", "chmod", "chown", "touch", "test", "[", "ssh-add", "ssh-keygen"}
LISTERS = {"ls", "stat"}
# ssh options that take a value (the next word, or the rest of the same word)
SSH_VALUE_FLAGS = set("BbcDEeFIiJLlmOoPpQRSWw")
# Names find may look for that only private keys and credential files have
FIND_SECRET_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
                     ".git-credentials", ".netrc")
FIND_NAME_TESTS = {"-name": False, "-iname": True}
WHY = ("a run never reads credential stores (ssh private keys, gh's hosts file or `gh auth token`, git and netrc "
       "credential files, the agents' login files): what it prints stays in the run's log and transcript. ssh, git "
       "and gh read them themselves: give ssh a key with `-i <key>`, give a stored secret to a command with "
       "`ttp secret env`, and call a store by what it is in text, never by its path")

# GitHub REST routes that move refs or write a repo's files: refs, tags, contents, merges, releases, renames
REF_ROUTE_RE = re.compile(r"^/?(?:repos/[^/]+/[^/]+|repositories/\d+)/(?:git/refs|git/tags|contents|merges"
                          r"|merge-upstream|releases|branches/[^/]+/rename)(?:/|$)", re.I)
# The same routes in a URL a client sends to
REF_URL_RE = re.compile(r"/(?:repos/[^/\s]+/[^/\s]+|repositories/\d+)/(?:git/refs|git/tags|contents/|merges\b"
                        r"|merge-upstream\b|releases\b|branches/[^/\s]+/rename)", re.I)
REF_MUTATION_RE = re.compile(r"\b(?:createRef|updateRefs?|deleteRef|createCommitOnBranch|mergeBranch)\b")
REF_WHY = ("refused: a run never writes git refs, tags, releases or file contents through the GitHub API "
           "(gh api, gh release create, gh repo sync, curl and the like). Commit, and land the change "
           "through `ttp push` and the push queue, which run the checks and review first")


def ssh_public(name: str) -> bool:
    """`name` (a path under ~/.ssh) holds no secret: a public key (a glob of them too), ssh's config or
    host lists, or a control socket (cm-*, or a name with ssh's % tokens)."""
    base = name.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".pub"):
        return True
    if not base or re.search(r"[*?\[]", name):
        return False
    return (base in SSH_PUBLIC or base.startswith(("cm-", "known_hosts")) or "%" in base
            or name.split("/", 1)[0] == "config.d")


def credential_files(home: str | Path | None = None) -> list[str]:
    """The credential files a run's Read tool may not open (absolute paths, missing ones too, but the
    ~/.ssh files that exist and are not public)."""
    home = Path(home or os.path.expanduser("~"))
    gh = Path(os.environ.get("GH_CONFIG_DIR") or Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "gh")
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    out = [gh / "hosts.yml", home / ".git-credentials", home / ".netrc", claude / ".credentials.json",
           codex / "auth.json"]
    try:
        out += sorted(p for p in (home / ".ssh").iterdir() if p.is_file() and not ssh_public(p.name))
    except OSError:
        pass
    return [str(p) for p in out]


def _passing_words(argv: list[str]) -> list[str]:
    """The words of `argv` that may name a key: an ssh key option's value, every word of a SILENT
    command, and a LISTERS word naming only the ~/.ssh dir."""
    name = os.path.basename(argv[0])
    if name in SILENT:
        return argv[1:]
    if name in LISTERS:
        return [w for w in argv[1:] if not any(m.group(1) for m in SSH_PATH_RE.finditer(w))]
    if name not in SSH_FAMILY:
        return []
    out = []
    for i, w in enumerate(argv[1:], 1):
        if argv[i - 1] == "-i" or (w.startswith("-i") and len(w) > 2):
            out.append(w)
        elif (argv[i - 1] == "-o" or w.startswith("-o")) and SSH_KEY_OPTION_RE.match(w[2:] if w.startswith("-o")
                                                                                    else w):
            out.append(w)
    return out


def _prints_token(argv: list[str]) -> bool:
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == "gh":
        return args[:2] == ["auth", "token"] or (args[:2] == ["auth", "status"]
                                                 and any(a in ("-t", "--show-token") for a in args))
    if name == "git" and "credential" in args:
        return args[args.index("credential") + 1:][:1] == ["fill"]
    return False


def ssh_remote(argv: list[str]) -> str:
    """The command an ssh call runs on the other host (its words after the destination), else ''."""
    if not argv or os.path.basename(argv[0]) not in ("ssh", "autossh"):
        return ""
    i = 1
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        if argv[i] == "--":
            i += 1
            break
        flags = argv[i][1:]
        j = next((k for k, c in enumerate(flags) if c in SSH_VALUE_FLAGS), None)
        i += 2 if j is not None and j == len(flags) - 1 else 1
    return " ".join(argv[i + 1:])


def _finds_secret(argv: list[str]) -> bool:
    """A find whose -name or -iname pattern matches a private key or credential file name, with a
    literal part beyond its wildcards (so `-name '*'` and `-name '*.py'` pass)."""
    if not argv or os.path.basename(argv[0]) != "find":
        return False
    from fnmatch import fnmatchcase
    for flag, pat in zip(argv[1:], argv[2:]):
        if flag not in FIND_NAME_TESTS or not re.search(r"\w", re.sub(r"\[[^\]]*\]|[*?]", "", pat)):
            continue
        fold = (lambda t: t.lower()) if FIND_NAME_TESTS[flag] else (lambda t: t)
        if any(fnmatchcase(fold(n), fold(pat)) for n in FIND_SECRET_NAMES):
            return True
    return False


def _unquoted(text: str) -> str:
    return re.sub(r"[\"'\\]", "", text)


def bash_refusal(cmd: str, segments: list[list[str]], home: str | Path | None = None) -> str | None:
    """Why a Bash call reads a credential store, else None: it names one by path, other than as a
    word a passing command takes (each such word excuses one mention), prints a token, or finds a
    key by name. The command an ssh call runs remotely counts as its own commands."""
    from .hook import _all_segments
    segments = [argv for argv in segments if argv]
    segments += [inner for argv in segments for inner in _all_segments(ssh_remote(argv))]
    if any(_prints_token(argv) or _finds_secret(argv) for argv in segments):
        return WHY
    rest = _unquoted(cmd)
    for argv in segments:
        for w in _passing_words(argv):
            if ".ssh" in w:
                rest = rest.replace(_unquoted(w), " ", 1)
    if STORE_RE.search(rest) or any(not ssh_public(m.group(1) or "") for m in SSH_PATH_RE.finditer(rest)):
        return WHY
    if any(p in rest for p in credential_files(home)[:5]):
        return WHY
    return None


def ref_write(endpoint: str, method: str, text: str) -> bool:
    """A `gh api` call to `endpoint` with `method` (its fields and body in `text`) writes refs, tags,
    releases or file contents."""
    if endpoint.strip("/").lower() == "graphql":
        return bool(REF_MUTATION_RE.search(text))
    return method not in ("GET", "HEAD") and bool(REF_ROUTE_RE.match(endpoint))


def gh_refusal(args: list[str]) -> str | None:
    """Why gh `args` (no `api`) moves a ref, inside a run: release create, release delete
    --cleanup-tag, repo sync of a named repo."""
    if args[:2] == ["release", "create"] or (args[:2] == ["release", "delete"] and "--cleanup-tag" in args):
        return REF_WHY
    if args[:2] == ["repo", "sync"]:
        values = {"-s", "--source", "-b", "--branch"}
        if any(not a.startswith("-") and args[i - 1] not in values for i, a in enumerate(args[2:], 2)):
            return REF_WHY
    return None
