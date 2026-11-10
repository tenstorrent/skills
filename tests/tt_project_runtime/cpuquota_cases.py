# The CPU-quota group of check and test commands (cpuquota.py), run by tests/test_tt_project_runtime.py
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _quota_bin(tmp_path, show_rc=0):
    """Stand-ins for systemctl and systemd-run that log their arguments; systemd-run runs the command."""
    import stat
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    log = tmp_path / "systemd.log"
    scripts = {
        "systemctl": f'#!/bin/sh\necho "systemctl $*" >> {log}\n'
                     f'[ "$2" = show ] && {{ echo /fake.service; exit {show_rc}; }}\nexit 0\n',
        "systemd-run": f'#!/bin/sh\necho "systemd-run $*" >> {log}.run\n'
                       'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n'}
    for name, text in scripts.items():
        (d / name).write_text(text)
        (d / name).chmod(stat.S_IRWXU)
    return d, log


def _main_in_process(cpuquota, conf, argv):
    import signal
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        return cpuquota.main([json.dumps(conf), "--", *argv])
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def test_cpu_group_settings_are_off_by_default_and_a_project_only_tightens(env):
    from ttp import cpuquota
    assert cpuquota.settings(None, None) is None and cpuquota.settings({"checks": {}}, {}) is None
    probe = {"busy_probe": "exit 0", "busy_cpu_quota": 2}
    assert cpuquota.settings({"checks": {"busy_probe": "exit 0"}}, {}) is None, "a probe with no busy quota limits nothing"
    assert cpuquota.settings({"checks": {"busy_cpu_quota": 2}}, {}) is None, "a busy quota with no probe neither"
    got = cpuquota.settings({"checks": {"cpu_quota": "12"}}, {"checks": {"cpu_quota": 16, **probe}})
    assert got == {"idle": 12.0, "busy": 2.0, "probe": "exit 0", "every": cpuquota.PROBE_S, "cgroup": ""}
    assert cpuquota.settings({"checks": {"cpu_quota": 40}}, {"checks": {"cpu_quota": 16}})["idle"] == 16, \
        "a project cannot raise the account's quota"
    got = cpuquota.settings({"checks": {"cpu_quota": 1}}, {"checks": probe})
    assert got["busy"] == 1, "the busy quota is never above the idle one"
    got = cpuquota.settings(None, {"checks": {**probe, "busy_probe_s": 0.01, "cpu_quota": "junk"}})
    assert got["idle"] is None and got["busy"] == 2 and got["every"] == cpuquota.PROBE_MIN_S


def test_the_busy_probe_reads_exit_0_as_busy_1_as_idle_and_anything_else_as_busy():
    from ttp import cpuquota
    assert cpuquota.busy("exit 0", 5) is True
    assert cpuquota.busy("exit 1", 5) is False
    assert cpuquota.busy("exit 2", 5) is True, "a broken probe keeps the tighter quota"
    assert cpuquota.busy("sleep 5", 0.2) is True, "a hung probe too"


def test_check_commands_run_in_the_cpu_group_only_once_a_quota_is_set(env, tmp_path, monkeypatch):
    from ttp import cpuquota, project, testslots
    monkeypatch.delenv(cpuquota.GROUP_ENV, raising=False)
    monkeypatch.setattr(testslots.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(testslots, "_cfg", lambda: None)
    assert testslots.low_priority(["pytest"]) == ["nice", "-n", "19", "ionice", "-c3", "pytest"], "unchanged by default"
    project.set_account_setting("checks.cpu_quota", 8)
    argv = testslots.low_priority(["pytest"])
    assert argv[:2] == [sys.executable, "-I"] and argv[-7:] == ["--", "nice", "-n", "19", "ionice", "-c3", "pytest"]
    assert json.loads(argv[4])["idle"] == 8
    monkeypatch.setenv(cpuquota.GROUP_ENV, "ttp-checks-1")
    assert testslots.low_priority(["pytest"])[0] == "nice", "a command already in a group gets no second one"
    # a worker's own test run (`ttp sandbox`) too: here no systemd --user answers, so it says so and runs as it is
    fake, _ = _quota_bin(tmp_path, show_rc=1)
    run = subprocess.run([sys.executable, "-m", "ttp", "sandbox", "--", "sh", "-c", "echo ran; exit 7"],
                         env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PYTHONPATH": str(RUNTIME),
                              cpuquota.GROUP_ENV: ""}, capture_output=True, text=True, timeout=60)
    assert run.returncode == 7 and run.stdout == "ran\n"
    assert "no CPU-quota group (no systemd --user manager)" in run.stderr, run.stderr


