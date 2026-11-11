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
    err = capsys.readouterr().err
    assert "the user or the coordinator makes this change" in err
    assert "`ttp config demo device.max_tasks <value>` in its own shell after `ttp audit why`" in err
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


def test_an_automated_setter_may_turn_an_owners_switch_its_safe_way_only(env, monkeypatch):
    """The guard follows the gates' safe direction: a run may turn a user-set sandbox key off (the
    unsandboxed-runs order) or a SAFE_WHEN_OFF switch to false, never the other way."""
    from ttp import configwho
    _config_clear_run_env(monkeypatch)
    p = make(env)
    worker = configwho.by("worker", task="9")
    for key, on, off in (("sandbox.checks", True, False), ("delivery.never_force", True, False)):
        p.set_config(key, on, by=configwho.by("user"))
        p.set_config(key, off, by=worker)
        assert configwho.at(p.raw_config(), key) == off
        p.set_config(key, off, by=configwho.by("user"))
        with pytest.raises(configwho.Protected):
            p.set_config(key, on, by=worker)
        assert configwho.at(p.raw_config(), key) == off
    p.set_config("sandbox.checks", True, by=configwho.by("user"))
    assert p.unset_config("sandbox.checks", by=configwho.by("upgrade"))   # removing it is off too


def test_a_coordinator_config_set_never_changes_a_value_the_user_set(env, monkeypatch):
    """The user lowered a worker cap at a terminal; the coordinator's config_set raising or lowering it, or
    removing it, is refused naming the user's value and when they set it, logged, and nothing is written;
    the provenance record still names the user. A config_set in a turn carrying the user's own message is
    theirs and passes; the coordinator writing the user's own value changes nothing; a coordinator's own
    value it may still change; its `ttp config` from a run is refused the same way."""
    from ttp import cli, configwho, coordinator as coord
    p = make(env)
    _config_clear_run_env(monkeypatch)
    cli.main(["config", "demo", "budget.max_parallel_workers", "3"])
    key = "budget.max_parallel_workers"
    for value in (4, 2, ""):
        probs = coord.apply(p, [{"type": "config_set", "key": key, "value": value}])
        assert len(probs) == 1 and "a coordinator never changes a value the user set" in probs[0], probs
        assert f"{key} is 3 (set by user via cli, " in probs[0] and "Only the user changes it" in probs[0], probs
        assert p.raw_config()["budget"]["max_parallel_workers"] == 3
        assert configwho.role(configwho.lookup(p.db, key, p.raw_config())) == "user"
    refused = [r for r in configwho.history(p, key) if r.get("refused")]
    assert [r.get("new") for r in refused] == [4, 2, None] and all(r["by"] == "coordinator" for r in refused)
    # A whole-section write reaching the user's key under it is refused too.
    with pytest.raises(configwho.Protected):
        p.set_config("budget", {"max_parallel_workers": 5}, by=configwho.by("coordinator", via="config_set"))
    # Writing the user's own value: nothing written, the user's record stays.
    assert coord.apply(p, [{"type": "config_set", "key": key, "value": 3}]) == []
    assert configwho.role(configwho.lookup(p.db, key, p.raw_config())) == "user"
    # A coordinator run's `ttp config` is refused the same way.
    monkeypatch.setattr(configwho, "from_env", lambda: configwho.by("coordinator", run="88", via="cli"))
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", key, "6"])
    assert p.raw_config()["budget"]["max_parallel_workers"] == 3
    # The user's own message this turn carries their word: the change is theirs.
    mid = p.db.x("INSERT INTO messages(ts, direction, kind, text) VALUES (?, 'in', 'user', 'raise the cap to 4')",
                 (time.time(),))
    assert coord.apply(p, [{"type": "config_set", "key": key, "value": 4}], user_turn=True, messages=[mid]) == []
    rec = configwho.lookup(p.db, key, p.raw_config())
    assert p.raw_config()["budget"]["max_parallel_workers"] == 4 and rec["by"] == "user" and rec["msgs"] == [mid]
    # A harness-posted message is not the user's word.
    sys_mid = p.db.x("INSERT INTO messages(ts, direction, kind, text, provenance) VALUES "
                     "(?, 'in', 'user', 'raise it', 'system')", (time.time(),))
    probs = coord.apply(p, [{"type": "config_set", "key": key, "value": 6}], user_turn=True, messages=[sys_mid])
    assert probs and "a coordinator never changes a value the user set" in probs[0], probs
    # Turning a permission switch off (its safe way) stays anyone's; turning it back on is the user's.
    p.set_config("delivery.code_tasks_may_push", True, by=configwho.by("user", via="cli"))
    p.set_config("delivery.code_tasks_may_push", False, by=configwho.by("coordinator", via="config_set"))
    assert p.raw_config()["delivery"]["code_tasks_may_push"] is False
    p.set_config("delivery.code_tasks_may_push", False, by=configwho.by("user", via="cli"))
    with pytest.raises(configwho.Protected):
        p.set_config("delivery.code_tasks_may_push", True, by=configwho.by("coordinator", via="config_set"))
    with pytest.raises(configwho.Protected):   # removing the user's (unsafe-way) value is a change too
        p.unset_config(key, by=configwho.by("coordinator", via="config_set"))
    # A value the coordinator set it still changes.
    for value in (["codex"], ["claude"]):
        assert coord.apply(p, [{"type": "config_set", "key": "coordinator.provider_failover_to", "value": value}]) == []
    assert p.raw_config()["coordinator"]["provider_failover_to"] == ["claude"]


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


