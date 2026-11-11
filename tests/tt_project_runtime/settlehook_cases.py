# Settle hook tests of tests/test_tt_project_runtime.py (settlehook.py: the project's coordinator.settle_cmd
# settles routine event batches without a model; the runtime keeps its never-settle list), which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

# A hook stand-in: saves its stdin to $SH_IN, then answers as $SH_MODE says.
_HOOK = r'''
import json, os, sys, time
data = sys.stdin.read()
open(os.environ["SH_IN"], "a").write(data + "\n")
mode = os.environ.get("SH_MODE", "all")
evs = json.loads(data)
if mode == "all":
    print(json.dumps([{"event_id": e["event_id"], "settle": True, "reason": "routine " + e["kind"]} for e in evs]))
elif mode == "first":
    print(json.dumps([{"event_id": evs[0]["event_id"], "settle": True, "reason": "known noise"}]))
elif mode == "extra":
    print(json.dumps([{"event_id": int(os.environ["SH_EXTRA"]), "settle": True, "reason": "not mine"}]
                     + [{"event_id": e["event_id"], "settle": False, "reason": "keep"} for e in evs]))
elif mode == "error":
    sys.stderr.write("boom\n")
    sys.exit(3)
elif mode == "sleep":
    time.sleep(30)
elif mode == "badjson":
    print("settled, I think")
elif mode == "noreason":
    print(json.dumps([{"event_id": e["event_id"], "settle": True} for e in evs]))
elif mode == "none":
    print("[]")
elif mode == "dict":
    print(json.dumps({"event_id": evs[0]["event_id"], "settle": True, "reason": "x"}))
'''


def _sh_setup(env, monkeypatch, tmp_path, mode="all", hook=True):
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=0)
    p.set_config("coordinator.batch_s", 0)
    p.set_config("coordinator.event_hold_s", 0)
    p.set_config("coordinator.plain_done_hold_max_s", 0)   # a held plain hand-off never reaches the hook
    script = tmp_path / "hook.py"
    script.write_text(_HOOK)
    if hook:
        p.set_config("coordinator.settle_cmd", f"{sys.executable} {script}")
    d.cfg = p.config()
    p.db.set_kv("last_coordinator_turn", clock[0] + 60)   # no idle or starve wake inside a test's ticks
    monkeypatch.setenv("SH_IN", str(tmp_path / "in.jsonl"))
    monkeypatch.setenv("SH_MODE", mode)
    turns = []

    def turn(*a, **k):   # a turn that handles its events
        starts.append(clock[0])
        turns.append(k["note"]["events"])
        p.db.x("UPDATE events SET status='handled' WHERE id IN (%s)" % ",".join(map(str, turns[-1] or [0])))
    monkeypatch.setattr(d, "start_run", turn)
    return p, d, clock, starts, turns


def _ev_x(p, clock, kind, text, severity, source, task, data):
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (clock[0], source, kind, severity, text, data, "queued", task))
    return p.db.one("SELECT MAX(id) i FROM events")["i"]


def _drive(d, clock, starts, ticks=12):
    """Ticks 5 s apart until a turn starts, letting each hook run finish (its thread) between ticks."""
    n = len(starts)
    for _ in range(ticks):
        d.maybe_coordinate()
        run = getattr(d, "_settle_run", None)
        if run is not None:
            assert run.wait(15), "the hook outlived its timeout"
        clock[0] += 20
        if len(starts) > n:
            return True
    return False


def _status(p, i):
    return p.db.one("SELECT status FROM events WHERE id=?", (i,))["status"]


