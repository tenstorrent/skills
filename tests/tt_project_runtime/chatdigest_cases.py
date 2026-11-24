# Chat digest: routine coordinator posts batched per chat (runtime/ttp/chatdigest.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _cd_out(p, chat="c1"):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND chat=? ORDER BY id",
                                      (chat,))]


def test_ack_progress_and_forecast_correction_replies_wait_for_the_digest(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    routine = ["Got it, on it.",
               "#12 is queued for a worker; expected finish 14:10 PST.",
               "Correction: #12 needs two passes on the checks queue, not one.",
               "#12 still waits on the checks queue, which runs 4 at a time."]
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": t} for t in routine], turn=1)
    assert _cd_out(p) == []
    held = p.db.kv(chatdigest.KV)["held"]["c1"]
    assert [h["text"] for h in held] == routine   # kept whole, never dropped
    assert [h["why"] for h in held] == ["ack", "progress", "forecast correction", "progress"]
    assert any("chat digest:" in n for n in p.db.kv(coord.NOTES_KEY, []))   # the coordinator hears of it
    assert chatdigest.counts(p.db)["c1"] == {"sent": 0, "held": 4, "digests": 0, "deduped": 0,
                                               "waiting": 4}


def test_results_decisions_outages_and_asks_go_at_once(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = p.db.add_task("t", "s", kind="work", status="done")
    important = [{"type": "reply", "chat": "c1", "text": f"#{tid} finished: the suite passes at 1a2b3c."},
                 {"type": "reply", "chat": "c1", "text": "I decided to run the soak on the spare host; say if you "
                                                       "want the other one."},
                 {"type": "reply", "chat": "c1", "text": "The build host is down since 09:10 PST."},
                 {"type": "reply", "chat": "c1", "text": "Which branch should the release use?"},
                 {"type": "reply", "chat": "c1", "text": "On it.", "delivers": [f"#{tid}"]}]
    coord.apply(p, important, turn=1)
    assert _cd_out(p) == [a["text"] for a in important]
    assert chatdigest.counts(p.db)["c1"]["sent"] == 5 and not ((p.db.kv(chatdigest.KV) or {}).get("held") or {}).get("c1")
    # A high notify always goes; a routine one waits like a reply.
    coord.apply(p, [{"type": "notify", "text": "Still waiting on the queue.", "severity": "high"},
                    {"type": "notify", "text": "Still waiting on the queue.", "severity": "low"}], turn=2)
    alerts = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='alert'")
    assert [m["text"] for m in alerts] == ["Still waiting on the queue."]
    assert chatdigest.counts(p.db)["all"]["held"] == 1


def test_a_reply_to_the_users_question_goes_at_once(env, monkeypatch):
    from ttp import coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.db.post("in", "where is #12 now", chat="c1")
    p.db.post("in", "is #12 still queued?", chat="c1")
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "#12 is queued for a worker, ETA 14:10 PST."}], turn=1)
    assert _cd_out(p) == ["#12 is queued for a worker, ETA 14:10 PST."]


def test_the_status_footer_leaves_a_sent_post_for_the_digest(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord, onhost
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = p.db.add_task("power cycle the box", "s", kind="work", status="queued", reply_chat="c1",
                        labels=["resource:box1-device"])
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Power cycling box1 now, nothing else touches it.",
                     "owners": [tid]}], turn=1)
    (got,) = _cd_out(p)
    assert onhost.MARK in got and "Status now:" not in got   # the honesty mark stays, the footer goes
    (held,) = p.db.kv(chatdigest.KV)["held"]["c1"]
    assert held["why"] == "status footer" and f"#{tid}: queued for a worker" in held["text"]


def test_the_digest_goes_at_most_every_n_hours_and_only_with_content(env, monkeypatch):
    from ttp import chatdigest
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("coordinator.chat_digest_h", 2)
    cfg, t0 = p.config(), 1_800_000_000.0
    assert chatdigest.flush(p, cfg, t0) == []   # nothing held: no digest
    chatdigest.hold(p.db, "c1", "#12 is queued for a worker.", "progress", t0)
    chatdigest.hold(p.db, "c1", "Got it.", "ack", t0 + 600)
    assert chatdigest.flush(p, cfg, t0 + 3600) == []   # the oldest waited 1 h of 2
    [mid] = chatdigest.flush(p, cfg, t0 + 2 * 3600)
    text = p.db.one("SELECT text, chat FROM messages WHERE id=?", (mid,))
    assert text["chat"] is None and text["text"].startswith("Digest: 2 routine updates since ")
    assert "#12 is queued for a worker." in text["text"] and "Got it." in text["text"]
    chatdigest.hold(p.db, "c1", "#13 is queued.", "progress", t0 + 2 * 3600 + 60)
    assert chatdigest.flush(p, cfg, t0 + 3 * 3600 + 120) == []   # 2 h since the last digest not yet passed
    assert len(chatdigest.flush(p, cfg, t0 + 4 * 3600 + 60)) == 1
    assert chatdigest.counts(p.db)["c1"] == {"sent": 0, "held": 3, "digests": 2, "waiting": 0, "deduped": 0}


