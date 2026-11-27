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


def test_outside_writes_is_high_on_a_home_entry_write_or_a_credential_store_not_on_a_read_or_run(tmp_path, scratch_home):
    """Sourcing a startup file, running a ~/.local program or writing a cache raises no high event; a write to a
    startup file or ~/Library/LaunchAgents does, as does any call naming a credential store."""
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()

    def high(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return sidefx.record(rd, 7, str(wt))["high"]

    assert high("source ~/.bashrc", "~/.local/bin/python3 -V", "echo x > ~/.cache/pip/x") is False
    for cmd in ("echo x >> ~/.bashrc", "echo x > ~/Library/LaunchAgents/x.plist", "cat ~/.netrc",
                "cp ~/Library/Keychains/login.keychain-db /tmp/k"):
        assert high(cmd) is True, cmd


def test_outside_writes_is_not_high_on_a_listing_of_a_credential_folder_only_on_reading_a_file_in_it(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    for folder, name in ((".claude", ".credentials.json"), (".ssh", "id_ed25519")):
        (scratch_home / folder).mkdir(exist_ok=True)
        (scratch_home / folder / name).write_text("")

    def high(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return sidefx.record(rd, 7, str(wt))["high"]

    assert high("ls ~/.claude/", "stat ~/.ssh", "ls -la ~/.ssh ~/.claude") is False
    for cmd in ("cat ~/.claude/.credentials.json", "grep -r x ~/.ssh", "cp -r ~/.ssh /tmp/k", "ls ~/.ssh > ~/.ssh/list",
                "stat ~/.ssh/id_ed25519"):
        assert high(cmd) is True, cmd
    assert high("ls ~/.s" + "sh", "grep -r x ~/.s" + "sh") is True   # a listing first keeps the read's alert


def test_inline_code_writing_only_its_own_files_leaves_other_paths_in_its_text_mentioned(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    loc = "~/.lo" + "cal/bin"

    def found(*cmds):
        (rd / "output.jsonl").write_text(json.dumps(_claude_calls(*(("Bash", {"command": c}) for c in cmds))[0]) + "\n")
        return {f["path"]: f["class"] for f in sidefx.scan(rd, str(wt), str(scratch_home))}

    local = str(scratch_home / ".lo") + "cal/bin"
    edit = f"s = open('{wt}/x.py').read().replace('a', '{loc}')\nopen('{wt}/x.py', 'w').write(s)\n"
    assert found(f"python3 - <<'EOF'\n{edit}EOF\n", f'python3 -c "open(\'out.txt\', \'w\').write(\'{loc}\')"') \
        == {local: "mentioned"}
    for code in (f"open('{loc}/t', 'w').write('x')", f"p = '{loc}/t'; open(p, 'w')",   # its write goes elsewhere
                 f"import os; open('a', 'w'); os.replace('a', '{loc}/t')", f"open('../t', 'w'); s = '{loc}'",
                 f"import os as o; open('a', 'w'); o.replace('a', '{loc}/t')",
                 f"from pathlib import Path; p = Path('a'); p.write_text('x'); p.replace('{loc}/t')",
                 f"import sys; open(sys.argv[1], 'w').write('{loc}')"):   # a target the prover cannot place
        assert "write" in found(f'python3 -c "{code}"').values(), code


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
                 "bash -lc 'rg /_launch src > hits.txt'", "perl -ne 'print if /^\\d+/' f > o.txt",
                 "awk 'NR>=2 && /\\<later\\>/ {print NR\": \"$0}' f | head", "awk '{print $0 \" > \" NR}' f") == set()
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
    # any hidden home entry, ~/bin and a credential store are sensitive; visible home paths and the harness's own folder are not
    assert all(sidefx.sensitive(f"{h}/{x}", h) for x in (".bash_aliases", ".config/autostart/x.desktop", "bin/git",
                                                         "Library/LaunchAgents/x.plist", "library/LaunchAgents/x",
                                                         "Library/Keychains/login.keychain-db"))
    assert not any(sidefx.sensitive(f"{h}/{x}", h) for x in ("notes.txt", "work/.hidden", ".tt-project/x", "binx",
                                                             "Library/Caches/x", "Library/LaunchAgentsX"))
    # real writes still count: an awk program's own output file, a grep's redirect, a quoted target
    assert found("awk '/x/ {print > \"/tmp/a\"}' f", "grep -n '/^def' f.py > /tmp/defs",
                 "sed -i 's/a/b/' \"$HOME/.config/c\"", "awk '/a>b/ {print >> \"/tmp/m\"}' f") \
        == {"/tmp/a", "/tmp/defs", f"{h}/.config/c", "/tmp/m"}
    writes = lambda c: {p for p, (cls, _) in sidefx.classify_paths(c, h, str(wt), str(rd)).items() if cls == "write"}
    assert not writes("awk 'NR>=2 && /\\<later\\>/ {print NR\": \"$0}' f | head") and not writes("awk '{print $0 \" > \" NR}' f")
    assert writes("awk '/a>b/ {print > \"/etc/m\"}' f") == {"/etc/m"}
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


def test_outside_writes_names_each_paths_class_and_its_reason(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd, o = tmp_path / "wt", tmp_path / "run", tmp_path / "elsewhere"
    wt.mkdir(), rd.mkdir()

    def classes(*cmds):
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
            *[("Bash", {"command": c}) for c in cmds])))
        return {f["path"]: (f["class"], f["why"]) for f in sidefx.scan(rd, str(wt))}

    got = classes(f"echo x > {o}/a", f"cat {o}/b | tee {o}/c", f"curl -fsSL -o {o}/d https://example.com/x",
                  f"cp -t {o}/e {o}/f", f"bash -lc 'rm -f {o}/g'", f"{o}/h.sh && touch {o}/i",
                  f"cd {o} && printf y >> rel.txt", f"sed -i s/a/b/ {o}/j", f"dd if={o}/k of={o}/l")
    assert got[f"{o}/a"] == ("write", "redirect target") and got[f"{o}/c"] == ("write", "tee target"), got
    assert got[f"{o}/b"] == ("read", "cat argument") and got[f"{o}/d"] == ("write", "curl -o"), got
    assert got[f"{o}/e"] == ("write", "cp -t directory") and got[f"{o}/f"] == ("read", "cp source"), got
    assert got[f"{o}/g"] == ("write", "rm target (inner command of bash -lc)"), got
    assert got[f"{o}/h.sh"] == ("executed", "run as a command") and got[f"{o}/i"] == ("write", "touch target"), got
    assert got[f"{o}/rel.txt"] == ("write", "redirect target") and got[f"{o}/j"] == ("write", "sed -i target"), got
    assert got[f"{o}/k"] == ("read", "dd if=") and got[f"{o}/l"] == ("write", "dd of="), got
    assert classes(f"python3 -c \"open('{o}/m', 'w').write('z')\" > /dev/null")[f"{o}/m"][0] == "write"


def test_outside_writes_reads_an_empty_argument_as_a_word_and_splits_each_command(tmp_path, scratch_home):
    """An empty argument ('') is a word, not a command break: a write after it is still seen. commands()
    gives each simple command's words, quoted text as `_`, a `bash -c` script's own commands in its place
    and no redirect targets, so a tool's name inside a commit message is no run of it."""
    from ttp import sidefx
    rd = tmp_path / "run"
    rd.mkdir()
    conf = str(scratch_home / ("." + "ssh") / "example-hop.conf")
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
        ("Bash", {"command": f"sed -i '' s/a/b/ {conf}"}))))
    got = {f["path"]: (f["class"], f["why"]) for f in sidefx.scan(rd, str(tmp_path / "wt"))}
    assert got[conf] == ("write", "sed -i target"), got
    assert sidefx.commands("git commit -qm 'fix: gh auth login > x' && git credential approve") == [
        "git commit -qm _", "git credential approve"]
    assert sidefx.commands("cd d; bash -lc 'echo hi > o | tee p' 2>/dev/null") == ["cd d", "echo hi", "tee p"]
    assert sidefx.commands("printf '' | gh auth login --with-token") == ["printf ''", "gh auth login --with-token"]
    assert sidefx.commands("echo 'unclosed") == ["echo 'unclosed"]

