# Device A/B shadow arms (route.device_ab_shadow, deviceshadow.py) tests of tests/test_tt_project_runtime.py, which
# runs this file in its own namespace (see _include_cases there). Helpers come from deviceab_cases.py and
# cellns_cases.py (sorted before this file).
from __future__ import annotations

import json as _json


def _shadow_cfg(p, **extra):
    cfg = _dab_cfg(p)
    cfg["route"]["device_ab_shadow"] = {
        "arms": [{"name": "glm", "model": "glm-x", "effort": "max", "provider": "chat"},
                 {"name": "sonnet-h", "model": "sonnet", "effort": "high", "provider": "claude"},
                 {"name": "opus-h", "model": "opus", "effort": "high", "provider": "claude"}],
        "second_reviewer": {"model": "glm-x", "effort": "max", "provider": "chat"}, **extra}
    return cfg


def _shadow_done(p, tid, arm, text="## Commands\n`cat log` - read\n## Conclusion\nok", **extra):
    import pathlib
    from ttp import deviceshadow
    f = pathlib.Path(p.state) / f"h-{tid}-{arm}.md"
    f.write_text(text)
    deviceshadow.record(p.db, tid, arm, status="done", handoff=str(f), model="m:max", minutes=2.0, usd=0.5, **extra)


def test_device_shadow_is_off_by_default_and_a_project_and_account_key(env):
    from ttp import deviceshadow
    from ttp.project import ACCOUNT_KEYS, EXTRA_KEYS
    p = make(env)
    assert deviceshadow.settings(p.config()) is None
    assert "device_ab_shadow" in ACCOUNT_KEYS["route"] and "device_ab_shadow" in EXTRA_KEYS["route"]
    for bad in ("on", ["x"], {"arms": []}, {"arms": [{"name": "a"}]}, {"model": "  "}, {"model": "m", "enabled": False}):
        assert deviceshadow.settings({"route": {"device_ab_shadow": bad}}) is None, bad
    s = deviceshadow.settings(_shadow_cfg(p))
    assert list(s["arms"]) == ["glm", "sonnet-h", "opus-h"]
    assert s["arms"]["glm"] == {"provider": "chat", "model": "glm-x", "effort": "max", "harness": "mini-swe-agent"}
    assert s["arms"]["opus-h"]["provider"] == "claude" and s["arms"]["opus-h"]["harness"] == "mini-swe-agent"
    assert (s["timeout_s"], s["budget_usd"]) == (1800, 3.0)
    one = deviceshadow.settings({"route": {"device_ab_shadow": {"model": "glm-x", "harness": "nope"}}})
    assert list(one["arms"]) == ["chat-glm-x-max"] and one["arms"]["chat-glm-x-max"]["harness"] == "mini-swe-agent"


def test_device_shadow_starts_each_arm_once_with_no_lock_resource_or_ab_label(env, monkeypatch):
    import subprocess
    from ttp import deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    tid = _dab_task(p)
    task = p.db.task(tid)
    calls, real = [], subprocess.Popen

    def fake(argv, **kw):   # the arm runners are recorded, the git calls pinning the code run
        if "device-shadow" not in argv:
            return real(argv, **kw)
        calls.append((argv, kw))

    monkeypatch.setattr(subprocess, "Popen", fake)
    labels, locks = p.db.task(tid)["labels"], p.db.q("SELECT * FROM kv WHERE key LIKE '%lock%'")
    assert deviceshadow.start(p, task, cfg) == ["glm", "sonnet-h", "opus-h"]
    assert deviceshadow.start(p, task, cfg) == [], "once per task and arm"
    assert len(calls) == 3
    for argv, kw in calls:
        assert argv[1:4] == ["-m", "ttp", "device-shadow"] and argv[4] == str(tid) and kw["start_new_session"]
    assert p.db.task(tid)["labels"] == labels and p.db.q("SELECT * FROM kv WHERE key LIKE '%lock%'") == locks
    assert set(deviceshadow.shadows(p.db.q)) == {(tid, "glm"), (tid, "sonnet-h"), (tid, "opus-h")}
    # Off: nothing starts.
    assert deviceshadow.start(p, p.db.task(_dab_task(p)), p.config()) == []
    assert len(calls) == 3


def test_device_shadow_is_out_of_the_live_ab_counts_and_stop_rules(env):
    from ttp import deviceab, deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    tid = _dab_task(p)
    _shadow_done(p, tid, "glm", called="cli 1; --system-prompt; no CLAUDE.md")
    deviceshadow.record(p.db, tid, "opus-h", status="error", error="boom")
    before = deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), cfg)
    assert before["arms"] == {} or all(v.get("tasks", 0) == 0 for v in before["arms"].values())
    assert before["shadow"]["arms"]["glm"]["called"].startswith("cli 1")
    assert before["shadow"]["arms"]["opus-h"]["failed"] == 1
    assert deviceab.find_incident(p.db, cfg, deviceab.settings(cfg), 0.0, 1e12) is None


