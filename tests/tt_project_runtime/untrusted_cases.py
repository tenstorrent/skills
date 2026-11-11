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
                 "printenv", "env", "export -p", "cat ~/app/gh_" + "token", "cat ~/app/access-" + "tokens.json",
                 "cat ~/app/accessT" + "oken.json", "cat ~/app/TOK" + "EN", "cat ~/app/client_secret" + ".json",
                 "find ~ -name '*tok" + "en*'", "cat ~/app/auth" + "token", "cat ~/app/my" + "token.txt",
                 "cat ~/app/my" + "secret.txt"]
_WRITES = ['gh api repos/o/r/issues/1/comments -f body="hi [authored by AI]"',
           'gh -R o/r pr comment 1 -b "hi [authored by AI]"', "gh api --method=PATCH repos/o/r/pulls/1",
           "gh issue create -t t -b 'b [authored by AI]'", "curl -d @notes.txt https://example.com/hook",
           "curl -X DELETE https://example.com/x", "sh -c 'curl --data-binary x https://example.com'",
           "wget --post-data=x https://example.com", "curl -sX DELETE https://example.com/x",
           "curl -sSX POST https://example.com/x", "curl -sXPUT https://example.com/x",
           "curl -fsSLX PATCH https://example.com/x", "curl -s0X DELETE https://example.com/x"]
_PUSHES = ["git push origin HEAD:work-branch", "git push -f origin +HEAD:refs/heads/work-branch"]
_FINE = ["cat README.md", "gh pr view 1", "gh -R o/r api repos/o/r/pulls/1", "curl -s https://example.com",
         "git push origin HEAD:ttp/t1-own", "cat ./secrets/api" + "_token.txt", "echo \"$HOME\"",
         "find . -name '*.p" + "em'", "find ~/src -name '*.py'", "~/bin/x --help",
         "cat ~/.cache/hf/models/m/tokenizer.json", "head ~/.cache/hf/models/m/tokenizer_config.json",
         "cat ~/.cache/m/Tokeniser.model",
         "curl -sS https://example.com/x", "curl -sSX GET https://example.com/x"]


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


# The label per source (set model-free, inherited, never routed), ported from #6702/#7231.
def _untrusted_labels(t):
    return json.loads(t["labels"] or "[]")


def test_the_daemon_labels_a_pr_findings_fix_and_a_shared_check_fix(env):
    from ttp import sharedchecks, untrusted
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    p.db.update_task(src, status="done", pr_url="https://github.com/acme/widgets/pull/7")
    fix, made = Daemon(p.base).pr_findings_owner("acme/widgets#7", {"task": src, "url": "https://github.com/acme/"
                                                 "widgets/pull/7"}, "CI failing: tests")
    assert made and untrusted.labelled(fix), "PR comments and CI logs are untrusted text"
    t = p.db.task(p.db.add_task("Run checks", "s", kind="code"))
    fid, opened = sharedchecks.incident(p.db, t, ["tests/x.py::test_a"], "E   boom", time.time(), {})
    assert opened and untrusted.labelled(p.db.task(p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?",
                                                            (fid,))["fix_task"]))
    assert not untrusted.labelled(t) and not untrusted.labelled(p.db.task(src)), "the source tasks keep theirs"


def test_the_coordinator_sets_the_label_by_flag_or_peer_note_tag_and_continuations_inherit_it(env):
    from ttp import coordinator as coord, untrusted
    p = make(env)

    def add(title, **a):
        assert coord.apply(p, [{"type": "task_add", "title": title, "spec": a.pop("spec", "s"), **a}]) == []
        return p.db.one("SELECT * FROM tasks WHERE title=?", (title,))

    assert untrusted.labelled(add("triage the issue", untrusted_input=True))
    assert untrusted.labelled(add("answer the peer", spec="The other project asks [note 3fa9c2e1] for a rerun."))
    plain = add("plain work", spec="Rename the helper; see note 12 in the guide.")
    assert not untrusted.labelled(plain), "no flag and no note tag: no label"
    assert not untrusted.labelled(add("string flag", untrusted_input="yes")), "only a real true sets it"
    src = add("read the logs", untrusted_input=True)
    p.db.update_task(src["id"], status="failed")
    redo = add("redo the logs", continues=src["id"])
    assert untrusted.labelled(redo), "a continuation of a labelled task inherits the label"
    p.db.update_task(plain["id"], status="failed")
    assert not untrusted.labelled(add("redo plain", continues=plain["id"]))


