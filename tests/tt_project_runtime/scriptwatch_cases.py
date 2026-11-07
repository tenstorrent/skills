# A check, schedule or heal check whose script is rewritten in place while it runs (scriptwatch), and
# tt-project's own script writes, which rename a new file into place instead (project.durable_write).


def _self_rewriting(path, then_exit=0):
    """A bash script whose first run rewrites itself in place (same inode), as an editor saving over it
    does: the running shell reads on into the new text and fails with a syntax error. The new version
    exits `then_exit`. Each run appends a line to <path>.runs."""
    code = ('import sys; f = open(sys.argv[1], "r+"); '
            f'f.write("#!/bin/bash\\necho x >> \\"$0.runs\\"\\nexit {then_exit}\\n#" + ")" * 3000 + "\\n")')
    path.write_text(
        "#!/bin/bash\n"
        "echo x >> \"$0.runs\"\n"
        "if [ ! -e \"$0.ran\" ]; then\n"
        "  : > \"$0.ran\"\n"
        f"  python3 -c {shlex.quote(code)} \"$0\"\n"
        "fi\n"
        "echo first version done\n")
    path.chmod(0o755)
    return path


def _runs(path):
    runs = path.with_name(path.name + ".runs")
    return len(runs.read_text().splitlines()) if runs.exists() else 0


def test_scriptwatch_reruns_once_a_command_whose_script_was_rewritten_in_place_mid_run(tmp_path):
    from ttp import scriptwatch
    script = _self_rewriting(tmp_path / "check.sh")
    ino = script.stat().st_ino
    run = lambda: subprocess.run(["bash", "-c", "bash check.sh"], cwd=tmp_path, capture_output=True, text=True)
    said = []
    out = scriptwatch.rerun(run, lambda r: r.returncode != 0, "bash check.sh", tmp_path, None, said.append)
    assert script.stat().st_ino == ino, "the script was edited in place, as in the reported failure"
    assert out.returncode == 0 and _runs(script) == 2
    assert said == [f"{scriptwatch.LABEL}: content changed in check.sh"]
    # Without the watch the same first run is the plain failure the report showed.
    (tmp_path / "check.sh.ran").unlink()
    (tmp_path / "check.sh.runs").unlink()
    _self_rewriting(script)
    first = run()
    assert first.returncode == 2 and "syntax error near unexpected token" in first.stderr


def test_scriptwatch_still_fails_a_real_failure_and_a_second_failure(tmp_path):
    from ttp import scriptwatch
    fail = tmp_path / "fail.sh"
    fail.write_text("#!/bin/bash\necho x >> \"$0.runs\"\nexit 1\n")
    said = []
    run = lambda name: lambda: subprocess.run(["bash", name], cwd=tmp_path).returncode
    # Nothing changed: the failure is reported as before, run once.
    assert scriptwatch.rerun(run("fail.sh"), bool, "bash fail.sh", tmp_path, None, said.append) == 1
    assert _runs(fail) == 1 and said == []
    # Changed mid-run, and the new version fails too: the rerun's failure counts.
    twice = _self_rewriting(tmp_path / "twice.sh", then_exit=3)
    assert scriptwatch.rerun(run("twice.sh"), bool, "bash twice.sh", tmp_path, None, said.append) == 3
    assert _runs(twice) == 2 and said == [f"{scriptwatch.LABEL}: content changed in twice.sh"]
    # A command that names no script, or no command (a remote check), is never rerun.
    count = tmp_path / "count"
    sh = lambda: subprocess.run(["sh", "-c", f"echo x >> {count}; exit 1"]).returncode
    assert scriptwatch.rerun(sh, bool, f"echo x >> {count}; exit 1", tmp_path) == 1
    assert scriptwatch.rerun(sh, bool, None, tmp_path) == 1
    assert len(count.read_text().splitlines()) == 2


