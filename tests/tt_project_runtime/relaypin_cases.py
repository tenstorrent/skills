# Relayed-order model pin tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. A relayed order naming a model is compared with the pins on record, and a stop order
# or added scope is an order, no likely correction (ttp.relaypin).
from __future__ import annotations


def _pins(*bodies, models=()):
    from types import SimpleNamespace
    entries = [{"name": f"pin{i}", "kind": "decision", "body": b} for i, b in enumerate(bodies)]
    cfg = {"providers": {"alpha": {"tiers": {f"t{i}": {"model": m} for i, m in enumerate(models)}}}}
    return SimpleNamespace(config=lambda: cfg, _memory_entries=lambda: entries)


def test_a_relayed_order_naming_another_model_than_the_pin_on_record_gets_one_line():
    from ttp import relaypin
    p = _pins("Reviews are pinned to opus-5-5.")
    assert relaypin.pin_line(p, "Run the reviews on sonnet-5-5 from now on.") == (
        "relayed order names sonnet-5-5; pin on record is opus-5-5 (memory [pin0])")
    held = _pins(models=["acme-large-4"])
    assert relaypin.pin_line(held, "Use acme-large-5 for the next batch.") == (
        "relayed order names acme-large-5; pin on record is acme-large-4 (config)")


def test_a_relayed_order_naming_the_pinned_model_or_none_or_with_no_pin_gets_no_line():
    from ttp import relaypin
    p = _pins("Reviews are pinned to opus-5-5.", models=["acme-large-4"])
    assert relaypin.pin_line(p, "Keep the reviews on opus.") == ""         # a family name is part of the pin
    assert relaypin.pin_line(p, "Keep the reviews on opus-5.5.") == ""     # dots and dashes are one spelling
    assert relaypin.pin_line(p, "Use acme-large-4 for the batch.") == ""
    assert relaypin.pin_line(p, "Run the checks again tonight.") == ""
    assert relaypin.pin_line(_pins(), "Run the reviews on sonnet-5-5.") == ""
    assert relaypin.pin_line(p, "Rerun step-4 on box-2 tonight.") == ""    # a 'name-N' word of no pinned family


def test_the_digest_shows_the_pin_line_under_a_relayed_message_only(env):
    from ttp import coordinator as coord
    p = make(env)
    p.add_memory("Reviews are pinned to opus-5-5.", kind="decision")
    relayed = p.db.post("in", "Run the reviews on opus-6 from now on.", chat="c1", provenance="cli-peer",
                        context="Sent by the relayer for the user.")
    direct = p.db.post("in", "Run the reviews on opus-6 from now on.", chat="c2", provenance="slack")
    digest = coord.digest(p, {}, [], [relayed])
    assert "  relayed order names opus-6; pin on record is opus-5-5 (memory [" in digest
    assert "relayed order names" not in coord.digest(p, {}, [], [direct])


def test_a_stop_order_or_added_scope_is_no_correction_but_a_contradiction_still_is(env):
    from ttp import correction
    p = make(env)
    p.db.post("out", "The build passed on every box last night.", chat="c1")
    p.db.x("INSERT INTO events(ts,source,kind,text) VALUES(?,?,?,?)", (time.time(), "daemon", "no_progress", "x"))
    orders = [p.db.post("in", text, chat="c1") for text in (
        "Stop the nightly runs until the queue is empty.",
        "No, stop pushing and also add the log to the report.",
        "Also run the checks on the new host.",
        "> The build passed on every box\nAdd the timing numbers too.")]
    assert correction.flag(p.db, p.config(), _rows(p, *orders)) == {}
    wrong = p.db.post("in", "That's wrong. Two boxes never ran it, stop saying it passed.", chat="c1")
    nope = p.db.post("in", "No, that is not what I asked for.", chat="c1")
    later = p.db.post("in", "Stop the runs. No, the build failed on box 2.", chat="c1")
    assert correction.flag(p.db, p.config(), _rows(p, wrong, nope, later)) == {
        wrong: "open stall: contradicts it", nope: "open stall: contradicts it", later: "open stall: contradicts it"}
