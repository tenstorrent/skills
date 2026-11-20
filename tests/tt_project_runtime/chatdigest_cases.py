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


def test_a_routine_hand_off_within_the_hour_is_folded_into_the_next_status(env, monkeypatch):
    """A finished task's routine hand-off and shipped notice wait as one line each and go out in the next
    status post, once the chat's status gap allows; nothing is lost."""
    from ttp import chatdigest, hourly, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
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
    chatdigest.mark_status(p.db, "main", now + 3650)   # a status reply reached the chat
    c = _cd_task(p, "one more")
    pushq._reply(p.db, c, "done", "Done.")
    assert chatdigest.status_flush(p, p.config(), now + 3700) == []
    got = chatdigest.status_flush(p, p.config(), now + 7300)
    assert got and f"#{c['id']} one more: done" in p.db.one("SELECT text FROM messages WHERE id=?", (got[0],))["text"]


def test_held_hand_off_lines_stay_in_the_web_app(env, monkeypatch):
    """The status post carrying the held hand-off lines is recorded for the web
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


def test_a_hand_off_also_posts_once_in_the_chat_of_an_obligation_it_delivers(env, monkeypatch):
    """#3399: a task replying to chat A that owns an order from chat B, or names one from chat C by key,
    posts its hand-off in A, B and C, once each; an order from A itself adds no second post in A."""
    from ttp import chatdigest, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    now = time.time()
    a = _cd_task(p, "find the causes", chat="A")
    for text, owners, chat in (("the causes", [a["id"]], "B"), ("the causes again", [a["id"]], "A"),
                               ("a related order", [], "C")):
        mid = p.db.post("in", text, chat=chat)
        obligations.add_order(p, text, mid, owners=owners, chat=chat, action_class="read", now=now)
    oc = next(o["id"] for o in obligations.items(p.db) if o["promise"]["chat"] == "C")
    pushq._reply(p.db, a, "done", f"Causes and fixes (for {oc}).")
    want = [f"Causes and fixes (for {oc})."]
    assert (_cd_out(p, "A"), _cd_out(p, "B"), _cd_out(p, "C")) == (want, want, want)


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


def test_a_promised_result_posts_whole_at_once_from_a_normal_finish(env, monkeypatch):
    from ttp import chatdigest
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    tid = _cd_promised_task(p)
    chatdigest.mark_status(p.db, "c1", time.time() - 60)   # inside the chat's status gap
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-cd-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "scored", "message": _CD_TABLE}))
    d = Daemon(p.base)
    with d.p.db.tx():
        d._finish_worker({"task": tid}, Usage(cost_usd=0.1), "ok", run_dir)
    assert p.db.task(tid)["status"] == "done"
    assert _cd_out(p)[-1] == _CD_TABLE, _cd_out(p)
    assert not (p.db.kv(chatdigest.KV) or {}).get("held", {}).get("c1")


def test_a_promised_result_posts_whole_at_once_from_a_model_free_on_pass_finish(env, monkeypatch):
    from ttp import chatdigest
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    _sessions(env, monkeypatch, "s1")
    tid = _cd_promised_task(p)
    d = Daemon(p.base)
    cmd = ("""printf '%s' '""" + json.dumps({"status": "done", "summary": "scored", "message": _CD_TABLE})
           + """' > "$TTP_RESULT\"""")
    monkeypatch.setenv("TTP_FAKE_SESSION", "s1")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "waiting", "summary": "scoring runs", "waiting_for": "its job", "retry_when": "true",
        "retry_after_s": 600, "on_pass_cmd": cmd}))
    d.dispatch()
    _finish_runs(p, d)
    assert p.db.task(tid)["status"] == "queued", dict(p.db.task(tid))
    chatdigest.mark_status(p.db, "c1", time.time() - 60)
    assert d._start_on_pass(p.db.task(tid), time.time()) is True
    assert _until_true(lambda: d._poll_on_pass(time.time()) or tid not in d._on_pass)
    assert p.db.task(tid)["status"] == "done", dict(p.db.task(tid))
    assert _cd_out(p)[-1] == _CD_TABLE, _cd_out(p)


