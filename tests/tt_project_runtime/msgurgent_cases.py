# Urgent messages never go silent (msgurgent.py, ttp say/note --urgent, ttp urgent-reply, the daemon step)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new msgurgent tests here.
from __future__ import annotations


def _urgent_project(env, tag="[authored by AI]"):
    p = make(env)
    p.db.ai_tag = tag
    return p


def _inbound(p, text, chat="c1", ts=None, **kw):
    mid = p.db.post("in", text, chat=chat, provenance="cli-legacy", **kw)
    if ts is not None:
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (ts, mid))
    return mid


def _outs(p, kind=None):
    rows = p.db.q("SELECT * FROM messages WHERE direction='out' ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_an_urgent_message_is_acked_within_a_minute_naming_the_owner_and_the_first_finding_time(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    tid = p.db.add_task("stream fix", "s", kind="code", origin="user")
    now = time.time()
    _inbound(p, f"urgent: the live stream is down, see #{tid}", ts=now)
    out = msgurgent.tick(p, p.config(), now=now + 5)
    assert out and out[0].startswith("ack")
    ack = _outs(p, "urgent_ack")
    assert len(ack) == 1 and ack[0]["chat"] == "c1"
    t = ack[0]["text"]
    assert "coordinator" in t and f"#{tid}" in t, t
    due = msgurgent.timefmt.short(now + 300, p, now)
    assert due in t and "UTC" in t, t
    assert t.endswith("[authored by AI]"), t
    assert ack[0]["ts"] - now < 60
    assert msgurgent.tick(p, p.config(), now=now + 10) == [], "the ack is posted once"
    assert len(_outs(p, "urgent_ack")) == 1


def test_a_late_notice_fires_once_reaches_the_senders_chat_and_wakes_the_coordinator(env):
    from ttp import msgurgent, mustact
    p = _urgent_project(env)
    now = time.time()
    mid = _inbound(p, "urgent: nothing works", ts=now)
    seen = []
    msgurgent.tick(p, p.config(), now=now + 1, steer=lambda t, k: seen.append((t, k)))
    assert not seen and not _outs(p, "urgent_late")
    out = msgurgent.tick(p, p.config(), now=now + 301, steer=lambda t, k: seen.append((t, k)))
    assert f"late {mid}" in out
    late = _outs(p, "urgent_late")
    assert len(late) == 1 and late[0]["chat"] == "c1" and late[0]["severity"] == "high"
    assert "next check" in late[0]["text"] and late[0]["text"].endswith("[authored by AI]")
    assert len(seen) == 1 and seen[0][1] == f"urgent-late-{mid}" and "first finding" in seen[0][0]
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (msgurgent.EVENT_KIND,))
    assert len(ev) == 1 and ev[0]["severity"] == "high"
    assert mustact.item(ev[0])["chat"] == "c1" and msgurgent.EVENT_KIND in mustact.KINDS
    assert msgurgent.tick(p, p.config(), now=now + 302, steer=lambda t, k: seen.append((t, k))) == []
    assert len(_outs(p, "urgent_late")) == 1 and len(seen) == 1


