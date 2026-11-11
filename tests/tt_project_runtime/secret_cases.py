# Per-user keys (`ttp secret`): the endpoint/<alias> slot for OpenAI-compatible endpoints (endpoint.py).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it. No test reaches the network.
from __future__ import annotations

_EP_KEY = "sk-placeholder-0123456789"
_EP_URL = "https://api.example.com/v1"


def _endpoint_home(env, monkeypatch):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "secrets-home")
    return project


def _secret(monkeypatch, *argv, key=_EP_KEY):
    from ttp import cli
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    cli.main(["secret", *argv])


def test_secret_endpoint_saves_key_and_url_after_a_passing_check(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    seen = []
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (seen.append((url, key)), (True, "HTTP 200"))[1])
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL + "/")
    out = capsys.readouterr()
    assert seen == [(_EP_URL, _EP_KEY)]
    assert "endpoint/tt-cloud" in out.out and _EP_KEY not in out.out + out.err
    saved = project.load_secrets()["endpoint/tt-cloud"]
    assert saved["key"] == _EP_KEY and saved["url"] == _EP_URL and saved["verified"] is True
    assert project.secrets_path().stat().st_mode & 0o777 == 0o600


def test_secret_endpoint_failed_check_refuses_to_save_unless_forced(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (False, "HTTP 401 Unauthorized"))
    with pytest.raises(SystemExit) as e:
        _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL)
    assert e.value.code != 0
    out = capsys.readouterr()
    assert "HTTP 401" in out.err and "--force" in out.err and _EP_KEY not in out.out + out.err
    assert "endpoint/tt-cloud" not in project.load_secrets()
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL, "--force")
    saved = project.load_secrets()["endpoint/tt-cloud"]
    assert saved["key"] == _EP_KEY and saved["verified"] is False and saved["check"] == "HTTP 401 Unauthorized"
    assert _EP_KEY not in capsys.readouterr().out


def test_secret_show_lists_endpoints_with_url_and_state_never_the_key(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    project.save_secret("jev", {"via": "typesafe", "key": "jev-key-placeholder"})
    endpoint.save("tt-cloud", _EP_URL, _EP_KEY, True, "HTTP 200")
    endpoint.save("other_2", "http://127.0.0.1:8000/v1", "other-key-placeholder", False, "HTTP 503")
    from ttp import cli
    cli.main(["secret", "show"])
    out = capsys.readouterr().out
    assert _EP_KEY not in out and "other-key-placeholder" not in out and "jev-key-placeholder" not in out
    lines = out.splitlines()
    assert json.loads(lines[0]) == {"jev": ["key", "via"]}   # the other slots keep their JSON line
    assert lines[1:] == ["endpoint/other_2 http://127.0.0.1:8000/v1 not verified (HTTP 503)",
                         f"endpoint/tt-cloud {_EP_URL} verified"]


def test_secret_endpoint_refuses_a_bad_alias_url_or_empty_key(env, monkeypatch, capsys):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: pytest.fail("checked a refused key"))
    for argv in (["endpoint", "a/b", "--url", _EP_URL], ["endpoint", "../x", "--url", _EP_URL],
                 ["endpoint", "has space", "--url", _EP_URL], ["endpoint", "--url", _EP_URL],
                 ["endpoint", "ok", "--url", "api.example.com/v1"], ["endpoint", "ok"],
                 ["jev", "stray-alias"]):
        with pytest.raises(SystemExit) as e:
            _secret(monkeypatch, *argv)
        assert e.value.code != 0, argv
    with pytest.raises(SystemExit):
        _secret(monkeypatch, "endpoint", "ok", "--url", _EP_URL, key="")
    assert _EP_KEY not in capsys.readouterr().err
    assert not project.load_secrets()
    with pytest.raises(ValueError):
        endpoint.credentials("a/b")


def test_endpoint_credentials_return_the_saved_key_and_url(env, monkeypatch):
    from ttp import endpoint
    _endpoint_home(env, monkeypatch)
    assert endpoint.credentials("tt-cloud") is None
    monkeypatch.setattr(endpoint, "verify", lambda url, key, timeout=10: (True, "HTTP 200"))
    _secret(monkeypatch, "endpoint", "tt-cloud", "--url", _EP_URL)
    assert endpoint.credentials("tt-cloud") == (_EP_KEY, _EP_URL)
    assert endpoint.credentials("other") is None


def test_endpoint_verify_sends_a_bearer_get_to_models_and_keeps_the_key_out_of_errors(monkeypatch):
    from ttp import endpoint
    sent = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(self, req, timeout=None):
        sent.append((req.get_method(), req.full_url, req.get_header("Authorization"), timeout))
        if len(sent) == 1:
            return Resp()
        if len(sent) == 2:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if len(sent) == 3:
            raise urllib.error.URLError(f"refused for {_EP_KEY}")
        raise TimeoutError(f"timed out sending {_EP_KEY}")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urlopen)
    assert endpoint.verify(_EP_URL, _EP_KEY) == (True, "HTTP 200")
    assert sent[0] == ("GET", f"{_EP_URL}/models", f"Bearer {_EP_KEY}", endpoint.VERIFY_TIMEOUT_S)
    assert endpoint.VERIFY_TIMEOUT_S <= 30
    for want in ("HTTP 401 Unauthorized", "cannot reach", "TimeoutError"):
        ok, msg = endpoint.verify(_EP_URL, _EP_KEY)
        assert not ok and want in msg and _EP_KEY not in msg, msg


@pytest.mark.parametrize("target", ["https://other.example.org/models", "http://api.example.com/v1/models"])
def test_endpoint_verify_never_forwards_the_key_on_a_cross_host_or_downgraded_redirect(monkeypatch, target):
    import email.message
    import io
    import urllib.response
    from ttp import endpoint
    calls = []

    def https_open(self, req):
        calls.append((req.full_url, req.get_header("Authorization")))
        h = email.message.Message()
        h["Location"] = target
        r = urllib.response.addinfourl(io.BytesIO(b""), h, req.full_url, 302)
        r.msg = "Found"
        return r
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open",
                        lambda self, req: calls.append((req.full_url, req.get_header("Authorization"))))
    ok, msg = endpoint.verify(_EP_URL, _EP_KEY)
    assert not ok and "302 redirect; key not sent" in msg and "example" not in msg and "key not sent" in msg and _EP_KEY not in msg, msg
    assert calls == [(f"{_EP_URL}/models", f"Bearer {_EP_KEY}")]


# Stored secrets never reach tool output, run logs, hand-offs, notes or helper services (redact.py).
_MASK = "[secret:endpoint/tt-cloud]"


def _stored(monkeypatch, home):
    """The fake endpoint key saved in `home`'s secrets store (the one runners under TTP_HOME read too)."""
    from ttp import endpoint, project
    monkeypatch.setattr(project, "HOME_DIR", pathlib.Path(home))
    endpoint.save("tt-cloud", _EP_URL, _EP_KEY, True, "HTTP 200")
    return project


def test_credentials_never_print_the_key_and_still_unpack(env, monkeypatch):
    from ttp import endpoint
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    got = endpoint.credentials("tt-cloud")
    key, url = got
    assert (key, url) == (_EP_KEY, _EP_URL) and got.key == _EP_KEY
    assert _EP_KEY not in repr(got) + str(got) + f"{got}" + repr([got]) and _EP_URL in repr(got)


def test_secret_check_reports_auth_state_and_never_the_key(env, monkeypatch, capsys):
    """The harness path for testing a key: `ttp secret check` (#4072 printed one through an ad-hoc read)."""
    import urllib.error
    from ttp import cli, endpoint
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    for err, state in ((urllib.error.HTTPError(_EP_URL, 401, f"bad key {_EP_KEY}", {}, None), "auth failed"),
                       (urllib.error.URLError(f"unknown url type: {_EP_KEY}/models"), "not ok")):
        class Opener:
            def open(self, req, timeout=0, _e=err):
                raise _e
        monkeypatch.setattr(endpoint.urllib.request, "build_opener", lambda *a, _o=Opener(): _o)
        with pytest.raises(SystemExit) as e:
            cli.main(["secret", "check", "tt-cloud"])
        out = capsys.readouterr()
        assert e.value.code == 1 and state in out.out and _EP_KEY not in out.out + out.err
    class Ok:
        def open(self, req, timeout=0):
            assert req.get_header("Authorization") == f"Bearer {_EP_KEY}"
            return contextlib.nullcontext(types.SimpleNamespace(status=200))
    monkeypatch.setattr(endpoint.urllib.request, "build_opener", lambda *a: Ok())
    with pytest.raises(SystemExit) as e:
        cli.main(["secret", "check"])
    out = capsys.readouterr()
    assert e.value.code == 0 and "reachable, auth ok" in out.out and _EP_KEY not in out.out + out.err


def test_secret_check_lists_served_models_and_flags_a_missing_tier_model(env, monkeypatch, capsys):
    """#5194: no tool showed which models an endpoint serves; a withdrawn tier model went unseen."""
    import urllib.error
    from ttp import cli, endpoint
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    p = make(env)
    p.set_config("providers.chat", {"endpoint": "tt-cloud", "tiers": {"light": {"model": "example-org/model-a", "route": True},
                                                                       "standard": {"model": "example-org/model-gone"}}})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    body = json.dumps({"object": "list", "data": [{"id": "example-org/model-b"}, {"id": "example-org/model-a"}]}).encode()
    class Ok:
        def open(self, req, timeout=0):
            assert req.full_url == f"{_EP_URL}/models"
            return contextlib.nullcontext(types.SimpleNamespace(status=200, read=lambda n=-1: body))
    monkeypatch.setattr(endpoint.urllib.request, "build_opener", lambda *a: Ok())
    with pytest.raises(SystemExit) as e:
        cli.main(["secret", "check", "tt-cloud"])
    out = capsys.readouterr()
    assert e.value.code == 0, out
    assert "serves 2 models: example-org/model-a, example-org/model-b" in out.out
    assert "providers.chat.tiers.standard.model example-org/model-gone: NOT SERVED" in out.out
    assert "tiers.light.model" not in out.out
    text = out.out + out.err
    assert _EP_KEY not in text and _EP_URL not in text and "api.example.com" not in text
    # A failed check names neither the key nor the URL or host either.
    for err in (urllib.error.HTTPError(_EP_URL, 404, "gone", {}, None),
                urllib.error.URLError(f"cannot connect to {_EP_URL} with {_EP_KEY}"),
                urllib.error.URLError("certificate is not valid for 'api.example.com'"),
                OSError("connect to api.example.com:443 failed")):
        class Bad:
            def open(self, req, timeout=0, _e=err):
                raise _e
        monkeypatch.setattr(endpoint.urllib.request, "build_opener", lambda *a, _o=Bad(): _o)
        ok, msg = endpoint.check("tt-cloud")
        assert not ok and _EP_KEY not in msg and _EP_URL not in msg and "api.example.com" not in msg, msg
    # Ids only: an answer that is not a list of models gives none, never its text.
    assert endpoint.model_ids({"data": [{"id": "a"}, {"name": "x"}, 3, "b"]}) == ["a", "b"]
    assert endpoint.model_ids({"error": "nope"}) == [] and endpoint.model_ids(None) == []


def test_a_models_check_redirect_names_no_host_in_secret_check_or_the_served_record(env, monkeypatch, capsys):
    """Review #5307: the redirect message named its target host, and only the exact base URL was scrubbed."""
    import http.server
    import threading
    from ttp import cli, endpoint, project, route

    class Moved(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere/models")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Moved)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = str(srv.server_port)
        monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "redirect-home")
        endpoint.save("tt-cloud", f"http://127.0.0.1:{port}/v1", _EP_KEY, True, "HTTP 200")
        with pytest.raises(SystemExit) as e:
            cli.main(["secret", "check", "tt-cloud"])
        out = capsys.readouterr()
        text = out.out + out.err
        assert e.value.code == 1 and "302 redirect; key not sent" in text, text
        p = make(env)
        assert route.refresh_served(p.state, "tt-cloud")
        raw = (p.state / route.MODELS_FILE).read_text()
        assert "302 redirect" in raw, raw
        for leak in ("127.0.0.1", port, _EP_KEY):
            assert leak not in text and leak not in raw, (leak, text, raw)
    finally:
        srv.shutdown()
        srv.server_close()


def test_secret_env_hands_the_key_by_environment_and_masks_the_output(env, monkeypatch, capfd):
    from ttp import cli
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    with pytest.raises(SystemExit) as e:
        cli.main(["secret", "env", "tt-cloud", "--", sys.executable, "-c",
                  "import os; print('key=' + os.environ['OPENAI_API_KEY'], os.environ['OPENAI_BASE_URL'])"])
    out = capfd.readouterr()
    assert e.value.code == 0 and f"key={_MASK} {_EP_URL}" in out.out and _EP_KEY not in out.out + out.err
    with pytest.raises(SystemExit) as e:
        cli.main(["secret", "show", "--", "true"])
    assert e.value.code != 0


def test_redact_masks_every_stored_secret_and_only_secret_fields(env, monkeypatch):
    from ttp import redact
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    project.save_secret("jev", {"via": "typesafe", "key": "jev-placeholder-key-42"})
    project.save_secret("slack", {"bot_token": "slack-placeholder-token", "user_id": "U0PLACEHOLDER", "user_email": "a@b.c"})
    text = f"a {_EP_KEY} b jev-placeholder-key-42 c slack-placeholder-token d U0PLACEHOLDER {_EP_URL} typesafe"
    got = redact.scrub(text)
    assert got == (f"a {_MASK} b [secret:jev] c [secret:slack] d U0PLACEHOLDER {_EP_URL} typesafe")
    assert redact.scrub_obj({"s": [text], _EP_KEY: 1}) == {"s": [got], _MASK: 1}
    assert redact.scrub(None) is None and redact.scrub(5) == 5


