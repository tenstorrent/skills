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
    p, d, _ = _pb_run(env, monkeypatch, pr)
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_ci_not_run"]
    assert len(ev) == 1, ev
    assert "Heavy (trigger: label run-heavy)" in ev[0] and "Nightly (trigger: workflow_dispatch)" in ev[0], ev
    assert "ci (" not in ev[0], "a workflow that ran is not listed"


def test_prbabysit_idle_past_idle_h_gives_pr_idle_once_with_the_reviewers(env, monkeypatch):
    from ttp import watchers
    _pb_gh(monkeypatch)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 50 * 3600))
    pr = _pb_pr(updatedAt=old, reviewRequests=[{"__typename": "User", "login": "rev1"}, {"name": "core"}],
                statusCheckRollup=[{"name": "validate", "workflowName": "ci", "status": "COMPLETED",
                                    "conclusion": "SUCCESS", "completedAt": old}])
    p, d, _ = _pb_run(env, monkeypatch, pr)
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    ev = [t for k, t in _pb_events(p) if k == "pr_idle"]
    assert len(ev) == 1 and "idle for 50 h" in ev[0] and "rev1, core" in ev[0], ev
    monkeypatch.setattr(watchers, "_read_prs", lambda order, root, w, t: {
        x["pr_url"]: {"pr": _pb_pr(), "data": None, "cut": None, "calls": []} for x in order})
    watchers.watch_prs(d)
    assert len([1 for k, _ in _pb_events(p) if k == "pr_idle"]) == 1, "fresh activity raises nothing more"


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
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr())
    watchers.watch_prs(d)
    assert _gh_calls(calls), "the stub gh was asked"
    assert [k for k, _ in _pb_events(p) if k in ("pr_ci_not_run", "pr_idle")] == [] and lines == []


def test_prbabysit_disabled_runs_nothing(env, monkeypatch):
    from ttp import watchers
    calls = _pb_gh(monkeypatch)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90 * 3600))
    p, d, lines = _pb_run(env, monkeypatch, _pb_pr(updatedAt=old, isDraft=True), cfg={"pr_babysit.enabled": False})
    watchers.watch_prs(d)
    assert calls == [] and lines == [] and not p.db.kv("pr_babysit")
    assert not [k for k, _ in _pb_events(p) if k in ("pr_ci_not_run", "pr_idle")]


def test_prbabysit_defaults_and_extra_prs_enter_the_watch_even_while_gh_is_logged_out(env, monkeypatch):
    from ttp import prbabysit, watchers
    assert prbabysit.settings({}) == {"enabled": True, "idle_h": 24, "extra_prs": []}
    _logged_out()
    p, d, _ = _pb_run(env, monkeypatch, _pb_pr(), cfg={"pr_babysit.extra_prs": ["acme/widgets#9"]}, task=False)
    seen = []
    monkeypatch.setattr(watchers, "_read_anon", lambda daemon, order, results: seen.extend(
        t["pr_url"] for t in order) or None)
    watchers.watch_prs(d)
    assert seen == ["https://github.com/acme/widgets/pull/9"], seen


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
