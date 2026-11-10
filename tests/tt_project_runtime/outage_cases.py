# Machine-ledger area: outage hand-offs as tracked tasks (outagetask.py). A long outage whose recovery owner
# cannot act, or records no change-ledger entry within heal.outage_owner_budget_min, becomes a task with a due
# time in the fallback (else a detecting project); overdue, one high page to its project, then one normal
# user alert. Exec'd into test_tt_project_runtime.py: make, _inbox and its imports are in scope.


def _outage_box(env, home="beta", **boxes):
    """Project `home` (the routing daemon's) and each box alias -> (owner, fallback): (p, ml)."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env, home)
    for alias, (owner, fallback) in (boxes or {"box-a": ("owner", "beta")}).items():
        mm.add(alias, tags="device")
        mm.set_recovery(alias, owner=owner, fallback=fallback)
    return p, ml


def _no_quiet(c, now, doc):
    return "", False


def _route(ml, at, able, **kw):
    """Route once at `at`, every page delivered: [(alias, kind, target, severity)]."""
    out = []
    for x in ml.route(at, able=lambda n: able.get(n, (False, "not a project of this user")),
                      healthy=lambda *a: [], quiet=_no_quiet, **kw):
        ml.delivered(x)
        out.append((x["alias"], x["kind"], x["target"], x.get("severity")))
    return out


def _tasks(pages):
    return [(a, t) for a, k, t, _ in pages if k == "machine_outage_task"]


def test_an_owner_that_cannot_act_gets_an_outage_task_in_the_fallback_else_the_detector(env):
    """(a) At the first routing past the grace: the task goes to the fallback when it can act, else to the
    detecting project (the co-tenant that reported the condition)."""
    p, ml = _outage_box(env, **{"box-a": ("owner", "beta"), "box-b": ("owner", "delta")})
    t0 = time.time() - 3600
    for alias in ("box-a", "box-b"):
        ml.report("gamma", alias, "down", "no answer to ssh", now=t0)
    able = {"owner": (False, "it is paused"), "beta": (True, ""), "gamma": (True, ""), "delta": (False, "stopped")}
    pages = _route(ml, t0 + ml.GRACE_S, able)
    assert sorted(_tasks(pages)) == [("box-a", "beta"), ("box-b", "gamma")], pages
    assert _tasks(_route(ml, t0 + ml.GRACE_S + 600, able)) == [], "queued once per episode"


def test_an_owner_that_records_no_change_in_its_budget_gets_an_outage_task(env):
    """(b) The owner can act: no task until heal.outage_owner_budget_min (60) from the outage's start; then
    one, unless a change-ledger entry on the machine was recorded since the outage began."""
    from ttp import outagetask
    p, ml = _outage_box(env, **{"box-a": ("owner", "beta"), "box-b": ("owner", "beta")})
    assert outagetask.budgets(None)["owner_s"] == 3600 == outagetask.budgets({"heal": {}})["owner_s"]
    t0 = time.time() - 4 * 3600
    ml.add_change("box-a", "an older drop-in", "rm it", "owner", expires="2d", now=t0 - 600)
    able = {"owner": (True, ""), "beta": (True, "")}
    for alias in ("box-a", "box-b"):
        ml.report("gamma", alias, "held", now=t0)
    got = []
    for m in range(30, 91, 5):
        at = t0 + m * 60
        for alias in ("box-a", "box-b"):
            ml.report("gamma", alias, "held", now=at)
        if m == 40:
            ml.add_change("box-b", "power-cycled the board", "none", "owner", expires="2d", now=at)
        got += [(m, a, t) for a, t in _tasks(_route(ml, at, able))]
    assert got == [(60, "box-a", "beta")], "an older change counts for no later outage; box-b's owner acted"
    # A shorter budget from the routing project's config.
    from ttp import machines as mm
    mm.add("box-c", tags="device")
    mm.set_recovery("box-c", owner="owner", fallback="beta")
    ml.report("gamma", "box-c", "held", now=t0)
    short = outagetask.budgets({"heal": {"outage_owner_budget_min": 40}})
    assert short["owner_s"] == 2400
    assert _tasks(_route(ml, t0 + 39 * 60, able, budgets=short)) == []
    assert _tasks(_route(ml, t0 + 40 * 60, able, budgets=short)) == [("box-c", "beta")]


def test_outage_budget_settings_are_validated_and_a_bad_one_keeps_the_default(env):
    """(3) heal.outage_*_min: generic defaults 60/30/30; a value outside 1 minute to a week (or not a number)
    is a config problem, refused by `ttp config`, and budgets() keeps the default for it."""
    from ttp import cli, outagetask
    from ttp.project import config_problems, unknown_key_hint
    p = make(env, "beta")
    assert outagetask.budgets(None) == {"owner_s": 3600.0, "due_s": 1800.0, "escalate_s": 1800.0}
    for name, _ in outagetask.KEYS.values():
        assert unknown_key_hint(f"heal.{name}") is None, name
    cli.main(["config", p.name, "heal.outage_owner_budget_min", "45"])
    assert p.config()["heal"]["outage_owner_budget_min"] == 45
    assert config_problems(p.raw_config()) == []
    assert outagetask.budgets(p.config())["owner_s"] == 45 * 60
    for bad in ("0", "soon", "true", str(outagetask.MAX_MIN + 1)):
        with pytest.raises(SystemExit):
            cli.main(["config", p.name, "heal.outage_task_due_min", bad])
    assert "outage_task_due_min" not in p.config()["heal"], "a refused value is not stored"
    raw = {"heal": {"outage_escalate_min": 0, "outage_task_due_min": "30"}}
    assert len(config_problems(raw)) == 2 and all("heal.outage_" in x for x in config_problems(raw))
    assert outagetask.budgets(raw) == outagetask.budgets(None), "an invalid value keeps the default"


def test_an_overdue_outage_task_pages_its_project_high_then_the_user_normal_each_once(env):
    """(c) One high page to the task's project at its due time, one normal user alert heal.outage_escalate_min
    later; neither repeats."""
    p, ml = _outage_box(env)
    t0 = time.time() - 6 * 3600
    able = {"owner": (False, "it is paused"), "beta": (True, "")}
    late = []
    for m in range(0, 241, 10):
        at = t0 + m * 60
        ml.report("gamma", "box-a", "held", now=at)
        late += [(m, t, s) for a, k, t, s in _route(ml, at, able, task_status=lambda proj, lab: "queued")
                 if k == "machine_outage_overdue"]
    assert late == [(60, "beta", "high"), (90, ml.USER, "normal")], late


@pytest.mark.parametrize("status,escalates,listed", [("cancelled", False, False), ("done", False, False),
                                                     ("failed", True, True), ("queued", True, True),
                                                     (None, True, None)])
def test_one_closed_state_rule_for_outage_escalation_and_the_open_lists(env, status, escalates, listed):
    """Done and cancelled outage tasks are closed: no overdue page, not listed open. A failed one stays open:
    listed in status, web and sweep, and it escalates. An unreadable state (None) escalates."""
    from ttp import outagetask, sweep, web
    p, ml = _outage_box(env)
    t0 = time.time() - 7200
    ml.report("gamma", "box-a", "down", now=t0)
    able = {"owner": (False, "it is paused"), "beta": (True, "")}
    kw = dict(task_status=lambda proj, lab: status)
    assert _tasks(_route(ml, t0 + ml.GRACE_S, able, **kw)) == [("box-a", "beta")]
    late = [k for a, k, t, s in _route(ml, t0 + ml.GRACE_S + 1860, able, **kw)]
    assert ("machine_outage_overdue" in late) is escalates, late
    assert outagetask.closed(status) is (not escalates)
    if listed is None:
        return
    now = time.time()
    rec = {"alias": "box-a", "condition": "down", "first": t0, "due": t0 + 3600, "why": "w", "tried": "t",
           "seen_by": ["gamma"], "label": f"outage:box-a:down@{int(t0)}"}
    tid = outagetask.ensure(p.db, rec, p, now)
    p.db.update_task(tid, status=status)
    assert ([t["id"] for t in outagetask.open_tasks(p.db)] == [tid]) is listed
    assert any(f"outage #{tid}" in x for x in web.health(p, p.db, now=now)["outages"]) is listed
    assert any(i["key"] == f"task:{tid}" and i["kind"] == "outage" for i in sweep.items(p.db, now)) is listed


def test_every_outage_hand_off_path_creates_a_task_row_never_only_a_note(env):
    """(d) The routing daemon's own project: Daemon._machine_page creates the task row (outagetask.ensure),
    once, with no note. Another project: a daemon note carrying the outage record, and the target's daemon
    files the task on reading it (upstream._file). A worker's note with such a record files none."""
    from ttp import upstream
    from ttp.daemon import Daemon
    p, ml = _outage_box(env)
    t0 = time.time() - 3600
    ml.report("gamma", "box-a", "down", "no answer", now=t0)
    page = next(x for x in ml.route(t0 + ml.GRACE_S, able=lambda n: (n == "beta", "it is paused"),
                                    healthy=lambda *a: [], quiet=_no_quiet) if x["kind"] == "machine_outage_task")
    assert page["target"] == "beta" and page["outage"]["label"] == f"outage:box-a:down@{int(t0)}"
    d = Daemon(p.base)
    assert d._machine_page(page, t0 + ml.GRACE_S) and d._machine_page(page, t0 + ml.GRACE_S)
    rows = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%outage:%'")
    assert len(rows) == 1 and rows[0]["status"] == "queued", rows
    assert json.loads(rows[0]["labels"]) == [page["outage"]["label"], f"due:{int(page['outage']['due'])}"]
    assert "owner owner cannot act: it is paused" in rows[0]["spec"]
    assert _inbox() == [], "at home: the task, no note"
    # Another project: the note carries the record; its daemon files the task when it reads the note.
    assert d._machine_page({**page, "target": "delta"}, t0 + ml.GRACE_S)
    notes = _inbox()
    assert [(n["to"], n["from"]) for n in notes] == [("delta", "daemon")] and notes[0]["outage"] == page["outage"]
    p.db.x("DELETE FROM tasks")
    rec = {**page["outage"], "label": f"outage:box-a:down@{int(t0) + 1}"}
    for _ in range(2):
        assert upstream.send("gamma", None, "beta", "an outage task", "high", sender="daemon",
                             extra={"outage": rec}) in ("sent", "duplicate")
    assert upstream.send("gamma", 1, "beta", "a worker", "normal", extra={"outage": {**rec, "label": "outage:x:y@1"}})
    upstream.ingest(p, p.config())
    upstream.ingest(p, p.config())
    rows = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%outage:%'")
    assert [json.loads(r["labels"])[0] for r in rows] == [rec["label"]], "one task; a worker's note files none"


