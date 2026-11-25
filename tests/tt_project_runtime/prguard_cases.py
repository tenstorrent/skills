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
    rc, err, ran = gh("pr", "comment", "7", "-b", "a secret [authored by AI]")
    assert rc == 1 and ran == [] and "public text names a secret" in err and "gh.d/10-public-text (exit 3)" in err, err
    rc, err, ran = gh("pr", "comment", "7", "-b", "fine [authored by AI]")
    assert rc == 0 and ran == ["pr comment 7 -b fine [authored by AI]"], err
    # A passing hook grants nothing: the draft guard still refuses an unapproved ready.
    rc, err, ran = gh("pr", "ready", "7")
    assert rc == 1 and ran == [] and "without the user's recorded approval" in err, err
    assert seen.read_text().splitlines() == ["pr comment 7 -b a secret [authored by AI]",
                                             "pr comment 7 -b fine [authored by AI]", "pr ready 7"]
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
    data = bytes(range(256)) * 600 + "Type: fix [authored by AI]\n".encode()   # over one read, not UTF-8
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
             ["auth", "git-credential", "get"], ["auth", "git-credential", "store"], ["auth", "git-credential", "erase"],
             ["ssh-key", "add", "-"], ["alias", "import", "-"], ["some-extension", "x"],
             ["pr", "comment", "7", "--body-file", "/dev/stdin"], ["pr", "edit", "7", "--body-file=/dev/fd/0"],
             ["release", "create", "v1", "--notes-file", "/dev/stdin"], ["pr", "comment", "7", "-F", "/dev/stdin"],
             ["issue", "comment", "7", "-F/proc/self/fd/0"], ["api", "x", "--input", "/dev/stdin"],
             ["api", "x", "--input=/proc/self/fd/0"], ["api", "x", "-F", "b=@/dev/stdin"],
             ["api", "x", "--field", "b=@/proc/self/fd/0"], ["api", "x", "-F=b=@/dev/fd/0"],
             ["ssh-key", "add", "/dev/stdin"], ["gist", "view", "/dev/fd/0"], ["codespace", "ssh", "-c", "c", "cat"]]
    quiet = [["api", "repos/<owner>/<repo>/commits?sha=main&per_page=1", "--jq", ".[0].sha"],
             ["api", "x", "-f", "b=@-"], ["api", "x", "--raw-field", "b=@-"], ["api", "x", "-F", "b=@file"],
             ["api", "x", "--input", "body.json"], ["api", "x", "--input=-x"], ["pr", "create", "--body-file", "b.md"],
             ["pr", "list"], ["pr", "view", "7", "--json", "body"], ["--version"], [],
             ["api", "x", "-f", "b=@/dev/stdin"], ["api", "x", "-F", "b=/dev/stdin"], ["api", "x", "-F", "b=-"],
             ["pr", "create", "--body-file", "/dev/stdinx"], ["codespace", "list"]]
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


def test_gh_auth_git_credential_gets_git_s_request_on_stdin(env, tmp_path):
    """git runs `gh auth git-credential get|store|erase` as its credential helper and writes the request on
    stdin: bin/gh passes it through to the real gh, never /dev/null."""
    p = make(env)
    fake = tmp_path / "credbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!/bin/sh\ncat > {tmp_path / 'gh.stdin'}\n")
    (fake / "gh").chmod(0o755)
    request = b"protocol=https\nhost=github.com\n\n"
    for op in ("get", "store", "erase"):
        (tmp_path / "gh.stdin").unlink(missing_ok=True)
        r = _gh_main(p, tmp_path, ["auth", "git-credential", op], data=request, fake=fake)
        assert r.returncode == 0, (op, r.stderr)
        assert (tmp_path / "gh.stdin").read_bytes() == request, op


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
        data = f'{{"body": "comment {i} [authored by AI]"}}\n'.encode()
        r = _gh_main(p, tmp_path, ["api", ep, *extra], data, fake=fake)
        assert r.returncode == 0, (extra, r.stderr)
        assert (tmp_path / "hook.stdin").read_bytes() == data and (tmp_path / "gh.stdin").read_bytes() == data, extra


