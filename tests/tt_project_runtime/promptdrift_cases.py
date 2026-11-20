# Worker prompt drift tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of the
# check that flags a project's own harness/prompts/worker.md against the template (promptdrift.py) here.
from __future__ import annotations


def _pd_template(p):
    from ttp import promptdrift
    text = promptdrift.template_text(p)
    assert text and "## Handoff (required)" in text
    return text


def _pd_write(p, text):
    (p.harness / "prompts" / "worker.md").write_text(text)


def _pd_grow(template, ratio):
    pad = int(len(template.encode()) * (ratio - 1))
    return template + "\n## Our own rules\n" + ("x" * 79 + "\n") * (pad // 80)


def test_prompt_drift_flags_a_worker_prompt_over_the_size_ratio_and_not_under_it(env):
    from ttp import promptdrift
    p = make(env)
    template = _pd_template(p)
    assert promptdrift.line(p) == ""   # the copy a project starts with is the template's
    _pd_write(p, _pd_grow(template, 1.4))
    assert promptdrift.line(p) == ""
    _pd_write(p, _pd_grow(template, 1.7))
    line = promptdrift.line(p)
    assert line.startswith("worker prompt: harness/prompts/worker.md ") and "(limit 1.5x)" in line, line
    assert "Merge it toward the template" in line
    # The ratio is the project's own setting; 0 switches the size flag off.
    p.set_config("coordinator.prompt_drift_ratio", 2)
    assert promptdrift.line(p) == ""
    p.set_config("coordinator.prompt_drift_ratio", 1.2)
    assert "(limit 1.2x)" in promptdrift.line(p)
    p.set_config("coordinator.prompt_drift_ratio", 0)
    assert promptdrift.line(p) == ""


def test_prompt_drift_flags_a_missing_template_section_and_one_the_template_dropped(env):
    from ttp import promptdrift
    p = make(env)
    template = _pd_template(p)
    _pd_write(p, template.replace("## Budget and time", "## Budget"))
    r = promptdrift.report(p)
    assert r["missing"] == ["## Budget and time"] and not r["big"] and not r["stale"]
    assert "lacks the template's '## Budget and time'" in promptdrift.line(p)
    # A section an older template had and a later one dropped (the harness's upstream snapshots) is stale
    # in a copy that still has it; a section the template never had is the project's own.
    wt = p.state / "pd-upstream"
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "-C", str(p.harness), "worktree", "add", "-q", str(wt), "upstream"], check=True)
    for text in (template + "\n## Old cluster rules\nold\n", template):
        (wt / "prompts" / "worker.md").write_text(text)
        subprocess.run(["git", "-C", str(wt), *ident, "commit", "-qam", "template"], check=True)
    subprocess.run(["git", "-C", str(p.harness), "worktree", "remove", "--force", str(wt)], check=True)
    _pd_write(p, template + "\n## Old cluster rules\nold\n\n## Our own rules\nours\n")
    r = promptdrift.report(p)
    assert r["stale"] == ["## Old cluster rules"] and not r["missing"]
    assert "keeps sections the template dropped: '## Old cluster rules'" in promptdrift.line(p)


def test_prompt_drift_tells_the_projects_coordinator_once_per_changed_copy(env):
    import time as _time
    from ttp import promptdrift
    p = make(env)
    template = _pd_template(p)
    events = lambda: p.db.q("SELECT severity,text,status FROM events WHERE kind='worker_prompt_drift'")
    now = _time.time()
    assert promptdrift.check(p, now) == "" and events() == []   # the template's own copy: nothing
    _pd_write(p, _pd_grow(template, 1.8))
    assert promptdrift.check(p, now + 1)
    rows = events()
    assert len(rows) == 1 and rows[0]["severity"] == "low" and rows[0]["status"] == "queued"
    assert "Your coordinator owns this copy" in rows[0]["text"] and "never edits it" in rows[0]["text"]
    # Unchanged: never again, also after the hourly look and a touch that leaves the text as it was.
    assert promptdrift.check(p, now + 2) == ""
    assert promptdrift.check(p, now + 2 * promptdrift.EVERY_S) == ""
    _pd_write(p, _pd_grow(template, 1.8))
    assert promptdrift.check(p, now + 3 * promptdrift.EVERY_S) == "" and len(events()) == 1
    # A changed copy that is still flagged is raised once more; the harness never edits the copy.
    _pd_write(p, _pd_grow(template, 2.0))
    before = (p.harness / "prompts" / "worker.md").read_text()
    assert promptdrift.check(p, now + 4 * promptdrift.EVERY_S) and len(events()) == 2
    assert (p.harness / "prompts" / "worker.md").read_text() == before


def test_prompt_drift_shows_in_doctor_and_the_daily_review(env, capsys):
    from ttp import audit, cli
    p = make(env)
    _pd_write(p, _pd_grow(_pd_template(p), 1.8))
    assert any(x.startswith("worker prompt: harness/prompts/worker.md ") for x in audit.lines(p.db, p=p))
    cli.main(["doctor", p.name])
    assert "worker prompt: harness/prompts/worker.md " in capsys.readouterr().out


def test_the_daemon_tick_runs_the_worker_prompt_drift_check(env):
    from ttp import daemon
    p = make(env)
    _pd_write(p, _pd_grow(_pd_template(p), 1.8))
    daemon.Daemon(p.base).lint_worker_prompt()
    assert p.db.one("SELECT id FROM events WHERE kind='worker_prompt_drift'")