def test_device_shadow_review_waits_for_the_live_task_and_then_asks_two_blind_reviewers(env):
    from ttp import deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    tid = _dab_task(p)
    _shadow_done(p, tid, "glm")
    deviceshadow.record(p.db, tid, "sonnet-h", status="running")
    second = {"model": "glm-x", "effort": "max", "provider": "chat"}
    assert deviceshadow.queue_reviews(p.db, cfg, second=second) == [], "the live task has not ended"
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    new = deviceshadow.queue_reviews(p.db, cfg, second=second)
    assert len(new) == 2, "a still running shadow has no review"
    first, sec = (p.db.task(i) for i in new)
    # An opaque code names the arm in the label and title (blindreview), and the report maps it back.
    from ttp import blindreview
    code = blindreview.code_for(p.db, tid, "glm")
    assert f'device_shadow_review:{tid}:{code}' in first["labels"] and f'device_shadow_review2:{tid}:{code}' in sec["labels"]
    assert blindreview.arm_of(p.db.q, code) == "glm" and blindreview.BLIND_LABEL in first["labels"]
    assert sec["provider"] == "chat" and sec["model"] == "glm-x" and first["provider"] != "chat"
    for t in (first, sec):
        low = (t["spec"] + t["title"]).lower()
        assert "glm" not in low and "sonnet" not in low and "device_shadow_scores" in t["spec"]
        assert "## Conclusion" in t["spec"]
    assert deviceshadow.queue_reviews(p.db, cfg, second=second) == [], "once per shadow and reviewer"
    deviceshadow.record(p.db, tid, "sonnet-h", status="done", handoff="")
    assert deviceshadow.queue_reviews(p.db, cfg, second=second) == [], "no hand-off: never reviewed"


def test_device_shadow_report_compares_arms_reviewers_and_whether_the_ranking_differs(env):
    from ttp import deviceab, deviceshadow
    p = make(env)
    t1, t2 = _dab_task(p), _dab_task(p)
    for t in (t1, t2):
        for arm in ("glm", "opus-h"):
            _shadow_done(p, t, arm, called="flags" if arm == "opus-h" else "")
    p.db.x("UPDATE tasks SET status='done'")

    def review(tid, arm, agreement, label, harmful=(), spent=0.1):
        rid = p.db.add_task("r", "s", kind="question", labels=[f"{label}{tid}:{arm}"])
        res = _json.dumps({"device_shadow_scores": {"agreement": agreement,
                                                    "harmful": [{"step": s, "why": "w"} for s in harmful]}})
        p.db.x("UPDATE tasks SET result=?, status='done', spent_usd=? WHERE id=?", (res, spent, rid))

    for t in (t1, t2):
        review(t, "glm", 4, deviceshadow.REVIEW_LABEL)
        review(t, "opus-h", 3, deviceshadow.REVIEW_LABEL, harmful=["tt-smi -r"] if t == t1 else ())
        review(t, "glm", 2, deviceshadow.REVIEW2_LABEL)
        review(t, "opus-h", 5, deviceshadow.REVIEW2_LABEL)
    r = deviceshadow.report(p.db.q)
    g, o = r["arms"]["glm"], r["arms"]["opus-h"]
    assert (g["agreement"], g["agrees_4_plus"], g["harmful_steps"], g["reviewed"]) == (4.0, 2, 0, 2)
    assert (o["agreement"], o["harmful_steps"], o["harmful_tasks"], o["called"]) == (3.0, 1, [t1], "flags")
    assert (g["agreement2"], o["agreement2"]) == (2.0, 5.0)
    assert r["ranking"] == ["glm", "opus-h"] and r["ranking_second"] == ["opus-h", "glm"] and r["ranking_differs"]
    assert r["review_usd"] == 0.8 and g["usd"] == 1.0 and g["minutes"] == 4.0
    text = "\n".join(deviceshadow.report_text(r))
    assert "the ranking differs" in text and "second reviewer 5.0/5" in text and "called: flags" in text
    assert deviceshadow.report(lambda *a, **k: []) is None
    # The live A/B report carries the section, and shadow tasks never count as live arms.
    live = deviceab.report(p.db.q, lambda k: p.db.kv(k, ""), _shadow_cfg(p))
    assert live["shadow"]["ranking_differs"] and "Shadow arms" in deviceab.report_text(live)


