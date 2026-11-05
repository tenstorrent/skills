# The daemon's model-free hand-off lint (runtime/ttp/handofflint.py). Run from test_tt_project_runtime.py.


def _code_hand_off(p, env, tid, result, dbs=None):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-lint-{tid}-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    if dbs is not None:
        dbs.append(d.p.db)
    d._prefetch_remote(tid, result)   # as the daemon does: the remote is asked before the transaction
    with d.p.db.tx():
        d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return [e["text"] for e in p.db.q("SELECT text FROM events WHERE task=? AND kind='task_notes'", (tid,))]


def _own_push(p, value):
    cfg = json.loads(p.config_path.read_text())
    d = cfg.setdefault("delivery", {})
    if value is None:
        d.pop("own_push", None)
    else:
        d["own_push"] = value
    p.config_path.write_text(json.dumps(cfg))


def test_a_code_hand_off_proposing_to_push_its_unpublished_branch_is_flagged_without_a_manual_call(env):
    p = make(env)
    remote = _with_origin(env)
    _own_push(p, None)   # unset with draft PRs on: a code task may create its own branch
    tid, path, branch = _code_task(p, "work", status="running")
    _commit_file(path, "a")
    push_it = {"status": "done", "summary": "did it",
               "followups": [{"title": f"mechanical: push {branch} and open its draft PR", "spec": "push it"}]}
    (note,) = _code_hand_off(p, env, tid, push_it)
    assert "hand-off lint:" in note and branch in note and "ttp push --own-branch" in note
    # The flag is a note, so the daemon queues no continue hop for that follow-up by itself.
    assert not p.db.q("SELECT 1 FROM tasks WHERE labels LIKE ?", (f'%"continues:{tid}"%',))
    assert not p.db.q("SELECT 1 FROM events WHERE task=? AND text LIKE '%Continue hop #%'", (tid,))
    # Clean hand-offs pass: a follow-up that pushes nothing, and an upstream note naming the branch.
    tid2, path2, branch2 = _code_task(p, "work 2", status="running")
    _commit_file(path2, "b")
    assert _code_hand_off(p, env, tid2, {"status": "done", "summary": "ok", "followups": [
        {"title": "add docs", "spec": "describe the flag"},
        {"title": "upstream: push refusal", "spec": f"pushing {branch2} was refused"}]}) == []
    # Once the branch is on the remote, the same follow-up is not flagged.
    subprocess.run(["git", "-C", str(path), "push", "-q", str(remote), f"{branch}:refs/heads/{branch}"], check=True)
    tid3 = p.db.add_task("work 3", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid3, branch=branch)
    assert _code_hand_off(p, env, tid3, push_it) == []


def test_the_hand_off_lint_skips_projects_whose_code_tasks_cannot_publish_their_branch(env):
    from ttp import handofflint
    p = make(env)
    _with_origin(env)
    _own_push(p, "off")   # the review lands the branch: no own step to leave behind
    tid, path, branch = _code_task(p, "work", status="running")
    _commit_file(path, "a")
    fup = {"status": "done", "summary": "x", "followups": [{"title": f"mechanical: push {branch}", "spec": "y"}]}
    assert _code_hand_off(p, env, tid, fup) == []
    assert handofflint.applies({"delivery": {"own_push": "on"}})
    assert handofflint.applies({"delivery": {}})
    assert not handofflint.applies({"delivery": {"own_push": "existing_only"}})
    assert not handofflint.applies({"delivery": {"draft_prs": False}})
    # The matching rule itself: a push aimed at the branch, "publish" or opening its PR; upstream notes
    # and continue hops never, nor "pushed" or "the push queue".
    def flags(title, spec="", branch="ttp/t1-x"):
        return bool(handofflint.proposes_push({"followups": [{"title": title, "spec": spec}]}, branch))
    assert flags("Push ttp/t1-x")
    assert flags("mechanical: push ttp/t1-x and open its draft PR", "push it")
    assert flags("mechanical: open PR", "push it")
    assert flags("ship it", "push the branch to origin")
    assert flags("mechanical: publish the branch")
    assert not flags("push something else")
    assert not flags("push ttp/t1-xy")
    assert not flags("upstream: push ttp/t1-x")
    assert not flags("continue: wire the watcher",
                     "Done: parser on branch ttp/t1-x, head abc123 (not pushed yet). Left: tests.")
    assert not flags("continue: push ttp/t1-x after the tests")
    assert not flags("mechanical: rename a config key", "then let the push queue deliver it")
    assert not flags("fix the push queue for ttp/t1-x")


