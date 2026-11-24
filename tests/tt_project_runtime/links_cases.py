# Outgoing-message link tests (links.py, the outgoing boundaries and the check command) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file. Add new link tests here.
from __future__ import annotations

CFG = {"jira_base": "https://example.atlassian.net", "jira_keys": ["EX"],
       "repos": {"smi": "example-org/example-smi"}, "single_repo": "",
       "file_url": lambda p: f"http://127.0.0.1:9/#token=ab&file={p}" if p.endswith(".json") else None,
       "is_file": lambda p: p.endswith(".json")}


def test_linkify_turns_each_reference_shape_into_a_link():
    from ttp import links
    out = links.linkify("EX-7 fixed, see smi#12 and example-org/example-repo#3, "
                        "state/runs/9/result.json and the code span `EX-7` stays", CFG)
    assert "[EX-7](https://example.atlassian.net/browse/EX-7)" in out
    assert "[smi#12](https://github.com/example-org/example-smi/issues/12)" in out
    assert "[example-org/example-repo#3](https://github.com/example-org/example-repo/issues/3)" in out
    assert "[state/runs/9/result.json](http://127.0.0.1:9/#token=ab&file=state/runs/9/result.json)" in out
    assert "`EX-7`" in out                 # a code span is never linkified


def test_linkify_links_pr_words_only_with_one_repo_and_never_a_bare_number():
    from ttp import links
    assert "](https://github.com/example-org/one/issues/9)" in \
        links.linkify("PR #9", {"single_repo": "example-org/one"})
    assert links.linkify("PR #9", {"single_repo": ""}) == "PR #9"          # no single repo: stays
    assert links.linkify("see #1234", CFG) == "see #1234"                 # a bare '#N' is a task
    assert links.linkify("plain text with no reference", CFG) == "plain text with no reference"


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
    out = " ".join(links.problems(text, none, task_ids=set()))
    for what in ("MINFRA-2596", "#288", "#64844", "result.json"):
        assert what in out, out
    full = {"jira_base": "https://example.atlassian.net", "jira_keys": ["MINFRA"],
            "repos": {}, "single_repo": "example-org/example-smi",
            "file_url": lambda p: "http://127.0.0.1:9/#token=ab&file=" + p, "is_file": lambda p: True}
    assert links.problems(text, full, task_ids={288, 64844}) == []


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
