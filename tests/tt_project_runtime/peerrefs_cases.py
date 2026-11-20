# Wait notes and another project's task ids (refwaits.refs with peers, peer_names, stale_facts): included by
# tests/test_tt_project_runtime.py, in its namespace.


def test_a_peer_named_before_a_ref_makes_it_no_task_of_this_project():
    from ttp.refwaits import refs
    peers = {"otherproj", "zeta-rt"}
    for text in ("push priority slot: otherproj task #3074 (run 9799)", "otherproj #508", "otherproj's #508",
                 "otherproj’s #508", "otherproj#508", "Zeta-RT task #10348 (run 28612)", "zeta-rt tasks #5",
                 "otherproj: #508", "(otherproj #508)", "otherproj #508, #509 and #512", "zeta-rt task #5, #6 or #7",
                 "otherproj ask #4"):
        assert refs(text, peers=peers) == [], text
    # Local refs keep counting, beside foreign ones and without peers.
    assert refs("otherproj task #3074 holds the slot; then task #12", peers=peers) == [("task", 12)]
    assert refs("otherproj #508 and needs #12", peers=peers) == [("task", 12)]
    assert refs("otherproj #508 lands; then #12", peers=peers) == [("task", 12)]
    for text, want in (("task #12", [("task", 12)]), ("#12", [("task", 12)]), ("needs #12", [("task", 12)]),
                       ("ask #3, then #12", [("ask", 3), ("task", 12)]), ("landed:#12", [("task", 12)])):
        assert refs(text, peers=peers) == want == refs(text), text
    # A name only counts whole: a longer word ending in a peer's name, or a peer's name further back.
    assert refs("notherproj #12", peers=peers) == [("task", 12)]
    assert refs("my-otherproj #12", peers=peers) == [("task", 12)]
    assert refs("otherproj handled it in #12", peers=peers) == [("task", 12)]
    assert refs("otherproj task #3074 (run 9799)") == [("task", 3074)]   # no peers known: as before


def test_peer_names_come_from_the_registry_notes_and_peers_file_never_own_or_plain_words(env):
    p = make(env, "alpha")
    from ttp import heldwait, project, upstream
    from ttp.refwaits import peer_names, refs
    for name in ("beta-two", "needs"):
        project.register(name, {"host": "testhost", "dir": str(p.root) + "-" + name})
    p.db.set_kv(upstream.KV_ROUTES, {"fp1": {"project": "Gamma", "host": "h"}, "fp2": "junk"})
    (p.harness / heldwait.PEERS_FILE).write_text(json.dumps(["delta"]))
    got = peer_names(p.db, "alpha", p.harness)
    assert {"beta-two", "gamma", "delta"} <= got and not {"alpha", "needs"} & got
    assert refs("alpha #12 and needs #13 and beta-two #14", peers=got) == [("task", 12), ("task", 13)]


def test_a_peers_task_never_becomes_a_fact_and_old_foreign_facts_are_dropped(env):
    p = make(env, "alpha")
    from ttp import daemon as dmod, project, refwaits
    project.register("otherproj", {"host": "testhost", "dir": str(p.root) + "-other"})
    d = dmod.Daemon(p.base)
    n = p.db.add_task("same number as the peer's", "s", kind="work", origin="user", status="done")
    real = p.db.add_task("a real dependency", "s", kind="work", origin="user", status="done")
    tid, _ = _ref_wait(p, f"push priority slot: otherproj task #{n} (run 9799)", probe="false")
    d.refresh_ref_waits()
    t = p.db.task(tid)
    assert f"task #{n} is done" not in t["blocked_reason"] and refwaits.FACTS_KEY not in json.loads(t["result"])
    # A note an older daemon rewrote from the foreign ref: its fact goes from refs_over and from the note, and
    # a real local ref's fact stays.
    fact, ok = f"task #{n} is done (not proof its work landed)", f"task #{real} is done (not proof its work landed)"
    res = {**json.loads(t["result"]), "waiting_for": f"otherproj task #{n} and task #{real}",
           refwaits.FACTS_KEY: [fact, ok]}
    p.db.update_task(tid, result=json.dumps(res),
                     blocked_reason=f"waiting for otherproj task #{n} and task #{real} ({fact}; {ok}); {fact}; "
                                    f"{ok}; its probe runs again now")
    p.db.set_kv(refwaits.SEEN_KEY, {str(tid): {"ep": f"{res['waiting_since']:.0f}", "seen": [f"task:{real}:done"]}})
    for _ in range(2):   # and never stored again on the next tick
        d.refresh_ref_waits()
        t = p.db.task(tid)
        assert json.loads(t["result"])[refwaits.FACTS_KEY] == [ok]
        assert fact not in t["blocked_reason"] and t["blocked_reason"] == (
            f"waiting for otherproj task #{n} and task #{real} ({ok}); {ok}; its probe runs again now")
    assert (p.logs / "daemon.log").read_text().count(f"task {tid} wait note: dropped") == 1
    # The same number with no peer before it is still this project's task.
    own, _ = _ref_wait(p, f"task #{n} to finish", probe="false")
    d.refresh_ref_waits()
    assert f"task #{n} is done" in p.db.task(own)["blocked_reason"]


def test_stale_facts_and_strip_facts():
    from ttp.refwaits import stale_facts, strip_facts
    a, b = "task #5 is done (not proof its work landed)", "ask #3 is resolved"
    assert stale_facts([a, b, "junk"], f"x; {a}", [("ask", 3)]) == [a]
    assert stale_facts([a], "", [("task", 5)]) == [] and stale_facts(None, f"; {b}", []) == [b]
    assert strip_facts(f"waiting for x ({a}; {b}); {a}; then", [a]) == f"waiting for x ({b}); then"
    assert strip_facts(f"waiting for x ({a}); {b}; then", [a]) == f"waiting for x; {b}; then"


def test_a_foreign_fact_the_note_cannot_lose_is_neither_rewritten_nor_logged_each_tick(env):
    p = make(env, "alpha")
    from ttp import daemon as dmod, project, refwaits
    project.register("otherproj", {"host": "testhost", "dir": str(p.root) + "-other"})
    d = dmod.Daemon(p.base)
    n = p.db.add_task("same number as the peer's", "s", kind="work", origin="user", status="done")
    tid, _ = _ref_wait(p, f"otherproj task #{n}", probe="false")
    note = f"waiting: task #{n} is done (not proof its work landed)"   # no separator strip_facts knows
    p.db.update_task(tid, blocked_reason=note)
    before = p.db.task(tid)["result"]
    for _ in range(2):
        d.refresh_ref_waits()
    t = p.db.task(tid)
    assert t["result"] == before and t["blocked_reason"] == note
    assert refwaits.FACTS_KEY not in json.loads(t["result"])
    log = p.logs / "daemon.log"
    assert f"task {tid} wait note: dropped" not in (log.read_text() if log.exists() else "")
