# Config area: `ttp config NAME KEY VALUE` on a project's own keys. '' removes the key (its default applies
# again, as for --account settings) and a list- or object-typed key refuses a value of another type.
# Exec'd into test_tt_project_runtime.py: make and its imports are in scope.


def test_config_empty_value_removes_a_list_key_and_its_default_applies(env, capsys):
    """`ttp config NAME sandbox.fence_freed_commands ''` once wrote "" over the list; now it removes the
    key, prunes the section it leaves empty and the effective value is the default list again. A provider
    list key with no built-in default (providers.claude.write_dirs) is removed alike; a key never set is
    reported, nothing written."""
    from ttp import cli
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    cli.main(["config", "demo", "sandbox.fence_freed_commands", '["make"]'])
    cli.main(["config", "demo", "providers.claude.write_dirs", '["out"]'])
    assert p.raw_config()["sandbox"]["fence_freed_commands"] == ["make"]
    capsys.readouterr()
    cli.main(["config", "demo", "sandbox.fence_freed_commands", ""])
    assert "removed" in capsys.readouterr().out
    assert "fence_freed_commands" not in (p.raw_config().get("sandbox") or {})
    assert p.config()["sandbox"]["fence_freed_commands"] == DEFAULT_CONFIG["sandbox"]["fence_freed_commands"]
    cli.main(["config", "demo", "providers.claude.write_dirs", ""])
    assert "write_dirs" not in p.raw_config()["providers"].get("claude", {})
    assert isinstance(p.raw_config()["providers"], dict), "a section still holding other keys stays"
    before = p.config_path.read_text()
    cli.main(["config", "demo", "sandbox.fence_freed_commands", ""])
    assert "was not set" in capsys.readouterr().out
    assert p.config_path.read_text() == before


def test_config_refuses_a_mistyped_list_or_object_value_and_writes_nothing(env, capsys):
    """A known list key given a string, number or object, and a known object key given a list, is refused
    with the expected type named; project.json is unchanged. A JSON-quoted "" or null is a value, not a
    removal, so it is refused on a list key too."""
    from ttp import cli
    p = make(env)
    before = p.config_path.read_text()
    for key, val, want in (("sandbox.fence_freed_commands", "make", "a JSON list"),
                           ("sandbox.fence_freed_commands", '""', "a JSON list"),
                           ("sandbox.fence_freed_commands", "3", "a JSON list"),
                           ("sandbox.fence_freed_commands", "null", "a JSON list"),
                           ("review.risky_paths", '{"a": 1}', "a JSON list"),
                           ("providers.claude.write_dirs", "out", "a JSON list"),
                           ("coordinator.turn_timeouts_s", "[1]", "a JSON object")):
        with pytest.raises(SystemExit):
            cli.main(["config", "demo", key, val])
        err = capsys.readouterr().err
        assert f"{key} needs {want}" in err and "nothing was written" in err, (key, val, err)
    assert p.config_path.read_text() == before
    # The right type is set as before.
    cli.main(["config", "demo", "coordinator.turn_timeouts_s", '{"standard": 900}'])
    assert p.raw_config()["coordinator"]["turn_timeouts_s"] == {"standard": 900}


def test_config_string_key_still_sets_and_empty_removes_it(env, capsys):
    """An ordinary string key keeps taking plain text; '' removes it so the default applies."""
    from ttp import cli
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    cli.main(["config", "demo", "heal.escalate", "coordinator"])
    assert p.raw_config()["heal"]["escalate"] == "coordinator"
    assert "heal.escalate = \"coordinator\"" in capsys.readouterr().out
    cli.main(["config", "demo", "heal.escalate", ""])
    assert "escalate" not in (p.raw_config().get("heal") or {})
    assert p.config()["heal"]["escalate"] == DEFAULT_CONFIG["heal"]["escalate"]


