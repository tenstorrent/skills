# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Desktop notifications for the machine the user sits at, with no account or setup.

One small agent per workstation watches every project in this machine's registry — local ones by
reading their database, remote ones with one ssh call per minute — and shows each new alert at
or above the severity floor as a native notification with a sound, meant to stay until closed (macOS
Notification Center through the tt-project app, which asks for the persistent style, once macOS shows it allowed
(ensure_app, app_registered; notifier.json `app: false` skips it), else terminal-notifier or osascript; or
notify-send, critical with no expiry, on a Linux desktop; notifier.json `persistent: false` lets them go). It never marks anything read; chats
and the web app keep their own cursors. Every delivery is recorded (DELIVERY): a failure goes to the
notifier log and raises one alert in this machine's projects until a later delivery succeeds
(Daemon.check_notifier); `ttp notifier test` sends one and tells how to fix a blocked one. The agent
logs its code version at start (RUNNING) and exits once the installed code changes, so its service
restarts it on the new code; `ttp setup` and `ttp upgrade` restart it too.
A remote host whose ssh polls keep failing gets one notification once it has been down for
HOST_DOWN_AFTER_S, and one more when it answers again: while it is down its projects cannot page.
So does a project whose daemon has not beaten for DAEMON_DOWN_AFTER_S while a service should keep it
running: its alerts would otherwise stop with no sign, as `ttp alerts` on an answering host is empty.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import headline, hoststate, sshcmd, timefmt
from .db import DB, SEVERITY_RANK
from .project import HOME_DIR, Project, durable_write, hostname, load_registry, write_json

STATE = HOME_DIR / "notifier-state.json"
CONF = HOME_DIR / "notifier.json"
HOSTS = "_hosts"   # state key for remote host reachability; no project name starts with '_'
HOST_DOWN_AFTER_S = 20 * 60   # notifier.json `host_down_after_s` overrides
ENABLED_CACHE_S = 60   # failing(): how long the agent's enabled state is reused between checks
HOST_POLL_GAP_S = 5 * 60   # polls further apart (sleep, notifier stopped) do not continue a failing streak
DAEMONS = "_daemons"   # state key for projects whose daemon is down
DAEMON_DOWN_AFTER_S = 3 * 3600   # notifier.json `daemon_down_after_s` overrides (floor 2 * HEARTBEAT_STALE_S)
NO_HEALTH = "_no_health"   # state key: host -> when its older ttp rejected `alerts --health`
HEALTH_RETRY_S = 3600   # how often such a host is asked with --health again (it may have been upgraded)
DELIVERY = HOME_DIR / "notifier-delivery.json"   # the last delivery's outcome, and the code last tested (refresh)
RUNNING = HOME_DIR / "notifier-running.json"   # the agent's pid and code version, written at its start
LOG = HOME_DIR / "notifier.log"
LABEL = "com.tt-project.notifier"   # the launchd agent
UNIT = "tt-project-notifier.service"   # the systemd user unit
ALERT_KEY = "notifier"   # the projects' alert while desktop delivery fails (Daemon.check_notifier)
TIMEOUT_S = 10
BUNDLE_ID = "com.tt-project.notifier"   # the macOS notifier app (ensure_app): its own Notifications entry
APP_NAME = "tt-project"
CLICK_URL = HOME_DIR / "notifier-click-url"   # the app opens it when an alert is clicked
STYLES = {0: "none", 8: "banners", 16: "persistent"}   # com.apple.ncprefs flags & 0x38, read only
LOG_TIMEOUT_S = 20   # one read of macOS's unified log (usernoted)
LOG_CHECK_S = 60   # while the app's allowance is unknown: how often an alert re-reads the log
LOG_SETTLE_S = 15   # after the app's first post: how long usernoted may take to log it (setup polls, no back-off)
LOG_POLL_S = 1   # how often setup re-reads the log in that window
LOG_WINDOW_S = 24 * 3600   # how long after the app's first post its log entries are looked for
SENDER_IDS = {APP_NAME: BUNDLE_ID, "osascript": "com.apple.ScriptEditor2",
              "terminal-notifier": "fr.julienxx.oss.terminal-notifier"}   # whose usernoted lines a test reads
DENIED = ("denied", "not authorized", "unauthorized", "disallowed", "not allowed")
SHOWN = re.compile(r"(?:usernoted:\s*|\d{4}-\d\d-\d\d[ T][\d:.+-]+\s+\w+\s+usernoted\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)?)?"
                   r"(not\s+)?(delivering|presenting)\b", re.I)   # a record BEGINNING so (after its prefix): shown, or not
LOG_RECORD = re.compile(r"\d{4}-\d\d-\d\d[ T]\d")   # `log show --style compact`: a line starting a record


def alerts_since(p: Project, after: int, floor: str = "high") -> list[dict]:
    """Broadcasts at or above the floor after `after`. Ones whose condition has since cleared are
    returned marked `cleared`, so the reader moves its cursor past them without showing them."""
    from .web import cleared
    rank = SEVERITY_RANK.get(floor, 2)
    # Filter severity in the query: a page of quieter broadcasts would otherwise stall the cursor.
    levels = [s for s, r in SEVERITY_RANK.items() if r >= rank]
    db = DB(p.state / "project.db")
    try:
        rows = db.q("SELECT id, ts, kind, severity, text, ref, channel, handled FROM messages WHERE direction='out' AND chat IS NULL "
                    f"AND id>? AND severity IN ({','.join('?' * len(levels))}) ORDER BY id LIMIT 50",
                    (after, *levels))
        now = time.time()
        for r in rows:
            # Information (a host reboot) is never pushed as an alert; the cursor still moves past it.
            # So is a quiet alert: another project on this machine already raised the same condition.
            r["cleared"] = r["kind"] == "info" or r.pop("channel") == "quiet" or cleared(db, r, now)
            del r["ref"], r["handled"]
    finally:
        db.close()
    return rows