def test_a_second_miss_notifies_the_user_at_high_once(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    mid = _inbound(p, "urgent: help", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    msgurgent.tick(p, p.config(), now=now + 301)
    assert not _outs(p, "alert")
    out = msgurgent.tick(p, p.config(), now=now + 601)
    assert f"escalated {mid}" in out
    al = _outs(p, "alert")
    assert len(al) == 1 and al[0]["severity"] == "high" and al[0]["chat"] is None
    assert msgurgent.tick(p, p.config(), now=now + 900) == []
    assert len(_outs(p, "alert")) == 1


def test_the_deadlines_survive_a_daemon_restart_and_fire_once(env):
    from ttp import msgurgent
    from ttp.project import Project
    p = _urgent_project(env)
    now = time.time()
    mid = _inbound(p, "urgent: the live stream is down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 2)
    assert len(_outs(p, "urgent_ack")) == 1
    p2 = Project(p.base)
    p2.db.ai_tag = "[authored by AI]"
    rec = msgurgent.records(p2.db)[str(mid)]
    assert rec["acked"] and rec["find_due"] == pytest.approx(now + 300)
    assert msgurgent.tick(p2, p2.config(), now=now + 30) == [], "no second ack after a restart"
    assert f"late {mid}" in msgurgent.tick(p2, p2.config(), now=now + 301)
    p3 = Project(p.base)
    assert msgurgent.tick(p3, p3.config(), now=now + 302) == [], "no second late notice after a restart"
    assert len(_outs(p3, "urgent_ack")) == 1 and len(_outs(p3, "urgent_late")) == 1


def test_the_coordinators_first_reply_closes_the_deadline(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    _inbound(p, "urgent: broken", ts=now)
    _inbound(p, "urgent: also broken", chat="c2", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    p.db.post("out", "digest line", chat="c1", kind="digest")
    p.db.post("out", "it is fixed", chat="c2", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 302) == [f"late {msgurgent.open_for_chat(p.db, 'c1')[0]['mid']}"]
    assert [r["chat"] for r in msgurgent.open_for_chat(p.db, None)] == ["c1"], "only c2 was answered"
    p.db.post("out", "first finding", chat="c1", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 900) == []
    assert not msgurgent.pending(p.db) and len(_outs(p, "alert")) == 0


def test_a_non_urgent_message_behaves_as_before(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    for text in ("please add a docs page about caching", "this is not urgent, whenever you can",
                 "the build is failing for a branch nobody uses"):
        _inbound(p, text, ts=now)
    assert msgurgent.tick(p, p.config(), now=now + 1) == []
    assert msgurgent.tick(p, p.config(), now=now + 400) == []
    assert not _outs(p) and not msgurgent.records(p.db)
    assert not p.db.q("SELECT 1 FROM events WHERE kind=?", (msgurgent.EVENT_KIND,))


def test_a_classifier_false_positive_gives_only_an_ack(env):
    from ttp import msgurgent
    p = _urgent_project(env)
    now = time.time()
    _inbound(p, "the users page of the web app crashed in a test, I think", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    assert len(_outs(p)) == 1 and _outs(p)[0]["kind"] == "urgent_ack"
    p.db.post("out", "looked: a test fixture, nothing live", chat="c1", kind="reply")
    assert msgurgent.tick(p, p.config(), now=now + 600) == []
    assert [r["kind"] for r in _outs(p)] == ["urgent_ack", "reply"] and not msgurgent.pending(p.db)


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
    _inbound(p, "urgent: old news", ts=now - 3600)
    assert msgurgent.tick(p, p.config(), now=now) == [] and not _outs(p)


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
    _inbound(p, "urgent: down", ts=now)
    msgurgent.tick(p, p.config(), now=now + 1)
    cli.main(["urgent-reply", "c1", "Found it: the encoder died; restarting it now, next check 14:40 PST."])
    assert [r["kind"] for r in _outs(p)][-1] == "reply"
    assert msgurgent.tick(p, p.config(), now=now + 400) == [] and not msgurgent.pending(p.db)


def test_the_must_act_block_names_the_late_urgent_message(env):
    from ttp import msgurgent, mustact
    p = _urgent_project(env)
    now = time.time()
    mid = _inbound(p, "urgent: down", ts=now)
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
    _inbound(d.p, "urgent: the live stream is down", ts=now - 400)
    p.db.set_kv("urgent_msg_cursor", 0)
    d.cfg = {**d.cfg, "urgent_msg": {"stale_s": 10_000}}
    d.watch_urgent_messages()
    assert len(_outs(d.p, "urgent_ack")) == 1
    assert len(_outs(d.p, "urgent_late")) == 1, "past its first-finding deadline on arrival of the scan"
    assert "first finding" in (run / "steer.md").read_text()