def test_scriptwatch_watches_named_scripts_the_neighbours_they_name_and_scripts_on_path(tmp_path):
    from ttp import scriptwatch
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    check = '#!/bin/sh\n. "$(dirname "$0")/lib.sh"\nhelper --x data.txt\n'
    for name, text in (("check", check), ("helper", "#!/bin/sh\n"), ("lib.sh", "x=1\n"), ("data.txt", "x\n")):
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    got = {p.name for p in scriptwatch.scripts("bin/check && python3 -m pytest test_a.py", tmp_path, {"PATH": ""})}
    assert got == {"check", "helper", "lib.sh"}   # a data file or a test module is no script
    assert {p.name for p in scriptwatch.scripts("check --fast", tmp_path, {"PATH": str(bin_)})} == {"check", "helper", "lib.sh"}
    seen = scriptwatch.watch("bin/check", tmp_path, {"PATH": ""})
    assert scriptwatch.changed(seen) == []
    tmp = bin_ / ".helper.tmp"
    tmp.write_text("#!/bin/sh\nexit 0\n")
    os.replace(tmp, bin_ / "helper")   # replaced by rename counts as well
    assert scriptwatch.changed(seen) == [str(bin_ / "helper")]



def test_scriptwatch_follows_named_neighbours_transitively_and_skips_unrelated_ones(tmp_path):
    # An edit to a script nothing watched names (another job's script in the same folder) must not count
    # as a mid-run change; a script reached through a chain of names must.
    from ttp import scriptwatch
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in (("check", '#!/bin/bash\nsource "$here/hop1.sh"\nrun-helper\n'),
                       ("hop1.sh", "bash ./hop2.sh\n"),           # check -> hop1 -> hop2: two hops
                       ("hop2.sh", "#!/bin/sh\n. ./hop1.sh\n"),   # and back to hop1: a cycle
                       ("run-helper", "#!/usr/bin/env python3\nimport mod\n"),
                       ("mod.py", "#!/usr/bin/env python3\n"),     # imported by module name
                       ("unrelated.sh", "echo other job\n"),
                       ("hop", "#!/bin/sh\n"),                    # a prefix of hop1.sh is no name of it
                       ("helper", "#!/bin/sh\n")):               # nor is a part of run-helper
        (bin_ / name).write_text(text)
    got = {p.name for p in scriptwatch.scripts("bin/check", tmp_path, {"PATH": ""})}
    assert got == {"check", "hop1.sh", "hop2.sh", "run-helper", "mod.py"}
    (bin_ / "loop.sh").write_text("for f in ./lib-*.sh; do . \"$f\"; done\ncase $1 in *) ;; esac\n")
    (bin_ / "lib-a.sh").write_text("x=1\n")
    assert {p.name for p in scriptwatch.scripts("bash bin/loop.sh", tmp_path, {"PATH": ""})} == {"loop.sh", "lib-a.sh"}
    big = bin_ / "big.sh"   # a script too long to read in full is taken to name every neighbour
    big.write_text("#" * (scriptwatch.MAX_READ + 1) + "\n")
    assert "unrelated.sh" in {p.name for p in scriptwatch.scripts("bash bin/big.sh", tmp_path, {"PATH": ""})}


def test_scriptwatch_reruns_for_an_edited_dependency_but_not_for_an_unrelated_neighbour(tmp_path):
    from ttp import scriptwatch
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "check.sh").write_text('. "$(dirname "$0")/lib.sh"\n')
    for name in ("lib.sh", "deep.sh", "other.sh"):
        (bin_ / name).write_text("bash ./deep.sh\n" if name == "lib.sh" else "x=1\n")
    for edit, runs in (("other.sh", 1), ("deep.sh", 2)):
        count = []
        def run():
            count.append(1)
            if len(count) == 1:
                with open(bin_ / edit, "a") as f:
                    f.write("y=2\n")
            return 1
        said = []
        assert scriptwatch.rerun(run, bool, "bash bin/check.sh", tmp_path, {"PATH": ""}, said.append) == 1
        assert len(count) == runs
        assert said == ([f"{scriptwatch.LABEL}: content changed in bin/deep.sh"] if runs == 2 else [])

def test_scriptwatch_watches_neighbours_a_script_reaches_through_a_path_built_at_run_time(tmp_path):
    # A path built at run time is an ambiguous reference: include what it may name rather than drop it.
    from ttp import scriptwatch
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("lib.sh", "other.sh", "common.bash"):
        (bin_ / name).write_text("x=1\n")
    every = {"check.sh", "lib.sh", "other.sh", "common.bash"}
    sh = {"check.sh", "lib.sh", "other.sh"}
    for body, want in (('for f in "$here"/*; do . "$f"; done\n', every),
                       ('. "$here/$n.sh"\n', sh),
                       ('. "$here/${NAME}.sh"\n', sh),
                       ('bash "$(dirname "$0")/$1"\n', every),
                       ('ls "$here" | while read f; do bash "$here/$f"; done\n', every),
                       ('. `dirname $0`/lib-$x.sh\n', {"check.sh"}),          # lib-*.sh: none here
                       ('. "$here/l${x}b.sh"\n', {"check.sh", "lib.sh"}),
                       ('echo $x $* "$@"; [ $? = 0 ] && echo $#\ncase $1 in *) ;; esac\n', {"check.sh"})):
        (bin_ / "check.sh").write_text(body)
        assert {p.name for p in scriptwatch.scripts("bash bin/check.sh", tmp_path, {"PATH": ""})} == want, body