def test_the_digest_stays_in_the_web_app(env, monkeypatch):
    """The digest is recorded for the web app only: no chat, the quiet
    channel, so no relay delivers it; it names the chat it was for."""
    from ttp import alerts, chatdigest
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("coordinator.chat_digest_h", 2)
    t0 = 1_800_000_000.0
    chatdigest.hold(p.db, "c1", "#12 is queued for a worker.", "progress", t0)
    [mid] = chatdigest.flush(p, p.config(), t0 + 2 * 3600)
    m = p.db.one("SELECT * FROM messages WHERE id=?", (mid,))
    assert m["chat"] is None and m["channel"] == alerts.QUIET and "(for chat c1)" in m["text"]
    assert "#12 is queued for a worker." in m["text"] and _cd_out(p) == []
    assert not p.db.unread_for_chat("c1", 0)   # no relay delivers it


def test_a_held_reminder_keeps_its_full_command_in_the_digest(env, monkeypatch):
    from ttp import chatdigest
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    cmd = "Still waiting for you to run `gh auth login --hostname github.com --git-protocol ssh` " + "x" * 400
    chatdigest.hold(p.db, "c1", cmd, "progress", 1_800_000_000.0)
    [mid] = chatdigest.flush(p, p.config(), 1_800_000_000.0 + 4 * 3600)
    assert cmd in p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"]


def test_with_the_digest_off_every_post_goes_at_once(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("coordinator.chat_digest_h", 0)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Got it."}], turn=1)
    assert _cd_out(p) == ["Got it."] and chatdigest.counts(p.db) == {}


def test_an_unacted_must_act_item_reaches_the_digest_not_the_chat(env, monkeypatch):
    """The overdue promise ignored for two turns: the update from the records waits in c1's digest."""
    from ttp import chatdigest, mustact
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p, d, ev, o, turn, told, check = _must_act_setup(env, monkeypatch)
    turn([{"type": "noop"}])
    turn([{"type": "noop"}])
    assert p.db.q("SELECT id FROM messages WHERE direction='out' AND text LIKE 'Automatic%'") == []
    (held,) = p.db.kv(chatdigest.KV)["held"]["c1"]
    assert "promise overdue" in held["text"] and held["why"] == "must-act not acted on"
    assert told and told[0][2] == f"must-act:{ev['fingerprint']}"
    [mid] = chatdigest.flush(p, p.config(), held["ts"] + 3 * 3600)
    assert "promise overdue" in p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"]


def test_promises_claims_and_steps_for_the_user_go_at_once_and_are_tracked(env, monkeypatch):
    """A post that commits to a time, carries claims or asks the user to act is never held: a held promise
    would skip promise tracking, and a held ask would leave the user's step unseen for hours."""
    from ttp import chatdigest, coordinator as coord, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    posts = ["Got it; I'll send the table by 02:00 PST.",
             "Queued for a worker, ETA 02:10 PST.",
             "Correction: the ETA is now 15:40 PST, not 14:10 PST.",
             "Waiting on your sign-in: run `gh auth login` on your laptop, then reply here.",
             "Still waiting: run `make setup` in your checkout so the worker can start."]
    for t in posts:
        assert chatdigest.why_hold(p.db, t, {"type": "reply"}, "c1") == "", t
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": posts[0]}], turn=1)
    assert _cd_out(p) == [posts[0]]
    assert [o for o in obligations.items(p.db) if (o.get("promise") or {}).get("due")]   # tracked as a promise
    claims = {"type": "reply", "chat": "c1", "text": "#12 still waits on the checks queue.",
              "claims": [{"text": "#12 waits on the checks queue", "expect": "true"}]}
    assert chatdigest.why_hold(p.db, claims["text"], claims, "c1") == ""


def test_an_unacted_outage_item_reaches_the_chat_at_once(env, monkeypatch):
    """A must-act item about an outage, ignored for two turns, is posted to its chat at once, not held."""
    from ttp import chatdigest, mustact, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p, d, ev, o, turn, told, check = _must_act_setup(env, monkeypatch)
    st = p.db.kv(obligations.KV)
    st["items"][mustact.item(ev)["key"]]["text"] += " The build host is down."
    p.db.set_kv(obligations.KV, st)
    turn([{"type": "noop"}])
    turn([{"type": "noop"}])
    assert p.db.q("SELECT id FROM messages WHERE direction='out' AND chat='c1' AND text LIKE 'Automatic%'")
    assert not ((p.db.kv(chatdigest.KV) or {}).get("held") or {}).get("c1")
    assert told and "told model-free" in told[0][1]


def test_an_unacted_queue_wait_reaches_no_chat(env):
    """A qwait item nobody acted on is raised again at its next mark; it never becomes a chat message."""
    from ttp import mustact, queuewait
    p = make(env)
    now = time.time()
    host = _qw_host(p, now, now - 40 * 60)
    queuewait.scan(p, QW_CFG, now, host)
    ev = _qw_events(p)[0]
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    mustact.settle(p, [ev["id"]], [{"type": "noop"}], [0], now)
    mustact.settle(p, [ev["id"]], [{"type": "noop"}], [0], now + 60)
    assert p.db.q("SELECT id FROM messages WHERE direction='out'") == []


def test_a_chat_can_set_its_own_digest_hours_and_a_high_floor_elsewhere_hides_nothing(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("coordinator.chat_digest_chats", {"c2": 0})
    assert chatdigest.every_s(p.config(), "c1") == 3 * 3600 and chatdigest.every_s(p.config(), "c2") == 0
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Got it."},
                    {"type": "reply", "chat": "c2", "text": "Got it."}], turn=1)
    assert _cd_out(p, "c1") == [] and _cd_out(p, "c2") == ["Got it."]