def _breakage_setup(p, key, value, setter, wrap_old="/opt/example/old-wrap.sh"):
    """A watched change of `key` with two spill hosts and this host passing before it; returns (change, put)
    where put(*runs) writes the watch's own run record: (ts offset, passed, host, task, extra fields)."""
    from ttp import configwho
    from ttp.project import durable_append
    if key == "checks.spill_wrap_cmd":
        p.set_config(key, wrap_old)
    p.set_config(key, value, by=setter)
    change = float(configwho.history(p, key)[-1]["ts"])

    def put(*rs):
        (p.state / configwho.RUNS).unlink(missing_ok=True)
        for dt, passed, host, task, extra in [(-900, True, "spill-a", "1", {}), (-850, True, "spill-b", "1", {}),
                                              (-800, True, "", "2", {}), *rs]:
            durable_append(p.state / configwho.RUNS, json.dumps({"ts": change + dt, "passed": passed, "host": host, "task": task,
                                                                   **extra}) + "\n")
    return change, put


def test_config_breakage_counts_only_runs_that_could_have_used_the_spill_setting(env, monkeypatch):
    """Replays a false alarm: checks.spill_wrap_cmd changed, then three local runs of two tasks failed. A
    local run never runs behind the spill wrapper, so no event; nor for spill runs behind another
    wrapper value (the change's digest differs: a check with its own wrapper, or a later change). The
    same failures behind the new value raise it; a non-spill key still counts local runs."""
    from ttp import configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    change, put = _breakage_setup(p, "checks.spill_wrap_cmd", "/opt/example/wrap.sh",
                                  configwho.by("coordinator", via="config_set"))
    put((60, False, "", "3", {}), (90, False, "", "4", {}), (120, False, "", "5", {}))
    assert configwho.watch(p, now=change + 300) == [], "local runs never use the spill wrapper"
    new = configwho.history(p, "checks.spill_wrap_cmd")[-1]["nh"]
    assert new == configwho.digest("/opt/example/wrap.sh")
    put(*[(dt, False, h, t, {"wrap": "own"}) for dt, h, t in ((60, "spill-a", "3"), (90, "spill-b", "4"), (120, "spill-a", "5"))])
    assert configwho.watch(p, now=change + 300) == [], "every check ran behind its own wrapper"
    put(*[(dt, False, h, t, {"wrap": new}) for dt, h, t in ((60, "spill-a", "3"), (90, "spill-b", "4"), (120, "spill-a", "5"))])
    assert configwho.watch(p, now=change + 300) == [f"config_breakage:checks.spill_wrap_cmd:{change}"]
    assert "3 check runs of 3 tasks failed on spill-a, spill-b" in _config_events(p, "config_breakage")[0]["text"]

    p.set_config("checks.timeout_s", 99, by=configwho.by("coordinator", via="config_set"))
    other = float(configwho.history(p, "checks.timeout_s")[-1]["ts"])
    from ttp.project import durable_append
    for dt, t in ((60, "6"), (90, "7"), (120, "8")):
        durable_append(p.state / configwho.RUNS, json.dumps({"ts": other + dt, "passed": False, "task": t}) + "\n")
    assert configwho.watch(p, now=other + 300) == [f"config_breakage:checks.timeout_s:{other}"], "any key: local runs count"