def test_outage_replay_held_box_paused_owner_muted_watcher_reaches_a_task_in_time(env, monkeypatch):
    """(f) Incident replay: a box held for 4 h, its owner paused, the watcher that sees it muted. The routing
    project's daemon files the task within budgets()['due_s'] of the grace, the overdue page at its due time
    goes to its coordinator as a tricky turn, and the user's alert follows once."""
    from ttp import coordinator as coord
    from ttp import daemon as dm
    from ttp import outagetask
    from ttp import screen as scr
    p, ml = _outage_box(env)
    b = outagetask.budgets(p.config())
    clock = {"t": time.time() - 5 * 3600}
    t0 = clock["t"]
    monkeypatch.setattr(scr.time, "time", lambda: clock["t"])
    held = json.dumps({"text": "box-a: held by the broker", "severity": "high", "repeat": True,
                       "machine": "box-a", "condition": "held"})
    monkeypatch.setattr(dm.schedrun, "run_group", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, held, ""))
    d = dm.Daemon(p.base)
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert coord.apply(p, [{"type": "observation_mute", "source": "watcher:hw", "match": "held",
                            "hours": 24, "why": "known"}]) == []
    seen = []
    for m in range(0, 241, 10):
        clock["t"] = t0 + m * 60
        d._run_command_watcher({"name": "hw"}, {"command": "x"})
        for x in ml.route(clock["t"], able=lambda n: (n != "owner", "it is paused"), healthy=lambda *a: [],
                          quiet=_no_quiet, budgets=b):
            assert d._machine_page(x, clock["t"])
            ml.delivered(x)
            seen.append((m, x["target"], x["kind"]))
    assert scr.mutes(p.db, clock["t"]), "the watcher stayed muted throughout"
    got = [s for s in seen if s[2] != "machine_condition"]
    assert got == [(30, "beta", "machine_outage_task"), (60, "beta", "machine_outage_overdue"),
                   (90, ml.USER, "machine_outage_overdue")], seen
    assert got[0][0] * 60 <= ml.GRACE_S + b["due_s"]
    t = p.db.one("SELECT * FROM tasks WHERE labels LIKE '%outage:box-a:held%'")
    assert t and t["status"] == "queued" and t["created"] - t0 <= ml.GRACE_S + b["due_s"]
    ev = p.db.one("SELECT * FROM events WHERE kind='machine_outage_overdue'")
    assert ev["severity"] == "high" and coord.EFFORT_EVENT_TRIGGERS["machine_outage_overdue"] == "resource"
    alerts = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'outage:box-a:held@%#user'")
    assert [a["severity"] for a in alerts] == ["normal"]