def _hook_ran(tmp_path):
    f = tmp_path / "in.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def test_settlehook_is_off_by_default_and_chat_settable(env, monkeypatch, tmp_path):
    from ttp import coordinator as coord, settlehook
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, hook=False)
    assert p.config()["coordinator"]["settle_cmd"] == ""
    assert settlehook.command(p.config()) == ""
    clock[0] += 60
    i = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", None, None)
    assert _drive(d, clock, starts), "a routine batch with no hook did not wake"
    assert i in turns[-1] and not _hook_ran(tmp_path)
    assert coord.settable("coordinator.settle_cmd")("  ./bin/settle --json ") == "./bin/settle --json"
    assert coord.settable("coordinator.settle_cmd")("") == ""
    for bad in ("a\nb", "x" * 3000, 5):
        with pytest.raises(ValueError):
            coord.settable("coordinator.settle_cmd")(bad)
    out = subprocess.run([sys.executable, "-m", "ttp", "config", "--chat-settable", "coordinator.settle_cmd", "./s"],
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert out.returncode == 0 and "accepted" in out.stdout, out.stdout + out.stderr


def test_settlehook_settles_a_routine_batch_with_its_logged_reason(env, monkeypatch, tmp_path):
    from ttp import audit, settlehook
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    clock[0] += 60
    ids = [_ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None),
           _ev_x(p, clock, "upstream_note", "note abc: a typo in a prompt", "low", "upstream", None, None),
           _ev_x(p, clock, "observation", "a watcher line repeated", "low", "cli-watch", None, None)]
    assert not _drive(d, clock, starts), "a settled batch still woke the coordinator"
    assert [_status(p, i) for i in ids] == ["handled"] * 3
    sent = _hook_ran(tmp_path)
    assert len(sent) == 1 and [e["event_id"] for e in sent[0]] == ids
    assert set(sent[0][0]) == {"event_id", "kind", "severity", "text"} and sent[0][0]["text"].startswith("#3 tidy")
    log = p.db.kv(settlehook.LOG_KEY)
    assert [(r["event"], r["reason"]) for r in log] == [(ids[0], "routine task_done"), (ids[1], "routine upstream_note"),
                                                        (ids[2], "routine observation")]
    feed = settlehook.feed(p.db, clock[0])
    assert feed and "routine task_done" in feed[-1]["text"] and feed[0]["state"] == "status"
    line = "\n".join(audit.lines(p.db, clock[0], p=p))
    assert "settle hook settled without a turn" in line and "3" in line and "routine task_done" in line, line
    # A later follow-up of the settled task is a new event and wakes on its own.
    clock[0] += 60
    f = _ev_x(p, clock, "followup_proposed", "#3 proposes: more docs", "normal", "task:3", 3, None)
    assert _drive(d, clock, starts) and f in turns[-1]


def _sh_ride(p, clock):
    from ttp import pagesev
    return _ev_x(p, clock, "observation", "a watcher line", "low", "cli-watch", None,
                 json.dumps({pagesev.RIDE_ALONG: True}))


def test_settlehook_a_ride_along_only_batch_past_its_hour_still_starts_a_turn(env, monkeypatch, tmp_path):
    from ttp import pagesev
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode="none")
    p.set_config("coordinator.idle_wake_s", 6 * 3600)
    p.set_config("coordinator.starve_wake_s", 6 * 3600)
    d.cfg = p.config()
    i = _sh_ride(p, clock)
    clock[0] += pagesev.RIDE_ALONG_MAX_S + 60
    assert _drive(d, clock, starts), "a ride-along past RIDE_ALONG_MAX_S never woke with the hook on"
    assert i in turns[-1]


@pytest.mark.parametrize("due", ["idle", "starve"])
def test_settlehook_an_idle_or_starve_wake_with_a_queued_ride_along_still_starts_a_turn(env, monkeypatch, tmp_path,
                                                                                       due):
    from ttp import daemon as daemon_mod
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode="none")
    clock[0] += 60
    _sh_ride(p, clock)
    p.db.set_kv("last_coordinator_turn", clock[0] - 7200)
    monkeypatch.setattr(daemon_mod, "idle_wake", lambda *a, **k: {"due": due, "wake": {}, "starve": {}})
    monkeypatch.setattr(daemon_mod.quietgate, "quiet", lambda *a, **k: False)
    assert _drive(d, clock, starts), f"a {due} wake with a queued ride-along never started with the hook on"


