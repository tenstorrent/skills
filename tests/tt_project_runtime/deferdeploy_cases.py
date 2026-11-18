# Deferred deploy tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. An after_push that exits 75 (it kept
# the deploy off a busy box) is deferred and retried by the daemon, never failed. The after_push commands here
# are plain shell in the test folder: no real deploy, setup, upgrade, crontab, systemd or power state.
from __future__ import annotations

_DEFER_LINE = "after_push: deploy deferred: live stream on box-a, retries when it ends"


def _defer_cmd(env):
    """An after_push that defers (exit 75) until the flag file exists, then deploys (exit 0)."""
    flag, seen = env["tmp"] / "box-free", env["tmp"] / "deployed.txt"
    return (f"if [ -f {flag} ]; then echo ran >> {seen}; exit 0; fi; echo '{_DEFER_LINE}'; exit 75"), flag, seen


def _dd_events(p, kind):
    return p.db.q("SELECT severity, status, text FROM events WHERE kind=? ORDER BY id", (kind,))


def _dd_spawner(monkeypatch):
    from ttp import pushq
    spawned = []
    monkeypatch.setattr(pushq, "subprocess", types.SimpleNamespace(DEVNULL=subprocess.DEVNULL,
                                                                   Popen=lambda argv, **k: spawned.append(argv)))
    return spawned


def test_an_after_push_that_exits_75_is_deferred_at_normal_severity_and_retried_until_it_deploys(env, monkeypatch):
    from ttp import batch, push, pushq
    cmd, flag, seen = _defer_cmd(env)
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [cmd])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "a deferred deploy does not fail the push"
    (r,) = _receipts(p)
    assert r["status"] == "deferred" and r["after_push"]["status"] == "deferred" and r["after_push"]["exit"] == 75
    assert r["after_push"]["reason"] == "deploy deferred: live stream on box-a, retries when it ends"
    spawned, alerts = _dd_spawner(monkeypatch), []
    now = time.time()
    for _ in range(2):
        assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a), now=now) == []
    told = _dd_events(p, "after_push_deferred")
    assert len(told) == 1 and told[0]["severity"] == "normal" and "live stream on box-a" in told[0]["text"], told
    assert not alerts and not spawned, "not retried before its backoff"
    assert not _dd_events(p, "after_push_failed") and not p.db.q("SELECT id FROM events WHERE severity='high'")
    assert not p.db.kv(pushq.AFTER_PUSH_OFF)
    pushq.tend_receipts(p, lambda *a, **k: alerts.append(a), now=now + 301)
    (r,) = _receipts(p)
    assert len(spawned) == 1 and r["status"] == "rerun" and not r.get("tries"), "a retry spends no try"
    # The box is still busy: the retry defers again, still one event for the episode.
    receipt = p.state / "after_push" / f"{r['id']}.json"
    assert batch.rerun_receipt(p, receipt) == 0
    (r,) = _receipts(p)
    assert r["status"] == "deferred" and r["deferred_since"] <= now
    pushq.tend_receipts(p, lambda *a, **k: alerts.append(a), now=r["after_push"]["ended"] + 1)
    assert len(_dd_events(p, "after_push_deferred")) == 1 and len(spawned) == 1
    # The box is free: the next retry deploys and closes the episode with one low event.
    flag.write_text("")
    later = json.loads(receipt.read_text())["after_push"]["ended"] + 301
    pushq.tend_receipts(p, lambda *a, **k: alerts.append(a), now=later)
    assert len(spawned) == 2
    assert batch.rerun_receipt(p, receipt) == 0
    (r,) = _receipts(p)
    assert r["status"] == "finished" and r["after_push"]["status"] == "ok" and seen.read_text() == "ran\n"
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a), now=later + 1) == [r["id"]] and not alerts
    ok = _dd_events(p, "after_push_ok")
    assert len(ok) == 1 and ok[0]["severity"] == "low" and ok[0]["text"].startswith("deploy done after deferral"), ok
    assert not _dd_events(p, "after_push_failed") and not _dd_events(p, "after_push_resumed")


def _dd_receipt(p, rid, started, status="deferred", target="origin/proj", **more):
    folder = p.state / "after_push"
    folder.mkdir(parents=True, exist_ok=True)
    m = {"v": 1, "id": rid, "status": status, "outcome": "pushed", "pushed_sha": rid[-1] * 40, "target": target,
         "started": started, "deferred_since": started + 5,
         "after_push": {"status": "deferred", "exit": 75, "ended": started + 5,
                        "reason": "deploy deferred: live stream on box-a, retries when it ends"}, **more}
    (folder / f"{rid}.json").write_text(json.dumps(m))
    return folder / f"{rid}.json"


