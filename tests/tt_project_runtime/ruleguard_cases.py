# Rule checks built from a project's own settings (ruleguard.py, through hook.py's PreToolUse and the
# GLM shell) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Every test runs under a
# scratch HOME, XDG_CONFIG_HOME and GH_CONFIG_DIR, with fake squeue, sinfo and ssh first on PATH.
from __future__ import annotations


RG_FAKE_SSH = '#!/bin/bash\necho "$@" >> "$FAKE_SLURM/ssh.log"\neval "${@: -1}"\n'
RG_FAKE_SQUEUE = ('#!/bin/sh\ncase "$*" in *" -j "*)\n'
                  '  [ -e "$FAKE_SLURM/gone" ] && { echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1; }\n'
                  '  [ -e "$FAKE_SLURM/jobs-broken" ] && { echo "slurm_load_jobs error: Unable to contact slurm controller" >&2; exit 1; }\n'
                  '  cat "$FAKE_SLURM/job" ;; *) cat "$FAKE_SLURM/nodes" ;; esac\n')
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


def test_slurm_nodes_lets_a_queue_only_sbatch_name_busy_nodes_only_when_the_project_says_so(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "nodes").write_text("someone-else 4411\n")
    (rule_home.fake / "sinfo").write_text("node8 drained\n")
    assert "4411" in _rg_deny("sbatch -w node8 job.sh", rule_home.work)
    rule_home.cfg["guards"]["slurm_queue_pinned"] = True
    assert _rg_deny("sbatch -w node8 job.sh", rule_home.work) is None
    assert _rg_deny("ssh login 'sbatch --nodelist=node8 -t 30 job.sh'", rule_home.work) is None
    # Calls that block for the node, or join or follow another user's job, are still refused
    for cmd in ("sbatch -I -w node8 job.sh", "sbatch --immediate -w node8 job.sh", "srun -w node8 hostname",
                "salloc -w node8"):
        assert "4411" in _rg_deny(cmd, rule_home.work), cmd
    (rule_home.fake / "job").write_text("someone-else 5150\n")
    for cmd in ("sbatch -w node8 --dependency=afterok:5150 job.sh", "sbatch -d afterany:5150+5 job.sh",
                "srun --jobid=5150 hostname"):
        assert "5150" in _rg_deny(cmd, rule_home.work), cmd
    (rule_home.fake / "job").write_text(f"{rule_home.me} 4412\n")
    assert _rg_deny("sbatch -w node8 --dependency=afterany:4412 job.sh", rule_home.work) is None


def test_slurm_nodes_refuses_scancel_of_another_users_job_and_node_state_updates(rule_home):
    rule_home.cfg["guards"] = {"slurm": True}
    (rule_home.fake / "job").write_text("someone-else 5150\n")
    for cmd in ("scancel 5150", "ssh login 'scancel -s 9 5150'", "scancel --signal=TERM 5150_2"):
        assert "5150" in _rg_deny(cmd, rule_home.work), cmd
    assert "someone-else" in _rg_deny("scancel -u someone-else", rule_home.work)
    (rule_home.fake / "job").write_text(f"{rule_home.me} 4412\n")
    assert _rg_deny("scancel 4412", rule_home.work) is None
    assert _rg_deny(f"scancel -u {rule_home.me} -s 9", rule_home.work) is None
    for cmd in ("scontrol update NodeName=node8 State=DRAIN Reason=x", "ssh login 'scontrol update nodename=node8 state=resume'"):
        assert "node's state" in _rg_deny(cmd, rule_home.work), cmd
    assert _rg_deny("scontrol show node node8", rule_home.work) is None
    assert _rg_deny("scontrol update JobId=4412 TimeLimit=30", rule_home.work) is None