def test_settlehook_a_rejection_due_wake_with_a_queued_ride_along_still_starts_a_turn(env, monkeypatch, tmp_path):
    from ttp import coordinator as coord, daemon as daemon_mod
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode="none")
    p.set_config("coordinator.idle_wake_s", 6 * 3600)
    p.set_config("coordinator.starve_wake_s", 6 * 3600)
    d.cfg = p.config()
    clock[0] += 60
    _sh_ride(p, clock)
    p.db.set_kv(coord.REJECTED_KEY, ["refused: a push to main"])
    p.db.set_kv(coord.REJECTED_AT_KEY, clock[0] - coord.REJECTION_WAIT_S - 60)
    monkeypatch.setattr(daemon_mod.quietgate, "quiet", lambda *a, **k: False)
    assert _drive(d, clock, starts), "a rejection-due wake with a queued ride-along never started with the hook on"


def test_settlehook_settles_only_what_it_names_the_rest_wakes(env, monkeypatch, tmp_path):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode="first")
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    b = _ev_x(p, clock, "task_done", "#4 tidy more → done: ok", "normal", "task:4", 4, None)
    assert _drive(d, clock, starts)
    assert _status(p, a) == "handled" and turns[-1] == [b]


def test_settlehook_never_settles_an_event_outside_its_input(env, monkeypatch, tmp_path):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode="extra")
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    monkeypatch.setenv("SH_EXTRA", str(a + 1000))
    assert _drive(d, clock, starts) and turns[-1] == [a]


_NEVER = [
    ("high", dict(kind="observation", text="a watcher line", severity="high")),
    ("critical", dict(kind="task_done", text="#3 → done", severity="critical")),
    ("task_failed", dict(kind="task_failed", text="#3 → failed")),
    ("obligation", dict(kind="obligation_overdue", text="an order is late")),
    ("promise kind", dict(kind="promise_due", text="a promise is due")),
    ("outage kind", dict(kind="outage", text="host down")),
    ("machine kind", dict(kind="machine_condition", text="a node changed")),
    ("must_act data", dict(kind="observation", text="act", data=json.dumps({"must_act": {"key": "k"}}))),
    ("failure text", dict(kind="task_done", text="#3 → done: two tests failed on the box")),
    ("restriction text", dict(kind="upstream_note", text="this would loosen a Restriction")),
    ("promise text", dict(kind="task_done", text="#3 → done: the promised report is out")),
    ("review verdict", dict(kind="task_done", text="#3 → done: verdict pass")),
    ("outage text", dict(kind="observation", text="the build box is unreachable")),
    ("machine observation", dict(kind="observation", text="a node is draining")),
    ("machine source", dict(kind="observation", text="something changed", source="daemon:machines")),
    ("user source", dict(kind="observation", text="hello", source="user")),
    ("own fault", dict(kind="observation", text="settle hook fault", source="daemon:settle-hook", severity="low")),
]


@pytest.mark.parametrize("name,ev", _NEVER, ids=[n for n, _ in _NEVER])
def test_settlehook_never_settles_a_listed_kind(env, monkeypatch, tmp_path, name, ev):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    b = _ev_x(p, clock, ev["kind"], ev["text"], ev.get("severity", "normal"), ev.get("source", "daemon"), 5,
              ev.get("data"))
    assert _drive(d, clock, starts), f"{name}: the batch did not wake"
    assert b in turns[-1] and a in turns[-1], f"{name}: {turns}"
    assert not _hook_ran(tmp_path), f"{name}: the hook saw a batch it may not settle"


def test_settlehook_never_settles_a_review_result_or_an_obligation_owner(env, monkeypatch, tmp_path):
    from ttp import obligations
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    rid = p.db.add_task("Review #3", "s", kind="review")
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", f"#{rid} Review #3 → done: looks fine", "normal", f"task:{rid}", rid, None)
    assert _drive(d, clock, starts) and turns[-1] == [a] and not _hook_ran(tmp_path)
    monkeypatch.setattr(obligations, "owner_due", lambda db: {7: 0.0})
    clock[0] += 60
    b = _ev_x(p, clock, "task_done", "#7 report → done: sent", "normal", "task:7", 7, None)
    assert _drive(d, clock, starts) and turns[-1] == [b] and not _hook_ran(tmp_path)


