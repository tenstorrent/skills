# The PR babysitting sweep (prbabysit.py, run by the pr-watch watcher): recorded gh answers as fixtures,
# no network. PRs are read through a stand-in for the watcher's batched read; REST reads are `watchers._gh`.

_PB_URL = "https://github.com/acme/widgets/pull/7"
_PB_FLOWS = {
    ".github/workflows/heavy.yml": "name: Heavy\non:\n  pull_request:\n    types: [labeled]\njobs:\n  x:\n"
                                   "    if: github.event.label.name == 'run-heavy'\n",
    ".github/workflows/nightly.yml": "name: Nightly\non:\n  workflow_dispatch:\njobs:\n  x:\n    runs-on: a\n",
    ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\njobs:\n  x:\n    runs-on: a\n",
}


def _pb_pr(**over):
    pr = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
          "url": _PB_URL, "title": "t", "author": {"login": "bot-owner"}, "headRefOid": "a" * 40,
          "headRefName": "ttp/x", "baseRefName": "main", "body": "", "comments": [], "reviews": [],
          "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reviewRequests": [],
          "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "statusCheckRollup": [{"name": "validate", "workflowName": "ci", "status": "COMPLETED",
                                 "conclusion": "SUCCESS"}]}
    return {**pr, **over}


def _pb_gh(monkeypatch, flows=_PB_FLOWS):
    """`watchers._gh` answering the workflow listing and files like GitHub's contents API; returns the calls."""
    import base64
    from ttp import watchers
    calls = []

    def gh(args, cwd):
        calls.append(args)
        path = args[1].split("?")[0].split("/contents/", 1)[-1] if args[:1] == ["api"] else ""
        if path == ".github/workflows":
            return [{"name": k.rsplit("/", 1)[-1], "path": k} for k in flows]
        if path in flows:
            return {"content": base64.b64encode(flows[path].encode()).decode()}
        return None
    monkeypatch.setattr(watchers, "_gh", gh)
    return calls


def _pb_run(env, monkeypatch, pr, cfg=None, task=True, name="demo"):
    """One pr-watch run over a task's PR read as `pr`; returns (project, events, chat lines)."""
    from ttp import chatdigest, watchers
    from ttp.daemon import Daemon
    p = make(env, name)
    for k, v in (cfg or {}).items():
        p.set_config(k, v)
    if task:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
               (_PB_URL, time.time()))
    d = Daemon(p.base)
    lines = []
    monkeypatch.setattr(chatdigest, "routine", lambda p_, chat, text, why, now=None, at_once=False:
                        lines.append((text, at_once)) or False)
    monkeypatch.setattr(watchers, "_read_prs", lambda order, root, w, t: {
        x["pr_url"]: {"pr": dict(pr), "data": None, "cut": None, "calls": ["pr view"]} for x in order})
    return p, d, lines


def _pb_events(p):
    return [(r["kind"], r["text"]) for r in p.db.q("SELECT kind, text FROM events WHERE source='pr'")]