def test_ttp_checks_reruns_a_check_whose_script_was_rewritten_mid_run_and_labels_it(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, scriptwatch
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _self_rewriting(repo / "check.sh")
    (repo / ".gitignore").write_text("*.ran\n*.runs\n")
    subprocess.run([*git, "add", "check.sh", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "check"], check=True)
    p.set_config("delivery.push_checks", [{"run": "bash check.sh", "cwd": "authoring"}])
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert f"ttp checks: {scriptwatch.LABEL}: content changed in check.sh" in out
    assert json.loads((run / "checks.json").read_text())["passed"] and _runs(repo / "check.sh") == 2
    assert scriptwatch.LABEL in (run / "checks.log").read_text()
    # A check that fails with its script unchanged still fails, run once.
    (repo / "fail.sh").write_text("#!/bin/bash\necho x >> \"$0.runs\"\nexit 1\n")
    subprocess.run([*git, "add", "-A"], check=True)   # with check.sh as its first run rewrote it
    subprocess.run([*git, "commit", "-qm", "fail"], check=True)
    p.set_config("delivery.push_checks", [{"run": "bash fail.sh", "cwd": "authoring"}])
    with pytest.raises(SystemExit):
        cli.main(["checks"])
    got = capsys.readouterr()
    assert scriptwatch.LABEL not in got.out and _runs(repo / "fail.sh") == 1, got
    assert not json.loads((run / "checks.json").read_text())["passed"]


def test_ttp_checks_skips_a_script_changed_rerun_that_cannot_end_before_the_wall_cap_and_keeps_the_result(
        env, tmp_path, monkeypatch, capsys):
    from ttp import checkwall, cli, scriptwatch
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    # Its first run appends to itself (a content change, no syntax error), takes a second and fails.
    (repo / "check.sh").write_text('#!/bin/bash\necho x >> "$0.runs"\n'
                                   'if [ ! -e "$0.ran" ]; then : > "$0.ran"; echo "# edited" >> "$0"; sleep 1; exit 1; fi\n')
    (repo / ".gitignore").write_text("*.ran\n*.runs\n")
    subprocess.run([*git, "add", "check.sh", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "check"], check=True)
    p.set_config("delivery.push_checks", [{"run": "bash check.sh", "cwd": "authoring"}])
    monkeypatch.setenv(checkwall.ENV, "3600")
    real, asked = checkwall.deadline_left, []
    def left(deadline):   # the run itself under the real cap; the rerun asks with half a second left
        asked.append(deadline)
        return real(deadline) if len(asked) == 1 else 0.5
    monkeypatch.setattr(checkwall, "deadline_left", left)
    with pytest.raises(SystemExit):
        cli.main(["checks"])
    out = capsys.readouterr().out
    assert re.search(rf"{re.escape(scriptwatch.LABEL)} skipped: the run took \d+ s and only 0 s is left before the "
                     r"wall-time cap; its result stands", out), out
    assert _runs(repo / "check.sh") == 1 and asked[0] is not None, "no rerun started"
    assert f"{scriptwatch.LABEL} skipped" in (run / "checks.log").read_text()
    assert not json.loads((run / "checks.json").read_text())["passed"], "the run's own result stands"
    # With the time to fit it, the rerun runs and its pass counts.
    (repo / "check.sh.ran").unlink()
    subprocess.run([*git, "checkout", "-q", "check.sh"], check=True)
    monkeypatch.setattr(checkwall, "deadline_left", real)
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert f"{scriptwatch.LABEL}: content changed in check.sh" in out and "skipped" not in out, out
    assert _runs(repo / "check.sh") == 3 and json.loads((run / "checks.json").read_text())["passed"]


def _push_check(path, edit, exit_code=0):
    """A push check script whose first run runs `edit` (shell, with $0 the script) and exits `exit_code`;
    later runs exit 0. Each run appends a line to <path>.runs."""
    path.write_text(
        "#!/bin/bash\n"
        "echo x >> \"$0.runs\"\n"
        "if [ ! -e \"$0.ran\" ]; then\n"
        "  : > \"$0.ran\"\n"
        f"  {edit}\n"
        f"  exit {exit_code}\n"
        "fi\n")
    path.chmod(0o755)
    return path


def test_a_push_check_whose_script_is_touched_or_recommitted_with_the_same_content_is_not_rerun(tmp_path):
    from ttp import push
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    same = 'cp "$0" "$0.new" && mv "$0.new" "$0" && touch -d "+1 hour" "$0" && git add -A && git commit -qm again --allow-empty'
    for code, want in ((0, 0), (1, push.CHECKS_FAILED)):
        name = f"check{code}.sh"
        _push_check(tmp_path / name, same, code)
        git("add", "-A")
        git("commit", "-qm", "check")
        ino = (tmp_path / name).stat().st_ino
        said = []
        assert push._run_one(f"bash {name}", tmp_path, dict(os.environ), said.append) == want
        assert (tmp_path / name).stat().st_ino != ino, "replaced, touched and recommitted: only the content is the same"
        assert _runs(tmp_path / name) == 1 and said == []


def test_a_passed_push_check_whose_script_content_changed_is_rerun_once_and_a_failed_one_never(tmp_path):
    from ttp import push, scriptwatch
    edit = 'echo "# new" >> "$0"'
    _push_check(tmp_path / "check.sh", edit)
    said = []
    assert push._run_one("bash check.sh", tmp_path, dict(os.environ), said.append) == 0
    assert said == [f"{scriptwatch.LABEL}: content changed in check.sh"] and _runs(tmp_path / "check.sh") == 2
    # A failure stands: not rerun, the push refused, and the report says the script changed.
    _push_check(tmp_path / "fail.sh", edit, exit_code=1)
    said = []
    assert push._run_one("bash fail.sh", tmp_path, dict(os.environ), said.append) == push.CHECKS_FAILED
    assert _runs(tmp_path / "fail.sh") == 1
    assert said == [f"{scriptwatch.KEPT}: content changed in fail.sh"]
    # A torn script (rewritten in place, failing with a syntax error) is a failure too: never rerun.
    _self_rewriting(tmp_path / "torn.sh")
    said = []
    assert push._run_one("bash torn.sh", tmp_path, dict(os.environ), said.append) == push.CHECKS_FAILED
    assert _runs(tmp_path / "torn.sh") == 1 and said == [f"{scriptwatch.KEPT}: content changed in torn.sh"]
    # At most one rerun: a rerun that changes the script again is not run a third time.
    count = []
    def run():
        count.append(1)
        with open(tmp_path / "check.sh", "a") as f:
            f.write("# again\n")
        return 0
    assert scriptwatch.rerun(run, bool, "bash check.sh", tmp_path, None, None, on_pass=True) == 0
    assert len(count) == 2


def test_a_push_check_reruns_when_a_named_neighbour_content_changed_not_an_unnamed_one(tmp_path):
    from ttp import push, scriptwatch
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "lib.sh").write_text("x=1\n")
    (bin_ / "other.sh").write_text("x=1\n")
    for edit, runs in (("other.sh", 1), ("lib.sh", 2)):
        (bin_ / "check.sh.ran").unlink(missing_ok=True)
        (bin_ / "check.sh.runs").unlink(missing_ok=True)
        _push_check(bin_ / "check.sh", '. "$(dirname "$0")/lib.sh"; echo y=2 >> "$EDIT"')   # names lib.sh only
        said = []
        assert push._run_one("bash bin/check.sh", tmp_path, {**os.environ, "EDIT": str(bin_ / edit)}, said.append) == 0
        assert _runs(bin_ / "check.sh") == runs
        assert said == ([f"{scriptwatch.LABEL}: content changed in bin/lib.sh"] if runs == 2 else [])