def test_secret_store_and_run_folders_are_owner_only(env, monkeypatch):
    from ttp import redact
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    assert project.secrets_path().stat().st_mode & 0o777 == 0o600
    assert project.HOME_DIR.stat().st_mode & 0o777 == 0o700
    d = redact.private_dir(env["tmp"] / "runs" / "7")
    assert d.stat().st_mode & 0o777 == 0o700
    d.chmod(0o755)
    assert redact.private_dir(d).stat().st_mode & 0o777 == 0o700


def test_scrub_run_masks_run_files_and_the_agents_session_transcripts(env, monkeypatch):
    from ttp import redact
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    run = env["tmp"] / "runs" / "9"
    run.mkdir(parents=True)
    sid = "0123abcd-4567-89ef-0123-456789abcdef"
    (run / "output.jsonl").write_text(json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n"
                                      + json.dumps({"tool_result": f"unknown url type: {_EP_KEY}/models"}) + "\n")
    (run / "result.json").write_text(json.dumps({"status": "done", "summary": f"key was {_EP_KEY}"}))
    (run / "job.log").write_text(f"Authorization: Bearer {_EP_KEY}\n")
    os.chmod(run / "job.log", 0o640)
    cfg, codex = env["tmp"] / "claude-own", env["tmp"] / "codex-own"
    (run / "run.json").write_text(json.dumps({"provider": "claude", "cwd": "/w", "env": {"CLAUDE_CONFIG_DIR": str(cfg)}}))
    main = cfg / "projects" / "-w" / f"{sid}.jsonl"
    sub = cfg / "projects" / "-w" / sid / "subagents" / "agent-1.jsonl"
    for f in (main, sub):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"content": f"x {_EP_KEY} y"}) + "\n")
    other = cfg / "projects" / "-w" / "another-session.jsonl"
    other.write_text(f"{_EP_KEY}\n")   # another session: not this run's
    assert redact.scrub_run(run) == 5
    for f in (run / "output.jsonl", run / "result.json", run / "job.log", main, sub):
        assert _EP_KEY not in f.read_text() and _MASK in f.read_text(), f
    json.loads((run / "result.json").read_text())
    assert (run / "job.log").stat().st_mode & 0o777 == 0o640
    assert _EP_KEY in other.read_text()
    assert redact.scrub_run(run) == 0   # safe to run twice
    rollout = codex / "sessions" / "2026" / "10" / "28" / f"rollout-2026-10-28T00-00-00-{sid}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"msg": 'with "quotes" ' + _EP_KEY}) + "\n")
    (run / "run.json").write_text(json.dumps({"provider": "codex", "cwd": "/w", "env": {"CODEX_HOME": str(codex)}}))
    assert redact.scrub_run(run) == 1 and _EP_KEY not in rollout.read_text()


def test_db_writes_and_daemon_log_mask_stored_secrets(env, monkeypatch):
    """Notes, events, notify text and hand-offs all go through Db.x; the daemon log through log()."""
    p = make(env)
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    from ttp import daemon
    tid = p.db.add_task(f"probe {_EP_KEY}", f"spec {_EP_KEY}", kind="work", tier="light", origin="user")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (time.time(), "worker", "note", "low", f"note {_EP_KEY}", "new"))
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"summary": f"done {_EP_KEY}"}), tid))
    daemon.log(p, f"error {_EP_KEY}")
    dump = json.dumps([p.db.q("SELECT * FROM events"), p.db.q("SELECT * FROM tasks")])
    assert _EP_KEY not in dump and _MASK in dump
    log = (p.logs / "daemon.log").read_text()
    assert _EP_KEY not in log and _MASK in log


@pytest.mark.parametrize("damage", ["mode0", "directory", "json-list", "non-utf8"])
def test_a_damaged_secret_store_never_breaks_db_writes_or_the_daemon_log(env, monkeypatch, damage):
    """Db.x and daemon.log scrub every write: a store that cannot be read or parsed keeps the values read
    before it was damaged (none if never read) and never raises."""
    p = make(env)
    from ttp import daemon, redact
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    store = project.secrets_path()
    monkeypatch.setattr(redact, "_cache", {"sig": None, "pairs": []})

    def damage_store():
        if damage == "mode0":
            store.chmod(0)
        elif damage == "directory":
            store.unlink()
            store.mkdir()
        else:
            store.write_bytes(b"[]" if damage == "json-list" else b'{"x": "\xff\xfe' + bytes([0xff, 0xfe]) + b'"}')

    def write(text):
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (time.time(), "worker", "note", "low", text, "new"))
        daemon.log(p, text)

    try:
        damage_store()                                  # damaged before it was ever read
        write("never read")
        assert redact.scrub(f"k {_EP_KEY}") == f"k {_EP_KEY}"
        if store.is_dir():
            store.rmdir()
        else:
            store.chmod(0o600)
            store.unlink()
        _stored(monkeypatch, env["tmp"] / "secrets-home")
        write(f"read {_EP_KEY}")                        # read once, then damaged: the old value stays masked
        damage_store()
        write(f"after {_EP_KEY}")
    finally:
        if store.exists() and not store.is_dir():
            store.chmod(0o600)
    dump = json.dumps(p.db.q("SELECT text FROM events")) + (p.logs / "daemon.log").read_text()
    assert "never read" in dump and f"read {_MASK}" in dump and f"after {_MASK}" in dump
    assert _EP_KEY not in dump


def test_a_run_that_prints_a_stored_secret_keeps_it_out_of_its_log_handoff_and_records(env, monkeypatch):
    p = make(env)
    _stored(monkeypatch, os.environ["TTP_HOME"])   # the runner subprocess reads the same store
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_SHELL", f"echo 'leaked {_EP_KEY}'; echo 'leaked {_EP_KEY}' > \"$TTP_RUN_DIR/job.log\"")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"status": "done", "summary": f"printed {_EP_KEY} once"}))
    tid = p.db.add_task("test the endpoint", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done")
    run = p.db.one("SELECT id, dir FROM runs WHERE task=?", (tid,))
    run_dir = pathlib.Path(run["dir"]) if run["dir"] else p.runs / str(run["id"])
    assert run_dir.stat().st_mode & 0o777 == 0o700
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert _EP_KEY not in f.read_text(errors="replace"), f.name
    assert _MASK in (run_dir / "result.json").read_text()
    dump = json.dumps([p.db.q("SELECT * FROM tasks"), p.db.q("SELECT * FROM events"), p.db.q("SELECT * FROM runs")])
    assert _EP_KEY not in dump and _MASK in dump


def test_text_for_jev_and_slack_is_masked(env, monkeypatch):
    from ttp import slack
    from ttp.providers import jev
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(jev.urllib.request, "urlopen",
                        lambda req, timeout=0: (sent.append(req.data.decode()), Resp(b'{"answers": {}}'))[1])
    j = jev.Jev({"enabled": True}, secrets={"jev": {"key": "jev-placeholder-key-42", "via": "typesafe"}})
    j.decide(f"log {_EP_KEY}", {"q": f"why {_EP_KEY}?"})
    assert sent and _EP_KEY not in sent[0] and _MASK in sent[0]
    s = slack.Slack("slack-placeholder-token", "U1", None)
    monkeypatch.setattr(s, "dm_channel", lambda: "D1")
    monkeypatch.setattr(s, "call", lambda method, **kw: (sent.append(kw["text"]), {"ts": "1"})[1])
    s.post("proj", f"alert {_EP_KEY}")
    assert _EP_KEY not in sent[-1] and _MASK in sent[-1]


def test_endpoint_errors_mask_every_stored_secret(env, monkeypatch):
    import urllib.error
    from ttp import endpoint, project
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    project.save_secret("jev", {"via": "typesafe", "key": "jev-placeholder-key-42"})

    class Opener:
        def open(self, req, timeout=0):
            raise urllib.error.URLError(f"echoed jev-placeholder-key-42 and {_EP_KEY}")
    monkeypatch.setattr(endpoint.urllib.request, "build_opener", lambda *a: Opener())
    ok, msg = endpoint.verify("https://other.example.com/v1", "another-placeholder-key")
    assert not ok and "jev-placeholder-key-42" not in msg and _EP_KEY not in msg and "[secret:jev]" in msg


@pytest.mark.parametrize("cmd, refused", [
    ("python3 -I -c 'from ttp import endpoint; print(endpoint.credentials(\"tt-cloud\"))'", True),
    ("cd x && python3 -c 'import ttp.project as p; print(p.load_secrets())'", True),
    ("python3 - <<'EOF'\nfrom ttp.endpoint import credentials\nurl, key = credentials('tt-cloud')\nEOF", True),
    ("cat ~/.tt-project/secrets.json", True),
    ("jq . \"$TTP_HOME/secrets.json\"", True),
    ("python3 -c 'from ttp import endpoint as e; print(e.env_for(\"x\"))'", True),
    ("grep -n 'credentials(' plugins/tt-project/runtime/ttp/endpoint.py", False),
    ("ttp secret check tt-cloud", False),
    ("ttp secret env tt-cloud -- python3 probe.py", False),
    ("ttp secret set peer-key < value.txt", False),
    ("ttp secret generate peer-set --keys alpha,beta", False),
    ("ttp secret env --secret peer-set:alpha=PEER -- python3 probe.py", False),
    ("ttp secret env tt-cloud --secret alpha-key=ALPHA -- sh -c 'curl -H \"x: $ALPHA\" localhost'", False),
    ("ttp secret env --secret alpha-key=A -- cat ~/.tt-project/secrets.json", True),
    ("ttp secret env --secret alpha-key=A -- python3 -c 'from ttp import project; print(project.load_secrets())'", True),
    ("python3 - <<'EOF'\ndef credentials(alias):\n    return None\nEOF", False),
])
def test_hook_refuses_ad_hoc_reads_of_the_secret_store(env, monkeypatch, cmd, refused):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    denied = bool(out) and out["hookSpecificOutput"].get("permissionDecision") == "deny"
    assert denied == refused, out
    if refused:
        assert "ttp secret check" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "ttp secret env --secret <name>" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_refuses_a_script_file_that_reads_the_secret_store(env, monkeypatch):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    (env["tmp"] / "probe.py").write_text("from ttp import endpoint\nprint(endpoint.credentials('tt-cloud'))\n")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "python3 probe.py"}, "cwd": str(env["tmp"])})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


_CUSTOM = "fake-custom-value-0123456789"


def _custom(monkeypatch, capsys, *argv, stdin=""):
    """`ttp secret <argv>` with `stdin`; (exit code, stdout + stderr)."""
    from ttp import cli
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        cli.main(["secret", *argv])
        code = 0
    except SystemExit as e:
        code = e.code or 0
    out = capsys.readouterr()
    return code, out.out + out.err


def test_secret_set_reads_the_value_from_stdin_and_stores_it_owner_only(env, monkeypatch, capsys):
    project = _endpoint_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, "set", "alpha-key", stdin=_CUSTOM + "\n")
    assert code == 0 and "alpha-key" in out and _CUSTOM not in out
    assert project.load_secrets()["alpha-key"] == _CUSTOM
    assert project.secrets_path().stat().st_mode & 0o777 == 0o600
    code, out = _custom(monkeypatch, capsys, "set", "alpha-key", stdin=_CUSTOM + "-2\r\n")   # replaces it
    assert code == 0 and project.load_secrets()["alpha-key"] == _CUSTOM + "-2" and _CUSTOM not in out
    code, _ = _custom(monkeypatch, capsys, "set", "multi_line", stdin=_CUSTOM + "\nsecond line\n\n")
    assert code == 0 and project.load_secrets()["multi_line"] == _CUSTOM + "\nsecond line\n"   # one newline off


@pytest.mark.parametrize("argv, stdin, why", [
    (["set", "alpha", _CUSTOM], "", "stdin"),                  # a value on the command line
    (["set", "alpha", "--", _CUSTOM], "", "stdin"),
    (["set", "alpha", "--force", _CUSTOM], "", "stdin"),       # after an option: argparse would echo it
    (["set", "alpha", "-" + _CUSTOM], "", "stdin"),            # a value that looks like an option
    (["set", "alpha", _CUSTOM, "--keys"], "", "--keys"),       # a valued option with no value
    (["set", "alpha", "--url", _CUSTOM], "", "--url"),          # an option set does not take
    (["set", "alpha"], "", "empty"),
    (["set", "alpha"], "\n", "empty"),
    (["set", "alpha"], "short\n", "characters"),               # too short to be masked in logs
    (["set"], _CUSTOM, "name"),
    (["set", "alpha", "--keys", "a,b"], _CUSTOM, "--keys"),
])
def test_secret_set_refuses_a_command_line_value_an_empty_or_short_value(env, monkeypatch, capsys, argv, stdin, why):
    project = _endpoint_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, *argv, stdin=stdin)
    assert code != 0 and why in out and _CUSTOM not in out
    assert "alpha" not in project.load_secrets()