def test_config_breakage_skips_failures_known_on_the_base_flaky_or_proven_new(env, monkeypatch):
    """Failed runs whose failing ids also failed on a base on the same host (the push-branch tip the gate
    reran), are quarantined as flaky, or that the gate proved new against the base at the same config,
    never blame a change; one unknown failing id in a run does."""
    from ttp import configwho, gatejudge
    p = make(env)
    _config_clear_run_env(monkeypatch)
    change, put = _breakage_setup(p, "checks.timeout_s", 99, configwho.by("coordinator", via="config_set"))
    known, flaky = "tests/test_a.py::test_old", "tests/test_a.py::test_flaky"
    gatejudge.save_base(p.state, gatejudge.host(), "abc123", {known: True, "tests/test_a.py::test_fine": False}, change)
    gatejudge.quarantine(p.state, [flaky], {"why": "test"}, change)
    put((60, False, "", "3", {"fails": [known]}), (90, False, "", "4", {"fails": [known, flaky]}),
        (120, False, "", "5", {"fails": [flaky]}))
    assert configwho.watch(p, now=change + 300) == [], "every failure known without the change"
    put((60, False, "", "3", {"proven": True}), (90, False, "", "4", {"proven": True}), (120, False, "", "5", {"proven": True}))
    assert configwho.watch(p, now=change + 300) == [], "proven new against the base at the same config: the code"
    gatejudge.save_base(p.state, "spill-a", "abc123", {"tests/test_a.py::test_spill": True}, change)
    put((60, False, "", "3", {"fails": ["tests/test_a.py::test_spill"]}), (90, False, "", "4", {"fails": [known]}),
        (120, False, "", "5", {"fails": [known]}))
    assert configwho.watch(p, now=change + 300) == [], "another host's base never counts, so 1 run is too few"
    put((60, False, "", "3", {"fails": [known, "tests/test_a.py::test_fine"]}), (90, False, "", "4", {"fails": [known]}),
        (120, False, "", "5", {}), (150, False, "", "5", {"fails": ["tests/test_a.py::test_new"]}))
    assert configwho.watch(p, now=change + 300) == [f"config_breakage:checks.timeout_s:{change}"]
    assert "3 check runs of 2 tasks" in _config_events(p, "config_breakage")[0]["text"]


def test_config_breakage_of_a_user_set_value_names_the_user_and_gives_no_undo(env, monkeypatch):
    """A value the user set: the event still reports the numbers, says the user set it and to look for
    the other cause, and carries no undo command for it or for user changes in its 'also changed' list."""
    from ttp import configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    p.set_config("checks.retries", 2, by=configwho.by("user", msgs=[12], via="chat"))
    change, put = _breakage_setup(p, "checks.timeout_s", 99, configwho.by("user", msgs=[14], via="chat"))
    put((60, False, "", "3", {}), (90, False, "", "4", {}), (120, False, "", "5", {}))
    assert configwho.watch(p, now=change + 300) == [f"config_breakage:checks.timeout_s:{change}"]
    text = _config_events(p, "config_breakage")[0]["text"]
    assert "3 check runs of 3 tasks failed on this host" in text and "user (message #14)" in text
    assert "The user set this value" in text and "Undo it" not in text and "undo:" not in text, text


def test_check_runs_keep_failing_ids_proof_and_wrapper_for_the_breakage_watch(env):
    from ttp import checkwall, configwho
    p = make(env)
    checkwall.record_run(p.state, 12.0, False, host="spill-a", task="9", fails=["t::a"], proven=True, wrap="d1")
    checkwall.record_run(p.state, 12.0, True, fails=["t::b"], proven=True, wrap="d2")
    a, b = configwho._rows(p.state / configwho.RUNS)
    assert (a["fails"], a["proven"], a["wrap"]) == (["t::a"], True, "d1")
    assert not {"fails", "proven", "wrap"} & set(b), "a pass keeps none; a local run no wrapper"


def test_check_runs_record_their_spill_host_and_task(env):
    from ttp import checkwall
    p = make(env)
    checkwall.record_run(p.state, 12.0, False, scratch="", host="spill-a", task="9")
    checkwall.record_run(p.state, 12.0, True, scratch="disk")
    a, b = [json.loads(x) for x in (p.state / checkwall.RUNS_LOG).read_text().splitlines()]
    assert (a["host"], a["task"]) == ("spill-a", "9") and "host" not in b and "task" not in b