def test_gh_body_file_dev_stdin_reaches_gh_intact(env, tmp_path):
    """`echo body | gh pr comment 7 --body-file /dev/stdin`: gh reads the piped body, not /dev/null."""
    p = make(env)
    fake = tmp_path / "stdinbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport sys\n"
                             f"open({str(tmp_path / 'gh.stdin')!r}, 'wb').write(open(sys.argv[-1], 'rb').read())\n")
    (fake / "gh").chmod(0o755)
    for hooked in (False, True):
        if hooked:
            _gh_hook(p, "10-a", f"#!/bin/sh\ncat > {tmp_path / 'hook.stdin'}\n")
        data = f"hello body {hooked} [authored by AI]\n".encode()
        r = _gh_main(p, tmp_path, ["pr", "comment", "7", "--body-file", "/dev/stdin"], data, fake=fake)
        assert r.returncode == 0, (hooked, r.stderr)
        assert (tmp_path / "gh.stdin").read_bytes() == data, hooked
        assert not hooked or (tmp_path / "hook.stdin").read_bytes() == data


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


def test_a_patch_to_the_gh_wrapper_is_refused_like_an_edit_on_every_provider(env, tmp_path, monkeypatch):
    """Codex's apply_patch reaches the hook with the patch in tool_input.command: each add, update, delete
    or move target is checked as an Edit's file_path is, also a patch in a shell command and relative paths."""
    from ttp import hook
    p = make(env)
    code = p.db.add_task("Fix it", "x", kind="code", tier="standard", origin="coordinator")
    harness = p.db.add_task("Add a gh hook", "x", kind="harness", tier="standard", origin="coordinator")
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    bin_ = p.harness / "bin"

    def denied(tool, body, cwd=tmp_path, key="command"):
        text = f"*** Begin Patch\n{body}\n*** End Patch"
        inp = {key: f"apply_patch <<'EOF'\n{text}\nEOF" if tool == "Bash" else text}
        out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": inp, "cwd": str(cwd)})
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    bad = [(f"*** Update File: {bin_ / 'gh'}\n@@\n-a\n+b", tmp_path), (f"*** Add File: {bin_ / 'gh.d' / '10-x'}\n+x", tmp_path),
           (f"*** Delete File: {bin_ / 'gh.d' / 'README.md'}", tmp_path),
           ("*** Update File: notes.txt\n*** Move to: " + str(bin_ / "gh"), tmp_path),
           ("  *** Update File:   gh\n@@\n-a\n+b", bin_), ("*** Add File: gh.d/20-y\n+x", bin_),
           ("*** Add File: src/a.py\n+x\n*** Update File: " + os.path.relpath(bin_ / "gh", tmp_path), tmp_path)]
    ok = [("*** Add File: src/a.py\n+x", tmp_path), ("*** Update File: gh\n@@\n-a\n+b", tmp_path),
          (f"*** Update File: {p.harness / 'prompts' / 'worker.md'}\n@@\n-a\n+b", tmp_path)]
    for task in (code, None):
        monkeypatch.setenv("TTP_TASK", str(task or ""))
        for body, cwd in bad:
            assert denied("apply_patch", body, cwd), (task, body)
            assert denied("apply_patch", body, cwd, key="input"), (task, body)
            assert denied("Bash", body, cwd), (task, body)
        for body, cwd in ok:
            assert not denied("apply_patch", body, cwd) and not denied("Bash", body, cwd), body
    monkeypatch.setenv("TTP_TASK", str(harness))
    for body, cwd in bad:
        assert not denied("apply_patch", body, cwd) and not denied("Bash", body, cwd), body


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


# --- the form of every PR body (prguard.body_form_problem) -------------------------------------------