def test_outside_writes_keeps_each_calls_status_and_only_a_guards_own_words_refuse_it(tmp_path, scratch_home):
    from ttp import sidefx
    wt, rd, o = tmp_path / "wt", tmp_path / "run", tmp_path / "elsewhere"
    wt.mkdir(), rd.mkdir()
    cmds = [f"touch {o}/ok", f"git commit -m x && touch {o}/husky", f"rm {o}/denied", f"rm {o}/hooked",
            f"rm {o}/hanging"]
    lines = _claude_calls(*[("Bash", {"command": c}) for c in cmds]) + [{"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": "", "is_error": False},
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": "Exit code 1\nhusky - pre-commit hook exited with error code 1 (blocked by hook)"},
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
         "content": [{"type": "text", "text": "PreToolUse:Bash hook error: [guard]: refused"}]},
        {"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "Error: something"}]}}]
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (rd / "refusals.jsonl").write_text(json.dumps({"kind": "credential", "command": cmds[3]}) + "\n")
    got = {f["path"].rsplit("/", 1)[1]: f for f in sidefx.scan(rd, str(wt))}
    assert (got["ok"]["status"], got["ok"]["exit"]) == ("completed", 0), got
    assert (got["husky"]["status"], got["husky"]["exit"]) == ("completed", 1), got   # its output is no guard
    assert got["denied"]["status"] == "refused" and "PreToolUse:Bash hook error" in got["denied"]["guard"], got
    assert got["hooked"]["status"] == "refused" and "credential" in got["hooked"]["guard"], got
    assert got["hanging"]["status"] == "started", got
    text = sidefx._names(list(got.values()))
    assert "completed, exit 1" in text and "started, never completed" in text and "stopped by" in text
    (rd / "refusals.jsonl").write_text("")   # Codex: completed with its code, or declined
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
        {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": f"rm {o}/cx"}},
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": f"rm {o}/cx",
                                            "exit_code": 2}},
        {"type": "item.completed", "item": {"id": "2", "type": "command_execution", "command": f"rm {o}/cd",
                                            "status": "declined"}}]))
    got = {f["path"].rsplit("/", 1)[1]: f for f in sidefx.scan(rd, str(wt))}
    assert (got["cx"]["status"], got["cx"]["exit"], got["cd"]["status"]) == ("completed", 2, "refused"), got
    assert len(sidefx.items(rd)) == 2   # a started and completed item is one call


