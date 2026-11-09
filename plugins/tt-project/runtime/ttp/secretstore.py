# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Where this machine keeps `ttp secret` keys: read when a run needs them, never copied elsewhere.

The choice is per machine, in secret-store.json in the ttp home (it holds no secret):
  {"backend": "file"}                        secrets.json in the ttp home, mode 0600 (the default)
  {"backend": "keychain"}                    the macOS login keychain (service tt-project-secrets)
  {"backend": "command", "command": [argv]}  a fetch command that prints the whole store as one
                                             JSON object on stdout (exit 0); read-only
  {"backend": "env"}                         TTP_SECRETS_JSON, read once and removed from this
                                             process's environment so no child inherits it; read-only.
                                             It serves that one process only (a script, a CI job): a
                                             daemon refuses to start with it, since its runs and the
                                             hooks the agent CLI starts would see no store
and "shared": true on a shared or build host: there no key is ever written in plaintext, so the file
backend refuses to save and `ttp secret push` to it is refused. A central mechanism plugs in as a
`command` (any program that prints the JSON) or as one more entry in BACKENDS. Values read from a
non-file backend live only in this process's memory, refreshed every CACHE_S seconds.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import project

CACHE_S = 60
FETCH_TIMEOUT_S = 30
KEYCHAIN_SERVICE = "tt-project-secrets"
ENV_VAR = "TTP_SECRETS_JSON"
_mem: dict = {"key": None, "at": 0.0, "data": None}
_env: dict = {}


class ReadOnly(RuntimeError):
    """This machine's store takes no writes from ttp (a fetch command, the environment, a shared host)."""


def config_path() -> Path:
    return project.HOME_DIR / "secret-store.json"


def config() -> dict:
    try:
        c = json.loads(config_path().read_text())
    except FileNotFoundError:
        return {"backend": "file"}
    except (OSError, ValueError) as e:
        raise project.SecretsUnreadable(f"cannot read {config_path()}: {e}") from None
    if not isinstance(c, dict) or c.get("backend", "file") not in BACKENDS:
        raise project.SecretsUnreadable(f"{config_path()}: backend must be one of {', '.join(BACKENDS)}")
    if c.get("backend") == "command" and not (isinstance(c.get("command"), list) and c["command"]
                                              and all(isinstance(w, str) for w in c["command"])):
        raise project.SecretsUnreadable(f"{config_path()}: the command backend needs \"command\": [argv words]")
    return {"backend": "file", **c}


def set_config(backend: str, command: list[str] | None = None, shared: bool = False) -> dict:
    c = {"backend": backend, **({"command": command} if command else {}), **({"shared": True} if shared else {})}
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.durable_write(config_path(), json.dumps(c), mode=0o600)
    _mem.update(key=None, data=None)
    config()   # validates what was written
    return c


def _parse(text: str, where: str) -> dict:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        raise project.SecretsUnreadable(f"{where} did not give a JSON object") from None
    if not isinstance(data, dict):
        raise project.SecretsUnreadable(f"{where} did not give a JSON object")
    return data


def _file_load(c: dict) -> dict:
    p = project.secrets_path()
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except (OSError, UnicodeDecodeError) as e:   # bad bytes: as unreadable as a hidden store
        raise project.SecretsUnreadable(f"cannot read the secrets store at {p}: "
                                        f"{getattr(e, 'strerror', None) or e}") from None


def _file_save(c: dict, data: dict) -> None:
    if c.get("shared"):
        raise ReadOnly(f"this host is marked shared ({config_path()}): no key is written here in plaintext; "
                       "set a run-time store with `ttp secret store command -- <fetch command>`")
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(project.HOME_DIR, 0o700)
    project.durable_write(project.secrets_path(), json.dumps(data), mode=0o600)


def _account() -> str:
    # The keychain item's account, also a word in the `security -i` line: a $USER that could split or
    # quote that line (spaces, quotes, a newline) is never put in it.
    user = os.environ.get("USER") or ""
    return user if re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user) else "tt-project"


def _keychain_load(c: dict) -> dict:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(), "-w"],
                       capture_output=True, text=True, timeout=FETCH_TIMEOUT_S)
    if r.returncode == 44:   # no such item yet
        return {}
    if r.returncode:
        raise project.SecretsUnreadable(f"keychain item {KEYCHAIN_SERVICE} unreadable (security exit {r.returncode})")
    return _parse(r.stdout, f"keychain item {KEYCHAIN_SERVICE}")


