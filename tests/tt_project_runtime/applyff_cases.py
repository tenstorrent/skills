# `ttp upgrade --apply` fast-forward tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file. A read-only
# harness working tree (a worker sandbox) is simulated with directory modes, never a real sandbox.
from __future__ import annotations


def _apply_ready(env, monkeypatch, tmp_path):
    """A conflicting upgrade queued as a harness task and resolved in its own worktree: (p, merged, base,
    restarts), with TTP_TASK set to that task."""
    from ttp import cli
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    h = p.harness
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    tid = p.db.one("SELECT id FROM tasks WHERE kind='harness'")["id"]
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    wt = tmp_path / "merge"
    _git_out(h, "worktree", "add", "-q", "--detach", str(wt), "main")
    subprocess.run(["git", "-C", str(wt), *ident, "merge", "-X", "theirs", "upstream"], capture_output=True)
    _git_out(wt, "add", "-A")
    subprocess.run(["git", "-C", str(wt), *ident, "commit", "-qm", "merge upstream"], capture_output=True)
    monkeypatch.setenv("TTP_TASK", str(tid))
    return p, _git_out(wt, "rev-parse", "HEAD"), _git_out(h, "rev-parse", "HEAD"), restarts


def _tree_dirs(h):
    return [d for d in [h, *h.rglob("*")] if d.is_dir() and ".git" not in d.relative_to(h).parts]


def _read_only(paths):
    for d in paths:
        d.chmod(0o555 if d.is_dir() else 0o444)


def _writable(paths):
    for d in paths:
        d.chmod(0o755 if d.is_dir() else 0o644)


def _clean(h):
    return _git_out(h, "status", "--porcelain", "--untracked-files=no") == ""


def _daemon_takes(p, before=None):
    """A stand-in daemon on another thread: it takes the fast-forward request with write access to the
    tree (before() gives it back) and records what it did."""
    import threading
    from ttp import applyff
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests", "apply_requests"]}))
    done = []

    def run():
        end = time.time() + 30
        while time.time() < end and not (p.state / applyff.APPLY_REQUEST).exists():
            time.sleep(0.05)
        if before:
            before()
        done.append(applyff.take(p))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, done


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_apply_in_a_sandbox_that_cannot_write_the_harness_tree_hands_the_fast_forward_to_the_daemon(
        env, monkeypatch, tmp_path, capsys):
    """The worker sandbox writes .git but not the working tree: --apply asks the daemon to move main, then
    restarts the daemon as usual. Main ends on the merge and the tree matches it."""
    from ttp import applyff, cli
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    dirs = _tree_dirs(h)
    _read_only(dirs)
    try:
        t, done = _daemon_takes(p, before=lambda: _writable(dirs))
        cli.main(["upgrade", "demo", "--apply", merged])
        t.join(10)
    finally:
        _writable(dirs)
    out = capsys.readouterr().out
    assert "asking the daemon to fast-forward main" in out, out
    assert done == [True] and _git_out(h, "rev-parse", "HEAD") == merged and _clean(h)
    assert restarts == [1], "the daemon restart still happens"
    assert not (p.state / applyff.APPLY_REQUEST).exists()
    assert not (p.state / (applyff.APPLY_REQUEST + ".taken")).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_apply_with_no_daemon_taking_the_request_withdraws_it_and_leaves_main_as_it_was(
        env, monkeypatch, tmp_path, capsys):
    from ttp import applyff, cli
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests", "apply_requests"]}))
    monkeypatch.setattr(applyff.request, "__defaults__", (0.3,))
    dirs = _tree_dirs(h)
    _read_only(dirs)
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", "--apply", merged])
    finally:
        _writable(dirs)
    err = capsys.readouterr().err
    assert e.value.code == 1 and "did not take the fast-forward request" in err, err
    assert f"rerun `ttp upgrade demo --apply {merged}`" in err
    assert _git_out(h, "rev-parse", "HEAD") == base and _clean(h) and not restarts
    assert not (p.state / applyff.APPLY_REQUEST).exists(), "withdrawn: no daemon moves main later"
    # A daemon on an older runtime (no apply_requests) is refused at once, nothing written.
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests"]}))
    assert applyff.request(p, merged, "1")[0] == "failed" and not (p.state / applyff.APPLY_REQUEST).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_a_fast_forward_that_fails_part_way_puts_the_tree_back_and_says_what_to_retry(
        env, monkeypatch, tmp_path, capsys):
    """One directory the merge writes is read-only, the others are not: git writes some files and fails.
    Every file it wrote is put back to main's version, main stays, nothing restarts, and the error names
    the rerun. The same holds when the daemon's fast-forward fails part-way."""
    from ttp import applyff, cli, release
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    touched = _git_out(h, "diff", "--name-only", base, merged).split()
    # git removes the files main loses first, then writes the changed ones: the removal goes through and
    # the read-only prompts/ stops it part-way.
    gone = _git_out(h, "diff", "--name-only", "--diff-filter=D", base, merged).split()
    assert gone and "prompts/kind-harness.md" in touched, touched
    ro = [h / "prompts", h / "prompts" / "kind-harness.md"]
    _read_only(ro)
    try:
        why = applyff.fast_forward(h, merged, release.GUARD_ENV)
        assert why and applyff.no_write(why), why
        assert _git_out(h, "rev-parse", "HEAD") == base and _clean(h), _git_out(h, "status", "--porcelain")
        # Through --apply: the probe is skipped (as if the sandbox let it pass), the daemon fails the same way.
        monkeypatch.setattr(applyff, "unwritable", lambda *a: "")
        _daemon_takes(p)
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", "--apply", merged])
    finally:
        _writable(ro)
    err = capsys.readouterr().err
    assert e.value.code == 1 and "did not fast-forward main" in err, err
    assert f"Main is still {base[:12]}" in err and f"rerun `ttp upgrade demo --apply {merged}`" in err
    assert _git_out(h, "rev-parse", "HEAD") == base and _clean(h) and not restarts


