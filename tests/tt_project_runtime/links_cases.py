# Outgoing-message link tests (links.py, the outgoing boundaries and the check command) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new link tests here.
from __future__ import annotations

CFG = {"jira_base": "https://jira.example.com", "jira_keys": ["EX"],
       "repos": {"smi": "example-org/example-smi"}, "single_repo": "",
       "file_url": lambda p: f"http://127.0.0.1:9/#token=ab&file={p}" if p.endswith(".json") else None,
       "is_file": lambda p: p.endswith(".json"),
       "ref_url": lambda n, kind: f"http://127.0.0.1:9/#token=ab&{kind or 'task'}={n}" if n == 5 else None}


def test_linkify_turns_each_reference_shape_into_a_link():
    from ttp import links
    out = links.linkify("EX-7 fixed, see smi#12 and example-org/example-repo#3, "
                        "state/runs/9/result.json and the code span `EX-7` stays", CFG)
    assert "[EX-7](https://jira.example.com/browse/EX-7)" in out
    assert "[smi#12](https://github.com/example-org/example-smi/issues/12)" in out
    assert "[example-org/example-repo#3](https://github.com/example-org/example-repo/issues/3)" in out
    assert "[state/runs/9/result.json](http://127.0.0.1:9/#token=ab&file=state/runs/9/result.json)" in out
    assert "`EX-7`" in out                 # a code span is never linkified


def test_linkify_links_pr_words_only_with_one_repo_and_a_number_is_an_id_link():
    from ttp import links
    assert "](https://github.com/example-org/one/issues/9)" in \
        links.linkify("PR #9", {"single_repo": "example-org/one"})
    assert links.linkify("PR #9", {"single_repo": ""}) == "PR #9"          # no single repo: stays
    # a '#N' is a tt-project id: it links to its web app page
    assert links.linkify("see #5", CFG) == "see [#5](http://127.0.0.1:9/#token=ab&task=5)"
    for text, kind in (("task #5", "task"), ("run #5", "run"), ("review #5", "task"), ("ask #5", "msg")):
        out = links.linkify(text, CFG)
        assert f"&{kind}=5" in out and f"[{text}]" in out, (text, out)
    assert links.linkify("run 5", CFG) == "run 5"   # without '#', a short number is a count
    # one that resolves to nothing is labelled, never left bare and never given a guessed repo
    assert links.linkify("see #1234", CFG) == "see #1234 (unresolved reference)"
    assert links.linkify("task #4", CFG) == "task #4"      # kinded and unresolved: its kind labels it
    assert links.linkify("plain text with no reference", CFG) == "plain text with no reference"
    # no resolver: the caller promised nothing about ids, so nothing changes
    assert links.linkify("see #5", {k: v for k, v in CFG.items() if k != "ref_url"}) == "see #5"
    # a second pass adds nothing
    once = links.linkify("task #5 needs a look", CFG)
    assert links.linkify(once, CFG) == once


def test_message_ids_never_become_task_links():
    from ttp import links
    tasks = {9675, 9707, 9720, 9733, 4311}   # 4311 is also a chat message id below
    cfg = {"ref_ok": lambda n, kind: n == 4311 if kind == "msg" else kind in ("", "task") and n in tasks,
           "ref_url": lambda n, kind: f"http://w/#task={n}" if kind in ("", "task") and n in tasks - {4311} else None}
    for text in ("Re #4311: two things", "user msg #4311 asked", "message #4311", "msg #9733", "Re: #9733"):
        assert links.linkify(text, cfg) == text, text   # a chat message's id never links
    assert links.linkify("see #4311", cfg) == "see #4311"   # a bare id a message also has: plain, unlabelled
    assert links.linkify("task #9733 done", cfg) == "[task #9733](http://w/#task=9733) done"
    out = links.linkify("tasks #9675, #9707 and #9720 landed", cfg)
    assert out == ("[tasks #9675](http://w/#task=9675), [#9707](http://w/#task=9707) and "
                   "[#9720](http://w/#task=9720) landed"), out
    assert links.linkify(out, cfg) == out
    assert links.problems("Re #4311, see #4311 and tasks #9675, #9707", cfg) == []
    assert "#99" in " ".join(links.problems("tasks #9675 and #99", cfg))
    once = links.linkify("tasks #9675 and #99, run #1 and #9733", cfg)   # a list id not of its word's kind
    assert "#99 (unresolved reference)" in once and "[#9733](http://w/#task=9733)" in once, once
    assert links.linkify(once, cfg) == once                              # reads the same on a second pass


