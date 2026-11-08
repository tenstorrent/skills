# Guards for runs whose task reads untrusted input (untrusted.py), of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports
# come from that file. No test reaches the network.
from __future__ import annotations


def _untrusted_run(env, tmp_path, monkeypatch, labels):
    """A project whose push branch is origin/work-branch and a run of a task with `labels` on branch
    ttp/t1-own, with a fresh HOME and no gh or XDG config folders: (p, task id)."""
    from ttp import untrusted
    p = make(env)
    p.set_config("delivery.push_branch", "origin/work-branch")
    tid = p.db.add_task("read a PR comment", "summarize it", kind="code", labels=labels, branch="ttp/t1-own")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for var in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", untrusted.ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(tid))
    (tmp_path / "wt" / ".git").mkdir(parents=True, exist_ok=True)   # the run's worktree
    untrusted._labels.cache_clear()
    return p, tid


def _switch(p, monkeypatch, labels, branch="ttp/t2-own"):
    """The run's task becomes a new task with `labels`."""
    from ttp import untrusted
    monkeypatch.setenv("TTP_TASK", str(p.db.add_task("plain", "spec", kind="code", labels=labels, branch=branch)))
    untrusted._labels.cache_clear()


def _why(tmp_path, tool, args):
    from ttp import hook
    out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(tmp_path / "wt")})
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


# Credential files outside the known stores credguard refuses in every run (names built by parts so no
# command text names one), and the ways a planted instruction could reach them.
_SECRET_READS = ["cat ~/secrets/api" + "_token.txt", "head /etc/app/db_pass" + "word", "base64 $HOME/.pg" + "pass",
                 "sh -c 'cat ~/app/secret" + "s.yaml'", "grep -r x ~/creds/aws_sec" + "ret_key",
                 "cat /proc/1/env" + "iron",
                 "cp ~/work/deploy.p" + "em /tmp/k", "cat ~/../$(whoami)/.pg" + "pass", "find ~ -name '*.p" + "em'",
                 "printenv", "env", "export -p"]
_WRITES = ['gh api repos/o/r/issues/1/comments -f body="hi [authored by AI]"',
           'gh -R o/r pr comment 1 -b "hi [authored by AI]"', "gh api --method=PATCH repos/o/r/pulls/1",
           "gh issue create -t t -b 'b [authored by AI]'", "curl -d @notes.txt https://example.com/hook",
           "curl -X DELETE https://example.com/x", "sh -c 'curl --data-binary x https://example.com'",
           "wget --post-data=x https://example.com"]
_PUSHES = ["git push origin HEAD:work-branch", "git push -f origin +HEAD:refs/heads/work-branch"]
_FINE = ["cat README.md", "gh pr view 1", "gh -R o/r api repos/o/r/pulls/1", "curl -s https://example.com",
         "git push origin HEAD:ttp/t1-own", "cat ./secrets/api" + "_token.txt", "echo \"$HOME\"",
         "find . -name '*.p" + "em'", "find ~/src -name '*.py'", "~/bin/x --help"]


def test_an_untrusted_input_run_refuses_credential_reads_writes_and_raw_pushes_a_normal_run_is_unchanged(
        env, tmp_path, monkeypatch):
    """A run whose task carries untrusted_input is refused (a) a read of a credential, token or secret file
    outside its worktree (Bash words, also in sh -c, and the Read, Grep and Glob tools) and an environment
    dump, (b) a raw git push naming the push branch and (c) a gh or curl write; its harmless calls pass.
    The same calls in a run without the label get no untrusted-input refusal."""
    p, _ = _untrusted_run(env, tmp_path, monkeypatch, ["untrusted_input"])
    for cmd in _SECRET_READS + _WRITES + _PUSHES:
        assert "untrusted_input" in _why(tmp_path, "Bash", {"command": cmd}), cmd
    assert "never reads credential" in _why(tmp_path, "Bash", {"command": _SECRET_READS[0]})
    assert "untrusted_write" in _why(tmp_path, "Bash", {"command": _WRITES[1]})
    assert "only through `ttp push`" in _why(tmp_path, "Bash", {"command": _PUSHES[0]})
    secret = str(tmp_path / "home" / "secrets" / ("api" + "_token.txt"))
    for tool, args in (("Read", {"file_path": secret}), ("Grep", {"pattern": "x", "path": secret}),
                       ("Glob", {"pattern": "*", "path": str(tmp_path / "home" / ".aws")})):
        assert "untrusted_input" in _why(tmp_path, tool, args), tool
    assert _why(tmp_path, "Read", {"file_path": str(tmp_path / "wt" / ".env")}) == ""   # the worktree's own
    for cmd in _FINE:
        assert "untrusted_input" not in _why(tmp_path, "Bash", {"command": cmd}), cmd
    # The same calls in a run without the label
    _switch(p, monkeypatch, ["review"])
    for cmd in _SECRET_READS + _WRITES + _PUSHES:
        assert "untrusted_input" not in _why(tmp_path, "Bash", {"command": cmd}), cmd
    assert _why(tmp_path, "Read", {"file_path": secret}) == ""