def test_outside_writes_stays_high_for_a_credential_path_whatever_its_class(env, tmp_path, scratch_home):
    from ttp.daemon import Daemon
    p = make(env)
    wt = tmp_path / "wt"
    wt.mkdir()
    key = f"{scratch_home}/{'.s' + 'sh'}/id_ed25519"
    _sidefx_run(Daemon(p.base), _claude_calls(("Bash", {"command": f"python3 fix.py {key}"})), wt)
    ev = _outside_events(p)
    assert len(ev) == 1 and ev[0]["severity"] == "high", [dict(e) for e in ev]
    assert json.loads(ev[0]["data"])["classes"] == {key: "mentioned only"}
    assert "mentioned only: argument to python3" in ev[0]["text"], ev[0]["text"]


def test_outside_writes_record_names_the_scan_that_saw_a_write_no_guard_refused(tmp_path, scratch_home):
    """A tee into a scratch folder runs (no hook blocks writes outside protected paths): its record names
    the run-end scan as the guard that saw it, not an empty guard; a refused call keeps its guard's words."""
    from ttp import sidefx
    wt, rd, o = tmp_path / "wt", tmp_path / "run", tmp_path / "scratch"
    wt.mkdir(), rd.mkdir()
    lines = _claude_calls(("Bash", {"command": f"echo hi | tee {o}/q.txt"}), ("Bash", {"command": f"rm {o}/r"})) + [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "hi", "is_error": False},
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": [{"type": "text", "text": "PreToolUse:Bash hook error: [guard]: refused"}]}]}}]
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    got = {f["path"].rsplit("/", 1)[1]: f for f in sidefx.scan(rd, str(wt))}
    assert got["q.txt"]["status"] == "completed" and got["q.txt"]["guard"] == sidefx.SEEN_BY, got
    assert got["r"]["status"] == "refused" and "PreToolUse:Bash hook error" in got["r"]["guard"], got
    assert "stopped by" not in sidefx._names([got["q.txt"]])


