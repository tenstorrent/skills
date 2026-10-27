# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which waits a host reboot may have broken (Daemon.wake_after_reboot).

A reboot wakes a waiting task only when what it waits on was local and transient: its probe checks
a local detached job (`ttp detach --check` without `--host`), its own `ttp checks --result`, a local
device runner job (`ttp devq probe`), a process (a pid, `pgrep`, `ps`) or a path under /tmp, /run,
/dev/shm, /proc or a run folder, outside any ssh command or `--host` command (those paths are on the other host); or a detached job of its own from the
earlier boot never wrote its .rc (the daemon checks that part). A timer, a probe of another host,
a CI run or another project's reply (`ttp ack-wait probe`) is as it was: the task stays asleep on
its timer and probe. `survives_reboot` keeps it asleep whatever its probe says.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# A path a reboot clears or that names a process of the earlier boot.
_VOLATILE_RE = re.compile(r"(?<![\w.~-])(?:/tmp|/var/tmp|/run|/var/run|/dev/shm|/proc)(?:/|\b)")
# A run folder of a project's state, by path or by the variable a run sets.
_RUN_DIR_RE = re.compile(r"(?:/state/runs/|\$\{?TTP_RUN_DIR\b)")
_PROC_CMDS = {"pgrep", "pidof", "ps", "pkill"}
_PUNCT = ";&|()`"
# `ttp detach --check` options that take a value (as cli._check_options reads them).
_CHECK_VALUED = ("--rows", "--after", "--mirror")
_ASSIGN_RE = re.compile(r"[A-Za-z_]\w*=")


def _commands(probe: str) -> list[list[str]] | None:
    """The probe's simple commands as word lists, or None when it does not parse."""
    try:
        lex = shlex.shlex(probe, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    out: list[list[str]] = [[]]
    for w in words:
        if not w.strip(_PUNCT):   # ; && || | & ( ) `: the next command starts
            out.append([])
        else:
            out[-1].append(w)
    return [c for c in out if c]


def _local_part(cmd: list[str]) -> list[str]:
    """The words of one command that run on this host: those before an `ssh` (its options, host and
    remote command run elsewhere)."""
    for i, w in enumerate(cmd):
        if w.rsplit("/", 1)[-1] in ("ssh", "mosh"):
            return cmd[:i]
    return cmd


def local_jobs(probe) -> list[Path]:
    """The .rc paths of the local `ttp detach --check` commands in a retry_when, alone or within a
    compound probe (&&, ;, ||, other tests; split as _commands splits it), in order, once each. A check
    with --host, or one after an ssh, is the other host's and never counted. [] when it checks no local
    job or when in doubt: it does not parse, or a local check names a path that is not an absolute .rc
    path, or an option `ttp detach --check` does not take."""
    cmds = _commands(probe) if isinstance(probe, str) else None
    out: list[Path] = []
    for cmd in cmds or ():
        words = _local_part(cmd)
        while words and _ASSIGN_RE.match(words[0]):
            words = words[1:]   # VAR=value before the command
        if len(words) < 3 or words[0].rsplit("/", 1)[-1] not in ("ttp", "ttp.sh") or words[1] != "detach" \
                or "--check" not in words[2:]:
            continue
        rest, paths, remote = words[2:], [], False
        while rest:
            w = rest.pop(0)
            key, eq, _ = w.partition("=")
            if key == "--host":
                remote = True
                break
            if w == "--check":
                continue
            if key in _CHECK_VALUED:
                if not eq and rest:
                    rest.pop(0)
                continue
            if w.startswith("-") or not w.endswith(".rc") or not Path(w).is_absolute():
                return []
            paths.append(Path(w))
        if not remote:
            out += [p for p in paths if p not in out]
    return out


def _transient_words(words: list[str], run_dirs: tuple[str, ...]) -> str:
    """Why these words, run on this host, check something a reboot ends; '' when they do not."""
    base = [w.rsplit("/", 1)[-1] for w in words]
    for i, b in enumerate(base):
        if b in ("ttp", "ttp.sh"):
            sub, rest = words[i + 1:i + 2], words[i + 2:]
            if sub == ["detach"] and "--check" in rest and any(w == "--host" or w.startswith("--host=") for w in rest):
                # the --mirror folder is copied in from the remote host; a reboot does not clear it
                words = [w for j, w in enumerate(words) if not (w.startswith("--mirror=") or w == "--mirror" or (j and words[j - 1] == "--mirror"))]
                break
    joined = " ".join(words)
    base = [w.rsplit("/", 1)[-1] for w in words]
    for i, b in enumerate(base):
        if b in ("ttp", "ttp.sh"):
            sub, rest = words[i + 1:i + 2], words[i + 2:]
            if sub == ["detach"] and "--check" in rest and not any(w == "--host" or w.startswith("--host=") for w in rest):
                return "a local detached job"
            if sub == ["checks"] and "--result" in rest:
                return "its own checks"
            if sub == ["devq"] and rest[:1] == ["probe"]:
                return "a device runner job"
    if any(b in _PROC_CMDS for b in base):
        return "a process"
    if "kill" in base and "-0" in words:
        return "a process"
    if _VOLATILE_RE.search(joined) and not any(w == "--host" or w.startswith("--host=") for w in words):
        return "a path a reboot clears"
    if _RUN_DIR_RE.search(joined) or any(d and d in joined for d in run_dirs):
        return "a file in a run folder"
    return ""


def transient(probe: str, run_dirs: tuple[str, ...] = ()) -> str:
    """Why a retry_when depends on local state a reboot ends (see the module doc), or '' when it does
    not. `run_dirs`: run folder paths of this project. A probe that does not parse is judged on its
    whole text, so it wakes when in doubt."""
    probe = probe.strip() if isinstance(probe, str) else ""
    if not probe:
        return ""
    cmds = _commands(probe)
    if cmds is None:
        return _transient_words(probe.split(), run_dirs)
    for cmd in cmds:
        why = _transient_words(_local_part(cmd), run_dirs)
        if why:
            return why
    return ""


def transient_besides_jobs(probe, run_dirs: tuple[str, ...] = ()) -> str:
    """Why a retry_when depends on local state a reboot ends apart from its local detached jobs (see
    local_jobs) and the run-folder files they write, or '' when nothing else: a process, a path a reboot
    clears, its own checks or a device runner job. A probe that does not parse says so."""
    probe = probe.strip() if isinstance(probe, str) else ""
    cmds = _commands(probe)
    if cmds is None:
        return "a probe that does not parse"
    for cmd in cmds:
        if local_jobs(shlex.join(cmd)):
            continue
        # A file in a run folder is the jobs' own output, wherever the folder lies.
        words = [w for w in _local_part(cmd) if not (_RUN_DIR_RE.search(w) or any(d and d in w for d in run_dirs))]
        why = _transient_words(words, run_dirs)
        if why and why != "a file in a run folder":
            return why
    return ""


def stays_asleep(prev: dict, run_dirs: tuple[str, ...] = ()) -> bool:
    """Whether a waiting hand-off from before the boot sleeps on through it by its own words: it says
    `survives_reboot`, or its retry_when checks nothing local that a reboot ends. A detached job of its
    own that the reboot cut short (no .rc) is the daemon's to check on top."""
    if prev.get("survives_reboot") is True:
        return True
    return not transient(str(prev.get("retry_when") or ""), run_dirs)
