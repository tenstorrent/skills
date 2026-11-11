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
    # Its turn names a task that finished meanwhile: the attach is rejected, nothing is attached.
    late = p.db.add_task("continue: CI read", "s", kind="work", tier="light", status="done")
    problems = coord.apply(p, [{"type": "obligation", "key": oid, "owners": [late]}])
    assert problems and "is done" in problems[0], problems
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


def test_order_started_owner_retires_the_late_event(env, monkeypatch):
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    ob.add_order(p, "build it", mid, [tid], action_class="read", now=now)
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?,?,?,?)", (tid, "worker", now + 60, "running"))
    check(now + ob.ORDER_START_S + 60)
    assert raised((ob.ORDER_LATE_KIND,)) == []


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
    probs = coord.apply(p, [{"type": "obligation", "key": oid, "owners": [kinds["plan"]]}])
    assert len(probs) == 1 and "is a plan task, which cannot carry a change order" in probs[0], probs
    assert str(kinds["plan"]) not in ob.get(p.db, oid)["owners"]
    n = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    probs = coord.apply(p, [{"type": "task_add", "title": "look at it", "spec": f"answer for obligation {oid}", "kind": "question"}])
    assert len(probs) == 1 and f"its spec names order {oid}" in probs[0], probs
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == n, "the refused task_add created a task"
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
    line = ob.order_audit_line(p.db, now)
    assert line == "user orders unstarted past 30 min: 2 of 3 in the last 7 days (1 of 2 in the 7 days before)", line
    assert line in "\n".join(audit.lines(p.db, now))


def test_set_due_refusal_says_a_plain_open_obligation_is_open_and_how_to_close_it(env, monkeypatch):
    """A due move on an open obligation with no promise names it open and the way to close it (evidence
    or why); an unknown or closed key keeps the old message; an order's due still moves."""
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
    with pytest.raises(ValueError, match="is not an open order, promise or claim"):
        ob.set_due(p, plain, now + 3600, now=now)
    oid = ob.add_order(p, "build it", mid, [tid], due=now + 7200, action_class="change", now=now).split()[1]
    ob.record(p, oid, "receipt", "built: commit abc123", None)
    with pytest.raises(ValueError, match="is not an open order, promise or claim"):
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


def test_a_receipt_citing_only_an_owner_hand_off_that_leaves_acceptance_unmet_is_refused(env, monkeypatch):
    """record(): a receipt whose evidence names only owners whose hand-off is partial, or filed by an owner
    task that ended so, is refused naming why; evidence naming a met owner or no owner is taken."""
    import pytest
    from ttp import obligations as ob
    p, d, now, tid, check = _ob_setup(env, monkeypatch)
    oid = ob.add(p, "build the deliverable", [tid], now=now)
    _ob_handoff(p, tid, "partial: 0 cases qualify")
    with pytest.raises(ValueError, match=f"receipt refused: #{tid}'s hand-off is marked"):
        ob.record(p, oid, "receipt", f"#{tid} finished", None, now=now)
    with pytest.raises(ValueError, match="acceptance is not met"):
        ob.record(p, oid, "receipt", "done", tid, now=now)
    assert ob.get(p.db, oid)["receipt"] is None
    other = p.db.add_task("build it again", "s", kind="work", tier="light")
    oid2 = ob.add(p, "build it again", [other], now=now)
    _ob_handoff(p, other, "built it: 3 rows")
    assert "no longer owed" in ob.record(p, oid2, "receipt", f"#{other} built it", None, now=now)
    assert "no longer owed" in ob.record(p, oid, "receipt", "commit abc123 has the table", None, now=now)