def test_linkify_never_links_twice_or_inside_protected_spans():
    from ttp import links
    text = "EX-7 and smi#12 plus tests/x.py:3, already https://example.org/EX-7 and [see EX-7](https://e/EX-7)"
    cfg = {**CFG, "file_url": lambda p: "http://127.0.0.1:9/#token=ab&file=" + p}
    once = links.linkify(text, cfg)
    assert links.linkify(once, cfg) == once          # idempotent
    assert "https://example.org/EX-7" in once and "[see EX-7](https://e/EX-7)" in once
    assert "[tests/x.py:3]" in once and "(http://127.0.0.1:9/#token=ab&file=tests/x.py)" in once


def test_lookalikes_never_link():
    from ttp import links
    text = "KEY-1 X-5 abcdef0123456789 2026-11-23 and example/other stay"
    assert links.linkify(text, CFG) == text


def test_problems_names_the_three_shapes_and_the_bare_number():
    from ttp import links
    text = "MINFRA-2596 landed, #288 and #64844 too, plus tt-project/state/runs/20240/result.json"
    none = {"repos": {}, "jira_keys": ["MINFRA"], "file_url": None, "is_file": lambda p: True,
            "single_repo": "", "jira_base": ""}
    out = " ".join(links.problems(text, none))
    for what in ("MINFRA-2596", "#288", "#64844", "result.json"):
        assert what in out, out
    full = {"jira_base": "https://jira.example.com", "jira_keys": ["MINFRA"],
            "repos": {}, "single_repo": "example-org/example-smi",
            "file_url": lambda p: "http://127.0.0.1:9/#token=ab&file=" + p, "is_file": lambda p: True,
            "ref_url": lambda n, kind: "x" if n in (288, 64844) else None}
    assert links.problems(text, full) == []


def test_readable_serves_only_tracked_files_and_run_outputs_never_credentials(tmp_path, monkeypatch):
    import subprocess
    from ttp import credguard, links, project
    home, root = tmp_path / "home", tmp_path / "proj"
    monkeypatch.setenv("HOME", str(home))
    state = root / "tt-project/state"
    (state / "runs/1").mkdir(parents=True)
    (state / "runs/1/result.json").write_text("{}")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src.py").write_text("x = 1\n")
    (root / "untracked.txt").write_text("u")
    subprocess.run(["git", "-C", str(root), "add", "src.py"], check=True)
    cred = sorted(Path(x) for x in credguard.credential_files() if str(x).startswith(str(home)))[0]
    fakes = {"cred": cred, "store": home / "store/s.json",
             "token": state / "web.token", "db": root / "tt-project.db", "gitcfg": root / ".git/config"}
    for f in fakes.values():
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.name != "config":
            f.write_text("fake")
    subprocess.run(["git", "-C", str(root), "add", "-f", str(fakes["db"])], check=True)   # tracked, still refused
    monkeypatch.setattr(project, "secrets_path", lambda: fakes["store"])

    class P:
        pass
    P.root, P.state = root, state
    assert links.readable(P, "src.py") == (root / "src.py").resolve()            # tracked source
    assert links.readable(P, str(state / "runs/1/result.json")) is not None      # a run's output
    assert links.readable(P, "untracked.txt") is None                           # not tracked
    assert links.readable(P, "/etc/passwd") is None and links.readable(P, "../x") is None
    for what, f in fakes.items():
        assert links.readable(P, str(f)) is None, what


def test_the_listener_line_and_status_ask_linkify_outgoing_text(env):
    p = make(env)
    p.set_config("links.jira_base", "https://jira.example.com")
    p.set_config("links.jira_keys", ["EX"])
    p.set_config("links.repos", {"smi": "example-org/example-smi"})
    from ttp import cli, links
    m = {"id": 1, "kind": "reply", "severity": "normal", "chat": None, "text": "EX-7 done, see smi#1"}
    assert "(https://jira.example.com/browse/EX-7)" in cli._listen_line(p, m)
    assert "(https://github.com/example-org/example-smi/issues/1)" in cli._listen_line(p, m)
    assert links.for_user(p, "PR #1", links.TEXT) == "PR #1"   # no single repo: nothing to link
    p.db.post("out", "ask about EX-7", kind="ask", severity="normal")
    lines = cli.status_text(p).splitlines()
    assert any("needs you" in l and "(https://jira.example.com/browse/EX-7)" in l for l in lines), lines


