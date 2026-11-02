# Upstream-notes tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new upstream tests here, not there.
from __future__ import annotations


def _fwd_state(target):
    from ttp import upstream
    return json.loads(upstream.forward_path().read_text())["targets"][target]


@pytest.mark.parametrize("darwin", [True, False])
def test_repeated_network_unreachable_forward_failures_read_as_a_macos_local_network_block(env, monkeypatch, darwin):
    """The daemon's forward records how often and since when it failed; on macOS, LOCAL_NET_FAILS
    'Network is unreachable' failures in a row are a suspected Local Network block, and `ttp doctor`
    names the fix with the daemon's python. Another error, or a send that goes through, ends it."""
    from ttp import upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    real = upstream._ssh_receive
    monkeypatch.setattr(upstream, "_darwin", lambda: darwin)
    upstream.set_forward_to(["example-host"])
    upstream.append("demo", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    fake["mode"].write_text("down")
    now = time.time()
    for i in range(upstream.LOCAL_NET_FAILS):
        assert upstream.forward(now + i * 7200) == 0
        st = _fwd_state("example-host")
        assert (st["fails"], st["fail_since"], st["unreachable"]) == (i + 1, now, i + 1)
        assert ("suspect" in st) == (darwin and i + 1 >= upstream.LOCAL_NET_FAILS)
    later = now + upstream.LOCAL_NET_FAILS * 7200
    lines = upstream.doctor_lines("UTC", later)
    assert len(lines) == 1 and f"failed {upstream.LOCAL_NET_FAILS}x since" in lines[0]
    py = os.path.realpath(sys.executable)
    assert (("Local Network" in lines[0]) and py in lines[0]) == darwin, lines
    if darwin:
        assert _fwd_state("example-host")["python"] == py
        assert "Local Network" in "\n".join(upstream.forward_status(later))
    # A different error ends the run of unreachable answers; the streak and its start stay.
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (None, "exit 255: Permission denied"))
    assert upstream.forward(later) == 0
    st = _fwd_state("example-host")
    assert (st["unreachable"], "suspect" in st, st["fail_since"]) == (0, False, now)
    assert "Local Network" not in "\n".join(upstream.doctor_lines("UTC", later))
    # A send that goes through clears the failure record.
    monkeypatch.setattr(upstream, "_ssh_receive", real)
    fake["mode"].write_text("ok")
    assert upstream.forward(later + 7200) == 1
    st = _fwd_state("example-host")
    assert not st["fails"] and not {"fail_since", "unreachable", "suspect", "python"} & set(st)
    assert upstream.doctor_lines("UTC", later + 7200) == []


def test_the_run_fallback_sends_a_failing_targets_notes_once_and_leaves_the_daemons_record(env, monkeypatch):
    """Past FALLBACK_AFTER_S of daemon failures, `ttp upstream --forward-now` (a run or the CLI) sends
    the pending notes through the same cursor: a lost ack, a resend and the daemon's later pass file
    each note once, and targets the daemon reaches are left to it."""
    from ttp import cli, upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    monkeypatch.setattr(upstream, "_darwin", lambda: True)
    upstream.set_forward_to(["example-host", "other-host"])
    fake["mode"].write_text("ok")
    now = time.time() - 3 * 3600                            # the daemon began failing hours ago
    assert upstream.forward(now) == 0                       # both reachable, nothing yet
    upstream.append("demo", 3, [{"title": "upstream: one", "spec": "a"}, {"title": "upstream: two", "spec": "b"}])
    # Only example-host fails for the daemon.
    real = upstream._ssh_receive
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (
        (None, "exit 255: ssh: connect to host x port 22: Network is unreachable") if t == "example-host"
        else real(t, lines, timeout)))
    assert upstream.forward(now + 60) == 2                  # other-host got both
    for later in (now + 200, now + 500):
        assert upstream.forward(later) == 0
    assert not upstream.fallback_due(now + 60 + upstream.FALLBACK_AFTER_S - 1)
    assert upstream.fallback_due(now + 60 + upstream.FALLBACK_AFTER_S)
    monkeypatch.setattr(upstream, "_ssh_receive", real)
    t = now + 60 + upstream.FALLBACK_AFTER_S
    calls = len(_ssh_calls(fake))
    # The run's send loses its ack: the cursor stays and its own back-off starts, the daemon's record stays.
    fake["mode"].write_text("drop-ack")
    assert upstream.forward(t, context="run") == 0
    assert _ssh_calls(fake)[calls:] == ["example-host"], "the fallback only tries the failing target"
    st = _fwd_state("example-host")
    assert st["run_fails"] == 1 and st["fails"] == 3 and st["suspect"] == "local_network" and not st.get("cursor")
    assert not upstream.fallback_due(t + 1), "the fallback waits out its own back-off"
    fake["mode"].write_text("ok")
    cli.main(["upstream", "--forward-now"])                 # the fallback's command, at the real time
    assert sorted(n["title"] for n in _remote_inbox(fake, "example-host")) == ["upstream: one", "upstream: two"]
    st = _fwd_state("example-host")
    assert st["cursor"] == upstream.path().stat().st_size and st["run_ok"] > t and "run_fails" not in st
    assert st["fails"] == 3 and st["suspect"] == "local_network", "the daemon's failure record is its own"
    assert not upstream.fallback_due(), "nothing is pending"
    # The daemon reaches it again: nothing is sent twice, and its failure record clears.
    assert upstream.forward(time.time() + 10) == 0
    titles = [n["title"] for n in _remote_inbox(fake, "example-host")]
    assert sorted(titles) == ["upstream: one", "upstream: two"]
    assert [n["title"] for n in _remote_inbox(fake, "other-host")].count("upstream: one") == 1
    assert not _fwd_state("example-host")["fails"]


def test_note_to_and_status_warn_when_the_forward_target_has_failed_for_hours(env, monkeypatch, capsys):
    """`ttp note --to` a project on a machine the daemon has failed to reach for WARN_AFTER_S says so
    and starts the fallback; `ttp status` shows the same line. A fresh failure says nothing yet."""
    from ttp import cli, upstream
    from ttp.project import register
    p = make(env)
    _note_run(env, monkeypatch, p)
    register("example-project", {"host": "example-host", "dir": "/w/example"})
    spawned = []
    monkeypatch.setattr(upstream, "spawn_fallback", lambda: spawned.append(1) or True)
    now = time.time()
    st = {"fails": 4, "fail_since": now - 60, "error_at": now - 60, "next": now + 600, "cursor": 0,
          "last_error": "exit 255: Network is unreachable", "unreachable": 4, "suspect": "local_network",
          "python": "/opt/example/bin/python3"}
    upstream.project.write_json(upstream.forward_path(), {"targets": {"example-host": st}})
    assert _note(cli, "--to", "example-project", "first note") == 0
    out = capsys.readouterr().out
    assert "warning" not in out and spawned == []
    assert "sending upstream notes on" not in cli.status_text(p)
    st["fail_since"] = now - 3 * 3600
    upstream.project.write_json(upstream.forward_path(), {"targets": {"example-host": st}})
    assert _note(cli, "--to", "example-project", "second note") == 0
    out = capsys.readouterr().out
    assert "warning: sending upstream notes on to example-host has failed for 3.0 h (4x" in out, out
    assert "Local Network" in out and "keep working" in out
    assert spawned == [1], "the run sends the notes the daemon cannot"
    assert "sending upstream notes on to example-host has failed for 3.0 h" in cli.status_text(p)
    # A target no longer among the forward targets is not reported.
    upstream.set_forward_to(["other-host"])
    assert upstream.forward_line() == "" and upstream.failing() == {}


