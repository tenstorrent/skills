def test_a_worker_run_sizes_its_compact_window_above_its_fixed_prompt(env):
    """A light window of 100k compacts near 67k: a run whose fixed prompt is about 70k tokens compacted
    on its first tool result and thrashed until it died. The window is raised to fit the fixed prompt,
    the compaction margin and some headroom; the raise is recorded on the run."""
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    p.set_config("budget.compact_window_tokens", {"light": 100000, "standard": 150000, "deep": 400000})
    d.cfg = p.config()

    def run(tier, prompt, **kw):
        tid = p.db.add_task(f"t {tier} {len(prompt)}", "s", kind="code", tier=tier, origin="user")
        rid = d.start_run("worker", prompt, "claude", tier, str(p.root), task=p.db.task(tid), **kw)
        (p.runs / str(rid) / "STOP").touch()
        env_ = json.loads((p.runs / str(rid) / "run.json").read_text())["env"]
        note = json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (rid,))["note"] or "{}")
        return int(env_.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 0), note

    # About 70k tokens of fixed prompt (with the tool allowance), split over the task prompt and the
    # system text: the window clears it by the compaction margin plus headroom.
    chars = (70_000 - dmod.TOOL_ALLOWANCE_TOKENS) * dmod.CHARS_PER_TOKEN
    window, note = run("light", "p" * (chars // 2), append_system="s" * (chars // 2))
    assert window >= 143_000
    assert note["compact_window"] == {"tier_tokens": 100000, "raised_to": window, "fixed_prompt_tokens": 70_000}
    assert "compact window raised to 143000" in (p.logs / "daemon.log").read_text()
    # A system prompt and its context count too.
    assert run("light", "go", system="s" * (chars // 2), context="c" * (chars // 2))[0] == window
    # A small prompt keeps the tier's window and records nothing.
    window, note = run("light", "go")
    assert window == 100000 and "compact_window" not in note
    assert run("standard", "go")[0] == 150000
    # A configured window above the floor is kept, even for the long prompt.
    window, note = run("deep", "p" * chars)
    assert window == 400000 and "compact_window" not in note
    # The raise stops at the agent's 1M ceiling.
    window, note = run("light", "p" * 5_000_000)
    assert window == 1_000_000 and note["compact_window"]["raised_to"] == 1_000_000
    # 0 keeps compaction off, whatever the prompt.
    p.set_config("budget.compact_window_tokens", {"light": 0, "standard": 150000, "deep": 200000})
    d.cfg = p.config()
    window, note = run("light", "p" * chars)
    assert window == 0 and "compact_window" not in note


def test_fit_compact_window_bounds():
    from ttp.daemon import fit_compact_window, COMPACT_MARGIN_TOKENS, COMPACT_HEADROOM_TOKENS
    assert fit_compact_window(100000, 0) == (100000, None)
    assert fit_compact_window(0, 10_000_000) == (0, None)
    assert fit_compact_window(-1, 10_000_000) == (-1, None)
    assert fit_compact_window(100000, -5) == (100000, None)
    assert fit_compact_window(100000, 10_000_000) == (1_000_000, 2_520_000)
    # Exactly at the floor: kept, not reported as raised.
    floor = 20_000 + COMPACT_MARGIN_TOKENS + COMPACT_HEADROOM_TOKENS + 10_000
    assert fit_compact_window(floor, 40_000) == (floor, None)
    assert fit_compact_window(floor - 1, 40_000) == (floor, 30_000)
