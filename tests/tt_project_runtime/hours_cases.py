# The user's usual active hours (hours.py, `ttp hours`): from the times of their own messages in the
# home zone, never fixed defaults. Run from tests/test_tt_project_runtime.py (see _include_cases there):
# helpers, fixtures and imports come from that file, and pytest collects these tests under it.
from __future__ import annotations


def _local_ts(zone, day, hour, minute=30):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 7, day, hour, minute, tzinfo=ZoneInfo(zone)).timestamp()


def test_hours_with_too_few_messages_or_no_quiet_hours_fall_back_to_now():
    from ttp import hours
    zone = "Europe/Berlin"
    now = _local_ts(zone, 20, 3)
    few = [_local_ts(zone, d, 10) for d in range(1, 6)]
    r = hours.assess(few, zone, now=now)
    assert r["fallback"] and r["ok"] and r["active"] is None and r["next_start"] == now, r
    assert "too few" in hours.describe(r, now) and "start now" in hours.describe(r, now)
    # Many messages on one day only: still too few days to tell.
    one_day = [_local_ts(zone, 5, h, m) for h in range(8, 20) for m in (0, 20, 40)]
    assert hours.assess(one_day, zone, now=now)["fallback"]
    # Messages at every hour of the day: no quiet hours, so no reason to wait.
    all_day = [_local_ts(zone, d, h) for d in range(1, 8) for h in range(24)]
    r = hours.assess(all_day, zone, now=now)
    assert r["fallback"] and r["ok"] and "no quiet run" in r["why"], r


def test_hours_a_clear_daytime_pattern_yields_a_window_in_the_home_zone():
    from ttp import hours
    zone = "Europe/Berlin"
    times = [_local_ts(zone, d, h, m) for d in range(1, 11) for h in range(9, 18) for m in (5, 35)]
    times.append(_local_ts(zone, 4, 23))          # one late night is not a habit
    r = hours.assess(times, zone, now=_local_ts(zone, 12, 12, 0))
    assert not r["fallback"] and r["active"]["start"] == "09:00" and r["active"]["end"] == "18:00", r
    assert r["ok"] and r["active_now"] and r["until"] == _local_ts(zone, 12, 18, 0)
    # At night: not now; the next window starts at 09:00 that morning.
    night = _local_ts(zone, 12, 3, 10)
    r = hours.assess(times, zone, now=night)
    assert not r["ok"] and not r["active_now"] and r["next_start"] == _local_ts(zone, 12, 9, 0), r
    line = hours.describe(r, night)
    assert "09:00-18:00 CEST" in line and "outside" in line and "09:00 CEST" in line, line
    # Late in the window with a step that needs an hour: the next morning's window.
    late = _local_ts(zone, 12, 17, 30)
    r = hours.assess(times, zone, now=late, need_s=3600)
    assert not r["ok"] and r["active_now"] and r["next_start"] == _local_ts(zone, 13, 9, 0), r
    assert hours.assess(times, zone, now=late, need_s=600)["ok"]
    # The same messages read in another zone give that zone's hours: the home zone decides.
    r = hours.assess(times, "UTC", now=night)
    assert (r["active"]["start"], r["active"]["end"]) == ("07:00", "16:00"), r