def test_config_empty_value_keeps_the_who_may_gates(env, monkeypatch, capsys):
    """Removing a key is a change like setting it: a worker may not remove runner.host_commands (nor the
    runner section holding it), and inside a run a user-only key is not removed."""
    from ttp import cli
    from ttp.coordinator import NEEDS_USER
    p = make(env)
    cli.main(["config", "demo", "runner.host_commands", json.dumps([{"cmd": "/bin/id -u", "exact": True}])])
    monkeypatch.setattr(cli, "in_run", lambda: True)
    monkeypatch.setenv("TTP_TASK_ID", "7")
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "1"))
    from ttp import configwho
    monkeypatch.setattr(configwho, "from_env", lambda: {"by": "worker", "task": 7})
    for key in ("runner.host_commands", "runner"):
        with pytest.raises(SystemExit):
            cli.main(["config", "demo", key, ""])
        assert "never by a worker" in capsys.readouterr().err
    assert p.raw_config()["runner"]["host_commands"]
    key = sorted(k for k in NEEDS_USER if "." in k)[0]
    p.set_config(key, 1)
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", key, ""])
    assert "only the user may change" in capsys.readouterr().err
    sec, _, k = key.partition(".")
    assert k in p.raw_config()[sec]


@pytest.mark.parametrize("key,raw,want", [
    ("budget.compact_window_tokens", "0", 0),          # daemon: 0 is off for every tier
    ("budget.compact_window_tokens", "120000", 120000),  # daemon: one window for every tier
    ("budget.split_reread_tokens", "0", 0),            # daemon: 0 is off for every tier
    ("budget.split_reread_tokens", "3000000", 3000000),
    ("waiting.wake_gate", "false", False),             # waitgate.settings: off
    ("budget.resume_lost", "false", False),            # daemon: not an object, a lost run starts fresh
])
def test_config_takes_the_plain_form_the_reader_reads(env, capsys, key, raw, want):
    """An object-typed key whose reader also takes a plain value (KEY_EXTRA_FORMS) is set to it."""
    from ttp import cli, waitgate
    p = make(env)
    cli.main(["config", "demo", key, raw])
    sec, _, k = key.partition(".")
    assert p.raw_config()[sec][k] == want and type(p.raw_config()[sec][k]) is type(want)
    if key == "waiting.wake_gate":
        assert waitgate.settings(p.config())["enabled"] is False


@pytest.mark.parametrize("key,raw,forms", [
    ("budget.compact_window_tokens", "-1", "a JSON object ({...}) or a non-negative number"),
    ("budget.compact_window_tokens", '"100000"', "or a non-negative number"),
    ("budget.compact_window_tokens", "true", "or a non-negative number"),
    ("budget.compact_window_tokens", "[1]", "or a non-negative number"),
    ("budget.split_reread_tokens", "-5", "or a non-negative number"),
    ("budget.split_reread_tokens", "off", "or a non-negative number"),
    ("budget.split_reread_tokens", "1e999", "or a non-negative number"),
    ("waiting.wake_gate", "true", "a JSON object ({...}) or false (off)"),   # waitgate.settings has no true form
    ("waiting.wake_gate", "0", "or false (off)"),
    ("waiting.wake_gate", "off", "or false (off)"),
    ("budget.resume_lost", "0", "or false (off)"),
    ("budget.resume_lost", "true", "or false (off)"),
])
def test_config_still_refuses_other_forms_naming_the_allowed_ones(env, capsys, key, raw, forms):
    from ttp import cli
    p = make(env)
    before = p.config_path.read_text()
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", key, raw])
    err = capsys.readouterr().err
    assert f"{key} needs " in err and forms in err and "nothing was written" in err, err
    assert p.config_path.read_text() == before


def test_config_set_and_the_web_settings_path_run_the_same_type_check(env, monkeypatch):
    """Project.set_config applies key_type_problem, so the coordinator's config_set and the web app's
    /api/config (both call it after their own per-key validator) refuse a mistyped value alike."""
    from ttp import coordinator as coord
    p = make(env)
    with pytest.raises(ValueError, match="sandbox.fence_freed_commands needs a JSON list"):
        p.set_config("sandbox.fence_freed_commands", "")
    with pytest.raises(ValueError, match="waiting.wake_gate needs a JSON object"):
        p.set_config("waiting.wake_gate", "off")
    assert "wake_gate" not in (p.raw_config().get("waiting") or {})
    monkeypatch.setitem(coord.USER_SETTABLE, "waiting.wake_gate", lambda v: v)   # a pass-through validator
    errs = coord.apply(p, [{"type": "config_set", "key": "waiting.wake_gate", "value": "off"}])
    assert errs and "waiting.wake_gate needs" in str(errs), errs
    assert "wake_gate" not in (p.raw_config().get("waiting") or {})
    assert coord.apply(p, [{"type": "config_set", "key": "waiting.wake_gate", "value": False}]) == []
    assert p.raw_config()["waiting"]["wake_gate"] is False