def test_secret_generate_stores_fresh_tokens_and_refuses_to_overwrite_unless_forced(env, monkeypatch, capsys):
    project = _endpoint_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, "generate", "peer-set", "--keys", "alpha,beta, gamma_2")
    first = project.load_secrets()["peer-set"]
    assert code == 0 and sorted(first) == ["alpha", "beta", "gamma_2"]
    assert len(set(first.values())) == 3 and all(len(t) >= 32 for t in first.values())
    assert "peer-set" in out and "alpha" in out and "gamma_2" in out
    assert not any(t in out for t in first.values())
    code, out = _custom(monkeypatch, capsys, "generate", "peer-set", "--keys", "alpha")
    assert code != 0 and "--force" in out and project.load_secrets()["peer-set"] == first
    code, out = _custom(monkeypatch, capsys, "set", "alpha-key", stdin=_CUSTOM)
    code, out = _custom(monkeypatch, capsys, "generate", "alpha-key", "--keys", "alpha")   # a `set` one too
    assert code != 0 and project.load_secrets()["alpha-key"] == _CUSTOM
    code, out = _custom(monkeypatch, capsys, "generate", "peer-set", "--keys", "alpha,delta", "--force")
    again = project.load_secrets()["peer-set"]
    assert code == 0 and sorted(again) == ["alpha", "delta"] and again["alpha"] != first["alpha"]
    assert not any(t in out for t in again.values())


@pytest.mark.parametrize("argv, why", [
    (["generate", "peer"], "--keys"),
    (["generate", "peer", "--keys", ""], "--keys"),
    (["generate", "peer", "--keys", "alpha,,beta"], "key name"),
    (["generate", "peer", "--keys", "Alpha"], "key name"),
    (["generate", "peer", "--keys", "alpha,alpha"], "twice"),
    (["generate", "peer", "--keys", "alpha", "beta"], "no value"),
    (["generate", "peer", "--keys=alpha", "--", "beta"], "no value"),
    (["generate", "--keys", "alpha"], "name"),
    (["endpoint", "tt-cloud", "--keys", "alpha"], "--keys"),
])
def test_secret_generate_refuses_bad_key_lists(env, monkeypatch, capsys, argv, why):
    project = _endpoint_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, *argv)
    assert code != 0 and why in out
    assert "peer" not in project.load_secrets()


@pytest.mark.parametrize("name", ["jev", "slack", "endpoint", "show", "check", "env", "push", "set", "generate",
                                  "Alpha", "a/b", "endpoint/x", "_alpha", "al.pha", "a" * 65])
@pytest.mark.parametrize("kind", ["set", "generate"])
def test_secret_custom_names_are_checked_and_builtin_secrets_unchanged(env, monkeypatch, capsys, name, kind):
    from ttp import endpoint
    project = _endpoint_home(env, monkeypatch)
    project.save_secret("jev", {"via": "typesafe", "key": "jev-placeholder-key-42"})
    endpoint.save("x", _EP_URL, _EP_KEY, True, "HTTP 200")
    before = project.load_secrets()
    extra = ["--keys", "alpha"] if kind == "generate" else []
    code, out = _custom(monkeypatch, capsys, kind, name, *extra, "--force", stdin=_CUSTOM)
    assert code != 0 and "name" in out and _CUSTOM not in out
    assert project.load_secrets() == before


def test_custom_secrets_are_masked_in_tool_output_logs_and_handoffs_and_show(env, monkeypatch, capsys):
    from ttp import cli, daemon, redact
    p = make(env)
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    assert _custom(monkeypatch, capsys, "set", "alpha-key", stdin=_CUSTOM + "\n")[0] == 0
    assert _custom(monkeypatch, capsys, "generate", "peer-set", "--keys", "alpha,beta")[0] == 0
    tokens = project.load_secrets()["peer-set"]
    text = f"tool printed {_CUSTOM} and {tokens['alpha']} then {tokens['beta']} and {_EP_KEY}"
    assert redact.scrub(text) == ("tool printed [secret:alpha-key] and [secret:peer-set] then [secret:peer-set] "
                                  f"and {_MASK}")
    run = env["tmp"] / "runs" / "11"
    run.mkdir(parents=True)
    (run / "output.jsonl").write_text(json.dumps({"type": "user", "content": text}) + "\n")
    (run / "job.log").write_text(text + "\n")
    (run / "result.json").write_text(json.dumps({"status": "done", "summary": text,
                                                 "metrics": {"peer": json.dumps(tokens)}}))
    assert redact.scrub_run(run) >= 3
    for f in run.iterdir():
        body = f.read_text()
        assert _CUSTOM not in body and not any(t in body for t in tokens.values()), f.name
        assert "[secret:peer-set]" in body
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (time.time(), "worker", "note", "low", text, "new"))
    daemon.log(p, text)
    for body in (json.dumps(p.db.q("SELECT * FROM events")), (p.logs / "daemon.log").read_text()):
        assert _CUSTOM not in body and not any(t in body for t in tokens.values()) and "[secret:alpha-key]" in body
    cli.main(["secret", "show"])
    out = capsys.readouterr().out
    assert _CUSTOM not in out and not any(t in out for t in tokens.values())
    assert json.loads(out.splitlines()[0]) == {"alpha-key": "set", "peer-set": ["alpha", "beta"]}


def _env_run(monkeypatch, capfd, *argv):
    """`ttp secret env <argv>`; (exit code, stdout + stderr as the caller sees them)."""
    from ttp import cli
    try:
        cli.main(["secret", "env", *argv])
        code = 0
    except SystemExit as e:
        code = e.code or 0
    out = capfd.readouterr()
    return code, out.out + out.err


def _custom_store(env, monkeypatch, capsys):
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    assert _custom(monkeypatch, capsys, "set", "alpha-key", stdin=_CUSTOM + "\n")[0] == 0
    assert _custom(monkeypatch, capsys, "generate", "peer-set", "--keys", "alpha,beta")[0] == 0
    return project.load_secrets()


def test_secret_env_injects_custom_secrets_by_environment_and_masks_the_output(env, monkeypatch, capfd, tmp_path):
    store = _custom_store(env, monkeypatch, capfd)
    tokens = store["peer-set"]
    probe = tmp_path / "probe.json"
    code, out = _env_run(monkeypatch, capfd, "--secret", "alpha-key=ALPHA", "--secret=peer-set:beta=PEER_BETA", "--",
                         sys.executable, "-c",
                         "import json, os, sys; e = os.environ; "
                         f"json.dump({{'argv': sys.argv, 'a': e['ALPHA'], 'b': e['PEER_BETA']}}, open({str(probe)!r}, 'w')); "
                         "print('a=' + e['ALPHA']); print('b=' + e['PEER_BETA'], file=sys.stderr); sys.exit(3)")
    seen = json.loads(probe.read_text())
    assert seen["a"] == _CUSTOM and seen["b"] == tokens["beta"]                 # injected by environment
    assert not any(_CUSTOM in w or tokens["beta"] in w for w in seen["argv"])    # never in argv
    assert code == 3                                                            # exit code passed through
    assert "a=[secret:alpha-key]" in out and "b=[secret:peer-set]" in out
    assert _CUSTOM not in out and not any(t in out for t in tokens.values())


