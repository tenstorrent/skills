# Worker gh guard tests (prguard.py: drafts, ready and reviewers over gh api) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file, and pytest collects these tests under its name. Add new gh guard route tests here.
from __future__ import annotations

_READY = 'query=mutation{markPullRequestReadyForReview(input:{pullRequestId:"PR_kwDOabc123"}){clientMutationId}}'
_CREATE = 'query=mutation{createPullRequest(input:{repositoryId:"R_1",title:"t"%s}){clientMutationId}}'
_REVIEW = 'query=mutation{requestReviews(input:{pullRequestId:"PR_kwDOabc123",userIds:["U_1"]}){clientMutationId}}'


def _approve_7(p):
    from ttp import prguard
    p.db.set_kv(prguard.APPROVALS_KEY, {"acme/widgets#7": {"answer": 1, "head": HEAD, "spent": None}})
    _heads_seen(p, "acme/widgets#7")


_GHE_GRAPHQL = ("https://ghe.example.com/api/graphql", "https://GHE.example.com/API/GraphQL/")


def test_workers_gh_marks_ready_over_every_api_route_only_with_the_users_approval(env, tmp_path, monkeypatch):
    """Each gh api spelling that takes a PR out of draft gets gh pr ready's decision: refused without the
    user's recorded approval, let through once with it."""
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    routes = [("api", "graphql", "-f", _READY), ("api", "/graphql", "-f", _READY), ("api", "graphql/", "-f", _READY),
              ("api", "GraphQL", "-f", _READY), ("api", "https://api.github.com/graphql", "-f", _READY),
              ("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-F", "draft=false"),
              ("api", "-X", "PATCH", "repositories/123/pulls/7", "-F", "draft=false"),
              ("api", "-X", "PATCH", "/repositories/123/pulls/7/", "-F", "draft=false")]
    for args in routes + [("pr", "ready", "7")]:
        rc, err, ran = gh(*args)
        assert rc == 1 and "without the user's recorded approval" in err and ran == [], (args, err)
    for args in routes + [("pr", "ready", "7")]:
        _approve_7(p)
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)
        rc, err, ran = gh(*args)   # the approval is spent
        assert rc == 1 and ran == [], args
    # Back to draft needs no approval, like gh pr ready --undo.
    rc, err, ran = gh("api", "/graphql", "-f",
                      'query=mutation{convertPullRequestToDraft(input:{pullRequestId:"PR_kwDOabc123"}){clientMutationId}}')
    assert rc == 0 and ran, err
    # GitHub Enterprise: an absolute URL (gh sends it as-is; GraphQL is at /api/graphql) is the same
    # route, and its PR is looked up on that host.
    monkeypatch.setenv("FAKE_GH_PR_HOST", "ghe.example.com")
    gh = _gh_runner(p, tmp_path)
    ghe = [("api", ep, "-f", _READY) for ep in _GHE_GRAPHQL]
    ghe += [("api", "-X", "PATCH", "https://ghe.example.com/api/v3/repos/acme/widgets/pulls/7", "-F", "draft=false")]
    for args in ghe:
        rc, err, ran = gh(*args)
        assert rc == 1 and "without the user's recorded approval" in err and ran == [], (args, err)
        _approve_7(p)
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)


def test_workers_gh_opens_drafts_only_over_every_api_route(env, tmp_path):
    """Like gh pr create without --draft, a create without the draft flag is refused on every route,
    approval or not; with it, it runs."""
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    _approve_7(p)
    for ep in ("graphql", "/graphql", "graphql/", "GRAPHQL", *_GHE_GRAPHQL):
        rc, err, ran = gh("api", ep, "-f", _CREATE % "")
        assert rc == 1 and "drafts only" in err and ran == [], (ep, err)
        rc, err, ran = gh("api", ep, "-f", _CREATE % ",draft:true")
        assert rc == 0 and ran, (ep, err)
    for ep in ("repos/acme/widgets/pulls", "repositories/123/pulls", "/repositories/123/pulls/",
               "https://ghe.example.com/api/v3/repos/acme/widgets/pulls"):
        rc, err, ran = gh("api", ep, "-f", "title=t", "-f", "head=x", "-f", "base=main")
        assert rc == 1 and "drafts only" in err and ran == [], (ep, err)
        rc, err, ran = gh("api", ep, "-F", "draft=true", "-f", "title=t")
        assert rc == 0 and ran, (ep, err)
    rc, err, ran = gh("pr", "create", "-t", "t", "-b", "b")
    assert rc == 1 and "refused" in err and ran == [], err


