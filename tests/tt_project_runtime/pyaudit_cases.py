# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# The run-time Python judge (pyaudit.py, pyaudit/sitecustomize.py): a real `python -c` child with the judge's
# PYTHONPATH and policy, and the static relief the shell guards give the code such a child runs.


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
    assert e["PYTHONPATH"] == f"{zp}{os.pathsep}{RUNTIME}" and pyaudit.loaded(e)
    assert f"{zp}{os.pathsep}{RUNTIME}" in json.loads(e["_argv"])[2]   # the hook sees what the run's commands see
    pol = json.loads(e[pyaudit.POLICY_ENV])
    assert any(r.endswith("/.ssh") for r in pol["roots"]) and "glm" not in pol
    # a changed zip, or the zip not first, loads no judge the static relief can count on
    assert not pyaudit.loaded({**e, "PYTHONPATH": str(RUNTIME)})
    Path(zp).write_bytes(Path(zp).read_bytes() + b"x")
    assert not pyaudit.loaded(e)


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


def test_pyaudit_glm_run_writes_only_in_its_roots(env, cred_home):
    e = _pa_env(cred_home, glm=True)
    assert "glm" in json.loads(e["TTP_PYAUDIT_POLICY"])
    out = cred_home.run.parent / "elsewhere"
    out.mkdir()
    r = _pa_run(e, f"open({str(out / 'x')!r}, 'w')", cred_home.work)
    assert "outside its worktree" in r.stderr and not (out / "x").exists()
    r = _pa_run(e, f"open('ok.txt', 'w'); open({str(cred_home.run / 'run.json')!r}, 'w')", cred_home.work)
    assert "guard's own files" in r.stderr and (cred_home.work / "ok.txt").exists()


def test_pyaudit_relief_only_when_every_condition_holds(env, cred_home):
    from ttp import glmguard, pathguard, pyaudit
    e = _pa_env(cred_home)
    target = cred_home.home / ".ssh" / "x"
    code = f"open({str(target)!r}, 'w')".replace("'", '"')

    def refused(cmd, env_=e):
        return pathguard.refusal({"tool_name": "Bash", "tool_input": {"command": cmd},
                                  "cwd": str(cred_home.work)}, env_)
    plain = f"python3 -c '{code}'"
    assert refused(plain, dict(os.environ, PATH="/usr/bin:/bin"))   # no judge: the shell guard reads the code
    assert refused(plain) is None and pyaudit.relieve(plain, e) == "python3 -c 'pass'"
    assert refused(f"cd /x && timeout 20 python3 -u - <<'EOF'\n{code}\nEOF") is None
    for cmd in (f"python3 -I -c '{code}'", f"python3 -E -c '{code}'", f"python3 -S -c '{code}'",
                f"env -i python3 -c '{code}'", f"PYTHONPATH= python3 -c '{code}'",
                f"python3 -c 'import ctypes; {code}'", f"python3 -c 'exec(\"{code}\")'",
                f"/usr/bin/python3 -c '{code}'", f"perl -e 'open(F, \">{target}\")'",
                f"node -e 'require(\"fs\").writeFileSync(\"{target}\", \"\")'",
                f"echo \"x; python3 -c 'a\" > {target}; echo \"'\"",   # the code a quoted string only seems to hold
                "python3 -c \"$(echo x); open(" + repr(str(target)) + ", 'w')\"",   # a command the shell runs
                f"python3 - <<EOF\n{code}; $X\nEOF"):   # a heredoc body the shell expands
        assert refused(cmd), cmd
    for cmd in ('python3 -c "$X"', "python3 - <<EOF\nprint(1); $X\nEOF"):   # code all expansion; a heredoc's
        assert pyaudit.relieve(cmd, e) == cmd, cmd
    assert refused(plain, {**e, "PATH": "bin:/usr/bin"})   # a relative PATH entry
    # a .pth newer than the judge's policy runs before it: no relief
    site = cred_home.home / ".local" / "lib" / f"python3.{sys.version_info[1]}" / "site-packages"
    site.mkdir(parents=True)
    (site / "x.pth").write_text("")
    if os.path.realpath(shutil.which("python3", path="/usr/bin")).endswith(f"python3.{sys.version_info[1]}"):
        assert refused(plain)
    # the GLM arm's shell guard gives the same relief
    g = _pa_env(cred_home, glm=True)
    db = f"python3 -c 'import sqlite3; sqlite3.connect(\"{cred_home.home}/project.db\")'"
    assert glmguard.shell_refusal(db, str(cred_home.work), str(cred_home.run), {**g, "PATH": "/usr/bin:/bin"},
                                  made=[]) is None