def test_secret_env_keeps_the_command_exact_and_combines_with_an_endpoint_alias(env, monkeypatch, capfd):
    _custom_store(env, monkeypatch, capfd)
    code, out = _env_run(monkeypatch, capfd, "tt-cloud", "--secret", "peer-set:alpha=TOK", "--", sys.executable,
                         "-c", "import os, sys; print(sys.argv[1:], os.environ['OPENAI_BASE_URL'], "
                               "len(os.environ['TOK']))", "--", "-x", "--secret=y")
    assert code == 0 and f"['--', '-x', '--secret=y'] {_EP_URL} 43" in out, out
    code, out = _env_run(monkeypatch, capfd, "--secret", "alpha-key=A", "--", sys.executable, "-c",
                         "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert code == 128 + 15


@pytest.mark.parametrize("spec, why", [
    ("nope=X", "no secret nope"),
    ("peer-set:gamma=X", "secret peer-set has no key gamma"),
    ("alpha-key:alpha=X", "secret alpha-key is a single value"),
    ("peer-set=X", "secret peer-set holds keys"),
    ("alpha-key=1BAD", "bad variable name for alpha-key"),
    ("alpha-key=A-B", "bad variable name for alpha-key"),
    ("peer-set:alpha=", "bad variable name for peer-set:alpha"),
    ("jev=X", "secret jev is built in"),
    ("slack=X", "secret slack is built in"),
    ("endpoint=X", "secret endpoint is built in"),
    ("endpoint/tt-cloud=X", "bad secret name"),
    (_CUSTOM, "--secret takes <name>[:<key>]=<VAR>"),
    (f"{_CUSTOM.upper()}=X", "bad secret name"),
    (f"peer-set:{_CUSTOM.upper()}=X", "bad key name for secret peer-set"),
])
def test_secret_env_refuses_bad_or_unknown_secrets_naming_no_value(env, monkeypatch, capfd, spec, why):
    store = _custom_store(env, monkeypatch, capfd)
    store_values = [_CUSTOM, *store["peer-set"].values(), _EP_KEY]
    code, out = _env_run(monkeypatch, capfd, "--secret", spec, "--", "true")
    assert code != 0 and why in out, out
    assert not any(v in out or v.upper() in out for v in store_values), out


@pytest.mark.parametrize("argv, why", [
    (["--secret", "alpha-key=A"], "usage"),                                 # no command
    (["--secret", "alpha-key=A", "extra", "words", "--", "true"], "usage"),  # more than one word before --
    (["--secret", "alpha-key=A", "--secret", "peer-set:alpha=A", "--", "true"], "set the same variable"),
])
def test_secret_env_refuses_a_bad_command_line(env, monkeypatch, capfd, argv, why):
    _custom_store(env, monkeypatch, capfd)
    code, out = _env_run(monkeypatch, capfd, *argv)
    assert code != 0 and why in out and _CUSTOM not in out, out


def test_secret_flag_goes_only_with_env(env, monkeypatch, capsys):
    _stored(monkeypatch, env["tmp"] / "secrets-home")
    code, out = _custom(monkeypatch, capsys, "show", "--secret", "alpha-key=A")
    assert code != 0 and "--secret goes only with" in out
    code, out = _custom(monkeypatch, capsys, "--help")
    assert code == 0 and "env --secret <name>[:<key>]=<VAR>" in " ".join(out.split())
    plugin = RUNTIME.parent
    for doc in ("docs/guide.md", "skills/tt-project-harness/SKILL.md"):
        assert "ttp secret env --secret <name>[:<key>]=<VAR> -- <command>" in (plugin / doc).read_text(), doc


def test_secret_env_masks_each_line_of_a_multi_line_secret(env, monkeypatch, capfd):
    project = _stored(monkeypatch, env["tmp"] / "secrets-home")
    pem = "-----BEGIN-----\nline-one-0123456789\nline-two-0123456789\n-----END-----"
    assert _custom(monkeypatch, capfd, "set", "pem-key", stdin=pem + "\n")[0] == 0
    assert project.load_secrets()["pem-key"] == pem
    code, out = _env_run(monkeypatch, capfd, "--secret", "pem-key=PEM", "--", sys.executable, "-c",
                         "import os; print(os.environ['PEM'])")
    assert code == 0 and "line-one" not in out and "line-two" not in out and "[secret:pem-key]" in out, out

_JEV_KEY = "fake-jev-key-0123456789abcdef"


def _jev_store(env, monkeypatch, capfd):
    """A store with the fake endpoint key, the custom secrets and a fake Jev key, in TTP_HOME too, so a
    command the daemon starts in a child process reads the same store."""
    home = env["tmp"] / "secrets-home"
    monkeypatch.setenv("TTP_HOME", str(home))
    store = _custom_store(env, monkeypatch, capfd)
    from ttp import project
    project.save_secret("jev", {"via": "direct", "key": _JEV_KEY})
    return store


_PRINT_JEV = ("import os, sys; v = os.environ['JK']; print('jk=' + v); print('err=' + v, file=sys.stderr); "
              "print('len', len(v)); sys.exit(5)")


def test_secret_env_gives_a_built_in_secret_only_by_its_field(env, monkeypatch, capfd):
    _jev_store(env, monkeypatch, capfd)
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", sys.executable, "-c", _PRINT_JEV)
    assert code == 5 and "jk=[secret:jev]" in out and "err=[secret:jev]" in out, out
    assert f"len {len(_JEV_KEY)}" in out and _JEV_KEY not in out
    # a field the store's scrub does not count as secret is masked too once given
    code, out = _env_run(monkeypatch, capfd, "--secret", "jev:via=V", "--", sys.executable, "-c",
                         "import os; print('via=' + os.environ['V'])")
    assert code == 0 and "via=direct" in out   # shorter than a maskable value: not a secret
    for spec, why in (("jev=X", "secret jev is built in"), ("jev:nope=X", "secret jev has no key nope"),
                      ("endpoint:key=X", "secret endpoint is built in"), ("show:key=X", "secret show is built in")):
        code, out = _env_run(monkeypatch, capfd, "--secret", spec, "--", "true")
        assert code != 0 and why in out and _JEV_KEY not in out, (spec, out)


def _sandboxed_run(env, monkeypatch, capfd, *argv, p=None, run="9001"):
    """`ttp secret env ...` as a sandboxed worker of a project runs it, the daemon's tick (detach.tend)
    running beside it: (exit code, output, run folder, project)."""
    import threading
    from ttp import detach, secretroute
    p = p or make(env)
    rd = p.runs / run
    rd.mkdir(parents=True)
    for k, v in (("TTP_RUN_DIR", str(rd)), ("SANDBOX_RUNTIME", "1"), ("TTP_TASK_ID", "77"), ("TTP_RUN_ID", run)):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(secretroute.SERVED_ENV, raising=False)
    monkeypatch.setattr(secretroute, "POLL_S", 0.05)
    monkeypatch.setattr(secretroute, "START_WAIT_S", 30)   # a broken tick fails, never hangs
    stop, errors = threading.Event(), []

    def daemon():
        while not stop.is_set():
            try:
                detach.tend(p)
            except Exception as e:
                errors.append(e)
            if len(list(rd.glob("secret-env-*.started"))) > 1:   # the served copy queued itself again
                errors.append("requeued")
                for f in rd.glob("secret-env-*.started"):
                    f.with_suffix(".rc").write_text("99\n")   # ends every waiting copy
                return
            stop.wait(0.1)
    t = threading.Thread(target=daemon, daemon=True)
    t.start()
    try:
        code, out = _env_run(monkeypatch, capfd, *argv)
    finally:
        stop.set()
        t.join(10)
    assert not errors, errors
    return code, out, rd, p


def _files_text(*roots) -> str:
    out = []
    for root in roots:
        for f in pathlib.Path(root).rglob("*"):
            if f.is_file() and f.name != "secrets.json":
                out.append(f.read_bytes().decode(errors="replace"))
    return "\n".join(out)


def test_a_sandboxed_secret_env_runs_through_the_daemon_and_the_value_stays_out(env, monkeypatch, capfd):
    store = _jev_store(env, monkeypatch, capfd)
    long_path = os.pathsep.join(str(env["tmp"] / f"no-such-dir-{'x' * 60}-{i}") for i in range(5))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH"), long_path])))
    code, out, rd, p = _sandboxed_run(env, monkeypatch, capfd, "--secret", "jev:key=JK",   # a long PYTHONPATH,
                                      "--secret", "peer-set:beta=PB", "--", sys.executable, "-c",  # as push checks
                                      _PRINT_JEV.replace("sys.exit(5)", "print('pb=' + os.environ['PB']); sys.exit(5)"))
    values = [_JEV_KEY, *store["peer-set"].values(), _CUSTOM]
    assert code == 5, out                                              # the command's exit code
    assert "jk=[secret:jev]" in out and "err=[secret:jev]" in out and "pb=[secret:peer-set]" in out, out
    assert f"len {len(_JEV_KEY)}" in out and "the daemon runs the command" in out
    assert not any(v in out for v in values)                           # not in the returned output
    seen = _files_text(p.state, p.root / "tt-project")                 # run folder, queue, records, logs
    assert not any(v in seen for v in values)
    rows = [json.loads(x) for x in (p.state / "secret-uses.jsonl").read_text().splitlines()]
    assert rows == [{"at": rows[0]["at"], "task": "77", "run": "9001", "secrets": ["jev:key", "peer-set:beta"]}]
    jobs = json.loads((rd / "detached.json").read_text())
    assert [j["status"] for j in jobs] == ["queued"]                # the job list shows what the worker ran
    assert jobs[0]["command"].startswith("ttp secret env --secret=jev:key=JK --secret=peer-set:beta=PB -- ")
    assert not list((p.state / "detach-queue").glob("*.json"))      # the request was consumed


def test_a_sandboxed_secret_env_checks_names_before_queueing_and_passes_store_errors_back(env, monkeypatch, capfd):
    _jev_store(env, monkeypatch, capfd)
    code, out, rd, p = _sandboxed_run(env, monkeypatch, capfd, "--secret", "jev=X", "--", "true")
    assert code != 0 and "secret jev is built in" in out and not (rd / "detached.json").exists(), out
    code, out, rd, p = _sandboxed_run(env, monkeypatch, capfd, "--secret", "jev:nope=X", "--", "true", p=p, run="2")
    assert code != 0 and "secret jev has no key nope" in out and _JEV_KEY not in out, out
    code, out, rd, p = _sandboxed_run(env, monkeypatch, capfd, "tt-cloud", "--", sys.executable, "-c",
                                      "import os; print('k=' + os.environ['OPENAI_API_KEY'])", p=p, run="3")
    assert code == 0 and f"k={_MASK}" in out and _EP_KEY not in out, out


def test_secret_env_outside_a_sandbox_runs_the_command_itself(env, monkeypatch, capfd):
    from ttp import detach
    _jev_store(env, monkeypatch, capfd)
    p = make(env)
    rd = p.runs / "9002"
    rd.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    monkeypatch.setattr(detach, "enqueue", lambda *a, **k: pytest.fail("queued outside a sandbox"))
    code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", sys.executable, "-c", _PRINT_JEV)
    assert code == 5 and "jk=[secret:jev]" in out and "daemon" not in out and not (rd / "detached.json").exists()
    assert not (p.state / "secret-uses.jsonl").exists()


def _queued_route(env, monkeypatch, capfd, run):
    """A sandboxed worker's run folder with no daemon running: (project, run folder)."""
    from ttp import secretroute
    _jev_store(env, monkeypatch, capfd)
    p = make(env)
    rd = p.runs / run
    rd.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    monkeypatch.delenv(secretroute.SERVED_ENV, raising=False)
    monkeypatch.setattr(secretroute, "POLL_S", 0.01)
    return p, rd


def _never_ran(p, rd):
    """No request left, and a daemon pass that comes later starts nothing."""
    from ttp import detach
    assert not list((p.state / "detach-queue").glob("*.json"))
    detach.tend(p)
    assert not list(rd.glob("secret-env-*.started")) and not list(rd.glob("secret-env-*.rc"))
    assert [f.read_text() for f in rd.glob("secret-env-*.lost")] and not list(rd.glob("secret-env-*.queued"))


def test_a_sandboxed_secret_env_gives_up_when_the_daemon_never_starts_it(env, monkeypatch, capfd):
    from ttp import secretroute
    p, rd = _queued_route(env, monkeypatch, capfd, "9003")
    monkeypatch.setattr(secretroute, "START_WAIT_S", 0.2)
    code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", "true")
    assert code == 1 and "the daemon did not start the command" in out and "nothing ran" in out, out
    _never_ran(p, rd)                                                 # withdrawn: no late start
    assert "never ran" in next(rd.glob("secret-env-*.lost")).read_text()
    # a retry runs it exactly once
    marker = env["tmp"] / "ran.txt"
    code, out, rd2, _ = _sandboxed_run(env, monkeypatch, capfd, "--secret", "jev:key=JK", "--", sys.executable,
                                       "-c", f"open({str(marker)!r}, 'a').write('x')", p=p, run="9004")
    from ttp import detach
    detach.tend(p)
    assert code == 0 and marker.read_text() == "x", out


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_a_stopped_sandboxed_secret_env_withdraws_its_queued_request(env, monkeypatch, capfd, sig):
    import signal
    import threading
    from ttp import secretroute
    p, rd = _queued_route(env, monkeypatch, capfd, "9005")
    monkeypatch.setattr(secretroute, "START_WAIT_S", 30)
    queue = p.state / "detach-queue"
    # A fresh `ttp` process's SIGINT handler, whatever an earlier test in this process left installed
    old_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    queued, give_up = threading.Event(), threading.Event()

    def stop():
        # Only once the request is queued (with SIGINT and SIGTERM held, so it waits for the handler):
        # a signal sent earlier would reach the caller before it set up its own handling.
        while not give_up.is_set() and not list(queue.glob("*.json")):
            give_up.wait(0.01)
        if not give_up.is_set():
            queued.set()
            signal.pthread_kill(threading.main_thread().ident, getattr(signal, sig))   # as a single-threaded caller sees it
    t = threading.Thread(target=stop, daemon=True)
    t.start()
    try:
        code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", "true")
    finally:
        give_up.set()
        t.join(10)
        signal.signal(signal.SIGINT, old_int)
    assert queued.is_set(), f"the request was never queued: {out}"
    assert code == 128 + getattr(signal, sig) and f"stopped by {sig}" in out and "nothing ran" in out, out
    _never_ran(p, rd)
    assert signal.getsignal(signal.SIGTERM) is not secretroute._raise_stopped   # the handler is restored


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_a_stop_a_finalizer_swallowed_still_withdraws_the_queued_request(env, monkeypatch, capfd, sig):
    """A stop handled while a finalizer runs (a __del__ the garbage collector called mid-wait) raises
    there, and Python drops that exception: the wait still ends on it at once, not at its deadline."""
    import signal
    from ttp import detach, secretroute
    p, rd = _queued_route(env, monkeypatch, capfd, "9007")
    monkeypatch.setattr(secretroute, "START_WAIT_S", 30)
    old_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    real_state, fired = detach.state, []

    class Finalized:
        def __del__(self):
            fired.append(sig)
            signal.raise_signal(getattr(signal, sig))   # its exception ends here, ignored

    def state(rc):
        if not fired:
            Finalized()
        return real_state(rc)
    monkeypatch.setattr(detach, "state", state)
    try:
        code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", "true")
    finally:
        signal.signal(signal.SIGINT, old_int)
    assert fired == [sig], out
    assert code == 128 + getattr(signal, sig) and f"stopped by {sig}" in out and "nothing ran" in out, out
    _never_ran(p, rd)
    assert secretroute._raise_stopped not in (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))


def test_withdrawing_a_request_never_races_the_daemon_into_a_late_start(env, monkeypatch, capfd):
    """The daemon read the request, then the caller withdrew it before the daemon took the job's lock:
    the daemon never starts it. Once the daemon holds the lock, a withdrawal reports it started."""
    from ttp import detach, locks
    p, rd = _queued_route(env, monkeypatch, capfd, "9006")
    rc, log = rd / "secret-env-race.rc", rd / "secret-env-race.log"
    detach.enqueue(p.state, rc, log, ["true"], str(rd), {})
    real_take, said = locks.try_take, []

    def take_after_withdrawal(paths, *a, **k):
        said.append(detach.withdraw_unstarted(rc))
        return real_take(paths, *a, **k)
    monkeypatch.setattr(locks, "try_take", take_after_withdrawal)
    detach.tend(p)
    assert said == [True] and not rc.exists() and not detach.started(rc).exists()
    assert detach.state(rc)[0] == "lost"

    rc2 = rd / "secret-env-race2.rc"
    detach.enqueue(p.state, rc2, rd / "secret-env-race2.log", ["true"], str(rd), {})
    held = real_take([locks.job_lock(rc2)], "the daemon claiming it")
    try:
        assert detach.withdraw_unstarted(rc2) is False and detach.pending(rc2)   # claimed: left to start
    finally:
        held.close()


@pytest.mark.parametrize("damage", ["mode0", "directory", "non-utf8"])
def test_status_skips_an_unreadable_secret_store_and_secret_users_still_fail(env, monkeypatch, capsys, damage):
    """In a worker sandbox the secrets store cannot be read: `ttp status` and `ttp status --json` still
    report and the JSON marks the store unreadable; a caller that needs a secret still fails, clearly."""
    p = make(env)
    from ttp import cli, endpoint, project, redact
    endpoint.save("tt-cloud", _EP_URL, _EP_KEY, True, "HTTP 200")   # in the home the project is registered in
    store = project.secrets_path()
    monkeypatch.setattr(redact, "_cache", {"sig": None, "pairs": []})
    if damage == "mode0":
        store.chmod(0)
    elif damage == "non-utf8":
        good = store.read_bytes()
        store.write_bytes(b'{"x": "\xff\xfe"}')
    else:
        store.unlink()
        store.mkdir()
    try:
        if damage == "mode0" and os.access(store, os.R_OK):
            pytest.skip("running as root: mode 0 is still readable")
        cli.main(["status", p.name, "--json"])
        sec = json.loads(capsys.readouterr().out)["secrets"]
        assert sec["readable"] is False and sec["path"] == str(store) and sec["error"]
        cli.main(["status", p.name])
        assert "secrets store: unreadable" in capsys.readouterr().out
        with pytest.raises(project.SecretsUnreadable, match="cannot read the secrets store"):
            endpoint.credentials("tt-cloud")
        with pytest.raises(SystemExit):
            cli.main(["secret", "check", "tt-cloud"])
        assert "cannot read the secrets store" in capsys.readouterr().err
    finally:
        if store.is_dir():
            store.rmdir()
        elif damage == "non-utf8":
            store.write_bytes(good)
        else:
            store.chmod(0o600)
    cli.main(["status", p.name, "--json"])
    assert json.loads(capsys.readouterr().out)["secrets"] == {"readable": True, "path": str(store)}


@pytest.mark.parametrize("cut", ["marker", "request"])
def test_a_signal_inside_enqueue_never_leaves_a_request_withdraw_cannot_find(env, monkeypatch, capfd, cut):
    """A signal lands between enqueue's two writes (a caller with other threads is not masked): no
    request is left that no marker names, so withdraw_unstarted clears it and the daemon starts nothing."""
    from ttp import detach
    p, rd = _queued_route(env, monkeypatch, capfd, "9007")
    rc = rd / "secret-env-cut.rc"
    name = "durable_write" if cut == "marker" else "write_json"
    real = getattr(detach, name)

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(detach, name, interrupted)
    with pytest.raises(KeyboardInterrupt):
        detach.enqueue(p.state, rc, rd / "secret-env-cut.log", ["true"], str(rd), {})
    monkeypatch.setattr(detach, name, real)
    assert detach.withdraw_unstarted(rc) is True and not detach.pending(rc)
    assert not list((p.state / "detach-queue").glob("*.json"))
    detach.tend(p)
    assert not rc.exists() and not detach.started(rc).exists()