def test_device_shadow_scrub_and_deny_tools(tmp_path):
    import subprocess
    from ttp import deviceshadow
    text = deviceshadow.scrub("path /root/proj/x token=abcd1234efgh ghp_abcdefghijklmnop Bearer abcdefghijkl1234 ok",
                              {"/root/proj": "/ro/code"})
    assert "/ro/code/x" in text and "abcd1234efgh" not in text and "ghp_" not in text and "abcdefghijkl1234" not in text
    assert text.endswith("ok")
    d = deviceshadow.deny_tools(tmp_path / "deny")
    assert {f.name for f in d.iterdir()} == set(deviceshadow.DENIED)
    r = subprocess.run([str(d / "ssh"), "host"], capture_output=True, text=True)
    assert r.returncode == 126 and "refused" in r.stderr


def test_device_shadow_snapshot_is_a_scrubbed_copy_without_the_live_tasks_rows(env, tmp_path):
    import subprocess
    from ttp import deviceshadow
    p = make(env)
    tid = _dab_task(p)
    other = p.db.add_task("other task", "spec token=hunter2hunter2", kind="work")
    (pathlib.Path(p.logs)).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(p.logs) / "a.log").write_text("line password=letmein123\nlast\n")
    dest = tmp_path / "state"
    deviceshadow.snapshot_state(p, tid, dest, {})
    rows = [_json.loads(x) for x in (dest / "tasks.jsonl").read_text().splitlines()]
    assert other in {r["id"] for r in rows} and tid not in {r["id"] for r in rows}
    blob = (dest / "tasks.jsonl").read_text() + (dest / "logs" / "a.log").read_text()
    assert "hunter2hunter2" not in blob and "letmein123" not in blob and "last" in blob
    repo = tmp_path / "srcrepo"
    repo.mkdir()
    (repo / "f.txt").write_text("hello")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=a", "-c", "user.email=a@b", "commit",
                                                              "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    assert deviceshadow.snapshot_code(repo, "nope", tmp_path / "code", 5) == ""
    assert (tmp_path / "code" / "f.txt").read_text() == "hello"
    assert "over" in deviceshadow.snapshot_code(repo, "HEAD", tmp_path / "code2", 0.000001)


def test_device_shadow_cell_is_read_only_refuses_ssh_and_hides_the_project(env, tmp_path):
    import os
    import subprocess
    from ttp import benchrun, cellenv, deviceshadow
    _any_namespace()
    p = make(env)
    tmp = tmp_path / "t"
    ro, ws = tmp / "ro", tmp / "work"
    (ro / "code").mkdir(parents=True)
    (ro / "code" / "f.txt").write_text("code")
    ws.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q",
                                                "--allow-empty", "-m", "s"]):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)
    cellenv.make(tmp / "venv", seed=())
    opts = {"dry_run": True, "ro_binds": [str(ro)], "deny_tools": str(deviceshadow.deny_tools(tmp / "deny"))}
    hidden = [pathlib.Path(p.root).resolve(), pathlib.Path(p.state).resolve()]
    cell, problems = benchrun.isolate(tmp, ws, {"provider": "chat", "model": "m", "effort": "max"}, opts, hidden,
                                      [str(p.root)])
    assert problems == [], problems
    script = ('echo x > "$RO/code/f.txt" 2>/dev/null; echo "write_ro=$?"; echo ok > "$RO/new" 2>/dev/null; '
              'echo "new_ro=$?"; echo y > scratch.txt; echo "write_ws=$?"; ssh h true 2>/dev/null; echo "ssh=$?"; '
              'rsync a b 2>/dev/null; echo "rsync=$?"; /usr/bin/ssh h true 2>/dev/null; echo "usrssh=$?"; '
              'ls "$STATE" >/dev/null 2>&1; echo "state=$?"; ls "$ROOT" >/dev/null 2>&1; echo "root=$?"; cat "$RO/code/f.txt"')
    r = subprocess.run(cell.wrap(["sh", "-c", script]), cwd=str(ws), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "RO": str(ro), "STATE": str(p.state), "ROOT": str(p.root),
                            **{k: v for k, v in cell.sets().items()}})
    got = dict(x.split("=", 1) for x in r.stdout.split() if "=" in x)
    assert got["write_ro"] != "0" and got["new_ro"] != "0", r.stdout + r.stderr
    assert got["write_ws"] == "0" and (ws / "scratch.txt").read_text() == "y\n"
    assert got["ssh"] == "126" and got["rsync"] == "126", "refused, not merely missing"
    assert got["usrssh"] != "0" and got["state"] != "0" and got["root"] != "0"
    assert r.stdout.strip().endswith("code") and (ro / "code" / "f.txt").read_text() == "code"