def test_the_daemon_refuses_a_fast_forward_request_no_running_apply_made(env, monkeypatch, tmp_path):
    """state/ is writable by workers: a request moves main only for the `ttp upgrade --apply` that holds
    the upgrade lock and journals this very merge, and only to a commit with upstream and main in it."""
    from ttp import applyff, locks, release, upjournal
    p, merged, base, _ = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness

    def ask(commit, at=None):
        at = time.time() if at is None else at
        (p.state / applyff.APPLY_REQUEST).write_text(json.dumps({"at": at, "merged": commit, "task": "1"}))
        assert applyff.take(p)
        return applyff.result(p, at)["text"]

    assert "no `ttp upgrade` holds the upgrade lock" in ask(merged)
    held = locks.try_take([release.upgrade_lock(p)], "ttp upgrade (pid 1)", "ttp upgrade")
    try:
        assert "journal does not name this merge" in ask(merged)
        upjournal.begin(p, "apply")
        upjournal.step(p, "merging", base=base, merged=base)
        assert "journal does not name this merge" in ask(merged)
        upjournal.step(p, "merging", base=base, merged=merged)
        assert "older than its requester waits" in ask(merged, at=time.time() - 3 * applyff.APPLY_WAIT_S)
        for commit, why in ((_git_out(h, "rev-parse", "HEAD~1"), "does not contain the current template"),
                            (_git_out(h, "rev-parse", "upstream"), "main moved")):
            upjournal.step(p, "merging", base=base, merged=commit)
            assert why in ask(commit)
        assert _git_out(h, "rev-parse", "HEAD") == base
        upjournal.step(p, "merging", base=base, merged=merged)
        assert f"fast-forwarded main to {merged[:12]}" in ask(merged)
        assert _git_out(h, "rev-parse", "HEAD") == merged and _clean(h)
    finally:
        upjournal.end(p)
        held.close()