def test_a_schedule_command_and_a_heal_check_whose_script_changed_mid_run_are_rerun_once(env, tmp_path):
    from ttp import heal, schedrun, scriptwatch
    _self_rewriting(tmp_path / "watch.sh")
    out = schedrun.run_group("bash watch.sh", cwd=str(tmp_path), timeout=60, env=dict(os.environ))
    assert out.returncode == 0 and out.stderr.startswith(f"ttp: {scriptwatch.LABEL}: content changed in watch.sh\n")
    assert _runs(tmp_path / "watch.sh") == 2
    _self_rewriting(tmp_path / "healthy.sh")
    rc, text = heal.check({"check": "bash healthy.sh", "timeout_s": 60}, {}, str(tmp_path), dict(os.environ))
    assert rc == 0 and text.endswith(f"{scriptwatch.LABEL}: content changed in healthy.sh")
    # An unhealthy answer with nothing changed is the check's answer, run once.
    (tmp_path / "sick.sh").write_text("#!/bin/bash\necho x >> \"$0.runs\"\necho down\nexit 1\n")
    assert heal.check({"check": "bash sick.sh", "timeout_s": 60}, {}, str(tmp_path), dict(os.environ)) == (1, "down")
    assert _runs(tmp_path / "sick.sh") == 1


