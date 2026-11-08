# Leak scan of a run's outward writes (outscan.py) tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# Every secret here is made up; token shapes are built by concatenation so this file matches none of them.
from __future__ import annotations

_OUT_SECRET = "made-up-store-value-4f1c9e"
_OUT_SHAPE = "gh" + "p_" + "Q" * 36
_OUT_FORBIDDEN = "BRD-" + "123456"


def _outscan_project(env, monkeypatch, run=True):
    """A project with one stored secret and one forbidden-data pattern; inside a run when `run`."""
    from ttp import project
    p = make(env)
    project.save_secret("acme", _OUT_SECRET)
    with open(p.charter_path, "a") as f:
        f.write("\n## Forbidden data\n- board serial: `BRD-\\d{6}`\n")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    if run:
        monkeypatch.setenv("TTP_TASK", "57")
    else:
        monkeypatch.delenv("TTP_TASK", raising=False)
    return p


def test_outscan_names_the_class_of_each_leak_and_never_the_value(env, monkeypatch):
    from ttp import outscan
    p = _outscan_project(env, monkeypatch)
    cases = {_OUT_SECRET: "stored secret (slot acme)", '"x": "' + _OUT_SECRET + '"': "stored secret",
             "see " + _OUT_SHAPE + " here": "GitHub token shape",
             "-----BEGIN RSA " + "PRIVATE KEY-----": "private key shape",
             "board " + _OUT_FORBIDDEN: "forbidden-data pattern 'board serial'"}
    for text, cls in cases.items():
        why = outscan.problem("ttp notify", "clean first", text, p=p)
        assert why and cls in why and "nothing was sent" in why, (text, why)
        assert _OUT_SECRET not in why and _OUT_SHAPE not in why and _OUT_FORBIDDEN not in why
    for clean in ("the build passed in 12 minutes", "slot acme holds the key", "sk-short", "BRD-12"):
        assert outscan.problem("ttp notify", clean, p=p) is None, clean
    # A forbidden-data pattern that is not a regex fails closed, naming the pattern.
    with open(p.charter_path, "a") as f:
        f.write("- broken: `(unclosed`\n")
    assert "'broken' is not a valid regex" in outscan.problem("ttp notify", "anything", p=p)
    assert outscan.mask_shapes("a " + _OUT_SHAPE + " b") == "a [redacted:GitHub token] b"


def test_runs_gh_refuses_a_leak_on_every_write_route_and_passes_clean_ones(env, tmp_path, monkeypatch):
    """Inline bodies, body files, stdin bodies, api fields and gist files: a planted secret, shape or
    forbidden-data hit is refused before the real gh runs; the same call with a clean payload runs."""
    p = _outscan_project(env, monkeypatch)
    body = tmp_path / "body.md"
    routes = [lambda t: (["pr", "comment", "7", "-b", t], b""),
              lambda t: (["issue", "create", "--title", "x", "--body", t], b""),
              lambda t: (body.write_text(t) and ["issue", "comment", "7", "--body-file", str(body)], b""),
              lambda t: (["pr", "comment", "7", "--body-file", "-"], t.encode()),
              lambda t: (["issue", "comment", "7", "-F", "/dev/stdin"], t.encode()),
              lambda t: (["api", "repos/acme/widgets/issues/7/comments", "-f", f"body={t}"], b""),
              lambda t: (["api", "repos/acme/widgets/issues/7/comments", "-F", "body=@-"], t.encode()),
              lambda t: (["api", "repos/acme/widgets/issues", "--input", "-"], t.encode()),
              lambda t: (body.write_text(t) and ["gist", "create", str(body)], b"")]
    for leak in (_OUT_SECRET, _OUT_SHAPE, "serial " + _OUT_FORBIDDEN):
        for route in routes:
            args, data = route(f"status: {leak}")
            (tmp_path / "gh.log").write_text("")
            r = _gh_main(p, tmp_path, args, data)
            err = r.stderr.decode()
            assert r.returncode == 1 and "refused" in err and "nothing was sent" in err, (args, err)
            assert leak not in err and (tmp_path / "gh.log").read_text() == "", args
    for route in routes:
        args, data = route("status: all green")
        r = _gh_main(p, tmp_path, args, data)
        assert "leak" not in r.stderr.decode() and "holds" not in r.stderr.decode(), (args, r.stderr)
    # Outside a run (no TTP_TASK) the wrapper does not scan.
    monkeypatch.delenv("TTP_TASK")
    r = _gh_main(p, tmp_path, ["pr", "comment", "7", "-b", _OUT_SHAPE])
    assert "holds" not in r.stderr.decode()