def test_workers_gh_never_requests_human_reviewers_over_any_api_route(env, tmp_path):
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    _approve_7(p)   # an approval to leave draft is not one to request reviews
    refused = [("api", ep, "-f", _REVIEW) for ep in ("graphql", "/graphql", "graphql/", "GraphQL", *_GHE_GRAPHQL)]
    refused += [("api", "repositories/123/pulls/7/requested_reviewers", "-f", "reviewers[]=someone"),
                ("api", "-X", "POST", "/repositories/123/pulls/7/requested_reviewers/", "-f", "team_reviewers[]=core"),
                ("api", "/graphql", "-f", _REVIEW.replace("requestReviews", "requestReviewsByLogin"))]
    for args in refused:
        rc, err, ran = gh(*args)
        assert rc == 1 and "never request human reviewers" in err and ran == [], (args, err)
    allowed = [("api", "repositories/123/pulls/7/requested_reviewers", "-f",
                "reviewers[]=copilot-pull-request-reviewer[bot]"),
               ("api", "-X", "DELETE", "repositories/123/pulls/7/requested_reviewers", "-f", "reviewers[]=someone"),
               ("api", "repositories/123/pulls/7/requested_reviewers")]
    for args in allowed:
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)


# --- the project's gh pre-hooks (bin/gh.d) ---------------------------------------------------------

def _gh_hook(p, name, body, mode=0o755):
    d = p.harness / "bin" / "gh.d"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    (d / name).chmod(mode)
    return d / name


def _gh_main(p, tmp_path, args, data=b"", fake=None, timeout_s=None, stdin=None):
    """bin/gh's prguard.main run in a child with `data` on stdin, its real gh `fake` (default FAKE_GH)."""
    from ttp import prguard
    if fake is None:
        _gh_runner(p, tmp_path)
        fake = tmp_path / "realbin"
    code = ("import sys; from ttp import prguard; "
            + (f"prguard.HOOK_TIMEOUT_S = {timeout_s}; " if timeout_s else "")
            + f"sys.exit(prguard.main(['gh', *{list(args)!r}], sys.argv[1]))")
    env = {**os.environ, "PATH": f"{p.harness / 'bin'}:{fake}:{os.environ['PATH']}", "TTP_PROJECT": str(p.base),
           "FAKE_GH_LOG": str(tmp_path / "gh.log")}
    env.pop("TTP_RUN_DIR", None)
    kw = {"stdin": stdin} if stdin is not None else {"input": data}
    return subprocess.run([sys.executable, "-c", code, str(p.harness / "bin")], capture_output=True, timeout=60,
                          env=env, cwd=str(pathlib.Path(prguard.__file__).parents[1]), **kw)