@pytest.mark.parametrize("fup,kept", [
    ({"title": "upstream: the worker prompt could name the lock command", "spec": "workers guess it"}, True),
    ({"title": "upstream: retries lose the spec", "spec": "the daemon drops it on a retry"}, True),
    ({"title": "upstream: ttp status hides waits", "spec": "show them"}, True),
    ({"title": "upstream: the build box needs a newer Node", "spec": "install it before the next run"}, False),
    ({"title": "upstream: model output drifts in bf16", "spec": "compare against the fp32 reference"}, False),
    ({"title": "upstream:", "spec": "the test machine lacks write access to the cache folder"}, False),
])
def test_an_upstream_follow_up_naming_nothing_of_tt_project_stays_with_its_project(fup, kept):
    from ttp import upstream
    (out,), moved = upstream.reroute([fup])
    assert upstream.is_note(out) == kept and bool(moved) != kept
    if not kept:
        assert out["title"] == moved[0] and out["title"] and out["spec"] == fup["spec"]
    other = {"title": "plain follow-up", "spec": "s"}
    assert upstream.reroute([other]) == ([other], [])


def test_the_daemon_files_a_project_local_upstream_follow_up_as_its_own(env):
    """Only the note about tt-project reaches the user's inbox; the other is proposed to this
    project's coordinator without the prefix, with a log line and a note saying why."""
    p = make(env)
    tid, _, ids = _hand_off(env, p, {"status": "done", "summary": "ok", "followups": [
        {"title": "upstream: the coordinator re-reads the whole digest", "spec": "cache it"},
        {"title": "upstream: the build box needs a newer Node", "spec": "install it"}]})
    assert [n["title"] for n in _upstream_inbox(env)] == ["upstream: the coordinator re-reads the whole digest"]
    texts = [e["text"] for e in p.db.q("SELECT text FROM events WHERE task=?", (tid,))]
    assert any(t.startswith("proposed follow-up: the build box needs a newer Node") for t in texts), texts
    assert any("Not an upstream note" in t and "the build box needs a newer Node" in t for t in texts), texts
    assert "names nothing of tt-project; kept as this project's follow-up" in (p.logs / "daemon.log").read_text()


def test_the_worker_prompt_and_harness_skill_keep_project_work_out_of_upstream_notes():
    plugin = RUNTIME.parent
    worker = " ".join((plugin / "template" / "prompts" / "worker.md").read_text().split())
    assert "`upstream: ...` follow-ups are only for tt-project faults" in worker
    assert "your own code, boxes, models or scripts get plain follow-ups" in worker
    skill = " ".join((plugin / "skills" / "tt-project-harness" / "SKILL.md").read_text().split())
    assert "only for a defect or missing feature in the tt-project plugin itself" in skill
    assert "names nothing of tt-project" in skill


def test_a_rerouted_upstream_follow_up_is_never_a_failed_reviews_fix(env):
    """A failed review's `upstream:` note that names nothing of tt-project loses its prefix, but stays
    out of the daemon's automatic fix: it reaches the coordinator as a queued proposal."""
    p = make(env)
    _, _, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40})
    _fail_review(env, p, rev["id"], [
        {"title": "add the missing test", "spec": "test_x must fail without the change"},
        {"title": "upstream: the build box needs a newer Node", "spec": "install it before the next run"}])
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    assert "test_x must fail" in fix["spec"] and "newer Node" not in fix["spec"]
    assert "install it before the next run" not in fix["spec"]
    ev = {e["text"].split(" — ")[0]: e["status"] for e in
          p.db.q("SELECT text, status FROM events WHERE task=? AND kind='followup_proposed'", (rev["id"],))}
    assert ev == {"proposed follow-up: add the missing test": "handled",
                  "proposed follow-up: the build box needs a newer Node": "queued"}


def test_a_note_to_another_project_carries_the_named_tasks_records_at_send_time(env, monkeypatch, capsys):
    """`ttp note --to` appends the live record of each task the note names (status, the commit recorded
    for its landing, its machine changes) so the receiver does not act on a stale claim. The facts fit
    within the note limit: they are dropped, never the note refused, when they do not."""
    from ttp import cli, machine_ledger as ml, machines, upstream
    machines.add("box-a")
    from ttp.cli import bootstrap
    from ttp.project import register
    src = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    dst = bootstrap(repo2, "second", "Another project.", "fake")
    register("second", {"host": "testhost", "dir": str(dst.root)})
    done = src.db.add_task("switch the service version", "s", kind="code", status="done")
    now = time.time()
    src.db.x("INSERT INTO task_landings(marker, task, sha, target) VALUES('m1', ?, ?, 'main')", (done, "d" * 40))
    ml.add_change("box-a", "switched the service", "switch back", src.name, expires="2d", task=done)
    _note_run(env, monkeypatch, src)
    assert _note(cli, "--to", "second", f"#{done} is not live yet; please wait") == 0
    out = capsys.readouterr().out
    (n,) = _upstream_inbox(env)
    assert n["spec"].startswith(f"#{done} is not live yet; please wait [facts at send time from {src.name}'s records: "
                                f"#{done} switch the service version (code, done); on the push branch as dddddddddd "
                                "(its detached push landing); machine change #1 on box-a (open): switched the service]"), n
    assert f"stored {upstream.receipt(n['spec'])}" in out
    # A note at the limit goes as written.
    long = f"#{done} " + "x" * (upstream.SPEC_CHARS - len(str(done)) - 2)
    assert _note(cli, "--to", "second", long) == 0
    assert any(x["spec"] == long for x in _upstream_inbox(env))


