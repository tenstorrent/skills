# Older rules a user message overrides (runtime/ttp/rulematch.py, its hook in correction.flag and its Course
# check lines) of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new rulematch tests here.
from __future__ import annotations


def _rm_project(env):
    import subprocess
    p = make(env)
    p.charter_path.write_text("# Charter\n\n## Policies\n- Keep the report worker hold at 60 minutes (user msg #4).\n\n"
                              "## Restrictions\n- Never hold the report worker idle past an hour (user msg #5).\n")
    p.add_memory("The report worker keepalive is 60 min; never restart it sooner.", kind="decision")
    (p.root / "keep.py").write_text("KEEPALIVE_S = 3600\nLIMIT = 3\nOTHER_CAP = 7\n")
    for cmd in (["init", "-q"], ["add", "keep.py"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", *cmd], cwd=p.root, check=True, capture_output=True)
    return p


def test_rulematch_durations_name_one_value_in_any_unit():
    from ttp import rulematch as rm
    assert rm.durations("an hour") == rm.durations("60 min") == rm.durations("3600 s") == {3600}
    assert rm.durations("make it 5 min or 1.5 hours") == {300, 5400}
    assert rm.durations("fix the bugs and tests, as I am sure") == set()   # plain words are no durations
    assert rm.durations("a second question: the report worker is slow") == set()
    assert rm.durations("12M and 100M tokens, 1.5M jobs") == set()   # a capital M is a size, not minutes
    assert rm.durations("24H or 2D") == {86400, 172800}


def test_rulematch_lists_the_older_rules_on_the_topic_and_marks_restrictions(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    got = rm.older(p, p.db, "the keepalive of an hour is too long for the report worker, make it 5 min")
    where = {(g["where"], g["name"]) for g in got}
    assert any(w == "memory" for w, _ in where) and ("code", "KEEPALIVE_S") in where
    assert ("code", "LIMIT") not in where and len(got) <= rm.MAX
    charter = [g for g in got if g["where"] == "charter"]
    assert any("user-only: change only by the user's word" in g["line"] for g in charter), got
    assert any(g["where"] == "charter" and "Restrictions" in g["name"] for g in got)
    cfg = rm.older(p, p.db, "the worker isolation setting is wrong")
    assert ("config", "providers.claude.worker_isolation") in {(g["where"], g["name"]) for g in cfg}


def test_rulematch_needs_more_than_one_shared_word_for_code_and_config(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    got = rm.older(p, p.db, "please add a test for the keepalive handling")
    assert not [g for g in got if g["where"] in ("code", "config")]


def test_rulematch_a_value_alone_never_matches_a_constant(env):
    import subprocess
    from ttp import rulematch as rm
    p = _rm_project(env)
    (p.root / "vals.py").write_text("RIDE_ALONG_MAX_S = 3600\nCRASH_WINDOW_S = 3600\nKEYLESS_TTL = 3600\nLIMIT_S = 300\n")
    subprocess.run(["git", "add", "vals.py"], cwd=p.root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v"], cwd=p.root, check=True,
                   capture_output=True)
    got = rm.older(p, p.db, "the nightly report email is taking an hour, make it 5 min")
    assert not [g for g in got if g["where"] == "code"], got
    got = rm.older(p, p.db, "the crash handling of an hour is too long, make it 5 min")
    assert [g["name"] for g in got if g["where"] == "code"] == ["CRASH_WINDOW_S"]   # one name word plus the value


def test_rulematch_a_constant_needs_its_name_word_beside_the_duration(env):
    import subprocess
    from ttp import rulematch as rm
    p = _rm_project(env)
    (p.root / "vals.py").write_text("LIMIT_S = 3600\n")
    subprocess.run(["git", "add", "vals.py"], cwd=p.root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v"], cwd=p.root, check=True,
                   capture_output=True)
    far = "the provider limit we hit is fine; what bothers me is the dashboard taking an hour to load"
    assert not [g for g in rm.older(p, p.db, far) if g["where"] == "code"]
    near = "the provider limit of an hour is too short"
    assert [g["name"] for g in rm.older(p, p.db, near) if g["where"] == "code"] == ["LIMIT_S"]


def test_rulematch_finds_nothing_for_unrelated_text(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    assert rm.older(p, p.db, "thanks, looks great") == []
    assert rm.older(p, p.db, "please rename the colour palette of the dashboard") == []


def test_rulematch_caps_the_list_and_flag_stores_and_shows_it_once(env):
    from ttp import correction, rulecite, rulematch as rm
    p = _rm_project(env)
    (p.root / "many.py").write_text("".join(f"WORKER_CAP_{i} = {i}\n" for i in range(9)))
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (time.time(), "in", "user", "the report worker cap is wrong, an hour is too long", 1))
    row = p.db.one("SELECT * FROM messages WHERE id=?", (mid,))
    correction.flag(p.db, p.config(), [row])
    rec = p.db.kv(rm.KV + str(mid))
    assert rec["msg"] == mid and 0 < len(rec["items"]) <= rm.MAX
    lines = rulecite.digest_lines(p.db)
    assert lines and all(l.startswith(f"- user msg {mid} may retire: ") for l in lines)
    assert len(lines) <= rm.MAX
    correction.flag(p.db, p.config(), [row])   # one record per message
    assert p.db.kv(rm.KV + str(mid)) == rec
    assert rm.digest_lines(p.db, time.time() + rm.OPEN_S + 60) == []


def _rm_flag(p, text="the report worker cap is wrong, an hour is too long"):
    from ttp import correction
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (time.time() - 3600, "in", "user", text, 1))
    correction.flag(p.db, p.config(), [p.db.one("SELECT * FROM messages WHERE id=?", (mid,))])
    return mid


def _rm_retire(p):
    import subprocess
    for e in p._memory_entries():
        p.forget_memory(e["name"])
    p.charter_path.write_text("# Charter\n\n## Policies\n- Nothing about the worker.\n")
    (p.root / "keep.py").write_text("LIMIT = 3\n")
    for cmd in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d"]):
        subprocess.run(["git", *cmd], cwd=p.root, check=True, capture_output=True)


def test_rule_gone_lists_where_the_old_value_still_stands_in_any_unit(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    left = rm.remaining(p, old="an hour", name="KEEPALIVE_S")
    assert {"memory", "charter", "code"} <= {x.split()[0].rstrip(":") for x in left}
    assert any("KEEPALIVE_S" in x for x in left)
    assert not [x for x in rm.remaining(p, old="3600 s", name="OTHER_CAP") if x.startswith(("code", "env", "config"))]
    assert any("KEEPALIVE_S" in x for x in rm.remaining(p, old="60 min", name="KEEPALIVE_S"))
    (p.root / "run.env").write_text("KEEPALIVE_MIN=60\nQUIET=1\n")
    import subprocess
    subprocess.run(["git", "add", "run.env"], cwd=p.root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "e"], cwd=p.root, check=True)
    assert any(x.startswith("env KEEPALIVE_MIN") for x in rm.remaining(p, old="1 hour", name="KEEPALIVE_MIN"))


def test_rule_gone_is_gone_once_the_value_is_removed_and_marks_the_message(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    mid = _rm_flag(p)
    assert rm.gone(p, p.db, msg=mid) and p.db.kv(rm.KV + str(mid) + rm.GONE) is None
    _rm_retire(p)
    assert rm.gone(p, p.db, msg=mid) == []
    assert p.db.kv(rm.KV + str(mid) + rm.GONE)["ts"] > 0
    assert rm.remaining(p, old="3600 s") == []


def test_rule_gone_digest_closes_the_line_and_reads_overdue_after_a_day(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    mid = _rm_flag(p)
    now = time.time()
    assert rm.digest_lines(p.db, now) and all("may retire" in l for l in rm.digest_lines(p.db, now))
    late = rm.digest_lines(p.db, now + rm.OVERDUE_S + 60)
    assert len(late) == 1 and late[0].startswith(f"- overdue: user msg {mid} rule still at ")
    assert "charter" in late[0]
    _rm_retire(p)
    rm.gone(p, p.db, msg=mid)
    assert rm.digest_lines(p.db, now) == [] and rm.digest_lines(p.db, now + rm.OVERDUE_S + 60) == []


def test_rule_gone_daily_line_counts_retired_rules_and_overdue_ones(env):
    from ttp import rulematch as rm, unblock
    p = _rm_project(env)
    a, b = _rm_flag(p), _rm_flag(p, "the report worker keepalive cap is too long at an hour")
    _rm_retire(p)
    rm.gone(p, p.db, msg=a)
    now = time.time()
    assert rm.daily_line(p.db, now) == "Rules retired today: 1 (median message-to-gone 1.0 h); overdue: 0"
    p.db.x("UPDATE kv SET ts=? WHERE key=?", (now - 2 * 86400 + 3600, rm.KV + str(b)))   # a day and more old, never gone
    assert rm.daily_line(p.db, now).endswith("overdue: 1")
    assert any(l.startswith("Rules retired today:") for l in unblock.lines(p.db, now))


def test_rule_gone_command_exits_by_whether_the_rule_stands(env, monkeypatch, capsys):
    from ttp import cli
    p = _rm_project(env)
    monkeypatch.chdir(p.root)
    with pytest.raises(SystemExit) as e:
        cli.main(["rule-gone", "--old", "60 min", "--name", "KEEPALIVE_S"])
    assert e.value.code == 1 and "KEEPALIVE_S" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["rule-gone", "--old", "7 min"])
    assert e.value.code == 0
    assert "gone" in capsys.readouterr().out


def test_rule_gone_unknown_message_is_an_error_not_a_gone_mark(env, monkeypatch, capsys):
    from ttp import cli, rulematch as rm
    p = _rm_project(env)
    monkeypatch.chdir(p.root)
    assert rm.gone(p, p.db, msg=9999) is None and p.db.kv(rm.KV + "9999" + rm.GONE) is None
    with pytest.raises(SystemExit) as e:
        cli.main(["rule-gone", "--msg", "9999"])
    assert e.value.code and p.db.kv(rm.KV + "9999" + rm.GONE) is None


def test_rule_gone_digest_checks_an_overdue_message_once_an_hour(env, monkeypatch):
    from ttp import rulematch as rm
    p = _rm_project(env)
    _rm_flag(p)
    late, calls = time.time() + rm.OVERDUE_S + 60, []
    real = rm.gone
    monkeypatch.setattr(rm, "gone", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = rm.digest_lines(p.db, late)
    assert rm.digest_lines(p.db, late + 60) == first and len(calls) == 1
    rm.digest_lines(p.db, late + rm.CHECK_S + 60)
    assert len(calls) == 2


_RM_HELD = "rig04 stays held while lent to the other group"


@pytest.mark.parametrize("body", ["Chip resets on rig04 go to the summary, dated 2026-11-02.",
                                  "rig04 builds keep a cache of 83M per worker.",
                                  "rig04 cache warmups run every 24 h.",
                                  "rig04 cache warmups run every 24 hours.",
                                  "rig04 disk budget is 16 GB per build.",
                                  "rig04 spend tally reached $8000 this quarter.",
                                  "rig04 nightly builds start at 09:18 PST."])
def test_rulematch_a_shared_alias_alone_never_matches_beside_a_number(env, body):
    from ttp import rulematch as rm
    p = make(env)
    p.add_memory(body, kind="decision")
    p.charter_path.write_text(f"# Charter\n\n## Resources\n- {body}\n")
    assert rm.older(p, p.db, _RM_HELD) == []   # the alias is its only shared word: no topic of its own


@pytest.mark.parametrize("ref", ["#812", "run-812", "msg-812"])
def test_rulematch_an_id_is_no_anchor(env, ref):
    from ttp import rulematch as rm
    p = make(env)
    p.add_memory(f"Warmups for {ref} used 100M of scratch space.", kind="decision")
    assert rm.older(p, p.db, f"the warmup of {ref} is too slow, cut it") == []


def test_rulematch_a_shared_alias_or_setting_name_still_flags_and_shows_it(env):
    from ttp import rulematch as rm
    p = make(env)
    p.add_memory("rig04 link drops: reset the board, logged 2026-11-24.", kind="decision")
    p.add_memory("Keep worker.slots at 3 while soak tests run.", kind="decision")
    got = rm.older(p, p.db, "rig04 link resets need a reboot now")
    assert [g["value"] for g in got if g["where"] == "memory"] == ["rig04"], got
    assert rm.remaining(p, items=got)   # the shown term is what rule-gone looks for
    got = rm.older(p, p.db, "lower worker.slots for the soak runs")
    assert [g["value"] for g in got if g["where"] == "memory"] == ["worker.slots"], got
    got = rm.older(p, p.db, "from tomorrow the nightly build should set worker.slots to four on every host")
    assert [g["value"] for g in got if g["where"] == "memory"] == ["worker.slots"], got   # the setting name alone


def test_rulematch_numeric_tokens_are_units_dates_and_ids_only():
    from ttp import rulematch as rm
    assert all(rm._numeric(t) for t in ("2026", "24h", "100m", "16gb", "msg-12", "nov-28", "#12", "$8000", "09:18"))
    assert not any(rm._numeric(t) for t in ("rig04", "worker.slots", "gbh01", "cache", "issue", "note", "t3k", "h100", "v100", "t3k-01"))


def test_rulematch_a_machine_alias_with_a_letter_after_its_digit_still_flags(env):
    from ttp import rulematch as rm
    p = make(env)
    p.add_memory("t3k reboots: use the remote reset first.", kind="decision")
    got = rm.older(p, p.db, "t3k reboots now need a full power cycle; the remote reset leaves stale links and hangs every chip")
    assert [g["value"] for g in got if g["where"] == "memory"], got


def test_rulematch_a_shared_short_setting_name_alone_flags(env):
    from ttp import rulematch as rm
    p = make(env)
    p.add_memory("Keep ci.tag at 3 while soak tests run.", kind="decision")
    got = rm.older(p, p.db, "lower ci.tag for nightly builds")
    assert [g["value"] for g in got if g["where"] == "memory"] == ["ci.tag"], got


def test_rule_gone_rematches_an_old_record_once_and_drops_items_the_matcher_no_longer_finds(env):
    """A record stored by an older matcher (here a numeric-only item, value '2026') is re-matched once before it is
    shown or counted: an item the current matcher no longer finds drops, a still-matched one keeps its line, and a
    record left with no items gives no Course check line and no overdue count."""
    from ttp import rulematch as rm
    p = _rm_project(env)
    p.add_memory("The dashboard budget review is due in 2026.", kind="decision")
    stale = next({"where": "memory", "name": e["name"], "line": e["body"], "value": "2026"}
                 for e in p._memory_entries() if "2026" in e["body"])
    a, b = _rm_flag(p), _rm_flag(p, "the 2026 plan is fine")
    real = p.db.kv(rm.KV + str(a))["items"]
    old = time.time() - 2 * 86400 + 3600
    for mid, items in ((a, [*real, stale]), (b, [stale])):
        p.db.x("UPDATE kv SET value=?, ts=? WHERE key=?", (json.dumps({"msg": mid, "items": items}), old, rm.KV + str(mid)))
    now = time.time()
    assert rm.daily_line(p.db, now).endswith("overdue: 1")
    assert p.db.kv(rm.KV + str(a)) == {"msg": a, "items": real, "matcher": rm.MATCHER}
    assert p.db.kv(rm.KV + str(b))["items"] == []
    assert p.db.one("SELECT ts FROM kv WHERE key=?", (rm.KV + str(a),))["ts"] == pytest.approx(old), "its age is kept"
    lines = rm.digest_lines(p.db, now)
    assert len(lines) == 1 and lines[0].startswith(f"- overdue: user msg {a} ") and "2026" not in lines[0]


def test_rule_gone_rematch_replaces_a_stale_value_with_the_current_matchers_item(env):
    """Stored items whose (where, name) the current matcher still finds are replaced by its items, once each, so a
    value an older matcher stored (here a number, kept twice under one key) does not stay shown."""
    from ttp import rulematch as rm
    p = _rm_project(env)
    p.add_memory("The dashboard budget review is due in 2026.", kind="decision")
    a = _rm_flag(p)
    real = p.db.kv(rm.KV + str(a))["items"]
    stale = [{**i, "value": "60", "line": f"an older line {n}"} for i in real for n in (1, 2)]
    assert real and stale != real
    p.db.x("UPDATE kv SET value=?, ts=? WHERE key=?",
           (json.dumps({"msg": a, "items": stale}), time.time() - 3600, rm.KV + str(a)))
    assert rm.digest_lines(p.db, time.time()) is not None
    assert p.db.kv(rm.KV + str(a))["items"] == real


def test_rule_gone_rematch_redoes_a_record_an_earlier_matcher_already_rematched(env):
    """A record an earlier matcher version stamped (here with a number as its value) is re-matched by the current
    one, and the stale number item disappears."""
    from ttp import rulematch as rm
    p = _rm_project(env)
    p.add_memory("The dashboard budget review is due in 2026.", kind="decision")
    a = _rm_flag(p)
    real = p.db.kv(rm.KV + str(a))["items"]
    stale = [{**i, "value": "60"} for i in real]
    p.db.x("UPDATE kv SET value=?, ts=? WHERE key=?",
           (json.dumps({"msg": a, "items": stale, "matcher": 1}), time.time() - 3600, rm.KV + str(a)))
    rm.digest_lines(p.db, time.time())
    rec = p.db.kv(rm.KV + str(a))
    assert rec["items"] == real and rec["matcher"] == rm.MATCHER
    assert not [i for i in rec["items"] if i["value"] == "60"]


def test_rule_gone_rematch_keeps_a_stored_item_that_stands_nowhere_so_the_rule_is_marked_gone(env):
    """An item whose recorded value is no longer in its place is kept as stored, even when the current matcher finds
    the same place with another value: the rule changed, so `gone` marks it."""
    from ttp import rulematch as rm
    p = _rm_project(env)
    p.add_memory("The dashboard budget review is due in 2026.", kind="decision")
    a = _rm_flag(p)
    real = p.db.kv(rm.KV + str(a))["items"]
    stale = [{**i, "value": "a value no entry holds"} for i in real if i["where"] == "memory"]
    p.db.x("UPDATE kv SET value=?, ts=? WHERE key=?",
           (json.dumps({"msg": a, "items": stale, "matcher": 1}), time.time() - 3600, rm.KV + str(a)))
    assert rm.gone(p, p.db, msg=a) == []
    assert p.db.kv(rm.KV + str(a) + rm.GONE) is not None
