# Config area: the change log, the owner guard and the breakage watch (configwho.py). Every change through
# set_config is logged with its setter, old and new values and undo; an automated setter never changes a
# value the user or a coordinator set; a change followed by failed check runs raises a coordinator event.
# Exec'd into test_tt_project_runtime.py: make and its imports are in scope.


def _config_clear_run_env(monkeypatch):
    for k in ("TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK_ID", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)


def _config_events(p, kind):
    return p.db.q("SELECT * FROM events WHERE kind=? ORDER BY ts", (kind,))


def test_config_log_records_each_setter_class_with_old_new_and_undo(env, monkeypatch):
    """User (a terminal), coordinator (config_set), worker (a run with a task), harness, upgrade and
    migration each leave their role in the key's record and one log line with old, new and undo."""
    from ttp import cli, configwho, coordinator as coord
    p = make(env)
    _config_clear_run_env(monkeypatch)
    cli.main(["config", "demo", "budget.max_parallel_workers", "6"])
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.provider_failover_to", "value": ["codex"]}]) == []
    monkeypatch.setattr(configwho, "from_env", lambda: configwho.by("worker", task=7, run="41", via="cli"))
    cli.main(["config", "demo", "heal.escalate", "coordinator"])
    p.set_config("checks.wall_cap_s", 900, by=configwho.by("harness", via="throughput"))
    p.set_config("coordinator.cache_ttl", "")
    assert "auto" in cli._auto_cache_ttl(p)
    p.set_config("device.max_tasks", 2)
    assert "device.max_tasks 2" in cli._migrate_load_caps(p)
    want = {"budget.max_parallel_workers": "user", "coordinator.provider_failover_to": "coordinator",
            "heal.escalate": "worker", "checks.wall_cap_s": "harness", "coordinator.cache_ttl": "upgrade",
            "device.max_tasks": "migration"}
    for key, role in want.items():
        assert configwho.role(configwho.lookup(p.db, key, p.raw_config())) == role, key
        rows = [r for r in configwho.history(p, key) if r["by"] == role]
        assert len(rows) == 1 and rows[0]["undo"].startswith(f"ttp config demo {key} "), (key, rows)
    worker = configwho.history(p, "heal.escalate")[-1]
    assert (worker["task"], worker["run"], worker["new"]) == (7, "41", "coordinator") and "old" not in worker
    assert configwho.history(p, "coordinator.cache_ttl")[-1]["old"] == ""
    removed = configwho.history(p, "device.max_tasks")[-1]
    assert removed["old"] == 2 and "new" not in removed and removed["undo"] == "ttp config demo device.max_tasks 2"
    review = "\n".join(configwho.review_lines(p))
    assert "budget.max_parallel_workers" not in review and "heal.escalate" in review and "by upgrade" in review


def test_upgrade_migration_and_worker_never_overwrite_a_user_or_coordinator_value(env, monkeypatch, capsys):
    """An automated setter changing a value the user or a coordinator set is refused: the value stays, the
    refusal is logged and one coordinator event is queued (not one per retry). The user and a coordinator
    still change it; an automated setter writing the same value changes nothing, not even the record."""
    from ttp import cli, configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    cli.main(["config", "demo", "coordinator.cache_ttl", '""'])   # the user's "" (cli: JSON "" is the string)
    p.set_config("device.max_tasks", 2, by=configwho.by("coordinator", via="config_set"))
    assert cli._auto_cache_ttl(p) == "" and p.raw_config()["coordinator"]["cache_ttl"] == ""
    assert cli._migrate_load_caps(p) == "" and p.raw_config()["device"]["max_tasks"] == 2
    with pytest.raises(configwho.Protected):
        p.set_config("coordinator.cache_ttl", "auto", by=configwho.by("upgrade", via="upgrade"))
    with pytest.raises(configwho.Protected):
        p.unset_config("device.max_tasks", by=configwho.by("migration"))
    with pytest.raises(configwho.Protected):   # a section write reaching a protected key under it
        p.set_config("device", {"max_tasks": 3}, by=configwho.by("harness"))
    events = _config_events(p, "config_overwrite_refused")
    assert [e["fingerprint"] for e in events] == ["config_overwrite_refused:coordinator.cache_ttl:upgrade",
                                                   "config_overwrite_refused:device.max_tasks:migration",
                                                   "config_overwrite_refused:device:harness"]
    assert all(e["status"] == "queued" for e in events) and "config_set" in events[0]["text"]
    assert cli._auto_cache_ttl(p) == "" and len(_config_events(p, "config_overwrite_refused")) == 3, "a retry: no second"
    refused = [r for r in configwho.history(p) if r.get("refused")]
    assert len(refused) == 3 and refused[0]["by"] == "upgrade" and refused[0]["new"] == "auto"
    # A worker's `ttp config` over the coordinator's value is refused, and told who makes it.
    monkeypatch.setattr(configwho, "from_env", lambda: configwho.by("worker", task=7, via="cli"))
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "device.max_tasks", "5"])
    assert "the coordinator (config_set) or the user makes this change" in capsys.readouterr().err
    # Same value: nothing written, the coordinator's record stays.
    p.set_config("device.max_tasks", 2, by=configwho.by("harness"))
    assert configwho.role(configwho.lookup(p.db, "device.max_tasks", p.raw_config())) == "coordinator"
    # The owners still change it.
    p.set_config("device.max_tasks", 4, by=configwho.by("coordinator", via="config_set"))
    assert p.raw_config()["device"]["max_tasks"] == 4
    monkeypatch.setattr(configwho, "from_env", lambda: configwho.by("user", via="cli"))
    cli.main(["config", "demo", "coordinator.cache_ttl", "auto"])
    assert p.raw_config()["coordinator"]["cache_ttl"] == "auto"
    assert _config_events(p, "config_overwrite_refused")[-1]["fingerprint"] == "config_overwrite_refused:device.max_tasks:worker"
    # A value set by hand or before setters were recorded (unknown) is not protected.
    p.set_config("heal.escalate", "coordinator")
    p.set_config("heal.escalate", "user", by=configwho.by("harness"))
    assert p.raw_config()["heal"]["escalate"] == "user"


