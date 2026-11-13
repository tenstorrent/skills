# Coordinator self-improvement tests of tests/test_tt_project_runtime.py (selfimprove.py: per-wake
# records, the general/local classifier, recorded self-changes kept or reverted by the daily review,
# and the guardrails), which runs this file in its own namespace (see _include_cases there): helpers,
# fixtures and imports come from that file.
from __future__ import annotations


def _si_wake(p, ended, **metrics):
    """A finished coordinator run carrying a wake record."""
    rec = {"cost_usd": 1.0, "tool_calls": 4, "rejected": 0, "claims_failed": 0, "orders_late": 0,
           "order_to_start_min": None, **metrics}
    p.db.x("INSERT INTO runs(role, status, started, ended, note) VALUES('coordinator', 'ok', ?, ?, ?)",
           (ended - 60, ended, json.dumps({"decided": 1, "wake": rec})))


def _si_home(env, monkeypatch):
    from ttp import project
    monkeypatch.setattr(project, "HOME_DIR", env["home"])
    return env["home"]


def test_selfimprove_classifier_calls_general_only_a_plugin_file_or_a_fresh_project_miss():
    from ttp.selfimprove import classify
    for f in ("plugins/tt-project/runtime/ttp/daemon.py", "runtime/ttp/coordinator.py",
              "plugins/tt-project/template/prompts/coordinator.md", "plugins/tt-project/skills/tt-project/SKILL.md",
              "template\\recurring.json"):
        assert classify([f])[0] == "general", f
    assert classify([], fresh_project=True) == ("general", "the miss would recur in a fresh project from the template")
    for files in ([], ["tt-project/harness/prompts/coordinator.md"], ["scripts/bench.sh"], ["src/model.py"]):
        assert classify(files)[0] == "local", files


def test_selfimprove_guardrails_refuse_restrictions_caps_gates_checks_reviews_and_the_ask_gate():
    from ttp.selfimprove import guard
    from ttp.coordinator import settable
    refused = [
        ("charter_note", "Restrictions", "ttp charter --undo 3"),
        ("config", "budget.daily_usd", "ttp config budget.daily_usd 20"),
        ("config", "spend.cap", "ttp config spend.cap 5"),
        ("config", "coordinator.session.rollover_tokens", "ttp config x 3"),
        ("config", "checks.required", "ttp config checks.required pytest"),
        ("config", "push.review_gate", "ttp config push.review_gate on"),
        ("config", "askauto.after_min", "ttp config askauto.after_min 30"),
        ("prompt", "ask gate rules", "git revert abc"),
        ("prompt", "coordinator.md", "ttp config BUDGET.DAILY_USD 9"),   # upper case and the undo too
        ("code", "the session mode", "git revert abc"),
        ("memory", "m1", "ttp memory --forget m1; rm -rf x"),         # shell syntax in a direct undo
        ("memory", "m1", "rm m1"),                                      # not a ttp command
        ("memory", "m1", ""),                                           # no undo
        ("model", "x", "ttp x"),                                        # not a local scope
        ("config", "no.such.key", "ttp config demo no.such.key 1"),     # chat may not set it
        ("config", "delivery.own_push", "ttp config demo delivery.own_push on"),
        # The undo is the change's own inverse, nothing else: another key, project, subcommand or entry.
        ("config", "coordinator.effort", "ttp config demo coordinator.model opus"),
        ("config", "coordinator.effort", "ttp config other coordinator.effort high"),
        ("config", "coordinator.effort", "ttp config demo coordinator.effort high extra"),
        ("config", "coordinator.effort", "ttp stop demo"),
        ("memory", "m1", "ttp memory --forget m2"),
        ("memory", "m1", "ttp note --to other m1"),
        ("memory", "m1", "ttp memory --forget 'm1'  x"),
        # A charter note or schedule has no undo command: the coordinator reverts it by its own action.
        ("charter_note", "Notes: batch status", "ttp charter --undo 3"),
        ("schedule", "nightly", "ttp schedules nightly --off"),
        ("schedule", "nightly", "git revert abc"),
    ]
    for kind, target, undo in refused:
        with pytest.raises(ValueError):
            guard(kind, target, undo, settable, "demo")
    guard("prompt", "coordinator.md: batch status lines", "git revert abc1234", settable, "demo")
    guard("memory", "fact-stale", "ttp memory --forget fact-stale", settable, "demo")
    guard("config", "coordinator.effort", "ttp config demo coordinator.effort high", settable, "demo")
    guard("schedule", "nightly", "schedule_set nightly back to every 24h", settable, "demo")


