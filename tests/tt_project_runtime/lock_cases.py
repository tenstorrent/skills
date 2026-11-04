# `ttp lock` tests (cmd_lock in cli.py, locks.py) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new lock tests here.
from __future__ import annotations


def _lock_env(env, p, **kw):
    return dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base), **kw)


def _lock(run_env, *args, cmd=("true",)):
    return subprocess.Popen([sys.executable, str(TTP), "lock", *args, "--", *cmd], env=run_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _mark(marks, name, flag=None):
    """A command that appends `start <name>` to marks, runs until `flag` exists (when given), then `end <name>`."""
    wait = f"while [ ! -e {shlex.quote(str(flag))} ]; do {_nap()}; done; " if flag else ""
    return ("bash", "-c", f"echo 'start {name}' >> {shlex.quote(str(marks))}; {wait}"
                        f"echo 'end {name}' >> {shlex.quote(str(marks))}")


def _lock_probe(run_env, *names, extra=()):
    return subprocess.run([sys.executable, str(TTP), "lock", *extra, "--probe", *names], env=run_env,
                          capture_output=True, text=True, timeout=60)


def _queue_len(p, res):
    from ttp import locks, shared
    return len(locks.queued(shared.locks_dir(p, res, p.config()), res))


def _stderr_until(proc, text, timeout=60):
    """The lines proc wrote to stderr up to the first one holding text (all of it if it ended first)."""
    got: list[str] = []

    def _read():
        for line in iter(proc.stderr.readline, ""):
            got.append(line)
            if text in line:
                return
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return "".join(got)


def _ended(procs, timeout=60):
    out = []
    for x in procs:
        try:
            out.append(x.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            for y in procs:
                y.kill()
            raise AssertionError("a ttp lock never finished (a deadlock?)")
    return out


def _until_or_ended(cond, procs, guard=180):
    """Wait for cond() or for one of procs to end, whichever comes first: an event, not a time window, so
    a slow start on a loaded host (seconds before a `ttp lock` takes a free lock) never fails it. One
    ended first fails with its exit code and stderr; `guard` only stops a hang."""
    deadline = time.time() + guard
    while not cond():
        if ended := [x for x in procs if x.poll() is not None]:
            raise AssertionError(f"ended first: {[(x.pid, x.returncode, x.stderr.read()) for x in ended]}")
        if time.time() > deadline:
            raise AssertionError(f"none of pids {[x.pid for x in procs]} got there in {guard} s (a hang?)")
        time.sleep(0.05)


def test_ttp_lock_takes_two_free_resources_and_shows_both_held_by_the_same_holder(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p, TTP_TASK="7")
    marks, go = tmp_path / "marks", tmp_path / "go"
    m = _lock(run_env, "rig-b,rig-a", cmd=_mark(marks, "both", go))
    try:
        assert _until_true(lambda: marks.exists() and "start both" in marks.read_text()), m.stderr.read()
        held = shared.held(p)
        a = [h.split(": ", 1)[1] for h in held if h.startswith("rig-a: ")]
        b = [h.split(": ", 1)[1] for h in held if h.startswith("rig-b: ")]
        assert len(a) == 1 and a == b and "task #7" in a[0], held
        r = _lock_probe(run_env, "rig-a,rig-b", extra=("--json",))
        rows = json.loads(r.stdout)
        assert r.returncode == 75 and [x["resource"] for x in rows] == ["rig-a", "rig-b"], (r.returncode, r.stdout)
        who = [[(h["holder"], h["pid"], h["command"]) for h in x["holders"]] for x in rows]
        assert all(x["state"] == "busy" for x in rows) and who[0] == who[1] != [], rows
    finally:
        go.touch()
    assert m.wait(timeout=60) == 0
    assert marks.read_text().split("\n")[:2] == ["start both", "end both"]
    r = _lock_probe(run_env, "rig-a,rig-b")
    assert r.returncode == 0 and r.stdout.splitlines() == ["rig-a: free", "rig-b: free"], (r.returncode, r.stdout)
    from ttp import activity
    takes = [e for e in activity.entries(p.state) if e.get("ev") == "take"]
    assert sorted(e["resource"] for e in takes) == ["rig-a", "rig-b"] and len({e["pid"] for e in takes}) == 1


def test_a_first_in_line_multi_waiter_holds_nothing_and_is_not_overtaken_by_later_single_waiters(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    marks, go_h = tmp_path / "marks", tmp_path / "go-h"
    h = _lock(run_env, "rig-a", cmd=_mark(marks, "H", go_h))
    procs = [h]
    try:
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        m = _lock(run_env, "rig-a,rig-b", cmd=_mark(marks, "M"))
        procs.append(m)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 1 and _queue_len(p, "rig-b") == 1)
        # It waits for rig-a only, yet keeps rig-b free: no subset is held while it waits.
        time.sleep(0.3)
        assert not [x for x in shared.held(p) if x.startswith("rig-b: ")], shared.held(p)
        assert _lock_probe(run_env, "rig-b").returncode == 75, "its place in rig-b's queue must show to probes"
        sb = _lock(run_env, "rig-b", cmd=_mark(marks, "SB"))
        sa = _lock(run_env, "rig-a", cmd=_mark(marks, "SA"))
        procs += [sb, sa]
        assert _until_true(lambda: _queue_len(p, "rig-a") == 2 and _queue_len(p, "rig-b") == 2)
        time.sleep(0.5)   # ten polls: rig-b is free, but the earlier multi-waiter is first in its queue
        assert "SB" not in marks.read_text() and sb.poll() is None, marks.read_text()
    finally:
        go_h.touch()
    assert _ended(procs) == [0, 0, 0, 0], [x.stderr.read() for x in procs]
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start")]
    assert starts[:2] == ["H", "M"] and set(starts[2:]) == {"SA", "SB"}, starts


def test_lock_probe_of_two_resources_is_busy_when_either_is_held_or_paused(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    held, go = tmp_path / "held", tmp_path / "go"
    h = _lock(run_env, "rig-b", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    try:
        _wait_for_file(held, h)
        r = _lock_probe(run_env, "rig-a,rig-b")
        assert r.returncode == 75 and r.stdout.splitlines()[0] == "rig-a: free" \
            and r.stdout.splitlines()[1].startswith("rig-b: busy (held by"), r.stdout
    finally:
        go.touch()
    assert h.wait(timeout=30) == 0
    assert _lock_probe(run_env, "rig-a,rig-b").returncode == 0
    p.db.set_kv("paused_resources", {"rig-a": {"reason": "maintenance", "since": time.time()}})
    r = _lock_probe(run_env, "rig-b,rig-a")
    assert r.returncode == 75 and "rig-a: paused (maintenance)" in r.stdout.splitlines(), r.stdout
    # A pause of either refuses the take too, before it queues anywhere.
    t = subprocess.run([sys.executable, str(TTP), "lock", "rig-b,rig-a", "--", "true"], env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert t.returncode == 75 and "rig-a is paused" in t.stderr and _queue_len(p, "rig-b") == 0, t.stderr


def test_two_multi_lockers_in_crossed_order_both_finish_and_never_overlap(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    marks = tmp_path / "marks"
    procs = []
    for i in range(3):
        procs += [_lock(run_env, "rig-a,rig-b", cmd=_mark(marks, f"ab{i}")),
                  _lock(run_env, "rig-b,rig-a", cmd=_mark(marks, f"ba{i}"))]
    assert _ended(procs) == [0] * 6, [x.stderr.read() for x in procs]
    lines = marks.read_text().splitlines()
    assert len(lines) == 12
    assert all(lines[k].split()[0] == "start" and lines[k + 1] == "end " + lines[k].split()[1]
               for k in range(0, 12, 2)), lines


def test_a_multi_lock_honours_its_timeout_and_names_every_resource_in_its_retry_probe(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    held, go = tmp_path / "held", tmp_path / "go"
    h = _lock(run_env, "rig-a", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    try:
        _wait_for_file(held, h)
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b,rig-a", "--", "true"],
                           env=run_env, capture_output=True, text=True, timeout=60)
        assert _queue_len(p, "rig-b") == 0 and _queue_len(p, "rig-a") == 0, "a give-up must leave no ticket"
    finally:
        go.touch()
    assert h.wait(timeout=30) == 0
    assert r.returncode == 75 and "retry_when `ttp lock --probe rig-a,rig-b`" in r.stderr, (r.returncode, r.stderr)


def test_a_multi_lock_keeps_the_device_and_nested_rules(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--shared", "rig-a,board-device", "--", "true"],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "board-device is a device lock" in r.stderr, r.stderr
    assert _lock_probe(run_env, "rig-a,board-device", extra=("--shared",)).returncode == 0
    # Inside a hold of rig-a, a lock of rig-a and rig-b takes rig-b only, and its command sees both held.
    out = tmp_path / "env"
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig-a,rig-b", "--", "sh", "-c",
                        f"echo $TTP_LOCKS_HELD > {shlex.quote(str(out))}"],
                       env={**run_env, "TTP_LOCKS_HELD": "rig-a"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "rig-a is held already by an enclosing ttp lock (exclusive); taking the rest" in r.stderr
    assert out.read_text().split() == ["rig-a,rig-b"]
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig-b,rig-a", "--", "true"],
                       env={**run_env, "TTP_LOCKS_SHARED": "rig-b"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "cannot lock rig-b exclusively inside a shared" in r.stderr, r.stderr


def test_one_resource_lock_runs_its_whole_command_and_probe_parsing_is_unchanged(env, tmp_path):
    from ttp import activity, cli, locks
    assert not hasattr(cli, "_lock_argv"), "argv is parsed as before: several resources are named in one word"
    p = make(env)
    run_env = _lock_env(env, p)
    # A command given without a leading `--` that has an inner one runs whole, under the one lock.
    for args, out in ((["board", "echo", "a", "--", "b"], "a -- b\n"), (["board", "echo", "box", "--", "-r"], "box -- -r\n"),
                      (["--timeout", "5", "board", "--", "echo", "x", "--", "y"], "x -- y\n")):
        r = subprocess.run([sys.executable, str(TTP), "lock", *args], env=run_env, capture_output=True, text=True, timeout=60)
        assert (r.returncode, r.stdout) == (0, out), (args, r.stdout, r.stderr)
        assert sorted({e["resource"] for e in activity.entries(p.state)
                       if e.get("ev") == "take"}) == ["board"], args
    assert locks.lock_probe_parts("ttp lock --probe board") == ([("board", False)], [])
    assert locks.lock_probe_parts("ttp lock --probe --shared a,b && make") == ([("a", True), ("b", True)], ["make"])
    assert locks.probe_resources("ttp lock --probe a,b") == ["a", "b"]
    assert locks.probe_resources("ttp lock --probe a b") == [], "a probe with a stray word is no lock probe, as before"


def _nested_lock(flag, res, started):
    """A command that creates `started` (it runs, so its lock is held), waits for `flag`, then runs a
    `ttp lock <res> -- true` inside it."""
    inner = shlex.join([sys.executable, str(TTP), "lock", res, "--", "true"])
    return ("bash", "-c", f": > {shlex.quote(str(started))}; "
                          f"while [ ! -e {shlex.quote(str(flag))} ]; do {_nap()}; done; {inner}")


def test_a_lock_holders_nested_lock_never_waits_behind_a_multi_waiter_for_its_own_lock(env, tmp_path):
    # Bug: a first-in-line `ttp lock rig-a,rig-b` waiter kept rig-b's queue, while rig-a's holder ran
    # `ttp lock rig-b` inside its command: each waited for the other, forever.
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    flag, started = tmp_path / "flag", tmp_path / "started"
    h = _lock(run_env, "--timeout", "0", "rig-a", cmd=_nested_lock(flag, "rig-b", started))
    procs = [h]
    try:
        _until_or_ended(started.exists, procs)   # the holder's command runs: rig-a is held
        assert any(x.startswith("rig-a: ") for x in shared.held(p)), shared.held(p)
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        _until_or_ended(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1, procs)
    finally:
        flag.touch()
    assert _ended(procs, timeout=20) == [0, 0], [x.stderr.read() for x in procs]
    # A waiter that holds nothing still never passes it (test above); only lock holders go past it.
    from ttp import locks
    t = locks.enqueue(tmp_path / "q", "r", "w", multi=True, priority=2, group="g")
    try:
        assert locks.is_multi(t) and locks.ticket_priority(t) == 2 and locks.ticket_group(t) == locks.group_id("g")
        u = locks.enqueue(tmp_path / "q", "r", "u", shared=True)
        assert not locks.is_multi(u) and locks.my_turn(tmp_path / "q", "r", u, 1) is False
        assert locks.my_turn(tmp_path / "q", "r", u, 1, past_multi=True)
        locks.dequeue(u)
    finally:
        locks.dequeue(t)


def test_a_whole_run_holders_nested_lock_never_waits_behind_a_multi_waiter_for_its_lock(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    go, started = tmp_path / "go", tmp_path / "started"
    # stands for the exclusive task's whole-run hold of rig-a; its command marks that it runs under it
    run_hold = _lock(run_env, "rig-a", cmd=("bash", "-c", f": > {shlex.quote(str(started))}; {_until(go)[-1]}"))
    procs = [run_hold]
    try:
        _until_or_ended(started.exists, procs)
        assert any(x.startswith("rig-a: ") for x in shared.held(p)), shared.held(p)
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        _until_or_ended(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1, procs)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({"exclusive": [{"resource": "rig-a"}]}))
        inner = _lock({**run_env, "TTP_RUN_DIR": str(run_dir)}, "--timeout", "0", "rig-b")
        procs.append(inner)
        assert inner.wait(timeout=20) == 0, inner.stderr.read()
    finally:
        go.touch()
    assert _ended(procs, timeout=20) == [0, 0, 0], [x.stderr.read() for x in procs]


def test_one_resource_lock_output_is_unchanged(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    r = _lock_probe(run_env, "board")
    assert (r.returncode, r.stdout, r.stderr) == (0, "board: free\n", "")
    r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c", "echo hi; echo err >&2"],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (0, "hi\n", "err\n")
    r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "true"], env={**run_env, "TTP_LOCKS_HELD": "board"},
                       capture_output=True, text=True, timeout=60)
    assert r.stderr == "ttp lock: board is held already by an enclosing ttp lock (exclusive); running the command under it\n"


def _reserve_for(p, res, task, stale=False):
    """The daemon's reservation of res for task (_resources_free(reserve=True)); its path."""
    from ttp import hold, locks, shared
    mark = locks.reserve_path(shared.locks_dir(p, res, p.config()), res)
    locks.reserve(mark, shared.holder(p, res, hold.who(task), p.config()))
    if stale:
        r = json.loads(mark.read_text())
        mark.write_text(json.dumps({**r, "ts": time.time() - locks.RESERVE_STALE_S - 5}))
    return mark


def test_a_tasks_own_driver_takes_the_slot_its_task_reserved_and_other_tasks_still_wait(env, tmp_path):
    """A queued exclusive task's reservation holds off other tasks' commands, but its own detached driver
    (same TTP_TASK) takes the slot ahead of them, as the task's run would, and clears the reservation."""
    from ttp import locks
    p = make(env)
    marks, go_h, go_d = tmp_path / "marks", tmp_path / "go-h", tmp_path / "go-d"
    h = _lock(_lock_env(env, p, TTP_TASK="1"), "rig-a", cmd=_mark(marks, "H", go_h))
    procs = [h]
    try:
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        mark = _reserve_for(p, "rig-a", 7)
        o = _lock(_lock_env(env, p, TTP_TASK="8"), "rig-a", cmd=_mark(marks, "O"))
        procs.append(o)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 1)
        d = _lock(_lock_env(env, p, TTP_TASK="7"), "rig-a", cmd=_mark(marks, "D", go_d))
        procs.append(d)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 2)
        go_h.touch()
        assert _until_true(lambda: "start D" in marks.read_text()), (marks.read_text(), d.stderr)
        assert locks.reserved_by(mark) is None, "the driver left its task's reservation behind"
        time.sleep(0.3)
        assert "start O" not in marks.read_text() and o.poll() is None, marks.read_text()
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK="9"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        _reserve_for(p, "rig-b", 7)
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK="9"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 75 and "reserved for task #7" in r.stderr, (r.returncode, r.stderr)
    finally:
        go_h.touch()
        go_d.touch()
    assert _ended(procs) == [0, 0, 0], [x.stderr.read() for x in procs]
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start")]
    assert starts == ["H", "D", "O"], starts


def test_a_stale_reservation_for_the_drivers_own_task_behaves_as_before(env, tmp_path):
    from ttp import locks
    p = make(env)
    mark = _reserve_for(p, "rig-a", 7, stale=True)
    before = mark.read_text()
    for task in ("7", "8"):
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "rig-a", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK=task), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (task, r.stderr)
    assert mark.read_text() == before and locks.reserved_by(mark) is None, "a stale reservation was touched"


def test_a_multi_lock_passes_only_its_own_tasks_reservations_all_or_nothing(env, tmp_path):
    from ttp import locks, shared
    p = make(env)
    mark = _reserve_for(p, "rig-a", 7)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="8"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "reserved for task #7" in r.stderr, (r.returncode, r.stderr)
    other = _reserve_for(p, "rig-b", 9)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="7"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "rig-b (reserved for task #9)" in r.stderr, (r.returncode, r.stderr)
    assert "rig-a (reserved" not in r.stderr and locks.reserved_by(mark) == "task #7", r.stderr
    assert not shared.held(p) or all("reserved for" in x for x in shared.held(p)), "a subset was held"
    other.unlink()
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "5", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="7"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and locks.reserved_by(mark) is None, (r.returncode, r.stderr)


def _pre_grant_hook(p, tmp_path, rc=75, timeout_s=None):
    """Set config locks.pre_grant_check to a script that logs its arguments, env and cwd to `log` and
    exits with the code in `verdict` (rewritable); (verdict, log)."""
    verdict, log = tmp_path / "verdict", tmp_path / "hook.log"
    verdict.write_text(str(rc))
    hook = tmp_path / "pre-grant.sh"
    hook.write_text(f'#!/bin/sh\necho "$1 $2 $TTP_RESOURCE $TTP_TASK_ID $PWD" >> {shlex.quote(str(log))}\n'
                    f'echo "repair hold for task 9"\nexit "$(cat {shlex.quote(str(verdict))})"\n')
    hook.chmod(0o755)
    p.set_config("locks.pre_grant_check", shlex.quote(str(hook)))
    if timeout_s is not None:
        p.set_config("locks.pre_grant_timeout_s", timeout_s)
    return verdict, log


def test_pre_grant_check_holds_ttp_lock_and_its_task_probe_until_it_grants(env, tmp_path):
    p = make(env)
    verdict, log = _pre_grant_hook(p, tmp_path)
    run_env = _lock_env(env, p, TTP_TASK="7")
    ran = tmp_path / "ran"
    for rc in (75, 1):
        verdict.write_text(str(rc))
        t = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "touch", str(ran)], env=run_env,
                           capture_output=True, text=True, timeout=60)
        assert t.returncode == 75 and not ran.exists() and _queue_len(p, "rig") == 0, t.stderr
        assert len(t.stderr.strip().splitlines()) == 1, t.stderr
        assert f"held by the project's pre-grant check (exit {rc}): repair hold for task 9" in t.stderr, t.stderr
        assert "`ttp lock --probe rig --task 7`" in t.stderr, t.stderr
    # Its resource and task as arguments and env, run from the project root.
    assert log.read_text().splitlines()[0] == f"rig 7 rig 7 {p.root}", log.read_text()
    from ttp import locks, shared
    assert locks.any_free(locks.slot_paths(shared.locks_dir(p, "rig", p.config()), "rig", 1))
    # The task's probe stays busy while the check holds; a probe for no task does not ask it.
    no_task = _lock_env(env, p)
    assert _lock_probe(no_task, "rig", extra=("--task", "7")).returncode == 75
    j = _lock_probe(no_task, "rig", extra=("--task", "7", "--json"))
    assert j.returncode == 75 and json.loads(j.stdout)["free"] is False and "pre-grant" in json.loads(j.stdout)["line"]
    asked = len(log.read_text().splitlines())
    assert _lock_probe(no_task, "rig").returncode == 0 and len(log.read_text().splitlines()) == asked
    verdict.write_text("0")
    assert _lock_probe(no_task, "rig", extra=("--task", "7")).returncode == 0
    t = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "touch", str(ran)], env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert t.returncode == 0 and ran.exists(), t.stderr


def test_a_broken_or_slow_pre_grant_check_holds_and_is_raised_once(env, tmp_path):
    from ttp import pregrant
    from ttp.daemon import Daemon
    p = make(env)
    verdict, _ = _pre_grant_hook(p, tmp_path, rc=3)
    run_env = _lock_env(env, p, TTP_TASK="7")
    for _ in range(3):   # every retry is held; the fault is recorded once per resource and exit
        t = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "true"], env=run_env,
                           capture_output=True, text=True, timeout=60)
        assert t.returncode == 75 and "pre-grant check is broken (exited 3)" in t.stderr, t.stderr
    assert len(list((p.state / pregrant.DIR).glob("*.json"))) == 1
    d = Daemon(p.base)
    obs = lambda: p.db.q("SELECT text FROM events WHERE source='daemon:pre-grant'")
    d.raise_pre_grant_faults()
    assert len(obs()) == 1 and "on rig for task #7 exited 3" in obs()[0]["text"], obs()
    assert "locks.pre_grant_check" in obs()[0]["text"]
    subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "true"], env=run_env, capture_output=True, timeout=60)
    d.raise_pre_grant_faults()
    assert len(obs()) == 1, obs()
    # No answer in time is broken too: held, its process group killed, recorded with the timeout.
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    t0 = time.time()
    got = pregrant.check(str(slow), p.root, "rig", 7, p.state, timeout=0.5)
    assert not got.granted and got.broken and got.rc is None and time.time() - t0 < 10, got
    d.raise_pre_grant_faults()
    assert len(obs()) == 2 and "no answer within 0.5 s" in obs()[-1]["text"], obs()
    # A shell that cannot run the command at all (exit 127) is broken, never a grant.
    assert pregrant.check("/nonexistent/check", p.root, "rig", 7).broken


