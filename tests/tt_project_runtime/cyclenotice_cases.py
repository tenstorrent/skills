# Power-cycle notice and interrupted upgrade tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# Only fake notice paths in the test folder are read (the env fixture points TTP_TEST_CYCLE_NOTICES there).
from __future__ import annotations


def _notice(env, monkeypatch, state="possible", reason="chip 3 dropped off the bus", **extra):
    path = env["tmp"] / "power-cycle-pending.json"
    monkeypatch.setenv("TTP_TEST_CYCLE_NOTICES", str(path))
    if state is not None:
        path.write_text(json.dumps({"state": state, "fault_class": "chip_drop", "chips": [3], "reason": reason,
                                    "hold_since": time.time() - 30, "earliest_cycle_at": time.time() + 90,
                                    "updated_at": time.time(), **extra}))
    return path


def _cn_exited_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _upgradable(env, monkeypatch):
    """A project whose installed template adds a line to worker.md, and a stubbed daemon restart."""
    from ttp import service
    p = make(env)
    restarts = []
    monkeypatch.setattr(service, "restart", lambda q: restarts.append(1) or "restarted")
    worker = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    _install_template(env, {"worker.md": worker + "\nupstream line\n"})
    return p, restarts


@pytest.mark.parametrize("state", ["possible", "requested"])
def test_upgrade_and_setup_change_nothing_and_exit_75_while_a_power_cycle_notice_holds(env, monkeypatch, capsys,
                                                                                      state):
    from ttp import __version__, cli
    p, restarts = _upgradable(env, monkeypatch)
    _notice(env, monkeypatch, state)
    head = _git_out(p.harness, "rev-parse", "HEAD")
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo"])
    err = capsys.readouterr().err
    assert e.value.code == 75 and f"state {state}" in err and "chip 3 dropped off the bus" in err, err
    assert "earliest cycle at" in err and err.count("power cycle") == 1
    assert _git_out(p.harness, "rev-parse", "HEAD") == head and not restarts
    assert not _git_out(p.harness, "status", "--porcelain") and not (p.state / "upgrade.journal").exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["setup", "--bin-dir", str(env["tmp"] / "bin")])
    err = capsys.readouterr().err
    assert e.value.code == 75 and f"state {state}" in err
    assert not (env["home"] / "lib" / __version__).exists() and not (env["tmp"] / "bin" / "ttp").exists()


def test_without_a_notice_the_upgrade_runs_and_the_override_proceeds_with_a_warning(env, monkeypatch, capsys):
    from ttp import cli
    p, restarts = _upgradable(env, monkeypatch)
    path = _notice(env, monkeypatch, None)       # the path is configured, the file absent
    cli.main(["upgrade", "demo"])
    assert "upstream line" in (p.harness / "prompts" / "worker.md").read_text() and restarts == [1]
    assert "power cycle" not in capsys.readouterr().err
    worker = (p.harness / "prompts" / "worker.md").read_text()
    _install_template(env, {"worker.md": worker + "second line\n"})
    _notice(env, monkeypatch, "requested")
    cli.main(["upgrade", "demo", "--ignore-cycle-notice"])
    err = capsys.readouterr().err
    assert "second line" in (p.harness / "prompts" / "worker.md").read_text() and restarts == [1, 1]
    assert "state requested" in err and "warning" in err.lower(), err
    path.unlink()
    assert not (p.state / "upgrade.journal").exists()


def test_a_malformed_notice_holds_only_while_fresh_and_a_stale_or_idle_one_never(env, monkeypatch):
    from ttp import cyclenotice
    path = _notice(env, monkeypatch, None)
    now = time.time()
    path.write_text("{not json")
    h = cyclenotice.held([str(path)], now)
    assert h and h["malformed"] and h["state"] == "unreadable"
    assert cyclenotice.held([str(path)], now + cyclenotice.MALFORMED_FRESH_S + 1) is None
    path.write_text(json.dumps({"state": "idle", "updated_at": now}))
    assert cyclenotice.held([str(path)], now) is None, "only a hold state holds"
    path.write_text(json.dumps({"state": "possible", "reason": "x", "updated_at": now}))
    assert cyclenotice.held([str(path)], now)["state"] == "possible"
    assert cyclenotice.held([str(path)], now + cyclenotice.VALID_FRESH_S + 1) is None, "a dead broker's leftover"
    assert cyclenotice.held([str(env["tmp"] / "absent.json")], now) is None
    assert cyclenotice.paths({"upgrade": {"cycle_notices": []}}) == [], "[] turns the hold off"
    assert cyclenotice.paths({}) == [str(path)]
    assert cyclenotice.default_paths() == [str(path)] and cyclenotice.NOTICE.startswith("/run/")


def test_a_malformed_fresh_notice_holds_the_upgrade(env, monkeypatch, capsys):
    from ttp import cli
    p, restarts = _upgradable(env, monkeypatch)
    _notice(env, monkeypatch, None).write_text("")
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo"])
    assert e.value.code == 75 and "unreadable" in capsys.readouterr().err and not restarts


