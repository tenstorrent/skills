# Harness scripts replaced while a shell runs them, and checks that fail on a transient syntax error in a
# script outside the change (syntaxfault) tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _torn_line(path) -> str:
    return f"{path}: line 3: syntax error near unexpected token `('\n"


def _torn_check(d, runs: list[str]):
    """A check command whose n-th run prints runs[n] (the last one repeats): '' exits 0, anything else
    exits 2. Returns its argv and how many times it ran."""
    for i, text in enumerate(runs):
        (d / f"torn{i}.log").write_text(text)
    count, script = d / "torn-count", d / "torn-check.sh"
    script.write_text(f"""n=$(cat {count} 2>/dev/null || echo 0)
echo $((n + 1)) > {count}
[ "$n" -gt {len(runs) - 1} ] && n={len(runs) - 1}
f={d}/torn$n.log
[ -s "$f" ] || exit 0
cat "$f"
exit 2
""")
    return ["sh", str(script)], lambda: int(count.read_text()) if count.exists() else 0


def _torn_repo(d):
    """A repository whose HEAD changes in.sh over its base: (repo, base, a script outside it that parses,
    one outside it that does not)."""
    repo = d / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for f in ("in.sh", "same.sh"):
        (repo / f).write_text("echo a\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "in.sh").write_text("echo b\n")
    subprocess.run([*git, "commit", "-qam", "change"], check=True)
    harness = d / "harness" / "bin"
    harness.mkdir(parents=True)
    whole, broken = harness / "tool", harness / "broken"
    whole.write_text("#!/bin/bash\nf() { echo ok; }\nf\n")
    broken.write_text("#!/bin/bash\nf() { echo ok;\n")
    return repo, base, whole, broken


def _outside_temp(monkeypatch, settle: float = 0):
    """The test folders count as outside every temporary folder; no settle before a rerun."""
    from ttp import syntaxfault
    monkeypatch.setattr(syntaxfault, "_temp_roots", lambda: [])
    monkeypatch.setattr(syntaxfault, "SETTLE_S", settle)


def test_syntaxfault_names_only_transient_syntax_errors_outside_the_change(tmp_path, monkeypatch):
    from ttp import syntaxfault
    repo, base, whole, broken = _torn_repo(tmp_path)
    # Under a temporary folder (a test's own fixture): never transient.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert syntaxfault.transient(_torn_line(whole), repo, base) == []
    _outside_temp(monkeypatch)
    t = syntaxfault.transient
    assert t(_torn_line(whole), repo, base) == [str(whole)]
    assert t("[shard 2] " + _torn_line(whole) + _torn_line(whole), repo, base) == [str(whole)]   # prefixed, once
    assert t(f'{whole}: 131: Syntax error: "(" unexpected\n', repo, base) == [str(whole)]          # dash
    assert t(_torn_line("same.sh"), repo, base) == ["same.sh"], "in the repository, untouched by the change"
    assert t(_torn_line("in.sh"), repo, base) == [], "a file the change touches: a real failure"
    assert t(_torn_line(repo / "in.sh"), repo, base) == []
    assert t(_torn_line(whole) + _torn_line("in.sh"), repo, base) == [], "one inside the change fails it all"
    assert t(_torn_line(broken), repo, base) == [], "still broken: a real failure"
    assert t(_torn_line(tmp_path / "gone.sh"), repo, base) == []
    assert t(_torn_line("same.sh"), repo, None) == [], "no base: every file of the repository counts as changed"
    assert t(_torn_line(whole), repo, None) == [str(whole)]
    assert t("bash: -c: line 1: syntax error near unexpected token `('\n", repo, base) == []
    assert t("1 failed, 3 passed\n", repo, base) == []
    (repo / "sub").mkdir()
    (repo / "sub" / "same.sh").write_text("echo a\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "sub"], check=True)
    assert t(_torn_line("same.sh"), repo, base) == [], "a relative name a changed file has, run from a subfolder"
    link = repo / "link.sh"
    link.symlink_to(repo / "in.sh")
    assert t(_torn_line(link), repo, base) == [], "a link to a changed file is that file"


def test_syntaxfault_judges_each_script_with_the_shell_that_reported_it(tmp_path, monkeypatch):
    """A bash-only script run with sh fails under dash every time, though `bash -n` passes it: dash's error
    format is judged with dash, bash's with bash, and a missing shell judges nothing transient."""
    import shutil
    from ttp import push, syntaxfault
    if not shutil.which("dash") or not shutil.which("bash"):
        pytest.skip("needs dash and bash")
    repo, base, whole, _ = _torn_repo(tmp_path)
    _outside_temp(monkeypatch)
    bashonly = whole.parent / "bashonly"
    bashonly.write_text("a=(1 2)\necho ${a[0]}\n")
    real = subprocess.run(["dash", str(bashonly)], capture_output=True, text=True)
    assert real.returncode != 0 and f"{bashonly}: 1: Syntax error" in real.stderr, real
    t = syntaxfault.transient
    assert t(real.stderr, repo, base) == [], "dash's error, still broken under dash: a real failure"
    assert t(_torn_line(bashonly), repo, base) == [str(bashonly)], "bash's error: bash parses it now"
    assert t(_torn_line(bashonly) + real.stderr, repo, base) == [], "named by both: each shell must pass it"
    assert t(_torn_line(whole) + real.stderr, repo, base) == [], "one script that fails its shell fails it all"
    which = shutil.which
    monkeypatch.setattr(syntaxfault.shutil, "which", lambda c, *a, **k: None if c == "dash" else which(c, *a, **k))
    assert t(f'{whole}: 3: Syntax error: "(" unexpected\n', repo, base) == [], "no dash here: never judged"
    assert t(_torn_line(whole), repo, base) == [str(whole)]
    monkeypatch.setattr(syntaxfault.shutil, "which", which)
    # The push queue: such a check fails after one run, never rerun or held as infra.
    said = []
    env = {**os.environ, "TTP_PUSH_TIP": base}
    assert push._run_one(f"sh {bashonly}", repo, env, said.append) == push.CHECKS_FAILED, said
    assert not any("running it once more" in s or "infrastructure fault" in s for s in said), said


def test_ttp_replacing_a_harness_script_mid_run_never_tears_it(tmp_path):
    """A shell reads a script a piece at a time: an in-place rewrite as it runs tears it, while ttp's own
    replacement (setup and upgrade copy harness trees with _copy_tree) leaves it the old file to the end."""
    from ttp import cli

    def run(replace) -> subprocess.CompletedProcess:
        bin_, src = tmp_path / replace.__name__ / "bin", tmp_path / replace.__name__ / "src"
        bin_.mkdir(parents=True)
        src.mkdir()
        go = tmp_path / replace.__name__ / "go"
        old = f"#!/bin/bash\nwhile [ ! -e {go} ]; do sleep 0.02; done\necho old-end\n"
        new = "#!/bin/bash\n" + "".join(f"echo pad{i} \"$(printf '(%s)' {i})\"\n" for i in range(40)) + "echo new-end\n"
        (bin_ / "tool").write_text(old)
        (bin_ / "tool").chmod(0o755)
        (src / "tool").write_text(new)
        (src / "tool").chmod(0o755)
        proc = subprocess.Popen([str(bin_ / "tool")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.3)   # the shell is in its wait loop, the rest of the file not read yet
        replace(src, bin_)
        go.touch()
        out, _ = proc.communicate(timeout=30)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out)

    def in_place(src, bin_):
        with open(bin_ / "tool", "r+") as f:
            f.write((src / "tool").read_text())

    def by_ttp(src, bin_):
        cli._copy_tree(src, bin_)

    torn = run(in_place)
    assert "old-end" not in torn.stdout, torn.stdout   # the control: an in-place rewrite does change what runs
    whole = run(by_ttp)
    assert whole.returncode == 0 and whole.stdout == "old-end\n", whole.stdout
    assert os.access(tmp_path / "by_ttp" / "bin" / "tool", os.X_OK)
    assert "new-end" in (tmp_path / "by_ttp" / "bin" / "tool").read_text()


def test_a_new_project_writes_no_harness_file_in_place(env, monkeypatch):
    from ttp import cli
    import pathlib
    import shutil as sh
    harness, in_place = [], []
    real_text, real_copy2 = pathlib.Path.write_text, sh.copy2

    def write_text(self, *a, **k):
        if harness and pathlib.Path(self).resolve().is_relative_to(harness[0]):
            in_place.append(str(self))
        return real_text(self, *a, **k)

    def copy2(src, dst, *a, **k):
        if harness and pathlib.Path(dst).resolve().is_relative_to(harness[0]):
            in_place.append(str(dst))
        return real_copy2(src, dst, *a, **k)

    real_project = cli.Project

    def project(root):
        p = real_project(root)
        harness[:] = [p.harness.resolve()]
        return p

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    monkeypatch.setattr(sh, "copy2", copy2)
    monkeypatch.setattr(cli, "Project", project)
    p = cli.bootstrap(env["repo"], "atomicharness", MAKE_BRIEF, "fake")
    assert harness and in_place == [], in_place
    assert (p.harness / ".gitignore").read_text() == "__pycache__/\n*.pyc\n"
    assert (p.harness / "pytest.ini").is_file()
    assert (p.harness / "runtime" / "ttp" / cli.SOURCE_FILE).read_text().strip()


def test_push_run_one_reruns_a_transient_syntax_error_once_and_holds_a_second(tmp_path, monkeypatch):
    """`ttp push` (push._run_one): a check failing on a syntax error in a script outside the change that
    parses now runs once more; failing so again, it is the host (BUSY with a retry_when), never
    CHECKS_FAILED on a head that was never tested. A syntax error in a file the change touches, or in a
    script still broken, fails at once."""
    from ttp import push, syntaxfault
    repo, base, whole, broken = _torn_repo(tmp_path)
    _outside_temp(monkeypatch)
    cases = {"pass": ([_torn_line(whole), ""], 0, 2),
             "infra": ([_torn_line(whole)], push.BUSY, 2),
             "then_real": ([_torn_line(whole), "1 failed\n"], push.CHECKS_FAILED, 2),
             "inside": ([_torn_line(repo / "in.sh")], push.CHECKS_FAILED, 1),
             "broken": ([_torn_line(broken)], push.CHECKS_FAILED, 1)}
    for name, (runs, want, n) in cases.items():
        d = tmp_path / name
        d.mkdir()
        argv, ran = _torn_check(d, runs)
        said = []
        assert push._run_one(" ".join(argv), repo, {**os.environ, "TTP_PUSH_TIP": base}, said.append) == want, (name, said)
        assert ran() == n, (name, said)
        assert any("running it once more" in s for s in said) == (n == 2), (name, said)
        infra = [s for s in said if "an infrastructure fault, not the change" in s]
        assert bool(infra) == (name == "infra"), (name, said)
        if infra:
            assert "; not pushing. Hand off `waiting`; retry_when: test \"$(date +%s)\" -ge " in infra[0], infra
            assert syntaxfault.RETRY_S > 0


def test_ttp_checks_reruns_a_transient_syntax_error_once_and_passes(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (tmp_path / "x").mkdir()
    _, _, whole, _ = _torn_repo(tmp_path / "x")
    _outside_temp(monkeypatch)
    argv, ran = _torn_check(tmp_path, [_torn_line(whole), ""])
    cli.main(["checks", "--", *argv])
    out = capsys.readouterr().out
    assert ran() == 2 and out.count("running it once more") == 1, out
    assert json.loads((run / "checks.json").read_text())["passed"] is True


def test_ttp_checks_a_transient_syntax_error_again_is_an_infra_fault_exit_75(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, syntaxfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (tmp_path / "x").mkdir()
    _, _, whole, broken = _torn_repo(tmp_path / "x")
    _outside_temp(monkeypatch)
    argv, ran = _torn_check(tmp_path, [_torn_line(whole)])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code == 75 and ran() == 2, "one rerun, never a loop"
    cap = capsys.readouterr()
    assert "an infrastructure fault, not the change" in cap.err and "neither passed nor failed" in cap.err, cap.err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["infra_fault"] == syntaxfault.INFRA, rec
    # A script still broken is a real failure, never rerun.
    (tmp_path / "torn-count").unlink()
    (tmp_path / "torn0.log").write_text(_torn_line(broken))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", *argv])
    assert e.value.code not in (0, 75) and ran() == 1
    assert "infra_fault" not in json.loads((run / "checks.json").read_text())


def test_push_batch_holds_on_a_transient_syntax_error_again_and_requeues_its_entries(env, monkeypatch, capsys):
    from ttp import push
    d = env["tmp"] / "torn"
    d.mkdir()
    _, _, whole, _ = _torn_repo(d)
    _outside_temp(monkeypatch)
    argv, ran = _torn_check(d, [_torn_line(whole)])
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"[ -e plugins/p/f1.txt ] || exit 0; {' '.join(argv)}"])
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "script_torn" and ran() == 2, m
    assert _statuses(m) == ["requeued"], m
    assert m["checks"]["script_torn"]["scripts"] == [str(whole)], m
    out = capsys.readouterr().out
    assert "the batch is held, not failed" in out and "check failed on" not in out, out


def test_a_push_batch_records_each_check_that_passed_as_its_command(env, monkeypatch):
    """checks.passed (which clears a check's script_torn count in pushq._torn_told) lists every check
    the batch passed, each as str(cmd)."""
    a, b = "true", "echo ok"
    p, repo, origin, other = _push_setup(env, monkeypatch, [a, b])
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert m["checks"]["passed"] == [a, b], m


def test_a_push_batch_held_script_torn_records_only_the_checks_that_passed_and_a_reused_pass(env, monkeypatch):
    """Two checks, the first passes and the second is held script_torn: checks.passed lists only the
    first. The next batch on the same tree reuses the first check's pass, and checks.passed lists it
    too, with the second check that now passes."""
    from ttp import push
    d = env["tmp"] / "torn"
    d.mkdir()
    _, _, whole, _ = _torn_repo(d)
    _outside_temp(monkeypatch)
    argv, ran = _torn_check(d, [_torn_line(whole), _torn_line(whole), ""])
    a, torn = "true", f"[ -e f1.txt ] || exit 0; {' '.join(argv)}"
    p, repo, origin, other = _push_setup(env, monkeypatch, [a, torn])
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "script_torn" and ran() == 2, m
    assert m["checks"]["passed"] == [a], m
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b2"), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and ran() == 3, m
    assert m["checks"]["reused"] == 1 and m["checks"]["passed"] == [a, torn], m
