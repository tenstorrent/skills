# Obligation owner chain tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new obligation tests (ttp/obligations.py) here, not there.
from __future__ import annotations


def _ob_history(p, oid):
    from ttp import obligations as ob
    return [h for h in ob.get(p.db, oid).get("history") or [] if h["event"] == "attached"]


def test_obligation_daemon_review_fix_and_rereview_of_a_done_owner_become_owners(env, monkeypatch):
    """Owner done, its daemon review queued: the review owns the obligation, then its fix and re-review."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    p.db.update_task(tid, status="done")
    rev = p.db.add_task(f"Review #{tid}: build the deliverable", "check it", kind="review", tier="light",
                        depends_on=[tid], labels=[f"auto_review:{tid}"])
    assert check(now + 600) == []
    o = ob.get(p.db, oid)
    assert str(rev) in o["owners"] and f"## Obligation {oid} " in p.db.task(rev)["spec"]
    assert [h["task"] for h in _ob_history(p, oid)] == [rev] and f"#{tid}" in _ob_history(p, oid)[0]["why"]
    p.db.update_task(rev, status="failed")
    # The daemon's fix and re-review: linked by label and by title; the re-review only through the fix
    # (auto_review) and the review (continues), both attached in the same pass.
    fix = p.db.add_task(f"Fix review #{rev}: build the deliverable", "fix", kind="code", tier="standard",
                        labels=[f"continues:{tid}", f"review_fix:{rev}"])
    rr = p.db.add_task(f"Re-review #{rev}: build the deliverable", "recheck", kind="review", tier="light",
                       depends_on=[fix], labels=[f"auto_review:{fix}", f"continues:{rev}"])
    unrelated = p.db.add_task("Review #999999: something else", "x", kind="review", tier="light",
                              labels=["auto_review:999999"])
    assert check(now + 1200) == []
    owners = ob.get(p.db, oid)["owners"]
    assert {str(fix), str(rr)} <= set(owners) and str(unrelated) not in owners
    # A second pass attaches nothing again.
    n = len(_ob_history(p, oid))
    check(now + 1300)
    assert len(_ob_history(p, oid)) == n


def test_obligation_title_only_hops_and_closed_carriers(env, monkeypatch):
    """A hop named only by its title is attached; a finished one, a promise and a received obligation
    are left alone."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    done_oid = ob.add(p, "the old table", [tid], now=now)
    ob.record(p, done_oid, "receipt", "results/table.md", None, now=now)
    p.db.update_task(tid, status="done")
    by_title = p.db.add_task(f"Review #{tid}: by hand", "check it", kind="review", tier="light")
    finished = p.db.add_task(f"Re-review #{tid}: old", "x", kind="review", tier="light", status="done")
    lookalike = p.db.add_task(f"Review #{tid}0: other", "x", kind="review", tier="light")
    check(now + 600)
    assert set(ob.get(p.db, oid)["owners"]) == {str(tid), str(by_title)}
    assert set(ob.get(p.db, done_oid)["owners"]) == {str(tid)}, "a received obligation takes no new owner"
    assert str(finished) not in ob.get(p.db, oid)["owners"] and str(lookalike) not in ob.get(p.db, oid)["owners"]


def test_obligation_task_add_continuing_an_owner_attaches_the_new_task(env, monkeypatch):
    """task_add `continues` = an owner id: a follow-up of a done owner and a takeover of a failed one."""
    from ttp import coordinator as coord, obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    other = p.db.add_task("second part", "s", kind="work", tier="light", origin="coordinator")
    oid = ob.add(p, "the benchmark table", [tid, other], now=now)
    p.db.update_task(tid, status="done")
    p.db.update_task(other, status="failed")
    problems = coord.apply(p, [
        {"type": "task_add", "title": "finish the table", "spec": "carry the table on", "kind": "work",
         "continues": tid},
        {"type": "task_add", "title": "redo the second part", "spec": "take it over", "kind": "work",
         "continues": other},
        {"type": "task_add", "title": "an unrelated chore", "spec": "tidy", "kind": "work"}])
    assert problems == [], problems
    rows = {t["title"]: t["id"] for t in p.db.q("SELECT id, title FROM tasks")}
    owners = ob.get(p.db, oid)["owners"]
    assert str(rows["finish the table"]) in owners and str(rows["redo the second part"]) in owners
    assert str(rows["an unrelated chore"]) not in owners
    whys = {h["task"]: h["why"] for h in _ob_history(p, oid)}
    assert f"#{tid}" in whys[rows["finish the table"]] and f"#{other}" in whys[rows["redo the second part"]]


def test_obligation_with_an_open_chain_raises_no_no_live_owner_event(env, monkeypatch):
    """The 2026-10-29 case: the owner done without a receipt, its follow-up and that one's review still
    open. Before, the overdue check said no live owner was left."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    fup = p.db.add_task("finish the table", "s", kind="code", tier="standard", labels=[f"follows:{tid}"])
    p.db.update_task(tid, status="done")
    p.db.update_task(fup, status="review")
    rev = p.db.add_task(f"Review #{fup}: finish the table", "check", kind="review", tier="light",
                        labels=[f"auto_review:{fup}"])
    assert check(now + 2 * 3600 + 60) == [], "an open chain is a live owner"
    assert {str(fup), str(rev)} <= set(ob.get(p.db, oid)["owners"])
    assert "no live owner" not in ob.lines(p.db, now + 2 * 3600 + 60)[0]


def test_obligation_no_live_owner_event_names_the_open_tasks_naming_a_former_owner(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    p.db.update_task(tid, status="done")
    named = p.db.add_task("table: second pass", f"Pick up where #{tid} stopped.", kind="work", tier="light")
    in_title = p.db.add_task(f"redo #{tid} cells", "s", kind="work", tier="light")
    p.db.add_task("old pass", f"after #{tid}", kind="work", tier="light", status="done")
    lookalike = p.db.add_task("other", f"see #{tid}1 and &#{tid};", kind="work", tier="light")
    evs = check(now + 2 * 3600 + 60)
    assert len(evs) == 1 and "no live owner is left" in evs[0]["text"], evs
    text = evs[0]["text"]
    assert f"#{named} (queued, names #{tid}) table: second pass" in text and f"#{in_title} (queued" in text
    assert f"#{lookalike} " not in text and "old pass" not in text
    assert f"ttp obligation attach {oid} <task>" in text
    assert set(ob.get(p.db, oid)["owners"]) == {str(tid)}, "candidates are named, not attached"


def test_obligation_no_live_owner_event_says_so_when_no_open_task_names_one(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    ob.add(p, "the benchmark table", [tid], now=now)
    p.db.update_task(tid, status="failed")
    evs = check(now + 2 * 3600 + 60)
    assert len(evs) == 1 and "no open task names a former owner" in evs[0]["text"], evs


def _handle_overdue(p):
    p.db.x("UPDATE events SET status='handled' WHERE kind='obligation_overdue' AND status='queued'")


def test_obligation_with_every_owner_ended_is_reowned_once_its_wake_was_handled(env, monkeypatch):
    """The 2026-10-27 case: every owner ended without a receipt (one cancelled, three done), the overdue wake
    went out once, and the coordinator's attach of a task that had just finished was rejected. The episode
    then stayed covered and nothing woke for 48 h. Now the daemon starts one owner, once per owner set."""
    from ttp import coordinator as coord, obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    others = [p.db.add_task(f"pass {i}", "s", kind="work", tier="light", origin="coordinator") for i in range(3)]
    oid = ob.add(p, "post each CI run's result", [tid, *others], now=now)
    p.db.update_task(tid, status="cancelled")
    for t in others:
        p.db.update_task(t, status="done")
    t0 = now + 2 * 3600 + 60
    assert len(check(t0)) == 1, "the no-live-owner wake"
    # Its turn names a task that finished meanwhile: left out with a note (#2116), nothing is attached.
    late = p.db.add_task("continue: CI read", "s", kind="work", tier="light", status="done")
    assert coord.apply(p, [{"type": "obligation", "key": oid, "owners": [late]}]) == []
    assert f"#{late} is done" in " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert len(check(t0 + 600)) == 1, "an unanswered wake: the daemon waits for the turn"
    _handle_overdue(p)
    p.db.x("INSERT INTO runs(task, role, status, started) VALUES(NULL, 'coordinator', 'running', ?)", (t0,))
    check(t0 + 900)
    check(t0 + 1200)
    assert set(ob.get(p.db, oid)["owners"]) == {str(t) for t in (tid, *others)}, "the answering turn may still attach"
    p.db.x("UPDATE runs SET status='ok'")
    # A turn started after the wake was seen answered never saw it: it does not hold the re-owning back.
    p.db.x("INSERT INTO runs(task, role, status, started) VALUES(NULL, 'coordinator', 'running', ?)", (t0 + 1300,))
    check(t0 + 1500)
    o = ob.get(p.db, oid)
    new = [int(t) for t in o["owners"] if int(t) not in (tid, *others)]
    assert len(new) == 1, o["owners"]
    t = p.db.task(new[0])
    assert t["status"] == "queued" and t["origin"] == "daemon" and t["priority"] == 1
    assert f"{ob.REOWN_LABEL}{oid}" in t["labels"] and f"## Obligation {oid} " in t["spec"]
    assert f"#{tid} (cancelled)" in t["spec"] and "post each CI run's result" in t["spec"]
    assert "no longer owed" in t["spec"] and "superseded" in t["spec"], "an ended subject: say so, the drop is asked once"
    assert "no live owner" not in ob.lines(p.db, t0 + 1500)[0]
    # A second pass starts nothing more.
    check(t0 + 2100)
    assert len(ob.get(p.db, oid)["owners"]) == 5
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_obligation_daemon_owner_ending_without_receipt_wakes_once_never_a_second_owner(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    p.db.update_task(tid, status="failed")
    t0 = now + 2 * 3600 + 60
    assert len(check(t0)) == 1
    _handle_overdue(p)
    check(t0 + 300)
    mine = [int(t) for t in ob.get(p.db, oid)["owners"] if int(t) != tid]
    assert len(mine) == 1, "no turn running: re-owned on the first pass that sees the wake answered"
    t1 = t0 + 600
    check(t1)   # its owner is live: the episode ends
    assert ob.get(p.db, oid)["raised"] is None
    # its subject ended (a PR closed and replaced): the daemon's owner says so and ends without a receipt
    ob.record(p, oid, "progress", "not owed: the PR was closed and replaced", mine[0], now=t1 + 60)
    p.db.update_task(mine[0], status="done")
    t2 = t1 + 2 * 3600 + 60
    evs = check(t2)
    assert len(evs) == 2, "the new episode's wake"
    _handle_overdue(p)
    evs = check(t2 + 300)
    assert len(evs) == 3 and evs[-1]["fingerprint"] == f"obligation:{oid}:reown:{tid}-{mine[0]}", evs
    assert f"#{mine[0]} ended without a receipt" in evs[-1]["text"] and evs[-1]["severity"] == "high"
    assert "not owed: the PR was closed and replaced" in evs[-1]["text"] and "drop it" in evs[-1]["text"]
    _handle_overdue(p)
    for k in range(1, 6):   # never a loop: no second owner, no repeated wake while the owners stay the same
        evs = check(t2 + 300 + k * 3 * 3600)
    assert len(evs) == 3
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 1


def test_obligation_reown_skips_a_wake_answered_with_an_owner_dropped_or_received(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    a = ob.add(p, "answered", [tid], now=now)
    b = ob.add(p, "dropped", [tid], now=now)
    c = ob.add(p, "received", [tid], now=now)
    p.db.update_task(tid, status="done")
    t0 = now + 2 * 3600 + 60
    assert len(check(t0)) == 3
    _handle_overdue(p)
    carrier = p.db.add_task("carry it on", "s", kind="work", tier="light", origin="coordinator")
    ob.attach(p, a, [carrier], now=t0 + 60)
    ob.drop(p, b, "no longer wanted", by="coordinator")
    ob.record(p, c, "receipt", "results/table.md", None, now=t0 + 60)
    check(t0 + 300)
    check(t0 + 600)
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 0


def test_obligation_wake_settled_without_a_model_turn_is_reowned_once(env, monkeypatch):
    """A no-live-owner wake that no turn read (a quiet-gate skip marks it handled, the rejection cooldown
    mutes it) is settled all the same: the daemon starts the one owner itself."""
    from ttp import obligations as ob, quietgate
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    a = ob.add(p, "the perf table", [tid], now=now)
    b = ob.add(p, "the CI summary", [tid], now=now)
    p.db.update_task(tid, status="done")
    t0 = now + 2 * 3600 + 60
    evs = {e["fingerprint"]: e for e in check(t0)}
    assert set(evs) == {f"obligation:{a}:1", f"obligation:{b}:1"}, evs
    quietgate.skip(p.db, [dict(evs[f"obligation:{a}:1"])], None, t0 + 30)
    p.db.x("UPDATE events SET status='muted' WHERE id=? AND status='queued'", (evs[f"obligation:{b}:1"]["id"],))
    check(t0 + 300)
    check(t0 + 600)
    for oid in (a, b):
        new = [t for t in ob.get(p.db, oid)["owners"] if int(t) != tid]
        assert len(new) == 1 and p.db.task(int(new[0]))["origin"] == "daemon", (oid, new)
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon'")["n"] == 2


def _order_setup(env, monkeypatch):
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    mid = p.db.post("in", "please build the deliverable", chat="c1")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now, mid))

    def raised(kinds):
        q = ",".join("?" * len(kinds))
        return p.db.q(f"SELECT * FROM events WHERE kind IN ({q}) ORDER BY id", kinds)
    return p, d, now, tid, check, mid, raised


def test_order_late_and_overdue_raise_once_with_must_act_and_survive_restart(env, monkeypatch):
    """An order no owner run started by start_by raises one order_late; at its due, one order_overdue.
    Both carry must_act; a new daemon (restart) reads the same ledger and raises nothing twice."""
    import json
    from ttp import obligations as ob
    from ttp.daemon import Daemon
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    note = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change", now=now)
    oid = note.split()[1]
    kinds = (ob.ORDER_LATE_KIND, ob.ORDER_KIND)
    check(now + 600)
    assert raised(kinds) == []
    check(now + ob.ORDER_START_S + 60)
    late = raised(kinds)
    assert [e["kind"] for e in late] == [ob.ORDER_LATE_KIND]
    assert json.loads(late[0]["data"])["must_act"] == {"key": oid, "chat": "c1", "tasks": [tid]}
    d2 = Daemon(p.base)   # a restart: the ledger is on disk
    monkeypatch.setattr(time, "time", lambda: now + 7300)
    d2._obligations_at = 0.0
    d2.check_obligations()
    d2._obligations_at = 0.0
    d2.check_obligations()
    assert [e["kind"] for e in raised(kinds)] == [ob.ORDER_LATE_KIND, ob.ORDER_KIND]
    assert ob.get(p.db, oid)["promise"]["order"] == mid


def test_an_order_asked_for_today_or_now_is_due_the_same_day(env, monkeypatch):
    """A message asking for it today or now, given no due, records its order due by the end of that
    home-zone day (at least 4 h after it, sent late); an explicit due wins, and 'not urgent' never counts."""
    from datetime import datetime, timezone
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    at = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc).timestamp()
    late = datetime(2026, 11, 19, 22, 30, tzinfo=timezone.utc).timestamp()
    week = at + 7 * 86400
    cases = (("fix the queue today", at, None), ("do it now!", late, None), ("asap please", at, None),
             ("please build the deliverable", at, at + 26 * 3600), ("fix it now", at, at + 26 * 3600),
             ("not urgent, next week", at, week))
    notes = []
    for text, ts, due in cases:
        m = p.db.post("in", text, chat="c1")
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (ts, m))
        notes.append(ob.add_order(p, "x", m, [tid], due=due, action_class="change", now=ts))
    dues = [ob.get(p.db, n.split()[1])["promise"]["due"] for n in notes]
    day = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc).timestamp()
    assert dues == [day, late + 4 * 3600, day, at + 26 * 3600, at + 26 * 3600, week]
    assert "asks for it today" in notes[0] and not any("asks for it today" in n for n in notes[3:])
    assert not ob.SAME_DAY.search("not urgent, next week") and not ob.SAME_DAY.search("this isn't urgent")


def test_order_started_owner_retires_the_late_event(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    ob.add_order(p, "build it", mid, [tid], action_class="read", now=now)
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?,?,?,?)", (tid, "worker", now + 60, "running"))
    check(now + ob.ORDER_START_S + 60)
    assert raised((ob.ORDER_LATE_KIND,)) == []


def test_order_owner_in_a_live_batch_run_is_started_and_late_once_that_run_is_gone(env, monkeypatch):
    from ttp import batchrun, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    lead = p.db.add_task("lead", "s", kind="work", tier="light", origin="user")
    p.db.update_task(lead, status="running")
    t2 = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    batchrun.record(p.db, p.db.task(lead), [p.db.task(tid), p.db.task(t2)], "ttp/t-lead", p.config())
    ob.add_order(p, "build it", mid, [tid], action_class="read", now=now)
    check(now + ob.ORDER_START_S + 60)
    assert raised((ob.ORDER_LATE_KIND,)) == []
    mid2 = p.db.post("in", "build the other thing", chat="c1")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now, mid2))
    p.db.update_task(lead, status="done")   # the run is gone without having started the second owner
    ob.add_order(p, "build the other thing", mid2, [t2], action_class="read", now=now)
    check(now + ob.ORDER_START_S + 120)
    assert [e["kind"] for e in raised((ob.ORDER_LATE_KIND,))] == [ob.ORDER_LATE_KIND]


def test_claim_expect_probe_miss_raises_claim_failed_once(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    out = p.db.post("out", "it is live", chat="c1", kind="reply")
    notes = ob.record_claims(p, [(out, "c1", [{"text": "the service is live", "expect": "false"}])], {}, now=now)
    oid = notes[0].split()[1]
    assert ob.due_claims(p.db, now) == [(oid, "false", f"claim:{oid}:{int(now)}")]
    assert ob.claim_result(p, oid, 75, now + 10) == []   # cannot answer yet: keeps waiting
    fps = ob.claim_result(p, oid, 1, now + 20)
    assert len(fps) == 1 and ob.claim_result(p, oid, 1, now + 30) == []
    ev = raised((ob.CLAIM_KIND,))
    assert len(ev) == 1 and oid in ev[0]["text"]
    out2 = p.db.post("out", "done", chat="c1", kind="reply")
    n2 = ob.record_claims(p, [(out2, "c1", [{"text": "it passes", "expect": "true"}])], {}, now=now)
    ob.claim_result(p, n2[0].split()[1], 0, now + 5)
    assert ob.get(p.db, n2[0].split()[1])["receipt"]


def test_a_claim_probe_whose_host_did_not_answer_is_unchecked_and_retried(env, monkeypatch):
    """Upstream note b8cfa86d5ff92f74: a claim probe that exits 255 (ssh could not reach its host, as
    while it reboots) is "could not check", never "does not hold": no claim_failed even past
    CLAIM_WAIT_S, the ledger says it is unchecked, due_claims keeps running it, and a later 0 holds."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    out = p.db.post("out", "it is up", chat="c1", kind="reply")
    notes = ob.record_claims(p, [(out, "c1", [{"text": "the box is up", "expect": "ssh box true"}])], {}, now=now)
    oid = notes[0].split()[1]
    late = now + ob.CLAIM_WAIT_S + 100
    assert ob.claim_result(p, oid, 255, now + 10) == [] and ob.claim_result(p, oid, 255, late) == []
    assert raised((ob.CLAIM_KIND,)) == []
    assert [c[0] for c in ob.due_claims(p.db, late)] == [oid], "retried on the next pass"
    assert any(oid in ln and "unchecked" in ln and "255" in ln for ln in ob.lines(p.db, late))
    ob.claim_result(p, oid, 0, late + 60)
    assert ob.get(p.db, oid)["receipt"] and raised((ob.CLAIM_KIND,)) == []


