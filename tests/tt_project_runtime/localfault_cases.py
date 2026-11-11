# Local fault tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new localfault.py tests here.
from __future__ import annotations


def _lf_handoff(p, tid, status="failed", marker=None, used=(), **res):
    """A hand-off of `status` through the daemon's hand-off path, its run dir holding `marker` (a file
    name, or (name, text)) and an onhost record of the resources in `used`."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p.db.update_task(tid, status="running")
    run_dir = p.base / f"lf-run-{tid}-{time.time_ns()}"
    run_dir.mkdir()
    for m in [marker] if isinstance(marker, (str, tuple)) else marker or []:
        name, text = m if isinstance(m, tuple) else (m, "{}\n")
        (run_dir / name).write_text(text)
    if used:
        from ttp import onhost
        onhost.record(run_dir, list(used))
    (run_dir / "result.json").write_text(json.dumps({"status": status, "summary": "could not go on", **res}))
    Daemon(p.base)._finish_worker({"task": tid, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
    return p.db.q("SELECT * FROM events WHERE task=? AND kind=? ORDER BY id", (tid, f"task_{status}"))[-1]


def test_local_faults_on_a_resource_the_run_never_used_are_counted_apart_and_raise_no_trouble(env):
    """A guard refusal, a credential alert or a check bootstrap fault fails a run on its own host: on a
    resource it never held, such a hand-off is counted apart (local), raises no resource_trouble event and
    gives no migrate advice. Guards stay enforced: only the count moves."""
    p = make(env)
    from ttp import localfault, machines as mm
    from ttp.daemon import Daemon
    from ttp.envfault import MARKER as ENV
    from ttp.hook import REFUSALS_FILE
    from ttp.workerloss import MARKER as LOSS
    mm.add("box-a", tags="cpu")
    mm.add("box-b", tags="cpu")
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    refusal = (REFUSALS_FILE, json.dumps({"ts": 1, "kind": "provenance", "command": "edit"}) + "\n")
    cases = [("guard", {"marker": refusal}), ("check_bootstrap", {"marker": ENV}),
             ("check_bootstrap", {"marker": LOSS}),
             ("credential", {"cred_changed": [["/h/.config/gh/hosts.yml", "changed"]]})]
    for kind, kw in cases:
        ev = _lf_handoff(p, tid, "blocked" if kind == "guard" else "failed", **kw)
        assert json.loads(ev["data"]) == {"local_fault": [kind], "used": []}, (kind, ev["data"])
    s = mm.stats(p.db)["box-a"]
    assert s["handoffs"] == 0 and s["failures"] == 0 and s["local"] == 4, s
    p.db.add_task("next soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    Daemon(p.base).check_resource_trouble(every_s=0)
    assert not p.db.q("SELECT id FROM events WHERE kind='resource_trouble'"), "local faults raised resource trouble"
    line = mm.trouble_line("box-a", s, mm.load(), set())
    assert "4 local faults not counted (guard refusals, credential alerts, check bootstrap faults" in line, line
    assert "waits only" not in line and "machines sharing its tags: box-b" in line
    # A bad run whose run dir shows a local fault is counted apart too; one without counts.
    bad = p.base / "lf-bad"
    bad.mkdir()
    (bad / ENV).write_text("{}\n")
    p.db.x("INSERT INTO runs(task, role, status, dir, started, ended) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "failed", str(bad), time.time() - 70, time.time() - 60))
    assert mm.stats(p.db)["box-a"]["runs"] == 0 and mm.stats(p.db)["box-a"]["local"] == 5
    assert localfault.kinds(p.base / "nowhere") == [] and localfault.tag(bad, {}, {"box-a"}) == \
        {"local_fault": ["check_bootstrap"], "used": []}


def test_failures_of_commands_that_ran_on_the_resource_still_count_and_raise_trouble(env):
    """The same local markers on a run that held the resource (a `ttp lock` grant, an exclusive hold or a
    remote command there) count against it, as does a failure with no local fault: trouble still starts."""
    p = make(env)
    from ttp import machines as mm
    from ttp.daemon import Daemon
    from ttp.envfault import MARKER as ENV
    mm.add("box-a", tags="cpu")
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    ev = _lf_handoff(p, tid, marker=ENV, used=["box-a"])
    assert json.loads(ev["data"]) == {"local_fault": ["check_bootstrap"], "used": ["box-a"]}
    assert _lf_handoff(p, tid)["data"] is None, "a plain failure was tagged local"
    s = mm.stats(p.db)["box-a"]
    assert s["handoffs"] == 2 and s["failures"] == 2 and not s["local"], s
    p.db.add_task("next soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    Daemon(p.base).check_resource_trouble(every_s=0)
    (row,) = p.db.q("SELECT text FROM events WHERE kind='resource_trouble'")
    assert "2 hand-offs failed or blocked" in row["text"] and "local faults" not in row["text"], row["text"]


def test_ssh_to_the_resource_counts_its_failures_and_only_role_or_provenance_refusals_are_local(env, monkeypatch):
    """A run that ran a command on the resource over plain ssh, scp or rsync used it (the Bash hook records
    the destination in onhost), so its failure counts against the resource even beside a refusal. Only
    worker-role, provenance and project local guard refusals are guard faults: a full-suite, no-device or
    user-site refusal alone tags nothing local."""
    p = make(env)
    from ttp import hook, localfault, machines as mm, onhost
    from ttp.hook import REFUSALS_FILE
    assert hook.ssh_targets("cd x && timeout 9 ssh -o BatchMode=yes -p 22 me@box-a 'make' | tail") == ["box-a"]
    assert hook.ssh_targets("scp f u@box-b:/tmp/ && rsync -az ./ box-c:d/") == ["box-b", "box-c"]
    assert hook.ssh_targets("echo ssh box-a; ls host:dir") == []
    mm.add("box-a", tags="cpu")
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    run_dir = p.base / "lf-ssh"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    hook.post_tool_use({"tool_name": "Bash", "tool_input": {"command": "ssh box-a 'pytest -x'"}})
    assert localfault.used(run_dir) == {"box-a"}
    assert json.loads((run_dir / onhost.FILE).read_text())["box-a"]["kind"] == "ssh"
    full = (REFUSALS_FILE, json.dumps({"ts": 1, "kind": "full_suite", "command": "pytest"}) + "\n")
    prov = (REFUSALS_FILE, json.dumps({"ts": 1, "kind": "provenance", "command": "edit"}) + "\n")
    # ssh to the resource and a full-suite refusal: a plain failure, counted against box-a.
    assert _lf_handoff(p, tid, marker=full, used=["box-a"])["data"] is None
    # Only a full-suite, no-device or user-site refusal, the resource unused: no local fault either.
    for k in ("full_suite", "no_device", "user_site"):
        (run_dir / REFUSALS_FILE).write_text(json.dumps({"kind": k}) + "\n")
        assert localfault.kinds(run_dir) == [], k
    # A provenance refusal on a run that ssh'd to the resource: tagged, but the resource was used.
    assert json.loads(_lf_handoff(p, tid, marker=prov, used=["box-a"])["data"]) == \
        {"local_fault": ["guard"], "used": ["box-a"]}
    # A provenance refusal only, the resource never used: counted apart.
    assert json.loads(_lf_handoff(p, tid, "blocked", marker=prov)["data"]) == {"local_fault": ["guard"], "used": []}
    s = mm.stats(p.db)["box-a"]
    assert s["failures"] == 2 and s["local"] == 1, s