def test_dispatch_holds_a_task_while_the_pre_grant_check_holds(env, tmp_path, monkeypatch):
    from ttp import pregrant
    from ttp import daemon as dmod
    p = make(env)
    verdict, log = _pre_grant_hook(p, tmp_path)
    d = dmod.Daemon(p.base)
    ex = p.db.add_task("Whole run", "s", kind="work", tier="light", origin="coordinator", labels=["exclusive:rig"])
    per = p.db.add_task("Per command", "s", kind="work", tier="light", origin="coordinator", labels=["resource:rig"])
    for tid in (ex, per):
        rec = d._resource_hold(p.db.task(tid))
        assert rec["kind"] == "pre_grant" and rec["resource"] == "rig" and rec["rc"] == 75, rec
        assert dmod.hold_reason(rec).startswith("rig: held by the project's pre-grant check (exit 75)")
    asked = len(log.read_text().splitlines())
    assert not d._resources_free(p.db.task(ex)) and len(log.read_text().splitlines()) == asked   # reused, not re-run
    verdict.write_text("0")
    later = time.time() + dmod.PRE_GRANT_REUSE_S + 1
    monkeypatch.setattr(dmod.time, "time", lambda: later)   # past the reuse window
    assert d._resource_hold(p.db.task(ex)) is None and d._resource_hold(p.db.task(per)) is None
    # A busy slot holds before the check is asked.
    d._pre_grant_seen.clear()
    busy = p.db.add_task("Holder", "s", kind="work", tier="light", origin="coordinator", labels=["exclusive:rig"])
    p.db.update_task(busy, status="running")
    asked = len(log.read_text().splitlines())
    assert d._resource_hold(p.db.task(ex))["kind"] == "busy" and len(log.read_text().splitlines()) == asked


def test_with_no_pre_grant_check_no_grant_path_runs_one(env, tmp_path, monkeypatch):
    from ttp import pregrant, runner
    from ttp import daemon as dmod
    p = make(env)
    assert pregrant.command(p.config()) == "" and p.config()["locks"]["pre_grant_check"] == ""

    def boom(*a, **k):
        raise AssertionError("the pre-grant check ran with config locks.pre_grant_check unset")
    monkeypatch.setattr(pregrant, "check", boom)
    d = dmod.Daemon(p.base)
    tid = p.db.add_task("Whole run", "s", kind="work", tier="light", origin="coordinator", labels=["exclusive:rig"])
    assert d._resource_hold(p.db.task(tid)) is None
    slot = tmp_path / "rig.0.lock"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    held = runner._take_exclusive(run_dir, [{"resource": "rig", "paths": [str(slot)]}], {"TTP_TASK": "5"},
                                  time.time() + 5)
    assert held and len(held) == 1
    held[0].close()
    run_env = _lock_env(env, p, TTP_TASK="7")
    assert subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "true"], env=run_env,
                          timeout=60).returncode == 0


def test_the_run_supervisor_takes_no_slot_the_pre_grant_check_holds(env, tmp_path, monkeypatch):
    """The check flipped between dispatch and the run's take: the run ends resource_busy without the
    slot, and the task goes back to the queue with no attempt spent."""
    from ttp import coordinator as coord, locks, runner
    from ttp import daemon as dmod
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")
    p = make(env)
    verdict, log = _pre_grant_hook(p, tmp_path)
    slot = tmp_path / "rig.0.lock"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pg = {"cmd": p.config()["locks"]["pre_grant_check"], "root": str(p.root), "state": str(p.state)}
    want = [{"resource": "rig", "paths": [str(slot)]}]
    assert runner._take_exclusive(run_dir, want, {"TTP_TASK": "5"}, time.time() + 5, pre_grant=pg) is None
    assert locks.any_free([slot]) and "rig: held by the project's pre-grant check" in (run_dir / "progress.md").read_text()
    assert log.read_text().split()[:4] == ["rig", "5", "rig", "5"]
    verdict.write_text("0")
    held = runner._take_exclusive(run_dir, want, {"TTP_TASK": "5"}, time.time() + 5, pre_grant=pg)
    assert held and not locks.any_free([slot])
    held[0].close()
    # End to end: dispatch saw a grant, the run's own take is held.
    verdict.write_text("75")
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["rig"], "exclusive": True}]) == []
    tid = p.db.one("SELECT id FROM tasks WHERE title='reflash'")["id"]
    p.db.x("UPDATE messages SET handled=1")
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(dmod.Daemon, "_pre_grant", lambda self, *a: dmod.pregrant.Answer(True, 0, False, "granted"))
    assert _run_until(d, p, lambda: p.db.q("SELECT id FROM runs WHERE task=? AND status!='running'", (tid,)))
    run, t = p.db.one("SELECT * FROM runs WHERE task=?", (tid,)), p.db.task(tid)
    assert run["status"] == "resource_busy", dict(run)
    assert t["status"] == "queued" and not t["attempts"] and t["not_before"], dict(t)
    assert f"rig {tid} rig {tid}" in log.read_text(), log.read_text()


def test_a_waiting_ttp_lock_asks_the_pre_grant_check_again_when_it_takes_the_lock(env, tmp_path):
    """A hold that began while a `ttp lock` waited in the queue: the waiter takes the freed slot, asks
    again, lets it go and exits 75 without running its command."""
    from ttp import locks, shared
    p = make(env)
    verdict, log = _pre_grant_hook(p, tmp_path, rc=0)
    env6, env7 = _lock_env(env, p, TTP_TASK="6", TTP_TEST_POLL_S="0.05"), _lock_env(env, p, TTP_TASK="7", TTP_TEST_POLL_S="0.05")
    marks, go, ran = tmp_path / "marks", tmp_path / "go", tmp_path / "ran"
    holder = _lock(env6, "rig", cmd=_mark(marks, "holder", go))
    waiter = None
    try:
        assert _until_true(lambda: marks.exists() and "start holder" in marks.read_text()), holder.stderr.read()
        waiter = _lock(env7, "rig", cmd=("touch", str(ran)))
        # Its first take has failed (it says it waits): a take on the first try reuses its fresh answer.
        seen = _stderr_until(waiter, "ttp lock: waiting for rig")
        assert "waiting for rig" in seen, seen
        assert _queue_len(p, "rig") == 1
        verdict.write_text("75")   # the project's repair hold begins while the waiter is queued
        go.touch()
        rc = _ended([holder, waiter])
        err = seen + waiter.stderr.read()
        assert rc == [0, 75] and not ran.exists(), (rc, err)
        assert "held by the project's pre-grant check (exit 75)" in err and "`ttp lock --probe rig --task 7`" in err, err
        assert _queue_len(p, "rig") == 0
        assert locks.any_free(locks.slot_paths(shared.locks_dir(p, "rig", p.config()), "rig", 1))
        assert [ln.split()[1] for ln in log.read_text().splitlines()] == ["6", "7", "7"], log.read_text()
    finally:
        go.touch()
        for x in (holder, waiter):
            if x and x.poll() is None:
                x.kill()


def test_the_run_supervisor_asks_the_pre_grant_check_again_once_it_has_the_slot(env, tmp_path, monkeypatch):
    """An answer older than PRE_GRANT_FRESH_S is asked again holding the slot: a hold that began meanwhile
    lets the slot go and the run does not start."""
    from ttp import locks, runner
    p = make(env)
    log = tmp_path / "hook.log"
    hook = tmp_path / "flip.sh"   # grants at the first ask, holds after
    hook.write_text(f'#!/bin/sh\necho "$1" >> {shlex.quote(str(log))}\n'
                    f'[ "$(wc -l < {shlex.quote(str(log))})" -le 1 ] && exit 0\necho "repair hold"\nexit 75\n')
    hook.chmod(0o755)
    monkeypatch.setattr(runner, "PRE_GRANT_FRESH_S", 0)
    slot, run_dir = tmp_path / "rig.0.lock", tmp_path / "run"
    run_dir.mkdir()
    pg = {"cmd": str(hook), "root": str(p.root), "state": str(p.state)}
    want = [{"resource": "rig", "paths": [str(slot)]}]
    assert runner._take_exclusive(run_dir, want, {"TTP_TASK": "5"}, time.time() + 5, pre_grant=pg) is None
    assert locks.any_free([slot]) and len(log.read_text().splitlines()) == 2
    assert "held by the project's pre-grant check (exit 75)" in (run_dir / "progress.md").read_text()
    # A fresh answer is not asked again.
    log.unlink()
    monkeypatch.setattr(runner, "PRE_GRANT_FRESH_S", 60)
    held = runner._take_exclusive(run_dir, want, {"TTP_TASK": "5"}, time.time() + 5, pre_grant=pg)
    assert held and len(log.read_text().splitlines()) == 1
    held[0].close()


