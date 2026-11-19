# Rule checks built from a project's own settings (ruleguard.py, through hook.py's PreToolUse and the
# GLM shell) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Every test runs under a
# scratch HOME, XDG_CONFIG_HOME and GH_CONFIG_DIR, with fake squeue, sinfo and ssh first on PATH.
from __future__ import annotations


RG_FAKE_SSH = '#!/bin/bash\necho "$@" >> "$FAKE_SLURM/ssh.log"\neval "${@: -1}"\n'
RG_FAKE_SQUEUE = '#!/bin/sh\ncase "$*" in *" -j "*) cat "$FAKE_SLURM/job" ;; *) cat "$FAKE_SLURM/nodes" ;; esac\n'
RG_FAKE_SINFO = '#!/bin/sh\n[ -e "$FAKE_SLURM/broken" ] && exit 1\ncat "$FAKE_SLURM/sinfo"\n'


@pytest.fixture
def rule_home(tmp_path, monkeypatch):
    """A scratch home, a run dir (the hook counts fires there), fake Slurm tools and ssh on PATH, and
    the project settings the hook reads (`cfg`, empty until a test fills it)."""
    import pwd
    from types import SimpleNamespace
    from ttp import aitag, project
    home, work, run, fake, acct = (tmp_path / n for n in ("h", "w", "run", "fake", "acct"))
    for d in (home / ".config" / "gh", work, run, fake / "bin", acct):
        d.mkdir(parents=True)
    for name, text in (("ssh", RG_FAKE_SSH), ("squeue", RG_FAKE_SQUEUE), ("sinfo", RG_FAKE_SINFO)):
        (fake / "bin" / name).write_text(text)
        (fake / "bin" / name).chmod(0o755)
    for name in ("nodes", "job", "sinfo"):
        (fake / name).write_text("")
    for var, val in (("HOME", home), ("XDG_CONFIG_HOME", home / ".config"), ("GH_CONFIG_DIR", home / ".config" / "gh"),
                     ("TTP_RUN_DIR", run), ("FAKE_SLURM", fake), ("PATH", f"{fake / 'bin'}:{os.environ['PATH']}")):
        monkeypatch.setenv(var, str(val))
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    monkeypatch.setattr(project, "HOME_DIR", acct)
    cfg: dict = {}
    monkeypatch.setattr(aitag, "project_config", lambda: cfg)
    return SimpleNamespace(home=home, work=work, run=run, fake=fake, cfg=cfg, me=pwd.getpwuid(os.getuid()).pw_name)


def _rg_deny(cmd, cwd, tool="Bash", tool_input=None):
    """The hook's refusal text for one tool call, or None when it lets the call run."""
    from ttp import hook
    out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": tool_input or {"command": cmd}, "cwd": str(cwd)})
    spec = (out or {}).get("hookSpecificOutput") or {}
    return spec.get("permissionDecisionReason", "").removeprefix("tt-project: ") if spec.get("permissionDecision") == "deny" else None


def _rg_fired(run):
    from ttp import ruleguard
    return [r["rule"] for r in ruleguard.fires(run)]


def test_protected_paths_refuses_writes_under_a_served_tree_here_and_over_ssh(rule_home):
    rule_home.cfg["guards"] = {"protected_paths": ["/srv/site", "~/served/*"]}
    for tool, inp in (("Write", {"file_path": "/srv/site/index.html", "content": "x"}),
                      ("Bash", {"command": "echo x > /srv/site/a"}),
                      ("Bash", {"command": "rm -rf ~/served/docs/old"}),
                      ("Bash", {"command": "ssh box 'cp a /srv/site/b'"})):
        why = _rg_deny("", rule_home.work, tool, inp)
        assert why and why.startswith("protected_paths (guards.protected_paths)"), (tool, inp, why)
    assert _rg_deny("echo x > /srv/other/a", rule_home.work) is None
    assert _rg_deny("cat /srv/site/index.html", rule_home.work) is None
    assert _rg_fired(rule_home.run) == ["protected_paths"] * 4


def test_slurm_nodes_refuses_srun_onto_a_node_another_user_holds(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "nodes").write_text(f"someone-else 4411\n{rule_home.me} 4412\n")
    (rule_home.fake / "sinfo").write_text("node7 allocated\n")
    why = _rg_deny("srun -w node7 -N1 hostname", rule_home.work)
    assert why and why.startswith("slurm_nodes (guards.slurm)") and "4411" in why and "4412" not in why
    # The same call through ssh to a login host asks the queue there
    why = _rg_deny("ssh login 'sbatch --nodelist=node7 job.sh'", rule_home.work)
    assert why and "4411" in why
    assert "login" in (rule_home.fake / "ssh.log").read_text()
    assert _rg_fired(rule_home.run) == ["slurm_nodes", "slurm_nodes"]


def test_slurm_nodes_refuses_a_drained_node_and_another_users_job(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "sinfo").write_text("node8 drained\n")
    assert "node8" in _rg_deny("salloc -w node8", rule_home.work)
    (rule_home.fake / "job").write_text("someone-else 5150\n")
    assert "5150" in _rg_deny("srun --jobid 5150 nvidia-smi", rule_home.work)