def test_open_outage_tasks_show_in_status_web_and_the_sweep(env):
    """(g) Smoke: the outage line in `ttp status` and the web health, and the sweep's outage item: listed
    'OUTAGE TASK, NOT DUE YET' before its due time, OVERDUE after; one item when the task is also stuck."""
    from ttp import cli, outagetask, sweep, web
    p = make(env)
    now = time.time()
    rec = {"alias": "box-a", "condition": "held", "first": now - 5400, "due": now - 1200, "why": "w",
           "tried": "t", "seen_by": ["gamma"], "label": f"outage:box-a:held@{int(now - 5400)}"}
    tid = outagetask.ensure(p.db, rec, p, now)
    assert f"outage #{tid} box-a held for 1.5 h, task queued" in cli.status_text(p)
    assert any(f"outage #{tid} box-a held" in x and "OVERDUE" in x for x in web.health(p, p.db, now=now)["outages"])
    late = next(i for i in sweep.items(p.db, now) if i["key"] == f"task:{tid}")
    assert late["kind"] == "outage" and "OVERDUE" in late["reason"] and late["ends"] is None
    early = next(i for i in sweep.items(p.db, now - 1500) if i["key"] == f"task:{tid}")
    assert "OVERDUE" not in early["reason"] and early["ends"] == "due"
    assert "OUTAGE TASK, NOT DUE YET" in sweep._line(early, {}, {}, now - 1500)
    p.db.update_task(tid, status="blocked")
    stuck = [i for i in sweep.items(p.db, now) if i["task"] == tid]
    assert len(stuck) == 1 and stuck[0]["kind"] == "outage" and "OVERDUE" in stuck[0]["reason"], stuck