def test_a_no_side_effects_claim_is_checked_against_the_runs_own_effects_on_other_projects(env, tmp_path, scratch_home):
    """cell handoff_side_effect_claim: a hand-off claiming no side effects outside its folder, from a run that
    stopped another project's unit and ran a git checkout in another project's harness, is marked false in its
    result.json and raised high; the effect on another project is raised even without a claim. Another
    project's registry row removed or daemon stopped since the spawn is found from the spawn snapshot."""
    import os
    from ttp import claimcheck, project, sidefx
    from ttp.daemon import Daemon
    p = make(env)
    peer = tmp_path / "peer"
    (peer / "tt-project" / "state").mkdir(parents=True)
    (peer / "tt-project" / "state" / "daemon.pid").write_text(str(os.getpid()))
    project.register("peer", {"host": "testhost", "dir": str(peer)})
    other = tmp_path / "other"
    (other / "tt-project").mkdir(parents=True)
    project.register("other", {"host": "testhost", "dir": str(other)})
    d = Daemon(p.base)
    wt = tmp_path / "wt"
    wt.mkdir()
    calls = _claude_calls(("Bash", {"command": "systemctl --user stop tt-project-peer.service"}),
                          ("Bash", {"command": f"git -C {peer}/tt-project/harness checkout -- project.json"}),
                          ("Bash", {"command": "systemctl --user restart tt-project-demo"}))   # its own: no effect
    claim = {"status": "done", "summary": "Fixed it. No side effects outside the worktree."}
    rid, run_dir, _ = _sidefx_run(d, calls, wt, result=claim, rc=0)
    res = json.loads((run_dir / "result.json").read_text())
    assert any("tt-project-peer" in x and "git checkout" in x for x in res[claimcheck.FALSE_KEY]), res
    assert "demo" not in json.dumps(res[claimcheck.FALSE_KEY])
    assert claimcheck.handoff_mark(res).startswith(" (false, by the run's own records:")
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (claimcheck.KIND,))
    assert len(ev) == 1 and ev[0]["severity"] == "high" and "tt-project-peer" in ev[0]["text"]
    # no claim: the effect on another project is still raised, and the hand-off is not marked
    rid, run_dir, _ = _sidefx_run(d, calls, wt, result={"status": "done", "summary": "Fixed it."}, rc=0)
    assert claimcheck.FALSE_KEY not in json.loads((run_dir / "result.json").read_text())
    ev = p.db.q("SELECT * FROM events WHERE kind='peer_effects'")
    assert len(ev) == 1 and "tt-project-peer" in ev[0]["text"] and ev[0]["severity"] == "high"
    # a true claim stays unmarked
    rid, run_dir, _ = _sidefx_run(d, _claude_calls(("Bash", {"command": f"ls {peer}"})), wt, result=claim, rc=0)
    assert claimcheck.FALSE_KEY not in json.loads((run_dir / "result.json").read_text())
    # from the spawn snapshot: a peer's daemon stopped and another's registry row removed
    snap = tmp_path / "snap"
    snap.mkdir()
    sidefx.snapshot_peers(snap, p.base)
    (peer / "tt-project" / "state" / "daemon.pid").write_text("4194999")
    project.unregister("other")
    (snap / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
        ("Bash", {"command": f"kill {os.getpid()}"}), ("Bash", {"command": "ttp remove other"}))))
    got = sidefx.peer_effects(snap, p.base, "demo")
    assert "project peer's daemon was stopped" in got and "project other's registry row was removed" in got


