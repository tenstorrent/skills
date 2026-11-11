# Coordinator-context tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of ctxfork
# (a worker or reviewer run starting from a fork of the coordinator's session, else its context on disk) here.
from __future__ import annotations

import datetime
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
    assert got["how"] == "resume" and not prompt.startswith(ctxfork.FORK_HEAD) and ctxfork.DISK_HEAD not in prompt


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
    """Claude Code forks by a transcript copy (the chat provider runs Claude Code: the same copy); Codex
    by a rollout copy (codex_fork cases below); Cursor keeps no forkable session (the disk block stands
    in); the fake copies its saved session."""
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
    assert "cannot fork" in get("cursor").fork_session(sid, "/w", None, str(uuid.uuid4()))
    assert get("chat").session_format == claude.session_format and not get("cursor").session_format
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


def _worker_session(cfg_dir, cwd, sid, said="I changed daemon.py and handed off"):
    """A finished worker run's transcript: its prompt and its last answer, a text."""
    folder = cfg_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join([_fork_line("user", sid, "the task's prompt"),
                               _fork_line("assistant", sid, [{"type": "text", "text": said}])]) + "\n")
    return path


def _ok_run(p, tid, sid, provider="claude", ended=None, role="worker"):
    ended = time.time() - 60 if ended is None else ended
    return p.db.x("INSERT INTO runs(task, role, provider, started, ended, status, session_id, note) "
                  "VALUES(?,?,?,?,?,'ok',?,?)", (tid, role, provider, ended - 600, ended, sid,
                                                json.dumps({"session_id": sid})))


def _start(p, d, tid, role="worker", provider="claude"):
    rid = d.start_run(role, "TASK PROMPT", provider, "standard", str(p.root), task=p.db.task(tid))
    spec = json.loads((p.runs / str(rid) / "run.json").read_text())
    row = p.db.one("SELECT note, session_id FROM runs WHERE id=?", (rid,))
    return (rid, spec["argv"], (p.runs / str(rid) / "prompt.md").read_text(),
            json.loads(row["note"] or "{}").get("coord_context"), row["session_id"])


def test_follow_ups_resume_the_session_of_the_run_they_carry_on(env, monkeypatch, tmp_path):
    """Owner's order (user msg #2210): continue hops, review fixes, rebases and re-reviews start from a copy
    of the session of the last ok run of the task they carry on, never writing into it, with the
    coordinator's newer context as a delta. A first review stays independent of its author's session."""
    from ttp import coordsession, ctxfork, obligations
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    code = p.db.add_task("the change", "do it", kind="code", origin="user")
    s_code = str(uuid.uuid4())
    src = _worker_session(cfg_dir, tmp_path / "wt-code", s_code)
    before = src.read_bytes()
    r_code = _ok_run(p, code, s_code, ended=time.time() - 3600)
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,1)",
           (time.time() - 7200, "in", "web", "user", "an order from before the run"))
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,1)",
           (time.time() - 60, "in", "web", "user", "a newer order: use threads"))
    obligations.add(p, "split independent parts into threads")

    def resumed(tid, run, role="worker"):
        rid, argv, prompt, got, session = _start(p, d, tid, role)
        assert got["how"] == "resume" and got["run"] == run, got
        assert session == got["session"] and session not in (s_code, coord)
        assert argv[argv.index("--resume") + 1] == session
        assert prompt.startswith(ctxfork.RESUME_HEAD) and prompt.rstrip().endswith("TASK PROMPT")
        assert "a newer order: use threads" in prompt and "an order from before the run" not in prompt
        assert "split independent parts into threads" in prompt
        assert ctxfork.forked({ctxfork.KEY: got})
        return session

    hop = p.db.add_task("continue: the change", "the rest", kind="code", origin="coordinator",
                        labels=[f"continues:{code}"])
    session = resumed(hop, r_code)
    copy = cfg_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(p.root)) / f"{session}.jsonl"
    rows = [json.loads(x) for x in copy.read_text().splitlines()]
    assert rows[-1]["message"]["content"][0]["text"] == "I changed daemon.py and handed off"
    assert {r["sessionId"] for r in rows} == {session}
    assert src.read_bytes() == before   # the earlier run's session is untouched
    # A first review is independent: it forks the coordinator, never the author's session; so does a
    # review a coordinator labelled as continuing the code task.
    review = p.db.add_task("Review #x", "review it", kind="review", origin="daemon", labels=[f"auto_review:{code}"])
    odd = p.db.add_task("Review again", "review it", kind="review", origin="coordinator",
                        labels=[f"continues:{code}"])
    for t in (review, odd):
        got = _start(p, d, t, "reviewer")[3]
        assert got["how"] == "fork" and "run" not in got, got
    fix = p.db.add_task(f"Fix review #{review}: the change", "fix", kind="code", origin="daemon",
                        labels=[f"continues:{code}", f"review_fix:{review}"])
    resumed(fix, r_code)
    s_rev = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / "wt-review", s_rev, "verdict: fail")
    r_rev = _ok_run(p, review, s_rev, ended=time.time() - 3600, role="reviewer")
    rereview = p.db.add_task(f"Re-review #{review}: x", "again", kind="review", origin="daemon",
                             labels=[f"auto_review:{fix}", f"continues:{review}"])
    resumed(rereview, r_rev, "reviewer")
    rebase = p.db.add_task("Rebase b onto the push branch", "rebase", kind="review", origin="daemon",
                           labels=[f"continues:{review}", "push_rebase:abc"])
    resumed(rebase, r_rev, "reviewer")
    # A follow-up's later run resumes its own last ok run, not the source again.
    s_hop = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / "wt-hop", s_hop, "first half done")
    r_hop = _ok_run(p, hop, s_hop, ended=time.time() - 3600)
    resumed(hop, r_hop)


def test_a_follow_up_falls_back_to_the_coordinator_fork_then_disk(env, monkeypatch, tmp_path):
    from ttp import coordsession, ctxfork
    from ttp import daemon as dmod
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    code = p.db.add_task("the change", "do it", kind="code", origin="user")
    hop = p.db.add_task("continue", "the rest", kind="code", origin="user", labels=[f"continues:{code}"])
    # No ok run to resume: the coordinator fork, as before.
    got = _start(p, d, hop)[3]
    assert got["how"] == "fork" and "no resume" not in got["why"], got
    # Its run was on a provider whose sessions this one cannot read.
    s = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / "wt", s)
    r = _ok_run(p, code, s, provider="codex")
    got = _start(p, d, hop)[3]
    assert got["how"] == "fork" and f"no resume: run {r} ran on codex" in got["why"], got
    # Its transcript is gone.
    p.db.x("UPDATE runs SET provider='claude', session_id=?, note='{}' WHERE id=?", (str(uuid.uuid4()), r))
    got = _start(p, d, hop)[3]
    assert got["how"] == "fork" and "not on disk" in got["why"], got
    # Nor can the coordinator's be forked: the disk block, naming both reasons.
    p.db.set_kv(coordsession.KEY, {})
    got = _start(p, d, hop)[3]
    assert got["how"] == "disk" and "no resume" in got["why"] and "no coordinator session" in got["why"], got
    # mode off: neither.
    monkeypatch.setenv("TTP_TEST_WORKER_CONTEXT", "off")
    assert _start(p, d, hop)[3]["how"] == "none"
    monkeypatch.delenv("TTP_TEST_WORKER_CONTEXT")
    # A rejected resume is never retried: then a rejected fork ends on the disk block, never a loop.
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    p.db.x("UPDATE runs SET session_id=? WHERE id=?", (s, r))
    hop2 = p.db.add_task("continue 2", "the rest", kind="code", origin="user", labels=[f"continues:{code}"])
    usage = types.SimpleNamespace(cost_usd=0.0, output_tokens=0)
    seen = []
    for _ in range(4):
        rid, _a, _pr, got, _s = _start(p, d, hop2)
        seen.append(got["how"])
        note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"])
        if not dmod._resume_never_started(note, usage):
            break
        note.update(not_waste="resume", resume_error="No conversation found")
        p.db.x("UPDATE runs SET status='failed', ended=?, note=? WHERE id=?", (time.time(), json.dumps(note), rid))
    assert seen == ["resume", "fork", "disk"], seen


