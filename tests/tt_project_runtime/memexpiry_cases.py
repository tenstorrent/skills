# Rule expiry (runtime/ttp/memexpiry.py, memory.default_expiry_days) of tests/test_tt_project_runtime.py, which
# runs this file in its own namespace (see _include_cases there). Add new memory-expiry tests here.
from __future__ import annotations


def _mx_entry(p, name):
    return next(e for e in p._memory_entries() if e["name"] == name)


def test_memexpiry_a_new_decision_fact_or_preference_gets_the_default_end(env):
    import time
    p = make(env)
    now = time.time()
    for kind in ("decision", "fact", "preference"):
        e = _mx_entry(p, p.add_memory(f"a plain {kind} entry", kind=kind).stem)
        assert e["auto"] and abs(e["end"]["expires"] - (now + 30 * 86400)) < 120, e
    given = _mx_entry(p, p.add_memory("a decision with an end", kind="decision", end={"until": "the next release"}).stem)
    assert not given["auto"] and "expires" not in given["end"], given


def test_memexpiry_restrictions_resources_settled_areas_and_standing_entries_are_exempt(env):
    p = make(env)
    for kind in ("restriction", "resource", "settled_area"):
        assert not _mx_entry(p, p.add_memory(f"a {kind} entry", kind=kind).stem)["end"], kind
    assert not _mx_entry(p, p.add_memory("a standing duty", kind="decision", standing=True).stem)["end"]


def test_memexpiry_the_setting_zero_turns_it_off(env):
    p = make(env)
    p.set_config("memory.default_expiry_days", 0)
    assert not _mx_entry(p, p.add_memory("a decision with no window", kind="decision").stem)["end"]


def test_memexpiry_renew_by_command_gives_a_fresh_window_and_refuses_archived(env, monkeypatch):
    import time
    import pytest
    from ttp import cli
    p = make(env)
    name = p.add_memory("a decision to renew", kind="decision").stem
    monkeypatch.chdir(p.root)
    cli.main(["memory", p.name, "--renew", name, "--days", "90"])
    assert abs(_mx_entry(p, name)["end"]["expires"] - (time.time() + 90 * 86400)) < 120
    p.forget_memory(name)
    with pytest.raises(SystemExit):
        cli.main(["memory", p.name, "--renew", name])


def test_memexpiry_a_cite_by_name_renews_an_auto_end_past_half_its_window(env):
    import time
    from ttp import memexpiry
    p = make(env)
    name = p.add_memory("a decision a hold cites", kind="decision").stem
    now = time.time()
    memexpiry.cite(p, [{"why": f"held by {name}"}], now)   # most of the window left: untouched
    assert _mx_entry(p, name)["end"]["expires"] < now + 31 * 86400
    memexpiry._rewrite(p, name, now + 5 * 86400, True)
    memexpiry.cite(p, [{"why": f"held by {name}"}], now)
    assert abs(_mx_entry(p, name)["end"]["expires"] - (now + 30 * 86400)) < 120
    other = p.add_memory("a decision nobody cites", kind="decision").stem
    memexpiry._rewrite(p, other, now + 5 * 86400, True)
    memexpiry.cite(p, [{"why": "held by something else"}], now)
    assert _mx_entry(p, other)["end"]["expires"] < now + 6 * 86400


def test_memexpiry_a_cite_never_extends_an_end_the_author_gave(env):
    import time
    from ttp import memexpiry
    p = make(env)
    name = p.add_memory("a decision with its own end", kind="decision", end={"expires": time.time() + 86400}).stem
    memexpiry.cite(p, [{"why": f"held by {name}"}], time.time())
    assert _mx_entry(p, name)["end"]["expires"] < time.time() + 2 * 86400


def test_memexpiry_a_merged_entry_that_replaces_an_auto_one_starts_a_fresh_window(env):
    import time
    from ttp import memexpiry
    p = make(env)
    old = p.add_memory("the old decision", kind="decision").stem
    memexpiry._rewrite(p, old, time.time() + 2 * 86400, True)
    added, _ = p.supersede_memory("the merged decision", [old], kind="decision")
    assert _mx_entry(p, added.stem)["end"]["expires"] > time.time() + 29 * 86400


def test_memexpiry_backfill_gives_old_entries_an_end_with_a_one_week_floor(env):
    import time
    from ttp import ends, memexpiry
    p = make(env)
    p.set_config("memory.default_expiry_days", 0)
    old = p.add_memory("an old decision", kind="decision").stem
    fresh = p.add_memory("a recent decision", kind="decision").stem
    path = p.memory_dir / f"{old}.md"
    path.write_text(path.read_text().replace(_mx_entry(p, old)["created"], ends.stamp(time.time() - 60 * 86400), 1))
    p.set_config("memory.default_expiry_days", 30)
    assert sorted(memexpiry.backfill(p)) == sorted([old, fresh])
    now = time.time()
    assert abs(_mx_entry(p, old)["end"]["expires"] - (now + 7 * 86400)) < 120   # created + 30 d is past: the floor
    assert abs(_mx_entry(p, fresh)["end"]["expires"] - (now + 30 * 86400)) < 120
    assert memexpiry.backfill(p) == []


