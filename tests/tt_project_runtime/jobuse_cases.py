# Detached-job use (jobuse) tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new job_use tests here, not there.
from __future__ import annotations


def _feed_ctr(jobuse, minutes, tree_cores, ctr_cores, ends_at=None):
    """A job whose own tree uses `tree_cores` while its containers use `ctr_cores` (a new container
    each `ends_at` minutes when set); returns the state and how many samples fired."""
    st, fired, tree, done, cur = None, 0, 0.0, 0, 0.0
    for i in range(minutes + 1):
        if ends_at and i and i % ends_at == 0:
            done, cur = done + 1, 0.0   # the container ended; the next one starts from zero
        got = {"cpu": tree, "start": "S", "dev": None, "ctr": {f"c{done}": cur} if ctr_cores else {}}
        st, fire = jobuse.assess(st, got, {"cores": 8}, 60.0 * i, _JOB_USE)
        fired += fire
        tree, cur = tree + 60.0 * tree_cores, cur + 60.0 * ctr_cores
    return st, fired


def test_a_job_whose_containers_are_busy_is_not_under_used_even_as_containers_come_and_go():
    from ttp import jobuse
    st, fired = _feed_ctr(jobuse, 40, 0.0, 7.0)
    assert fired == 0 and "under" not in st and st["used"] == 7
    # Containers that end keep their CPU in the total: the window is not restarted and still reads busy
    # (only what a container used after its last sample is lost: here 1 minute in 5).
    st, fired = _feed_ctr(jobuse, 40, 0.0, 7.0, ends_at=5)
    assert fired == 0 and st["used"] >= 5.5 and len(st["ring"]) > 10 and st["ctr_done"] > 0
    # Idle containers do not hide an idle job.
    st, fired = _feed_ctr(jobuse, 40, 0.5, 0.0)
    assert fired == 1 and "ctr" not in st
    st, fired = _feed_ctr(jobuse, 40, 0.0, 0.1, ends_at=7)
    assert fired == 1


def test_container_cpu_counts_only_containers_started_after_the_job(monkeypatch):
    from ttp import jobuse
    if not pathlib.Path("/proc/self/stat").exists():
        pytest.skip("needs /proc")
    proc = subprocess.Popen([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<0.3: pass\n"
                             "time.sleep(30)"], start_new_session=True)
    try:
        time.sleep(0.5)
        mine, kid = jobuse._stat(os.getpid())[19], jobuse._stat(proc.pid)[19]
        ctrs = [{"id": "abc", "pid": proc.pid}, {"id": "gone", "pid": 2 ** 22 + 12345}]
        got = jobuse.container_cpu(ctrs, mine)
        assert set(got) == {"abc"} and got["abc"] > 0
        assert jobuse.container_cpu(ctrs, int(kid) + 1) == {}, "a container older than the job is not its work"
        assert jobuse.container_cpu(ctrs, "Mon_Oct_27") == {} and jobuse.container_cpu(ctrs, None) == {}
        assert jobuse.container_cpu(None, mine) == {} and jobuse.container_cpu([], mine) == {}
        # No readable cgroup: the container's init tree is read instead.
        monkeypatch.setattr(jobuse, "_cgroup_cpu", lambda pid: None)
        assert 0 <= jobuse.container_cpu(ctrs, mine)["abc"] <= jobuse._cpu(proc.pid, jobuse._hz())
    finally:
        proc.kill()
        proc.wait()


def test_docker_listing_parses_and_degrades_without_docker(tmp_path, monkeypatch):
    from ttp import jobuse
    if not pathlib.Path("/proc/self/stat").exists():
        assert jobuse.containers() == []
        return
    monkeypatch.setenv("PATH", str(tmp_path / "none"))
    assert jobuse.containers() == [], "no docker: nothing to add, nothing fails"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    docker = bin_ / "docker"

    def fake(body):
        docker.write_text("#!/bin/sh\n" + body)
        docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))
    fake('case "$1" in ps) printf "%s\\n%s\\n%s\\n" aaaaaaaaaaaaaaaa bbbb cccc;;\n'
         ' inspect) printf "aaaaaaaaaaaaaaaa 4242\\nbbbb 0\\nnot a row at all\\ncccc x\\n"; exit 1;; esac\n')
    assert jobuse.containers() == [{"id": "aaaaaaaaaaaa", "pid": 4242}], "stopped, garbled or vanished rows skip"
    fake('echo "permission denied while trying to connect to the docker daemon socket" >&2; exit 1\n')
    assert jobuse.containers() is None, "docker there but unreadable"
    fake('exit 0\n')
    assert jobuse.containers() == []
    fake('sleep 5\n')
    assert jobuse.containers(timeout=0.3) is None, "a hung docker never blocks the check"