def test_pr_body_internal_refs_are_ids_not_github_refs_counts_or_versions():
    from ttp.prguard import internal_refs
    for text, want in (("Seen in run 250, and run 290.", ["run 250", "run 290"]), ("per review #45", ["review #45"]),
                       ("task #29 and task 29", ["task #29", "task 29"]), ("Run #7 failed", ["Run #7"]),
                       ("runs 250 or 290", ["runs 250 or 290"]), ("(rerun 12)", ["rerun 12"]),
                       ("ask #3, ask 12", ["ask #3", "ask 12"]), ("user msg #4; msg 9", ["user msg #4", "msg 9"]),
                       ("o1084 holds", ["o1084"]), ("Task#29, r\u200bun 250", ["Task#29", "run 250"]), ("on ttp/t8900-x-y", ["ttp/t8900-x-y"]), ("see t8900-fix-x.", ["t8900-fix-x"]),
                       ("in tt-project/state/runs/123", ["tt-project/state/runs/123"]),
                       ("at worktrees/t8900/a.py", ["worktrees/t8900/a.py"])):
        assert internal_refs(text) == want, (text, internal_refs(text))
    for text in ("Fixes #123", "owner/repo#12", "https://github.com/o/r/pull/9", "sha 89a2ea3bd", "0.2.1339, v1.2.3",
                 "step 2", "a test run", "run 3 times", "runs 2 jobs", "runs on N150", "run 3-4 times", "run 1.5 hours",
                 "run 2x faster", "-O2 and o3", "run 100 iterations", "review 2 files", "ask 3 questions",
                 "run 8 tests in parallel", "tests/t3000/x.py", "runs on t3000 and n300", "the run's state", "runs 10 seconds"):
        assert internal_refs(text) == [], (text, internal_refs(text))


def test_pr_body_word_cap_skips_comments_code_and_template_lines(tmp_path):
    from ttp.prguard import body_form_problem, prose_words
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    template = "## Why\n\n## How it was tested\n" + "".join(f"- [ ] checklist item number {i}\n" for i in range(80))
    (repo / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(template)
    assert prose_words("<!-- a b c -->\n```\nx y z\n```\n~~~\nq\n~~~\nOne two, three.\n| - |") == 3
    assert body_form_problem([template], {}, str(repo)) is None   # a template alone never trips it
    long = template + "word " * 301
    why = body_form_problem([long], {}, str(repo))
    assert why and "301 words of prose, over the cap of 300 (delivery.pr_body_max_words)" in why, why
    assert "2-3 sentences of why on top" in why and "no internal run, task or review ids" in why
    assert body_form_problem([long], {"delivery": {"pr_body_max_words": 400}}, str(repo)) is None
    assert body_form_problem([long], {"delivery": {"pr_body_max_words": "lots"}}, str(repo))   # bad value: default
    assert body_form_problem([long], {}, str(tmp_path)) and body_form_problem(["", "  "], {}) is None


def test_workers_gh_refuses_a_pr_body_with_internal_ids_or_over_the_cap_on_every_route(env, tmp_path):
    """Every project: nothing is sent, the refusal names each phrase and tells the run to rewrite."""
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    bad, long = tmp_path / "bad.md", tmp_path / "long.md"
    bad.write_text("Why: run 250 showed it.\n\nSee review #45 and task #29.\n")
    long.write_text("Plain words. " * 160)
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"body": "per run 290"}))
    for args in (("pr", "create", "--draft", "-t", "t", "-b", "from run 250"), ("pr", "create", "-d", "-tt", "-F", str(bad)),
                 ("pr", "create", "--draft", "--body-file", str(long)), ("pr", "edit", "7", "--body=task #29"),
                 ("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-f", "body=see review #45"),
                 ("api", "repos/acme/widgets/pulls", "-F", "draft=true", "--input", str(inp)),
                 ("api", "graphql", "-f", _CREATE % ',draft:true,body:"ask #3"')):
        rc, err, ran = gh(*args)
        assert rc == 1 and ran == [] and "Rewrite the body and retry" in err, (args, err)
        assert "320 words of prose, over the cap of 300" in err if args[-1] == str(long) else "internal" in err, err
    rc, err, _ = gh("pr", "create", "--draft", "-F", str(bad))
    assert "'run 250', 'review #45', 'task #29'" in err, err
    rc, err, ran = gh("pr", "create", "--draft", "-F", "/dev/null")   # a pipe or device: gh would get nothing
    assert rc == 1 and ran == [] and "regular file" in err, err
    p.set_config("delivery.pr_body_max_words", 400)   # only project config raises the cap
    for args in (("pr", "create", "--draft", "--body-file", str(long)), ("pr", "create", "--draft", "--fill"),
                 ("pr", "create", "--draft", "-t", "t"), ("pr", "create", "--draft", "-b", "Fixes #12; run 3 times."),
                 ("pr", "edit", "7", "--add-label", "x"), ("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-f", "title=t")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)