def test_a_hanging_pre_grant_check_blocks_a_daemon_tick_for_about_one_timeout(env, tmp_path):
    """A hook that never answers in time: one resource's broken answer serves every task on it, and a
    tick starts no new check once its checks took the timeout, so the tick is not held for N timeouts."""
    from ttp import daemon as dmod
    p = make(env)
    log = tmp_path / "hook.log"
    hook = tmp_path / "hang.sh"
    hook.write_text(f'#!/bin/sh\necho "$1 $2" >> {shlex.quote(str(log))}\nexec sleep 30\n')
    hook.chmod(0o755)
    p.set_config("locks.pre_grant_check", shlex.quote(str(hook)))
    p.set_config("locks.pre_grant_timeout_s", 0.5)
    d = dmod.Daemon(p.base)
    tids = [p.db.add_task(f"T{i}", "s", kind="work", tier="light", origin="coordinator", labels=["resource:rig"])
            for i in range(5)]
    d._tick_began = time.monotonic()
    t0 = time.monotonic()
    assert not d._dispatchable()
    d.dispatch()
    assert time.monotonic() - t0 < 2.0, time.monotonic() - t0   # one 0.5 s timeout, not five
    assert len(log.read_text().splitlines()) == 1, log.read_text()
    for tid in tids:
        rec = d._resource_hold(p.db.task(tid))
        assert rec["kind"] == "pre_grant" and p.db.task(tid)["status"] == "queued", rec
    d.raise_pre_grant_faults()
    assert len(p.db.q("SELECT text FROM events WHERE source='daemon:pre-grant'")) == 1
    # Five resources: the first check spends the tick's budget; the others are held unasked, unrecorded.
    log.unlink()
    other = [p.db.add_task(f"R{i}", "s", kind="work", tier="light", origin="coordinator", labels=[f"resource:rig{i}"])
             for i in range(5)]
    d._tick_began = time.monotonic()
    t0 = time.monotonic()
    recs = [d._resource_hold(p.db.task(tid)) for tid in other]
    assert time.monotonic() - t0 < 2.0 and len(log.read_text().splitlines()) == 1, log.read_text()
    assert all(r["kind"] == "pre_grant" for r in recs) and [r["rc"] for r in recs[1:]] == [None] * 4, recs
    d.raise_pre_grant_faults()
    assert len(p.db.q("SELECT text FROM events WHERE source='daemon:pre-grant'")) == 2
    d._tick_began = time.monotonic()   # the next tick asks the next one
    d._resource_hold(p.db.task(other[1]))
    assert len(log.read_text().splitlines()) == 2


def _probing_driver(marks, release, tries=15):
    """A detached driver that, like a resumable try loop, probes the lock before each try and gives up
    after `tries` busy probes; its try runs under `ttp lock board`, then it runs until `release` exists."""
    q = shlex.quote
    ttp = f"{q(sys.executable)} {q(str(TTP))}"
    return ["bash", "-c",
            f"n=0; until {ttp} lock --probe board >> {q(str(marks))}.probe 2>&1; do n=$((n+1)); "
            f"[ $n -ge {tries} ] && {{ echo 'gave up' >> {q(str(marks))}; exit 9; }}; {_nap(0.1)}; done; "
            f"{ttp} lock board -- sh -c 'echo try1 >> {q(str(marks))}' || exit 8; "
            f"while [ ! -e {q(str(release))} ]; do {_nap()}; done"]


def test_a_driver_probing_its_own_runs_kept_hold_starts_its_try_and_the_hold_then_ends(env, tmp_path):
    # (a), (d), (b): an exclusive run ends while its detached driver waits for the board through
    # `ttp lock --probe`. The keeper of the run's hold kept the board for that driver, so the driver's
    # probe must read it as free for it, its try run under the hold, and the hold end with the driver.
    p = make(env)
    from ttp import hold, locks
    from ttp.daemon import Daemon
    mine, other = _board_task(p, "flash"), _board_task(p, "other")
    run_dir, marks, release = p.runs / "61", tmp_path / "marks", tmp_path / "release"
    _exclusive_run_detaching(p, mine["id"], 61, run_dir, _probing_driver(marks, release))
    paths = Daemon(p.base)._slot_paths("board")
    try:
        assert _until_true(lambda: marks.exists() and marks.read_text().strip(), 60), "the driver never got past its probe"
        assert marks.read_text().splitlines() == ["try1"], marks.read_text()
        assert "free for this run (held by task #%d (run 61)" % mine["id"] in (tmp_path / "marks.probe").read_text()
        # The try ran under the kept hold: the board never was free for anyone else meanwhile.
        assert locks.held_labels(paths) == [f"task #{mine['id']} (run 61), detached jobs"]
        assert hold.read(run_dir)["state"] == "holding"
        for who in ({}, {"TTP_TASK": str(other["id"])}):
            r = _lock_probe(dict(os.environ, TTP_PROJECT=str(p.base), **who), "board")
            assert r.returncode == 75 and "busy" in r.stdout, r.stdout
    finally:
        release.touch()
    # (b) No live job of the run uses the hold any more: it ends within a keeper poll or two.
    assert _until_true(lambda: (run_dir / "job.rc").exists()) and (run_dir / "job.rc").read_text().strip() == "0"
    assert _until_true(lambda: locks.any_free(paths), 10), "the hold outlived its last detached job"
    assert hold.read(run_dir)["state"] == "released"
    assert _lock_probe(dict(os.environ, TTP_PROJECT=str(p.base), TTP_TASK=str(other["id"])), "board").returncode == 0


def test_a_kept_hold_reads_free_only_for_its_own_runs_lineage(env, tmp_path):
    # (c): the probe's pass-through never covers a hold of another task or project, a plain `ttp lock`
    # command of the same task, a keeper that does not list the caller's job, or a run that does not
    # hold the resource for its whole run.
    p = make(env)
    from ttp import hold
    from ttp.daemon import Daemon
    mine, other = _board_task(p, "flash"), _board_task(p, "other")
    run_dir, release = p.runs / "62", tmp_path / "release"
    _exclusive_run_detaching(p, mine["id"], 62, run_dir, _until(release))
    try:
        job = str((run_dir / "job.rc").resolve())
        assert hold.read(run_dir)["jobs"] == [job]
        spec = json.loads((run_dir / "run.json").read_text())

        def probe(task, rd, rc, extra=()):
            e = dict(os.environ, TTP_PROJECT=str(p.base), TTP_TASK=str(task), TTP_RUN_ID="99",
                     TTP_RUN_DIR=str(rd), TTP_DETACHED="1", TTP_DETACH_RC=str(rc))
            return _lock_probe(e, "board", extra=extra).returncode

        def run_of(task, n, exclusive=True):
            rd = p.runs / str(n)
            rd.mkdir()
            (rd / "run.json").write_text(json.dumps({**spec, "env": {"TTP_TASK": str(task)},
                                                     **({} if exclusive else {"exclusive": []})}))
            return rd
        assert probe(mine["id"], run_dir, job) == 0   # the keeper's own job: granted under the hold
        theirs = run_of(other["id"], 63)
        assert probe(other["id"], theirs, job) == 75, "another task's job passed a hold kept for this task"
        assert probe(mine["id"], run_dir, job, extra=("--task", str(other["id"]))) == 75
        assert probe(mine["id"], run_of(mine["id"], 64), p.runs / "64" / "x.rc") == 75, \
            "a job the keeper does not keep for passed its hold"
        assert probe(mine["id"], run_of(mine["id"], 65, exclusive=False), job) == 75, \
            "a run that holds nothing for its whole run passed the hold"
    finally:
        release.touch()
    # A held slot of another project's task with the same number, or the same task's plain command lock.
    import fcntl
    slot = tmp_path / "board.0.lock"
    for lbl in (f"demo task #{mine['id']} (run 62), detached jobs", f"task #{mine['id']} (run 62)"):
        slot.write_text(json.dumps({"holder": lbl}))
        with open(slot) as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            env_ = {"TTP_RUN_ID": "62", "TTP_DETACHED": "1", "TTP_DETACH_RC": job}
            assert hold.covers([slot], f"task #{mine['id']}", spec, env_) is None, lbl
    assert _until_true(lambda: (hold.read(run_dir) or {}).get("state") == "released"), "the hold outlived its job"


def _whole_run_hold(slot, lbl):
    """Holds `slot` as a run supervisor does (its label `lbl`), in this process, until the file closes."""
    import fcntl
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps({"holder": lbl}))
    f = open(slot)
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _run_listing_the_board(env, p, tmp_path, run_id):
    """Tasks mine and other on the board, and a detached job's env of mine's run `run_id`, whose run.json
    lists the board for its whole run; with lock(env, *args), its `ttp lock board -- touch ran`."""
    from ttp.daemon import Daemon
    mine, other = _board_task(p, "flash"), _board_task(p, "other")
    paths = Daemon(p.base)._slot_paths("board")
    run_dir, ran = p.runs / str(run_id), tmp_path / "ran"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "env": {"TTP_TASK": str(mine["id"]), "TTP_RUN_ID": str(run_id)}, "holds": str(p.state / "holds"),
        "exclusive": [{"resource": "board", "paths": [str(x) for x in paths], "holder": f"task #{mine['id']}"}]}))
    job = _lock_env(env, p, TTP_TASK=str(mine["id"]), TTP_RUN_ID=str(run_id), TTP_RUN_DIR=str(run_dir),
                    TTP_DETACHED="1", TTP_DETACH_RC=str(run_dir / "job.rc"))

    def lock(e, *args):
        return subprocess.run([sys.executable, str(TTP), "lock", *args, "board", "--", "touch", str(ran)],
                              env=e, capture_output=True, text=True, timeout=60)
    return mine, other, paths, run_dir, ran, job, lock


def test_a_runs_lock_never_passes_a_lost_hold_whose_slot_another_task_took(env, tmp_path):
    # run.json lists the board for the whole run, but the run's hold was recorded lost (its keeper died)
    # and another task took the slot: the run's `ttp lock board` must not run beside that task; it is
    # busy (75) on the no-wait path, or waits. A pause still refuses it.
    p = make(env)
    from ttp import coordinator as coord
    from ttp import hold, locks
    mine, other, paths, run_dir, ran, job, lock = _run_listing_the_board(env, p, tmp_path, 71)
    hold._write(run_dir, {"state": "lost", "why": "its keeper died and board was taken meanwhile"})
    release = tmp_path / "release"
    theirs = _lock(_lock_env(env, p, TTP_TASK=str(other["id"]), TTP_RUN_ID="72"), "board", cmd=_until(release))
    waiter = None
    try:
        assert _until_true(lambda: locks.held_labels(paths) == [f"task #{other['id']} (run 72)"])
        r = lock(job, "--timeout", "2")
        assert r.returncode == 75 and not ran.exists(), ("ran beside the task that took a lost hold's slot", r)
        coord.pause_resource(p, "board", True, reason="maintenance", by="user")
        r = lock(job, "--timeout", "2")
        assert r.returncode == 75 and "board is paused" in r.stderr and not ran.exists(), r
        coord.pause_resource(p, "board", False)
        waiter = subprocess.Popen([sys.executable, str(TTP), "lock", "--timeout", "0", "board", "--", "touch", str(ran)],
                                  env=job, stderr=subprocess.PIPE, text=True)
        assert "waiting for board" in waiter.stderr.readline() and not ran.exists()
    finally:
        release.touch()
    assert theirs.wait(timeout=30) == 0
    assert waiter.wait(timeout=30) == 0 and ran.exists(), "the waiter never took the board once it was free"
    ran.unlink()
    # Another task's whole-run hold never covers this run either, whatever its run.json says.
    with _whole_run_hold(paths[0], hold.label(f"task #{other['id']}", 71, hold.WHOLE_RUN)):
        r = lock(job, "--timeout", "2")
        assert r.returncode == 75 and not ran.exists(), r


def test_a_slot_label_rewrite_never_shows_a_held_slot_with_no_holder(tmp_path, monkeypatch):
    # A held slot's label is rewritten in place by relabel and label_pid (a `ttp lock` handing its slot
    # to its command, a run handing its hold to its keeper): a reader between two steps of that write
    # (held_labels, so hold.covers and the test above) sees the old label or the new one, never the slot
    # held with an empty label. A take writes over the last holder's label, which a slot keeps after
    # release: it builds its label (run_ids, taker) before the take and cuts the old one in the step
    # right after, so a reader sees the old holder on the newly held slot for that one step at most
    # (where hold.covers could pass a lost run of the old holder), then no holder (still held).
    import builtins
    from ttp import locks
    slot, seen, busy = tmp_path / "board.0.lock", [], []

    def look():
        if not busy:
            busy.append(1)
            try:
                seen.append(tuple(locks.held_labels([slot])))
            finally:
                busy.pop()

    class Watched:   # the slot's open file, read from another open file before and after each call
        def __init__(self, f):
            self._f = f

        def __getattr__(self, name):
            got = getattr(self._f, name)
            if not callable(got):
                return got

            def step(*args, **kw):
                look()
                try:
                    return got(*args, **kw)
                finally:
                    look()
            return step

    def opened(file, *args, **kw):
        f = builtins.open(file, *args, **kw)
        return Watched(f) if not busy and Path(file) == slot else f
    monkeypatch.setattr(locks, "open", opened, raising=False)
    for name in ("pwrite", "ftruncate"):
        real = getattr(os, name)
        monkeypatch.setattr(locks.os, name, lambda *a, _real=real: (look(), _real(*a), look())[1])
    built = []   # what a reader sees while the take builds its label: never the old holder on a held slot

    def building(real):
        def call(*a, **kw):
            look(), built.append(seen[-1:])
            got = real(*a, **kw)
            look(), built.append(seen[-1:])
            return got
        return call
    for name in ("run_ids", "taker"):
        monkeypatch.setattr(locks, name, building(getattr(locks, name)))
    old, new = "task #1 (run 1)", "task #2 (run 2)"
    f = locks.try_take([slot], old, "a long command " * 20)
    f.close()
    seen.clear()
    f = locks.try_take([slot], new, "short")   # its label is shorter than the one it writes over
    try:
        assert seen and set(seen) <= {(), (old,), ("",), (new,)} and seen[-1] == (new,), seen
        assert built and [(old,)] not in built, f"the slot showed its old holder while the take built its label: {seen}"
        assert (old,) not in seen[seen.index(("",)):], seen   # the old label is cut once, never seen again
        for rewrite in (lambda: locks.relabel(f, {"run_since": 1.0}), lambda: locks.label_pid(f, os.getpid(), "command")):
            seen.clear()
            rewrite()
            assert seen and set(seen) == {(new,)}, seen
        h = json.loads(slot.read_text())
        assert (h["holder"], h["command"], h["run_since"], h["via"]) == (new, "short", 1.0, "command")
        assert slot.read_text() == json.dumps(h), "the label kept the padding of the longer one it replaced"
    finally:
        f.close()