def test_config_value_with_spaces_and_equals_round_trips_through_set_log_and_undo(env, monkeypatch):
    """A wrapper command with spaces, '=' and quotes is stored as typed, logged as is, and its undo
    command, run, puts the earlier value back exactly."""
    import shlex
    from ttp import cli, configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    first = 'env PATH=/opt/example/venv/bin:$PATH A="x y" /opt/example/wrap.sh --flag=1'
    cli.main(["config", "demo", "checks.spill_wrap_cmd", first])
    assert p.raw_config()["checks"]["spill_wrap_cmd"] == first
    cli.main(["config", "demo", "checks.spill_wrap_cmd", "/opt/example/wrap.sh"])
    row = configwho.history(p, "checks.spill_wrap_cmd")[-1]
    assert (row["old"], row["new"]) == (first, "/opt/example/wrap.sh")
    words = shlex.split(row["undo"])
    assert words[:3] == ["ttp", "config", "demo"]
    cli.main(words[1:])
    assert p.raw_config()["checks"]["spill_wrap_cmd"] == first


def test_config_log_redacts_secret_keys_and_values():
    from ttp import configwho
    fake = "xox" + "b-1234567890-abc"   # built here, so the repo's secret scan sees no token
    assert configwho.shown("notify.slack_token", fake) == "<redacted>"
    assert configwho.shown("x", {"api_key": "k", "n": 2}) == {"api_key": "<redacted>", "n": 2}
    assert configwho.shown("checks.cmd", "curl -H 'Authorization: Bearer abcdefghijkl'") \
        == "curl -H 'Authorization: <redacted>'"
    assert configwho.shown("x", "a" * 2000).endswith("...(cut)")
    assert "redacted or cut" in configwho.undo_cmd("demo", "notify.slack_token", fake)


def test_config_breakage_watch_raises_one_event_naming_the_change_and_its_undo(env, monkeypatch):
    """A change followed by failed check runs of two tasks on hosts that passed before, none passing
    since, raises one queued event with the key, setter, old and new values and undo; a host that
    passes again after the change, or one task's failures alone, raise none."""
    from ttp import checkwall, configwho
    from ttp.project import durable_append
    p = make(env)
    _config_clear_run_env(monkeypatch)
    p.set_config("checks.spill_wrap_cmd", "env A=1 /opt/example/wrap.sh")   # by hand: unknown, not protected
    p.set_config("checks.spill_wrap_cmd", "/opt/example/wrap.sh", by=configwho.by("worker", task=5, via="cli"))
    rows = configwho.history(p, "checks.spill_wrap_cmd")
    assert [r["by"] for r in rows] == ["unknown", "worker"] and not any(r.get("refused") for r in rows)

    def runs(*rs):
        (p.state / checkwall.RUNS_LOG).unlink(missing_ok=True)
        for ts, passed, host, task in rs:
            durable_append(p.state / checkwall.RUNS_LOG,
                           json.dumps({"ts": ts, "s": 60, "passed": passed, "host": host, "task": task}) + "\n")

    change = float(rows[-1]["ts"])
    before = [(change - 900, True, "spill-a", "1"), (change - 800, True, "spill-b", "2")]
    runs(*before, (change + 60, False, "spill-a", "3"), (change + 120, True, "spill-a", "4"),
         (change + 180, False, "spill-b", "4"), (change + 240, False, "", "5"))
    assert configwho.watch(p, now=change + 300) == [], "spill-a passed since; this host never passed before"
    runs(*before, (change + 60, False, "spill-a", "3"), (change + 90, False, "spill-b", "3"),
         (change + 120, False, "spill-a", "3"))
    assert configwho.watch(p, now=change + 300) == [], "one task's failures alone"
    runs(*before, (change + 60, False, "spill-a", "3"), (change + 90, False, "spill-b", "4"),
         (change + 120, False, "spill-a", "5"), (change + 150, True, "", "6"))
    fps = configwho.watch(p, now=change + 300)
    assert fps == [f"config_breakage:checks.spill_wrap_cmd:{change}"]
    ev = _config_events(p, "config_breakage")
    assert len(ev) == 1 and ev[0]["status"] == "queued"
    text = ev[0]["text"]
    for part in ("checks.spill_wrap_cmd", '"env A=1 /opt/example/wrap.sh" -> "/opt/example/wrap.sh"',
                 "worker (task #5)", "spill-a, spill-b",
                 "undo: ttp config demo checks.spill_wrap_cmd 'env A=1 /opt/example/wrap.sh'"):
        assert part in text, (part, text)
    assert configwho.watch(p, now=change + 300) == [] and len(_config_events(p, "config_breakage")) == 1, "raised once per change"
    assert configwho.watch(p, now=change + configwho.WATCH_S + 1) == [], "an old change is not watched"


def test_check_runs_record_their_spill_host_and_task(env):
    from ttp import checkwall
    p = make(env)
    checkwall.record_run(p.state, 12.0, False, scratch="", host="spill-a", task="9")
    checkwall.record_run(p.state, 12.0, True, scratch="disk")
    a, b = [json.loads(x) for x in (p.state / checkwall.RUNS_LOG).read_text().splitlines()]
    assert (a["host"], a["task"]) == ("spill-a", "9") and "host" not in b and "task" not in b