def _resumed_run(p, cfg_dir, tmp_path, tid, src_run, how="resume"):
    """An ok run of `tid` that started by `how` (resuming run `src_run`), its transcript on disk."""
    from ttp import ctxfork
    sid = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / f"wt-{tid}", sid, f"task {tid} handed off")
    rid = _ok_run(p, tid, sid)
    got = {"how": how, "why": "x", "session": sid}
    if how == "resume":
        got["run"] = src_run
    p.db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps({"session_id": sid, ctxfork.KEY: got}), rid))
    return rid


def _fix_chain(p, cfg_dir, tmp_path):
    """A code task with an ok run, and a daemon review fix that continues it, ready to start."""
    code = p.db.add_task("the change", "do it", kind="code", origin="user")
    s = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / "wt-code", s)
    r_code = _ok_run(p, code, s, ended=time.time() - 3600)
    return code, r_code


def _fix(p, prev, n):
    review = p.db.add_task(f"Review #{prev}", "review it", kind="review", origin="daemon",
                           labels=[f"auto_review:{prev}"])
    return p.db.add_task(f"Fix review #{review}: the change ({n})", "fix", kind="code", origin="daemon",
                         labels=[f"continues:{prev}", f"review_fix:{review}"])


def test_a_review_fix_resumes_the_code_tasks_session_and_counts_its_hops(env, monkeypatch, tmp_path):
    """#7754 (b): a daemon fix resumes the session of the task it fixes; a later round's fix resumes the
    earlier fix's session, one hop deeper, while within review.hop_max (default 2)."""
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    assert ctxfork.hop_max(d.cfg) == 2
    code, r_code = _fix_chain(p, cfg_dir, tmp_path)
    fix1 = _fix(p, code, 1)
    got = _start(p, d, fix1)[3]
    assert got["how"] == "resume" and got["run"] == r_code, got
    assert ctxfork.hops(p, ctxfork.follow_source(p, fix1), fix1) == 1
    r_fix1 = _resumed_run(p, cfg_dir, tmp_path, fix1, r_code)
    fix2 = _fix(p, fix1, 2)
    assert ctxfork.hops(p, ctxfork.follow_source(p, fix2), fix2) == 2
    got = _start(p, d, fix2)[3]
    assert got["how"] == "resume" and got["run"] == r_fix1, got
    # A later run of the same fix resumes its own run: no extra hop.
    r_fix2 = _resumed_run(p, cfg_dir, tmp_path, fix2, r_fix1)
    assert ctxfork.hops(p, ctxfork.follow_source(p, fix2), fix2) == 2
    assert _start(p, d, fix2)[3]["run"] == r_fix2
    assert not p.db.one("SELECT id FROM events WHERE kind=?", (ctxfork.HOP_CAP_KIND,))


def test_a_review_fix_past_the_hop_cap_starts_fresh_and_raises_one_event(env, monkeypatch, tmp_path):
    """Past review.hop_max resumed hops the fix starts from the coordinator's context (fork, else disk),
    and the coordinator gets one review_hop_cap event per fix task, however often it starts."""
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    code, r_code = _fix_chain(p, cfg_dir, tmp_path)
    fix1 = _fix(p, code, 1)
    r_fix1 = _resumed_run(p, cfg_dir, tmp_path, fix1, r_code)
    fix2 = _fix(p, fix1, 2)
    r_fix2 = _resumed_run(p, cfg_dir, tmp_path, fix2, r_fix1)
    fix3 = _fix(p, fix2, 3)
    got = _start(p, d, fix3)[3]
    assert got["how"] == "fork" and "run" not in got, got
    assert "review.hop_max 2" in got["why"] and f"resuming run {r_fix2} would be hop 3" in got["why"], got
    ev = p.db.q("SELECT * FROM events WHERE kind=?", (ctxfork.HOP_CAP_KIND,))
    assert len(ev) == 1 and ev[0]["task"] == fix3 and ev[0]["status"] == "queued", ev
    assert f"#{fix3}" in ev[0]["text"] and f"#{fix2}" in ev[0]["text"]
    assert json.loads(ev[0]["data"]) == {"hops": 3, "cap": 2, "run": r_fix2, "source_task": fix2}
    # No coordinator session either: the disk block, still no second event.
    p.db.set_kv(coordsession.KEY, {})
    got = _start(p, d, fix3)[3]
    assert got["how"] == "disk" and "review.hop_max 2" in got["why"], got
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (ctxfork.HOP_CAP_KIND,))) == 1
    # A coordinator's own continue hop is not capped; review.hop_max 0 turns the cap off.
    hop = p.db.add_task("continue: the change", "more", kind="code", origin="coordinator",
                        labels=[f"continues:{fix2}"])
    assert _start(p, d, hop)[3]["how"] == "resume"
    d.cfg.setdefault("review", {})["hop_max"] = 0
    assert _start(p, d, fix3)[3]["how"] == "resume"


def test_a_review_fix_on_a_provider_without_resume_falls_back_and_a_fresh_start_resets_the_count(
        env, monkeypatch, tmp_path):
    """Provider parity (user msg #1828): a fix on a provider that cannot resume the code task's session
    (Cursor, or another session format) takes the coordinator fork, else disk, like any follow-up; a fix
    that started fresh ends its lineage's count, so the next round's fix resumes it as hop 1."""
    from ttp import coordsession, ctxfork
    from ttp import daemon as dmod
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    code, r_code = _fix_chain(p, cfg_dir, tmp_path)
    fix1 = _fix(p, code, 1)
    cursor = dmod.get_provider("cursor")
    got = ctxfork.prepare(p, d.cfg, cursor, "cursor", str(p.root), None, task=fix1)
    assert got["how"] in ("fork", "disk") and "run" not in got and "no resume" in got["why"], got
    codex = dmod.get_provider("codex")
    got = ctxfork.prepare(p, d.cfg, codex, "codex", str(p.root), None, task=fix1)
    assert got["how"] in ("fork", "disk") and f"no resume: run {r_code} ran on claude" in got["why"], got
    # The chat provider runs Claude Code: it resumes the same session format.
    assert ctxfork.prepare(p, d.cfg, dmod.get_provider("chat"), "chat", str(p.root), None,
                           task=fix1)["how"] == "resume"
    r_fix1 = _resumed_run(p, cfg_dir, tmp_path, fix1, None, how="fork")
    fix2 = _fix(p, fix1, 2)
    r_fix2 = _resumed_run(p, cfg_dir, tmp_path, fix2, r_fix1)
    fix3 = _fix(p, fix2, 3)
    assert ctxfork.hops(p, ctxfork.follow_source(p, fix3), fix3) == 2
    got = _start(p, d, fix3)[3]
    assert got["how"] == "resume" and got["run"] == r_fix2, got
    assert not p.db.one("SELECT id FROM events WHERE kind=?", (ctxfork.HOP_CAP_KIND,))


def test_stats_show_how_worker_runs_started_and_follow_ups_resumed(env):
    from ttp import coordstats, ctxfork
    p = make(env)
    a = p.db.add_task("a", "x", kind="code", origin="user")
    b = p.db.add_task("b", "x", kind="code", origin="user", labels=[f"continues:{a}"])
    c = p.db.add_task("c", "x", kind="code", origin="user", labels=[f"continues:{a}"])
    now = time.time()
    for tid, how in ((a, "fork"), (b, "resume"), (c, "fork"), (a, "disk"), (a, None)):
        note = {ctxfork.KEY: {"how": how}} if how else {}
        p.db.x("INSERT INTO runs(task, role, started, status, note) VALUES(?, 'worker', ?, 'ok', ?)",
               (tid, now - 10, json.dumps(note)))
    s = coordstats.report(p.db, p.runs, "UTC", 1, now=now)
    (day,) = s["worker_context"]
    assert day["runs"] == 5 and day["how"] == {"fork": 2, "resume": 1, "disk": 1, "unrecorded": 1}
    assert day["follow_ups"] == 2 and day["resumed"] == 1
    text = coordstats.text(s)
    assert "fork 2 (40%)" in text and "follow-ups resumed 1 of 2" in text


def _fork_codex_rollout(home, sid, cwd="/coord"):
    """A Codex rollout as `codex exec` 0.160 saves it: session meta, a finished turn naming the thread,
    then a turn still running and a last line still being written."""
    folder = home / "sessions" / "2026" / "11" / "08"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-08T10-00-00-{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "session_meta", "payload": {"id": sid, "session_id": sid, "cwd": cwd}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": cwd}},
        {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": sid, "text": "decided X"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "thread_id": sid}},
        {"type": "event_msg", "payload": {"type": "task_started", "thread_id": sid}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "text": "next wake"}},
    ]) + "\n" + '{"type": "event_msg", "payload": {"thread_id": "' + sid)
    return path


