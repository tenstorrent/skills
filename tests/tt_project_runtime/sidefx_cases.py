# Outside-writes tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of what a failed run did outside its folders here (sidefx.py).
from __future__ import annotations


@pytest.fixture
def scratch_home(tmp_path, monkeypatch):
    """A fresh HOME: nothing here reads or writes the real one."""
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / ".config" / "gh"))
    return home


def _sidefx_run(d, lines, cwd, *, result=None, rc=1, provider="claude", tid=None):
    """A worker run of a new task (or task `tid`) in `cwd` whose transcript is `lines`, finished by the
    daemon; with `result`, it wrote that hand-off first."""
    db = d.p.db
    tid = tid or db.add_task("premise check", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    db.update_task(tid, status="running")
    rid = db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
               "VALUES(?,'worker',?,'opus',?,'running',?,?)", (tid, provider, time.time() - 60, d.boot, "{}"))
    run_dir = d.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(cwd), "argv": []}))
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (run_dir / "stderr.log").write_text("")
    if result:
        (run_dir / "result.json").write_text(json.dumps(result))
    d.finish_run(db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": rc, "started": time.time() - 60, "ended": time.time(), "stopped": None, "slept_s": 0})
    return rid, run_dir, db.task(tid)


def _claude_calls(*calls):
    """A Claude Code stream-json transcript of these (tool, input) calls."""
    return [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(calls)]}}]


def _outside_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='outside_writes'")


def test_a_failed_run_that_wrote_home_config_and_tmp_is_named_in_the_retry_and_raised(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    from ttp import prompts
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    scratch = tmp_path / "scratch-out"
    rid, run_dir, task = _sidefx_run(Daemon(p.base), _claude_calls(
        ("Bash", {"command": "mkdir -p ~/.config && printf 'x' > ~/.config/x"}),
        ("Write", {"file_path": str(scratch / "out.txt"), "content": "y"}),
        ("Bash", {"command": "ls /etc && cat README.md"})), wt)
    assert not (scratch_home / ".config" / "x").exists()   # only the transcript says so: nothing ran
    ev = _outside_events(p)
    assert len(ev) == 1 and ev[0]["severity"] == "high" and ev[0]["status"] == "queued", [dict(e) for e in ev]
    assert ev[0]["task"] == task["id"] and f"run {rid}" in ev[0]["text"]
    for want in (f"{scratch_home}/.config/x", f"{scratch}/out.txt"):
        assert want in ev[0]["text"] and want in json.loads(ev[0]["data"])["paths"], ev[0]["text"]
    assert "/etc" not in json.loads(ev[0]["data"])["paths"]   # a read-only command is no write
    text = prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)
    assert f"Run {rid}, the earlier run of this task, ended without a hand-off and did these outside your folders" in text
    assert f"{scratch_home}/.config/x" in text and f"{scratch}/out.txt" in text and "do not repeat them" in text
    assert "printf 'x'" not in text   # paths only, never a command's text