def test_cell_standin_covers_a_system_tool_that_is_a_link(tmp_path, monkeypatch):
    # An alternatives-style link (/usr/bin/x -> /etc/alternatives/x -> a file) cannot be a mount point: the
    # stand-in covers the file it leads to, once.
    from ttp import cellns
    backend = _any_namespace()
    real = tmp_path / "real-tool"
    real.write_text("x")
    link = tmp_path / "bin" / "tool"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(cellns, "SYSTEM", (str(tmp_path),))
    monkeypatch.setenv("PATH", str(link.parent))
    stand = tmp_path / "stand"
    stand.write_text("#!/bin/sh\nexit 126\n")
    cell = cellns.build(tmp_path / "cell", tmp_path, [], python="/usr/bin/python3", name=backend, standins={"tool": stand})
    joined = " ".join(map(str, cell.prefix))
    assert cell.prefix and str(link) not in joined
    if cell.backend == "bwrap":
        assert joined.count(str(real.resolve())) == 1 and f"--ro-bind {stand} {real.resolve()}" in joined


def test_device_shadow_runner_is_a_hidden_command():
    import argparse
    from ttp import cli
    assert callable(cli.cmd_device_shadow)
    src = pathlib.Path(cli.__file__).read_text()
    assert '"device-shadow"' in src and "argparse.SUPPRESS" in src


def test_device_shadow_cell_command_cannot_reach_a_host_listener(env, tmp_path):
    # A shadow arm's cell has an empty network of its own: a service listening on the host's loopback is
    # unreachable from a command in it by a raw socket and by urllib; the model calls are made outside.
    import os
    import subprocess
    import sys
    from ttp import benchrun, cellenv, deviceshadow
    _any_namespace()
    p = make(env)
    tmp = tmp_path / "t"
    ro, ws = tmp / "ro", tmp / "work"
    (ro / "code").mkdir(parents=True)
    ws.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q",
                                                "--allow-empty", "-m", "s"]):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)
    cellenv.make(tmp / "venv", seed=())
    opts = {"dry_run": True, "ro_binds": [str(ro)], "deny_tools": str(deviceshadow.deny_tools(tmp / "deny"))}
    hidden = [pathlib.Path(p.root).resolve(), pathlib.Path(p.state).resolve()]
    arm = {"provider": "claude", "model": "sonnet", "effort": "high", "harness": "mini-swe-agent"}
    cell, problems = benchrun.isolate(tmp, ws, arm, opts, hidden, [str(p.root)])
    assert problems == [], problems
    host = _HostListener()
    try:
        script = tmp / "reach.py"
        script.write_text(_REACH)
        r = subprocess.run(cell.wrap([cell.python, "-I", str(script), f"127.0.0.1:{host.port}"]), cwd=str(ws),
                           capture_output=True, text=True, timeout=60, env={**os.environ, **cell.sets()})
        assert r.stdout.strip() == "socket=unreachable urllib=unreachable", (r.stdout, r.stderr)
    finally:
        host.close()


def test_device_shadow_claude_arm_on_mini_swe_agent_is_allowed_and_records_how_it_was_called(env, monkeypatch):
    # A shadow arm {provider claude, model sonnet, effort high, harness mini-swe-agent} is kept as written (no
    # chat-only check refuses it) and its run records the call its runner wrote to exit.json as `called`.
    import pathlib
    from ttp import benchrun, cellenv, deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    cfg["route"]["device_ab_shadow"]["arms"][1]["harness"] = "mini-swe-agent"
    s = deviceshadow.settings(cfg)
    assert s["arms"]["sonnet-h"] == {"provider": "claude", "model": "sonnet", "effort": "high",
                                     "harness": "mini-swe-agent"}
    benchrun.check_harness("sonnet-h", s["arms"]["sonnet-h"])
    tid = _dab_task(p)
    p.config = lambda: cfg

    class Cell:
        conf = {}

        def close(self):
            pass
    seen = {}

    def launch(run_dir, ws, body, arm, opts, cfg_, hidden, cell_env, conf, logins, cell):
        seen["arm"] = arm
        pathlib.Path(run_dir).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(run_dir) / "exit.json").write_text(_json.dumps({"called": "claude 9.9 --system-prompt <prompt>"}))
        (pathlib.Path(ws) / "handoff.md").write_text("## Commands\n`cat log` - read\n## Conclusion\nok")
        return "claude", 1.0, False
    monkeypatch.setattr(benchrun, "harness_ready", lambda kind, version="", root=None: {"dir": "x"})
    monkeypatch.setattr(benchrun, "isolate", lambda *a, **k: (Cell(), []))
    monkeypatch.setattr(benchrun, "launch", launch)
    monkeypatch.setattr(cellenv, "make", lambda *a, **k: pathlib.Path("/nonexistent-venv"))
    monkeypatch.setattr(cellenv, "env", lambda *a, **k: {})
    got = deviceshadow.run(p, tid, "sonnet-h")
    assert got["status"] == "done" and got["called"] == "claude 9.9 --system-prompt <prompt>", got
    assert seen["arm"]["provider"] == "claude" and seen["arm"]["harness"] == "mini-swe-agent"