def test_prbabysit_a_non_draft_pr_with_a_failed_check_gives_pr_findings(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    pr = _pb_pr(statusCheckRollup=[{"name": "validate", "workflowName": "ci", "status": "COMPLETED",
                                    "conclusion": "FAILURE"}])
    p, d, _ = _pb_run(env, monkeypatch, pr)
    watchers.watch_prs(d)
    ev = _pb_events(p)
    assert any(k == "pr_findings" and "validate" in t and "draft review" not in t for k, t in ev), ev


def test_prbabysit_skipped_and_never_run_workflows_give_pr_ci_not_run_once(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    pr = _pb_pr(statusCheckRollup=[
        {"name": "validate", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "build", "checkSuite": {"workflowRun": {"workflow": {"name": "Heavy"}}}, "status": "COMPLETED",
         "conclusion": "SKIPPED"}])
    p, d, _ = _pb_run(env, monkeypatch, pr, cfg={"pr_babysit.dispatch": {"acme/widgets": ["nightly.yml"]}})
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_ci_not_run"]
    assert len(ev) == 1, ev
    assert "Heavy (heavy.yml, trigger: label run-heavy)" in ev[0] and "Nightly (nightly.yml, trigger: workflow_dispatch)" in ev[0], ev
    assert "ci (" not in ev[0], "a workflow that ran is not listed"


def test_prbabysit_a_never_run_manual_workflow_is_listed_only_when_dispatch_names_it(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr())
    watchers.watch_prs(d)
    assert [t for k, t in _pb_events(p) if k == "pr_ci_not_run"] == [], "nothing is dispatchable: nothing to report"


def test_prbabysit_idle_past_idle_h_gives_pr_idle_once_with_the_reviewers(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 50 * 3600))
    pr = _pb_pr(createdAt=old, reviewRequests=[{"__typename": "User", "login": "rev1"}, {"name": "core"}],
                statusCheckRollup=[{"name": "validate", "workflowName": "ci", "status": "COMPLETED",
                                    "conclusion": "SUCCESS", "completedAt": old}])
    p, d, _ = _pb_run(env, monkeypatch, pr)
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_idle"]
    assert len(ev) == 1 and "idle for 50 h" in ev[0] and "rev1, core" in ev[0], ev
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    bots = {**pr, "updatedAt": fresh, "comments": [
        {"author": {"login": "ci[bot]"}, "body": "coverage", "createdAt": fresh},
        {"author": {"login": "bot-owner"}, "body": f"ping {watchers.TTP_MARKER}", "createdAt": fresh}],
        "statusCheckRollup": [{**pr["statusCheckRollup"][0], "completedAt": fresh}]}
    person = {**bots, "comments": [{"author": {"login": "rev1"}, "body": "looking", "createdAt": fresh}]}
    for later in (bots, person):
        monkeypatch.setattr(watchers, "_read_prs", lambda order, root, w, t, later=later: {
            x["pr_url"]: {"pr": dict(later), "data": None, "cut": None, "calls": []} for x in order})
        watchers.watch_prs(d)
        assert len([1 for k, _ in _pb_events(p) if k == "pr_idle"]) == 1, "the same quiet period is told once"
    rec = p.db.kv("pr_babysit")["acme/widgets#7"]
    assert rec["idle"] == pytest.approx(time.time() - 50 * 3600, abs=60), "bots, checks and the ping reset nothing"


def test_prbabysit_a_status_change_posts_one_chat_line_and_an_unchanged_sweep_none(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr(isDraft=True))
    watchers.watch_prs(d)
    assert lines == [], "a first sighting is a baseline"
    approved = _pb_pr(isDraft=False, reviewDecision="APPROVED")
    monkeypatch.setattr(watchers, "_read_prs", lambda order, root, w, t: {
        x["pr_url"]: {"pr": dict(approved), "data": None, "cut": None, "calls": []} for x in order})
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    assert sorted(lines) == [(f"[acme/widgets#7]({_PB_URL}): out of draft", False),
                             (f"[acme/widgets#7]({_PB_URL}): review approved", True)], lines


def test_prbabysit_red_ci_on_a_ready_pr_posts_at_once_and_green_joins_the_digest(env, monkeypatch):
    from ttp import prbabysit
    _pb_gh(monkeypatch)
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr())
    t = {"id": None, "pr_url": _PB_URL}
    sig = lambda checks: {"state": "OPEN", "draft": False, "decision": "", "mergeable": "MERGEABLE", "checks": checks}
    for checks in ("passing", "failing", "pending", "failing", "passing"):
        prbabysit.sweep(d, t, _pb_pr(), sig(checks), lambda a, c: None)
    assert lines == [(f"[acme/widgets#7]({_PB_URL}): CI red", True), (f"[acme/widgets#7]({_PB_URL}): CI green", False)]


def test_prbabysit_exit_75_gives_no_event(env, monkeypatch):
    from ttp import watchers
    answer, calls = _stub_gh(env, monkeypatch, rc=75, err="queued for the workstation")
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr(), cfg={"pr_babysit.dispatch": {"acme/widgets": ["nightly.yml"]}})
    watchers.watch_prs(d)
    assert _gh_calls(calls), "the stub gh was asked"
    assert [k for k, _ in _pb_events(p) if k in ("pr_ci_not_run", "pr_idle")] == [] and lines == []


def test_prbabysit_disabled_runs_nothing(env, monkeypatch):
    from ttp import watchers
    calls = _pb_gh(monkeypatch)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90 * 3600))
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr(createdAt=old, isDraft=True), cfg={"pr_babysit.enabled": False})
    watchers.watch_prs(d)
    assert calls == [] and lines == [] and not p.db.kv("pr_babysit")
    assert not [k for k, _ in _pb_events(p) if k in ("pr_ci_not_run", "pr_idle")]


