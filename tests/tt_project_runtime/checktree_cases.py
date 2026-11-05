# Checked-tree tests (checktree) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of which tree `ttp checks` checks here.
from __future__ import annotations


def _behind_tip(p, repo, git):
    """Point the push branch's remote ref one commit past HEAD, so HEAD is stale."""
    p.set_config("delivery.push_branch", "proj")
    (repo / "b.txt").write_text("b\n")
    subprocess.run([*git, "add", "b.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "b"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/proj", "HEAD"], check=True)
    subprocess.run([*git, "reset", "-q", "--hard", "HEAD~1"], check=True)


def test_ttp_checks_start_line_names_head_branch_and_worktree(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    br = subprocess.run([*git, "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    cli.main(["checks", "--", "true"])
    out = capsys.readouterr().out
    assert f"ttp checks: checking {head[:12]} (branch {br}) in worktree {repo.resolve()}" in out
    assert "WARNING" not in out
    # A detached HEAD says so.
    subprocess.run([*git, "checkout", "-q", "--detach"], check=True)
    cli.main(["checks", "--fresh", "--", "true"])
    assert f"ttp checks: checking {head[:12]} (detached HEAD) in worktree {repo.resolve()}" in capsys.readouterr().out


def test_ttp_checks_result_probe_names_the_checked_head_and_worktree(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (run / "checks.pid").write_text(json.dumps({"pid": 1, "worktree": "/w/cand", "head": "c" * 40, "branch": "cut"}))
    (run / "checks.rc").write_text("0\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--result", str(run)])
    assert e.value.code == 0
    assert f"ttp checks: checking {'c' * 12} (branch cut) in worktree /w/cand" in capsys.readouterr().out


def test_ttp_checks_warns_when_the_caller_is_in_another_worktree_than_a_stale_checked_tree(env, tmp_path, monkeypatch,
                                                                                         capsys):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _behind_tip(p, repo, git)
    other = tmp_path / "cand"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(other), "origin/proj"], check=True)
    monkeypatch.chdir(other)
    cli.main(["checks", "--worktree", str(repo), "--", "true"])
    out = capsys.readouterr().out
    warn = [l for l in out.splitlines() if "WARNING" in l]
    assert len(warn) == 1, out
    assert str(repo.resolve()) in warn[0] and str(other.resolve()) in warn[0]
    assert "1 commit(s) behind origin/proj" in warn[0] and "`cd` into that worktree" in warn[0]
    assert warn[0] in (run / "checks.log").read_text()
    # The same stale tree checked from inside it: no warning (it is the tree the caller is in).
    monkeypatch.chdir(repo)
    cli.main(["checks", "--fresh", "--", "true"])
    assert "WARNING" not in capsys.readouterr().out
    # The run's own worktree (run.json cwd) counts as where the caller is, too.
    (run / "run.json").write_text(json.dumps({"cwd": str(other)}))
    cli.main(["checks", "--fresh", "--", "true"])
    assert "WARNING" in capsys.readouterr().out


def test_checktree_warning_fires_only_for_a_stale_or_project_root_tree_off_the_callers_tree(tmp_path):
    from ttp import checktree
    repo = tmp_path / "root"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for n in ("a", "b"):
        (repo / n).write_text(n)
        subprocess.run([*git, "add", n], check=True)
        subprocess.run([*git, "commit", "-qm", n], check=True)
    tip = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    old = subprocess.run([*git, "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", tip], check=True)
    ref, top, elsewhere = "refs/remotes/origin/main", str(repo.resolve()), str(tmp_path / "wt")
    assert checktree.behind(top, old, ref) == 1 and checktree.behind(top, tip, ref) == 0
    assert checktree.behind(top, old, "refs/remotes/origin/gone") is None
    # The project root at a stale head: warned even when the run itself sits there (the 9079 case).
    w = checktree.warning(top, old, root=repo, tip_ref=ref, callers=[top])
    assert "the project root checkout" in w and "1 commit(s) behind origin/main" in w
    # The project root up to date, checked from inside it: quiet; from another worktree: warned.
    assert checktree.warning(top, tip, root=repo, tip_ref=ref, callers=[top]) == ""
    assert elsewhere in checktree.warning(top, tip, root=repo, tip_ref=ref, callers=[elsewhere])
    # Not the root: stale and off the caller's tree warns; current or on the caller's tree does not.
    assert checktree.warning(top, old, root=tmp_path, tip_ref=ref, callers=[elsewhere])
    assert checktree.warning(top, tip, root=tmp_path, tip_ref=ref, callers=[elsewhere]) == ""
    assert checktree.warning(top, old, root=tmp_path, tip_ref=ref, callers=[top]) == ""
    # No push target known: only a root checked from elsewhere warns.
    assert checktree.warning(top, old, root=None, tip_ref="", callers=[elsewhere]) == ""


def _temp_worktree(run, git, tmp):
    """A linked worktree of the checks repo inside the run's temporary folder (run.json tmp_dir)."""
    tmp.mkdir()
    (run / "run.json").write_text(json.dumps({"cwd": str(run), "tmp_dir": str(tmp)}))
    wt = tmp / "wt"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(wt), "HEAD"], check=True)
    return wt


