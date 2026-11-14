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


def test_a_scoped_exception_is_still_refused_where_another_item_bans_the_same_action_on_its_scope(env):
    """A real contradiction stays refused: another item bans the exception's own action (power cycling, or
    any action at all) on a name in its scope."""
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
        assert len(err) == 1 and "it is wider than a ban another item keeps" in err[0], (other, err)
        assert f"\"{other}\"" in err[0] and "node-7" in err[0] and "device test" not in err[0], err
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