def test_known_outages_keep_one_entry_per_project_on_a_machine(env, capsys):
    """[note 5c3a02eaf81d7794] Two projects' outages on one machine both stay: setting one again changes only
    that project's entry, clearing one leaves the other in force, and the list, status and paging name each."""
    import json as _json
    from ttp.cli import main
    alpha, ml, able = _quiet_box(env)
    main(["machines", "outage", "box-a", "tray swap", "--expires", "1d", "--by", "alpha", "--covers", "tray"])
    main(["machines", "outage", "box-a", "fan repair", "--expires", "3d", "--by", "gamma", "--owner", "a ticket"])
    out = capsys.readouterr().out.splitlines()[-1]
    assert "known outages from 2 projects" in out and "by alpha; covers: tray): tray swap" in out
    assert "by gamma; owner: a ticket): fan repair" in out
    main(["machines", "outage", "box-a", "tray swap, second try", "--expires", "2d", "--by", "alpha"])
    assert "by gamma; owner: a ticket): fan repair" in capsys.readouterr().out
    got = ml.outage_entries()["box-a"]
    assert sorted(got) == ["alpha", "gamma"] and got["alpha"]["why"] == "tray swap, second try"
    assert got["gamma"]["why"] == "fan repair" and "covers" not in got["alpha"]
    o = ml.outages()["box-a"]
    assert o["until"] == got["gamma"]["until"] and o["owner"] == "a ticket" and "covers" not in o
    main(["machines", "outage", "box-a", "--json"])
    assert [e["by"] for e in _json.loads(capsys.readouterr().out)["box-a"]["entries"]] == ["gamma", "alpha"]
    main(["machines", "status", "box-a"])
    out = capsys.readouterr().out
    assert "tray swap, second try" in out and "fan repair" in out and "owner: a ticket" in out
    t0 = time.time()
    ml.report("delta", "box-a", "down", now=t0)
    assert _walk(ml, able, t0 + 3600) == []
    assert "gamma: fan repair; alpha: tray swap, second try" in ml.conditions()["box-a:down"]["page"]["quiet"]
    # Clearing is per project too: another project's entry is not touched without --by.
    main(["machines", "outage", "box-a", "--clear", "--by", "delta"])
    assert "no known outage from delta; open from alpha, gamma" in capsys.readouterr().out
    main(["machines", "outage", "box-a", "--clear", "--by", "alpha"])
    assert "alpha's known outage cleared; still out" in capsys.readouterr().out
    assert sorted(ml.outage_entries()["box-a"]) == ["gamma"]
    assert ml.outage_line("box-a", ml.outages()["box-a"]).startswith("box-a: known outage until")


def test_one_projects_outage_expiring_leaves_the_others_in_force(env):
    alpha, ml, able = _quiet_box(env)
    t0 = time.time()
    ml.set_outage("box-a", "short repair", "alpha", "2h", now=t0)
    ml.set_outage("box-a", "long repair", "gamma", "2d", now=t0, covers="fabric")
    later = t0 + 3 * 3600
    assert sorted(ml.outage_entries(later)["box-a"]) == ["gamma"]
    o = ml.outages(later)["box-a"]
    assert o["why"] == "long repair" and o["covers"] == ["fabric"]
    ml.report("delta", "box-a", "down", now=later)
    assert ml.quiet_reason(ml.conditions()["box-a:down"], later)[1], "still a known outage"
    assert "box-a" not in ml.outages(t0 + 3 * 86400), "out only while an entry is open"
    # Expired entries leave the file at the next write, and a new entry starts from the open ones only.
    ml.set_outage("box-a", "next repair", "alpha", "1d", now=later)
    assert sorted(ml._read(ml.ROUTES, {})[ml.OUTAGE_ENTRIES]["box-a"]) == ["alpha", "gamma"]