def test_note_to_and_notify_refuse_a_leak_before_anything_is_filed(env, monkeypatch, capsys):
    from ttp import cli
    from ttp.daemon import NOTIFY_FILE
    p = _outscan_project(env, monkeypatch)
    run_dir = _note_run(env, monkeypatch, p)
    for leak in (_OUT_SECRET, _OUT_SHAPE, _OUT_FORBIDDEN):
        assert _note(cli, "--to", "second", f"fyi {leak}") == 2
        err = capsys.readouterr().err
        assert "ttp note --to refused" in err and leak not in err, err
        try:
            cli.main(["notify", f"fyi {leak}"])
            rc = 0
        except SystemExit as e:
            rc = e.code
        err = capsys.readouterr().err
        assert rc == 2 and "ttp notify refused" in err and leak not in err, err
    assert not (run_dir / NOTIFY_FILE).exists() and not (env["home"] / "upstream.jsonl").exists()
    assert not (run_dir / "progress.md").exists() or _OUT_SECRET not in (run_dir / "progress.md").read_text()
    cli.main(["notify", "the build passed"])
    assert "the build passed" in (run_dir / NOTIFY_FILE).read_text()
    assert _note(cli, "--to", "second", "the build passed") != 0   # no such project: past the scan
    assert "refused" not in capsys.readouterr().err


def test_a_chat_post_masks_credential_shapes_and_stored_values(env, monkeypatch):
    from ttp.slack import Slack
    _outscan_project(env, monkeypatch, run=False)
    body = Slack._body("demo", f"token {_OUT_SHAPE} and {_OUT_SECRET}", None)
    assert _OUT_SHAPE not in body and _OUT_SECRET not in body
    assert "[redacted:GitHub token]" in body and "[secret:acme]" in body


_OUT_FAKE_GH = '''#!{python}
import os, sys
a = sys.argv[1:]
if a[:2] == ["alias", "list"]:
    with open(os.environ["FAKE_GH_LOG"] + ".aliases", "a") as f:
        f.write("listed\\n")
    print("post: api repos/acme/widgets/issues/7/comments -F body=@$1")
    print("put: gist create")
    print("sh: !cat $1 | gh gist create")
    sys.exit(0)
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(" ".join(a) + " <" + ("" if os.isatty(0) else sys.stdin.read()) + ">\\n")
'''


def test_runs_gh_scans_kept_stdin_aliases_and_field_files(env, tmp_path, monkeypatch):
    """Review #6111: stdin gh keeps without a `-` (gist create, variable set, workflow run, an alias or
    extension), a gh alias's expansion (its files, a shell alias's text) and `-F key=@file` on any command
    are scanned; clean ones run with their stdin passed on; auth login's stdin is left to gh."""
    p = _outscan_project(env, monkeypatch)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "gh").write_text(_OUT_FAKE_GH.format(python=sys.executable))
    (fake / "gh").chmod(0o755)
    log, f = tmp_path / "gh.log", tmp_path / "payload.txt"
    routes = [lambda t: (["gist", "create", "--public"], t.encode()),
              lambda t: (["variable", "set", "X"], t.encode()),
              lambda t: (["workflow", "run", "w", "--json"], t.encode()),
              lambda t: (["myext", "send"], t.encode()),
              lambda t: (["put"], t.encode()),
              lambda t: (f.write_text(t) and ["post", str(f)], b""),
              lambda t: (["sh", t], b""),
              lambda t: (["sh", "x"], t.encode()),
              lambda t: (f.write_text(t) and ["workflow", "run", "w", "-F", f"note=@{f}"], b""),
              lambda t: (f.write_text(t) and ["workflow", "run", "w", "--field=note=@" + str(f)], b"")]
    for leak in (_OUT_SECRET, _OUT_SHAPE, "serial " + _OUT_FORBIDDEN):
        for route in routes:
            args, data = route(f"status: {leak}")
            log.write_text("")
            r = _gh_main(p, tmp_path, args, data, fake=fake)
            err = r.stderr.decode()
            assert r.returncode == 1 and "refused" in err and "nothing was sent" in err, (args, err)
            assert leak not in err and log.read_text() == "", args
    clean = "status: all green [authored by AI]"   # tagged: the AI-tag guard also checks the post alias's comment
    for route in routes:
        args, data = route(clean)
        log.write_text("")
        r = _gh_main(p, tmp_path, args, data, fake=fake)
        assert r.returncode == 0 and "refused" not in r.stderr.decode(), (args, r.stderr)
        assert log.read_text().count("\n") == 1, args
        if data:
            assert f"<{clean}>" in log.read_text(), args   # read once, passed on to gh
    # One `gh alias list` per call, though both the scan and the draft guard expand the alias.
    (tmp_path / "gh.log.aliases").write_text("")
    _gh_main(p, tmp_path, ["put"], b"fine", fake=fake)
    assert (tmp_path / "gh.log.aliases").read_text() == "listed\n"
    # auth login --with-token reads gh's own token: not scanned, and gh still gets it.
    log.write_text("")
    r = _gh_main(p, tmp_path, ["auth", "login", "--with-token"], _OUT_SHAPE.encode(), fake=fake)
    assert r.returncode == 0 and _OUT_SHAPE in log.read_text()