def _web_app(proj):
    """A live web app for `proj` (a Project, or any object with a .name): the port it answers on.
    Only /api/name of it is asked here, so a fake project object is enough for a foreign app."""
    import http.server
    import threading
    from ttp import web
    handler = type("Handler", (web.Handler,), {"daemon_ref": type("H", (), {"p": proj})})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stop_web_apps(*servers):
    """Stop a test's web apps, so no server thread or port outlives the test."""
    for s in servers:
        s.shutdown()
        s.server_close()


def test_the_web_app_linkifies_its_messages_and_asks(env):
    p = make(env)
    p.set_config("links.jira_base", "https://jira.example.com")
    p.set_config("links.jira_keys", ["EX"])
    p.db.post("out", "EX-8 shipped", kind="reply", severity="normal")
    p.db.post("out", "asking about EX-9", kind="ask", severity="normal")
    import http.server
    import json
    import threading
    import urllib.error
    import urllib.request
    from ttp import web
    holder = type("Holder", (), {"p": p})
    handler = type("Handler", (web.Handler,), {"daemon_ref": holder})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tok = web.token(p).strip()
        def get(path, tok=tok):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                                         headers={"X-TTP-Token": tok} if tok else {})
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        p.db.set_kv("web", {"port": 9})   # a web app link exists: ids resolve to its pages
        task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
        p.db.post("out", f"task #{task} finished", kind="reply", severity="normal")
        rows = [r for r in get("/api/messages") if "EX-8" in r["text"]]
        assert rows and "](https://jira.example.com/browse/EX-8)" in rows[0]["text"], rows
        # an id reference resolves from the web app's own thread and connection, not p.db
        id_rows = [r for r in get("/api/messages") if f"#{task}" in r["text"]]
        assert id_rows and f"#task={task}" in id_rows[0]["text"], id_rows
        assert any("](https://jira.example.com/browse/EX-9)" in a["text"]
                   for a in web.state_payload(p, p.db)["attention"])
        # a file link opens exactly what /api/file serves, and nothing outside the project tree
        (p.state / "runs/1").mkdir(parents=True, exist_ok=True)
        (p.state / "runs/1/result.json").write_text('{"status": "done"}')
        web.token(p)
        row = get(f"/api/file?path={urllib.parse.quote(str(p.state / 'runs/1/result.json'))}")
        assert row["text"] == '{"status": "done"}'
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/file?path=/etc/passwd")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=10)
    finally:
        _stop_web_apps(srv)


def test_ids_link_to_their_web_app_page_and_unresolved_ones_get_labelled(env):
    p = make(env)
    p.db.set_kv("web", {"port": 8123})    # a web app link exists: id references can resolve
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.x("INSERT INTO runs(id,task,role) VALUES(?,?,?)", (777, task, "worker"))
    msg = p.db.post("out", "task %d is running" % task, kind="reply", severity="normal")
    from ttp import links
    out = links.for_user(p, f"#4 or task #{task} and run 777 and ask #{msg} and review #{task}")
    assert f"#{task}" in out and "](" in out and f"#task={task}" in out, out  # a task id resolves
    assert "run 777" in out and "#run=777" in out, out                   # a run id
    assert f"ask #{msg}" in out and f"#msg={msg}" in out, out            # an ask: its message page
    assert f"[review #{task}](http://127.0.0.1:8123/#task={task})" in out, out  # a review names its task
    assert "#4 (unresolved reference)" in out, out                       # an id that matches nothing
    assert "(unresolved" not in links.for_user(p, f"task #{task}")          # resolution is not labelling
    p.set_config("links.web_base", "https://example.org/app")
    assert f"https://example.org/app/#task={task}" in links.for_user(p, f"task #{task}")
    assert links.problems("#4 needs a look", links.config(p)) and links.problems(f"task #{task} done", links.config(p)) == []
    assert links.for_user(p, f"see #{msg}") == f"see #{msg}"   # a bare id a chat message has: never a task link
    other = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "u", ""))
    while p.db.q("SELECT 1 FROM messages WHERE id=?", (other,)):
        other = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "u", ""))
    assert f"#task={other}" in links.for_user(p, f"see #{other}")   # one only a task has still links