def test_workers_gh_checks_a_pr_body_from_stdin_and_passes_gh_the_same_bytes(env, tmp_path):
    p = make(env)
    fake = tmp_path / "stdinbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport sys\n"
                             f"open({str(tmp_path / 'gh.stdin')!r}, 'wb').write(sys.stdin.buffer.read())\n")
    (fake / "gh").chmod(0o755)
    for path in ("-", "/dev/stdin"):
        r = _gh_main(p, tmp_path, ["pr", "create", "--draft", "-t", "t", "--body-file", path], b"as run 250 found\n", fake=fake)
        assert r.returncode == 1 and b"'run 250'" in r.stderr and not (tmp_path / "gh.stdin").exists(), (path, r.stderr)
        data = "Faster startup — caches warm.\n".encode()
        r = _gh_main(p, tmp_path, ["pr", "edit", "7", "--body-file", path], data, fake=fake)
        assert r.returncode == 0 and (tmp_path / "gh.stdin").read_bytes() == data, (path, r.stderr)
        (tmp_path / "gh.stdin").unlink()


def _ghhold_bins(tmp_path):
    """A stand-in harness gh wrapper and a stand-in real gh, each in its own folder."""
    shim, real = tmp_path / "wrap", tmp_path / "sysbin"
    for d, text in ((shim, "#!/bin/sh\n# from ttp.prguard import main\n"), (real, "#!/bin/sh\necho real\n")):
        d.mkdir()
        (d / "gh").write_text(text)
        (d / "gh").chmod(0o755)
    return shim, real


def test_ghhold_holds_the_gh_credentials_for_the_wrapper_only_and_marks_a_path_that_skips_it(tmp_path):
    """The agent's env carries no gh token and an empty GH_CONFIG_DIR; the wrapper's restore() puts them
    back; git's github.com helper goes through the wrapper; a PATH whose first gh is not the wrapper marks
    the run; with no wrapper on PATH nothing changes."""
    from ttp import ghhold
    shim, real = _ghhold_bins(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    base = {"PATH": f"{shim}:{real}:/usr/bin", "HOME": str(tmp_path), "GH_TOKEN": "tok-a", "GITHUB_TOKEN": "tok-b",
            "GH_CONFIG_DIR": str(tmp_path / "ghconf"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "x"}
    out = ghhold.hold(base, run)
    assert not {"GH_TOKEN", "GITHUB_TOKEN"} & set(out) and out["GH_CONFIG_DIR"] == str(run / ghhold.EMPTY)
    assert not list((run / ghhold.EMPTY).iterdir()) and out[ghhold.BIN_ENV] == str(shim)
    assert out["TTP_GH_HELD_GH_TOKEN"] == "tok-a" and out["TTP_GH_HELD_GH_CONFIG_DIR"] == str(tmp_path / "ghconf")
    assert out["GIT_CONFIG_COUNT"] == "3" and out["GIT_CONFIG_KEY_0"] == "user.name"
    assert out["GIT_CONFIG_KEY_1"] == out["GIT_CONFIG_KEY_2"] == "credential.https://github.com.helper"
    assert out["GIT_CONFIG_VALUE_1"] == "" and out["GIT_CONFIG_VALUE_2"] == f"!{shim / 'gh'} auth git-credential"
    assert (run / ghhold.ZDOT / ".zshrc").is_file() and out["ZDOTDIR"] == str(run / ghhold.ZDOT)
    assert not ghhold.marked(str(run)) and ghhold.hold(out, run) == out, "held once"
    back = dict(out)
    ghhold.restore(back)
    assert {k: back.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", ghhold.HOLD_ENV)} == {
        "GH_TOKEN": "tok-a", "GITHUB_TOKEN": "tok-b", "GH_CONFIG_DIR": str(tmp_path / "ghconf"), ghhold.HOLD_ENV: None}
    assert not [k for k in back if k.startswith(ghhold.HELD)]
    bare = {**base, "PATH": f"{real}:/usr/bin"}
    assert ghhold.hold(bare, run) == bare, "no wrapper to restore them: nothing held"
    other = tmp_path / "run2"
    other.mkdir()
    ghhold.hold({**base, "PATH": f"{real}:{shim}"}, other)
    assert ghhold.marked(str(other)) and str(real / "gh") in (other / ghhold.MARK).read_text()


def test_ghhold_wrapper_runs_the_real_gh_with_the_held_credentials(env, tmp_path):
    """bin/gh restores what hold() held before it runs the real gh: the token the agent never saw."""
    from ttp import ghhold
    p = make(env)
    fake = tmp_path / "realbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport os\n"
                             "print(os.environ.get('GH_TOKEN'), os.environ.get('GH_CONFIG_DIR'), "
                             f"os.environ.get('{ghhold.HOLD_ENV}'))\n")
    (fake / "gh").chmod(0o755)
    run = tmp_path / "run"
    run.mkdir()
    agent = ghhold.hold({**os.environ, "PATH": f"{p.harness / 'bin'}:{fake}:{os.environ['PATH']}",
                         "GH_TOKEN": "tok-held", "TTP_PROJECT": str(p.base), "TTP_PYTHON": sys.executable}, run)
    agent.pop("PYTHONPATH", None)
    assert "GH_TOKEN" not in agent
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, env=agent, timeout=60)
    assert r.stdout.split() == ["tok-held", str(os.environ.get("GH_CONFIG_DIR")), "None"], (r.stdout, r.stderr)
    r = subprocess.run([str(fake / "gh"), "auth", "status"], capture_output=True, text=True, env=agent, timeout=60)
    assert r.stdout.split() == ["None", str(run / ghhold.EMPTY), str(run / ghhold.EMPTY)], "around the wrapper"