def test_slurm_nodes_lets_free_nodes_and_own_jobs_pass(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "nodes").write_text(f"{rule_home.me} 4412\n")
    (rule_home.fake / "sinfo").write_text("node7 mixed\nnode9 idle\n")
    (rule_home.fake / "job").write_text(f"{rule_home.me} 4412\n")
    assert _rg_deny("srun -w node7,node9 hostname", rule_home.work) is None
    assert _rg_deny("srun --jobid=4412 hostname", rule_home.work) is None
    assert _rg_deny("sbatch job.sh", rule_home.work) is None   # the scheduler picks: nothing to ask


def test_slurm_nodes_refuses_when_the_queue_cannot_be_read(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "broken").write_text("")
    why = _rg_deny("srun -w node7 hostname", rule_home.work)
    assert why and "could not tell who holds node7" in why


def test_slurm_nodes_stays_off_until_the_project_turns_it_on(rule_home):
    (rule_home.fake / "nodes").write_text("someone-else 4411\n")
    assert _rg_deny("srun -w node7 hostname", rule_home.work) is None
    assert not (rule_home.fake / "ssh.log").exists()


def test_landing_push_refuses_a_raw_push_to_the_push_branch_and_channel_tags(rule_home):
    rule_home.cfg["delivery"] = {"push_branch": "origin/team/landing", "fast_forward_also": ["team/stable"],
                                 "channel_tags": ["team/release"]}
    for cmd in ("git push origin HEAD:team/landing", "git push -f origin +work:refs/heads/team/stable",
                "git -C repo push origin team/release", "git push --tags origin", "git push --all origin"):
        why = _rg_deny(cmd, rule_home.work)
        assert why and why.startswith("landing_push (delivery.push_branch") and "ttp push" in why, cmd
    assert _rg_deny("git push origin HEAD:team/my-work", rule_home.work) is None
    # No refspec: the current branch decides
    repo = rule_home.work / "repo"
    subprocess.run(["git", "init", "-q", "-b", "team/landing", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "i"], check=True)
    assert _rg_deny("git push origin", repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "team/my-work"], check=True)
    assert _rg_deny("git push origin", repo) is None


def test_landing_push_ships_empty_and_lets_every_push_pass(rule_home):
    assert _rg_deny("git push origin HEAD:main", rule_home.work) is None


def test_user_undo_refuses_replaying_the_undo_of_a_user_ordered_change(rule_home, monkeypatch):
    from ttp import machine_ledger
    entries = [{"id": 7, "user_msg": 42, "undo": "sudo systemctl enable --now box-recovery.timer"},
               {"id": 8, "undo": "sudo rm /etc/box/drop-in.conf"}]
    monkeypatch.setattr(machine_ledger, "changes", lambda open_only=True: entries)
    why = _rg_deny("ssh box 'sudo systemctl  enable --now \"box-recovery.timer\"'", rule_home.work)
    assert why and why.startswith("user_undo") and "7" in why
    assert _rg_deny("ssh box sudo rm /etc/box/drop-in.conf", rule_home.work) is None   # not user-ordered
    assert _rg_fired(rule_home.run) == ["user_undo"]


def test_guards_off_turns_one_rule_off_and_only_the_user_may_set_it(rule_home, monkeypatch):
    from ttp import coordinator, glmenv, machine_ledger
    monkeypatch.setattr(machine_ledger, "changes", lambda open_only=True: [
        {"id": 7, "user_msg": 42, "undo": "systemctl enable box-recovery.timer"}])
    rule_home.cfg.update({"delivery": {"push_branch": "team/landing"},
                          "guards": {"protected_paths": ["/srv/site"], "off": ["landing_push", "protected_paths"]}})
    assert _rg_deny("git push origin HEAD:team/landing", rule_home.work) is None
    assert _rg_deny("echo x > /srv/site/a", rule_home.work) is None
    assert _rg_deny("systemctl enable box-recovery.timer", rule_home.work)   # still on
    assert glmenv.refusal("systemctl enable box-recovery.timer", str(rule_home.work))   # the GLM shell asks it too
    assert coordinator.NEEDS_USER.get("guards.off")


def test_ttp_guards_lists_each_rule_and_ttp_stats_counts_fires_per_rule_and_model(env, capsys):
    from ttp import cli, ruleguard
    p = make(env)
    p.set_config("guards.slurm", True)
    capsys.readouterr()
    cli.main(["guards", "demo"])
    out = capsys.readouterr().out
    assert [ln.split(":")[0] for ln in out.splitlines()] == list(ruleguard.RULES)
    assert "guards.slurm: on" in out
    now = time.time()
    for model in ("glm-5.3", "claude-opus-5-5"):
        rid = p.db.x("INSERT INTO runs(role,provider,model,started,status) VALUES('worker','x',?,?,'ok')", (model, now))
        (p.runs / str(rid)).mkdir(parents=True, exist_ok=True)
        (p.runs / str(rid) / ruleguard.FIRES).write_text(json.dumps({"ts": now, "rule": "landing_push", "why": "w"}) + "\n")
    assert ruleguard.counts(p, now - 60, now + 60)["landing_push"] == {"glm-5.3": 1, "claude-opus-5-5": 1}
    cli.main(["stats", "demo", "--guards", "--json"])
    assert json.loads(capsys.readouterr().out)["landing_push"]["glm-5.3"] == 1
