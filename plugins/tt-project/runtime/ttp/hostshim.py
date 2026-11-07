# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The stand-in a test run's PATH finds for crontab, systemctl, loginctl, at, atq, atrm, batch, reboot,
shutdown, poweroff, halt, ipmitool and tt-smi (hoststate.py writes one small sh script per name).

Usage: `python3 -I hostshim.py <store> <name> [args...]`. A write (a new crontab, an enabled or
started unit, an at job, a power command, a device or BMC reset) changes only the fake store and is
recorded in <store>/calls.jsonl; a read is served from the store where it holds the answer, else
passed to the real command (found on PATH without this folder). Standard library only: it runs
isolated, outside the ttp package."""
import json
import os
import shutil
import sys
import time

POWER = {"reboot", "shutdown", "poweroff", "halt"}
SYSTEMCTL_READS = {"status", "show", "cat", "is-active", "is-enabled", "is-failed", "is-system-running",
                   "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies",
                   "list-jobs", "list-machines", "get-default", "show-environment", "help"}
LOGINCTL_WRITES = ("enable-linger", "disable-linger", "terminate-", "kill-", "lock-", "unlock-", "attach",
                   "flush-devices", "poweroff", "reboot", "halt", "suspend", "hibernate")
IPMI_WRITES = {"on", "off", "cycle", "reset", "soft", "bootdev", "raw", "cold", "warm", "set", "clear",
               "delete", "identify", "diag", "bootparam", "activate"}
TTSMI_RESETS = ("-r", "--reset", "-glx_reset", "--glx_reset", "-glx_reset_auto", "--glx_reset_auto",
                "-glx_reset_tray", "--glx_reset_tray")


def _log(store, name, args, write):
    with open(os.path.join(store, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"ts": time.time(), "cmd": name, "argv": args, "write": write}) + "\n")


def _load(store, name, empty):
    try:
        with open(os.path.join(store, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return empty


def _save(store, name, doc):
    tmp = os.path.join(store, f".{name}.tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, os.path.join(store, name))


def _real(store, name, args):
    """Pass a read on to the real command (never this folder's stand-in)."""
    mine = os.path.realpath(os.path.join(store, "bin"))
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and os.path.realpath(d) != mine and not os.path.exists(os.path.join(d, ".ttp-shims")))
    real = shutil.which(name, path=path)
    if not real:
        print(f"{name}: command not found", file=sys.stderr)
        return 127
    os.execv(real, [name, *args])


def crontab(store, args):
    tab = os.path.join(store, "crontab")
    rest = [a for i, a in enumerate(args) if a != "-u" and (i == 0 or args[i - 1] != "-u")]
    if "-l" in rest:
        _log(store, "crontab", args, False)
        if not os.path.exists(tab):
            print(f"no crontab for {os.environ.get('USER', 'user')}", file=sys.stderr)
            return 1
        with open(tab) as f:
            sys.stdout.write(f.read())
        return 0
    _log(store, "crontab", args, True)
    if "-r" in rest:
        if os.path.exists(tab):
            os.unlink(tab)
        return 0
    if "-e" in rest:
        print("crontab -e: not run in a test run (the crontab is a fake)", file=sys.stderr)
        return 0
    src = [a for a in rest if not a.startswith("-") or a == "-"]
    text = sys.stdin.read() if not src or src[0] == "-" else open(src[0]).read()
    with open(tab + ".tmp", "w") as f:
        f.write(text)
    os.replace(tab + ".tmp", tab)
    return 0


def systemctl(store, args):
    words = [a for a in args if not a.startswith("-")]
    verb, units = (words[0], words[1:]) if words else ("list-units", [])
    state = _load(store, "units.json", {"enabled": [], "active": [], "masked": []})
    if verb in SYSTEMCTL_READS:
        _log(store, "systemctl", args, False)
        if verb in ("is-active", "is-enabled") and units and all(
                u in state["enabled"] + state["active"] + state["masked"] for u in units):
            key = "active" if verb == "is-active" else "enabled"
            for u in units:
                print("masked" if u in state["masked"] else key if u in state[key] else f"in{key}")
            return 0 if all(u in state[key] for u in units) else 1
        return _real(store, "systemctl", args)
    _log(store, "systemctl", args, True)
    now = "--now" in args
    for u in units:
        for key, on in {"enable": [("enabled", True)] + ([("active", True)] if now else []),
                        "disable": [("enabled", False)] + ([("active", False)] if now else []),
                        "start": [("active", True)], "restart": [("active", True)],
                        "reload-or-restart": [("active", True)], "try-restart": [("active", True)],
                        "stop": [("active", False)], "mask": [("masked", True)],
                        "unmask": [("masked", False)]}.get(verb, []):
            state[key] = sorted(set(state[key]) | {u}) if on else [x for x in state[key] if x != u]
    _save(store, "units.json", state)
    return 0


def loginctl(store, args):
    words = [a for a in args if not a.startswith("-")]
    if words and words[0].startswith(LOGINCTL_WRITES):
        _log(store, "loginctl", args, True)
        return 0
    _log(store, "loginctl", args, False)
    return _real(store, "loginctl", args)


def at(store, name, args):
    jobs = _load(store, "at.json", [])
    if name == "atq" or (name == "at" and "-l" in args):
        _log(store, name, args, False)
        for j in jobs:
            print(f"{j['id']}\t{j['when']}")
        return 0
    _log(store, name, args, True)
    if name == "atrm" or (name == "at" and ("-r" in args or "-d" in args)):
        drop = {a for a in args if not a.startswith("-")}
        jobs = [j for j in jobs if str(j["id"]) not in drop]
    else:
        n = max([j["id"] for j in jobs] or [0]) + 1
        jobs.append({"id": n, "when": " ".join(a for a in args if not a.startswith("-")) or "now",
                     "script": sys.stdin.read() if not sys.stdin.isatty() else ""})
        print(f"job {n} at (a test run's fake: never runs)", file=sys.stderr)
    _save(store, "at.json", jobs)
    return 0


def main(argv):
    store, name, args = argv[1], argv[2], argv[3:]
    if name == "crontab":
        return crontab(store, args)
    if name == "systemctl":
        return systemctl(store, args)
    if name == "loginctl":
        return loginctl(store, args)
    if name in ("at", "atq", "atrm", "batch"):
        return at(store, name, args)
    if name in POWER:
        _log(store, name, args, True)
        print(f"{name}: not run in a test run (recorded in a fake store)", file=sys.stderr)
        return 0
    if name == "ipmitool" and IPMI_WRITES & {a.lower() for a in args}:
        _log(store, name, args, True)
        return 0
    if name == "tt-smi" and any(a.split("=")[0] in TTSMI_RESETS for a in args):
        _log(store, name, args, True)
        print("tt-smi: a reset is not run in a test run (recorded in a fake store)", file=sys.stderr)
        return 0
    _log(store, name, args, False)
    return _real(store, name, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