_OUT_ARGS_GH = '''#!{python}
import os, sys
a = sys.argv[1:]
if a[:2] == ["alias", "list"]:
    print("sh: !cat $1 | gh gist create")
    sys.exit(0)
with open(os.environ["FAKE_GH_LOG"], "a") as f:   # like gh where its flags leave stdin alone: never reads it
    f.write(" ".join(a) + "\\n")
'''


def test_runs_gh_reads_stdin_ahead_only_where_gh_reads_it(env, tmp_path, monkeypatch):
    """Review #6495: a command whose flags leave stdin alone is not read ahead, so an open idle pipe does
    not hang it and a `while read` loop keeps its lines; where those flags leave stdin as the input, a
    leak on it is still refused; an extension or shell alias with an idle pipe is refused, not hung."""
    p = _outscan_project(env, monkeypatch)
    fake = tmp_path / "argsbin"
    fake.mkdir()
    (fake / "gh").write_text(_OUT_ARGS_GH.format(python=sys.executable))
    (fake / "gh").chmod(0o755)
    log, f = tmp_path / "gh.log", tmp_path / "f.txt"
    f.write_text("all green\n")
    idle = [["workflow", "run", "w", "-f", "a=b"], ["variable", "set", "X", "--body", "v"],
            ["secret", "set", "X", "-bv"], ["variable", "set", "X", "--env-file", str(f)],
            ["secret", "set", "X", "-f", str(f)], ["gist", "create", str(f)], ["gist", "create", "-d", "x", str(f)],
            ["ssh-key", "add", str(f), "-t", "k"], ["gpg-key", "add", str(f)], ["alias", "import", str(f)]]
    for args in idle:
        log.write_text("")
        rd, wr = os.pipe()
        try:
            t0 = time.monotonic()
            r = _gh_main(p, tmp_path, args, fake=fake, stdin=rd)
            took = time.monotonic() - t0
        finally:
            os.close(rd)
            os.close(wr)
        assert r.returncode == 0 and took < 5 and log.read_text().count("\n") == 1, (args, took, r.stderr)
    for args in (["myext", "send"], ["sh", "x"], ["extension", "exec", "myext"]):
        log.write_text("")
        rd, wr = os.pipe()
        try:
            t0 = time.monotonic()
            r = _gh_main(p, tmp_path, args, fake=fake, stdin=rd)
            took = time.monotonic() - t0
        finally:
            os.close(rd)
            os.close(wr)
        err = r.stderr.decode()
        assert r.returncode == 1 and took < 10 and "nothing was sent" in err and log.read_text() == "", (args, err)
    for args in (["secret", "set", "X"], ["variable", "set", "X", "--env-file=-"], ["variable", "set", "X", "-b", ""],
                 ["ssh-key", "add", "-t", "k"], ["gpg-key", "add"], ["alias", "import"], ["gist", "create", "-p"]):
        log.write_text("")
        r = _gh_main(p, tmp_path, args, f"x {_OUT_SHAPE}".encode(), fake=fake)
        assert r.returncode == 1 and "refused" in r.stderr.decode() and log.read_text() == "", args
    # `while read x; do gh workflow run w -f a=$x; done < list` calls gh once per line.
    from ttp import prguard
    (tmp_path / "list").write_text("one\ntwo\n")
    code = ("import sys; from ttp import prguard; "
            "sys.exit(prguard.main(['gh', 'workflow', 'run', 'w', '-f', 'a=' + sys.argv[2]], sys.argv[1]))")
    log.write_text("")
    r = subprocess.run(["sh", "-c", 'while read x; do "$0" -c "$1" "$2" "$x"; done < "$3"', sys.executable, code,
                        str(p.harness / "bin"), str(tmp_path / "list")], capture_output=True, timeout=60,
                       env={**os.environ, "PATH": f"{p.harness / 'bin'}:{fake}:{os.environ['PATH']}",
                            "TTP_PROJECT": str(p.base), "FAKE_GH_LOG": str(log)},
                       cwd=str(pathlib.Path(prguard.__file__).parents[1]))
    assert log.read_text() == "workflow run w -f a=one\nworkflow run w -f a=two\n", r.stderr


