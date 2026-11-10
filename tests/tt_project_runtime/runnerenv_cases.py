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
    # An env the host cannot make is the host's fault, never the change's: nothing runs, ttp checks
    # waits on an env-fault marker (75), the push queue's batch is held (envfault.Fault), not failed.
    from types import SimpleNamespace
    from ttp import batch, cli, envfault, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    bad = tmp_path / "py.lock"   # outside the checkout: no commit changed it
    bad.write_text("--no-index\nnosuchpkg==1\n")
    monkeypatch.setattr(runnerenv, "lock_of", lambda cfg, top=None: bad)
    monkeypatch.setattr(runnerenv, "ready", lambda lock: False)   # no background build here
    ran = tmp_path / "ran"
    with pytest.raises(SystemExit) as ex:
        cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    assert ex.value.code == 75 and not ran.exists()
    assert "ttp checks: not run:" in capsys.readouterr().out
    mark = json.loads((run / envfault.MARKER).read_text())
    assert mark["kind"] == "pyenv" and mark["sig"].startswith(runnerenv.FAULT) and "nosuchpkg" in mark["why"]
    assert mark["sig"] in envfault.open_faults(p), "recorded, so `ttp checks --env-ok` waits on it"
    assert cli._checks_env_ok(p, [mark["sig"]]) == 1
    # The push queue: the batch's check raises the host fault, runs nothing.
    monkeypatch.setattr(push, "version_guard", lambda d: False)
    monkeypatch.setattr(push, "applicable", lambda *a: (["touch " + str(ran)], []))
    me = SimpleNamespace(checks_at=None, _checkout=lambda h: None, d={}, wt=repo, tip="", checks={"runs": 0},
                         check_run_dir=str(run), branch="b", p=SimpleNamespace(config=lambda: {}))
    me._check_env = lambda head="": batch.Batch._check_env(me, head)
    with pytest.raises(envfault.Fault) as fe:
        batch.Batch._check(me, "a" * 40, ["touch " + str(ran)])
    assert fe.value.fault["kind"] == "pyenv" and not ran.exists()
    # ... and ttp land and ttp push wait (BUSY), not CHECKS_FAILED.
    said = []
    assert push.env_wait(None, repo, "", "HEAD", runnerenv.Unavailable("x"), said.append) == push.BUSY
    assert "environment fault " + runnerenv.FAULT in said[0]


def test_a_lock_the_checked_commit_changed_fails_it_when_its_env_cannot_be_made(env, tmp_path, monkeypatch, capsys):
    # The one exception: a checks.python_lock in the checkout that the change itself edited.
    from types import SimpleNamespace
    from ttp import batch, cli, envfault, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "py.lock").write_text("--no-index\nnosuchpkg==1\n")
    subprocess.run([*git, "add", "py.lock"], check=True)
    subprocess.run([*git, "commit", "-qm", "lock"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cfg = {"checks": {"python_lock": "py.lock"}}
    assert runnerenv.changed_by(repo, cfg, base, head) and not runnerenv.changed_by(repo, cfg, head, head)
    assert not runnerenv.changed_by(repo, {}, base, head), "the plugin's own lock is never the change's"
    monkeypatch.setattr(runnerenv, "lock_of", lambda c, top=None: Path(top or ".") / "py.lock")
    ran = tmp_path / "ran"
    with pytest.raises(SystemExit) as ex:
        cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    assert ex.value.code == 1 and not ran.exists() and not (run / envfault.MARKER).exists()
    monkeypatch.setattr(push, "version_guard", lambda d: False)
    monkeypatch.setattr(push, "applicable", lambda *a: (["touch " + str(ran)], []))
    me = SimpleNamespace(checks_at=None, _checkout=lambda h: None, d={}, wt=repo, tip=base, checks={"runs": 0},
                         check_run_dir=str(run), branch="b", p=SimpleNamespace(config=lambda: cfg))
    me._check_env = lambda head="": batch.Batch._check_env(me, head)
    got = batch.Batch._check(me, head, ["touch " + str(ran)])
    assert got[0] == "python env" and "nosuchpkg" in got[1] and not ran.exists()


def test_ttp_checks_builds_the_env_from_the_checked_commits_lock_not_the_worktrees(env, tmp_path, monkeypatch, capsys):
    # sa44: the worktree's uncommitted lock edit is not the commit's; the env comes from the commit's.
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    runnerenv, _ = _fake_base(tmp_path, monkeypatch)
    (repo / "py.lock").write_text("# nothing pinned\n")
    subprocess.run([*git, "add", "py.lock"], check=True)
    subprocess.run([*git, "commit", "-qm", "lock"], check=True)
    (repo / "py.lock").write_text("--no-index\nnosuchpkg==1\n")   # uncommitted
    monkeypatch.setattr(runnerenv, "lock_of", lambda c, top=None: Path(top or ".") / "py.lock")
    ran = tmp_path / "ran"
    with pytest.raises(SystemExit):   # the dirty tree is refused, after the env is made from the commit's lock
        cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    out = capsys.readouterr().out
    want = hashlib.sha256(b"# nothing pinned\n").hexdigest()[:12]
    assert "not run:" not in out and f"lock {want} from {run}/" in out, out


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
    assert "# requires-python: >=3.10" in out.read_text().splitlines(), "the frozen lock keeps the Python it needs"
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
    nsite, pip_user, first = seen.read_text().split("|")
    assert (nsite, pip_user) == ("1", "unset")
    assert os.path.realpath(first) == os.path.realpath(os.environ["TTP_TEST_CHECKS_PYTHON_ENV"] + "/bin")
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
    for name, stand_in, rc in (("ok", os.environ["TTP_TEST_CHECKS_PYTHON_ENV"], "0"), ("bad", str(tmp_path / "none"), slurmcheck.NO_ENV)):
        q = tmp_path / name
        q.mkdir()
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(q / "tree.tar"), "HEAD"], check=True)
        (q / "job.sh").write_text(slurmcheck.job_script(shlex.quote(str(q)), "", cmd))
        subprocess.run(["sh", str(q / "job.sh")], cwd=q, env={**os.environ, "TTP_TEST_CHECKS_PYTHON_ENV": stand_in},
                       capture_output=True, timeout=120)
        assert (q / "rc").read_text().strip() == rc
    nsite, first = seen.read_text().split("|")
    assert nsite == "1"
    assert os.path.realpath(first) == os.path.realpath(os.environ["TTP_TEST_CHECKS_PYTHON_ENV"] + "/bin")


