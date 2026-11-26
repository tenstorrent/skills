# Urgent messages never go silent (msgurgent.py, ttp say/note --urgent, ttp urgent-reply, the daemon step)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new msgurgent tests here.
from __future__ import annotations


def _urgent_project(env, tag="[authored by AI]"):
    p = make(env)
    p.db.ai_tag = tag
    return p


def _urg_inbound(p, text, chat="c1", ts=None, **kw):
    mid = p.db.post("in", text, chat=chat, provenance="cli-legacy", **kw)
    if ts is not None:
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (ts, mid))
    return mid


def _urg_outs(p, kind=None):
    rows = p.db.q("SELECT * FROM messages WHERE direction='out' ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_an_urgent_message_is_acked_within_a_minute_naming_the_owner_and_the_first_finding_time(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    tid = p.db.add_task("stream fix", "s", kind="code", origin="user")
    now = time.time()
    _urg_inbound(p, f"urgent: the live stream is down, see #{tid}", ts=now)
    out = msgurgent.tick(p, p.config(), now=now + 5)
    assert out and out[0].startswith("ack")
    ack = _urg_outs(p, "urgent_ack")
    assert len(ack) == 1 and ack[0]["chat"] == "c1"
    t = ack[0]["text"]
    assert "coordinator" in t and f"#{tid}" in t, t
    due = msgurgent.timefmt.short(now + 300, p, now)
    assert due in t and "UTC" in t, t
    assert t.endswith("[authored by AI]"), t
    assert ack[0]["ts"] - now < 60
    assert msgurgent.tick(p, p.config(), now=now + 10) == [], "the ack is posted once"
    assert len(_urg_outs(p, "urgent_ack")) == 1


def test_a_late_notice_fires_once_reaches_the_senders_chat_and_wakes_the_coordinator(env):
    from ttp import msgurgent, mustact
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: nothing works", ts=now)
    seen = []
    msgurgent.tick(p, p.config(), now=now + 1, steer=lambda t, k: seen.append((t, k)))
    assert not seen and not _urg_outs(p, "urgent_late")
    out = msgurgent.tick(p, p.config(), now=now + 301, steer=lambda t, k: seen.append((t, k)))
    assert f"late {mid}" in out
    late = _urg_outs(p, "urgent_late")
    assert len(late) == 1 and late[0]["chat"] == "c1" and late[0]["severity"] == "high"
    assert "next check" in late[0]["text"] and late[0]["text"].endswith("[authored by AI]")
    assert len(seen) == 1 and seen[0][1] == f"urgent-late-{mid}" and "first finding" in seen[0][0]
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (msgurgent.EVENT_KIND,))
    assert len(ev) == 1 and ev[0]["severity"] == "high"
    assert mustact.item(ev[0])["chat"] == "c1" and msgurgent.EVENT_KIND in mustact.KINDS
    assert msgurgent.tick(p, p.config(), now=now + 302, steer=lambda t, k: seen.append((t, k))) == []
    assert len(_urg_outs(p, "urgent_late")) == 1 and len(seen) == 1


def test_a_second_miss_notifies_the_user_at_high_once(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: help", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    msgurgent.tick(p, p.config(), now=now + 301)
    assert not _urg_outs(p, "alert")
    out = msgurgent.tick(p, p.config(), now=now + 601)
    assert f"escalated {mid}" in out
    al = _urg_outs(p, "alert")
    assert len(al) == 1 and al[0]["severity"] == "high" and al[0]["chat"] is None
    assert msgurgent.tick(p, p.config(), now=now + 900) == []
    assert len(_urg_outs(p, "alert")) == 1


def test_the_deadlines_survive_a_daemon_restart_and_fire_once(env):
    from ttp import msgurgent
    from ttp.project import Project
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 2)
    assert len(_urg_outs(p, "urgent_ack")) == 1
    p2 = Project(p.base)
    p2.db.ai_tag = "[authored by AI]"
    rec = msgurgent.records(p2.db)[str(mid)]
    assert rec["acked"] and rec["find_due"] == pytest.approx(now + 300)
    assert msgurgent.tick(p2, p2.config(), now=now + 30) == [], "no second ack after a restart"
    assert f"late {mid}" in msgurgent.tick(p2, p2.config(), now=now + 301)
    p3 = Project(p.base)
    assert msgurgent.tick(p3, p3.config(), now=now + 302) == [], "no second late notice after a restart"
    assert len(_urg_outs(p3, "urgent_ack")) == 1 and len(_urg_outs(p3, "urgent_late")) == 1