def test_the_automatic_upgrade_waits_for_a_notice_without_spending_its_try(env, monkeypatch):
    from ttp import release
    p, restarts = _upgradable(env, monkeypatch)
    _notice(env, monkeypatch, "possible")
    why = release.hold_reason(p, release.drift(p) or {"key": "k"}) if hasattr(release, "drift") else ""
    assert why and why.endswith(release.CYCLE_HELD) and "state possible" in why


def test_an_ordinary_push_after_push_waits_for_the_notice_and_runs_once_it_is_gone(env, monkeypatch):
    """The deploy is held, not failed: no alert, no try spent, and the daemon runs it once the file is gone."""
    from ttp import batch, push, pushq
    seen = env["tmp"] / "after_push.txt"
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [f"echo ran >> {seen}"])
    _notice(env, monkeypatch, "requested")
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    (r,) = _receipts(p)
    assert r["status"] == "held" and not seen.exists() and "state requested" in r["after_held"]["why"]
    spawned, alerts = [], []
    monkeypatch.setattr(pushq, "subprocess", types.SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **k: spawned.append(argv)))
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [] and not spawned and not alerts
    assert not p.db.one("SELECT id FROM events WHERE kind='after_push_failed'")
    (env["tmp"] / "power-cycle-pending.json").unlink()
    pushq.tend_receipts(p, lambda *a, **k: alerts.append(a))
    (r,) = _receipts(p)
    assert len(spawned) == 1 and r["status"] == "rerun" and not r.get("tries"), "a held deploy spends no try"
    pushq.tend_receipts(p, lambda *a, **k: alerts.append(a))
    assert len(spawned) == 1, "not started twice while its rerun starts"
    assert batch.rerun_receipt(p, p.state / "after_push" / f"{r['id']}.json") == 0
    (r,) = _receipts(p)
    assert r["status"] == "finished" and r["after_push"]["status"] == "ok" and seen.read_text() == "ran\n", r["after_push"].get("tail")
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [r["id"]] and not alerts
    assert p.db.one("SELECT id FROM events WHERE kind='after_push_ok'")


def test_an_after_push_a_reboot_cut_short_is_run_again_once_at_the_next_daemon_tick(env, monkeypatch):
    from ttp import pushq
    p = make(env)
    p.set_config("delivery.after_push", ["true"])
    folder = p.state / "after_push"
    folder.mkdir(parents=True)
    receipt = folder / "r1.json"
    receipt.write_text(json.dumps({"v": 1, "id": "r1", "status": "running", "outcome": "pushed", "pushed_sha": "a" * 40,
                                   "target": "origin/x", "pid": _cn_exited_pid(), "boot": "an-earlier-boot",
                                   "started": time.time() - 60}))
    spawned = []
    monkeypatch.setattr(pushq, "subprocess", types.SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **k: spawned.append(argv)))
    assert pushq.tend_receipts(p) == [] and len(spawned) == 1
    m = json.loads(receipt.read_text())
    assert m["status"] == "rerun" and m["tries"] == 1
    assert p.db.one("SELECT id FROM events WHERE kind='after_push_resumed'")
    # Its rerun never started: after RERUN_START_S it is reported as stopped, not run a third time.
    reported = pushq.tend_receipts(p, now=time.time() + pushq.RERUN_START_S + 5)
    assert reported == ["r1"] and len(spawned) == 1
    assert json.loads(receipt.read_text())["after_push"]["status"] == "killed"


class _Cut(BaseException):
    """A kill or power cut at one step of the upgrade."""


def _cut_at(monkeypatch, step, partial=None):
    from ttp import upjournal
    real = upjournal.step

    def step_(p, name, **fields):
        real(p, name, **fields)
        if name == step:
            if partial:
                partial(p)
            rec = json.loads(upjournal.path(p).read_text())
            upjournal.path(p).write_text(json.dumps({**rec, "pid": _cn_exited_pid()}))
            raise _Cut(name)
    monkeypatch.setattr(upjournal, "step", step_)
    return real


def _half_merge(p):
    """The merge wrote some of its files, then the power went."""
    (p.harness / "prompts" / "worker.md").write_text("half")


@pytest.mark.parametrize("step", ["committed", "merging", "merged", "restarting"])
def test_an_upgrade_cut_short_at_any_step_is_finished_by_the_next_run_with_no_half_state(env, monkeypatch, step,
                                                                                       capsys):
    from ttp import cli, upjournal
    p, restarts = _upgradable(env, monkeypatch)
    head = _git_out(p.harness, "rev-parse", "HEAD")
    real = _cut_at(monkeypatch, step, _half_merge if step == "merging" else None)
    with pytest.raises(_Cut):
        cli.main(["upgrade", "demo"])
    assert upjournal.path(p).exists() and not restarts
    moved = _git_out(p.harness, "rev-parse", "HEAD") != head
    assert moved == (step in ("merged", "restarting"))
    monkeypatch.setattr(upjournal, "step", real)
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    assert f"stopped at step {step}" in out, out
    h = p.harness
    assert "upstream line" in (h / "prompts" / "worker.md").read_text()
    assert not _git_out(h, "status", "--porcelain")
    assert subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode != 0
    assert not upjournal.path(p).exists() and restarts, "the stopped daemon was restarted"