@pytest.mark.parametrize("key,value", [
    ("coordinator.conclusion_floor", "off"),
    ("coordinator.conclusion_floor", "deep/xhigh"),
    ("providers_allowed", "claude"),
    ("coordinator.provider_failover_to", "claude"),
    ("ack_wait.probe_commands", "my-probe --x"),
])
def test_set_config_takes_the_string_form_the_reader_reads(env, key, value):
    """A list- or object-typed key whose reader also reads a string (KEY_EXTRA_FORMS) stores it as given,
    and the reader reads it."""
    from ttp import ackwait, failover, provallow, tierfloor
    p = make(env)
    p.set_config(key, value)
    cfg = p.config()
    read = {"coordinator.conclusion_floor": lambda: tierfloor.floor(cfg),
            "providers_allowed": lambda: provallow.names(cfg),
            "coordinator.provider_failover_to": lambda: failover.targets(cfg),
            "ack_wait.probe_commands": lambda: ackwait.probe_commands(cfg)}[key]()
    want = {"off": {"tier": "light", "effort": ""}, "deep/xhigh": {"tier": "deep", "effort": "xhigh"},
            "my-probe --x": [["my-probe", "--x"]]}.get(value)
    assert read == want if want is not None else "claude" in read, read


@pytest.mark.parametrize("key,value,forms", [
    ("coordinator.conclusion_floor", "nonsense", "or a tier and effort such as 'standard/high', or 'off'"),
    ("coordinator.conclusion_floor", "standard/loud", "or a tier and effort"),
    ("coordinator.conclusion_floor", 3, "a JSON object ({...}) or a tier and effort"),
    ("coordinator.conclusion_floor", False, "or a tier and effort"),
    ("providers_allowed", 1, "a JSON list ([...]) or a comma-separated string"),
    ("providers_allowed", {"claude": 1}, "or a comma-separated string"),
    ("coordinator.provider_failover_to", True, "a JSON list ([...]) or a string"),
    ("ack_wait.probe_commands", {"cmd": "x"}, "a JSON list ([...]) or a string"),
    ("shared_resources", "board", "needs a JSON list ([...]), not"),   # shared.names ignores a string
])
def test_set_config_still_refuses_what_no_reader_reads(env, key, value, forms):
    p = make(env)
    before = p.config_path.read_text()
    with pytest.raises(ValueError) as e:
        p.set_config(key, value)
    assert f"{key} needs " in str(e.value) and forms in str(e.value), str(e.value)
    assert p.config_path.read_text() == before


# The coordinator's config_set and the web app's /api/config follow the same rules as `ttp config`.

def test_coordinator_config_set_empty_removes_a_list_or_object_key_and_its_default_applies(env):
    """config_set '' once stored [] (provider_failover_to) or was refused (conclusion_floor); now it removes
    the key so its default applies, and the next turn's notes say so. A key never set writes nothing."""
    from ttp import coordinator as coord
    from ttp.project import DEFAULT_CONFIG
    p = make(env)
    p.set_config("coordinator.provider_failover_to", ["codex"])
    p.set_config("coordinator.conclusion_floor", {"tier": "deep", "effort": "high"})
    for key in ("coordinator.provider_failover_to", "coordinator.conclusion_floor"):
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": ""}]) == [], key
        assert key.split(".")[1] not in (p.raw_config().get("coordinator") or {}), key
        assert p.config()["coordinator"][key.split(".")[1]] == DEFAULT_CONFIG["coordinator"][key.split(".")[1]]
        assert any(f"config_set {key}: removed" in n for n in p.db.kv(coord.NOTES_KEY) or []), key
    before = p.config_path.read_text()
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.provider_failover_to", "value": ""}]) == []
    assert p.config_path.read_text() == before
    assert any("was not set" in n for n in p.db.kv(coord.NOTES_KEY) or [])