def health(p: Project, now: float | None = None) -> dict:
    """{heartbeat_age_s (None without a heartbeat), service (its kind, None without one), paused}. The age
    is taken on the project's own host, so a reader elsewhere never compares two machines' clocks."""
    from . import service
    now = time.time() if now is None else now
    try:
        age = max(0.0, now - (p.state / "heartbeat").stat().st_mtime)
    except OSError:
        age = None
    svc = service.installed(p)
    db = DB(p.state / "project.db")
    try:
        paused = bool(db.kv("paused", False))
    finally:
        db.close()
    return {"heartbeat_age_s": age, "service": svc["kind"] if svc else None, "paused": paused}


def _remote_alerts(entry: dict, name: str, after: int, floor: str, state: dict | None = None,
                   now: float | None = None) -> tuple[list[dict] | None, bool, dict | None]:
    """(the alerts, or None when the poll failed; whether ssh reached the host; the project's health,
    or None when unknown). ssh exits 255 when it cannot connect; any other failure is the remote
    command's, on a host that answers. A host whose older ttp rejects --health is asked without it,
    and with it again once an hour (kept in `state`)."""
    now = time.time() if now is None else now
    no_health = state.setdefault(NO_HEALTH, {}) if state is not None else {}
    host = _host(entry)
    try:
        sshcmd.check_host(host)
    except sshcmd.BadHost:
        return None, False, None
    ask = now - float(no_health.get(host, 0)) >= HEALTH_RETRY_S
    cmd = sshcmd.remote_cmd(entry, ["alerts", name, "--after", after, "--floor", floor, "--json"])
    for flag in (" --health", "") if ask else ("",):
        try:
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", host,
                                  cmd + flag], capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            return None, False, None
        if flag and out.returncode == 2 and "unrecognized arguments" in (out.stderr or ""):
            no_health[host] = now
            continue
        break
    if out.returncode != 0:
        return None, out.returncode != 255, None
    try:
        got = json.loads(out.stdout or "[]")
    except ValueError:
        return None, True, None
    if not isinstance(got, dict):
        return got, True, None
    if flag:
        no_health.pop(host, None)
    return got.get("alerts") or [], True, got.get("health") if isinstance(got.get("health"), dict) else None


def _host(entry: dict) -> str:
    return entry.get("ssh") or entry["host"]


def _track_hosts(state: dict, reached: dict[str, bool], now: float, down_after_s: float,
                 max_gap_s: float = HOST_POLL_GAP_S, started: float | None = None) -> None:
    """One notification once a host has failed every poll for `down_after_s`, one when it answers
    again, nothing in between. Kept in the notifier's state, so a restart neither forgets nor re-alarms.
    A gap over `max_gap_s` from the last failed poll's end to this poll's start (a sleeping laptop)
    starts the streak anew; the poll's own time (slow ssh to many hosts) is no gap."""
    started = now if started is None else started
    hosts = state.setdefault(HOSTS, {})
    if reached:   # an empty registry read must not drop an alarmed host (it would alarm twice)
        for host in [h for h in hosts if h not in reached]:   # no project on it any more
            del hosts[host]
    for host, ok in sorted(reached.items()):
        h = hosts.setdefault(host, {})
        if ok:
            if h.get("alarmed"):
                show(f"tt-project · {host}", f"{host} answers again; its projects can page again")
            hosts[host] = {"ok": now}
            continue
        if not h.get("alarmed") and started - h.get("last_fail", started) > max_gap_s:
            h.pop("failing_since", None)
        since = h.setdefault("failing_since", now)
        h["last_fail"] = now
        if not h.get("alarmed") and now - since >= down_after_s:
            zone = timefmt.ambient()
            show(f"tt-project · {host}",
                 f"{host} unreachable since {timefmt.short(since, zone, now)}; its projects cannot page")
            h["alarmed"] = True


def _track_daemons(state: dict, health: dict[str, dict | None], hosts_of: dict[str, str | None], now: float,
                   down_after_s: float, host_alarmed: set[str] = frozenset(), names: set[str] | None = None) -> None:
    """One notification once a project's daemon has not beaten for `down_after_s` while a service should
    keep it running, one when it beats again, nothing in between; kept in the state across restarts.
    `health` holds this poll's answers (None: unknown, nothing changes); `hosts_of` names each remote
    project's host (None: this machine); `host_alarmed` the hosts alarmed as unreachable before this
    poll: their alarm (or recovery notice) already speaks for the projects on them."""
    daemons = state.setdefault(DAEMONS, {})
    if names:   # an empty registry read must not drop an alarmed project (it would alarm twice)
        for name in [n for n in daemons if n not in names]:
            del daemons[name]
    zone = None
    for name, h in sorted(health.items()):
        if not isinstance(h, dict):
            continue
        host = hosts_of.get(name)
        age = h.get("heartbeat_age_s")
        down = bool(h.get("service")) and isinstance(age, (int, float)) and age >= down_after_s
        was = daemons.get(name)
        if not down:
            if was:
                del daemons[name]
                # a stopped service is no recovery; nor is one the host's recovery notice just covered
                if h.get("service") and host not in host_alarmed:
                    show(f"tt-project · {name}", f"{name} daemon runs again; it can page again")
            continue
        if was:
            continue
        if host in host_alarmed and state.get(HOSTS, {}).get(host, {}).get("alarmed"):
            continue
        zone = zone or timefmt.ambient()
        since = now - age
        where = f"its host {host} answers. `ttp restart {name}` on that host." if host else \
            f"`ttp restart {name}` on this machine."
        show(f"tt-project · {name}", f"{name} daemon down since {timefmt.short(since, zone, now)} "
             f"(no heartbeat for {age / 3600:.1f} h); {where}")
        daemons[name] = {"alarmed": now, "since": since}


