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
        assert jobuse.container_cpu(ctrs, mine)["abc"] >= 0.2
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