def test_a_pinned_env_fault_closes_once_its_env_is_made_or_its_lock_is_gone(tmp_path, monkeypatch):
    from ttp import envfault, runnerenv
    lock = tmp_path / "py.lock"
    lock.write_text("# nothing pinned\n")
    f = runnerenv.fault(lock, "pip failed")
    assert f["sig"] == runnerenv.FAULT + hashlib.sha256(lock.read_bytes()).hexdigest()[:12]
    assert "pinned Python env cannot be made" in envfault.line(f)
    assert envfault._still(f) is None, "the suite's ready stand-in env: made"
    monkeypatch.setenv("TTP_TEST_CHECKS_PYTHON_ENV", str(tmp_path / "none"))
    assert envfault._still(f) == f, "not made: still open, the probe never waits for a build"
    lock.unlink()
    assert envfault._still(f) is None


def test_runnerenv_key_skips_a_lock_line_for_another_platform_so_its_env_is_reused(tmp_path, monkeypatch):
    # PyYAML is pinned for macOS only (its Pythons ship none; Linux distributions' do): adding that
    # line must not change a Linux host's key, or a lab box with no package index could never rebuild.
    from ttp import runnerenv
    base = "pytest==8.4.2 --hash=sha256:aa\n# a comment; sys_platform == \"darwin\"\nexceptiongroup==1.3.1 ; python_version < \"3.11\"\n"
    mac = ('pyyaml==6.0.3 ; sys_platform == "darwin" \\\n    --hash=sha256:bb \\\n    --hash=sha256:cc\n'
           "colorama==0.4.6 ; sys_platform=='win32' --hash=sha256:dd\n")
    both = "pyyaml==6.0.3 ; sys_platform == \"darwin\" and python_version >= \"3.9\"\n"
    notmac = "pyyaml==6.0.3 ; sys_platform != 'darwin'\n"
    for host, kept in (("linux", ""), ("darwin", mac.split("colorama")[0])):
        monkeypatch.setattr(runnerenv.sys, "platform", host)
        assert runnerenv._for_host((base + mac).encode()) == (base + kept).encode(), host
        assert (runnerenv.key_of((base + mac).encode(), "/usr/bin/python3")
                == runnerenv.key_of(base.encode(), "/usr/bin/python3")) == (host == "linux")
        assert runnerenv._for_host((base + both).encode()) == (base + both).encode(), "a compound marker always counts"
        assert runnerenv._for_host((base + notmac).encode()) == (base + notmac * (host == "linux")).encode()
    # The shipped lock: on macOS it pins PyYAML (the smoke's yaml-importing tests collect there), with a
    # hash for each macOS CPython wheel; on Linux it reads exactly as without that line.
    text = runnerenv.LOCK.read_bytes()
    monkeypatch.setattr(runnerenv.sys, "platform", "darwin")
    yaml_line = [ln for ln in runnerenv._for_host(text).decode().splitlines() if ln.startswith("pyyaml==")]
    assert len(yaml_line) == 1 and 'sys_platform == "darwin"' in yaml_line[0] and yaml_line[0].count("--hash=sha256:") >= 2
    monkeypatch.setattr(runnerenv.sys, "platform", "linux")
    assert b"pyyaml" not in runnerenv._for_host(text).lower()
    req = b"# requires-python: >=3.10\n"   # picks the interpreter, which the key names itself
    assert req in text and runnerenv._for_host(text) == text.replace((yaml_line[0] + "\n").encode(), b"").replace(req, b"")