def test_the_hand_off_lint_never_asks_the_remote_inside_a_transaction(env, monkeypatch):
    from ttp import handofflint, push
    p = make(env)
    _with_origin(env)
    _own_push(p, None)
    tid, path, branch = _code_task(p, "work", status="running")
    _commit_file(path, "a")
    real, asked, dbs = push._git_net, [], [p.db]

    def net(*a, **k):
        assert not any(db.conn.in_transaction for db in dbs), "git network call under the DB write lock"
        asked.append(a)
        return real(*a, **k)
    monkeypatch.setattr(push, "_git_net", net)
    push_it = {"status": "done", "summary": "x", "followups": [{"title": f"push {branch}", "spec": "y"}]}
    (note,) = _code_hand_off(p, env, tid, push_it, dbs)
    assert "hand-off lint:" in note and any("ls-remote" in a for a in asked)
    # No prefetched answer (an unreachable remote, or none asked): inside the transaction it flags nothing
    # and asks nothing.
    handofflint._seen.clear()
    asked.clear()
    task = p.db.task(tid)
    with p.db.tx():
        assert handofflint.lint(p, json.loads(p.config_path.read_text()), task, push_it) == []
    assert asked == []


def test_a_continue_hop_naming_its_unpublished_branch_is_still_queued_by_the_daemon(env):
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("chains.requeue", False)   # the new-hop path, not chaincap.requeue
    _with_origin(env)
    _own_push(p, None)

    def hand_off(tid, head):   # the hop names its branch and says it is not pushed yet
        branch = p.db.task(tid)["branch"]
        result = {"status": "done", "summary": "half done", "followups": [{"title": "continue: wire the watcher",
                  "spec": f"Done: parser on branch {branch}, head {head[:7]} (not pushed yet). Left: tests."}]}
        (env["tmp"] / f"run-{tid}" / "result.json").write_text(json.dumps(result))
        Daemon(p.base)._prefetch_remote(tid, result)
    root, _, done, _ = _finish_code(env, p, "feature", {"a.py": 10}, before=hand_off)
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (root,))
    assert f"Continue hop #{hop['id']} queued by the daemon" in done["text"] and "hand-off lint" not in done["text"]
    assert not p.db.q("SELECT 1 FROM events WHERE task=? AND kind='task_notes'", (root,))


def test_the_hand_off_lint_asks_the_remote_of_the_repository_the_task_worked_in(env, tmp_path):
    """A task that worked in another repository's checkout has no branch in the project root: the
    lint asks that repository's remote, so a branch already published there is not flagged, and one
    missing there still is."""
    p = make(env)
    _with_origin(env)   # the project root's remote, which never has the branch
    _own_push(p, None)
    origin2, second = tmp_path / "second.git", tmp_path / "second"
    git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)
    git(tmp_path, "init", "-q", "--bare", str(origin2))
    git(tmp_path, "init", "-q", "-b", "main", str(second))
    _commit_file(second, "lib")
    git(second, "remote", "add", "origin", str(origin2))
    git(second, "push", "-q", "origin", "main")

    def task_in_second(branch):
        tid = p.db.add_task(f"work on {branch}", "s", kind="code", tier="light", origin="user")
        wt = second / "wt" / f"t{tid}"
        git(second, "worktree", "add", "-q", "-b", branch, str(wt), "main")
        _commit_file(wt, branch.replace("/", "-"))
        p.db.update_task(tid, branch=branch)
        rid = p.db.x("INSERT INTO runs(task, role, status, started, ended) VALUES(?,?,?,?,?)",
                     (tid, "worker", "done", time.time(), time.time()))
        run_dir = p.runs / str(rid)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps({"cwd": str(wt)}))
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
        return tid, wt

    def push_it(branch):
        return {"status": "done", "summary": "did it",
                "followups": [{"title": f"mechanical: push {branch} and open its draft PR", "spec": "push it"}]}

    tid, wt = task_in_second("ttp/t1-published")
    git(wt, "push", "-q", "-u", "origin", "ttp/t1-published")
    assert _code_hand_off(p, env, tid, push_it("ttp/t1-published")) == []
    tid2, _ = task_in_second("ttp/t2-unpublished")
    (note,) = _code_hand_off(p, env, tid2, push_it("ttp/t2-unpublished"))
    assert "hand-off lint:" in note and "ttp/t2-unpublished" in note