def _cd_chats(p, *ids):
    import time
    for c in ids:
        p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES(?,?,?,?,0)",
               (c, time.time(), None, time.time()))


def test_a_status_goes_at_most_once_per_gap_and_answers_asks_and_outages_still_go(env, monkeypatch):
    """cells: chat_interval. A second status within coordinator.chat_status_gap_h waits for the digest; an
    answer to the user's question and an outage go at once; after the gap a status goes again."""
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    assert chatdigest.status_gap_s(p.config(), "c1") == 3600
    first, second = ("Backlog: 12 tasks open, 3 queued; #40 done.", "Backlog: 11 tasks open, 4 queued; #41 done.")
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": first}], turn=1)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": second}], turn=2)
    assert _cd_out(p) == [first]
    assert [h["why"] for h in p.db.kv(chatdigest.KV)["held"]["c1"]] == ["status gap"]
    assert any("chat_status_gap_h" in n for n in p.db.kv(coord.NOTES_KEY, []))
    p.db.post("in", "how many tasks are left?", chat="c1")
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "11 tasks left, 4 queued."}], turn=3)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "The build host is down; 11 tasks wait for it."}], turn=4)
    assert _cd_out(p)[1:] == ["11 tasks left, 4 queued.", "The build host is down; 11 tasks wait for it."]
    st = p.db.kv(chatdigest.KV)
    st["status"]["c1"] -= 3601
    p.db.set_kv(chatdigest.KV, st)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Backlog: 9 tasks open, 2 queued; #42 done."}], turn=5)
    assert _cd_out(p)[-1] == "Backlog: 9 tasks open, 2 queued; #42 done."
    p.set_config("coordinator.chat_status_gap_chats", {"c1": 0})
    assert chatdigest.status_gap_s(p.config(), "c1") == 0 and chatdigest.status_gap_s(p.config(), "c2") == 3600


def test_a_near_repeat_to_another_chat_of_the_user_is_not_sent_within_the_hour(env, monkeypatch):
    """cells: cross_chat_dedupe."""
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    status = "Backlog: 12 tasks open, 3 queued; #40 done, the rest by 15:10."
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": status}], turn=1)
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": "Backlog now: 12 tasks open, 3 queued; #40 done, the "
                                                             "rest by 15:10."}], turn=2)
    assert _cd_out(p, "c1") == [status] and _cd_out(p, "c2") == []
    assert chatdigest.counts(p.db)["c2"]["deduped"] == 1
    assert any("nearly repeats message #" in n for n in p.db.kv(coord.NOTES_KEY, []))
    other = "I picked the spare host for the soak runs."
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": other}], turn=3)
    assert _cd_out(p, "c2") == [other]
    p.set_config("coordinator.chat_users", {"c1": "a", "c3": "b"})   # another user's chat gets its own copy
    coord.apply(p, [{"type": "reply", "chat": "c3", "text": status}], turn=4)
    assert _cd_out(p, "c3") == [status]
    p.db.x("UPDATE messages SET ts = ts - 3700")
    p.set_config("coordinator.chat_users", {})
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": status}], turn=5)
    assert _cd_out(p, "c2") == [other, status]   # an hour on, it goes again
    assert chatdigest.near("The build host is down.", "The build host is up.") is False


