# PR body check tests (prbody.py, through prguard's worker `gh`) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file, and pytest collects these tests under its name. Add new PR body check tests here.
from __future__ import annotations

PR_BODY_CHECK = '''#!{python}
import os, sys
body = sys.stdin.read()
with open(os.environ["TTP_PR_BODY_FILE"]) as f:
    assert f.read() == body
with open(os.environ["PR_CHECK_LOG"], "a") as f:
    f.write(os.environ["TTP_PR_ACTION"] + " " + os.environ["TTP_PR_REPO"] + " " + body.splitlines()[0] + "\\n"
            if body else os.environ["TTP_PR_ACTION"] + " (empty)\\n")
if not body.startswith("Type: "):
    print("the first line must name the change's type, like 'Type: fix'")
    sys.exit(3)
'''


def _pr_body_project(env, tmp_path, setting=None):
    """A project whose PR body check (if `setting` is given) is the PR_BODY_CHECK script, its gh runner
    and the log of the bodies the check saw."""
    p = make(env)
    script = tmp_path / "check_pr_body"
    script.write_text(PR_BODY_CHECK.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "pr-check.log"
    log.write_text("")
    os.environ["PR_CHECK_LOG"] = str(log)   # the gh runner passes os.environ on (monkeypatched by env)
    if setting is not None:
        p.set_config("delivery.pr_body_check", setting(str(script)) if callable(setting) else setting)

    def seen():
        out = log.read_text().splitlines()
        log.write_text("")
        return out
    return p, _gh_runner(p, tmp_path), seen


def test_pr_body_check_unset_leaves_create_and_edit_unchanged(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path)
    for args in (("pr", "create", "--draft", "-t", "t", "--fill"), ("pr", "create", "-d", "-t", "t", "-b", "no type"),
                 ("pr", "edit", "7", "--body", "no type"), ("pr", "edit", "7", "-t", "new title")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], err
    assert seen() == []
    from ttp import prbody
    assert prbody.problem("create", ["-d", "--fill"], {}) is None and prbody.parse("") is None


def test_pr_body_check_lets_a_passing_body_through_and_refuses_a_failing_one(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    body = tmp_path / "body.md"
    body.write_text("Type: fix\n\nWhat changed.\n")
    rc, err, ran = gh("pr", "create", "--draft", "-R", "acme/widgets", "-t", "t", "--body-file", str(body))
    assert rc == 0 and len(ran) == 1, err
    assert seen() == ["create acme/widgets Type: fix"]
    rc, err, ran = gh("pr", "create", "--draft", "-R", "acme/widgets", "-t", "t", "-b", "What changed.")
    assert rc == 1 and ran == []
    assert "delivery.pr_body_check, exit 3" in err and "must name the change's type, like 'Type: fix'" in err
    assert seen() == ["create acme/widgets What changed."]
    # Every spelling of the body is read, and an option's value is never taken for one.
    for args in (["-d", "--body=Type: x"], ["-d", "-bType: x"], ["-d", "-t", "-b", "--body", "Type: x"],
                 ["-d", "-F", str(body)], ["-d", f"--body-file={body}"]):
        rc, err, ran = gh("pr", "create", "-R", "acme/widgets", *args)
        assert rc == 0 and ran, (args, err)
        assert seen()[0].startswith("create acme/widgets Type: "), args
    from ttp import prbody
    assert prbody._scan(["-dbx", "-fwF", "f", "-tb", "-R", "a/b"], prbody.CREATE_VALUES) == \
        ({"-b": "x", "-F": "f", "-t": "b", "-R": "a/b"}, [])


def test_pr_body_check_covers_edits_that_set_the_body_only(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    rc, err, ran = gh("pr", "edit", "https://github.com/acme/widgets/pull/7", "--body", "no type")
    assert rc == 1 and ran == [] and "Type: fix" in err
    assert seen() == ["edit acme/widgets no type"]
    rc, err, ran = gh("pr", "edit", "7", "-R", "acme/widgets", "-bType: docs")
    assert rc == 0 and ran == ["pr edit 7 -R acme/widgets -bType: docs"], err
    assert seen() == ["edit acme/widgets Type: docs"]
    for args in (("pr", "edit", "7", "-t", "new title"), ("pr", "edit", "7", "-t", "-b"),
                 ("pr", "edit", "7", "--add-label", "x", "--title=--body")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], err
    assert seen() == []
    # A body read from stdin is checked, then handed on to gh.
    from ttp import prguard
    cfg = p.config()
    assert "exit 3" in prguard.check(["pr", "edit", "7", "-F", "-"], "/bin/false", stdin_text="no type", cfg=cfg)
    assert prguard.check(["pr", "edit", "7", "-F", "-"], "/bin/false", stdin_text="Type: fix\n", cfg=cfg) is None
    assert "cannot read the PR body from stdin" in prguard.check(["pr", "edit", "7", "-F", "-"], "/bin/false", cfg=cfg)
    assert seen() == ["edit  no type", "edit  Type: fix"]   # no -R: the repo is not looked up without globs
    fake = tmp_path / "realbin" / "gh"
    r = subprocess.run([sys.executable, "-c", "import sys; from ttp import prguard; "
                        "sys.exit(prguard.main(['gh', 'pr', 'edit', '7', '-R', 'acme/widgets', '-F', '-'], sys.argv[1]))",
                        str(p.harness / "bin")], input="Type: fix\nbody\n", capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{p.harness / 'bin'}:{fake.parent}:{os.environ['PATH']}",
                            "TTP_PROJECT": str(p.base), "FAKE_GH_LOG": str(tmp_path / "gh.log")},
                       cwd=str(pathlib.Path(prguard.__file__).parents[1]))
    assert r.returncode == 0, r.stderr
    assert seen() == ["edit acme/widgets Type: fix"]


def test_pr_body_check_skips_repos_its_globs_leave_out(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: {"run": s, "repos": ["acme/*"],
                                                             "skip_repos": ["acme/scratch-*"]})
    for repo in ("acme/scratch-notes", "ACME/Scratch-Notes", "https://github.com/acme/scratch-x", "other/widgets"):
        rc, err, ran = gh("pr", "create", "--draft", "-R", repo, "-t", "t", "-b", "no type")
        assert rc == 0 and ran, (repo, err)
    rc, err, ran = gh("pr", "edit", "https://github.com/acme/scratch-x/pull/3", "-b", "no type")
    assert rc == 0 and ran, err
    assert seen() == []
    rc, err, ran = gh("pr", "create", "--draft", "-R", "github.com/acme/widgets", "-t", "t", "-b", "no type")
    assert rc == 1 and ran == [] and seen() == ["create acme/widgets no type"]
    from ttp import prbody
    conf = prbody.parse({"run": "x", "skip_repos": ["acme/scratch-*"]})
    assert prbody.applies(conf, "") and not prbody.applies(conf, "acme/scratch-1")   # an unknown repo is checked


def test_pr_body_check_refuses_an_unreadable_body_and_a_check_that_cannot_run(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    for args in (("--fill",), ("-df",), ("--editor",), ("-T", "tmpl.md"), ()):
        rc, err, ran = gh("pr", "create", "--draft", "-t", "t", *args)
        assert rc == 1 and ran == [] and "--body-file" in err and "delivery.pr_body_check" in err, (args, err)
    rc, err, ran = gh("pr", "create", "--draft", "-t", "t", "-F", str(tmp_path / "missing.md"))
    assert rc == 1 and ran == [] and "cannot read the --body-file" in err
    rc, err, ran = gh("pr", "create", "--draft", "-t", "t", "-b", "Type: x", "-F", str(tmp_path / "missing.md"))
    assert rc == 1 and ran == [] and "once" in err
    assert seen() == []
    p.set_config("delivery.pr_body_check", str(tmp_path / "no-such-check"))
    rc, err, ran = gh("pr", "create", "--draft", "-t", "t", "-b", "Type: x")
    assert rc == 1 and ran == [] and "delivery.pr_body_check) could not run (exit 127)" in err
    p.set_config("delivery.pr_body_check", {"run": "sleep 30", "timeout_s": 0.5})
    rc, err, ran = gh("pr", "edit", "7", "-b", "Type: x")
    assert rc == 1 and ran == [] and "could not run (timed out after 0.5 s)" in err
    p.set_config("delivery.pr_body_check", {"run": "true", "repos": "acme/*"})
    rc, err, ran = gh("pr", "create", "--draft", "-t", "t", "-b", "Type: x")
    assert rc == 1 and ran == [] and "delivery.pr_body_check is malformed" in err
    from ttp.project import config_problems
    assert any(x.startswith("delivery.pr_body_check: ") for x in config_problems({"delivery": {"pr_body_check": 5}}))
    assert not [x for x in config_problems({"delivery": {"pr_body_check": {"run": "true", "skip_repos": ["a/*"]}}})
                if "pr_body_check" in x]


def test_pr_body_check_covers_rest_api_writes_of_a_pr_body(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    good = tmp_path / "good.md"
    good.write_text("Type: fix\n\nWhat changed.\n")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"title": "t", "body": "no type"}))
    for args in (("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-f", "body=no type"),
                 ("api", "--method=patch", "/repos/acme/widgets/pulls/7", "--raw-field", "body=no type"),
                 ("api", "repos/acme/widgets/pulls/7", "-F", "body=no type"),   # POST implied by a field
                 ("api", "-X", "PATCH", "https://api.github.com/repos/acme/widgets/pulls/7", "--input", str(bad)),
                 ("api", "repos/acme/widgets/pulls", "-F", "draft=true", "-f", "head=x", "-f", "body=no type")):
        rc, err, ran = gh(*args)
        assert rc == 1 and ran == [] and "delivery.pr_body_check, exit 3" in err, (args, err)
        assert seen() == [("create" if args[-6:-5] == ("-F",) else "edit") + " acme/widgets no type"], args
    for args in (("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-F", f"body=@{good}"),
                 ("api", "repos/acme/widgets/pulls", "-F", "draft=true", "-f", "body=Type: docs")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)
        assert seen()[0].endswith("acme/widgets Type: " + ("fix" if "@" in args[-1] else "docs"))
    # Reads, writes without a body and other endpoints are not checked.
    for args in (("api", "repos/acme/widgets/pulls/7"), ("api", "-X", "GET", "repos/acme/widgets/pulls", "-f", "body=x"),
                 ("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-f", "title=t"),
                 ("api", "repos/acme/widgets/issues/7/comments", "-f", "body=no type")):
        rc, err, ran = gh(*args)
        assert rc == 0 and ran, (args, err)
    assert seen() == []


def test_pr_body_check_covers_graphql_pr_mutations(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    upd = 'mutation{updatePullRequest(input:{pullRequestId:"PR_kwDOabc123",%s}){pullRequest{url body}}}'
    for body in ('body:"no type\\nmore"', 'body: """\n    no type\n    """'):
        rc, err, ran = gh("api", "graphql", "-f", "query=" + upd % body)
        assert rc == 1 and ran == [] and "delivery.pr_body_check, exit 3" in err, (body, err)
        assert seen() == ["edit  no type"]
    rc, err, ran = gh("api", "graphql", "-f", "query=" + upd % 'body:"Type: fix\\u0021"')
    assert rc == 0 and ran and seen() == ["edit  Type: fix!"], err
    good = tmp_path / "good.md"
    good.write_text("Type: feat\n")
    create = ('mutation($b:String!){createPullRequest(input:{repositoryId:"R_1",baseRefName:"main",headRefName:"x",'
              'title:"t",draft:true,body:$b}){pullRequest{url}}}')
    rc, err, ran = gh("api", "graphql", "-f", "query=" + create, "-F", f"b=@{good}")
    assert rc == 0 and ran and seen() == ["create  Type: feat"], err
    rc, err, ran = gh("api", "graphql", "-f", "query=" + create, "-f", "b=no type")
    assert rc == 1 and ran == [] and seen() == ["create  no type"], err
    whole = "mutation($input:UpdatePullRequestInput!){updatePullRequest(input:$input){pullRequest{url}}}"
    rc, err, ran = gh("api", "graphql", "-f", "query=" + whole, "-f", "input[pullRequestId]=PR_kwDOabc123",
                      "-f", "input[body]=no type")
    assert rc == 1 and ran == [] and seen() == ["edit  no type"], err
    inp = tmp_path / "q.json"
    inp.write_text(json.dumps({"query": whole, "variables": {"input": {"pullRequestId": "PR_x", "body": "no type"}}}))
    rc, err, ran = gh("api", "graphql", "--input", str(inp))
    assert rc == 1 and ran == [] and seen() == ["edit  no type"], err
    # A mutation that leaves the body alone, and other GraphQL calls, are not checked.
    for q in (upd % 'title:"t"', "query{viewer{login}}", 'mutation{addComment(input:{subjectId:"I_1",body:"x"}){clientMutationId}}'):
        rc, err, ran = gh("api", "graphql", "-f", "query=" + q)
        assert rc == 0 and ran, (q, err)
    assert seen() == []


def test_pr_body_check_refuses_a_gh_api_pr_body_it_cannot_read(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: s)
    from ttp import prguard
    cfg = p.config()
    rest = ["api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "--input", "-"]
    why = prguard.check(rest, "/bin/false", cfg=cfg)
    assert why and "cannot read the --input from stdin for delivery.pr_body_check" in why
    assert "exit 3" in prguard.check(rest, "/bin/false", stdin_text='{"body": "no type"}', cfg=cfg)
    assert prguard.check(rest, "/bin/false", stdin_text='{"body": "Type: x"}', cfg=cfg) is None
    assert seen() == ["edit acme/widgets no type", "edit acme/widgets Type: x"]
    assert "cannot read the field file from stdin for delivery.pr_body_check" in prguard.check(
        ["api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-F", "body=@-"], "/bin/false", cfg=cfg)
    notjson = tmp_path / "body.txt"
    notjson.write_text("Type: x, not JSON")
    rc, err, ran = gh("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "--input", str(notjson))
    assert rc == 1 and ran == [] and "as JSON for delivery.pr_body_check" in err, err
    rc, err, ran = gh("api", "graphql", "-f",
                      'query=mutation{updatePullRequest(input:{pullRequestId:"PR_kwDOabc123",body:$b}){clientMutationId}}')
    assert rc == 1 and ran == [] and "the GraphQL variable $b for delivery.pr_body_check" in err, err
    rc, err, ran = gh("api", "graphql", "-f",
                      "query=mutation($i:UpdatePullRequestInput!){updatePullRequest(input:$i){clientMutationId}}")
    assert rc == 1 and ran == [] and "the GraphQL variable $i for delivery.pr_body_check" in err, err
    assert seen() == []


def test_pr_body_check_globs_apply_to_rest_api_and_check_graphql(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path, lambda s: {"run": s, "repos": ["acme/*"],
                                                             "skip_repos": ["acme/scratch-*"]})
    for repo in ("acme/scratch-notes", "other/widgets"):
        rc, err, ran = gh("api", "-X", "PATCH", f"repos/{repo}/pulls/3", "-f", "body=no type")
        assert rc == 0 and ran, (repo, err)
    assert seen() == []
    rc, err, ran = gh("api", "-X", "PATCH", "repos/ACME/Widgets/pulls/3", "-f", "body=no type")
    assert rc == 1 and ran == [] and seen() == ["edit acme/widgets no type"], err
    # A GraphQL call names its repo by node id: the repo is not told, so the check runs.
    rc, err, ran = gh("api", "graphql", "-f", 'query=mutation{createPullRequest(input:{repositoryId:"R_kgDOscratch",'
                      'baseRefName:"main",headRefName:"x",title:"t",draft:true,body:"no type"}){pullRequest{url}}}')
    assert rc == 1 and ran == [] and seen() == ["create  no type"], err
    from ttp import prguard
    for current, refused in (("acme/scratch-x", False), ("acme/widgets", True)):   # {owner}/{repo}: the current repo
        monkeypatch.setenv("GH_REPO", current)
        why = prguard.check(["api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/3", "-f", "body=no type"], "/bin/false",
                            cfg=p.config())
        assert bool(why) == refused, (current, why)
    assert seen() == ["edit acme/widgets no type"]


def test_pr_body_check_unset_leaves_gh_api_calls_unchanged(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_CHECK_LOG", "")
    p, gh, seen = _pr_body_project(env, tmp_path)
    from ttp import prbody, prguard

    def no_read(*a, **k):
        raise AssertionError("a body was read while the check is unset")
    monkeypatch.setattr(prbody, "_read", no_read)
    calls = [["api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-f", "body=no type"],
             ["api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "--input", "-"],
             ["api", "repos/acme/widgets/pulls", "-F", "draft=true", "-F", "body=@-"],
             ["api", "graphql", "-f", 'query=mutation{updatePullRequest(input:{pullRequestId:"PR_kwDOabc123",'
                                      'body:$missing}){clientMutationId}}'],
             ["api", "graphql", "-f", "query=mutation($i:UpdatePullRequestInput!){updatePullRequest(input:$i){x}}"]]
    for args in calls:
        assert prguard.check(args, "/bin/false", cfg=p.config()) is None, args
        assert prbody.api_problem(*prguard._api_target(args[1:]), args[1:], {}) is None
    # The guard's own refusals stay as they were.
    assert "drafts only" in prguard.check(["api", "repos/acme/widgets/pulls", "-f", "body=x"], "/bin/false", cfg={})
    rc, err, ran = gh(*calls[0])
    assert rc == 0 and ran == [" ".join(calls[0])], err
    assert seen() == []
