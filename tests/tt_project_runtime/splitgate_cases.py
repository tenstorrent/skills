# Area case file for tests/test_tt_project_runtime.py: the no_split hand-off field (splitgate), read by ponytail.
from __future__ import annotations


def test_a_recorded_no_split_reason_is_read_as_a_why_line_by_ponytail(env):
    """splitgate.reason: the first non-blank string FIELD of the hand-offs, trimmed and cut to 300 chars;
    ponytail.reason_text carries it as a "why this size" line."""
    from ttp import checksettle, ponytail, splitgate
    assert splitgate.FIELD == "no_split" and splitgate.FIELD in checksettle.KEYS
    assert splitgate.reason() == "" and splitgate.reason({}, None, "x", ["y"]) == ""
    for junk in ("", "   ", 5, None, ["x"]):
        assert splitgate.reason({"no_split": junk}) == ""
    assert splitgate.reason({"no_split": " "}, {"no_split": "  one schema change  "}, {"no_split": "later"}) \
        == "one schema change"
    assert len(splitgate.reason({"no_split": "w" * 500})) == 300
    reviewed = [{"spec": "do it", "result": json.dumps({"status": "done", "no_split": "all or nothing"})}]
    text = ponytail.reason_text({"spec": "review"}, {"summary": "ok"}, reviewed)
    assert "why this size: all or nothing" in text
    assert "why this size" not in ponytail.reason_text({"spec": "review"}, {"summary": "ok"},
                                                       [{"spec": "do it", "result": "{}"}])