def _keychain_save(c: dict, data: dict) -> None:
    # Hex through `security -i` on stdin: the value is never on a command line.
    line = (f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {_account()} "
            f"-X {json.dumps(data).encode().hex()}\n")
    r = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=FETCH_TIMEOUT_S)
    if r.returncode or "error" in (r.stdout + r.stderr).lower():
        raise project.SecretsUnreadable(f"could not save to keychain item {KEYCHAIN_SERVICE} (exit {r.returncode})")


def _command_load(c: dict) -> dict:
    try:
        r = subprocess.run(c["command"], capture_output=True, text=True, timeout=FETCH_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise project.SecretsUnreadable(f"secret fetch command {c['command'][0]} failed: {type(e).__name__}") from None
    if r.returncode:   # its stderr may hold a value: never repeated
        raise project.SecretsUnreadable(f"secret fetch command {c['command'][0]} exited {r.returncode}")
    return _parse(r.stdout, f"secret fetch command {c['command'][0]}")


def _env_load(c: dict) -> dict:
    if ENV_VAR in os.environ:
        _env["text"] = os.environ.pop(ENV_VAR)
    if "text" not in _env:   # never given to this process: unreadable, not empty, so masking fails closed
        raise project.SecretsUnreadable(
            f"{ENV_VAR} was not given to this process: the env store serves only the one process it is "
            "given to (a script, a CI job), never a daemon's runs or hooks; choose keychain or command there")
    return _parse(_env["text"], ENV_VAR)


def _read_only(c: dict, data: dict) -> None:
    raise ReadOnly(f"this machine's secret store ({c['backend']}) takes no writes from ttp: "
                   "store the key in it with its own tools")


# name -> (load, save, plaintext on disk here)
BACKENDS = {"file": (_file_load, _file_save, True), "keychain": (_keychain_load, _keychain_save, False),
            "command": (_command_load, _read_only, False), "env": (_env_load, _read_only, False)}


def load() -> dict:
    c = config()
    if c["backend"] == "file":
        return BACKENDS["file"][0](c)
    key = json.dumps([str(config_path()), c], sort_keys=True)
    if _mem["key"] == key and time.time() - _mem["at"] < CACHE_S:
        return dict(_mem["data"])
    data = BACKENDS[c["backend"]][0](c)
    _mem.update(key=key, at=time.time(), data=dict(data))
    return data


def save(data: dict) -> None:
    c = config()
    BACKENDS[c["backend"]][1](c, data)
    _mem.update(key=None, data=None)


def signature():
    """Changes when the store may have: the file's stat for the file backend (None: no file), else the
    config and the cache period, so redact re-reads a remote store at most every CACHE_S seconds."""
    try:
        c = config()
    except project.SecretsUnreadable:
        return ("unreadable-config", int(time.time() // CACHE_S))
    if c["backend"] == "file":
        try:
            st = project.secrets_path().stat()
        except OSError:
            return None
        return (str(project.secrets_path()), st.st_mtime_ns, st.st_size, st.st_ino)
    return (json.dumps(c, sort_keys=True), int(time.time() // CACHE_S))


def plaintext_report() -> str:
    """One line when a plaintext key file sits here although it should not (a shared host, or another
    backend chosen), else "". Never deletes it: moving the keys and removing the file is the owner's."""
    p = project.secrets_path()
    try:
        c = config()
        if not p.is_file() or (c["backend"] == "file" and not c.get("shared")):
            return ""
    except (OSError, project.SecretsUnreadable):
        return ""
    if c["backend"] == "file":   # shared: still read, never written
        return (f"plaintext key file at {p} on a host marked shared: ttp still reads it but writes no key "
                "here; move its keys to a run-time store (`ttp secret store keychain|command`), then delete "
                "it yourself")
    return (f"plaintext key file left at {p} (this machine reads keys from {c['backend']}): ttp no longer "
            "reads or writes it; move its keys to the store, then delete it yourself")


def command_words() -> list[str]:
    """Words naming this machine's store reader, for the hook's refusal of runs that read it directly."""
    try:
        c = config()
    except project.SecretsUnreadable:
        return []
    return {"keychain": [KEYCHAIN_SERVICE], "command": [" ".join(c.get("command") or [])]}.get(c["backend"], [])
