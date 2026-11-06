"""The account's local leak-pattern file, which checks and `ttp sandbox` pass on by path.

A repository's leak test reads setup-specific patterns (internal domains, host names, user names) from a
file outside the repository: $TTP_LEAK_PATTERNS_FILE, else $XDG_CONFIG_HOME/tt-project/leak-patterns.txt
(default ~/.config/tt-project/leak-patterns.txt). A check or test run may give its commands another HOME,
where that file is missing and the test skips; check_env (push) and `ttp sandbox` set the variable to the
file found from the invoking account's own home, so the test runs. Only the path goes on: the file is never
read here, printed, copied or sent to another host (a check run elsewhere gets only its own `env`)."""
from __future__ import annotations

import os
import pwd

ENV = "TTP_LEAK_PATTERNS_FILE"
REL = os.path.join("tt-project", "leak-patterns.txt")


def _readable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _account() -> str:
    """This account's home from the password database, or ""."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return ""


def path(env: dict | None = None) -> str:
    """The absolute path of a readable pattern file for `env` (default this process's environment), or "":
    $TTP_LEAK_PATTERNS_FILE when set, else under $XDG_CONFIG_HOME, $HOME/.config or the account's home
    from the password database (a temp HOME has none), the first that holds one."""
    env = os.environ if env is None else env
    if v := env.get(ENV):
        p = os.path.abspath(os.path.expanduser(v))
        return p if _readable(p) else ""
    homes = [env.get("XDG_CONFIG_HOME") or "", *(os.path.join(h, ".config") for h in (env.get("HOME"), _account()) if h)]
    return next((p for h in homes if os.path.isabs(h) and _readable(p := os.path.join(h, REL))), "")


def with_path(env: dict) -> dict:
    """`env` with TTP_LEAK_PATTERNS_FILE set to path(env) when there is one; as it is otherwise (a set
    variable naming no readable file stays, so the test says why it skips)."""
    return {**env, ENV: p} if (p := path(env)) else env