def test_the_first_post_delivers_the_other_chats_promise_on_the_same_topic(env, monkeypatch):
    """cells: promise_merge. A promise made in c2 is delivered by the post the user read in c1, when the
    dropped c2 repeat itself carried `delivers`; a dropped progress repeat without it delivers nothing."""
    from ttp import coordinator as coord, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = p.db.add_task("table", "s", kind="work", status="queued")
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": f"I'll send the #{tid} backlog table by 23:00."}], turn=1)
    o = next(o for o in obligations.open_promises(p.db) if o["promise"]["chat"] == "c2")
    p.db.x("UPDATE messages SET ts = ts - 7200")
    table = f"#{tid} backlog table is ready: 3 of 4 cells pass, 1 queued."
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": table}], turn=2)
    first = p.db.one("SELECT id FROM messages WHERE chat='c1' ORDER BY id DESC LIMIT 1")["id"]
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": f"#{tid} backlog table is ready now: 3 of 4 cells pass, 1 queued.",
                     "delivers": [o["id"]]}], turn=3)
    assert _cd_out(p, "c2") == [f"I'll send the #{tid} backlog table by 23:00."]
    got = next(x for x in obligations.items(p.db) if x["id"] == o["id"])
    assert got["delivered"]["message"] == first and got["delivered"]["via"] == "repeat"
    assert any(f"delivered by reply #{first}" in n for n in p.db.kv(coord.NOTES_KEY, []))


def test_a_dropped_progress_repeat_never_delivers_the_other_chats_promise(env, monkeypatch):
    """cells: promise_merge. Review #8333: a progress post naming #N, dropped as a repeat in c2, must not
    mark c2's results promise delivered, so it stays owed after #N finishes."""
    from ttp import chatdigest, coordinator as coord, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = p.db.add_task("bench", "s", kind="work", status="queued")
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": f"I'll post the before/after numbers once #{tid} "
                                                             f"lands, by 07:30."}], turn=1)
    o = next(o for o in obligations.open_promises(p.db) if o["promise"]["chat"] == "c2")
    p.db.x("UPDATE messages SET ts = ts - 7200")
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": f"#{tid} still queued, 3 tasks queued, ETA 06:30."}], turn=2)
    coord.apply(p, [{"type": "reply", "chat": "c2", "text": f"#{tid} is still queued, 3 tasks queued, ETA 06:30."}],
                turn=3)
    assert chatdigest.counts(p.db)["c2"]["deduped"] == 1   # the repeat was dropped
    got = next(x for x in obligations.items(p.db) if x["id"] == o["id"])
    assert not got.get("delivered")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    obligations.check(p)
    assert o["id"] in {x["id"] for x in obligations.open_promises(p.db)}


def test_the_digest_shows_a_chats_own_status_interval(env, monkeypatch):
    """cells: digest_line."""
    from ttp import coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    _cd_chats(p, "c1", "c2")
    p.set_config("coordinator.chat_status_gap_chats", {"c2": 2})
    chats = coord.digest(p, {}, [], []).split("## Chats attached")[1].split("\n## ")[0]
    assert "- c2 (chat), active 0 min ago, status at most every 2 h" in chats
    assert "status at most" not in next(line for line in chats.splitlines() if line.startswith("- c1"))


def _cd_task(p, title, chat="main"):
    t = p.db.add_task(title, "spec")
    p.db.x("UPDATE tasks SET reply_chat=? WHERE id=?", (chat, t))
    return dict(p.db.task(t))


def _cd_due(p, tid):
    """The reply_due row a finished reply_chat task raised, or None."""
    return p.db.one("SELECT * FROM events WHERE kind='reply_due' AND task=? ORDER BY id DESC", (tid,))


def _cd_hand_offs(rows) -> list[str]:
    """The hand-off texts enclosed in reply_due event rows: the event text carries the spans-wrapped
    hand-off after 'Hand-off: '. Shared by the reply, writeup and handoffcheck case files."""
    from ttp import spans
    return [spans.strip(r["text"].partition("Hand-off: ")[2]).strip() for r in rows]