def test_a_single_outage_record_from_an_older_runtime_migrates_without_loss(env):
    """An older runtime (or state from before entries) keeps one record per machine under "outages": it is
    read as its project's entry, kept when another project adds its own, and an older runtime overwriting or
    clearing the machine's record still counts."""
    alpha, ml, able = _quiet_box(env)
    mm = __import__("ttp.machines", fromlist=["add"])
    mm.add("box-b", tags="device")
    t0 = time.time()
    old = {"why": "board swap", "owner": "", "by": "gamma", "since": t0, "until": t0 + 86400}
    with ml._locked():
        ml._write(ml.ROUTES, {"conditions": {}, "outages": {"box-a": dict(old), "box-b": dict(old, by="")}})
    assert ml.outage_entries(t0)["box-a"] == {"gamma": old} and list(ml.outage_entries(t0)["box-b"]) == ["user"]
    assert ml.outages(t0)["box-a"]["why"] == "board swap"
    ml.set_outage("box-a", "fan repair", "alpha", "2d", now=t0 + 60)
    doc = ml._read(ml.ROUTES, {})
    assert sorted(doc[ml.OUTAGE_ENTRIES]["box-a"]) == ["alpha", "gamma"]
    assert doc["outages"]["box-a"]["until"] == t0 + 60 + 2 * 86400, "older readers see the merged record"
    # An older runtime overwrites the machine's record: that becomes its project's entry, the others stay.
    with ml._locked():
        doc = ml._read(ml.ROUTES, {})
        doc["outages"]["box-a"] = dict(old, why="board swap, part two", since=t0 + 120)
        ml._write(ml.ROUTES, doc)
    got = ml.outage_entries(t0 + 180)["box-a"]
    assert got["gamma"]["why"] == "board swap, part two" and got["alpha"]["why"] == "fan repair"
    # An older runtime clears the machine: every entry of it ends, as it meant.
    with ml._locked():
        doc = ml._read(ml.ROUTES, {})
        doc["outages"].pop("box-a")
        ml._write(ml.ROUTES, doc)
    assert "box-a" not in ml.outages(t0 + 180) and "box-b" in ml.outages(t0 + 180)


def test_zoneless_expires_is_read_in_the_home_zone_never_utc():
    """[note 1de9156a8106e61e] A zone-less --expires (date and time, or a date alone) is read in the home zone, in
    winter (PST) and summer (PDT); one with Z or an offset keeps it; a delay is unchanged."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ttp import ends
    la, now = "America/Los_Angeles", 1_790_000_000.0   # 2026-09: both dates below lie ahead, within a year
    utc = lambda s: datetime.fromisoformat(s).timestamp()
    assert ends.parse_expires("2027-01-15T09:00", now, where=la) == utc("2027-01-15T17:00+00:00")   # PST
    assert ends.parse_expires("2027-07-15 09:00", now, where=la) == utc("2027-07-15T16:00+00:00")   # PDT
    assert ends.parse_expires("2027-07-15", now, where=la) == utc("2027-07-15T07:00+00:00")
    assert ends.parse_expires("2027-07-15T09:00Z", now, where=la) == utc("2027-07-15T09:00+00:00")
    assert ends.parse_expires("2027-07-15T09:00-07:00", now, where=la) == utc("2027-07-15T16:00+00:00")
    assert ends.parse_expires("2027-07-15T09:00+0200", now, where=la) == utc("2027-07-15T07:00+00:00")
    assert ends.parse_expires("3h", now, where=la) == now + 3 * 3600
    assert ends.parse_expires("2d", now, where=la) == now + 2 * 86400
    assert ends.parse_expires("2027-01-15T09:00", now, where=ZoneInfo(la)) == utc("2027-01-15T17:00+00:00")
    assert ends.from_action({"expires": "2027-01-15T09:00"}, now, where=la)["expires"] == utc("2027-01-15T17:00+00:00")


def _ahead_at(days: int, zone: str, hm=(15, 30)):
    """A wall time `days` ahead in `zone`: (its zone-less ISO form, its epoch time, its abbreviation)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    d = (datetime.now(ZoneInfo(zone)) + timedelta(days=days)).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return d.strftime("%Y-%m-%dT%H:%M"), d.timestamp(), d.strftime("%Z")


