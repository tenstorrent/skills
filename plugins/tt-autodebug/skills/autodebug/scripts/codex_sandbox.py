"""Check Codex sandbox startup without a model call; print the permitted mode."""

import os
import signal
import subprocess
import sys


def select_sandbox(timeout=15):
    skip_child_sandbox = os.environ.get("AUTODEBUG_SKIP_CHILD_SANDBOX", "0")
    if skip_child_sandbox not in {"0", "1"}:
        raise SystemExit("autodebug: AUTODEBUG_SKIP_CHILD_SANDBOX must be 0 or 1")
    if skip_child_sandbox == "1":
        print(
            "autodebug: AUTODEBUG_SKIP_CHILD_SANDBOX=1: skipping the additional Codex "
            "child sandbox and its preflight (danger-full-access, no approval prompts). "
            "Any inherited parent restrictions still apply; this does not establish "
            "that a parent sandbox exists. Fresh-process isolation and inspection-only "
            "instructions remain in effect.",
            file=sys.stderr,
        )
        return "danger-full-access"

    # Inherit the caller's cwd, environment and Codex configuration. The shell
    # exercises sandbox initialization, not the target code or a paid model.
    try:
        with subprocess.Popen(
            ["codex", "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "/bin/sh", "-c", "exit 0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        ) as probe:
            try:
                output, _ = probe.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(probe.pid, signal.SIGKILL)
                probe.communicate()
                raise SystemExit("autodebug: sandbox preflight timed out; no model was started")
    except OSError as error:
        raise SystemExit(f"autodebug: cannot run sandbox preflight: {error}; no model was started") from error

    if probe.returncode == 0:
        return "workspace-write"

    print(output.rstrip(), file=sys.stderr)
    raise SystemExit(
        f"autodebug: sandbox preflight failed (exit {probe.returncode}); no model was started. "
        "The calling agent must assess the failure and existing authorization before "
        "explicitly retrying with AUTODEBUG_SKIP_CHILD_SANDBOX=1."
    )


if __name__ == "__main__":
    print(select_sandbox())