def test_an_alert_text_reaches_the_user_linkified_from_a_fresh_connection(env):
    from ttp import notifier, weblink
    p = make(env)
    srv = _web_app(p)                      # a web app this machine reaches and that answers as p
    p.db.set_kv("web", {"port": srv.server_address[1]})
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.post("out", f"alert about task #{task}", kind="alert", severity="high")
    try:
        rows = [r for r in notifier.alerts_since(p, 0, "high") if not r.get("cleared")]
        assert rows and f"#{task} (" in rows[0]["text"] and f"#task={task}" in rows[0]["text"], rows
        weblink._ANSWERED.clear()
    finally:
        _stop_web_apps(srv)


def test_the_check_command_names_what_a_message_left_unlinked(env, capsys):
    p = make(env)
    p.set_config("links.jira_keys", ["EX"])
    (p.root / "tt-project/state/runs/5").mkdir(parents=True, exist_ok=True)
    (p.root / "tt-project/state/runs/5/result.json").write_text("{}")
    p.db.post("out", "EX-1 and #2 and #3 need a look, tt-project/state/runs/5/result.json too",
              kind="reply", severity="normal")
    from ttp import cli
    a = types.SimpleNamespace(name=p.name, file=None)
    with pytest.raises(SystemExit) as e:
        cli.cmd_links(a)
    assert e.value.code == 1
    out = capsys.readouterr().out
    for shape in ("EX-1", "#2", "#3", "result.json"):
        assert shape in out, out


def test_problems_reports_no_ticket_without_jira_keys(env):
    from ttp import cli, links
    none = {"repos": {}, "jira_keys": [], "file_url": None, "is_file": lambda p: False,
            "single_repo": "", "jira_base": "", "ref_url": lambda n, kind: None}
    assert links.problems("-3 and x-12 and run-4", none) == []
    assert "ABC-12" in " ".join(links.problems("ABC-12 broke", dict(none, jira_keys=["ABC"])))
    p = make(env)
    p.db.post("out", "retry -3 then x-12", kind="reply", severity="normal")
    cli.cmd_links(types.SimpleNamespace(name=p.name, file=None))   # exits 0: no SystemExit


def test_no_outgoing_link_carries_the_web_token_and_a_second_pass_changes_nothing(env):
    from ttp import links, web
    p = make(env)
    p.db.set_kv("web", {"port": 8123})
    tok = web.token(p).strip()
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.x("INSERT INTO runs(id,task,role) VALUES(?,?,?)", (777, task, "worker"))
    (p.state / "runs/777").mkdir(parents=True, exist_ok=True)
    (p.state / "runs/777/result.json").write_text("{}")
    text = f"task #{task}, run 777, review #{task} and {p.state / 'runs/777/result.json'}"
    for fmt in (links.MD, links.TEXT):
        out = links.for_user(p, text, fmt)
        assert out.count("http://127.0.0.1:8123/#") == 4, out
        assert tok not in out and "token" not in out, out
        assert links.for_user(p, out, fmt) == out                # idempotent in both shapes
    p.set_config("links.web_base", "https://example.org/app")
    assert tok not in links.for_user(p, f"task #{task}")
    assert links.for_user(p, "the queue runs 4 at a time, task 2 of 5") == "the queue runs 4 at a time, task 2 of 5"


def test_the_web_app_keeps_its_token_for_token_free_links():
    import pathlib
    import ttp
    js = (pathlib.Path(ttp.__file__).parent / "web/app.js").read_text()
    assert 'localStorage.getItem("ttp_token")' in js and 'localStorage.setItem("ttp_token"' in js
    assert 'Q.get("task")' in js   # a #task=N link opens its page


def test_the_git_cache_follows_the_pushed_commit_not_the_ref_name(tmp_path):
    import subprocess
    from ttp import links
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                    "https://github.com/example-org/example-repo.git"], check=True)

    def commit(name):
        (root / name).write_text("x\n")
        for args in (["add", name], ["-c", "user.email=t@example.com", "-c", "user.name=t",
                                     "commit", "-m", "add " + name]):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

    class P:
        def config(self):
            return {"delivery": {"push_branch": "origin/main"}}
    P.root, P.state = str(root), root / "tt-project/state"
    p = P()

    def cfg():
        return links.config(p)

    sha1 = commit("old.txt")
    subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", sha1], check=True)
    assert cfg()["file_url"]("old.txt") == f"https://github.com/example-org/example-repo/blob/{sha1}/old.txt"
    sha2 = commit("new.txt")
    subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", sha2], check=True)
    out = cfg()   # the tracking ref kept its name: the cache must follow the commit it points at
    assert out["file_url"]("new.txt") == f"https://github.com/example-org/example-repo/blob/{sha2}/new.txt"
    assert links._GIT_CACHE[str(root)][0][0] == sha2   # bounded: the latest commit per repo


