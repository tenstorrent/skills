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
    from ttp import service
    assert "restart onto" not in out and not (p.state / service.RESTART_REQUEST).exists(), \
        "a daemon that takes fast-forward requests is never asked to restart first"


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
    # A daemon on an older runtime (no apply_requests) whose runtime on disk predates them too is refused
    # at once, nothing written: a restart would not help.
    from ttp import service
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests"]}))
    monkeypatch.setattr(applyff, "_disk_takes", lambda p: "")
    outcome, why = applyff.request(p, merged, "1")
    assert outcome == "failed" and f"`ttp upgrade demo --apply {merged}` outside a worker sandbox" in why, why
    assert not (p.state / applyff.APPLY_REQUEST).exists() and not (p.state / service.RESTART_REQUEST).exists()


def _old_daemon_restarts(p, before=None):
    """A stand-in daemon older than fast-forward requests: it takes the restart request, comes back
    taking them (its heartbeat says so) and then takes the fast-forward like _daemon_takes. Records the
    upgrade journal's step when the restart was asked for."""
    import threading
    from ttp import applyff, service, upjournal
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests"],
                                                   "receipt": {"version": "0.0.1"}}))
    seen, done = [], []

    def run():
        req = p.state / service.RESTART_REQUEST
        end = time.time() + 30
        while time.time() < end and not req.exists():
            time.sleep(0.05)
        at = json.loads(req.read_text())["at"]
        seen.append((upjournal.read(p) or {}).get("step"))
        req.unlink()
        (p.state / "heartbeat").write_text(json.dumps({"pid": 2, "started": time.time(),
                                                       "takes": ["restart_requests", "apply_requests"]}))
        (p.state / service.RESTART_RESULT).write_text(json.dumps({"at": at, "outcome": "running",
                                                                  "text": "the daemon is running"}))
        while time.time() < end and not (p.state / applyff.APPLY_REQUEST).exists():
            time.sleep(0.05)
        if before:
            before()
        done.append(applyff.take(p))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, seen, done


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_apply_in_a_sandbox_restarts_a_daemon_older_than_fast_forward_requests_then_hands_it_the_merge(
        env, monkeypatch, tmp_path, capsys):
    """The live daemon predates fast-forward requests but takes restart requests, and the runtime on disk
    takes them: --apply has it restart onto that runtime, before the journal names the merge, then hands
    it the fast-forward. Main ends on the merge, never a manual step."""
    from ttp import applyff, cli, service
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    dirs = _tree_dirs(h)
    _read_only(dirs)
    try:
        t, seen, done = _old_daemon_restarts(p, before=lambda: _writable(dirs))
        cli.main(["upgrade", "demo", "--apply", merged])
        t.join(10)
    finally:
        _writable(dirs)
    out = capsys.readouterr().out
    assert "ttp 0.0.1) does not take fast-forward requests; asking it to restart onto" in out, out
    assert "takes fast-forward requests" in out and "asking the daemon to fast-forward main" in out, out
    assert seen and seen[0] != "merging", "the restarted daemon finds no merge of a live run to recover"
    assert done == [True] and _git_out(h, "rev-parse", "HEAD") == merged and _clean(h)
    assert restarts == [1], "the upgrade's own restart still follows"
    for f in (applyff.APPLY_REQUEST, applyff.APPLY_REQUEST + ".taken", service.RESTART_REQUEST):
        assert not (p.state / f).exists(), f


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_apply_says_why_when_the_restarted_daemon_still_does_not_take_fast_forward_requests(
        env, monkeypatch, tmp_path, capsys):
    """The restart fails (or brings back an old daemon): --apply stops with main as it was, the merge
    unjournalled, and no fast-forward request left for a daemon to take later."""
    from ttp import applyff, cli, service
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": ["restart_requests"]}))
    monkeypatch.setattr(applyff, "RESTART_WAIT_S", 0.4)
    monkeypatch.setattr(service, "request_restart",
                        lambda p, why, wait_s: service._said("the restart helper could not start", "failed"))
    dirs = _tree_dirs(h)
    _read_only(dirs)
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", "--apply", merged])
        monkeypatch.setattr(service, "request_restart",
                            lambda p, why, wait_s: service._said("the daemon is running", "running"))
        with pytest.raises(SystemExit) as e2:
            cli.main(["upgrade", "demo", "--apply", merged])
    finally:
        _writable(dirs)
    err = capsys.readouterr().err
    assert e.value.code == 1 and "the restart did not bring one up: the restart helper could not start" in err, err
    assert e2.value.code == 1 and "no daemon taking them ticked within" in err, err
    assert _git_out(h, "rev-parse", "HEAD") == base and _clean(h) and not restarts
    assert not (p.state / applyff.APPLY_REQUEST).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_a_daemon_taking_neither_request_gets_no_sandboxed_merge_task_and_a_clear_status(
        env, monkeypatch, tmp_path, capsys):
    """A daemon that takes neither fast-forward nor restart requests leaves a sandboxed --apply no way to
    move main: `ttp upgrade` queues no merge task that would sit stuck and says, model-free, what to run
    outside a sandbox."""
    from ttp import applyff, cli, release, service
    p, restarts = _conflicting_upgrade(env, monkeypatch)
    h = p.harness
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": [], "receipt": {"version": "0.0.1"}}))
    base = _git_out(h, "rev-parse", "HEAD")
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "demo"])
    err = capsys.readouterr().err
    assert e.value.code == 1 and "No harness task was queued" in err, err
    assert "nor restart requests" in err and "run `ttp upgrade demo --apply <commit>`" in err, err
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE kind='harness'")["n"] == 0
    assert release.finish_holder(p) is None and _git_out(h, "rev-parse", "HEAD") == base and not restarts
    # The same check as an automatic upgrade records it failed, with the reason, for the status line.
    monkeypatch.setattr(release, "finish", lambda p, outcome, **kw: finished.append((outcome, kw.get("why"))))
    finished = []
    with pytest.raises(SystemExit):
        cli._refuse_unappliable_task(p, True, "a conflict")
    assert finished and finished[0][0] == "failed" and "nor restart requests" in finished[0][1], finished


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only directories")
def test_apply_with_a_daemon_taking_neither_request_stops_at_once_and_names_the_command(
        env, monkeypatch, tmp_path, capsys):
    """A merge task queued before (by an older `ttp upgrade`) whose daemon takes neither request: its
    --apply stops at once, names the command to run outside a sandbox and writes no request."""
    from ttp import applyff, cli, service
    p, merged, base, restarts = _apply_ready(env, monkeypatch, tmp_path)
    h = p.harness
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "takes": []}))
    dirs = _tree_dirs(h)
    _read_only(dirs)
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["upgrade", "demo", "--apply", merged])
    finally:
        _writable(dirs)
    err = capsys.readouterr().err
    assert e.value.code == 1 and f"run `ttp upgrade demo --apply {merged}` outside a worker sandbox" in err, err
    assert _git_out(h, "rev-parse", "HEAD") == base and _clean(h) and not restarts
    for f in (applyff.APPLY_REQUEST, service.RESTART_REQUEST):
        assert not (p.state / f).exists(), f


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
