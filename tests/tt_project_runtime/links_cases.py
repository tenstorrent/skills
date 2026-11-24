# Outgoing-message link tests (links.py, the outgoing boundaries and the check command) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new link tests here.
from __future__ import annotations

CFG = {"jira_base": "https://example.atlassian.net", "jira_keys": ["EX"],
       "repos": {"smi": "example-org/example-smi"}, "single_repo": "",
       "file_url": lambda p: f"http://127.0.0.1:9/#token=ab&file={p}" if p.endswith(".json") else None,
       "is_file": lambda p: p.endswith(".json"),
       "ref_url": lambda n, kind: f"http://127.0.0.1:9/#token=ab&{kind or 'task'}={n}" if n == 5 else None}


def test_linkify_turns_each_reference_shape_into_a_link():
    from ttp import links
    out = links.linkify("EX-7 fixed, see smi#12 and example-org/example-repo#3, "
                        "state/runs/9/result.json and the code span `EX-7` stays", CFG)
    assert "[EX-7](https://example.atlassian.net/browse/EX-7)" in out
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
    for text, kind in (("task #5", "task"), ("run #5", "run"), ("review #5", "task"), ("msg #5", "msg"),
                       ("ask #5", "msg"), ("run 5", "run")):
        out = links.linkify(text, CFG)
        assert f"&{kind}=5" in out and f"[{text}]" in out, (text, out)
    # one that resolves to nothing is labelled, never left bare and never given a guessed repo
    assert links.linkify("see #1234", CFG) == "see #1234 (unresolved reference)"
    assert links.linkify("task #4", CFG) == "task #4"      # kinded and unresolved: its kind labels it
    assert links.linkify("plain text with no reference", CFG) == "plain text with no reference"
    # no resolver: the caller promised nothing about ids, so nothing changes
    assert links.linkify("see #5", {k: v for k, v in CFG.items() if k != "ref_url"}) == "see #5"
    # a second pass adds nothing
    once = links.linkify("task #5 needs a look", CFG)
    assert links.linkify(once, CFG) == once


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
    full = {"jira_base": "https://example.atlassian.net", "jira_keys": ["MINFRA"],
            "repos": {}, "single_repo": "example-org/example-smi",
            "file_url": lambda p: "http://127.0.0.1:9/#token=ab&file=" + p, "is_file": lambda p: True,
            "ref_url": lambda n, kind: "x" if n in (288, 64844) else None}
    assert links.problems(text, full) == []


def test_readable_keeps_the_file_view_inside_the_project_tree(tmp_path):
    from ttp import links
    (tmp_path / "state/runs/1").mkdir(parents=True)
    (tmp_path / "state/runs/1/result.json").write_text("{}")
    (tmp_path / "web.token").write_text("x")
    (tmp_path / "note.txt").write_text("in")

    class P:
        root = str(tmp_path)
    assert links.readable(P, "state/runs/1/result.json") == (tmp_path / "state/runs/1/result.json").resolve()
    assert links.readable(P, str(tmp_path / "state/runs/1/result.json")) is not None
    assert links.readable(P, "/etc/passwd") is None                       # outside the roots
    assert links.readable(P, "../outside.txt") is None
    assert links.readable(P, "web.token") is None                        # a secret stays unreadable
    assert links.readable(P, "note.txt") == (tmp_path / "note.txt").resolve()


def test_the_listener_line_and_status_ask_linkify_outgoing_text(env):
    p = make(env)
    p.set_config("links.jira_base", "https://example.atlassian.net")
    p.set_config("links.jira_keys", ["EX"])
    p.set_config("links.repos", {"smi": "example-org/example-smi"})
    from ttp import cli, links
    m = {"id": 1, "kind": "reply", "severity": "normal", "chat": None, "text": "EX-7 done, see smi#1"}
    assert "(https://example.atlassian.net/browse/EX-7)" in cli._listen_line(p, m)
    assert "(https://github.com/example-org/example-smi/issues/1)" in cli._listen_line(p, m)
    assert links.for_user(p, "PR #1", links.TEXT) == "PR #1"   # no single repo: nothing to link
    p.db.post("out", "ask about EX-7", kind="ask", severity="normal")
    lines = cli.status_text(p).splitlines()
    assert any("needs you" in l and "(https://example.atlassian.net/browse/EX-7)" in l for l in lines), lines


def test_the_web_app_linkifies_its_messages_and_asks(env):
    p = make(env)
    p.set_config("links.jira_base", "https://example.atlassian.net")
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
        rows = [r for r in get("/api/messages") if "EX-8" in r["text"]]
        assert rows and "](https://example.atlassian.net/browse/EX-8)" in rows[0]["text"], rows
        assert any("](https://example.atlassian.net/browse/EX-9)" in a["text"]
                   for a in web.state_payload(p, p.db)["attention"])
        # a file link opens exactly what /api/file serves, and nothing outside the project tree
        (p.root / "state/runs/1").mkdir(parents=True, exist_ok=True)
        (p.root / "state/runs/1/result.json").write_text('{"status": "done"}')
        web.token(p)
        row = get(f"/api/file?path={urllib.parse.quote(str(p.root / 'state/runs/1/result.json'))}")
        assert row["text"] == '{"status": "done"}'
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/file?path=/etc/passwd")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=10)
    finally:
        srv.shutdown()


def test_ids_link_to_their_web_app_page_and_unresolved_ones_get_labelled(env):
    p = make(env)
    p.db.set_kv("web", {"port": 8123})    # a web app link exists: id references can resolve
    task = p.db.x("INSERT INTO tasks(created,title,spec) VALUES(?,?,?)", (0, "t", ""))
    p.db.x("INSERT INTO runs(id,task,role) VALUES(?,?,?)", (777, task, "worker"))
    msg = p.db.post("out", "task %d is running" % task, kind="reply", severity="normal")
    from ttp import links
    out = links.for_user(p, f"#{task} and run 777 and msg #{msg} and #4 and review #{task}")
    assert f"#{task}" in out and "](" in out and f"&task={task}" in out, out  # a task id resolves
    assert "run 777" in out and "&run=777" in out, out                   # a run id
    assert f"msg #{msg}" in out and f"&msg={msg}" in out, out            # a message id
    assert f"review #{task}" in out and f"&task={task}" in out, out      # a review names its task
    assert "#4 (unresolved reference)" in out, out                       # an id that matches nothing
    assert "(unresolved" not in links.for_user(p, f"#{task}")          # resolution is not labelling
    p.set_config("links.web_base", "https://example.org/app")
    assert "https://example.org/app/#token=" in links.for_user(p, f"#{task}")
    assert links.problems("#4 needs a look", links.config(p)) and links.problems(f"#{task} done", links.config(p)) == []


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
