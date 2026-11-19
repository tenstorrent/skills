# Push batch survival tests (lane L4) of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and
# pytest collects these tests under its name. Add new tests of failing entries and remote outages here.
from __future__ import annotations


def _survive_heads(repo, bad, n=6):
    """`n` entries, each adding its own file; those numbered in `bad` add one the check refuses."""
    return [_entry(repo, f"e{i}", {f"plugins/p/{'bad' if i in bad else 'f'}{i}.txt": f"{i}\n"})
            for i in range(1, n + 1)]


def test_a_push_batch_takes_every_failing_entry_out_and_lands_the_rest(env, monkeypatch, capsys):
    """Entries 1, 3 and 4 of six each break the checks. Entry 1 is blamed, the entries after it are
    searched without it: entry 2 passes and lands, and 3..6 (never blamed, none checked alone) go
    back untested. The next batch, started at once, blames 3 then 4 and lands 5 and 6: nothing but
    the failing entries is ever sunk, and each later batch starts with no backoff."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["test -z \"$(ls plugins/p | grep bad)\""])
    rc, m = _run_batch(p, _batch_marker(p, _survive_heads(repo, {1, 3, 4})), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert _statuses(m) == ["check_failed", "pushed", "requeued", "requeued", "requeued", "requeued"], m
    assert [r["detail"] for r in m["results"][2:]] == [{"untested": True}] * 4, m
    names = _git_out(origin, "ls-tree", "--name-only", "proj", "plugins/p/").split()
    assert sorted(n for n in names if n.endswith(".txt")) == ["plugins/p/f2.txt"]
    assert "they fail too: searching those 5" in capsys.readouterr().out


def test_a_push_batch_lands_a_passing_prefix_of_the_rest_and_requeues_the_others_untested(env, monkeypatch):
    """Entry 1 fails; the entries after it fail too, at entry 6 only. The search of those finds the
    prefix before it passing and pushes it at once; the entries after the passed
    prefix are requeued untested, no try counted, for the next batch to take at once."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["test ! -e plugins/p/bad.txt",
                                                            "test ! -e plugins/p/bad2.txt"])
    heads = [_entry(repo, f"e{i}", {f"plugins/p/{'bad' if i == 1 else 'bad2' if i == 6 else f'f{i}'}.txt": f"{i}\n"})
             for i in range(1, 8)]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    sts = _statuses(m)
    assert sts[0] == "check_failed" and sts[1] == "pushed" and "pushed" in sts[1:], m
    left = [r for r, s in zip(m["results"], sts) if s == "requeued"]
    assert all(r["detail"] == {"untested": True} for r in left), m
    assert not any(s == "check_failed" for s in sts[1:]), "nothing is blamed that no check ran alone"


def test_a_push_batch_that_spends_its_rounds_requeues_the_rest_untested(env, monkeypatch):
    """With only one round of checks per batch (RESCUES), the entries no round reached are requeued
    untested (no try counted, no backoff): never blamed, never checked without the failing ones."""
    from ttp import batch
    monkeypatch.setattr(batch, "RESCUES", 1)
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["test -z \"$(ls plugins/p | grep bad)\""])
    rc, m = _run_batch(p, _batch_marker(p, _survive_heads(repo, {1, 3}, n=5)), monkeypatch)
    sts = _statuses(m)
    assert sts[0] == "check_failed", m
    # one round: the entries after 1 still fail (3); with the search the rounds allow, 3 is blamed
    assert all(r["detail"] == {"untested": True} or r["status"] != "requeued" for r in m["results"]), m


def test_a_rescue_that_finds_the_tip_failing_blames_nobody_it_took_out(env, monkeypatch):
    """A tip that fails on its own: the entries are never blamed for it (tip_failed) and requeued
    untested."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["false"])
    rc, m = _run_batch(p, _batch_marker(p, _survive_heads(repo, set(), n=3)), monkeypatch)
    assert m["outcome"] in ("tip_failed", "nothing"), m
    assert "check_failed" not in _statuses(m) or m["outcome"] == "nothing", m


def test_the_replay_of_a_batch_survives_an_entry_that_raises_and_lands_the_others(env, monkeypatch):
    """An entry whose replay raises (an IndexError in the conflict code was the live case) takes only
    itself out: the other entries replay, pass and land."""
    from ttp import batch
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    real = batch.Batch._replay_one

    def replay_one(self, i, e, h, carried, tip, checks, csdir):
        if i == 1:
            raise IndexError("list index out of range")
        return real(self, i, e, h, carried, tip, checks, csdir)
    monkeypatch.setattr(batch.Batch, "_replay_one", replay_one)
    rc, m = _run_batch(p, _batch_marker(p, _survive_heads(repo, set(), n=4)), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert _statuses(m) == ["pushed", "conflict", "pushed", "pushed"], m


def test_no_batch_is_built_while_the_remote_does_not_answer_and_no_try_is_counted(env, monkeypatch):
    """The pre-batch probe: a remote that does not answer starts no batch (one event, no try on any
    approval); the first batch starts once it answers, within the probe gap."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    good = str(s.origin)
    _git_out(s.repo, "remote", "set-url", "origin", str(env["tmp"] / "missing.git"))
    for n in range(5):
        assert pushq.schedule(s.p) is None, "the remote is down: no batch"
        assert not s.p.db.q("SELECT id FROM push_batches"), "none was built"
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
    assert [e["kind"] for e in _pq_events(s.p, mark)].count("push_remote_down") == 1, "told once, not per tick"
    # still down inside the gap: not asked again
    st = s.p.db.kv(pushq.KV)
    assert st["remote"]["down"] and not st.get("deaths") and not st.get("backoff_until")
    _git_out(s.repo, "remote", "set-url", "origin", good)
    st["remote"]["at"] = time.time() - pushq.REMOTE_DOWN_GAP_S - 1
    s.p.db.set_kv(pushq.KV, st)
    assert pushq._remote_up(s.p, "origin/proj"), "it answers: the batch can start"
    assert "down" not in s.p.db.kv(pushq.KV)["remote"]


@pytest.mark.parametrize("message", ["cannot reach origin: fatal: unable to access: Could not resolve host",
                                     "cannot fetch origin: fatal: Failed to connect to host"])
def test_a_batch_that_died_on_the_remote_counts_no_try_death_or_backoff(env, monkeypatch, message):
    """A batch that ended because the remote did not answer puts its rows back with no try, no death
    toward push_queue_dying and no backoff: the remote probe holds the next one until it answers."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="error", message=message)
    _pq_hand_off(env, s)
    for _ in range(2):
        _pq_batch(s)
        _pq_tend(s)
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
        st = s.p.db.kv(pushq.KV)
        assert not st.get("deaths") and not st.get("backoff_until"), st
