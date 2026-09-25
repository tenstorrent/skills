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


def accessible_repository() -> dict[str, str] | None:
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_ASKPASS="false", SSH_ASKPASS="false")
    git = shutil.which("git")
    if not git:
        return None
    if succeeds([git, "ls-remote", "--exit-code", HTTPS_URL, "HEAD"], env):
        return {"repository": REPOSITORY, "clone_url": HTTPS_URL, "auth_method": "git"}
    # Verify Git can use gh's existing credentials, not just API access. Keep the
    # helper override command-local and report it for the installation to reuse.
    if shutil.which("gh") and succeeds(
        [git, "-c", "credential.https://github.com.helper=",
         "-c", "credential.https://github.com.helper=!gh auth git-credential",
         "ls-remote", "--exit-code", HTTPS_URL, "HEAD"], env
    ):
        return {"repository": REPOSITORY, "clone_url": HTTPS_URL, "auth_method": "gh"}
    # Use existing SSH keys/config without a password or new-host-key prompt.
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5"
    if succeeds([git, "ls-remote", "--exit-code", SSH_URL, "HEAD"], env):
        return {"repository": REPOSITORY, "clone_url": SSH_URL, "auth_method": "ssh"}
    return None


def main() -> int:
    repository = accessible_repository()
    if repository is None:
        return 1
    print(json.dumps(repository))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
