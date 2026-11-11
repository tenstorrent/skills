# Orphaned remote `ttp checks` run tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file (the fake ssh
# from remotecheck_cases). Every process stopped here is one the test started itself, in a group of its own.
from __future__ import annotations

# A stand-in for a check's process on the host: a session (and group) of its own, its pid in OC_PID,
# then sleeps in the folder it was started in.
_OC_SLEEPER = ("import os, sys, time\nos.setsid()\nopen(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
               "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\ntime.sleep(120)\n")


def _oc_checkout(remote, name, owner, age_s=600):
    d = remote / f"ttp-check.{name}"
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "ttp-owner").write_text(owner + "\n")
    t = time.time() - age_s
    os.utime(d, (t, t))
    return d


def _oc_sleep_in(d, tmp_path):
    pidf = tmp_path / f"{d.name}.pid"
    proc = subprocess.Popen([sys.executable, "-c", _OC_SLEEPER, str(pidf)], cwd=d, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while not pidf.exists() and time.time() < deadline:
        time.sleep(0.05)
    return proc


def _oc_env(tmp_path, monkeypatch):
    from ttp import remotecheck
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    monkeypatch.setattr(remotecheck, "paused", lambda host, where=None: "")
    return remote


def _oc_dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_a_remote_check_whose_local_owner_is_gone_is_stopped_by_its_group_and_its_folder_removed(tmp_path, monkeypatch):
    """The scan stops the process group of a checkout of this machine whose owner (pid and start time) is
    gone, and removes its folder; a checkout whose owner lives, one of another machine, and a young one stay."""
    from ttp import checkcap, orphanchecks
    remote = _oc_env(tmp_path, monkeypatch)
    tag = checkcap.host_tag()
    orphan = _oc_checkout(remote, "orphan", f"{tag} {_oc_dead_pid()} 12345")
    live = _oc_checkout(remote, "live", orphanchecks.owner_line())
    other = _oc_checkout(remote, "other", f"otherhst {_oc_dead_pid()} 12345")
    young = _oc_checkout(remote, "young", f"{tag} {_oc_dead_pid()} 12345", age_s=0)
    procs = {d.name: _oc_sleep_in(d, tmp_path) for d in (orphan, live, other, young)}
    try:
        assert [r["dir"] for r in orphanchecks.scan("box1", dry_run=True)] == [str(orphan)]
        assert procs[orphan.name].poll() is None and orphan.exists(), "a dry run stops nothing"
        (rec,) = orphanchecks.scan("box1")
        assert rec["dir"] == str(orphan) and rec["groups"] == str(procs[orphan.name].pid)
        assert "stopped orphaned check" in rec["text"] and "folder removed" in rec["text"]
        assert procs[orphan.name].wait(timeout=15) is not None and not orphan.exists()
        for d in (live, other, young):
            assert procs[d.name].poll() is None and d.exists(), d.name
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
            p.wait()


def test_a_remote_check_still_running_in_a_removed_checkout_is_stopped(tmp_path, monkeypatch):
    """A check whose folder was removed under it (its local side cleaned up while the run went on) has no
    owner left: the scan finds it by its working folder and stops its group."""
    from ttp import checkcap, orphanchecks
    remote = _oc_env(tmp_path, monkeypatch)
    gone = _oc_checkout(remote, "gone", orphanchecks.owner_line())
    proc = _oc_sleep_in(gone, tmp_path)
    try:
        shutil.rmtree(gone)
        (rec,) = orphanchecks.scan("box1")
        assert rec["dir"] == str(gone) and rec["groups"] == str(proc.pid)
        assert proc.wait(timeout=15) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_remote_check_names_its_owner_and_its_cleanup_stops_what_the_run_left_behind(tmp_path):
    """remotecheck writes this machine's tag and its own pid into the checkout (outside the work tree), and
    once the run ends its cleanup stops a process the run left running there, not only removes the folder."""
    from ttp import checkcap
    repo = _rc_repo(tmp_path)
    out = tmp_path / "seen"
    out.mkdir()
    cmd = (f"cat .git/ttp-owner > {out}/owner; git status --porcelain > {out}/status; "
           f"{shlex.quote(sys.executable)} -c {shlex.quote(_OC_SLEEPER)} {out}/left </dev/null >/dev/null 2>&1 & "
           f"while [ ! -e {out}/left ]; do sleep 0.05; done")
    rc, said = _rc_run(tmp_path, repo, cmd)
    assert rc == 0, said
    tag, pid, start = (out / "owner").read_text().split()
    assert tag == checkcap.host_tag() and pid.isdigit() and start.isdigit()
    assert (out / "status").read_text() == ""
    left = int((out / "left").read_text())
    deadline = time.time() + 15
    while Path(f"/proc/{left}").exists() and time.time() < deadline:
        time.sleep(0.1)
    alive = Path(f"/proc/{left}").exists()
    if alive:
        os.kill(left, signal.SIGKILL)   # the test's own process
    assert not alive, "the process the run left behind is stopped by the cleanup"
    assert _rc_left(tmp_path) == []


def test_orphan_check_sweep_scans_each_check_host_once_per_window_for_all_projects(tmp_path, monkeypatch):
    """sweep covers the spill hosts and delivery.checks_host, never "local"; a host scanned within the
    window (by any project of this user: a stamp in the tt-project home) is skipped."""
    from ttp import orphanchecks, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    calls = []

    def run(host, script):
        calls.append(host)
        return 0, f"{orphanchecks.LIST_MARK} /tmp/ttp-check.x 1 2 9999\n" if "ttp-orphan-dir" in script else \
            f"{orphanchecks.REAP_MARK} /tmp/ttp-check.x 4242\n"

    from ttp import remotecheck
    monkeypatch.setattr(remotecheck, "paused", lambda host, where=None: "")
    cfg = {"delivery": {"checks_spill_hosts": ["h1", "h2"], "checks_host": "h3"}}
    assert orphanchecks.hosts(cfg)[:3] == ["h1", "h2", "h3"]
    recs = orphanchecks.sweep(cfg, now=1000.0, run=run, alive=lambda pid, st: False)
    assert [r["host"] for r in recs][:3] == ["h1", "h2", "h3"] and recs[0]["groups"] == "4242"
    n = len(calls)
    assert orphanchecks.sweep(cfg, now=1060.0, run=run, alive=lambda pid, st: False) == [] and len(calls) == n
    assert orphanchecks.sweep(cfg, now=1000.0 + orphanchecks.EVERY_S, run=run, alive=lambda pid, st: False)
    assert orphanchecks.hosts({"delivery": {"checks_spill_hosts": [], "checks_host": "local"}}) == []
    assert orphanchecks.orphans(f"{orphanchecks.LIST_MARK} /tmp/ttp-check.y 1 2 5\n") == [], "within the grace"


def test_reap_report_removes_only_this_machines_dead_checkouts_and_tallies_each_host(tmp_path, monkeypatch):
    """The `ttp checks --reap` report on a fake host: an own dead checkout is removed; an own live one, a
    foreign one and one with no owner file stay; the line counts found, live, reaped and skipped."""
    from ttp import checkcap, orphanchecks
    remote = _oc_env(tmp_path, monkeypatch)
    tag = checkcap.host_tag()
    dead = _oc_checkout(remote, "dead", f"{tag} {_oc_dead_pid()} 12345")
    live = _oc_checkout(remote, "live", orphanchecks.owner_line())
    foreign = _oc_checkout(remote, "foreign", f"otherhst {_oc_dead_pid()} 12345")
    unmarked = remote / "ttp-check.unmarked"
    unmarked.mkdir()
    os.utime(unmarked, (time.time() - 600, time.time() - 600))
    cfg = {"delivery": {"checks_spill_hosts": ["box1"]}}
    dry = orphanchecks.reap_report(cfg, dry_run=True)
    assert dry[-1] == "box1: found 2, live 1, would reap 1, skipped 2 (not this machine's, or no owner file)"
    assert dead.exists(), "a dry run removes nothing"
    lines = orphanchecks.reap_report(cfg)
    assert lines[-1] == "box1: found 2, live 1, reaped 1, skipped 2 (not this machine's, or no owner file)"
    assert any(str(dead) in ln and "folder removed" in ln for ln in lines[:-1])
    assert not dead.exists()
    assert live.exists() and foreign.exists() and unmarked.exists()


def test_reap_report_skips_an_unreachable_or_paused_host_and_still_reports_the_rest(tmp_path, monkeypatch):
    """A host ssh cannot reach, one that is paused, and one that raises are each one skipped line; the next
    host is still reaped, and nothing raises."""
    from ttp import orphanchecks, remotecheck
    monkeypatch.setattr(remotecheck, "paused", lambda host, where=None: "paused by a hold" if host == "held" else "")

    def run(host, script):
        if host == "down":
            return 255, "ssh: no route"
        if host == "odd":
            raise ValueError("boom")
        return 0, f"{orphanchecks.SKIP_MARK} 0\n"

    cfg = {"delivery": {"checks_spill_hosts": ["down", "held", "odd", "up"]}}
    assert orphanchecks.reap_report(cfg, run=run) == [
        "down: skipped (unreachable)", "held: skipped (paused by a hold)", "odd: skipped (error: ValueError)",
        "up: found 0, live 0, reaped 0, skipped 0 (not this machine's, or no owner file)"]


def test_checks_reap_prints_the_report_of_each_check_host_and_exits_zero(tmp_path, monkeypatch, capsys):
    """`ttp checks --reap` runs the local reap, then the per-host report, and exits 0 even for a host out of reach."""
    import argparse
    from ttp import cli, leftoverchecks, orphanchecks
    p = types.SimpleNamespace(config=lambda: {"delivery": {"checks_spill_hosts": ["down"]}})
    monkeypatch.setattr(cli, "here", lambda *a, **k: p)
    monkeypatch.setattr(leftoverchecks, "reap", lambda *a, **k: [])
    monkeypatch.setattr(leftoverchecks, "test_orphans", lambda *a, **k: [])
    monkeypatch.setattr(orphanchecks, "reap_report", lambda cfg, dry_run=False: ["down: skipped (unreachable)"])
    monkeypatch.setattr("ttp.daemon.log", lambda *a, **k: None)
    a = argparse.Namespace(reap=True, dry_run=False, result=None, gap=None)
    with pytest.raises(SystemExit) as e:
        cli.cmd_checks(a)
    assert e.value.code == 0 and "down: skipped (unreachable)" in capsys.readouterr().out