def test_a_finished_reply_chat_task_raises_reply_due_and_never_posts_its_hand_off(env, monkeypatch):
    """cells: handoff. The user's chat gets the coordinator's plain outcome summary, never a
    worker's raw hand-off. The daemon raises one reply_due event naming the chat and the landing
    facts, with the hand-off enclosed as data, and arms the fallback."""
    from ttp import chatdigest, coordinator as coord, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Queued for a worker."},
                    {"type": "task_add", "title": "tidy the docs", "spec": "tidy", "kind": "work", "tier": "light",
                     "priority": 3, "reply_chat": "c1"}], turn=1)
    t = dict(p.db.one("SELECT * FROM tasks WHERE reply_chat='c1' ORDER BY id DESC LIMIT 1"))
    before = _cd_out(p)
    pushq._reply(p.db, t, "done", "Docs tidied. Two pages merged.")
    assert _cd_out(p) == before, "no part of the hand-off reaches the out queue"
    ev = _cd_due(p, t["id"])
    assert ev and ev["status"] == "queued" and ev["kind"] == "reply_due"
    assert json.loads(ev["data"])["chat"] == "c1"          # names the chat
    assert json.loads(ev["data"])["facts"]["status"] == "done"
    assert "Docs tidied" in ev["text"]                      # the hand-off goes in as enclosed data
    armed = p.db.kv(chatdigest.FALLBACK_KV)
    assert len(armed) == 1 and armed[0]["task"] == t["id"]  # the fallback is armed for that chat


def test_a_landed_code_change_carries_its_landing_facts_to_the_event(env, monkeypatch):
    """The reply_due event of a task the push queue settled carries what landed where, and whether the
    installed release runs it, so the coordinator can say what is live without a hand-off."""
    from ttp import chatdigest, pushq
    p = make(env)
    t = _cd_task(p, "fix the reply path", chat="c1")
    pushq._reply(p.db, t, "done", "Fixed.", facts=pushq.landing_facts("abcdef1234", "0.2.1520", "relay/x"))
    ev = _cd_due(p, t["id"])
    assert ev
    f = json.loads(ev["data"])["facts"]
    assert (f["landed"], f["sha"], f["version"], f["branch"]) == (True, "abcdef1234", "0.2.1520", "relay/x")
    assert f["deploy"] in ("deployed", "pending install", "not deployed")
    assert "landed as abcdef1" in ev["text"]


# A sanitized fixture shaped like a worker's hand-off plus its shipped-by tail (the shape of the
# post the user objected to), with no hostnames, names or internal URLs in it.
_CD_HANDOFF = """Two fixes are on the branch (commit 16b5701a4, on top of cf33224a2). It is ready for review.

**1. Older hosts keep the listener alive**
- The `--holder` retry is now one helper, `_drop_unrecognized`: when the host rejects the flag, it drops the flag and its value and retries.
- A project upgraded before its host keeps its listener. Tests fail without the change.

**2. A late base is picked up**
- While the base is unset it is decided again for each batch and each reconnect.
- The forward path records one row per change of the base.

Tests: 4 new tests. The smoke suite and the plugin validation passed; run 20377. Some remote and
timezone tests fail the same way at the base commit."""
_CD_SHIPPED = "(shipped by review #9674: pushed b40ca7854 as 0.2.1519 to relay/x)"
_CD_FIXTURE = _CD_HANDOFF + "\n" + _CD_SHIPPED


def test_a_replay_of_the_raw_hand_off_reply_never_reaches_the_chat(env, monkeypatch):
    """A finished reply_chat task's hand-off, with its shipped-by tail, never posts to the chat:
    it goes to the coordinator as a reply_due event, and only a record-built outcome line can reach
    the chat, after the bound."""
    from ttp import chatdigest, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    t = _cd_task(p, "Message links: older hosts keep the forwarded listener alive", chat="c1")
    now = time.time()
    pushq._reply(p.db, t, "done", _CD_FIXTURE, facts=pushq.landing_facts("b40ca7854", "0.2.1519", "relay/x"))
    assert not [m for m in p.db.q("SELECT text FROM messages WHERE direction='out'")], "nothing posts"
    for bad in ("16b5701a4", "smoke suite", "shipped by review", "Two fixes are on the branch"):
        assert not any(bad in m["text"] for m in p.db.q("SELECT text FROM messages")), bad
    ev = _cd_due(p, t["id"])
    assert ev and json.loads(ev["data"])["chat"] == "c1"
    assert "16b5701a4" in ev["text"] and "Hand-off" in ev["text"]      # enclosed data only
    assert chatdigest.fallbacks(p, now + 10) == [], "the coordinator is still answering"


