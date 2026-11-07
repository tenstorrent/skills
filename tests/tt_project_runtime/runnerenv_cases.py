# Pinned checks Python env tests (runnerenv) of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file,
# and pytest collects these tests under its name. Add new runnerenv tests here, not there.
from __future__ import annotations


def _fake_base(tmp_path, monkeypatch):
    """runnerenv with a stand-in base interpreter: its `-m venv` makes an env whose python runs this
    the real base one and whose `-m pip install` fails on a lock naming nosuchpkg; builds counts the
    venvs made."""
    from ttp import runnerenv
    real = shlex.quote(runnerenv._base_python())   # outside any venv: the user site beats system packages
    monkeypatch.delenv("TTP_TEST_CHECKS_PYTHON_ENV", raising=False)
    monkeypatch.setenv("TTP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runnerenv, "_ready", {})
    builds = tmp_path / "builds"
    py = shlex.quote(sys.executable)
    envpy = ('#!/bin/sh\nif [ "$1 $2" = "-m pip" ]; then\n'
             '  if grep -q nosuchpkg "$6"; then echo "ERROR: No matching distribution found for nosuchpkg==1" >&2; exit 1; fi\n'
             f'  exit 0\nfi\nexec {real} "$@"\n')
    base = tmp_path / "base-python"
    base.write_text('#!/bin/sh\nif [ "$1 $2" = "-m venv" ]; then\n'
                    f'  echo "$4" >> {shlex.quote(str(builds))}\n  mkdir -p "$4/bin"\n'
                    f'  cat > "$4/bin/python" <<\'EOF\'\n{envpy}EOF\n'
                    '  chmod +x "$4/bin/python"\n  ln -s python "$4/bin/python3"\n  exit 0\nfi\n'
                    f'exec {py} "$@"\n')
    base.chmod(0o755)
    monkeypatch.setattr(runnerenv, "_base_python", lambda: str(base))
    n = lambda: len(builds.read_text().splitlines()) if builds.exists() else 0
    return runnerenv, n


def test_runnerenv_reuses_an_env_and_a_lock_change_builds_beside_it(tmp_path, monkeypatch):
    runnerenv, builds = _fake_base(tmp_path, monkeypatch)
    lock = tmp_path / "a.lock"
    lock.write_text("pytest==8.4.2\n")
    first = runnerenv.ensure(lock)
    assert builds() == 1 and first["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert (runnerenv.root() / first["key"]).resolve() == Path(first["path"])
    runnerenv._ready.clear()   # a new process: the link is read, nothing is built
    assert runnerenv.ensure(lock)["path"] == first["path"] and builds() == 1
    lock.write_text("pytest==8.4.3\n")   # a new lock: a new env beside the old, which stays
    second = runnerenv.ensure(lock)
    assert builds() == 2 and second["key"] != first["key"] and Path(first["path"]).is_dir()
    assert "python env " + second["key"] in runnerenv.describe(second)


def test_runnerenv_rebuilds_a_broken_env_into_a_new_folder_and_moves_the_link(tmp_path, monkeypatch):
    runnerenv, builds = _fake_base(tmp_path, monkeypatch)
    lock = tmp_path / "a.lock"
    lock.write_text("pytest==8.4.2\n")
    old = runnerenv.ensure(lock)
    (Path(old["path"]) / "bin" / "python").unlink()   # broken: its python is gone
    runnerenv._ready.clear()
    replaced = []
    real = os.replace
    monkeypatch.setattr(runnerenv.os, "replace", lambda a, b: replaced.append((a, b)) or real(a, b))
    new = runnerenv.ensure(lock)
    assert builds() == 2 and new["path"] != old["path"] and Path(old["path"]).is_dir()
    assert replaced and Path(replaced[0][1]) == runnerenv.root() / old["key"], "the link moves in one rename"
    assert (runnerenv.root() / old["key"]).resolve() == Path(new["path"])


def test_runnerenv_builds_once_for_concurrent_callers(tmp_path, monkeypatch):
    import threading
    runnerenv, builds = _fake_base(tmp_path, monkeypatch)
    lock = tmp_path / "a.lock"
    lock.write_text("pytest==8.4.2\n")
    got, errs = [], []
    def go():
        try:
            got.append(runnerenv.ensure(lock)["path"])
        except Exception as e:   # noqa: BLE001 - reported below
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not errs and len(got) == 2 and len(set(got)) == 1 and builds() == 1


def test_runnerenv_fails_closed_on_an_unreadable_lock_or_a_failed_install(tmp_path, monkeypatch):
    runnerenv, builds = _fake_base(tmp_path, monkeypatch)
    with pytest.raises(runnerenv.Unavailable, match="cannot be read.*checks.python_lock"):
        runnerenv.ensure(tmp_path / "missing.lock")
    bad = tmp_path / "bad.lock"
    bad.write_text("--no-index\nnosuchpkg==1\n")
    with pytest.raises(runnerenv.Unavailable, match="nosuchpkg.*never fall back to ~/.local"):
        runnerenv.ensure(bad)
    home = runnerenv.root()
    assert builds() == 1 and not [q for q in home.iterdir() if not q.name.startswith(".")], \
        "no link and no half-built folder stay"
    assert not [q for q in home.iterdir() if q.name.endswith(".link")]


def test_runnerenv_lock_of_reads_a_relative_lock_in_the_checkout(tmp_path):
    from ttp import runnerenv
    assert runnerenv.lock_of({}) == runnerenv.LOCK == runnerenv.lock_of({"checks": {"python_lock": "  "}})
    assert runnerenv.lock_of({"checks": {"python_lock": "ci/py.lock"}}, tmp_path) == tmp_path / "ci" / "py.lock"
    assert runnerenv.lock_of({"checks": {"python_lock": "/x/py.lock"}}, tmp_path) == Path("/x/py.lock")
    assert runnerenv.LOCK.is_file() and "--hash=sha256:" in runnerenv.LOCK.read_text()


def test_check_env_turns_the_user_site_off_and_fronts_the_pinned_env(tmp_path, monkeypatch):
    from ttp import push
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    lock = tmp_path / "a.lock"
    lock.write_text("pytest==8.4.2\n")
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = push.check_env("task", "", str(tmp_path), lock=lock)
    info = runnerenv.ensure(lock)
    assert env["PYTHONNOUSERSITE"] == "1" and "PIP_USER" not in env and env["TTP_CHECKS_PYTHON_ENV"] == info["key"]
    assert env["PATH"].split(os.pathsep)[0] == str(Path(info["path"]) / "bin")


def test_ttp_checks_never_import_a_newer_pytest_from_the_user_site(env, tmp_path, monkeypatch, capsys):
    # A pytest 99.0 in the user site (as `pip install --user` puts it) is what python3 imports without
    # the pinned env; a `ttp checks` run never sees it.
    from ttp import cli
    from ttp import runnerenv
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    base = runnerenv._base_python()   # the env's python runs it (_fake_base)
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    userbase = tmp_path / "userbase"
    plain = {k: v for k, v in os.environ.items() if k not in ("PYTHONNOUSERSITE", "PYTHONPATH")}
    plain["PYTHONUSERBASE"] = str(userbase)
    site_dir = subprocess.run([base, "-c", "import site; print(site.getusersitepackages())"],
                              env=plain, capture_output=True, text=True, check=True).stdout.strip()
    (Path(site_dir) / "pytest").mkdir(parents=True)
    (Path(site_dir) / "pytest" / "__init__.py").write_text("__version__ = '99.0'\n")
    probe = "import importlib.util as u; s = u.find_spec('pytest'); print('PYTEST-AT', s and s.origin)"
    seen = subprocess.run([base, "-c", probe], env=plain, capture_output=True, text=True).stdout
    assert str(userbase) in seen, f"control: the fake user-site pytest loads without the env: {seen}"
    monkeypatch.setenv("PYTHONUSERBASE", str(userbase))
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    out = tmp_path / "out"
    cli.main(["checks", "--", "sh", "-c", f"python3 -c \"{probe}\" > {out}"])
    assert "PYTEST-AT" in out.read_text() and str(userbase) not in out.read_text()
    rec = json.loads((run / "checks.json").read_text())
    assert rec["python_env"]["key"] == runnerenv.ensure()["key"]
    assert "python env " in capsys.readouterr().out


def test_ttp_checks_and_the_push_queue_run_nothing_without_the_pinned_env(env, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from ttp import batch, cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    (repo / "py.lock").write_text("--no-index\nnosuchpkg==1\n")
    monkeypatch.setattr(runnerenv, "lock_of", lambda cfg, top=None: repo / "py.lock")
    ran = tmp_path / "ran"
    with pytest.raises(SystemExit) as ex:
        cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    assert ex.value.code == 1 and not ran.exists()
    assert "ttp checks: not run:" in capsys.readouterr().out
    # The push queue: the batch's check reports the env, runs nothing.
    monkeypatch.setattr(push, "version_guard", lambda d: False)
    monkeypatch.setattr(push, "applicable", lambda *a: (["touch " + str(ran)], []))
    me = SimpleNamespace(checks_at=None, _checkout=lambda h: None, d={}, wt=repo, tip="", checks={"runs": 0},
                         check_run_dir=str(run), branch="b", p=SimpleNamespace(config=lambda: {}))
    me._check_env = lambda: batch.Batch._check_env(me)
    got = batch.Batch._check(me, "a" * 40, ["touch " + str(ran)])
    assert got[0] == "python env" and "nosuchpkg" in got[1] and not ran.exists()


def test_runnerenv_base_python_is_outside_any_venv(tmp_path):
    # Made from a venv's python, the env would see that venv's packages (and before Python 3.11
    # sys._base_executable names the venv's own python).
    from ttp import runnerenv
    venv = tmp_path / "v"
    subprocess.run([runnerenv._base_python(), "-m", "venv", "--without-pip", str(venv)], check=True)
    code = "from ttp import runnerenv; print(runnerenv._base_python())"
    got = subprocess.run([str(venv / "bin" / "python"), "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(Path(runnerenv.__file__).parents[1])}).stdout.strip()
    assert got and not Path(os.path.abspath(got)).is_relative_to(os.path.abspath(venv)), got
    assert Path(got).resolve() == Path(runnerenv._base_python()).resolve()


def test_ttp_checks_freeze_lock_pins_the_user_sites_packages_the_lock_does_not_name(env, tmp_path, monkeypatch, capsys):
    # A project whose checks import packages from ~/.local pins them: the default pins (hashes dropped,
    # since pip's hash mode needs every line hashed), plus the user site's other packages at their
    # versions; a user-site pytest never replaces the lock's own. Nothing is installed.
    from ttp import cli, runnerenv
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    userbase = tmp_path / "userbase"
    plain = {k: v for k, v in os.environ.items() if k != "PYTHONNOUSERSITE"}
    site_dir = Path(subprocess.run([runnerenv._base_python(), "-c", "import site; print(site.getusersitepackages())"],
                                   env={**plain, "PYTHONUSERBASE": str(userbase)}, capture_output=True, text=True,
                                   check=True).stdout.strip())
    for name, ver in (("Some_Pkg", "1.2.3"), ("pytest", "99.0")):
        info = site_dir / f"{name}-{ver}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n")
    monkeypatch.setenv("PYTHONUSERBASE", str(userbase))
    out = tmp_path / "py.lock"
    with pytest.raises(SystemExit) as ex:
        cli.main(["checks", "--freeze-lock", str(out)])
    assert ex.value.code == 0
    lines = [ln for ln in out.read_text().splitlines() if not ln.startswith("#")]
    assert "Some_Pkg==1.2.3" in lines and "pytest==8.4.2" in lines and "pytest==99.0" not in lines
    assert "--hash" not in out.read_text() and "Some_Pkg==1.2.3" in capsys.readouterr().out
    assert not (run / "checks.json").exists(), "it runs no check"


def test_a_remote_check_runs_in_the_hosts_pinned_env_and_never_without_it(tmp_path):
    # A check on another host (delivery.checks_host, a spill host) makes the pinned env there from the
    # same lock and runs with the user site off; where it cannot be made the check does not run and
    # it is that host's fault (FAULT_RC), so the run goes again elsewhere.
    from ttp import remotecheck
    repo = _rc_repo(tmp_path)
    seen = tmp_path / "seen"
    cmd = f'printf "%s|%s|%s" "$PYTHONNOUSERSITE" "${{PIP_USER-unset}}" "${{PATH%%:*}}" > {seen}'
    rc, said = _rc_run(tmp_path, repo, cmd, PIP_USER="1")
    assert rc == 0, said
    assert seen.read_text() == f"1|unset|{os.environ['TTP_TEST_CHECKS_PYTHON_ENV']}/bin"
    assert "TTP_ENV_" not in said and _rc_left(tmp_path) == []
    seen.unlink()
    rc, said = _rc_run(tmp_path, repo, cmd, TTP_TEST_CHECKS_PYTHON_ENV=str(tmp_path / "none"))
    assert rc == remotecheck.FAULT_RC and not seen.exists(), said
    assert "pinned Python env could not be made there" in said and _rc_left(tmp_path) == []


def test_a_slurm_check_job_runs_in_the_nodes_pinned_env_and_is_a_fault_without_it(tmp_path):
    from ttp import slurmcheck
    repo = _rc_repo(tmp_path)
    seen = tmp_path / "seen"
    cmd = f'printf "%s|%s" "$PYTHONNOUSERSITE" "${{PATH%%:*}}" > {seen}'
    for name, stand_in, rc in (("ok", os.environ["TTP_TEST_CHECKS_PYTHON_ENV"], "0"), ("bad", str(tmp_path / "none"), "fault")):
        q = tmp_path / name
        q.mkdir()
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(q / "tree.tar"), "HEAD"], check=True)
        (q / "job.sh").write_text(slurmcheck.job_script(shlex.quote(str(q)), "", cmd))
        subprocess.run(["sh", str(q / "job.sh")], cwd=q, env={**os.environ, "TTP_TEST_CHECKS_PYTHON_ENV": stand_in},
                       capture_output=True, timeout=120)
        assert (q / "rc").read_text().strip() == rc
    assert seen.read_text() == f"1|{os.environ['TTP_TEST_CHECKS_PYTHON_ENV']}/bin"
