# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# The run-time Python judge (pyaudit.py, pyaudit/sitecustomize.py): a real `python -c` child with the judge's
# PYTHONPATH and policy, judging what the code does as it runs.


def _pa_env(cred_home, glm=False):
    """A run environment with the judge wired in (pyaudit.wire), as the daemon starts a run."""
    from ttp import pyaudit
    e = dict(os.environ, PATH="/usr/bin:/bin", TMPDIR=str(cred_home.run.parent / "tmp"))
    os.makedirs(e["TMPDIR"], exist_ok=True)
    if glm:
        e["TTP_GLM_RUN"] = "1"
    hook_argv = ["claude", "--settings", json.dumps({"env": {"PYTHONPATH": str(RUNTIME)}})]
    e["_argv"] = json.dumps(pyaudit.wire(e, hook_argv, str(RUNTIME), str(cred_home.work), str(cred_home.run), {}))
    return e


def _pa_run(e, code, cwd):
    return subprocess.run([sys.executable, "-c", code], env=e, cwd=cwd, capture_output=True, text=True, timeout=60)


def test_pyaudit_wire_puts_the_judge_first(env, cred_home):
    from ttp import pyaudit
    e = _pa_env(cred_home)
    zp = str(cred_home.run / "pyaudit" / "pyaudit.zip")
    assert e["PYTHONPATH"] == f"{zp}{os.pathsep}{RUNTIME}"
    assert f"{zp}{os.pathsep}{RUNTIME}" in json.loads(e["_argv"])[2]   # the hook sees what the run's commands see
    pol = json.loads(e[pyaudit.POLICY_ENV])
    assert any(r.endswith("/.ssh") for r in pol["roots"]) and "glm" not in pol


def test_pyaudit_refuses_writes_reads_and_children(env, cred_home):
    ssh = cred_home.home / ".ssh"
    key = ssh / "id_ed25519"
    key.write_text("secret")
    e = _pa_env(cred_home)   # credguard lists the keys there are when the run starts
    (cred_home.work / "f").write_text("x")
    for code in (f"open({str(ssh / 'new')!r}, 'w')",
                 f"import os; os.remove({str(key)!r})",
                 f"import os; os.rename({str(cred_home.work / 'f')!r}, {str(ssh / 'f')!r})",
                 f"import shutil; shutil.rmtree({str(ssh)!r})",
                 f"print(open({str(key)!r}).read())",
                 f"import subprocess; subprocess.run(['rm', '-f', {str(key)!r}])",
                 f"import os; os.system('rm -f {key}')",
                 # fork_exec straight: the wrapper judges it before the real one runs
                 f"import _posixsubprocess as p; p.fork_exec([b'rm', b'-f', {str(key).encode()!r}], [b'/bin/rm'],"
                 " True, (), None, None, -1, -1, -1, -1, -1, -1, -1, -1, False, False, None, None, None, -1, None)",
                 "import sys; del sys.modules['_posixsubprocess']; import _posixsubprocess",
                 "import ctypes; ctypes.CDLL('libc.so.6')",
                 "import os; os.putenv('PYTHONPATH', '')"):
        r = _pa_run(e, code, cred_home.work)
        assert r.returncode != 0 and "tt-project guard" in r.stderr, (code, r.stderr[-400:])
        assert "secret" not in r.stdout
    assert key.read_text() == "secret" and (cred_home.work / "f").exists() and not (ssh / "new").exists()
    log = (cred_home.run / "refusals.jsonl").read_text()
    assert '"kind": "pyaudit"' in log


