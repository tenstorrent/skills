# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# Test cases run by tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and
# imports come from that file. Machine tag notes (machines.set_tag_note) and watchdup's episode counting.
from __future__ import annotations


def test_a_tag_note_is_set_listed_shown_once_and_removed(env, capsys):
    from ttp import machines as mm
    from ttp.cli import main
    main(["machines", "add", "box-a", "--tags", "kindx,device", "--no-ssh-check"])
    main(["machines", "add", "box-b", "--tags", "kindx", "--no-ssh-check"])
    main(["machines", "add", "box-c", "--tags", "other", "--no-ssh-check"])
    capsys.readouterr()
    main(["machines", "tag-note", "KindX", "  every kindx box has  one widget\nand two gears "])
    assert "saved every machine tagged kindx: every kindx box has one widget and two gears" in capsys.readouterr().out
    main(["machines", "tag-note", "unused", "a tag no listed machine has"])
    # Machines and their notes survive each other's saves.
    mm.add("box-a", note="rack one")
    assert mm.tag_notes()["kindx"]["note"] == "every kindx box has one widget and two gears"
    assert mm.load()["box-a"]["note"] == "rack one"
    capsys.readouterr()
    main(["machines", "list"])
    out = capsys.readouterr().out
    assert "every machine tagged kindx: every kindx box has one widget and two gears" in out
    main(["machines", "tag-note", "kindx"])
    assert capsys.readouterr().out.startswith("every machine tagged kindx:")
    # The coordinator's Machines section shows it once, above the machines, and not the unused tag's.
    lines = mm.list_lines()
    hits = [i for i, x in enumerate(lines) if "every machine tagged kindx" in x]
    assert len(hits) == 1 and hits[0] < min(i for i, x in enumerate(lines) if x.startswith("- box-"))
    assert not any("unused" in x for x in lines)
    # Removed: gone from every view, kept on file as an empty note so the removal travels.
    main(["machines", "tag-note", "kindx", ""])
    assert "kindx" not in mm.tag_notes() and mm.tag_notes(include_removed=True)["kindx"]["note"] == ""
    assert not any("tagged kindx" in x for x in mm.list_lines())


def test_a_tag_note_is_refused_when_too_long_secret_or_a_bad_tag(env, monkeypatch):
    from ttp import machines as mm, redact
    with pytest.raises(ValueError, match="at most 1500"):
        mm.set_tag_note("kindx", "x" * (mm.TAG_NOTE_CHARS + 1))
    mm.set_tag_note("kindx", "x" * mm.TAG_NOTE_CHARS)
    for bad in ("", "a b", "-x", "a/b"):
        with pytest.raises(ValueError):
            mm.set_tag_note(bad, "text")
    monkeypatch.setattr(redact, "scrub", lambda t: t.replace("hunter2", "***"))
    with pytest.raises(ValueError, match="secret"):
        mm.set_tag_note("kindx", "the password is hunter2")
    assert mm.tag_notes()["kindx"]["note"] == "x" * mm.TAG_NOTE_CHARS


def test_a_tag_note_past_its_until_leaves_the_prompts_and_is_marked_in_the_list(env):
    from ttp import machines as mm
    mm.add("box-a", tags="kindx")
    mm.set_tag_note("kindx", "temporary truth", until="1d")
    assert any("temporary truth (until" in x for x in mm.list_lines())
    later = time.time() + 2 * 86400
    assert mm.task_tag_notes(["box-a"], now=later) == []
    assert "may no longer hold" in mm.tag_note_line("kindx", mm.tag_notes()["kindx"], now=later)


def test_tag_notes_merge_tag_by_tag_newest_wins_and_push_carries_them(env):
    from ttp import machines as mm
    mine = {"machines": {}, "tag_notes": {"a": {"note": "mine", "updated": 10}, "b": {"note": "", "updated": 9},
                                          "c": {"note": "tie mine", "updated": 5}}}
    theirs = {"machines": {}, "tag_notes": {"a": {"note": "old", "updated": 3}, "b": {"note": "theirs", "updated": 4},
                                            "c": {"note": "tie theirs", "updated": 5}, "d": {"note": "only", "updated": 1}}}
    got = mm.merge(mine, theirs)["tag_notes"]
    assert {t: n["note"] for t, n in got.items()} == {"a": "mine", "b": "", "c": "tie mine", "d": "only"}
    assert "tag_notes" not in mm.merge({}, {})
    # A list with only tag notes is still copied.
    mm.set_tag_note("kindx", "text")
    assert mm._doc()["tag_notes"]["kindx"]["note"] == "text"


def test_a_worker_whose_task_names_a_tagged_machine_sees_its_tag_note(env):
    from ttp import machines as mm
    from ttp.prompts import worker_task
    p = make(env)
    mm.add("box-a", tags="kindx", shared="box-a-dev")
    mm.add("box-b", tags="other")
    mm.set_tag_note("kindx", "every kindx box has one widget")
    mm.set_tag_note("other", "other boxes have gears")
    on = p.db.add_task("on box", "spec", kind="work", labels=["resource:box-a-dev"])
    off = p.db.add_task("no box", "spec", kind="work")
    text = worker_task(p, p.db.task(on), str(p.root), None)
    assert "box-a: every machine tagged kindx: every kindx box has one widget" in text
    assert "gears" not in text
    assert "every machine tagged" not in worker_task(p, p.db.task(off), str(p.root), None)
    assert mm.task_tag_notes(["box-b"]) == ["- box-b: every machine tagged other: other boxes have gears"]


def test_watcher_real_episodes_of_one_stable_key_are_not_churn(env, monkeypatch):
    from ttp import watchdup
    run, issues, p = _json_watcher(env, monkeypatch)
    log = lambda: (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    # Five real occurrences in a day: each episode clears before the next one starts.
    for n in range(1, 6):
        run("real", {"text": "box-a: power cycled", "severity": "high", "key": "cycle", "episode": f"e{n}"})
        run("real", {"text": "box-a: cleared: power cycled", "severity": "high", "key": "cycle"})
    assert len(p.db.q("SELECT id FROM issues WHERE source='watcher:real'")) == 5
    assert watchdup.counts(p.db, "watcher:real", time.time()) == [("box-a", 1)]
    assert "noisy watcher:" not in log() and "watcher:real opened" not in log()
    # A new episode on every run while the condition never clears still warns ...
    for n in range(1, 6):
        run("churn", {"text": "box-a: job failed", "severity": "high", "key": "job", "episode": f"run{n}"})
    assert watchdup.counts(p.db, "watcher:churn", time.time()) == [("box-a", 5)]
    assert "watcher:churn opened 4 issues on subject 'box-a'" in log()   # warned once, past the threshold
    assert watchdup.CHURN_FIX in log() and "start a new episode only once it cleared" in watchdup.CHURN_FIX
    # ... and so does one key per value.
    for n in range(1, 6):
        run("perval", {"text": f"box-a: queue {n} deep", "severity": "high", "key": f"queue:{n}"})
    assert watchdup.counts(p.db, "watcher:perval", time.time()) == [("box-a", 5)]