def test_prbabysit_defaults_and_purview_extra_prs_enter_the_watch_even_while_gh_is_logged_out(env, monkeypatch):
    from ttp import prbabysit, watchers
    assert prbabysit.settings({}) == {"enabled": True, "idle_h": 24, "dispatch": {}, "no_post_prs": []}
    _logged_out()
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr(), cfg={"watchers.purview_extra": ["acme/widgets#9"]}, task=False)
    seen = []
    monkeypatch.setattr(watchers, "_read_anon", lambda daemon, order, results: seen.extend(
        t["pr_url"] for t in order) or None)
    watchers.watch_prs(d)
    assert seen == ["https://github.com/acme/widgets/pull/9"], seen
    p.db.set_kv("pr_purview", {"at": time.time(), "prs": {"https://github.com/acme/widgets/pull/5": {"branch": "b"}}})
    seen.clear()
    watchers.watch_prs(d)   # a recorded purview PR is read too, and a known record does not stop the run
    assert set(seen) == {"https://github.com/acme/widgets/pull/5", "https://github.com/acme/widgets/pull/9"}, seen


def test_prbabysit_a_purview_extra_owner_repo_number_is_read_and_owned(env, monkeypatch):
    from ttp import prbabysit, watchers
    _pb_gh(monkeypatch)
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr(url="https://github.com/acme/widgets/pull/9", headRefName="b9"),
                      cfg={"watchers.purview_extra": ["acme/widgets#9"]}, task=False)
    url = "https://github.com/acme/widgets/pull/9"
    assert url in watchers._purview_scope(d)[2]
    p.db.set_kv("pr_purview", {"at": time.time(), "prs": {url: {"branch": "b9"}}})
    watchers.watch_prs(d)
    assert prbabysit.owned(p.db) == {"acme/widgets#9": "b9"}, p.db.kv("pr_babysit", {})


def test_prbabysit_an_idle_purview_pr_is_read_every_sweep_not_daily(env, monkeypatch):
    from ttp import prbabysit, watchers
    p, gh, d = _purview_setup(env, monkeypatch, {9: _gql_node(9)})
    now = [time.time()]
    monkeypatch.setattr(watchers.time, "time", lambda: now[0])
    reads = lambda: sum(c[:2] == ["api", "graphql"] or c[:2] == ["pr", "view"] for c in gh.calls)
    watchers.watch_prs(d)
    first = reads()
    now[0] += 7 * 3600   # idle by the purview rules, which alone would wait a day
    watchers.watch_prs(d)
    assert reads() > first, "a PR nobody works on is still read, so an approval or a conflict is told"
    assert prbabysit.READ_S < 3600


def _pb_guarded(env, extra=None):
    """A project whose sweep has recorded one open owned PR (acme/widgets#7, branch ttp/x), and its settings."""
    p = make(env)
    p.db.set_kv("pr_babysit", {_PB_URL: {"branch": "ttp/x", "open": True}})
    cfg = {"pr_babysit": {"dispatch": {"acme/widgets": ["heavy.yml", "label:run-heavy"]}, **(extra or {})}}
    return p, cfg


def _pb_why(p, cfg, *args):
    from ttp import prguard
    return prguard.check(list(args), "/nonexistent/gh", db=p.db, cfg=cfg)


def test_prbabysit_guard_lets_a_listed_workflow_run_on_an_owned_prs_head_and_nothing_else(env):
    p, cfg = _pb_guarded(env)
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"branch": "ttp/x", "open": True}})
    run = ("workflow", "run", "heavy.yml", "-R", "acme/widgets")
    assert _pb_why(p, cfg, *run, "--ref", "ttp/x") is None
    assert _pb_why(p, cfg, *run, "--ref=ttp/x") is None
    assert "not the head of an open PR this project owns" in _pb_why(p, cfg, *run, "--ref", "main")
    assert "not the head of an open PR this project owns" in _pb_why(p, cfg, *run, "--ref", "ttp/other")
    assert "not the head of an open PR this project owns" in _pb_why(
        p, cfg, "workflow", "run", "heavy.yml", "-R", "acme/other", "--ref", "ttp/x")
    assert "not listed" in _pb_why(p, cfg, "workflow", "run", "nightly.yml", "-R", "acme/widgets", "--ref", "ttp/x")
    for extra in (["-f", "a=b"], ["-F", "a=b"], ["--raw-field", "a=b"], ["--json"], ["--ref", "main"]):
        assert "no inputs or other options" in _pb_why(p, cfg, *run, "--ref", "ttp/x", *extra), extra
    assert "no inputs or other options" in _pb_why(p, cfg, "workflow", "run", "-R", "acme/widgets", "--ref", "ttp/x")