def test_hook_refuses_a_pr_body_with_internal_refs_or_over_the_cap_on_any_gh_path(env, tmp_path, monkeypatch):
    """The tool hook applies the wrapper's PR body rule before the command runs, so a bare gh a login
    shell's PATH resolves around the wrapper gets it too; a short plain body passes."""
    from ttp import hook
    p = make(env)
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))

    def why(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        spec = (out or {}).get("hookSpecificOutput") or {}
        return spec.get("permissionDecisionReason", "") if spec.get("permissionDecision") == "deny" else None

    long = " ".join(["word"] * 400)
    (tmp_path / "body.md").write_text("Fixes the flaky retry; found in task #29.\n")
    assert "internal" in why('gh pr create --draft --title t --body "Found by run 250 for task #29."')
    assert "internal" in why("cd . && gh pr create --draft --title t --body-file body.md")
    assert "words" in why(f'gh pr edit 7 --body "{long}"')
    assert why('gh pr create --draft --title t --body "Retries now back off, so a slow server no longer '
               'fails the sync."') is None
    assert why("gh pr view 7") is None
    assert why("/usr/bin/" + 'gh pr create --draft --title t --body "ok"'), "a path to gh is refused outright"


def test_hook_refuses_gh_writes_in_a_run_whose_first_gh_was_not_the_wrapper(env, tmp_path, monkeypatch):
    from ttp import ghhold, hook
    p = make(env)
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))

    def denied(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        spec = (out or {}).get("hookSpecificOutput") or {}
        return spec.get("permissionDecision") == "deny" and spec.get("permissionDecisionReason", "")

    assert not denied('gh pr comment 7 --body "Looks fine now."')
    ghhold.mark(tmp_path, "first gh on PATH: /usr/bin/gh")
    assert "not the harness gh wrapper" in denied('gh pr comment 7 --body "Looks fine now."')
    assert "not the harness gh wrapper" in denied("gh api -X PATCH repos/acme/widgets/pulls/7 -f title=x")
    assert not denied("gh pr view 7") and not denied("gh api repos/acme/widgets/pulls/7")


def test_worker_agent_env_holds_the_gh_token_behind_the_wrapper(env, monkeypatch):
    """A worker's agent sees no gh token and an empty GH_CONFIG_DIR (the runner's hold, on every provider)."""
    from ttp.daemon import Daemon
    p = make(env)
    monkeypatch.setenv("GH_TOKEN", "tok-worker")
    monkeypatch.setenv("TTP_FAKE_SHELL", 'env > "$TTP_RUN_DIR/agent-env.txt"')
    tid = p.db.add_task("look at the env", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    run = pathlib.Path(p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))["dir"])
    seen = dict(line.split("=", 1) for line in (run / "agent-env.txt").read_text().splitlines() if "=" in line)
    assert "GH_TOKEN" not in seen and seen.get("GH_CONFIG_DIR") == str(run / "gh-empty"), seen.get("GH_CONFIG_DIR")
    assert seen.get("TTP_RUN_BIN") == str(p.harness / "bin") and seen.get("ZDOTDIR") == str(run / "zdotdir")


