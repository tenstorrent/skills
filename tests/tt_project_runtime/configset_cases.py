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