def test_memexpiry_backfill_leaves_an_entry_written_permanent(env):
    from ttp import memexpiry
    p = make(env)
    kept = p.add_memory("a rule that stays for good", kind="decision", default_end=False).stem
    p.set_config("memory.default_expiry_days", 0)
    other = p.add_memory("an entry from before the default", kind="decision").stem
    p.set_config("memory.default_expiry_days", 30)
    assert memexpiry.backfill(p) == [other] and _mx_entry(p, kept)["end"] == {}


def test_memexpiry_the_daemon_archives_an_entry_at_its_expiry(env):
    import time
    from ttp import ends, memexpiry
    p = make(env)
    name = p.add_memory("a decision that lapses", kind="decision").stem
    keep = p.add_memory("a decision that stays", kind="decision").stem
    memexpiry._rewrite(p, name, time.time() + 60, True)
    assert ends.Ends(p).tick(time.time() + 120)
    assert (p.memory_dir / "archive" / f"{name}.md").exists() and not (p.memory_dir / f"{name}.md").exists()
    assert (p.memory_dir / f"{keep}.md").exists()


def test_memexpiry_the_digest_lists_expiring_entries_and_reviews_unused_standing_ones_daily(env):
    import time
    from ttp import memexpiry
    p = make(env)
    soon = p.add_memory("a decision about to lapse", kind="decision").stem
    duty = p.add_memory("a standing duty", kind="decision", standing=True).stem
    now = time.time()
    memexpiry._rewrite(p, soon, now + 2 * 86400, True)
    lines = memexpiry.digest_lines(p, now)
    assert f"- expiring: {soon} (last cited never)" in lines and not any(duty in x for x in lines)
    later = memexpiry.digest_lines(p, now + 31 * 86400)
    assert f"- review: {duty} unused 30 d" in later
    assert not [x for x in memexpiry.digest_lines(p, now + 31 * 86400 + 60) if x.startswith("- review:")]   # once a day
    memexpiry.cite(p, [{"why": f"per {duty}"}], now + 31 * 86400 + 90)
    assert not [x for x in memexpiry.digest_lines(p, now + 55 * 86400) if duty in x and "review" in x]


def test_memexpiry_backfill_of_many_entries_makes_one_harness_commit(env, monkeypatch):
    from ttp import memexpiry
    p = make(env)
    p.set_config("memory.default_expiry_days", 0)
    names = [p.add_memory(f"an entry from before the default {i}", kind="decision").stem for i in range(5)]
    p.set_config("memory.default_expiry_days", 30)
    commits = []
    monkeypatch.setattr(type(p), "commit_harness", lambda self, paths, msg, removed=(): commits.append(list(paths)))
    assert sorted(memexpiry.backfill(p)) == sorted(names)
    assert len(commits) == 1 and sorted(x.stem for x in commits[0]) == sorted(names)
    assert all(_mx_entry(p, n)["end"].get("expires") for n in names)


def test_memexpiry_backfill_commits_the_entries_it_rewrote_when_a_later_one_fails(env, monkeypatch):
    from ttp import memexpiry
    p = make(env)
    p.set_config("memory.default_expiry_days", 0)
    for i in range(3):
        p.add_memory(f"an entry from before the default {i}", kind="decision")
    p.set_config("memory.default_expiry_days", 30)
    commits, real, calls = [], memexpiry._write_end, []

    def flaky(*a):
        calls.append(1)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(*a)
    monkeypatch.setattr(memexpiry, "_write_end", flaky)
    monkeypatch.setattr(type(p), "commit_harness", lambda self, paths, msg, removed=(): commits.append(list(paths)))
    with pytest.raises(OSError):
        memexpiry.backfill(p)
    assert len(commits) == 1 and len(commits[0]) == 2


def test_memexpiry_uses_drops_ids_of_rules_and_entries_that_no_longer_exist(env):
    import time
    from ttp import memexpiry
    p = make(env)
    name = p.add_memory("a decision still in force", kind="decision").stem
    now = time.time()
    p.db.set_kv(memexpiry.KEY, {"R-dead": {"seen": 1, "cited": 0}, "gone-entry": {"seen": 1, "cited": 0},
                                name: {"seen": 5, "cited": 6}})
    uses = memexpiry._uses(p, now)
    assert "R-dead" not in uses and "gone-entry" not in uses and uses[name] == {"seen": 5, "cited": 6}
    assert not [x for x in memexpiry.digest_lines(p, now + 40 * 86400) if "R-dead" in x]
    assert "R-dead" not in p.db.kv(memexpiry.KEY)


def test_memexpiry_uses_keeps_rule_times_while_the_charter_cannot_be_read(env):
    import time
    from ttp import memexpiry
    p = make(env)
    now = time.time()
    p.db.set_kv(memexpiry.KEY, {"R-ab12": {"seen": 5, "cited": 6}, "P-cd34": {"seen": 7, "cited": 0}})
    saved = p.charter_path.read_text()
    p.charter_path.unlink()
    assert memexpiry._uses(p, now) == {"R-ab12": {"seen": 5, "cited": 6}, "P-cd34": {"seen": 7, "cited": 0}}
    memexpiry.digest_lines(p, now)
    assert p.db.kv(memexpiry.KEY)["R-ab12"] == {"seen": 5, "cited": 6}
    p.charter_path.write_text(saved)
    assert "R-ab12" not in memexpiry._uses(p, now)