def test_outside_writes_reads_a_heredoc_body_as_data_and_the_claim_check_names_no_effect(env, tmp_path, scratch_home):
    """A heredoc's body is the text it feeds a command, not commands or paths written: appending test code that
    names other projects' units and /proc paths writes only the heredoc's own target, and stops nothing."""
    from ttp import sidefx
    p = make(env)
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    body = ("def test_x():\n    run('systemctl --user stop tt-project-peer.service')\n"
            "    os.system(\"systemctl --user restart tt-project-demo.service\")\n"
            "    cwd = os.readlink(f'/proc/{pid}/cwd') or '/nonexistent'\n"
            "    open(f'/proc/{pid}/cmdline').read()  # it's a read\n"
            f"    Path('{tmp_path}/other/tt-project/projectremove_cases.py').write_text('x')\n")
    cmd = f"cd {wt} && cat >> tests/x_cases.py <<'EOF'\n{body}EOF\necho done"
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(("Bash", {"command": cmd}))))
    assert sidefx.scan(rd, str(wt)) == []
    assert sidefx.classify_paths(cmd, str(scratch_home), str(wt), str(rd))[f"{wt}/tests/x_cases.py"][0] == "write"
    assert sidefx.claim_check(rd, {"summary": "No side effects outside the worktree."}, str(wt), p.base, "demo") \
        == ([], [])
    # a body fed to a shell or an interpreter runs: its writes and unit stops are found
    sh = f"bash <<'EOF'\nsystemctl --user stop tt-project-peer.service\nrm -f {tmp_path}/o/f\nEOF"
    py = f"python3 - <<EOF\nopen('{tmp_path}/o/g', 'w').write('z')\nEOF"
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
        ("Bash", {"command": sh}), ("Bash", {"command": py}))))
    assert {f["path"]: f["class"] for f in sidefx.scan(rd, str(wt))} == {f"{tmp_path}/o/f": "write",
                                                                        f"{tmp_path}/o/g": "write"}
    assert any("tt-project-peer" in x for x in sidefx.peer_effects(rd, p.base, "demo"))


def test_a_quoted_heredoc_operator_hides_no_real_command_after_it(env, tmp_path, scratch_home):
    """`<<X` inside quotes, an escape or a comment opens no heredoc: the writes and unit stops after it are found."""
    from ttp import sidefx
    p = make(env)
    wt, rd, o = tmp_path / "wt", tmp_path / "run", tmp_path / "o"
    wt.mkdir(), rd.mkdir()
    shapes = [f'echo "a <<X"\nrm -f {o}/f\nsystemctl --user stop tt-project-peer.service\nX',
              f"echo 'a <<X'\nrm -f {o}/f\nsystemctl --user stop tt-project-peer.service\nX",
              f"echo a \\<<X\nrm -f {o}/f\nsystemctl --user stop tt-project-peer.service\nX",
              f"echo ok # <<X\nrm -f {o}/f\nsystemctl --user stop tt-project-peer.service\nX"]
    for cmd in shapes:
        assert sidefx.classify_paths(cmd, str(scratch_home), str(wt), str(rd)).get(f"{o}/f", ("",))[0] == "write", cmd
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(("Bash", {"command": cmd}))))
        assert [f["path"] for f in sidefx.scan(rd, str(wt))] == [f"{o}/f"], cmd
        assert any("tt-project-peer" in x for x in sidefx.peer_effects(rd, p.base, "demo")), cmd
    # an unquoted heredoc after a line with a quoted one still hides its body
    cmd = f'echo "a <<X" && cat >> {wt}/t <<EOF\nrm -f {o}/g\nEOF'
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(("Bash", {"command": cmd}))))
    assert sidefx.scan(rd, str(wt)) == []


