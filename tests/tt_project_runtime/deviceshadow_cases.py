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
    calls = []

    class Fake:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(subprocess, "Popen", Fake)
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
    tid = _dab_task(p)
    _shadow_done(p, tid, "glm")
    deviceshadow.record(p.db, tid, "sonnet-h", status="running")
    second = {"model": "glm-x", "effort": "max", "provider": "chat"}
    assert deviceshadow.queue_reviews(p.db, second=second) == [], "the live task has not ended"
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    new = deviceshadow.queue_reviews(p.db, second=second)
    assert len(new) == 2, "a still running shadow has no review"
    first, sec = (p.db.task(i) for i in new)
    assert f'device_shadow_review:{tid}:glm' in first["labels"] and f'device_shadow_review2:{tid}:glm' in sec["labels"]
    assert sec["provider"] == "chat" and sec["model"] == "glm-x" and first["provider"] != "chat"
    for t in (first, sec):
        low = t["spec"].lower()
        assert "glm" not in low and "sonnet" not in low and "device_shadow_scores" in t["spec"]
        assert "## Conclusion" in t["spec"]
    assert deviceshadow.queue_reviews(p.db, second=second) == [], "once per shadow and reviewer"
    deviceshadow.record(p.db, tid, "sonnet-h", status="done", handoff="")
    assert deviceshadow.queue_reviews(p.db, second=second) == [], "no hand-off: never reviewed"


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
        r = subprocess.run(cell.wrap([sys.executable, "-I", str(script), f"127.0.0.1:{host.port}"]), cwd=str(ws),
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