def test_gh_pre_hooks_can_only_refuse_and_the_draft_guard_still_runs_after_them(env, tmp_path):
    p = make(env)
    readme = p.harness / "bin" / "gh.d" / "README.md"
    assert readme.is_file() and "can only refuse" in readme.read_text()   # a fresh project gets the contract
    gh = _gh_runner(p, tmp_path)
    seen = tmp_path / "hook.log"
    _gh_hook(p, "10-public-text", f'#!/bin/sh\necho "$@" >> {seen}\n'
             'case "$*" in *secret*) echo "public text names a secret" >&2; exit 3;; esac\n')
    _gh_hook(p, "20-off", "#!/bin/sh\nexit 1\n", mode=0o644)      # not executable: skipped
    _gh_hook(p, ".30-hidden", "#!/bin/sh\nexit 1\n")               # hidden: skipped
    rc, err, ran = gh("pr", "comment", "7", "-b", "a secret")
    assert rc == 1 and ran == [] and "public text names a secret" in err and "gh.d/10-public-text (exit 3)" in err, err
    rc, err, ran = gh("pr", "comment", "7", "-b", "fine")
    assert rc == 0 and ran == ["pr comment 7 -b fine"], err
    # A passing hook grants nothing: the draft guard still refuses an unapproved ready.
    rc, err, ran = gh("pr", "ready", "7")
    assert rc == 1 and ran == [] and "without the user's recorded approval" in err, err
    assert seen.read_text().splitlines() == ["pr comment 7 -b a secret", "pr comment 7 -b fine", "pr ready 7"]
    # A hook that cannot start refuses, naming it.
    broken = _gh_hook(p, "40-broken", "#!/nonexistent/interpreter\n")
    rc, err, ran = gh("pr", "list")
    assert rc == 1 and ran == [] and "gh.d/40-broken could not run" in err, err
    broken.unlink()
    rc, err, ran = gh("pr", "list")
    assert rc == 0 and ran == ["pr list"], err


def test_gh_pre_hooks_and_gh_get_the_same_stdin_and_a_slow_hook_or_stdin_refuses(env, tmp_path):
    p = make(env)
    fake = tmp_path / "stdinbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport sys\n"
                             f"open({str(tmp_path / 'gh.stdin')!r}, 'wb').write(sys.stdin.buffer.read())\n")
    (fake / "gh").chmod(0o755)
    for n in ("10-a", "20-b"):
        _gh_hook(p, n, f"#!/bin/sh\ncat > {tmp_path / n}.stdin\n")
    data = bytes(range(256)) * 600 + "Type: fix\n".encode()            # over one read, not UTF-8
    r = _gh_main(p, tmp_path, ["issue", "comment", "7", "--body-file", "-"], data, fake=fake)
    assert r.returncode == 0, r.stderr
    assert [(tmp_path / f).read_bytes() for f in ("10-a.stdin", "20-b.stdin", "gh.stdin")] == [data] * 3
    text = "Type: docs\nbody\n"                                       # stdin the draft guard reads too
    r = _gh_main(p, tmp_path, ["pr", "create", "--draft", "-t", "t", "--body-file", "-"], text.encode(), fake=fake)
    assert r.returncode == 0, r.stderr
    assert [(tmp_path / f).read_text() for f in ("10-a.stdin", "20-b.stdin", "gh.stdin")] == [text] * 3
    # A hook past its timeout refuses, and the real gh never runs.
    (tmp_path / "gh.stdin").unlink()
    _gh_hook(p, "30-slow", "#!/bin/sh\nsleep 30\n")
    t0 = time.monotonic()
    r = _gh_main(p, tmp_path, ["pr", "list"], fake=fake, timeout_s=1)
    assert r.returncode == 1 and b"gh.d/30-slow ran past 1 s" in r.stderr and time.monotonic() - t0 < 20, r.stderr
    assert not (tmp_path / "gh.stdin").exists()
    (p.harness / "bin" / "gh.d" / "30-slow").unlink()
    # A stdin gh reads that never ends refuses instead of hanging.
    rd, wr = os.pipe()
    try:
        r = _gh_main(p, tmp_path, ["issue", "comment", "7", "--body-file", "-"], fake=fake, timeout_s=1, stdin=rd)
    finally:
        os.close(rd)
        os.close(wr)
    assert r.returncode == 1 and b"stdin did not end within 1 s" in r.stderr and not (tmp_path / "gh.stdin").exists()


