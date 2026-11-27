# Area case file for tests/test_tt_project_runtime.py: a PR-branch delivery's whole size (prsize).
import http.server
import json
import threading


def _anon(env, monkeypatch, routes):
    """A local stand-in for GitHub's REST API keyed by the full request path (query included):
    `routes` maps the path a GET asks for to its JSON body; returns the paths it was asked for."""
    asked = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            status, body = routes.get(self.path, (404, {"message": "Not Found"}))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TTP_GH_ANON_API", f"http://127.0.0.1:{srv.server_address[1]}")
    import atexit
    atexit.register(srv.shutdown)
    return asked


def _anon_pr_routes(closed, head=None, additions=None, repo="acme/widgets"):
    """The stand-in's routes: the open PR headed by `head`, and closed PRs with `additions` details."""
    routes = {}
    if head:
        routes[f"/repos/{repo}/pulls?head={repo.split('/')[0]}:{head}&state=open&per_page=1"] = \
            (200, [{"number": 7, "state": "open", "title": "One big feature", "merged_at": None,
                    "html_url": f"https://github.com/{repo}/pull/7",
                    "head": {"ref": head, "sha": "f" * 40}}])
    routes[f"/repos/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc"] = \
        (200, [{"number": n, "merged_at": "2026-01-01T00:00:00Z"} for n in closed])
    for n, adds in (additions or {}).items():
        routes[f"/repos/{repo}/pulls/{n}"] = (200, {"additions": adds})
    return routes


def test_a_pr_branch_delivery_is_judged_on_the_prs_cumulative_diff_and_the_repos_norm(env, monkeypatch):
    """A review of a change that delivers onto an open PR's branch carries the PR's whole diffstat
    (split into code, tests, docs and generated files) and the repo's norm, whatever the size of the
    change's own commits; the ponytail lane's verdict is told to cover the PR as a whole."""
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    p.set_config("delivery.push_branch", "main")
    p.set_config("delivery.own_push", "on")
    base = _git_out(p.root, "rev-parse", "HEAD")
    _git_out(p.root, "update-ref", "refs/remotes/origin/HEAD", base)
    # The PR's branch: a big cumulative change an earlier task already put there; this task's own
    # change is three lines on top of it, in its own worktree.
    for name, lines in (("big.py", 5000), ("tests/test_big.py", 100), ("docs/big.md", 40),
                        ("vendor/gen.py", 200)):
        (p.root / name).parent.mkdir(parents=True, exist_ok=True)
        (p.root / name).write_text("".join(f"{i}\n" for i in range(lines)))
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the big PR work")
    _git_out(p.root, "update-ref", "refs/heads/feature/x", "HEAD")
    _anon(env, monkeypatch, _anon_pr_routes([11, 12, 13, 14], head="feature/x",
                                            additions={11: 120, 12: 240, 13: 700, 14: 900}))
    monkeypatch.setattr("ttp.ghroute.repo_of", lambda cwd: "acme/widgets")
    _, _, _, (rev,) = _finish_code(env, p, "tiny fix", {"tiny.py": 3}, labels=["pr_branch:feature/x"])
    spec = rev["spec"]
    assert "judge the whole PR's cumulative diff" in spec and "acme/widgets/pull/7" in spec, spec
    # The cumulative stat, not the task's own 3 lines: every category, added and removed.
    assert "Its cumulative diff since its merge base is +5343/-0 across 5 files" in spec, spec
    assert "code +5003/-0" in spec and "tests +100/-0" in spec and "generated +200/-0" in spec
    assert "median 470, p90 900 added lines over 4 recent merges" in spec
    assert "ponytail lane's verdict covers the PR as a whole" in spec