def test_a_claim_never_takes_another_callers_earlier_probe_answer_and_its_own_run_decides(env, monkeypatch):
    """Another caller ran the claim's probe string shortly before its due and got 1 (cached for
    STALL_PROBE_KEEP_S): check_claims runs its own probe under its own key and fails nothing on that
    answer; its fresh run after the due decides the claim."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    out = p.db.post("out", "it is live", chat="c1", kind="reply")
    due = now + 120
    [line] = ob.record_claims(p, [(out, "c1", [{"text": "the service is live", "expect": "true", "due": due}])],
                              {}, now=now)
    oid = line.split()[1]
    d._stall_probes["true"] = [None, due - 30, 1]   # a probe stall's re-check answered 1 before the due
    monkeypatch.setattr(time, "time", lambda: due + 5)
    d._claims_at = 0.0
    d.check_claims()
    assert raised((ob.CLAIM_KIND,)) == [] and ob.get(p.db, oid)["promise"]["claim"]["last"] is None
    [key] = [k for k in d._stall_probes if k != "true"]
    assert key == f"claim:{oid}:{int(due)}"
    d._stall_probes[key][0].wait(_load_scaled(30))
    d._claims_at = 0.0
    d.check_claims()
    assert raised((ob.CLAIM_KIND,)) == [] and ob.get(p.db, oid)["receipt"]["evidence"] == "`true` exited 0"


def test_an_unstarted_order_raises_one_start_late_signal(env, monkeypatch):
    """An order whose owner never starts gets its order_late at start_by and no announced_not_started
    (at 10 min or later) on top: one start-late wake per order. Its owner, announced in a reply, is
    not raised again as an announced task either."""
    from ttp import obligations as ob, unstarted
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    p.db.update_task(tid, reply_chat="c1")
    ob.add_order(p, "build the deliverable", mid, [tid], action_class="change", now=now)
    kinds = (ob.ORDER_LATE_KIND, unstarted.ANNOUNCED_KIND)
    for at in (now + 660, now + ob.ORDER_START_S + 60, now + ob.ORDER_START_S + 900):
        check(at)
        unstarted.announced(p, at)
    assert [e["kind"] for e in raised(kinds)] == [ob.ORDER_LATE_KIND]


def _turn(d, rid, actions, mids):
    """One coordinator turn with `actions` for user messages `mids`, through the daemon's apply path."""
    from types import SimpleNamespace
    out = SimpleNamespace(structured={"actions": actions, "summary": ""}, error="", final_text="", cost_usd=0.0)
    d._finish_coordinator({"id": rid, "dir": "x"}, out, "ok", {"messages": list(mids)})


def test_a_turn_handling_a_user_message_needs_its_order_or_a_no_order_reason_once(env, monkeypatch):
    """A user message's turn records its order (add or link) or a `no_order` with a reason on an applied
    reply or noop. Missing both, it is refused once and the message comes back to the next turn; a
    second miss is let through. A relayer's findings need neither."""
    from ttp import coordinator as coord, obligations as ob, turnrules
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)

    def handled(m):
        return p.db.one("SELECT handled FROM messages WHERE id=?", (m,))["handled"]

    def refused():
        return [x for x in p.db.kv(coord.REJECTED_KEY, []) or [] if x.startswith(turnrules.PREFIX)]
    reply = {"type": "reply", "chat": "c1", "text": "on it"}
    _turn(d, 1, [reply], [mid])
    assert handled(mid) == 0 and len(refused()) == 1 and f"#{mid}" in refused()[0]
    assert f"user message #{mid} is shown to you again, once: record its order" in coord.digest(p, {}, [], [])
    _turn(d, 2, [reply], [mid])   # the same miss again: let through, the message is handled
    assert handled(mid) == 1 and refused() == []

    def fresh(text="please do it"):
        return p.db.post("in", text, chat="c1")
    # Each of these records the order or a reason: nothing is refused, the message is handled.
    m = fresh()
    cases = [
        (m, [reply, {"type": "obligation", "order": m, "text": "do it", "owners": [tid], "action_class": "change"}]),
    ]
    oid = ob_add(p, tid)
    m2 = fresh()
    cases.append((m2, [reply, {"type": "obligation", "key": oid, "order": m2}]))   # link to an open obligation
    m3 = fresh("thanks")
    cases.append((m3, [dict(reply, no_order="thanks only", order=m3)]))
    m4 = fresh("what is running?")
    cases.append((m4, [reply, {"type": "noop", "no_order": "a status question, answered"}]))   # covers the turn
    for rid, (m, actions) in enumerate(cases, start=10):
        _turn(d, rid, actions, [m])
        assert handled(m) == 1 and refused() == [], (m, actions, p.db.kv(coord.REJECTED_KEY))
    assert m2 in ob.get(p.db, oid)["orders"] and m2 in ob.order_messages(p.db)
    # Bypasses: each is refused once and the message comes back.
    bad = [
        [dict(reply, no_order="   ")],                                              # a blank reason
        [{"type": "task_add", "title": "t", "spec": "s", "no_order": "x"}],          # not a reply or noop
        [dict(reply, no_order="answered", order=999999)],                            # names another message
        [dict(reply, text="x" * 30000, no_order="answered")],                       # its reply was refused
        [{"type": "obligation", "order": None, "key": "", "text": "untracked note"}],  # an obligation, not an order
    ]
    for rid, actions in enumerate(bad, start=30):
        m = fresh()
        _turn(d, rid, actions, [m])
        assert handled(m) == 0 and len(refused()) == 1 and f"#{m}" in refused()[0], (actions, p.db.kv(coord.REJECTED_KEY))
    # A relayer's findings are no order.
    f = p.db.post("in", "", chat="c1", kind="finding")
    _turn(d, 50, [reply], [f])
    assert handled(f) == 1 and refused() == []