def test_gh_needs_stdin_only_for_the_forms_that_read_it():
    from ttp.prguard import needs_stdin
    reads = [["api", "x", "--input", "-"], ["api", "x", "--input=-"], ["api", "x", "-F", "b=@-"], ["api", "x", "-Fb=@-"],
             ["api", "x", "-F=b=@-"], ["api", "x", "--field", "b=@-"], ["api", "x", "--field=b=@-"],
             ["pr", "create", "--body-file", "-"], ["pr", "edit", "7", "--body-file=-"], ["pr", "comment", "7", "-F", "-"],
             ["issue", "comment", "7", "-F-"], ["pr", "review", "7", "-F=-"], ["release", "create", "v1", "--notes-file", "-"],
             ["gist", "create"], ["secret", "set", "X"], ["variable", "set", "X"], ["auth", "login", "--with-token"],
             ["ssh-key", "add", "-"], ["alias", "import", "-"], ["some-extension", "x"]]
    quiet = [["api", "repos/<owner>/<repo>/commits?sha=main&per_page=1", "--jq", ".[0].sha"],
             ["api", "x", "-f", "b=@-"], ["api", "x", "--raw-field", "b=@-"], ["api", "x", "-F", "b=@file"],
             ["api", "x", "--input", "body.json"], ["api", "x", "--input=-x"], ["pr", "create", "--body-file", "b.md"],
             ["pr", "list"], ["pr", "view", "7", "--json", "body"], ["--version"], []]
    assert [a for a in reads if not needs_stdin(a)] == [] and [a for a in quiet if needs_stdin(a)] == []


_STDIN_STATE_GH = """#!{python}
import os, select, sys
r = select.select([0], [], [], 0)[0]
state = ("eof" if not os.read(0, 1) else "data") if r else "open"
open({out!r}, "w").write(state)
"""


def test_gh_and_its_hooks_never_stall_on_an_open_stdin_pipe_gh_does_not_read(env, tmp_path):
    """A parent's stdin pipe that nobody writes to or closes: a gh call that does not read stdin answers at
    once, its gh.d hooks and the real gh see EOF (/dev/null), not a hang until HOOK_TIMEOUT_S."""
    p = make(env)
    fake = tmp_path / "statebin"
    fake.mkdir()
    (fake / "gh").write_text(_STDIN_STATE_GH.format(python=sys.executable, out=str(tmp_path / "gh.state")))
    (fake / "gh").chmod(0o755)
    args = ["api", "repos/<owner>/<repo>/commits?sha=main&per_page=1", "--jq", ".[0].sha"]
    for hooked in (False, True):
        if hooked:
            _gh_hook(p, "10-reads", f"#!/bin/sh\ncat > {tmp_path / 'hook.stdin'}\n")
        rd, wr = os.pipe()
        try:
            t0 = time.monotonic()
            r = _gh_main(p, tmp_path, args, fake=fake, stdin=rd)   # the default 30 s hook timeout
            took = time.monotonic() - t0
        finally:
            os.close(rd)
            os.close(wr)
        assert r.returncode == 0 and took < 5, (hooked, took, r.stderr)
        assert (tmp_path / "gh.state").read_text() == "eof", hooked
        assert not hooked or (tmp_path / "hook.stdin").read_bytes() == b""


def test_gh_api_stdin_forms_still_reach_hooks_and_gh_intact(env, tmp_path):
    p = make(env)
    fake = tmp_path / "stdinbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport sys\n"
                             f"open({str(tmp_path / 'gh.stdin')!r}, 'wb').write(sys.stdin.buffer.read())\n")
    (fake / "gh").chmod(0o755)
    _gh_hook(p, "10-a", f"#!/bin/sh\ncat > {tmp_path / 'hook.stdin'}\n")
    ep = "repos/<owner>/<repo>/issues/7/comments"
    for i, extra in enumerate((["--input", "-"], ["--input=-"], ["-F", "body=@-"], ["--field=body=@-"])):
        data = f'{{"body": "comment {i}"}}\n'.encode()
        r = _gh_main(p, tmp_path, ["api", ep, *extra], data, fake=fake)
        assert r.returncode == 0, (extra, r.stderr)
        assert (tmp_path / "hook.stdin").read_bytes() == data and (tmp_path / "gh.stdin").read_bytes() == data, extra


