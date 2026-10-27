# Worker gh guard tests (prguard.py: drafts, ready and reviewers over gh api) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file, and pytest collects these tests under its name. Add new gh guard route tests here.
from __future__ import annotations

_READY = 'query=mutation{markPullRequestReadyForReview(input:{pullRequestId:"PR_kwDOabc123"}){clientMutationId}}'
_CREATE = 'query=mutation{createPullRequest(input:{repositoryId:"R_1",title:"t"%s}){clientMutationId}}'
_REVIEW = 'query=mutation{requestReviews(input:{pullRequestId:"PR_kwDOabc123",userIds:["U_1"]}){clientMutationId}}'


def _approve_7(p):
    from ttp import prguard
    p.db.set_kv(prguard.APPROVALS_KEY, {"acme/widgets#7": {"answer": 1, "head": HEAD, "spent": None}})
    _heads_seen(p, "acme/widgets#7")


_GHE_GRAPHQL = ("https://ghe.example.com/api/graphql", "https://GHE.example.com/API/GraphQL/")


def test_workers_gh_marks_ready_over_every_api_route_only_with_the_users_approval(env, tmp_path, monkeypatch):
    """Each gh api spelling that takes a PR out of draft gets gh pr ready's decision: refused without the
    user's recorded approval, let through once with it."""
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    routes = [("api", "graphql", "-f", _READY), ("api", "/graphql", "-f", _READY), ("api", "graphql/", "-f", _READY),
              ("api", "GraphQL", "-f", _READY), ("api", "https://api.github.com/graphql", "-f", _READY),
              ("api", "-X", "PATCH", "repos/acme/widgets/pulls/7", "-F", "draft=false"),
              ("api", "-X", "PATCH", "repositories/123/pulls/7", "-F", "draft=false"),
              ("api", "-X", "PATCH", "/repositories/123/pulls/7/", "-F", "draft=false")]
    for args in routes + [("pr", "ready", "7")]:
        rc, err, ran = gh(*args)
        assert rc == 1 and "without the user's recorded approval" in err and ran == [], (args, err)
    for args in routes + [("pr", "ready", "7")]:
        _approve_7(p)
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)
        rc, err, ran = gh(*args)   # the approval is spent
        assert rc == 1 and ran == [], args
    # Back to draft needs no approval, like gh pr ready --undo.
    rc, err, ran = gh("api", "/graphql", "-f",
                      'query=mutation{convertPullRequestToDraft(input:{pullRequestId:"PR_kwDOabc123"}){clientMutationId}}')
    assert rc == 0 and ran, err
    # GitHub Enterprise: an absolute URL (gh sends it as-is; GraphQL is at /api/graphql) is the same
    # route, and its PR is looked up on that host.
    monkeypatch.setenv("FAKE_GH_PR_HOST", "ghe.example.com")
    gh = _gh_runner(p, tmp_path)
    ghe = [("api", ep, "-f", _READY) for ep in _GHE_GRAPHQL]
    ghe += [("api", "-X", "PATCH", "https://ghe.example.com/api/v3/repos/acme/widgets/pulls/7", "-F", "draft=false")]
    for args in ghe:
        rc, err, ran = gh(*args)
        assert rc == 1 and "without the user's recorded approval" in err and ran == [], (args, err)
        _approve_7(p)
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)


def test_workers_gh_opens_drafts_only_over_every_api_route(env, tmp_path):
    """Like gh pr create without --draft, a create without the draft flag is refused on every route,
    approval or not; with it, it runs."""
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    _approve_7(p)
    for ep in ("graphql", "/graphql", "graphql/", "GRAPHQL", *_GHE_GRAPHQL):
        rc, err, ran = gh("api", ep, "-f", _CREATE % "")
        assert rc == 1 and "drafts only" in err and ran == [], (ep, err)
        rc, err, ran = gh("api", ep, "-f", _CREATE % ",draft:true")
        assert rc == 0 and ran, (ep, err)
    for ep in ("repos/acme/widgets/pulls", "repositories/123/pulls", "/repositories/123/pulls/",
               "https://ghe.example.com/api/v3/repos/acme/widgets/pulls"):
        rc, err, ran = gh("api", ep, "-f", "title=t", "-f", "head=x", "-f", "base=main")
        assert rc == 1 and "drafts only" in err and ran == [], (ep, err)
        rc, err, ran = gh("api", ep, "-F", "draft=true", "-f", "title=t")
        assert rc == 0 and ran, (ep, err)
    rc, err, ran = gh("pr", "create", "-t", "t", "-b", "b")
    assert rc == 1 and "refused" in err and ran == [], err


def test_workers_gh_never_requests_human_reviewers_over_any_api_route(env, tmp_path):
    p = make(env)
    gh = _gh_runner(p, tmp_path)
    _approve_7(p)   # an approval to leave draft is not one to request reviews
    refused = [("api", ep, "-f", _REVIEW) for ep in ("graphql", "/graphql", "graphql/", "GraphQL", *_GHE_GRAPHQL)]
    refused += [("api", "repositories/123/pulls/7/requested_reviewers", "-f", "reviewers[]=someone"),
                ("api", "-X", "POST", "/repositories/123/pulls/7/requested_reviewers/", "-f", "team_reviewers[]=core"),
                ("api", "/graphql", "-f", _REVIEW.replace("requestReviews", "requestReviewsByLogin"))]
    for args in refused:
        rc, err, ran = gh(*args)
        assert rc == 1 and "never request human reviewers" in err and ran == [], (args, err)
    allowed = [("api", "repositories/123/pulls/7/requested_reviewers", "-f",
                "reviewers[]=copilot-pull-request-reviewer[bot]"),
               ("api", "-X", "DELETE", "repositories/123/pulls/7/requested_reviewers", "-f", "reviewers[]=someone"),
               ("api", "repositories/123/pulls/7/requested_reviewers")]
    for args in allowed:
        rc, err, ran = gh(*args)
        assert rc == 0 and ran == [" ".join(args)], (args, err)