def test_a_viewer_s_links_open_only_its_own_project_s_web_app(env, monkeypatch):
    """The replay of the real case: on the viewer's machine the project host's own port is
    another project's web app, so a link built on it opens the wrong project."""
    import types
    from ttp import cli, links, tunnel, weblink
    p = make(env)
    weblink._ANSWERED.clear()
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    m = {"id": 1, "kind": "reply", "severity": "normal", "chat": 0, "text": f"task #{task} done"}
    app = _web_app(p)                                   # this project's web app
    mine = _web_app(types.SimpleNamespace(name="other"))   # what the host's own port serves here
    p.db.set_kv("web", {"port": mine.server_address[1]})

    try:
        # the viewer's kept tunnel: its local port answers as this project, so links build on it
        monkeypatch.setattr(tunnel, "installed", lambda name: {"local": app.server_address[1]})
        line = cli._listen_line(p, m)
        assert f"(http://127.0.0.1:{app.server_address[1]}/#task={task})" in line, line
        assert f":{mine.server_address[1]}/" not in line, line

        # no kept tunnel, and the project's own port answers as another project: no URL at all
        monkeypatch.setattr(tunnel, "installed", lambda name: None)
        line = cli._listen_line(p, m)
        assert "#task=" not in line and f"task #{task}" in line, line

        # a viewer on another machine with no checked base: this host's port is never printed
        monkeypatch.setenv("SSH_CONNECTION", "203.0.113.10 55555 22")
        line = cli._listen_line(p, m)
        assert "http://" not in line, line

        # a forwarder's checked base: the host builds its links on it and records the proof
        monkeypatch.delenv("SSH_CONNECTION")
        passed = f"http://127.0.0.1:{app.server_address[1]}/"
        base = cli.links_show_base(p, passed, 7, "the name the base answered as")
        assert base == passed
        assert f"(http://127.0.0.1:{app.server_address[1]}/#task={task})" in cli._listen_line(p, m, base=base)
        rows = json.loads((p.state / "link-forwards.json").read_text())
        assert rows[-1]["chat"] == 7 and rows[-1]["base"] == passed
        assert rows[-1]["project"] == "the name the base answered as" and rows[-1]["ts"] > 0
    finally:
        _stop_web_apps(app, mine)


def test_the_notifier_s_alerts_use_the_viewer_s_checked_base(env, monkeypatch):
    import types
    from ttp import notifier, tunnel, weblink
    p = make(env)
    weblink._ANSWERED.clear()
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.post("out", f"alert about task #{task}", kind="alert", severity="high")
    app = _web_app(p)                                       # this project's web app
    other = _web_app(types.SimpleNamespace(name="other"))   # another project's app
    p.db.set_kv("web", {"port": other.server_address[1]})   # its own port serves another project

    try:
        monkeypatch.setattr(tunnel, "installed", lambda name: {"local": app.server_address[1]})
        rows = [r for r in notifier.alerts_since(p, 0, "high") if not r.get("cleared")]
        assert rows and f"http://127.0.0.1:{app.server_address[1]}/#task={task}" in rows[0]["text"], rows

        monkeypatch.setattr(tunnel, "installed", lambda name: None)
        weblink._ANSWERED.clear()
        rows = [r for r in notifier.alerts_since(p, 0, "high") if not r.get("cleared")]
        assert rows and "#task=" not in rows[0]["text"], rows
    finally:
        _stop_web_apps(app, other)


def test_the_web_app_renders_its_message_links_relative_to_itself(env):
    p = make(env)
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.set_kv("web", {"port": 8123})   # the host's own port: irrelevant to the page it serves
    p.db.post("out", f"is task #{task} on track?", kind="ask", severity="normal")
    from ttp import links, web
    cfg = links.config(p, p.db, base="")
    assert links.linkify(f"task #{task}", cfg) == f"[task #{task}](#task={task})"
    attention = web.state_payload(p, p.db)["attention"]
    assert attention and "](#task=" in attention[0]["text"] and "127.0.0.1" not in attention[0]["text"]