def test_note_status_follows_a_remote_note_from_filed_local_to_forwarded_to_delivered(env, monkeypatch, capsys):
    """`ttp note --to` a project on another machine prints the id `ttp note --status` takes; that says
    filed-local until the forwarder sends it, then asks the far machine (`ttp upstream --note-state`
    over ssh): forwarded while the project there has not read it, delivered (exit 0) once it has."""
    from ttp import cli, upstream
    from ttp.db import DB
    from ttp.project import register
    p = make(env)
    _note_run(env, monkeypatch, p)
    fake = _fake_forward_ssh(env, monkeypatch)
    register("far", {"host": "farbox", "dir": "/w/far"})
    assert _note(cli, "--to", "far", "please rerun the sweep") == 0
    (n,) = _upstream_inbox(env)
    assert f"`ttp note --status {n['fp']}` shows whether far read it" in capsys.readouterr().out
    assert _note(cli, "--status", n["fp"]) == 1
    assert capsys.readouterr().out.startswith("filed-local: ") and not fake["log"].exists(), "no ssh before the send"
    assert upstream.forward(time.time()) == 1
    # The far machine holds it but runs no project of that name.
    assert _note(cli, "--status", n["fp"]) == 2
    assert "is in farbox's inbox, but no project far runs there" in capsys.readouterr().out
    far_dir = env["tmp"] / "remote" / "farbox" / "w-far"
    (far_dir / "tt-project" / "state").mkdir(parents=True)
    (env["tmp"] / "remote" / "farbox" / ".tt-project" / "registry.json").write_text(
        json.dumps({"projects": {"far": {"dir": str(far_dir)}}}))
    far_db = DB(far_dir / "tt-project" / "state" / "project.db")
    assert _note(cli, "--status", n["fp"]) == 1
    assert capsys.readouterr().out.startswith(f"forwarded: note {n['fp']} is in farbox's inbox; far's daemon")
    far_db.set_kv(upstream.KV_CURSOR, {"offsets": {}, "seen": ["0" * 16, n["fp"]]})
    # Outside a run too: a probe a waiting hand-off runs from the project root.
    monkeypatch.delenv("TTP_RUN_DIR")
    assert _note(cli, "--status", n["fp"].upper()) == 0
    assert capsys.readouterr().out.startswith("delivered: far's daemon on farbox read note")
    # The far machine cannot be asked: still forwarded, never delivered or unknown.
    fake["mode"].write_text("down")
    assert _note(cli, "--status", n["fp"]) == 1
    assert "forwarded: note" in (out := capsys.readouterr().out) and "could not ask whether far read it" in out
    # An id no note has, or not an id at all.
    assert _note(cli, "--status", "0123456789abcdef") == 2
    assert capsys.readouterr().out.startswith("unknown: no note 0123456789abcdef")
    assert _note(cli, "--status", "nope") == 2
    assert _note(cli, "--status", n["fp"], "some text") == 2