# A write form that runs across two commands (a header option whose value is the next line's write) is still a
# write: only a match wholly inside one literal read-only curl is excused
API = "https://api." + "github.com/repos/o/r/"
SPLIT_REPROS = ("curl -s " + API + "pulls/1 -H -X\nPUT -c application/json " + API + "pulls/1/merge",
                "curl -s " + API + "git/refs/heads/main -H --request\nPOST -c application/json " + API + "git/refs")


def test_split_write_forms_stay_refused_through_the_hook(tmp_path, monkeypatch):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    for cmd in SPLIT_REPROS:
        out = hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})[0]
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_draft_guard_passes_a_literal_read_only_curl():
    from ttp import hook
    reads = ("curl -sS -m 30 -D /tmp/d/pr.hdr -o /tmp/d/pr.json -H 'Accept: application/vnd.github+json' "
             + API + "pulls/1",
             "mkdir -p /tmp/d && curl -fsSL -X GET -D- " + API + "pulls/1; grep -c x /tmp/d/pr.hdr")
    for cmd in reads:
        assert hook.draft_bypass(cmd) is None, cmd
    writes = ("curl -s -D h -X POST " + API + "pulls",
              "curl -s -D h -d @b.json " + API + "pulls/1",
              "curl -s -D h -XPATCH " + API + "pulls/1",
              "curl -s -D h --request=PUT " + API + "pulls/1/merge",
              "curl -s -D h -H 'X-HTTP-Method-Override: PATCH' " + API + "pulls/1",
              "curl -s -D h -H @hdrs " + API + "pulls/1",
              "curl -s -D h -K cfg " + API + "pulls/1",
              "curl -s -D h -F draft=false " + API + "pulls/1",
              "curl -s -D \"$H\" -X PATCH " + API + "pulls/1",
              "curl -s -D h " + API + "pulls/1; curl -X PATCH " + API + "pulls/1")
    for cmd in writes:
        assert hook.draft_bypass(cmd), cmd
    assert hook.api_ref_write("curl -s -D h -X PATCH " + API + "git/refs/heads/main")
    assert not hook.api_ref_write("curl -s -D h " + API + "git/refs/heads/main", hook._read_spans(
        "curl -s -D h " + API + "git/refs/heads/main"))


def test_literal_argv_splits_like_bash(tmp_path):
    """Every text the literal test accepts splits into the words bash itself passes; the rest are refused."""
    import subprocess
    from ttp import hook
    accepted = ["curl -s https://api.github.com/repos/o/r/pulls/1", "a  b", "", "x", "-w '%{http_code}' -H 'A: $B `c` \\'",
                "'a'\"b\"c", "\"\" ''", "\"a b\"'c d'e", "a,b+c%d^e@f:g/h.i_j-k=l", "'#' \"x'y\" 'p\"q' \"#\"'!'",
                "'*' '?' '~' '[a]' '{a,b}' '$(x)' '<' '|' ';' '&'", "-x=y"]
    refused = ["$A", "a\\ b", "a>b", "a|b", "a;b", "a&b", "(a)", "{a,b}", "a*", "a?", "~", "[a]", "!a", "a #b", "a\tb",
               "a\nb", "a\rb", "\"$A\"", "\"`a`\"", "\"a\\b\"", "\"!a\"", "'a", "\"a", "é", "A=1 b", "a<b", "`a`"]
    for text in accepted:
        words = hook._literal_argv(text)
        out = subprocess.run(["bash", "--noprofile", "--norc", "-c", "printf '%s\\0' " + text], env={}, cwd=tmp_path,
                             capture_output=True, check=True).stdout
        assert words is not None and words == (out.decode().split("\0")[:-1] if words else []), text
    for text in refused:
        assert hook._literal_argv(text) is None, text