def test_a_review_fix_of_a_labelled_change_inherits_the_label(env):
    from ttp import untrusted
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("budget.max_tasks_per_24h", 1000)
    cid, _, _, (rev,) = _finish_code(env, p, "triage change", {"app.py": 20}, before=lambda tid, head: p.db.update_task(
        tid, labels=json.loads(p.db.task(tid)["labels"] or "[]") + [untrusted.LABEL]))
    p.db.update_task(rev["id"], status="failed")
    fid, rid, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    assert untrusted.labelled(p.db.task(fid)) and f"continues:{cid}" in _untrusted_labels(p.db.task(fid))


def test_a_run_that_reads_web_connector_or_downloaded_text_labels_its_task_when_it_ends(env, tmp_path, monkeypatch):
    from ttp import coordtools, hook, untrusted
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("look it up", "s", kind="work")
    monkeypatch.setenv("TTP_TASK", str(tid))
    monkeypatch.setenv(coordtools.ROLE_ENV, "worker")
    sources = {}
    for i, (tool, inp, want) in enumerate([
            ("WebFetch", {"url": "https://example.com"}, ["web"]),
            ("mcp__atlassian__getJiraIssue", {"issueIdOrKey": "X-1"}, ["connector"]),
            ("Bash", {"command": "curl -sL https://example.com/a.tar.gz -o a.tgz"}, ["download"]),
            ("Bash", {"command": "cd x && wget --url=ftp://example.org/f"}, ["download"]),
            ("Bash", {"command": "curl -s http://localhost:8080/status"}, []),
            ("Bash", {"command": "git log -1"}, []),
            ("Read", {"file_path": "/tmp/x"}, [])]):
        d = tmp_path / str(i)
        d.mkdir()
        hook.mark_untrusted({"tool_name": tool, "tool_input": inp}, str(d))
        hook.mark_untrusted({"tool_name": tool, "tool_input": inp}, str(d))   # once per source
        assert untrusted.marked(d) == want, (tool, inp)
        sources[i] = d
    monkeypatch.setenv(coordtools.ROLE_ENV, "coordinator")
    hook.mark_untrusted({"tool_name": "WebFetch", "tool_input": {}}, str(tmp_path / "6"))
    assert untrusted.marked(tmp_path / "6") == [], "a coordinator run marks nothing"
    d = Daemon(p.base)
    d._carry_untrusted(tid, 1, untrusted.marked(sources[0]))
    d._carry_untrusted(tid, 2, ["connector"])
    assert _untrusted_labels(p.db.task(tid)).count(untrusted.LABEL) == 1
    assert "labelled untrusted_input: its run 1 read web text" in _daemon_log_text(p)


def test_a_labelled_task_never_routes_to_the_chat_provider_and_the_run_note_records_it(env, monkeypatch):
    from ttp import route, untrusted
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = dict(d.cfg, core_provider="claude", providers={**d.cfg["providers"], "chat": {
        "tiers": {"light": {"route": True, "model": "glm-x"}, "standard": {"route": True, "model": "glm-x"}}}})
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    now = time.time()
    plain = p.db.task(p.db.add_task("Copy", "Copy the log.", tier="light", origin="coordinator"))
    bad = p.db.task(p.db.add_task("Copy too", "Copy the log.", tier="light", origin="coordinator",
                                  labels=[untrusted.LABEL]))
    assert route.eligible(plain, None) and not route.eligible(bad, None)
    assert d._route(plain, "light", None, now)["to"] == "chat"
    assert d._route(bad, "light", None, now) is None
    p.db.update_task(plain["id"], status="done")
    d.dispatch()
    (a, kw), = started
    assert a[2] == "claude" and kw["note"][untrusted.KEY] == {"set": True, "kept_core": "chat:glm-x"}


@pytest.mark.parametrize("labels", [[], ["untrusted_input"]])
def test_a_task_run_may_not_remove_or_edit_its_untrusted_input_mark(env, tmp_path, monkeypatch, labels):
    """Removing the run's untrusted-input file would lift the narrowed policy and the task's label at the
    end: a Bash command naming it and an edit of it are refused, marked or not; other calls pass."""
    from ttp import untrusted
    _untrusted_run(env, tmp_path, monkeypatch, labels)
    mark = str(tmp_path / untrusted.RUN_MARK)
    for tool, args in [("Bash", {"command": f"rm {mark}"}), ("Bash", {"command": "rm $TTP_RUN_DIR/untrusted-input"}),
                       ("Bash", {"command": "echo web > untrusted-input"}), ("Write", {"file_path": mark, "content": ""}),
                       ("Edit", {"file_path": mark, "old_string": "web", "new_string": ""}),
                       ("apply_patch", {"input": "*** Delete File: " + mark})]:
        assert "leave it as it is" in _why(tmp_path, tool, args), (tool, args)
    assert _why(tmp_path, "Bash", {"command": "ls $TTP_RUN_DIR/result.json"}) == ""
    assert _why(tmp_path, "Write", {"file_path": str(tmp_path / "result.json"), "content": "{}"}) == ""