def test_note_status_of_a_local_or_queued_note(env, monkeypatch, capsys):
    """A note for a project on this machine is delivered once its daemon read the inbox; one past the
    hourly limit, or one a sandbox could not file, is queued; `--note-state` answers for this machine."""
    from ttp import cli, upstream
    from ttp.cli import bootstrap
    from ttp.daemon import Daemon
    from ttp.project import register
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    dst = bootstrap(repo2, "second", "Another project.", "fake")
    register("second", {"host": "testhost", "dir": str(dst.root)})
    run_dir = _note_run(env, monkeypatch, p)
    assert _note(cli, "--to", "second", "the cache is stale") == 0
    (n,) = _upstream_inbox(env)
    capsys.readouterr()
    assert _note(cli, "--status", n["fp"]) == 1
    assert "filed-local: " in (out := capsys.readouterr().out) and "second's daemon has not read it" in out
    assert upstream.note_state(n["fp"]) == {"fp": n["fp"], "inbox": True, "to": "second", "read": False}
    Daemon(dst.base).read_upstream()
    assert _note(cli, "--status", n["fp"]) == 0
    assert capsys.readouterr().out.startswith("delivered: second's daemon read note")
    assert _upstream_cli(cli, "--note-state", n["fp"]) == 0
    assert json.loads(capsys.readouterr().out)["read"] is True
    # Queued: in this run's directory (a read-only sandbox) and in the project's outbox (the hourly limit).
    upstream.queue(run_dir, "second", "from a sandbox")
    assert _note(cli, "--status", upstream.note_id("second", "from a sandbox")) == 1
    assert "waits in this run's directory" in capsys.readouterr().out
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(p.name, 5, "second", f"filler {i}", now=now)
    assert upstream.send_or_queue(p.state, p.name, 5, "second", "past the limit", now=now)[0] == "queued"
    assert _note(cli, "--status", upstream.note_id("second", "past the limit")) == 1
    assert "waits in this project's outbox" in capsys.readouterr().out
    # An upstream note for the maintainers is not addressed: --mine follows it.
    upstream.append("demo", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    assert _note(cli, "--status", upstream.fingerprint("upstream: a lesson", "s")) == 2
    assert "ttp upstream --mine" in capsys.readouterr().out


def test_forward_status_labels_a_retired_target_and_forward_prunes_its_row(env, monkeypatch):
    """A row outside the current target set reads 'no longer a target' (never failing, never counted),
    a failing current target still reads failing, and the next forward save drops the retired row."""
    from ttp import upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    now = time.time()
    bad = {"fails": 5, "fail_since": now - 5 * 3600, "error_at": now - 3600, "next": now + 600,
           "tried": now - 3600, "last_error": "exit 255: down"}
    upstream.project.write_json(upstream.forward_path(), {"targets": {"gone-host": dict(bad), "live-host": dict(bad)}})
    upstream.set_forward_to(["live-host"])
    lines = upstream.forward_status(now)
    gone = [ln for ln in lines if ln.startswith("gone-host")]
    assert len(gone) == 1 and "no longer a target" in gone[0] and "failing" not in gone[0]
    assert any(ln.startswith("live-host") and "failing (5x" in ln for ln in lines)
    assert list(upstream.failing(now)) == ["live-host"]
    assert "gone-host" not in upstream.forward_line(now)
    upstream.append("demo", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    upstream.forward(now + 3 * 3600)
    assert set(json.loads(upstream.forward_path().read_text())["targets"]) == {"live-host"}


def test_upstream_fixed_prints_the_note_and_what_it_read(env, monkeypatch, capsys):
    """`ttp upstream --fixed <fp>` used to print nothing: its exit code was all a reader got. It now
    prints the note (its title, or that it is not in this inbox), its state and the running version
    the answer rests on; a malformed fingerprint fails with a usage error."""
    from ttp import cli, upstream
    p = make(env)
    note = {"title": "upstream: retries lose the spec", "spec": "Keep the spec on retry."}
    upstream.append("demo", 4, [note])
    fp = upstream.fingerprint(note["title"], note["spec"])
    monkeypatch.chdir(p.base)
    _runs_version(p, "0.2.9")
    capsys.readouterr()

    def fixed(arg):
        rc = _upstream_cli(cli, "--fixed", arg)
        out = capsys.readouterr()
        return rc, out.out.strip(), out.err

    rc, out, _ = fixed(fp)
    assert rc == 1 and out.startswith(f"note {fp} (upstream: retries lose the spec): "), out
    assert "no fix recorded yet" in out and "daemon runs 0.2.9: waiting for a fix" in out, out
    upstream.resolve(fp, "0.2.10", "", "")
    rc, out, _ = fixed(fp)
    assert rc == 1 and "fixed in 0.2.10" in out and "runs 0.2.9: not yet" in out, out
    _runs_version(p, "0.2.10")
    rc, out, _ = fixed(fp)
    assert rc == 0 and "fixed in 0.2.10" in out and "runs 0.2.10: it has the fix" in out, out
    other = upstream.fingerprint("upstream: elsewhere", "s")
    upstream.wontfix(other, "works as designed")
    rc, out, _ = fixed(other)
    assert rc == upstream.WONTFIX_RC and out.startswith(f"note {other} (not in this machine's inbox): wontfix"), out
    assert "works as designed" in out, out
    p.db.set_kv("daemon", {})
    rc, out, _ = fixed(fp)
    assert rc == 2 and "running daemon version is unknown" in out, out
    rc, out, err = fixed("nothex")
    assert rc == 2 and out == "" and "not a note fingerprint" in err, (out, err)


def test_upstream_fixed_in_a_sandbox_reads_the_running_version_ttp_status_reads(env, monkeypatch):
    """In a worker's sandbox (its own pid namespace) the daemon's pid probe fails, so `ttp upstream
    --fixed` said "running daemon version is unknown" while `ttp status` showed it running. It now reads
    the same answer `ttp status` gives: a fresh heartbeat naming the recorded daemon stands in. A stale
    heartbeat, one naming another pid, or no daemon record still reads unknown."""
    from ttp import identity, upstream
    p = make(env)
    gone = subprocess.Popen(["true"])
    gone.wait()                                  # a pid no process holds: what the sandbox sees
    monkeypatch.setenv("TTP_RUN_DIR", str(p.state / "runs" / "1"))
    hb = p.state / "heartbeat"

    def daemon(pid, version="0.2.9", hb_pid=None, age=0):
        p.db.set_kv("daemon", {"pid": pid, "host": "testhost", "started": time.time()})
        hb.write_text(json.dumps({"pid": hb_pid or pid, "receipt": {"pid": hb_pid or pid, "version": version}}))
        t = time.time() - age
        os.utime(hb, (t, t))

    daemon(gone.pid)
    assert identity.probe(p.state, p.db.kv("daemon", {}), "testhost")["state"] == "running"
    assert upstream.running_version(p) == "0.2.9"
    fp = upstream.fingerprint("upstream: x", "s")
    upstream.resolve(fp, "0.2.9", "", "")
    rc, line = upstream.fixed_status(p, fp)
    assert rc == 0 and "daemon runs 0.2.9: it has the fix" in line, line
    daemon(gone.pid, age=identity.HEARTBEAT_FRESH_S + 60)
    assert upstream.running_version(p) is None
    daemon(gone.pid, hb_pid=gone.pid + 1)
    assert upstream.running_version(p) is None
    p.db.set_kv("daemon", {})
    assert upstream.running_version(p) is None
    assert upstream.fixed_status(p, fp)[0] == 2
    monkeypatch.delenv("TTP_RUN_DIR")            # outside a sandbox the pid probe decides
    daemon(os.getpid(), age=identity.HEARTBEAT_FRESH_S + 60)
    assert upstream.running_version(p) == "0.2.9"


def _raw_inbox_line(env, rec):
    with open(env["home"] / "upstream.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_ttp_upstream_lists_each_inbox_note_with_its_sender_open_first_and_bounded(env, monkeypatch, capsys):
    """Bare `ttp upstream` used to print only the forward status, so finding who filed a note meant
    reading the inbox file by hand. It now lists each maintainers' note with its fingerprint, status,
    sending project, host and task, open notes first, newest first, within INBOX_LIST_MAX, then the
    forward status. Notes addressed to a project are not listed; an older record lacking the sender
    fields reads `unknown`."""
    from ttp import cli, upstream
    t0 = time.time()
    for i, who in enumerate(["alpha", "beta", "gamma", "delta"]):
        monkeypatch.setattr(upstream.time, "time", lambda i=i: t0 + i)
        upstream.append(who, 10 + i, [{"title": f"upstream: lesson {i}", "spec": f"s{i}"}])
    monkeypatch.undo()
    fps = [upstream.fingerprint(f"upstream: lesson {i}", f"s{i}") for i in range(4)]
    upstream.resolve(fps[3], "0.3.1")
    upstream.wontfix(fps[2], "the project's own script")
    old = "0123456789abcdef"
    _raw_inbox_line(env, {"ts": t0 - 50, "title": "upstream: an old note", "spec": "x", "fp": old})
    upstream.send("alpha", 3, "beta", "an addressed note, not for the maintainers")
    assert _upstream_cli(cli) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "upstream notes in this machine's inbox: 3 open of 5"
    assert lines[1:4] == [f"{fps[1]} open from beta on testhost task #11: upstream: lesson 1",
                          f"{fps[0]} open from alpha on testhost task #10: upstream: lesson 0",
                          f"{old} open from unknown on unknown task unknown: upstream: an old note"]
    assert lines[4:6] == [f"{fps[3]} fixed in 0.3.1 from delta on testhost task #13: upstream: lesson 3",
                          f"{fps[2]} wontfix from gamma on testhost task #12: upstream: lesson 2"]
    assert "addressed" not in "\n".join(lines) and lines[6:] == upstream.forward_status()
    # Bounded: the newest open notes first, then a count of the rest.
    monkeypatch.setattr(upstream, "INBOX_LIST_MAX", 2)
    assert _upstream_cli(cli) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith(fps[1]) and lines[2].startswith(fps[0]) and lines[3] == "… 3 more not shown"
    # --forward-status keeps printing the forward status alone.
    assert _upstream_cli(cli, "--forward-status") == 0
    assert capsys.readouterr().out.splitlines() == upstream.forward_status()


def test_upstream_resolve_prints_the_sender_and_answers_a_local_sending_project_once(env, monkeypatch, capsys):
    """`ttp upstream --resolve` prints who sent the note. A sender that is another project registered on
    this machine gets the answer (fixed in X, or won't fix and why) once, as an addressed note; resolving
    again sends nothing new. A sender on another machine, the resolving project itself, an unregistered
    or unknown sender, or a note not in this inbox: it says no answer was sent and why, and the resolve
    still succeeds."""
    from ttp import cli, upstream
    from ttp.cli import bootstrap
    from ttp.project import register
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    register("second", {"host": "testhost", "dir": str(bootstrap(repo2, "second", "Another project.", "fake").root)})
    monkeypatch.chdir(p.base)
    notes = {k: {"title": f"upstream: {k} note", "spec": "s"} for k in ("fix", "nofix", "far", "self", "ghost")}
    fp = {k: upstream.fingerprint(n["title"], n["spec"]) for k, n in notes.items()}
    upstream.append("second", 7, [notes["fix"], notes["nofix"]])
    upstream.append("demo", 8, [notes["self"]])
    upstream.append("ghost", 9, [notes["ghost"]])
    _raw_inbox_line(env, {"ts": time.time(), "project": "far", "host": "farbox", "task": 2, "via": "farbox",
                          "title": notes["far"]["title"], "spec": "s", "fp": fp["far"]})
    old = "0123456789abcdef"
    _raw_inbox_line(env, {"ts": time.time(), "title": "upstream: old", "spec": "s", "fp": old})

    def answers():
        return [n for n in _upstream_inbox(env) if n.get("to") == "second"]

    assert _upstream_cli(cli, "--resolve", fp["fix"], "--fixed-in", "0.3.2") == 0
    out = capsys.readouterr().out
    (a,) = answers()
    assert f"sender: second on testhost task #7; answer sent to second as {a['fp']}" in out
    assert a["project"] == "demo" and f"{fp['fix']}" in a["spec"] and "fixed in tt-project 0.3.2" in a["spec"]
    assert f"ttp upstream --ack {fp['fix']}" in a["spec"]
    # Idempotent: the same resolution again sends nothing new.
    assert _upstream_cli(cli, "--resolve", fp["fix"], "--fixed-in", "0.3.2") == 0
    assert f"answer already sent to second as {a['fp']}" in capsys.readouterr().out and len(answers()) == 1
    assert _upstream_cli(cli, "--resolve", fp["nofix"], "--wontfix", "the project's own check") == 0
    assert "answer sent to second" in capsys.readouterr().out
    assert len(answers()) == 2 and "won't fix" in answers()[1]["spec"] and "the project's own check" in answers()[1]["spec"]
    assert _upstream_cli(cli, "--resolve", fp["nofix"], "--wontfix", "the project's own check") == 0
    assert "answer already sent" in capsys.readouterr().out and len(answers()) == 2
    # No answer: each says why, and the resolve is still recorded.
    for key, why in [("far", "sender: far on farbox task #2; no answer sent: far runs on another machine (farbox)"),
                     ("self", "sender: demo on testhost task #8; no answer sent: the sender is this project"),
                     ("ghost", "no answer sent: no project ghost is registered on this machine")]:
        assert _upstream_cli(cli, "--resolve", fp[key], "--fixed-in", "0.3.2") == 0
        assert why in capsys.readouterr().out, key
        assert upstream.resolution(fp[key])["fixed_in"] == "0.3.2"
    assert _upstream_cli(cli, "--resolve", old, "--wontfix", "not ours") == 0
    assert ("sender: unknown on unknown task unknown; no answer sent: the note names no sending project"
            in capsys.readouterr().out) and upstream.wontfixed(old)
    assert _upstream_cli(cli, "--resolve", "fedcba9876543210", "--fixed-in", "0.3.2") == 0
    assert "sender: unknown (note fedcba9876543210 is not in this machine's inbox); no answer sent" in capsys.readouterr().out
    assert len(answers()) == 2 and not [n for n in _upstream_inbox(env) if n.get("to") not in (None, "second")]
    # A sandbox that cannot write the user's files: the resolve and the answer wait in the run's directory.
    run_dir = env["tmp"] / "run-sandboxed"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setattr(upstream, "_record", lambda rec: (_ for _ in ()).throw(PermissionError(13, "read-only")))
    monkeypatch.setattr(upstream, "send_or_queue", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "read-only")))
    upstream.append("second", 7, [{"title": "upstream: sandboxed", "spec": "s"}])
    assert _upstream_cli(cli, "--resolve", upstream.fingerprint("upstream: sandboxed", "s"), "--fixed-in", "0.3.3") == 0
    assert "answer queued in this run's directory to second" in capsys.readouterr().out
    (q,) = [json.loads(x) for x in (run_dir / upstream.QUEUED_FILE).read_text().splitlines()]
    assert q["to"] == "second" and "fixed in tt-project 0.3.3" in q["text"] and len(answers()) == 2


def _pushed_note(to, spec, sender="alpha", host="laptop", task=3, ts=None):
    """A note project `sender` on machine `host` addressed to `to`, as a --receive line."""
    from ttp import upstream
    return {"ts": ts or time.time(), "project": sender, "host": host, "task": task, "to": to, "severity": "normal",
            "title": f"note to {to}", "spec": spec, "fp": upstream.fingerprint(f"to:{to}", spec)}


def test_a_note_pushed_from_an_unreachable_machine_keeps_its_route_and_the_reply_goes_back_on_its_next_call(
        env, monkeypatch, capsys):
    """A note another machine pushed here (`--receive`, `via` its alias) from a project `ttp list` does not
    show: its route is kept in the reading project's database, its event says how to answer, `--to <sender>`
    names `--reply`, and `ttp note --reply <fp>` files the answer as a `back` note the forwarder never sends:
    the sending machine's next call here takes it, and `--status` follows it to sent back."""
    from ttp import cli, upstream
    p = make(env)
    _note_run(env, monkeypatch, p)
    sent = _pushed_note(p.name, "can you share the sweep logs?")
    ack, rc = upstream.receive((json.dumps(sent) + "\n").encode(), "laptop")
    assert (rc, ack["accepted"], ack["replies"]) == (0, 1, [])
    assert upstream.ingest(p, {}) == 1
    route = p.db.kv(upstream.KV_ROUTES)[sent["fp"]]
    assert (route["project"], route["host"], route["via"], route["src"], route["task"]) == (
        "alpha", "laptop", "laptop", upstream.LOCAL_TO, 3)
    (ev,) = p.db.q("SELECT text FROM events WHERE kind='upstream_note'")
    assert ev["text"].endswith(f"[note {sent['fp']}] Reply: `ttp note --reply {sent['fp']} \"<text>\"`.")
    # The sender is unknown here: the error says how to answer instead of only 'unknown project'.
    assert _note(cli, "--to", "alpha", "logs attached") == 2
    err = capsys.readouterr().err
    assert "unknown project alpha" in err and f"`ttp note --reply {sent['fp']} \"<text>\"`" in err
    assert "`--to alpha@laptop`" in err
    assert len(_upstream_inbox(env)) == 1, "nothing filed"
    assert _note(cli, "--reply", sent["fp"], "logs are in the run folder") == 0
    out = capsys.readouterr().out
    reply = _upstream_inbox(env)[-1]
    assert (reply["to"], reply["back"], reply["project"], reply["task"]) == ("alpha", "laptop", p.name, 57)
    assert "at" not in reply and f"as {reply['fp']}" in out and "waits here until laptop's daemon" in out
    assert _note(cli, "--status", reply["fp"]) == 1
    assert capsys.readouterr().out.startswith(f"waiting: note {reply['fp']} is in this machine's inbox")
    # The forwarder never sends a `back` note, not even to a target that runs a project of that name.
    fake = _fake_forward_ssh(env, monkeypatch)
    from ttp.project import register
    register("alpha", {"host": "farbox", "dir": "/w/alpha"})
    upstream.forward(time.time())
    assert not any(n.get("fp") == reply["fp"] for n in _remote_inbox(fake, "farbox"))
    # Another machine's call does not get it; the sending machine's next call does, each time until it is old.
    assert upstream.receive(b"", "otherbox")[0]["replies"] == []
    (got,) = upstream.receive(b"", "laptop")[0]["replies"]
    assert upstream._valid(got)["fp"] == reply["fp"] and "back" not in got
    assert _note(cli, "--status", reply["fp"]) == 0
    assert capsys.readouterr().out.startswith(f"sent back: laptop's daemon took note {reply['fp']}")
    assert upstream.receive(b"", "laptop", now=time.time() + upstream.REPLY_BACK_S + 1)[0]["replies"] == []


def test_a_reply_by_note_id_goes_to_a_forward_target_and_a_target_hands_back_replies_it_cannot_send(
        env, monkeypatch, capsys):
    """`--to <project>@<machine>` on a forward target goes there (`at`) though no registry entry names the
    project. After addressed notes went to a target, the forwarder asks it for replies; the `back` replies
    it holds for this machine are filed here with `via` the target, and `--reply` answers them directly."""
    from ttp import cli, upstream
    p = make(env)
    _note_run(env, monkeypatch, p)
    fake = _fake_forward_ssh(env, monkeypatch)
    upstream.set_forward_to(["farbox"])
    assert _note(cli, "--to", "alpha@farbox", "is the sweep done?") == 0
    (n,) = _upstream_inbox(env)
    assert (n["to"], n["at"]) == ("alpha", "farbox") and "sends it on to farbox" in capsys.readouterr().out
    now = time.time()
    assert upstream.forward(now) == 1
    assert [x["fp"] for x in _remote_inbox(fake, "farbox")] == [n["fp"]]
    assert _fwd_state("farbox")["addressed"] == now
    # farbox holds a reply for this machine it cannot send (its project answered with --reply).
    back = {**_pushed_note(p.name, "yes, results are in", host="farbox"), "back": "testhost", "from": "worker"}
    with open(fake["remote"] / "farbox" / ".tt-project" / "upstream.jsonl", "a") as f:
        f.write(json.dumps(back) + "\n")
    assert upstream.forward(now + 60) == 0 and not any(x["fp"] == back["fp"] for x in _upstream_inbox(env)), \
        "asked again only after PULL_EVERY_S"
    upstream.forward(now + upstream.PULL_EVERY_S + 1)
    got = [x for x in _upstream_inbox(env) if x["fp"] == back["fp"]]
    assert len(got) == 1 and (got[0]["via"], got[0]["project"], got[0]["to"]) == ("farbox", "alpha", p.name)
    assert "back" not in got[0]
    upstream.forward(now + 2 * upstream.PULL_EVERY_S + 2)
    assert len([x for x in _upstream_inbox(env) if x["fp"] == back["fp"]]) == 1, "filed once"
    assert upstream.ingest(p, {}) == 1
    capsys.readouterr()
    assert _note(cli, "--reply", back["fp"], "thanks, closing") == 0
    r = _upstream_inbox(env)[-1]
    assert (r["to"], r["at"]) == ("alpha", "farbox") and "back" not in r


def test_a_reply_or_peer_note_with_no_route_is_refused_never_dropped(env, monkeypatch, capsys):
    """No route: a non-zero exit saying it was not sent, and nothing filed. An unknown id, a sender that
    no longer runs here, a note read from another machine's inbox that came there from a third one, and
    `<project>@<machine>` for a machine there is no route to."""
    from ttp import cli, upstream
    p = make(env)
    _note_run(env, monkeypatch, p)
    assert _note(cli, "--reply", "0123456789abcdef", "hi") == 2
    assert "not sent: no note 0123456789abcdef from another project came to demo" in capsys.readouterr().err
    assert _note(cli, "--reply", "nope", "hi") == 2
    assert "not sent: not a note fingerprint" in capsys.readouterr().err
    local = _pushed_note(p.name, "from a project that is gone", host="testhost")
    remote = _pushed_note(p.name, "read from a far inbox", host="laptop")
    p.db.set_kv(upstream.KV_ROUTES, {
        local["fp"]: upstream._route(local, upstream.LOCAL, time.time()),
        remote["fp"]: upstream._route({**remote, "via": "laptop"}, "farbox", time.time())})
    assert _note(cli, "--reply", local["fp"], "hi") == 2
    assert "not sent: project alpha wrote it on this machine but no longer runs here" in capsys.readouterr().err
    assert _note(cli, "--reply", remote["fp"], "hi") == 2
    assert "this machine has no route back to laptop" in capsys.readouterr().err
    assert _note(cli, "--to", "alpha@nowhere", "hi") == 2
    assert "not sent: this machine does not send notes to nowhere" in capsys.readouterr().err
    assert _note(cli, "--to", "alpha@testhost", "hi") == 2
    assert "no project alpha runs on this machine" in capsys.readouterr().err
    assert _note(cli, "--to", "alpha", "--reply", local["fp"], "hi") == 2
    assert not (env["home"] / "upstream.jsonl").exists(), "nothing was filed"
    # Read from a far inbox, written there: the reply goes there once that machine is a forward target.
    p.db.set_kv(upstream.KV_ROUTES, {remote["fp"]: upstream._route(remote, "farbox", time.time())})
    assert _note(cli, "--reply", remote["fp"], "hi") == 2
    assert "does not send notes there" in capsys.readouterr().err
    upstream.set_forward_to(["farbox"])
    assert _note(cli, "--reply", remote["fp"], "hi") == 0
    assert (_upstream_inbox(env)[-1]["to"], _upstream_inbox(env)[-1]["at"]) == ("alpha", "farbox")


def test_a_long_note_event_keeps_its_id_and_reply_hint(env):
    from ttp import upstream
    from ttp.coordinator import EVENT_CHARS_BY_KIND
    cap = EVENT_CHARS_BY_KIND["upstream_note"]
    n = _pushed_note("demo", "x" * upstream.SPEC_CHARS)
    sev, text = upstream._event({**n, "via": "laptop" * 10}, cap)
    assert len(text) <= cap and text.endswith(f"[note {n['fp']}] Reply: `ttp note --reply {n['fp']} \"<text>\"`.")


def test_upstream_resolve_fixed_in_refuses_a_version_without_the_fixing_tasks_landed_commit(env, monkeypatch, capsys):
    """`ttp upstream --resolve <fp> --fixed-in <V>`: when a task's hand-off lists the note in `resolves`
    (or `--task` names one), its landed commit must be in the first commit on the push branch whose
    version_bump file carries V, also as a rebased copy. A version before the fix, one no commit carries
    yet, a task that has not landed or does not exist is refused, naming the task, its commit and V, and
    nothing is recorded. With no fixing task the version is not checked and one line says so."""
    from ttp import cli, upstream
    p = make(env)
    repo = p.root
    monkeypatch.chdir(p.base)
    for r in _git_out(repo, "remote").split():
        _git_out(repo, "remote", "remove", r)
    man = repo / "plugins/p/.claude-plugin/plugin.json"
    man.parent.mkdir(parents=True)

    def commit(msg, version=None, path=None):
        if version:
            man.write_text(json.dumps({"name": "p", "version": version}, indent=2) + "\n")
        if path:
            (repo / path).write_text(msg + "\n")
        _git_out(repo, "add", "-A")
        _git_out(repo, "commit", "-qm", msg)
        return _git_out(repo, "rev-parse", "HEAD")

    start = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    commit("p 0.1.0", "0.1.0")
    _git_out(repo, "checkout", "-qb", "proj")
    commit("p 0.1.1", "0.1.1")
    fix = commit("the fix", path="fix.txt")
    commit("p 0.1.2", "0.1.2")
    _git_out(repo, "checkout", "-qb", "ttp/t-side", "HEAD~3")   # a done task's own commit, landed rebased
    side = commit("the side fix", path="side.txt")
    _git_out(repo, "checkout", "-q", "proj")
    _git_out(repo, "cherry-pick", side)
    commit("p 0.1.3", "0.1.3")
    _git_out(repo, "checkout", "-q", start)
    p.set_config("delivery.push_branch", "proj")
    p.set_config("delivery.version_bump", {"files": ["plugins/p/.claude-plugin/plugin.json"]})
    fps = {k: upstream.fingerprint(f"upstream: {k}", "s") for k in ("landed", "rebased", "pending", "none")}
    landed = p.db.add_task("fix the note", "s", kind="code")
    p.db.update_task(landed, status="done", result=json.dumps({"status": "done", "resolves": [fps["landed"]]}))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) "
           "VALUES(?,'b',?,'proj','landed',?,?,?)", (landed, fix, time.time(), time.time(), fix))
    rebased = p.db.add_task("fix another note", "s", kind="code", branch="ttp/t-side")
    p.db.update_task(rebased, status="done", result=json.dumps({"status": "done", "resolves": [fps["rebased"]]}))
    pending = p.db.add_task("fix a third note", "s", kind="code")
    p.db.update_task(pending, status="running", result=json.dumps({"resolves": fps["pending"]}))
    mention = p.db.add_task("only mentions a note", "s", kind="code")   # named in a summary, not in resolves
    p.db.update_task(mention, status="running", result=json.dumps({"summary": f"see note {fps['none']}"}))

    def resolve(fp, v, *extra):
        rc = _upstream_cli(cli, "--resolve", fp, "--fixed-in", v, *extra)
        out = capsys.readouterr()
        return rc, out.out + out.err

    # Refused: a version before the fix, a version no commit carries, a task that has not landed.
    rc, out = resolve(fps["landed"], "0.1.1")
    assert rc == 2 and upstream.resolution(fps["landed"]) is None
    assert (f"refused: note {fps['landed']} is not fixed in 0.1.1: #{landed} fixes note {fps['landed']} but its "
            f"landed commit {fix[:10]} (its push queue landing) is not in version 0.1.1 (its first commit ") in out
    rc, out = resolve(fps["landed"], "v0.1.9")
    assert rc == 2 and "is not in version 0.1.9 (no commit on refs/heads/proj carries it yet)" in out
    rc, out = resolve(fps["pending"], "0.1.3")
    assert rc == 2 and f"#{pending} fixes note {fps['pending']} but is not landed" in out
    assert upstream.resolution(fps["pending"]) is None
    rc, out = resolve(fps["rebased"], "0.1.2")
    assert rc == 2 and f"landed commit {side[:10]} (its branch ttp/t-side) is not in version 0.1.2" in out
    rc, out = resolve(fps["none"], "0.1.3", "--task", "99999")
    assert rc == 2 and "no task #99999 in this project" in out and upstream.resolution(fps["none"]) is None
    # Accepted: the version that carries it, a later one, a rebased copy of the task's commit.
    for fp, v, tid in ((fps["landed"], "0.1.2", landed), (fps["landed"], "0.1.3", landed),
                       (fps["rebased"], "0.1.3", rebased)):
        rc, out = resolve(fp, v)
        assert rc == 0 and f"version {v} checked: #{tid} (" in out and upstream.resolution(fp)["fixed_in"] == v
    # Unchecked: no task lists the note; an explicit --task is checked like a listed one.
    rc, out = resolve(fps["none"], "0.1.0")
    assert rc == 0 and f"version 0.1.0 not checked: no task's hand-off lists note {fps['none']} in resolves" in out
    rc, out = resolve(fps["none"], "0.1.1", "--task", str(landed))
    assert rc == 2 and f"#{landed} fixes note" in out and upstream.resolution(fps["none"])["fixed_in"] == "0.1.0"
    rc, out = resolve(fps["none"], "0.1.2", "--task", str(landed))
    assert rc == 0 and upstream.resolution(fps["none"])["fixed_in"] == "0.1.2"
    assert _upstream_cli(cli, "--resolve", fps["none"], "--wontfix", "r", "--task", str(landed)) == 2
    # No version_bump: not checked, recorded as before.
    p.set_config("delivery.version_bump", None)
    rc, out = resolve(fps["pending"], "0.1.3")
    assert rc == 0 and "version 0.1.3 not checked: delivery.version_bump is not set" in out