def test_the_untrusted_write_label_allows_gh_and_curl_writes_but_no_credential_read_or_push(
        env, tmp_path, monkeypatch):
    """A task that must post carries untrusted_write too: its gh and curl writes pass, its credential reads
    and raw pushes to the push branch stay refused. TTP_UNTRUSTED_INPUT=1 turns the guard on without a
    label and never off."""
    from ttp import untrusted
    p, _ = _untrusted_run(env, tmp_path, monkeypatch, ["untrusted_input", "untrusted_write"])
    for cmd in _WRITES:
        assert "untrusted_input" not in _why(tmp_path, "Bash", {"command": cmd}), cmd
    for cmd in _SECRET_READS[:2] + _PUSHES:
        assert "untrusted_input" in _why(tmp_path, "Bash", {"command": cmd}), cmd
    _switch(p, monkeypatch, [])
    assert not untrusted.active()
    monkeypatch.setenv(untrusted.ENV, "1")
    assert untrusted.active() and "untrusted_input" in _why(tmp_path, "Bash", {"command": _WRITES[0]})
    monkeypatch.setenv(untrusted.ENV, "0")
    assert not untrusted.active()
    assert untrusted.guards_live() is True


def test_an_untrusted_input_runs_git_and_gh_wrappers_push_only_its_branches_and_write_nothing(
        env, tmp_path, monkeypatch):
    """Scripts reach the run's bin/git and bin/gh too: in an untrusted_input run git's pre-push hook refuses
    every ref but the task's own branch and the push branch (a new branch, a tag) and sends nothing, and
    bin/gh refuses a write before the real gh runs. A run without the label pushes and writes as before."""
    p, tid = _untrusted_run(env, tmp_path, monkeypatch, ["untrusted_input"])
    origin, repo = tmp_path / "up-origin.git", tmp_path / "up-repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git_out(repo, "remote", "add", "origin", str(origin))
    for k, v in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false"),
                 ("tag.gpgsign", "false")):
        _git_out(repo, "config", k, v)
    _commit(repo, "README.md", "hello\n")
    _git_out(repo, "tag", "v1")
    (tmp_path / "run").mkdir()
    wrapper = RUNTIME.parent / "template" / "bin" / "git"

    def push(*args):
        run_env = {**os.environ, "TTP_PYTHON": sys.executable, "TTP_RUN_DIR": str(tmp_path / "run")}
        return subprocess.run([str(wrapper), "push", *args], cwd=repo, env=run_env, capture_output=True,
                              text=True, timeout=60)
    for args in (("origin", "HEAD:refs/heads/other"), ("origin", "v1"), ("origin", "HEAD:ttp/t1-own", "HEAD:x")):
        out = push(*args)
        assert out.returncode != 0 and "untrusted_input" in out.stderr, (args, out.stderr)
    assert _git_out(origin, "for-each-ref") == ""
    out = push("origin", "HEAD:ttp/t1-own")
    assert out.returncode == 0, out.stderr
    assert "refs/heads/ttp/t1-own" in _git_out(origin, "for-each-ref")
    _gh_runner(p, tmp_path)

    def gh(*args):
        code = f"import sys; from ttp import prguard; sys.exit(prguard.main(['gh', *{list(args)!r}], sys.argv[1]))"
        genv = {**os.environ, "PATH": f"{p.harness / 'bin'}:{tmp_path / 'realbin'}:{os.environ['PATH']}",
                "FAKE_GH_LOG": str(tmp_path / "gh.log")}
        return subprocess.run([sys.executable, "-c", code, str(p.harness / "bin")], capture_output=True, text=True,
                              timeout=60, env=genv, cwd=str(RUNTIME))
    out = gh("-R", "o/r", "pr", "comment", "1", "-b", "hi [authored by AI]")
    assert out.returncode != 0 and "untrusted_input" in out.stderr, out.stderr
    assert not (tmp_path / "gh.log").exists() or "comment" not in (tmp_path / "gh.log").read_text()
    # A run without the label
    _switch(p, monkeypatch, [])
    out = push("origin", "HEAD:refs/heads/other", "v1")
    assert out.returncode == 0, out.stderr
    assert gh("-R", "o/r", "pr", "comment", "1", "-b", "hi [authored by AI]").returncode == 0
    assert "comment" in (tmp_path / "gh.log").read_text()