def ob_add(p, tid):
    from ttp import obligations as ob
    return ob.add(p, "an open obligation", [tid])


def test_a_change_or_device_order_is_never_owned_by_a_question_plan_or_review_task(env, monkeypatch):
    """Owner fit, model-free: a change or device order refuses a question, plan or review task as its
    owner, wherever the coordinator names one (the order itself, a later attach, a task_add whose spec
    names it, a class change); a read order takes any task, a change order a code, harness or work task."""
    import pytest
    from ttp import coordinator as coord, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    kinds = {k: p.db.add_task(f"a {k} task", "s", kind=k, tier="light", origin="coordinator")
             for k in ("question", "plan", "review", "code", "harness")}
    for cls in ("change", "device"):
        for k in ob.UNFIT_OWNER_KINDS:
            with pytest.raises(ValueError, match=f"is a {k} task, which cannot carry a {cls} order"):
                ob.add_order(p, "do it", mid, [kinds[k]], action_class=cls)
    assert ob.order_messages(p.db) == set()
    for k in ("question", "plan", "review"):   # a read order: any owner
        m = p.db.post("in", "what does it say?", chat="c1")
        assert "recorded" in ob.add_order(p, "read it", m, [kinds[k]], action_class="read")
    note = ob.add_order(p, "change it", mid, [kinds["code"], tid], action_class="change")
    oid = note.split()[1]
    # A later attach through the coordinator's action, and a task_add whose spec names the order.
    # The coordinator leaves the unfit owner out with a note, never rejecting the action.
    assert coord.apply(p, [{"type": "obligation", "key": oid, "owners": [kinds["plan"]]}]) == []
    assert "is a plan task, which cannot carry a change order" in " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert str(kinds["plan"]) not in ob.get(p.db, oid)["owners"]
    n = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    assert coord.apply(p, [{"type": "task_add", "title": "look at it", "spec": f"answer for obligation {oid}",
                            "kind": "question"}]) == []
    assert f"its spec names order {oid}, but it is not attached" in " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == n + 1, "the task_add was dropped"
    assert ob.get(p.db, oid)["promise"]["tasks"] == sorted([kinds["code"], tid])
    assert coord.apply(p, [{"type": "obligation", "key": oid, "owners": [kinds["harness"]]}]) == []
    # The same message's order turned into a device order keeps the fit of the owners it has.
    q = p.db.post("in", "read the log", chat="c1")
    ob.add_order(p, "read the log", q, [kinds["question"]], action_class="read")
    with pytest.raises(ValueError, match="is a question task, which cannot carry a device order"):
        ob.add_order(p, "read the log", q, [], action_class="device")


def test_a_reply_reporting_a_run_already_cited_in_that_chat_is_refused_unless_repeat(env):
    """A reply names the runs whose results it reports in `cites`; a run an earlier reply to the same chat
    cited is refused (not sent) unless `repeat` is true. Another chat, a new run, or `repeat` go out; a
    cite of no run or of a non-id is refused. Plain coordinator.apply: the stateless turn path."""
    from ttp import coordinator as coord
    p = make(env)
    r1, r2 = (p.db.x("INSERT INTO runs(task, role, status, started) VALUES(NULL, 'worker', 'ok', ?)", (time.time(),))
              for _ in range(2))

    def replies(chat):
        return p.db.one("SELECT COUNT(*) n FROM messages WHERE direction='out' AND kind='reply' AND chat=?", (chat,))["n"]
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": "bench: 12 ms", "cites": [r1]}]) == []
    assert replies("c1") == 1
    probs = coord.apply(p, [{"type": "reply", "chat": "c1", "text": "again: bench 12 ms", "cites": [r2, r1]}])
    assert len(probs) == 1 and f"run {r1}'s result went to this chat" in probs[0] and replies("c1") == 1, probs
    # the same batch: the second reply of one run is refused too
    probs = coord.apply(p, [{"type": "reply", "chat": "c1", "text": "new: 9 ms", "cites": [r2]},
                            {"type": "reply", "chat": "c1", "text": "new: 9 ms (repeat)", "cites": [r2]}])
    assert len(probs) == 1 and f"run {r2}'s result" in probs[0] and replies("c1") == 2, probs
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": "as asked: 12 ms", "cites": [r1], "repeat": True}]) == []
    assert coord.apply(p, [{"type": "reply", "chat": "c2", "text": "bench: 12 ms", "cites": [r1]}]) == []
    assert replies("c1") == 3 and replies("c2") == 1
    for bad, why in (([999999], "no run #999999"), (["r1"], "list of run ids"), ([True], "list of run ids"),
                     ("12", "list of run ids")):
        probs = coord.apply(p, [{"type": "reply", "chat": "c3", "text": "x", "cites": bad}])
        assert len(probs) == 1 and why in probs[0], (bad, probs)
    assert replies("c3") == 0


def test_the_daily_audit_counts_user_orders_unstarted_past_30_minutes_over_7_days_and_the_week_before(env, monkeypatch):
    from ttp import audit, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    assert ob.order_audit_line(p.db, now) == ""
    day = 86400

    def order(age_s, started_after_s):
        m = p.db.post("in", "do it", chat="c1")
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - age_s, m))
        oid = ob.add_order(p, "do it", m, [tid], action_class="change", now=now - age_s).split()[1]
        if started_after_s is not None:
            st = p.db.kv(ob.KV)
            st["items"][oid]["promise"]["started"] = now - age_s + started_after_s
            p.db.set_kv(ob.KV, st)
    order(2 * day, 600)           # started in time
    order(3 * day, 3600)          # started late
    order(1 * day, None)          # never started
    order(600, None)              # too new to say
    order(9 * day, 7200)          # the week before: late
    order(10 * day, 60)           # the week before: in time
    order(20 * day, None)         # out of both windows
    # Orders a reply, a memory or charter change settled (closed, no owner, no run) need no task: left out.
    for age in (1 * day, 4 * day, 8 * day):
        m = p.db.post("in", "what is the status?", chat="c1")
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - age, m))
        oid = ob.add_order(p, "answer the status question", m, [], action_class="read", now=now - age).split()[1]
        ob.close_promise(p.db, oid, "reply #9 answered it", by="coordinator", now=now - age + 60)
    m = p.db.post("in", "fix it", chat="c1")   # an open order with no owner still counts: unstarted
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 2 * day, m))
    ob.add_order(p, "fix it", m, [], action_class="change", now=now - 2 * day)
    line = ob.order_audit_line(p.db, now)
    assert line == "user orders unstarted past 30 min: 3 of 4 in the last 7 days (1 of 2 in the 7 days before)", line
    assert line in "\n".join(audit.lines(p.db, now))


def test_set_due_refusal_says_a_plain_open_obligation_is_open_and_how_to_close_it(env, monkeypatch):
    """A due move on an open obligation with no promise names it open and the way to close it (evidence
    or why); a closed one says it is closed; an unknown key is refused; an order's due still moves."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    plain = ob.add(p, "ship the report", [tid], by="coordinator", now=now)
    with pytest.raises(ValueError) as e:
        ob.set_due(p, plain, now + 3600, now=now)
    msg = str(e.value)
    assert f"{plain} is open" in msg and "evidence" in msg and "why" in msg
    assert "not an open order" not in msg
    with pytest.raises(ValueError, match="no obligation"):
        ob.set_due(p, "o999", now + 3600, now=now)
    ob.drop(p, plain, "recorded by mistake", by="coordinator", now=now)
    with pytest.raises(ob.DueNote, match="already closed \\(dropped\\)"):
        ob.set_due(p, plain, now + 3600, now=now)
    oid = ob.add_order(p, "build it", mid, [tid], due=now + 7200, action_class="change", now=now).split()[1]
    ob.record(p, oid, "receipt", "built: commit abc123", None)
    with pytest.raises(ob.DueNote, match="already closed \\(receipt recorded\\)"):
        ob.set_due(p, oid, now + 3600, now=now)
    mid2 = p.db.post("in", "please build another", chat="c1")
    open_order = ob.add_order(p, "build another", mid2, [tid], due=now + 7200, action_class="change",
                              now=now).split()[1]
    assert ob.set_due(p, open_order, now + 9000, now=now).startswith(f"{open_order} now due")
    assert ob.get(p.db, open_order)["promise"]["due"] == now + 9000


def _claim_on_task(env, monkeypatch, due_in=7200):
    """A claim backed by task `tid` that then failed: (p, check, now, tid, oid, claim events)."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    out = p.db.post("out", "the table is posted", chat="c1", kind="reply")
    [line] = ob.record_claims(p, [(out, "c1", [{"text": "the table is posted", "task": tid,
                                                "due": now + due_in}])], {}, now=now)
    p.db.update_task(tid, status="failed")
    return p, check, now, tid, line.split()[1], lambda: raised((ob.CLAIM_KIND,))


def test_a_claim_on_a_failed_task_holds_through_its_done_continuation(env, monkeypatch):
    """The named task failed, a task that continues it ended done: the claim holds, its receipt names
    the continuation, and no 'does not hold' is raised."""
    from ttp import obligations as ob
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    hop = p.db.add_task("redo the table", "s", kind="work", tier="light", labels=[f"continues:{tid}"])
    p.db.update_task(hop, status="done")
    check(now + 600)
    o = ob.get(p.db, oid)
    assert o["receipt"]["task"] == hop and f"#{hop}" in o["receipt"]["evidence"] and events() == []


def test_a_claim_on_a_failed_task_waits_for_an_open_continuation_until_its_due(env, monkeypatch):
    from ttp import obligations as ob
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    hop = p.db.add_task("Continue: the table", "s", kind="work", tier="light", parent=tid)
    for st in ("queued", "running", "waiting"):
        p.db.update_task(hop, status=st)
        check(now + 600)
        assert events() == [] and not ob.get(p.db, oid)["receipt"]
    check(now + 7300)   # still open at the due time: it does not hold, naming the open hop
    [ev] = events()
    assert "does not hold" in ev["text"] and f"continuation #{hop} is still waiting" in ev["text"]
    p.db.update_task(hop, status="done")   # it ends done later: the claim holds after all
    check(now + 7400)
    assert ob.get(p.db, oid)["receipt"]["task"] == hop


def test_a_claim_on_a_failed_task_with_no_continuation_does_not_hold_as_before(env, monkeypatch):
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    p.db.add_task("unrelated", "s", kind="work", tier="light", labels=[f"continues:{tid + 1000}"])
    check(now + 600)
    [ev] = events()
    assert f"does not hold: #{tid} is failed. You said" in ev["text"]


def test_a_claim_holds_through_a_two_hop_chain_of_continuations(env, monkeypatch):
    """failed -> failed `follows:` hop -> done `continue:` follow-up of the hop: the claim holds on the last."""
    from ttp import obligations as ob
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    a = p.db.add_task("redo it", "s", kind="work", tier="light", labels=[f"follows:{tid}"])
    p.db.update_task(a, status="failed")
    b = p.db.add_task("continue: redo it", "s", kind="work", tier="light", parent=a)
    p.db.update_task(b, status="done")
    check(now + 600)
    assert ob.get(p.db, oid)["receipt"]["task"] == b and events() == []


def test_a_continuation_cycle_never_loops_and_a_chain_of_failures_does_not_hold(env, monkeypatch):
    import json
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    a = p.db.add_task("redo it", "s", kind="work", tier="light", labels=[f"continues:{tid}"])
    b = p.db.add_task("redo it again", "s", kind="work", tier="light", labels=[f"continues:{a}"])
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"continues:{b}"]), tid))   # tid <- b <- a <- tid
    for t in (a, b):
        p.db.update_task(t, status="cancelled")
    check(now + 600)
    [ev] = events()
    assert f"#{tid} is failed, and so is each task that continues it (#{a}, #{b})" in ev["text"]