def _git_repo(path, text):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    (path / "f.txt").write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=a", "-c", "user.email=a@b", "commit",
                                                              "-q", "-m", text]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_device_shadow_code_copy_holds_the_repo_files_at_the_tasks_pinned_sha_for_every_arm(env, tmp_path,
                                                                                           monkeypatch):
    import subprocess
    from ttp import deviceshadow
    # The project repo: pinned once, every arm's copy holds that commit's tree even after the repo moves on.
    repo = tmp_path / "repo"
    sha = _git_repo(repo, "v1")
    pins = deviceshadow.code_pins(repo, "nope")
    assert pins == [["", sha]]
    _git_repo(repo, "v2")
    for arm in ("glm", "sonnet-h", "opus-h"):
        assert deviceshadow.snapshot_code(repo, "HEAD", tmp_path / arm, 5, pins) == ""
        assert (tmp_path / arm / "f.txt").read_text() == "v1"
    # A project root that is not a repo: each repo one level under it at its HEAD, never the harness folder,
    # hidden folders or plain folders.
    root = tmp_path / "ops"
    a_sha = _git_repo(root / "tools", "tool")
    _git_repo(root / "tt-project", "harness")
    _git_repo(root / ".hidden", "hidden")
    (root / "plain").mkdir()
    (root / "tt-project" / "state").mkdir()
    pins = deviceshadow.code_pins(root, "HEAD", (root / "tt-project" / "state",))
    assert pins == [["tools", a_sha]]
    assert deviceshadow.snapshot_code(root, "HEAD", tmp_path / "ops-code", 5, pins) == ""
    assert (tmp_path / "ops-code" / "tools" / "f.txt").read_text() == "tool"
    assert sorted(x.name for x in (tmp_path / "ops-code").iterdir()) == ["tools"]
    # start() pins the code once and every arm's record carries the same pins.
    p = make(env)
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: None if "device-shadow" in argv else real(argv, **k))
    tid = _dab_task(p)
    assert len(deviceshadow.start(p, p.db.task(tid), _shadow_cfg(p))) == 3
    got = {str(rec.get("code")) for rec in deviceshadow.shadows(p.db.q).values()}
    assert len(got) == 1 and "None" not in got