def test_prbabysit_guard_adds_only_a_listed_label_to_an_owned_pr_and_never_a_reviewer(env):
    p, cfg = _pb_guarded(env)
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"branch": "ttp/x", "open": True}})
    edit = ("pr", "edit", "7", "-R", "acme/widgets")
    assert _pb_why(p, cfg, *edit, "--add-label", "run-heavy") is None
    assert "not listed" in _pb_why(p, cfg, *edit, "--add-label", "other")
    assert "not listed" in _pb_why(p, cfg, *edit, "--add-label", "run-heavy,other")
    assert "only on an open PR this project owns" in _pb_why(p, cfg, "pr", "edit", "8", "-R", "acme/widgets",
                                                              "--add-label", "run-heavy")
    assert "only on an open PR this project owns" in _pb_why(p, cfg, "issue", "edit", "7", "-R", "acme/widgets",
                                                              "--add-label", "run-heavy")
    assert _pb_why(p, cfg, *edit, "--add-reviewer", "someone"), "reviewer requests stay refused"
    assert _pb_why(p, cfg, *edit, "--add-label", "run-heavy", "--add-reviewer", "someone")


def test_prbabysit_guard_requires_the_ai_tag_on_a_reviewer_ping_comment(env):
    p, cfg = _pb_guarded(env)
    call = ("pr", "comment", "7", "-R", "acme/widgets", "-b")
    assert "[Authored by AI]" in (_pb_why(p, cfg, *call, "Friendly ping: a review would unblock this.") or "")
    assert _pb_why(p, cfg, *call, "Friendly ping: a review would unblock this.\n\n[Authored by AI]") is None


def test_prbabysit_no_post_prs_refuse_comments_edits_dispatch_and_the_push(env, monkeypatch, tmp_path):
    from ttp import push
    p, cfg = _pb_guarded(env, {"no_post_prs": ["Acme/Widgets#7"]})
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"branch": "ttp/x", "open": True}})
    for args in (("pr", "comment", "7", "-R", "acme/widgets", "-b", "hi\n\n[Authored by AI]"),
                 ("pr", "comment", _PB_URL, "-b", "hi\n\n[Authored by AI]"),
                 ("pr", "edit", "7", "-R", "acme/widgets", "--add-label", "run-heavy"),
                 ("pr", "edit", _PB_URL, "--title", "t"),
                 ("workflow", "run", "heavy.yml", "-R", "acme/widgets", "--ref", "ttp/x")):
        assert "pr_babysit.no_post_prs" in _pb_why(p, cfg, *args), args
    other = ("pr", "comment", "8", "-R", "acme/widgets", "-b", "hi\n\n[Authored by AI]")
    assert _pb_why(p, cfg, *other) is None, "another PR in the repo is not held"
    p.set_config("pr_babysit.no_post_prs", ["acme/widgets#7"])
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "ttp/x", str(repo)], check=True)
    monkeypatch.delenv("TTP_TASK", raising=False)
    with pytest.raises(ValueError, match="no_post_prs"):
        push.own_target(p, repo)


def test_prbabysit_new_settings_default(env):
    from ttp import prbabysit
    assert prbabysit.settings({}) == prbabysit.DEFAULTS and prbabysit.DEFAULTS["dispatch"] == {}
    assert prbabysit.DEFAULTS["no_post_prs"] == [] and "extra_prs" not in prbabysit.DEFAULTS
    assert make(env).config()["pr_babysit"] == prbabysit.DEFAULTS


@pytest.mark.parametrize("cfg,want,absent", [
    ({}, "Ping them once", "No-post"),
    ({"pr_babysit.no_post_prs": ["acme/widgets#7"]}, "No-post PR: tell the user", "Ping them")])
def test_prbabysit_pr_idle_text_names_the_ping_unless_the_pr_is_no_post(env, monkeypatch, cfg, want, absent):
    from ttp import watchers
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 50 * 3600))
    pr = _pb_pr(createdAt=old, reviewRequests=[{"__typename": "User", "login": "rev1"}],
                statusCheckRollup=[{"name": "validate", "workflowName": "ci", "status": "COMPLETED",
                                    "conclusion": "SUCCESS", "completedAt": old}])
    _pb_gh(monkeypatch)
    p, d, _ = _pb_run(env, monkeypatch, pr, cfg=cfg)
    watchers.watch_prs(d)
    text = [t for k, t in _pb_events(p) if k == "pr_idle"]
    assert len(text) == 1 and want in text[0] and absent not in text[0], text
    watchers.watch_prs(d)
    assert len([k for k, _ in _pb_events(p) if k == "pr_idle"]) == 1, "the same quiet period is reported once"