def test_machines_change_and_outage_read_zoneless_expires_in_the_project_home_zone(env, monkeypatch, capsys):
    """[note 1de9156a8106e61e] On a host whose own zone is UTC, a project worker's zone-less `--expires` for
    `machines change add|edit`, `--power-cycle-at` and `machines outage` is read in the project's home zone, and
    the confirmation and `change list` show it there with its abbreviation."""
    from datetime import datetime
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import timefmt
    from ttp.cli import main
    la = "America/Los_Angeles"
    p = make(env, "alpha")
    p.set_config(timefmt.KEY, la)
    mm.add("box-a", tags="device")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(p.root)
    iso, at, abbr = _ahead_at(5, la)
    main(["machines", "change", "add", "box-a", "a drop-in", "--undo", "true", "--expires", iso])
    out = capsys.readouterr().out
    c = ml.changes()[-1]
    assert c["expires"] == at and f"ends {iso[:10]} 15:30 {abbr}" in out
    main(["machines", "change", "list"])
    assert f"15:30 {abbr}" in capsys.readouterr().out
    iso2, at2, abbr2 = _ahead_at(6, la, (8, 0))
    main(["machines", "change", "edit", str(c["id"]), "--expires", iso2])
    assert ml.changes()[-1]["expires"] == at2 and f"08:00 {abbr2}" in capsys.readouterr().out
    main(["machines", "change", "edit", str(c["id"]), "--expires", iso2 + "Z"])
    capsys.readouterr()
    assert ml.changes()[-1]["expires"] == datetime.fromisoformat(iso2 + "+00:00").timestamp()
    iso3, at3, abbr3 = _ahead_at(2, la, (10, 0))
    main(["machines", "change", "add", "box-a", "fan swap", "--power-cycle-at", iso3])
    capsys.readouterr()
    assert ml.changes()[-1]["at"] == at3 and f"10:00 {abbr3}" in ml.changes()[-1]["what"]
    main(["machines", "outage", "box-a", "tray swap", "--expires", iso])
    out = capsys.readouterr().out
    assert ml.outage_entries()["box-a"]["alpha"]["until"] == at and f"15:30 {abbr}" in out
    main(["machines", "outage", "box-a", "tray swap", "--expires", iso[:10] + "T15:30-07:00"])
    capsys.readouterr()
    assert ml.outage_entries()["box-a"]["alpha"]["until"] == datetime.fromisoformat(iso[:10] + "T15:30-07:00").timestamp()


def test_zoneless_expires_with_no_project_uses_the_account_zone_else_the_machines_never_utc(env, monkeypatch,
                                                                                           tmp_path, capsys):
    """[note 1de9156a8106e61e] Run outside any project, a zone-less time is read in the account's budget.timezone,
    else this machine's zone; never UTC by default."""
    import json as _json
    from ttp import ends
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.cli import main
    from ttp.project import account_settings_path
    mm.add("box-a", tags="device")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TTP_TEST_LOCAL_TZ", "Asia/Tokyo")
    iso, at, abbr = _ahead_at(5, "Asia/Tokyo")
    assert ends.parse_expires(iso) == at
    main(["machines", "outage", "box-a", "tray swap", "--expires", iso, "--by", "alpha"])
    assert ml.outage_entries()["box-a"]["alpha"]["until"] == at and f"15:30 {abbr}" in capsys.readouterr().out
    account_settings_path().parent.mkdir(parents=True, exist_ok=True)
    account_settings_path().write_text(_json.dumps({"budget": {"timezone": "Europe/Berlin"}}))
    iso, at, _ = _ahead_at(5, "Europe/Berlin")
    main(["machines", "change", "add", "box-a", "a drop-in", "--undo", "true", "--expires", iso, "--by", "alpha"])
    assert ml.changes()[-1]["expires"] == at


def _change_pages(ml, at):
    """Route once at `at` (everyone can act), every page delivered: [(target, stage)] of overdue changes."""
    out = []
    for x in ml.route(at, able=lambda n: (True, ""), healthy=lambda *a: [], quiet=_no_quiet):
        ml.delivered(x)
        if x["kind"] == "machine_change_overdue":
            out.append((x["target"], x["stage"]))
    return out


def _pause_box(kind, p, ml, now):
    """Pause box-a the `kind` way: a user pause of another project's resource, a machine-wide hold or a known
    outage. Returns (the reason it records, a callable that ends it)."""
    from ttp import coordinator as coord
    from ttp import fleethold
    if kind == "user":
        coord.pause_resource(p, "box-a-device", True, reason="weekend", by="user")
        return "the user paused beta box-a-device", lambda: coord.pause_resource(p, "box-a-device", False, by="user")
    if kind == "hold":
        fleethold.set_hold("box-a", "weekend", "user", "beta", now=now)
        return "held machine-wide", lambda: fleethold.lift("box-a", True, "test")
    ml.set_outage("box-a", "board repair", "beta", "10d", now=now)
    return "known outage until", lambda: ml.clear_outage("box-a")


@pytest.mark.parametrize("kind", ["user", "hold", "outage"])
def test_an_overdue_change_on_a_paused_machine_pages_only_its_project_until_the_pause_ends(env, kind):
    """A user pause (of another project's resource), a machine-wide hold or a known outage: the change's own
    project hears once, nobody past it however long; once it ends the next step waits REPAGE_S from then."""
    p, ml = _outage_box(env)
    t0 = time.time()
    cid = ml.add_change("box-a", "a scratch dir for a repro", "rm -rf /tmp/scratch", "gamma", expires="1h",
                        now=t0)["id"]
    why, end = _pause_box(kind, p, ml, t0)
    due = t0 + 3601
    assert _change_pages(ml, due) == [("gamma", 0)]
    for k in (1, 2, 3):
        assert _change_pages(ml, due + k * ml.REPAGE_S + 1) == [], k
    page = next(c for c in ml.changes() if c["id"] == cid)["page"]
    assert why in page["quiet"] and page["stage"] == 0, page
    end()
    lifted = due + 3 * ml.REPAGE_S + 100
    assert _change_pages(ml, lifted) == [], "not at once"
    page = next(c for c in ml.changes() if c["id"] == cid)["page"]
    assert "quiet" not in page and page["at"] == lifted
    assert _change_pages(ml, lifted + ml.REPAGE_S - 60) == []
    assert _change_pages(ml, lifted + ml.REPAGE_S) == [("owner", 1)]