def test_a_secret_env_request_past_its_own_start_deadline_is_lost_never_started(env, monkeypatch, capfd):
    """A caller killed by SIGKILL withdraws nothing: its request carries its own start deadline, and the
    daemon records it lost past that, well before QUEUED_S. Other queued jobs keep QUEUED_S."""
    import time
    from ttp import detach, secretroute
    p, rd = _queued_route(env, monkeypatch, capfd, "9007")
    rc, plain = rd / "secret-env-killed.rc", rd / "plain.rc"
    q = detach.enqueue(p.state, rc, rd / "secret-env-killed.log", ["true"], str(rd), {},
                       start_within=secretroute.START_WAIT_S + secretroute.START_MARGIN_S)
    m = json.loads(q.read_text())
    assert m["start_by"] == m["queued"] + secretroute.START_WAIT_S + secretroute.START_MARGIN_S
    q2 = detach.enqueue(p.state, plain, rd / "plain.log", ["true"], str(rd), {})
    assert "start_by" not in json.loads(q2.read_text())
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + secretroute.START_WAIT_S + secretroute.START_MARGIN_S + 1)
    held = []   # nothing may start: the plain job's start is stubbed out to stay synchronous
    monkeypatch.setattr(detach, "start", lambda rc_, *a, **k: held.append(Path(rc_)) or 0)
    monkeypatch.setattr(detach, "renice", lambda *a, **k: None)
    detach.tend(p)
    assert not rc.exists() and not detach.started(rc).exists() and rc not in held
    st, why = detach.state(rc)
    assert st == "lost" and "start deadline" in why and "never ran" in why, why
    assert not q.exists() and not detach.queued(rc).exists()
    assert held == [plain]   # well inside QUEUED_S: the job with no deadline of its own still starts


@pytest.mark.parametrize("damaged", ["soon", [1], {"at": 1}])
def test_a_damaged_start_deadline_never_starts_the_request(env, monkeypatch, capfd, damaged):
    from ttp import detach
    p, rd = _queued_route(env, monkeypatch, capfd, "9008")
    rc = rd / "secret-env-damaged.rc"
    q = detach.enqueue(p.state, rc, rd / "secret-env-damaged.log", ["true"], str(rd), {}, start_within=60)
    m = json.loads(q.read_text())
    m["start_by"] = damaged
    q.write_text(json.dumps(m))
    monkeypatch.setattr(detach, "start", lambda *a, **k: pytest.fail("started"))
    monkeypatch.setattr(detach, "renice", lambda *a, **k: None)
    detach.tend(p)
    assert detach.state(rc)[0] == "lost"


def test_a_second_sigint_during_the_withdrawal_still_prints_the_report(env, monkeypatch, capfd):
    """Ctrl-C twice: the second one, held while the request is withdrawn, never cuts the report short."""
    import signal
    import threading
    from ttp import detach, secretroute
    p, rd = _queued_route(env, monkeypatch, capfd, "9009")
    monkeypatch.setattr(secretroute, "START_WAIT_S", 30)
    queue = p.state / "detach-queue"
    real_withdraw = detach.withdraw_unstarted

    def withdraw_with_a_second_sigint(*a, **k):
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)   # held by _held_signals until the block ends
        return real_withdraw(*a, **k)
    monkeypatch.setattr(detach, "withdraw_unstarted", withdraw_with_a_second_sigint)

    # A fresh `ttp` process's SIGINT handler, whatever an earlier test in this process left installed
    old_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    queued, give_up = threading.Event(), threading.Event()

    def stop():
        # Only once the request is queued (with SIGINT and SIGTERM held, so it waits for the handler):
        # a signal sent earlier would reach the caller before it set up its own handling.
        while not give_up.is_set() and not list(queue.glob("*.json")):
            give_up.wait(0.01)
        if not give_up.is_set():
            queued.set()
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)   # as a single-threaded caller sees it
    t = threading.Thread(target=stop, daemon=True)
    t.start()
    try:
        code, out = _env_run(monkeypatch, capfd, "--secret", "jev:key=JK", "--", "true")
        int_after = signal.getsignal(signal.SIGINT)
    finally:
        give_up.set()
        t.join(10)
        signal.signal(signal.SIGINT, old_int)
    assert queued.is_set(), f"the request was never queued: {out}"
    assert code == 128 + signal.SIGINT and "stopped by SIGINT" in out and "nothing ran" in out, out
    _never_ran(p, rd)
    assert int_after is signal.default_int_handler   # both handlers are restored
    assert signal.getsignal(signal.SIGTERM) is not secretroute._raise_stopped


def test_doctor_masks_the_web_app_token_and_points_to_ttp_web(env, monkeypatch, capsys):
    """Workers run doctor: its output stays in their logs and transcripts, so the web app's access token
    shows only a 4-character prefix there; `ttp web` keeps the full link for the user."""
    from ttp import cli, web
    p = make(env)
    p.db.set_kv("web", {"port": 18700})
    tok = web.token(p)
    monkeypatch.setattr(cli, "need", lambda *a: p)
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    out = capsys.readouterr().out
    assert tok not in out and "#token=" not in out, out
    line = next(l for l in out.splitlines() if l.startswith("web app:"))
    assert line == f"web app: http://127.0.0.1:18700/ (token {tok[:4]}…; the full link: `ttp web demo`)", line
    from ttp import weblink
    assert weblink.link(p) == f"http://127.0.0.1:18700/#token={tok}"   # the user-facing link is unchanged
    p.db.set_kv("web", {})
    monkeypatch.setattr(p, "config", lambda: {"web": {}})
    assert cli.web_line(p) == "web app: not running yet (check `ttp status`)"


@pytest.mark.parametrize("in_run", [True, False])
def test_ttp_web_and_connect_mask_the_web_app_token_only_in_a_worker_run(env, monkeypatch, capsys, tmp_path, in_run):
    """A worker run (TTP_RUN_DIR set) keeps its output in its transcript, so `ttp web` (local, remote,
    --tunnel, --tunnel --keep) and `ttp connect` print the address and the token's first 4 characters
    there; the user's own shell still gets the full link."""
    from ttp import cli, consent, timefmt, tunnel, web, weblink
    if in_run:
        monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    p = make(env)
    p.db.set_kv("web", {"port": 18700})
    tok = web.token(p)
    monkeypatch.setattr(weblink, "verify", lambda url, name: ("", ""))
    monkeypatch.setattr(cli, "need", lambda *a: p)
    monkeypatch.setattr(cli, "status_text", lambda p: "status")
    monkeypatch.setattr(timefmt, "in_ssh_login", lambda: True)
    monkeypatch.setattr(cli, "resolve", lambda name: (p, None))
    cli.main(["web", "demo"])
    cli.main(["connect", "demo"])
    # a remote project: --tunnel, its not-yet-open forward, and --tunnel --keep (weblink.remote)
    rtok = "abcdef0123456789"
    monkeypatch.setattr(cli, "resolve", lambda name: (None, {"host": "host-a", "dir": "/srv/p"}))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, f"web app: http://127.0.0.1:18701/#token={rtok}\n", ""))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(tunnel, "installed", lambda name: None)
    monkeypatch.setattr(tunnel, "keep", lambda *a, **k: (18801, "kept"))
    monkeypatch.setattr(consent, "gate", lambda *a: "")
    for argv in (["web", "demo", "--tunnel"], ["web", "demo"], ["web", "demo", "--tunnel", "--keep"]):
        cli.main(argv)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("web app")]
    assert len(lines) == 5, out
    if in_run:
        assert tok not in out and rtok not in out and "#token=" not in out, out
        note = "the full link is shown only outside worker runs"
        assert lines[0] == lines[1] == f"web app: http://127.0.0.1:18700/ (token {tok[:4]}…; {note})", lines
        assert all(f"(token {rtok[:4]}…; {note})" in l for l in lines[2:]), lines
    else:
        assert lines[0] == lines[1] == f"web app: http://127.0.0.1:18700/#token={tok}", lines
        assert all(l.endswith(f"/#token={rtok}") for l in lines[2:]), lines


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_doctor_reports_an_unreadable_secret_store_in_one_line_and_runs_its_later_checks(env, monkeypatch, capsys):
    from ttp import cli, project
    p = make(env)
    p.set_config("runner.host_commands", ["sudo -n systemctl restart a.service"])   # a later doctor line
    monkeypatch.setattr(cli, "need", lambda *a: p)
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "secrets-home")
    project.HOME_DIR.mkdir()
    store = project.secrets_path()
    store.write_text(json.dumps({"jev": {"key": _EP_KEY}}))
    store.chmod(0)
    try:
        cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    finally:
        store.chmod(0o600)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if str(store) in l]   # the Slack line says "unknown here" without it
    assert len(lines) == 1 and lines[0].startswith(f"jev: key unknown, cannot read the secrets store at {store}: "), out
    assert _EP_KEY not in out, out
    assert "host-run: runner.host_commands entry 1" in out, "doctor runs on to the host-run checks"
    assert "process sandbox for checks:" in out, "and on to the checks after them"


# Private-key blocks never reach a run's files, records, outbox or views (redact.KEY_RE). Every key here is
# assembled at test time from random bytes: the repository holds no key-looking text.
def _key_block(kind="OPENSSH", headers=""):
    """(block, body lines) of a random PEM-style private key of `kind` ("" for a plain one)."""
    import base64
    body = base64.b64encode(os.urandom(400)).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    label = (kind + " " if kind else "") + "PRIVATE" + " KEY"
    dash = "-" * 5
    block = f"{dash}BEGIN {label}{dash}\n{headers}" + "\n".join(lines) + f"\n{dash}END {label}{dash}\n"
    return block, lines


def _no_key(text, lines):
    return not any(ln in text for ln in lines) and not any(ln[:24] in text for ln in lines)


@pytest.mark.parametrize("kind,headers", [("OPENSSH", ""), ("RSA", ""), ("EC", ""), ("DSA", ""), ("", ""),
                                          ("ENCRYPTED", ""), ("RSA", "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B\n\n"),
                                          ("PGP", "Version: Test 1\n\n")])
def test_private_key_blocks_are_masked_raw_and_json_escaped(kind, headers):
    from ttp import redact
    block, lines = _key_block(kind, headers)
    if kind == "PGP":
        block = block.replace("PRIVATE KEY-", "PRIVATE KEY BLOCK-")
    out = redact.scrub("cat k:\n" + block + "done")
    assert out == f"cat k:\n{redact.KEY_MASK}\ndone"
    line = json.dumps({"type": "tool_result", "content": "before " + block + " after"})
    out = redact.scrub(line)
    assert _no_key(out, lines) and json.loads(out)["content"] == f"before {redact.KEY_MASK}\n after"
    twice = json.dumps(json.dumps({"t": block}))   # JSON in a JSON string
    out = redact.scrub(twice)
    assert _no_key(out, lines) and redact.KEY_MASK in json.loads(json.loads(out))["t"]


def test_a_cut_off_key_block_is_masked_to_the_end_of_its_string_and_two_in_a_line_each():
    from ttp import redact
    block, lines = _key_block("RSA")
    cut = json.dumps({"t": "x " + block[:200]}) + "\n" + json.dumps({"n": 1}) + "\n"
    out = redact.scrub(cut)
    assert _no_key(out, lines) and [json.loads(x) for x in out.splitlines()] == [{"t": "x " + redact.KEY_MASK}, {"n": 1}]
    assert redact.scrub("x " + block[:200]) == "x " + redact.KEY_MASK
    other, lines2 = _key_block("EC")
    one = json.dumps({"t": block.rstrip() + " and " + other.rstrip()})
    out = redact.scrub(one)
    assert _no_key(out, lines + lines2) and json.loads(out)["t"] == f"{redact.KEY_MASK} and {redact.KEY_MASK}"


def test_lone_marker_lines_and_a_block_trimmed_at_its_start_are_masked(env, monkeypatch):
    """Regression (user msg #1904): a transcript that held only the BEGIN and END lines of a key, with no
    body between them, and a block whose start output trimming cut off."""
    from ttp import project, redact
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "empty-home")
    block, lines = _key_block()
    begin, end = block.splitlines()[0], block.splitlines()[-1]
    for text in (begin + "\n" + end + "\n", begin, end, "cat k\n" + end + "\nok"):
        out = redact.scrub(text)
        assert "PRIVATE" not in out and redact.KEY_MASK in out, text
    assert redact.scrub("cat k\n" + end + "\nok") == f"cat k\n{redact.KEY_MASK}\nok"
    trimmed = "kept prose\n" + "\n".join(lines[2:]) + "\n" + end + "\n"
    assert redact.scrub(trimmed) == f"kept prose\n{redact.KEY_MASK}\n"
    line = json.dumps({"type": "tool_result", "content": "...\n" + "\n".join(lines[3:]) + "\n" + end})
    out = redact.scrub(line)
    assert _no_key(out, lines) and json.loads(out)["content"] == "...\n" + redact.KEY_MASK
    run = env["tmp"] / "runs" / "12"
    run.mkdir(parents=True)
    (run / "output.jsonl").write_text(json.dumps({"tool_result": begin + "\n" + end + "\n"}) + "\n"
                                      + json.dumps({"tool_result": end}) + "\n")
    assert redact.scrub_run(run) == 2
    out = (run / "output.jsonl").read_text()
    assert "PRIVATE" not in out and [json.loads(x) for x in out.splitlines()] == [
        {"tool_result": redact.KEY_MASK + "\n"}, {"tool_result": redact.KEY_MASK}]