def test_selfimprove_wake_record_counts_cost_tools_rejections_claims_late_orders_and_start_delay(env):
    from types import SimpleNamespace
    from ttp import selfimprove
    p = make(env)
    now = time.time()
    from ttp import unstarted
    p.db.set_kv(selfimprove.LAST_WAKE_KEY, now - 600)
    # claims_failed: work a reply announced that the daemon found not started (unstarted.announced),
    # counted only when raised since the last wake.
    early = p.db.add_task("announced long ago", "s", reply_chat="c1")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 3000, early))
    assert len(unstarted.announced(p, now - 900)) == 1, "raised before the last wake"
    late = p.db.add_task("announced, never started", "s", reply_chat="c1")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 1500, late))
    assert len(unstarted.announced(p, now - 60)) == 1
    tid = p.db.add_task("asked in chat", "s", reply_chat="c1")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 1800, tid))
    p.db.x("INSERT INTO runs(task, role, status, started) VALUES(?, 'worker', 'running', ?)", (tid, now - 300))
    old = p.db.add_task("started before", "s", reply_chat="c1")
    p.db.x("INSERT INTO runs(task, role, status, started) VALUES(?, 'worker', 'running', ?)", (old, now - 5000))
    p.db.x("INSERT INTO runs(task, role, status, started) VALUES(?, 'worker', 'running', ?)", (old, now - 100))
    use = SimpleNamespace(cost_usd=0.42, extra={"tool_calls": 7})
    rec = selfimprove.wake(p.db, use, [{"type": "noop"}], ["task_add: refused"], now - 120, now)
    assert rec == {"cost_usd": 0.42, "tool_calls": 7, "rejected": 1, "claims_failed": 1, "orders_late": 0,
                   "order_to_start_min": 25.0, "starts": 1}, rec
    assert p.db.kv(selfimprove.LAST_WAKE_KEY) == now
    # The daemon keeps it on the coordinator run's note; wakes() reads it back.
    _si_wake(p, now - 10, cost_usd=2.0)
    assert [w["cost_usd"] for w in selfimprove.wakes(p.db, now - 60, now)] == [2.0]


def test_selfimprove_a_helping_change_is_kept_and_one_that_did_not_is_reverted(env, monkeypatch):
    from ttp import selfimprove, tried
    from ttp.coordinator import settable
    _si_home(env, monkeypatch)
    p = make(env)
    now = time.time()
    for h in (40, 30, 20):   # the baseline: 4 tool calls and 1 rejection per wake
        _si_wake(p, now - h * 3600, tool_calls=4, rejected=1)
    a = {"kind": "config", "name": "coordinator.effort", "text": "lower effort", "metric": "tool_calls",
         "evidence": "3 wakes in a row re-read the same status", "key": "Repeat Status!",
         "command": f"ttp config {p.name} coordinator.effort high", "files": ["tt-project/project.json"]}
    note = selfimprove.add(p, a, now + 2 * 86400, settable=settable, now=now)
    assert note.startswith("self-change s1 recorded: tool_calls baseline 4.0 over 3 wakes"), note
    b = {**a, "kind": "code", "name": "scripts/poll.sh", "text": "poll less", "metric": "rejected",
         "command": "git revert deadbee", "files": ["scripts/poll.sh"]}
    selfimprove.add(p, b, now + 2 * 86400, settable=settable, now=now)
    c = {**a, "text": "try again", "metric": "cost_usd"}
    selfimprove.add(p, c, now + 2 * 86400, settable=settable, now=now)
    recs = selfimprove.items(p.db)
    assert [r["id"] for r in recs] == ["s1", "s2", "s3"] and recs[0]["cls"] == "repeat-status"
    assert recs[0]["check_at"] == now + 2 * 86400 and recs[0]["status"] == "open"
    with pytest.raises(ValueError):   # measured too soon to tell
        selfimprove.add(p, a, now + 3600, settable=settable, now=now)
    for h in (10, 20, 30):   # after: fewer tool calls, the same rejections, a higher cost
        _si_wake(p, now + h * 3600, tool_calls=2, rejected=1, cost_usd=3.0)
    assert selfimprove.review(p, now + 3600) == [], "nothing is due before its check time"
    lines = selfimprove.review(p, now + 3 * 86400)
    assert lines[0] == "s1 tool_calls 4.0 -> 2.0 (3 wakes): kept", lines
    assert lines[1].startswith("s2 rejected 1.0 -> 1.0 (3 wakes): revert task #"), lines
    assert lines[2].endswith("config_set applied"), lines
    st = {r["id"]: r for r in selfimprove.items(p.db)}
    assert [st[k]["status"] for k in ("s1", "s2", "s3")] == ["kept", "reverted", "reverted"]
    assert p.config()["coordinator"]["effort"] == "high", "the config revert really restores the old value"
    revert = p.db.one("SELECT * FROM tasks WHERE title LIKE 'revert self-change s2%'")
    assert revert["kind"] == "harness" and "git revert deadbee" in revert["spec"], "code reverts go through review"
    assert selfimprove.review(p, now + 4 * 86400) == [], "each is measured once"
    # Both are Tried ledger entries: kept, and rejected.
    ledger = {e["id"]: e for e in tried.entries(p.db, now=now + 3 * 86400 + 5)}
    assert ledger["s1"]["outcome"] == "kept" and ledger["s2"]["outcome"] == "rejected"
    assert tried.describe(ledger["s2"]).startswith("self-change s2 'poll less' was rejected")
    assert "rejected 1.0 -> 1.0" in ledger["s2"]["result"]