def _conf_on(key: str) -> bool:
    """A notifier.json switch that is on unless set to false."""
    try:
        return json.loads(CONF.read_text()).get(key, True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def persistent() -> bool:
    """Alerts stay on screen until closed: the default; notifier.json `"persistent": false` turns it off."""
    return _conf_on("persistent")


def app_wanted() -> bool:
    """macOS: build and send through the tt-project app; notifier.json `"app": false` turns it off (the
    notifier then keeps terminal-notifier or osascript). Setup and upgrade never write notifier.json."""
    return _conf_on("app")


def app_path() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


# The app: an AppleScript applet (osacompile ships with macOS, no compiler or paid signing identity),
# so `display notification` is posted by the app itself and shows under its own name, icon and
# Notifications entry, not Script Editor's. Started with --ttp-notify it posts one; started any other
# way (a click on one of its alerts) it opens the last alert's web app link.
APP_SCRIPT = """use AppleScript version "2.4"
use framework "Foundation"
use scripting additions

on run
	set argv to (current application's NSProcessInfo's processInfo()'s arguments()) as list
	set i to 0
	repeat with k from 1 to count of argv
		if item k of argv is "--ttp-notify" then set i to k
	end repeat
	if i > 0 and (count of argv) >= i + 3 then
		display notification (item (i + 2) of argv) with title (item (i + 1) of argv) sound name (item (i + 3) of argv)
		delay 1
	else
		try
			set u to do shell script "cat " & quoted form of "@CLICK_URL@"
			if u starts with "http" then open location u
		end try
	end if
end run
"""


def app_info(keep: bool | None = None) -> dict:
    """The Info.plist keys ensure_app sets on the applet osacompile makes."""
    keep = persistent() if keep is None else keep
    return {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
            # the default style macOS gives the app when it first posts: alert = stays until closed
            "NSUserNotificationAlertStyle": "alert" if keep else "banner",
            "LSBackgroundOnly": True,   # no Dock icon; LSUIElement stops an applet's notifications
            "CFBundleIconFile": "applet"}


def _app_rev(info: dict, script: str) -> str:
    import hashlib
    return hashlib.sha256((script + json.dumps(info, sort_keys=True)).encode()).hexdigest()[:12]


def _icon_png(n: int = 256) -> bytes:
    """A plain app icon (a white T on a rounded purple square) as PNG, made here: no image files shipped."""
    import struct
    import zlib
    r, bar, stem = n * 0.18, (n * 0.22, n * 0.78, n * 0.22, n * 0.34), (n * 0.43, n * 0.57, n * 0.34, n * 0.80)
    rows = []
    for y in range(n):
        row = bytearray(b"\0")
        for x in range(n):
            dx, dy = max(r - x, x - (n - 1 - r), 0), max(r - y, y - (n - 1 - r), 0)
            if dx * dx + dy * dy > r * r:
                row += b"\0\0\0\0"
            elif bar[0] <= x < bar[1] and bar[2] <= y < bar[3] or stem[0] <= x < stem[1] and stem[2] <= y < stem[3]:
                row += b"\xff\xff\xff\xff"
            else:
                row += b"\x5b\x2b\x9e\xff"
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def _icns() -> bytes:
    import struct
    png = _icon_png(256)
    entry = b"ic08" + struct.pack(">I", 8 + len(png)) + png   # ic08: 256x256 PNG
    return b"icns" + struct.pack(">I", 8 + len(entry)) + entry


def app_ready() -> Path | None:
    """The notifier app when it is installed and is ours (show sends through it), else None."""
    import plistlib
    app = app_path()
    try:
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return app if info.get("CFBundleIdentifier") == BUNDLE_ID else None


def ensure_app() -> tuple[Path | None, str]:
    """macOS: build (or rebuild, when its script or keys changed) the notifier app under ~/Applications,
    ad-hoc signed (`codesign -s -`: no paid identity), with no root. Idempotent: an app of the same
    revision is left alone. (the app or None, a line to print: empty when nothing changed). A failure
    keeps the older app, if any, and the notifier falls back to terminal-notifier or osascript."""
    import plistlib
    import tempfile
    if sys.platform != "darwin" or not app_wanted():
        return None, ""
    script = APP_SCRIPT.replace("@CLICK_URL@", str(CLICK_URL))
    info = app_info()
    rev = _app_rev(info, script)
    app = app_path()
    try:
        if plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("TTProjectRev") == rev:
            return app, ""
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    missing = [t for t in ("osacompile", "codesign") if not shutil.which(t)]
    if missing:
        return app_ready(), f"notifier app not built: {' and '.join(missing)} not found"
    app.parent.mkdir(parents=True, exist_ok=True)
    # osacompile picks the output type from the name: only one ending in .app gives an applet
    new, old = app.with_name(f".new-{app.name}"), app.with_name(f".old-{app.name}")
    for d in (new, old, app.with_name(f".{app.name}.new"), app.with_name(f".{app.name}.old")):   # + older builds'
        _remove(d)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "notifier.applescript"
            src.write_text(script, encoding="utf-8")
            _must(["osacompile", "-o", str(new), str(src)])
        plist = new / "Contents" / "Info.plist"
        keys = plistlib.loads(plist.read_bytes())
        keys.update(info, TTProjectRev=rev)
        plist.write_bytes(plistlib.dumps(keys))
        (new / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
        (new / "Contents" / "Resources" / "applet.icns").write_bytes(_icns())
        _must(["codesign", "--force", "--deep", "--sign", "-", str(new)])   # the edited plist voids osacompile's
        if app.exists():
            app.rename(old)
        new.rename(app)
        shutil.rmtree(old, ignore_errors=True)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        _remove(new)
        if old.exists() and not app.exists():
            old.rename(app)
        log(f"notifier app not built: {e}")
        return app_ready(), f"notifier app not built: {e}"
    lsreg = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
             "/Support/lsregister")
    if os.path.exists(lsreg):   # Launch Services reads the new Info.plist now, not at its next scan
        subprocess.run([lsreg, "-f", str(app)], capture_output=True, timeout=60)
    return app, f"notifier app {app} ready ({'persistent' if persistent() else 'banner'} alerts requested)"


def _remove(p: Path) -> None:
    """A build leftover, a folder or a file (osacompile makes a script file from a name not ending in .app)."""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def _must(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")


def _ncprefs() -> bytes:
    """macOS's Notifications settings, read only: the plist file, else (current macOS has no such file)
    what `defaults export` gives for the domain. b"" when neither answers."""
    try:
        return (Path.home() / "Library" / "Preferences" / "com.apple.ncprefs.plist").read_bytes()
    except OSError:
        pass
    if not shutil.which("defaults"):
        return b""
    try:
        r = subprocess.run(["defaults", "export", "com.apple.ncprefs", "-"], capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return r.stdout if r.returncode == 0 else b""


def alert_style() -> str:
    """How macOS shows the app's notifications, read (never written) from its Notifications settings:
    persistent, banners, none, 'not registered' (they list apps, not this one: it has not posted yet) or
    'unknown' (unreadable, or no apps listed: current macOS may export an empty dict)."""
    import plistlib
    try:
        prefs = plistlib.loads(_ncprefs())
    except (ValueError, plistlib.InvalidFileException):
        return "unknown"
    apps = prefs.get("apps") if isinstance(prefs, dict) else None
    if not isinstance(apps, list) or not apps:
        return "unknown"
    for a in apps:
        if isinstance(a, dict) and a.get("bundle-id") == BUNDLE_ID:
            try:
                return STYLES.get(int(a.get("flags", 0)) & 0x38, "unknown")
            except (TypeError, ValueError):
                return "unknown"
    return "not registered"


def app_registered() -> bool:
    """macOS shows the app allowed: only then do alerts go through it (_command). Its Notifications
    settings decide when they list apps; where they say nothing, usernoted's log (app_allowed). A failed,
    timed-out or empty read of both keeps the sender."""
    style = alert_style()
    return style in STYLES.values() or style == "unknown" and app_allowed()


def _usernoted(bundle: str, since: float) -> list[str] | None:
    """usernoted's unified-log lines naming `bundle` since `since`, read only; None when unreadable."""
    if not shutil.which("log"):
        return None
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    try:
        r = subprocess.run(["log", "show", "--style", "compact", "--start", start, "--predicate",
                            f'process == "usernoted" AND eventMessage CONTAINS "{bundle}"'],
                           capture_output=True, text=True, errors="replace", timeout=LOG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    recs: list[str] = []   # one per log record: a message spanning lines joins its first line
    for ln in r.stdout.splitlines():
        if recs and not LOG_RECORD.match(ln) and LOG_RECORD.match(recs[-1]):
            recs[-1] += " " + ln.strip()
        else:
            recs.append(ln)
    return [rec for rec in recs if bundle in rec]


def _log_verdict(lines: list[str]) -> str:
    """'denied' (a denial, or a record not delivered or not presented: Focus, display state, turned off),
    'allowed' (only a record that was delivered or presented, not negated) or 'none' (nothing decisive)."""
    said = [m for ln in lines for m in [SHOWN.match(ln.strip())] if m]
    if any(w in ln.lower() for ln in lines for w in DENIED) or any(m.group(1) for m in said):
        return "denied"
    return "allowed" if said else "none"


def app_allowed(force: bool = False) -> bool:
    """Where macOS's settings list no apps: whether usernoted's log shows the app's notification delivered
    or presented, with no denial or negated record, since its first post (register_app). Remembered once
    seen; until then re-read at most every LOG_CHECK_S (`force`: now, polling every LOG_POLL_S until a
    decisive record or LOG_SETTLE_S after the post), only for LOG_WINDOW_S after that post. A read before
    LOG_SETTLE_S sets no back-off. An unreadable log, a denial or nothing: False, the sender stays."""
    d, now = delivery(), time.time()
    if d.get("app_allowed"):
        return True
    posted = d.get("app_posted")
    if not isinstance(posted, (int, float)) or now - posted > LOG_WINDOW_S or \
            not force and now - float(d.get("app_log_checked") or 0) < LOG_CHECK_S:
        return False
    while True:
        lines = _usernoted(BUNDLE_ID, posted - 5)
        verdict = "unreadable" if lines is None else _log_verdict(lines)
        early = now < posted + LOG_SETTLE_S   # usernoted may not have logged the post yet
        if not force or not early or verdict in ("allowed", "denied"):
            break
        time.sleep(max(0.0, min(LOG_POLL_S, posted + LOG_SETTLE_S - time.time())))
        now = time.time()
    d = dict(delivery(), app_log=verdict)
    if not early or verdict in ("allowed", "denied"):
        d["app_log_checked"] = now
    if verdict == "allowed":
        d["app_allowed"] = now
    try:
        write_json(DELIVERY, d)
    except OSError as e:
        log(f"could not record the app's log check: {e}")
    return verdict == "allowed"


def muted_lines(backend: str, since: float) -> list[str]:
    """macOS: why a test notification sent since `since` was held back, from usernoted's log (read only)."""
    bundle = SENDER_IDS.get(backend)
    lines = [ln.lower() for ln in (_usernoted(bundle, since - 2) or [])] if sys.platform == "darwin" and bundle else []
    if any("muted by display state" in ln for ln in lines):
        return ["macOS muted the test notification while the screen is shared or mirrored (display state); "
                "alerts show again once that ends"]
    if any("focus" in ln or "do not disturb" in ln for ln in lines):
        return ["macOS held the test notification back for a Focus mode: turn it off or allow tt-project's sender there"]
    return []


def _app_args(app: Path, title: str, body: str) -> list[str]:
    # -n: a new instance per alert, so one still posting never swallows the next; -g: no focus
    return ["open", "-g", "-n", "-a", str(app), "--args", "--ttp-notify", title, body, "Glass"]


def app_pending() -> bool:
    """macOS: the app is built and wanted but macOS does not list it yet (alerts use the old sender)."""
    return sys.platform == "darwin" and app_wanted() and app_ready() is not None and not app_registered()


def register_app() -> list[str]:
    """macOS: while the app is built but not yet listed in Notifications, post one notification through
    it, so macOS lists it and asks once to allow it. Alerts keep their sender until it is listed."""
    app = app_ready() if sys.platform == "darwin" and app_wanted() and shutil.which("open") else None
    if not app or app_registered():
        return []
    r_ts = time.time()
    try:
        r = subprocess.run(_app_args(app, APP_NAME, "tt-project alerts come from this app once you allow it."),
                           capture_output=True, text=True, errors="replace", timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"the {APP_NAME} app could not post its first notification: {e}"]
    if r.returncode != 0:
        return [f"the {APP_NAME} app could not post its first notification: exited {r.returncode}"]
    d = delivery()
    d.pop("app_allowed", None)
    d.update(app_posted=r_ts, app_log_checked=0)
    try:   # where macOS's settings say nothing, the log shows from here whether it was allowed (app_allowed)
        write_json(DELIVERY, d)
    except OSError as e:
        log(f"could not record the app's first notification: {e}")
    return [f"the {APP_NAME} app posted its first notification: allow it if macOS asks"]


def style_lines(backend: str) -> list[str]:
    """After a macOS test notification: the alert style when macOS shows it, and the one fix line only
    when alerts would still go away by themselves (banners, none, or a fallback sender)."""
    if not persistent() or sys.platform != "darwin":
        return []
    if backend != APP_NAME:
        entry = "Script Editor" if backend == "osascript" else backend
        if app_pending():   # built, but macOS's settings do not show it listed (or cannot be read)
            return [f"alerts still come from {entry} and may go away by themselves; to keep them on screen: "
                    f"System Settings > Notifications > {APP_NAME} > Allow notifications on, alert style Persistent "
                    "(older macOS: Alerts). macOS lets no program set another app's alert style"]
        return [f"alerts come from {entry} and may go away by themselves: System Settings > Notifications > "
                f"{entry} > alert style Persistent (older macOS: Alerts)"]
    style = alert_style()
    if style == "persistent":
        return ["alert style: persistent (stays until closed)"]
    if style in ("banners", "none"):
        return [f"alert style: {style}; to keep alerts until closed: System Settings > Notifications > "
                f"{APP_NAME} > Allow notifications on, alert style Persistent (older macOS: Alerts)"]
    if style == "unknown":   # allowed per usernoted's log; the style itself is not readable here
        return [f"alert style: macOS does not say here (the app asks for Persistent); if its alerts go away by "
                f"themselves: System Settings > Notifications > {APP_NAME} > alert style Persistent (older macOS: Alerts)"]
    return [f"alert style: {style} (the app asks macOS for persistent alerts)"]


def _command(title: str, body: str, url: str | None, severity: str = "high") -> tuple[str, list[str] | None, str]:
    """(the backend, its argv or None, why there is none) for this machine. Every one plays a sound."""
    if sys.platform == "darwin":
        # the app only once macOS lists it (app_registered): until then, or opted out, the sender stays
        app = app_ready() if shutil.which("open") and app_wanted() else None
        if app and app_registered():
            return APP_NAME, _app_args(app, title, body), ""
        tn = shutil.which("terminal-notifier")
        if tn:
            # no -group: a shared group would let a later alert replace an earlier unread one
            args = [tn, "-title", title, "-message", body, "-sound", "default"]
            return "terminal-notifier", args + (["-open", url] if url else []), ""
        if not shutil.which("osascript"):
            return "osascript", None, "osascript not found on PATH"
        # text goes as argv, never into the script source: non-ASCII or quotes cannot break it
        return "osascript", ["osascript", "-e", "on run argv",
                             "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                             "-e", "end run", body, title], ""
    if sys.platform.startswith("linux"):
        if not shutil.which("notify-send"):
            return "notify-send", None, "notify-send is not installed"
        if not any(os.environ.get(v) for v in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")):
            return "notify-send", None, "no desktop session (DISPLAY, WAYLAND_DISPLAY and DBUS_SESSION_BUS_ADDRESS unset)"
        keep = persistent()   # critical with no expiry: it stays until dismissed
        urgency = "critical" if keep or severity == "critical" else "normal"
        return "notify-send", ["notify-send", "-a", "tt-project", "-u", urgency] + (["-t", "0"] if keep else []) + [
            "-h", "string:sound-name:message-new-instant", title, body], ""
    return sys.platform, None, f"no desktop notification backend on {sys.platform}"


PREVIEW = 240   # chars of a message a desktop notification shows
MORE = " (full text in the chat and web)"


def preview(body: str) -> str:
    """`body` on one line, cut to PREVIEW with a pointer to the full text when it is longer."""
    full = " ".join(str(body or "").split())
    return full if len(full) <= PREVIEW else headline.clip(full, PREVIEW - len(MORE)) + MORE


def show(title: str, body: str, url: str | None = None, severity: str = "high") -> dict:
    """Show one notification. Returns and records (DELIVERY) {backend, ok, confirmed, error, ts}; a
    failure (non-zero exit, timeout, no backend) also goes to the notifier log. `confirmed`: the backend
    says it was shown; on macOS exit 0 only means it was handed over, a blocked app shows nothing."""
    body = preview(body)
    backend, args, why = _command(title, body, url, severity)
    res = {"backend": backend, "ok": False, "confirmed": False, "error": why, "ts": time.time()}
    if args:
        try:
            r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            res["error"] = f"timed out after {TIMEOUT_S}s"
        except OSError as e:
            res["error"] = f"could not run: {e}"
        else:
            if r.returncode == 0:
                res.update(ok=True, confirmed=backend == "notify-send", error="")
                if url and backend == APP_NAME:
                    try:   # what a click on it opens (the app reads it when started without --ttp-notify)
                        durable_write(CLICK_URL, url.encode())
                    except OSError as e:
                        log(f"could not keep the click link: {e}")
            else:
                res["error"] = f"exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    record(res)
    return res


def log(line: str) -> None:
    """One timestamped line in the notifier log (launchd also sends the agent's stderr there)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')} notifier: {line}\n")
    except OSError:
        print(f"notifier: {line}", file=sys.stderr, flush=True)


def delivery() -> dict:
    try:
        d = json.loads(DELIVERY.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def record(res: dict) -> None:
    """Keep the last delivery's outcome; a failure is logged and keeps when the failing began."""
    d = delivery()
    if not res["ok"]:
        log(f"{res['backend']} delivery failed: {res['error']}")
        since = d.get("failing_since") if d.get("ok") is False else None
        res = dict(res, failing_since=since or res["ts"])
    d.update(res)
    if res["ok"]:
        d.pop("failing_since", None)
    try:
        write_json(DELIVERY, d)
    except OSError as e:
        log(f"could not record the delivery: {e}")


def failing() -> dict | None:
    """The last delivery when it failed while this machine's agent is installed and enabled: the
    condition of the ALERT_KEY alert (alerts.holds). A failed `ttp notifier test` on a box with no agent
    (headless, no notify-send) alerts nothing; disabling or removing the agent clears the alert."""
    d = delivery()
    return d if d.get("ok") is False and enabled(max_age_s=ENABLED_CACHE_S) else None


def fix_steps(res: dict) -> list[str]:
    """Short, exact steps that make desktop notifications show on this machine."""
    b, err = res.get("backend"), res.get("error") or ""
    again = "Then run `ttp notifier test` again."
    other = ("  Or stop using the tt-project app: set \"app\": false in ~/.tt-project/notifier.json; alerts then "
             "come from terminal-notifier (`brew install terminal-notifier`) or else osascript (Script Editor).")
    pending = [f"The {APP_NAME} app is not in System Settings > Notifications yet, so alerts come from {b} until "
               "it is: allow its first notification, then keep alerts on screen: System Settings > Notifications > "
               f"{APP_NAME} > Allow notifications on, alert style Persistent (older macOS: Alerts). "
               "macOS lets no program set another app's alert style.", other] if b in ("osascript", "terminal-notifier") and \
        app_pending() else []
    if b == "osascript":
        return pending + ["macOS shows these as from Script Editor. If you saw nothing:",
                "  1. System Settings > Notifications > Script Editor: turn on Allow notifications (Banners or Alerts).",
                "  2. Script Editor not listed: open Script Editor, run `display notification \"test\"` once, then do step 1.",
                "  3. Turn off Focus (Do Not Disturb) or allow it there.",
                "  Or `brew install terminal-notifier`: the notifier then uses it (its own entry in Notifications).",
                "  " + again]
    if b == APP_NAME:
        return ["If you saw nothing:",
                f"  1. System Settings > Notifications > {APP_NAME}: turn on Allow notifications "
                "(alert style Persistent keeps them until closed).",
                "  2. Turn off Focus (Do Not Disturb) or allow it there.",
                other, "  " + again]
    if b == "terminal-notifier":
        return pending + ["If you saw nothing:",
                "  1. System Settings > Notifications > terminal-notifier: turn on Allow notifications (Banners or Alerts).",
                "  2. Turn off Focus (Do Not Disturb) or allow it there.",
                "  " + again]
    if b == "notify-send" and "not installed" in err:
        return ["Install notify-send (Debian/Ubuntu: `sudo apt install libnotify-bin`; Fedora: `sudo dnf install "
                "libnotify`). " + again]
    if b == "notify-send" and "no desktop session" in err:
        return ["This machine has no desktop session. Install the notifier on the computer you sit at "
                "(`ttp notifier install` there), and remove it here: `ttp notifier uninstall`."]
    if b == "notify-send":
        return ["No notification server answered: check that a desktop session runs and Do Not Disturb is off. " + again]
    return ["This system has no desktop notifications; alerts still reach the chat and the web app."]


def test(ask=None) -> tuple[bool, list[str]]:
    """Send one test notification the way alerts go (show). (delivered, lines to print). `ask(prompt)`
    (an interactive terminal) confirms what the backend cannot: whether the user saw it."""
    built = ensure_app()[1]   # macOS: the tt-project app, built when it can be
    pre = ([built] if built else []) + register_app()
    res = show("tt-project", "Test notification: desktop alerts from tt-project reach this machine.")
    b = sb = res["backend"]   # sb: whose alert style counts from now on
    if sys.platform == "darwin" and b != APP_NAME and app_wanted() and app_ready() and alert_style() == "unknown" \
            and app_allowed(force=True):   # its first post may have been allowed since the test went out
        pre.append(f"macOS shows the {APP_NAME} app allowed: alerts come from it from now on")
        sb = APP_NAME
    if res["ok"]:
        pre += muted_lines(b, res["ts"])
    if not res["ok"]:
        return False, pre + [f"test notification failed ({b}): {res['error']}"] + fix_steps(res)
    if res["confirmed"]:
        return True, pre + [f"test notification delivered ({b})"]
    if ask is None:
        return True, pre + [f"test notification sent ({b}); macOS does not say whether it was shown"] + \
            ([] if sb != APP_NAME and app_pending() else style_lines(sb)) + fix_steps(res)
    if ask("Did a tt-project notification appear? [y/N] "):
        record(dict(res, confirmed=True, ts=time.time()))
        return True, pre + [f"test notification delivered ({b}), seen by you"] + style_lines(sb)
    res = dict(res, ok=False, confirmed=False, error="sent, but you did not see it", ts=time.time())
    record(res)
    return False, pre + [f"test notification not shown ({b}): macOS took it but did not show it"] + fix_steps(res)


def code(runtime: Path) -> str:
    """'<version> (<commit>)' of a runtime folder."""
    from .release import runtime_commit, runtime_version
    return f"{runtime_version(runtime)} ({runtime_commit(runtime)})"


def own_runtime() -> Path:
    return Path(__file__).parent.parent


def installed_code() -> str:
    from .release import installed
    rt = installed() / "runtime"
    return code(rt if rt.is_dir() else own_runtime())


def agent_file() -> Path | None:
    """This user's installed notifier agent (launchd plist or systemd user unit), None without one (or
    in tests that never touch the account's own agent: TTP_TEST_NO_NOTIFIER)."""
    if os.environ.get("TTP_TEST_NO_NOTIFIER"):
        return None
    f = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist" if sys.platform == "darwin" else \
        hoststate.unit_dir() / UNIT   # a test run's fake under its marker
    return f if f.exists() else None


_enabled_cache: dict = {}   # {"at": time, "on": bool}: failing() runs every daemon tick


def _service_on() -> bool:
    """launchd has the agent loaded, or systemd has the unit enabled: the user keeps it on."""
    if sys.platform == "darwin":
        cmd = ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]
    else:
        cmd = hoststate.argv(["systemctl", "--user", "is-enabled", UNIT])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and (sys.platform == "darwin" or r.stdout.strip() == "enabled")


def enabled(max_age_s: float = 0) -> bool:
    """This user's agent is installed and enabled (not disabled or booted out by hand). `max_age_s`:
    reuse an answer that recent."""
    if not agent_file():
        return False
    now = time.time()
    if max_age_s and now - _enabled_cache.get("at", 0) < max_age_s:
        return _enabled_cache["on"]
    on = _service_on()
    _enabled_cache.update(at=now, on=on)
    return on


def uninstall() -> str:
    """Stop and remove this user's agent and forget its delivery and start records, so no alert, status
    line or `ttp setup`/`ttp upgrade` brings it back; `ttp notifier install` does."""
    def quiet(*cmd):
        try:
            subprocess.run(hoststate.argv(cmd), capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    f = agent_file()
    if f and sys.platform == "darwin":
        quiet("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
    elif f:
        quiet("systemctl", "--user", "disable", "--now", UNIT)
    for g in (f, DELIVERY, RUNNING):
        if g:
            g.unlink(missing_ok=True)
    if f and sys.platform != "darwin":
        quiet("systemctl", "--user", "daemon-reload")
    _enabled_cache.clear()
    return "removed" if f else "no desktop notifier installed here"


def running() -> dict | None:
    """The agent's start record (RUNNING) while its process lives; None when it is not running or
    started before agents wrote one (an unknown code version)."""
    try:
        rec = json.loads(RUNNING.read_text())
        os.kill(int(rec["pid"]), 0)
    except PermissionError:
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def status_lines() -> list[str]:
    """What `ttp status` says about this machine's notifier: nothing while it runs the installed code
    and its last delivery went out; nothing without an enabled agent (none, or disabled by hand)."""
    if not enabled():
        return []
    out = []
    run, want = running(), installed_code()
    if not run:
        out.append("desktop notifier: running code unknown (not running, or started by an older ttp); "
                   "`ttp notifier restart`")
    elif run.get("code") != want:
        out.append(f"desktop notifier runs ttp {run.get('code')}, not the installed {want}: `ttp notifier restart`")
    bad = failing()
    if bad:
        out.append(f"desktop notifier: last delivery failed ({bad.get('backend')}: {bad.get('error')}); "
                   "`ttp notifier test` shows the fix")
    return out


def refresh(send_test: bool, always: bool = False) -> list[str]:
    """`ttp setup`, `ttp upgrade`: restart an installed agent so it runs the installed code (`always`, or
    while it runs other code), then (`send_test`) send the test notification once per installed code
    version: none once one went out on it. On macOS it first builds or refreshes the notifier app
    (ensure_app); a new app gets its test too, which brings up macOS's one Allow prompt. Lines to print;
    none without an installed agent, and none for one the user disabled or booted out: install() would
    turn it back on."""
    if not enabled():
        return []
    want = installed_code()
    run = running()
    built = ensure_app()[1]
    out = ["desktop notifier: " + built] if built else []
    if always or not run or run.get("code") != want or _background(agent_file()):
        out.append(f"desktop notifier: {install()} (runs ttp {want})")
    if send_test:
        if delivery().get("tested") == want and not built:
            out.append(f"desktop notifier: a test notification already went out on ttp {want}; "
                       "`ttp notifier test` sends another")
        else:
            out += ["desktop notifier: " + ln if i == 0 else ln for i, ln in enumerate(test_once(want)[1])]
    return out


def _background(f: Path | None) -> bool:
    """An agent an older tt-project installed with launchd's ProcessType Background: install() rewrites it."""
    if not f or f.suffix != ".plist":
        return False
    import plistlib
    try:
        return plistlib.loads(f.read_bytes()).get("ProcessType") == "Background"
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
        return False


def test_once(want: str | None = None, ask=None) -> tuple[bool, list[str]]:
    """The test notification, remembered per installed code version once it went out. Its outcome goes
    to the notifier log too, so a later look at the log shows what the self-check found."""
    ok, lines = test(ask)
    log("self-check: " + " | ".join(ln.strip() for ln in lines))
    if ok:
        d = delivery()
        d["tested"] = want or installed_code()
        try:
            write_json(DELIVERY, d)
        except OSError:
            pass
    return ok, lines


def run_once(state: dict, floor: str, down_after_s: float = HOST_DOWN_AFTER_S, now: float | None = None,
             max_gap_s: float = HOST_POLL_GAP_S, daemon_down_after_s: float = DAEMON_DOWN_AFTER_S) -> dict:
    started = time.time() if now is None else now
    reached: dict[str, bool] = {}
    healths: dict[str, dict | None] = {}
    hosts_of: dict[str, str | None] = {}
    projects = load_registry().get("projects", {})
    for name, entry in sorted(projects.items()):
        after = int(state.get(name, -1))
        local = entry.get("host") in (None, hostname())
        if local:
            p = Project(entry["dir"])
            if not (p.state / "project.db").exists():
                continue
            hosts_of[name] = None
            try:
                healths[name] = health(p)
            except Exception:   # unknown health changes nothing; its alerts still go out
                healths[name] = None
            if after < 0:   # first sighting: start from now, do not replay history
                db = DB(p.state / "project.db")
                state[name] = int(db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"])
                db.close()
                continue
            rows = alerts_since(p, after, floor)
        else:
            rows, ok, healths[name] = _remote_alerts(entry, name, max(after, 0), floor, state, started)
            hosts_of[name] = _host(entry)
            reached[_host(entry)] = reached.get(_host(entry), False) or ok
            if rows is None:
                continue
            if after < 0:
                state[name] = max([r["id"] for r in rows] + [0])
                continue
        for r in rows:
            if not r.get("cleared"):
                show(f"tt-project · {name}", r["text"], severity=r.get("severity") or "high")
            state[name] = max(int(state.get(name, 0)), int(r["id"]))
    if reached:   # hosts no project lives on any more need no --health memory
        no_health = state.get(NO_HEALTH, {})
        for host in [h for h in no_health if h not in reached]:
            del no_health[host]
    host_alarmed = {h for h, v in state.get(HOSTS, {}).items() if v.get("alarmed")}
    end = time.time() if now is None else now
    _track_hosts(state, reached, end, down_after_s, max_gap_s, started)
    _track_daemons(state, healths, hosts_of, end, daemon_down_after_s, host_alarmed, set(projects))
    return state


def main() -> int:
    from . import __version__
    from .daemon import HEARTBEAT_STALE_S
    conf = {"min_severity": "high", "interval_s": 60, "host_down_after_s": HOST_DOWN_AFTER_S,
            "daemon_down_after_s": DAEMON_DOWN_AFTER_S}
    try:
        conf.update(json.loads(CONF.read_text()))
    except (OSError, ValueError):
        pass
    daemon_down_after_s = max(2 * HEARTBEAT_STALE_S, float(conf["daemon_down_after_s"]))
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    mine = code(own_runtime())
    py = ".".join(map(str, sys.version_info[:3]))
    write_json(RUNNING, {"pid": os.getpid(), "code": mine, "version": __version__, "python": py,
                         "runtime": str(own_runtime()), "started": time.time()})
    log(f"started: ttp {mine} under Python {py} from {own_runtime()}")
    while True:
        try:
            state = run_once(state, conf["min_severity"], float(conf["host_down_after_s"]),
                             max_gap_s=max(HOST_POLL_GAP_S, 3 * int(conf["interval_s"])),
                             daemon_down_after_s=daemon_down_after_s)
            write_json(STATE, state)
        except Exception as e:  # keep notifying other projects even if one fails
            print(f"notifier: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if outdated(mine):
            log(f"installed code is now ttp {installed_code()}; exiting so the service restarts on it")
            return 0
        time.sleep(max(15, int(conf["interval_s"])))


def outdated(mine: str) -> bool:
    """This agent runs from the installed runtime's path (lib/current) and that now holds other code: an
    exit lets launchd or systemd start it again on the new code."""
    from .release import installed
    return own_runtime() == installed() / "runtime" and installed_code() != mine


def install() -> str:
    """Background agent for this user on this machine (launchd or systemd user unit), (re)started on the
    installed runtime's stable path, so a later `ttp setup` or upgrade only needs a restart."""
    from .release import installed
    _enabled_cache.clear()   # failing() asks launchd or systemd again
    runtime = installed() / "runtime"
    if not (runtime / "ttp" / "notifier.py").is_file():   # no `ttp setup` yet: this copy
        runtime = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "ttp.notifier"]
    from .providers.base import service_path
    env = {"PYTHONPATH": str(runtime), "PATH": service_path(), "HOME": str(Path.home())}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        durable_write(plist, plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "WorkingDirectory": str(HOME_DIR),
            "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
            "ProcessType": "Standard", "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG)}))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
        return "installed and started (launchd)" if r.returncode == 0 else f"launchd failed: {r.stderr.strip()[:200]}"
    unit_dir = hoststate.unit_dir()   # a test run's fake under its marker
    unit_dir.mkdir(parents=True, exist_ok=True)
    envs = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    durable_write(
        unit_dir / UNIT,
        f"[Unit]\nDescription=tt-project desktop notifier\n\n[Service]\nWorkingDirectory={HOME_DIR}\n"
        f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n{envs}\nRestart=always\nRestartSec=15\n\n"
        f"[Install]\nWantedBy=default.target\n")
    subprocess.run(hoststate.argv(["systemctl", "--user", "daemon-reload"]), capture_output=True)
    for step in (["enable", UNIT], ["restart", UNIT]):   # restart: `enable --now` leaves a running one on old code
        r = subprocess.run(hoststate.argv(["systemctl", "--user", *step]), capture_output=True, text=True)
        if r.returncode != 0:
            return f"systemd failed: {r.stderr.strip()[:200]}"
    return "installed and started (systemd user unit)"


if __name__ == "__main__":
    sys.exit(main())