def _push_run(env, tmp_path, monkeypatch):
    """A project with planted values, a clone of a bare origin holding one clean commit on main, and
    `push(*args)` running `git push <args>` in a run through the harness's git wrapper: (p, repo, push)."""
    p = _outscan_project(env, monkeypatch)
    origin, repo = tmp_path / "leak-origin.git", tmp_path / "leak-repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git_out(repo, "remote", "add", "origin", str(origin))
    for k, v in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false"),
                 ("tag.gpgsign", "false")):
        _git_out(repo, "config", k, v)
    _commit(repo, "README.md", "hello\n")
    _git_out(repo, "push", "-q", "origin", "main")
    (tmp_path / "run").mkdir()
    run_env = {**os.environ, "TTP_PYTHON": sys.executable, "TTP_RUN_DIR": str(tmp_path / "run")}
    wrapper = RUNTIME.parent / "template" / "bin" / "git"

    def push(*args):
        return subprocess.run([str(wrapper), "push", *args], cwd=repo, env=run_env, capture_output=True,
                              text=True, timeout=60)
    return p, repo, push


def _remote_refs(repo):
    return _git_out(repo, "ls-remote", "origin")


def test_runs_git_push_refuses_a_leak_in_a_message_an_added_line_or_a_tag_message(env, tmp_path, monkeypatch):
    """Every run's git push goes through the leak scan in its pre-push hook (gitguard.HOOK ->
    outscan.pre_push): a stored secret, a token shape or a forbidden-data pattern is refused in a commit
    message, an added line (also one a diff header could hide) and an annotated tag's message, naming the
    commit, path and class but never the value; nothing reaches the remote."""
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    before = _remote_refs(repo)
    leaks = {_OUT_SECRET: "stored secret (slot acme)", _OUT_SHAPE: "GitHub token shape",
             "serial " + _OUT_FORBIDDEN: "forbidden-data pattern 'board serial'"}
    for n, (leak, cls) in enumerate(leaks.items()):
        cases = {"message": (f"notes.txt", "clean\n", f"fix: {leak}"),
                 "fake diff in message": ("notes.txt", "clean\n", f"x\n\ndiff --git a/n b/n\n+++ b/n\n@@ -1 +1 @@\n {leak}"),
                 "added line": ("src/conf.txt", f"key = {leak}\n", "add conf"),
                 "added line like a header": ("src/conf.txt", f"++ {leak}\n", "add conf")}
        for name, (path, text, msg) in cases.items():
            _git_out(repo, "checkout", "-q", "-B", "work", "main")
            (repo / path).parent.mkdir(exist_ok=True)
            (repo / path).write_text(text)
            _git_out(repo, "add", path)
            _git_out(repo, "commit", "-qm", msg)
            sha = _git_out(repo, "rev-parse", "--short", "HEAD")
            out = push("origin", "work")
            assert out.returncode != 0 and "git push refused" in out.stderr and cls in out.stderr, (name, out.stderr)
            assert "nothing was sent" in out.stderr and f"commit {sha}" in out.stderr, (name, out.stderr)
            assert ("(its message)" if "message" in name else f"({path})") in out.stderr, (name, out.stderr)
            assert leak not in out.stderr and _OUT_FORBIDDEN not in out.stderr, name
        _git_out(repo, "checkout", "-q", "main")
        _git_out(repo, "tag", "-a", f"v{n}", "-m", f"release notes: {leak}")
        out = push("origin", f"v{n}")
        assert out.returncode != 0 and f"the tag v{n} (its message)" in out.stderr and cls in out.stderr, out.stderr
        assert leak not in out.stderr
    assert _remote_refs(repo) == before
    # git called by its path would skip the wrapper: a run's Bash call doing so is refused.
    from ttp import hook
    assert "skips the run's leak scan" in hook.git_push_bypass([["/usr/bin/git", "push", "origin", "work"]])
    assert hook.git_push_bypass([["/usr/bin/git", "log"]]) is None
    monkeypatch.delenv("TTP_TASK")
    assert hook.git_push_bypass([["/usr/bin/git", "push", "origin", "work"]]) is None


def test_tests_run_outside_the_role_of_the_run_running_them(env, tmp_path, monkeypatch):
    """Suites run inside a live run (every run sets TTP_ROLE), the coordinator's too: its role never reaches
    a test, or the harness git wrapper first on PATH refuses every push a test makes (coordtools)."""
    assert not {"TTP_ROLE", "TTP_COORD_FULL_TOOLS", "TTP_TASK"} & set(os.environ)
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    _git_out(repo, "checkout", "-q", "-b", "clean")
    _commit(repo, "a.txt", "the build passed\n")
    assert push("origin", "clean").returncode == 0 and "refs/heads/clean" in _remote_refs(repo)


