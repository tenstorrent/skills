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


def _nested_lock(flag, res):
    """A command that waits for `flag`, then runs a `ttp lock <res> -- true` inside it."""
    inner = shlex.join([sys.executable, str(TTP), "lock", res, "--", "true"])
    return ("bash", "-c", f"while [ ! -e {shlex.quote(str(flag))} ]; do {_nap()}; done; {inner}")


def test_a_lock_holders_nested_lock_never_waits_behind_a_multi_waiter_for_its_own_lock(env, tmp_path):
    # Bug: a first-in-line `ttp lock rig-a,rig-b` waiter kept rig-b's queue, while rig-a's holder ran
    # `ttp lock rig-b` inside its command: each waited for the other, forever.
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    flag = tmp_path / "flag"
    h = _lock(run_env, "--timeout", "0", "rig-a", cmd=_nested_lock(flag, "rig-b"))
    procs = [h]
    try:
        assert _until_true(lambda: any(x.startswith("rig-a: ") for x in shared.held(p)))
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        assert _until_true(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1)
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
    go = tmp_path / "go"
    run_hold = _lock(run_env, "rig-a", cmd=_until(go))   # stands for the exclusive task's whole-run hold of rig-a
    procs = [run_hold]
    try:
        assert _until_true(lambda: any(x.startswith("rig-a: ") for x in shared.held(p)))
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        assert _until_true(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1)
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
