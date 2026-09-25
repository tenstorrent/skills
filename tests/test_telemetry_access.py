"""Optional telemetry discovery must be quiet, bounded and noninteractive."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

PATH = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup/scripts/telemetry_access.py"
SPEC = importlib.util.spec_from_file_location("telemetry_access", PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("success", ["gh", "https", "ssh", None])
def test_access_routes_use_existing_credentials_without_prompts(monkeypatch, success, capsys):
    calls = []
    monkeypatch.setattr(probe.shutil, "which", lambda name: name)

    def check(command, env):
        kind = "gh" if command[0] == "gh" else "ssh" if command[-2] == probe.SSH_URL else "https"
        calls.append(kind)
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GCM_INTERACTIVE"] == "never"
        assert env["GIT_ASKPASS"] == env["SSH_ASKPASS"] == "false"
        if kind == "ssh":
            assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
            assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]
        return kind == success

    monkeypatch.setattr(probe, "succeeds", check)
    result = probe.main()
    output = capsys.readouterr()
    assert not output.err
    if success is None:
        assert result == 1 and not output.out
        assert calls == ["gh", "https", "ssh"]
    else:
        assert result == 0
        data = json.loads(output.out)
        assert data["repository"] == "tenstorrent/ar-dashboard"
        assert data["clone_url"] == (probe.SSH_URL if success == "ssh" else probe.HTTPS_URL)
        assert calls[-1] == success


def test_missing_tools_are_silent(monkeypatch, capsys):
    monkeypatch.setattr(probe.shutil, "which", lambda name: None)
    assert probe.main() == 1
    assert capsys.readouterr() == ("", "")


def test_denied_access_cli_does_not_expose_errors_or_credentials(tmp_path):
    for name in ("gh", "git"):
        tool = tmp_path / name
        tool.write_text(f"#!{sys.executable}\nimport sys\nprint('private error', file=sys.stderr)\nraise SystemExit(1)\n")
        tool.chmod(0o755)
    result = subprocess.run([sys.executable, str(PATH)], env={**os.environ, "PATH": str(tmp_path)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""


def test_hung_probe_is_bounded_and_missing_executable_is_unavailable(monkeypatch):
    monkeypatch.setattr(probe, "TIMEOUT_S", 0.05)
    started = time.monotonic()
    assert not probe.succeeds([sys.executable, "-c", "import time; time.sleep(60)"], os.environ.copy())
    assert time.monotonic() - started < 5
    assert not probe.succeeds(["/nonexistent-telemetry-access-tool"], os.environ.copy())
