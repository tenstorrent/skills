# CLAUDE_ENV_FILE tests (loopproxy.py and loopproxy.sh) of tests/test_tt_project_runtime.py, which runs
# this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new CLAUDE_ENV_FILE tests here.
from __future__ import annotations


def _sourced(shell: str, env: dict, prelude: str = "") -> dict:
    """PREV_RAN after `shell` sources loopproxy.sh with `env`, and the script's exit status."""
    from ttp.loopproxy import SCRIPT
    probe = 'printf "%s=%s\\0" PREV_RAN "${PREV_RAN-}"'
    out = subprocess.run([shell, "-c", f'{prelude}. "$1"; echo "rc=$?" >&2; {probe}', "sh", str(SCRIPT)],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env}, timeout=30)
    assert out.returncode == 0 and "rc=0" in out.stderr, out.stderr
    return dict(kv.split("=", 1) for kv in out.stdout.split("\0") if kv)


def test_loopproxy_script_sources_an_inherited_env_file(tmp_path):
    from ttp.loopproxy import PREV_ENV
    prev = tmp_path / "prev.sh"
    prev.write_text('PREV_RAN=ran\n')
    assert _sourced("bash", {PREV_ENV: str(prev)})["PREV_RAN"] == "ran"
    assert _sourced("bash", {PREV_ENV: str(tmp_path / "missing.sh")})["PREV_RAN"] == ""
    assert _sourced("bash", {}, prelude="set -u; ")["PREV_RAN"] == "", "a shell with `set -u` does not stop"


def test_loopproxy_env_file_keeps_an_inherited_one():
    from ttp.loopproxy import PREV_ENV, SCRIPT, env_file
    assert env_file({}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}
    assert env_file({"CLAUDE_ENV_FILE": "/x/env.sh"}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "/x/env.sh"}
    assert env_file({"CLAUDE_ENV_FILE": str(SCRIPT)}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}


def test_loopproxy_env_file_never_chains_to_another_projects_copy():
    """A daemon started from another project's worker shell inherits that project's CLAUDE_ENV_FILE (its
    own loopproxy.sh) and PREV_ENV naming the same file: chaining to it made every worker Bash call
    source that copy forever (exit 1, no output). Another copy is skipped for the file it chains to;
    the inherited PREV_ENV is always overridden."""
    from ttp.loopproxy import PREV_ENV, SCRIPT, env_file
    other = "/other/project/tt-project/harness/runtime/ttp/loopproxy.sh"
    assert env_file({"CLAUDE_ENV_FILE": other, PREV_ENV: other}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}
    assert env_file({"CLAUDE_ENV_FILE": other, PREV_ENV: "/x/env.sh"}) == \
        {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "/x/env.sh"}
    assert env_file({PREV_ENV: other}) == {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: ""}


@pytest.mark.parametrize("shell", ["bash", "dash"])
def test_loopproxy_script_never_sources_itself_forever(tmp_path, shell):
    """PREV_ENV naming this script, or another copy whose own PREV_ENV is the same file, ends after one
    pass: the variable is unset while the previous file runs, and restored afterwards."""
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    from ttp.loopproxy import PREV_ENV, SCRIPT
    other = tmp_path / "ttp" / "loopproxy.sh"
    other.parent.mkdir()
    other.write_text(SCRIPT.read_text())
    for prev in (str(SCRIPT), str(other)):
        _sourced(shell, {PREV_ENV: prev})
        out = subprocess.run([shell, "-c", f'. "$1"; printf "%s" "${{{PREV_ENV}-}}"', "sh", str(SCRIPT)],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"], PREV_ENV: prev},
                             timeout=30)
        assert out.returncode == 0 and out.stdout == prev, out.stderr[-500:]


def test_loopproxy_claude_worker_env_names_the_script(monkeypatch):
    from ttp.loopproxy import SCRIPT
    from ttp.providers.claude import Claude
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    monkeypatch.setattr(Claude, "supports", lambda self, flag: False)
    for read_only in (False, True):
        _, env = Claude().build(role="worker", model="m", effort="high", cwd="/tmp", budget_usd=1.0,
                                read_only=read_only, schema=None, restrictions={})
        assert env["CLAUDE_ENV_FILE"] == str(SCRIPT) and SCRIPT.is_file()


@pytest.mark.parametrize("shell", ["bash", "dash"])
def test_loopproxy_script_and_the_run_zsh_files_put_the_wrapper_folder_first_again(shell, tmp_path):
    """A PATH a command or the user's own startup file changed gets the run's gh wrapper folder back in
    front (loopproxy.sh for Claude Code's Bash, the run's ZDOTDIR files for zsh); once is enough."""
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    from ttp import ghhold
    from ttp.loopproxy import SCRIPT
    wrap, user = tmp_path / "wrap", tmp_path / "user"
    user.mkdir()
    (user / ".zshrc").write_text('PATH="/opt/sys/bin:$PATH"; export PATH; USER_RC=ran\n')
    (tmp_path / "run").mkdir()
    zd = ghhold.zdotdir(tmp_path / "run", str(user))
    base = {"PATH": f"/usr/bin:/bin:{wrap}", "TTP_RUN_BIN": str(wrap), "HOME": str(tmp_path)}
    for script, extra in ((SCRIPT, {}), (zd / ".zshrc", {"ZDOTDIR": str(zd), "TTP_ZDOTDIR_ORIG": str(user)})):
        twice = '. "$1"; ' if script == SCRIPT else ""   # loopproxy.sh runs before every command
        out = subprocess.run([shell, "-c", twice + '. "$1"; printf "%s|%s|%s" "$PATH" "${USER_RC-}" "${ZDOTDIR-}"',
                              "sh", str(script)], capture_output=True, text=True, env={**base, **extra}, timeout=30)
        path, rc, zdot = out.stdout.split("|")
        assert out.returncode == 0 and path.split(":")[0] == str(wrap), (script, out.stdout, out.stderr)
        assert path.split(":").count(str(wrap)) == 2, "put in front once, the old entry left"
        assert (rc, zdot) == (("ran", str(zd)) if extra else ("", "")), (script, out.stdout)
    out = subprocess.run([shell, "-c", '. "$1"; printf %s "$PATH"', "sh", str(SCRIPT)], capture_output=True,
                         text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert out.stdout == "/usr/bin:/bin", "no run bin folder: PATH untouched"
