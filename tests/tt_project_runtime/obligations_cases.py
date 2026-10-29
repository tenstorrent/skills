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
    p.db.update_task(mine[0], status="done")   # the daemon's owner also ends without a receipt
    t2 = t1 + 2 * 3600 + 60
    evs = check(t2)
    assert len(evs) == 2, "the new episode's wake"
    _handle_overdue(p)
    evs = check(t2 + 300)
    assert len(evs) == 3 and evs[-1]["fingerprint"] == f"obligation:{oid}:reown:{tid}-{mine[0]}", evs
    assert f"#{mine[0]} ended without a receipt" in evs[-1]["text"] and evs[-1]["severity"] == "high"
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