def _fork_fake_codex(tmp_path, monkeypatch):
    """A codex CLI whose `exec --help` lists the resume subcommand, as 0.160's does."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "codex"
    exe.write_text("#!/bin/sh\necho 'Commands:'\necho '  resume  Resume a previous session by id'\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def test_codex_fork_session_copies_the_rollout_under_a_new_id(monkeypatch, tmp_path):
    from ttp.providers import get_provider as get
    codex = get("codex")
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    sid, new = str(uuid.uuid4()), str(uuid.uuid4())
    _fork_codex_rollout(home, sid)
    assert codex.fork_session(sid, "/run", None, "../x") == "bad session id"
    assert codex.fork_session(str(uuid.uuid4()), "/run", None, new) == "the session's rollout is not on disk"
    own = tmp_path / "run-home"   # the run's own CODEX_HOME gets the copy
    assert codex.fork_session(sid, "/run", {"CODEX_HOME": str(own)}, new) == ""
    [copy] = list((own / "sessions").glob(f"*/*/*/rollout-*-{new}.jsonl"))
    assert copy.stat().st_mode & 0o777 == 0o600
    text = copy.read_text()
    assert sid not in text and text.endswith("\n")   # every id swapped; the partial last line left out
    rows = [json.loads(x) for x in text.splitlines()]   # up to the last finished turn
    assert rows[-1]["payload"]["type"] == "task_complete" and "next wake" not in text
    assert len(rows) == 4 and rows[0]["payload"] == {"id": new, "session_id": new, "cwd": "/run"}
    assert rows[2]["payload"]["thread_id"] == new and rows[2]["payload"]["text"] == "decided X"
    assert codex.session_saved(new, "/run", {"CODEX_HOME": str(own)})
    # A rollout with no session meta, or with no finished turn, is not one to start from.
    bad = str(uuid.uuid4())
    _fork_codex_rollout(home, bad).write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
    assert "no session_meta" in codex.fork_session(bad, "/run", None, str(uuid.uuid4()))
    path = _fork_codex_rollout(home, bad)
    path.write_text("\n".join(x for x in path.read_text().splitlines() if "task_complete" not in x) + "\n")
    assert "no finished turn" in codex.fork_session(bad, "/run", None, str(uuid.uuid4()))


def test_ctxfork_forks_across_providers_only_into_the_same_session_format(env, monkeypatch, tmp_path):
    """A chat-provider run forks a Claude coordinator's session (it runs Claude Code); a Codex run forks a
    Codex coordinator's (rollout copy, resumed by `codex exec resume`); a Codex or Cursor run of a
    Claude coordinator, or a Claude run of a Codex one, takes the disk block, saying why."""
    from ttp import coordsession, ctxfork
    from ttp.providers import claude as claude_mod, get_provider as get
    monkeypatch.setattr(claude_mod, "_FLAGS", {"--resume": True, "--session-id": True})
    monkeypatch.delenv("TTP_TEST_WORKER_CONTEXT")
    cfg_dir, home = tmp_path / "claude-config", tmp_path / "codex-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("CODEX_HOME", str(home))
    _fork_fake_codex(tmp_path, monkeypatch)
    p = make(env)
    cfg = p.config()
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 9000})
    got = ctxfork.prepare(p, cfg, get("chat"), "chat", str(p.root), None)
    assert got["how"] == "fork" and got["from"] == "claude" and got["tokens"] == 9000, got
    assert get("chat").resume_args(got["session"]) == ["--resume", got["session"]]
    assert list((cfg_dir / "projects").glob(f"*/{got['session']}.jsonl"))
    got = ctxfork.prepare(p, cfg, get("codex"), "codex", str(p.root), None)
    assert got["how"] == "disk" and "cannot resume its sessions" in got["why"], got
    got = ctxfork.prepare(p, cfg, get("cursor"), "cursor", str(p.root), None)
    assert got["how"] == "disk" and "has no fork" in got["why"], got
    csid = str(uuid.uuid4())
    _fork_codex_rollout(home, csid)
    p.db.set_kv(coordsession.KEY, {"id": csid, "provider": "codex", "context_tokens": 7000})
    got = ctxfork.prepare(p, cfg, get("codex"), "codex", str(p.root), None)
    assert got["how"] == "fork" and got["from"] == "codex", got
    assert get("codex").resume_args(got["session"]) == ["resume", got["session"]]
    assert list((home / "sessions").glob(f"*/*/*/rollout-*-{got['session']}.jsonl"))
    for name in ("claude", "chat"):
        assert ctxfork.prepare(p, cfg, get(name), name, str(p.root), None)["how"] == "disk"
    # An unknown provider name in the saved state is another format, never an error.
    p.db.set_kv(coordsession.KEY, {"id": csid, "provider": "gone", "context_tokens": 7000})
    assert "runs on gone" in ctxfork.prepare(p, cfg, get("codex"), "codex", str(p.root), None)["why"]


def test_a_continued_run_records_that_it_resumed_its_own_session(env, monkeypatch, tmp_path):
    p, d, cfg_dir, run = _fork_daemon(env, monkeypatch, tmp_path)
    own = str(uuid.uuid4())
    argv, prompt, got, session = run(resume=own)
    assert got == {"how": "resume", "why": "continues its own session", "session": own}
    assert "--resume" in argv and not prompt.startswith(("# FORKED", "# COORDINATOR CONTEXT"))


def test_each_provider_reports_its_start_mode_through_one_method(monkeypatch, tmp_path):
    """fork_gap says, per provider and session source, whether a run forks or takes the disk block, and
    start_from_session is the one call that does it: claude and chat fork a Claude Code session, codex a
    Codex rollout, cursor none (it resumes only its own chats); an unknown source is another format."""
    from ttp.providers import claude as claude_mod, get_provider as get
    monkeypatch.setattr(claude_mod, "_FLAGS", {"--resume": True, "--session-id": True})
    _fork_fake_codex(tmp_path, monkeypatch)
    for run, src in (("claude", "claude"), ("chat", "claude"), ("claude", "chat"), ("codex", "codex")):
        assert get(run).fork_gap(src) == "", (run, src)
    assert "cannot resume its sessions" in get("codex").fork_gap("claude")
    assert "cannot resume its sessions" in get("claude").fork_gap("codex")
    assert "runs on gone" in get("chat").fork_gap("gone")
    for src in ("cursor", "claude", "codex"):
        assert "has no fork" in get("cursor").fork_gap(src)
    gap = get("cursor").start_from_session("cursor", str(uuid.uuid4()), "/run", None, str(uuid.uuid4()))
    assert "has no fork" in gap
    gap = get("codex").start_from_session("claude", str(uuid.uuid4()), "/run", None, str(uuid.uuid4()))
    assert "cannot resume its sessions" in gap   # never reaches the copy
    monkeypatch.setattr(claude_mod, "_FLAGS", {"--resume": False, "--session-id": True})
    assert "cannot resume a session" in get("claude").fork_gap("claude")


def _sub_rollout(home, day, sid, parent, totals, stamp="2026-11-08T10:00:00.000Z"):
    folder = home / "sessions" / "2026" / "11" / day
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"id": sid, "session_id": parent or sid}
    if parent:
        meta.update(parent_thread_id=parent,
                    source={"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}})
    rows = [{"type": "session_meta", "payload": meta}]
    for t, (i, c, o) in zip([stamp] * len(totals) if isinstance(stamp, str) else stamp, totals):
        rows.append({"timestamp": t, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}})
    (folder / f"rollout-2026-11-{day}T10-00-00-{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_codex_sub_agents_count_in_the_run_cost(monkeypatch, tmp_path):
    """A Codex worker's sub-agents (multi_agent, on in 0.160) write their own rollouts and are not in
    turn.completed: their tokens, at any depth and past midnight, are added to the run's usage and to
    the runner's cost_so_far; another thread's sub-agents are not; a resumed run counts only since."""
    from ttp.providers import get_provider as get
    from ttp.providers.base import RunUsage
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex = get("codex").use("gpt-5.5", None)
    root, child, grand, other, stranger = (str(uuid.uuid4()) for _ in range(5))
    _sub_rollout(home, "08", root, None, [(46268, 42752, 88)])
    _sub_rollout(home, "08", child, root, [(15112, 12032, 7)])
    _sub_rollout(home, "09", grand, child, [(1000, 0, 10)])   # started past midnight UTC
    _sub_rollout(home, "08", other, None, [(5, 0, 5)])
    _sub_rollout(home, "08", stranger, other, [(99999, 0, 999)])   # another thread's sub-agent
    out = tmp_path / "output.jsonl"
    out.write_text(json.dumps({"type": "thread.started", "thread_id": root}) + "\n" + json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 46268, "cached_input_tokens": 42752,
                                             "output_tokens": 88}}) + "\n")
    u = codex.parse(out)
    own = u.cost_usd
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (46268 - 42752, 42752, 88)
    codex.add_subagent_usage(u, out)
    assert u.extra["subagent_tokens"] == [16112, 12032, 17]
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (46268 - 42752 + 4080, 42752 + 12032, 105)
    assert u.cost_usd == own + codex._price(16112, 12032, 17) > own
    assert codex.cost_so_far(out) == u.cost_usd
    # A resumed run: a sub-agent's tokens before the run started are not its own.
    _sub_rollout(home, "08", child, root, [(15112, 12032, 7), (20112, 12032, 9)],
                 stamp=["2026-11-08T09:00:00.000Z", "2026-11-08T11:00:00.000Z"])
    since = datetime.datetime(2026, 11, 8, 10, tzinfo=datetime.timezone.utc).timestamp()
    assert codex._subagent_tokens(out, since) == (5000 + 1000, 0, 2 + 10)   # the grandchild ran after
    # A sub-agent whose first line is still being written is read once it is whole.
    late = str(uuid.uuid4())
    part = home / "sessions" / "2026" / "11" / "08" / f"rollout-2026-11-08T10-00-00-{late}.jsonl"
    part.write_text('{"type": "session_meta", "payload": {"id": "' + late + '", "parent_thread_id": "' + root)
    assert codex._subagent_tokens(out) == (16112 + 5000, 12032, 17 + 2)
    _sub_rollout(home, "08", late, root, [(7, 0, 1)])
    assert codex._subagent_tokens(out) == (16112 + 5000 + 7, 12032, 17 + 2 + 1)
    # No thread, no rollouts, or a layout that moved: nothing added, never an error.
    bare = RunUsage()
    (tmp_path / "none.jsonl").write_text("")
    codex.add_subagent_usage(bare, tmp_path / "none.jsonl")
    assert bare.cost_usd == 0 and "subagent_tokens" not in bare.extra


def _finish_codex_run(p, home, lines, started):
    """A Codex worker run with CODEX_HOME at `home`, finished through Daemon.finish_run; its row."""
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("build", "", kind="work", tier="standard", priority=3, budget_usd=8.0, labels=[])
    p.db.update_task(tid, status="running")
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,boot_id,note) "
                 "VALUES(?,'worker','codex','opus',?,'running',?,'{}')", (tid, started, d.boot))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (run_dir / "stderr.log").write_text("")
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 4.0, "timeout_s": 1200, "argv": [],
                                                  "env": {"CODEX_HOME": str(home)}}))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": 0, "started": started, "ended": time.time(), "stopped": "budget", "slept_s": 0})
    return p.db.one("SELECT * FROM runs WHERE id=?", (rid,))


def test_a_cut_off_codex_run_with_sub_agents_books_its_own_thread_and_theirs_once(env, monkeypatch):
    """A Codex run cut off mid-turn (no turn.completed) that spawned a sub-agent: its own thread is
    priced from its rollout and the sub-agent added once; the sub-agent's cost alone must not make
    the run look priced and skip the cut-off pricing. A completed run books turn.completed plus its
    sub-agents once."""
    home = env["tmp"] / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    started = time.time() - 600
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(started + 30))
    root, child = str(uuid.uuid4()), str(uuid.uuid4())
    _sub_rollout(home, "08", root, None, [(2_000_000, 0, 50_000)], stamp=stamp)
    _sub_rollout(home, "08", child, root, [(10_000, 0, 100)], stamp=stamp)
    p = make(env)
    p.set_config("pricing", {"codex": {"opus": [1.0, 0.1, 2.0]}})
    own, sub = 2_000_000 / 1e6 + 50_000 * 2.0 / 1e6, 10_000 / 1e6 + 100 * 2.0 / 1e6
    run = _finish_codex_run(p, home, [{"type": "thread.started", "thread_id": root}], started)
    assert run["cost_basis"] == "tokens", dict(run)
    assert run["cost_usd"] == pytest.approx(own + sub), dict(run)
    done = _finish_codex_run(p, home, [{"type": "thread.started", "thread_id": root}, {
        "type": "turn.completed", "usage": {"input_tokens": 300_000, "cached_input_tokens": 0,
                                            "output_tokens": 1_000}}], started)
    assert done["cost_usd"] == pytest.approx(0.3 + 0.002 + sub), dict(done)


def test_a_resumed_runs_delta_shows_only_triaged_chat_and_none_when_chat_is_off(env):
    """A resumed worker sees only handled inbound chat (as coordsession.chat_lines), never a user message the
    coordinator has not triaged; worker_chat_chars 0 leaves the chat section out, as disk_block does."""
    from ttp import ctxfork
    p = make(env)
    since = time.time() - 600
    for text, handled in (("a triaged order", 1), ("an untriaged order", 0)):
        p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,?)",
               (time.time() - 60, "in", "web", "user", text, handled))
    p.db.x("INSERT INTO messages(ts,direction,chat,kind,text,handled) VALUES(?,?,?,?,?,1)",
           (time.time() - 30, "out", "web", "reply", "on it"))
    got = ctxfork.delta_block(p, {}, since)
    assert "a triaged order" in got and "on it" in got and "an untriaged order" not in got
    assert "left out" not in got
    off = ctxfork.delta_block(p, {"coordinator": {"session": {"worker_chat_chars": 0}}}, since)
    assert "### Chat since then" not in off and "a triaged order" not in off
    assert "### Order ledger" in off


def test_forked_runs_send_the_coordinator_prefix_and_keep_their_own_text_after_it(env, monkeypatch, tmp_path):
    """Task #49: the cache matches tools, then the system prompt, then the history. Two forks with a
    different task, run id and worktree send the coordinator's last launch's system prompt, answer
    schema and Bash timeouts byte for byte; their own system prompt and task follow in the prompt."""
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    from ttp.providers import claude
    monkeypatch.setattr(claude, "_FLAGS", {**claude._FLAGS, claude.APPEND_SYSTEM_FILE: True})
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 50000})

    def start(cwd, own="# WORKER RULES\nwrite result.json"):
        tid = p.db.add_task(f"t {uuid.uuid4().hex[:6]}", "do the one thing", kind="code", origin="user")
        rid = d.start_run("worker", f"TASK PROMPT {tid}", "claude", "standard", str(cwd), task=p.db.task(tid),
                          append_system=own)
        rd = p.runs / str(rid)
        note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}")
        return (json.loads((rd / "run.json").read_text())["argv"], (rd / "prompt.md").read_text(),
                (rd / "system.md").read_text(), note.get("coord_context") or {})

    argv, prompt, system, ctx = start(p.root)   # no coordinator run yet: the run's own system prompt
    assert system.startswith("# WORKER RULES") and "prefix" not in ctx and "--json-schema" not in argv
    # The coordinator's last launch, as a full-tools session wake records it: the worker's tools plus its
    # answer schema and the Bash timeouts of its tool hooks.
    coord_dir = tmp_path / "coord-run"
    coord_dir.mkdir()
    (coord_dir / "system.md").write_text("# Role\nYou coordinate.")
    settings = json.loads(argv[argv.index("--settings") + 1])
    settings = {**settings, "hooks": {"PreToolUse": []},
                "env": {"TTP_COORD_FULL_TOOLS": "1", "BASH_DEFAULT_TIMEOUT_MS": "300000", "BASH_MAX_TIMEOUT_MS": "300000"}}
    coord = [a for a in argv if a != "-"]
    i = coord.index("--append-system-prompt-file")
    del coord[i:i + 2]   # the coordinator's own, below
    coord[coord.index("--settings") + 1] = json.dumps(settings)
    coord += ["--json-schema", '{"type": "object"}', "--append-system-prompt-file", str(coord_dir / "system.md")]
    p.db.x("INSERT INTO runs(role,status,started,dir) VALUES('coordinator','ok',?,?)", (time.time(), str(coord_dir)))
    (coord_dir / "run.json").write_text(json.dumps({"argv": coord}))
    runs = []
    for wt in ("wt1", "wt2"):
        (tmp_path / wt).mkdir()
        runs.append(start(tmp_path / wt))
    for argv, prompt, system, ctx in runs:
        assert ctx["how"] == "fork" and ctx["prefix"] == "coordinator", ctx
        assert system == "# Role\nYou coordinate."
        assert argv[argv.index("--json-schema") + 1] == '{"type": "object"}'
        got = json.loads(argv[argv.index("--settings") + 1])
        assert got["env"]["BASH_MAX_TIMEOUT_MS"] == "300000" and "TTP_COORD_FULL_TOOLS" not in got["env"]
        assert got["hooks"] != {"PreToolUse": []}   # the run keeps its own hooks
        lead, _, rest = prompt.partition("# WORKER RULES\nwrite result.json")
        assert lead.startswith(ctxfork.FORK_HEAD) and "StructuredOutput" in lead and "TASK PROMPT" in rest
    # Up to the per-run part, the two requests are the same: model, tools' settings, schema, system prompt.
    shape = lambda a: ({k: v for k, v in json.loads(a[a.index("--settings") + 1]).items() if k != "hooks"},  # noqa: E731
                       a[a.index("--model") + 1] if "--model" in a else "", a[a.index("--json-schema") + 1])
    assert shape(runs[0][0]) == shape(runs[1][0]) and runs[0][2] == runs[1][2]
    assert runs[0][1].split("TASK PROMPT")[0] == runs[1][1].split("TASK PROMPT")[0]
    # Another model cannot read the coordinator's cache: the run keeps its own prefix and says why.
    other = [*coord, "--model", "other-model"] if "--model" not in coord else \
        [("other-model" if i and coord[i - 1] == "--model" else a) for i, a in enumerate(coord)]
    (coord_dir / "run.json").write_text(json.dumps({"argv": other}))
    argv, prompt, system, ctx = start(p.root)
    assert system.startswith("# WORKER RULES") and "--json-schema" not in argv
    if "--model" in argv:   # task #74: it takes the disk block, not a fork it would write again
        assert ctx["how"] == "disk" and "is not the coordinator's other-model" in ctx["why"], ctx
        assert prompt.startswith(ctxfork.DISK_HEAD)
    else:
        assert "differs" in ctx["prefix"]
    # Task #7824: a coordinator system prompt holding its no-tools or read-only rule is never a worker's,
    # cache or not: the fork keeps the run's own system prompt and answer format.
    for rule in ctxfork.COORD_TOOL_RULES:
        (coord_dir / "run.json").write_text(json.dumps({"argv": coord}))
        (coord_dir / "system.md").write_text(f"# Role\nYou coordinate.\n{rule}: only your answer.")
        argv, prompt, system, ctx = start(p.root)
        assert ctx["how"] == "fork" and "prefix" not in ctx and "--json-schema" not in argv, ctx
        assert system.startswith("# WORKER RULES") and not ctxfork.coord_only([system, prompt]), system


def test_sonnet_workers_and_reviewers_share_one_system_prompt_while_memory_changes(env, monkeypatch, tmp_path):
    """Task #51: the cache matches tools, then the system prompt. A worker and a reviewer run on the same
    model, with a different task, run id and worktree, and a memory entry added between them, send the
    same settings and system prompt byte for byte; memory, which changes most often, leads each run's
    own prompt after it."""
    from ttp.prompts import worker_system, worker_task
    p, d, _cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    from ttp.providers import claude
    monkeypatch.setattr(claude, "_FLAGS", {**claude._FLAGS, claude.APPEND_SYSTEM_FILE: True})
    p.add_memory("MEM-ONE the nightly report goes to notes/.", kind="decision", title="one", audience="workers")

    def start(role, wt, kind):
        (tmp_path / wt).mkdir()
        tid = p.db.add_task(f"{kind} {uuid.uuid4().hex[:6]}", f"do the {wt} thing", kind=kind, origin="user")
        task = p.db.task(tid)
        rid = d.start_run(role, worker_task(p, task, str(tmp_path / wt), f"ttp/t{tid}"), "claude", "standard",
                          str(tmp_path / wt), task=task, append_system=worker_system(p, task))
        rd = p.runs / str(rid)
        return json.loads((rd / "run.json").read_text())["argv"], (rd / "prompt.md").read_text(), \
            (rd / "system.md").read_text()

    a = start("worker", "wtA", "code")
    p.add_memory("MEM-TWO the bench box is shared.", kind="fact", title="two", audience="workers")
    b = start("reviewer", "wtB", "review")
    shape = lambda a: ({k: v for k, v in json.loads(a[a.index("--settings") + 1]).items() if k != "hooks"},  # noqa: E731
                       a[a.index("--model") + 1] if "--model" in a else "")
    assert shape(a[0]) == shape(b[0]) and a[2] == b[2]
    assert "MEM-ONE" not in a[2] and "PROJECT MEMORY" not in a[2]
    assert "MEM-ONE" in a[1] and "MEM-TWO" not in a[1] and "MEM-TWO" in b[1]


def test_a_run_on_another_claude_model_forks_the_coordinators_last_turns_else_takes_the_disk_block(env, monkeypatch, tmp_path):
    """Task #8308 (before: task #74): the prompt cache is per model, so a Sonnet fork of an Opus
    coordinator cannot read its cache. It forks only the session's last turns (worker_fork_other_model_tokens,
    "tail"), the same copy for every such run, so they share one cache entry; a model outside the Claude
    families, or the setting at 0, takes the disk block. A run on the coordinator's model forks it whole."""
    from ttp import coordsession, ctxfork
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 50000})
    coord_dir = tmp_path / "coord-run"
    coord_dir.mkdir()
    (coord_dir / "run.json").write_text(json.dumps({"argv": ["claude", "-p", "--model", "claude-opus-5-5"]}))
    p.db.x("INSERT INTO runs(role,status,started,dir) VALUES('coordinator','ok',?,?)", (time.time(), str(coord_dir)))
    from ttp.providers import get_provider
    prov = get_provider("claude")
    copies = []
    for model, how in (("sonnet", "fork"), ("claude-sonnet-5-5", "fork"), ("claude-opus-5-5", "fork"),
                       ("opus", "fork"), (None, "fork"), ("glm-5.3", "disk")):
        got = ctxfork.prepare(p, p.config(), prov, "claude", str(p.root), {}, model=model)
        assert got["how"] == how, (model, got)
        assert ("tail" in got) == (model in ("sonnet", "claude-sonnet-5-5")), (model, got)
        if "tail" in got:
            assert got["tail"] == 8000 and got["tokens"] == 8000, got
            path = [x for x in prov.session_paths(got["session"], str(p.root), {}) if x.name.endswith(".jsonl")][0]
            copies.append([{k: v for k, v in json.loads(line).items() if k != "sessionId"}
                           for line in path.read_text().splitlines()])
        if how == "disk":
            assert "not the coordinator's claude-opus-5-5" in got["why"] and got["lead"].startswith(ctxfork.DISK_HEAD)
    assert len(copies) == 2 and copies[0] == copies[1]   # one shared copy: the same history, byte for byte
    p.set_config("coordinator.session.worker_fork_other_model_tokens", 0)
    got = ctxfork.prepare(p, p.config(), prov, "claude", str(p.root), {}, model="sonnet")
    assert got["how"] == "disk" and "not the coordinator's" in got["why"], got
    assert ctxfork.same_model("opus", "claude-opus-5-5") and not ctxfork.same_model("sonnet", "claude-opus-5-5")
    assert not ctxfork.same_model("claude-opus-4-1", "claude-opus-5-5")