# The text of a reply that went out with its template slot unfilled (a shared text for several replies).
_SLOT_REPLY = ("[note %s] Correction on the ack: the config key has existed since the 0.2.685 batch (commit "
               "3ebcf99ba), and is in your installed 0.2.697 - it is off by default.")


def test_a_note_or_reply_with_an_unfilled_placeholder_is_refused_before_anything_is_filed(env, monkeypatch, capsys):
    """`ttp note --reply`, `--to` and a lesson for the maintainers refuse a text that still holds a template
    placeholder (`[note %s]`, `[note <fp>]`, a bare `<text>`): a non-zero exit naming it, nothing filed or
    queued. The shared send paths refuse it too: `send_or_queue`, `queue`, and a queued line `send_queued` meets."""
    from ttp import cli, upstream
    p = make(env)
    _note_run(env, monkeypatch, p)
    _fake_forward_ssh(env, monkeypatch)
    upstream.set_forward_to(["farbox"])
    remote = _pushed_note(p.name, "read from a far inbox", host="laptop")
    p.db.set_kv(upstream.KV_ROUTES, {remote["fp"]: upstream._route(remote, "farbox", time.time())})
    cases = [("--reply", remote["fp"], _SLOT_REPLY, "'[note %s]'"),
             ("--to", "alpha@farbox", "[note <fp>] logs attached", "'[note <fp>]'"),
             ("--to", "alpha@farbox", "answer to [note {fp}]", "'[note {fp}]'"),
             ("--to", "alpha@farbox", "re [note $FP]: done", "'[note $FP]'"),
             ("--reply", remote["fp"], '"<text>"', "'\"<text>\"'"),
             ("--to", upstream.MAINTAINERS, "lesson [note ${fp}]", "'[note ${fp}]'")]
    for flag, to, text, found in cases:
        assert _note(cli, flag, to, text) != 0, text
        err = capsys.readouterr().err
        assert f"not sent: the note's text still holds the placeholder {found}" in err, err
    assert not (env["home"] / "upstream.jsonl").exists(), "nothing was filed"
    assert not (p.state / upstream.OUTBOX_FILE).exists(), "nothing was queued"
    for fn in (lambda: upstream.send_or_queue(p.state, p.name, 3, "alpha", _SLOT_REPLY),
               lambda: upstream.queue(env["home"], "alpha", _SLOT_REPLY)):
        with pytest.raises(upstream.NotePlaceholder):
            fn()
    run = env["home"] / "slotrun"
    run.mkdir()
    (run / upstream.QUEUED_FILE).write_text(json.dumps({"to": "alpha", "text": _SLOT_REPLY}) + "\n")
    assert upstream.send_queued(run, p.name, 3, p.state)["invalid"] == 1
    assert not (env["home"] / "upstream.jsonl").exists()


