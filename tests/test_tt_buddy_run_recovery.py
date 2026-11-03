"""The tt-buddy run skill's recovery guidance must match tt-device-mcp's post-kill recovery."""

from __future__ import annotations

import pathlib
import re


RUN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-buddy" / "skills" / "run"
SKILL_MD = (RUN / "SKILL.md").read_text(encoding="utf-8")
MCP_MD = (RUN / "mcp.md").read_text(encoding="utf-8")
RECOVERY_MD = (RUN / "recovery.md").read_text(encoding="utf-8")
EXECUTION_MD = (RUN / "execution.md").read_text(encoding="utf-8")


def substitution_row(step: str) -> str:
    match = re.search(rf"^\| {re.escape(step)} \|(.*)\|$", MCP_MD, re.MULTILINE)
    assert match, f"mcp.md § Recovery substitutions has no '{step}' row"
    return match.group(1)


def test_reset_after_kill_is_conditional():
    """The broker resets and runs the full check after a kill; agents add one only when asked."""
    assert "## After a kill" in MCP_MD
    assert "After a kill" in substitution_row("3. Reset")
    assert "reset (only if the backend needs one)" in RECOVERY_MD
    assert "triage → kill → reset → verify" not in RECOVERY_MD
    assert "kill → reset → verify" not in SKILL_MD


def test_post_kill_reset_rule_holds_on_released_brokers():
    """Released tt-device-mcp already resets after a kill: no version gate or 1.0.0 exception."""
    after_kill = MCP_MD.split("## After a kill", 1)[1].split("\n## ", 1)[0]
    assert "startup_verify" not in MCP_MD
    assert "1.0.0" not in after_kill
    assert "any kill, timeout or forced preemption" in after_kill
    assert "runs none of its own" in after_kill


def test_verify_goes_through_the_broker():
    """Verify reads the broker's status: tt-smi reads the device outside the broker."""
    verify = substitution_row("4. Verify").strip()
    assert verify.startswith("`tt_device_queue_status`")


def test_no_shell_kill_of_any_signal():
    for name, text in (("mcp.md", MCP_MD), ("recovery.md", RECOVERY_MD)):
        assert "any signal" in text, f"{name}: shell-kill ban covers only some signals"
        assert "NEVER `kill -9` via Bash" not in text, name


def test_broker_limits_match_tt_device_mcp():
    assert "60s cap" not in MCP_MD
    assert "default 180s, max 600s" in MCP_MD
    assert "default 600s, hard max 1500s" in MCP_MD


def test_wedge_detection_and_timeout_envelope_are_linked():
    assert "## Detect the wedge early" in RECOVERY_MD
    assert "## Job-timeout envelope" in EXECUTION_MD
    assert "## Long-run watchdog" in SKILL_MD
    for section in ("§ Job-timeout envelope", "§ Detect the wedge early"):
        assert section in SKILL_MD, f"SKILL.md does not point to {section}"