def test_slurm_nodes_reads_user_variables_as_the_caller_and_an_ended_job_as_held_by_nobody(rule_home):
    """`scancel -u $USER` (also over ssh, also ${USER}, $LOGNAME, $(whoami)) names the caller; scancel or a
    --dependency of a job squeue no longer lists ('Invalid job id') has no live holder and runs. Another
    user's live job, another user by name and a queue that cannot be read are still refused."""
    rule_home.cfg["guards"] = {"slurm": True}
    for cmd in ("scancel -u $USER --name x", "ssh login 'scancel -u $USER --name x'", 'scancel -u "${USER}" -s 9',
                "scancel --user=$LOGNAME", "scancel -u $(whoami)", 'scancel -u "$(id -un)"', "scancel -u `whoami`"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd
    assert "someone-else" in _rg_deny("scancel -u someone-else --name x", rule_home.work)
    # the caller's own substitution must sit in the scancel's own text, not in another command's
    for cmd in ("scancel -u $(cat f); squeue -u $(whoami)", "squeue -u $(whoami) && scancel -u $(cat f)",
                "ssh login 'scancel -u $(cat f)'; echo $(whoami) | squeue -u $(id -un)",
                "scancel -u $(whoami) -u $(cat f)", "scancel --user=$(cat f) -u $(whoami)",
                "scancel --name $(echo x) -u $(cat f)", "echo scancel -u $(whoami); scancel -u $(cat f)"):
        assert _rg_deny(cmd, rule_home.work), cmd
    assert _rg_deny("squeue -u $(whoami); scancel -u $(whoami) --name x", rule_home.work) is None
    (rule_home.fake / "gone").write_text("")
    for cmd in ("scancel 7001", "ssh login 'scancel 7001'", "sbatch --dependency=afterany:7001 job.sh"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd
    (rule_home.fake / "gone").unlink()
    (rule_home.fake / "job").write_text("someone-else 5150\n")
    assert "5150" in _rg_deny("scancel 5150", rule_home.work)
    (rule_home.fake / "jobs-broken").write_text("")
    assert "could not tell who holds 7001" in _rg_deny("scancel 7001", rule_home.work)


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


def _rg_power_shims(rule_home):
    """reboot, shutdown, systemctl, ipmitool and kexec shims first on PATH that only log: no test reaches a
    real power call."""
    for name in ("reboot", "shutdown", "systemctl", "ipmitool", "kexec"):
        (rule_home.fake / "bin" / name).write_text(f'#!/bin/sh\necho {name} "$@" >> "$FAKE_SLURM/power.log"\n')
        (rule_home.fake / "bin" / name).chmod(0o755)
    return rule_home.fake / "power.log"


REBOOTS = ("sudo reboot", "shutdown -r now", "systemctl reboot", "sudo systemctl --no-pager poweroff",
           "systemctl start reboot.target", "kexec -e", "echo b | sudo tee /proc/sysrq-trigger",
           "echo b > /proc/sysrq-trigger", "sh -c 'sync; reboot'", "init 6", "sudo -n reboot", "sudo -u root -n reboot",
           "echo b | sudo dd of=/proc/sysrq-trigger", "python3 -c \"open('/proc/sysrq-trigger','w').write('b')\"",
           "busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager Reboot b true")


def test_host_reboot_refuses_a_reboot_with_no_power_cycle_intent_on_the_ledger(rule_home, monkeypatch):
    from ttp import machine_ledger
    log = _rg_power_shims(rule_home)
    monkeypatch.setenv("TTP_HOST", "testhost")
    monkeypatch.setattr(machine_ledger, "changes", lambda open_only=True: [])
    for cmd in REBOOTS + ("ssh box sudo reboot", "ssh -p 22 me@box 'echo b > /proc/sysrq-trigger'",
                          "ipmitool -I lanplus -H box-bmc -U admin chassis power cycle", "ipmitool -H box-bmc power off",
                          "curl -X POST http://box:8080/power_cycle", "ipmitool -H box-bmc raw 0x00 0x02 0x02"):
        why = _rg_deny(cmd, rule_home.work)
        assert why and why.startswith("host_reboot") and "--power-cycle-at" in why, cmd
    for cmd in ("systemctl status reboot.target", "shutdown -c", "kexec -l /boot/vmlinuz", "echo reboot later",
                "ipmitool -H box-bmc chassis power status", "git log --grep reboot", "ipmitool raw 0x06 0x01",
                "ttp machines change add box 'kernel update' --power-cycle-at 10m"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd
    assert not log.exists(), "the hook only decides: nothing ran"
    assert set(_rg_fired(rule_home.run)) == {"host_reboot"}


def test_host_reboot_refuses_inside_the_lead_time_and_passes_an_announced_intent_for_that_host(rule_home, monkeypatch):
    from ttp import glmenv, machine_ledger, ruleguard
    log = _rg_power_shims(rule_home)
    monkeypatch.setenv("TTP_HOST", "testhost")
    now = time.time()
    entries = [{"id": 5, "alias": "testhost", "kind": machine_ledger.POWER_CYCLE, "at": now + 60, "added": now - 30,
                "announced": now - 20},
               {"id": 6, "alias": "box", "kind": machine_ledger.POWER_CYCLE, "at": now + 60, "added": now - 600,
                "announced": now - 590},
               {"id": 7, "alias": "far", "kind": machine_ledger.POWER_CYCLE, "at": now + 7200, "added": now - 600,
                "announced": now - 590},
               {"id": 8, "alias": "mute", "kind": machine_ledger.POWER_CYCLE, "at": now + 60, "added": now - 600}]
    monkeypatch.setattr(machine_ledger, "changes", lambda open_only=True: entries)
    why = _rg_deny("sudo reboot", rule_home.work)
    assert why and why.startswith("host_reboot") and f"{ruleguard.REBOOT_LEAD_S}s" in why, "recorded 30s ago: too soon"
    assert glmenv.refusal("sudo systemctl reboot", str(rule_home.work)), "the GLM shell asks it too"
    for cmd in ("ssh box sudo reboot", "ipmitool -H box-bmc chassis power cycle", "ssh me@box 'echo b > /proc/sysrq-trigger'"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd   # box's intent was recorded 10 minutes ago
    for cmd in ("ssh box sudo tee /proc/sysrq-trigger",
                "ssh box busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager Reboot b false"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd   # unquoted, so it runs on box
    assert _rg_deny("ssh box cat > /proc/sysrq-trigger", rule_home.work), "a local redirect is this host's"
    for cmd in ("ssh box sudo bash <<EOF\necho b > /proc/sysrq-trigger\nEOF", "ssh box sudo bash <<EOF\nreboot\nEOF",
                "cat <<EOF | ssh box bash\nreboot\nEOF"):
        assert _rg_deny(cmd, rule_home.work) is None, cmd   # a heredoc fed to ssh runs on box
    for cmd in ("ssh other <<EOF\necho b > /proc/sysrq-trigger\nEOF", "ssh other sudo bash <<EOF\nreboot\nEOF",
                "bash <<EOF\necho b > /proc/sysrq-trigger\nEOF", "cat <<EOF | bash\necho b > /proc/sysrq-trigger\nEOF",
                "cat <<EOF | ssh other bash\nreboot\nEOF", "ssh box sudo ssh other <<EOF\nreboot\nEOF"):
        assert _rg_deny(cmd, rule_home.work), cmd
    assert _rg_deny("ssh other sudo reboot", rule_home.work), "another host's intent does not cover this one"
    assert _rg_deny("ssh far sudo reboot", rule_home.work), "outside the intent's window"
    assert _rg_deny("ssh mute sudo reboot", rule_home.work), "an intent never announced in its chat"
    assert _rg_deny("ssh box 'echo b > /proc/sysrq-trigger'; echo b > /proc/sysrq-trigger", rule_home.work), \
        "box's intent does not cover the local write beside it"
    entries[0]["added"] = now - 600
    assert _rg_deny("sudo reboot", rule_home.work) is None
    rule_home.cfg["guards"] = {"reboot_lead_s": 900}
    assert _rg_deny("sudo reboot", rule_home.work), "the project's lead time"
    rule_home.cfg["guards"] = {"reboot_lead_s": 0}
    assert ruleguard.reboot_lead_s(rule_home.cfg) == ruleguard.REBOOT_LEAD_MIN_S, "never under a minute"
    monkeypatch.setattr(machine_ledger, "changes", lambda open_only=True: 1 / 0)
    assert "could not run" in _rg_deny("sudo reboot", rule_home.work), "an unreadable ledger refuses"
    assert _rg_deny("ls", rule_home.work) is None, "only reboot commands read it"
    assert not log.exists()


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
