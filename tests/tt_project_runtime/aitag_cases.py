# AI-authorship tag tests (aitag.py: the worker `gh` wrapper and the connector PreToolUse hook) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Add new AI-authorship tag tests here.
from __future__ import annotations


def test_ai_tag_reads_the_last_text_line_past_hidden_lines_and_markup():
    from ttp import aitag
    tag = aitag.DEFAULT
    assert aitag.ends_with_tag("Looks good.\n\n[authored by AI]\n", tag)
    assert aitag.ends_with_tag("Looks good. [Authored by AI]\n<!-- ttp -->\n", tag)
    assert aitag.ends_with_tag("<p>Hi</p><p>[authored by AI]</p></body></html>\n", tag)
    assert aitag.ends_with_tag("Fixed.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)", tag)
    assert aitag.ends_with_tag("Fixed.\n_Written by AI_", tag)
    for body in ("Looks good.", "[authored by AI]\nLooks good.", "Looks good.\n<!-- [authored by AI] -->",
                 "an AI accelerator note"):
        assert not aitag.ends_with_tag(body, tag), body
    assert aitag.ok("", tag) and aitag.ok("anything", "")   # nothing posted; the guard turned off
    assert aitag.ends_with_tag("x " * 50000 + "[authored by AI]", tag)   # a long body reads its end
    assert aitag.tag({}) == tag and aitag.tag({"delivery": {"ai_tag": " (bot) "}}) == "(bot)"
    assert aitag.tag({"delivery": {"ai_tag": ""}}) == ""
    assert aitag.problems(None) == [] and aitag.problems("(bot)") == []
    assert aitag.problems(3) and aitag.problems("a\nb")


