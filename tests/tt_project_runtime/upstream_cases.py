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


def test_forward_status_says_recovered_once_a_send_goes_through_after_failures(env, monkeypatch):
    """`ttp upstream --forward-status` stops saying 'failing' once a run or the CLI got a note through
    after the daemon's failures, while the daemon keeps its own record for the run fallback."""
    from ttp import upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    upstream.set_forward_to(["example-host"])
    fake["mode"].write_text("ok")
    now = time.time() - 3 * 3600
    assert upstream.forward(now) == 0
    upstream.append("demo", 3, [{"title": "upstream: one", "spec": "a"}])
    real = upstream._ssh_receive
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (None, "exit 255: ssh: connection timed out"))
    for later in (now + 60, now + 200, now + 500):
        assert upstream.forward(later) == 0
    (line,) = upstream.forward_status(now + 600)
    assert "failing (3x" in line and "recovered" not in line
    monkeypatch.setattr(upstream, "_ssh_receive", real)
    t = now + 60 + upstream.FALLBACK_AFTER_S
    assert upstream.forward(t, context="run") == 1
    assert _fwd_state("example-host")["fails"] == 3, "the daemon's failure record is its own"
    (line,) = upstream.forward_status(t + 100)
    assert "failing" not in line and "recovered: a send went through" in line and "3 failed tries" in line
    # A daemon failure after that send is failing again.
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (None, "exit 255: ssh: connection timed out"))
    upstream.forward(t + 7200)
    (line,) = upstream.forward_status(t + 7300)
    assert "failing (4x" in line and "recovered" not in line


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


def test_upstream_fixed_on_a_never_filed_note_exits_as_a_broken_probe_not_a_not_yet(env, monkeypatch, capsys):
    """`ttp upstream --fixed <fp>` on an id no one filed said "open, no fix recorded yet" and exited 1, a
    not-yet, so a task waiting on it slept until its deferral ran out. An fp in no inbox, resolved log or
    outbox, and on no forward target, now prints "never filed" and exits NEVER_FILED_RC, which the daemon
    reads as a broken probe. Filed, fixed, outbox-only and forward-target-only notes exit as before, and a
    forward target that cannot be asked keeps the wait (an unreachable machine never ends a real one)."""
    from ttp import cli, daemon, upstream
    p = make(env)
    monkeypatch.chdir(p.base)
    _runs_version(p, "0.2.9")
    assert upstream.NEVER_FILED_RC not in (0, *daemon.NOT_YET_RCS)
    note = {"title": "upstream: a made-up fault", "spec": "s"}
    upstream.append("demo", 4, [note])
    filed = upstream.fingerprint(note["title"], note["spec"])
    unknown, fixed_fp, queued, remote = "0123456789abcdef", "fedcba9876543210", "00000000000000aa", "00000000000000bb"
    upstream.resolve(fixed_fp, "0.2.9", "", "")    # its resolution came back over the forward exchange only
    (p.state / upstream.OUTBOX_FILE).write_text(json.dumps({"fp": queued, "to": "made-up-peer", "text": "t"}) + "\n")
    answers = {}
    monkeypatch.setattr(upstream, "lookup_targets", lambda: list(answers))
    monkeypatch.setattr(upstream, "ssh_pipe", lambda target, args, data, timeout: answers[target](args))
    capsys.readouterr()

    def probe(fp):
        rc = _upstream_cli(cli, "--fixed", fp)
        return rc, capsys.readouterr().out.strip()

    rc, out = probe(unknown)
    assert rc == upstream.NEVER_FILED_RC and out.startswith(f"unknown note {unknown}: never filed"), out
    assert probe(filed)[0] == 1
    assert probe(fixed_fp)[0] == 0
    rc, out = probe(queued)
    assert rc == 1 and "waiting for a fix" in out, out
    answers["made-up-target"] = lambda args: (json.dumps({"fp": remote, "inbox": remote in args}).encode(), "")
    rc, out = probe(remote)
    assert rc == 1 and "in made-up-target's inbox" in out, out
    assert probe(unknown)[0] == upstream.NEVER_FILED_RC, "a target that answered no kept the wait"
    answers["made-up-down"] = lambda args: (None, "ssh: connect timed out")
    rc, out = probe(unknown)
    assert rc == 1 and "could not ask made-up-down" in out, out
    monkeypatch.setattr(upstream, "NEVER_FILED_ASK_S", 0)    # out of time: the targets left count as unasked
    answers["made-up-target"] = lambda args: (json.dumps({"inbox": False}).encode(), "")
    rc, out = probe(unknown)
    assert rc == 1 and "could not ask made-up-target, made-up-down" in out, out
    monkeypatch.setattr(upstream, "NEVER_FILED_ASK_S", 45)
    p.db.set_kv("daemon", {})
    assert probe(unknown)[0] == 2 and probe(filed)[0] == 2
    del answers["made-up-down"]
    assert probe(unknown)[0] == upstream.NEVER_FILED_RC, "an unknown running version hid a never-filed id"


def test_a_new_wait_on_a_never_filed_note_is_refused_where_it_is_set(env, monkeypatch, capsys):
    """A start_when (coordinator or `ttp task set-when`), a set-when retry_when and a waiting hand-off's
    retry_when that wait on `ttp upstream --fixed <fp>` for an id never filed here are refused, naming it.
    A filed note, the hand-off's own `upstream:` note (filed after its probe is read) and any id while a
    forward target exists (it may be known only there) are kept."""
    from ttp import cli, upstream
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    p = make(env)
    monkeypatch.chdir(p.base)
    unknown = "0123456789abcdef"
    note = {"title": "upstream: a made-up harness fault", "spec": "s"}
    upstream.append("demo", 4, [note])
    filed = upstream.fingerprint(note["title"], note["spec"])
    problems = coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s",
                                "start_when": f"ttp upstream --fixed {unknown.upper()}"}])
    assert problems and f"never filed" in problems[0] and unknown in problems[0], problems
    assert not p.db.q("SELECT id FROM tasks WHERE title='later'")
    assert not coord.apply(p, [{"type": "task_add", "title": "later", "spec": "s",
                                "start_when": f"ttp upstream --fixed {filed}"}])
    waiting = _due_waiting_task(p, "exit 1")
    before = p.db.task(waiting)["result"]
    with pytest.raises(SystemExit):
        cli.main(["task", "demo", "set-when", str(waiting), f"ttp upstream --fixed '{unknown}'"])
    assert unknown in capsys.readouterr().err and p.db.task(waiting)["result"] == before
    with monkeypatch.context() as m:
        m.setattr(upstream, "lookup_targets", lambda: ["made-up-target"])
        assert upstream.unfiled_waits(f"ttp upstream --fixed {unknown}", p.state) == []
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    own = {"title": "upstream: a made-up worker prompt gap", "spec": "made-up spec"}
    for probe, fups in ((f"ttp upstream --fixed={unknown}", []),
                        (f"ttp upstream --fixed {upstream.fingerprint(own['title'], own['spec'])}", [own])):
        monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
            "status": "waiting", "summary": "waits on a fix", "waiting_for": "a fix", "retry_after_s": 900,
            "retry_when": probe, "followups": fups}))
        tid = p.db.add_task(f"waits on {probe}", "s", kind="work", tier="light", origin="user")
        assert _run_until(d, p, lambda: p.db.one("SELECT id FROM events WHERE kind='task_waiting' AND task=?",
                                                 (tid,)))
        res = json.loads(p.db.task(tid)["result"])
        refused = _events(p, tid, "handoff_probe_refused")
        if fups:
            assert res["retry_when"] == probe and not refused, (res, refused)
        else:
            assert "retry_when" not in res and res["retry_when_refused"] == probe, res
            assert len(refused) == 1 and f"{unknown}" in refused[0]["text"] and "never filed" in refused[0]["text"]


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


def test_only_a_project_that_reads_the_inbox_replies_to_a_general_note(env, monkeypatch, capsys):
    """A general note (no `to`) is for the maintainers: `--reply` to it from a project with `upstream.ingest`
    off is refused and files nothing, as a scratch project replaying a spec that quotes `[note <fp>]` would
    otherwise answer the real sender; the project that reads the inbox still answers it."""
    from ttp import cli, upstream
    from ttp.project import register
    p = make(env)
    (env["tmp"] / "second").mkdir()
    register("second", {"host": "testhost", "dir": str(env["tmp"] / "second")})
    _note_run(env, monkeypatch, p)
    upstream.append("second", 7, [{"title": "upstream: the retry default", "spec": "make it 5"}])
    fp = upstream.fingerprint("upstream: the retry default", "make it 5")
    assert _note(cli, "--reply", fp, "fixed in 0.3.2") == 2
    assert f"not sent: no note {fp} from another project came to demo" in capsys.readouterr().err
    assert len(_upstream_inbox(env)) == 1, "nothing filed"
    p.set_config("upstream.ingest", True)
    assert _note(cli, "--reply", fp, "fixed in 0.3.2") == 0
    assert _upstream_inbox(env)[-1]["to"] == "second"


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