def test_the_fallback_outcome_line_posts_from_records_only_once_the_bound_passes(env, monkeypatch):
    """No coordinator reply within `coordinator.reply_fallback_s`: one short model-free outcome line
    built from records (never hand-off prose) posts to the chat; a chat the coordinator answered in
    time gets nothing."""
    from ttp import chatdigest, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    t = _cd_task(p, "Reply path: the user's chat gets the coordinator's plain summary", chat="c1")
    pushq._reply(p.db, t, "done", _CD_FIXTURE, facts=pushq.landing_facts("b40ca7854", "0.2.1519", "relay/x"))
    now = time.time()
    assert chatdigest.fallbacks(p, now + 10) == []       # inside the bound: nothing posts
    got = chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60)
    assert got and len(got) == 1
    text = p.db.one("SELECT text FROM messages WHERE id=?", (got[0],))["text"]
    assert len(text) <= 300 and "landed as b40ca78" in text and "0.2.1519" in text
    assert not any(w in text for w in ("16b5701a4", "smoke suite", "shipped by review", "Opus"))
    assert p.db.kv(chatdigest.FALLBACK_KV) == [], "armed once, spent once"
    # A chat the coordinator answered in time gets no fallback line; a routine info post is not an
    # answer, and the fallback still fires.
    t2 = _cd_task(p, "second task", chat="c2")
    pushq._reply(p.db, t2, "done", "Rows posted.", facts={"need": "run the Mac upgrade"})
    p.db.post("out", "Finished since the last status (1):", chat="c2", kind="info", severity="normal")
    got2 = chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60)
    assert got2 and len(got2) == 1
    p.db.post("out", "The fix is live as 0.2.1519; run the Mac upgrade.", chat="c2", kind="reply")
    assert chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60) == []


def test_the_coordinator_s_reply_closes_the_delivery(env, monkeypatch):
    """The coordinator's reply to the chat, `delivers` naming the task, is the delivery: the promise
    the task's hand-off recorded closes on it, and no fallback line follows."""
    from ttp import chatdigest, coordinator as coord, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = _cd_promised_task(p)
    pushq._reply(p.db, dict(p.db.task(tid)), "done", _CD_TABLE,
                 facts=pushq.landing_facts("b40ca7854", "0.2.1519", "relay/x"))
    assert _cd_due(p, tid)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "The column is in: GLM-5.3 agrees on 9 of 12.",
                     "delivers": [f"#{tid}"]}], turn=2)
    owed = next(o for o in obligations.items(p.db) if (o.get("promise") or {}).get("chat") == "c1")
    assert owed["delivered"], "the reply with `delivers` is the delivery"
    now = time.time()
    assert chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60) == []


def test_one_task_s_fallback_line_never_disarms_another_s_in_the_same_chat(env, monkeypatch):
    """Two tasks finish in one chat and the coordinator stays silent (the case the fallback exists
    for). The first task's bound passes first and its line posts; that reply-shaped row must not
    read as the coordinator's answer for the second task, whose line still posts at its bound."""
    from ttp import chatdigest, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    a = _cd_task(p, "first task", chat="c1")
    b = _cd_task(p, "second task", chat="c1")
    pushq._reply(p.db, a, "done", "First done.")
    pushq._reply(p.db, b, "done", "Second done.")
    assert len(p.db.kv(chatdigest.FALLBACK_KV)) == 2
    st = p.db.kv(chatdigest.FALLBACK_KV)     # the first task finishes minutes before the second
    for item in st:
        if item["task"] == a["id"]:
            item["due"] = time.time() - 1
    p.db.set_kv(chatdigest.FALLBACK_KV, st)
    first = chatdigest.fallbacks(p)
    assert len(first) == 1
    texts = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE chat='c1' ORDER BY id")]
    assert len(texts) == 1 and texts[0].startswith(f"#{a['id']} ")
    assert p.db.one("SELECT ref FROM messages WHERE id=?", (first[0],))["ref"] == chatdigest.FALLBACK_REF
    now = time.time()
    got = chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60)
    texts = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE chat='c1' ORDER BY id")]
    assert len(got) == 1 and len(texts) == 2 and texts[1].startswith(f"#{b['id']} "), "B still posts"
    assert p.db.kv(chatdigest.FALLBACK_KV) == [], "armed once, spent once"


def test_a_reply_delivering_one_task_leaves_the_other_s_fallback_armed(env, monkeypatch):
    """A coordinator reply about one task is not an answer for another in the same chat: a
    reply whose `delivers` names only A leaves B armed, and B's outcome line still posts."""
    from ttp import chatdigest, coordinator as coord, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    a = _cd_promised_task(p)      # its promise gives the reply's `delivers` a record to land on
    pushq._reply(p.db, dict(p.db.task(a)), "done", "Column posted.")
    b = _cd_task(p, "second task", chat="c1")
    pushq._reply(p.db, b, "done", "Second done.")
    now = time.time()
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "The column is in: 9 of 12 agree.",
                     "delivers": [f"#{a}"]}], turn=2)
    got = chatdigest.fallbacks(p, now + chatdigest.DEFAULT_FALLBACK_S + 60)
    lines = p.db.q("SELECT text FROM messages WHERE ref=?", (chatdigest.FALLBACK_REF,))
    assert len(got) == 1 and len(lines) == 1, "only B's fallback line posts"
    assert lines[0]["text"].startswith(f"#{b['id']} ")
    texts = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE chat='c1' ORDER BY id")]
    assert any(t.startswith("The column is in") for t in texts), "the reply itself reached the chat"
    assert p.db.kv(chatdigest.FALLBACK_KV) == []