def test_runnerenv_makes_the_env_from_a_python_the_locks_requires_python_allows(tmp_path, monkeypatch):
    # ttp under macOS's /usr/bin/python3 (3.9) made the default lock's env with it, and pip found no
    # iniconfig 2.3.1 (it needs 3.10): the env is made from a Python that meets the lock's
    # requires-python line instead, keyed by that Python, or nothing runs and the error names both.
    runnerenv, builds = _fake_base(tmp_path, monkeypatch)
    monkeypatch.setattr(runnerenv, "_found", {})
    monkeypatch.setattr(runnerenv, "_PY_DIRS", ())
    assert runnerenv.requires(runnerenv.LOCK.read_bytes()) == [(">=", (3, 10))]
    assert runnerenv.requires(b"# Requires-Python:>=3.10, <3.14\npytest==1\n") == [(">=", (3, 10)), ("<", (3, 14))]
    assert runnerenv.requires(b"pytest==1\n# requires-python is read by runnerenv\n") is None
    for bad in (b"# requires-python: 3.10\n", b"# requires-python: >=3.10 (pluggy)\n", b"# requires-python:\n"):
        with pytest.raises(runnerenv.Unavailable, match="requires-python line"):
            runnerenv.requires(bad)
    meets = runnerenv._meets
    assert meets((3, 10, 0), [(">=", (3, 10))]) and not meets((3, 9, 6), [(">=", (3, 10))])
    assert meets((3, 10, 4), [("==", (3, 10))]) and not meets((3, 11, 0), [("==", (3, 10))])
    assert meets((3, 10, 5), [(">", (3, 10))]) and not meets((3, 13, 1), [("<", (3, 13))])
    assert not meets((3, 12, 4), [("<=", (3, 12))]) and meets((3, 13, 0), [(">=", (3, 10)), ("!=", (3, 12))])
    # Candidates: a PATH folder with a non-Python, a Python too old and one that meets the lock (its
    # base is the stand-in base, so its env builds as _fake_base's do).
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake_base = runnerenv._base_python()
    for name, body in (("python3", "exit 1"),
                       ("python3.41", f'echo \'["{tmp_path}/py41", "41.0.0 (old)", [41, 0, 0]]\''),
                       ("python3.17", f'echo \'["{fake_base}", "42.1.0 (new)", [42, 1, 0]]\'')):
        (bin_ / name).write_text(f'#!/bin/sh\n[ "$3" = --interpreter ] || exit 2\n{body}\n')
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    lock = tmp_path / "a.lock"
    lock.write_text("# requires-python: >=42\npytest==1\n")
    assert runnerenv.interpreter(lock.read_bytes()) == (fake_base, "42.1.0 (new)")
    info = runnerenv.ensure(lock)
    assert builds() == 1 and info["key"] == runnerenv.key_of(lock.read_bytes(), fake_base, "42.1.0 (new)")
    assert info["key"] != runnerenv.key_of(lock.read_bytes(), fake_base), "the key names the Python it was made from"
    runnerenv._ready.clear()
    assert runnerenv.ensure(lock)["key"] == info["key"] and builds() == 1 and runnerenv.ready(lock)
    # None meets it: nothing is built and the error names the Python needed and the one found.
    lock.write_text("# requires-python: >=43\npytest==1\n")
    with pytest.raises(runnerenv.Unavailable, match=r"needs Python >=43.*runs under Python \d+\.\d+.*install a Python >=43"):
        runnerenv.ensure(lock)
    assert builds() == 1 and not runnerenv.ready(lock)
    # A lock without the line, or one this Python meets, keeps this Python and its key as before.
    lock.write_text(f"# requires-python: >={sys.version_info[0]}.{sys.version_info[1]}\npytest==1\n")
    assert runnerenv.interpreter(lock.read_bytes()) == (fake_base, sys.version)
    assert runnerenv.key_of(lock.read_bytes(), fake_base) == runnerenv.key_of(b"pytest==1\n", fake_base)


def test_runnerenv_probes_a_real_python_for_its_base_and_version(tmp_path, monkeypatch):
    from ttp import runnerenv
    monkeypatch.setattr(runnerenv, "_found", {})
    base, version, info = runnerenv._probe(sys.executable)
    assert info == tuple(sys.version_info[:3]) and version == sys.version
    assert Path(base).resolve() == Path(runnerenv._base_python()).resolve(), "a venv's python gives its base"
    assert runnerenv._probe(str(tmp_path / "nope")) is None
