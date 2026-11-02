# Self-blocking detector tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of selfblock.py and its daemon check here.
from __future__ import annotations

# Held for the user's word on the coordinator's own call: each must fire.
_SELF_BLOCKING = (
    "Keeping the test gate at the full suite: that rule changes only on the user's word",
    "Holds the landing order until the user says which change goes first",
    "Pacing stays at one review a day; only you can change it",
    "waiting for your ok on narrowing the hold scope to one box",
    "Needs the user to approve raising max_parallel_tasks to 4",
    "pending user approval for the retry policy",
    "Reset the idle box? It has no other tenant; waiting on the user's go-ahead",
)
# One of the five real gates: each must stay quiet.
_REAL_GATES = (
    "Deleting the old results directory needs the user's word",                         # (a) destroys data
    "Waits for the user: the machine is shared with other teams",                         # (a) others use it
    "Rewriting the branch history needs your ok: another developer pushes to it",        # (a) shared branch
    "Waits for the user's ok to merge PR 12",                                             # (b) a merge
    "Needs your yes to post the summary in the channel on your behalf",                   # (b) speaks as the user
    "Waiting for the user to sign in with the device code",                               # (c) credential
    "Raising the daily budget to $40 needs the user's word",                              # (c) money
    "Held on the user's word: a charter Restriction forbids pushing there",               # (d) restriction
    "The resource was paused by the user; it lifts on their word",                        # (e) the user's pause
    "delivery.allow_protected_push_branch needs the user's word",                         # a user-only setting
)


def test_selfblock_finding_fires_for_the_coordinators_own_call_and_is_quiet_for_real_gates():
    from ttp import selfblock
    for s in _SELF_BLOCKING:
        assert selfblock.finding(s), s
    for s in _REAL_GATES:
        assert selfblock.finding(s) is None, s
    # No wait on the user at all: quiet.
    assert selfblock.finding("waits for the broker queue to drain") is None
    assert selfblock.finding("In the user's words (verbatim): keep going") is None
    assert selfblock.finding("") is None
    # Said in the negative, or with curly quotes and no apostrophe: read as written.
    assert selfblock.finding("Decided the pacing myself, no longer waiting for your ok") is None
    assert selfblock.finding("A project gate is never held for the user's word") is None
    assert selfblock.finding("Held on the user’s say-so for the pacing")
    assert selfblock.finding("blocked: the users word decides the hold scope")


def test_selfblock_raises_one_event_per_finding_from_tasks_asks_and_replies(env):
    p = make(env)
    from ttp import coordinator as coord, selfblock
    db = p.db
    own = db.add_task("narrow the test gate")
    db.update_task(own, status="blocked", blocked_reason=_SELF_BLOCKING[0])
    real = db.add_task("merge it")
    db.update_task(real, status="blocked", blocked_reason=_REAL_GATES[3])
    # An irreversible ask naming no harm is the coordinator's; one naming deletion is a real gate.
    a_own = db.post("out", "Change the review pacing to two a day?", chat=None, kind="ask",
                    severity="normal", ref="blocking:irreversible")
    db.post("out", "Delete the stale run folders?", chat=None, kind="ask", severity="normal",
            ref="blocking:irreversible")
    # A user-only ask, and a task held on it, stay quiet whatever they say.
    a_user = db.post("out", "Merge PR 7?", chat=None, kind="ask", severity="high", ref="blocking:merge")
    held = db.add_task("waits on the merge")
    db.update_task(held, status="blocked", blocked_reason=f"waits for your ok on ask {a_user}",
                   labels=json.dumps([f"waits:ask:{a_user}"]))
    r_own = db.post("out", "I left the hold scope as it is: that changes only on your word.", chat=None,
                    kind="reply", severity="low")
    r_pause = db.post("out", "The queue stays paused until you say so: you asked me to pause it.", chat=None,
                      kind="reply", severity="low")   # (e): a pause the user set
    now = time.time()
    found = selfblock.raise_due(db, now)
    got = {(f["source"], f["id"]) for f in found}
    assert ("task", own) in got and ("ask", a_own) in got and ("reply", r_own) in got
    assert ("task", real) not in got and ("task", held) not in got and ("ask", a_user) not in got
    assert ("reply", r_pause) not in got and len(found) == 3
    (ev,) = db.q("SELECT * FROM events WHERE kind=?", (selfblock.KIND,))
    assert ev["status"] == "queued" and f"task #{own}" in ev["text"]
    assert "memory_add kind decision" in ev["text"] and "notify low" in ev["text"]
    assert coord.EFFORT_EVENT_TRIGGERS[selfblock.KIND] == "stuck"
    # Each finding raises once, across checks.
    assert selfblock.raise_due(db, now + 60) == []
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    # A new reason on the same task is a new finding.
    db.update_task(own, blocked_reason="Landing order stays fixed until the user decides")
    (again,) = selfblock.raise_due(db, now + 120)
    assert (again["source"], again["id"]) == ("task", own)
    # An old reply is out of the window.
    assert all(f["source"] != "reply" for f in selfblock.scan(db, now + 2 * selfblock.REPLY_WINDOW_S))