@pytest.mark.parametrize("text", [
    "is a < b and c > d here?", 'the "<div>" and <b> tags render', "it costs $5 an hour",
    "commit 3ebcf99ba fixed it, see #4965", "[note ce11b4d40c27f9f4] is answered",
    "note 6e55bf9e40ef6c7b held a bad `[note %s]` tag", "retry_when: test \"$(date +%s)\" -ge 1",
    "run `ttp note --reply <fp> \"<text>\"`", "[note: read this first] 100% done", "a <text> field"])
def test_ordinary_note_text_is_not_taken_for_a_placeholder(env, monkeypatch, capsys, text):
    """The placeholder guard is narrow: comparisons, HTML-like words, dollar amounts, hashes and ids, a real
    `[note <16 hex>]` tag and placeholders quoted in backticks all go through."""
    from ttp import cli, upstream
    upstream.check_placeholder(text)
    p = make(env)
    _note_run(env, monkeypatch, p)
    _fake_forward_ssh(env, monkeypatch)
    upstream.set_forward_to(["farbox"])
    assert _note(cli, "--to", "alpha@farbox", text) == 0, capsys.readouterr().err
    assert _upstream_inbox(env)[-1]["spec"].startswith(" ".join(text.split()))


def _confirm_pair(env):
    """A sending project and a second project on this machine that fixed note `fp` in release 0.2.1."""
    from ttp import upstream
    from ttp.cli import bootstrap
    from ttp.project import register
    src = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    dst = bootstrap(repo2, "second", "Another project.", "fake")
    register("second", {"host": "testhost", "dir": str(dst.root)})
    fp = upstream.fingerprint("upstream: gh fails in the sandbox", "the proxy host does not resolve")
    upstream.resolve(fp, "0.2.1", "proxy host rewritten", "second")
    return src, dst, fp


