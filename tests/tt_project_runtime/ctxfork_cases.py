# Coordinator-context tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of ctxfork
# (a worker or reviewer run starting from a fork of the coordinator's session, else its context on disk) here.
from __future__ import annotations

import uuid


def _fork_line(kind, sid, content, **extra):
    return json.dumps({"type": kind, "sessionId": sid, "uuid": str(uuid.uuid4()),
                       "message": {"role": kind, "content": content}, **extra})


def _fork_meta(kind, sid):
    return json.dumps({"type": kind, "sessionId": sid, "uuid": str(uuid.uuid4())})


def _fork_transcript(cfg_dir, cwd, sid):
    """A coordinator transcript as Claude Code writes it under --json-schema: each wake's answer is a
    StructuredOutput call with no tool result, followed by bookkeeping rows; its last wake is still
    running (a tool call open and a partial line)."""
    folder = cfg_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join([
        _fork_line("user", sid, "digest: the user ordered full context"),
        _fork_meta("attachment", sid),
        _fork_line("assistant", sid, [{"type": "thinking", "thinking": "plan"}]),
        _fork_line("assistant", sid, [{"type": "tool_use", "id": "s1", "name": "StructuredOutput",
                                       "input": {"actions": []}}]),
        _fork_meta("last-prompt", sid), _fork_meta("atis-latch", sid), _fork_meta("cost-state", sid),
        _fork_line("user", sid, "next wake"),
        _fork_line("assistant", sid, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
    ]) + "\n" + '{"type": "user", "sessionId": "' + sid)
    return path


def _fork_daemon(env, monkeypatch, tmp_path, provider="claude"):
    from ttp import daemon as dmod

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    from ttp.providers import claude
    monkeypatch.setattr(claude, "_FLAGS", {"--resume": True, "--session-id": True})
    cfg_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("TTP_TEST_WORKER_CONTEXT")
    p = make(env)
    d = dmod.Daemon(p.base)

    def run(role="worker", resume=None):
        tid = p.db.add_task(f"t {uuid.uuid4().hex[:6]}", "do the one thing", kind="code", origin="user")
        rid = d.start_run(role, "TASK PROMPT", provider, "standard", str(p.root), task=p.db.task(tid),
                          resume=resume)
        spec = json.loads((p.runs / str(rid) / "run.json").read_text())
        row = p.db.one("SELECT note, session_id FROM runs WHERE id=?", (rid,))
        return (spec["argv"], (p.runs / str(rid) / "prompt.md").read_text(),
                json.loads(row["note"] or "{}").get("coord_context"), row["session_id"])
    return p, d, cfg_dir, run


def test_a_worker_run_forks_the_coordinator_session_and_never_writes_into_it(env, monkeypatch, tmp_path):
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    src = _fork_transcript(cfg_dir, p.base, sid)
    before = src.read_bytes()
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 50000})
    for role in ("worker", "reviewer"):
        argv, prompt, got, session = run(role)
        assert got["how"] == "fork", got
        assert got["tokens"] == 50000 and got["session"] == session != sid
        assert argv[argv.index("--resume") + 1] == session and "--session-id" not in argv
        assert prompt.startswith(ctxfork.FORK_HEAD) and prompt.rstrip().endswith("TASK PROMPT")
        copy = cfg_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(p.root)) / f"{session}.jsonl"
        rows = [json.loads(x) for x in copy.read_text().splitlines()]
        # It ends at the last whole answer, the StructuredOutput call: the bookkeeping rows after it, the
        # open tool call, the wake's prompt and the partial line are left out; a thinking-only line is
        # no answer. Every line carries the run's own id.
        assert [r["type"] for r in rows] == ["user", "attachment", "assistant", "assistant"]
        assert rows[-1]["message"]["content"][0]["name"] == "StructuredOutput"
        assert {r["sessionId"] for r in rows} == {session}
        assert oct(copy.stat().st_mode & 0o777) == "0o600"
    assert src.read_bytes() == before   # the coordinator's own transcript is untouched
    # A lost run continuing its own session gets neither a fork nor the disk block.
    argv, prompt, got, session = run(resume=str(uuid.uuid4()))
    assert got is None and not prompt.startswith(ctxfork.FORK_HEAD) and ctxfork.DISK_HEAD not in prompt