def test_coordinator_config_set_refuses_a_mistyped_list_or_object_value(env, monkeypatch):
    """A value whose JSON type does not match a list or object key is refused and nothing is written, even
    when the key's own converter lets it through; the right type still sets."""
    from ttp import coordinator as coord
    p = make(env)
    monkeypatch.setitem(coord.USER_SETTABLE, "sandbox.fence_freed_commands", lambda v: v)
    monkeypatch.setitem(coord.USER_SETTABLE, "coordinator.turn_timeouts_s", lambda v: v)
    before = p.config_path.read_text()
    for key, val, want in (("sandbox.fence_freed_commands", "make", "a JSON list"),
                           ("sandbox.fence_freed_commands", 3, "a JSON list"),
                           ("sandbox.fence_freed_commands", None, "a JSON list"),
                           ("coordinator.turn_timeouts_s", [1], "a JSON object")):
        probs = coord.apply(p, [{"type": "config_set", "key": key, "value": val}], user_turn=True)
        assert probs and f"{key} needs {want}" in probs[0] and "nothing was written" in probs[0], (key, val, probs)
    assert p.config_path.read_text() == before
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.turn_timeouts_s", "value": {"standard": 900}}],
                       user_turn=True) == []
    assert p.raw_config()["coordinator"]["turn_timeouts_s"] == {"standard": 900}


def test_coordinator_config_set_empty_keeps_the_who_may_gates(env):
    """Removing a user-only key outside a user turn is refused unless its default is the off side: a spend cap,
    an on-by-default switch and the merge list stay; an off-by-default switch is removed. In a user turn the
    spend cap is removed; the merge list is still never emptied by config_set (it only adds repos)."""
    from ttp import coordinator as coord
    p = make(env)
    p.set_config("budget.daily_usd", 7.0)
    p.set_config("budget.reset_credits_auto", False)
    p.set_config("delivery.auto_merge_repos", ["org/repo"])
    p.set_config("delivery.backup_remote", "backup")
    before = p.raw_config()
    for key in ("budget.daily_usd", "budget.reset_credits_auto", "delivery.auto_merge_repos"):
        probs = coord.apply(p, [{"type": "config_set", "key": key, "value": ""}])
        assert probs and f"{key} needs the user's approval" in probs[0], (key, probs)
    assert p.raw_config() == before
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.backup_remote", "value": ""}]) == []
    assert "backup_remote" not in (p.raw_config().get("delivery") or {})
    assert coord.apply(p, [{"type": "config_set", "key": "budget.daily_usd", "value": ""}], user_turn=True) == []
    assert "daily_usd" not in (p.raw_config().get("budget") or {})
    mid = p.db.x("INSERT INTO messages(ts, direction, kind, text) VALUES (?, 'in', 'user', 'drop the merge grant')",
                 (time.time(),))   # the user's own word: config_set still only adds repos
    probs = coord.apply(p, [{"type": "config_set", "key": "delivery.auto_merge_repos", "value": ""}],
                        user_turn=True, messages=[mid])
    assert probs and "only adds repos" in probs[0], probs
    assert p.raw_config()["delivery"]["auto_merge_repos"] == ["org/repo"]