def test_without_systemd_user_or_a_cgroup_the_command_runs_as_today_and_says_why_once(env, tmp_path, monkeypatch):
    from ttp import cpuquota
    monkeypatch.delenv(cpuquota.GROUP_ENV, raising=False)
    fake, log = _quota_bin(tmp_path, show_rc=1)
    argv = cpuquota.wrap(["sh", "-c", 'echo "group=$TTP_CPU_GROUP"; exit 5'], {"idle": 2, "busy": 2, "probe": "", "every": 30, "cgroup": ""})
    run_env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PYTHONPATH": "/nonexistent"}
    first = subprocess.run(argv, env=run_env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 5 and first.stdout == "group=\n", "run as it is, its exit code kept"
    assert first.stderr.count("no CPU-quota group") == 1
    again = subprocess.run(argv, env=run_env, capture_output=True, text=True, timeout=60)
    assert again.returncode == 5 and "no CPU-quota group" not in again.stderr, "said once per reason"
    assert not log.with_suffix(".log.run").exists(), "no scope started"
    missing = subprocess.run(argv, env={**run_env, "PATH": "/nonexistent"}, capture_output=True, text=True, timeout=60)
    assert missing.returncode == 127 and "systemd-run or systemctl is missing" in missing.stderr


def test_the_quota_tightens_while_the_probe_says_busy_and_lifts_when_idle(env, tmp_path, monkeypatch):
    from ttp import cpuquota
    fake, log = _quota_bin(tmp_path)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    root = tmp_path / "cg"
    (root / "fake.service").mkdir(parents=True)
    (root / "fake.service" / "cgroup.controllers").write_text("cpu memory pids\n")
    monkeypatch.setattr(cpuquota, "CGROUP_ROOT", root)
    flag, step = tmp_path / "busy", tmp_path / "step"
    probe = f"test -e {flag}"
    # busy, then idle, as the command itself says: the wrapper probes every 0.1 s and changes the quota in place
    script = (f"touch {flag}; while ! grep -q CPUQuota=100% {log}; do sleep 0.05; done; rm {flag}; "
              f"while ! grep -q 'CPUQuota=$' {log}; do sleep 0.05; done; echo \"$TTP_CPU_GROUP\" > {step}; exit 3")
    rc = _main_in_process(cpuquota, {"idle": None, "busy": 1, "probe": probe, "every": 0.1, "cgroup": ""},
                          ["timeout", "20", "sh", "-c", script])
    lines = log.read_text().splitlines()
    assert rc == 3, lines
    start = log.with_suffix(".log.run").read_text()
    assert "--user --scope" in start and "-p CPUQuota" not in start, "idle and no idle quota: started unlimited"
    unit = start.split("--unit=")[1].split()[0]
    sets = [x for x in lines if "set-property" in x]
    assert sets[0] == f"systemctl --user set-property --runtime {unit} CPUQuota=100%"
    assert sets[-1] == f"systemctl --user set-property --runtime {unit} CPUQuota="
    assert step.read_text().strip() + ".scope" == unit, "the command knows its group"
    # an idle quota starts the scope with it
    assert _main_in_process(cpuquota, {"idle": 4, "busy": 1, "probe": "exit 1", "every": 30, "cgroup": ""}, ["true"]) == 0
    assert "-p CPUQuota=400%" in log.with_suffix(".log.run").read_text().splitlines()[-1]


def test_a_delegated_cgroup_v2_directory_holds_the_command_where_systemd_user_cannot(env, tmp_path, monkeypatch):
    from ttp import cpuquota
    monkeypatch.setattr(cpuquota, "_systemd", lambda: "no systemd --user manager")
    cg = tmp_path / "delegated"
    cg.mkdir()
    conf = {"idle": None, "busy": 0.5, "probe": "exit 0", "every": 30, "cgroup": str(cg)}
    assert cpuquota._cgroup(str(cg)).startswith(f"{cg} is no cgroup v2 directory")
    (cg / "cgroup.subtree_control").write_text("memory\n")
    assert "cpu controller is not enabled" in cpuquota._cgroup(str(cg))
    (cg / "cgroup.subtree_control").write_text("cpu memory\n")
    assert cpuquota._cgroup(str(cg)) is None
    assert _main_in_process(cpuquota, conf, ["sh", "-c", "exit 4"]) == 4
    child, = [d for d in cg.iterdir() if d.is_dir()]
    assert (child / "cpu.max").read_text() == "50000 100000\n", "busy: half a core"
    assert (child / "cgroup.procs").read_text() == "0", "the command joined it before it ran"


def test_a_delegated_cgroup_the_command_cannot_join_runs_it_without_one_and_leaves_no_child(env, tmp_path):
    # a child whose cgroup.procs cannot be opened for write (as EACCES outside the delegated subtree, or
    # EBUSY, would refuse it): the command still runs, with its own exit code, says why once, leaves no child.
    # Run in its own process: the fallback execs the command in place of the wrapper.
    from ttp import cpuquota
    cg = tmp_path / "delegated"
    cg.mkdir()
    (cg / "cgroup.subtree_control").write_text("cpu memory\n")
    conf = {"idle": 1, "busy": 1, "probe": "", "every": 30, "cgroup": str(cg)}
    boot = ("import shutil, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from ttp import cpuquota\n"
            "made = Path.mkdir\n"
            "def mkdir(self, *a, **k):\n"
            "    made(self, *a, **k)\n"
            "    if self.parent == Path(sys.argv[2]): made(self / 'cgroup.procs')\n"
            "Path.mkdir = mkdir\n"
            "cpuquota._rmdir = shutil.rmtree   # cgroupfs drops a group's control files with it\n"
            "cpuquota._systemd = lambda: 'no systemd --user manager'\n"
            "sys.exit(cpuquota.main([sys.argv[3], '--', 'sh', '-c', 'echo ran; exit 6']))")
    runtime = str(Path(cpuquota.__file__).resolve().parent.parent)
    run_env = {**os.environ, "TTP_HOME": str(tmp_path / "home")}
    for first in (True, False):
        out = subprocess.run([sys.executable, "-c", boot, runtime, str(cg), json.dumps(conf)],
                             env=run_env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 6 and out.stdout == "ran\n", out.stderr
        assert "Traceback" not in out.stderr, out.stderr
        assert [d.name for d in cg.iterdir() if d.is_dir()] == [], "no ttp-checks-* child left"
        said = out.stderr.count(f"no CPU-quota group (cannot join a group in {cg}: Is a directory)")
        assert said == (1 if first else 0), out.stderr