def test_upstream_resolve_accepts_a_retired_tip_fix_at_a_version_after_the_squash(env, monkeypatch, capsys):
    """A fix whose landed commit is on a retired tip (the push branch was squashed since) resolves at a
    version cut after the squash, and at a version the squash folded whose first commit on the retired tip
    carries it; a version without it stays refused and records nothing, the retired tip's own version too
    when its first commit predates the fix, whether the squash kept or dropped that commit."""
    from ttp import cli, landed, upstream
    p = make(env)
    repo = p.root
    monkeypatch.chdir(p.base)
    for r in _git_out(repo, "remote").split():
        _git_out(repo, "remote", "remove", r)
    man = repo / "plugins/p/.claude-plugin/plugin.json"
    man.parent.mkdir(parents=True)
    p.set_config("delivery.version_bump", {"files": ["plugins/p/.claude-plugin/plugin.json"]})

    def commit(msg, version=None, path=None):
        if version:
            man.write_text(json.dumps({"name": "p", "version": version}, indent=2) + "\n")
        if path:
            (repo / path).write_text(msg + "\n")
        _git_out(repo, "add", "-A")
        _git_out(repo, "commit", "-qm", msg)
        return _git_out(repo, "rev-parse", "HEAD")

    def squash(onto, then):                                       # one commit holding the old tip's tree
        old = _git_out(repo, "rev-parse", "HEAD")
        _git_out(repo, "reset", "-q", "--soft", onto)
        _git_out(repo, "commit", "-qm", "squash")
        commit(f"p {then}", then)
        assert landed.retire_tip(p, old) == old
        return old

    def fixed_by(fix, branch):
        p.set_config("delivery.push_branch", branch)
        fp = upstream.fingerprint(f"upstream: squashed fix on {branch}", "s")
        tid = p.db.add_task("fix the note", "s", kind="code")
        p.db.update_task(tid, status="done", result=json.dumps({"status": "done", "resolves": [fp]}))
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) "
               "VALUES(?,'b',?,?,'landed',?,?,?)", (tid, fix, branch, time.time(), time.time(), fix))

        def resolve(v):
            rc = _upstream_cli(cli, "--resolve", fp, "--fixed-in", v, "--task", str(tid))
            out = capsys.readouterr()
            return rc, out.out + out.err
        return fp, resolve

    start = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    base = commit("p 0.1.0", "0.1.0")
    _git_out(repo, "checkout", "-qb", "proj")                     # the squash folds versions 0.1.1 and 0.1.2
    fix = commit("the fix", path="fix.txt")
    commit("p 0.1.1", "0.1.1")
    commit("p 0.1.2", "0.1.2")
    old = squash(base, "0.1.3")
    _git_out(repo, "checkout", "-qb", "proj2", base)              # the squash keeps the bump to 0.1.1
    bump = commit("p 0.1.1", "0.1.1")
    late = commit("a late fix", path="late.txt")
    commit("more work", path="more.txt")
    old2 = squash(bump, "0.1.2")
    _git_out(repo, "checkout", "-qb", "proj3", base)              # the squash drops the bump to 0.1.1
    commit("p 0.1.1", "0.1.1")
    fix3 = commit("a fix after 0.1.1", path="fix3.txt")
    squash(base, "0.1.2")
    _git_out(repo, "checkout", "-q", start)

    fp, resolve = fixed_by(fix, "proj")
    rc, out = resolve("0.1.0")                                      # before the fix
    assert rc == 2 and "is not in version 0.1.0" in out and upstream.resolution(fp) is None
    rc, out = resolve("0.1.1")                                      # folded; carries it on the retired tip
    assert rc == 0 and "is in " in out and f"on the retired tip {old[:10]}" in out
    rc, out = resolve("0.1.3")                                      # cut after the squash
    assert rc == 0 and f"is on the retired tip {old[:10]} (0.1.2), which " in out
    assert upstream.resolution(fp)["fixed_in"] == "0.1.3"
    fp, resolve = fixed_by(late, "proj2")
    rc, out = resolve("0.1.1")                                      # the retired tip's own version, cut before it
    assert rc == 2 and "is not in version 0.1.1" in out and upstream.resolution(fp) is None
    rc, out = resolve("0.1.2")
    assert rc == 0 and f"is on the retired tip {old2[:10]} (0.1.1), which " in out
    fp, resolve = fixed_by(fix3, "proj3")
    rc, out = resolve("0.1.1")                                      # first on the new history at the squash
    assert rc == 2 and "is not in version 0.1.1" in out and upstream.resolution(fp) is None


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


def _page_pair(env, monkeypatch):
    """A paging project, a second one that made three changes on made-up machines, and a page from the
    first one's daemon to the second about each, read by the second."""
    from ttp import machine_ledger as ml, machines, alerts, upstream
    src, dst, _ = _confirm_pair(env)
    now = time.time()
    pages = {}
    for alias in ("box-a", "box-b", "box-c"):
        machines.add(alias)
        c = ml.add_change(alias, "lowered the clock", "reset the clock", "second", expires="1h", now=now - 7200)
        text = f"Machine change #{c['id']} on {alias} is overdue: lowered the clock (by second). Page 1."
        assert upstream.send(src.name, None, "second", text, "normal", sender="daemon",
                             extra={"page_fp": alerts.change_fingerprint(f"machine-change:{c['id']}", c["added"])}) == "sent"
        pages[c["id"]] = upstream._inbox()[-1]["fp"]
    upstream.ingest(dst, dst.config())
    return src, dst, pages


def test_a_reply_that_only_reports_a_page_changes_close_is_settled_without_a_coordinator_wake(env, monkeypatch, capsys):
    """`ttp machines change close <id>` answers the page about it with the canonical close report; the
    paging project settles it model-free, recorded with its reason and note id and counted in the daily
    review. An open change, more words, high severity or a reply to a note that is not a page all wake."""
    from ttp import audit, cli, machine_ledger as ml, upstream
    src, dst, pages = _page_pair(env, monkeypatch)
    by_id = {c["id"]: c for c in ml.changes()}

    def reply(cid, text, sev="normal", to_note=None):
        assert upstream.send("second", 5, src.name, text, sev, reply_to=to_note or pages[cid]) == "sent"

    def ingest():
        before = {r["id"] for r in src.db.q("SELECT id FROM events")}
        upstream.ingest(src, src.config())
        return [dict(r) for r in src.db.q("SELECT id, severity, status, text FROM events WHERE kind='upstream_note'")
                if r["id"] not in before]

    # The change is still open: the report wakes.
    reply(2, upstream.close_report_text(by_id[2]))
    (ev,) = ingest()
    assert ev["status"] == "queued", ev
    # Closed, but the reply says more (a request): wakes.
    ml.close_change(3, "", by="second")
    reply(3, upstream.close_report_text(by_id[3]) + " Please also restart its service.")
    # Closed, the exact report, but high: wakes.
    reply(3, upstream.close_report_text(by_id[3]), "high")
    # The exact report of a closed change, answering a worker's note instead of a page: wakes.
    assert upstream.send(src.name, 7, "second", "Is box-c still lowered?") == "sent"
    reply(3, upstream.close_report_text(by_id[3]), to_note=upstream._inbox()[-1]["fp"])
    got = ingest()
    assert [e["status"] for e in got] == ["queued"] * 3, got
    assert upstream.close_reports_settled(src.name) == 0
    # The close itself answers the page: settled, recorded once, with no wake.
    _note_run(env, monkeypatch, dst)
    cli.main(["machines", "change", "close", "1"])
    assert f"answered page note {pages[1]}" in capsys.readouterr().out
    n = upstream._inbox()[-1]
    assert (n["reply_to"], n["confirms"], n["to"]) == (pages[1], pages[1], src.name)
    (ev,) = ingest()
    assert ev["status"] == "handled" and ev["severity"] == "low" and upstream.CLOSE_REPORT in ev["text"], ev
    assert f"[note {n['fp']}]" in ev["text"]
    rec = upstream.wontfixed(n["fp"])
    assert rec["note"].startswith(upstream.CLOSE_REPORT) and pages[1] in rec["note"] and rec["by"] == src.name
    src.db.set_kv(upstream.KV_CURSOR, {})   # read again after a crash: recorded once
    ingest()
    assert upstream.close_reports_settled(src.name, time.time() - 60) == 1
    assert any("close reports settled model-free" in x and x.endswith(": 1")
               for x in audit.lines(src.db, p=src)), audit.lines(src.db, p=src)


