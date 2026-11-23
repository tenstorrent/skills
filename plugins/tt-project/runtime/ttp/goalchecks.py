# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Stock goal checks: model-free answers to "is this goal met?" that a project references instead of
writing its own script. `ttp goal <check> ...` runs one; use it as a `retry_when`, `start_when`,
`on_pass_cmd` step, heal `check` or watcher command.

- `suite_green [--name N] [--min-tests N] [--cwd DIR] -- <cmd...>`: the test command exits 0 at the
  current head (its commit is in the line) and, with `--min-tests`, at least that many tests ran
  (counted from pytest, unittest or ctest summaries, or `--count-regex`).
- `artifact_fresh <path or glob> [--max-age-h H] [--newer-than-commit REV [--repo DIR]]
  [--newer-than PATH] [--min-bytes N]`: the newest match exists, has at least `--min-bytes` (1),
  and is newer than each bound given.
- `service_up [--unit U [--user] [--host ALIAS]] [--url URL [--expect CODE...] [--body-contains T]]`:
  the systemd unit is active, the URL answers an expected status (any 2xx/3xx by default).

Each observation is one JSON line in the watcher format, with stable keys:
`{"text", "severity", "key", "check", "ok"}` plus the check's own fields; a met goal is an info line
whose text reads `<key>: cleared: ...`, a missed one is `normal`, and `"error": true` marks a check that
could not run (a missing command or input, a timeout, git or systemctl failing).
Exit 0 every goal met, 1 one is not, 75 none failed but one could not be checked (the unknown code of
`start_when`, `retry_when` and heal checks).
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

UNKNOWN = 75
FAIL_SEVERITY = "normal"
# A summary's test count: (pattern, how its groups give the count).
_COUNTS = (
    (re.compile(r"\bRan (\d+) tests?\b"), lambda m: int(m.group(1))),                    # unittest
    (re.compile(r"\btests? passed, \d+ tests? failed out of (\d+)"), lambda m: int(m.group(1))),  # ctest
)
_PYTEST = re.compile(r"\b(\d+) (passed|failed|errors?|xfailed|xpassed)\b")


def observation(check: str, key: str, ok: bool | None, text: str, **fields) -> dict:
    """One JSON-line observation. `ok` None: the check could not run (`"error": true`). A met goal's
    text says `cleared:`, so as a watcher line it closes the issue an earlier miss opened under its key."""
    obs = {"text": f"{key}: {'cleared: ' if ok else ''}{text}", "severity": "info" if ok else FAIL_SEVERITY,
           "key": key, "check": check, "ok": bool(ok)}
    if ok is None:
        obs["error"] = True
    return {**obs, **fields}


def exit_code(observations: list[dict]) -> int:
    if any(not o["ok"] and not o.get("error") for o in observations):
        return 1
    return UNKNOWN if any(o.get("error") for o in observations) else 0


def test_count(output: str, regex: str | None = None) -> int | None:
    """Tests that ran, from the runner's summary (the last one printed); None when none is found."""
    if regex:
        found = re.findall(regex, output, re.MULTILINE)
        if not found:
            return None
        last = found[-1]
        return int(last[0] if isinstance(last, tuple) else last)
    for line in reversed(output.splitlines()):
        hits = _PYTEST.findall(line)
        if hits and ("=" in line or " in " in line):
            return sum(int(n) for n, _ in hits)
        if re.search(r"\bno tests ran\b", line):
            return 0
        for pattern, count in _COUNTS:
            m = pattern.search(line)
            if m:
                return count(m)
    return None


