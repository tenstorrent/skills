# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Has a device broker's hold been released? The check behind `ttp broker-released`, for a
`retry_when` or `on_pass_cmd` that waits on a broker hold (a startup hold, a hold during a recovery).

A broker says so in more than one way, and a check that looks for only one of them misses the
others: a `device_released` event, a log line such as `STARTUP hold released after 2949s` or
`hold released`, or its /health answer turning to `held: false` with a healthy fsm. Any of them
counts. The input is any mix of /health JSON and log text (e.g. `curl -s .../health; tail -n 500
<log>` over ssh), read line by line:
- /health JSON (an object with a `held` field; the last one counts) is live, so it decides: `held`
  false with a healthy fsm (the broker's `fsm_state`; else `fsm`, a string or an object's `state`)
  is released; `held` true, or an fsm that is not healthy, is not, whatever the log says.
- Otherwise (no /health, or one with no fsm) the last hold line in the log decides: a release line
  (above) is released, a later line that sets a hold again (a `device_held` event, `hold set`,
  `hold engaged`, ...) is not.
"""
from __future__ import annotations

import json
import re

RELEASE_RE = re.compile(r"\bhold\s+released\b|\bdevice_released\b", re.I)
# `device_held(?!_)`: the event, not a field such as `device_held_since`.
HOLD_RE = re.compile(r"\bhold\s+(?:set|placed|engaged|started|acquired|applied)\b|\bdevice_held\b(?!_)", re.I)
HEALTHY = "healthy"


def _health(line: str) -> dict | None:
    """The /health object on this line (one with a `held` field), else None."""
    start = line.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(line[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "held" in obj else None


def _fsm(health: dict) -> str:
    fsm = health.get("fsm_state")
    if fsm is None:
        fsm = health.get("fsm")
    if isinstance(fsm, dict):
        fsm = fsm.get("state")
    return str(fsm).strip().lower() if fsm is not None else ""


def released(text: str) -> tuple[bool, str]:
    """(released, why) for broker /health JSON and log text."""
    health, last = None, None
    for line in text.splitlines():
        health = _health(line) or health
        if RELEASE_RE.search(line):
            last = (True, line.strip())
        elif HOLD_RE.search(line):
            last = (False, line.strip())
    if health is not None:
        held, fsm = health.get("held"), _fsm(health)
        if held is False and fsm == HEALTHY:
            return True, f"/health: held false, fsm {fsm}"
        if held is not False or fsm:
            return False, f"/health: held {json.dumps(held)}, fsm {fsm or 'not reported'}"
    if last is None:
        if health is not None:
            return False, "/health: held false, fsm not reported, and no hold line in the log"
        return False, "no /health answer and no hold line in the log"
    return last[0], ("released: " if last[0] else "held again: ") + last[1][-200:]