def test_runs_git_push_passes_clean_and_already_pushed_commits_and_keeps_refusing_a_removed_leak(
        env, tmp_path, monkeypatch, capsys):
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    own_hook = tmp_path / "own-hook"
    (repo / ".git" / "hooks" / "pre-push").write_text(f"#!/bin/sh\ncat >/dev/null\necho ran >> {own_hook}\n")
    (repo / ".git" / "hooks" / "pre-push").chmod(0o755)
    # A clean push goes, and the repository's own pre-push hook still runs after the scan.
    _git_out(repo, "checkout", "-q", "-b", "clean")
    _commit(repo, "a.txt", "the build passed\n")
    _git_out(repo, "tag", "-a", "v1", "-m", "release one")
    out = push("origin", "clean", "v1")
    assert out.returncode == 0, out.stderr
    assert own_hook.read_text() == "ran\n" and "refs/tags/v1" in _remote_refs(repo)
    # A leaky commit the remote already has (pushed outside a run) is not sent again: pushing it under a
    # new name, or a clean commit on top of it, passes.
    _git_out(repo, "checkout", "-q", "-b", "old", "main")
    _commit(repo, "old.txt", f"{_OUT_SHAPE}\n")
    # Outside a run: no TTP_TASK, TTP_ROLE or TTP_COORD_FULL_TOOLS, so a harness git wrapper first on PATH
    # (a worker's or coordinator's run host) does not scan it.
    outside = {k: v for k, v in os.environ.items() if k not in ("TTP_TASK", "TTP_ROLE", "TTP_COORD_FULL_TOOLS")}
    subprocess.run(["git", "-C", str(repo), "push", "-q", "--no-verify", "origin", "old"], env=outside,
                   capture_output=True, text=True, check=True)
    assert push("origin", "old:refs/heads/copy").returncode == 0
    _commit(repo, "new.txt", "clean\n")
    out = push("origin", "old")
    assert out.returncode == 0, out.stderr
    # A later commit that removes the leak still refuses: the earlier commit carries it.
    _git_out(repo, "checkout", "-q", "-b", "fixup", "main")
    _commit(repo, "b.txt", f"token {_OUT_SECRET}\n")
    first = _git_out(repo, "rev-parse", "--short", "HEAD")
    _commit(repo, "b.txt", "token from ttp secret acme\n")
    for args in (["origin", "fixup"], ["--no-verify", "origin", "fixup"], ["-o", "x", "origin", "fixup"]):
        out = push(*args)
        assert out.returncode != 0 and f"commit {first} (b.txt)" in out.stderr and "removes it" in out.stderr, out.stderr
    # A remote-tracking ref set by hand does not mark it as already on the remote.
    _git_out(repo, "update-ref", "refs/remotes/origin/fixup", "HEAD")
    assert push("origin", "fixup").returncode != 0
    # A merge whose own resolution adds the leak is refused too.
    _git_out(repo, "checkout", "-q", "-B", "side", "main")
    _commit(repo, "s.txt", "side\n")
    _git_out(repo, "checkout", "-q", "-B", "merged", "clean")
    _git_out(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    (repo / "s.txt").write_text(f"side\n{_OUT_SHAPE}\n")
    _git_out(repo, "add", "s.txt")
    _git_out(repo, "commit", "-qm", "merge side")
    out = push("origin", "merged")
    assert out.returncode != 0 and "(s.txt)" in out.stderr and "GitHub token shape" in out.stderr, out.stderr
    assert "refs/heads/fixup" not in _remote_refs(repo) and "refs/heads/merged" not in _remote_refs(repo)
    # A remote the hook cannot list refuses (fails closed) rather than scanning nothing.
    import io
    from ttp import outscan
    monkeypatch.chdir(repo)
    head = _git_out(repo, "rev-parse", "clean")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/clean {head} refs/heads/clean {'0' * 40}\n"))
    assert outscan.pre_push(["gone", str(tmp_path / "missing.git")]) == 1
    assert "could not list what the remote has" in capsys.readouterr().err


def _wrapped_git(repo, tmp_path, *args):
    """`git <args>` in `repo` in a run, through the harness's git wrapper (as _push_run's push does)."""
    env = {**os.environ, "TTP_PYTHON": sys.executable, "TTP_RUN_DIR": str(tmp_path / "run")}
    return subprocess.run([str(RUNTIME.parent / "template" / "bin" / "git"), *args], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=60)


def test_runs_git_push_scans_a_root_commit_and_the_objects_a_replace_ref_hides(env, tmp_path, monkeypatch):
    """log.showRoot=false (the repository's or the user's config) would leave a root commit's added lines
    out of `git log -p`, and a replace ref would show the scan other content than the push sends."""
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    _git_out(repo, "config", "log.showRoot", "false")
    _git_out(repo, "checkout", "-q", "--orphan", "fresh")
    _git_out(repo, "rm", "-rqf", ".")
    _commit(repo, "root.txt", f"token {_OUT_SHAPE}\n")
    sha = _git_out(repo, "rev-parse", "--short", "HEAD")
    out = push("origin", "fresh")
    assert out.returncode != 0 and f"commit {sha} (root.txt)" in out.stderr, out.stderr
    assert "GitHub token shape" in out.stderr and _OUT_SHAPE not in out.stderr
    # A replace ref swaps the leaky commit for a clean one in what git log shows; the push sends the original.
    _git_out(repo, "checkout", "-q", "-B", "hidden", "main")
    _commit(repo, "h.txt", f"key {_OUT_SECRET}\n")
    leaky = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "-B", "decoy", "main")
    _commit(repo, "h.txt", "clean\n")
    _git_out(repo, "replace", leaky, _git_out(repo, "rev-parse", "HEAD"))
    out = push("origin", "hidden")
    assert out.returncode != 0 and "(h.txt)" in out.stderr and "stored secret" in out.stderr, out.stderr
    assert "refs/heads/fresh" not in _remote_refs(repo) and "refs/heads/hidden" not in _remote_refs(repo)