def test_a_confirmation_marker_survives_the_outbox_the_run_queue_and_forwarding(env, monkeypatch):
    """The marker travels every path an addressed note takes: the run's queue, the outbox past the hourly
    limit, and a forwarded line checked by `_valid`; a malformed marker is dropped, never filed."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    run_dir = _note_run(env, monkeypatch, src)
    upstream.queue(run_dir, "second", "works via the run queue", confirms=fp)
    upstream.send_queued(run_dir, src.name, 57, src.state)
    for i in range(upstream.NOTES_PER_HOUR + 1):   # each from its own task: one task's repeats are dropped
        upstream.send_or_queue(src.state, src.name, 100 + i, "second", f"works {i}", confirms=fp)
    assert all(n.get("confirms") == fp for n in upstream._outbox(src.state))
    upstream.drain_outbox(src.state, src.name, time.time() + 7200)
    notes = _upstream_inbox(env)
    assert len(notes) == upstream.NOTES_PER_HOUR + 2 and all(n.get("confirms") == fp for n in notes)
    assert upstream._valid(notes[0])["confirms"] == fp
    assert "confirms" not in upstream._valid({**notes[0], "confirms": "not-a-fingerprint"})
    assert upstream.send(src.name, 57, "second", "bad marker", now=time.time() + 4 * 3600, confirms="../x") == "sent"
    assert "confirms" not in _upstream_inbox(env)[-1]


def test_a_tasks_repeat_note_naming_the_same_note_ids_in_other_words_is_dropped(env, monkeypatch, capsys):
    """A note the same task already sent the same project within REPEAT_S, naming the same set of note ids
    (in the text or as --confirms), is dropped as a duplicate in any wording: no inbox line, no event, and
    `ttp note` names the earlier note. A new id, no id, another task, an older note, or a fix, regression
    or wontfix recorded since all still send."""
    from ttp import cli, upstream
    src, dst, fp = _confirm_pair(env)
    fp2 = upstream.fingerprint("upstream: b", "second note")
    upstream.resolve(fp2, "0.2.1", "fixed too", "second")
    _note_run(env, monkeypatch, src)
    assert _note(cli, "--to", "second", f"Confirmed on 0.2.1: notes {fp} and {fp2} both work.") == 0
    capsys.readouterr()
    (first,) = _upstream_inbox(env)
    for words in (f"{fp2} and {fp} still hold after a restart", f"[note {fp}] [note {fp2}] confirmed again",
                  f"FYI: {fp}, {fp2} fine"):
        assert _note(cli, "--to", "second", words) == 0
        out = capsys.readouterr().out
        assert f"not sent: this task already sent it note {first['fp']}" in out, out
    # A --confirms marker counts as a named id: the same set in other words is the same repeat.
    assert _note(cli, "--to", "second", "--confirms", fp, f"also {fp2}: fine") == 0
    assert first["fp"] in capsys.readouterr().out
    assert len(_upstream_inbox(env)) == 1
    assert upstream.ingest(dst, dst.config()) == 1
    now = time.time()
    # Still sent: a new id, a subset, no id, another task, another project.
    fp3 = upstream.fingerprint("upstream: c", "third note")
    assert upstream.send(src.name, 57, "second", f"{fp} {fp2} {fp3} all fine", now=now) == "sent"
    assert upstream.send(src.name, 57, "second", f"only {fp} checked", now=now) == "sent"
    assert upstream.send(src.name, 57, "second", "no ids here", now=now) == "sent"
    assert upstream.send(src.name, 57, "second", "no ids, other words", now=now) == "sent"
    assert upstream.send(src.name, 58, "second", f"{fp} and {fp2} hold here too", now=now) == "sent"
    assert upstream.send(src.name, 57, "tt-project", f"{fp} and {fp2} fine", now=now) == "sent"
    # Not hex ids: a longer hash or an uppercase run names no note.
    assert upstream.named_notes(f"commit {fp}{fp2} {fp.upper()} x{fp}") == frozenset()
    # Past REPEAT_S the same set sends again; so does one after a record of news about one of the ids.
    assert upstream.send(src.name, 57, "second", f"{fp} {fp2} day two", now=now + upstream.REPEAT_S + 1) == "sent"
    assert upstream.send(src.name, 59, "second", f"{fp} {fp2} first", now=now) == "sent"
    upstream.regress(fp2, "fails again", src.name, now=now + 5)
    assert upstream.send(src.name, 59, "second", f"{fp2} regressed, {fp} fine", now=now + 10) == "sent"
    assert upstream.send(src.name, 59, "second", f"{fp2} broke again; {fp} still ok", now=now + 20) == "duplicate"


def test_a_tasks_repeat_note_is_dropped_on_the_outbox_and_run_queue_paths(env, monkeypatch):
    """Past the hourly limit a repeat of a queued or filed note is dropped, not queued; the run queue a
    sandboxed worker leaves counts it a duplicate too. Other notes queue as before."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    fp2 = upstream.fingerprint("upstream: b", "second note")
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 1, "second", f"filler {i}", now=now)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} and {fp2} work", now=now) == ("queued", 1)
    first = upstream._outbox(src.state)[0]["fp"]
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp2}, {fp}: confirmed", now=now) == ("duplicate", 0)
    assert upstream.repeat_of(src.state, src.name, 57, "second", f"{fp2}, {fp}: confirmed", now=now) == first
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} works", now=now) == ("queued", 2)
    assert upstream.send_or_queue(src.state, src.name, 58, "second", f"{fp} and {fp2} hold", now=now) == ("queued", 3)
    run_dir = _note_run(env, monkeypatch, src)
    upstream.queue(run_dir, "second", f"again: {fp} {fp2}")
    upstream.queue(run_dir, "second", f"new: {fp2}, {fp}, done")
    got = upstream.send_queued(run_dir, src.name, 57, src.state)
    assert got["duplicate"] == 2 and got["queued"] == 0, got
    assert upstream.outbox_pending(src.state) == 3
    # Drained to the inbox, a repeat behind a waiting note is still dropped there.
    upstream.drain_outbox(src.state, src.name, now + 7200)
    upstream.send(src.name, 1, "second", "after", now=now + 7200)
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 1, "second", f"more {i}", now=now + 7300)
    assert upstream.send_or_queue(src.state, src.name, 60, "second", "wait", now=now + 7300)[0] == "queued"
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} {fp2} ok", now=now + 7300) == ("duplicate", 0)
    assert sum(n.get("task") == 57 and upstream.named_notes(n["spec"]) == {fp, fp2}
               for n in _upstream_inbox(env)) == 1


@pytest.mark.parametrize("first,second,sent", [("low", "high", True), ("normal", "high", True),
                                                 ("normal", "normal", False), ("high", "low", False)])
def test_a_tasks_repeat_note_at_a_higher_severity_is_an_escalation_and_is_filed(env, first, second, sent):
    """A task's repeat naming the same note ids at a higher severity than its earlier note asks for
    attention: it is filed and surfaced like a first note. One at the same or a lower severity is dropped."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    fp2 = upstream.fingerprint("upstream: b", "second note")
    now = time.time()
    assert upstream.send(src.name, 57, "second", f"{fp} and {fp2} look off", severity=first, now=now) == "sent"
    got = upstream.send(src.name, 57, "second", f"{fp2}, {fp}: now blocking", severity=second, now=now + 60)
    assert got == ("sent" if sent else "duplicate")
    assert upstream.ingest(dst, dst.config()) == (2 if sent else 1)
    if sent:
        assert _queued_notes(dst)[-1]["severity"] == "high" and _queued_notes(dst)[-1]["status"] == "queued"
        # Once escalated, a repeat at the same severity is dropped again.
        assert upstream.send(src.name, 57, "second", f"{fp} {fp2} still blocking", severity="high",
                             now=now + 120) == "duplicate"


def test_a_higher_severity_repeat_is_queued_on_the_outbox_and_run_queue_paths(env, monkeypatch):
    """Past the hourly limit, a task's escalation of a queued or filed note queues (a high one files at once, on its own allowance); a same-severity repeat
    is still dropped, and `repeat_of` names no earlier note for the escalation."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 1, "second", f"filler {i}", now=now)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} looks off", "low", now=now) == ("queued", 1)
    assert upstream.repeat_of(src.state, src.name, 57, "second", f"{fp} blocks", now=now, severity="normal") is None
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} blocks", "normal", now=now) == ("queued", 2)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", f"{fp} still blocks", "normal",
                                  now=now) == ("duplicate", 0)
    run_dir = _note_run(env, monkeypatch, src)
    upstream.queue(run_dir, "second", f"{fp}: low again", "low")
    got = upstream.send_queued(run_dir, src.name, 57, src.state)
    assert got["duplicate"] == 1 and got["queued"] == 0, got
    assert [n["severity"] for n in upstream._outbox(src.state)] == ["low", "normal"]