@needs_sockets
def test_web_api_config_empty_removes_and_a_mistyped_value_is_refused(env):
    """The web app's settings: '' removes the key (its default applies); a list key given another type is a
    400 naming the expected type and writes nothing (provider_failover_to's converter once let it through)."""
    from ttp import web
    p = make(env)
    port = _start_web(p)

    def post(body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-TTP-Token": web.token(p), "Content-Type": "application/json"})
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
            except OSError:
                time.sleep(0.1)
        raise AssertionError("the web app did not start")
    before = p.config_path.read_text()
    for val in (5, {"a": 1}, None):   # one string ("codex") is a plain form its reader takes (KEY_EXTRA_FORMS)
        code, out = post({"key": "coordinator.provider_failover_to", "value": val})
        assert code == 400 and "needs a JSON list" in out["error"], (val, code, out)
    assert p.config_path.read_text() == before
    p.set_config("coordinator.provider_failover_to", ["codex"])
    p.set_config("budget.daily_usd", 7.0)
    for key in ("coordinator.provider_failover_to", "budget.daily_usd"):
        assert post({"key": key, "value": ""}) == (200, {"ok": True, "removed": True}), key
    assert "provider_failover_to" not in (p.raw_config().get("coordinator") or {})
    assert "daily_usd" not in (p.raw_config().get("budget") or {})
    assert post({"key": "budget.daily_usd", "value": ""}) == (200, {"ok": True, "removed": False})


def test_an_unset_weekly_cap_is_seven_days_at_the_daily_cap_and_a_set_one_is_kept(env, capsys):
    """An unset budget.weekly_usd once sat at $200 under a $300 daily cap and stopped work first. Unset,
    it is now 7 times the daily cap in force (account or project); a weekly cap someone set is kept as
    set, and `ttp config` warns when it is below the daily cap."""
    from ttp import cli
    from ttp.project import weekly_below_daily
    p = make(env)
    assert p.config()["budget"]["weekly_usd"] == 7 * p.config()["budget"]["daily_usd"]
    cli.main(["config", "demo", "budget.daily_usd", "300"])
    assert p.config()["budget"]["weekly_usd"] == 2100.0
    assert "weekly_usd" not in p.raw_config()["budget"], "the default is computed, never written"
    capsys.readouterr()
    cli.main(["config", "demo", "budget.weekly_usd", "200"])
    err = capsys.readouterr().err
    assert "warning: budget.weekly_usd $200 is below budget.daily_usd $300" in err, err
    assert p.config()["budget"]["weekly_usd"] == 200.0, "a set value is kept"
    cli.main(["config", "demo", "budget.weekly_usd", "0"])
    assert p.config()["budget"]["weekly_usd"] == 0, "0 (no weekly cap) is a set value too"
    assert not weekly_below_daily(p.config()["budget"]), "no weekly cap is never below the daily one"
    cli.main(["config", "demo", "budget.weekly_usd", "5000"])
    assert "warning" not in capsys.readouterr().err
    assert weekly_below_daily({"daily_usd": 300, "weekly_usd": 200})
    assert not weekly_below_daily({"daily_usd": 0, "weekly_usd": 200}), "no daily cap: nothing to compare"


def _with_raw(p, **sections):
    """project.json as an older plugin left it: `sections` merged in by hand, no setter recorded."""
    raw = p.raw_config()
    for sec, keys in sections.items():
        raw.setdefault(sec, {}).update(keys)
    p.config_path.write_text(json.dumps(raw))


def test_config_empty_value_removes_an_unknown_key_present_and_records_who(env, capsys):
    """A key the schema no longer knows (one an older plugin retired) that project.json still holds is removed
    by `ttp config NAME KEY ''`, which says what it held and records the removal in config provenance; the
    other keys of its section stay. Absent, the same request still errors (the key is retired), and a non-empty
    value on it is still refused, nothing written."""
    from ttp import cli, configwho
    from ttp.project import unknown_key_hint
    p = make(env)
    assert unknown_key_hint("coordinator.unblock_effort")
    _with_raw(p, coordinator={"unblock_effort": "high", "model": "some-model", "effort": "xhigh"})
    capsys.readouterr()
    cli.main(["config", "demo", "coordinator.unblock_effort", ""])
    out = capsys.readouterr().out
    assert 'coordinator.unblock_effort removed (was "high")' in out, out
    coord = p.raw_config()["coordinator"]
    assert "unblock_effort" not in coord and coord["model"] == "some-model" and coord["effort"] == "xhigh"
    rec = configwho.lookup(p.db, "coordinator.unblock_effort", p.raw_config())
    assert rec and rec.get("h") == "-", rec
    before = p.config_path.read_text()
    for value in ("", "low"):
        with pytest.raises(SystemExit) as e:
            cli.main(["config", "demo", "coordinator.unblock_effort", value])
        assert e.value.code == 2 and "coordinator.unblock_effort is retired" in capsys.readouterr().err, value
    _with_raw(p, coordinator={"unblock_effort": None})
    before = p.config_path.read_text()
    with pytest.raises(SystemExit):     # present, but setting a value is still refused
        cli.main(["config", "demo", "coordinator.unblock_effort", "low"])
    assert "is retired" in capsys.readouterr().err and p.config_path.read_text() == before
    cli.main(["config", "demo", "coordinator.unblock_effort", ""])    # present at null: still removed
    assert "unblock_effort" not in p.raw_config()["coordinator"]


def test_config_removing_an_unknown_key_keeps_the_who_may_gates(env, monkeypatch, capsys):
    """The removal of an unknown key runs the same gates as any removal: inside a run, a key under a user-only
    setting is not removed, and a worker may not remove anything under runner.host_commands."""
    from ttp import cli, configwho, coordinator
    p = make(env)
    _with_raw(p, coordinator={"unblock_effort": "high"})
    monkeypatch.setitem(coordinator.NEEDS_USER, "coordinator.unblock_effort", "spend")
    monkeypatch.setattr(cli, "in_run", lambda: True)
    monkeypatch.setenv("TTP_TASK_ID", "7")
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "1"))
    monkeypatch.setattr(configwho, "from_env", lambda: {"by": "worker", "task": 7})
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "coordinator.unblock_effort", ""])
    assert "only the user may change" in capsys.readouterr().err
    assert p.raw_config()["coordinator"]["unblock_effort"] == "high"
    _with_raw(p, runner={"host_commands": {"legacy": 1}})
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "runner.host_commands.legacy", ""])
    assert "never by a worker" in capsys.readouterr().err
    assert p.raw_config()["runner"]["host_commands"] == {"legacy": 1}


