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
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Power cycling it now, nothing else touches the box.",
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
    p.set_config("notify.chat_status", True)   # the opt-in: the digest goes to the chat
    cfg, t0 = p.config(), 1_800_000_000.0
    assert chatdigest.flush(p, cfg, t0) == []   # nothing held: no digest
    chatdigest.hold(p.db, "c1", "#12 is queued for a worker.", "progress", t0)
    chatdigest.hold(p.db, "c1", "Got it.", "ack", t0 + 600)
    assert chatdigest.flush(p, cfg, t0 + 3600) == []   # the oldest waited 1 h of 2
    [mid] = chatdigest.flush(p, cfg, t0 + 2 * 3600)
    text = p.db.one("SELECT text, chat FROM messages WHERE id=?", (mid,))
    assert text["chat"] == "c1" and text["text"].startswith("Digest: 2 routine updates since ")
    assert "#12 is queued for a worker." in text["text"] and "Got it." in text["text"]
    chatdigest.hold(p.db, "c1", "#13 is queued.", "progress", t0 + 2 * 3600 + 60)
    assert chatdigest.flush(p, cfg, t0 + 3 * 3600 + 120) == []   # 2 h since the last digest not yet passed
    assert len(chatdigest.flush(p, cfg, t0 + 4 * 3600 + 60)) == 1
    assert chatdigest.counts(p.db)["c1"] == {"sent": 0, "held": 3, "digests": 2, "waiting": 0, "deduped": 0}


def test_the_digest_stays_in_the_web_app_by_default(env, monkeypatch):
    """#8356: without notify.chat_status the digest is recorded for the web app only: no chat, the quiet
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


def test_a_routine_hand_off_within_the_hour_is_folded_into_the_next_status(env, monkeypatch):
    """A finished task's routine hand-off and shipped notice wait as one line each and go out in the next
    status post, once the chat's status gap allows; nothing is lost."""
    from ttp import chatdigest, hourly, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    p.set_config("notify.chat_status", True)   # the opt-in: status posts go to the chat
    p.db.post("in", "hello", chat="main")
    now = time.time()
    chatdigest.mark_status(p.db, "main", now - 600)   # a status went out 10 minutes ago
    a, b = _cd_task(p, "tidy the docs"), _cd_task(p, "speed up a check")
    pushq._reply(p.db, a, "done", "Docs tidied. Two pages merged.")
    pushq._reply(p.db, b, "done", "(shipped by review #9) the check runs in 40 s now.")
    assert not p.db.q("SELECT id FROM messages WHERE direction='out'"), "both held inside the status gap"
    assert hourly.post(p, now) is None and chatdigest.status_flush(p, p.config(), now) == []
    held = [i["task"] for i in p.db.kv(chatdigest.KV)["held"]["main"] if i["why"] == chatdigest.HANDOFF]
    assert held == [a["id"], b["id"]]
    o = obligations.add(p, "an order", now=now)
    obligations.record(p, o, "receipt", "commit abc1234", None, now=now + 3100)
    mid = hourly.post(p, now + 3601)
    text = p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"]
    assert "Finished since the last status (2):" in text
    assert f"- #{a['id']} tidy the docs: done: Docs tidied." in text and f"#{b['id']} speed up a check: done" in text
    assert not [i for i in p.db.kv(chatdigest.KV)["held"]["main"] if i["why"] == chatdigest.HANDOFF]
    # With no table due, status_flush posts the held lines alone once the gap allows.
    c = _cd_task(p, "one more")
    pushq._reply(p.db, c, "done", "Done.")
    assert chatdigest.status_flush(p, p.config(), now + 3700) == []
    got = chatdigest.status_flush(p, p.config(), now + 7300)
    assert got and f"#{c['id']} one more: done" in p.db.one("SELECT text FROM messages WHERE id=?", (got[0],))["text"]


def test_held_hand_off_lines_stay_in_the_web_app_without_chat_status(env, monkeypatch):
    """#8356 with #8341: by default the status post carrying the held hand-off lines is recorded for the web
    app only (no chat, the quiet channel); nothing is lost and no chat gets it."""
    from ttp import alerts, chatdigest, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    now = time.time()
    chatdigest.mark_status(p.db, "main", now - 60)
    a = _cd_task(p, "tidy the docs")
    pushq._reply(p.db, a, "done", "Docs tidied.")
    assert chatdigest.status_flush(p, p.config(), now) == []   # inside the status gap
    got = chatdigest.status_flush(p, p.config(), now + 3700)
    assert got
    row = p.db.one("SELECT chat, channel, text FROM messages WHERE id=?", (got[0],))
    assert row["chat"] is None and row["channel"] == alerts.QUIET and f"#{a['id']} tidy the docs: done" in row["text"]
    assert not [i for i in (p.db.kv(chatdigest.KV)["held"].get("main") or []) if i["why"] == chatdigest.HANDOFF]


def test_an_owed_or_user_blocking_hand_off_posts_at_once(env, monkeypatch):
    """A hand-off that delivers an open obligation its task owns, or a failure only the user can clear,
    goes at once inside the status gap; a routine failure waits."""
    from ttp import chatdigest, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    now = time.time()
    chatdigest.mark_status(p.db, "main", now - 60)
    a = _cd_task(p, "find the causes")
    obligations.add(p, "the causes and fixes", owners=[a["id"]], now=now)
    pushq._reply(p.db, a, "done", "Causes and fixes: the table posted on each batch.")
    assert _cd_out(p, "main") == ["Causes and fixes: the table posted on each batch."]
    b = _cd_task(p, "rerun a check")
    pushq._reply(p.db, b, "failed", "The build host is down.")
    c = _cd_task(p, "flaky test")
    pushq._reply(p.db, c, "failed", "A test was flaky; retried by itself.")
    assert _cd_out(p, "main")[1:] == [f"(task #{b['id']} failed) The build host is down."]
    assert [i["task"] for i in p.db.kv(chatdigest.KV)["held"]["main"]] == [c["id"]]


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
                found.append(f"{f.name}:{call.lineno}")
                if not names & pace:
                    bad.append(f"{f.name}:{call.lineno} in {fn.name}")
    assert found, "the scan found no posting call: the guard is broken"
    assert not bad, f"chat posts that skip the chat's status pace: {bad}"