def _queued_notes(p):
    return [dict(r) for r in p.db.q("SELECT severity, text, status FROM events WHERE kind='upstream_note'")]


def test_a_confirmation_is_the_senders_ack_and_closes_on_the_fixing_project_with_no_coordinator_wake(
        env, monkeypatch, capsys):
    """`ttp note --confirms <fp>` records the sender's ack (the fixing project's receipt) and sends no note.
    With --to the note goes marked; the fixing project closes it model-free as 'confirmation, not a bug',
    as a handled event that wakes no coordinator, and its daily review counts it."""
    from ttp import audit, cli, upstream
    src, dst, fp = _confirm_pair(env)
    _note_run(env, monkeypatch, src)
    assert _note(cli, "--confirms", fp, "gh works from a worker on 0.2.1") == 0
    assert "no note to it is needed" in capsys.readouterr().out
    assert upstream.state(fp) == "closed" and upstream.acked(fp)["project"] == src.name
    assert not (env["home"] / "upstream.jsonl").exists(), "the ack alone is the receipt"
    # tt-project's maintainers: also only the ack.
    assert _note(cli, "--to", "tt-project", "--confirms", fp, "still works") == 0
    assert not (env["home"] / "upstream.jsonl").exists()
    assert _note(cli, "--to", "second", "--confirms", fp, "confirmed on 0.2.1: gh works") == 0
    (n,) = _upstream_inbox(env)
    assert n["confirms"] == fp
    assert upstream.ingest(dst, dst.config()) == 1
    (ev,) = _queued_notes(dst)
    assert ev["status"] == "handled" and ev["severity"] == "low" and upstream.CONFIRMATION in ev["text"], ev
    assert upstream.wontfixed(n["fp"])["note"].startswith(upstream.CONFIRMATION)
    assert upstream.confirmations_closed("second", time.time() - 60) == 1
    # A crash before the read cursor moved reads the note again: it is still closed once.
    dst.db.set_kv(upstream.KV_CURSOR, {})
    assert upstream.ingest(dst, dst.config()) == 1
    assert upstream.confirmations_closed("second", time.time() - 60) == 1
    dst.db.x("DELETE FROM events WHERE kind='upstream_note' AND id > (SELECT MIN(id) FROM events WHERE kind='upstream_note')")
    assert any("confirmation notes closed model-free" in x and x.endswith(": 1")
               for x in audit.lines(dst.db, p=dst)), audit.lines(dst.db, p=dst)
    # An fp that has no fix here is refused whole: nothing recorded, nothing sent.
    other = "0123456789abcdef"
    assert _note(cli, "--to", "second", "--confirms", other, "works") == 2
    assert len(_upstream_inbox(env)) == 1 and not upstream.acked(other)