def test_a_newer_push_of_the_same_target_supersedes_a_pending_deferred_retry(env, monkeypatch):
    from ttp import pushq
    p = make(env)
    p.set_config("delivery.after_push", ["true"])
    t = time.time() - 600
    old = _dd_receipt(p, "r1", t)
    _dd_receipt(p, "r2", t + 60, status="finished", reported=t + 70,
                after_push={"status": "ok", "exit": 0, "started": t + 60, "ended": t + 65})
    spawned = _dd_spawner(monkeypatch)
    assert pushq.tend_receipts(p, now=time.time()) == ["r1"] and not spawned, "replaced, never run again"
    m = json.loads(old.read_text())
    assert m["status"] == "finished" and m["after_push"]["status"] == "superseded" and m["reported"]
    (ev,) = _dd_events(p, "after_push_superseded")
    assert ev["severity"] == "low"
    assert not _dd_events(p, "after_push_failed")


def test_a_pending_deferred_retry_survives_a_daemon_restart_and_a_long_deferral_tells_the_coordinator_once(env, monkeypatch):
    from ttp import pushq
    from ttp.project import Project
    p = make(env)
    p.set_config("delivery.after_push", ["true"])
    t = time.time()
    receipt = _dd_receipt(p, "r1", t - 25 * 3600, deferred_said=t - 25 * 3600)
    spawned = _dd_spawner(monkeypatch)
    fresh = Project(p.base)     # a restarted daemon: only what is on disk
    pushq.tend_receipts(fresh, now=t)
    m = json.loads(receipt.read_text())
    assert len(spawned) == 1 and m["status"] == "rerun" and not m.get("tries")
    assert not _dd_events(fresh, "after_push_deferred"), "the episode was already told before the restart"
    (long_,) = _dd_events(fresh, "after_push_deferred_long")
    assert long_["severity"] == "normal" and long_["status"] == "queued"
    assert m["deferred_long_said"] and m["deferred_since"] == t - 25 * 3600 + 5


def test_an_after_push_that_exits_1_still_fails_at_high_severity(env, monkeypatch):
    from ttp import push, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", ["echo broken; exit 1"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    (r,) = _receipts(p)
    assert r["status"] == "finished" and r["after_push"]["status"] == "failed"
    alerts = []
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append((a, k))) == [r["id"]]
    (ev,) = _dd_events(p, "after_push_failed")
    assert ev["severity"] == "high" and alerts and alerts[0][0][0] == "after_push_failed"
    assert not _dd_events(p, "after_push_deferred")


def test_a_push_batch_whose_after_push_exits_75_is_deferred_and_deploys_on_a_later_retry(env, monkeypatch):
    from ttp import pushq
    cmd, flag, seen = _defer_cmd(env)
    s = _pq_real(env, monkeypatch, after_push=[cmd])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    assert m["outcome"] == "pushed" and m["after_push"]["status"] == "deferred", m.get("after_push")
    for _ in range(2):
        _pq_tend(s)
        assert bid not in pushq._children, "no retry before its backoff"
    told = _dd_events(s.p, "after_push_deferred")
    assert len(told) == 1 and told[0]["severity"] == "normal" and bid in told[0]["text"], told
    assert not _dd_events(s.p, "after_push_failed")
    row = s.p.db.one("SELECT after_push, after_finalized FROM push_batches WHERE id=?", (bid,))
    assert row["after_push"] == "deferred" and not row["after_finalized"]
    m = json.loads(marker.read_text())
    m["after_push"]["ended"] -= 400     # its backoff has passed
    marker.write_text(json.dumps(m))
    flag.write_text("")
    _pq_tend(s)
    assert bid in pushq._children, "the retry resumes the batch at its after_push"
    child = pushq._children[bid]
    child.wait(timeout=60)
    assert child.returncode == 0, (s.p.state / "pushes" / f"{bid}.log").read_text()
    _pq_tend(s)
    m = json.loads(marker.read_text())
    assert m["after_push"]["status"] == "ok" and seen.read_text() == "ran\n", m.get("after_push")
    ok = _dd_events(s.p, "after_push_ok")
    assert len(ok) == 1 and ok[0]["severity"] == "low" and ok[0]["text"].startswith("deploy done after deferral"), ok
    assert s.p.db.one("SELECT after_tries FROM push_batches WHERE id=?", (bid,))["after_tries"] == 0
    assert not _dd_events(s.p, "after_push_failed") and not s.p.db.q("SELECT id FROM events WHERE severity='high'")
