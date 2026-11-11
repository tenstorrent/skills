# Old user orders re-read by intent (runtime/ttp/oldorders.py: the reason and context stored with an order,
# the model-free scan, the digest's 'Re-read by intent' section and its Course check line) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _oo_msg(p, text, ts=None):
    return p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                  (ts or time.time(), "in", "user", text, 1))


def _oo_age(path, days):
    """Backdate memory entry `path` by `days` (its front matter's `created`)."""
    import re as _re
    from ttp.project import _stamp
    path.write_text(_re.sub(r"^created: \S+", f"created: {_stamp(time.time() - days * 86400)}",
                            path.read_text(), count=1, flags=_re.M))


def test_a_memory_order_keeps_its_reason_and_the_users_words_around_it(env):
    from ttp import coordinator as coord
    p = make(env)
    msg = _oo_msg(p, "my laptop is overloaded,\nstop all expensive work except what I asked for")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "preference", "standing": True,
                            "text": f"Run only high-priority work the user asked for (user msg #{msg}).",
                            "reason": "the laptop was overloaded\n---\nsee #12"}]) == []
    e = next(e for e in p._memory_entries() if e["kind"] == "preference")
    assert e["reason"] == "the laptop was overloaded — see #12"   # one front-matter-safe line
    assert e["context"].startswith(f'user msg #{msg} (') and "overloaded, stop all expensive work" in e["context"]
    assert e["body"].startswith("Run only high-priority work")   # the body is untouched
    # An entry stored without one reads back empty, never missing.
    plain = p.add_memory("Builds use the shared cache.", kind="fact")
    got = next(e for e in p._memory_entries() if e["name"] == plain.stem)
    assert got["reason"] == "" and got["context"] == ""


def test_an_order_obligation_keeps_its_reason_and_message_words(env):
    from ttp import obligations
    p = make(env)
    msg = _oo_msg(p, "pause the nightly soak while the rig is in repair")
    obligations.add_order(p, "pause the nightly soak", msg, action_class="change", reason="rig in repair (#7)")
    pr = next(o for o in obligations.items(p.db) if (o.get("promise") or {}).get("order") == msg)["promise"]
    assert pr["reason"] == "rig in repair (#7)" and "pause the nightly soak while" in pr["context"]


def test_old_orders_lists_only_old_user_orders_whose_reason_is_over_or_unknown(env):
    from ttp import oldorders
    p = make(env)
    msg = _oo_msg(p, "stop expensive work for now")
    fix = p.db.add_task("relieve the overloaded host", "s", origin="user")
    unknown = p.add_memory(f"Run only urgent work (user msg #{msg}).", kind="preference", standing=True)
    over = p.add_memory(f"Skip the long soak (user msg #{msg}).", kind="preference",
                        reason=f"host overloaded until #{fix} lands")
    holds = p.add_memory(f"Prefer the small runner (owner's msg #{msg}).", kind="preference",
                         reason="the large runner is shared")   # a reason that still holds
    p.add_memory(f"Never push to main (user msg #{msg}).", kind="restriction")   # only the user loosens one
    p.add_memory("Prefer short specs.", kind="preference")                          # cites no user message
    ended = p.add_memory(f"Hold the deploy (user msg #{msg}).", kind="preference", end={"expires": time.time() + 3 * 86400})
    for f in (unknown, over, holds, ended):
        _oo_age(f, 2)
    young = p.add_memory(f"Batch small fixes (user msg #{msg}).", kind="preference")   # under a day old
    keys = [i["key"] for i in oldorders.scan(p)]
    assert unknown.stem in keys and over.stem not in keys and holds.stem not in keys and young.stem not in keys
    assert ended.stem not in keys and not any(k.startswith("restriction-") for k in keys)
    p.db.update_task(fix, status="done")   # its reason ended: listed as over
    items = {i["key"]: i for i in oldorders.scan(p)}
    assert items[over.stem]["state"] == f"over (#{fix}: done)" and items[unknown.stem]["state"] == "unknown"
    assert items[unknown.stem]["msgs"] == [msg] and items[unknown.stem]["held"] == []