def test_upgrade_drops_retired_keys_once_and_keeps_the_rest(env):
    """The upgrade's migration removes each project.RETIRED_KEYS key project.json holds, one note line and one
    provenance record each; coordinator.model, coordinator.effort and every other key stay, and a second run
    changes nothing."""
    from ttp import cli, configwho
    from ttp.project import RETIRED_KEYS, unknown_key_hint
    assert "coordinator.unblock_effort" in RETIRED_KEYS
    assert all(unknown_key_hint(k) for k in RETIRED_KEYS), "a retired key is one no part of the runtime reads"
    p = make(env)
    assert cli._drop_retired_keys(p) == [], "nothing retired held: nothing to change"
    _with_raw(p, coordinator={"unblock_effort": "low", "model": "some-model", "effort": "high"},
              budget={"daily_usd": 5}, upgrade={"auto": True, "merge_retry_h": 6})
    before = p.raw_config()
    notes = cli._drop_retired_keys(p)
    assert len(notes) == 2 and any("coordinator.unblock_effort" in n for n in notes), notes
    assert any("upgrade.merge_retry_h" in n for n in notes), notes
    after = p.raw_config()
    del before["coordinator"]["unblock_effort"], before["upgrade"]["merge_retry_h"]
    assert after == before
    rec = configwho.lookup(p.db, "coordinator.unblock_effort", after)
    assert rec and rec.get("h") == "-" and "retired" in rec.get("via", ""), rec
    text = p.config_path.read_text()
    assert cli._drop_retired_keys(p) == [] and p.config_path.read_text() == text, "a rerun is a no-op"


