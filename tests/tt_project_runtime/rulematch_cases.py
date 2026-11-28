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


def test_rulematch_lists_the_older_rules_on_the_topic_and_marks_restrictions(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    got = rm.older(p, p.db, "an hour is too long for the report worker, make it 5 min", 9)
    where = {(g["where"], g["name"]) for g in got}
    assert any(w == "memory" for w, _ in where) and ("code", "KEEPALIVE_S") in where
    assert ("code", "LIMIT") not in where and len(got) <= rm.MAX
    charter = [g for g in got if g["where"] == "charter"]
    assert any("user-only: change only by the user's word" in g["line"] for g in charter), got
    assert any(g["where"] == "charter" and "Restrictions" in g["name"] for g in got)
    cfg = rm.older(p, p.db, "the worker isolation setting is wrong", 10)
    assert ("config", "providers.claude.worker_isolation") in {(g["where"], g["name"]) for g in cfg}


def test_rulematch_finds_nothing_for_unrelated_text(env):
    from ttp import rulematch as rm
    p = _rm_project(env)
    assert rm.older(p, p.db, "thanks, looks great", 11) == []
    assert rm.older(p, p.db, "please rename the colour palette of the dashboard", 12) == []


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