def test_push_scan_in_a_partial_clone_looks_up_without_lazy_fetches_and_prefetches_the_sent_blobs(
        env, tmp_path, monkeypatch):
    """In a blob:none partial clone, the scan's object lookups (the remote's advertised objects, each
    sent object's type) set GIT_NO_LAZY_FETCH, so missing objects are not fetched one by one past the
    time limit; the blobs git log -p diffs over the sent range are fetched in one batch before it runs,
    and a leak in them is still refused."""
    from ttp import outscan
    p = _outscan_project(env, monkeypatch)
    srv, clone = tmp_path / "pc-server", tmp_path / "pc-clone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(srv)], check=True)
    for k, v in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false"),
                 ("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true")):
        _git_out(srv, "config", k, v)
    _commit(srv, "keep.txt", "kept\n")
    _commit(srv, "f.txt", "one\n")
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{srv}", str(clone)],
                   check=True, capture_output=True)
    base = _git_out(clone, "rev-parse", "HEAD")
    old_blob, keep_blob = (_git_out(clone, "rev-parse", f"HEAD:{f}") for f in ("f.txt", "keep.txt"))

    def missing():
        out = _git_out(clone, "rev-list", "--objects", "--missing=print", base)
        return {line[1:] for line in out.splitlines() if line.startswith("?")}

    def sent(text):
        new_blob = subprocess.run(["git", "-C", str(clone), "hash-object", "-w", "--stdin"], input=text,
                                  capture_output=True, text=True, check=True).stdout.strip()
        # The tree object written by hand: read-tree and mktree would fetch every missing blob it names
        raw = b"".join(f"{mode} {name}\0".encode() + bytes.fromhex(new_blob if sha == old_blob else sha)
                       for mode, _, sha, name in (line.split() for line in
                                                  _git_out(clone, "ls-tree", base).splitlines()))
        tree = subprocess.run(["git", "-C", str(clone), "hash-object", "-t", "tree", "-w", "--stdin"], input=raw,
                              check=True, capture_output=True).stdout.decode().strip()
        commit = subprocess.run(["git", "-C", str(clone), "-c", "user.name=T", "-c", "user.email=t@example.com",
                                 "commit-tree", tree, "-p", base, "-m", "edit f"], check=True,
                                capture_output=True, text=True).stdout.strip()
        return [f"refs/heads/w {commit} refs/heads/w {'0' * 40}"]

    assert old_blob in missing()
    real_run, real_popen, seen = subprocess.run, subprocess.Popen, {"lookups": [], "log_missing": [], "fetches": 0}

    def run(cmd, *a, **kw):
        if cmd[:1] == ["git"] and ("cat-file" in cmd or "ls-remote" in cmd):
            seen["lookups"].append((cmd[1], (kw.get("env") or {}).get("GIT_NO_LAZY_FETCH")))
        seen["fetches"] += cmd[:1] == ["git"] and "fetch" in cmd
        return real_run(cmd, *a, **kw)

    def popen(cmd, *a, **kw):
        if cmd[:2] == ["git", "log"] and "-p" in cmd:
            seen["log_missing"].append(old_blob in missing())
        return real_popen(cmd, *a, **kw)
    monkeypatch.setattr(outscan.subprocess, "run", run)
    monkeypatch.setattr(outscan.subprocess, "Popen", popen)
    monkeypatch.chdir(clone)
    why = outscan.push_problem(sent(f"token {_OUT_SHAPE}\n"), str(srv), p=p)
    assert why and "GitHub token shape" in why and "(f.txt)" in why and _OUT_SHAPE not in why, why
    assert seen["log_missing"] == [False], seen   # fetched before git log ran, not by it
    assert seen["lookups"] and all(v == "1" for _, v in seen["lookups"]), seen["lookups"]
    assert keep_blob in missing()   # only what the diff reads, not the whole tree
    assert seen["fetches"] == 1 and outscan.push_problem(sent("clean\n"), str(srv), p=p) is None
    assert seen["fetches"] == 1   # nothing left to fetch
    # Outside a partial clone nothing is fetched and the scan reads as before.
    _git_out(clone, "config", "--unset", "remote.origin.promisor")
    assert "stored secret" in outscan.push_problem(sent(f"key {_OUT_SECRET}\n"), str(srv), p=p)
    assert seen["fetches"] == 1