def test_order_stuck_behind_a_landing_nothing_can_make_raises_one_must_act_per_episode(env, monkeypatch):
    """An order whose owner waits on landed:#n, where n is done, its last review failed and nothing open
    carries it on: one order_landing_stuck must-act event past the grace, naming the order, the owner, n and
    the review; never twice; retired once a fix continues n; a new failed review is a new episode."""
    from ttp import deadwait, eta
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    n = p.db.add_task("the fix", "s", kind="code", status="done")
    p.db.update_task(n, branch=f"ttp/t{n}-fix")
    p.db.update_task(tid, labels=[f"start_when:landed:#{n}"])
    rv = p.db.add_task(f"Review #{n}", "s", kind="review", depends_on=[n], status="failed")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now, rv))
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 86400, action_class="change", now=now).split()[1]
    kind = [ob.LANDING_STUCK_KIND]
    check(now + deadwait.GRACE_S - 60)
    assert raised(kind) == [], "a re-review the same step queues gets the grace"
    check(now + deadwait.GRACE_S + 60)
    ev = raised(kind)
    assert len(ev) == 1 and ev[0]["severity"] == "high"
    for word in (oid, f"#{tid}", f"landed:#{n}", f"review #{rv} failed", "after landed:#"):
        assert word in ev[0]["text"], word
    import json
    assert json.loads(ev[0]["data"])["must_act"]["key"] == oid
    assert eta.estimate(p.db, tid, now + deadwait.GRACE_S + 60)["after"] == f"landed:#{n}"
    check(now + deadwait.GRACE_S + 300)
    assert len(raised(kind)) == 1, "one event per episode"
    # A fix continuing n is a path again: the event is retired.
    fix = p.db.add_task("Fix review", "s", kind="code", labels=[f"continues:{n}"])
    check(now + deadwait.GRACE_S + 400)
    assert [e["status"] for e in raised(kind)] == ["handled"] and ob.get(p.db, oid).get("landing_stuck") is None
    # The fix's re-review fails too and nothing continues it: a new episode, one new event.
    p.db.update_task(fix, status="done")
    rv2 = p.db.add_task(f"Review #{fix}", "s", kind="review", depends_on=[fix], status="failed")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now + 500, rv2))
    check(now + 500 + deadwait.GRACE_S + 60)
    ev = raised(kind)
    assert len(ev) == 2 and f"review #{rv2} failed" in ev[1]["text"]
    check(now + 500 + deadwait.GRACE_S + 120)
    assert len(raised(kind)) == 2


# A reported reply (upstream note 22debba2bfa9e948), the authored prose verbatim with a generic host and the
# harness's appended status footer verbatim with generic task ids: only the footer names times.
_AUTHORED = ("I’m returning the missing check-configuration repair and validation to the existing integration "
             "owner, then requeuing its reviewer. This replaces my reviewer-only plan; the earlier 18:08 PST "
             "review estimate is withdrawn. Consolidation and sustained streaming remain owed, without a "
             "supported completion date. Planned, not started on the host yet: I’m also assigning the "
             "encoder-only PR repair to box-a’s verified existing runtime under your fastest-route instruction.")
_FOOTER = ("\nStatus now: #{a}: queued for a worker, ETA: no estimate yet (#{a} waits on outside work: no measured "
           "finish yet); #{b}: queued for a worker, ETA 17:26 PST to start, finish 18:27 PST (critical path #{b}); "
           "#{c}: queued for a worker, ETA 17:31 PST to start, finish 17:35 PST (critical path #{c}→#{b})")


def _footer_reply(p, now, text, tasks):
    from ttp import obligations as ob
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)", (now, "out", "reply", text, 1))
    return ob.record_promises(p, [(mid, "c1", text)], tasks, now=now), ob.open_promises(p.db)


def test_promise_never_comes_from_the_harness_status_footer(env):
    """A reply whose only times are in the appended 'Status now:' footer records no promise, owned or not;
    the same footer after the sender's own timed commitment still records that one, from its words only."""
    import time
    p = make(env)
    now = time.time()
    ids = [p.db.add_task(f"leg {i}", "s", kind="work", reply_chat="c1") for i in range(3)]
    footer = _FOOTER.format(a=ids[0], b=ids[1], c=ids[2])
    notes, open_ = _footer_reply(p, now, _AUTHORED + footer, {})
    assert notes == [] and open_ == [], (notes, open_)
    notes, open_ = _footer_reply(p, now, _AUTHORED + footer, {i: "c1" for i in ids})
    assert notes == [] and open_ == [], (notes, open_)
    own = f"I'll post the merged table here by 16:00 PST once #{ids[1]} finishes."
    notes, (o,) = _footer_reply(p, now, own + footer, {})
    assert "Status now" not in o["text"] and "16:00" in o["text"], o["text"]
    assert o["promise"]["tasks"] == [ids[1]], o["promise"]


def test_promise_is_owned_by_the_batch_task_its_words_name_never_a_cancelled_one(env):
    """Of the batch's tasks replying to the promise's chat, the ones its own words name (#id) own it; with
    none named, all of them but a cancelled one do."""
    import time
    p = make(env)
    now = time.time()
    a, b, gone = (p.db.add_task(f"leg {i}", "s", kind="work", reply_chat="c1") for i in range(3))
    p.db.update_task(gone, status="cancelled")
    batch = {a: "c1", b: "c1", gone: "c1"}
    _, (o,) = _footer_reply(p, now, f"I'll post #{b}'s table here by 16:00 PST.", batch)
    assert o["promise"]["tasks"] == [b], o["promise"]
    _, open_ = _footer_reply(p, now, "I'll report the soak result here by 17:00 PST.", batch)
    (o2,) = [x for x in open_ if "soak" in x["text"]]
    assert o2["promise"]["tasks"] == [a, b], o2["promise"]


def test_a_status_line_is_checked_at_send_and_never_tracked_as_ending_done(env):
    # "#N is queued" / "has started" / "is waiting" observes the record now; it is no promise that #N ends done.
    from ttp import obligations as ob
    p = make(env)
    tid = p.db.add_task("build it", "spec")
    for text, extra in [(f"#{tid} is queued", {}), (f"#{tid} is now in the queue", {}),
                        ("the build", {"state": "queued"})]:
        c = {"text": text, "task": tid, **extra}
        ob.check_claims([c], p)
        out = p.db.post("out", text, chat="c1", kind="reply")
        [note] = ob.record_claims(p, [(out, "c1", [c])], {})
        assert note.startswith("claim not tracked") and "a status" in note
    assert ob.open_promises(p.db) == []
    for status, text in [("running", f"#{tid} has started"), ("running", f"#{tid} is running"),
                         ("waiting", f"#{tid} is waiting on its checks"), ("done", f"#{tid} has started")]:
        p.db.update_task(tid, status=status)
        ob.check_claims([{"text": text, "task": tid}], p)
    p.db.update_task(tid, status="queued")
    for c in [{"text": f"#{tid} has started", "task": tid}, {"text": "it runs", "task": tid, "state": "running"},
              {"text": f"#{tid} is waiting", "task": tid}]:
        with pytest.raises(ValueError, match="is queued in its record"):
            ob.check_claims([c], p)
    for c in [{"text": "x", "task": tid, "state": "finished"}, {"text": "x", "expect": "true", "state": "queued"}]:
        with pytest.raises(ValueError, match="claim `state`"):
            ob.check_claims([c], p)


def test_a_claim_that_names_an_outcome_stays_tracked_as_ending_done(env):
    # A real promise ("I'll report the commit", "is running and lands by 15:00", a due) keeps its tracking.
    from ttp import obligations as ob
    p = make(env)
    tid = p.db.add_task("build it", "spec")
    for text, extra in [(f"#{tid} is queued and I'll report the commit", {}), (f"#{tid} is running, lands by 15:00", {}),
                        (f"#{tid} has started", {"due": time.time() + 7200}), (f"#{tid} is done", {}),
                        (f"#{tid} is queued and #{tid + 1} is running", {})]:
        c = {"text": text, "task": tid, **extra}
        assert ob.claim_state(c) is None, text
        out = p.db.post("out", text, chat="c1", kind="reply")
        [note] = ob.record_claims(p, [(out, "c1", [c])], {})
        assert f"#{tid} ends done" in note, (text, note)


def test_a_claim_on_a_superseded_review_holds_through_the_review_that_covers_it(env, monkeypatch):
    """A superseded task (a review a later passing one covers) is closed: the claim on it holds with a
    receipt naming the covering review, no 'does not hold' is raised, and it counts as closed elsewhere."""
    from ttp import obligations as ob
    from ttp.db import SUPERSEDED, SUPERSEDED_KEY
    p, check, now, tid, oid, events = _claim_on_task(env, monkeypatch)
    p.db.update_task(tid, status=SUPERSEDED, result=json.dumps({SUPERSEDED_KEY: {"by_review": 99}}))
    check(now + 7300)
    o = ob.get(p.db, oid)
    assert o["receipt"]["task"] == tid and "superseded by review #99" in o["receipt"]["evidence"], o
    assert events() == [] and SUPERSEDED in ob.CLOSED


def _ob_handoff(p, tid, summary, status="done", handoff="done"):
    import json
    p.db.update_task(tid, status=status, result=json.dumps({"status": handoff, "summary": summary}))