def test_a_confirmation_marker_survives_the_outbox_the_run_queue_and_forwarding(env, monkeypatch):
    """The marker travels every path an addressed note takes: the run's queue, the outbox past the hourly
    limit, and a forwarded line checked by `_valid`; a malformed marker is dropped, never filed."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    run_dir = _note_run(env, monkeypatch, src)
    upstream.queue(run_dir, "second", "works via the run queue", confirms=fp)
    upstream.send_queued(run_dir, src.name, 57, src.state)
    for i in range(upstream.NOTES_PER_HOUR + 1):
        upstream.send_or_queue(src.state, src.name, 57, "second", f"works {i}", confirms=fp)
    assert all(n.get("confirms") == fp for n in upstream._outbox(src.state))
    upstream.drain_outbox(src.state, src.name, time.time() + 7200)
    notes = _upstream_inbox(env)
    assert len(notes) == upstream.NOTES_PER_HOUR + 2 and all(n.get("confirms") == fp for n in notes)
    assert upstream._valid(notes[0])["confirms"] == fp
    assert "confirms" not in upstream._valid({**notes[0], "confirms": "not-a-fingerprint"})
    assert upstream.send(src.name, 57, "second", "bad marker", now=time.time() + 4 * 3600, confirms="../x") == "sent"
    assert "confirms" not in _upstream_inbox(env)[-1]


def test_a_confirmation_that_also_reports_a_problem_still_wakes_the_coordinator(env, monkeypatch):
    """Only the marker of a clean fix here closes a note: words alone never do, nor a marker of a note
    with no release fix, one resolved by another project, one closed as wontfix, or one the sender
    reports regressed. Each of those still raises its event as before."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    unfixed = upstream.fingerprint("upstream: x", "not fixed")
    theirs = upstream.fingerprint("upstream: y", "fixed elsewhere")
    upstream.resolve(theirs, "0.2.1", "", "third")
    dropped = upstream.fingerprint("upstream: z", "won't fix")
    upstream.resolve(dropped, "0.2.1", "", "second")
    upstream.wontfix(dropped, "not a bug", "second")
    upstream.send(src.name, 57, "second", f"Confirmed on 0.2.1, note {fp} acked. New problem: gh times out.")
    for i, c in enumerate((unfixed, theirs, dropped)):
        upstream.send(src.name, 57, "second", f"confirms {i}", confirms=c)
    assert upstream.ingest(dst, dst.config()) == 4
    assert [e["status"] for e in _queued_notes(dst)] == ["queued"] * 4
    # The sender confirms, then finds the fix does not hold: its regression keeps the note an event.
    upstream.regress(fp, "gh fails again after a restart", src.name)
    upstream.send(src.name, 57, "second", "it held at first", confirms=fp)
    assert upstream.ingest(dst, dst.config()) == 1
    assert _queued_notes(dst)[-1]["status"] == "queued"
    assert upstream.confirmations_closed("second") == 0