def test_device_shadow_empty_code_copy_is_retried_then_a_recorded_skip_not_a_run(env, monkeypatch, tmp_path):
    from ttp import benchrun, deviceshadow
    monkeypatch.setattr(deviceshadow, "COPY_RETRY_S", 0)
    calls = []

    def empty(root, ref, dest, cap_mb, pins=None):
        calls.append(dest)
        dest.mkdir(parents=True, exist_ok=True)
        return "git archive of the project repo at abc failed; no code copy"
    monkeypatch.setattr(deviceshadow, "snapshot_code", empty)

    def launch(*a, **k):
        raise AssertionError("an arm ran on an empty code copy")
    monkeypatch.setattr(benchrun, "launch", launch)
    monkeypatch.setattr(benchrun, "isolate", launch)
    p = make(env)
    cfg = _shadow_cfg(p)
    p.config = lambda: cfg
    tid = _dab_task(p)
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "skipped" and len(calls) == 2, got
    assert "empty after a retry" in got["error"] and "git archive of the project repo" in got["error"]
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert deviceshadow.queue_reviews(p.db, cfg) == [], "a skip is never reviewed"
    r = deviceshadow.report(p.db.q)
    assert r["arms"]["glm"]["skipped"] == 1 and r["arms"]["glm"]["done"] == 0
    text = "\n".join(deviceshadow.report_text(r))
    assert "skipped 1" in text and f"skipped #{tid}: no arm run: the code copy is empty" in text
    # A retry that finds the files runs on.
    calls.clear()

    def second_time(root, ref, dest, cap_mb, pins=None):
        calls.append(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if len(calls) == 2:
            (dest / "f.txt").write_text("x")
        return ""
    monkeypatch.setattr(deviceshadow, "snapshot_code", second_time)
    assert deviceshadow.copy_code(tmp_path, "HEAD", tmp_path / "code", 5, None) == (True, "") and len(calls) == 2


def test_device_shadow_copy_finds_nested_and_bare_repos_and_always_holds_a_code_tasks_own_repo_at_its_base(
        env, tmp_path, monkeypatch):
    import subprocess
    from ttp import deviceshadow
    monkeypatch.setattr(deviceshadow, "COPY_RETRY_S", 0)

    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    # A project root that is not a repo: a checkout one level down, one two levels down, a bare mirror two
    # levels down (its main moved on past the code task's base, and a topic branch on it), and a repo too deep.
    root = tmp_path / "proj"
    tools = _git_repo(root / "tools", "tool")
    app = _git_repo(root / "work" / "app", "app")
    src = tmp_path / "src"
    base = _git_repo(src, "base")
    git(src, "checkout", "-q", "-b", "topic")
    (src / "f.txt").write_text("topic")
    git(src, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qam", "topic")
    git(src, "checkout", "-q", "main")
    (src / "f.txt").write_text("later")
    git(src, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qam", "later")
    later = git(src, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", "-q", "--mirror", str(src), str(root / "work" / "lib-mirror.git")], check=True)
    _git_repo(root / "a" / "b" / "c" / "deep", "deep")
    git(root / "work" / "app", "worktree", "add", "-q", str(root / "work" / "app-wt"))   # a view, not a repo
    _git_repo(root / "tmp" / "copy", "copy")   # a throwaway folder
    _git_repo(root / "tt-project", "harness")
    (root / "tt-project" / "state").mkdir()
    skip = (root / "tt-project" / "state",)
    assert deviceshadow.find_repos(root, skip) == ["tools", "work/app", "work/lib-mirror.git"]
    # Any other kind: every repo found, the bare mirror at its HEAD.
    pins = deviceshadow.code_pins(root, "HEAD", skip, {"id": 1, "kind": "work", "spec": "Run it."})
    assert pins == [["tools", tools], ["work/app", app], ["work/lib-mirror.git", later]]
    # A code task whose repo is the bare mirror: that repo pinned at the commit it branched from.
    task = {"id": 2, "kind": "code", "spec": f"Rework the change in work/lib-mirror.git on main {base[:12]}; "
                                              f"it replaces {later}."}
    pins = deviceshadow.code_pins(root, "HEAD", skip, task)
    assert ["work/lib-mirror.git", base, "task"] in pins and len(pins) == 3, pins
    ok, note = deviceshadow.copy_code(root, "HEAD", tmp_path / "code", 5, pins, task, skip)
    assert ok and note == "", note
    assert (tmp_path / "code" / "work" / "lib-mirror.git" / "f.txt").read_text() == "base"
    assert (tmp_path / "code" / "work" / "app" / "f.txt").read_text() == "app"
    # The branch it builds on finds the repo and its head, with no path in the spec.
    task = {"id": 3, "kind": "code", "spec": "pr_branch: topic\nFix the bug.", "labels": '["pr_branch:topic"]'}
    assert ["work/lib-mirror.git", git(src, "rev-parse", "topic"), "task"] in deviceshadow.code_pins(
        root, "HEAD", skip, task)
    # A code task whose repo is not found: the copy is not good enough to run an arm.
    task = {"id": 4, "kind": "code", "spec": "Fix the bug."}
    ok, note = deviceshadow.copy_code(root, "HEAD", tmp_path / "code2", 5, None, task, skip)
    assert not ok and "lacks the code task's own repo (not found under the project root)" in note
    # A project root that is a repo: a code task's copy is that repo at the task's base.
    pins = deviceshadow.code_pins(src, "HEAD", (), {"id": 5, "kind": "code", "spec": f"On {base}."})
    assert pins == [["", base, "task"]]


def test_device_shadow_copy_lacking_a_code_tasks_repo_is_retried_then_a_recorded_skip(env, monkeypatch):
    from ttp import benchrun, deviceshadow
    monkeypatch.setattr(deviceshadow, "COPY_RETRY_S", 0)
    calls = []

    def other_repo_only(root, ref, dest, cap_mb, pins=None):
        calls.append(dest)
        (dest / "other").mkdir(parents=True, exist_ok=True)
        (dest / "other" / "f.txt").write_text("x")
        return ""
    monkeypatch.setattr(deviceshadow, "snapshot_code", other_repo_only)
    monkeypatch.setattr(deviceshadow, "code_pins", lambda *a, **k: [["other", "abc1234"]])

    def launch(*a, **k):
        raise AssertionError("an arm ran without the code task's repo")
    monkeypatch.setattr(benchrun, "launch", launch)
    monkeypatch.setattr(benchrun, "isolate", launch)
    p = make(env)
    cfg = _shadow_cfg(p)
    p.config = lambda: cfg
    tid = _dab_task(p, kind="code")
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "skipped" and len(calls) == 2, got
    assert "lacks the code task's own repo" in got["error"]
    text = "\n".join(deviceshadow.report_text(deviceshadow.report(p.db.q)))
    assert f"skipped #{tid}: no arm run: the code copy lacks the code task's own repo" in text


def test_a_second_reviewer_whose_provider_is_not_allowed_gets_no_twin_and_the_first_review_says_why(env):
    # upstream note 04acfea4503e3ff5: with providers_allowed leaving out the second reviewer's provider, its pinned
    # twin could never start and raised ready_not_started per review. No twin; the first review records why.
    from ttp import deviceab, deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    assert deviceab.tick(p.db, {**cfg, "providers_allowed": ["claude"]}, 1.0)["reviews"] == []   # the A/B starts
    allowed = deviceab.second_reviewer(cfg)
    assert allowed["provider"] == "chat" and "skip" not in allowed, "unset providers_allowed: every provider"
    assert "skip" not in deviceab.second_reviewer({**cfg, "providers_allowed": ["claude", "chat"]})
    for listed in (["claude"], "claude", ["claude", "codex"]):
        got = deviceab.second_reviewer({**cfg, "providers_allowed": listed})
        assert got["provider"] == "chat" and got["skip"] == "its provider is not in providers_allowed", listed
    # A second reviewer on the core provider is always allowed.
    core = {**cfg, "providers_allowed": ["codex"], "route": {**cfg["route"], "device_ab_shadow": {
        **cfg["route"]["device_ab_shadow"], "second_reviewer": {"model": "sonnet", "provider": "claude"}}}}
    assert "skip" not in deviceab.second_reviewer(core)
    skip = deviceab.second_reviewer({**cfg, "providers_allowed": ["claude"]})
    # The live A/B's reviews.
    tid = _dab_task(p, labels=("device_ab:glm", "needs_device"))
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    new = deviceab.queue_reviews(p.db, deviceab.settings(cfg), 0.0, second=skip)
    assert len(new) == 1
    first = p.db.task(new[0])
    assert first["provider"] in (None, "") and "second_reviewer_skipped:its provider is not in providers_allowed" in first["labels"] and "chat" not in first["labels"]
    assert deviceab.queue_reviews(p.db, deviceab.settings(cfg), 0.0, second=skip) == [], "no twin later either"
    assert not p.db.q("SELECT id FROM tasks WHERE provider='chat'"), "no task pinned outside providers_allowed"
    # Allowed again: the twin is queued as before, the first review is not repeated.
    again = deviceab.queue_reviews(p.db, deviceab.settings(cfg), 0.0, second=allowed)
    assert len(again) == 1 and p.db.task(again[0])["provider"] == "chat"
    assert "second_reviewer_skipped" not in p.db.task(again[0])["labels"]
    # The shadow's reviews.
    _shadow_done(p, tid, "glm")
    got = deviceshadow.queue_reviews(p.db, cfg, second=skip)
    assert len(got) == 1 and "second_reviewer_skipped:" in p.db.task(got[0])["labels"]
    assert deviceshadow.queue_reviews(p.db, cfg, second=skip) == []
    both = deviceshadow.queue_reviews(p.db, cfg, second=allowed)
    assert len(both) == 1 and p.db.task(both[0])["provider"] == "chat"
    # The daemon's path: tick reads the config and queues no twin outside providers_allowed.
    tid2 = _dab_task(p, labels=("device_ab:glm", "needs_device"))
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid2,))
    out = deviceab.tick(p.db, {**cfg, "providers_allowed": ["claude"]}, 2.0)
    assert len(out["reviews"]) == 1 and all(p.db.task(i)["provider"] != "chat" for i in out["reviews"])


def _shadow_fake_run(p, monkeypatch):
    # run()'s cell and agent stood in: returns the launches seen (each with its cell env).
    import pathlib
    from ttp import benchrun, cellenv
    launches = []

    class Cell:
        conf = {}

        def close(self):
            pass

    def launch(run_dir, ws, body, arm, opts, cfg_, hidden, cell_env, conf, logins, cell):
        launches.append(dict(cell_env))
        pathlib.Path(run_dir).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(run_dir) / "exit.json").write_text("{}")
        (pathlib.Path(ws) / "handoff.md").write_text("## Commands\n`pytest -q` - run the suite\n## Conclusion\nok")
        return "chat", 1.0, False
    monkeypatch.setattr(benchrun, "harness_ready", lambda kind, version="", root=None: {"dir": "x"})
    monkeypatch.setattr(benchrun, "isolate", lambda *a, **k: (Cell(), []))
    monkeypatch.setattr(benchrun, "launch", launch)
    monkeypatch.setattr(cellenv, "make", lambda *a, **k: pathlib.Path("/nonexistent-venv"))
    monkeypatch.setattr(cellenv, "env", lambda *a, **k: {})
    return launches


def test_device_shadow_on_a_paused_host_or_resource_starts_and_runs_no_arm_and_records_the_skip(env, monkeypatch):
    # The shadow's agent may run the project's suites on this host: a pause of a resource the live task names, or
    # of this host, keeps every arm off, recorded `skipped` with the pause (never done), at start and in run.
    import subprocess
    from ttp import coordinator, deviceshadow, project
    p = make(env)
    cfg = _shadow_cfg(p)
    p.config = lambda: cfg
    tid = _dab_task(p)
    spawned, real = [], subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: spawned.append(argv) if "device-shadow" in argv
                        else real(argv, **k))
    coordinator.pause_resource(p, "box1-device", True, reason="maintenance", by="user")
    assert deviceshadow.start(p, p.db.task(tid), cfg) == [] and spawned == []
    recs = deviceshadow.shadows(p.db.q)
    assert {arm for (_, arm) in recs} == {"glm", "sonnet-h", "opus-h"}
    for rec in recs.values():
        assert rec["status"] == "skipped" and "box1-device is paused" in rec["error"] and "maintenance" in rec["error"]
    assert f"skipped #{tid}: no arm run: box1-device is paused" in "\n".join(deviceshadow.report_text(
        deviceshadow.report(p.db.q)))
    # A run started before the pause still runs no suite: the check before the agent starts.
    launches = _shadow_fake_run(p, monkeypatch)
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "skipped" and "box1-device is paused" in got["error"] and launches == []
    # This host's own pause keeps an arm off too.
    coordinator.pause_resource(p, "box1-device", False, by="user")
    coordinator.pause_resource(p, project.hostname(), True, reason="host upkeep", by="user")
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "skipped" and "host upkeep" in got["error"] and launches == []
    coordinator.pause_resource(p, project.hostname(), False, by="user")
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "done" and len(launches) == 1, got


