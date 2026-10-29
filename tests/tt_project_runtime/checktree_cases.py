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
