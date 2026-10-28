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
    assert not ok and "302 redirect to" in msg and "key not sent" in msg and _EP_KEY not in msg, msg
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