def test_selfimprove_each_undo_kind_reverts_by_its_own_gated_path(env, monkeypatch):
    from ttp import selfimprove, tried
    from ttp.coordinator import settable
    _si_home(env, monkeypatch)
    p = make(env)
    now = time.time()
    for h in (40, 30, 20):
        _si_wake(p, now - h * 3600, cost_usd=1.0)
    for h in (10, 20, 30):   # after: a higher cost, so every change is reverted
        _si_wake(p, now + h * 3600, cost_usd=3.0)
    added = p.add_memory("check the broker before a device run", title="broker first").stem
    older = p.add_memory("a fact the user gave", title="user fact").stem
    p.set_config("coordinator.effort", "low")
    base = {"text": "a change", "metric": "cost_usd", "evidence": "costly wakes", "key": "cost"}
    due = now + 2 * 86400
    for a, at in (({"kind": "config", "name": "coordinator.effort",
                    "command": f"ttp config {p.name} coordinator.effort high"}, now),
                  ({"kind": "memory", "name": added, "command": f"ttp memory --forget {added}"}, now),
                  ({"kind": "memory", "name": older, "command": f"ttp memory --forget {older}"}, now + 7200),
                  ({"kind": "schedule", "name": "nightly", "command": "schedule_set nightly back to 24h"}, now)):
        selfimprove.add(p, {**base, **a}, due, settable=settable, now=at)
    lines = selfimprove.review(p, now + 3 * 86400)
    assert lines[0].endswith("config_set applied") and lines[1].endswith("memory_forget applied"), lines
    assert "revert failed, still open" in lines[2] and "older than the change" in lines[2], lines
    assert "asked the coordinator to revert it" in lines[3], lines
    assert p.config()["coordinator"]["effort"] == "high"
    names = [e["name"] for e in p._memory_entries()]
    assert added not in names and older in names, "only the entry the change added is forgotten"
    st = {r["id"]: r for r in selfimprove.items(p.db)}
    assert [st[k]["status"] for k in ("s1", "s2", "s3", "s4")] == ["reverted", "reverted", "open", "reverted"]
    evs = {r["fingerprint"]: r["text"] for r in p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'self_change_revert:%'")}
    assert set(evs) == {"self_change_revert:s3", "self_change_revert:s4"}, evs
    assert "schedule_set" in evs["self_change_revert:s4"] and "back to 24h" in evs["self_change_revert:s4"]
    # A failed revert is unresolved: no Tried entry while the change is live; the next review retries it,
    # raising the coordinator only once.
    assert "s3" not in {e["id"] for e in tried.entries(p.db, now=now + 3 * 86400 + 5)}
    again = selfimprove.review(p, now + 4 * 86400)
    assert len(again) == 1 and again[0].startswith("s3 ") and "still open" in again[0], again
    assert st["s3"]["revert_tries"] == 1 and selfimprove.items(p.db)[2]["revert_tries"] == 2
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE fingerprint = 'self_change_revert:s3'")["n"] == 1
    assert any("revert failed" in ln for ln in selfimprove.summary(p.db, now + 4 * 86400, where=p))


def test_selfimprove_general_fixes_and_helpful_repeats_go_upstream(env, monkeypatch):
    from ttp import selfimprove, upstream
    from ttp.coordinator import settable
    _si_home(env, monkeypatch)
    p = make(env)
    now = time.time()
    a = {"kind": "prompt", "name": "coordinator.md", "text": "say the start only once a run started",
         "metric": "claims_failed", "evidence": "two false 'started' claims", "key": "early-start-claim",
         "command": "git revert abc1234", "files": ["plugins/tt-project/template/prompts/coordinator.md"]}
    note = selfimprove.add(p, a, now + 86400, settable=settable, now=now)
    assert "general (it needs plugin file" in note and "filed upstream (new note)" in note, note
    assert selfimprove.items(p.db) == [], "a general fix is no local change"
    notes = [json.loads(ln) for ln in upstream.path().read_text().splitlines()]
    assert notes[0]["title"].startswith("upstream: coordinator self-improvement:") and "two false" in notes[0]["spec"]
    assert "already filed" in selfimprove.add(p, a, now + 86400, settable=settable, now=now)
    assert "fresh project" in selfimprove.add(p, {**a, "files": [], "fresh_project": True}, now + 86400,
                                               settable=settable, now=now)
    # A local fix that helped, whose miss class another project recorded too, goes upstream with its numbers.
    _si_wake(p, now - 3600, claims_failed=2)
    local = {**a, "files": ["tt-project/harness/prompts/coordinator.md"], "text": "check before claiming"}
    selfimprove.add(p, local, now + 86400, settable=settable, now=now)
    assert selfimprove.projects_with("early-start-claim", now) == {p.name}
    selfimprove._note_miss("other-project", "early-start-claim", now)
    _si_wake(p, now + 3600, claims_failed=0)
    assert selfimprove.review(p, now + 2 * 86400) == ["s1 claims_failed 2.0 -> 0.0 (1 wakes): kept, filed upstream"]
    last = json.loads(upstream.path().read_text().splitlines()[-1])
    assert "claims_failed 2.0 -> 0.0" in last["spec"] and "1 other project" in last["spec"]


def test_selfimprove_the_self_change_action_applies_with_its_guardrails(env, monkeypatch):
    from ttp import coordinator as coord, prompts, selfimprove
    _si_home(env, monkeypatch)
    p = make(env)
    ok = {"type": "self_change", "kind": "memory", "name": "fact-stale", "text": "add a fact",
          "metric": "rejected", "evidence": "rejected twice", "key": "stale-fact",
          "command": "ttp memory --forget fact-stale"}
    bad = {**ok, "kind": "config", "name": "budget.daily_usd", "command": f"ttp config {p.name} budget.daily_usd 5"}
    problems = coord.apply(p, [ok, bad, {**ok, "metric": "happiness"}, {**ok, "start_after": "2h"}])
    assert len(problems) == 3 and all(x.startswith("self_change: ") for x in problems), problems
    assert "budget" in problems[0] and "metric 'happiness'" in problems[1] and "12 h" in problems[2]
    rec = selfimprove.items(p.db)
    assert len(rec) == 1 and rec[0]["check_at"] > time.time() + 2.9 * 86400, "default: measured in 3 days"
    for k in ("metric", "fresh_project", "generic"):
        assert k in coord.ACTIONS_SCHEMA["properties"]["actions"]["items"]["properties"], k
    # The prompt row shows only in the persistent session's role, always on: the stateless role (a
    # provider that keeps no session) keeps today's prompt.
    assert "session" in prompts.coordinator_features(p)
    assert "`self_change`" not in prompts.coordinator_role(p)
    assert "`self_change`" in prompts.coordinator_session_role(p)


def test_selfimprove_claude_counts_each_tool_call_once(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "claude.jsonl"
    use = lambda i: {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}   # noqa: E731
    lines = [{"type": "assistant", "message": {"id": "m1", "content": [use(1)]}},
             {"type": "assistant", "message": {"id": "m1", "content": [use(1), use(2)]}},   # streamed again
             {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "hi"}, use(3)]}},
             {"type": "result", "subtype": "success", "total_cost_usd": 0.1, "result": "ok", "usage": {}}]
    out.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert get_provider("claude").parse(out).extra["tool_calls"] == 3


