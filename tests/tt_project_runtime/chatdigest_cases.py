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
    assert chatdigest.counts(p.db)["c1"] == {"sent": 0, "held": 4, "digests": 0, "waiting": 4}


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
    assert chatdigest.counts(p.db)["c1"] == {"sent": 0, "held": 3, "digests": 2, "waiting": 0}


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
