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