def test_selfimprove_a_task_bound_change_is_measured_only_with_its_applied_receipt(env, monkeypatch):
    from ttp import coordinator as coord, selfimprove, tried
    from ttp.coordinator import settable
    _si_home(env, monkeypatch)
    p = make(env)
    now = time.time()
    for h in (40, 30, 20):
        _si_wake(p, now - h * 3600, cost_usd=1.0)
    for h in (10, 20, 30, 60, 70, 80):   # after: a higher cost, so an applied change is reverted
        _si_wake(p, now + h * 3600, cost_usd=3.0)
    landed = {}   # task id -> (commit, still pending): the fake harness/landing lookup
    monkeypatch.setattr(selfimprove, "_commit", lambda p, task: landed.get(task["id"], ("", False)))
    base = {"kind": "script", "name": "scripts/poll.sh", "text": "poll less", "metric": "cost_usd",
            "evidence": "costly wakes", "key": "cost", "command": "git revert HEAD", "files": ["scripts/poll.sh"]}
    due = now + 2 * 86400
    noop, real, slow = (p.db.add_task(f"make change {n}", "spec", kind="harness", origin="coordinator")
                        for n in ("a", "b", "c"))
    for tid in (noop, real, slow):
        selfimprove.add(p, base, due, settable=settable, now=now, task=tid)
    with pytest.raises(ValueError):   # only the harness or code task that makes it
        selfimprove.add(p, base, due, settable=settable, now=now, task=99999)
    p.db.update_task(noop, status="done")   # a diagnosis only: no commit
    p.db.update_task(real, status="done")
    landed[real] = ("abc1234", False)
    # slow stays running: pending
    lines = selfimprove.review(p, now + 3 * 86400)
    assert lines[0] == f"s1: not applied (its task #{noop} ended with no source change): not measured, nothing to revert"
    assert len(lines) == 1, "the applied one is measured over its own span from its receipt on; the pending one waits"
    st = {r["id"]: r for r in selfimprove.items(p.db)}
    assert st["s1"]["status"] == "not_applied" and "after" not in st["s1"]
    assert st["s2"]["applied"]["commit"] == "abc1234" and st["s2"]["status"] == "open"
    assert st["s3"]["status"] == "open" and "applied" not in st["s3"]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE title LIKE 'revert self-change%'")["n"] == 0
    landed[real] = ("fff9999", False)   # a later lookup never replaces the receipt
    lines = selfimprove.review(p, now + 6 * 86400)
    assert len(lines) == 1 and lines[0].startswith("s2 cost_usd 1.0 -> 3.0") and "revert task #" in lines[0], lines
    st = {r["id"]: r for r in selfimprove.items(p.db)}
    assert st["s2"]["status"] == "reverted" and st["s2"]["applied"]["commit"] == "abc1234"
    revert = p.db.one("SELECT * FROM tasks WHERE title LIKE 'revert self-change s2%'")
    assert "abc1234" in revert["spec"] and "git revert HEAD" in revert["spec"]
    assert st["s3"]["status"] == "open" and selfimprove.review(p, now + 9 * 86400) == [], "still pending: left alone"
    ledger = {e["id"] for e in tried.entries(p.db, now=now + 6 * 86400 + 5)}
    assert "s2" in ledger and "s1" not in ledger, "a change never applied makes no cost-effect claim"
    # The coordinator binds a code self-change to the batch's one harness or code task_add.
    probs = coord.apply(p, [{"type": "task_add", "title": "shorten the poll script", "spec": "edit scripts/poll.sh",
                             "kind": "harness", "tier": "standard", "priority": 2},
                            {"type": "self_change", **base, "text": "poll even less"}])
    assert not probs, probs
    made = p.db.one("SELECT id FROM tasks WHERE title = 'shorten the poll script'")["id"]
    assert selfimprove.items(p.db)[-1]["task"] == made