def test_an_order_owned_by_a_reply_chat_task_posts_its_result_whole(env, monkeypatch):
    """Owned open, or closed by the task's own receipt before it hands off: either way it posts whole."""
    from ttp import chatdigest, obligations, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    now = time.time()
    chatdigest.mark_status(p.db, "c1", now - 60)
    for receipt in (False, True):
        mid = p.db.post("in", "post the new table with the same columns", chat="c1")
        t = _cd_task(p, "the new table", chat="c1")
        oid = obligations.add_order(p, "post the new table", mid, [t["id"]], due=now + 7200,
                                    action_class="change", now=now).split()[1]
        if receipt:   # the run's own `ttp obligation receipt` as a required owner
            obligations.attach(p, oid, [t["id"]], now=now)
            obligations.record(p, oid, "receipt", "table in the hand-off", t["id"], now=now + 1)
            assert not obligations._open(obligations.get(p.db, oid))
        pushq._reply(p.db, t, "done", _CD_TABLE + f" ({receipt})")
        assert _cd_out(p)[-1] == _CD_TABLE + f" ({receipt})", _cd_out(p)


def test_a_promise_in_a_posted_hand_off_goes_to_the_next_task_replying_there(env, monkeypatch):
    """#8608 posted 'I will post the finished rows when they end.'; #8639, added later to finish them,
    owned nothing, so its table was held. The posted promise is recorded and the new task owns it."""
    from ttp import chatdigest, coordinator as coord, obligations
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    first = _cd_task(p, "the reference rows", chat="c1")
    mid = p.db.post("in", "post the new table with the same columns", chat="c1")
    obligations.add_order(p, "post the new table", mid, [first["id"]], due=time.time() + 7200, action_class="change")
    posted = chatdigest.handoff(p.db, p.config(), first, "done",
                                "Rows so far. The open models are slow; I will post the finished rows when they end.",
                                p=p)
    assert posted
    owed = [o for o in obligations.items(p.db) if obligations._open(o) and (o.get("promise") or {}).get("chat") == "c1"
            and (o.get("promise") or {}).get("order") is None]
    assert owed and not owed[0]["promise"]["tasks"]
    coord.apply(p, [{"type": "task_add", "title": "finish the open-model rows", "spec": "finish them",
                     "kind": "work", "tier": "light", "priority": 2, "reply_chat": "c1"}], turn=2)
    tid = p.db.one("SELECT id FROM tasks WHERE reply_chat='c1' ORDER BY id DESC LIMIT 1")["id"]
    assert chatdigest.owed(p.db, tid, "c1") == [owed[0]["id"]]
    chatdigest.mark_status(p.db, "c1", time.time() - 60)
    assert chatdigest.handoff(p.db, p.config(), dict(p.db.task(tid)), "done", _CD_TABLE)
    assert _cd_out(p)[-1] == _CD_TABLE


def test_a_reply_chat_hand_off_owing_nothing_is_still_one_held_line(env, monkeypatch):
    from ttp import chatdigest, coordinator as coord, pushq
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p = make(env)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Queued for a worker."},
                    {"type": "task_add", "title": "tidy the docs", "spec": "tidy", "kind": "work", "tier": "light",
                     "priority": 3, "reply_chat": "c1"}], turn=1)
    t = dict(p.db.one("SELECT * FROM tasks WHERE reply_chat='c1' ORDER BY id DESC LIMIT 1"))
    chatdigest.mark_status(p.db, "c1", time.time() - 60)
    before = _cd_out(p)
    pushq._reply(p.db, t, "done", "Docs tidied. Two pages merged.")
    assert _cd_out(p) == before
    assert [i["task"] for i in p.db.kv(chatdigest.KV)["held"]["c1"] if i["why"] == chatdigest.HANDOFF] == [t["id"]]


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