def test_the_shipped_default_forks_a_coordinator_session_of_any_size(env, monkeypatch, tmp_path):
    """No size cap by default (owner's order, user msgs #2116, #2040): a session far past the old 120k cap
    still forks for workers and reviews; the run compacts in place if it must. A cap of 0 or below is none."""
    from ttp import coordsession
    p, d, cfg_dir, run = _fork_daemon(env, monkeypatch, tmp_path)
    assert coordsession.settings(p.config())["worker_fork_max_tokens"] == 0
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    for tokens in (143209, 900000):
        p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": tokens})
        for role in ("worker", "reviewer"):
            argv, prompt, got, session = run(role)
            assert got["how"] == "fork" and got["tokens"] == tokens, got
            assert argv[argv.index("--resume") + 1] == session != sid
    for cap in (-1, 0):
        p.set_config("coordinator.session.worker_fork_max_tokens", cap)
        d.cfg = p.config()
        assert run()[2]["how"] == "fork", cap
    # A cap the user sets: at the cap forks, above it takes the disk block.
    p.set_config("coordinator.session.worker_fork_max_tokens", 900000)
    d.cfg = p.config()
    assert run()[2]["how"] == "fork"
    p.set_config("coordinator.session.worker_fork_max_tokens", 899999)
    d.cfg = p.config()
    got = run()[2]
    assert got["how"] == "disk" and "over worker_fork_max_tokens 899999" in got["why"], got


def test_a_worker_run_falls_back_to_the_coordinator_context_on_disk(env, monkeypatch, tmp_path):
    from ttp import coordsession, ctxfork, obligations
    p, d, cfg_dir, run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    src = _fork_transcript(cfg_dir, p.base, sid)
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,1)",
           (time.time(), "in", "web", "user", "use threads for independent parts"))
    obligations.add(p, "split independent parts into threads")
    coordsession.save_note(p, sid, "decided: fork workers from the session")

    def disk(why):
        argv, prompt, got, session = run()
        assert got["how"] == "disk" and why in got["why"], got
        assert "--resume" not in argv and prompt.startswith(ctxfork.DISK_HEAD)
        assert "split independent parts into threads" in prompt   # the order ledger
        assert "use threads for independent parts" in prompt       # the chat, verbatim
        return prompt

    disk("no coordinator session saved")
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "codex", "context_tokens": 50000})
    assert "decided: fork workers from the session" in disk("runs on codex")
    # A cap the user sets still holds (the shipped default has none).
    p.set_config("coordinator.session.worker_fork_max_tokens", 120000)
    d.cfg = p.config()
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 500000})
    disk("over worker_fork_max_tokens 120000")
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 5000})
    old = time.time() - 30 * 3600
    os.utime(src, (old, old))
    disk("last written 30 h ago")
    src.unlink()
    disk("not on disk")
    # A transcript with no finished answer cannot be forked either.
    _fork_transcript(cfg_dir, p.base, sid).write_text(_fork_line("user", sid, "only a prompt") + "\n")
    disk("no finished answer")
    # Off: neither the fork nor the disk block.
    p.set_config("coordinator.session.worker_context", "off")
    d.cfg = p.config()
    argv, prompt, got, session = run()
    assert got["how"] == "none" and prompt.startswith("TASK PROMPT")
    # "disk" skips the fork even when it could run.
    _fork_transcript(cfg_dir, p.base, sid)
    p.set_config("coordinator.session.worker_context", "disk")
    d.cfg = p.config()
    disk("worker_context disk")


def test_fork_session_per_provider(env, monkeypatch, tmp_path):
    """Claude Code forks by a transcript copy; Codex, Cursor and the chat provider's own coordinator keep
    no forkable session (the disk block stands in); the fake copies its saved session."""
    from ttp.providers import get_provider as get
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    sid = str(uuid.uuid4())
    _fork_transcript(tmp_path / "cfg", "/somewhere/else", sid)
    claude = get("claude")
    assert claude.fork_session(sid, "/w" + "x" * 300, None, str(uuid.uuid4())).startswith("the run's working")
    assert claude.fork_session(sid, "/w", None, "../escape") == "bad session id"
    assert claude.fork_session(str(uuid.uuid4()), "/w", None, str(uuid.uuid4())) == \
        "the session's transcript is not on disk"
    own = tmp_path / "other-account"   # the run's own config directory gets the copy
    new = str(uuid.uuid4())
    assert claude.fork_session(sid, "/w", {"CLAUDE_CONFIG_DIR": str(own)}, new) == ""
    assert (own / "projects" / "-w" / f"{new}.jsonl").is_file()
    for name in ("codex", "cursor"):
        assert "cannot fork" in get(name).fork_session(sid, "/w", None, str(uuid.uuid4()))
    # The copy starts at the session's last compaction: the agent resumes from there anyway.
    src = next((tmp_path / "cfg" / "projects").glob(f"*/{sid}.jsonl"))
    src.write_text(_fork_line("user", sid, "before compaction") + "\n"
                   + json.dumps({"type": "system", "subtype": "compact_boundary", "sessionId": sid}) + "\n"
                   + src.read_text())
    new = str(uuid.uuid4())
    assert claude.fork_session(sid, "/w", None, new) == ""
    rows = [json.loads(x) for x in (tmp_path / "cfg" / "projects" / "-w" / f"{new}.jsonl").read_text().splitlines()]
    assert rows[0]["subtype"] == "compact_boundary" and "before compaction" not in json.dumps(rows)
    monkeypatch.setenv("TTP_FAKE_SESSIONS", str(tmp_path / "fake"))
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake" / f"{sid}.jsonl").write_text("{}\n")
    assert get("fake").fork_session(sid, "/w", None, new) == "" and (tmp_path / "fake" / f"{new}.jsonl").is_file()


