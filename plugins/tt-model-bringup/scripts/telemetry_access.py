#!/usr/bin/env python3
"""Quietly check optional telemetry repository access using existing credentials.

Print the repository/clone URL on success; exit 1 without output otherwise.
This probe never installs anything, starts authentication, or contacts a dashboard.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess

REPOSITORY = "tenstorrent/ar-dashboard"
HTTPS_URL = f"https://github.com/{REPOSITORY}.git"
SSH_URL = f"git@github.com:{REPOSITORY}.git"
TIMEOUT_S = 5


def succeeds(command: list[str], env: dict[str, str]) -> bool:
    try:
        with subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=os.name == "posix") as process:
            try:
                return process.wait(timeout=TIMEOUT_S) == 0
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
    except OSError:
        pass
    return False


def accessible_url() -> str | None:
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_ASKPASS="false", SSH_ASKPASS="false")
    # Reading contents, rather than public repository metadata, proves code access.
    gh = shutil.which("gh")
    if gh and succeeds([gh, "api", "--hostname", "github.com",
                        f"repos/{REPOSITORY}/contents/README.md?ref=main"], env):
        return HTTPS_URL
    git = shutil.which("git")
    if not git:
        return None
    if succeeds([git, "ls-remote", "--exit-code", HTTPS_URL, "HEAD"], env):
        return HTTPS_URL
    # Use existing SSH keys/config without a password or new-host-key prompt.
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5"
    if succeeds([git, "ls-remote", "--exit-code", SSH_URL, "HEAD"], env):
        return SSH_URL
    return None


def main() -> int:
    url = accessible_url()
    if url is None:
        return 1
    print(json.dumps({"repository": REPOSITORY, "clone_url": url}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