def test_runs_git_push_refuses_a_ref_to_a_blob_or_a_tree(env, tmp_path, monkeypatch):
    """A ref whose new object is a blob or a tree (also behind an annotated tag) is not read by the commit
    scan: a run's push of one is refused, naming the ref and the object's type, never its content."""
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    (repo / "loose.txt").write_text(f"token {_OUT_SHAPE}\n")
    blob = _git_out(repo, "hash-object", "-w", "loose.txt")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    _git_out(repo, "tag", "-a", "blobtag", "-m", "a blob", blob)
    for spec, ref, kind in ((f"{blob}:refs/tags/x", "refs/tags/x", "blob"), (f"{tree}:refs/tags/y", "refs/tags/y", "tree"),
                            ("blobtag", "refs/tags/blobtag", "blob")):
        out = push("origin", spec)
        assert out.returncode != 0 and "git push refused" in out.stderr, (spec, out.stderr)
        assert f"{ref} would send a {kind}" in out.stderr and "nothing was sent" in out.stderr, (spec, out.stderr)
        assert _OUT_SHAPE not in out.stderr
    assert "refs/tags/" not in _remote_refs(repo)


def test_runs_git_aliases_and_send_pack_go_through_the_push_guard(env, tmp_path, monkeypatch):
    """A git alias for push (given with -c, in the config, chained, or with its own global options) is
    resolved the way git does and scanned like `git push`; a shell alias that pushes and `git send-pack`
    (which runs no pre-push hook) are refused in a run. An alias never shadows a builtin, so neither here."""
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    _git_out(repo, "checkout", "-q", "-B", "work", "main")
    _commit(repo, "a.txt", f"token {_OUT_SHAPE}\n")
    sha = _git_out(repo, "rev-parse", "--short", "HEAD")
    _git_out(repo, "config", "alias.pp", "push")
    _git_out(repo, "config", "alias.first", "second")
    _git_out(repo, "config", "alias.second", "push -q")
    _git_out(repo, "config", "alias.opt", "-c color.ui=never push")
    _git_out(repo, "config", "alias.shp", "!git push")
    url = str(tmp_path / "leak-origin.git")
    for args in (["-c", "alias.p=push", "p", "origin", "work"], ["pp", "origin", "work"], ["first", "origin", "work"],
                 ["-C", str(repo), "opt", "origin", "work"], ["-c", "alias.P=pp", "P", "origin", "work"]):
        out = _wrapped_git(repo, tmp_path, *args)
        assert out.returncode != 0 and f"commit {sha} (a.txt)" in out.stderr, (args, out.stderr)
    _git_out(repo, "config", "alias.run", "!git")
    for args, why in ((["shp", "origin", "work"], "shell alias"), (["send-pack", url, "work"], "send-pack"),
                      (["run", "push", "origin", "work"], "shell alias"),
                      (["-c", "alias.sp=send-pack", "sp", url, "work"], "send-pack")):
        out = _wrapped_git(repo, tmp_path, *args)
        assert out.returncode != 0 and why in out.stderr and "tt-project" in out.stderr, (args, out.stderr)
    assert "refs/heads/work" not in _remote_refs(repo)
    # Clean through an alias goes; aliases to other commands and a builtin's name run as git runs them.
    _git_out(repo, "checkout", "-q", "-B", "tidy", "main")
    _commit(repo, "b.txt", "clean\n")
    assert _wrapped_git(repo, tmp_path, "pp", "-q", "origin", "tidy").returncode == 0
    assert "refs/heads/tidy" in _remote_refs(repo)
    out = _wrapped_git(repo, tmp_path, "-c", "alias.st=status --short", "st")
    assert out.returncode == 0 and "On branch" not in out.stdout, out.stderr
    out = _wrapped_git(repo, tmp_path, "-c", "alias.status=push origin work", "status")
    assert out.returncode == 0 and "On branch tidy" in out.stdout and "refs/heads/work" not in _remote_refs(repo)
    # Outside a run every call goes straight to git, aliases included.
    monkeypatch.delenv("TTP_TASK")
    out = _wrapped_git(repo, tmp_path, "-c", "alias.sp=send-pack", "sp", "--help")
    assert "tt-project" not in out.stderr
    from ttp import hook
    monkeypatch.setenv("TTP_TASK", "57")
    assert "skips" in hook.git_push_bypass([["/usr/bin/git", "send-pack", url, "work"]])