def test_pyaudit_screens_code_shell_expansions_give_as_it_starts(env, cred_home):
    from ttp import glmguard, pathguard, pyaudit
    if sys.version_info < (3, 10) or not os.path.realpath(shutil.which("python3", path="/usr/bin") or "").endswith(
            f"python3.{sys.version_info[1]}"):
        pytest.skip("needs the system python3 3.10+ as the test's interpreter")
    e = _pa_env(cred_home)
    cmd = ("for d in 1 2; do f=runs/$d/result.json; if [ -f \"$f\" ]; then python3 -c "
           "\"import json; print(json.load(open('$f')))\"; fi; done")
    bare = dict(os.environ, PATH="/usr/bin:/bin", TTP_GLM_RUN="1")
    assert glmguard.shell_refusal(cmd, str(cred_home.work), str(cred_home.run), bare, made=[])
    assert glmguard.shell_refusal(cmd, str(cred_home.work), str(cred_home.run), {**e, "TTP_GLM_RUN": "1"},
                                  made=[]) is None
    assert pyaudit.relieve(cmd, e).count("-c 'pass'") == 1
    shapes = cred_home.run / "pyaudit" / "expanded.jsonl"
    assert [json.loads(x) for x in shapes.read_text().splitlines()] == [["import json; print(json.load(open('",
                                                                         "')))"]]
    pyaudit.relieve(cmd, e)
    assert len(shapes.read_text().splitlines()) == 1   # recorded once
    # what the expansion gives is screened as the Python starts: a word that reaches past the judge stops it
    (cred_home.work / "runs" / "1").mkdir(parents=True)
    (cred_home.work / "runs" / "1" / "result.json").write_text("{}")
    for val, ok in (("runs/1/result.json", True), ("x')); import ctypes; print(('", False),
                    ("x')).__class__.__subclasses__(); print(('", False)):
        r = _pa_run(e, f"import json; print(json.load(open('{val}')))", cred_home.work)
        assert (r.returncode == 0) == ok and ("tt-project guard" in r.stderr) != ok, (val, r.stderr[-300:])
    # the same words in code of another shape, which the shell guards read in full: the judge lets it start
    r = _pa_run(e, "import ctypes; print('x')", cred_home.work)
    assert r.returncode == 0 and r.stdout.strip() == "x"
    assert pathguard.refusal({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cred_home.work)},
                             e) is None


def test_pyaudit_real_harm_stays_refused(env, cred_home):
    """Deleting the home folder or reading a credential through inline Python: refused as it runs, and by the
    shell guards (deletes and moves) whether the static relief is on or off."""
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
    r = _pa_run(e, f"import os, pathlib; pathlib.PurePath.__str__ = lambda s: {str(ssh / 'd')!r}; os.mkdir(pathlib.Path('d'))",
                cred_home.work)   # os calls pass the judge the path object itself
    assert r.returncode != 0 and "tt-project guard" in r.stderr
    assert not (ssh / "d").exists(), r.stderr
    assert key.read_text() == "secret" and cred_home.home.is_dir() and not (ssh / "d").exists()
    r = _pa_run(e, "import os, pathlib, subprocess; os.mkdir('d'); os.rename('d', 'e'); open('e/f', 'w').write('x');"
                " os.link('e/f', 'g'); pathlib.Path('h').write_text('y'); subprocess.run(['ls'], cwd='e', check=True)",
                cred_home.work)
    assert r.returncode == 0, r.stderr[-400:]
    # what the static relief leaves to the shell guards: a dir_fd, inspect, a changed HOME, a word that goes on
    for cmd in ("python3 -c 'import os; os.open(\"k\", os.O_WRONLY, dir_fd=3)'", "python3 -c 'import inspect'",
                "HOME=/tmp python3 -c 'print(1)'", "python3 -c 'print(1)'x"):
        assert pyaudit.relieve(cmd, e) == cmd, cmd