def test_the_daemon_counts_a_jobs_busy_containers_and_degrades_without_docker(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod, jobuse
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    rc, lock = _job_of_waiting_task(p, tid, {"cores": 8})
    try:
        now = time.time()
        # The job's own tree is idle: 100 CPU-seconds over 16 min.
        monkeypatch.setattr(jobuse, "sample", lambda pid, nodes=None: {"cpu": 100.0, "start": "S", "dev": None})
        seed = {str(rc): {"task": tid, "name": "build", "req": {"cores": 8},
                          "st": {"ring": [[now - 960, 0.0], [now - 60, 95.0]], "start": "S", "since": now - 960,
                                 "ctr": {"c1": 0.0}, "ctr_done": 0.0}}}
        calls = []
        monkeypatch.setattr(jobuse, "containers", lambda: calls.append(1) or [{"id": "c1", "pid": 1}])
        monkeypatch.setattr(jobuse, "container_cpu", lambda ctrs, start: {"c1": 960.0 * 7})
        p.db.set_kv(jobuse.KV, seed)
        d = dmod.Daemon(p.base)
        d.sample_jobs()
        assert not p.db.q("SELECT * FROM events WHERE kind='job_underuse'")
        assert "used 7.1 cores" in p.db.kv(jobuse.KV)[str(rc)]["line"] and calls == [1]
        # Docker unreadable: the result is today's (tree only), and docker is left alone for a while.
        for ctrs in ([], None):
            p.db.set_kv(jobuse.KV, seed)
            p.db.x("DELETE FROM events")
            d = dmod.Daemon(p.base)
            calls.clear()
            monkeypatch.setattr(jobuse, "containers", lambda: calls.append(1) or ctrs)
            d.sample_jobs()
            ev = p.db.q("SELECT * FROM events WHERE kind='job_underuse' AND task=?", (tid,))
            assert len(ev) == 1 and "used 0.1 cores" in p.db.kv(jobuse.KV)[str(rc)]["line"]
            d._jobs_sampled = 0
            d.sample_jobs()
            assert len(calls) == (2 if ctrs == [] else 1), "an unreadable docker is not asked every pass"
    finally:
        lock.close()


def test_a_steadily_idle_job_sampled_at_uneven_gaps_stays_one_stretch():
    from ttp import jobuse
    # A busy daemon samples every 1 to 4 min: no sample may fall a full window back, yet the window
    # keeps its base and the stretch never ends and starts again.
    st, t, cpu, fired, ends = None, 0.0, 0.0, 0, 0
    for gap in [60, 65, 190, 250, 120, 185, 62, 240] * 20:
        t, cpu = t + gap, cpu + gap * 0.05
        was = (st or {}).get("under")
        st, fire = jobuse.assess(st, {"cpu": cpu, "start": "S", "dev": None}, {"cores": 16}, t, _JOB_USE)
        fired += fire
        ends += bool(was and not st.get("under"))
    assert fired == 1 and ends == 0 and st["used"] == 0.05


def _replay(d, clock, cpu, steps):
    """Sample the job once per (gap, cpu or None for an unmeasured sample) step."""
    for gap, v in steps:
        clock[0] += gap
        cpu["v"] = v
        d._jobs_sampled = 0
        d.sample_jobs()


def test_a_flapping_under_used_job_raises_one_event_until_restart_request_change_or_hold_off(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod, jobuse
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    rc, lock = _job_of_waiting_task(p, tid, {"cores": 8})
    try:
        clock, cpu, start = [time.time()], {"v": 0.0}, {"v": "S"}
        monkeypatch.setattr(time, "time", lambda: clock[0])
        monkeypatch.setattr(jobuse, "sample", lambda pid, nodes=None: None if cpu["v"] is None else
                            {"cpu": cpu["v"], "start": start["v"], "dev": None})
        d = dmod.Daemon(p.base)

        def events():
            return p.db.q("SELECT * FROM events WHERE kind='job_underuse' AND task=? ORDER BY id", (tid,))
        # An API-bound job: about 0.05 cores, its CPU total drops each time a child process ends and
        # one sample cannot be measured; each restarts the stretch. Over 2.5 h: one event.
        steps, v = [], 0.0
        for i in range(60):
            v += 9.0
            if i in (20, 35, 50):
                v -= 30.0   # child processes ended: their CPU left the tree
            steps.append((150, None if i == 28 else v))
        _replay(d, clock, cpu, steps)
        assert len(events()) == 1
        entry = p.db.kv(jobuse.KV)[str(rc)]
        assert entry["st"].get("under") and entry["raised"]["at"] < clock[0] - 3600
        # The kv record forgot it (or an A-B-A request flip): the events table still holds it off.
        entry.pop("raised")
        p.db.set_kv(jobuse.KV, {str(rc): entry})
        _replay(d, clock, cpu, [(60, v + 1.0)])
        assert len(events()) == 1 and p.db.kv(jobuse.KV)[str(rc)]["raised"]
        # Its request changes (re-recorded with fewer cores): a new event.
        run_dir = rc.parent
        jobs = json.loads((run_dir / "detached.json").read_text())
        jobs[0]["requested"] = {"cores": 4}
        (run_dir / "detached.json").write_text(json.dumps(jobs))
        _replay(d, clock, cpu, [(60, v + 3.0)])
        assert len(events()) == 2 and "asked for 4 cores" in events()[-1]["text"]
        # Restarted (a new root process): a new stretch and a new event once it has a full window.
        start["v"], v = "S2", 0.0
        for _ in range(20):
            v += 3.0
            _replay(d, clock, cpu, [(60, v)])
        assert len(events()) == 3
        # Still under-used, flapping, past the 6 h hold-off: raised once more, not per stretch.
        steps = []
        for i in range(150):
            v += 9.0 - (30.0 if i % 25 == 24 else 0.0)
            steps.append((150, v))
        _replay(d, clock, cpu, steps)
        assert len(events()) == 4
        fps = [e["fingerprint"] for e in events()]
        assert fps[3] == fps[2] and len(set(fps)) == 3
    finally:
        lock.close()


def test_idle_by_design_names_what_explains_a_jobs_idle_time_and_nothing_else():
    from ttp import jobuse
    now = time.time()

    def why(job=None, labels=(), res=(), paused=None, gate=""):
        return jobuse.idle_by_design({"labels": json.dumps(list(labels))}, job or {"command": "make -j8"},
                                     set(res), paused or {}, gate, now)
    assert why() == ""
    for cmd in ("ttp checks --detach -- pytest -q", "/opt/bin/ttp.sh checks run", "cd w && ttp checks"):
        assert "checks" in why({"command": cmd}), cmd
    for cmd in ("make checks", "./run_checks.sh", "echo ttp-checks", "pytest -k checks"):
        assert why({"command": cmd}) == "", cmd   # a plain command naming checks waits on no slot
    assert "start_when" in why(labels=["start_when:test -e /tmp/x"])
    assert why(labels=["start_when:now"]) == ""
    assert "start_after" in why(labels=[f"start_after:{now + 600}"])
    assert why(labels=[f"start_after:{now - 600}"]) == "", "a start_after already passed explains nothing"
    assert "paused resource box-a" in why(res={"box-a"}, paused={"box-a": {"reason": "r"}})
    assert why(res={"box-b"}, paused={"box-a": {"reason": "r"}}) == ""
    assert "spend cap" in why({"command": "x", "spend": {"capped": True}})
    assert why({"command": "x", "spend": {"capped": False}}) == ""
    assert why(gate="the budget gate (red) allows no new work") == "the budget gate (red) allows no new work"


@pytest.mark.parametrize("case", ["checks", "after", "when", "paused_label", "paused_lock", "capped", "gate",
                                  "plain", "after_passed", "other_pause", "gate_green"])
def test_an_under_used_job_idle_by_design_wakes_no_model_and_an_unexplained_one_still_does(env, monkeypatch, case):
    p = make(env)
    from ttp import budget, daemon as dmod, db as dbm, jobuse
    tid = p.db.add_task("bench", "s", kind="work", tier="light", origin="user")
    rc, lock = _job_of_waiting_task(p, tid, {"cores": 8})
    try:
        now = time.time()
        reg = rc.parent / "detached.json"
        job = json.loads(reg.read_text())[0]
        job.update({"checks": {"command": "ttp checks --detach -- pytest -q"},
                    "paused_lock": {"locks": ["box-a"]}, "capped": {"spend": {"capped": True}}}.get(case, {}))
        reg.write_text(json.dumps([job]))
        labels = {"after": [f"start_after:{now + 900}"], "after_passed": [f"start_after:{now - 900}"],
                  "when": ["start_when:test -e /tmp/never"], "paused_label": ["resource:box-a"],
                  "other_pause": ["resource:box-b"]}.get(case)
        if labels:
            p.db.update_task(tid, labels=json.dumps(labels))
        if case in ("paused_label", "paused_lock", "other_pause"):
            p.db.set_kv(dbm.PAUSED_RESOURCES_KEY, {"box-a": {"reason": "maintenance", "since": now, "by": "user"}})
        monkeypatch.setattr(jobuse, "sample", lambda pid, nodes=None: {"cpu": 100.0, "start": "S", "dev": None})
        monkeypatch.setattr(jobuse, "containers", lambda: [])
        p.db.set_kv(jobuse.KV, {str(rc): {"task": tid, "name": "build", "req": {"cores": 8},
                                          "st": {"ring": [[now - 960, 0.0], [now - 60, 95.0]], "start": "S",
                                                 "since": now - 960}}})
        d = dmod.Daemon(p.base)
        if case.startswith("gate"):
            d.gates = {k: budget.Gate(k, level="green" if case == "gate_green" else "red",
                                      allow_new_work=case == "gate_green")
                       for k in {"claude", "fake", str(d.cfg.get("core_provider") or "claude")}}
        d.sample_jobs()
        ev = p.db.q("SELECT * FROM events WHERE kind='job_underuse' AND task=?", (tid,))
        entry = p.db.kv(jobuse.KV)[str(rc)]
        res = json.loads(p.db.task(tid)["result"])
        if case in ("plain", "after_passed", "other_pause", "gate_green"):
            assert len(ev) == 1 and "by_design" not in entry, (case, entry)
            assert "underuse_wake" in res or entry.get("pulled"), (case, res, entry)   # its check pulled in
        else:
            assert not ev and entry.get("by_design") and "idle by design" in entry["line"], (case, entry)
            assert "underuse_wake" not in res and not entry.get("pulled"), (case, res, entry)
    finally:
        lock.close()