def test_a_worker_runs_config_of_a_coordinator_set_spill_wrap_cmd_is_refused_and_recorded(env, monkeypatch, capsys):
    """Replays the lost pinned env: a coordinator set checks.spill_wrap_cmd to a wrapper plus an env part;
    a worker run's `ttp config` of the wrapper alone is refused, the value stays whole, the refusal is
    logged with the worker's task and run, the coordinator gets one event, and the worker is told how to
    propose the change instead."""
    from ttp import cli, configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    whole = "/opt/example/spill-wrap.sh env PATH=/opt/example/venv/bin:/usr/bin PYTHONNOUSERSITE=1"
    monkeypatch.setenv("TTP_RUN_ID", "15290")   # the coordinator's own run: `ttp config` from its shell
    cli.main(["config", "demo", "checks.spill_wrap_cmd", whole])
    assert configwho.history(p, "checks.spill_wrap_cmd")[-1]["by"] == "coordinator"
    monkeypatch.setenv("TTP_RUN_ID", "15316")   # a worker run of task 7492
    monkeypatch.setenv("TTP_TASK_ID", "7492")
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "checks.spill_wrap_cmd", "/opt/example/spill-wrap.sh"])
    err = capsys.readouterr().err
    assert "worker may not change" in err and "propose it in your hand-off" in err
    assert p.raw_config()["checks"]["spill_wrap_cmd"] == whole
    assert configwho.role(configwho.lookup(p.db, "checks.spill_wrap_cmd", p.raw_config())) == "coordinator"
    refused = [r for r in configwho.history(p, "checks.spill_wrap_cmd") if r.get("refused")]
    assert len(refused) == 1 and (refused[0]["by"], refused[0]["task"], refused[0]["run"]) == ("worker", 7492, "15316")
    assert (refused[0]["old"], refused[0]["new"]) == (whole, "/opt/example/spill-wrap.sh")
    events = _config_events(p, "config_overwrite_refused")
    assert [e["fingerprint"] for e in events] == ["config_overwrite_refused:checks.spill_wrap_cmd:worker"]
    assert events[0]["status"] == "queued" and "set by" in events[0]["text"]



def test_config_set_refusing_a_key_a_coordinator_sets_by_cli_names_that_route_and_a_users_key_does_not(env):
    """A known key config_set refuses (not chat-settable) but a coordinator may set by `ttp config`: the
    rejection names that route, so a worker told to hand it to the coordinator never loops. A key that is
    the user's alone (NEEDS_USER) and an unknown key get no such route."""
    from ttp.coordinator import apply
    p = make(env)
    probs = apply(p, [{"type": "config_set", "key": "checks.spill_wrap_cmd", "value": "/opt/example/wrap.sh"}])
    assert any("checks.spill_wrap_cmd is not user-settable from chat" in x
               and "`ttp config <project> checks.spill_wrap_cmd <value>` in its own shell after `ttp audit "
                   "why`" in x for x in probs), probs
    assert "spill_wrap_cmd" not in (p.raw_config().get("checks") or {})
    for key in ("delivery.backup_remote", "checks.no_such_key"):
        probs = apply(p, [{"type": "config_set", "key": key, "value": "x"}])
        assert probs and not any("ttp audit why" in x for x in probs), (key, probs)

def test_config_breakage_watch_fires_when_the_passes_before_the_change_are_older_than_recent_rows(env, monkeypatch):
    """Many spill hosts: the passes of the failing hosts before a change are more than checkwall.RECENT
    rows back, and RUNS_LOG has been trimmed to its last RECENT rows; the watch's own record still has
    each host's last pass, so the alarm fires once."""
    import time as _time
    from ttp import checkwall, configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    monkeypatch.setattr(checkwall, "RUNS_MAX_BYTES", 1)   # RUNS_LOG trims to RECENT rows on every run
    monkeypatch.setattr(configwho, "RUNS_MAX_BYTES", 1)   # and the watch's record trims on every run too
    clock = [_time.time() - 4000 - configwho.WATCH_S]
    monkeypatch.setattr(_time, "time", lambda: clock[0])

    def run(passed, host, task):
        clock[0] += 10
        checkwall.record_run(p.state, 60.0, passed, host=host, task=task)

    run(True, "spill-a", "1")
    run(True, "spill-b", "2")
    for i in range(checkwall.RECENT + 5):
        run(True, f"spill-{i % 7 + 10}", str(100 + i))
    clock[0] += configwho.WATCH_S   # the passes before the change are older than the record's window
    p.set_config("checks.spill_wrap_cmd", "/opt/example/wrap.sh", by=configwho.by("worker", task=5, via="cli"))
    change = float(configwho.history(p, "checks.spill_wrap_cmd")[-1]["ts"])
    for task, host in (("3", "spill-a"), ("4", "spill-b"), ("5", "spill-a")):
        run(False, host, task)
        for i in range(checkwall.RECENT):
            run(True, "", str(200 + i))   # local reruns after each spill failure
    assert len((p.state / checkwall.RUNS_LOG).read_text().splitlines()) <= checkwall.RECENT + 1
    kept = (p.state / configwho.RUNS).read_text().splitlines()
    assert len(kept) == 3 * (checkwall.RECENT + 1) + 2 + 7, "the runs since the window plus each host's last pass"
    fps = configwho.watch(p, now=clock[0])
    assert fps == [f"config_breakage:checks.spill_wrap_cmd:{change}"]
    assert configwho.watch(p, now=clock[0]) == [] and len(_config_events(p, "config_breakage")) == 1
    assert "spill-a, spill-b" in _config_events(p, "config_breakage")[0]["text"]