def test_public_keys_and_prose_about_private_keys_stay_as_they_are():
    from ttp import redact
    dash = "-" * 5
    for text in (f"{dash}BEGIN PUBLIC KEY{dash}\nMIIBIjANBgkq\n{dash}END PUBLIC KEY{dash}\n",
                 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI user@example",
                 "Never print a private key; the PRIVATE KEY header names its type.",
                 f"a {dash}BEGIN CERTIFICATE{dash} block"):
        assert redact.scrub(text) == text


def test_scrub_run_masks_private_keys_with_no_stored_secret_and_keeps_jsonl_valid(env, monkeypatch):
    from ttp import project, redact
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "empty-home")   # no secrets store at all
    block, lines = _key_block()
    run = env["tmp"] / "runs" / "11"
    run.mkdir(parents=True)
    sid = "0123abcd-4567-89ef-0123-456789abcdef"
    (run / "output.jsonl").write_text(json.dumps({"type": "system", "session_id": sid}) + "\n"
                                      + json.dumps({"tool_result": block}) + "\n"
                                      + json.dumps({"tool_result": block[:150]}) + "\n")
    (run / "stderr.log").write_text("warn\n" + block + "tail\n")
    cfg = env["tmp"] / "claude-own"
    (run / "run.json").write_text(json.dumps({"provider": "claude", "cwd": "/w", "env": {"CLAUDE_CONFIG_DIR": str(cfg)}}))
    session = cfg / "projects" / "-w" / f"{sid}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"content": block}) + "\n")
    assert redact.scrub_run(run) == 4
    for f in (run / "output.jsonl", run / "stderr.log", session):
        assert _no_key(f.read_text(), lines) and redact.KEY_MASK in f.read_text(), f
    assert len([json.loads(x) for x in (run / "output.jsonl").read_text().splitlines()]) == 3
    assert redact.scrub_run(run) == 0


def test_a_run_that_prints_a_private_key_keeps_it_out_of_every_copy(env, monkeypatch):
    """Regression (user msg #1901): a worker that prints a key file into its transcript leaves the key in no
    stored or shown copy: run files, hand-off, database rows, outbox, the web app's run and state views."""
    p = make(env)
    from ttp import outbox, project
    from ttp.daemon import Daemon
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "key-home")
    block, lines = _key_block()
    keyfile, tool = env["tmp"] / "k", env["tmp"] / "tool.jsonl"
    keyfile.write_text(block)
    tool.write_text(json.dumps({"type": "user", "tool_result": {"content": block}}) + "\n")
    monkeypatch.setenv("TTP_FAKE_SHELL", f"cat '{tool}'; cat '{keyfile}'; cat '{keyfile}' > \"$TTP_RUN_DIR/job.log\"; "
                                         f"cat '{keyfile}' >&2")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"status": "done", "summary": "the key file holds " + block}))
    tid = p.db.add_task("read the key file", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done")
    run = p.db.one("SELECT id, dir FROM runs WHERE task=?", (tid,))
    run_dir = pathlib.Path(run["dir"]) if run["dir"] else p.runs / str(run["id"])
    files = [f for f in run_dir.rglob("*") if f.is_file()]
    assert {"output.jsonl", "result.json", "job.log", "stderr.log"} <= {f.name for f in files}
    for f in files:
        assert _no_key(f.read_text(errors="replace"), lines), f.name
    out = (run_dir / "output.jsonl").read_text()
    assert _masked(out) and _masked((run_dir / "result.json").read_text())
    json.loads(out.splitlines()[0])   # the tool line stays valid JSON
    dump = json.dumps([p.db.q(f"SELECT * FROM {t}") for t in ("tasks", "events", "runs", "messages")])
    assert _no_key(dump, lines) and _masked(dump)
    outbox.add("peer", ["say", "the key: " + block], outbox.new_id(), None)
    assert _no_key(outbox.path("peer").read_text(), lines) and _masked(outbox.path("peer").read_text())
    if _sockets_refused():
        return
    from ttp import web
    port = _start_web(p)
    # A live run's progress.md (not masked yet): the block inside the 8000-character tail /api/run shows,
    # then one the tail cut lands inside, mid-line, which only masking before the cut catches.
    lead = "15:00 note: "
    cut = block.index(lines[3]) + 10
    for pad in (100, 8000 + cut - len(block) - 1):
        (run_dir / "progress.md").write_text(lead + block + "x" * pad + "\n")
        for path in (f"/api/run/{run['id']}", "/api/state"):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-TTP-Token": web.token(p)})
            for _ in range(50):
                try:
                    body = urllib.request.urlopen(req, timeout=5).read().decode()
                    break
                except urllib.error.URLError:
                    time.sleep(0.1)
            assert _no_key(body, lines) and not _key_piece(body, lines), (path, pad)
            view = json.loads(body)
            if path.startswith("/api/run"):
                assert "x" * 50 in view["progress"], "the view shows the progress tail"


def _key_piece(text, lines, n=20):
    """Whether any `n` characters in a row of the key's body show in `text` (a line cut mid-way)."""
    return any(ln[i:i + n] in text for ln in lines for i in range(0, len(ln) - n + 1))


def _masked(text):
    from ttp import redact
    return redact.KEY_MASK in text


def test_prose_after_a_lone_key_marker_line_survives_and_a_cut_block_is_still_masked(env):
    """Review #6952: a raw BEGIN line with no END after it masked everything up to the next quote, so a
    stored chat message or task spec that quoted the marker lost its prose for good (Db.x scrubs every
    string). In raw text a cut-off block ends at its last key line; in a JSON string, at the string's end."""
    from ttp import redact
    block, lines = _key_block("RSA", "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B\n\n")
    begin = block.splitlines()[0]
    prose = "This line, with text: (more) and 2.5 words.\n\nAnother paragraph. Done"
    for text in (begin + "\n" + prose, "see " + begin + " in the file.\n" + prose, begin + "\r\n" + prose,
                 begin + "\nNote: a header-like line\n" + prose):
        out = redact.scrub(text)
        assert out.endswith(prose) and "PRIVATE" not in out and redact.KEY_MASK in out, out
    cut = block.index(lines[2]) + 30   # cut mid-line, then prose or a trimming mark
    for tail in ("", "\n" + prose, "...\n" + prose, "\n\n" + prose):
        out = redact.scrub("x " + block[:cut] + tail)
        assert _no_key(out, lines) and not _key_piece(out, lines) and "DEK-Info" not in out, out
        assert out.endswith(tail.lstrip(".")) if tail else out == "x " + redact.KEY_MASK, out
    for sep in ("\t", "\u2192"):   # numbered lines (`cat -n`, a file viewer), cut before the END line
        numbered = "".join(f"{i + 1:6}{sep}{ln}\n" for i, ln in enumerate(block.splitlines()[:6]))
        out = redact.scrub(numbered + prose)
        assert out.endswith("\n" + prose) and not _key_piece(out, lines), out
    t0 = time.monotonic()   # space-only lines match one way: no backtracking blow-up before prose
    for filler in ("\n   ", "\n  7\t  ", "\nAAAA   \n   "):
        assert redact.scrub(begin + filler * 3000 + "\nx y").endswith("\nx y")
    assert time.monotonic() - t0 < 5
    squashed = begin + " " + " ".join(lines[:4]) + "\n" + prose   # one line, cut before its END
    out = redact.scrub(squashed)
    assert out == redact.KEY_MASK + "\n" + prose and not _key_piece(out, lines)
    js = json.dumps({"t": "x " + block[:cut] + " then prose, all one string"}) + "\n" + json.dumps({"n": 1})
    out = redact.scrub(js)   # JSON-escaped: to the end of its string, and the JSONL stays valid
    assert [json.loads(x) for x in out.splitlines()] == [{"t": "x " + redact.KEY_MASK}, {"n": 1}]
    p = make(env)
    tid = p.db.add_task("quote", "The file starts with " + begin + "\n" + prose, kind="work", origin="user")
    spec = p.db.task(tid)["spec"]
    assert spec == "The file starts with " + redact.KEY_MASK + "\n" + prose, spec


@pytest.mark.parametrize("name,mark", [("job", "job.started"), ("job", "job.queued"), ("checks", "checks.pid")])
def test_scrub_run_leaves_a_live_detached_jobs_log_whole_and_masks_it_once_the_job_ends(env, monkeypatch, name, mark):
    """Review #6952: scrub_run renamed a new file over a log a `ttp detach` or `ttp checks --detach` job still
    wrote, so the job's later lines went to the unlinked file and vanished from the log."""
    from ttp import detach, project, redact
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "empty-home")
    block, lines = _key_block()
    run = env["tmp"] / "runs" / "13"
    run.mkdir(parents=True)
    (run / mark).write_text("1\n")
    (run / "stderr.log").write_text(block)
    log = run / f"{name}.log"
    with open(log, "w") as w:   # as the job wrapper's `>"$log"`: one open file, its own offset
        w.write("line 0\n" + block)
        w.flush()
        masked = redact.scrub_run(run)
        for i in range(1, 50):
            w.write(f"line {i}\n")
            w.flush()
    text = log.read_text()
    assert all(f"line {i}\n" in text for i in range(50)), text[-200:]
    assert masked == 1   # stderr.log only
    assert _masked((run / "stderr.log").read_text())
    rc = run / f"{name}.rc"
    if name == "job":   # the daemon's sweep closes the job's record once it ended: its log is masked then
        (run / "detached.json").write_text(json.dumps([{"rc": str(rc), "status": "running"}]))
        rc.write_text("0\n")
        assert detach.close(rc)["status"] == "ended"
        logs = [log]
    else:   # review #7153: the detached checks themselves mask checks.log and checks.out as they end
        import signal
        import types
        from ttp import cli
        (run / cli.CHECKS_OUT).write_text("out 0\n" + block)
        monkeypatch.setitem(cli._CHECK_STOP, "stopped", None)
        handlers = {sig: signal.getsignal(sig) for sig in cli._CHECK_STOP_SIGNALS}
        monkeypatch.setattr(cli, "_checks_released", lambda go, d: True)
        monkeypatch.setattr(cli, "cmd_checks", lambda _a: [(run / n).open("a").write(f"{n} end\n")
                                                           for n in ("checks.log", cli.CHECKS_OUT)])
        monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"go\n")))
        monkeypatch.setenv("TTP_CHECK_OUTPUT_DIR", str(run))
        monkeypatch.delenv("TTP_CHECKS_TMP_FD", raising=False)
        try:
            assert cli._checks_child(types.SimpleNamespace(rc=str(rc))) == 0
        finally:
            for sig, h in handlers.items():
                signal.signal(sig, h)
        assert rc.read_text() == "0\n"
        logs = [log, run / cli.CHECKS_OUT]
    for f in logs:
        text = f.read_text()
        assert _no_key(text, lines) and _masked(text), f
    assert "line 49\n" in log.read_text()
    if name == "checks":
        assert "checks.log end\n" in log.read_text() and (run / "checks.out").read_text().startswith("out 0\n")