def test_a_held_slots_label_read_mid_rewrite_is_read_again_and_never_counts_the_slot_free(tmp_path, monkeypatch):
    # A read that catches write_label mid-copy gets a torn label (new text over the old one's tail):
    # it is read again, never skipped as if the slot were free (held_labels, so turn's free count and
    # hold.covers; the reaper's label). One that never parses still counts the slot held.
    import pathlib
    from ttp import lockreap, locks
    slot, other = tmp_path / "board.0.lock", tmp_path / "board.1.lock"
    f = locks.try_take([slot], "task #2 (run 2)", "cmd")
    good, torn, reads = slot.read_text(), [1], []
    real = pathlib.Path.read_text

    def read_text(self, *a, **kw):
        if self != slot:
            return real(self, *a, **kw)
        reads.append(1)
        if torn and torn[0] > 0:
            torn[0] -= 1
            return good[:len(good) // 2] + '"command": "cmd"}'   # the new text's start, the old one's tail
        return real(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    try:
        assert locks.held_labels([slot, other]) == ["task #2 (run 2)"] and len(reads) == 2
        torn[0] = 1
        assert lockreap.label(slot)["holder"] == "task #2 (run 2)"
        torn[0] = locks.LABEL_READS
        assert locks.held_labels([slot, other]) == [""], "a held slot whose label never read counted free"
        assert locks.turn(tmp_path, "board", tmp_path / "none", [slot]) is True   # not queued: its turn
        assert len([x for x in locks.held_labels([slot]) if x is not None]) == 1
    finally:
        f.close()


def test_a_runs_lock_passes_through_at_once_while_its_lineage_holds_the_resource(env, tmp_path):
    # The run's own whole-run hold (this run, or a detached job of it after a later run took it over):
    # its `ttp lock board` runs at once under the hold, without waiting. A pause still refuses it.
    p = make(env)
    from ttp import coordinator as coord
    from ttp import hold
    mine, other, paths, run_dir, ran, job, lock = _run_listing_the_board(env, p, tmp_path, 71)
    for e in (dict(job, TTP_DETACHED="", TTP_DETACH_RC=""), dict(job, TTP_RUN_ID="70")):
        with _whole_run_hold(paths[0], hold.label(f"task #{mine['id']}", 71, hold.WHOLE_RUN)):
            start = time.time()
            r = lock(e, "--timeout", "5")
            assert r.returncode == 0 and ran.exists() and time.time() - start < 5, r
            assert "held already by this task's whole run" in r.stderr, r.stderr
            ran.unlink()
            coord.pause_resource(p, "board", True, by="user")
            r = lock(e, "--timeout", "2")
            assert r.returncode == 75 and "board is paused" in r.stderr and not ran.exists(), r
            coord.pause_resource(p, "board", False)


def _fake_stat(proc, pid, flags):
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "stat").write_text(f"{pid} (some name) S 1 {pid} {pid} 0 -1 {flags} 0 0 0 0\n")


def test_kthread_reads_the_pf_kthread_flag_and_is_false_when_it_cannot_tell(tmp_path):
    from ttp import locks
    _fake_stat(tmp_path, 2, 0x00208040)      # kthreadd
    _fake_stat(tmp_path, 13, 0x00200000)
    _fake_stat(tmp_path, 400, 0x00400100)    # a user process
    assert locks.kthread(2, proc=str(tmp_path)) and locks.kthread(13, proc=str(tmp_path))
    assert not locks.kthread(400, proc=str(tmp_path))
    assert not locks.kthread(401, proc=str(tmp_path)) and not locks.kthread("x", proc=str(tmp_path))
    (tmp_path / "5").mkdir()
    (tmp_path / "5" / "stat").write_text("garbage")
    assert not locks.kthread(5, proc=str(tmp_path))


def test_a_take_in_its_runs_pid_namespace_names_itself_with_its_process_group(tmp_path, monkeypatch):
    from ttp import locks
    for k in ("TTP_PIDNS", "TTP_RUN_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(locks, "_pid_ns", lambda: "pid:[1]")
    got = locks.taker({"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)})
    assert (got["pid"], got["pgid"], got["pidns"]) == (os.getpid(), os.getpgid(0), "pid:[1]")
    assert "ns_pid" not in got and "owner_fallback" not in got
    assert locks.taker({}) == got    # outside a run: the same


def test_a_sandboxed_take_names_its_runs_agent_never_its_own_low_namespace_pid(tmp_path, monkeypatch):
    from ttp import locks
    (tmp_path / "child.pid").write_text("4321\ntoken\n")
    monkeypatch.setattr(locks, "_pid_ns", lambda: "pid:[2]")   # a `ttp sandbox`'s namespace
    monkeypatch.setattr(locks.os, "getpid", lambda: 13)
    monkeypatch.setattr(locks, "_pgid", lambda pid: 1)
    monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": False)
    got = locks.taker({"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)})
    assert (got["pid"], got["pgid"], got["pidns"]) == (4321, 4321, "pid:[1]")
    assert (got["ns_pid"], got["ns_pidns"]) == (13, "pid:[2]")
    # the label of a slot such a take holds says the same; without its run's namespace, its own
    for run_env, want in (({"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)}, (4321, "pid:[1]", 4321)),
                          ({}, (13, "pid:[2]", 1))):
        for k in ("TTP_PIDNS", "TTP_RUN_DIR"):
            monkeypatch.delenv(k, raising=False)
        for k, v in run_env.items():
            monkeypatch.setenv(k, v)
        f = locks.try_take([tmp_path / "r.0.lock"], "task #1 (run 2)", "cmd")
        try:
            lbl = json.loads((tmp_path / "r.0.lock").read_text())
        finally:
            f.close()
        assert (lbl["pid"], lbl["pidns"], lbl["pgid"]) == want


def test_a_sandboxed_take_records_its_runs_agent_start_time_from_child_pid(tmp_path, monkeypatch):
    from ttp import locks
    monkeypatch.setattr(locks, "_pid_ns", lambda: "pid:[2]")
    monkeypatch.setattr(locks.os, "getpid", lambda: 13)
    monkeypatch.setattr(locks, "_pgid", lambda pid: 1)
    monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": False)
    run = {"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)}
    (tmp_path / "child.pid").write_text("4321\n987654\n")   # as the runner writes it: pid, start time
    got = locks.taker(run)
    assert (got["pid"], got["pid_start"]) == (4321, "987654")
    # an older runner's child.pid (no start time, or an empty line): no start time, as before
    for text in ("4321\n", "4321", "4321\n\n", "4321\n  \n"):
        (tmp_path / "child.pid").write_text(text)
        got = locks.taker(run)
        assert (got["pid"], got["pid_start"]) == (4321, None), text


def test_a_take_whose_agent_is_a_kernel_thread_falls_back_to_itself_and_says_so(tmp_path, monkeypatch):
    from ttp import locks
    monkeypatch.setattr(locks, "_pid_ns", lambda: "pid:[2]")
    monkeypatch.setattr(locks.os, "getpid", lambda: 13)
    monkeypatch.setattr(locks, "_pgid", lambda pid: 1)
    for agent, kthreads in (("2", set()), ("77", {77})):
        (tmp_path / "child.pid").write_text(f"{agent}\n")
        monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc", k=kthreads: int(pid) in k)
        got = locks.taker({"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)})
        assert (got["pid"], got["pidns"], got["pgid"]) == (13, "pid:[2]", 1)
        assert f"pid {agent} is a kernel thread" in got["owner_fallback"]
    (tmp_path / "child.pid").write_text("junk\n")     # an unreadable record: itself, no claim
    got = locks.taker({"TTP_PIDNS": "pid:[1]", "TTP_RUN_DIR": str(tmp_path)})
    assert got["pid"] == 13 and "owner_fallback" not in got


def test_a_held_label_naming_a_kernel_thread_is_holder_unverifiable_and_its_run_worker_judges(env, monkeypatch):
    from ttp import locks
    from ttp.runner import boot_id
    p = make(env)
    kt = 2 if os.getpid() != 2 else 3    # under `ttp sandbox` this test may be pid 2 itself
    monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": int(pid) == kt)
    here = {"node": os.uname().nodename, "pidns": locks._pid_ns()}
    tid = p.db.add_task("Holder", "s", kind="work", tier="light", origin="coordinator")
    rid = p.db.x("INSERT INTO runs(task, role, started, status, pid, boot_id) VALUES(?, 'worker', ?, 'ok', 999999, ?)",
                 (tid, time.time(), boot_id()))
    lbl = f"task #{tid} (run {rid})"
    bad = {"holder": lbl, **here, "pid": kt}
    assert locks.pid_alive(bad) is None
    assert locks._pid_note(bad) == f", pid {kt} is a kernel thread here: holder unverifiable"
    good = {"holder": lbl, **here, "pid": os.getpid()}    # a valid label: unchanged
    assert locks.pid_alive(good) is True and locks._pid_note(good) == f", pid {os.getpid()} alive"
    note = locks._holder_note(bad, where=p)
    assert f"holder unverifiable; pid 999999 gone (run {rid}'s worker)" in note, note
    assert "stale" not in note      # its task is not done
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert locks._holder_note(bad, where=p).endswith(f"; stale: task #{tid} is done")
    p.db.x("UPDATE runs SET pid=NULL WHERE id=?", (rid,))    # no worker to ask: neither alive nor dead
    note = locks._holder_note(bad, where=p)
    assert "holder unverifiable" in note and "stale" not in note, note


def test_an_urgent_lock_waiter_goes_ahead_of_routine_ones_and_only_an_urgent_task_gets_the_mark(env, tmp_path):
    """locks.my_turn (arrival order): a waiter at task priority 1 or better takes the next slot before
    earlier routine waiters; cli._urgent_priority gives that mark only to an urgent task's waiter."""
    from ttp import cli, locks
    q = tmp_path / "q"
    routine = locks.enqueue(q, "r", "routine", priority=3)
    plain = locks.enqueue(q, "r", "no task")
    hot = locks.enqueue(q, "r", "urgent", priority=1)
    try:
        assert locks.my_turn(q, "r", hot, 1) and not locks.my_turn(q, "r", routine, 1)
        assert not locks.my_turn(q, "r", plain, 1)
        assert locks.my_turn(q, "r", routine, 2) and not locks.my_turn(q, "r", plain, 2), "then arrival order"
    finally:
        for t in (routine, plain, hot):
            locks.dequeue(t)
    p = make(env)
    a = p.db.add_task("routine", "s", kind="work", origin="user", priority=2)
    b = p.db.add_task("urgent", "s\n  Urgent: the user asked twice", kind="work", origin="user", priority=3)
    c = p.db.add_task("p1", "s", kind="work", origin="user", priority=1)
    assert [cli._urgent_priority(p, str(t)) for t in (a, b, c)] == [None, 1, 1]
    assert cli._urgent_priority(p, "") is None and cli._urgent_priority(p, "99999") is None
    assert cli._urgent_priority(p, "#12") is None, "only a plain task id"


def _fake_pidns_proc(root, procs):
    """A /proc stand-in under `root` with each (pid here, pid namespace, NSpid, state) of `procs`."""
    root.mkdir(exist_ok=True)
    for here, ns, nspid, state in procs:
        (root / str(here) / "ns").mkdir(parents=True)
        os.symlink(ns, root / str(here) / "ns" / "pid")
        (root / str(here) / "status").write_text(f"Name:\tx\nState:\t{state} (x)\nNSpid:\t{nspid}\n")
    return root


def test_lock_probe_judges_a_pid_from_another_pid_namespace_through_that_namespace(env, tmp_path, monkeypatch):
    """A taker or keeper in a sandbox records its pid in its own PID namespace: the probe says the pid is
    from another namespace and judges it through /proc (that namespace's process with that innermost pid),
    alive unknown with why only when nothing of that namespace is visible. A done task's hold is stale
    only when its holder is gone, never when it lives or cannot be checked. Same-namespace, other-host and
    cannot-tell-its-own-namespace labels read as before."""
    from ttp import locks
    me = os.uname().nodename
    foreign = {"pid": 2, "node": me, "pidns": "pid:[999]"}
    monkeypatch.setattr(locks, "_PROC", str(tmp_path / "no-proc"))
    # Same namespace: judged directly, /proc never scanned.
    same = {"pid": os.getpid(), "node": me, "pidns": locks._pid_ns()}
    assert locks._pid_note(same) == f", pid {os.getpid()} alive" and locks.pid_alive(same) is True
    assert locks._pid_note({"pid": 2, "node": "another-host", "pidns": "pid:[999]"}) == ", pid 2"
    assert locks.pid_alive({"pid": 2, "node": "another-host", "pidns": "pid:[999]"}) is None
    cases = [
        ("live", [(4321, "pid:[999]", "4321 2", "S")], True, ", pid 2 (from another PID namespace; pid 4321 here) alive"),
        ("dead", [(4321, "pid:[999]", "4321 1", "S"), (4322, "pid:[5]", "4322 2", "S")], False,
         ", pid 2 (from another PID namespace, which no longer has it) gone"),
        ("zombie", [(4321, "pid:[999]", "4321 2", "Z")], False,
         ", pid 2 (from another PID namespace, which no longer has it) gone"),
        ("unseen", [(4322, "pid:[5]", "4322 2", "S")], None,
         ", pid 2 (from another PID namespace, not meaningful here; alive unknown: "
         "no process of its PID namespace is visible from here)"),
    ]
    p = make(env)
    t = p.db.add_task("holder", "s", kind="work", origin="user")
    p.db.update_task(t, status="done")
    held = _held_build_lock(p, "build-a", "x")
    paths = [pathlib.Path(held.name)]
    try:
        for name, procs, alive, note in cases:
            monkeypatch.setattr(locks, "_PROC", str(_fake_pidns_proc(tmp_path / name, procs)))
            assert locks._pid_note(foreign) == note, name
            assert locks.pid_alive(foreign) is alive, name
            held.seek(0)
            held.truncate()
            held.write(json.dumps({"holder": f"task #{t} (run 99)", "since": time.time(), "command": "x", **foreign}))
            held.flush()
            rec = locks.holder_records(paths, p)[0]
            assert rec["alive"] is alive and rec["stale"] is (alive is False), (name, rec)
            assert rec["note"].startswith(note.removeprefix(", ").removesuffix(")")), (name, rec)
            assert alive is not None or rec["note"].endswith("; no record of run 99 to judge it through)"), rec
        # A side that cannot tell its own namespace judges nothing, as before.
        monkeypatch.setattr(locks, "_pid_ns", lambda: None)
        assert locks._pid_note(foreign) == ", pid 2" and locks.pid_alive(foreign) is None
    finally:
        held.close()


def test_lock_probe_sees_a_sandboxed_keeper_alive_then_gone(env, tmp_path):
    """End to end in a real PID namespace: a taker inside one hands its slot to a keeper there; `ttp lock
    --probe` outside names the keeper's pid as another namespace's, alive with its pid here, then gone once
    the keeper ends while the taker still holds the slot."""
    from ttp import locks, shared
    unshare = shutil.which("unshare")
    probe_ns = [unshare, "--user", "--map-current-user", "--pid", "--fork", "--mount-proc", "true"] if unshare else []
    if not unshare or subprocess.run(probe_ns, capture_output=True, timeout=30).returncode:
        pytest.skip("no unprivileged PID namespaces here")
    p = make(env)
    cfg = p.config()
    path = locks.slot_paths(shared.locks_dir(p, "build-a", cfg), "build-a", shared.slots(p, "build-a", cfg))[0]
    ready, stop, runtime = tmp_path / "ready", tmp_path / "stop", str(TTP.parents[1] / "runtime")
    inner = (f"import os, subprocess, sys, time\nsys.path.insert(0, {runtime!r})\nfrom ttp import locks\n"
             f"f = locks.try_take([__import__('pathlib').Path({str(path)!r})], 'sandboxed taker', 'x')\n"
             "k = subprocess.Popen(['sleep', '300'])\nlocks.label_pid(f, k.pid)\n"
             f"open({str(ready)!r}, 'w').write(str(k.pid))\n"
             f"while not os.path.exists({str(stop)!r}): time.sleep(0.05)\n"
             "k.kill(); k.wait()\n"
             f"open({str(ready)!r}, 'w').write('gone')\n"
             f"while os.path.exists({str(stop)!r}): time.sleep(0.05)\n")
    proc = subprocess.Popen(probe_ns[:-1] + [sys.executable, "-c", inner])
    try:
        assert _wait_for(lambda: ready.exists() and ready.read_text().strip().isdigit(), 30), "taker never took it"
        out = _ttp_run(p, "lock", "--probe", "build-a").stdout
        assert "from another PID namespace; pid " in out and ") alive" in out, out
        rec = json.loads(_ttp_run(p, "lock", "--probe", "--json", "build-a").stdout)["holders"][0]
        assert rec["alive"] is True and rec["pidns"] != locks._pid_ns() and rec["via"] == "keeper", rec
        stop.touch()
        assert _wait_for(lambda: ready.read_text() == "gone", 30)
        out = _ttp_run(p, "lock", "--probe", "build-a").stdout
        assert "(from another PID namespace, which no longer has it) gone" in out, out
    finally:
        stop.unlink(missing_ok=True)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _foreign_run(p, task, run_id):
    """A running run row of `task` whose folder is a temp dir, recorded on this boot: what a sandboxed probe
    judges a foreign holder through."""
    from ttp import runner
    rd = p.runs / str(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(id,task,role,provider,status,started,dir,boot_id) VALUES(?,?,'worker','claude','running',?,?,?)",
           (run_id, task, time.time(), str(rd), runner.boot_id()))
    return rd


def _label(held, fields):
    held.seek(0)
    held.truncate()
    held.write(json.dumps({"since": time.time(), "command": "x", **fields}))
    held.flush()


def test_lock_probe_in_a_sandbox_judges_an_unseen_foreign_holder_through_its_run(env, tmp_path, monkeypatch):
    """A probe inside one sandbox sees none of the holder's PID namespace (the host's, or another sandbox's):
    it judges the holder through its run's folder instead of 'alive unknown'. A keeper lives while its
    hold.json names it, holding, with a fresh hold.beat, and is gone once the record ended the hold; a run's
    agent (child.pid) lives while the run's lease is fresh and is gone once exit.json exists. A stale beat or
    lease, a pid the run does not name, no run record or a holder outside this project stay unknown with the
    route tried, and never make a done task's hold stale. A taker that shares the probe's namespace is judged
    by its own pid (ns_pid)."""
    from ttp import hold, locks
    me = os.uname().nodename
    monkeypatch.setattr(locks, "_PROC", str(_fake_pidns_proc(tmp_path / "proc", [(4322, "pid:[5]", "4322 2", "S")])))
    p = make(env)
    t = p.db.add_task("holder", "s", kind="work", origin="user")
    p.db.update_task(t, status="done")
    rd = _foreign_run(p, t, 77)
    held = _held_build_lock(p, "build-a", "x")
    paths = [pathlib.Path(held.name)]
    foreign = {"node": me, "pidns": "pid:[999]"}
    keeper = {"holder": f"task #{t} (run 77), detached jobs", "pid": 4242, "via": "keeper", "taker_pid": 9, **foreign}
    agent = {"holder": f"task #{t} (run 77)", "pid": 4243, **foreign}

    def probe(fields):
        _label(held, fields)
        rec = locks.holder_records(paths, p)[0]
        return rec["alive"], rec["stale"], rec["note"], locks.holders_now(paths, p)[0]

    old = time.time() - 600
    try:
        hold._write(rd, {"task": str(t), "run": "77", "state": "holding", "keeper": 4242, "jobs": []})
        (rd / hold.BEAT).touch()
        alive, stale, note, line = probe(keeper)
        assert alive is True and not stale, note
        assert note.startswith("pid 4242 (from another PID namespace, not visible here; run 77's keeper beat ")
        assert note.split(";")[1].endswith(" alive") and "alive unknown" not in line and "stale" not in line, line
        os.utime(rd / hold.BEAT, (old, old))   # a stale beat: unknown, never gone
        alive, stale, note, _ = probe(keeper)
        assert alive is None and not stale and "alive unknown: " in note and "run 77's keeper beat is 6" in note, note
        (rd / hold.BEAT).unlink()
        assert "run 77's keeper beat is missing" in probe(keeper)[2]
        hold._write(rd, {"task": str(t), "run": "77", "state": "holding", "keeper": 1111, "jobs": []})
        alive, stale, note, _ = probe(keeper)
        assert alive is None and not stale and "hold record names no keeper pid 4242" in note, note
        hold._write(rd, {"task": str(t), "run": "77", "state": "released", "keeper": 4242, "jobs": []})
        alive, stale, note, _ = probe(keeper)
        assert alive is False and stale and "run 77's keeper ended its hold (released)) gone" in note, note

        (rd / "child.pid").write_text("4243\n123\n")
        (rd / "lease").touch()
        alive, stale, note, _ = probe(agent)
        assert alive is True and not stale and "run 77's agent, its lease renewed " in note, note
        os.utime(rd / "lease", (old, old))
        alive, stale, note, _ = probe(agent)
        assert alive is None and not stale and "run 77's agent, its lease is 6" in note, note
        (rd / "exit.json").write_text("{}")
        alive, stale, note, _ = probe(agent)
        assert alive is False and stale and "run 77's agent exited) gone" in note, note
        (rd / "runner.pid").write_text("4244\n")
        (rd / "lease").touch()
        assert probe({**agent, "pid": 4244})[0] is True, "a whole-run supervisor lives while the lease is fresh"

        alive, stale, note, _ = probe({**agent, "pid": 4999})
        assert alive is None and not stale and "pid 4999 is not run 77's agent, supervisor or keeper" in note, note
        alive, stale, note, _ = probe({**agent, "holder": f"task #{t} (run 78)"})
        assert alive is None and not stale and "no record of run 78 to judge it through" in note, note
        _label(held, {**agent, "holder": "someone else"})
        assert locks.holders_now(paths, p)[0].endswith(
            "alive unknown: no process of its PID namespace is visible from here; "
            "no run of this project's to judge it through)"), locks.holders_now(paths, p)
        p.db.x("UPDATE runs SET boot_id='an-earlier-boot' WHERE id=77")
        assert "run 77 was recorded on an earlier boot" in probe(keeper)[2]

        # The taker shares this namespace (a probe in the taker's own sandbox): its ns_pid answers.
        mine = {**agent, "ns_pid": os.getpid(), "ns_pidns": locks._pid_ns()}
        assert locks._pid_note(mine) == f", pid 4243 (from another PID namespace; its taker is pid {os.getpid()} here) alive"
        assert locks.pid_alive(mine) is True and probe(mine)[:2] == (True, False)
        gone = subprocess.Popen(["true"])
        gone.wait()
        assert "alive unknown: " in locks._pid_note({**mine, "ns_pid": gone.pid}), "a gone taker proves nothing"
        assert "alive unknown: " in locks._pid_note({**mine, "ns_pidns": "pid:[6]"})
    finally:
        held.close()


def test_lock_probe_inside_a_real_sandbox_sees_a_live_keeper_outside_it_through_its_beat(env, tmp_path):
    """End to end: a slot held on the host, labelled as a run's keeper of detached jobs; `ttp lock --probe`
    run inside a fresh PID namespace (as `ttp sandbox` runs commands) cannot see the host's processes, yet
    reports the keeper alive through its run's hold.beat, then gone once its hold record ended."""
    from ttp import hold, locks
    unshare = shutil.which("unshare")
    ns = [unshare, "--user", "--map-current-user", "--pid", "--fork", "--mount-proc"] if unshare else []
    if not unshare or subprocess.run(ns + ["true"], capture_output=True, timeout=30).returncode:
        pytest.skip("no unprivileged PID namespaces here")
    p = make(env)
    t = p.db.add_task("holder", "s", kind="work", origin="user")
    rd = _foreign_run(p, t, 81)
    held = _held_build_lock(p, "build-a", f"task #{t} (run 81)")
    try:
        locks.label_pid(held, os.getpid())
        h = json.loads(pathlib.Path(held.name).read_text())
        assert h["via"] == "keeper" and h["pidns"] == locks._pid_ns()
        hold._write(rd, {"task": str(t), "run": "81", "state": "holding", "keeper": os.getpid(), "jobs": []})
        (rd / hold.BEAT).touch()

        def inside():
            r = subprocess.run(ns + [sys.executable, str(TTP), "lock", "--probe", "--json", "build-a"],
                               capture_output=True, text=True, timeout=60, env=dict(os.environ, TTP_PROJECT=str(p.base)))
            assert r.returncode == 75, (r.returncode, r.stdout, r.stderr)
            return json.loads(r.stdout)["holders"][0]

        rec = inside()
        assert rec["alive"] is True and "run 81's keeper beat " in rec["note"], rec
        assert "alive unknown" not in rec["note"] and rec["note"].endswith(" alive"), rec
        hold._write(rd, {"task": str(t), "run": "81", "state": "handed_over", "keeper": os.getpid(), "jobs": []})
        rec = inside()
        assert rec["alive"] is False and "keeper ended its hold (handed_over)) gone" in rec["note"], rec
    finally:
        held.close()


def test_ttp_lock_holds_until_its_command_ends_when_signalled_as_the_command_starts(env):
    # A signal right after the command started, before ttp lock had set up its forwarding, used to end
    # ttp lock at once (rc 143): the lock was freed while the command still ran. A sitecustomize stretches
    # that window by sleeping after the command's Popen.
    p = make(env)
    sc, marks, pidf = env["tmp"] / "sc", env["tmp"] / "marks.txt", env["tmp"] / "cmd.pid"
    sc.mkdir()
    (sc / "sitecustomize.py").write_text(
        "import os, subprocess, time\n"
        "_real = subprocess.Popen.__init__\n"
        "def _slow(self, *a, **k):\n"
        "    _real(self, *a, **k)\n"
        "    if 'started' in str(a[:1]):\n"
        "        time.sleep(float(os.environ['LOCK_TEST_POPEN_DELAY']))\n"
        "subprocess.Popen.__init__ = _slow\n")
    run_env = _lock_env(env, p, LOCK_TEST_POPEN_DELAY="2",
                        PYTHONPATH=os.pathsep.join(filter(None, [str(sc), os.environ.get("PYTHONPATH")])))
    script = (f"echo $$ > {shlex.quote(str(pidf))}; echo started >> {shlex.quote(str(marks))}; "
              f"sleep 4; echo ended >> {shlex.quote(str(marks))}")
    first = _lock(run_env, "board", cmd=("sh", "-c", script))
    pid = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not (marks.exists() and "started" in marks.read_text()):
            time.sleep(0.02)
        pid = int(pidf.read_text())
        first.send_signal(signal.SIGTERM)       # inside the stretched window after Popen
        first.wait(timeout=30)
        # ttp lock ended only once its command was gone: the command got the signal (no 'ended').
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        assert not alive, "ttp lock ended while its command still ran"
        assert "ended" not in marks.read_text(), "the signal was not passed on to the command"
        # Free once the command is gone (the `sleep` it left running never had the lock's file).
        assert _until_true(lambda: _lock_probe(run_env, "board").returncode == 0)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _cmd_ended(pid, timeout=30):
    """Whether process pid ended (or is a zombie no one reaped yet) within timeout."""
    from ttp import locks

    def _done():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return locks.zombie(pid)
    return _until_true(_done, timeout)


_CMD_HANG_S = 600   # only a hang guard: _cmd_end_waiter's wait ends as the process does


def _cmd_end_waiter(pid):
    """For a process pid that is alive now (and not this one's child): a call that returns whether it
    ended, once it has. It waits on a pidfd opened now, which is readable as soon as the process exits,
    reaped or not, whoever adopted it, so a loaded host only delays the answer: a 30 s poll of it timed
    out on a busy host while the command was still on its way out. No pidfd (macOS): _cmd_ended."""
    try:
        pfd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        return lambda: _cmd_ended(pid)

    def _wait():
        import select
        try:
            return bool(select.select([pfd], [], [], _CMD_HANG_S)[0])
        finally:
            os.close(pfd)
    return _wait


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="no pidfd: _cmd_end_waiter polls as _cmd_ended")
def test_cmd_end_waiter_waits_for_an_adopted_process_however_long_it_is_held_up_and_sees_a_zombie(tmp_path):
    # The sigkilled test below polled its orphaned command for 30 s; on a loaded host the command was not
    # gone yet. A stopped process stands in for one the host starves past that poll: the waiter answers
    # once it ends, whoever reaps it, and an unreaped zombie counts as ended.
    pidf = tmp_path / "pid"
    sh = subprocess.Popen(["sh", "-c", f"sleep 30 & echo $! > {shlex.quote(str(pidf))}"])
    sh.wait()   # the sleep is adopted by init (or a subreaper): not this process's child
    pid = int(pidf.read_text())
    ended = _cmd_end_waiter(pid)
    os.kill(pid, signal.SIGSTOP)
    assert not _cmd_ended(pid, timeout=0.3), "a held-up process reads as running to a timed poll"
    os.kill(pid, signal.SIGKILL)
    assert ended()
    from ttp import locks
    child = subprocess.Popen(["true"])
    ended = _cmd_end_waiter(child.pid)
    try:
        assert ended() and locks.zombie(child.pid)
    finally:
        child.wait()


@pytest.mark.parametrize("closes", [False, True])
@pytest.mark.parametrize("mode", [(), ("--shared",)])
def test_ttp_lock_stays_held_while_its_command_runs_after_ttp_lock_is_sigkilled(env, mode, closes):
    # A SIGKILL (or the OOM killer) of `ttp lock` cannot be caught. The lock lives on the open file the
    # command is handed too, and on a keeper's for a command that closes the fds it inherits (as ssh
    # does), so it stays held, and reads as held by the live command, until the command ends.
    p = make(env)
    pidf, go = env["tmp"] / "cmd.pid", env["tmp"] / "go"
    script = (f"echo $$ > {shlex.quote(str(pidf))}.tmp; mv {shlex.quote(str(pidf))}.tmp {shlex.quote(str(pidf))}; "
              f"while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.05; done")
    cmd = ("sh", "-c", script)
    if closes:
        cmd = (sys.executable, "-c", "import os, sys; os.closerange(3, 65536); os.execvp(sys.argv[1], sys.argv[1:])", *cmd)
    run_env = _lock_env(env, p)
    first = _lock(run_env, *mode, "board", cmd=cmd)
    pid = None
    try:
        assert _until_true(lambda: pidf.exists()), first.stderr.read()
        pid = int(pidf.read_text())
        ended = _cmd_end_waiter(pid)   # the command waits for go: alive until then
        first.kill()
        first.wait(timeout=30)
        os.kill(pid, 0)   # the command outlives its ttp lock
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "board", "--", "true"],
                           env=run_env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 75, (r.returncode, r.stderr)
        r = _lock_probe(run_env, "board", extra=("--json",))
        got = json.loads(r.stdout)
        assert r.returncode == 75 and not got["free"], (r.returncode, r.stdout)
        hs = got["holders"]
        assert [(h["pid"], h["alive"], h["stale"], h.get("taker_pid")) for h in hs] \
            == [(pid, True, False, first.pid)], hs
        assert f"pid {pid} alive" in _lock_probe(run_env, "board").stdout
    finally:
        go.touch()
    assert pid and ended()
    assert _until_true(lambda: _lock_probe(run_env, "board").returncode == 0), _lock_probe(run_env, "board").stdout


def _stretched_start(env, p, stretch):
    """A lock env whose `ttp lock` sleeps 3 s right after it starts its command (stretch "command") or the
    keeper (stretch "keeper"), touching tmp/stretched as the sleep begins; and that command's script."""
    sc, pidf, go = env["tmp"] / "sc", env["tmp"] / "cmd.pid", env["tmp"] / "go"
    sc.mkdir()
    (sc / "sitecustomize.py").write_text(
        "import os, subprocess, time\n"
        "_real = subprocess.Popen.__init__\n"
        "_mark = {'command': 'cmd.pid', 'keeper': 'select.select'}[os.environ['LOCK_TEST_STRETCH']]\n"
        "def _slow(self, *a, **k):\n"
        "    _real(self, *a, **k)\n"
        "    if _mark in str(a[:1]):\n"
        f"        open({str(env['tmp'] / 'stretched')!r}, 'w').close()\n"
        "        time.sleep(3)\n"
        "subprocess.Popen.__init__ = _slow\n")
    run_env = _lock_env(env, p, LOCK_TEST_STRETCH=stretch,
                        PYTHONPATH=os.pathsep.join(filter(None, [str(sc), os.environ.get("PYTHONPATH")])))
    script = (f"echo $$ > {shlex.quote(str(pidf))}.tmp; mv {shlex.quote(str(pidf))}.tmp {shlex.quote(str(pidf))}; "
              f"while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.05; done")
    return run_env, ("sh", "-c", script), pidf, go


@pytest.mark.parametrize("stretch", ["command", "keeper"])
def test_ttp_lock_sigkilled_once_its_command_runs_leaves_the_lock_held_and_named_for_the_command(env, stretch):
    # The command used to run before the keeper had the lock's files (a SIGKILL of `ttp lock` then freed
    # the lock while the command ran) and before the label named it (it read as held by the dead `ttp
    # lock`: a flake of the sigkilled test above). The command now runs only once both are in place.
    p = make(env)
    run_env, cmd, pidf, go = _stretched_start(env, p, stretch)
    first = _lock(run_env, "board", cmd=cmd)
    pid = None
    try:
        assert _until_true(lambda: pidf.exists()), first.stderr.read()
        pid = int(pidf.read_text())
        first.kill()
        first.wait(timeout=30)
        os.kill(pid, 0)   # the command outlives its ttp lock
        r = _lock_probe(run_env, "board", extra=("--json",))
        got = json.loads(r.stdout)
        assert r.returncode == 75 and not got["free"], (r.returncode, r.stdout)
        assert [(h["pid"], h["alive"], h.get("taker_pid")) for h in got["holders"]] == [(pid, True, first.pid)], got
    finally:
        go.touch()
    assert pid and _cmd_ended(pid)
    assert _until_true(lambda: _lock_probe(run_env, "board").returncode == 0), _lock_probe(run_env, "board").stdout


@pytest.mark.parametrize("stretch", ["command", "keeper"])
def test_ttp_lock_sigkilled_before_its_command_runs_never_runs_it(env, stretch):
    # A SIGKILL of `ttp lock` while it sets up the hold (the keeper, the label) leaves no command running
    # unlocked: the command never starts, and the lock is free once the keeper sees that.
    p = make(env)
    run_env, cmd, pidf, go = _stretched_start(env, p, stretch)
    first = _lock(run_env, "board", cmd=cmd)
    try:
        assert _until_true(lambda: (env["tmp"] / "stretched").exists()), first.stderr.read()
        first.kill()
        first.wait(timeout=30)
        assert _until_true(lambda: _lock_probe(run_env, "board").returncode == 0), _lock_probe(run_env, "board").stdout
        assert not pidf.exists(), "the command ran though ttp lock never handed it the lock"
    finally:
        go.touch()
        if pidf.exists():
            _cmd_ended(int(pidf.read_text()))


@pytest.mark.parametrize("missing", ["no-such-command-here", "./no-such-file"])
def test_ttp_lock_names_a_command_it_cannot_run_and_exits_127(env, missing):
    p = make(env)
    r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", missing], env=_lock_env(env, p),
                       capture_output=True, text=True, timeout=60, cwd=env["tmp"])
    assert r.returncode == 127 and f"ttp lock: cannot run {missing}: No such file or directory" in r.stderr, r
    assert _lock_probe(_lock_env(env, p), "board").returncode == 0


@pytest.mark.parametrize("mode", [(), ("--shared",)])
def test_ttp_lock_is_free_once_its_command_ends_though_a_process_it_left_running_lives_on(env, mode):
    # A process the command leaves running in the background (its own session: setsid, a daemon) used to
    # inherit the lock's open file and keep the lock for as long as it lived, after the command, its
    # process group and ttp lock were all gone: a gate stayed taken for days. The command never gets the
    # lock's files now (only ttp lock's keeper holds them), so the lock ends with the command.
    p = make(env)
    pidf, go = env["tmp"] / "bg.pid", env["tmp"] / "go"
    bg = f"while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.05; done"
    fds = env["tmp"] / "fds.txt"
    script = (f"ls -l /proc/self/fd > {shlex.quote(str(fds))} 2>&1; "
              f"setsid sh -c {shlex.quote(bg)} > /dev/null 2>&1 < /dev/null & echo $! > {shlex.quote(str(pidf))}")
    run_env = _lock_env(env, p)
    first = _lock(run_env, *mode, "board", cmd=("sh", "-c", script))
    try:
        assert first.wait(timeout=60) == 0, first.stderr.read()
        bg_pid = int(pidf.read_text())
        assert not _cmd_ended(bg_pid, timeout=0.2), "the background process should still run"
        if sys.platform.startswith("linux"):
            assert "board.0.lock" not in fds.read_text(), fds.read_text()   # not inherited by the command
        r = _lock_probe(run_env, "board")
        assert r.returncode == 0, r.stdout
    finally:
        go.touch()
    assert _cmd_ended(bg_pid)


def test_a_waiting_ttp_lock_logs_who_sent_it_sigterm_and_still_exits_143(env, tmp_path):
    # A detached job waiting on a lock got rc 143 with no sender logged (note e5b657dfd48c4253): the
    # waiter now names the signal's sender in its stderr, progress.md and the job-signal stream.
    p = make(env)
    run_env = _lock_env(env, p)
    held, go, run_dir = tmp_path / "held", tmp_path / "go", tmp_path / "run"
    run_dir.mkdir()
    h = _lock(run_env, "rig-a", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    w = None
    try:
        _wait_for_file(held, h)
        w = _lock(dict(run_env, TTP_TEST_POLL_S="30", TTP_RUN_DIR=str(run_dir)), "rig-a")
        assert "waiting for" in _stderr_until(w, "waiting for")
        time.sleep(0.3)   # into its nap
        w.send_signal(signal.SIGTERM)
        assert w.wait(timeout=30) == 143
        err = w.stderr.read()
    finally:
        go.touch()
        if w and w.poll() is None:
            w.kill()
    assert h.wait(timeout=30) == 0
    assert f"SIGTERM received by pid {w.pid}" in err and f"from pid {os.getpid()}" in err, err
    prog = (run_dir / "progress.md").read_text()
    assert f"SIGTERM received by pid {w.pid}" in prog and f"from pid {os.getpid()}" in prog, prog
    rows = [json.loads(x) for x in (p.state / "job-signals.jsonl").read_text().splitlines()]
    assert any(r.get("received") and r["signal"] == "SIGTERM" and r["from_pid"] == os.getpid()
               and r["pid"] == w.pid for r in rows), rows


def test_lock_alternatives_take_an_unpaused_one_and_a_pause_of_all_says_held_by_pause(env, tmp_path):
    # `ttp lock 'A|B'`: a paused alternative is never taken or waited on; the free one is taken first;
    # every one paused exits 75 at once naming "held by pause on", which a busy lock never says.
    p = make(env)
    from ttp import coordinator as coord
    run_env, ran = _lock_env(env, p), tmp_path / "ran"
    touch = ("touch", str(ran))
    coord.pause_resource(p, "box-a", True, reason="maintenance", by="user")
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "box-a|box-b", "--", *touch],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and ran.exists() and "box-a paused; taking box-b" in r.stderr, r
    assert _queue_len(p, "box-a") == 0
    ran.unlink()
    # Unpaused: the free alternative goes first, not a busy one ahead of it.
    coord.pause_resource(p, "box-a", False)
    release = tmp_path / "release"
    busy = _lock(run_env, "box-a", cmd=_until(release))
    try:
        assert _until_true(lambda: _lock_probe(run_env, "box-a").returncode == 75)
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "box-a|box-b", "--", *touch],
                           env=run_env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and ran.exists(), r
        ran.unlink()
        # A busy lock that gives up is 75 too, but never says it is held by a pause.
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "box-a", "--", *touch],
                           env=run_env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 75 and "held by pause" not in r.stderr and not ran.exists(), r
    finally:
        release.touch()
    assert busy.wait(timeout=30) == 0
    # All paused: 75 at once, naming each, queued on none; the probe of the alternatives says 75 as well.
    coord.pause_resource(p, "box-a", True, by="user")
    coord.pause_resource(p, "box-b", True, reason="bench work", by="user")
    r = subprocess.run([sys.executable, str(TTP), "lock", "box-a|box-b", "--", *touch],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "held by pause on box-a, box-b" in r.stderr and not ran.exists(), r
    assert _queue_len(p, "box-a") == 0 and _queue_len(p, "box-b") == 0
    assert _lock_probe(run_env, "box-a|box-b").returncode == 75
    # A single paused lock says it too, and alternatives never mix with a set of locks.
    r = subprocess.run([sys.executable, str(TTP), "lock", "box-b", "--", *touch], env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "held by pause on box-b" in r.stderr and "box-b is paused" in r.stderr, r
    r = subprocess.run([sys.executable, str(TTP), "lock", "box-a|box-b,box-c", "--", *touch], env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and not ran.exists(), r


# A worker's `ttp lock` is freed from the sandbox for the lock only (providers/claude.py FENCE_EXCLUDED):
# a privileged command goes through `ttp host-run`'s allowlist, never around it (privlock.py).
def _priv_stubs(tmp_path):
    """A bin folder of sudo, su, doas, pkexec and ssh stand-ins, each appending its name and words to a log."""
    bin_dir, log = tmp_path / "privbin", tmp_path / "priv.log"
    bin_dir.mkdir()
    for name in ("sudo", "su", "doas", "pkexec", "ssh"):
        f = bin_dir / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(log))}\n')
        f.chmod(0o755)
    return bin_dir, log


def test_worker_ttp_lock_refuses_a_privileged_command_and_points_to_host_run(env, tmp_path):
    p = make(env)
    bin_dir, log = _priv_stubs(tmp_path)
    run_dir = p.base / "state" / "runs" / "9"
    run_dir.mkdir(parents=True)
    path = f"{bin_dir}:{os.environ['PATH']}"
    worker = _lock_env(env, p, TTP_TASK="7", TTP_RUN_DIR=str(run_dir), PATH=path)
    for cmd in (["sudo", "-n", "tt-smi", "-glx_reset"], [str(bin_dir / "sudo"), "true"], ["bash", "-c", "sudo true"],
                ["sh", "-c", "echo a; doas true"], ["bash", "-lc", "x=$(pkexec id)"], ["sh", "-c", "eval su -c id"],
                ["env", "A=1", "timeout", "-s", "KILL", "5", "nohup", "su", "-c", "id"], ["sh", "-c", "true\nsudo id"],
                ["bash", "-c", "bash -c 'sudo id'"], ["--disruptive", "--", "sudo", "id"]):
        r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", *cmd], env=worker, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 2 and "ttp host-run" in r.stderr, (cmd, r)
        assert not log.exists() and _queue_len(p, "rig") == 0, cmd
        assert _lock_probe(worker, "rig").returncode == 0, cmd
    # A non-privileged command still runs under the lock, a remote sudo (the remote host's business)
    # too, and so does `ttp host-run`, which the daemon checks against runner.host_commands.
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "bash", "-c", f"echo ok > {out}; echo sudo"],
                       env=worker, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and out.read_text() == "ok\n", r
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "ssh", "box", "sudo -n tt-smi -s"],
                       env=worker, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and log.read_text() == "ssh box sudo -n tt-smi -s\n", r
    from ttp import privlock
    assert privlock.refusal(["ttp", "host-run", "--", "sudo", "-n", "tt-smi", "-s"]) is None
    # Outside a run (the user's own shell) the lock runs what it is given.
    user = {k: v for k, v in _lock_env(env, p, PATH=path).items()
            if k not in ("TTP_RUN_DIR", "TTP_RUN_ID", "TTP_NOTE_RUN_DIR")}
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "sudo", "true"], env=user,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and log.read_text().splitlines()[-1] == "sudo true", r


def test_worker_ttp_lock_runs_its_command_without_new_privileges(env, monkeypatch):
    # Whatever route the command takes to a setuid program (a script, an interpreter), it gains nothing:
    # cmd_lock sets no_new_privs for a worker's command (a sandboxed runner has it already, so spy on it).
    from ttp import cli, privlock
    p = make(env)
    run_dir = p.base / "state" / "runs" / "9"
    run_dir.mkdir(parents=True)
    calls = []

    class _Set(Exception):
        pass

    def spy():
        calls.append(1)
        raise _Set   # stop before the lock: the call is what this checks
    monkeypatch.setattr(privlock, "no_new_privs", spy)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    with pytest.raises(_Set):
        cli.main(["lock", "rig", "--", "true"])
    assert calls == [1]
    # Outside a run (the user's own shell) the command runs as given.
    for k in ("TTP_RUN_DIR", "TTP_RUN_ID", "TTP_NOTE_RUN_DIR"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["lock", "rig", "--", "true"])
    assert e.value.code in (0, None) and calls == [1]


def _no_new_privs_here() -> str:
    try:
        return next((ln.split()[1] for ln in open("/proc/self/status") if ln.startswith("NoNewPrivs:")), "")
    except OSError:
        return ""


@pytest.mark.skipif(_no_new_privs_here() != "0", reason="the kernel flag shows only where the test runner lacks "
                    "it (a sandboxed runner has NoNewPrivs 1 already) and on Linux; the spy test covers the call")
def test_worker_ttp_lock_child_shows_no_new_privs(env, tmp_path):
    p = make(env)
    run_dir = p.base / "state" / "runs" / "9"
    run_dir.mkdir(parents=True)
    worker = _lock_env(env, p, TTP_TASK="7", TTP_RUN_DIR=str(run_dir))
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "grep", "^NoNewPrivs", "/proc/self/status"],
                       env=worker, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.split() == ["NoNewPrivs:", "1"], r


def test_root_client_refuses_state_changing_clients_of_root_services(monkeypatch):
    # A root-owned service's unix socket is another way around host-run's allowlist; user-scoped and
    # read-only use stays allowed.
    from ttp import privlock
    monkeypatch.setattr(privlock.os, "geteuid", lambda: 1000)
    refused = {"docker run": [["docker", "run", "img"], ["/usr/bin/docker", "run", "img"], ["docker", "-H", "unix:///s", "run", "x"],
                              ["docker", "--context=c", "run", "x"], ["env", "A=1", "docker", "run", "x"],
                              ["nice", "-n", "5", "docker", "run", "x"], ["ionice", "-c", "3", "docker", "run", "x"],
                              ["timeout", "5", "docker", "run", "x"], ["stdbuf", "-oL", "docker", "run", "x"],
                              ["nohup", "docker", "run", "x"], ["sh", "-c", "docker run x"], ["bash", "-c", "docker ps; docker run x"],
                              ["bash", "-lc", "y=$(docker run x)"], ["env", "-S", "docker run x"], ["env", "-Sdocker run x"],
                              ["env", "--split-string=docker run x"], ["env", "-S", "A=1", "docker", "run", "x"]],
               "docker exec": [["docker", "exec", "c", "sh"]], "docker container rm": [["docker", "container", "rm", "c"]],
               "podman run": [["podman", "--remote", "run", "x"], ["podman", "-r", "run", "x"], ["podman", "--url", "unix:///s", "run", "x"],
                              ["podman", "--connection=root", "run", "x"], ["sh", "-c", "podman --remote run x"]],
               "systemctl restart": [["systemctl", "restart", "u"], ["systemctl", "--system", "restart", "u"],
                                     ["systemctl", "--user", "--system", "restart", "u"], ["systemctl", "-H", "status", "restart", "u"],
                                     ["timeout", "9", "systemctl", "restart", "u"], ["bash", "-c", "systemctl restart u"]],
               "systemctl enable": [["systemctl", "--global", "enable", "u"]],
               "systemctl start": [["sh", "-c", "systemctl status u && systemctl start u"]],
               "service ssh restart": [["service", "ssh", "restart"]], "busctl call": [["busctl", "call", "a", "/b", "c", "d"]],
               "loginctl terminate-user": [["loginctl", "terminate-user", "x"]], "loginctl enable-linger": [["loginctl", "enable-linger"]],
               "machinectl poweroff": [["machinectl", "poweroff", "m"]], "machinectl read-only": [["machinectl", "read-only", "i"]]}
    for hit, cases in refused.items():
        for argv in cases:
            assert privlock.root_client(argv) == hit, argv
            assert privlock.refusal(argv).startswith(f"ttp lock does not run `{hit}` for a worker"), argv
            assert "ttp host-run" in privlock.refusal(argv), argv
    allowed = [["systemctl", "--user", "restart", "u"], ["systemctl", "restart", "--user", "u"], ["systemctl", "status", "u"],
               ["systemctl", "is-active", "u"], ["systemctl", "is-enabled", "u"], ["systemctl", "show", "-p", "MainPID", "u"],
               ["systemctl", "cat", "u"], ["systemctl", "list-units"], ["systemctl", "--no-pager", "list-timers"], ["systemctl"],
               ["systemctl", "-n", "50", "status", "u"], ["timeout", "5", "systemctl", "status", "u"],
               ["docker", "ps"], ["docker", "images"], ["docker", "inspect", "c"], ["docker", "logs", "-f", "c"],
               ["docker", "version"], ["docker", "info"], ["docker", "--help"], ["docker", "-H", "unix:///s", "ps"],
               ["docker", "container", "ls"], ["sh", "-c", "docker ps"], ["podman", "run", "x"], ["podman", "build", "."],
               ["podman", "--remote", "ps"], ["service", "ssh", "status"], ["service", "--status-all"],
               ["busctl", "--user", "call", "a", "/b", "c", "d"], ["busctl", "list"], ["busctl", "tree", "a"],
               ["loginctl", "list-sessions"], ["loginctl", "show-user", "x"], ["loginctl", "session-status"], ["loginctl"],
               ["machinectl", "list"], ["machinectl", "status", "m"], ["ssh", "box", "docker run x"], ["bash", "-c", "ssh h 'systemctl restart u'"],
               ["echo", "docker", "run"], ["grep", "systemctl restart", "f"], ["ttp", "host-run", "--", "systemctl", "restart", "u"]]
    for argv in allowed:
        assert privlock.root_client(argv) is None and privlock.refusal(argv) is None, argv
    # podman run as root uses the root socket and storage.
    monkeypatch.setattr(privlock.os, "geteuid", lambda: 0)
    assert privlock.root_client(["podman", "run", "x"]) == "podman run"
    assert privlock.root_client(["podman", "ps"]) is None


def test_worker_ttp_lock_refuses_a_root_service_client_and_the_user_shell_runs_it(env, tmp_path):
    p = make(env)
    bin_dir, log = tmp_path / "svcbin", tmp_path / "svc.log"
    bin_dir.mkdir()
    for name in ("docker", "systemctl"):
        f = bin_dir / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(log))}\n')
        f.chmod(0o755)
    run_dir = p.base / "state" / "runs" / "9"
    run_dir.mkdir(parents=True)
    path = f"{bin_dir}:{os.environ['PATH']}"
    worker = _lock_env(env, p, TTP_TASK="7", TTP_RUN_DIR=str(run_dir), PATH=path)
    for cmd in (["docker", "run", "x"], ["env", "A=1", "systemctl", "restart", "u"], ["sh", "-c", "systemctl stop u"]):
        r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", *cmd], env=worker, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 2 and "ttp host-run" in r.stderr and "root-owned service" in r.stderr, (cmd, r)
        assert not log.exists() and _queue_len(p, "rig") == 0, cmd
    for cmd in (["systemctl", "--user", "restart", "u"], ["docker", "ps"]):
        r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", *cmd], env=worker, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0 and log.read_text().splitlines()[-1] == " ".join(cmd), (cmd, r)
    user = {k: v for k, v in _lock_env(env, p, PATH=path).items()
            if k not in ("TTP_RUN_DIR", "TTP_RUN_ID", "TTP_NOTE_RUN_DIR")}
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig", "--", "docker", "run", "x"], env=user,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and log.read_text().splitlines()[-1] == "docker run x", r


def test_privileged_finds_sudo_in_command_position_only():
    from ttp import privlock
    hits = {"sudo": [["sudo", "x"], ["/usr/bin/sudo", "x"], ["nice", "-n", "5", "sudo", "x"], ["xargs", "-I", "{}", "sudo", "x"],
                     ["sh", "-c", "a && `sudo b`"], ["sh", "-c", "a | sudo tee f"], ["zsh", "-ec", "{ sudo x; }"],
                     ["busybox", "sh", "-c", "sudo x"], ["env", "-S", "sudo x"], ["sh", "-c", "if true; then sudo x; fi"], ["bash", "-c", "'sudo x"]],
            "run0": [["run0", "x"]], "doas": [["stdbuf", "-oL", "doas", "x"]]}
    for name, cases in hits.items():
        for argv in cases:
            assert privlock.privileged(argv) == name, argv
    for argv in (["ssh", "h", "sudo x"], ["bash", "-c", "ssh h 'a; sudo b'"], ["sh", "-c", "echo sudo"],
                 ["grep", "sudo", "f"], ["python3", "-c", "print(1)"], ["bash", "-c"], ["ttp", "host-run", "--", "sudo", "x"]):
        assert privlock.privileged(argv) is None, argv
    assert privlock.lock_command(["ttp", "lock", "--timeout", "5", "r", "--disruptive", "--", "sudo", "x"]) == ["sudo", "x"]
    assert privlock.lock_command(["ttp", "lock", "a", "--", "ttp", "lock", "b", "--", "su"]) == ["su"]
    assert privlock.lock_command(["ttp", "lock", "--probe", "r"]) is None


def test_worker_hook_points_sudo_under_ttp_lock_to_host_run(env, monkeypatch, tmp_path):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))

    def why(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason") or ""
    for cmd in ("ttp lock r -- sudo tt-smi -glx_reset", "timeout 60 ttp lock r -- bash -c 'sudo x'",
                "ttp lock --timeout 5 r --disruptive -- su -c y", "ttp lock a -- ttp lock b -- doas z",
                "ttp lock r pkexec z"):
        assert "ttp host-run" in why(cmd), cmd
    for cmd in ("ttp lock r -- ssh box 'sudo -n tt-smi -s'", "ttp lock r -- ttp host-run -- sudo -n x",
                "ttp lock --probe r", "ttp lock r -- make test"):
        assert "host-run" not in why(cmd), cmd


def test_a_lock_wait_notice_never_follows_a_symlinked_progress_log(env, tmp_path):
    p = make(env)
    run_dir = tmp_path / "wrun"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"stall_s": 2}))
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    (run_dir / "progress.md").symlink_to(target)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    try:
        _wait_for_file(held, holder)
        r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "true"], capture_output=True,
                           text=True, env={**run_env, "TTP_RUN_DIR": str(run_dir)}, timeout=60)
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert r.returncode == 75 and "refused to append to" in r.stderr, r.stderr
    assert target.read_text() == "keep me\n"
    rows = p.db.q("SELECT data FROM events WHERE kind='append_refused'")
    assert rows and "waiting for board" in json.loads(rows[0]["data"])["text"]