def test_a_pause_after_the_owner_was_paged_stops_the_next_step_and_its_undelivered_page(env):
    p, ml = _outage_box(env)
    t0 = time.time()
    cid = ml.add_change("box-a", "an ssh config line", "sed -i /scratch/d cfg", "gamma", expires="1h", now=t0)["id"]
    due = t0 + 3601
    assert _change_pages(ml, due) == [("gamma", 0)]
    pages = ml.route(due + ml.REPAGE_S, able=lambda n: (True, ""), healthy=lambda *a: [], quiet=_no_quiet)
    assert [x["target"] for x in pages] == ["owner"]   # claimed, never delivered
    _pause_box("user", p, ml, due)
    later = due + ml.REPAGE_S + ml.CLAIM_S + 60
    assert _change_pages(ml, later) == [] and _change_pages(ml, later + 3 * ml.REPAGE_S) == []
    assert "pending" not in next(c for c in ml.changes() if c["id"] == cid)["page"]


def test_an_overdue_change_on_an_unpaused_machine_escalates_to_the_user(env):
    p, ml = _outage_box(env)
    t0 = time.time()
    ml.add_change("box-a", "stopped the recovery watchdog", "systemctl start watchdog", "gamma", expires="1h", now=t0)
    due = t0 + 3601
    got = [_change_pages(ml, due + k * ml.REPAGE_S) for k in range(4)]
    assert got == [[("gamma", 0)], [("owner", 1)], [("beta", 2)], [(ml.USER, 3)]]   # risky: the user at the end
    ml.add_change("box-a", "a scratch dir for a repro", "rm -rf /tmp/scratch", "gamma", expires="1h", now=t0)
    got = [_change_pages(ml, due + k * ml.REPAGE_S) for k in range(6)]
    assert (ml.USER, 3) not in [x for g in got for x in g]   # harmless: never the user


def _expired(ml, n, by="alpha", what="soak test drop-in", undo="rm -f /tmp/soak.conf", at=None, alias="box-a"):
    """n change entries by `by` on `alias`, each past its end by an hour at `at`: their ids."""
    at = time.time() if at is None else at
    return [ml.add_change(alias, f"{what} {i}", undo, by, expires="1h", now=at - 7200)["id"] for i in range(n)]


def _all_able(n):
    return True, ""


def test_expired_changes_of_one_project_reach_its_coordinator_as_one_item_and_never_the_user(env, monkeypatch):
    """(a) N expired entries of one project: one queued coordinator event listing each with renew, close or
    undo; the others' fingerprints are filed handled, so a resend wakes nobody; the user hears nothing."""
    from ttp.daemon import Daemon
    alpha, ml = _outage_box(env, "alpha", **{"box-a": ("owner", "beta")})
    ids = _expired(ml, 3)
    d = Daemon(alpha.base)
    told = []
    monkeypatch.setattr(d, "alert", lambda *a, **k: told.append(a))
    d.route_machines()
    rows = alpha.db.q("SELECT kind, status, text FROM events WHERE kind='machine_change_overdue' ORDER BY id")
    queued = [r for r in rows if r["status"] == "queued"]
    assert len(queued) == 1 and len(rows) == 3 and all(r["status"] == "handled" for r in rows[1:])
    text = queued[0]["text"]
    assert all(f"#{i} on box-a" in text for i in ids) and "rm -f /tmp/soak.conf" in text
    assert "renew it" in text and "close it" in text and "undo it" in text and "no word" in text
    assert "Outage" not in text and "Acting now" not in text and told == []
    # The same records sent again (a crash before they were marked, or a later step's resend): nothing new.
    assert ml.route(time.time(), able=_all_able, healthy=lambda *a: [], quiet=_no_quiet) == []
    fps = [r["fingerprint"] for r in alpha.db.q("SELECT fingerprint FROM events WHERE kind='machine_change_overdue' "
                                                 "ORDER BY id")]
    again = [{"target": "alpha", "kind": "machine_change_overdue", "key": f"machine-change:{i}", "alias": "box-a",
              "fp": fp, "line": f"#{i}", "change": i, "text": "x", "id": fp} for i, fp in zip(ids, fps)]
    assert d._machine_pages(again, time.time())
    assert alpha.db.one("SELECT COUNT(*) n FROM events WHERE kind='machine_change_overdue'")["n"] == 3