def test_ttp_checks_refuse_to_detach_or_queue_a_worktree_in_the_runs_temporary_folder(env, tmp_path, monkeypatch,
                                                                                     capsys):
    # Queued checks of a tree under the run's TMPDIR died with ENOENT once the run ended and its TMPDIR
    # went: they are refused up front, naming a lasting place in the run dir, and nothing is queued.
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    wt = _temp_worktree(run, git, tmp_path / "runtmp")
    monkeypatch.setattr(push, "_boxed", lambda: True)
    for how in (["--worktree", str(wt)], []):   # by flag, and from a shell inside it
        if not how:
            monkeypatch.chdir(wt / ".")
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--detach", *how, "--", "true"])
        err = capsys.readouterr().err
        assert e.value.code == 2, err
        assert f"worktree {wt.resolve()} is inside this run's temporary folder {(tmp_path / 'runtmp').resolve()}" in err
        assert f"worktree move {wt.resolve()} {run / 'wt-wt'}" in err
        assert not list((p.state / detach.QUEUE).glob("*.json")) and not (run / cli.CHECKS_PID).exists()
    # The run dir's own tmp is removed at run end too; a symlink into the temp folder resolves into it.
    link = tmp_path / "link"
    link.symlink_to(wt)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--detach", "--worktree", str(link), "--", "true"])
    assert e.value.code == 2 and "temporary folder" in capsys.readouterr().err
    # Foreground checks there still run: nothing outlives the call.
    cli.main(["checks", "--worktree", str(wt), "--", "true"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    # Moved to the suggested lasting place, the same checks queue.
    lasting = run / "wt-wt"
    subprocess.run([*git, "worktree", "move", str(wt), str(lasting)], check=True)
    monkeypatch.chdir(lasting)
    cli.main(["checks", "--detach", "--fresh", "--", "true"])
    assert "queued" in capsys.readouterr().out
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert json.loads(q.read_text())["checks"]["worktree"] == str(lasting.resolve())


def test_ttp_checks_never_hand_a_temporary_folder_worktree_over_to_detached_checks(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    wt = _temp_worktree(run, git, run / "tmp")
    monkeypatch.chdir(wt)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "FOREGROUND_WAIT_S", 0.5)
    ran = tmp_path / "ran"
    holder = _cap_holder("other project task #3 (run 9)")
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    finally:
        holder.kill()
        holder.wait()
    captured = capsys.readouterr()
    assert e.value.code == 2 and "every checks slot stayed busy" in captured.err, captured
    assert f"inside this run's temporary folder {(run / 'tmp').resolve()}" in captured.err
    assert cli.CHECKS_HANDOVER not in captured.out and not (run / cli.CHECKS_PID).exists() and not ran.exists()


def test_ttp_checks_record_a_symlinked_worktree_as_its_real_path(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    link = tmp_path / "via"
    link.symlink_to(repo)
    monkeypatch.chdir(tmp_path)
    cli.main(["checks", "--worktree", str(link), "--", "true"])
    assert f"in worktree {repo.resolve()}" in capsys.readouterr().out
    assert json.loads((run / "checks.json").read_text())["worktree"] == str(repo.resolve())
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--fresh", "--worktree", str(link), "--", "true"])
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert json.loads(q.read_text())["checks"]["worktree"] == str(repo.resolve())
    assert json.loads(q.read_text())["cwd"] == str(repo.resolve())
    # The shell's own directory reached through the link records the real path too.
    monkeypatch.chdir(link)
    cli.main(["checks", "--fresh", "--", "true"])
    assert json.loads((run / "checks.json").read_text())["worktree"] == str(repo.resolve())


def test_checktree_temp_root_matches_only_the_runs_own_temporary_folders(tmp_path):
    from ttp import checktree
    run, tmp = tmp_path / "run", tmp_path / "t"
    run.mkdir()
    tmp.mkdir()
    assert checktree.temp_root(tmp / "w", run) == ""   # no run.json: only the run dir's tmp counts
    assert checktree.temp_root(run / "tmp" / "w", run) == str((run / "tmp").resolve())
    (run / "run.json").write_text(json.dumps({"tmp_dir": str(tmp)}))
    assert checktree.temp_root(tmp / "a" / "b", run) == str(tmp.resolve())
    assert checktree.temp_root(tmp, run) == str(tmp.resolve())
    assert checktree.temp_root(tmp_path / "t2" / "w", run) == "", "a sibling with the same prefix is not inside"
    assert checktree.temp_root(run / "wt-x", run) == "", "the run dir itself lasts"
    assert checktree.temp_root(tmp / "w", None) == ""
    (run / "run.json").write_text("{not json")
    assert checktree.temp_root(tmp / "w", run) == ""