def test_an_order_is_not_kept_by_its_owner_ending_with_a_hand_off_that_leaves_acceptance_unmet(env, monkeypatch):
    """_kept: the order's owner ended done but its checked hand-off is 'partial:', 'cells unreported:' or
    blocked, and a reply went to the chat: the order stays owed and lines() says why. A met continuation
    of that owner keeps it, as does a met hand-off."""
    import time as _t
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change",
                       now=now).split()[1]
    for summary, handoff in (("partial: 0 of 3 cases qualify, the table does not exist yet", "done"),
                             ("cells unreported: a, b", "done"), ("the input is missing", "blocked")):
        _ob_handoff(p, tid, summary, handoff=handoff)
        p.db.post("out", "here is where it stands", chat="c1", kind="reply")
        check(_t.time() + 60)
        o = ob.get(p.db, oid)
        assert o["receipt"] is None, (summary, o["receipt"])
        line = next(ln for ln in ob.lines(p.db) if ln.startswith(oid))
        assert "held open:" in line and f"#{tid}" in line, line
    hop = p.db.add_task("finish the deliverable", "s", kind="work", tier="light", labels=[f"continues:{tid}"])
    ob.attach(p, oid, [hop])
    _ob_handoff(p, hop, "the table is built: 3 cases qualify")
    p.db.post("out", "the table is posted", chat="c1", kind="reply")
    check(_t.time() + 120)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_a_met_order_owner_still_keeps_the_order(env, monkeypatch):
    import time as _t
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change",
                       now=now).split()[1]
    _ob_handoff(p, tid, "built it; the table has 3 rows")
    p.db.post("out", "the table is posted", chat="c1", kind="reply")
    check(_t.time() + 60)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_a_delivered_obligation_is_not_closed_when_its_owner_hand_off_leaves_acceptance_unmet(env, monkeypatch):
    """_delivered/deliver(): a reply's `delivers` names the obligation and its owner ended, but failed or
    with a blocked hand-off: the obligation stays owed, the note says why, check() leaves it open."""
    import time as _t
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "build the deliverable", [tid], now=now)
    _ob_handoff(p, tid, "the input does not exist yet", handoff="blocked")
    mid = p.db.post("out", "done", chat="c1", kind="reply")
    [note] = ob.deliver(p, [(mid, "c1", [oid])], now=now + 10)
    assert "not closed" in note and f"#{tid}" in note, note
    check(_t.time() + 60)
    assert ob.get(p.db, oid)["receipt"] is None
    _ob_handoff(p, tid, "tried", status="failed", handoff="failed")
    check(_t.time() + 120)
    assert ob.get(p.db, oid)["receipt"] is None
    _ob_handoff(p, tid, "built it")
    check(_t.time() + 180)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_a_receipt_citing_only_an_owner_hand_off_that_leaves_acceptance_unmet_is_kept_as_progress(env, monkeypatch):
    """record(): a receipt whose evidence names only owners whose hand-off is partial, or filed by an owner
    task that ended so, is kept as progress naming why, the obligation still owed; evidence naming a met
    owner or no owner is taken."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "build the deliverable", [tid], now=now)
    _ob_handoff(p, tid, "partial: 0 cases qualify")
    assert f"receipt recorded as progress, not a receipt: #{tid}'s hand-off is marked" in ob.record(
        p, oid, "receipt", f"#{tid} finished", None, now=now)
    assert "acceptance is not met" in ob.record(p, oid, "receipt", "done", tid, now=now)
    assert ob.get(p.db, oid)["owners"][str(tid)]["evidence"][-1]["text"] == "done"
    assert ob.get(p.db, oid)["receipt"] is None
    other = p.db.add_task("build it again", "s", kind="work", tier="light")
    oid2 = ob.add(p, "build it again", [other], now=now)
    _ob_handoff(p, other, "built it: 3 rows")
    assert "no longer owed" in ob.record(p, oid2, "receipt", f"#{other} built it", None, now=now)
    assert "no longer owed" in ob.record(p, oid, "receipt", "commit abc123 has the table", None, now=now)


def test_an_order_whose_bounded_owner_hands_off_incomplete_stays_open_until_an_explicit_receipt(env, monkeypatch):
    """The bounded owner ends done marked 'partial:' (delivery incomplete) and a full-delivery successor is
    attached: the order stays open with its message and due through both and the successor's met finish
    (the successor carries none of the owner's work), closes only on an explicit receipt, and keeps its
    history once closed."""
    import time as _t
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change",
                       now=now).split()[1]

    def still_open():
        o = ob.get(p.db, oid)
        assert o["receipt"] is None and o["promise"]["order"] == mid and o["promise"]["due"] == now + 7200, o
        return o
    _ob_handoff(p, tid, "partial: 0 of 4 cases qualify, adoption incomplete")
    p.db.post("out", "here is where it stands", chat="c1", kind="reply")
    check(_t.time() + 60)
    still_open()
    full = p.db.add_task("deliver all 4 cases", "s", kind="work", tier="light")
    ob.attach(p, oid, [full])
    o = still_open()
    assert {tid, full} <= set(ob._owner_ids(o)), o
    _ob_handoff(p, full, "all 4 cases qualify; adoption done")
    p.db.post("out", "all 4 cases are in", chat="c1", kind="reply")
    check(_t.time() + 120)
    still_open()
    line = next(ln for ln in ob.lines(p.db) if ln.startswith(oid))
    assert "held open:" in line and f"#{tid}" in line, line
    ob.record(p, oid, "receipt", f"#{full} delivered all 4 cases; checked in its hand-off", None, now=now + 200)
    o = ob.get(p.db, oid)
    assert o["receipt"] is not None and o["promise"]["order"] == mid, o
    assert any(ln.startswith(oid) for ln in ob.lines(p.db)), "closed history is kept"


def test_an_acknowledgement_only_run_neither_closes_its_task_nor_submits_it_for_review(env, monkeypatch):
    """A worker run that exits 0 with only an acknowledgement in its output and no hand-off is not done and
    not in review: its order stays open with its original message and due."""
    from ttp import obligations as ob
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    d = Daemon(p.base)
    now = time.time()
    mid = p.db.post("in", "please build the deliverable", chat="c1")
    tid = p.db.add_task("build the deliverable", "make it", kind="work", tier="light", origin="user")
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change",
                       now=now).split()[1]
    monkeypatch.setenv("TTP_FAKE_RESULT", "none")
    monkeypatch.setenv("TTP_FAKE_SUMMARY", "Acknowledged; starting on it now.")
    d.dispatch()
    _finish_runs(p, d)
    t = p.db.task(tid)
    assert t["status"] not in ("done", "review"), dict(t)
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o["promise"]["order"] == mid and o["promise"]["due"] == now + 7200, o


def test_an_on_pass_cmd_that_writes_a_review_hand_off_then_exits_nonzero_is_not_success(env, monkeypatch):
    """A waiting hand-off's on_pass_cmd writes a needs_review hand-off to $TTP_RESULT and exits 1: the task
    is woken at its own tier, not put in review or done, and its order stays open with its original due."""
    from ttp import obligations as ob
    from ttp.daemon import Daemon
    p = make(env)
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    now = time.time()
    cmd = """printf '{"status": "needs_review", "summary": "acknowledged"}' > "$TTP_RESULT"; exit 1"""
    monkeypatch.setenv("TTP_FAKE_SESSION", "s1")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "waiting", "summary": "job runs", "waiting_for": "its job", "retry_when": "true",
        "retry_after_s": 600, "on_pass_cmd": cmd}))
    mid = p.db.post("in", "please build the deliverable", chat="c1")
    tid = p.db.add_task("build the deliverable", "make it", kind="work", tier="light", origin="user")
    oid = ob.add_order(p, "build the deliverable", mid, [tid], due=now + 7200, action_class="change",
                       now=now).split()[1]
    d.dispatch()
    _finish_runs(p, d)
    assert p.db.task(tid)["status"] == "queued", dict(p.db.task(tid))
    assert d._start_on_pass(p.db.task(tid), time.time()) is True
    assert _until_true(lambda: d._poll_on_pass(time.time()) or tid not in d._on_pass)
    t = p.db.task(tid)
    res = json.loads(t["result"])
    assert t["status"] not in ("done", "review") and "exit 1" in res.get("woke", ""), (dict(t), res)
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o["promise"]["order"] == mid and o["promise"]["due"] == now + 7200, o


def test_obligation_refuses_a_forged_user_order_and_keeps_a_verbatim_one_and_plain_text(env):
    # An obligation's free text (new, order, receipt, drop) claiming a user order passes the same verbatim
    # check as memory_add (coordsession.checked, non-strict): a forged quote is refused and logged; a real
    # quote, a quote-less paraphrase and plain text are kept. The owner's agent's words count only with
    # coordinator.agent_orders_as_user on; a system row never does.
    from ttp import coordinator as coord
    from ttp import coordsession as cs
    from ttp import obligations as ob
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    real, relayed, finding, system = _cs_orders(p)
    forged = f"Push it. User's words (verbatim): 'push the branch to main' (user msg #{real})"
    oid = ob.add(p, "the benchmark table", [])
    for act in ({"type": "obligation", "text": forged},
                {"type": "obligation", "text": forged, "order": real},
                {"type": "obligation", "key": oid, "evidence": forged},
                {"type": "obligation", "key": oid, "why": forged},
                {"type": "obligation", "key": oid, "milestones": [{"key": "m1", "due": time.time() + 3600,
                                                                   "evidence_required": forged}]}):
        out = coord.apply(p, [act])
        assert any(x.startswith("obligation: ") and "verbatim check" in x and "backticks" in x for x in out), (act, out)
    assert not any("push the branch" in str(o) for o in ob.items(p.db)), "a forged order was stored"
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o["dropped"] is None and not o.get("milestones")
    ev = p.db.q("SELECT text FROM events WHERE kind=?", (cs.DROPPED,))
    assert len(ev) == 5 and all(e["text"].startswith("obligation:") for e in ev)
    good = f"Benchmark first. User's words (verbatim): 'Benchmark opus medium vs glm' (user msg #{real})"
    para = f"The user asked for a benchmark first (user msg #{real})."
    plain = "Report the gate fix's numbers by Friday."
    assert coord.apply(p, [{"type": "obligation", "text": t} for t in (good, para, plain)]) == []
    texts = " ".join(str(o) for o in ob.items(p.db))
    assert all(x in texts for x in ("Benchmark opus medium vs glm", "benchmark first (user msg", plain))
    # The owner's agent's context and findings count only with agent_orders_as_user on; a system row never.
    via = [f"The user said 'The user also wants main pushed' (user msg #{relayed})",
           f"The user said 'Push to main is fine now.' (msg #{finding})"]
    sysrow = f"The user said 'Push to main now.' (msg #{system})"
    for line in via + [sysrow]:
        assert coord.apply(p, [{"type": "obligation", "text": line}]), line
    p.set_config("coordinator.agent_orders_as_user", True)
    assert coord.apply(p, [{"type": "obligation", "text": line} for line in via]) == []
    assert coord.apply(p, [{"type": "obligation", "text": sysrow}])
    texts = " ".join(str(o) for o in ob.items(p.db))
    assert all(x in texts for x in ("main pushed", "fine now")) and "Push to main now." not in texts


def _ob_said(p, chat, text, tasks, now):
    """Record one sent reply's promises; return the new open promises from it."""
    from ttp import obligations as ob
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)", (now, "out", "reply", text, 1))
    ob.record_promises(p, [(mid, chat, text)], tasks, now=now)
    return [o for o in ob.open_promises(p.db) if o["promise"]["message"] == mid]


def test_a_plan_on_a_condition_with_no_time_is_never_a_promise_but_a_timed_one_is(env):
    """'If the rerun passes, I'll post the numbers' commits to no time: no promise. One naming a time
    ('by 17:00') is a promise due then; 'even if' is no condition."""
    from datetime import datetime, timedelta
    from ttp import obligations as ob, timefmt
    for s in ("If the rerun passes, I'll post the numbers here.", "I'll post the numbers if the rerun passes.",
              "Unless the box is busy, I'll start the soak.", "In case it fails, I will rerun it."):
        assert ob.commitments(s) == [], s
    assert ob.commitments("I'll post the numbers even if the run fails.")
    p = make(env)
    tz = timefmt.tzinfo(p)
    base = datetime.now(tz).replace(hour=9, minute=0, second=0, microsecond=0)
    now = base.timestamp()
    at_17 = base.replace(hour=17).timestamp()
    assert _ob_said(p, "c1", "If the rerun passes, I'll post the numbers here.", {}, now) == []
    for i, s in enumerate(("If the checks pass, I'll report back by 17:00.", "I'll report back by 17:00.")):
        got = _ob_said(p, f"c{i + 2}", s, {}, now + i)
        assert len(got) == 1 and got[0]["promise"]["timed"] and got[0]["promise"]["due"] == at_17, s


def test_a_condition_in_another_clause_leaves_an_unconditional_promise(env):
    """Only a condition gating the commitment itself drops it: one leading the sentence or right after the
    commitment in its own clause. A trailing condition on another clause keeps the promise (fail open)."""
    from ttp import obligations as ob
    for s in ("I will report back and tell you if it fails.", "I'll report back; if the check fails I'll rerun it.",
              "I'll post the numbers here, and flag anything if it fails.",
              "I'll report back; I'll rerun it if the check fails."):
        assert ob.commitments(s) == [s], s
    for s in ("If the rerun passes, I'll post the numbers here.", "And if the rerun passes, I'll post the numbers.",
              "I'll post the numbers if the rerun passes.", "Unless the box is busy, I'll start the soak."):
        assert ob.commitments(s) == [], s
    p = make(env)
    now = time.time()
    assert len(_ob_said(p, "c1", "I will report back and tell you if it fails.", {}, now)) == 1
    assert _ob_said(p, "c2", "If the rerun passes, I'll post the numbers here.", {}, now + 1) == []