def test_a_last_turns_copy_opens_on_a_user_turn_and_its_start_moves_only_in_steps(monkeypatch, tmp_path):
    """Task #8308: the copy a run on another model starts from holds about tail_tokens of the session's
    last turns, opens on a user turn (never a tool's result) with no parent, and keeps its start while the
    session grows by less than half the cap, so the forks in between share one cache entry. A provider
    that cannot copy only the last turns says so, and the caller takes the disk block."""
    from ttp.providers import get_provider as get
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    claude, sid = get("claude"), str(uuid.uuid4())
    folder = tmp_path / "cfg" / "projects" / "-src"
    folder.mkdir(parents=True)
    so = [{"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {"actions": []}}]
    lines = []
    for i in range(10):
        lines += [_fork_line("user", sid, f"wake {i} " + "x" * 4000),
                  _fork_line("assistant", sid, [{"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {}}]),
                  _fork_line("user", sid, [{"type": "tool_result", "tool_use_id": f"b{i}", "content": "y" * 2000}]),
                  _fork_line("assistant", sid, so)]

    def copy(cap):
        (folder / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
        new = str(uuid.uuid4())
        assert claude.fork_session(sid, "/w", None, new, tail_tokens=cap) == ""
        return [json.loads(x) for x in (tmp_path / "cfg" / "projects" / "-w" / f"{new}.jsonl").read_text().splitlines()]

    rows = copy(3000)
    assert rows[0]["message"]["content"].startswith("wake ") and rows[0]["parentUuid"] is None
    assert sum(len(json.dumps(r["message"])) // 4 for r in rows) <= 3000 and len(rows) < len(lines)
    assert len(copy(10 ** 6)) == len(lines)   # under the cap: the whole session
    starts = set()
    for i in range(12):   # about 1000 tokens in all: under the step of 1500
        lines += [_fork_line("user", sid, f"small {i}"), _fork_line("assistant", sid, so)]
        starts.add(copy(3000)[0]["uuid"])
    assert len(starts) <= 2, starts
    for name in ("codex", "fake"):
        assert "cannot copy only" in get(name).start_from_session(name, sid, "/w", None, str(uuid.uuid4()),
                                                                  tail_tokens=3000)


def test_a_fork_of_the_last_turns_sends_the_coordinators_prefix_on_its_own_model():
    """Task #8308: every Sonnet fork of an Opus coordinator sends the coordinator's tools, answer schema
    and system prompt on its own model, so all of them share one prefix; a whole-session fork on another
    model is still refused the shape."""
    from ttp import ctxfork
    coord = ["claude", "-p", "--model", "claude-opus-5-5",
             "--settings", json.dumps({"env": {"BASH_MAX_TIMEOUT_MS": "300000"}}), "--json-schema", '{"type": "object"}']
    shape = {"argv": coord, "system": "# Role"}
    runs = [["claude", "-p", "--model", "sonnet", "--settings", json.dumps({"hooks": {"h": n}}), "-"] for n in (1, 2)]
    got, why = ctxfork.shape_argv(runs[0], shape)
    assert got is None and "model differs" in why
    out = [ctxfork.shape_argv(a, shape, other_model=True)[0] for a in runs]
    for a in out:
        assert a[a.index("--model") + 1] == "sonnet" and a[a.index("--json-schema") + 1] == '{"type": "object"}'
        assert json.loads(a[a.index("--settings") + 1])["env"] == {"BASH_MAX_TIMEOUT_MS": "300000"} and a[-1] == "-"
    assert [x for x in out[0] if "hooks" not in x] == [x for x in out[1] if "hooks" not in x]


def test_a_follow_up_on_another_model_starts_fresh_never_resuming_the_other_models_session(env, monkeypatch, tmp_path):
    """Owner's order (msg #188, task #175): the prompt cache is per model, so a follow-up that resumed a
    session another model made (Sonnet resuming an Opus run or the reverse) wrote its whole history again
    and never read it (task #53: 4 of 10 cold resumes). It starts as a fresh run of its model does (the
    coordinator fork, shaped, or the disk block), with the task it continues and that task's last
    hand-off in its prompt and none of the other run's history. A same-model follow-up still resumes."""
    from ttp import coordsession, ctxfork
    from ttp.prompts import worker_task
    from ttp.providers import get_provider
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    coord = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, coord)
    p.db.set_kv(coordsession.KEY, {"id": coord, "provider": "claude", "context_tokens": 5000})
    code = p.db.add_task("the change", "do it", kind="code", origin="user")
    p.db.update_task(code, status="done", result=json.dumps({"status": "done", "summary": "HANDOFF: half of it done"}))
    s_code = str(uuid.uuid4())
    _worker_session(cfg_dir, tmp_path / "wt-code", s_code, "OTHER MODEL'S HISTORY")
    r_code = _ok_run(p, code, s_code, ended=time.time() - 60)
    hop = p.db.add_task("continue: the change", "the rest", kind="code", origin="coordinator",
                        labels=[f"continues:{code}"])
    prov = get_provider("claude")

    def prep(run_model, model):
        p.db.x("UPDATE runs SET model=? WHERE id=?", (run_model, r_code))
        return ctxfork.prepare(p, p.config(), prov, "claude", str(p.root), {}, task=hop, model=model)

    # Same model, or one's bare family alias, or a model unknown on either side: it resumes as before.
    for run_model, model in (("claude-opus-5-5", "claude-opus-5-5"), ("claude-opus-5-5", "opus"),
                             ("sonnet", "claude-sonnet-5-5"), ("", "claude-sonnet-5-5"), ("claude-opus-5-5", None)):
        got = prep(run_model, model)
        assert got["how"] == "resume" and got["run"] == r_code, (run_model, model, got)
    # Across models: never a resume. With no coordinator model known it forks the coordinator as a fresh run does.
    for run_model, model in (("claude-opus-5-5", "claude-sonnet-5-5"), ("sonnet", "opus")):
        got = prep(run_model, model)
        assert got["how"] == "fork" and "run" not in got, got
        assert f"no resume: run {r_code} ran on {run_model}, this run on {model}" in got["why"], got
        assert not got["lead"].startswith(ctxfork.RESUME_HEAD)
    # A coordinator on Opus: a Sonnet follow-up of an Opus run forks the coordinator's last turns, as a fresh
    # Sonnet run does (task #8308); a non-Claude model, or the setting at 0, takes the disk block.
    coord_dir = tmp_path / "coord-run"
    coord_dir.mkdir()
    (coord_dir / "run.json").write_text(json.dumps({"argv": ["claude", "-p", "--model", "claude-opus-5-5"]}))
    p.db.x("INSERT INTO runs(role,status,started,dir) VALUES('coordinator','ok',?,?)", (time.time(), str(coord_dir)))
    got = prep("claude-opus-5-5", "claude-sonnet-5-5")
    assert got["how"] == "fork" and got.get("tail") and "run" not in got, got
    assert "no resume: run" in got["why"] and "ran on claude-opus-5-5" in got["why"], got
    assert not got["lead"].startswith(ctxfork.RESUME_HEAD)
    got = prep("claude-opus-5-5", "glm-5.3")
    assert got["how"] == "disk" and got["lead"].startswith(ctxfork.DISK_HEAD), got
    assert "ran on claude-opus-5-5" in got["why"] and "not the coordinator's" in got["why"], got
    p.set_config("coordinator.session.worker_fork_other_model_tokens", 0)
    got = prep("claude-opus-5-5", "claude-sonnet-5-5")
    assert got["how"] == "disk" and "not the coordinator's" in got["why"], got
    p.set_config("coordinator.session.worker_fork_other_model_tokens", 8000)
    assert prep("claude-sonnet-5-5", "claude-opus-5-5")["how"] == "fork"   # an Opus follow-up: the coordinator fork
    # Through the daemon: the run's own model against the source run's, and its prompt carries the record.
    p.db.x("DELETE FROM runs WHERE role='coordinator'")
    (tmp_path / "wt-hop").mkdir()
    task = p.db.task(hop)

    def start():
        rid = d.start_run("worker", worker_task(p, task, str(tmp_path / "wt-hop"), f"ttp/t{hop}"), "claude",
                          "standard", str(p.root), task=task)
        rd = p.runs / str(rid)
        argv = json.loads((rd / "run.json").read_text())["argv"]
        row = p.db.one("SELECT model, note, session_id FROM runs WHERE id=?", (rid,))
        return argv, (rd / "prompt.md").read_text(), json.loads(row["note"] or "{}")["coord_context"], row
    p.db.x("UPDATE runs SET model=NULL WHERE id=?", (r_code,))
    _a, _pr, got, row = start()
    assert got["how"] == "resume" and got["run"] == r_code, got   # the source has no model: as before
    assert row["model"], "the run records its model, which the next follow-up compares"
    p.db.x("UPDATE runs SET model=? WHERE id=?", (row["model"], r_code))
    assert start()[2]["how"] == "resume"   # the same model: it resumes
    p.db.x("UPDATE runs SET model=? WHERE id=?", ("some-other-model", r_code))
    argv, prompt, got, row = start()
    assert got["how"] == "fork" and f"run {r_code} ran on some-other-model" in got["why"], got
    assert prompt.startswith(ctxfork.FORK_HEAD) and ctxfork.RESUME_HEAD not in prompt
    assert f"This task continues #{code}" in prompt and "HANDOFF: half of it done" in prompt
    copy = cfg_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(p.root)) / f"{got['session']}.jsonl"
    assert "OTHER MODEL'S HISTORY" not in copy.read_text()
    assert argv[argv.index("--resume") + 1] == got["session"] != s_code


_CODEX_COORD_TEXT = "# Role\nYou coordinate.\nThis session has no tools: no files, shell, ssh or web; only your answer."


def _dev(*texts):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": t} for t in texts]}


def _codex_rollout_with_instructions(home, sid, cwd="/coord"):
    """A Codex coordinator rollout as `codex exec` saves it: the harness's developer instructions next to
    Codex's own <...> blocks, a compaction whose histories carry them again, then a finished turn."""
    path = _fork_codex_rollout(home, sid, cwd)
    rows = [json.loads(x) for x in path.read_text().split("\n")[:4]]
    dev = [{"type": "response_item", "payload": _dev(_CODEX_COORD_TEXT, "<skills_instructions>skills</skills_instructions>")},
           {"type": "response_item", "payload": _dev("<multi_agent_role>root</multi_agent_role>")},
           {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": "# a user digest"}]}}]
    compacted = {"type": "compacted", "payload": {"message": "", "replacement_history": [_dev(_CODEX_COORD_TEXT)],
                                                  "guardian_history": [{"items": [_dev(_CODEX_COORD_TEXT)]}]}}
    path.write_text("\n".join(json.dumps(r) for r in rows[:1] + dev + rows[1:3] + [compacted] + rows[3:]) + "\n")
    return path


def test_codex_own_instructions_replace_the_sessions_harness_text_everywhere(monkeypatch, tmp_path):
    """Task #198 (user msg #208): `codex exec resume` keeps the rollout's developer messages and takes no new
    developer_instructions, so a worker forked from a coordinator without tools ran under "This session has
    no tools". The run's own text replaces the harness's blocks (top level and inside compactions); Codex's
    own <...> blocks, user messages and the source session stay; Claude takes each launch's system prompt."""
    from ttp.providers import get_provider as get
    codex = get("codex")
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    sid, new = str(uuid.uuid4()), str(uuid.uuid4())
    src = _codex_rollout_with_instructions(home, sid)
    before = src.read_bytes()
    own = {"CODEX_HOME": str(tmp_path / "run-home")}
    assert codex.fork_session(sid, "/run", own, new) == ""
    worker = "# WORKER RULES\nuse your tools"
    assert codex.own_instructions(new, "/run", own, worker) == ""
    [copy] = list((tmp_path / "run-home" / "sessions").glob(f"*/*/*/rollout-*-{new}.jsonl"))
    text = copy.read_text()
    assert "no tools" not in text and text.count("WORKER RULES") == 3   # top level, replacement and guardian
    assert "<skills_instructions>" in text and "<multi_agent_role>" in text and "# a user digest" in text
    assert copy.stat().st_mode & 0o777 == 0o600 and src.read_bytes() == before
    rows = [json.loads(x) for x in text.splitlines()]
    assert rows[-1]["payload"]["type"] == "task_complete"
    # Twice is the same; a message holding the harness text twice keeps the run's text once, and its
    # other parts stay (task #210).
    assert codex.own_instructions(new, "/run", own, worker) == "" and copy.read_text().count("WORKER RULES") == 3
    two = str(uuid.uuid4())
    path = _fork_codex_rollout(home, two)
    path.write_text(json.dumps({"type": "response_item", "payload": _dev("# a", "<x>", "# a", "# b")}) + "\n" + '{"partial')
    assert codex.own_instructions(two, "/", None, worker) == ""
    lines = path.read_text().split("\n")
    assert [c["text"] for c in json.loads(lines[0])["payload"]["content"]] == [worker, "<x>", "# b"]
    assert lines[-1] == '{"partial'   # a line still being written stays as it was
    assert codex.own_instructions(str(uuid.uuid4()), "/", None, worker) == ""   # no session here: nothing to do
    bare = str(uuid.uuid4())
    _fork_codex_rollout(home, bare)
    assert "no developer instructions" in codex.own_instructions(bare, "/", None, worker)
    assert get("claude").own_instructions(sid, "/run", None, worker) == "" and src.read_bytes() == before


def test_codex_own_instructions_keep_codexs_memory_block(monkeypatch, tmp_path):
    """Task #210 (review #203 of #198): Codex appends its own "## Memory" block (no "<") to the developer
    message after the harness text. The swap replaces only the harness text, at top level and inside
    compactions; the Memory block, Codex's <...> blocks and their order stay exactly as they were."""
    from ttp.providers import get_provider as get
    codex = get("codex")
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    sid, new = str(uuid.uuid4()), str(uuid.uuid4())
    path = _fork_codex_rollout(home, sid)
    memory = "## Memory\n\nYou have notes from earlier sessions.\n- prefer short answers"
    msg = _dev(_CODEX_COORD_TEXT, memory, "<skills_instructions>skills</skills_instructions>")
    rows = [json.loads(x) for x in path.read_text().split("\n")[:4]]
    compacted = {"type": "compacted", "payload": {"message": "", "replacement_history": [msg],
                                                  "guardian_history": [{"items": [msg]}]}}
    alone = _dev("  " + memory)   # a message holding only Codex's Memory block: nothing of ours to replace
    path.write_text("\n".join(json.dumps(r) for r in rows[:1] + [{"type": "response_item", "payload": msg},
                                                                  {"type": "response_item", "payload": alone}]
                              + rows[1:3] + [compacted] + rows[3:]) + "\n")
    before = path.read_bytes()
    own = {"CODEX_HOME": str(tmp_path / "run-home")}
    assert codex.fork_session(sid, "/run", own, new) == ""
    worker = "# WORKER RULES\nuse your tools"
    assert codex.own_instructions(new, "/run", own, worker) == ""
    [copy] = list((tmp_path / "run-home" / "sessions").glob(f"*/*/*/rollout-*-{new}.jsonl"))
    text = copy.read_text()
    assert "no tools" not in text and "You coordinate" not in text and text.count("WORKER RULES") == 3
    want = [worker, memory, "<skills_instructions>skills</skills_instructions>"]
    got = []

    def walk(node):
        if isinstance(node, dict) and node.get("role") == "developer":
            got.append([c["text"] for c in node["content"]])
        elif isinstance(node, (list, dict)):
            for x in (node if isinstance(node, list) else node.values()):
                walk(x)
    for line in text.splitlines():
        walk(json.loads(line))
    assert got == [want, ["  " + memory], want, want], got   # top level, alone, replacement, guardian
    assert path.read_bytes() == before   # the coordinator's session is never written
    assert codex.own_instructions(new, "/run", own, worker) == "" and copy.read_text() == text   # twice is the same


def test_codex_worker_runs_start_with_their_own_instructions_and_the_coordinator_keeps_its(env, monkeypatch, tmp_path):
    """Task #198: a Codex worker forked from a Codex coordinator, a follow-up resuming that worker's
    session and the worker's own continued session all run under the worker's system prompt with none
    of the coordinator-only text; a Codex coordinator run keeps its own."""
    from ttp import coordsession
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _fork_fake_codex(tmp_path, monkeypatch)
    p, d, _cfg, _run = _fork_daemon(env, monkeypatch, tmp_path, provider="codex")
    csid = str(uuid.uuid4())
    coord_src = _codex_rollout_with_instructions(home, csid, str(p.base))
    p.db.set_kv(coordsession.KEY, {"id": csid, "provider": "codex", "context_tokens": 7000})
    worker = "# WORKER RULES\nuse your tools and write result.json"

    def start(tid, **kw):
        rid = d.start_run("worker", "TASK PROMPT", "codex", "standard", str(p.root), task=p.db.task(tid),
                          append_system=worker, **kw)
        argv = json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]
        got = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}").get("coord_context")
        [path] = [f for f in tmp_path.rglob(f"rollout-*-{got['session']}.jsonl")]
        return argv, got, path.read_text()

    tid = p.db.add_task("the change", "do it", kind="code", origin="user")
    argv, got, text = start(tid)
    assert got["how"] == "fork" and argv[argv.index("resume") + 1] == got["session"], (got, argv)
    assert "no tools" not in text and "WORKER RULES" in text and "<multi_agent_role>" in text
    assert "This session has no tools" in coord_src.read_text()   # the coordinator's session is untouched
    _argv, again, text = start(tid, resume=got["session"])   # its own session, continued
    assert again["how"] == "resume" and "no tools" not in text and "WORKER RULES" in text
    # A follow-up resumes a copy of an earlier run's session (whatever it held): it gets its own text too.
    _ok_run(p, tid, csid, provider="codex", ended=time.time() - 60)
    hop = p.db.add_task("continue: the change", "the rest", kind="code", origin="coordinator",
                        labels=[f"continues:{tid}"])
    _argv, follow, text = start(hop)
    assert follow["how"] == "resume" and "no tools" not in text and "WORKER RULES" in text, follow
    # A coordinator run keeps the coordinator's instructions, and no worker start rewrote its session.
    rid = d.start_run("coordinator", "decide", "codex", "standard", str(p.base), read_only=True,
                      append_system=_CODEX_COORD_TEXT)
    argv = json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]
    assert any(a.startswith("developer_instructions=") and "This session has no tools" in a for a in argv)
    assert coord_src.read_text().count("This session has no tools") == 3


def test_the_launch_role_check_knows_the_coordinators_own_rules_and_no_worker_prompt_has_one():
    """Task #7824: the model-free launch-role check matches the coordinator prompts' own tool and answer
    rules, so a template edit cannot silently disarm it; no worker, reviewer or other prompt carries one."""
    from ttp import ctxfork
    prompts = pathlib.Path(ctxfork.__file__).resolve().parents[2] / "template" / "prompts"
    session = (prompts / "coordinator-session.md").read_text()
    assert all(rule in session for rule in ctxfork.COORD_TOOL_RULES)
    assert ctxfork.COORD_ANSWER_RULE in (prompts / "coordinator.md").read_text()
    for path in prompts.glob("*.md"):
        if not path.name.startswith("coordinator"):
            assert ctxfork.coord_only([path.read_text()]) == "", path.name


def test_a_codex_worker_fork_that_keeps_the_coordinators_rules_starts_from_the_disk_block(env, monkeypatch, tmp_path):
    """Task #7824: two workers forked from a Codex coordinator session without tools handed off having run
    nothing, under its "This session has no tools". Where the copied rollout still carries a coordinator-only
    rule after the run's own instructions went in, the launch takes the disk block, checked model-free
    before the agent starts: no resume, the run's own developer instructions, the reason in its note."""
    from ttp import coordsession, ctxfork
    from ttp.providers import codex as cmod
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _fork_fake_codex(tmp_path, monkeypatch)
    p, d, _cfg, _run = _fork_daemon(env, monkeypatch, tmp_path, provider="codex")
    csid = str(uuid.uuid4())
    _codex_rollout_with_instructions(home, csid, str(p.base))
    p.db.set_kv(coordsession.KEY, {"id": csid, "provider": "codex", "context_tokens": 7000})
    worker = "# WORKER RULES\nuse your tools and write result.json"
    # A Codex whose rollout the swap does not reach (another layout, another nesting).
    monkeypatch.setattr(cmod.Codex, "own_instructions", lambda self, *a: "")
    tid = p.db.add_task("the change", "do it", kind="code", origin="user")
    rid = d.start_run("worker", "TASK PROMPT", "codex", "standard", str(p.root), task=p.db.task(tid),
                      append_system=worker)
    argv = json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]
    got = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}")["coord_context"]
    assert got["how"] == "disk" and "This session has no tools" in got["role_gap"] and "session" not in got, got
    assert "resume" not in argv and (p.runs / str(rid) / "prompt.md").read_text().startswith(ctxfork.DISK_HEAD)
    assert not ctxfork.coord_only(argv) and any("WORKER RULES" in a for a in argv), argv


def test_every_providers_worker_launch_from_the_coordinators_session_carries_the_worker_role(env, monkeypatch, tmp_path):
    """Task #7824, on each provider path the harness ships: a worker started while the coordinator has a
    session gets either a copy it can run its own role in or the disk block, and the launch-role check
    refuses a copy whose instructions carry a coordinator-only rule on every provider."""
    from ttp import coordsession, ctxfork
    from ttp.providers import all_providers
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    names = sorted({prov.name for prov in all_providers()})
    assert {"claude", "codex", "cursor", "chat"} <= set(names), names
    for name in names:
        prov = next(x for x in all_providers() if x.name == name)
        p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 5000})
        got = ctxfork.prepare(p, d.cfg, prov, name, str(p.root), None)
        assert got["how"] in ("fork", "disk"), (name, got)
        assert not ctxfork.coord_only([got.get("lead", "")]), name
        fake = {"how": "fork", "session": str(uuid.uuid4())}
        assert ctxfork.own_role(prov, fake, str(p.root), None, "# WORKER RULES") == "", name
        assert "This session has no tools" in ctxfork.own_role(prov, fake, str(p.root), None,
                                                              "# WORKER RULES\nThis session has no tools."), name
        prov.session_instructions = lambda *a: [_CODEX_COORD_TEXT]   # a session keeping the coordinator's
        assert "no tools" in ctxfork.own_role(prov, fake, str(p.root), None, "# WORKER RULES"), name