def _addressed(env, fp):
    """The inbox as its readers see it (`_lines`): the notes with id `fp`."""
    from ttp import upstream
    return [n for n in upstream._lines((env["home"] / "upstream.jsonl").read_bytes())[0] if n["fp"] == fp]


@pytest.mark.parametrize("first,second,raised", [("low", "high", True), ("normal", "high", True),
                                                  ("normal", "normal", False), ("high", "low", False)])
def test_the_same_note_again_at_a_higher_severity_raises_the_stored_note_and_surfaces_once(env, first, second, raised):
    """The same text to the same project at a strictly higher severity raises the stored note's severity:
    still one note to every reader, surfaced once more as an escalation. At the same or a lower severity
    it is dropped as a duplicate."""
    from ttp import upstream
    src, dst, _ = _confirm_pair(env)
    text = "the forwarder stalls on a full outbox"
    assert upstream.send(src.name, 57, "second", text, severity=first) == "sent"
    assert upstream.ingest(dst, dst.config()) == 1
    assert upstream.send(src.name, 57, "second", text, severity=second) == ("sent" if raised else "duplicate")
    (n,) = _addressed(env, upstream.note_id("second", text))
    assert n["severity"] == (second if raised else first)
    assert upstream.ingest(dst, dst.config()) == (1 if raised else 0)
    assert upstream.ingest(dst, dst.config()) == 0
    events = _queued_notes(dst)
    assert len(events) == (2 if raised else 1)
    if raised:
        assert events[-1]["severity"] == "high" and events[-1]["text"].startswith("escalated to high")
        assert text in events[-1]["text"]
        # Once raised, the same text at that severity is a duplicate again.
        assert upstream.send(src.name, 57, "second", text, severity="high") == "duplicate"


def test_a_note_and_its_escalation_read_together_surface_once_at_the_higher_severity(env):
    """A reader that meets the note and its escalation in one read files one event, at the raised severity."""
    from ttp import upstream
    src, dst, _ = _confirm_pair(env)
    assert upstream.send(src.name, 57, "second", "builds hang", severity="low") == "sent"
    assert upstream.send(src.name, 57, "second", "builds hang", severity="high") == "sent"
    assert upstream.ingest(dst, dst.config()) == 1
    (ev,) = _queued_notes(dst)
    assert ev["severity"] == "high" and "builds hang" in ev["text"]


def test_the_same_note_at_a_higher_severity_raises_it_on_the_outbox_and_run_queue_paths(env, monkeypatch):
    """Past the hourly limit: the same text as a waiting outbox note raises that entry in place (one entry);
    as a note already filed it raises the filed note at once (an escalation is not limited), from a worker
    and from a run's queue; at the same or a lower severity it stays dropped."""
    from ttp import upstream
    src, dst, _ = _confirm_pair(env)
    now = time.time()
    assert upstream.send(src.name, 57, "second", "filed note", severity="normal", now=now) == "sent"
    assert upstream.send(src.name, 57, "second", "queued run note", severity="low", now=now) == "sent"
    for i in range(upstream.NOTES_PER_HOUR - 2):
        upstream.send(src.name, 1, "second", f"filler {i}", now=now)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", "waiting note", "low", now=now) == ("queued", 1)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", "waiting note", "high", now=now) == ("queued", 1)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", "waiting note", "normal", now=now) == ("queued", 1)
    assert [n["severity"] for n in upstream._outbox(src.state)] == ["high"]
    assert upstream.send_or_queue(src.state, src.name, 57, "second", "filed note", "high", now=now) == ("sent", 0)
    assert upstream.send_or_queue(src.state, src.name, 57, "second", "filed note", "normal", now=now) == ("duplicate", 0)
    assert [n["severity"] for n in _addressed(env, upstream.note_id("second", "filed note"))] == ["high"]
    run_dir = _note_run(env, monkeypatch, src)
    upstream.queue(run_dir, "second", "queued run note", "high")
    upstream.queue(run_dir, "second", "filed note", "low")
    got = upstream.send_queued(run_dir, src.name, 57, src.state)
    assert (got["sent"], got["duplicate"], got["queued"]) == (1, 1, 0), got
    assert [n["severity"] for n in _addressed(env, upstream.note_id("second", "queued run note"))] == ["high"]
    assert upstream.outbox_pending(src.state) == 1


def test_only_a_workers_note_at_a_known_higher_severity_raises_the_filed_note(env):
    """A daemon's page with the same text, or a worker's at a severity that is not one, stays a duplicate."""
    from ttp import upstream
    src, dst, _ = _confirm_pair(env)
    assert upstream.send(src.name, 57, "second", "disk nearly full", severity="low") == "sent"
    assert upstream.send(src.name, None, "second", "disk nearly full", "high", sender="daemon") == "duplicate"
    assert upstream.send(src.name, 57, "second", "disk nearly full", severity="urgent") == "duplicate"
    assert [n["severity"] for n in _addressed(env, upstream.note_id("second", "disk nearly full"))] == ["low"]
    assert len((env["home"] / "upstream.jsonl").read_text().splitlines()) == 1


def test_an_escalation_forwarded_from_another_machine_raises_the_received_note(env):
    """The forwarder sends an escalation like any line; the receiver raises the note it has (one note,
    higher severity), counts a repeat of it as a duplicate, and files it as the note if it lacks one."""
    from ttp import upstream
    sent = _pushed_note("second", "the sweep logs are gone")
    up = {**sent, "severity": "high", "escalation": True, "escalated": time.time()}
    ack, rc = upstream.receive((json.dumps(sent) + "\n" + json.dumps(up) + "\n").encode(), "laptop")
    assert (rc, ack["accepted"], ack["duplicates"], ack["rejected"]) == (0, 2, 0, 0)
    ack, rc = upstream.receive((json.dumps(up) + "\n").encode(), "laptop")
    assert (rc, ack["accepted"], ack["duplicates"]) == (0, 0, 1)
    (n,) = _addressed(env, sent["fp"])
    assert n["severity"] == "high" and n["via"] == "laptop"
    other = _pushed_note("second", "a note only its escalation reached")
    assert upstream.receive((json.dumps({**other, "severity": "high", "escalation": True}) + "\n").encode(),
                            "laptop")[0]["accepted"] == 1
    assert [n["severity"] for n in _addressed(env, other["fp"])] == ["high"]


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


