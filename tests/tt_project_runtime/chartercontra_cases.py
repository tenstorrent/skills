# The charter_update contradiction check (coordinator._scoped_exception, _append_conflicts) refuses only an
# edit that permits what another Restrictions item bans, never one that only shares words or names with it,
# for tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers such as make and _turn_said come from there and from coordinator_cases.py.
from __future__ import annotations

_ALIAS_BANS = ("# demo\n\n## Restrictions\n"
               "- Never power cycle a serving host. (user msg #1)\n"
               "- No device test runs on node-7 outside the shared lock; record each hold step with its undo. "
               "(user msg #1)\n"
               "- Never open pull requests. (user msg #1)\n")


def test_a_scoped_exception_naming_an_alias_other_items_share_passes(env):
    """Upstream report: an `excepts` exception naming a machine alias that another item also names was
    refused as wider than that item's ban. Another item naming the alias for another action (device tests
    through the lock) keeps its ban beside the exception: only an item banning the same action on a name in
    the exception's scope makes it wider."""
    p = make(env)
    from ttp import coordinator as coord
    p.charter_path.write_text(_ALIAS_BANS)
    mids, src = _turn_said(p, "you may power cycle node-7 with cycle_test.sh")
    act = {"type": "charter_update", "section": "Restrictions", "excepts": "Never power cycle a serving host.",
           "text": "Workers may power cycle node-7 with `cycle_test.sh`.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    charter = p.charter_path.read_text()
    assert "where this item wins: Workers may power cycle node-7 with `cycle_test.sh`." in charter, charter
    assert "No device test runs on node-7 outside the shared lock" in charter


def test_an_ordered_exception_goes_in_beside_another_ban_on_its_scope_unless_that_ban_guards_people(env):
    """Another item banning the exception's own action (or any action) on a name in its
    scope no longer refuses an ordered exception: the newest ordered word wins, so the same exception goes
    in beside that ban too. A ban that guards other people's machines or jobs (relay.HELD) still refuses."""
    p = make(env)
    from ttp import coordinator as coord
    for k, other in enumerate(("Never power cycle node-7 during a soak.", "Never touch node-7.")):
        charter = _ALIAS_BANS.replace("- Never open", f"- {other} (user msg #1)\n- Never open")
        p.charter_path.write_text(charter)
        mids, src = _turn_said(p, "you may power cycle node-7 with cycle_test.sh")
        err = coord.apply(p, [{"type": "charter_update", "section": "Restrictions",
                               "excepts": "Never power cycle a serving host.",
                               "text": "Workers may power cycle node-7 with `cycle_test.sh`.", "source": src}],
                          turn=1 + k, user_turn=True, messages=mids)
        assert err == [], (other, err)
        got = [ln for ln in p.charter_path.read_text().splitlines() if "cycle_test.sh`." in ln]
        assert len(got) == 2 and any(f"Exception to \"{other}\"" in ln for ln in got), got
        assert f"- {other} (user msg #1)" in p.charter_path.read_text()   # the ban itself is never edited
    other = "Never power cycle node-7 during another user's job."
    charter = _ALIAS_BANS.replace("- Never open", f"- {other} (user msg #1)\n- Never open")
    p.charter_path.write_text(charter)
    mids, src = _turn_said(p, "you may power cycle node-7 with cycle_test.sh")
    err = coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "excepts": "Never power cycle a serving host.",
                           "text": "Workers may power cycle node-7 with `cycle_test.sh`.", "source": src}],
                      turn=9, user_turn=True, messages=mids)
    assert len(err) == 1 and "it is wider than a ban another item keeps" in err[0] and other in err[0], err
    assert p.charter_path.read_text() == charter


_COMMON_WORDS = ("# demo\n\n## Restrictions\n"
                 "- Run each soak test through the shared lock; record every hold step with its undo. "
                 "(user msg #1)\n"
                 "- Never run a test on host-x while it holds a user's job; record why in the hold log. "
                 "(user msg #1)\n"
                 "- Never undo another project's step without its record. (user msg #1)\n"
                 "- Never open pull requests. (user msg #1)\n")
_COMMON_ITEM = "Run each soak test through the shared lock; record every hold step with its undo."


def test_a_quoted_edit_sharing_only_common_words_with_other_items_passes(env):
    """Upstream report: replacing one clause of an item in place (`quote`) was refused for words the quoted
    item already held (test, hold, step, record, undo, through), flagging unrelated items. Only what the
    edit adds is compared with the other items."""
    p = make(env)
    from ttp import coordinator as coord
    text = ("Run each soak test through the shared lock; record every hold step with its undo, except that a "
            "dry run on node-3 may skip the record.")
    p.charter_path.write_text(_COMMON_WORDS)
    assert coord._append_conflicts(_COMMON_WORDS, "Restrictions", text, _COMMON_ITEM, lifts_only=True) == []
    mids, src = _turn_said(p, "a dry run on node-3 may skip the record")
    err = coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": _COMMON_ITEM,
                           "text": text, "source": src}], turn=1, user_turn=True, messages=mids)
    assert err == [], err
    body = p.charter_path.read_text()
    assert "dry run on node-3 may skip the record" in body and "Never run a test on host-x" in body
    assert "Never undo another project's step" in body