def test_prbabysit_guard_covers_gh_api_writes_to_a_no_post_pr_and_workflow_dispatches(env):
    p, cfg = _pb_guarded(env, {"no_post_prs": ["acme/widgets#7"]})
    api = ("api", "-X", "POST")
    assert "no_post_prs" in _pb_why(p, cfg, *api, "repos/acme/widgets/issues/7/comments", "-f", "body=hi\n[Authored by AI]")
    assert "no_post_prs" in _pb_why(p, cfg, "api", "repos/acme/widgets/pulls/7/reviews", "-f", "event=APPROVE")
    assert "no_post_prs" in _pb_why(p, cfg, *api, "graphql", "-f", "query=mutation{addComment}")
    assert "gh workflow run" in _pb_why(p, cfg, *api, "repos/acme/widgets/actions/workflows/heavy.yml/dispatches")
    assert _pb_why(p, cfg, "api", "repos/acme/widgets/issues/7/comments") is None   # a read
    assert _pb_why(p, cfg, "api", "graphql", "-f", "query={viewer{login}}") is None   # a graphql read
    assert "gh pr edit" in _pb_why(p, cfg, *api, "repos/acme/widgets/issues/9/labels", "-f", "labels[]=run-heavy")
    assert "gh pr edit" in _pb_why(p, cfg, *api, "graphql", "-f", "query=mutation{addLabelsToLabelable}")
    assert _pb_why(p, cfg, *api, "repos/acme/widgets/issues/9/assignees") is None   # another PR
    patch = ("api", "-X", "PATCH")
    assert "gh pr edit" in _pb_why(p, cfg, *patch, "repos/acme/widgets/issues/9", "-f", "labels[]=run-heavy")
    assert "gh pr edit" in _pb_why(p, cfg, *patch, "repos/acme/widgets/issues/9", "-f", "labels=run-heavy")
    assert "gh pr edit" in _pb_why(p, cfg, *api, "graphql", "-f", "query=mutation{updatePullRequest(input:{labelIds:[1]}){x}}")
    assert "gh pr edit" in _pb_why(p, cfg, "pr", "create", "--label", "run-heavy")
    assert _pb_why(p, cfg, *patch, "repos/acme/widgets/issues/9", "-f", "title=x") is None   # another PR, no label
    assert _pb_why(p, cfg, *api, "repos/acme/widgets/pulls/9/comments/5/replies", "-f", "body=CI labels: run-heavy\n[Authored by AI]") is None
    assert "no_post_prs" in _pb_why(p, cfg, "pr", "update-branch", "7", "-R", "Acme/Widgets")


def test_prbabysit_a_workflow_read_not_answered_is_asked_again_next_sweep_and_kept_by_blob(env, monkeypatch):
    from ttp import watchers
    calls = _pb_gh(monkeypatch)
    real = watchers._gh
    gone = {"on": True}
    monkeypatch.setattr(watchers, "_gh", lambda a, c: None if gone["on"] and a[:1] == ["api"] else real(a, c))
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr(statusCheckRollup=[
        {"name": "build", "checkSuite": {"workflowRun": {"workflow": {"name": "Heavy"}}}, "status": "COMPLETED",
         "conclusion": "SKIPPED"}]))
    watchers.watch_prs(d)
    assert not p.db.kv("pr_workflows"), "an unanswered read caches nothing"
    gone["on"] = False
    watchers.watch_prs(d)
    assert len(p.db.kv("pr_workflows")) == len(_PB_FLOWS) + 1, "each file by blob, the listing by sha"
    n = len(calls)
    watchers.watch_prs(d)
    assert [c for c in calls[n:] if "/contents/.github/workflows/" in c[1]] == [], "each file is read once per blob"