def test_a_run_sets_a_dollar_cap_only_on_the_users_own_message_carrying_the_setting(env, monkeypatch, capsys):
    """A cap change the user wrote in chat as the setting itself is applied by a run with --user-msg,
    recorded as the user's with the message id, only when that message is the user's own and carries the
    cap's key with that one value; a harness post, a relayer's context, another value or cap, an old
    message, a later user or coordinator set and every other user-only key are refused (capword)."""
    from ttp import capword, cli, configwho
    p = make(env)
    p.set_config("budget.weekly_usd", 200.0)
    now = time.time()

    def msg(text, ts=now - 60, kind="user", provenance=""):
        p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,provenance) VALUES(?,?,?,?,?,?)",
               (ts, "in", "c", kind, text, provenance))
        return p.db.one("SELECT MAX(id) i FROM messages")["i"]

    ok = msg("go faster: budget.weekly_usd 400 please")
    harness = msg("budget.weekly_usd 400", provenance="system")
    relayed = msg("go faster\n" + __import__("ttp.relay", fromlist=["MARK"]).MARK + "\nbudget.weekly_usd 400")
    other_value = msg("budget.weekly_usd 500", ts=now - 7200)   # older than `ok`: the user's newest word is 400
    other_cap = msg("budget.daily_usd 400")
    old = msg("budget.weekly_usd 400", ts=now - capword.MAX_AGE_S - 60)
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text) VALUES(?,?,?,?,?)",
           (now - 30, "out", "c", "user", "budget.weekly_usd 400"))   # an outbound row is nobody's order
    out = p.db.one("SELECT MAX(id) i FROM messages")["i"]
    monkeypatch.setattr(cli, "in_run", lambda: True)
    monkeypatch.setenv("TTP_TASK_ID", "7")
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "1"))
    monkeypatch.setattr(configwho, "from_env", lambda: {"by": "worker", "task": 7})
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "budget.weekly_usd", "400"])
    assert "--user-msg" in capsys.readouterr().err   # the refusal names the path
    for mid, why in ((harness, "not the user's own"), (relayed, "do not carry"), (other_value, "do not carry"),
                     (other_cap, "do not carry"), (old, "older than"), (out, "not the user's own"),
                     (99999, "not the user's own")):
        with pytest.raises(SystemExit):
            cli.main(["config", "demo", "budget.weekly_usd", "400", "--user-msg", str(mid)])
        assert why in capsys.readouterr().err, mid
    with pytest.raises(SystemExit):   # a section write that would change the cap: only the cap's own key
        cli.main(["config", "demo", "budget", json.dumps({"weekly_usd": 400}), "--user-msg", str(ok)])
    assert "itself, not budget" in capsys.readouterr().err
    with pytest.raises(SystemExit):   # another user-only key never takes this path
        cli.main(["config", "demo", "delivery.code_tasks_may_push", "true", "--user-msg", str(ok)])
    assert "applies only" in capsys.readouterr().err
    assert p.raw_config()["budget"]["weekly_usd"] == 200.0
    cli.main(["config", "demo", "budget.weekly_usd", "400", "--user-msg", f"#{ok}"])
    assert p.raw_config()["budget"]["weekly_usd"] == 400
    rec = configwho.get(p.db, "budget.weekly_usd")
    assert rec["by"] == "user" and rec["msgs"] == [ok] and rec["task"] == 7, rec
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='config_grant'")["n"] == 1
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 3600, ok))   # the user set it after that message
    p.set_config("budget.weekly_usd", 300.0, by=configwho.by("user", via="chat"))
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "budget.weekly_usd", "400", "--user-msg", str(ok)])
    assert "later value stands" in capsys.readouterr().err
    daily = msg("budget.daily_usd 250")   # a later coordinator set wins too (on a key the user has not set)
    p.set_config("budget.daily_usd", 220.0, by=configwho.by("coordinator"))
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "budget.daily_usd", "250", "--user-msg", str(daily)])
    assert "later value stands" in capsys.readouterr().err
    assert p.raw_config()["budget"]["daily_usd"] == 220.0


def test_capword_wording_alone_never_grants_a_cap_only_the_setting_itself(env):
    """No gate reads the meaning of the user's words (user msg #2116): prose that plainly orders a cap
    ('raise the weekly cap to $400') grants nothing in a run; only the setting itself, the cap's full key
    with one plain value, grants it. A key inside another key, a short key name, two values, a '#' id,
    a number that runs on (400,000, 400k, 400.5.1) and an outbound copy are refused."""
    from ttp import capword
    p = make(env)
    now = time.time()

    def problem(text, key, value):
        p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,provenance) VALUES(?,?,?,?,?,?)",
               (now - 60, "in", "c", "user", text, ""))
        return capword.problem(p.db, key, value, p.db.one("SELECT MAX(id) i FROM messages")["i"], now=now)

    for text, key, value in (("raise the weekly cap to $400 please", "budget.weekly_usd", 400),
                             ("daily cap $1.5k", "budget.daily_usd", 1500),
                             ("set the weekly limit to 1,200 usd.", "budget.weekly_usd", 1200),
                             ("set daily_usd to 250", "budget.daily_usd", 250),
                             ("budget.global_daily_usd 3000", "budget.daily_usd", 3000),
                             ("xbudget.daily_usd 3000", "budget.daily_usd", 3000),
                             ("budget.daily_usd #400", "budget.daily_usd", 400),
                             ("budget.weekly_usd 400,000", "budget.weekly_usd", 400),
                             ("budget.weekly_usd 400k", "budget.weekly_usd", 400),
                             ("budget.weekly_usd 400.5.1", "budget.weekly_usd", 400.5),
                             ("budget.weekly_usd 400 or budget.weekly_usd 500", "budget.weekly_usd", 400),
                             ("budget.weekly_usd 400", "budget.daily_usd", 400)):
        assert problem(text, key, value), text
    for text, key, value in (("budget.weekly_usd 400", "budget.weekly_usd", 400),
                             ("please set budget.daily_usd=$250.5 today.", "budget.daily_usd", 250.5),
                             ("budget.daily_usd: 1500", "budget.daily_usd", 1500),
                             ("budget.weekly_usd 400, and budget.weekly_usd=400", "budget.weekly_usd", 400)):
        assert problem(text, key, value) is None, text

    # The user's newest word wins: an older message's value is refused once a later message of theirs
    # gives the key another value (spend fail-open otherwise); a later message repeating the same value,
    # naming another key, or posted by the harness changes nothing.
    p.db.x("DELETE FROM messages")

    def msg(text, age, provenance=""):
        p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,provenance) VALUES(?,?,?,?,?,?)",
               (now - age, "in", "c", "user", text, provenance))
        return p.db.one("SELECT MAX(id) i FROM messages")["i"]
    old = msg("budget.weekly_usd 800", 3600)
    assert capword.problem(p.db, "budget.weekly_usd", 800, old, now=now) is None
    msg("budget.weekly_usd 800 again", 1800)
    msg("budget.daily_usd 50", 1200)
    msg("budget.weekly_usd 5000", 900, provenance="system")
    assert capword.problem(p.db, "budget.weekly_usd", 800, old, now=now) is None
    newer = msg("make it budget.weekly_usd 300", 60)
    why = capword.problem(p.db, "budget.weekly_usd", 800, old, now=now)
    assert why and f"message #{newer}" in why and "300" in why, why
    assert capword.problem(p.db, "budget.weekly_usd", 300, newer, now=now) is None