def test_a_watcher_or_precheck_exit_1_is_its_answer_and_never_rerun_when_a_neighbour_changed(env):
    from ttp import daemon as dm, schedrun, scriptwatch
    p = make(env)
    bin_dir = p.root / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "helper.sh").write_text("#!/bin/bash\ntrue\n")
    # A watcher's first run reports, replaces a neighbour script by an atomic rename and exits 1 ("found
    # something"); a rerun would print nothing and its report would be lost.
    (bin_dir / "watch").write_text(
        "#!/bin/bash\n"
        "echo x >> \"$0.runs\"\n"
        "if [ ! -e \"$0.ran\" ]; then\n"
        "  : > \"$0.ran\"\n"
        "  printf '#!/bin/bash\\nfalse\\n' > bin/helper.sh.tmp && mv bin/helper.sh.tmp bin/helper.sh\n"
        "  echo 'chip dropped'\n"
        "  exit 1\n"
        "fi\n")
    d = dm.Daemon(p.base)
    assert d._run_command_watcher({"name": "chips"}, {"command": "bash bin/watch"}) == "ok (1 observations)"
    assert _runs(bin_dir / "watch") == 1
    # The schedule precheck's exit 1 ("the run goes ahead") is an answer too: run once.
    (bin_dir / "watch.ran").unlink()
    (bin_dir / "watch.runs").unlink()
    out = schedrun.run_group("bash bin/watch", cwd=str(p.root), timeout=60, env=dict(os.environ), ok=(0, 1))
    assert out.returncode == 1 and out.stdout == "chip dropped\n" and _runs(bin_dir / "watch") == 1
    # A real failure (a bash syntax error from a script rewritten in place) is still rerun once.
    _self_rewriting(bin_dir / "broken.sh")
    out = schedrun.run_group("bash bin/broken.sh", cwd=str(p.root), timeout=60, env=dict(os.environ), ok=(0, 1))
    assert out.returncode == 0 and out.stderr.startswith(f"ttp: {scriptwatch.LABEL}: content changed in bin/broken.sh\n")
    assert _runs(bin_dir / "broken.sh") == 2


def test_script_writes_rename_a_new_file_into_place_keep_the_mode_and_leave_no_partial_file(env, tmp_path, monkeypatch):
    from ttp import cli, project
    f = tmp_path / "bin" / "check"
    f.parent.mkdir()
    f.write_text("#!/bin/sh\necho old\n")
    f.chmod(0o750)
    reader = open(f)   # a shell running the old version keeps reading the old file whole
    ino = f.stat().st_ino
    project.durable_write(f, "#!/bin/sh\necho new\n")
    assert f.stat().st_ino != ino and stat.S_IMODE(f.stat().st_mode) == 0o750
    assert reader.read() == "#!/bin/sh\necho old\n" and f.read_text() == "#!/bin/sh\necho new\n"
    reader.close()
    # A write that fails part way leaves the old file whole and no temporary file.
    real = os.replace
    monkeypatch.setattr(project.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        project.durable_write(f, "#!/bin/sh\necho half\n")
    monkeypatch.setattr(project.os, "replace", real)
    assert f.read_text() == "#!/bin/sh\necho new\n" and [x.name for x in f.parent.iterdir()] == ["check"]
    # Installing a template tree over a live one (ttp setup, init, upgrade) renames each file in too.
    src = tmp_path / "src"
    src.mkdir()
    (src / "check").write_text("#!/bin/sh\necho installed\n")
    (src / "check").chmod(0o755)
    ino = f.stat().st_ino
    cli._copy_tree(src, f.parent)
    assert f.stat().st_ino != ino and f.read_text() == "#!/bin/sh\necho installed\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o755 and [x.name for x in f.parent.iterdir()] == ["check"]