def test_device_shadow_run_takes_the_hosts_test_slots_for_its_agent_and_never_runs_beside_a_full_pool(env, monkeypatch):
    # The arm's agent runs on a grant of the host's test slots (as a worker's checks do), held for its life and
    # passed to it; a pool that stays full for the arm's time limit is a recorded skip, never a run beside it.
    from ttp import deviceshadow, testslots
    p = make(env)
    cfg = _shadow_cfg(p, timeout_s=60)
    p.config = lambda: cfg
    tid = _dab_task(p)
    launches = _shadow_fake_run(p, monkeypatch)
    asked, closed = [], []

    class Grant:
        k = 2

        def env(self):
            return {"TTP_TEST_SLOTS": "2"}

        def close(self):
            closed.append(len(launches))
    monkeypatch.setattr(testslots, "acquire", lambda holder, **k: asked.append((holder, k)) or None)
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "skipped" and "test slots" in got["error"] and launches == []
    assert asked[0][0] == f"device-shadow #{tid} glm" and asked[0][1]["wait_s"] == 60.0
    assert asked[0][1]["project_name"] == p.name
    monkeypatch.setattr(testslots, "acquire", lambda holder, **k: Grant())
    got = deviceshadow.run(p, tid, "glm")
    assert got["status"] == "done", got
    assert launches == [{"TTP_TEST_SLOTS": "2"}] and closed == [1], "held while the agent ran, then released"