def test_a_harmless_expired_change_never_pages_the_user(env):
    """(b) An entry the risk rule calls harmless, nobody acting on it for days: it stays with the projects."""
    p, ml = _outage_box(env, "beta", **{"box-a": ("owner", "beta")})
    t0 = time.time()
    _expired(ml, 1, by="beta", at=t0)
    assert ml.change_risk(ml.changes(open_only=False)[0], {}) == ""
    able = {"beta": (False, "its daemon is stopped"), "owner": (False, "its daemon is stopped")}
    seen = []
    for h in range(0, 24 * 6):
        seen += _route(ml, t0 + h * 3600, able)
    assert seen and ml.USER not in [t for _, _, t, _ in seen]


def test_a_risky_expired_change_unhandled_for_a_day_pages_the_user_once(env):
    """(c) An entry that stopped a recovery service: after its projects let 24 h pass, the user once, as a
    risky change (no outage wording); never before 24 h, and never again."""
    p, ml = _outage_box(env, "beta", **{"box-a": ("owner", "beta")})
    t0 = time.time()
    _expired(ml, 1, by="beta", what="stopped the recovery watchdog", undo="systemctl start watchdog", at=t0)
    assert ml.change_risk(ml.changes(open_only=False)[0], {})
    able = {"beta": (False, "its daemon is stopped"), "owner": (False, "its daemon is stopped")}
    users = []
    for h in range(0, 24 * 5):
        at = t0 + h * 3600
        for x in ml.route(at, able=lambda n: able.get(n, (False, "?")), healthy=lambda *a: [], quiet=_no_quiet):
            ml.delivered(x)
            if x["target"] == ml.USER:
                users.append((at, x["text"]))
    assert len(users) == 1 and users[0][0] - t0 >= ml.USER_AFTER_S
    assert "risky" in users[0][1] and "Outage" not in users[0][1] and "Acting now" not in users[0][1]


@pytest.mark.parametrize("how", ["renew", "close", "undo"])
def test_renew_close_or_undo_clears_an_expired_change_and_its_page(env, how):
    """(d) Each of the three choices ends the entry's page: no pending page, nothing on later rounds."""
    p, ml = _outage_box(env, "beta", **{"box-a": ("owner", "beta")})
    t0 = time.time()
    cid = _expired(ml, 1, by="beta", what="stopped the recovery watchdog", undo="systemctl start watchdog", at=t0)[0]
    able = {"beta": (True, ""), "owner": (True, "")}
    assert [t for _, _, t, _ in _route(ml, t0, able)] == ["beta"]
    if how == "renew":
        ml.edit_change(cid, "beta", expires="2d", now=t0 + 60)
    else:   # undo: its undo is run, then the entry closed
        ml.close_change(cid, "undone" if how == "undo" else "kept", by="beta", now=t0 + 60)
    c = next(c for c in ml.changes(open_only=False) if c["id"] == cid)
    assert not (c.get("page") or {}).get("pending")
    assert all(_route(ml, t0 + h * 3600, able) == [] for h in range(1, 30))


@pytest.mark.parametrize("why", ["not a project of this user", "it is paused"])
def test_an_expired_change_of_a_gone_or_paused_project_goes_to_the_recovery_owner(env, why):
    """(e) Its project gone or paused: the machine's recovery owner gets it first, then its fallback."""
    p, ml = _outage_box(env, "beta", **{"box-a": ("owner", "beta")})
    t0 = time.time()
    _expired(ml, 1, by="gamma", at=t0)
    able = {"gamma": (False, why), "owner": (False, "its daemon is stopped"), "beta": (True, "")}
    assert [t for _, _, t, _ in _route(ml, t0, able)] == ["owner"]
    assert _route(ml, t0 + 60, able) == []   # the owner's daemon found stopped: passed over once it stays so
    assert [t for _, _, t, _ in _route(ml, t0 + ml.UNAVAILABLE_S + 120, able)] == ["beta"]


def test_a_relayed_batch_of_expired_changes_is_filed_once_per_change(env):
    """A batch note from another project's daemon wakes once; a later copy of any change in it is handled."""
    from ttp import upstream
    p = make(env)
    fps = ["machine-change:7@100", "machine-change:8@100"]
    assert upstream.send("peer", None, p.name, "2 machine changes are past their end", "normal", sender="daemon",
                         extra={"page_fp": fps[0], "page_fps": fps}) == "sent"
    assert upstream.send("peer", None, p.name, "change 8 again", "normal", sender="daemon",
                         extra={"page_fp": fps[1]}) == "sent"
    upstream.ingest(p, p.config())
    rows = p.db.q("SELECT fingerprint, status, text FROM events WHERE source='upstream' ORDER BY id")
    assert [r["status"] for r in rows if r["status"] == "queued"] == ["queued"]
    assert {r["fingerprint"] for r in rows} >= set(fps)
    assert next(r for r in rows if "again" in r["text"])["status"] == "handled"
