# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's home time zone, and times shown to the user in it.

Projects often run on a box set to UTC while the user works from a workstation in another zone. Each
project keeps the IANA zone of the user's workstation as `home_timezone` in its project.json, with the
machine that last set it as `home_timezone_from`:

- `ttp new` records the zone of the machine it runs on; created on another machine from a
  workstation, it records the workstation's zone (sent as `--home-tz`), never the box's. From an ssh
  login (a server's zone) it sends none, and the project gets the `migrate` zone with no
  `home_timezone_from`, so no machine's spend push moves it before the workstation's next connect.
- `ttp connect` from a workstation sends its current zone (`--home-tz`), and so does a local
  `ttp connect` typed on the project's own machine; neither does from an ssh login (a server's zone). A machine's spend push
  (globalcap.push) carries its zone too, and moves only the projects that machine set last. A
  change is logged once in the feed (old -> new), so the zone follows a travelling user.
- A project without one (made before this existed) gets, once, the account's `budget.timezone` if
  set, else this machine's zone (`migrate`, at daemon start). Its workstation's next connect corrects it.

`budget.timezone` defaults to the home zone; an explicit setting (account or project) wins.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEY = "home_timezone"
FROM_KEY = "home_timezone_from"
LOCALTIME = Path("/etc/localtime")
ETC_TIMEZONE = Path("/etc/timezone")


def valid(name) -> str | None:
    """`name` as an IANA zone name ('America/Los_Angeles'), or None when it is not one. A path into
    a zoneinfo folder (TZ=':/usr/share/zoneinfo/Europe/Paris', a /etc/localtime target) gives its name."""
    if not isinstance(name, str):
        return None
    name = name.strip().lstrip(":")
    if "zoneinfo/" in name:
        name = name.rsplit("zoneinfo/", 1)[1]
        for pre in ("posix/", "right/"):
            name = name.removeprefix(pre)
    if not name or name.startswith("/") or ".." in name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def detect_local(env: dict | None = None, localtime: Path | None = None, etc_timezone: Path | None = None,
                 timedatectl: bool = True) -> str:
    """This machine's IANA zone: TZ when it names one, else the /etc/localtime link's target (macOS and
    Linux), else /etc/timezone or timedatectl; UTC when none of them says."""
    env = os.environ if env is None else env
    forced = valid(env.get("TTP_TEST_LOCAL_TZ"))   # tests: never the zone of the machine running them
    if forced:
        return forced
    tz = valid(env.get("TZ"))
    if tz:
        return tz
    try:
        tz = valid(os.readlink(localtime or LOCALTIME))
        if tz:
            return tz
    except OSError:
        pass
    try:
        tz = valid((etc_timezone or ETC_TIMEZONE).read_text().splitlines()[0])
        if tz:
            return tz
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    if timedatectl:
        try:
            r = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True,
                               text=True, timeout=3)
            tz = valid(r.stdout) if r.returncode == 0 else None
            if tz:
                return tz
        except (OSError, subprocess.SubprocessError):
            pass
    return "UTC"


def in_ssh_login(env: dict | None = None) -> bool:
    """This process runs under an ssh login: the machine's own zone is a server's, not the user's."""
    env = os.environ if env is None else env
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"))


# the zone -----------------------------------------------------------------------------------------
def zone_name(cfg: dict | None) -> str:
    """The home zone of a project's settings (raw or layered project.json); UTC when it has none."""
    return valid((cfg or {}).get(KEY)) or "UTC"


def home(p) -> str:
    """A Project's home zone name; UTC when it has none."""
    return zone_name(p.raw_config())


def zone(p_or_cfg) -> ZoneInfo:
    """The home zone of a Project or its settings, as a tzinfo."""
    cfg = p_or_cfg if isinstance(p_or_cfg, dict) else p_or_cfg.raw_config()
    return ZoneInfo(zone_name(cfg))


def _tz(where) -> ZoneInfo:
    if where is None:
        return ZoneInfo("UTC")
    if isinstance(where, ZoneInfo):
        return where
    if isinstance(where, str):
        return ZoneInfo(valid(where) or "UTC")
    return zone(where)


def tzinfo(where) -> ZoneInfo:
    """The zone of `where` (a zone name, ZoneInfo, Project or settings; None is UTC), as a tzinfo."""
    return _tz(where)


# formatting ---------------------------------------------------------------------------------------
def long(ts: float, where) -> str:
    """'2026-10-09 23:32 PDT': `ts` in the zone of `where` (a zone name, ZoneInfo, Project or settings)."""
    return datetime.fromtimestamp(ts, _tz(where)).strftime("%Y-%m-%d %H:%M %Z")


def short(ts: float, where, now: float | None = None) -> str:
    """'23:32 PDT' within 20 hours of now, else 'Fri 23:32 PDT'."""
    fmt = "%H:%M %Z" if abs(ts - (now if now is not None else time.time())) < 20 * 3600 else "%a %H:%M %Z"
    return datetime.fromtimestamp(ts, _tz(where)).strftime(fmt)


def abbrev(where, ts: float | None = None) -> str:
    """The zone's abbreviation at `ts` (now by default): 'PDT', 'PST', 'UTC'."""
    return datetime.fromtimestamp(ts if ts is not None else time.time(), _tz(where)).strftime("%Z")


# recording ----------------------------------------------------------------------------------------
def set_home(p, tz: str | None, source: str = "", why: str = "") -> bool:
    """Make `tz` the project's home zone, set from machine `source`. Logs one feed line when the zone
    changes (old -> new); returns whether it did. An invalid zone changes nothing."""
    tz = valid(tz)
    if not tz:
        return False
    raw = p.raw_config()
    if p.config_status != "ok" or not raw:
        return False                      # never write a project.json read from a fallback copy
    old = valid(raw.get(KEY))
    changed = old != tz
    if changed:
        p.set_config(KEY, tz)
    if source and raw.get(FROM_KEY) != source:
        p.set_config(FROM_KEY, source)
    if changed and old:
        p.db.post("out", f"Home time zone {old} -> {tz}" + (f" ({why})" if why else "") + ".",
                  chat=None, kind="info", severity="low")
    return changed


def follow_push(p, tz: str | None, source: str) -> bool:
    """A machine's spend push carried its zone: follow it only when that machine set the zone last
    (another machine pushing, such as a server, never moves it)."""
    raw = p.raw_config()
    if not source or raw.get(FROM_KEY) != source:
        return False
    return set_home(p, tz, source, f"from {source}")


def fallback() -> tuple[str, bool]:
    """The zone for a project no workstation sent one for: the account's budget.timezone if set,
    else this machine's. Returns (zone, whether it is the account's)."""
    from .project import load_account_settings
    acct = valid((load_account_settings().get("budget") or {}).get("timezone"))
    return acct or detect_local(), bool(acct)


def migrate(p, log=None) -> str | None:
    """A project with no home zone gets the account's budget.timezone if set, else this machine's
    zone. Returns the zone it set, or None when it had one."""
    raw = p.raw_config()
    if p.config_status != "ok" or not raw or valid(raw.get(KEY)):
        return None
    tz, acct = fallback()
    p.set_config(KEY, tz)
    if log:
        log(f"home time zone set to {tz} ({'the account budget.timezone' if acct else 'this machine'}); "
            f"the workstation's next ttp connect corrects it")
    return tz