def test_a_high_severity_confirmation_is_filed_as_a_note_and_left_open(env, monkeypatch):
    """A sender that marks a confirmation high asks for attention: the receiver files it as a queued
    event and leaves it open, whatever sent it; a low or normal one still closes model-free."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    upstream.send(src.name, 57, "second", "confirmed, but look at this", severity="high", confirms=fp)
    assert upstream.ingest(dst, dst.config()) == 1
    (high,) = _upstream_inbox(env)
    (ev,) = _queued_notes(dst)
    assert ev["status"] == "queued" and ev["severity"] == "high", ev
    assert not upstream.wontfixed(high["fp"]) and upstream.confirmations_closed("second") == 0
    # Each from its own task: one task's repeat confirmation of the same note is dropped (`_repeat`).
    for task, sev in ((58, "low"), (59, "normal")):
        upstream.send(src.name, task, "second", f"confirmed at {sev}", severity=sev, confirms=fp)
    assert upstream.ingest(dst, dst.config()) == 2
    assert [e["status"] for e in _queued_notes(dst)] == ["queued", "handled", "handled"]
    assert upstream.confirmations_closed("second") == 2


@pytest.mark.parametrize("title", ["test probe", "  upstream: test probe "])
def test_upstream_fp_adds_the_prefix_a_filed_title_carries_and_files_nothing(env, monkeypatch, capsys, title):
    """`ttp upstream --fp` hashed a bare title as given, an id no filed note (always `upstream: ...`)
    can have, and a worker took the printed id for a filed note. It now adds the prefix, says on
    stderr that nothing was filed, keeps stdout the bare id, and writes no inbox, notes or resolved file."""
    from ttp import cli, upstream, project
    p = make(env)
    monkeypatch.chdir(p.base)
    roots = {project.HOME_DIR, upstream.path().parent, upstream.resolved_path().parent}

    def snapshot():
        return {f: f.read_bytes() for r in roots if r.exists() for f in sorted(r.rglob("*")) if f.is_file()}

    before = snapshot()
    capsys.readouterr()
    assert _upstream_cli(cli, "--fp", title, "spec") == 0
    out = capsys.readouterr()
    assert snapshot() == before, "ttp upstream --fp wrote a file"
    assert not upstream.path().exists() and not upstream.resolved_path().exists()
    upstream.append("demo", 4, [{"title": "upstream: test probe", "spec": "spec"}])
    filed = [json.loads(line)["fp"] for line in upstream.path().read_text().splitlines()]
    assert out.out == filed[0] + "\n", out.out
    assert "a fingerprint only, no note was filed" in out.err, out.err
    assert ("'upstream: test probe'" in out.err) == (title == "test probe"), out.err


def test_replies_with_the_same_words_to_two_notes_are_two_linked_notes_on_every_path(env, monkeypatch, capsys):
    """`ttp note --reply <fp>` marks the reply with the note it answers (`reply_text`), and the mark is part
    of its id: the same words to two notes are two replies, each linked to its own, filed directly, queued
    in the run's directory (a read-only sandbox) or in the outbox (the hourly limit), and read so by the
    project it answers. The same words to the same note stay one; a plain `--to` note keeps its old id;
    a queued row written before the mark (no `reply_to`) is still filed."""
    from ttp import cli, upstream
    src, dst, _ = _confirm_pair(env)
    a = upstream.send("second", 4, src.name, "is the cache stale?")
    b = upstream.send("second", 5, src.name, "is the log rotated?")
    assert (a, b) == ("sent", "sent")
    fa, fb = (upstream.note_id(src.name, t) for t in ("is the cache stale?", "is the log rotated?"))
    run_dir = _note_run(env, monkeypatch, src)
    words = "won't fix: by design, see note 0123456789abcdef"
    printed = []
    for fp in (fa, fb, fa):
        assert _note(cli, "--reply", fp, words) == 0
        printed.append(re.search(r" as ([0-9a-f]{16})", capsys.readouterr().out).group(1))
    replies = [n for n in _upstream_inbox(env) if n.get("project") == src.name]
    assert [n["reply_to"] for n in replies] == [fa, fb], "the second is not a repeat, the third is a duplicate"
    assert printed == [replies[0]["fp"], replies[1]["fp"], replies[0]["fp"]] and printed[0] != printed[1]
    assert all(n["spec"] == f"[reply to note {n['reply_to']}] {words}" for n in replies)
    assert replies[0]["fp"] == upstream.note_id("second", words, fa) != upstream.note_id("second", words)
    # A plain note: no mark, its id as before.
    assert _note(cli, "--to", "second", words) == 0
    plain = _upstream_inbox(env)[-1]
    assert plain["spec"] == words and "reply_to" not in plain and plain["fp"] == upstream.fingerprint("to:second", words)
    # The project answered reads each reply linked to its note, also through a forwarded line's check.
    assert upstream.ingest(dst, dst.config()) == 3
    texts = [r["text"] for r in dst.db.q("SELECT text FROM events WHERE kind='upstream_note' ORDER BY id")]
    assert f"[reply to note {fa}] {words}" in texts[0] and f"[reply to note {fb}] {words}" in texts[1]
    assert upstream._valid(replies[1])["reply_to"] == fb and "reply_to" not in upstream._valid(plain)
    # A read-only sandbox: the run's queue keeps the target, and the daemon files each reply linked.
    def ro(*_a, **_k):
        raise PermissionError(30, "Read-only file system")
    words2 = "closing as answered"
    printed, _ = [], capsys.readouterr()
    with monkeypatch.context() as m:
        m.setattr(upstream, "send_or_queue", ro)
        for fp in (fa, fb, fb):
            assert _note(cli, "--reply", fp, words2) == 0
            printed.append(re.search(r" as ([0-9a-f]{16})", capsys.readouterr().out).group(1))
    rows = [json.loads(x) for x in (run_dir / upstream.QUEUED_FILE).read_text().splitlines()]
    assert [r["reply_to"] for r in rows] == [fa, fb, fb] and len(set(printed)) == 2
    # A row an older version queued, without the mark, is still filed as it was.
    with open(run_dir / upstream.QUEUED_FILE, "a") as f:
        f.write(json.dumps({"to": "second", "text": "an old queued note", "severity": "normal"}) + "\n")
    got = upstream.send_queued(run_dir, src.name, 57, src.state)
    assert (got["sent"], got["duplicate"], got["invalid"]) == (3, 1, 0)
    filed = {n["fp"]: n for n in _upstream_inbox(env)}
    assert [filed[fp].get("reply_to") for fp in printed] == [fa, fb, fb]
    assert filed[upstream.fingerprint("to:second", "an old queued note")]["spec"] == "an old queued note"
    # The outbox keeps the target past the hourly limit, and draining it files each reply linked.
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 200 + i, "second", f"filler {i}", now=now)
    for fp in (fa, fb, fa):
        assert upstream.send_or_queue(src.state, src.name, 300, "second", "one more thing", now=now, reply_to=fp)[0] == "queued"
    assert [n["reply_to"] for n in upstream._outbox(src.state)] == [fa, fb]
    assert upstream.drain_outbox(src.state, src.name, now + 7200)[0] == 2
    assert {n["reply_to"] for n in _upstream_inbox(env) if n["spec"].endswith("one more thing")} == {fa, fb}


def test_a_reply_mark_is_not_a_named_note_and_never_hides_a_placeholder():
    """The mark names the note answered, not a note the text is about: a reply naming no note is no
    repeat of the task's last reply to that note (`named_notes`), and a reply that is only a template
    placeholder is still refused. A fingerprint that is not 16 hex marks nothing."""
    from ttp import upstream
    fp = "0123456789abcdef"
    marked = upstream.reply_text("thanks", fp)
    assert marked == f"[reply to note {fp}] thanks" and upstream.reply_text(marked, fp) == marked
    assert upstream.reply_of(marked) == fp and upstream.named_notes(marked) == frozenset()
    assert upstream.reply_text("thanks", "../x") == "thanks" and upstream.reply_of("thanks") is None
    for bad in ('"<text>"', "<reply>"):
        with pytest.raises(upstream.NotePlaceholder):
            upstream.check_placeholder(upstream.reply_text(bad, fp))


def test_ack_wait_cursor_skips_consumed_replies_and_terminal_waits_past_acknowledgements(env, monkeypatch, capsys,
                                                                                         tmp_path):
    # An interim reply taken once never counts again (--after), and --terminal passes only on a reply that
    # reads as a result (done, failed, a prerequisite), never on an acknowledgement or a plan.
    from ttp import ackwait, cli, upstream
    from ttp.project import register
    p = make(env)
    (env["tmp"] / "peer").mkdir()
    register("peer", {"host": "testhost", "dir": str(env["tmp"] / "peer")})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    request = "please run the canary and report its result"
    assert upstream.send(p.name, 4, "peer", request) == "sent"
    fp = upstream.note_id("peer", request)
    upstream.send("peer", 1, p.name, f"re {fp}: acknowledged, the canary is queued and a result follows later")
    interim = upstream.note_id(p.name, f"re {fp}: acknowledged, the canary is queued and a result follows later")
    probe = ["probe", "--from", "peer", "--hash", fp]
    assert _ack_wait(cli, *probe) == 0, "without --terminal any reply still counts, as before"
    assert _ack_wait(cli, *probe, "--terminal") == 1, "an acknowledgement is no terminal result"
    assert "only acknowledge or plan" in capsys.readouterr().out
    assert _ack_wait(cli, *probe, "--after", interim) == 1, "the consumed interim reply never counts again"
    assert upstream.send("peer", 2, p.name, f"re {fp}: I will start the canary soon") == "sent"
    assert _ack_wait(cli, *probe, "--after", interim, "--terminal") == 1
    result = f"re {fp}: canary failed on its prerequisite check; status: failed"
    assert upstream.send("peer", 3, p.name, result) == "sent"
    assert _ack_wait(cli, *probe, "--after", interim, "--terminal") == 0
    out = tmp_path / "result.json"
    monkeypatch.setenv("TTP_RESULT", str(out))
    cli.main(["ack-wait", "settle", "--from", "peer", "--hash", fp, "--after", interim, "--terminal"])
    got = json.loads(out.read_text())
    assert got["metrics"]["reply_note"] == upstream.note_id(p.name, result) and "status: failed" in got["summary"]
    assert ackwait.find(p.name, "peer", fp, [upstream.note_id(p.name, result)]) is None, "the cursor drops earlier ones"
    for text, want in (("status: done", True), ("done; the canary passed", True), ("Status: queued, done later", False),
                       ("received, will do", False), ("blocked on a prerequisite: the pinned image", True),
                       ("thanks", False), ("result = pending; prior run failed", False)):
        assert ackwait.terminal(text) is want, text
    # The sender's records ride along as a [facts at send time ...] block: only its own words decide.
    facts = " [facts at send time from peer's records: #12 'canary' status: done, landed 1a2b3c4 | #13 failed]"
    assert ackwait.terminal("re abc: queued, a result follows later" + facts) is False
    assert ackwait.terminal("re abc: canary done; status: done" + facts) is True
    assert ackwait.terminal("re abc: status: failed, the image is gone" + facts) is True
    final = f"re {fp}: the canary passed on the rerun; status: done{facts}"
    later = f"re {fp}: acknowledged, rerunning the canary now{facts}"
    assert upstream.send("peer", 5, p.name, later) == "sent"
    after = [interim, upstream.note_id(p.name, result)]
    after_args = [x for a in after for x in ("--after", a)]
    assert _ack_wait(cli, *probe, *after_args, "--terminal") == 1, "an interim reply with facts still waits"
    assert upstream.send("peer", 6, p.name, final) == "sent"
    assert _ack_wait(cli, *probe, *after_args, "--terminal") == 0, "a final reply with facts is terminal"


def test_ack_wait_terminal_ignores_pending_words_in_appended_facts():
    """A terminal disposition with no status line of its own stays terminal when the appended peer-task records
    carry pending words (queued, work, started); an explicit interim note stays interim with or without them."""
    from ttp import ackwait, taskfacts
    facts = (f"{taskfacts.FACTS_MARK}owner's records: #159 'repair' work task, queued, cancelled | "
             "#165 code task, started; next step pending]")
    terminal = "re 3c59: the shipped release fixes it; the repair landed and the behaviour check passed"
    interim = "re 3c59: acknowledged, the repair is queued and a result follows later"
    for sep in ("", "\n", "  "):          # whatever whitespace a transport left before the block
        block = sep + facts.lstrip() if sep else facts
        assert ackwait.terminal(terminal + block) is True, repr(sep)
        assert ackwait.terminal(interim + block) is False, repr(sep)
    assert ackwait.terminal(terminal) is True and ackwait.terminal(interim) is False
    # the facts block's own status words decide nothing either way
    assert ackwait.terminal("re 3c59: still working on it" + facts.replace("queued", "status: done")) is False
    assert ackwait.own_words(terminal + facts) == terminal


def test_ack_wait_terminal_reads_a_multi_part_reply_as_one_and_still_waits_past_a_multi_part_plan(env, monkeypatch,
                                                                                                  capsys, tmp_path):
    """A terminal answer sent as `[k/N]` parts counts once every part is in, read as one: a finished task in the
    sender's records backs it, so evidence words ("started", "queued") in a part do not hold the wait. A plan or an
    acknowledgement sent in parts still waits, even beside a done task. A later part joins its packet by the
    sender's task and N without naming the note; a part from another sender task does not."""
    from ttp import ackwait, cli, taskfacts, upstream
    from ttp.project import register
    p = make(env)
    (env["tmp"] / "peer").mkdir()
    register("peer", {"host": "testhost", "dir": str(env["tmp"] / "peer")})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    request = "please send the evidence packet for the four cells"
    assert upstream.send(p.name, 4, "peer", request) == "sent"
    fp = upstream.note_id("peer", request)
    probe = ["probe", "--from", "peer", "--hash", fp, "--terminal"]

    def facts(*lines):
        return f"{taskfacts.FACTS_MARK}peer's records: " + " | ".join(lines) + "]"

    def send(task, text):
        assert upstream.send("peer", task, p.name, text) == "sent"
        return upstream.note_id(p.name, text)

    send(7, f"[reply to note {fp}] [1/2] Acknowledged; the plan is to rerun the four cells."
            + facts("#3 Earlier report (work, done)"))
    send(7, "[2/2] Next step: the packet follows once the reruns finish.")
    assert _ack_wait(cli, *probe) == 1, "a plan in parts is no terminal result, a done task beside it or not"
    assert "only acknowledge or plan" in capsys.readouterr().out
    parts = [f"[reply to note {fp}] [1/3] Evidence packet for the four cells, from saved records only."
             + facts("#21 Report check (work, done)"),
             "[2/3] Each cell: job started 09:10 and done 09:40, 0 errors.",
             "[3/3] Missing: decoded duration, absent in all four cells; closing it needs new runs (owned by #22)."
             + facts("#22 Rerun cells (work, queued)")]
    assert not any(ackwait.terminal(t) for t in parts), "no part alone reads as terminal"
    ids = [send(8, parts[0])]
    assert _ack_wait(cli, *probe) == 1, "one part of three waits"
    assert "in 3 parts has 1 in so far" in capsys.readouterr().out
    ids.append(send(8, parts[1]))
    send(9, "[3/3] Unrelated: the nightly sweep has no new cells.")
    assert _ack_wait(cli, *probe) == 1, "a part from another sender task completes nothing"
    ids.append(send(8, parts[2]))
    assert _ack_wait(cli, *probe) == 0, "the complete packet, backed by a done task, is terminal"
    out = tmp_path / "result.json"
    monkeypatch.setenv("TTP_RESULT", str(out))
    cli.main(["ack-wait", "settle", "--from", "peer", "--hash", fp, "--terminal"])
    got = json.loads(out.read_text())
    assert got["metrics"]["reply_note"] == ids[-1] and got["metrics"]["reply_parts"] == ids
    assert "[1/3] Evidence packet" in got["summary"] and "[3/3] Missing" in got["summary"]
    assert ackwait.find(p.name, "peer", fp, [ids[-1]], True) is None, "the cursor consumes the whole packet"
    # unbacked, a packet needs terminal words and no pending ones, as a single note does
    assert ackwait.packet_terminal(["[1/2] The canary ran.", "[2/2] It passed on both hosts."]) is True
    assert ackwait.packet_terminal(["[1/2] The canary started.", "[2/2] Half the cells passed."]) is False
    assert ackwait.packet_terminal(["[1/2] status: queued", "[2/2] passed" + facts("#5 x (work, done)")]) is False
    assert ackwait.packet_terminal(["[1/2] Queued the reruns.", "[2/2] Started cell 1." + facts("#5 x (work, done)")]) \
        is False, "pending words alone wait beside a done task"
    assert ackwait.packet_terminal(["[1/2] The canary started.", "[2/2] It passed." + facts("#5 x (work, done)")]) is True
    for text, want in (("[2/3] x", (2, 3)), ("(1/2) x", (1, 2)), ("Part 2 of 3: x", (2, 3)), ("part 1/2 x", (1, 2)),
                       ("3/4 cells passed", None), ("[1/1] x", None), ("[4/3] x", None),
                       (f"[reply to note {fp}] [1/3] x", (1, 3))):
        assert ackwait.part(text) == want, text