def test_workers_gh_refuses_comments_reviews_and_issues_without_the_ai_tag(env, tmp_path):
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    tagged = "Done, see the run log.\n\n[authored by AI]\n<!-- ttp -->"
    for args in (("pr", "comment", "7", "-b", "Done"), ("pr", "comment", "7", "--body=Done"),
                 ("pr", "comment", "7", "-bDone"), ("issue", "comment", "7", "--body", "Done"),
                 ("issue", "create", "-t", "Bug", "-b", "Steps"), ("pr", "review", "7", "-c", "-b", "Nit"),
                 ("pr", "review", "7", "--approve", "--body", "LGTM")):
        rc, err, ran = gh(*args)
        assert rc == 1 and ran == [] and "AI-authorship tag '[authored by AI]' (delivery.ai_tag)" in err, (args, err)
    for args in (("pr", "comment", "7", "-b", tagged), ("issue", "create", "-t", "Bug", "-b", tagged),
                 ("pr", "review", "7", "--approve"), ("pr", "comment", "7", "--delete-last", "--yes")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran, (args, err)
    # A body gh would get some other way cannot be checked: refused with how to pass it.
    for args in (("pr", "comment", "7", "--editor"), ("issue", "create", "-t", "Bug", "--web")):
        rc, err, ran = gh(*args)
        assert rc == 1 and ran == [] and "--body-file" in err, (args, err)
    body = tmp_path / "body.md"
    body.write_text("Repro attached.\n")
    rc, err, ran = gh("issue", "comment", "7", "--body-file", str(body))
    assert rc == 1 and ran == [], err
    body.write_text("Repro attached.\n\n[authored by AI]\n")
    rc, err, ran = gh("issue", "comment", "7", "--body-file", str(body))
    assert rc == 0 and ran, err
    rc, err, ran = gh("issue", "comment", "7", "--body-file", str(tmp_path / "missing.md"))
    assert rc == 1 and "cannot read the --body-file" in err, err


def test_workers_gh_body_from_stdin_is_checked_and_reaches_gh_intact(env, tmp_path):
    p = make(env)
    fake = tmp_path / "stdinbin"
    fake.mkdir()
    (fake / "gh").write_text(f"#!{sys.executable}\nimport sys\n"
                             f"open({str(tmp_path / 'gh.stdin')!r}, 'wb').write(sys.stdin.buffer.read())\n")
    (fake / "gh").chmod(0o755)
    for path in ("-", "/dev/stdin"):
        r = _gh_main(p, tmp_path, ["pr", "comment", "7", "--body-file", path], b"plain\n", fake=fake)
        assert r.returncode == 1 and b"AI-authorship tag" in r.stderr, (path, r.stderr)
        data = b"\xff\xfe binary first\n[authored by AI]\n"
        r = _gh_main(p, tmp_path, ["pr", "comment", "7", "--body-file", path], data, fake=fake)
        assert r.returncode == 0 and (tmp_path / "gh.stdin").read_bytes() == data, (path, r.stderr)
    r = _gh_main(p, tmp_path, ["api", "repos/a/b/issues/7/comments", "--input", "/dev/stdin"],
                 b'{"body": "untagged"}', fake=fake)
    assert r.returncode == 1 and b"AI-authorship tag" in r.stderr, r.stderr


def test_workers_gh_leaves_pr_bodies_alone_and_a_custom_or_empty_tag_applies(env, tmp_path):
    p = make(env)
    from ttp import aitag
    cfg = p.config()
    # PR bodies stay as they are: a PR names the AI tool that made it, or says nothing at all.
    for rest in (["create", "--draft", "-t", "t", "-b", "Fix.\n\n🤖 Generated with Claude Code"],
                 ["create", "--draft", "-t", "t", "-b", "Fix."], ["edit", "7", "-b", "Fix."]):
        assert aitag.gh_problem("pr", rest, cfg) is None, rest
    assert aitag.gh_problem("issue", ["edit", "7", "-b", "Fix."], cfg) is None
    p.set_config("delivery.ai_tag", "(bot)")
    cfg = p.config()
    assert aitag.gh_problem("pr", ["comment", "7", "-b", "ok (bot)"], cfg) is None
    why = aitag.gh_problem("pr", ["comment", "7", "-b", "ok"], cfg)
    assert why and "'(bot)'" in why
    p.set_config("delivery.ai_tag", "")
    assert aitag.gh_problem("pr", ["comment", "7", "-b", "ok"], p.config()) is None


def test_workers_gh_api_posts_need_the_ai_tag_but_pr_and_issue_body_edits_do_not(env, tmp_path):
    from ttp import prguard
    p = make(env)
    cfg = p.config()

    def why(*args, stdin=None):
        return prguard.check(list(args), "/bin/false", stdin_text=stdin, cfg=cfg)
    refused = (("api", "repos/a/b/issues/7/comments", "-f", "body=hi"),
               ("api", "-X", "PATCH", "repos/a/b/issues/comments/9", "-f", "body=hi"),
               ("api", "repos/a/b/pulls/7/reviews", "-f", "body=hi", "-f", "event=COMMENT"),
               ("api", "repos/a/b/pulls/7/comments/3/replies", "-F", "body=@-"),
               ("api", "https://api.github.com/repos/a/b/issues", "-f", "title=t", "-f", "body=hi"),
               ("api", "repos/a/b/commits/abc/comments", "--input", "-"),
               ("api", "graphql", "-f", 'query=mutation{addComment(input:{subjectId:"I_1",body:"hi"}){clientMutationId}}'),
               ("api", "graphql", "-f", "query=mutation($i:AddCommentInput!){addComment(input:$i){clientMutationId}}",
                "-f", "i[subjectId]=I_1", "-f", "i[body]=hi"),
               # a comment beside a PR body edit is still checked
               ("api", "graphql", "-f", 'query=mutation{a:updatePullRequest(input:{pullRequestId:"P",body:"x"})'
                '{clientMutationId} b:addComment(input:{subjectId:"I_1",body:"hi"}){clientMutationId}}'))
    for args in refused:
        w = why(*args, stdin='{"body": "hi"}' if "-" in args else "hi" if "body=@-" in args else None)
        assert w and "AI-authorship tag" in w, (args, w)
    for args in (("api", "repos/a/b/issues/7/comments", "-f", "body=hi\n[authored by AI]"),
                 ("api", "repos/a/b/pulls/7/reviews", "-f", "event=APPROVE"),
                 ("api", "repos/a/b/issues/7/comments"),
                 ("api", "-X", "PATCH", "repos/a/b/issues/7", "-f", "body=edited"),
                 ("api", "-X", "PATCH", "repos/a/b/pulls/7", "-f", "body=edited"),
                 ("api", "graphql", "-f", 'query=mutation{updatePullRequest(input:{pullRequestId:"P",body:$b})'
                  '{clientMutationId}}'),
                 ("api", "graphql", "-f", "query=query{viewer{login}}")):
        assert why(*args) is None, args
    w = why("api", "repos/a/b/issues/7/comments", "--input", "-")
    assert w and "cannot read the --input from stdin to check it for the AI-authorship tag" in w


def _post_hook(monkeypatch, tmp_path, p, tool, tool_input):
    from ttp import hook
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": tool_input})
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason") if out else None


def test_connector_posts_without_the_ai_tag_are_refused_by_the_pre_tool_hook(env, tmp_path, monkeypatch):
    p = make(env)
    tag = "\n\n[authored by AI]"
    refused = (("mcp__claude_ai_Slack__slack_send_message", {"channel_id": "C1", "message": "Build is green"}),
               ("mcp__claude_ai_Slack__slack_send_message_draft", {"channel_id": "C1", "message": "hi"}),
               ("mcp__claude_ai_Atlassian__createJiraIssue", {"projectKey": "X", "summary": "s", "description": "d"}),
               ("mcp__claude_ai_Atlassian__addCommentToJiraIssue", {"issueIdOrKey": "X-1", "commentBody": "c"}),
               ("mcp__claude_ai_Atlassian__createConfluencePage", {"spaceId": "1", "title": "t",
                                                                   "body": "<p>Notes</p>"}),
               ("mcp__claude_ai_Gmail__create_draft", {"to": ["a@example.com"], "subject": "s",
                                                       "body": "Hello" + tag, "htmlBody": "<p>Hello</p>"}),
               ("mcp__mail__send_email", {"to": "a@example.com", "body": "Hello"}),
               ("mcp__jira__create_issue", {"fields": {"summary": "s", "description": {
                   "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "d"}]}]}}}))
    for tool, inp in refused:
        why = _post_hook(monkeypatch, tmp_path, p, tool, inp)
        assert why and "AI-authorship tag '[authored by AI]'" in why, (tool, why)
    passed = (("mcp__claude_ai_Slack__slack_send_message", {"channel_id": "C1", "message": "Build is green" + tag}),
              ("mcp__claude_ai_Atlassian__createJiraIssue", {"projectKey": "X", "summary": "s",
                                                             "description": "d" + tag, "contentFormat": "markdown"}),
              ("mcp__claude_ai_Gmail__create_draft", {"to": ["a@example.com"], "subject": "s", "body": "Hi" + tag,
                                                      "htmlBody": "<p>Hi</p><p>[authored by AI]</p>"}),
              ("mcp__jira__create_issue", {"fields": {"summary": "s", "description": {
                  "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "[authored by AI]"}]}]}}}),
              ("mcp__claude_ai_Slack__slack_read_channel", {"channel_id": "C1"}),
              ("mcp__claude_ai_Slack__slack_search_public", {"query": "send message"}),
              ("mcp__claude_ai_Atlassian__getJiraIssue", {"issueIdOrKey": "X-1"}),
              ("mcp__claude_ai_Atlassian__editJiraIssue", {"issueIdOrKey": "X-1", "fields": {"labels": ["a"]}}),
              ("mcp__claude_ai_Gmail__send_draft", {"draft_id": "d1"}),
              ("mcp__github__create_pull_request", {"title": "t", "body": "PR body"}),
              ("mcp__claude_ai_Slack__slack_add_reaction", {"name": "eyes"}))
    for tool, inp in passed:
        assert _post_hook(monkeypatch, tmp_path, p, tool, inp) is None, tool
    # A worker's other tools and a run outside a task are left alone.
    assert _post_hook(monkeypatch, tmp_path, p, "Read", {"file_path": "x"}) is None
    monkeypatch.delenv("TTP_RUN_DIR")
    from ttp import hook
    assert hook.pre_tool_use({"tool_name": refused[0][0], "tool_input": refused[0][1]})[0] is None
    # The tag is the project's setting; "" turns the guard off.
    p.set_config("delivery.ai_tag", "(bot)")
    why = _post_hook(monkeypatch, tmp_path, p, *refused[0])
    assert why and "'(bot)'" in why
    assert _post_hook(monkeypatch, tmp_path, p, refused[0][0], {"message": "hi (bot)"}) is None
    p.set_config("delivery.ai_tag", "")
    assert _post_hook(monkeypatch, tmp_path, p, *refused[0]) is None


def test_replies_and_notifies_to_the_user_go_out_untagged_while_posts_to_others_need_the_tag(env, tmp_path,
                                                                                           monkeypatch, capsys):
    """The tag is for posts others read; the user's own channels (a coordinator reply, a worker notify)
    carry it only if the text did, and the prompts say so in both coordinator modes."""
    from types import SimpleNamespace
    from ttp import cli, daemon
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    turn = SimpleNamespace(structured={"actions": [{"type": "reply", "text": "Tests are green.", "chat": "c1"}],
                                       "summary": ""}, error="", final_text="")
    d._finish_coordinator({"dir": "x"}, turn, "ok", {})
    assert p.db.one("SELECT text FROM messages WHERE kind='reply'")["text"] == "Tests are green."
    tid = p.db.add_task("probe", "spec", kind="work", tier="standard", origin="coordinator")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))
    cli.main(["notify", "--severity", "low", "Review is ready."])
    capsys.readouterr()
    assert daemon.relay_worker_notifies(p, p.db.task(tid), run_dir) == 1
    sent = [r["text"] for r in p.db.q("SELECT text FROM messages") if "Review is ready." in (r["text"] or "")]
    assert sent and not any("authored by AI" in t for t in sent), sent
    # The same words posted where others read them are refused until they end with the tag.
    from ttp import aitag
    assert aitag.gh_problem("pr", ["comment", "7", "-b", "Review is ready."], p.config())
    assert aitag.tool_problem("mcp__claude_ai_Slack__slack_send_message", {"message": "Review is ready."}, p.config())
    # Both coordinator modes carry the rule once: the session role is coordinator.md then
    # coordinator-session.md, so the rule lives only in coordinator.md.
    from ttp.prompts import coordinator_role, coordinator_session_role
    for name, text in (("stateless", coordinator_role(p)), ("session", coordinator_session_role(p))):
        assert text.count("AI-authorship tag") == 1, name
        at = text.index("AI-authorship tag")
        assert "replies" in text[at - 150:at + 200].lower() and "never" in text[at - 150:at + 200], name
    prompts = pathlib.Path(aitag.__file__).parents[2] / "template" / "prompts"
    worker = (prompts / "worker.md").read_text()
    assert "Posts others (not the user) read end with `[authored by AI]`" in worker