def test_obligation_actions_that_change_nothing_owed_are_notes_and_kept_gates_still_refuse(env, monkeypatch):
    """#2116 part 2: an obligation action whose wording was off but whose meaning is clear goes through
    or is noted, never rejected: a due move on a closed obligation or one with no due, an order linked to
    a closed key (recorded as a new order), and finished or unfit owners (left out, the rest attached).
    Still refused in the same batch: a missed due with no plan change, an unknown milestone on one that
    has milestones, a Restrictions change without the user's word and a task_update touching a guard line.
    coordprecheck refuses none of the noted ones either."""
    from ttp import coordinator as coord, coordprecheck, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")

    def notes():
        return " ".join(p.db.kv(coord.NOTES_KEY) or [])
    done = p.db.add_task("finished", "s", kind="code", tier="light", origin="coordinator")
    p.db.update_task(done, status="done")
    rev = p.db.add_task("a review", "s", kind="review", tier="light", origin="coordinator")
    code = p.db.add_task("the change", "s", kind="code", tier="light", origin="coordinator")
    # An order whose owners include a finished and a review task: recorded with the code task only.
    assert coord.apply(p, [{"type": "obligation", "order": mid, "text": "build it", "action_class": "change",
                            "owners": [done, rev, code]}]) == []
    oid = ob.open_order(p.db, mid)["id"]
    assert ob.get(p.db, oid)["promise"]["tasks"] == [code]
    assert f"#{done} is done, so not attached" in notes() and "is a review task" in notes()
    # Due moves that change nothing owed: closed (receipt, dropped) and open with no due to move.
    plain = ob.add(p, "a plain one", [tid], now=now)
    gone = ob.add(p, "dropped one", [tid], now=now)
    ob.drop(p, gone, "by mistake", by="coordinator")
    for key, why in ((plain, "is open but has no promise"), (gone, "is already closed (dropped)")):
        act = {"type": "obligation", "key": key, "due": now + 3 * 3600}
        assert coordprecheck.problems(p, [dict(act)]) == [] and coord.apply(p, [act]) == [], key
        assert f"{key} {why}" in notes()
    # An order linked to a closed key is recorded as a new order, its text and class carried over.
    ob.record(p, oid, "receipt", "commit abc", None)
    again = p.db.post("in", "build it again", chat="c1")
    act = {"type": "obligation", "key": oid, "order": again, "owners": [code]}
    assert coordprecheck.problems(p, [dict(act)]) == [] and coord.apply(p, [act]) == []
    new = ob.open_order(p.db, again)
    assert new and new["id"] != oid and new["promise"]["class"] == "change" and new["text"] == "build it"
    assert f"obligation {oid} is closed: the order of message #{again} is recorded as a new one" in notes()
    # Kept: an unknown milestone, a Restrictions change without a source and a task_update replacing a
    # guard line. A missed due with no plan change is applied and its chat told (replan.bare_move).
    late = p.db.post("in", "and ship it", chat="c1")
    ob.add_order(p, "ship it", late, [code], due=now - 60, action_class="change", now=now - 7200)
    lid = ob.open_order(p.db, late)["id"]
    guarded = p.db.add_task("loader", "1. load\n\nRestrictions:\n- never push to main\n", kind="work")
    before = p.charter_path.read_text()
    ob.add_milestones(p, plain, [{"key": "draft", "due": now + 3600, "evidence_required": "a draft"}])
    probs = coord.apply(p, [
        {"type": "obligation", "key": lid, "due": now + 7200},
        {"type": "obligation", "key": plain, "evidence": "done", "milestone": "nope"},
        {"type": "charter_update", "section": "Restrictions", "quote": "Never merge.", "text": "Merging is fine."},
        {"type": "task_update", "id": guarded, "replace": {"old": "- never push to main", "new": ""}}])
    assert len(probs) == 3, probs
    assert ob.get(p.db, lid)["promise"]["due"] == now + 7200 and f"{lid} missed its due" in notes()
    assert any("no milestone nope" in x and "its milestones: draft" in x for x in probs), probs
    assert any(x.startswith("charter_update") for x in probs) and p.charter_path.read_text() == before
    assert any("touches a safety line" in x for x in probs), probs