def test_ack_wait_terminal_counts_a_reply_closing_the_request_without_a_run_but_never_as_done(env, monkeypatch, capsys,
                                                                                             tmp_path):
    # A peer that closes or declines the request without a run has answered: --terminal passes on it, whatever
    # "will" it adds, and settle records it as needs_review marked closed, never as done.
    from ttp import ackwait, cli, upstream
    from ttp.project import register
    p = make(env)
    (env["tmp"] / "peer").mkdir()
    register("peer", {"host": "testhost", "dir": str(env["tmp"] / "peer")})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    request = "please time the warm start and report it"
    assert upstream.send(p.name, 4, "peer", request) == "sent"
    fp = upstream.note_id("peer", request)
    reply = f"re {fp}: closing this timing request without a run; the cache it measures is gone, so I will not rearm it"
    assert upstream.send("peer", 1, p.name, reply) == "sent"
    probe = ["probe", "--from", "peer", "--hash", fp, "--terminal"]
    assert _ack_wait(cli, *probe) == 0, capsys.readouterr().out
    out = tmp_path / "result.json"
    monkeypatch.setenv("TTP_RESULT", str(out))
    for status in ("done", "needs_review"):
        cli.main(["ack-wait", "settle", "--from", "peer", "--hash", fp, "--terminal", "--status", status])
        got = json.loads(out.read_text())
        assert got["status"] == "needs_review" and got["metrics"]["reply_closed"] is True, (status, got)
        assert "closed without a result" in got["summary"] and "without a run" in got["summary"]
    for text in ("Declined the request: no budget for it.", "status: withdrawn",
                 "The request was closed without a result.",
                 "Withdrawing your request: the fix landed elsewhere."):
        assert ackwait.terminal(text) is True and ackwait.closed(text), text
    assert ackwait.packet_terminal(["[1/2] Acknowledged.", "[2/2] Closing your request without a run."]) is True
    for text in ("received, will run it soon", "I will close the request once the run is in", "status: queued",
                 "status: in progress; closing this request needs a run first, a result follows later",
                 "I am not closing your request yet; a run follows.", "I won't be closing the request.",
                 "Closing this request needs a run first; the result follows later.",
                 "I am not declining the request, the run is queued and a result will follow."):
        assert ackwait.terminal(text) is False and not ackwait.closed(text), text
    # a real result that mentions closing is a result, settled as asked
    for text in ("Done: the timing passed, 41 s; closing the request.",
                 "Finished the run and closed your request: 41 s.", "Landed in PR 5, which closes the request."):
        assert ackwait.terminal(text) is True and not ackwait.closed(text), text
        assert ackwait.handoff({"spec": text, "fp": "c" * 16}, "peer", "abc")["status"] == "done", text
    # a close counts only said outright: no negation anywhere earlier in its sentence, no close beside a result
    for text in ("I am not going to be closing your request.", "I am not, as you asked, closing the request.",
                 "No reason for closing the request; I will rerun it tomorrow.",
                 "Closing the request after tomorrow's run.", "Measured 41 s on the new cache; closing the request.",
                 "Fixed in PR 5, which closes the request.", "Merged PR 5 and closed the request: 41 s.",
                 "The timing is 41 s; request closed.", "Done: 41 s, closing the request; I declined the extra rerun.",
                 "Instead of declining your request, I will rerun it tomorrow.",
                 "Rather than closing the request without a run, I will rerun it tomorrow.",
                 "Closing this request without a run? Not at all; I will measure it tomorrow.",
                 "Withdrawing the request would be wrong; I will rerun it tomorrow.",
                 "Passed: 41 s. I declined your second rerun request.",
                 "Done in 41 s, measured without a result cache; closing the request.",
                 "Closing your request without a run is not an option; measuring it now.",
                 "Closing the request without a run makes no sense, so I am running it now.",
                 "Declining the request is not needed; the run is going now.",
                 "Closing this request without a run? No. Measuring it now.",
                 "Withdrawing my request was a mistake, the run is going now.",
                 "Passed: 41 s. I declined your rerun request.",
                 "Timing passed at 41 s; declined the rerun request since the numbers are stable.",
                 "Timing passed: 41 s.\nDeclined the rerun request, the numbers are stable.",
                 "Closing this request without a run?\nNot really. Measuring it today.",
                 "[1/2] Timing passed: 41 s.\n[2/2] Declined the rerun request, the numbers are stable."):
        assert not ackwait.closed(text), text
    # the status line decides, as for any reply: a result naming something closed is still done
    assert ackwait.terminal("status: done; the old request is closed") is True
    assert not ackwait.closed("status: done; the old request is closed")
    # a packet whose first part carries the sender's records still reads its later parts' own words
    records = " [facts at send time from peer's records: #12 t (work, done)]"
    packet = ackwait._joined([{"spec": "[1/2] re #12 timing" + records, "fp": "a" * 16}, {"spec": "[2/2] Closing your request without a run.", "fp": "b" * 16}])
    assert ackwait.handoff(packet, "peer", "abc")["status"] == "needs_review"
    done = {"spec": "re abc: the timing passed, 41 s", "fp": "f" * 16}
    assert ackwait.handoff(done, "peer", "abc")["status"] == "done", "a real result still settles as asked"
    assert "reply_closed" not in ackwait.handoff(done, "peer", "abc")["metrics"]