def test_a_signalled_lock_waiter_never_follows_a_symlinked_progress_log(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    held, go, run_dir = tmp_path / "held", tmp_path / "go", tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    (run_dir / "progress.md").symlink_to(target)
    h = _lock(run_env, "rig-a", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    w = None
    try:
        _wait_for_file(held, h)
        w = _lock(dict(run_env, TTP_TEST_POLL_S="30", TTP_RUN_DIR=str(run_dir)), "rig-a")
        _stderr_until(w, "waiting for")
        time.sleep(0.3)
        w.send_signal(signal.SIGTERM)
        assert w.wait(timeout=30) == 143, "the waiter still ends with the signal"
        err = w.stderr.read()
    finally:
        go.touch()
        if w and w.poll() is None:
            w.kill()
    assert h.wait(timeout=30) == 0
    assert f"SIGTERM received by pid {w.pid}" in err and "refused to append to" in err, err
    assert target.read_text() == "keep me\n"


_QUEUE_WAITER = r'''
import os, sys
from pathlib import Path
from ttp import locks
d, res, since, fork = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1"
t = locks.enqueue(d, res, "waiter", since=since)
if fork and os.fork() == 0:   # a child that keeps the ticket's open file, and so its lock, until stdin closes
    os.read(0, 1)
    os._exit(0)
print("ready", flush=True)
for _line in sys.stdin:
    print(locks.dropped(t), locks.run_ended(t), locks.my_turn(d, res, t, 99), flush=True)
'''


def _queue_waiter(d, res, since, run_env, fork=False):
    """A process queued on res (arrival `since`) once it says so; each line it reads, it answers whether
    its ticket was dropped, whether its run ended and whether it is its turn."""
    w = subprocess.Popen([sys.executable, "-c", _QUEUE_WAITER, str(d), res, str(since), "1" if fork else "0"],
                         env={**run_env, "PYTHONPATH": str(RUNTIME)}, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert w.stdout.readline() == "ready\n", w.stderr.read()
    return w


def _queue_ask(w):
    w.stdin.write("?\n")
    w.stdin.flush()
    return w.stdout.readline().split()


def _queue_plain_env():
    return {k: v for k, v in os.environ.items() if k not in ("TTP_RUN_DIR", "TTP_DETACHED", "TTP_OUTLIVES_RUN", "PYTHONPATH")}


def test_a_killed_waiters_queue_entry_is_dropped_even_when_a_child_keeps_its_lock_and_the_next_waiter_goes(
        tmp_path, monkeypatch):
    from ttp import locks
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    d, res = tmp_path / "locks", "suite"
    plain = _queue_waiter(d, res, 1.0, _queue_plain_env())
    forked = _queue_waiter(d, res, 2.0, _queue_plain_env(), fork=True)
    mine = locks.enqueue(d, res, "next", since=3.0)
    try:
        assert [t.name.split("-")[0] for t in locks.queued(d, res)] == ["0000000001.000000", "0000000002.000000",
                                                                       "0000000003.000000"]
        assert not locks.my_turn(d, res, mine, 1)
        stale = [t for t in locks.queued(d, res) if t != mine]
        for w in (plain, forked):
            w.kill()
            w.wait(timeout=60)
        assert locks._ticket_live(stale[1]), "the forked child no longer holds the killed waiter's lock"
        assert locks.queued(d, res) == [mine]
        assert not any(t.exists() or locks._owner_file(t).exists() for t in stale)
        assert locks.my_turn(d, res, mine, 1)
    finally:
        locks.dequeue(mine)
        for w in (plain, forked):
            w.kill()
            w.stdin.close()
            w.wait(timeout=60)


def test_a_waiter_whose_run_ended_loses_its_queue_place_and_never_takes_a_turn_but_a_detached_job_keeps_its(
        tmp_path, monkeypatch):
    from ttp import locks
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    d, res, run = tmp_path / "locks", "suite", tmp_path / "run"
    run.mkdir()
    in_run = _queue_waiter(d, res, 1.0, {**_queue_plain_env(), "TTP_RUN_DIR": str(run)})
    job = _queue_waiter(d, res, 2.0, {**_queue_plain_env(), "TTP_RUN_DIR": str(run), "TTP_DETACHED": "1"})
    mine = locks.enqueue(d, res, "next", since=3.0)
    try:
        assert len(locks.queued(d, res)) == 3 and _queue_ask(in_run) == ["False", "False", "True"]
        (run / "exit.json").write_text("{}")
        q = locks.queued(d, res)
        assert len(q) == 2 and q[1] == mine and locks._arrival(q[0]) == 2.0, q
        # the ended run's waiter, still alive, has no place and never takes a turn; it knows why
        assert _queue_ask(in_run) == ["True", "True", "False"]
        assert _queue_ask(job) == ["False", "False", "True"]
        assert not locks.my_turn(d, res, mine, 1) and locks.my_turn(d, res, mine, 2)
        # a check the daemon runs after the run ended, with the run's variables, is not tied to it
        assert locks._waits_for_run({"TTP_RUN_DIR": str(run)}) == ""
    finally:
        locks.dequeue(mine)
        for w in (in_run, job):
            w.kill()
            w.wait(timeout=60)


def test_a_reused_pid_never_keeps_a_queue_entry_and_a_live_or_unjudgeable_waiter_keeps_its_place(tmp_path,
                                                                                                monkeypatch):
    import fcntl
    from ttp import locks
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    d, res = tmp_path / "locks", "suite"
    qd = locks.queue_dir(d, res)
    qd.mkdir(parents=True)
    me = locks._owner(os.getpid())
    held = []

    def ticket(since, rec):
        # a ticket whose lock this test holds (as a process that inherited it would), with owner record rec
        path = qd / f"{since:017.6f}-{os.getpid()}"
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        held.append(f)
        if rec is not None:
            locks._owner_file(path).write_text(json.dumps(rec))
        return path

    try:
        reused = ticket(1.0, {**me, "pid_start": "1"})              # its pid now names another process
        live = ticket(2.0, me)                                     # the very process
        other_ns = ticket(3.0, {**me, "pid": 999999, "pidns": "pid:[1]"})   # another PID namespace: cannot tell
        other_host = ticket(4.0, {**me, "pid_start": "1", "node": me["node"] + "-elsewhere"})
        no_record = ticket(5.0, None)                              # an older runtime's ticket
        gone = ticket(6.0, {**me, "pid": 999999})                  # no such process
        mine = locks.enqueue(d, res, "mine", since=7.0)
        q = locks.queued(d, res)
        assert q == [live, other_ns, other_host, no_record, mine], q
        assert not reused.exists() and not gone.exists() and not locks._owner_file(reused).exists()
        assert locks.queued(d, res) == q   # read again: nobody moves
        assert [locks.my_turn(d, res, t, 2) for t in q] == [True, True, False, False, False]
        orphan = qd / f".{0.5:017.6f}-1{locks.OWNER_TICKET}"
        orphan.write_text("{}")
        locks.queued(d, res)
        assert not orphan.exists() and locks._owner_file(live).exists()
    finally:
        locks.dequeue(locks.queued(d, res)[-1])
        for f in held:
            f.close()


def test_a_ttp_lock_waiter_whose_run_ended_leaves_the_queue_and_exits_75(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    run_env.pop("TTP_DETACHED", None)
    go, started, run = tmp_path / "go", tmp_path / "started", tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text("{}")
    holder = _lock({k: v for k, v in run_env.items() if k != "TTP_RUN_DIR"}, "board",
                   cmd=("bash", "-c", f": > {shlex.quote(str(started))}; {_until(go)[-1]}"))
    procs = [holder]
    try:
        _until_or_ended(started.exists, procs)
        w = _lock({**run_env, "TTP_RUN_DIR": str(run)}, "board")
        procs.append(w)
        _until_or_ended(lambda: _queue_len(p, "board") == 1, procs)
        (run / "exit.json").write_text("{}")
        assert _queue_len(p, "board") == 0
        assert w.wait(timeout=60) == 75
        assert "the run this waited for has ended" in w.stderr.read()
    finally:
        go.touch()
    assert holder.wait(timeout=60) == 0


def test_a_later_landing_lock_waiter_goes_first_fifo_within_each_class_and_a_long_waiter_keeps_its_place(tmp_path):
    """locks.my_turn: landing waiters (LANDING_TICKET) go ahead of every routine non-landing one, in
    arrival order among themselves, in both the arrival and the fair order; an urgent waiter goes ahead of
    routine landings; the rest keep their order. A waiter that has waited LANDING_AGE_S is not passed by a
    later landing: no starvation."""
    from ttp import locks
    q = tmp_path / "q"
    now = time.time()
    routine = locks.enqueue(q, "gate", "proj-a suite", since=now - 30, group="proj-a")
    hot = locks.enqueue(q, "gate", "proj-a urgent suite", since=now - 20, priority=1, group="proj-a")
    land1 = locks.enqueue(q, "gate", "proj-b landing", since=now - 10, group="proj-b", landing=True)
    land2 = locks.enqueue(q, "gate", "proj-c landing", since=now - 5, group="proj-c", landing=True, multi=True,
                          priority=3)
    every = (routine, hot, land1, land2)
    try:
        assert [locks.is_landing(t) for t in every] == [False, False, True, True]
        assert locks.is_multi(land2) and locks.ticket_priority(land2) == 3 and locks.ticket_group(land2), \
            "older readers' fields still parse"
        for fair in (False, True):
            assert [locks.my_turn(q, "gate", t, 1, fair=fair) for t in every] == [False, True, False, False], fair
            assert [locks.my_turn(q, "gate", t, 2, fair=fair) for t in every] == [False, True, True, False], fair
            assert [locks.my_turn(q, "gate", t, 3, fair=fair) for t in every] == [False, True, True, True], fair
        assert locks.my_turn(q, "gate", hot, 3) and not locks.my_turn(q, "gate", routine, 3), "urgent, then arrival"
        locks.dequeue(land1)
        locks.dequeue(land2)
        assert locks.my_turn(q, "gate", hot, 1) and not locks.my_turn(q, "gate", routine, 1), "no landing: as before"
        old = locks.enqueue(q, "gate", "proj-a long suite", since=now - locks.LANDING_AGE_S - 60, group="proj-a")
        late = locks.enqueue(q, "gate", "proj-b landing", since=now, group="proj-b", landing=True)
        every += (old, late)
        assert locks.my_turn(q, "gate", old, 1) and not locks.my_turn(q, "gate", late, 1), "aged: not passed"
        assert locks.my_turn(q, "gate", hot, 2) and not locks.my_turn(q, "gate", late, 2), "urgent first"
        assert locks.my_turn(q, "gate", late, 3) and not locks.my_turn(q, "gate", routine, 3)
        shared = locks.enqueue(q, "gate", "proj-d landing", shared=True, landing=True)
        every += (shared,)
        assert locks.is_landing(shared) and locks.is_shared(shared)
    finally:
        for t in every:
            locks.dequeue(t)


def test_an_urgent_lock_waiter_goes_ahead_of_routine_landings_and_an_urgent_landing_first(tmp_path):
    """locks.landing_first: with a landing waiting, urgent waiters (priority 1) go ahead of routine
    landings, urgent landings first, both by arrival; routine landings, then the rest. The aged bound
    still leads. A running holder is never stopped: only the queue order changes."""
    from ttp import locks
    q = tmp_path / "q"
    now = time.time()
    every = (routine := locks.enqueue(q, "gate", "a suite", since=now - 50, priority=3, group="a"),
             land := locks.enqueue(q, "gate", "b landing", since=now - 40, priority=3, group="b", landing=True),
             hot := locks.enqueue(q, "gate", "c urgent suite", since=now - 30, priority=1, group="c"),
             hot_land := locks.enqueue(q, "gate", "d urgent landing", since=now - 20, priority=1, group="d",
                                       landing=True))
    try:
        for fair in (False, True):
            order = [t for n in range(1, 5) for t in every
                     if locks.my_turn(q, "gate", t, n, fair=fair, urgent_max=2)
                     and not locks.my_turn(q, "gate", t, n - 1, fair=fair, urgent_max=2)]
            assert order == [hot_land, hot, land, routine], fair
        assert locks.landing_first([routine, land, hot], now) == [hot, land, routine]
        assert locks.landing_first([routine, hot], now) == [routine, hot], "no landing: unchanged"
        old = locks.enqueue(q, "gate", "e long suite", since=now - locks.LANDING_AGE_S - 60, group="e")
        every += (old,)
        assert locks.landing_first(locks.queued(q, "gate"), now)[:2] == [old, hot_land], "aged still leads"
    finally:
        for t in every:
            locks.dequeue(t)


@pytest.mark.parametrize("hot_batch", [True, False], ids=["urgent-batch", "routine-batch"])
def test_a_push_batch_carrying_an_urgent_approval_lands_ahead_of_an_earlier_urgent_waiter(env, tmp_path, hot_batch):
    """A push batch's checks have no TTP_TASK: Batch._urgent_mark (an entry covers an urgent approval)
    labels the landing mark batch._stream holds urgent, and its `ttp lock` waiter, verified under that
    mark, carries the urgent rank: it goes ahead of an earlier urgent non-landing waiter. A routine
    batch's landing still goes after it (locks.landing_first). A forged mark gives no rank at all
    (landmark.label is the verified one: test_only_a_held_landing_mark_of_an_ancestor_makes_a_landing_waiter)."""
    import threading
    import types
    from ttp import batch, landmark
    p = make(env)
    hot = p.db.add_task("hot", "s\nurgent: the user is waiting", kind="code", origin="coordinator")
    routine = p.db.add_task("routine", "s", kind="code", origin="coordinator")
    shape = types.SimpleNamespace(p=p, entries=[{"task": routine}] + ([{"task": hot}] if hot_batch else []))
    assert batch.Batch._urgent_mark(shape) is hot_batch
    marks, go, go_b = tmp_path / "marks", tmp_path / "go", tmp_path / "go-b"
    env_b = _lock_env(env, p, PYTHONPATH=str(RUNTIME), TTP_PUSH_MODE="target")
    env_b.pop("TTP_TASK", None)
    late = [sys.executable, str(TTP), "lock", "gate", "--", *_mark(marks, "land", go_b)]
    got = []
    t = threading.Thread(target=lambda: got.append(batch._stream(late, tmp_path, env=env_b, urgent_mark=hot_batch)),
                         daemon=True)
    holder = _lock(_lock_env(env, p), "gate", cmd=_mark(marks, "holder", go))
    procs = [holder]
    try:
        _until_or_ended(lambda: marks.exists() and "start holder" in marks.read_text(), procs)
        procs.append(_lock(_lock_env(env, p, TTP_TASK=str(hot)), "gate", cmd=_mark(marks, "hot")))
        _until_or_ended(lambda: _queue_len(p, "gate") == 1, procs)
        t.start()
        _until_or_ended(lambda: _queue_len(p, "gate") == 2, procs)
        go.touch()
        _until_or_ended(lambda: "start land" in marks.read_text() or "start hot" in marks.read_text(), procs[1:])
        go_b.touch()
        t.join(60)
        assert not t.is_alive() and got and got[0][0] == 0, got
        assert _ended(procs) == [0, 0]
    finally:
        go.touch()
        go_b.touch()
        for x in procs:
            if x.poll() is None:
                x.kill()
        t.join(60)
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start ")]
    assert starts == (["holder", "land", "hot"] if hot_batch else ["holder", "hot", "land"]), starts
    assert list(landmark.marks_dir().iterdir()) == []


def _mark_holder(env):
    """A process outside the waiter's ancestry holding a real landing mark; (process, mark path)."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys, time\nfrom ttp import landmark\nm = landmark.hold()\n"
                             "print(m.path, flush=True)\nsys.stdin.read()"], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().strip()


def _verified(env):
    r = subprocess.run([sys.executable, "-c", "from ttp import landmark; print(landmark.verified())"], env=env,
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def test_only_a_held_landing_mark_of_an_ancestor_makes_a_landing_waiter(env, tmp_path):
    """landmark.verified: true only for a process under the mark's holder; no mark, a file nobody holds, a held
    file outside the marks folder, another process's mark or a label naming the waiter's ancestor written
    into a file nobody holds are all non-landing."""
    import fcntl
    from ttp import landmark, locks
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), PYTHONPATH=str(RUNTIME))
    mine = landmark.hold()
    assert mine is not None and mine.path.parent == landmark.marks_dir()
    other, theirs = _mark_holder(run_env)
    loose = landmark.marks_dir() / "forged.lock"
    loose.write_text(json.dumps({"since": time.time(), **locks._owner(os.getpid())}))
    outside = tmp_path / "outside.lock"
    held_outside = open(outside, "a+")
    fcntl.flock(held_outside, fcntl.LOCK_EX | fcntl.LOCK_NB)
    held_outside.write(json.dumps(locks._owner(os.getpid())))
    held_outside.flush()
    try:
        assert _verified(dict(run_env, **{landmark.ENV: str(mine.path)})) == "True"
        assert not landmark.verified({landmark.ENV: str(mine.path)}), "the holder itself is no waiter under it"
        for forged in ("", str(loose), str(outside), theirs, str(landmark.marks_dir() / "missing.lock")):
            assert _verified(dict(run_env, **{landmark.ENV: forged})) == "False", forged
    finally:
        other.stdin.close()
        other.wait(timeout=60)
        held_outside.close()
        loose.unlink()
        mine.close()
    assert not mine.path.exists()
    assert _verified(dict(run_env, **{landmark.ENV: str(mine.path)})) == "False", "let go: no landing"


def test_a_wait_line_names_a_holders_project_from_its_root_not_its_project_field(tmp_path):
    """locks.holder_kind: the project comes from the record's project_root (a lock's tt-project folder or a
    bare root), never from its project field, which may name only the harness folder; a record with no root
    says so. Whether it is a landing comes only from the label's landing field."""
    from ttp import locks
    root = tmp_path / "proj-x"
    rec = {"holder": "tt-project #7 run 9", "project": "tt-project", "project_root": str(root / "tt-project")}
    assert locks.holder_kind(rec) == f"not a landing, project at {root}"
    assert locks.holder_kind({**rec, "landing": True}) == f"a landing, project at {root}"
    assert locks.holder_kind({**rec, "project_root": str(root)}) == f"not a landing, project at {root}"
    assert locks.holder_kind({"holder": "x", "project": "proj-y"}) == "not a landing, no project recorded"
    assert locks.holder_kind({"landing": "yes", "project_root": ""}) == "a landing, no project recorded"


@pytest.mark.parametrize("real", [True, False], ids=["landing", "forged"])
def test_a_landing_ttp_lock_from_another_project_takes_a_shared_lock_before_an_earlier_suite_waiter(env, tmp_path, real):
    """Two projects share a lock. Project A's suite waits first, project B's waiter queues later: it gets the
    lock first when it runs under a real landing mark of its parent (and its label says landing), and in
    arrival order when its marker is forged (a mark another process holds)."""
    from ttp import landmark, locks
    a, b = make(env, "proj-a"), _second_project(env, "proj-b")
    for p in (a, b):
        p.set_config("shared_resources", ["gate"])
    marks, go, go_b = tmp_path / "marks", tmp_path / "go", tmp_path / "go-b"
    env_a, env_b = _lock_env(env, a), _lock_env(env, b, PYTHONPATH=str(RUNTIME))
    other = mark = None
    if real:
        mark = landmark.hold()
        env_b[landmark.ENV] = str(mark.path)
    else:
        other, env_b[landmark.ENV] = _mark_holder(env_b)
    holder = _lock(env_a, "gate", cmd=_mark(marks, "holder", go))
    procs = [holder]
    try:
        _until_or_ended(lambda: marks.exists() and "start holder" in marks.read_text(), procs)
        procs.append(_lock(env_a, "gate", cmd=_mark(marks, "suite")))
        _until_or_ended(lambda: _queue_len(a, "gate") == 1, procs)
        procs.append(_lock(env_b, "gate", cmd=_mark(marks, "late", go_b)))
        _until_or_ended(lambda: _queue_len(a, "gate") == 2, procs)
        go.touch()
        first = "start late" if real else "start suite"
        _until_or_ended(lambda: first in marks.read_text(), procs[1:])
        if real:
            row = json.loads(_lock_probe(env_a, "gate", extra=("--json",)).stdout)
            assert [h.get("landing") for h in row["holders"]] == [True], row
            assert locks.holder_kind(row["holders"][0]) == f"a landing, project at {b.root.resolve()}", row
        go_b.touch()
        assert _ended(procs) == [0, 0, 0]
    finally:
        go.touch()
        go_b.touch()
        for x in procs:
            if x.poll() is None:
                x.kill()
        if other:
            other.stdin.close()
            other.wait(timeout=60)
        if mark:
            mark.close()
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start ")]
    assert starts == (["holder", "late", "suite"] if real else ["holder", "suite", "late"]), starts


def test_only_a_landing_check_runs_under_a_landing_mark(env, tmp_path):
    """push._run_one: a check in TTP_PUSH_MODE target (the push branch, the push queue, `ttp land`) runs under
    a landing mark a `ttp lock` in it verifies; an own-branch check does not, nor does it pass on a mark it
    was given; the mark is gone once the check ends."""
    from ttp import landmark, push
    out = tmp_path / "seen"
    cmd = push.Check(f"{shlex.quote(sys.executable)} -c 'from ttp import landmark; print(landmark.verified())' "
                     f">> {shlex.quote(str(out))}")
    cmd.sandbox = False
    inherited = landmark.hold()
    base = dict(os.environ, TTP_HOME=str(env["home"]), PYTHONPATH=str(RUNTIME), **{landmark.ENV: str(inherited.path)})
    try:
        for mode in ("target", "own", "checks"):
            assert push._run_one(cmd, tmp_path, dict(base, TTP_PUSH_MODE=mode), lambda m: None) == 0, mode
    finally:
        inherited.close()
    assert out.read_text().split() == ["True", "False", "False"]
    assert list(landmark.marks_dir().iterdir()) == []


@pytest.mark.parametrize("mode", ["target", "own"])
def test_a_push_batch_checks_ttp_lock_goes_ahead_of_an_earlier_suite_waiter_only_for_the_push_branch(env, tmp_path, mode):
    """batch._stream (every push batch check: Batch._run_check, its reruns and cap probes): a check in
    TTP_PUSH_MODE target runs under a landing mark the batch process holds, so its `ttp lock` from another
    project is queued as a landing and takes a shared lock before an earlier suite waiter. Any other check
    waits in arrival order, even given a held mark of the batch process; the mark is gone once it ends."""
    import threading
    from ttp import batch, landmark
    a, b = make(env, "proj-a"), _second_project(env, "proj-b")
    for p in (a, b):
        p.set_config("shared_resources", ["gate"])
    marks, go, go_b = tmp_path / "marks", tmp_path / "go", tmp_path / "go-b"
    env_a = _lock_env(env, a)
    inherited = landmark.hold()
    env_b = _lock_env(env, b, PYTHONPATH=str(RUNTIME), TTP_PUSH_MODE=mode, **{landmark.ENV: str(inherited.path)})
    late = [sys.executable, str(TTP), "lock", "gate", "--", *_mark(marks, "late", go_b)]
    got = []
    t = threading.Thread(target=lambda: got.append(batch._stream(late, tmp_path, env=env_b)), daemon=True)
    holder = _lock(env_a, "gate", cmd=_mark(marks, "holder", go))
    procs = [holder]
    try:
        _until_or_ended(lambda: marks.exists() and "start holder" in marks.read_text(), procs)
        procs.append(_lock(env_a, "gate", cmd=_mark(marks, "suite")))
        _until_or_ended(lambda: _queue_len(a, "gate") == 1, procs)
        t.start()
        _until_or_ended(lambda: _queue_len(a, "gate") == 2, procs)
        if mode == "target":
            assert len(list(landmark.marks_dir().iterdir())) == 2, "the batch process holds a mark while it runs"
        go.touch()
        _until_or_ended(lambda: "start late" in marks.read_text() or "start suite" in marks.read_text(), procs[1:])
        seen = marks.read_text()
        assert (seen.find("start late") % 10**6 < seen.find("start suite") % 10**6) == (mode == "target"), seen
        go_b.touch()
        t.join(60)
        assert not t.is_alive() and got and got[0][0] == 0, got
        assert _ended(procs) == [0, 0]
    finally:
        go.touch()
        go_b.touch()
        for x in procs:
            if x.poll() is None:
                x.kill()
        t.join(60)
        inherited.close()
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start ")]
    assert starts == (["holder", "late", "suite"] if mode == "target" else ["holder", "suite", "late"]), starts
    assert list(landmark.marks_dir().iterdir()) == []