def test_a_task_that_opened_a_pr_is_judged_on_that_pr(env, monkeypatch):
    """A task whose hand-off records its own PR (`pr` in its result) gets the PR block on that PR
    even when it carries no pr_branch: the block reads the PR by its number and state."""
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    asked = _anon(env, monkeypatch, {
        "/repos/acme/widgets/pulls/7": (200, {"number": 7, "state": "open", "merged_at": None,
                                              "title": "One big feature",
                                              "html_url": "https://github.com/acme/widgets/pull/7",
                                              "head": {"ref": "b", "sha": "f" * 40}}),
        "/repos/acme/widgets/pulls?state=closed&per_page=100&sort=updated&direction=desc": (200, [
            {"number": 11, "merged_at": "2026-01-01T00:00:00Z"},
            {"number": 12, "merged_at": "2026-01-01T00:00:00Z"}]),
        "/repos/acme/widgets/pulls/11": (200, {"additions": 100}),
        "/repos/acme/widgets/pulls/12": (200, {"additions": 300})})
    _, _, _, (rev,) = _finish_code(env, p, "opened a PR", {"app.py": 5},
                                   result={"status": "done", "summary": "works",
                                           "pr": "https://github.com/acme/widgets/pull/7"})
    assert "judge the whole PR's cumulative diff" in rev["spec"] and "acme/widgets/pull/7" in rev["spec"]
    assert "median 200, p90 300 added lines over 2 recent merges" in rev["spec"]
    assert "/repos/acme/widgets/pulls/7" in asked


def test_a_push_branch_delivery_heads_no_open_pr_and_gets_no_pr_block(env, monkeypatch):
    """A change that lands on the push branch gets no PR block, even when the repo has open PRs:
    only a branch that heads an open PR is judged whole."""
    monkeypatch.delenv("TTP_TEST_PONYTAIL")
    p = make(env)
    _anon(env, monkeypatch, _anon_pr_routes([11], head="main"))
    monkeypatch.setattr("ttp.ghroute.repo_of", lambda cwd: "acme/widgets")
    _, _, _, (rev,) = _finish_code(env, p, "plain change", {"app.py": 30})
    assert "cumulative diff" not in rev["spec"] and "ponytail lane's verdict covers the PR" \
           not in rev["spec"]


def test_the_repo_norm_reads_merged_pr_details_and_is_cached(env, monkeypatch):
    """The norm takes the repo's recently merged PRs' added lines (median and p90) and is cached per
    repo for a day: a second read asks nothing. Too few readable PRs is no norm; a route that cannot
    answer leaves none."""
    from ttp import prsize
    asked = _anon(env, monkeypatch, _anon_pr_routes([11, 12, 13], additions={11: 100, 12: 200},
                                                    repo="acme/norm-one"))
    assert prsize.repo_norm("acme/norm-one") == {"median": 150, "p90": 200, "n": 2}
    # the list, each merged PR's detail (a 404 detail is skipped), and none of it asked twice
    assert asked == ["/repos/acme/norm-one/pulls?state=closed&per_page=100&sort=updated&direction=desc",
                     "/repos/acme/norm-one/pulls/11", "/repos/acme/norm-one/pulls/12",
                     "/repos/acme/norm-one/pulls/13"], asked
    n = len(asked)
    assert prsize.repo_norm("acme/norm-one") == {"median": 150, "p90": 200, "n": 2} and len(asked) == n
    # the read stops at NORM_PRS merged PRs
    monkeypatch.setattr(prsize, "NORM_PRS", 2)
    _anon(env, monkeypatch, _anon_pr_routes([11, 12, 13], additions={11: 100, 12: 200, 13: 300},
                                            repo="acme/norm-two"))
    assert prsize.repo_norm("acme/norm-two") == {"median": 150, "p90": 200, "n": 2}
    # the route cannot answer: no norm
    monkeypatch.setenv("TTP_GH_ANON_API", "http://127.0.0.1:9")
    assert prsize.repo_norm("acme/norm-three") is None


def test_the_stat_splits_the_prs_cumulative_diff_into_code_tests_docs_and_generated(env):
    """stat() measures a branch's whole diff against its merge base with its base branch, split into
    the four buckets; a branch it cannot measure is None."""
    from ttp import prsize
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD")
    for name in ("app.py", "tests/test_app.py", "docs/guide.md", "vendor/lib.py", "package-lock.json"):
        (p.root / name).parent.mkdir(parents=True, exist_ok=True)
        (p.root / name).write_text("".join(f"{i}\n" for i in range(10)))
    (p.root / "logo.png").write_bytes(bytes(range(64)))
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big")
    _git_out(p.root, "update-ref", "refs/remotes/origin/HEAD", base)
    st = prsize.cumulative_stat(p, "HEAD")
    assert st["adds"] == 50 and st["files"] == 6, st
    assert st["tests"] == [10, 0, 1] and st["docs"] == [10, 0, 1]
    assert st["generated"] == [20, 0, 2] and st["code"] == [10, 0, 2], st   # the binary counts as a code file
    assert st["base"] == "origin/HEAD"
    assert prsize.cumulative_stat(p, "no-such-branch") is None