def test_a_promise_in_a_finished_task_s_hand_off_is_owed_until_the_coordinator_replies(env, monkeypatch):
    """#8608 posted 'I will post the finished rows when they end.'; the promise records from the
    hand-off (by_task), a later task replying there adopts it, and its own reply delivers it."""
    from ttp import chatdigest, coordinator as coord, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    first = _cd_task(p, "the reference rows", chat="c1")
    mid = p.db.post("in", "post the new table with the same columns", chat="c1")
    obligations.add_order(p, "post the new table", mid, [first["id"]], due=time.time() + 7200, action_class="change")
    chatdigest.handoff(p.db, p.config(), first, "done",
                       "Rows so far. The open models are slow; I will post the finished rows when they end.", p=p)
    owed = [o for o in obligations.items(p.db) if obligations._open(o) and (o.get("promise") or {}).get("chat") == "c1"
            and (o.get("promise") or {}).get("order") is None]
    assert owed and not owed[0]["promise"]["tasks"]
    coord.apply(p, [{"type": "task_add", "title": "finish the open-model rows", "spec": "finish them",
                     "kind": "work", "tier": "light", "priority": 2, "reply_chat": "c1"}], turn=2)
    tid = p.db.one("SELECT id FROM tasks WHERE reply_chat='c1' ORDER BY id DESC LIMIT 1")["id"]
    got = next(o for o in obligations.items(p.db) if o["id"] == owed[0]["id"])
    assert tid in got["promise"]["tasks"], "the new task adopted the promise"


def test_every_daemon_chat_post_of_kind_info_or_reply_keeps_the_chat_pace():
    """Built from the posting code itself: every call in the runtime that posts an out message of kind
    'info' or 'reply' to a chat (chat not None) must sit in a function that applies the chat's pace
    (status_wait, hold, checked or handoff). A new posting path that skips it fails here."""
    import ast
    pace = {"status_wait", "hold", "checked", "handoff", "mark_status"}
    found, bad = [], []
    for f in sorted((RUNTIME / "ttp").glob("*.py")):
        tree = ast.parse(f.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(fn)
                     if isinstance(n, (ast.Attribute, ast.Name))}
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                func = call.func
                if not (isinstance(func, ast.Attribute) and func.attr == "post"):
                    continue
                kw = {k.arg: k.value for k in call.keywords if k.arg}
                is_db = (call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "out")
                is_parts = isinstance(func.value, ast.Name) and func.value.id == "msgparts"
                kind = kw.get("kind")
                chat = kw.get("chat")
                if not (is_db or is_parts) or not (isinstance(kind, ast.Constant) and kind.value in ("info", "reply")):
                    continue
                if chat is None or (isinstance(chat, ast.Constant) and chat.value is None):
                    continue   # a broadcast to every chat, not one chat's status
                if fn.name == "cmd_urgent_reply":
                    continue   # the coordinator's own mid-turn reply to a user's urgent message: never paced
                found.append(f"{f.name}:{call.lineno}")
                if fn.name == "cmd_urgent_reply":
                    continue   # an urgent message's first finding is exempt from the chat's pace on purpose
                if fn.name == "fallbacks":
                    continue   # the daemon's one bounded outcome line (chatdigest.fallbacks): at most
                    # once per finished task, only past the reply bound, built from records only
                if not names & pace:
                    bad.append(f"{f.name}:{call.lineno} in {fn.name}")
    assert found, "the scan found no posting call: the guard is broken"
    assert not bad, f"chat posts that skip the chat's status pace: {bad}"


# #8657: a reply_chat task whose result the coordinator promised in its chat, or that owns the user's order
# there, finished done (normally and model-free through its on_pass_cmd) and was held as one status line.
_CD_TABLE = "| model | passed |\n|---|---|\n| GLM-5.3 | 9/12 |\n| Opus 5.5 | 11/12 |"
_CD_PROMISE = ("That pass is queued now. I'll post its column and how often it agrees with Opus here when it "
               "finishes.")


def _cd_promised_task(p, chat="c1"):
    """The coordinator's batch: a reply promising the result in `chat`, and the task that makes it."""
    from ttp import coordinator as coord
    coord.apply(p, [{"type": "reply", "chat": chat, "text": _CD_PROMISE},
                    {"type": "task_add", "title": "score the open model's column", "spec": "score it", "kind": "work",
                     "tier": "light", "priority": 2, "reply_chat": chat}], turn=1)
    return p.db.one("SELECT id FROM tasks WHERE reply_chat=? ORDER BY id DESC LIMIT 1", (chat,))["id"]