def test_runs_gh_release_assets_are_scanned_to_their_end(env, tmp_path, monkeypatch):
    """The files `gh release upload` and `gh release create` send (an optional #label after each) are
    scanned with the same classes, to their end however large; the refusal names the file, not the value."""
    from ttp import outscan
    p = _outscan_project(env, monkeypatch)
    small, big = tmp_path / "notes.txt", tmp_path / "asset.bin"
    with open(big, "wb") as f:   # the leak sits past MAX_FILE_BYTES
        f.write(b"\0" * (outscan.MAX_FILE_BYTES + 100) + f"key {_OUT_SECRET}".encode())
    for leak in (_OUT_SHAPE, "serial " + _OUT_FORBIDDEN):
        small.write_text(f"x {leak}\n")
        for args in (["release", "upload", "v1", str(small)], ["release", "upload", "v1", f"{small}#Notes"],
                     ["release", "create", "v1", "--notes", "n [authored by AI]", "-t", "t", str(small)]):
            (tmp_path / "gh.log").write_text("")
            r = _gh_main(p, tmp_path, args)
            err = r.stderr.decode()
            assert r.returncode == 1 and "notes.txt" in err and "nothing was sent" in err, (args, err)
            assert leak not in err and (tmp_path / "gh.log").read_text() == "", args
    r = _gh_main(p, tmp_path, ["release", "upload", "v1", "--clobber", str(big)])
    assert r.returncode == 1 and "stored secret" in r.stderr.decode() and "asset.bin" in r.stderr.decode()
    small.write_text("all green\n")
    r = _gh_main(p, tmp_path, ["release", "upload", "v1", str(small)])
    assert "holds" not in r.stderr.decode(), r.stderr


def test_audit_counts_the_leak_scan_refusals_by_route(env, tmp_path, monkeypatch, capsys):
    """Each refusal (gh, ttp note --to, ttp notify, git push) is recorded without its value, and
    `ttp audit` counts them by route."""
    from ttp import audit, cli
    p, repo, push = _push_run(env, tmp_path, monkeypatch)
    _git_out(repo, "checkout", "-q", "-B", "work", "main")
    _commit(repo, "a.txt", f"token {_OUT_SHAPE}\n")
    assert push("origin", "work").returncode != 0
    assert _gh_main(p, tmp_path, ["pr", "comment", "7", "-b", _OUT_SHAPE]).returncode == 1
    _note_run(env, monkeypatch, p)
    assert _note(cli, "--to", "second", f"fyi {_OUT_SECRET}") == 2
    with pytest.raises(SystemExit):
        cli.main(["notify", f"fyi {_OUT_SECRET}"])
    capsys.readouterr()
    rows = p.db.q("SELECT text, data FROM events WHERE kind='outscan_refused'")
    assert len(rows) == 4 and not any(v in r["text"] + r["data"] for r in rows for v in (_OUT_SHAPE, _OUT_SECRET))
    line = next(x for x in audit.lines(p.db, p=p) if x.startswith("leak scan refusals"))
    assert line == "leak scan refusals: 4 (gh 1, git push 1, ttp note --to 1, ttp notify 1)", line
    assert audit.report(p.db, p=p)["outscan_refusals"] == {"gh": 1, "git push": 1, "ttp note --to": 1, "ttp notify": 1}


def test_ttp_push_scans_the_outgoing_history_first_and_runs_no_check_on_a_leak(env, tmp_path, monkeypatch):
    """`ttp push` runs the leak scan of tip..HEAD as its own first step: a secret shape in an earlier
    commit of the change (later removed) fails in seconds with the check never started and nothing
    sent; a clean change goes on to its checks and lands."""
    from ttp import push
    p, repo, _ = _push_run(env, tmp_path, monkeypatch)
    ran = tmp_path / "check-ran"
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    checks = push.check_list([f"touch {ran}"])
    _git_out(repo, "push", "-q", "origin", "main:proj")
    _git_out(repo, "checkout", "-q", "-B", "work", "main")
    _commit(repo, "conf.txt", f"key = {_OUT_SHAPE}\n")
    _commit(repo, "conf.txt", "key = from the store\n")
    said: list[str] = []
    before = _remote_refs(repo)
    assert push.push(repo, "origin", "proj", checks, 1, say=said.append) == push.CHECKS_FAILED
    assert not ran.exists() and _remote_refs(repo) == before, said
    assert any("ttp push refused" in m and "GitHub token shape" in m and "not pushing" in m for m in said), said
    assert not any(_OUT_SHAPE in m for m in said)
    from ttp import outscan
    assert any(outscan.REWRITE_TIP in m for m in said), said   # how to rewrite without a .git-rewrite left behind
    _git_out(repo, "checkout", "-q", "-B", "clean", "main")
    _commit(repo, "ok.txt", "fine\n")
    said.clear()
    assert push.push(repo, "origin", "proj", checks, 1, say=said.append) == 0, said
    assert ran.exists() and any("leak scan: the outgoing commits hold no secret shape" in m for m in said), said