def test_a_worker_runs_config_removal_of_a_coordinator_set_value_ends_cleanly(env, monkeypatch, capsys):
    """`ttp config NAME KEY ''` from a worker run over a coordinator's value: the guard's refusal ends the
    command with its message (no traceback) and the propose-it text, and the value stays."""
    from ttp import cli, configwho
    p = make(env)
    _config_clear_run_env(monkeypatch)
    monkeypatch.setenv("TTP_RUN_ID", "15290")
    cli.main(["config", "demo", "checks.spill_wrap_cmd", "/opt/example/spill-wrap.sh env A=1"])
    monkeypatch.setenv("TTP_RUN_ID", "15316")
    monkeypatch.setenv("TTP_TASK_ID", "7492")
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "demo", "checks.spill_wrap_cmd", ""])
    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "worker may not change" in err and "propose it in your hand-off" in err and "Traceback" not in err
    assert p.raw_config()["checks"]["spill_wrap_cmd"] == "/opt/example/spill-wrap.sh env A=1"


@needs_sockets
def test_the_web_app_serves_without_saving_its_port_over_an_owners_falsy_web_port(env):
    """An owner set web.port to 0: the web app's default port save is refused by the guard; it still
    binds a port and serves, and the owner's value stays."""
    from ttp import configwho, web
    p = make(env)
    p.set_config("web.port", 0, by=configwho.by("user", via="cli"))

    class Stub:
        pass
    stub = Stub()
    stub.p = p
    httpd = web.bind(stub)
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()
    assert p.raw_config()["web"]["port"] == 0
    assert configwho.role(configwho.lookup(p.db, "web.port", p.raw_config())) == "user"


def test_a_relayer_created_project_takes_agent_orders_as_the_owners_with_its_setter_recorded(env, monkeypatch, capsys):
    """`ttp new --relayer` (the relayer skill creates projects with it) turns coordinator.agent_orders_as_user
    on, recorded as the owner's (their agent created it), so an upgrade's rewrite leaves it theirs; a project made
    without it keeps the default (off). A value the user set later stays theirs."""
    from ttp import cli, configwho, relay
    _config_clear_run_env(monkeypatch)
    plain = make(env)
    assert not plain.config()["coordinator"].get("agent_orders_as_user")
    assert "coordinator" not in plain.raw_config()
    root = env["tmp"] / "relayed"
    shutil.copytree(env["repo"], root, symlinks=True, ignore=shutil.ignore_patterns("tt-project"))
    q = cli.bootstrap(root, "relayed", "A relayed brief.", "fake", relayer=True)
    assert q.raw_config()["coordinator"] == {"agent_orders_as_user": True}
    assert q.config()["coordinator"]["agent_orders_as_user"] is True
    rec = configwho.lookup(q.db, relay.AGENT_KEY, q.raw_config())
    assert configwho.role(rec) == "user" and "--relayer" in rec.get("via", "")
    q.set_config(relay.AGENT_KEY, True, by=configwho.by("upgrade", via="upgrade"))   # an upgrade's rewrite
    assert configwho.role(configwho.lookup(q.db, relay.AGENT_KEY, q.raw_config())) == "user", "it stays the owner's"
    q.set_config(relay.AGENT_KEY, False, by=configwho.by("user", via="cli"))
    assert q.config()["coordinator"]["agent_orders_as_user"] is False
    assert cli.bootstrap(root, "relayed", "again", "fake", relayer=True).config()["coordinator"][
        "agent_orders_as_user"] is False   # an existing project is never re-created over
    with pytest.raises(SystemExit):
        cli.main(["new", "--help"])
    assert "--relayer" in capsys.readouterr().out