def _git_head(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def suite_green(cmd: list[str], name: str | None = None, cwd: str = ".", min_tests: int = 0,
                timeout_s: float = 3600, count_regex: str | None = None) -> list[dict]:
    from .heal import _exec
    key = f"suite_green:{name or ' '.join(cmd)[:60]}"
    if not cmd:
        return [observation("suite_green", key, None, "no test command given")]
    if not os.path.isdir(cwd):
        return [observation("suite_green", key, None, f"no folder {cwd}")]
    head = _git_head(cwd)
    rc, out = _exec(cmd[0] if len(cmd) == 1 else cmd, cwd, timeout_s, os.environ.copy())
    fields = {"head": head, "rc": rc}
    tail = out.splitlines()[-1][-200:] if out else ""
    if rc in (124, 126, 127):   # a timeout, or the command cannot be started: nothing was tested
        return [observation("suite_green", key, None, f"could not run (rc {rc}): {tail}", **fields)]
    ran = test_count(out, count_regex)
    fields["tests"] = ran
    if rc != 0:
        return [observation("suite_green", key, False, f"failed (rc {rc}) at {(head or '?')[:12]}: {tail}", **fields)]
    if min_tests:
        if ran is None:
            return [observation("suite_green", key, None, f"passed but printed no test count (need {min_tests})",
                                **fields)]
        if ran < min_tests:
            return [observation("suite_green", key, False, f"only {ran} tests ran (need {min_tests})", **fields)]
    return [observation("suite_green", key, True, f"green at {(head or '?')[:12]}"
                        + (f", {ran} tests" if ran is not None else ""), **fields)]


def _commit_time(rev: str, repo: str) -> float | None:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%ct", rev, "--"], cwd=repo, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except ValueError:
        return None


def artifact_fresh(pattern: str, max_age_h: float | None = None, newer_than_commit: str | None = None,
                   repo: str = ".", newer_than: str | None = None, min_bytes: int = 1,
                   now: float | None = None) -> list[dict]:
    key = f"artifact_fresh:{pattern}"
    now = time.time() if now is None else now
    from .knownpaths import glob_root_ok
    if not glob_root_ok(pattern):   # a walk of / or the home folder: slow, and macOS privacy prompts
        return [observation("artifact_fresh", key, None, "refused: the pattern searches / or the home folder; "
                            "start it at the folder the artifact is in")]
    paths = [p for p in glob.glob(os.path.expanduser(pattern), recursive=True) if os.path.isfile(p)]
    if not paths:
        return [observation("artifact_fresh", key, False, "missing")]
    stats = {}
    for p in paths:
        try:
            stats[p] = os.stat(p)
        except OSError:
            pass  # vanished between the glob and the stat
    if not stats:
        return [observation("artifact_fresh", key, None, "every match vanished while being read")]
    newest = max(stats, key=lambda p: stats[p].st_mtime)
    st = stats[newest]
    fields = {"path": newest, "mtime": int(st.st_mtime), "bytes": st.st_size}
    if st.st_size < min_bytes:
        return [observation("artifact_fresh", key, False, f"{newest} has {st.st_size} bytes (need {min_bytes})",
                            **fields)]
    age_h = (now - st.st_mtime) / 3600
    if max_age_h is not None and age_h > max_age_h:
        return [observation("artifact_fresh", key, False, f"{newest} is {age_h:.1f} h old (max {max_age_h:g} h)",
                            **fields)]
    bounds = []
    if newer_than_commit:
        t = _commit_time(newer_than_commit, repo)
        if t is None:
            return [observation("artifact_fresh", key, None, f"cannot read commit {newer_than_commit} in {repo}",
                                **fields)]
        bounds.append((f"commit {newer_than_commit}", t))
    if newer_than:
        try:
            bounds.append((newer_than, os.path.getmtime(newer_than)))
        except OSError:
            return [observation("artifact_fresh", key, None, f"cannot read input {newer_than}", **fields)]
    for what, t in bounds:
        if st.st_mtime <= t:
            return [observation("artifact_fresh", key, False, f"{newest} is older than {what}", **fields)]
    return [observation("artifact_fresh", key, True, f"{newest} is fresh ({age_h:.1f} h old)", **fields)]


def _http(url: str, expect: list[int] | None, body_contains: str | None, timeout_s: float) -> dict:
    key = f"service_up:{url}"
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        return observation("service_up", key, None, "bad URL: only http and https are checked", url=url)
    try:
        req = urllib.request.Request(url)
    except ValueError as e:
        return observation("service_up", key, None, f"bad URL: {e}", url=url)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            code, body = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read(65536) or b""
        except OSError:
            body = b""
    except (urllib.error.URLError, OSError) as e:
        return observation("service_up", key, False, f"does not answer: {getattr(e, 'reason', e)}", url=url,
                           status=None)
    ok = code in expect if expect else 200 <= code < 400
    if not ok:
        return observation("service_up", key, False, f"answered {code}", url=url, status=code)
    if body_contains and body_contains not in body.decode("utf-8", "replace"):
        return observation("service_up", key, False, f"answered {code} without the expected text", url=url,
                           status=code)
    return observation("service_up", key, True, f"answered {code}", url=url, status=code)


def service_up(unit: str | None = None, user: bool = False, host: str | None = None, url: str | None = None,
               expect: list[int] | None = None, body_contains: str | None = None,
               timeout_s: float = 30) -> list[dict]:
    if not unit and not url:
        return [observation("service_up", "service_up", None, "give --unit, --url or both")]
    out = []
    if unit:
        from .heal import _check_systemd
        key = f"service_up:{host + ':' if host else ''}{unit}"
        rc, text = _check_systemd({"unit": unit, "user": user, "host": host, "timeout_s": timeout_s}, {}, ".")
        out.append(observation("service_up", key, None if rc not in (0, 1) else rc == 0, text, unit=unit))
    if url:
        out.append(_http(url, expect, body_contains, timeout_s))
    return out


def run(a) -> int:
    """`ttp goal`: print each observation as a JSON line; return the exit code."""
    if a.goal == "suite_green":
        cmd = list(a.cmd)
        if cmd[:1] == ["--"]:
            cmd = cmd[1:]
        obs = suite_green(cmd, a.name, a.cwd, a.min_tests, a.timeout, a.count_regex)
    elif a.goal == "artifact_fresh":
        obs = artifact_fresh(a.path, a.max_age_h, a.newer_than_commit, a.repo, a.newer_than, a.min_bytes)
    else:
        obs = service_up(a.unit, a.user, a.host, a.url, a.expect, a.body_contains, a.timeout)
    for o in obs:
        print(json.dumps(o, sort_keys=True))
    return exit_code(obs)