def test_a_note_this_machine_only_sends_on_is_kept_handled_and_an_addressed_one_wakes(env, monkeypatch):
    """A note addressed to the project that names another machine (`at`) is only sent on from here: kept
    as a handled event (no coordinator wake) and still forwarded. One addressed to it with no `at` wakes
    it, and the digest line says who sent it to whom."""
    from ttp import upstream
    p = make(env)
    fake = _fake_forward_ssh(env, monkeypatch)
    upstream.set_forward_to(["farbox"])
    assert upstream.send("alpha", 3, p.name, "a lesson for the far copy", at="farbox") == "sent"
    assert upstream.send("alpha", 4, p.name, "a lesson for this project") == "sent"
    passing, mine = _upstream_inbox(env)
    assert upstream.ingest(p, {}) == 2
    rows = p.db.q("SELECT status, severity, text, data FROM events WHERE kind='upstream_note' ORDER BY id")
    assert [r["status"] for r in rows] == ["handled", "queued"]
    assert rows[0]["text"].startswith(f"kept handled, sent on to farbox, where {p.name} runs: ")
    assert f"[note {mine['fp']}]" in rows[1]["text"]
    upstream.forward(time.time())
    assert [x["fp"] for x in _remote_inbox(fake, "farbox")] == [passing["fp"]], "still forwarded"
    queued = p.db.q("SELECT kind, data FROM events WHERE status='queued'")
    line = upstream.digest_line(p, {}, queued)
    assert f"{mine['fp']} from a worker of alpha on {upstream.project.hostname()} to this project" in line
    assert passing["fp"] not in line


def test_a_general_note_another_machines_reader_gets_is_a_handled_copy_for_a_reader_here(env, monkeypatch):
    """A project reading this machine's inbox (`upstream.ingest`) while this machine's general notes go
    to a machine where another project reads the inbox keeps its own machine's notes as handled copies;
    one received from elsewhere (`via`) is its own to read and wakes it."""
    from ttp import upstream
    p = make(env)
    cfg = {"upstream": {"ingest": True}}
    now = time.time()
    upstream.project.write_json(upstream.forward_path(),
                                {"targets": {"farbox": {"reader": "beta", "last_ok": now, "probed": now}}})
    upstream.append("alpha", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    got = {**_pushed_note(None, "another lesson", host="laptop"), "title": "upstream: another",
           "fp": upstream.fingerprint("upstream: another", "another lesson")}
    got.pop("to")
    ack, rc = upstream.receive((json.dumps(got) + "\n").encode(), "laptop")
    assert (rc, ack["accepted"]) == (0, 1)
    assert upstream.ingest(p, cfg, now) == 2
    rows = p.db.q("SELECT kind, status, text, data FROM events WHERE kind='upstream_note' ORDER BY id")
    assert [r["status"] for r in rows] == ["handled", "queued"]
    assert "a copy: sent on to farbox, where project beta reads the user's upstream inbox" in rows[0]["text"]
    line = upstream.digest_line(p, cfg, [rows[1]])
    assert "from a worker of alpha on laptop (sent on from laptop) to the upstream inbox (general)" in line


def test_high_severity_notes_file_at_once_ahead_of_the_queue_on_their_own_allowance(env, monkeypatch):
    """A high note files at once when the normal limit is full and others wait, goes ahead of queued normal
    ones when it must wait itself, has an hourly allowance of its own, and leaves normal notes as they were."""
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 1, "second", f"filler {i}", now=now)
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "normal a", now=now) == ("queued", 1)
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "low b", "low", now=now) == ("queued", 2)
    # Filed at once, behind nothing, though the normal limit is full and two notes wait.
    assert upstream.send_or_queue(src.state, src.name, 3, "second", "hold now", "high", now=now) == ("sent", 0)
    hold = upstream.note_id("second", "hold now")
    (n,) = [n for n in upstream._lines(upstream.path().read_bytes())[0] if n["fp"] == hold]
    assert n["severity"] == "high" and n["lane"] == "high"
    assert upstream.high_used(src.name, now) == 1
    assert "high-severity allowance" in upstream.note_status(hold)[1]
    # Its allowance is limited too: the rest wait, ahead of the normal ones, and keep their order.
    for i in range(1, upstream.HIGH_NOTES_PER_HOUR):
        assert upstream.send_or_queue(src.state, src.name, 3, "second", f"hold {i}", "high", now=now)[0] == "sent"
    assert upstream.send_or_queue(src.state, src.name, 3, "second", "hold late 1", "high", now=now) == ("queued", 1)
    assert upstream.send_or_queue(src.state, src.name, 3, "second", "hold late 2", "high", now=now) == ("queued", 2)
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "normal c", now=now) == ("queued", 5)
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "normal a", "high", now=now) == ("queued", 1)   # raised in place: first in, so first out of the high ones
    late = upstream.note_id("second", "hold late 1")
    assert "ahead of the normal notes" in upstream._queued(late, src.state, None)
    # An hour on, the high ones file first, then the normal ones in their order (the limit is 10).
    upstream.drain_outbox(src.state, src.name, now + 3700)
    texts = [n["spec"] for n in upstream._lines(upstream.path().read_bytes())[0] if n["to"] == "second"
             and n["ts"] > now + 3000]
    assert texts[:3] == ["normal a", "hold late 1", "hold late 2"]
    assert texts[3:] == ["low b", "normal c"]
    assert not upstream._outbox(src.state)