def test_ttp_hours_reads_only_the_users_messages_and_check_exits_1_outside_the_window(env, capsys, monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    p = make(env)
    zone = "Asia/Tokyo"
    p.set_config("home_timezone", zone)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    # Too few messages: start now.
    assert _ttp("hours", "--check") == 0
    assert "no usual active hours yet" in capsys.readouterr().out
    # The user writes at every hour but the six around now, over the last five days.
    now = datetime.now(ZoneInfo(zone))
    quiet = {(now.hour + k) % 24 for k in range(-2, 4)}
    stamps = []
    for d in range(1, 6):
        day = (now - timedelta(days=d)).replace(minute=30, second=0, microsecond=0)
        stamps += [day.replace(hour=h).timestamp() for h in range(24) if h not in quiet]
    for ts in stamps:
        mid = p.db.post("in", "a message", chat="web", channel="web", kind="user", provenance="web")
        p.db.x("UPDATE messages SET ts=? WHERE id=?", (ts, mid))
    # The project's own system posts are not the user's: one at this hour every day changes nothing.
    for d in range(1, 6):
        mid = p.db.post("in", "brief", channel="system", kind="user", provenance="system")
        p.db.x("UPDATE messages SET ts=? WHERE id=?", ((now - timedelta(days=d)).timestamp(), mid))
    assert _ttp("hours", "--json", "--check") == 1
    r = json.loads(capsys.readouterr().out)
    assert r["messages"] == len(stamps) and not r["ok"] and not r["fallback"], r
    assert r["next_start_text"].endswith(" JST") and "outside" in r["summary"], r


def test_hours_presence_passes_only_within_the_minutes_and_names_the_last_message_in_the_home_zone():
    from ttp import hours
    zone = "America/Los_Angeles"
    now = _local_ts(zone, 20, 9, 15)
    r = hours.presence(now - 4 * 60, zone, 10, now=now)
    assert r["ok"] and "09:11 PDT (4 min ago)" in r["text"] and "present" in r["text"], r
    # Exactly at the limit still counts; a second past it does not.
    assert hours.presence(now - 600, zone, 10, now=now)["ok"]
    r = hours.presence(now - 601, zone, 10, now=now)
    assert not r["ok"] and "09:04 PDT (10 min ago)" in r["text"] and "wait until they write" in r["text"], r
    # The day before: the day is named, hours ago, still the home zone (never UTC).
    r = hours.presence(_local_ts(zone, 19, 8, 14), zone, 10, now=now)
    assert not r["ok"] and "Sun 08:14 PDT (25 h ago)" in r["text"] and "UTC" not in r["text"], r
    r = hours.presence(None, zone, 10, now=now)
    assert not r["ok"] and "has not written" in r["text"], r


def test_ttp_hours_present_exits_1_with_when_the_user_last_wrote_and_0_once_they_write(env, capsys, monkeypatch):
    import time as _time
    p = make(env)
    p.set_config("home_timezone", "Asia/Tokyo")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    # Never wrote: absent, even though there is no usual pattern yet (which alone says "start now").
    assert _ttp("hours", "--check", "--present", "10") == 1
    out = capsys.readouterr().out
    assert out.startswith("ttp hours: the user has not written") and "start now" not in out, out
    # Wrote 25 minutes ago: still absent; the one line says when, in the home zone.
    mid = p.db.post("in", "hi", chat="web", channel="web", kind="user", provenance="web")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (_time.time() - 25 * 60, mid))
    # The project's own system post a minute ago is not the user being there.
    sid = p.db.post("in", "brief", channel="system", kind="user", provenance="system")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (_time.time() - 60, sid))
    assert _ttp("hours", "--check", "--for", "300", "--present", "10") == 1
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1 and " JST (25 min ago)" in out and "not within the last 10 min" in out, out
    assert _ttp("hours", "--json", "--check", "--present", "10") == 1
    r = json.loads(capsys.readouterr().out)
    assert not r["present"]["ok"] and r["present"]["last_text"].endswith(" JST") and "25 min ago" in r["summary"], r
    # They write now: present, and the window check still runs alongside.
    mid = p.db.post("in", "back", chat="web", channel="web", kind="user", provenance="web")
    assert _ttp("hours", "--check", "--for", "300", "--present", "10") == 0
    out = capsys.readouterr().out
    assert "start now" in out and "(0 min ago), within the last 10 min: present" in out, out
    # Without --present nothing changes: the check passes on the window alone.
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (_time.time() - 3600, mid))
    assert _ttp("hours", "--check", "--for", "300") == 0
    assert "last wrote" not in capsys.readouterr().out


def test_worker_prompt_holds_a_short_deadline_user_step_until_the_user_is_present():
    text = " ".join((RUNTIME.parent / "template" / "prompts" / "worker.md").read_text().split())
    for part in ("`ttp hours --check --for <seconds they need>`", "plus `--present 10` if under 10 min",
                 "never start or send it on a timer", "with that check as `retry_when`"):
        assert part in text, part