def test_a_quoted_edit_that_permits_what_another_item_bans_is_still_refused(env):
    """The edit adds a permit for what another item bans (the same action on the same named host): refused,
    naming that item, with or without the quoted words around it; so is a permit of a banned action on a
    name the quoted item already held."""
    p = make(env)
    from ttp import coordinator as coord
    for k, (quote, text) in enumerate((
            (_COMMON_ITEM, _COMMON_ITEM[:-1] + ", and workers may run a test on host-x while it holds a job."),
            ("Run each soak test on host-x through the shared lock.",
             "Workers may run a test on host-x while it holds a user's job, through the shared lock."))):
        charter = _COMMON_WORDS.replace(_COMMON_ITEM, quote)
        p.charter_path.write_text(charter)
        mids, src = _turn_said(p, "tests may run on host-x while it holds a job")
        err = coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": quote,
                               "text": text, "source": src}], turn=1 + k, user_turn=True, messages=mids)
        assert len(err) == 1 and "contradicts the Restrictions item" in err[0], (text, err)
        assert "\"Never run a test on host-x while it holds a user's job" in err[0], err
        assert "Never undo another project's step" not in err[0], err
        assert p.charter_path.read_text() == charter
_BOX_A_ITEMS = ("# demo\n\n## Restrictions\n- Never open pull requests. (user msg #1)\n"
                "- Whenever box-b is too full, CPU checks may run on box-a, but never device work. (user msg #1)\n"
                "- Exception (a) to 'Never change the power limit of a serving host': box-a may keep its power "
                "limit at 170 W during soaks, never above 170 W. (user msg #1)\n"
                "- Never change the power limit of a serving host. (user msg #1)\n")


def test_charter_gate_power_limit_exception_ignores_items_that_only_name_the_host(env):
    """Upstream note e5f551e13689fbef: an exception lowering box-a's power limit was refused as wider than an
    item allowing CPU checks on box-a and an older power exception there, though neither bans that action.
    Only items restricting the exception's action count; a real ban on power changes on box-a still blocks."""
    p = make(env)
    from ttp import coordinator as coord
    act = {"type": "charter_update", "section": "Restrictions",
           "excepts": "Never change the power limit of a serving host.", "text": "box-a may lower its power limit to 160 W."}
    p.charter_path.write_text(_BOX_A_ITEMS)
    mids, src = _turn_said(p, "change the power limit of box-a from 170 to 160 W")
    assert coord.apply(p, [{**act, "source": src}], turn=1, user_turn=True, messages=mids) == []
    stored = [ln for ln in p.charter_path.read_text().splitlines() if "160 W" in ln]
    assert len(stored) == 1 and "Exception to \"Never change the power limit of a serving host.\"" in stored[0]
    assert "Exception (a)" not in stored[0]     # it narrows the ban itself, not the older exception quoting it
    assert coord._ban_part("- Whenever box-b is too full, CPU checks may run on box-a, but never device work.") \
        .strip() == "never device work"
    # an apostrophe inside a word (user's) stays inside the quote it sits in: no ban word leaks out of it
    assert coord._ban_part("- Exception to 'Never reboot the user's box or never touch its power': box-a may run "
                           "CPU checks, never device work.").strip() == "never device work"
    # a ban on power changes on box-a itself gets the same ordered exception beside it;
    # one guarding another user's job there still blocks
    p.charter_path.write_text(_BOX_A_ITEMS + "- Never change the power limit of box-a. (user msg #1)\n")
    mids, src = _turn_said(p, "change the power limit of box-a from 170 to 160 W")
    assert coord.apply(p, [{**act, "source": src}], turn=2, user_turn=True, messages=mids) == []
    assert "Exception to \"Never change the power limit of box-a.\"" in p.charter_path.read_text()
    p.charter_path.write_text(_BOX_A_ITEMS + "- Never change the power limit of box-a during another user's job. "
                                             "(user msg #1)\n")
    mids, src = _turn_said(p, "change the power limit of box-a from 170 to 160 W")
    err = coord.apply(p, [{**act, "source": src}], turn=3, user_turn=True, messages=mids)
    assert err and "wider than a ban another item keeps" in err[0] and "Never change the power limit of box-a" in err[0]
    assert "160 W" not in p.charter_path.read_text()