def test_selfblock_quiet_for_a_setting_hold_and_a_queued_task(env):
    p = make(env)
    from ttp import selfblock, settinghold
    db = p.db
    t = db.add_task("land on main")
    db.update_task(t, status="blocked", blocked_reason=f"{settinghold.NOTE} delivery.allow_protected_push_branch "
                                                       f"is off; waits for the user's word")
    q = db.add_task("queued")
    db.update_task(q, blocked_reason=_SELF_BLOCKING[0])   # queued: no hold
    assert selfblock.scan(db) == []


def test_selfblock_reads_a_waiting_tasks_hand_off_and_survives_a_torn_result(env):
    p = make(env)
    from ttp import selfblock
    db = p.db
    w = db.add_task("waits")
    db.update_task(w, status="waiting", result=json.dumps({"waiting_for": "the user's ok on the landing order"}))
    torn = db.add_task("torn")
    db.update_task(torn, status="waiting", result="{not json")
    signin = db.add_task("sign in")
    db.update_task(signin, status="waiting", result=json.dumps({"waiting_for": "waits for the user to sign in"}))
    assert [(f["source"], f["id"]) for f in selfblock.scan(db)] == [("task", w)]


def test_daemon_self_blocking_check_raises_and_survives_a_failure(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod, selfblock
    d = dmod.Daemon(p.base)
    t = p.db.add_task("narrow the gate")
    p.db.update_task(t, status="blocked", blocked_reason=_SELF_BLOCKING[2])
    d.check_self_blocking()
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    d._selfblock_checked = 0.0
    monkeypatch.setattr(selfblock, "raise_due", lambda *a, **k: 1 / 0)
    d.check_self_blocking()   # logged, never raised


def test_selfblock_reply_counts_only_hold_wording_not_a_bare_possessive():
    from ttp import selfblock
    for s in ("Decided: narrowed the test gate. Your word reverses it.",
              "Decided: narrowed the test gate to the changed files. Your word reverses it.",
              "Which laptop to use is your call.",
              "your decision.",
              "I can keep the hold scope at all boxes if you prefer; your decision."):
        assert selfblock.finding(s, rx=selfblock.HOLD) is None, s
    for s in ("I left the hold scope as it is: that changes only on your word.",
              "The landing order stays as is until your ok.",
              "Pacing is held for the user's word.",
              "Only you can widen the hold scope.",
              "This needs you to approve the new pacing."):
        assert selfblock.finding(s, rx=selfblock.HOLD), s
    # A PR or draft review ask is the user's (the PR flow): quiet.
    assert selfblock.finding("Waiting for your draft review of the change") is None
    assert selfblock.finding("Held for the user's PR review") is None


def test_selfblock_reply_raises_at_most_once_per_window_and_never_on_its_own_answer(env):
    p = make(env)
    from ttp import selfblock
    db = p.db
    db.post("out", "Decided: narrowed the test gate. Your word reverses it.", chat=None, kind="reply",
            severity="low")
    db.post("out", "Which laptop to use is your call.", chat=None, kind="reply", severity="low")
    now = time.time()
    assert selfblock.raise_due(db, now) == []
    r1 = db.post("out", "I left the hold scope as it is: that changes only on your word.", chat=None,
                 kind="reply", severity="low")
    (f,) = selfblock.raise_due(db, now + 1)
    assert (f["source"], f["id"]) == ("reply", r1)
    # The turn's answer, and any other holding reply, posted inside the window raise nothing,
    # neither now nor once the window has passed.
    db.post("out", "Kept the landing order: it changes only on your word.", chat=None, kind="reply", severity="low")
    assert selfblock.raise_due(db, now + 60) == []
    assert selfblock.raise_due(db, now + selfblock.REPLY_WINDOW_S + 120) == []
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    # A task finding is not limited by the reply window.
    t = db.add_task("pacing")
    db.update_task(t, status="blocked", blocked_reason=_SELF_BLOCKING[2])
    assert [(g["source"], g["id"]) for g in selfblock.raise_due(db, now + 120)] == [("task", t)]


def test_selfblock_flags_a_throughmute_hold_with_no_gate_and_spares_a_cited_one(env):
    p = make(env)
    from ttp import selfblock
    from ttp import screen
    db = p.db
    later = time.time() + 3600   # every mute in force
    # A throughput-class mute whose owner is 'outside' (its why says only the user or an outside
    # party can clear it) and names no GATES entry: one finding, naming the mute and its wait.
    flagged = {"until": later, "source": "watcher:queue", "match": "lane_wait", "throughput": {"owner": {
        "outside": "the lane is full; only the user or an outside party can clear it, so the mute "
                   "waits on their word for the pacing"}}}
    # Its why names a GATES entry (shared machine): quiet.
    gated = {"until": later, "source": "watcher:box", "match": "queue", "throughput": {"owner": {
        "outside": "the shared machine is busy; only the user or an outside party can clear it"}}}
    # It cites the user's own message or ask: a user pause stays until the user's word.
    user_cited = {"until": later, "source": "watcher:queue", "match": "lane", "throughput": {"owner": {
        "outside": "only the user can clear it; (user msg #12, 2026-10-01) set the mute"}}}
    a_user = db.post("out", "Pause the runs?", chat=None, kind="ask", severity="normal", ref="blocking:merge")
    ask_cited = {"until": later, "source": "watcher:queue", "match": "lane2", "throughput": {"owner": {
        "outside": f"only the user can clear it; waits for your ok on ask {a_user}"}}}
    # A task- or probe-owned mute is never a selfblock finding (review_lines flags it).
    owned = {"source": "watcher:queue", "match": "lane3", "throughput": {"owner": {"task": 7}},
             "end_when": None, "until": time.time() + 3600}
    db.set_kv(screen.MUTES_KEY, [flagged, gated, user_cited, ask_cited, owned])
    found = selfblock.scan(db)
    mutes = [(f["source"], f["id"]) for f in found if f["source"] == "mute"]
    assert mutes == [("mute", "watcher:queue 'lane_wait'")], mutes
    assert "the lane is full" in found[0]["cited"] or "only the user" in found[0]["cited"]
    # raise_due lists it in its event, once (its fingerprint keeps it raised across checks).
    (f2,) = selfblock.raise_due(db, time.time() + 1)
    assert (f2["source"], f2["id"]) == mutes[0]
    assert selfblock.raise_due(db, time.time() + 120) == []
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1


def test_selfblock_quiet_for_a_wait_on_the_users_pr_review():
    from ttp import selfblock
    # 'review of the draft PR' / 'review of PR #N' is a draft review: the user's (PR flow), so a
    # task held on it must not raise. The old pattern matched only '(draft|pr) review' wording.
    for s in ("waits for the user's review of the draft PR #12",
              "waits for the user's review of the pull request",
              "Held until the user's review of the draft PR"):
        assert selfblock.finding(s) is None, s


def test_selfblock_judges_an_outside_mute_on_its_full_why_not_the_clipped_owner(env):
    p = make(env)
    from ttp import selfblock
    from ttp import screen
    db = p.db
    later = time.time() + 3600
    pad = "the lane is full; only the user or an outside party can clear it, so the mute waits on their word. " * 3
    assert len(pad) > 200
    # throughmute.vet stores owner['outside'] = why[:200]; the gate or cite past char 200 is only
    # in the mute's full why, which must keep the mute quiet.
    mutes = []
    for i, tail in enumerate(("the shared machine is busy", "(user msg #12) set the mute", "")):
        why = pad + tail
        mutes.append({"until": later, "source": "watcher:queue", "match": f"lane{i}", "why": why,
                      "throughput": {"owner": {"outside": why[:200]}}})
    db.set_kv(screen.MUTES_KEY, mutes)
    found = [(f["source"], f["id"]) for f in selfblock.scan(db) if f["source"] == "mute"]
    # Only the one with no gate and no cite anywhere in its why is flagged.
    assert found == [("mute", "watcher:queue 'lane2'")], found


def test_selfblock_raises_a_block_on_a_coordinator_set_key_named_as_the_users_within_one_tick(env):
    p = make(env)
    from ttp import coordinator as coord, daemon as dmod, selfblock
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.channel_tags", "value": ["chan"]}]) == []
    t = p.db.add_task("dogfood cut")
    # Also names a real gate ('force-push'): the recorded setter wins over the guess.
    p.db.update_task(t, status="blocked", blocked_reason="delivery.channel_tags is user-set and must not be "
                                                         "overridden, so I cannot move the tag without a force-push")
    dmod.Daemon(p.base).check_self_blocking()
    (ev,) = p.db.q("SELECT * FROM events WHERE kind=?", (selfblock.KIND,))
    assert f"task #{t}" in ev["text"] and "set by coordinator" in ev["text"] and "--why" in ev["text"], ev["text"]
    assert [(f["source"], f["id"]) for f in json.loads(ev["data"])["findings"]] == [("setter", t)]
    # A key with no record (set by hand, or before setters were recorded) is unknown: raised too.
    w = p.db.add_task("waits")
    p.db.update_task(w, status="waiting", result=json.dumps({"waiting_for": "budget.max_parallel_workers is the "
                                                                            "user's setting; not overriding it"}))
    found = {f["id"]: f for f in selfblock.scan(p.db)}
    assert found[w]["source"] == "setter" and "set by unknown" in found[w]["cited"], found


def test_selfblock_quiet_for_a_key_the_user_set(env, monkeypatch):
    p = make(env)
    from ttp import cli, coordinator as coord, selfblock
    for k in ("TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK_ID", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    cli.main(["config", p.name, "delivery.channel_tags", '["chan"]'])   # typed at a terminal: the user
    m = p.db.post("in", "two workers at a time", chat=None, kind="user")
    coord.apply(p, [{"type": "config_set", "key": "budget.max_parallel_workers", "value": 3}],
                user_turn=True, messages=[m])
    t = p.db.add_task("cut")
    p.db.update_task(t, status="blocked", blocked_reason="delivery.channel_tags and budget.max_parallel_workers are "
                                                         "user-set; I will not override them")
    assert selfblock.scan(p.db) == []
    assert selfblock.owned_keys("delivery.channel_tags is user-set") == ["delivery.channel_tags"]
    # No claim of ownership, or no config key: not this check.
    assert selfblock.owned_keys("waits for delivery.channel_tags to settle") == []
    assert selfblock.owned_keys("the user's setting in foo.bar") == []
    assert selfblock.owned_keys("set by the user in project.json") == []


def test_config_setter_is_recorded_shown_and_survives_a_restart_and_an_upgrade_migration(env, monkeypatch, capsys):
    p = make(env)
    from ttp import cli, configwho, coordinator as coord, db as dbmod
    from ttp.project import Project
    for k in ("TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK_ID", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    old = p.raw_config()
    p.set_config("delivery.never_force", True)   # a caller naming no setter
    assert configwho.role(configwho.get(p.db, "delivery.never_force")) == "unknown"
    coord.apply(p, [{"type": "config_set", "key": "delivery.channel_tags", "value": ["chan"]}])
    m = p.db.post("in", "two at a time", chat=None, kind="user")
    coord.apply(p, [{"type": "config_set", "key": "budget.max_parallel_workers", "value": 2}],
                user_turn=True, messages=[m])
    monkeypatch.setenv("TTP_RUN_ID", "77")
    monkeypatch.setenv("TTP_TASK_ID", "42")
    cli.main(["config", p.name, "heal.outage_owner_budget_min", "45"])
    monkeypatch.delenv("TTP_TASK_ID")
    cli.main(["config", p.name, "heal.outage_task_due_min", "30"])
    assert configwho.get(p.db, "budget.max_parallel_workers")["msgs"] == [m]
    rec = configwho.get(p.db, "heal.outage_owner_budget_min")
    assert (rec["by"], rec["task"]) == ("worker", 42)
    assert configwho.role(configwho.get(p.db, "heal.outage_task_due_min")) == "coordinator"
    # No name of a person anywhere in a record: roles, ids and the path only.
    assert set().union(*[r.keys() for r in p.db.kv(configwho.KEY).values()]) <= {"by", "msgs", "task", "run", "via", "ts"}
    # Restart: a new process opens the database afresh. Upgrade: the migrations run again on it.
    p.db.conn.close()
    c = sqlite3.connect(p.state / "project.db")
    for key in (dbmod.PROVENANCE_MIGRATION, dbmod.PUSH_QUEUE_MIGRATION, dbmod.LEDGER_ACCOUNT_MIGRATION):
        c.execute("DELETE FROM meta WHERE key=?", (key,))
    c.commit()
    c.close()
    q = Project(p.root)
    assert configwho.role(configwho.get(q.db, "delivery.channel_tags")) == "coordinator"
    assert configwho.get(q.db, "budget.max_parallel_workers")["msgs"] == [m]
    monkeypatch.delenv("TTP_RUN_ID")
    capsys.readouterr()
    cli.main(["config", p.name, "get", "--why", "budget.max_parallel_workers"])
    cli.main(["config", p.name, "get", "--why", "delivery.channel_tags"])
    cli.main(["config", p.name, "--why", "coordinator.effort"])   # never set: unknown
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"budget.max_parallel_workers: set by user (message #{m}) via chat, 20"), out
    assert out[1].startswith("delivery.channel_tags: set by coordinator via config_set"), out
    assert out[2].startswith("coordinator.effort: set by unknown"), out
    # --why only reads: nothing named 'get' was set.
    assert "get" not in q.raw_config() and q.raw_config().get("coordinator") == old.get("coordinator")
    # A whole section set replaces the records under it; a key with none takes its section's.
    q.set_config("delivery", {**q.raw_config().get("delivery", {})}, by=configwho.by("user", via="cli"))
    assert configwho.role(configwho.get(q.db, "delivery.channel_tags")) == "user"


def test_selfblock_setter_check_quiet_for_a_credential_money_or_sign_in_wait(env):
    p = make(env)
    from ttp import coordinator as coord, selfblock
    coord.apply(p, [{"type": "config_set", "key": "jev.enabled", "value": True}])   # coordinator-set
    reasons = ("jev.enabled needs the user's key (an API credential only they hold)",
               "waiting for a user-only step: sign in to the Slack app so notify.slack can post (credential)",
               "budget.max_usd is user-set and raising it needs money only the user holds")
    for r in reasons:
        assert selfblock.setter_finding(p.db, r) is None, r
    ids = []
    for r in reasons:
        t = p.db.add_task("w")
        p.db.update_task(t, status="waiting", result=json.dumps({"waiting_for": r}))
        ids.append(t)
    assert [f for f in selfblock.scan(p.db) if f["id"] in ids and f["source"] == "setter"] == []
    # 'budget.' in a key name is no spend: the setting-ownership guess still raises.
    assert "set by unknown" in selfblock.setter_finding(p.db, "budget.max_parallel_workers is user-set")
    assert "set by coordinator" in selfblock.setter_finding(p.db, "jev.enabled is the user's setting")
    # The event names the setter rule and does not claim the setter finding names no gate at all.
    txt = selfblock.text([{"source": "setter", "id": 1, "text": "x", "cited": "k"}])
    assert "None names" not in txt and "names no credential, money or step" in txt, txt
    txt = selfblock.text([{"source": "setter", "id": 1, "text": "x", "cited": "k"},
                          {"source": "task", "id": 2, "text": "y", "cited": "z"}])
    assert "None of the others names" in txt, txt