def test_prbabysit_a_skipped_job_of_a_workflow_that_ran_is_not_not_run_and_a_label_entry_lists_its_workflow(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    pr = _pb_pr(statusCheckRollup=[
        {"name": "validate", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "publish", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SKIPPED"}])
    p, d, _ = _pb_run(env, monkeypatch, pr, cfg={"pr_babysit.dispatch": {"acme/widgets": ["label:run-heavy"]}})
    watchers.watch_prs(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_ci_not_run"]
    assert len(ev) == 1 and "Heavy (heavy.yml, trigger: label run-heavy)" in ev[0] and "ci (" not in ev[0], ev
    assert "Nightly" not in ev[0], "only what dispatch lists"


def test_prbabysit_pending_checks_hold_back_only_the_never_run_list_and_a_mergeable_unknown_repeats_nothing(env, monkeypatch):
    from ttp import prbabysit
    _pb_gh(monkeypatch)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 50 * 3600))
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr(), cfg={"pr_babysit.dispatch": {"acme/widgets": ["heavy.yml"]}})
    sig = lambda m, checks="pending": {"state": "OPEN", "draft": False, "decision": "", "mergeable": m, "checks": checks}
    pr = _pb_pr(createdAt=old, reviewRequests=[{"login": "rev1"}], statusCheckRollup=[
        {"name": "x", "workflowName": "ci", "status": "IN_PROGRESS", "conclusion": ""}])
    for m in ("MERGEABLE", "CONFLICTING", "UNKNOWN", "CONFLICTING"):
        prbabysit.sweep(d, {"id": None, "pr_url": _PB_URL}, pr, sig(m), lambda a, c: None)
    ev = [k for k, _ in _pb_events(p)]
    assert "pr_idle" in ev and "pr_ci_not_run" not in ev, ev
    assert [x for x, _ in lines].count(f"[acme/widgets#7]({_PB_URL}): has a merge conflict") == 1, lines


def test_prbabysit_dispatch_keys_match_any_case_and_a_nameless_workflow_is_named_by_its_path(env, monkeypatch):
    from ttp import prbabysit, watchers
    assert prbabysit.settings({"pr_babysit": {"dispatch": {"Acme/Widgets": ["x"]}}})["dispatch"] == {"acme/widgets": ["x"]}
    assert prbabysit.workflow_info(".github/workflows/h.yml", "on:\n  workflow_dispatch:\n")["name"] == ".github/workflows/h.yml"
    assert prbabysit.workflow_info("h.yml", "name: Build  # main build\non:\n  workflow_dispatch:\n")["name"] == "Build"
    _pb_gh(monkeypatch)
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr(), cfg={"pr_babysit.dispatch": {"Acme/Widgets": ["nightly.yml"]}})
    watchers.watch_prs(d)
    assert [t for k, t in _pb_events(p) if k == "pr_ci_not_run" and "Nightly" in t], _pb_events(p)


def _pb_route_state(alias: str, age_s: float, ok: bool = True) -> None:
    """The host-wide gh-route record: `alias` last answered `age_s` ago."""
    from ttp import ghauth, ghroute
    from ttp.project import write_json
    write_json(ghauth._home(None) / ghroute.STATE, {"ok": ok, "at": time.time() - age_s, "via": alias})


def test_prbabysit_a_route_silent_past_24_h_gets_one_keyed_event_and_clears_when_it_answers(env):
    from ttp import prbabysit
    from ttp.daemon import Daemon
    _logged_out()
    p = make(env, "route")
    d = Daemon(p.base)
    d.cfg = {"delivery": {"gh_route": "box1"}}
    _pb_route_state("box1", 25 * 3600)
    prbabysit.route_check(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_route_silent"]
    assert len(ev) == 1 and "box1" in ev[0] and "25 h" in ev[0] and "ssh alias" in ev[0], ev
    prbabysit.route_check(d)   # the same silence is told once
    assert len([1 for k, _ in _pb_events(p) if k == "pr_route_silent"]) == 1
    _pb_route_state("box1", 3600)   # the route answered: the condition clears
    prbabysit.route_check(d)
    assert not p.db.kv("pr_route"), "the mark clears once the route answers"
    _pb_route_state("box1", 26 * 3600)   # a fresh silence is told again
    prbabysit.route_check(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_route_silent"]
    assert len(ev) == 2 and "26 h" in ev[1], ev
    _pb_route_state("box1", 0, ok=False)   # a route answering only with failures is still silent
    prbabysit.route_check(d)
    assert len([1 for k, _ in _pb_events(p) if k == "pr_route_silent"]) == 2, "a failing answer is no answer"


def test_prbabysit_a_silent_workstation_queue_names_the_user_step(env):
    from ttp import prbabysit
    from ttp.daemon import Daemon
    _logged_out()
    p = make(env, "route2")
    d = Daemon(p.base)
    _pb_route_state("workstation", 25 * 3600)
    prbabysit.route_check(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_route_silent"]
    assert len(ev) == 1 and "gh queue" in ev[0] and "ttp bridge gh-pull" in ev[0], ev


def _pb_files_gh(files: dict):
    """A `watchers._gh` stand-in answering the changed-files read, and the calls it got."""
    calls = []

    def gh(a, c):
        calls.append(a)
        return files.get(a[1].split("/pulls/")[1].split("/")[0])
    return gh, calls


def test_prbabysit_two_open_prs_changing_the_same_files_get_one_event_per_pair(env, monkeypatch):
    from ttp import prbabysit
    from ttp.daemon import Daemon
    p = make(env, "overlap")
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"open": True, "branch": "b", "head": "h7"},
                                "acme/widgets#5": {"open": True, "branch": "b", "head": "h5"},
                                "acme/widgets#9": {"open": True, "branch": "b", "head": "h9"}})
    files = {"5": [{"filename": "a.py"}, {"filename": "b.py"}],
             "7": [{"filename": "a.py"}, {"filename": "c.py"}],
             "9": [{"filename": "z.py"}]}
    gh, calls = _pb_files_gh(files)
    prbabysit.overlap_check(Daemon(p.base), gh)
    ev = [t for k, t in _pb_events(p) if k == "pr_overlap"]
    assert len(ev) == 1 and "acme/widgets#7" in ev[0] and "acme/widgets#5" in ev[0], ev
    assert "a.py" in ev[0] and "#9" not in ev[0], ev[0]
    assert sorted(calls) == [["api", "repos/acme/widgets/pulls/5/files?per_page=100"],
                             ["api", "repos/acme/widgets/pulls/7/files?per_page=100"],
                             ["api", "repos/acme/widgets/pulls/9/files?per_page=100"]], calls
    n_calls = len(calls)
    prbabysit.overlap_check(Daemon(p.base), gh)   # once per pair
    assert len([1 for k, _ in _pb_events(p) if k == "pr_overlap"]) == 1
    assert calls[n_calls:] == [], "the files are cached per head"


def test_prbabysit_a_pair_with_another_project_pr_and_a_disjoint_pair_give_no_event(env, monkeypatch):
    from ttp import ghauth, prbabysit
    from ttp.daemon import Daemon
    from ttp.project import write_json
    p = make(env, "overlap2")
    p.db.set_kv("pr_babysit", {"acme/widgets#55": {"open": True, "branch": "b", "head": "h55"}})
    write_json(ghauth._home(None) / prbabysit.HOST_FILE, {"tt-buddy": {"at": time.time(),
             "prs": {"acme/widgets#46": {"files": ["b.py"], "head": "h46"},
                     "acme/widgets#47": {"files": ["q.py"], "head": "h47"}}}})
    gh, _ = _pb_files_gh({"55": [{"filename": "a.py"}, {"filename": "b.py"}]})
    prbabysit.overlap_check(Daemon(p.base), gh)
    ev = [t for k, t in _pb_events(p) if k == "pr_overlap"]
    assert len(ev) == 1 and "acme/widgets#55" in ev[0] and "#46" in ev[0] and "b.py" in ev[0], ev
    assert not any("#47" in t for t in ev), "disjoint files: no pair"
    assert "overlap2" in (ghauth._home(None) / prbabysit.HOST_FILE).read_text()


def test_ghroute_mark_keeps_each_routes_answer_under_the_state_lock(env, monkeypatch):
    from ttp import ghauth, ghroute
    held = []
    real = ghauth._locked
    monkeypatch.setattr(ghauth, "_locked", lambda home, fn, block, lock=ghauth.LOCK: held.append(lock) or real(home, fn, block, lock))
    ghroute.mark(True, via="box1")
    ghroute.mark(False, via="box2")
    assert held == [ghroute.STATE + ".lock"] * 2, "each write takes the route state's own lock"
    assert ghroute.last_answer("box1")["ok"] is True and ghroute.last_answer("box2")["ok"] is False


def test_ghroute_a_corrupt_route_file_reads_as_no_record_and_never_raises(env):
    from ttp import ghauth, ghroute
    path = ghauth._home(None) / ghroute.STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    for bad in ('{"via": "box1"}', '{"via": "box1", "ok": true}', '{"via": [1], "ok": true, "at": 1}',
                '{"box1": 5, "via": ""}', '{"box1": {"ok": true, "at": "soon"}}', "[1]", "{"):
        path.write_text(bad)
        assert ghroute.works(via="box1") is False and ghroute.last_answer("box1").get("ok") is not False, bad
        path.write_text(bad)
        ghroute.mark(True, via="box3")   # never raises into the gh call path
        assert ghroute.last_answer("box3")["ok"] is True, bad
        assert ghroute.works(via="box1") is False, bad
        path.write_text(bad)
        assert ghroute.works() is False, bad


def test_prbabysit_an_unread_pr_keeps_its_cached_files_in_the_host_record_and_a_project_with_no_open_pr_clears_it(env):
    from ttp import ghauth, prbabysit
    from ttp.daemon import Daemon
    p = make(env, "overlap3")
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"open": True, "branch": "b", "head": "h7"},
                                "acme/widgets#5": {"open": True, "branch": "b", "head": "h5"}})
    both = {"5": [{"filename": "a.py"}], "7": [{"filename": "c.py"}]}
    prbabysit.overlap_check(Daemon(p.base), _pb_files_gh(both)[0])
    # #5 gets a new head and its read gets no answer while #7 answers: #5 keeps its last files
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"open": True, "branch": "b", "head": "h7"},
                                "acme/widgets#5": {"open": True, "branch": "b", "head": "h5b"}})
    prbabysit.overlap_check(Daemon(p.base), _pb_files_gh({"7": both["7"]})[0])
    prs = prbabysit._host_prs(time.time())
    assert prs.get("acme/widgets#5") == ["a.py"] and prs.get("acme/widgets#7") == ["c.py"], prs
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"open": False}, "acme/widgets#5": {"open": False}})
    prbabysit.overlap_check(Daemon(p.base), _pb_files_gh({})[0])
    assert prbabysit._host_prs(time.time()) == {}, "closed PRs leave the record other projects read"
    for bad in ('{"other": 5}', '{"other": {"at": %d, "prs": [1]}}', '{"other": {"at": %d, "prs": {"a#1": 5}}}'):
        (ghauth._home(None) / prbabysit.HOST_FILE).write_text(bad.replace("%d", str(int(time.time()))))
        assert prbabysit._host_prs(time.time()) == {}, bad