def test_upgrade_leaves_the_projects_gh_pre_hooks_alone(env, monkeypatch):
    from ttp import cli, service
    p = make(env)
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    mine = _gh_hook(p, "10-mine", "#!/bin/sh\nexit 0\n")
    _git_out(p.harness, "add", "-A")
    _git_out(p.harness, "commit", "-q", "-m", "a gh pre-hook")
    loose = _gh_hook(p, "20-uncommitted", "#!/bin/sh\nexit 0\n")
    _install_template(env)
    (env["home"] / "lib" / "current" / "runtime" / "ttp" / "SOURCE_COMMIT").write_text("dddd4444\n")
    cli.main(["upgrade", "demo"])
    for f in (mine, loose):
        assert f.read_text() == "#!/bin/sh\nexit 0\n" and os.access(f, os.X_OK), f
    assert (p.harness / "bin" / "gh.d" / "README.md").is_file()
    assert "(dddd4444)" in _git_out(p.harness, "log", "-1", "--format=%s", "upstream")
    assert _git_out(p.harness, "ls-tree", "--name-only", "upstream", "bin/gh.d/") == "bin/gh.d/README.md"


def test_only_a_harness_task_edits_the_gh_wrapper_and_its_pre_hooks(env, tmp_path, monkeypatch):
    from ttp import hook
    p = make(env)
    code = p.db.add_task("Fix it", "x", kind="code", tier="standard", origin="coordinator")
    harness = p.db.add_task("Add a gh hook", "x", kind="harness", tier="standard", origin="coordinator")
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    bin_ = p.harness / "bin"

    def denied(tool, **inp):
        payload = {"tool_name": tool, "tool_input": inp, "cwd": str(tmp_path)}
        out, _ = hook.pre_tool_use(payload)
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    edits = [("Edit", {"file_path": str(bin_ / "gh.d" / "10-x")}), ("Write", {"file_path": str(bin_ / "gh")}),
             ("Write", {"file_path": str(bin_ / "gh.d")}), ("MultiEdit", {"file_path": str(bin_ / "gh.d" / "a")}),
             ("Edit", {"file_path": os.path.relpath(bin_ / "gh.d" / "10-x", tmp_path)})]
    bash = ["rm $TTP_PROJECT/harness/bin/gh.d/10-x", f"chmod -x {bin_}/gh.d/10-x", f"mv {bin_}/gh /tmp/x",
            "cd $TTP_PROJECT/harness/bin && rm -r gh.d", "echo exit 0 > \"$TTP_PROJECT/harness/bin/gh\""]
    for task in (code, None):
        monkeypatch.setenv("TTP_TASK", str(task or ""))
        for tool, inp in edits:
            assert denied(tool, **inp), (task, tool, inp)
        for cmd in bash:
            assert denied("Bash", command=cmd), (task, cmd)
    assert not denied("Edit", file_path=str(tmp_path / "gh.d" / "x"))
    assert not denied("Write", file_path=str(p.harness / "prompts" / "worker.md"))
    assert not denied("Bash", command="gh pr list") and not denied("Bash", command="ls template/bin/gh.d")
    monkeypatch.setenv("TTP_TASK", str(harness))
    for tool, inp in edits:
        assert not denied(tool, **inp), (tool, inp)
    for cmd in bash:
        assert not denied("Bash", command=cmd), cmd