def test_an_upgrade_cut_mid_merge_rolls_back_to_main_and_a_merged_one_restarts_the_old_daemon(env, monkeypatch):
    from ttp import cli, service, upjournal
    p, restarts = _upgradable(env, monkeypatch)
    head = _git_out(p.harness, "rev-parse", "HEAD")
    real = _cut_at(monkeypatch, "merging", _half_merge)
    with pytest.raises(_Cut):
        cli.main(["upgrade", "demo"])
    monkeypatch.setattr(upjournal, "step", real)
    line = upjournal.recover(p, log=lambda m: None)
    assert "rolled back" in line and _git_out(p.harness, "rev-parse", "HEAD") == head
    assert not _git_out(p.harness, "status", "--porcelain") and not upjournal.path(p).exists()
    assert upjournal.recover(p) == "", "once"
    # Cut after main moved, while a daemon that started before it runs: it is asked to restart.
    started = time.time() - 5
    _cut_at(monkeypatch, "merged")
    with pytest.raises(_Cut):
        cli.main(["upgrade", "demo"])
    line = upjournal.recover(p, daemon_started=started, log=lambda m: None)
    assert "finished" in line and "restarts" in line and not restarts
    req = json.loads((p.state / service.RESTART_REQUEST).read_text())
    assert req["at"] >= started and service.take_restart_request.__name__


def test_a_live_upgrade_journal_is_left_alone(env, monkeypatch):
    from ttp import upjournal
    p = make(env)
    upjournal.begin(p, "upgrade")
    upjournal.step(p, "merging", base="x", merged="y")
    assert upjournal.recover(p) == "" and upjournal.path(p).exists(), "its run still goes"
    upjournal.end(p)
    assert not upjournal.path(p).exists()


def test_an_automatic_upgrade_a_reboot_cut_short_is_retried_a_bounded_number_of_times(env, monkeypatch):
    from ttp import release, upjournal
    p = make(env)
    for n in range(1, upjournal.MAX_INTERRUPTS + 1):
        p.db.set_kv(release.KV_AUTO, {**(p.db.kv(release.KV_AUTO) or {}), "key": "k", "outcome": "running"})
        upjournal.path(p).parent.mkdir(parents=True, exist_ok=True)
        upjournal.path(p).write_text(json.dumps({"pid": _cn_exited_pid(), "boot": "old", "step": "committed",
                                                 "auto": True}))
        assert upjournal.recover(p, log=lambda m: None)
        rec = p.db.kv(release.KV_AUTO)
        assert rec["interrupted"] == n
        assert rec["outcome"] == ("held" if n < upjournal.MAX_INTERRUPTS else "failed")


def _plugin_copy(tmp):
    import shutil
    plugin = tmp / "plugin"
    for part in ("runtime", "template", "bin"):
        shutil.copytree(RUNTIME.parent / part, plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
    return plugin


def test_a_setup_cut_short_is_finished_or_rolled_back_by_the_next_ttp_command(env, tmp_path, capsys):
    import os as _os
    from ttp import __version__, cli
    from ttp.runner import boot_id
    lib_dir = env["home"] / "lib"
    lib = lib_dir / __version__
    (lib / "runtime" / "ttp").mkdir(parents=True)
    (lib / "runtime" / "ttp" / "cli.py").write_text("")          # the copy stopped half way
    journal = lib_dir / cli.SETUP_JOURNAL
    plugin = _plugin_copy(tmp_path)
    journal.write_text(json.dumps({"pid": _cn_exited_pid(), "boot": boot_id(), "source": str(plugin), "lib": str(lib),
                                   "commit": "abc123", "existed": False, "prev": ""}))
    assert "finished" in cli._recover_setup()
    assert (lib / "runtime" / "ttp" / "cli.py").read_text() == (plugin / "runtime" / "ttp" / "cli.py").read_text()
    assert _os.readlink(lib_dir / "current") == str(lib) and not journal.exists()
    assert (lib / "runtime" / "ttp" / "SOURCE_COMMIT").read_text().strip() == "abc123"
    # The plugin folder it copied from is gone: back to the release lib/current pointed at before.
    old = lib_dir / "0.0.1"
    (old / "runtime").mkdir(parents=True)
    import shutil
    shutil.rmtree(lib)
    (lib / "runtime").mkdir(parents=True)
    journal.write_text(json.dumps({"pid": _cn_exited_pid(), "boot": "an-earlier-boot", "source": str(tmp_path / "gone"),
                                   "lib": str(lib), "existed": False, "prev": str(old)}))
    cli.main(["list"])
    assert "rolled back" in capsys.readouterr().err
    assert _os.readlink(lib_dir / "current") == str(old) and not lib.exists() and not journal.exists()