def test_a_full_high_allowance_does_not_hold_normal_notes_and_normal_limits_are_unchanged(env, monkeypatch):
    from ttp import upstream
    src, dst, fp = _confirm_pair(env)
    now = time.time()
    for i in range(upstream.HIGH_NOTES_PER_HOUR):
        assert upstream.send(src.name, 1, "second", f"hold {i}", "high", now=now) == "sent"
    assert upstream.send(src.name, 1, "second", "hold more", "high", now=now) == "limited"
    # The high notes do not use the normal limit.
    for i in range(upstream.NOTES_PER_HOUR):
        assert upstream.send(src.name, 2, "second", f"n {i}", now=now) == "sent"
    assert upstream.send(src.name, 2, "second", "n over", now=now) == "limited"
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "n over", now=now) == ("queued", 1)
    assert upstream.send_or_queue(src.state, src.name, 2, "second", "hold more", "high", now=now) == ("queued", 1)
    # A limited high note does not stop the normal ones from filing once their own limit allows.
    upstream.drain_outbox(src.state, src.name, now + 1800)
    assert upstream.outbox_pending(src.state) == 2
    upstream.drain_outbox(src.state, src.name, now + 3700)
    assert upstream.outbox_pending(src.state) == 0


def test_note_to_says_which_allowance_a_high_note_used(env, monkeypatch, capsys):
    from ttp import cli, upstream
    src, dst, fp = _confirm_pair(env)
    _note_run(env, monkeypatch, src)
    now = time.time()
    for i in range(upstream.NOTES_PER_HOUR):
        upstream.send(src.name, 1, "second", f"filler {i}", now=now)
    capsys.readouterr()
    assert _note(cli, "--to", "second", "--severity", "high", "stop the work") == 0
    out = capsys.readouterr().out
    assert "filed in its inbox" in out and "filed at once on its own allowance (1 of" in out
    assert _note(cli, "--to", "second", "ordinary") == 0
    assert "queued: number 1 in this project's outbox (other projects get at most" in capsys.readouterr().out
    for i in range(upstream.HIGH_NOTES_PER_HOUR - 1):
        assert _note(cli, "--to", "second", "--severity", "high", f"stop {i}") == 0
    capsys.readouterr()
    assert _note(cli, "--to", "second", "--severity", "high", "stop again") == 0
    assert "ahead of the normal notes" in capsys.readouterr().out
    assert _note(cli, "--status", upstream.note_id("second", "stop again")) == 1
    assert "high-severity allowance" in capsys.readouterr().out


def _second(env, name="other"):
    """A second project on this machine, in its own clone of the test repo."""
    from ttp.cli import bootstrap
    from ttp.project import register
    repo2 = env["tmp"] / f"repo-{name}"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    p = bootstrap(repo2, name, "Another project.", "fake")
    register(name, {"host": "testhost", "dir": str(p.root)})
    return p


def _hold_on_window(p, resets_at: float) -> None:
    """`p`'s daemon last saw its core provider's 7-day window stop new work until `resets_at`."""
    p.db.set_kv("gates", {"claude": {"allow_new_work": False, "level": "red",
                                     "reasons": ["7d window at 90% (the project stops at 90%)"],
                                     "numbers": {"plan": [{"window": "7d", "resets_at": resets_at}]}}})


def test_peer_hold_reads_another_projects_pause_and_spend_gate_with_its_resume_time(env):
    """peerhold.state reads a project on this machine read-only: a spend gate holding new work gives its window's
    reset, a pause gives its end, and a project that runs or is not here is never reported held."""
    from ttp import peerhold, projpause
    other = make(env, "other")
    assert peerhold.state(other) == {"held": False, "why": "", "until": None}
    at = time.time() + 30 * 3600
    _hold_on_window(other, at)
    st = peerhold.state(other)
    assert st["held"] and st["until"] == at and "7d window" in st["why"] and "window resets" in st["why"], st
    line = peerhold.line("other", st, "UTC")
    assert line.startswith("other held until ") and "UTC" in line
    from datetime import datetime, timezone
    end = datetime.fromtimestamp(at + 60, timezone.utc).isoformat()
    projpause.pause(other.db, "user", "repairs", "chat", end, time.time(), None)
    other.db.set_kv("paused", True)
    st = peerhold.state(other)
    assert st["held"] and "paused by user: repairs" in st["why"] and abs(st["until"] - (at + 60)) < 1, st
    assert peerhold.of("not-here") is None and peerhold.line("x", None, "UTC") == ""


def test_a_note_to_a_held_project_prints_and_carries_its_resume_time(env, monkeypatch, capsys):
    """`ttp note --to` a project whose spend gate holds it prints when it resumes, and the note's facts suffix
    carries the same line; a project that runs gets neither."""
    from ttp import cli
    src = make(env)
    other = _second(env)
    _note_run(env, monkeypatch, src)
    assert _note(cli, "--to", "other", "can you rerun the bench?") == 0
    assert " held " not in capsys.readouterr().out
    assert "[facts at send time" not in _upstream_inbox(env)[-1]["spec"]
    _hold_on_window(other, time.time() + 30 * 3600)
    assert _note(cli, "--to", "other", "can you rerun the bench now?") == 0
    out = capsys.readouterr().out
    assert "other held until " in out and "waits at least until then" in out, out
    spec = _upstream_inbox(env)[-1]["spec"]
    assert spec.startswith(f"can you rerun the bench now? [facts at send time from {src.name}'s records: other held until ")


def test_a_promise_whose_owner_task_sent_a_note_names_the_held_project_and_flags_an_early_due(env, monkeypatch):
    """The digest's promise line names a project the owner task sent a note to (linked by the inbox's sender task,
    not by wording) while it is held, and flags a due before its resume time unless the promise quotes it."""
    from ttp import cli, obligations, peerhold, timefmt
    src = make(env)
    other = _second(env)
    _note_run(env, monkeypatch, src, task="57")
    assert _note(cli, "--to", "other", "please rerun") == 0
    now = time.time()
    at = now + 30 * 3600
    _hold_on_window(other, at)
    o = {"id": "o1", "text": "bench table by tomorrow", "owners": {"57": {}},
         "promise": {"chat": "c", "message": 1, "due": now + 3600, "timed": True, "tasks": [57]}}
    got = peerhold.promise_note(src.name, o, "UTC", now)
    assert got.startswith("; other held until ") and "DUE BEFORE IT RESUMES" in got, got
    o["text"] = f"bench table once other resumes at {timefmt.short(at, 'UTC', now).split(' ')[-2]}"
    assert "DUE BEFORE" not in peerhold.promise_note(src.name, o, "UTC", now)
    o["promise"]["tasks"], o["owners"] = [58], {}
    assert peerhold.promise_note(src.name, o, "UTC", now) == ""   # task 58 sent no note
    assert obligations._peer_hold("", o, now, "UTC") == ""