def test_prbabysit_guard_never_dispatches_a_run_already_queued_or_running_on_the_head(env, tmp_path):
    """A live run of the workflow on exactly the PR's head covers its jobs: a push or PR event run (no inputs),
    or a dispatch run created after this project's own receipt. Another head, a finished run, a dispatch with no
    receipt (inputs not proven equal) or an unreadable run list do not count."""
    from ttp import prguard
    p, cfg = _pb_guarded(env)
    head = "a" * 40
    p.db.set_kv("pr_babysit", {"acme/widgets#7": {"branch": "ttp/x", "open": True, "head": head}})
    runs, gh = tmp_path / "runs.json", tmp_path / "gh"
    gh.write_text(f"#!/bin/sh\ncat {runs}\n")
    gh.chmod(0o755)
    later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
    why = lambda: prguard.check(["workflow", "run", "heavy.yml", "-R", "acme/widgets", "--ref", "ttp/x"],
                                str(gh), db=p.db, cfg=cfg)
    run = lambda **kw: json.dumps([{"databaseId": 41, "headSha": head, "status": "queued", "event": "pull_request",
                                    "createdAt": later, **kw}])
    for other in (run(event="workflow_dispatch"), run(headSha="b" * 40), run(status="completed"), "not json"):
        runs.write_text(other)
        assert why() is None, other
    assert list(p.db.kv("pr_dispatched", {})) == [f"acme/widgets heavy.yml {head}"], "an allowed dispatch leaves its receipt"
    for live in (run(), run(status="in_progress"), run(event="workflow_dispatch")):
        runs.write_text(live)
        assert "run 41 of heavy.yml is already queued or running" in (why() or ""), live