def test_settlehook_a_user_message_runs_no_hook(env, monkeypatch, tmp_path):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _drive(d, clock, starts) and a in turns[-1] and not _hook_ran(tmp_path)


def test_settlehook_rechecks_the_never_settle_list_on_its_answer(env, monkeypatch, tmp_path):
    """An event that turned unsettleable while the hook ran (here: raised to high) is not settled."""
    from ttp import settlehook
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    clock[0] += 60
    a = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    run = settlehook.Run(settlehook.command(d.cfg), str(p.root), settlehook.candidates(p.db, p.db.q(
        "SELECT id, ts, kind, severity, task, source, data FROM events WHERE id=?", (a,))))
    assert run.wait(15) and run.settle == {a: "routine task_done"}
    p.db.x("UPDATE events SET severity='high' WHERE id=?", (a,))
    assert settlehook.apply(p.db, run, clock[0]) == [] and _status(p, a) == "queued"


@pytest.mark.parametrize("mode", ["error", "sleep", "badjson", "noreason", "dict"])
def test_settlehook_fails_open_with_one_low_observation(env, monkeypatch, tmp_path, mode):
    from ttp import settlehook
    monkeypatch.setattr(settlehook, "TIMEOUT_S", 1.0)
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path, mode=mode)
    for round_ in range(2):
        clock[0] += 3600
        a = _ev_x(p, clock, "task_done", f"#3 tidy docs {round_} → done: ok", "normal", "task:3", 3, None)
        assert _drive(d, clock, starts), f"{mode}: a hook fault kept the batch from waking"
        assert a in turns[-1]
    assert len(_hook_ran(tmp_path)) == 2
    assert not p.db.kv(settlehook.LOG_KEY)
    # A low observation is one deduplicated issue that wakes nobody on its own.
    assert not p.db.q("SELECT id FROM events WHERE source=?", (settlehook.FAULT_SOURCE,))
    obs = p.db.q("SELECT severity, title, count FROM issues WHERE source=?", (settlehook.FAULT_SOURCE,))
    assert len(obs) == 1 and obs[0]["severity"] == "low" and obs[0]["count"] == 2, [dict(o) for o in obs]
    assert "settle_cmd" in obs[0]["title"], obs[0]["title"]
    assert len(p.db.kv(settlehook.FAULTS_KEY)) == 2
    assert "settled nothing on a fault" in settlehook.audit_line(p.db, 0)


def test_settlehook_parse_shapes():
    from ttp import settlehook
    assert settlehook.parse(b'[{"event_id": 1, "settle": true, "reason": "ok"}, {"event_id": 2, "settle": false}]',
                            {1, 2}) == {1: "ok"}
    assert settlehook.parse(b"[]", {1}) == {}
    for bad in (b"", b"null", b"{}", b'[{"event_id": "1", "settle": true, "reason": "x"}]',
                b'[{"event_id": true, "settle": true, "reason": "x"}]', b'[1]',
                b'[{"event_id": 1, "settle": true, "reason": " "}]'):
        with pytest.raises(ValueError):
            settlehook.parse(bad, {1})


def test_settlehook_is_not_asked_about_a_plain_done_the_hold_keeps_waiting(env, monkeypatch, tmp_path):
    p, d, clock, starts, turns = _sh_setup(env, monkeypatch, tmp_path)
    p.set_config("coordinator.plain_done_hold_max_s", 3000)
    d.cfg = p.config()
    clock[0] += 60
    i = _ev_x(p, clock, "task_done", "#3 tidy docs → done: ok", "normal", "task:3", 3, None)
    assert not _drive(d, clock, starts) and not _hook_ran(tmp_path) and _status(p, i) == "queued"
    clock[0] += 3000   # past the hold: the batch goes through the hook as any other
    assert _drive(d, clock, starts) or _status(p, i) == "handled"
    assert _hook_ran(tmp_path)