def test_the_promise_detector_reads_the_column_and_rows_promises():
    from ttp import obligations
    assert obligations.commitments(_CD_PROMISE)
    assert obligations.commitments("The host is slow, so I will post the finished rows when they end.")
    assert not obligations.commitments("I will tighten the filter next.")
    assert not obligations.commitments("If you want, I'll post the rows.")


def test_a_reply_the_user_asked_for_goes_at_once_after_a_status_and_a_late_repeat_waits(env, monkeypatch):
    """cells: held_reply. Upstream note d12a28e292fd1e82: a reply whose `order` names the user's request for an
    ETA and a plan (no '?') is a requested result, never status: the status gap a minute-old status post
    started does not hold it. The same order on a status post past the answer window is status again
    (correction #2617: status tables keep the hourly pace)."""
    from ttp import chatdigest, coordinator as coord
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    first = "Backlog: 12 tasks open, 3 queued; #40 done."
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": first}], turn=1)
    m = p.db.post("in", "Send an ETA and the plan in one line, then the per-box times once measured.", chat="c1")
    eta = "ETA 15:40 PST: plan is the 3 boxes in parallel, 2 queued behind #12; per-box times once measured."
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": eta}], turn=2)   # no order: a status, held
    assert _cd_out(p) == [first]
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": eta, "order": m, "no_order": "answered"}], turn=3)
    assert _cd_out(p) == [first, eta]
    assert chatdigest.requested(p.db, {"type": "reply", "order": m}, "c1") == m
    assert chatdigest.requested(p.db, {"type": "reply", "order": m}, "c2") is None   # another chat's request
    p.db.x("UPDATE messages SET ts=ts-? WHERE direction='out' AND chat='c1'", (chatdigest.ANSWER_S + 1,))
    st = p.db.kv(chatdigest.KV)
    st["status"]["c1"] = time.time() - 60
    p.db.set_kv(chatdigest.KV, st)
    later = "Backlog: 10 tasks open, 2 queued; #41 done."
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": later, "order": m}], turn=4)
    assert _cd_out(p) == [first, eta]
    assert [h["text"] for h in p.db.kv(chatdigest.KV)["held"]["c1"]][-1] == later
    nonask = p.db.post("in", "fine, carry on", chat="c1")
    assert chatdigest.requested(p.db, {"type": "reply", "order": nonask}, "c1") is None


def test_a_held_reply_answers_no_order_and_its_note_says_the_chat_never_got_it(env, monkeypatch):
    """cells: held_order, digest_line. A reply the status gap held never reached the chat: its `no_order`
    covers nothing (order_gap refuses naming the message), and the coordinator's
    note on it says it went to the web app only, with the order id."""
    from ttp import coordinator as coord, turnrules
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Backlog: 12 tasks open, 3 queued; #40 done."}], turn=1)
    m = p.db.post("in", "fine, carry on", chat="c1")
    actions = [{"type": "reply", "chat": "c1", "text": "Backlog: 11 tasks open, 4 queued; #41 done.",
                "order": m, "no_order": "status read"}]
    held: list = []
    coord.apply(p, actions, turn=2, held=held)
    assert held == [0] and len(_cd_out(p)) == 1
    note = [n for n in p.db.kv(coord.NOTES_KEY, []) if "NOT delivered" in n][-1]
    assert "web app" in note and f"order #{m}" in note
    gap = turnrules.order_gap(p.db, set(), actions, set(), [m], held=held)
    assert gap and gap.startswith(turnrules.PREFIX) and f"#{m}" in gap and "NOT delivered" in gap
    p.db.set_kv(turnrules.NO_ORDER_REFUSED_KEY, [])
    assert turnrules.order_gap(p.db, set(), actions, set(), [m]) is None   # sent: its no_order covers it


def test_a_web_app_only_digest_does_not_start_the_chats_status_gap(env, monkeypatch):
    """Upstream note d12a28e292fd1e82: a digest the chat never got (the web app only) leaves its status
    gap alone."""
    from ttp import chatdigest
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("coordinator.chat_digest_h", 2)
    t0 = 1_800_000_000.0
    chatdigest.hold(p.db, "c1", "#12 is queued for a worker.", "progress", t0)
    assert chatdigest.flush(p, p.config(), t0 + 2 * 3600)
    assert chatdigest.status_wait(p.db, p.config(), "c1", t0 + 2 * 3600 + 1) == 0