def test_close_masks_a_jobs_log_once_a_leftover_process_frees_its_lock(env, monkeypatch):
    """Review #7153: a job closed while a leftover child still held its job lock kept its log unmasked for good."""
    import fcntl
    from ttp import detach, locks, project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "empty-home")
    block, lines = _key_block()
    p = make(env)
    run = p.runs / "21"
    run.mkdir(parents=True)
    rc, log = run / "job.rc", run / "job.log"
    (run / "job.started").write_text("1\n")
    log.write_text("line 0\n" + block + "line 1\n")
    (run / "detached.json").write_text(json.dumps([{"rc": str(rc), "status": "running"}]))
    rc.write_text("0\n")
    with open(locks.job_lock(rc), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert detach.close_records(p) == 1
        assert block in log.read_text()   # still written to: left whole
        assert detach.close(rc).get("unmasked") is True
        fcntl.flock(held, fcntl.LOCK_UN)
    (p.state / "detach-closed.at").unlink()
    assert detach.close_records(p) == 0   # closed before: counted once, masked now
    text = log.read_text()
    assert _no_key(text, lines) and _masked(text) and "line 1\n" in text
    assert "unmasked" not in detach.close(rc)


class _TrackedRead:
    """An open file that records how many bytes each read asks for (a whole-file read: the file's size)."""

    def __init__(self, fh, size, reads):
        self._fh, self._size, self._reads = fh, size, reads

    def read(self, n=-1):
        self._reads.append(self._size if n is None or n < 0 else n)
        return self._fh.read(n)

    def readlines(self, *a):
        self._reads.append(self._size)
        return self._fh.readlines(*a)

    def __iter__(self):
        self._reads.append(self._size)
        return iter(self._fh)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def __getattr__(self, name):
        return getattr(self._fh, name)


def test_ending_a_run_with_a_large_output_file_reads_it_capped_and_a_detached_job_masks_its_key(env, monkeypatch):
    """User msg #1918: the daemon's run-end masking (reap_runs, finish_run) reads each run file within a
    fixed cap and each run within a time limit; a file over the cap is masked by a detached job, every
    line kept, and so is a big detached-job log the sweep closes (detach.close)."""
    import builtins
    from ttp import daemon as dm
    from ttp import detach, project, redact
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "empty-home")
    p = make(env)
    block, lines = _key_block()
    run_dir = env["tmp"] / "big-run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"actions": [], "_cost": 0.01}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    filler = "".join(f"step {i}: " + "y" * 90 + "\n" for i in range(15000))   # about 1.5 MiB
    big, bg = run_dir / "job.log", run_dir / "bg.log"
    for f in (big, bg):
        f.write_text(filler + block + filler + "last line\n")
    before = big.read_text()
    (run_dir / "bg.started").write_text("1\n")   # a detached job, still writing bg.log at the run's end
    rid = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
                 ("coordinator", "fake", time.time(), "running", str(run_dir), "x", "{}"))
    cap = 1 << 20
    monkeypatch.setattr(redact, "STEP_FILE_BYTES", cap, raising=False)
    reads: list[int] = []
    watched = {str(big), str(bg)}
    real_open, real_rb, real_rt = builtins.open, pathlib.Path.read_bytes, pathlib.Path.read_text

    def tracked_open(file, *a, **k):
        fh = real_open(file, *a, **k)
        return _TrackedRead(fh, os.path.getsize(file), reads) if str(file) in watched else fh

    def whole(real):
        def read(self, *a, **k):
            if str(self) in watched:
                reads.append(self.stat().st_size)
            return real(self, *a, **k)
        return read

    def in_step(fn):
        with monkeypatch.context() as m:
            m.setattr(builtins, "open", tracked_open)
            m.setattr(pathlib.Path, "read_bytes", whole(real_rb))
            m.setattr(pathlib.Path, "read_text", whole(real_rt))
            return fn()

    d = dm.Daemon(p.base)
    t0 = time.monotonic()
    in_step(lambda: d._run_step(d.reap_runs))
    assert time.monotonic() - t0 < dm.STEP_BUDGET_S
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] != "running"
    assert max(reads, default=0) <= cap + 1, f"the daemon step read {max(reads)} bytes of a big run file"

    def masked(f):
        for _ in range(300):
            if redact.KEY_MASK in f.read_text():
                return True
            time.sleep(0.1)
        return False
    assert masked(big), (run_dir / "scrub.log").read_text() if (run_dir / "scrub.log").exists() else "no scrub.log"
    assert big.read_text() == before.replace(block.rstrip("\n"), redact.KEY_MASK), "a line was lost"
    assert "detached job" in (p.logs / "daemon.log").read_text()
    assert redact.KEY_MASK not in bg.read_text()   # left whole while its job ran
    rc = run_dir / "bg.rc"
    (run_dir / "detached.json").write_text(json.dumps([{"rc": str(rc), "status": "running"}]))
    rc.write_text("0\n")
    reads.clear()
    assert in_step(lambda: detach.close(rc))["status"] == "ended"
    assert max(reads, default=0) <= cap + 1, f"the sweep read {max(reads)} bytes of a big job log"
    assert masked(bg) and bg.read_text() == before.replace(block.rstrip("\n"), redact.KEY_MASK)
    assert _no_key((run_dir / "scrub.log").read_text(), lines)
    left: list = []   # past its time limit, the whole folder is left for the detached job
    monkeypatch.setattr(redact, "STEP_SCRUB_S", 0.0, raising=False)
    (run_dir / "late.log").write_text(block)
    assert redact.scrub_run(run_dir, step=True, left=left) == 0 and left == [run_dir]
    assert redact.main([str(run_dir)]) == 0 and _no_key((run_dir / "late.log").read_text(), lines)


# The run-time store (secretstore): no long-lived plaintext keys on shared or build hosts.
_STORE_KEY = "fake-store-value-0123456789abcdef"


def _runtime_store_home(env, monkeypatch):
    from ttp import secretstore
    project = _endpoint_home(env, monkeypatch)
    secretstore._mem.update(key=None, data=None)
    secretstore._env.clear()
    return project, secretstore


def _fetch_script(env, body: str):
    f = env["tmp"] / "fetch-keys"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return f


def test_a_command_store_is_read_at_run_time_cached_in_memory_and_never_written(env, monkeypatch, capsys):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    calls = env["tmp"] / "calls"
    fetch = _fetch_script(env, f'echo x >> "{calls}"\necho \'{{"jev": {{"key": "{_STORE_KEY}"}}}}\'\n')
    assert _custom(monkeypatch, capsys, "store", "command", "--", str(fetch))[0] == 0
    assert _STORE_KEY not in secretstore.config_path().read_text()
    assert project.load_secrets()["jev"]["key"] == _STORE_KEY
    assert project.load_secrets()["jev"]["key"] == _STORE_KEY
    assert calls.read_text().count("x") == 1, "a second read within the cache period reuses memory"
    from ttp import redact
    assert redact.scrub(f"value {_STORE_KEY}") == "value [secret:jev]"
    code, out = _custom(monkeypatch, capsys, "set", "peer-key", stdin=_CUSTOM)
    assert code and "not saved" in out and _CUSTOM not in out
    assert not project.secrets_path().exists(), "a run-time store never leaves a plaintext file"


def test_a_failing_fetch_command_names_no_output_and_status_still_runs(env, monkeypatch):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    fetch = _fetch_script(env, f'echo "{_STORE_KEY}" >&2\nexit 7\n')
    secretstore.set_config("command", [str(fetch)])
    with pytest.raises(project.SecretsUnreadable) as e:
        project.load_secrets()
    assert "exited 7" in str(e.value) and _STORE_KEY not in str(e.value)
    assert project.secrets_state()["readable"] is False
    _fetch_script(env, "echo not-json\n")
    secretstore._mem.update(key=None)
    with pytest.raises(project.SecretsUnreadable):
        project.load_secrets()


def test_a_failed_fetch_is_not_retried_for_fail_s_and_keeps_no_value(env, monkeypatch):
    """A burst of reads (masking each line, a hook per tool call) runs a failing fetch once per FAIL_S, not
    per call; the remembered failure is its error text only; once due, the next read fetches again."""
    project, secretstore = _runtime_store_home(env, monkeypatch)
    calls = env["tmp"] / "calls"
    fetch = _fetch_script(env, f'echo x >> "{calls}"\necho "{_STORE_KEY}" >&2\nexit 7\n')
    secretstore.set_config("command", [str(fetch)])
    for _ in range(5):
        with pytest.raises(project.SecretsUnreadable) as e:
            project.load_secrets()
        assert "exited 7" in str(e.value) and _STORE_KEY not in str(e.value)
    assert calls.read_text().count("x") == 1, "a failed fetch ran again within FAIL_S"
    assert _STORE_KEY not in json.dumps(secretstore._mem) and not project.secrets_path().exists()
    _fetch_script(env, f'echo x >> "{calls}"\necho \'{{"jev": {{"key": "{_STORE_KEY}"}}}}\'\n')
    secretstore._mem["at"] -= secretstore.FAIL_S + 1   # the failure is due for a retry
    assert project.load_secrets()["jev"]["key"] == _STORE_KEY
    assert calls.read_text().count("x") == 2 and secretstore._mem["fail"] == ""
    assert not project.secrets_path().exists(), "a fetched store is never written in plaintext"


def test_a_run_the_daemon_cannot_mask_raises_one_secrets_unmasked_event_per_cause(env, monkeypatch, tmp_path):
    """Deliverable 7: a run the runner flagged unmasked, with the store still unreadable in the daemon, is
    logged and raised as one high secrets_unmasked event naming the cause (never a value); a second run
    failing for the same cause adds none; a run the daemon can mask raises none."""
    from ttp.daemon import Daemon
    p = make(env)
    project, secretstore = _runtime_store_home(env, monkeypatch)
    d = Daemon(p.base)   # started while the store read (empty); it fails from here on
    secretstore.set_config("command", [str(_fetch_script(env, f'echo "{_STORE_KEY}" >&2\nexit 7\n'))])
    redact = _fresh_masking(monkeypatch)

    def run(n, unmasked):
        run_dir = tmp_path / f"run{n}"
        run_dir.mkdir()
        (run_dir / "output.jsonl").write_text(json.dumps({"type": "result", "result": "done", "usage": {},
                                                          "subtype": "success", "total_cost_usd": 0}) + "\n")
        p.db.x("INSERT INTO runs(role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
               ("worker", "fake", "", time.time(), "running", str(run_dir), "x"))
        info = {"rc": 0, "ended": time.time(), **({"unmasked": unmasked} if unmasked else {})}
        (run_dir / "exit.json").write_text(json.dumps(info))
        d.reap_runs()

    def events():
        return p.db.q("SELECT severity, text FROM events WHERE kind='secrets_unmasked'")

    run(1, "secret fetch command fetch-keys exited 7")
    got = events()
    assert len(got) == 1 and got[0]["severity"] == "high", got
    assert "could not mask stored secrets" in got[0]["text"] and "exited 7" in got[0]["text"]
    assert _STORE_KEY not in got[0]["text"]
    secretstore._mem["at"] -= secretstore.FAIL_S + 1
    redact._cache["sig"] = None
    run(2, "secret fetch command fetch-keys exited 7")
    assert len(events()) == 1, "one event per cause, not per run"
    _fetch_script(env, f'echo \'{{"jev": {{"key": "{_STORE_KEY}"}}}}\'\n')
    secretstore._mem.update(key=None, data=None)
    redact._cache["sig"] = None
    run(3, "secret fetch command fetch-keys exited 7")
    assert len(events()) == 1, "a run the daemon could mask raises no event"


def test_an_env_store_is_read_once_and_removed_from_the_environment(env, monkeypatch):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    secretstore.set_config("env")
    monkeypatch.setenv(secretstore.ENV_VAR, json.dumps({"slack": {"bot_token": _STORE_KEY}}))
    assert project.load_secrets()["slack"]["bot_token"] == _STORE_KEY
    assert secretstore.ENV_VAR not in os.environ, "no child process inherits the keys"
    secretstore._mem.update(key=None)
    assert project.load_secrets()["slack"]["bot_token"] == _STORE_KEY


def test_a_keychain_store_round_trips_and_the_value_is_never_on_a_command_line(env, monkeypatch):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    bindir, kc, argv_log = env["tmp"] / "fakebin", env["tmp"] / "kc.hex", env["tmp"] / "argv.log"
    bindir.mkdir()
    # a stand-in for macOS `security`: -i reads `add-generic-password ... -X <hex>` on stdin
    (bindir / "security").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{argv_log}"\n'
        'if [ "$1" = -i ]; then read -r line; echo "$line" | sed "s/.*-X //" > ' f'"{kc}"; exit 0; fi\n'
        f'[ -f "{kc}" ] || exit 44\n'
        f'python3 -c "import sys; print(bytes.fromhex(open(sys.argv[1]).read().strip()).decode())" "{kc}"\n')
    (bindir / "security").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    secretstore.set_config("keychain")
    assert project.load_secrets() == {}
    project.save_secret("peer-key", _STORE_KEY)
    assert project.load_secrets() == {"peer-key": _STORE_KEY}
    assert _STORE_KEY not in argv_log.read_text() and _STORE_KEY.encode().hex() not in argv_log.read_text()
    assert not project.secrets_path().exists()


def test_a_shared_host_never_gets_a_plaintext_key_file(env, monkeypatch, capsys):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, "store", "file", "--shared")
    assert code and "no plaintext" in out
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    secretstore.config_path().write_text(json.dumps({"backend": "file", "shared": True}))   # written by hand
    code, out = _custom(monkeypatch, capsys, "set", "peer-key", stdin=_CUSTOM)
    assert code and "marked shared" in out and not project.secrets_path().exists()
    for bad in (["store", "nosuch"], ["store", "command"], ["store", "env", "--", "true"], ["show", "--shared"]):
        assert _custom(monkeypatch, capsys, *bad)[0], bad


def test_a_left_plaintext_key_file_is_reported_never_deleted(env, monkeypatch, capsys):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    project.save_secret("peer-key", _CUSTOM)   # the old default: plaintext
    assert secretstore.plaintext_report() == ""
    code, out = _custom(monkeypatch, capsys, "store", "env", "--shared")
    assert code == 0 and "plaintext key file left" in out and "delete it yourself" in out
    code, out = _custom(monkeypatch, capsys, "store")
    assert "secret store: env (shared host" in out and "plaintext key file left" in out and _CUSTOM not in out
    assert project.secrets_path().exists()
    monkeypatch.setenv(secretstore.ENV_VAR, "{}")
    code, out = _custom(monkeypatch, capsys, "show")
    assert code == 0 and "plaintext key file left" in out