def test_coordinator_config_set_in_a_user_turn_is_the_users_only_when_it_cites_their_word(env):
    """A coordinator config_set in a turn that carries an unrelated user message is the coordinator's and
    stays its to change. It is the user's (and locked against the coordinator) only when the action cites
    the user's message (`user_msg`) or quotes their words (`quote`), or the key is one only their word sets."""
    from ttp import configwho, coordinator as coord
    p = make(env)

    def said(text):
        return p.db.x("INSERT INTO messages(ts, direction, kind, text) VALUES (?, 'in', 'user', ?)",
                      (time.time(), text))

    key = "providers.claude.mcp_withheld"
    m = said("land the backlog first")   # nothing about the setting
    assert coord.apply(p, [{"type": "config_set", "key": key, "value": ["a"]}], user_turn=True, messages=[m]) == []
    assert configwho.role(configwho.get(p.db, key)) == "coordinator"
    assert coord.apply(p, [{"type": "config_set", "key": key, "value": ["a", "b"]}]) == []   # still its own
    assert p.raw_config()["providers"]["claude"]["mcp_withheld"] == ["a", "b"]
    # A quote not in the user's message, or a user_msg from another turn, grants nothing.
    for cite in ({"quote": "withhold the mail connector"}, {"user_msg": m + 99}, {"quote": "la"}):
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": ["a", "c"], **cite}],
                           user_turn=True, messages=[m]) == [], cite
        assert configwho.role(configwho.get(p.db, key)) == "coordinator", cite
    # Cited: the user's, and the coordinator's later change is refused.
    for key, cite in (("providers.claude.mcp_withheld", "user_msg"),
                      ("providers.claude.mcp_servers", {"quote": "Keep  the MAIL connector out"})):
        u = said("please keep the mail connector out of workers")
        cite = {"user_msg": u} if cite == "user_msg" else cite
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": ["mail"], **cite}],
                           user_turn=True, messages=[m, u]) == [], cite
        rec = configwho.get(p.db, key)
        assert rec["by"] == "user" and rec["msgs"] == [u], (cite, rec)
        probs = coord.apply(p, [{"type": "config_set", "key": key, "value": ["mail", "x"]}])
        assert probs and "a coordinator never changes a value the user set" in probs[0], probs
        assert p.raw_config()["providers"]["claude"][key.rsplit(".", 1)[1]] == ["mail"]
    # A key only the user's word sets, in their turn: theirs, with no cite needed.
    w = said("weekly cap 300")
    assert coord.apply(p, [{"type": "config_set", "key": "budget.weekly_usd", "value": 300}],
                       user_turn=True, messages=[w]) == []
    rec = configwho.get(p.db, "budget.weekly_usd")
    assert rec["by"] == "user" and rec["msgs"] == [w], rec