def test_prbabysit_a_skipped_or_never_run_workflow_or_an_unread_one_holds_the_clean_report(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    ok = {"name": "validate", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
    heavy = {"name": "build", "workflowName": "Heavy", "status": "COMPLETED", "conclusion": "SKIPPED"}
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr())
    clean = lambda: [t for k, t in _pb_events(p) if k == "pr_clean"]

    def read(*checks, head="a" * 40):
        monkeypatch.setattr(watchers, "_read_prs", lambda order, root, w, t: {x["pr_url"]: {
            "pr": _pb_pr(statusCheckRollup=list(checks), headRefOid=head), "data": {"data": {}}, "cut": None,
            "calls": []} for x in order})
        watchers.watch_prs(d)
    read({**ok, "conclusion": "FAILURE"}, head="0" * 40)   # told it has findings: a clean head after it is news
    read(ok, heavy)
    read(heavy, head="b" * 40)
    assert clean() == [], "a skipped workflow is no pass"
    monkeypatch.setattr(watchers, "_gh", lambda a, c: None)
    read(ok, heavy, head="d" * 40)
    assert clean() == [], "what ran is unknown while the workflow files are unread"
    _pb_gh(monkeypatch)
    read(ok, {**heavy, "conclusion": "SUCCESS"}, head="e" * 40)
    assert len(clean()) == 1, "every workflow ran on the head: clean"