def test_a_run_that_did_no_work_under_the_coordinators_role_is_not_retried_the_same_way(env, monkeypatch, tmp_path):
    """Task #7824: a worker that handed off having run nothing because it got the coordinator's role
    (no tools, its {"actions"} answer) is marked; a copied session's task restarts from the disk block, and
    a run with no copy, which would start the same way again, is not retried or topped up."""
    from ttp import coordsession, ctxfork
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p, d, cfg_dir, _run = _fork_daemon(env, monkeypatch, tmp_path)
    sid = str(uuid.uuid4())
    _fork_transcript(cfg_dir, p.base, sid)
    p.db.set_kv(coordsession.KEY, {"id": sid, "provider": "claude", "context_tokens": 5000})
    said = "Nothing was run: this session has no tools under the coordinator-only instructions."
    assert ctxfork.role_miss("", {"status": "failed", "summary": said})
    assert ctxfork.role_miss('{"actions": []}', None) and not ctxfork.role_miss('{"actions": []}', {"status": "done"})
    assert not ctxfork.role_miss("", {"status": "failed", "summary": "the build failed: no tools package on the box"})

    def finish(note, handoff=None, final=""):
        tid = p.db.add_task(f"t {uuid.uuid4().hex[:6]}", "do it", kind="code", origin="user")
        p.db.update_task(tid, status="running")
        run_dir = tmp_path / f"run-{tid}"
        run_dir.mkdir()
        if handoff:
            (run_dir / "result.json").write_text(json.dumps(handoff))
        rid = p.db.x("INSERT INTO runs(task, role, provider, started, ended, status, note) VALUES(?,?,?,?,?,?,?)",
                     (tid, "worker", "claude", time.time() - 60, time.time(), "ok",
                      json.dumps({**note, "role_miss": "x"})))
        Daemon(p.base)._finish_worker({"id": rid, "task": tid, "role": "worker", "note": json.dumps(note)},
                                      RunUsage(cost_usd=0.5, final_text=final), "ok", run_dir)
        return tid, p.db.task(tid)

    # No copied session: the same start again would repeat it, so the task fails with the reason.
    for handoff, final in (({"status": "failed", "summary": said}, ""), (None, '{"actions": []}')):
        tid, t = finish({ctxfork.KEY: {"how": "disk"}}, handoff, final)
        assert t["status"] == "failed" and "not retried" in t["result"], (t["status"], t["result"])
    # A fork: retried, and the next start takes the disk block, not the same fork.
    tid, t = finish({ctxfork.KEY: {"how": "fork", "session": "s"}}, {"status": "failed", "summary": said})
    assert t["status"] == "queued", t["status"]
    from ttp.providers import get_provider
    got = ctxfork.prepare(p, d.cfg, get_provider("claude"), "claude", str(p.root), None, task=tid)
    assert got["how"] == "disk" and "coordinator's role" in got["why"], got
