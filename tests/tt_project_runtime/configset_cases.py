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