def test_fork_session_ends_at_a_text_or_structured_answer_never_at_another_open_call(monkeypatch, tmp_path):
    """A --json-schema answer (StructuredOutput) and a plain text answer both end a copy; a line that
    also calls another tool, or calls only another tool, does not."""
    from ttp.providers import get_provider as get
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    claude, sid = get("claude"), str(uuid.uuid4())
    folder = tmp_path / "cfg" / "projects" / "-src"
    folder.mkdir(parents=True)
    so = {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {"actions": []}}
    bash = {"type": "tool_use", "id": "b1", "name": "Bash", "input": {}}

    def last(*tail):
        (folder / f"{sid}.jsonl").write_text("\n".join([_fork_line("user", sid, "wake"), *tail]) + "\n")
        new = str(uuid.uuid4())
        why = claude.fork_session(sid, "/w", None, new)
        if why:
            return why
        rows = (tmp_path / "cfg" / "projects" / "-w" / f"{new}.jsonl").read_text().splitlines()
        return json.loads(rows[-1])["message"]["content"]

    assert last(_fork_line("assistant", sid, [so]), _fork_meta("cost-state", sid)) == [so]
    assert last(_fork_line("assistant", sid, [{"type": "text", "text": "ok"}])) == [{"type": "text", "text": "ok"}]
    assert last(_fork_line("assistant", sid, [so]), _fork_line("assistant", sid, [so, bash])) == [so]
    assert last(_fork_line("assistant", sid, [{"type": "text", "text": "x"}, bash])) == \
        "the session has no finished answer to start from"
    assert last(_fork_line("assistant", sid, [{"type": "thinking", "thinking": "t"}])) == \
        "the session has no finished answer to start from"


def test_a_rejected_fork_requeues_free_and_the_next_start_takes_the_disk_block(env, monkeypatch, tmp_path):
    """A forked run the agent rejects before doing anything ("No conversation found") costs no attempt,
    and the task's next start uses the coordinator's context on disk with the reason, not the same fork."""
    from ttp import coordsession, ctxfork
    from ttp import daemon as dmod
    p, d, cfg_dir, run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 5000})
    tid = p.db.add_task("forked one", "do the one thing", kind="code", origin="user")
    rid = d.start_run("worker", "TASK PROMPT", "claude", "standard", str(p.root), task=p.db.task(tid))
    assert ctxfork.forked(json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"]))
    usage = types.SimpleNamespace(cost_usd=0.0, output_tokens=0)
    assert dmod._resume_never_started({ctxfork.KEY: {"how": "fork"}}, usage)
    assert not dmod._resume_never_started({ctxfork.KEY: {"how": "disk"}}, usage)
    # The run ended as the daemon records a failed resume that never started.
    note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])
    note.update(not_waste="resume", resume_error="No conversation found with session ID: x")
    p.db.x("UPDATE runs SET status='failed', ended=?, note=? WHERE id=?", (time.time(), json.dumps(note), rid))
    got = ctxfork.prepare(p, d.cfg, dmod.get_provider("claude"), "claude", str(p.root), None, task=tid)
    assert got["how"] == "disk" and f"run {rid}'s fork was rejected" in got["why"] \
        and "No conversation found" in got["why"], got
    # Another task, or this task after a run that did start, forks as before.
    assert ctxfork.prepare(p, d.cfg, dmod.get_provider("claude"), "claude", str(p.root), None)["how"] == "fork"
    note["not_waste"] = "handoff"
    p.db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps(note), rid))
    assert ctxfork.prepare(p, d.cfg, dmod.get_provider("claude"), "claude", str(p.root), None,
                           task=tid)["how"] == "fork"