def test_draft_guard_allows_a_path_that_keeps_the_harness_bin_first(env, tmp_path, monkeypatch):
    """A test may put a fake gh behind the wrapper; every PATH that drops the wrapper or puts it behind
    another dir stays refused."""
    from ttp import hook
    base = tmp_path / "proj"
    bin_ = base / "harness" / "bin"
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(base))
    monkeypatch.delenv("TTP_TASK", raising=False)
    (tmp_path / "ok.sh").write_text('PATH="$TTP_PROJECT/harness/bin:/tmp/fake:$PATH"\ngh pr ready 7\n')
    (tmp_path / "bad.sh").write_text('PATH="/tmp/fake:$TTP_PROJECT/harness/bin:$PATH"\ngh pr ready 7\n')

    def denied(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    allowed = [f"PATH={bin_}:/tmp/fake:$PATH gh pr ready 7", f"PATH='{bin_}/:/tmp/fake' gh pr ready 7",
               'PATH="$TTP_PROJECT/harness/bin:/tmp/fake:$PATH" gh pr create -t t',
               "export PATH=${TTP_PROJECT}/harness/bin:/tmp/fake:$PATH; gh api -X PATCH repos/a/b/pulls/7 -F draft=false",
               "env PATH=$TTP_PROJECT/harness/bin/:/tmp/fake gh pr ready 7", "PATH=$TTP_PROJECT/harness/bin gh pr ready 7",
               "bash ok.sh", f"PATH={bin_}:/tmp/fake:$PATH bash ok.sh",
               'PATH="$TTP_PROJECT/harness/bin":/tmp/fake:$PATH gh pr ready 7',
               f"PATH='{bin_}'; gh pr ready 7", 'PATH="$TTP_PROJECT/harness/bin/" gh pr ready 7']
    for cmd in allowed:
        assert not denied(cmd), cmd
    refused = ["env -u PATH gh pr ready 7", "unset PATH; gh pr ready 7", "env -i gh pr ready 7",
               "PATH=/usr/bin gh pr ready 7", "PATH=/tmp/fake:$TTP_PROJECT/harness/bin:$PATH gh pr ready 7",
               f"PATH=/tmp/fake:{bin_}:$PATH gh pr ready 7", f"PATH={bin_}x:$PATH gh pr ready 7",
               "PATH=$TTP_PROJECT/harness/bin2:$PATH gh pr ready 7",
               "PATH=$TTP_PROJECT/harness/bin/../../x:$PATH gh pr ready 7",
               "PATH='$TTP_PROJECT/harness/bin:/tmp/fake' gh pr ready 7",          # single quotes: no expansion
               "TTP_PROJECT=/tmp/x; PATH=$TTP_PROJECT/harness/bin:$PATH gh pr ready 7",
               "export TTP_PROJECT=/tmp/x PATH=$TTP_PROJECT/harness/bin:$PATH; gh pr ready 7",
               "PATH=${TTP_PROJECT:-/tmp/x}/harness/bin:$PATH gh pr ready 7",
               'n=TTP_PROJ; eval "${n}ECT=/tmp/x"; PATH=$TTP_PROJECT/harness/bin:$PATH gh pr ready 7',
               'n=TTP_PROJ; export "${n}ECT=/tmp/x"; PATH=$TTP_PROJECT/harness/bin:$PATH gh pr ready 7',
               f"PATH={bin_}:$PATH; PATH=/tmp/fake:$PATH gh pr ready 7", "/usr/bin/gh pr ready 7",
               "command -p gh pr ready 7", "PATH=$PATH:/tmp/fake command -p gh pr ready 7",
               "bash bad.sh", "TTP_PROJECT=/tmp/x bash ok.sh", "PATH=/tmp/fake:$PATH bash ok.sh",
               # a closing quote followed by more of the same word: the value goes on past the bin dir
               'PATH="$TTP_PROJECT/harness/bin"/../../../tmp/fake:$PATH gh pr ready 7',
               f'PATH="{bin_}"/../../../tmp/fake:$PATH gh pr ready 7',
               f"PATH='{bin_}'/../../../tmp/fake:$PATH gh pr ready 7",
               'PATH="$TTP_PROJECT/harness/bin"x:$PATH gh pr ready 7',
               'export PATH="${TTP_PROJECT}/harness/bin""/../../../tmp/fake:$PATH"; gh pr ready 7',
               f"PATH='{bin_}/'x:$PATH gh pr ready 7", 'PATH="$TTP_PROJECT/harness/bin"\'/x\':$PATH gh pr ready 7']
    for cmd in refused:
        assert denied(cmd), cmd