def test_outside_writes_names_no_read_only_command_path_or_argument_as_written(tmp_path, scratch_home):
    """Read-only commands, process substitution, output thrown into /dev/null, a PATH-style assignment and a
    path inside a quoted argument are no writes; real redirects, tee, cp, rm and `bash -c` writes still are."""
    from ttp import sidefx
    wt, rd, o = tmp_path / "wt", tmp_path / "run", tmp_path / "elsewhere"
    wt.mkdir(), rd.mkdir()

    def writes(*cmds):
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
            *[("Bash", {"command": c}) for c in cmds])))
        return {f["path"] for f in sidefx.scan(rd, str(wt)) if f["class"] == "write"}

    for c in (f"wc -l {o}/a/locks.py", f"diff {o}/a/locks.py {o}/b/locks.py && echo IDENTICAL",
              f"P={o}/a.py; diff <(grep -n def $P) <(grep -n def {o}/b.py)", f"sed -n 1,20p {o}/x.py",
              f"grep -n foo {o}/dir 2>/dev/null", f"ls {o}/state 2>/dev/null", f"ls {o}/state &>/dev/null",
              f"cat {o}/state >/dev/null", f"export PATH={o}/bin:$PATH && cp a.txt b.txt",
              f"PATH={o}/bin:$PATH make -C . > build.log", f"python3 gen.py --note \"see {o}/t29/file\" > out.txt",
              f"cat {o}/notes.md > notes.md 2>/dev/null"):
        assert writes(c) == set(), c
    assert writes(f"echo x > {o}/f1", f"echo y | tee {o}/f2", f"cp a.txt {o}/", f"rm {o}/f3",
                  f"bash -c 'echo z > {o}/f4'", f"git clone https://example.com/r {o}/clone",
                  f"f={o}/f5; echo x > $f") \
        == {f"{o}/f1", f"{o}/f2", str(o), f"{o}/f3", f"{o}/f4", f"{o}/clone", f"{o}/f5"}


def test_peer_effects_lets_a_run_remove_the_scratch_project_its_own_task_made(env, tmp_path, monkeypatch):
    """A scratch project under the account's scratch folder that this run's task made (`ttp new` records made_by)
    may be removed again without a peer-effects alert; one another task made, or one outside the scratch folder,
    still raises it."""
    from ttp import project, sidefx
    p = make(env)
    scratch = project.HOME_DIR / "scratch"
    rows = {"mine": (scratch / "mine", {"project": "demo", "task": 57}),
            "theirs": (scratch / "theirs", {"project": "demo", "task": 58}),
            "outside": (tmp_path / "outside", {"project": "demo", "task": 57})}
    for name, (d, by) in rows.items():
        (d / "tt-project" / "state").mkdir(parents=True)
        project.register(name, {"host": "testhost", "dir": str(d), "made_by": by})
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "run.json").write_text(json.dumps({"env": {"TTP_TASK": "57"}}))
    sidefx.snapshot_peers(rd, p.base)
    for name in rows:
        project.unregister(name)
    (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
        *[("Bash", {"command": f"ttp remove {n} --delete-files"}) for n in rows])))
    got = sidefx.peer_effects(rd, p.base, "demo")
    assert "project theirs's registry row was removed" in got and "project outside's registry row was removed" in got
    assert not any("mine" in x for x in got), got


def test_ttp_new_in_a_run_records_which_task_made_the_project(env, monkeypatch):
    from ttp import cli
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", "57")
    assert cli._made_by() == {"made_by": {"project": "demo", "task": 57}}
    monkeypatch.delenv("TTP_TASK")
    assert cli._made_by() == {}