@pytest.mark.parametrize("rejections", [
    ["ttp: error: unrecognized arguments: --link-base http://127.0.0.1:18800/ --link-as demo"],
    ["ttp: error: unrecognized arguments: --link-base http://127.0.0.1:18800/",
     "ttp: error: unrecognized arguments: --link-as demo"],
    ["ttp: error: unrecognized arguments: --holder mac/a --link-base http://127.0.0.1:18800/ "
     "--link-as demo"],
], ids=["both link flags", "one flag at a time", "holder and both link flags"])
def test_an_older_project_host_still_gets_its_listener_s_messages(env, monkeypatch, capsys, rejections):
    """A laptop upgraded before its project host: the host's older parser rejects the newer
    optional flags, and the listener retries without the ones it names and relays its messages
    (it would otherwise exit on the parse error and show nothing). Without the link flags the
    host prints no URL at all, which is the safe fallback, never another project's page."""
    from ttp import cli, tunnel, weblink
    rejects, sent = [*rejections], []
    named = {f for f in ("--holder", "--link-base", "--link-as") if f in " ".join(rejections)}

    def ssh(entry, argv, capture=False):
        sent.append(list(argv))
        if rejects:
            return subprocess.CompletedProcess(argv, 2, "", rejects.pop(0))
        print("[#7 demo (reply, normal)] hi")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(cli, "_ssh", ssh)
    monkeypatch.setattr(cli, "_ssh_relay", lambda entry, argv, shown: ssh(entry, argv))
    monkeypatch.setattr(cli, "flush_outbox", lambda *a, **k: None)
    monkeypatch.setattr(tunnel, "installed", lambda name: {"local": 18800})
    monkeypatch.setattr(weblink, "checked_base", lambda base, name: base)   # the tunnel answers
    assert cli.forward_listen({"host": "far", "dir": "/x"},
                              ["listen", "demo", "--chat", "c1", "--holder", "mac/a"], "demo") == 0
    assert sent[0][-4:] == ["--link-base", "http://127.0.0.1:18800/", "--link-as", "demo"], sent[0]
    assert len(sent) == len(rejections) + 1, sent
    assert all(f not in sent[-1] for f in named), (named, sent[-1])
    assert capsys.readouterr().out == "[#7 demo (reply, normal)] hi\n", "the listener keeps relaying"


def test_a_forwarded_listener_picks_up_a_link_base_that_comes_up_after_it_started(env, monkeypatch):
    """The forwarder decides the link base again on each reconnect while it has none, so a kept
    tunnel or web app still coming up when the listener started still carries links once it
    answers, without the listener restarting."""
    from ttp import cli, weblink
    argvs, answers = [], iter([None, None, "http://127.0.0.1:18800/"])

    def forward(entry, argv, quiet=False, shown=None, failure=None):
        argvs.append(list(argv))
        return 255 if len(argvs) < 3 else 0     # two drops, then a clean end
    monkeypatch.setattr(cli, "forward", forward)
    monkeypatch.setattr(cli, "flush_outbox", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "LINK_BASE_RECHECK_S", 0.0)   # every reconnect re-decides at once
    monkeypatch.setattr(weblink, "tunnel_base", lambda name: next(answers))
    assert cli.forward_listen({"host": "far", "dir": "/x"}, ["listen", "demo", "--chat", "c1"],
                              "demo") == 0
    assert len(argvs) == 3 and "--link-base" not in argvs[0], argvs[0]
    assert argvs[2][-4:] == ["--link-base", "http://127.0.0.1:18800/", "--link-as", "demo"], argvs[2]


def test_the_forwarded_base_proof_row_records_once_per_change(env):
    """A listener that re-decides its link base records one proof row per change of it, not one
    per batch: the row is what proves a viewer's checked base reached this host."""
    from ttp import cli
    p = make(env)
    app = _web_app(p)
    try:
        passed = f"http://127.0.0.1:{app.server_address[1]}/"
        rows = lambda: json.loads((p.state / "link-forwards.json").read_text())
        assert cli.links_show_base(p, passed, 7, "demo") == passed
        assert rows() == [{"chat": 7, "base": passed, "project": "demo",
                            "ts": rows()[-1]["ts"]}] and len(rows()) == 1
        # the same base again: nothing new is recorded
        assert cli.links_show_base(p, passed, 7, "demo", noted=passed) == passed
        assert len(rows()) == 1
        # a changed base: one more row, naming it
        assert cli.links_show_base(p, "http://127.0.0.1:9/", 7, "demo", noted=passed)
        assert [r["base"] for r in rows()] == [passed, "http://127.0.0.1:9/"]
    finally:
        _stop_web_apps(app)