def test_the_coordinators_first_reply_closes_the_deadline(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    _urg_inbound(p, "urgent: broken", ts=now)
    _urg_inbound(p, "urgent: also broken", chat="c2", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    p.db.post("out", "digest line", chat="c1", kind="digest")
    p.db.post("out", "it is fixed", chat="c2", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 302) == [f"late {msgurgent.open_for_chat(p.db, 'c1')[0]['mid']}"]
    assert [r["chat"] for r in msgurgent.open_for_chat(p.db, None)] == ["c1"], "only c2 was answered"
    p.db.post("out", "first finding", chat="c1", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 900) == []
    assert not msgurgent.pending(p.db) and len(_urg_outs(p, "alert")) == 0


def test_a_non_urgent_message_behaves_as_before(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    for text in ("please add a docs page about caching", "this is not urgent, whenever you can",
                 "the build is failing for a branch nobody uses"):
        _urg_inbound(p, text, ts=now)
    assert msgurgent.tick(p, p.config(), now=now + 1) == []
    assert msgurgent.tick(p, p.config(), now=now + 400) == []
    assert not _urg_outs(p) and not msgurgent.records(p.db)
    assert not p.db.q("SELECT 1 FROM events WHERE kind=?", (msgurgent.EVENT_KIND,))


def test_a_classifier_false_positive_gives_only_an_ack(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    _urg_inbound(p, "the users page of the web app crashed in a test, I think", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    assert len(_urg_outs(p)) == 1 and _urg_outs(p)[0]["kind"] == "urgent_ack"
    p.db.post("out", "looked: a test fixture, nothing live", chat="c1", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 600) == []
    assert [r["kind"] for r in _urg_outs(p)] == ["urgent_ack", "reply"] and not msgurgent.pending(p.db)


@pytest.mark.parametrize("text,urgent", [
    ("urgent: look at this", True), ("[URGENT] build", True), ("**urgent** build", True),
    ("the live stream is down for our viewers", True), ("customers report the site is broken", True),
    ("sev0 right now", True), ("ASAP please", True),
    ("not urgent", False), ("no rush on the stream docs", False), ("the stream docs mention down-scaling", False),
    ("a test is broken", False), ("", False)])
def test_the_classifier_is_conservative(env, text, urgent):
    from ttp import msgurgent
    assert msgurgent.classify({}, text)[0] is urgent


def test_the_classifier_is_configurable(env):
    from ttp import msgurgent
    assert msgurgent.classify({"urgent_msg": {"markers": ["code red"]}}, "this is code red")[0]
    assert not msgurgent.classify({}, "this is code red")[0]
    assert not msgurgent.classify({"urgent_msg": {"enabled": False}}, "urgent: the live stream is down")[0]
    assert msgurgent.classify({}, "", "urgent: relayed")[0], "the sender's mark may sit in the context"
    from ttp.project import DEFAULT_CONFIG as DEFAULTS
    assert DEFAULTS["urgent_msg"]["enabled"] and DEFAULTS["urgent_msg"]["ack_s"] == 60
    assert DEFAULTS["urgent_msg"]["first_s"] == 300


def test_a_backlog_older_than_the_stale_window_is_not_acked(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    _urg_inbound(p, "urgent: old news", ts=now - 3600)
    assert msgurgent.tick(p, p.config(), now=now) == [] and not _urg_outs(p)


def test_ttp_say_urgent_marks_the_message(env, monkeypatch, capsys):
    from ttp import cli, msgurgent
    p = _urgent_project(env)
    monkeypatch.chdir(p.root)
    cli.main(["say", "--urgent", "the thing is on fire"])
    row = p.db.one("SELECT * FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1")
    assert row["text"].lower().startswith("urgent:") and msgurgent.classify({}, row["text"])[0]


def test_urgent_reply_is_the_coordinators_and_needs_an_open_urgent_message(env, monkeypatch, capsys):
    from ttp import cli, coordtools, msgurgent
    p = _urgent_project(env)
    monkeypatch.chdir(p.root)
    monkeypatch.setenv(coordtools.ROLE_ENV, "worker")
    with pytest.raises(SystemExit):
        cli.main(["urgent-reply", "c1", "finding"])
    monkeypatch.setenv(coordtools.ROLE_ENV, "coordinator")
    with pytest.raises(SystemExit):
        cli.main(["urgent-reply", "c1", "finding"])   # nothing urgent is open
    now = time.time()
    _urg_inbound(p, "urgent: down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    cli.main(["urgent-reply", "c1", "Found it: the encoder died; restarting it now, next check 14:40 PST."])
    assert [r["kind"] for r in _urg_outs(p)][-1] == "reply"
    assert msgurgent.tick(p, p.config(), now=now + 400) == [] and not msgurgent.pending(p.db)


def test_the_must_act_block_names_the_late_urgent_message(env):
    from ttp import msgurgent, mustact
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    msgurgent.tick(p, p.config(), now=now + 301)
    ids = [r["id"] for r in p.db.q("SELECT id FROM events WHERE kind=?", (msgurgent.EVENT_KIND,))]
    text = mustact.block(p.db, ids)
    assert f"urgent#{mid}" in text and "urgent-reply" in text, text


def test_the_daemon_step_acks_and_reaches_a_running_coordinator_turn(env):
    from ttp import daemon as dm
    p = _urgent_project(env)
    run = env["tmp"] / "coord-run"
    run.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(NULL,'coordinator','claude','running',?,?)",
           (time.time(), str(run)))
    d = dm.Daemon(p.base)
    d.p.db.ai_tag = "[authored by AI]"
    now = time.time()
    _urg_inbound(d.p, "urgent: the live stream is down", ts=now - 400)
    p.db.set_kv("urgent_msg_cursor", 0)
    d.cfg = {**d.cfg, "urgent_msg": {"stale_s": 10_000}}
    d.watch_urgent_messages()
    assert len(_urg_outs(d.p, "urgent_ack")) == 1
    assert len(_urg_outs(d.p, "urgent_late")) == 1, "past its first-finding deadline on arrival of the scan"
    assert "first finding" in (run / "steer.md").read_text()


def _urg_turn(p, tmp, provider="claude", started=None, messages=(), hooked=False):
    """A running coordinator turn row with its own run dir (the marker file when `hooked`)."""
    run = tmp / f"coord-run-{provider}-{len(list(tmp.glob('coord-run-*')))}"
    run.mkdir()
    if hooked:
        (run / "steer.hooked").write_text("1")
    p.db.x("INSERT INTO runs(task,role,provider,status,started,dir,note) VALUES(NULL,'coordinator',?,'running',?,?,?)",
           (provider, started if started is not None else time.time(), str(run), json.dumps({"messages": list(messages)})))
    return run


def _urg_rec(p, mid, now, **kw):
    from ttp import msgurgent
    rec = msgurgent.records(p.db)[str(mid)]
    rec.update(kw)
    return rec


def test_urgent_messages_are_read_first_by_the_coordinator_wake_and_the_digest(env):
    from ttp import msgurgent, coordinator as coord
    p = _urgent_project(env)
    a = _urg_inbound(p, "status: all green")
    b = _urg_inbound(p, "urgent: the live stream is down")
    c = _urg_inbound(p, "ok thanks")
    rows = [r for r in p.db.q("SELECT * FROM messages WHERE direction='in' ORDER BY id") if r["id"] in (a, b, c)]
    ordered, ids = msgurgent.first(p.db, p.config(), rows)
    assert [m["id"] for m in ordered] == [b, a, c] and ids == {b}
    plain, none = msgurgent.first(p.db, p.config(), [r for r in rows if r["id"] != b])
    assert [m["id"] for m in plain] == [a, c] and none == set()
    parts = dict(coord.digest_parts(p, {}, [], [a, b, c])[0])
    text = next(v for v in parts.values() if f"user message #{b}" in v)
    assert text.index(f"user message #{b}") < text.index(f"user message #{a}")
    assert "[URGENT:" in text.split(f"user message #{a}")[0] and "[URGENT:" not in text.split(f"user message #{a}")[1]
    off = {**p.config(), "urgent_msg": {"enabled": False}}
    assert msgurgent.first(p.db, off, rows)[1] == set()


def test_urgent_message_skips_the_debounce_and_the_relay_window_but_a_status_message_does_not(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    _wg_msg(p, clock, "status: the train is green, 3 changes landed")
    assert not _turn_within(d, clock, starts, 25), "a status message woke a turn inside the relay window"
    _wg_msg(p, clock, "urgent: the live stream is down for a customer")
    assert _turn_within(d, clock, starts, 10), "an urgent message waited on the debounce or relay window"
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    _wg_msg(p, clock, "urgent: the live stream is down for a customer", provenance="slack")
    assert _turn_within(d, clock, starts, 4, step=1), "an urgent message waited out the 15 s debounce"


def test_reach_a_hooked_running_turn_gets_the_urgent_update_and_is_cut_only_when_it_never_reads(env):
    from ttp import hook, msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    run = _urg_turn(p, env["tmp"], provider="claude", started=now - 600)
    rec = _urg_rec(p, mid, now)
    c = msgurgent.config(p.config())
    assert msgurgent.reach(p, rec, c, now + 2) == "steer"
    body = (run / "steer.md").read_text()
    assert f"URGENT message #{mid}" in body and "ttp urgent-reply" in body and "live stream is down" in body
    rec["reached"] = "steer"
    assert msgurgent.reach(p, rec, c, now + 2 + c["read_s"] - 1) == "steer", "cut before read_s"
    assert not (run / "STOP").exists()
    # The hook reads it: the turn has the message, so nothing is cut however long it waits.
    text, off = hook.unread_update(run)
    hook.mark_seen(run, off)
    assert msgurgent.reach(p, rec, c, now + 2 + c["read_s"] + 60) == "in_turn"
    assert not (run / "STOP").exists()
    # Unread past read_s (a long reasoning step with no tool call): the turn is cut and resumes later.
    p.db.x("UPDATE runs SET status='ok'")
    run2 = _urg_turn(p, env["tmp"], provider="claude", started=now - 600)
    rec2 = _urg_rec(p, mid, now, reached=None)
    assert msgurgent.reach(p, rec2, c, now + 10) == "steer"
    rec2["reached"] = "steer"
    assert msgurgent.reach(p, rec2, c, now + 10 + c["read_s"] + 1) == "cut"
    assert (run2 / "STOP").exists() and "preempt" in (run2 / "STOP").read_text()


def test_reach_an_unhooked_provider_turn_is_cut_after_cut_after_s_and_a_marker_makes_it_a_steer(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    c = msgurgent.config(p.config())
    young = _urg_turn(p, env["tmp"], provider="cursor", started=now - 5)
    rec = _urg_rec(p, mid, now)
    assert msgurgent.reach(p, rec, c, now) is None, "a young turn that cannot read updates is retried, not cut"
    assert not (young / "STOP").exists() and not (young / "steer.md").exists()
    assert msgurgent.reach(p, rec, c, now + c["cut_after_s"] + 1) == "cut"
    assert "preempt" in (young / "STOP").read_text()
    assert msgurgent.reach(p, rec, c, now + c["cut_after_s"] + 2) == "next", "already ending: the next turn reads it"
    p.db.x("UPDATE runs SET status='ok'")
    marked = _urg_turn(p, env["tmp"], provider="cursor", started=now - 300, hooked=True)
    assert msgurgent.reach(p, rec, c, now + 5) == "steer" and f"URGENT message #{mid}" in (marked / "steer.md").read_text()


def test_reach_a_chat_mini_swe_agent_turn_has_no_hook_so_it_is_cut_not_steered(env):
    """A chat provider turn whose run.json names a harness (mini-swe-agent) has no update hook even
    though the chat class inherits Claude Code's steer_hook flag: the urgent message is never
    written to its steer.md; the turn is cut once it is old enough, like any unhooked provider's."""
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    c = msgurgent.config(p.config())
    run = _urg_turn(p, env["tmp"], provider="chat", started=now - 5)
    (run / "run.json").write_text(json.dumps({"harness": {"kind": "mini-swe-agent", "version": "2.4.6"}}))
    rec = _urg_rec(p, mid, now)
    assert msgurgent.reach(p, rec, c, now) is None, "a young turn without a hook is retried, not steered"
    assert not (run / "steer.md").exists(), "the chat class's inherited steer_hook must not steer a mini run"
    assert msgurgent.reach(p, rec, c, now + c["cut_after_s"] + 1) == "cut"
    assert "preempt" in (run / "STOP").read_text() and not (run / "steer.md").exists()


def test_reach_with_no_turn_running_or_the_message_in_the_turn_cuts_nothing(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    c = msgurgent.config(p.config())
    rec = _urg_rec(p, mid, now)
    assert msgurgent.reach(p, rec, c, now + 2) == "next"
    run = _urg_turn(p, env["tmp"], provider="cursor", started=now - 600, messages=[mid])
    assert msgurgent.reach(p, rec, c, now + 2) == "in_turn"
    assert not (run / "STOP").exists() and not (run / "steer.md").exists()
    p.db.x("UPDATE runs SET note=NULL")
    p.db.x("UPDATE messages SET handled=1 WHERE id=?", (mid,))
    assert msgurgent.reach(p, rec, c, now + 2) == "in_turn"
    assert not (run / "STOP").exists()


def test_tick_records_where_the_urgent_message_reached_and_logs_it_once(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _urg_inbound(p, "urgent: the live stream is down", ts=now)
    run = _urg_turn(p, env["tmp"], provider="claude", started=now - 600)
    out = msgurgent.tick(p, p.config(), now=now + 1)
    assert f"reach {mid} steer" in out
    assert msgurgent.records(p.db)[str(mid)]["reached"] == "steer"
    assert not any(o.startswith("reach") for o in msgurgent.tick(p, p.config(), now=now + 2))
    assert (run / "steer.md").read_text().count(f"URGENT message #{mid}") == 1, "the update is appended once"
    out = msgurgent.tick(p, p.config(), now=now + 1 + msgurgent.config(p.config())["read_s"] + 2)
    assert f"reach {mid} cut" in out and (run / "STOP").exists()
    assert not any(o.startswith("reach") for o in msgurgent.tick(p, p.config(), now=now + 200))