def test_device_shadow_and_ab_queue_no_review_while_switched_off(env):
    # upstream note 0a8b27dbbacdec8d: the daemon's 5-minute tick queued shadow reviews with the shadow switched
    # off. Each place that queues A/B or shadow work honours its off switch; this checks the review queuers.
    from ttp import deviceab, deviceshadow
    p = make(env)
    cfg = _shadow_cfg(p)
    tid = _dab_task(p, labels=("device_ab:glm", "needs_device"))
    _shadow_done(p, tid, "glm")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    shadow_off = {**cfg, "route": {**cfg["route"], "device_ab_shadow": {
        **cfg["route"]["device_ab_shadow"], "enabled": False}}}
    ab_off = {**cfg, "route": {**cfg["route"], "device_ab": {**cfg["route"]["device_ab"], "enabled": False}}}
    for off in ({}, {"route": {}}, shadow_off):
        assert deviceshadow.queue_reviews(p.db, off) == [], off
    assert deviceab.settings(ab_off) is None and deviceab.settings(cfg) is not None
    assert deviceab.tick(p.db, ab_off, 1.0) == {}
    assert deviceab.queue_reviews(p.db, deviceab.settings(ab_off), 0.0) == []
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Device %review%'"), "nothing queued while off"
    assert len(deviceshadow.queue_reviews(p.db, cfg)) == 1 and len(deviceab.queue_reviews(p.db, deviceab.settings(cfg), 0.0)) == 1