def test_a_failed_run_that_wrote_only_inside_its_folders_gets_no_block_and_no_event(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    from ttp import prompts
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    rid, run_dir, task = _sidefx_run(Daemon(p.base), _claude_calls(
        ("Bash", {"command": f"cd {wt} && echo a > notes.txt 2>/dev/null && rm -f build/x"}),
        ("Bash", {"command": "echo '{}' > $TTP_RUN_DIR/scratch.json"}),
        ("Edit", {"file_path": str(wt / "src" / "a.py"), "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "rel/b.txt", "content": "c"})), wt)
    assert not _outside_events(p) and not (run_dir / "outside.json").exists()
    assert "the earlier run of this task" not in prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)


def test_a_run_with_a_valid_hand_off_gets_no_block(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    from ttp import prompts
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    rid, run_dir, task = _sidefx_run(Daemon(p.base), _claude_calls(
        ("Bash", {"command": "printf 'x' > ~/.config/x"})), wt,
        result={"status": "failed", "summary": "tried and failed"}, rc=0)
    assert not _outside_events(p) and not (run_dir / "outside.json").exists()
    assert "the earlier run of this task" not in prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)


def test_outside_writes_event_is_normal_off_the_home_config_and_raised_once(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    d = Daemon(p.base)
    rid, run_dir, task = _sidefx_run(d, _claude_calls(("Bash", {"command": f"cp a.txt {tmp_path}/elsewhere/a.txt"})), wt)
    ev = _outside_events(p)
    assert len(ev) == 1 and ev[0]["severity"] == "normal", [dict(e) for e in ev]
    d._outside_writes(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), run_dir)   # the same run again
    assert len(_outside_events(p)) == 1


def test_outside_writes_parser_reads_every_shipped_provider_format(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    h = str(scratch_home)

    def found(lines, refusals=()):
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        (rd / "refusals.jsonl").write_text("".join(json.dumps(x) + "\n" for x in refusals))
        return {f["path"] for f in sidefx.scan(rd, str(wt))}

    # Claude Code and the chat bridge (the same stream-json): MCP file tools and apply_patch too
    assert found(_claude_calls(
        ("mcp__fs__write_file", {"path": "/srv/data/a"}),
        ("mcp__fs__read_file", {"path": "/srv/data/b"}),
        ("apply_patch", {"input": f"*** Begin Patch\n*** Update File: {h}/.ssh/config\n*** Add File: in/tree.py\n"
                                  "*** End Patch"}),
        ("NotebookEdit", {"notebook_path": "/opt/nb.ipynb"}))) == {"/srv/data/a", f"{h}/.ssh/config", "/opt/nb.ipynb"}
    # Codex `exec --json`: commands, file changes and MCP calls
    assert found([
        {"type": "item.started", "item": {"id": "1", "type": "command_execution",
                                          "command": "bash -lc 'echo k >> $HOME/.netrc'"}},
        {"type": "item.completed", "item": {"id": "2", "type": "file_change",
                                            "changes": [{"path": "/var/tmp/x.py", "kind": "add"},
                                                        {"path": str(wt / "ok.py"), "kind": "update"}]}},
        {"type": "item.completed", "item": {"id": "3", "type": "mcp_tool_call", "server": "fs", "tool": "edit_file",
                                            "arguments": json.dumps({"path": "/srv/e"})}}]) \
        == {f"{h}/.netrc", "/var/tmp/x.py", "/srv/e"}
    # Cursor stream-json tool_call events
    assert found([
        {"type": "tool_call", "subtype": "started", "tool_call": {"shellToolCall": {"args": {"command": "touch /tmp/c1"}}}},
        {"type": "tool_call", "subtype": "started", "tool_call": {"writeToolCall": {"args": {"path": "/tmp/c2"}}}},
        {"type": "tool_call", "subtype": "started", "tool_call": {"readToolCall": {"args": {"path": "/tmp/c3"}}}}]) \
        == {"/tmp/c1", "/tmp/c2"}
    # the hook's log: a rewritten command ran, a refused one did not
    assert found([], [{"kind": "rewrite", "command": "pytest", "rewritten": "pytest > /tmp/hooklog.txt"},
                      {"kind": "no_device", "command": "rm -rf /tmp/refused"}]) == {"/tmp/hooklog.txt"}


def test_outside_writes_parser_shell_edges(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    h = str(scratch_home)

    def found(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return {f["path"] for f in sidefx.scan(rd, str(wt))}

    assert found("sed -i s/a/b/ f.txt", "curl https://example.com/x | head", "echo hi >/dev/null 2>&1") == set()
    assert found("cat ~/.ssh/known_hosts") == {f"{h}/.ssh/known_hosts"}   # a credential folder counts unwritten
    assert found(f"cd {h} && printf y > .config/gh/hosts.yml") == {f"{h}/.config/gh/hosts.yml"}   # relative after cd
    assert found("python3 -c \"open('/tmp/p.txt','w').write('z')\"") == {"/tmp/p.txt"}
    assert found("curl -s https://example.com/f -o /tmp/dl") == {"/tmp/dl"}
    assert found(f"echo z | tee -a {wt}/../sibling/log") == {str(tmp_path / "sibling" / "log")}   # .. leaves the tree
    assert found(f"rm -rf {wt}2/x") == {f"{wt}2/x"}   # a prefix of the worktree's name is not inside it
    rec = sidefx.record(rd, 7, str(wt))
    assert rec["high"] is False and json.loads((rd / sidefx.FILE).read_text())["run"] == 7


def test_outside_writes_skips_regex_text_ssh_remote_commands_and_saved_tool_output(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    h = str(scratch_home)

    def found(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return {f["path"] for f in sidefx.scan(rd, str(wt))}

    # regex and awk text is no path, in each tool and in a `bash -lc` script (Codex's form)
    assert found("grep -n '/^def' f.py; rm -f x", "awk '/_launch/ {print $1}' f > out.txt",
                 "sed -n '/^def/,/^$/p' f.py | head; touch y", "grep -rn -e /_launch -e '/x$' . > hits.txt",
                 "bash -lc 'rg /_launch src > hits.txt'", "perl -ne 'print if /^\\d+/' f > o.txt") == set()
    # a comparison is no redirect: `>=` after a cd elsewhere named '<dir>/=NNN:' before
    assert found(f"cd {tmp_path}/state && python3 -c 'print(1 if 5>=3 else 0)' && grep -c 'run>=123:' log") == set()
    # an ssh remote command's paths are the remote host's; its local redirect and key are local
    assert found("ssh box 'rm -rf /tmp/r && echo x > ~/.config/r'", "ssh -o BatchMode=yes box \"cat /srv/a > /srv/b\"",
                 "ssh box rm /tmp/r2") == set()
    assert found(f"ssh -i ~/.ssh/k box 'cat /var/log/x' > /tmp/local.log\nrm /tmp/after") \
        == {f"{h}/.ssh/k", "/tmp/local.log", "/tmp/after"}
    # Claude Code's saved large tool output is read, not a credential folder
    saved = f"{h}/.claude/projects/-wt/tool-results/b1.txt"
    assert found(f"sed -n 1,40p {saved}", f"grep -n x {saved} | head") == set()
    assert not sidefx.sensitive(saved, h) and sidefx.sensitive(f"{h}/.claude/settings.json", h)
    assert sidefx.sensitive(f"{h}/.claude/projects/-wt/memory.md", h)
    # real writes still count: an awk program's own output file, a grep's redirect, a quoted target
    assert found("awk '/x/ {print > \"/tmp/a\"}' f", "grep -n '/^def' f.py > /tmp/defs",
                 "sed -i 's/a/b/' \"$HOME/.config/c\"") == {"/tmp/a", "/tmp/defs", f"{h}/.config/c"}
    assert found("rm /tmp/my\\ dir/f") == {"/tmp/my\\"}   # an escaped space is no regex escape
    assert found("echo hi 2>&1 > /tmp/s") == {"/tmp/s"} and found("echo 'unclosed > /tmp/u") == {"/tmp/u"}


def test_outside_writes_reads_a_tool_name_only_in_command_position(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    h = str(scratch_home)

    def found(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return {f["path"] for f in sidefx.scan(rd, str(wt))}

    # an argument ending in /rg, /sed, /ssh or /sh is no tool: the write targets after it stay named
    assert f"{h}/.ssh/authorized_keys" in found("cp ./tools/rg ~/.ssh/authorized_keys")
    assert f"{h}/.ssh/a" in found("rm -rf ~/x/sed ~/.ssh/a")
    assert f"{h}/.ssh/b" in found("cp /opt/bin/ssh ~/.ssh/b")
    assert f"{h}/.config/c" in found("mv /usr/bin/sh $HOME/.config/c")
    assert found("echo x > /tmp/rg /tmp/o") >= {"/tmp/o"}   # a redirect target named rg is no grep
    # a tool in command position, behind sudo, VAR=val, xargs, time or a separator, still has its text blanked
    lt = sidefx._local_text
    assert lt("sudo rg /etc/x ~/.ssh") == "sudo rg  ~/.ssh"
    assert lt("sudo -u root rg /etc/x f") == "sudo -u root rg  f"
    assert lt("FOO=1 sed -n '/^a/p' f") == "FOO=1 sed -n 'p' f"
    assert lt("find . | xargs grep /x") == "find . | xargs grep "
    assert lt("a; ssh host rm /tmp/r") == "a; ssh host  "
    assert lt("time awk '/x/ {print}' f") == "time awk ' {print}' f"
    assert lt("if grep -q /x f; then rm y; fi") == "if grep -q  f; then rm y; fi"
    assert found("sudo rg x ~/.ssh", "FOO=1 sed -n p f", "find . | xargs grep /x",
                 "a; ssh host rm /tmp/r", "time awk '/x/ {print}' f > out.txt") == {f"{h}/.ssh"}


def test_outside_writes_finds_the_tool_past_timeout_git_process_substitution_and_long_options(tmp_path,
                                                                                               scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    h = str(scratch_home)

    def found(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return {f["path"] for f in sidefx.scan(rd, str(wt))}

    lt = sidefx._local_text
    # timeout, its options and its duration come before the command
    assert lt("timeout 30 grep pat ~/x") == "timeout 30 grep  ~/x"
    assert lt("timeout 5 rg /x f") == "timeout 5 rg  f"
    assert lt("timeout -s KILL -k 3 --foreground --preserve-status -v 9 grep /x f") \
        == "timeout -s KILL -k 3 --foreground --preserve-status -v 9 grep  f"
    # git grep is a grep, past git's own options
    assert lt("git grep -n foo -- path") == "git grep -n  -- path"
    assert lt("git -C dir grep /x f") == "git -C dir grep  f"
    assert lt("git --no-pager grep /x f") == "git --no-pager grep  f"
    assert lt("git -c core.pager=cat grep /x") == "git -c core.pager=cat grep "
    # process substitution starts a command; it is no redirect
    assert lt("diff <(grep /x f) y") == "diff <(grep  f) y"
    assert lt("tee >(rg /x) < in") == "tee >(rg ) < in"
    # sudo, env and xargs long options that take a value
    assert lt("sudo --user root rg /x f") == "sudo --user root rg  f"
    assert lt("env --unset X grep /x f") == "env --unset X grep  f"
    assert found("timeout 5 rg /x f", "git grep 'rm /tmp/x'", "diff <(grep /x f) y", "sudo --user root rg /x f",
                 "git -C . grep -n /etc/y") == set()
    # a real target stays named, and git's other commands keep their text
    assert found("timeout 5 cp a ~/.ssh/authorized_keys") == {f"{h}/.ssh/authorized_keys"}
    assert f"{h}/.ssh/authorized_keys" in found("cp ./tools/rg ~/.ssh/authorized_keys")
    assert found("timeout 5 grep x f > /tmp/hits") == {"/tmp/hits"}
    assert lt("git commit -m /x") == "git commit -m /x" and lt("git -C d rg /x") == "git -C d rg /x"


def test_a_later_failed_run_that_touched_nothing_keeps_the_earlier_runs_writes_named(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    from ttp import prompts
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    d = Daemon(p.base)
    rid1, _, task = _sidefx_run(d, _claude_calls(("Bash", {"command": f"rm -rf {tmp_path}/shared/cache"})), wt)
    rid2, run_dir2, _ = _sidefx_run(d, _claude_calls(("Bash", {"command": "ls"})), wt, tid=task["id"])
    assert not (run_dir2 / "outside.json").exists()
    text = prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)
    assert f"Run {rid1}, an earlier run of this task, ended without a hand-off" in text and f"{tmp_path}/shared/cache" in text
    _sidefx_run(d, _claude_calls(("Bash", {"command": "ls"})), wt, tid=task["id"],
                result={"status": "failed", "summary": "tried"}, rc=0)   # a hand-off reported it
    assert f"Run {rid1}" not in prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)
    rid4, _, _ = _sidefx_run(d, _claude_calls(("Bash", {"command": "ls"})), wt, tid=task["id"])
    assert "earlier run of this task" not in prompts.worker_task(p, p.db.task(task["id"]), str(wt), None)