def test_keys_reach_another_machine_only_on_request_and_never_a_shared_one(env, monkeypatch, tmp_path, capsys):
    import subprocess as sp
    from types import SimpleNamespace
    from ttp import cli, coordinator, machines, timefmt
    project, secretstore = _runtime_store_home(env, monkeypatch)
    project.save_secret("peer-key", _CUSTOM)
    remote = tmp_path / "remote"
    (remote / ".tt-project").mkdir(parents=True)
    real_run = sp.run

    def fake_ssh(argv, **k):   # runs the remote command here, with the remote machine's HOME
        if argv[:1] != ["ssh"]:
            return real_run(argv, **k)
        return real_run(["sh", "-c", argv[-1]], env={"HOME": str(remote), "PATH": os.environ["PATH"]}, **k)
    monkeypatch.setattr(cli.subprocess, "run", fake_ssh)
    (remote / ".tt-project" / "secret-store.json").write_text(json.dumps({"backend": "command", "command": ["x"]}))
    assert "could not copy" in cli.push_secrets("box") and not (remote / ".tt-project" / "secrets.json").exists()
    (remote / ".tt-project" / "secret-store.json").write_text(json.dumps({"backend": "file", "shared": True}))
    assert "could not copy" in cli.push_secrets("box") and not (remote / ".tt-project" / "secrets.json").exists()
    (remote / ".tt-project" / "secret-store.json").unlink()
    assert "copied keys" in cli.push_secrets("box")
    assert json.loads((remote / ".tt-project" / "secrets.json").read_text()) == {"peer-key": _CUSTOM}
    # ttp new --host copies nothing unless --copy-secrets
    pushed = []
    monkeypatch.setattr(cli, "push_secrets", lambda host: pushed.append(host) or "copied")
    monkeypatch.setattr(cli, "ship_runtime", lambda host: "~/stage/bin/ttp")
    monkeypatch.setattr(machines, "push", lambda host: "machines: pushed")
    monkeypatch.setattr(timefmt, "in_ssh_login", lambda: True)
    monkeypatch.setattr(cli, "register", lambda name, entry: None)
    monkeypatch.setattr(cli, "detect_provider", lambda: "")
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    for copy in (False, True):
        a = SimpleNamespace(dir="~/root", host="box", no_secrets=False, copy_secrets=copy, name="demo", provider="",
                            no_service=True, coordinator_effort=coordinator.DEFAULT_EFFORT)
        cli.new_remote(a, "brief")
    assert pushed == ["box"] and "saved keys stay on this machine" in capsys.readouterr().out


@pytest.mark.parametrize("backend, cmd, refused", [
    ({"backend": "command", "command": ["/opt/it/fetch-keys", "--all"]}, "/opt/it/fetch-keys --all | head", True),
    ({"backend": "keychain"}, "security find-generic-password -s tt-project-secrets -w", True),
    ({"backend": "env"}, "printenv TTP_SECRETS_JSON", True),
    ({"backend": "file"}, "python3 -c 'from ttp import secretstore; print(secretstore.load())'", True),
    ({"backend": "command", "command": ["/opt/it/fetch-keys", "--all"]}, "ttp secret store", False),
    ({"backend": "keychain"}, "security list-keychains", False),
])
def test_hook_refuses_runs_that_read_the_run_time_store_directly(env, monkeypatch, backend, cmd, refused):
    from ttp import hook
    project, secretstore = _runtime_store_home(env, monkeypatch)
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    secretstore.config_path().write_text(json.dumps(backend))
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"]))
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(env["tmp"])})
    denied = bool(out) and out["hookSpecificOutput"].get("permissionDecision") == "deny"
    assert denied == refused, out


def _fresh_masking(monkeypatch):
    """A process that never read the store (a run, a hook), outside a sandboxed run."""
    from ttp import redact
    monkeypatch.setattr(redact, "_cache", {"sig": None, "pairs": [], "error": None, "ever": False})
    for var in ("TTP_RUN_DIR", "SANDBOX_RUNTIME"):
        monkeypatch.delenv(var, raising=False)
    return redact


def test_a_daemon_refuses_the_env_store_and_its_runs_never_inherit_or_miss_it_silently(env, monkeypatch, capsys):
    from ttp import outscan
    from ttp.daemon import Daemon
    p = make(env)
    project, secretstore = _runtime_store_home(env, monkeypatch)
    code, out = _custom(monkeypatch, capsys, "store", "env")
    assert code == 0 and "a daemon refuses to start with it" in out
    monkeypatch.setenv(secretstore.ENV_VAR, json.dumps({"jev": {"key": _STORE_KEY}}))
    assert Daemon(p.base).run() == 2
    err = capsys.readouterr().err
    assert "daemon refused" in err and "keychain or command" in err and _STORE_KEY not in err
    assert secretstore.ENV_VAR not in os.environ, "no run started by it inherits the keys"
    assert not (p.state / "daemon.pid").exists()
    # A process started without the variable (a run, a hook): the store is unreadable, never empty
    secretstore._env.clear()
    secretstore._mem.update(key=None, data=None)
    redact = _fresh_masking(monkeypatch)
    assert redact.pairs() == [] and redact.blind() and secretstore.ENV_VAR in redact.store_error()
    assert "secrets store could not be read" in outscan.problem("ttp notify", "hello", p=p)


def test_a_store_unreadable_at_the_runs_end_flags_the_run_and_says_so(env, monkeypatch, capsys):
    from ttp import runner
    project, secretstore = _runtime_store_home(env, monkeypatch)
    secretstore.set_config("command", [str(_fetch_script(env, f'echo "{_STORE_KEY}" >&2\nexit 7\n'))])
    redact = _fresh_masking(monkeypatch)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"text": "hello"}) + "\n")
    exit_info: dict = {"rc": 0}
    runner._mask_run(run_dir, exit_info)
    out = capsys.readouterr().out
    assert "exited 7" in exit_info["unmasked"] and _STORE_KEY not in exit_info["unmasked"]
    assert "runner: could not mask stored secrets in the run: secret fetch command" in out and _STORE_KEY not in out
    assert redact.pairs() == [], "pairs() still never raises"
    # A store read once in this process keeps masking its values: not flagged
    _fetch_script(env, f'echo \'{{"jev": {{"key": "{_STORE_KEY}"}}}}\'\n')
    secretstore._mem.update(key=None, data=None)
    redact._cache["sig"] = None
    (run_dir / "output.jsonl").write_text(json.dumps({"text": f"leaked {_STORE_KEY}"}) + "\n")
    exit_info = {"rc": 0}
    runner._mask_run(run_dir, exit_info)
    assert "unmasked" not in exit_info and _STORE_KEY not in (run_dir / "output.jsonl").read_text()


def test_outscan_refuses_while_the_store_is_unreadable_and_passes_once_it_reads(env, monkeypatch):
    from ttp import outscan
    project, secretstore = _runtime_store_home(env, monkeypatch)
    secretstore.set_config("command", [str(_fetch_script(env, "exit 3\n"))])
    redact = _fresh_masking(monkeypatch)
    why = outscan.problem("ttp note --to", "a clean note")
    assert why and "secrets store could not be read" in why and "nothing was sent" in why
    _fetch_script(env, f'echo \'{{"jev": {{"key": "{_STORE_KEY}"}}}}\'\n')
    secretstore._mem.update(key=None, data=None)
    redact._cache["sig"] = None
    assert outscan.problem("ttp note --to", "a clean note") is None
    assert "a stored secret" in outscan.problem("ttp note --to", f"note {_STORE_KEY}")


def test_a_shared_host_still_reading_its_plaintext_file_is_reported_truthfully(env, monkeypatch):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    project.save_secret("peer-key", _CUSTOM)
    secretstore.config_path().write_text(json.dumps({"backend": "file", "shared": True}))   # written by hand
    assert project.load_secrets() == {"peer-key": _CUSTOM}, "the file backend still reads it"
    line = secretstore.plaintext_report()
    assert "still reads it" in line and "no longer reads" not in line and _CUSTOM not in line
    secretstore.config_path().write_text(json.dumps({"backend": "keychain"}))
    assert "no longer reads or writes it" in secretstore.plaintext_report()


@pytest.mark.parametrize("user,account", [("alice.b", "alice.b"), ('ev il" -A', "tt-project"),
                                          ("a\nadd-internet-password", "tt-project"), ("", "tt-project")])
def test_the_keychain_line_never_splits_on_the_users_name(env, monkeypatch, user, account):
    project, secretstore = _runtime_store_home(env, monkeypatch)
    monkeypatch.setenv("USER", user)
    seen = []
    monkeypatch.setattr(secretstore.subprocess, "run", lambda argv, **k: (
        seen.append(k["input"]), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
    secretstore._keychain_save({}, {"peer-key": _CUSTOM})
    words = seen[0].split()
    assert seen[0].count("\n") == 1 and words[words.index("-a") + 1] == account and len(words) == 8


def _pump_live_run(tmp_path, monkeypatch, body, **spec):
    """Starts runner.supervise in a thread over a stand-in agent running Python `body`; returns
    (thread, run dir)."""
    import threading
    from ttp import runner
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.1")
    child = tmp_path / "agent.py"
    tmp_path.mkdir(exist_ok=True)
    child.write_text("import json, os, sys, time\n" + body)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({"argv": [sys.executable, str(child)], "cwd": str(tmp_path),
                                                  "provider": "fake", "timeout_s": 60, **spec}))
    t = threading.Thread(target=runner.supervise, args=(run_dir,), daemon=True)
    t.start()
    return t, run_dir


def _pump_live_wait(check, s=20):
    end = time.time() + s
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_a_running_agents_output_holds_no_key_text_while_it_runs(env, tmp_path, monkeypatch):
    """#6934's follow-up: the runner masks each line before it reaches output.jsonl, so a reader in the
    middle of the run (runwatch, the web app, a copy) finds no key, JSONL-escaped or printed raw."""
    from ttp import redact, runner
    block, lines = _key_block()
    keyfile = tmp_path / "k"
    keyfile.write_text(block)
    t, run_dir = _pump_live_run(tmp_path, monkeypatch, (
        f"block = open({str(keyfile)!r}).read()\n"
        "print(json.dumps({'type': 'user', 'tool_result': {'content': block}}), flush=True)\n"
        "sys.stdout.write('raw:\\n' + block + 'after the block\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('warning: ' + block + 'printed\\n'); sys.stderr.flush()\n"
        "print(json.dumps({'type': 'assistant', 'text': 'printed'}), flush=True)\n"
        "time.sleep(30)\n"))
    out = run_dir / "output.jsonl"
    assert _pump_live_wait(lambda: out.exists() and "printed" in out.read_text()), "the agent's lines reach the file mid-run"
    assert not (run_dir / "exit.json").exists(), "the agent still runs"
    text = out.read_text()
    assert _no_key(text, lines) and text.count(redact.KEY_MASK) == 3, text
    assert json.loads(text.splitlines()[0])["tool_result"]["content"] == redact.KEY_MASK + "\n"
    assert "raw:\n" + redact.KEY_MASK + "\n" + redact.KEY_MASK + "\nafter the block\n" in text
    err = run_dir / "stderr.log"
    assert _pump_live_wait(lambda: "printed" in err.read_text()) and _no_key(err.read_text(), lines), err.read_text()
    runner.request_stop(run_dir)
    t.join(60)
    assert json.loads((run_dir / "exit.json").read_text())["stopped"]


def test_the_masked_stream_still_shows_progress_to_the_stall_guard_and_loses_no_line(env, tmp_path, monkeypatch):
    t, run_dir = _pump_live_run(tmp_path, monkeypatch, (
        "for i in range(25):\n"
        "    print(json.dumps({'type': 'assistant', 'n': i}), flush=True); time.sleep(0.1)\n"
        "sys.stdout.write('tail without a line end')\n"), stall_s=1)
    t.join(60)
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["rc"] == 0 and info["stopped"] is None, info
    lines = (run_dir / "output.jsonl").read_text().split("\n")
    assert [json.loads(x)["n"] for x in lines[:-1]] == list(range(25)) and lines[-1] == "tail without a line end"
    t, run_dir2 = _pump_live_run(tmp_path / "quiet", monkeypatch,
                            "print(json.dumps({'type': 'assistant'}), flush=True); time.sleep(30)\n", stall_s=1)
    t.join(60)
    assert json.loads((run_dir2 / "exit.json").read_text())["stopped"] == "stalled"


def test_a_masking_fault_or_a_tool_holding_the_pipe_never_loses_output_or_hangs_the_run(env, tmp_path, monkeypatch):
    from ttp import redact, runner
    block, lines = _key_block()
    keyfile = tmp_path / "k"
    keyfile.write_text(block)

    def broken(self, data):
        raise RuntimeError("masking broke")
    monkeypatch.setattr(redact.LineScrubber, "line", broken)
    # The agent leaves a process behind that keeps its stdout open: the run still ends.
    started = time.time()
    t, run_dir = _pump_live_run(tmp_path, monkeypatch, (
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        f"print(json.dumps({{'t': open({str(keyfile)!r}).read()}}), flush=True)\n"
        "print('last', flush=True)\n"))
    t.join(60)
    assert time.time() - started < 15, "the pump gave up on the pipe a tool kept open"
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["rc"] == 0, info
    text = (run_dir / "output.jsonl").read_text()
    assert text.endswith("last\n") and _no_key(text, lines) and redact.KEY_MASK in text, "masked at the end"
    assert runner.PUMP_DRAIN_S < 15


def test_line_scrubber_drops_a_raw_blocks_body_and_keeps_what_follows():
    from ttp import redact
    block, lines = _key_block("RSA", "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B\n\n")
    s = redact.LineScrubber()
    stream = ("cat key\n" + block + "plain prose after it, with spaces\n").encode()
    out = b"".join(s.line(x) for x in stream.splitlines(keepends=True)).decode()
    assert out == f"cat key\n{redact.KEY_MASK}\n{redact.KEY_MASK}\nplain prose after it, with spaces\n"
    # A BEGIN line followed by prose ends the block at the prose; a line without a key passes as it was.
    s = redact.LineScrubber()
    head = block.splitlines(keepends=True)[0]
    out = b"".join(s.line(x.encode()) for x in (head, "Not a key line.\n", "x\r\n")).decode()
    assert out == f"{redact.KEY_MASK}\nNot a key line.\nx\r\n"