def test_old_orders_names_the_holds_that_cite_them_and_open_orders_only_when_held(env):
    from ttp import obligations, oldorders
    p = make(env)
    msg = _oo_msg(p, "stop expensive work for now")
    rule = p.add_memory(f"Run only urgent work (user msg #{msg}).", kind="preference", standing=True)
    _oo_age(rule, 3)
    blocked = p.db.add_task("big soak", "s", origin="user")
    p.db.update_task(blocked, status="blocked", blocked_reason=f"held by [{rule.stem}]")
    other = p.db.add_task("other", "s", origin="user")
    p.db.update_task(other, status="blocked", blocked_reason="needs #1")
    ask = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)",
                 (time.time(), "out", "ask", f"May I run the soak? user msg #{msg} said to stop", 0))
    item = next(i for i in oldorders.scan(p) if i["key"] == rule.stem)
    assert item["held"] == [f"blocked #{blocked}", f"ask #{ask}"]
    # An open order no hold cites is ongoing work, not listed; one a hold cites is.
    omsg = _oo_msg(p, "keep the queue drained")
    obligations.add_order(p, "keep the queue drained", omsg, action_class="change", now=time.time() - 3 * 86400)
    assert not any(i["what"].startswith("order ") for i in oldorders.scan(p))
    oid = obligations.open_order(p.db, omsg)["id"]
    p.db.update_task(other, blocked_reason=f"waits on {oid}")
    got = next(i for i in oldorders.scan(p) if i["key"] == oid)
    assert got["held"] == [f"blocked #{other}"] and got["state"] == "unknown"


def test_the_re_read_section_shows_daily_and_on_a_blocker_sweep(env):
    from ttp import oldorders, sweep
    p = make(env)
    msg = _oo_msg(p, "stop expensive work while my laptop is overloaded")
    rule = p.add_memory(f"Run only urgent work (user msg #{msg}).", kind="preference", standing=True,
                        context=f'user msg #{msg}: "stop expensive work while my laptop is overloaded"')
    _oo_age(rule, 4)
    now = time.time()
    lines = oldorders.digest_lines(p, [], now)
    assert lines[0] == oldorders.HEADER and len(lines) == 2
    assert f"memory [{rule.stem}] (4d old): Run only urgent work" in lines[1]
    assert "reason unknown" in lines[1] and "while my laptop is overloaded" in lines[1]
    assert oldorders.digest_lines(p, [], now + 60) == []                       # once a day
    assert oldorders.digest_lines(p, [], now + oldorders.RECHECK_S + 1)[1].replace("(5d old)", "(4d old)") == lines[1]
    # A blocker sweep lists the held ones at once.
    t = p.db.add_task("soak", "s", origin="user")
    p.db.update_task(t, status="blocked", blocked_reason=f"user msg #{msg}: only urgent work")
    ev = p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                (now, "daemon", sweep.SWEEP_KIND, "sw", "normal", "x", "new"))
    assert oldorders.digest_lines(p, [], now + oldorders.RECHECK_S + 120) == []
    swept = oldorders.digest_lines(p, [ev], now + oldorders.RECHECK_S + 180)
    assert len(swept) == 2 and swept[1].endswith(f"cited by blocked #{t}")


def test_the_coordinator_digest_carries_the_re_read_section(env):
    from ttp import coordinator as coord, oldorders
    p = make(env)
    msg = _oo_msg(p, "only urgent work for now")
    _oo_age(p.add_memory(f"Run only urgent work (user msg #{msg}).", kind="preference", standing=True), 2)
    assert "## Re-read by intent" in coord.digest(p, {}, [], [])
    assert oldorders.scan(p) and "## Re-read by intent" not in coord.digest(p, {}, [], [])   # listed today


def test_course_check_flags_a_hold_that_cites_an_old_order(env):
    from ttp import coursecheck, oldorders, push
    p = make(env)
    msg = _oo_msg(p, "stop expensive work for now")
    rule = p.add_memory(f"Run only urgent work (user msg #{msg}).", kind="preference", standing=True)
    _oo_age(rule, 2)
    t = p.db.add_task("soak", "s", origin="user")
    p.db.update_task(t, status="blocked", blocked_reason=f"only urgent work ([{rule.stem}])")
    oldorders.scan(p)
    lines, ident = coursecheck.lines(p.db, p.config(), p.state / push.DETACHED)
    assert len(lines) == 1 and lines[0].startswith(f"- old order holds work: blocked #{t} cites memory [{rule.stem}]")
    assert f"(user msg #{msg}, 4" in lines[0].replace(", 47 h", ", 4").replace(", 48 h", ", 4") and "reason unknown" in lines[0] and "memory_forget" in lines[0]
    assert ident == f"old_order:{rule.stem}:blocked #{t}"
    p.set_config("coordinator.course_check", {"old_orders": 0})   # turned off
    assert coursecheck.lines(p.db, p.config(), p.state / push.DETACHED)[0] == []