def test_pyaudit_glm_shell_puts_the_judge_first_again(tmp_path, monkeypatch):
    """A GLM run's shell command (GuardedEnvironment) puts the judge's zip back first on PYTHONPATH."""
    from ttp import glmenv, pyaudit

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            return {"output": os.environ["PYTHONPATH"], "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    zp = str(tmp_path / "pyaudit.zip")
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv(pyaudit.POLICY_ENV, json.dumps({"zip": zp}))
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    out = env._guarded({"command": "true"}, str(tmp_path))
    assert out["returncode"] == 0 and out["output"] == zp + os.pathsep + "/elsewhere"
    pyaudit.first(os.environ)
    assert os.environ["PYTHONPATH"] == zp + os.pathsep + "/elsewhere"   # already first: unchanged


def test_pyaudit_later_hook_is_not_added(env, cred_home):
    r = _pa_run(_pa_env(cred_home), "import sys; sys.addaudithook(lambda e, a: print('ADDED') if e == 'open' "
                                    "else None); open('/dev/null')", cred_home.work)
    assert "ADDED" not in r.stdout


def test_pyaudit_allows_ordinary_work(env, cred_home):
    e = _pa_env(cred_home)
    code = ("import ctypes, json, os, subprocess, tempfile; open('out.txt', 'w').write('x');"
            "fd, p = tempfile.mkstemp(); os.write(fd, b'y'); os.close(fd); os.remove(p);"
            "print(subprocess.run(['echo', 'hi'], capture_output=True, text=True).stdout.strip());"
            "print(subprocess.run('ls out.txt', shell=True, capture_output=True, text=True).stdout.strip())")
    r = _pa_run(e, code, cred_home.work)
    assert r.returncode == 0 and r.stdout.split() == ["hi", "out.txt"], r.stderr[-400:]
    # a script is no inline code: the judge stays out of it
    (cred_home.work / "s.py").write_text("import ctypes; ctypes.CDLL('libc.so.6'); print('ok')")
    r = subprocess.run([sys.executable, "s.py"], env=e, cwd=cred_home.work, capture_output=True, text=True)
    assert r.stdout.strip() == "ok"


@pytest.mark.away_from_tmp
def test_pyaudit_glm_run_writes_only_in_its_roots(env, cred_home):
    e = _pa_env(cred_home, glm=True)
    assert "glm" in json.loads(e["TTP_PYAUDIT_POLICY"])
    out = cred_home.home / ".elsewhere"   # a hidden home entry: a shared temp dir (a RAM-disk suite's) is open to a write
    out.mkdir()
    r = _pa_run(e, f"open({str(out / 'x')!r}, 'w')", cred_home.work)
    assert "outside its worktree" in r.stderr and not (out / "x").exists()
    r = _pa_run(e, f"open('ok.txt', 'w'); open({str(cred_home.run / 'run.json')!r}, 'w')", cred_home.work)
    assert "guard's own files" in r.stderr and (cred_home.work / "ok.txt").exists()


def test_pyaudit_real_harm_stays_refused(env, cred_home):
    """Deleting the home folder or reading a credential through inline Python: refused as it runs, and by the
    shell guards (deletes and moves) whether the judge is wired or not."""
    from ttp import pathguard
    key = cred_home.home / ".ssh" / "id_ed25519"
    key.write_text("secret")
    (cred_home.home / "notes.txt").write_text("mine")
    e = _pa_env(cred_home)
    home = str(cred_home.home)
    for code in (f"import shutil; shutil.rmtree({home!r})",
                 f"import shutil; shutil.move({home!r}, {str(cred_home.work / 'gone')!r})",
                 f"print(open({str(key)!r}).read())"):
        r = _pa_run(e, code, cred_home.work)
        assert r.returncode != 0 and "tt-project guard" in r.stderr and "secret" not in r.stdout, code
    for cmd in (f"rm -rf {home}", f"mv {home} /tmp/gone"):
        for env_ in (e, dict(os.environ, PATH="/usr/bin:/bin")):
            assert pathguard.refusal({"tool_name": "Bash", "tool_input": {"command": cmd},
                                      "cwd": str(cred_home.work)}, env_), cmd
    assert key.read_text() == "secret" and (cred_home.home / "notes.txt").read_text() == "mine"


def test_pyaudit_closes_the_reviewed_bypasses(env, cred_home):
    """Each way around the judge the reviews found: refused by what the judge reads (never a crash of it), and
    ordinary relative-path work still runs."""
    from ttp import pyaudit
    ssh = cred_home.home / ".ssh"
    key = ssh / "id_ed25519"
    key.write_text("secret")
    e = _pa_env(cred_home)
    home = str(cred_home.home)
    for code in (f"import os; os.link({str(key)!r}, 'k2')",   # a new name for a credential
                 f"import subprocess; subprocess.run(['ls', '-rf', {home!r}], executable='/bin/rm')",   # real program
                 f"import subprocess; subprocess.run('rm -rf $D', shell=True, env={{'D': {home!r}}})",   # child env
                 f"import subprocess; subprocess.run(['sh', '-c', 'rm -rf $1', 'x', {home!r}])",   # positional args
                 f"import os; os.mkdir({str(ssh / 'd')!r}, dir_fd=-1)"):   # a dir_fd of -1 is the cwd
        r = _pa_run(e, code, cred_home.work)
        assert r.returncode != 0 and "tt-project guard" in r.stderr and "could not read" not in r.stderr, \
            (code, r.stderr[-400:])
        assert not (ssh / "d").exists(), (code, r.stderr)
    # a path type of the code's own runs no code inside the judge: refused closed
    r = _pa_run(e, f"class P:\n    def __fspath__(self): return {str(ssh / 'n')!r}\nopen(P(), 'w')", cred_home.work)
    assert r.returncode != 0 and "tt-project guard" in r.stderr
    r = _pa_run(e, f"import os, pathlib; pathlib.PurePath.__str__ = lambda s: {str(ssh / 'd')!r}; "
                   "os.mkdir(pathlib.Path('d'))", cred_home.work)   # os calls pass the judge the path object itself
    assert r.returncode != 0 and "tt-project guard" in r.stderr
    assert not (ssh / "d").exists(), r.stderr
    assert key.read_text() == "secret" and cred_home.home.is_dir() and not (ssh / "d").exists()
    r = _pa_run(e, "import os, pathlib, subprocess; os.mkdir('d'); os.rename('d', 'e'); open('e/f', 'w').write('x');"
                " os.link('e/f', 'g'); pathlib.Path('h').write_text('y'); subprocess.run(['ls'], cwd='e', check=True)",
                cred_home.work)
    assert r.returncode == 0, r.stderr[-400:]


def test_pyaudit_judges_the_computed_paths_the_static_proof_leaves_to_it(env, cred_home):
    """cells: audited_negatives. A computed path pyproof passes under the judge (a self-set TMPDIR, a joined
    credential path, the guard's own files) is refused as it runs; a plain computed one is written."""
    key = cred_home.home / ("." + "ssh") / ("id_" + "ed25519")
    key.write_text("secret")
    e = _pa_env(cred_home, glm=True)
    home = str(cred_home.home)
    for code in (f"import os; os.environ['TMPDIR']={home!r}; open(os.environ['TMPDIR']+'/.tt-project/x','a').write('x')",
                 f"p=''.join([{home!r}, '/.s', 'sh/id_', 'ed25519']); print(open(p).read())",
                 f"p=''.join([{str(cred_home.run)!r}, '/run', '.json']); open(p,'w')"):
        r = _pa_run(e, code, cred_home.work)
        assert r.returncode != 0 and "tt-project guard" in r.stderr and "secret" not in r.stdout, code
    assert not (cred_home.home / ".tt-project" / "x").exists()
    r = _pa_run(e, "p=''.join(['ok', '2.txt']); open(p,'w').write('x')", cred_home.work)
    assert r.returncode == 0 and (cred_home.work / "ok2.txt").exists(), r.stderr


def test_pyaudit_passes_the_remote_checks_own_ssh_and_still_refuses_credential_reads(env, cred_home):
    """A spilled check's ssh from a run: its script (the scratch HOME's list of credential stores it keeps out,
    its kill of the check's process group) and its cleanup's reap, which the guard refused in ssh's command
    line, go on stdin and pass; a credential read over ssh, or gh's token, is still refused."""
    stubs = cred_home.work / "bin"
    stubs.mkdir()
    for name, body in (("ssh", "cat >/dev/null; echo fed"), ("gh", "echo token")):
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    scratch = cred_home.work / "scratch-home"
    scratch.mkdir()
    e = _pa_env(cred_home)
    e.update(PATH=f"{stubs}:{e['PATH']}", HOME=str(scratch), TTP_REAL_HOME=str(cred_home.home),
             TTP_CHECKS_HOME_FILES='[".gitconfig"]')
    code = """if 1:
        import subprocess
        from ttp import orphanchecks, remotecheck as rc, scratchhome
        q = '/tmp/ttp-check.AAAAAAAA'
        script = rc._run_script(q, rc.exports({}) + scratchhome.remote_lines(rc.ENV_DIR, 'exit 95'), 'true', 'M:')
        assert '.git-credentials' in script and 'kill -TERM' in script
        for s in (script, orphanchecks.reap_script([q], 5)):
            try:
                subprocess.run(rc._ssh('spill', s))
                raise SystemExit('in the command line: passed')
            except PermissionError:
                pass
            argv, fed = rc._ssh_fed('spill', s)
            r = subprocess.run(argv, input=fed, capture_output=True, text=True)
            assert r.returncode == 0 and r.stdout == 'fed\\n', r
        print('ok')"""
    r = _pa_run(e, code, cred_home.work)
    assert r.returncode == 0 and r.stdout == "ok\n", r.stderr[-800:]
    for argv in (["ssh", "spill", "cat", "~/" + "." + "ssh/id_" + "ed25519"], ["gh", "auth", "token"]):
        r = _pa_run(e, f"import subprocess; print(subprocess.run({argv!r}, capture_output=True, text=True).stdout)",
                    cred_home.work)
        assert r.returncode != 0 and "tt-project guard" in r.stderr and "fed" not in r.stdout, (argv, r.stderr[-400:])
        assert "token" not in r.stdout


def test_pyaudit_on_the_glm_arm_passes_the_remote_checks_ssh_command_line(env, cred_home):
    """On the GLM arm the judge reads an ssh command's remote command too and refuses an expansion in it
    (`$( )`, `eval`): the spilled check's own ssh holds none, so its Popen is not refused."""
    stubs = cred_home.work / "bin"
    stubs.mkdir()
    (stubs / "ssh").write_text("#!/bin/sh\ncat >/dev/null; echo fed\n")
    (stubs / "ssh").chmod(0o755)
    e = _pa_env(cred_home, glm=True)
    e.update(PATH=f"{stubs}:{e['PATH']}")
    code = """if 1:
        import subprocess
        from ttp import remotecheck as rc
        argv, fed = rc._ssh_fed('spill', 'echo "$(date)"; eval "$X"')
        assert not any(c in a for a in argv for c in ('$', '`', 'eval')), argv
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        p.stdin.write(fed)
        p.stdin.close()
        assert p.stdout.read() == 'fed\\n'
        try:
            subprocess.run(['ssh', 'spill', 'sh -c \\'s=$(cat); eval "$s"\\''])
            raise SystemExit('an expansion in the command line: passed')
        except PermissionError:
            pass
        print('ok')"""
    r = _pa_run(e, code, cred_home.work)
    assert r.returncode == 0 and r.stdout == "ok\n", r.stderr[-800:]


def test_pyaudit_on_the_glm_arm_lets_a_whole_remote_check_run_on_its_host(env, cred_home, tmp_path):
    """Every command a remote check starts (login, tree copy, checkout, the check, cleanup) passes the GLM
    arm's guard: the check runs on the host, never refused before its ssh starts."""
    repo = _rc_repo(tmp_path)
    bin_, remote = _rc_fake_ssh(tmp_path)
    e = _pa_env(cred_home, glm=True)
    e.update(PATH=f"{bin_}:{e['PATH']}", FAKE_REMOTE_TMP=str(remote))
    code = """if 1:
        import sys
        from ttp import remotecheck
        sys.exit(remotecheck.main(['--host', 'box1', '--', 'echo ran-there']))"""
    r = _pa_run(e, code, repo)
    assert r.returncode == 0 and "ran-there" in r.stdout, (r.stdout + r.stderr)[-1500:]
    assert "tt-project guard" not in r.stdout + r.stderr and _rc_left(tmp_path) == []