def test_a_peer_change_another_run_made_in_the_same_window_is_never_this_runs_effect(env, tmp_path):
    """cells: peer_concurrent_attribution. A scratch project another run made and removed while this run ran, and a
    daemon someone else stopped, are no effect of this run; its own `ttp remove`, kill of the daemon's pid or write
    of the registry is."""
    import os
    from ttp import project, sidefx
    p = make(env)
    for n in ("scratchx", "peer"):
        (tmp_path / n / "tt-project" / "state").mkdir(parents=True)
        project.register(n, {"host": "testhost", "dir": str(tmp_path / n),
                             "made_by": {"project": "demo", "task": 913}})
    (tmp_path / "peer" / "tt-project" / "state" / "daemon.pid").write_text(str(os.getpid()))
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "run.json").write_text(json.dumps({"env": {"TTP_TASK": "796"}}))
    sidefx.snapshot_peers(rd, p.base)
    project.unregister("scratchx")   # the other run's own `ttp remove scratchx --delete-files`
    (tmp_path / "peer" / "tt-project" / "state" / "daemon.pid").write_text("4194999")

    def effects(*cmds, tool="Bash"):
        (rd / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _claude_calls(
            *[(tool, {"command": c} if tool == "Bash" else {"file_path": c, "content": "{}"}) for c in cmds])))
        return sidefx.peer_effects(rd, p.base, "demo")
    assert effects("ttp list", "git status", f"cat {project.registry_path()}", "grep -rn scratchx notes.md",
                   f"ls {tmp_path}/scratchx") == []
    assert effects("ttp remove scratchx --delete-files") == ["project scratchx's registry row was removed"]
    assert effects(f"kill {os.getpid()}") == ["project peer's daemon was stopped"]
    assert effects("ttp peer stop") == ["project peer's daemon was stopped"]
    assert len(effects(str(project.registry_path()), tool="Write")) == 2
    assert effects(f"rm -rf {tmp_path}/scratchx") == ["project scratchx's registry row was removed"]


def test_sed_in_place_counts_only_its_file_operands_as_targets_never_its_script(tmp_path):
    """A sed script such as `s/_fmt(/_rate_str(/g` was read as the write target /_rate_str; -e, -f, -E, -i.bak,
    other s delimiters and `--` are parsed, a `w file` command still counts, and a relative target after a cd too."""
    from ttp import sidefx
    wt = str(tmp_path / "wt")

    def writes(cmd):   # the writes outside the worktree
        return {k: v[1] for k, v in sidefx.classify_paths(cmd, "/home/u", wt, "").items()
                if v[0] == "write" and not k.startswith(wt + "/")}
    assert writes("sed -i 's/def _fmt(v) -> str:/def _rate_str(v) -> str:/; s/_fmt(/_rate_str(/g' src/m.py") == {}
    assert writes("sed -i -e 's|a|/b|' -e 's,/c,d,' src/m.py /tmp/out") == {"/tmp/out": "sed -i target"}
    assert writes("sed -Ei.bak 's/x/\\/y/' /srv/a") == {"/srv/a": "sed -i target"}
    assert writes("sed --in-place --expression=s/a/b/ -- /srv/b") == {"/srv/b": "sed -i target"}
    assert writes("sed -i '/stage-task.sh/d' f.py") == {}
    assert writes("sed -i 's/a/b/w /etc/x' f.py") == {"/etc/x": "sed -i script with a w command"}
    assert writes("sed -i 's|a|b|w /etc/x' f.py") == {"/etc/x": "sed -i script with a w command"}   # any s delimiter
    assert writes("sed -i 's|a w|b|' f.py") == {}   # a w inside the pattern is no command
    assert writes("sed -i '24s/0:41/0:81/' t.sh && sed -i '35s/.*/  echo C8 wrote its 3 rounds/' t.sh") == {}
    assert writes("sed -i '1,5s/x/y/w /etc/o' f.py") == {"/etc/o": "sed -i script with a w command"}
    assert writes(f"cd /srv && sed -i '/warn/d; 2,/wait/!d' {wt}/f.py") == {}   # an address regex is no w command
    assert writes("sed -i '/x/w /etc/a' f.py") == {"/etc/a": "sed -i script with a w command"}
    assert writes("sed -i '$w /etc/last' f.py; sed -i 'y/abc/wxy/' f.py") == {"/etc/last": "sed -i script with a w command"}
    assert sidefx.classify_paths("sed -i -f /tmp/s.sed f.py", "/home/u", wt, "")["/tmp/s.sed"][0] == "executed"
    assert writes("cd /srv/o && sed -i s/a/b/ rel.txt") == {"/srv/o/rel.txt": "sed -i target"}
    assert writes("sed 's/a/b/' /srv/c > /srv/d") == {"/srv/d": "redirect target"}   # no -i: reads /srv/c