def test_a_worker_receipt_closes_only_its_own_part_and_reopen_undoes_a_mistaken_receipt(env, monkeypatch):
    """Worker #297 filed a receipt for the before numbers alone and closed the user's whole order:
    a worker's receipt delivers its own task's part; the order stays owed while another open owner has
    none. An ended owner's part was carried on by a later owner. `reopen` undoes a receipt."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    other = p.db.add_task("the fixes", "make them", kind="code", tier="standard", origin="coordinator")
    oid = ob.add(p, "before numbers and the fixes", [tid, other], now=now)
    msg = ob.record(p, oid, "receipt", "before numbers posted", tid, now=now + 60)
    o = ob.get(p.db, oid)
    assert not o["receipt"] and o["owners"][str(tid)]["receipt"] and f"#{other}" in msg
    assert all(t != str(tid) for t, _ in ob.overdue(o, now + 3 * ob.CLOCK_S))   # a delivered part has no clock
    assert any(f"#{tid} part delivered" in line for line in ob.lines(p.db, now=now + 120))
    assert "no longer owed" in ob.record(p, oid, "receipt", "fixes landed", other, now=now + 240)
    assert ob.get(p.db, oid)["receipt"]["task"] == other
    assert "reopened" in ob.reopen(p, oid, "filed for a part only", now=now + 300)
    o = ob.get(p.db, oid)
    assert not o["receipt"] and not any(own.get("receipt") for own in o["owners"].values())
    assert o["reopened"][0]["receipt"]["task"] == other and str(tid) in o["reopened"][0]["parts"]
    assert "nothing to reopen" in ob.reopen(p, oid, "again", now=now + 360)
    with pytest.raises(ValueError):
        ob.reopen(p, oid, " ")


def test_a_worker_receipt_closes_the_obligation_once_the_other_owners_ended(env, monkeypatch):
    """A done or cancelled other owner (a carrier chain: fix, review, continue) never holds the receipt."""
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    old = p.db.add_task("first try", "x", kind="work", tier="light", origin="coordinator")
    oid = ob.add(p, "the deliverable", [old, tid], now=now)
    p.db.update_task(old, status="done")
    assert "no longer owed" in ob.record(p, oid, "receipt", "built it", tid, now=now + 60)
    oid2 = ob.add(p, "another", [tid], now=now)
    assert "no longer owed" in ob.record(p, oid2, "receipt", "single owner", tid, now=now + 60)


def test_a_refused_run_record_is_filed_and_noted_once_and_a_transient_failure_retries(env, monkeypatch):
    """A run's record its task may not file (not an owner) is refused once: read again after its line
    count is lost (a closed task's offset pruned, the task re-queued), it is not refused, logged or
    noted again. A failure that may pass (the database busy) files nothing and the next pass files it."""
    import sqlite3
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "the urgent report", [tid], now=now)
    other = ob.add(p, "another report", [], now=now)
    run_dir = p.runs / "r-refused"
    run_dir.mkdir(parents=True)
    p.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
           (tid, "worker", now, "running", str(run_dir)))
    rid = str(p.db.q("SELECT id FROM runs WHERE dir=?", (str(run_dir),))[0]["id"])
    (run_dir / ob.RUN_FILE).write_text(json.dumps({"ts": now + 60, "id": other, "kind": "progress",
                                                   "evidence": "commit old"}) + "\n")
    refused = ob.file_runs(p, now + 100)
    assert len(refused) == 1 and "not a required owner" in refused[0]
    st = ob._load(p.db)
    st["runs"].pop(rid)   # its line count lost: the record is read again from line 0
    p.db.set_kv(ob.KV, st)
    assert ob.file_runs(p, now + 200) == [], "not refused or logged again"
    assert (run_dir / "progress.md").read_text().count("obligation record refused") == 1
    ob.append_run(run_dir, oid, "progress", "commit abc", str(tid))
    real, calls = ob.record, []

    def busy(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)
    monkeypatch.setattr(ob, "record", busy)
    assert ob.file_runs(p, now + 300) == []
    assert ob.get(p.db, oid)["owners"][str(tid)]["evidence"] == [], "nothing filed while the database is busy"
    assert ob.file_runs(p, now + 400) == []
    assert [e["text"] for e in ob.get(p.db, oid)["owners"][str(tid)]["evidence"]] == ["commit abc"]
    assert (run_dir / "progress.md").read_text().count("obligation record refused") == 1


def test_an_order_start_stamp_is_its_first_owner_runs_own_start_even_when_attached_late(env, monkeypatch):
    """An owner attached past the start limit whose run began 3 min after the message stamps the order
    started at that run's start, not at the check: the order is not late."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", now + 180, now + 900, "done"))
    oid = ob.add_order(p, "build it", mid, [], action_class="read", now=now).split()[1]
    ob.attach(p, oid, [tid], now=now + 2000)
    check(now + 2100)
    assert ob.get(p.db, oid)["promise"]["started"] == now + 180
    assert ob.order_starts(p.db, now - 1, now + 1, now + 2100) == (1, 0)


def test_an_order_closed_by_a_receipt_before_any_start_stamp_counts_its_on_time_run_not_late_forever(
        env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build it", mid, [tid], action_class="read", now=now).split()[1]
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", now + 300, now + 600, "done"))
    ob.close_promise(p.db, oid, "delivered", by="coordinator", now=now + 700)   # no check ran in between
    assert ob.get(p.db, oid)["promise"].get("started") is None
    assert ob.order_starts(p.db, now - 1, now + 1, now + 9 * 86400) == (1, 0)


def test_a_follow_up_order_on_an_ordered_message_starts_its_clock_at_its_own_record(env, monkeypatch):
    """A second order for a message whose first order is closed is on time when its owner runs within the
    limit of its own record, not of the message: no order_late alarm, no late count, the first order kept."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    first = ob.add_order(p, "first", mid, [tid], action_class="read", now=now).split()[1]
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", now + 60, now + 120, "done"))
    ob.close_promise(p.db, first, "delivered", by="coordinator", now=now + 300)
    later = now + 4 * 3600
    again = ob.add_order(p, "follow-up", mid, [], action_class="read", now=later).split()[1]
    pr = ob.get(p.db, again)["promise"]
    assert pr["start_by"] == later + ob.ORDER_START_S
    check(later + 60)
    assert ob.get(p.db, again)["promise"]["late"] is None and raised((ob.ORDER_LATE_KIND,)) == []
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", later + 600, later + 900, "done"))
    ob.attach(p, again, [tid], now=later + 700)
    check(later + 800)
    assert ob.order_starts(p.db, now - 1, now + 1, later + 900) == (2, 0)


def test_a_follow_up_order_owned_by_the_task_that_ran_for_the_first_order_is_late_until_it_runs_again(
        env, monkeypatch):
    """A follow-up order owned by the task that already ran for the first order is not started by
    that old run: with no new run it raises one order_late past its own start_by and counts late."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    first = ob.add_order(p, "first", mid, [tid], action_class="read", now=now).split()[1]
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", now + 60, now + 120, "done"))
    ob.close_promise(p.db, first, "delivered", by="coordinator", now=now + 300)
    later = now + 4 * 3600
    again = ob.add_order(p, "follow-up", mid, [tid], action_class="read", now=later).split()[1]
    check(later + 60)
    assert ob.get(p.db, again)["promise"]["started"] is None, "the run for the first order does not start it"
    check(later + ob.ORDER_START_S + 60)
    pr = ob.get(p.db, again)["promise"]
    assert pr["late"] is not None and len(raised((ob.ORDER_LATE_KIND,))) == 1
    assert ob.order_starts(p.db, now - 1, now + 1, later + ob.ORDER_START_S + 120) == (2, 1)


def test_a_follow_up_order_running_again_within_its_limit_starts_at_the_new_run(env, monkeypatch):
    """The same task running again within ORDER_START_S of the follow-up's record starts the order at
    the new run's own start: on time, no order_late, no late count."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    first = ob.add_order(p, "first", mid, [tid], action_class="read", now=now).split()[1]
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", now + 60, now + 120, "done"))
    ob.close_promise(p.db, first, "delivered", by="coordinator", now=now + 300)
    later = now + 4 * 3600
    again = ob.add_order(p, "follow-up", mid, [tid], action_class="read", now=later).split()[1]
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
           (tid, "worker", later + 600, later + 900, "done"))
    check(later + 700)
    assert ob.get(p.db, again)["promise"]["started"] == later + 600
    assert ob.get(p.db, again)["promise"]["late"] is None and raised((ob.ORDER_LATE_KIND,)) == []
    assert ob.order_starts(p.db, now - 1, now + 1, later + 900) == (2, 0)


def test_an_order_whose_owners_are_all_deferred_or_dropped_is_no_late_start(env, monkeypatch):
    """Owners deferred past the start limit (start_after, a start_when probe) or all cancelled unrun leave
    the order out of the late count; the deferral is kept on the order, so it holds after the label goes."""
    import json
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    ob.add_order(p, "build it later", mid, [tid], action_class="read", now=now)
    p.db.update_task(tid, labels=json.dumps([f"start_after:{now + 3 * 3600}", "start_why:report after the soak"]),
                     not_before=now + 3 * 3600)
    check(now + ob.ORDER_START_S + 60)
    assert ob.order_starts(p.db, now - 1, now + 1, now + 2 * 3600) == (0, 0)
    p.db.update_task(tid, labels="[]", not_before=None)   # the deferral passed: its labels are gone
    assert ob.order_starts(p.db, now - 1, now + 1, now + 2 * 3600) == (0, 0)
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
           (tid, now + 3 * 3600, now + 3 * 3600 + 600))
    check(now + 3 * 3600 + 700)                          # the deferred owner ran past the limit, as planned
    assert ob.order_starts(p.db, now - 1, now + 1, now + 4 * 3600) == (0, 0)
    m2 = p.db.post("in", "build the other thing", chat="c1")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now, m2))
    t2 = p.db.add_task("other", "s", kind="work", tier="light", origin="user")
    ob.add_order(p, "build the other thing", m2, [t2], action_class="read", now=now)
    p.db.update_task(t2, status="cancelled")
    assert ob.order_starts(p.db, now - 1, now + 1, now + 2 * 3600) == (0, 0)
    p.db.update_task(t2, status="queued")   # a live unrun owner still counts late
    assert ob.order_starts(p.db, now - 1, now + 1, now + 2 * 3600) == (1, 1)


def test_must_act_lists_an_order_with_no_owner_from_10_minutes_until_an_owner_or_a_reply(env, monkeypatch):
    from ttp import mustact, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build it", mid, [], action_class="change", now=now).split()[1]
    assert mustact.unowned(p.db, now + 540) == [] and mustact.block(p.db, [], now + 540) == ""
    line = mustact.block(p.db, [], now + 660)
    assert f"## {mustact.SECTION}" in line and f"- {oid}: user msg #{mid} (change) has no owner task 11 min" in line
    ob.attach(p, oid, [tid], now=now + 700)
    assert mustact.unowned(p.db, now + 720) == []
    m2 = p.db.post("in", "and the other one", chat="c1")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now, m2))
    ob.add_order(p, "other", m2, [], action_class="read", now=now)
    assert len(mustact.unowned(p.db, now + 900)) == 1
    p.db.post("out", "on it: done by 10:00", chat="c1", kind="reply")
    assert mustact.unowned(p.db, now + 900) == []


def _receipt_order_setup(env, monkeypatch, status="running", own_commit=True):
    """A change order owned by a code task whose own branch carries one fresh unpushed commit (the
    11-24 replay's shape: a run recording its receipt while its change is still unreviewed), in a
    project whose repo pushes to a bare origin. `own_commit=False` leaves the branch where the
    daemon cut it. Returns (p, repo, tid, oid)."""
    from ttp import obligations as ob
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    mid = p.db.post("in", "please build the deliverable", chat="c1")
    tid = p.db.add_task("build the deliverable", "s", kind="code", tier="standard", origin="coordinator")
    branch = f"ttp/t{tid}-build"
    _git_out(repo, "checkout", "-q", "-b", branch)   # the daemon cuts the task branch at the tip
    if own_commit:
        _commit(repo, "change.txt", "change\n")      # the run's fresh commit, unreviewed, not pushed
    p.db.update_task(tid, branch=branch)
    oid = ob.add_order(p, "build the deliverable", mid, [tid], action_class="change").split()[1]
    ob.attach(p, oid, [tid])          # a run may record for its task only once it owns the order
    p.db.update_task(tid, status=status)   # after attach: a closed task cannot be attached
    return p, repo, tid, oid


def test_a_run_s_receipt_with_only_unreviewed_commits_keeps_the_order_open(env, monkeypatch):
    """Replay of the 11-24 case: a run records its change order's receipt right after committing,
    before any review or push. The receipt text is kept as progress, the order stays open and the
    run is told why, no matter which hex ids its evidence names."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch)
    head = _git_out(repo, "rev-parse", "HEAD")
    run_dir = env["tmp"] / "run-r"; run_dir.mkdir()
    msg = ob.record(p, oid, "receipt", f"commit {head} built and checked", tid, run_dir=str(run_dir))
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o.get("unverified") is None, "an unlanded change must not close it"
    assert o["owners"][str(tid)]["evidence"][-1]["text"] == f"commit {head} built and checked", \
        "kept as progress"
    assert "not landed" in msg and "not landed" in (run_dir / "progress.md").read_text(), \
        (msg, "the run is told why")


def test_a_running_code_owner_without_commits_closes_its_change_order(env, monkeypatch):
    """A running code owner whose branch never moved has no commits of its own to land: its change
    order's receipt closes the order unchecked, with no 'has not finished' hold."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, own_commit=False)
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is not None and o.get("unverified") is None, o


def test_a_change_order_closes_once_its_owner_s_commit_is_on_the_push_branch(env, monkeypatch):
    """The same receipt once the owner's commit is on the push branch closes the order."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    _git_out(repo, "push", "-q", "origin", f"{_git_out(repo, 'rev-parse', 'HEAD')}:refs/heads/proj")
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is not None and "unverified" not in o, o


def test_a_check_that_cannot_run_marks_the_order_unverified_and_keeps_it_open(env, monkeypatch):
    """A fetch that cannot read the push branch keeps the order open, marked unverified in lines()."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _git_out(repo, "update-ref", "-d", "refs/remotes/origin/proj")
    msg = ob.record(p, oid, "receipt", "built and checked", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and "unverified" in msg and o.get("unverified"), (msg, o.get("unverified"))
    assert any("unverified" in ln for ln in ob.lines(p.db, open_only=True)), ob.lines(p.db, open_only=True)


def test_a_change_order_owned_by_a_work_task_closes_on_its_receipt(env, monkeypatch):
    """A change order owned by a work task (no repo change to land) closes on its receipt unchecked."""
    from ttp import obligations as ob
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    mid = p.db.post("in", "please roll the config out", chat="c1")
    tid = p.db.add_task("roll the config out", "s", kind="work", tier="light", origin="coordinator")
    oid = ob.add_order(p, "roll the config out", mid, [tid], action_class="change").split()[1]
    ob.attach(p, oid, [tid])
    assert "no longer owed" in ob.record(p, oid, "receipt", "the rollout is done and checked", tid)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_ownerless_orders_and_coordinator_receipts_close_a_change_order_unchanged(env, monkeypatch):
    """An ownerless change order, and a coordinator receipt on an owned one, close unchecked."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch)   # unlanded change, live code owner
    mid = p.db.post("in", "please build another one", chat="c1")
    ownerless = ob.add_order(p, "build another one", mid, [], action_class="change").split()[1]
    assert "no longer owed" in ob.record(p, ownerless, "receipt", "built and checked", None)
    assert ob.get(p.db, ownerless)["receipt"] is not None
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", None)
    assert ob.get(p.db, oid)["receipt"] is not None, "a coordinator receipt is trusted"


def test_a_hex_token_in_a_receipt_that_is_not_a_commit_cannot_hold_an_order(env, monkeypatch):
    """No commit id is parsed out of the receipt text: a hex token that is not a commit neither
    marks the order unverified nor holds it open once its owner's commit landed."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    _git_out(repo, "push", "-q", "origin", f"{_git_out(repo, 'rev-parse', 'HEAD')}:refs/heads/proj")
    assert "no longer owed" in ob.record(p, oid, "receipt", "release 5f1c9d2e3b is out", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is not None and o.get("unverified") is None, o


def test_a_change_order_in_a_project_with_only_a_landed_probe_closes_unchanged(env, monkeypatch):
    """No push branch to check against, only a landed probe: a change order's receipt closes
    unchecked, as before the gate existed."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    p.set_config("delivery.push_branch", "")
    p.set_config("delivery.landed_probe", "true")
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", tid)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_a_change_order_of_a_code_owner_without_commits_closes_unchanged(env, monkeypatch):
    """A code owner that ended with no commits of its own has nothing to land: its receipt closes
    the order unchecked."""
    from ttp import obligations as ob
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    mid = p.db.post("in", "please build the deliverable", chat="c1")
    tid = p.db.add_task("build the deliverable", "s", kind="code", tier="standard", origin="coordinator")
    oid = ob.add_order(p, "build the deliverable", mid, [tid], action_class="change").split()[1]
    ob.attach(p, oid, [tid])
    p.db.update_task(tid, status="done")   # no branch, no commits
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is not None and o.get("unverified") is None, o


def test_a_later_decided_check_clears_a_stale_unverified_mark(env, monkeypatch):
    """A receipt the check could not run on marks the order unverified; once the check can decide
    again the stale mark is cleared while the order stays open until its change lands."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _git_out(repo, "update-ref", "-d", "refs/remotes/origin/proj")
    ob.record(p, oid, "receipt", "built and checked", tid)
    assert ob.get(p.db, oid).get("unverified"), "the check could not run"
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "origin.git"))
    msg = ob.record(p, oid, "receipt", "built and checked", tid)
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and "not landed" in msg and o.get("unverified") is None, (msg, o)
    _git_out(repo, "push", "-q", "origin", f"{_git_out(repo, 'rev-parse', 'HEAD')}:refs/heads/proj")
    assert "no longer owed" in ob.record(p, oid, "receipt", "built and checked", tid)


def test_an_owner_with_a_recorded_landing_closes_as_today(env, monkeypatch):
    """A change order whose owner's landing is recorded (the push queue wrote its landing) closes on
    a receipt as it always did."""
    from ttp import obligations as ob
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES('m1', ?, ?, 'origin/proj', 0)",
           (tid, _git_out(repo, "rev-parse", "HEAD")))
    assert "no longer owed" in ob.record(p, oid, "receipt", "commit 12ab34cd56 built it", tid)
    assert ob.get(p.db, oid)["receipt"] is not None


def test_the_daemon_files_a_change_order_receipt_as_progress_until_it_lands(env, monkeypatch):
    """The daemon's own path (file_runs) under the transaction guard: a run's receipt record for a
    change order whose commit never landed is filed as progress, the order stays open, and the
    fetch the check needs runs before the write transaction."""
    import json
    import time as _t
    from ttp import obligations as ob
    monkeypatch.setenv("TTP_TX_GUARD", "1")     # a fetch inside the write transaction is refused
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    run_dir = env["tmp"] / f"run-{tid}"; run_dir.mkdir()
    p.db.x("INSERT INTO runs(task, role, started, status, dir) VALUES(?, 'worker', ?, 'running', ?)",
           (tid, _t.time(), str(run_dir)))
    (run_dir / ob.RUN_FILE).write_text(json.dumps(
        {"ts": _t.time(), "id": oid, "kind": "receipt", "evidence": "built and checked",
         "task": str(tid), "milestone": None}) + "\n")
    ob.file_runs(p)
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o["owners"][str(tid)]["evidence"], "filed as progress, still owed"
    assert "not landed" in (run_dir / "progress.md").read_text(), "the run is told why"
    assert "receipt" not in o["owners"][str(tid)]


# Action forms: an `obligation` action or run record whose meaning is clear is applied as meant.

def _obform_run(p, task, oid, kind, evidence, milestone=None):
    """A running run of task `task` that wrote a `kind` record for `oid` (RUN_FILE); its directory."""
    from ttp import obligations as ob
    run_dir = p.runs / f"r-obform-{task}"
    run_dir.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
           (task, "worker", time.time(), "running", str(run_dir)))
    ob.append_run(run_dir, oid, kind, evidence, str(task), milestone=milestone)
    return run_dir


def _obform_notes(p, coord):
    return [n for n in p.db.kv(coord.NOTES_KEY) or [] if "applied as" in n], " ".join(p.db.kv(coord.NOTES_KEY) or [])


def test_obform_a_finished_owner_is_replaced_by_its_open_heirs(env, monkeypatch):
    """Item 5 (sample: owner done #A, open "Fix review #A"): the fix is attached instead, with one note, and
    coordprecheck refuses nothing. A finished owner with two open successors gets both; one with
    none is left out with fit_owners' note and the rest attached."""
    from ttp import coordinator as coord, coordprecheck, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)

    def task(title, **kw):
        return p.db.add_task(title, "s", kind="code", tier="light", origin="coordinator", **kw)
    done, lone, twice = task("build part a"), task("build part b"), task("build part c")
    for t in (done, lone, twice):
        p.db.update_task(t, status="done")
    fix = task(f"Fix review #{done}: build part a")
    fix2 = task(f"Fix review #{twice}: build part c")
    hop2 = task("continue: build part c", parent=twice)
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    act = {"type": "obligation", "key": oid, "owners": [done, lone, twice]}
    assert coordprecheck.problems(p, [dict(act)]) == []
    assert coord.apply(p, [act]) == []
    applied, notes = _obform_notes(p, coord)
    assert applied == [f"actions[0] (obligation) applied as: owners=[{fix}, {lone}, {fix2}, {hop2}] "
                       f"(was: [{done}, {lone}, {twice}])"], applied
    assert sorted(ob.get(p.db, oid)["owners"]) == sorted([str(tid), str(fix), str(fix2), str(hop2)])
    assert f"#{lone} is done, so not attached" in notes and f"#{twice} is done" not in notes


def test_obform_a_review_or_plan_owner_of_a_change_order_is_replaced_by_the_task_making_the_change(env, monkeypatch):
    """Was refused (sample "order oN: #N is a review task, which cannot carry a change order"): a review owner is
    replaced by the code task it reviews, a plan owner by its open code child, each with a note; a plan with
    no such child is still left out with fit_owners' note."""
    from ttp import coordinator as coord, coordprecheck, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)

    def task(title, kind, **kw):
        return p.db.add_task(title, "s", kind=kind, tier="light", origin="coordinator", **kw)
    code = task("build part a", "code")
    review = task(f"review #{code}", "review", labels=[f"auto_review:{code}"])
    plan, lone = task("plan part b", "plan"), task("plan part c", "plan")
    child = task("build part b", "code", parent=plan)
    oid = ob.add_order(p, "change it", mid, [tid], action_class="change").split()[1]
    act = {"type": "obligation", "key": oid, "owners": [review, plan, lone]}
    assert coordprecheck.problems(p, [dict(act)]) == []
    assert coord.apply(p, [act]) == []
    applied, notes = _obform_notes(p, coord)
    assert applied == [f"actions[0] (obligation) applied as: owners=[{code}, {child}, {lone}] "
                       f"(was: [{review}, {plan}, {lone}])"], applied
    owners = ob.get(p.db, oid)["owners"]
    assert {str(code), str(child)} <= set(owners) and not {str(review), str(plan), str(lone)} & set(owners)
    assert f"#{lone} is a plan task, which cannot carry a change order" in notes


def test_obform_owner_0_is_left_out_and_the_rest_recorded(env, monkeypatch):
    """Item 8 (an owner sent as 0, refused as no such task): owner 0 is a placeholder, left out with one note;
    the order is recorded with the other owner and coordprecheck refuses nothing."""
    from ttp import coordinator as coord, coordprecheck, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    act = {"type": "obligation", "order": mid, "text": "build the deliverable", "action_class": "change",
           "owners": [0, tid]}
    assert coordprecheck.problems(p, [dict(act)]) == []
    assert coord.apply(p, [act]) == []
    assert _obform_notes(p, coord)[0] == [f"actions[0] (obligation) applied as: owners=[{tid}] (was: [0, {tid}])"]
    assert ob.open_order(p.db, mid)["promise"]["tasks"] == [tid]


def test_obform_a_milestone_on_an_obligation_with_none_is_progress_never_a_receipt(env, monkeypatch):
    """Item 9 (sample "no milestone watcher_live on oN; its milestones: none"), from both paths: the
    coordinator's action and a run's record the daemon files. Each is kept as progress under the
    milestone's name, the order stays owed, and coordprecheck refuses nothing."""
    from ttp import coordinator as coord, coordprecheck, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    oid = ob.add_order(p, "build the deliverable", mid, [tid], action_class="change", now=now).split()[1]
    ob.attach(p, oid, [tid], now=now)
    act = {"type": "obligation", "key": oid, "evidence": "the watcher is live", "milestone": "watcher_live"}
    assert coordprecheck.problems(p, [dict(act)]) == []
    assert coord.apply(p, [act]) == []
    assert f"{oid} has no milestones: 'watcher_live: the watcher is live' recorded as progress, not a receipt" \
        in _obform_notes(p, coord)[1]
    _obform_run(p, tid, oid, "receipt", "reset answered", milestone="reset-answer")
    assert ob.file_runs(p, now + 60) == []
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and "receipt" not in o["owners"][str(tid)]
    assert [e["text"] for e in o["owners"][str(tid)]["evidence"]] == ["reset-answer: reset answered"]
    assert [h["evidence"] for h in o["history"] if h["event"] == "progress"] == ["watcher_live: the watcher is live"]


def test_obform_a_run_s_record_from_a_task_carrying_an_owner_on_makes_it_an_owner(env, monkeypatch):
    """Item 10, the owner rule's samples: a run's record for an obligation its task does not own is filed
    when the task carries an owner's work on ("Fix review #<owner>", a "Review #" of that fix, a "continue:"
    of an owner, a parent_copy of a continuation, a task delivering onto an owner's branch) or its spec
    names the key: it is made an owner first, with a history entry and a daemon.log line. An unrelated
    task's record stays refused, and so does a review's on a change order (keep list)."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)

    def task(title, spec="s", kind="code", **kw):
        return p.db.add_task(title, spec, kind=kind, tier="light", origin="coordinator", **kw)
    p.db.update_task(tid, branch=f"ttp/t{tid}-build")
    oid = ob.add(p, "the benchmark table", [tid], now=now)
    p.db.update_task(tid, status="done")
    fix = task(f"Fix review #{tid}: build the deliverable")
    rev = task(f"Review #{fix}: build the deliverable", kind="review")
    cont = task(f"continue: missing cells of #{tid}", parent=tid)
    hop = task("build the deliverable again", labels=[f"continues:{tid}"])
    copy = task("build the deliverable, part 2", parent=hop, tier_source="parent_copy")
    onto = task("land the table", labels=[f"pr_branch:ttp/t{tid}-build"])
    says = task("write the summary", spec=f"Owns obligation {oid}: the benchmark table.", kind="work")
    other = task("something else", kind="work")
    code = task("make the change")
    order = ob.add_order(p, "make the change", mid, [code], action_class="change", now=now).split()[1]
    ob.attach(p, order, [code], now=now)
    review = task(f"Review #{code}: make the change", kind="review", labels=[f"auto_review:{code}"])
    for t in (fix, rev, cont, copy, onto, says, other):
        _obform_run(p, t, oid, "progress", f"step of #{t}")
    _obform_run(p, review, order, "progress", "reviewed")
    p.db.update_task(fix, status="done")   # its run's records are filed after it ended
    refused = ob.file_runs(p, now + 60)
    assert sorted(refused) == sorted(x for x in refused if "not a required owner" in x) and len(refused) == 2
    assert any(f"#{other} is not" in x for x in refused) and any(f"#{review} is not" in x for x in refused)
    o = ob.get(p.db, oid)
    for t in (fix, rev, cont, copy, onto, says):
        assert [e["text"] for e in o["owners"][str(t)]["evidence"]] == [f"step of #{t}"], t
    assert str(other) not in o["owners"] and str(review) not in ob.get(p.db, order)["owners"]
    want = {fix: f"descends from #{tid}", rev: f"descends from #{fix}", cont: f"descends from #{tid}",
            copy: f"descends from #{tid}", onto: f"descends from #{tid}", says: f"its spec names {oid}"}
    assert {h["task"]: h["why"] for h in _ob_history(p, oid)} == \
        {t: f"#{t} {why}: its run's record is filed" for t, why in want.items()}
    log = (p.logs / "daemon.log").read_text()
    assert all(f"obligation owner added: #{t} ({why}) for {oid}" in log for t, why in want.items()), log


def test_obform_an_adopted_fix_s_receipt_still_waits_for_its_change_to_land(env, monkeypatch):
    """sa109 unchanged: a "Fix review #<owner>" run on the owner's branch files the change order's receipt
    through the daemon (under the transaction guard). It is made an owner, then judged as any owner's
    receipt: its commit is not landed, so the receipt is kept as progress and the order stays open, though
    its text names that commit."""
    from ttp import obligations as ob
    monkeypatch.setenv("TTP_TX_GUARD", "1")     # a fetch inside the write transaction is refused
    p, repo, tid, oid = _receipt_order_setup(env, monkeypatch, status="done")
    head = _git_out(repo, "rev-parse", "HEAD")
    fix = p.db.add_task(f"Fix review #{tid}: build the deliverable", "fix", kind="code", tier="standard",
                        origin="coordinator", branch=f"ttp/t{tid}-build")
    run_dir = _obform_run(p, fix, oid, "receipt", f"landed as {head}")
    p.db.update_task(fix, status="done")
    assert ob.file_runs(p) == []
    o = ob.get(p.db, oid)
    assert o["receipt"] is None and o.get("unverified") is None and "receipt" not in o["owners"][str(fix)]
    assert [e["text"] for e in o["owners"][str(fix)]["evidence"]] == [f"landed as {head}"], "kept as progress"
    assert "not landed" in (run_dir / "progress.md").read_text(), "the run is told why"


def test_a_receipt_on_partial_or_blocked_owner_work_is_kept_as_progress_and_the_obligation_stays_owed(
        env, monkeypatch):
    """Replayed refusals: a coordinator receipt citing an owner whose hand-off is 'partial: 4 of 11 cells',
    'partial: 8 of 9 cells' or blocked waiting (its urgent part landed) is applied as progress with a note:
    no problem, no receipt, the obligation still owed."""
    from ttp import coordinator as coord, obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    cases = [("partial: 4 of 11 cells (missing: a-rerun, b-rerun)", "done", "done"),
             ("partial: 8 of 9 cells (missing: live). Cost dropped about 20x", "done", "done"),
             ("Urgent part (user msg #3399) landed as 989ee0b79, before the due. 1) the rest waits", "blocked",
              "waiting")]
    for summary, status, handoff in cases:
        t = p.db.add_task("build the cells", "s", kind="work", tier="light")
        oid = ob.add(p, "the cell table", [t], now=now)
        _ob_handoff(p, t, summary, status=status, handoff=handoff)
        probs = coord.apply(p, [{"type": "obligation", "key": oid, "evidence": f"#{t} {summary[:40]}"}])
        notes = " ".join(p.db.kv(coord.NOTES_KEY) or [])
        o = ob.get(p.db, oid)
        assert probs == [], probs
        assert o["receipt"] is None and ob._open(o), o
        assert f"{oid}: receipt recorded as progress" in notes and f"{oid} stays owed" in notes, notes
        assert any(h["event"] == "progress" and f"#{t}" in h["evidence"] for h in o["history"]), o


def test_obform_kept_forms_are_not_rewritten_and_still_refused(env, monkeypatch):
    """The keep list, in one batch (a receipt citing a partial hand-off is kept as progress and a missed due
    moved with no plan change is applied and told, both notes): a review never owns a change order, not even as a done owner's only open successor;
    a due move on an open obligation with no due stays a note. None is rewritten (no "applied as" note)."""
    from ttp import coordinator as coord, obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)

    def task(title, kind="code", **kw):
        return p.db.add_task(title, "s", kind=kind, tier="light", origin="coordinator", **kw)
    part = ob.add(p, "the cell table", [tid], now=now)
    _ob_handoff(p, tid, "partial: 8 of 9 cells (missing: live)")
    plain = ob.add(p, "a plain one", [], now=now)
    late = p.db.post("in", "and ship it", chat="c1")
    code = task("ship it")
    ob.add_order(p, "ship it", late, [code], due=now - 60, action_class="change", now=now - 7200)
    made = task("make the change")
    p.db.update_task(made, status="done")
    rev = task(f"Review #{made}: make the change", kind="review", labels=[f"auto_review:{made}"])
    probs = coord.apply(p, [
        {"type": "obligation", "key": part, "evidence": f"#{tid} finished"},
        {"type": "obligation", "key": ob.open_order(p.db, late)["id"], "due": now + 7200},
        {"type": "obligation", "order": mid, "text": "build it", "action_class": "change",
         "owners": [made, rev, code]},
        {"type": "obligation", "key": plain, "due": now + 3600}])
    applied, notes = _obform_notes(p, coord)
    assert len(probs) == 0 and applied == [], (probs, applied)
    assert f"{part}: receipt recorded as progress" in notes and "so the acceptance is not met" in notes, notes
    assert "missed its due" in notes and "with no plan change, as sent" in notes, notes
    assert ob.get(p.db, part)["receipt"] is None and ob.open_order(p.db, mid)["promise"]["tasks"] == [code]
    assert f"#{made} is done, so not attached" in notes and f"#{rev} is a review task" in notes
    assert f"{plain} is open but has no promise or due to move" in notes


def test_owned_open_decodes_the_obligations_once_for_a_sweep_of_many_tasks(env, monkeypatch):
    from ttp import obligations as ob
    p = make(env)
    tids = [p.db.add_task(f"owner {i}", "s", kind="code", tier="light", origin="coordinator") for i in range(200)]
    first = ob.add(p, "two owners", tids[:2], now=time.time())
    ob.add(p, "no owner", [], now=time.time())
    loads = []
    real = ob._load
    monkeypatch.setattr(ob, "_load", lambda db: loads.append(1) or real(db))
    assert [ob.owned_open(p.db, t) for t in tids[:3]] == [[first], [first], []]
    for t in tids:
        ob.owned_open(p.db, t)
    assert len(loads) == 1, "the kv is decoded once while it is unchanged"
    done = ob.add(p, "owned by the last task", tids[-1:], now=time.time())
    assert ob.owned_open(p.db, tids[-1]) == [done] and len(loads) == 3   # add's own read, then the changed kv
