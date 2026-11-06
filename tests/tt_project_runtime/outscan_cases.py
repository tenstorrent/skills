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
    for route in routes:
        args, data = route("status: all green")
        log.write_text("")
        r = _gh_main(p, tmp_path, args, data, fake=fake)
        assert r.returncode == 0 and "refused" not in r.stderr.decode(), (args, r.stderr)
        assert log.read_text().count("\n") == 1, args
        if data:
            assert "<status: all green>" in log.read_text(), args   # read once, passed on to gh
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
