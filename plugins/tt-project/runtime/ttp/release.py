# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Newer tt-project releases. `ttp setup` installs a release into ~/.tt-project/lib/current; each
project's daemon compares that with its own harness runtime, says so while they differ and, with
`upgrade.auto` on, merges a strictly newer version into its own harness through `ttp upgrade` (one try
per release). The same version from another commit is only shown: a hand install or a rebuilt branch
must not trigger an unattended upgrade."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import locks, timefmt
from .project import HOME_DIR, Project, durable_write, fsync_dir, open_append_or_null

# A release candidate's fresh one-line install check, run model-free: `ttp install-check [<commit>]`.
#
# A release cut must show that the public one-line install works at the candidate commit. Run by the
# model as `curl ... | sh`, that download marks the run as reading untrusted input (untrusted.py,
# source 'download'), and a marked run may push no tag, so the cut could never move its channel tag
# (`ttp push --move-tag`). Here ttp itself does the download and the install, and the model only reads
# the recorded pass or fail:
#
# - the installer is the project's own `plugins/tt-project/install.sh` at exactly that commit, from the
#   GitHub repository of the push remote (else `origin`): no free URL, so this is no download channel
#   for other text;
# - it runs with `TT_PROJECT_REF=<commit>` in a fresh temporary HOME (and XDG dirs) under the run
#   folder, the user's own config dirs left out, the PATH profile untouched;
# - it passes when the installer exits 0 and the installed `ttp --version` names the commit;
# - the result (commit, exit code, installed version, URL, log path, time) is written to
#   `install-check.json` in the run folder (else the temp dir) and appended to the project's
#   state/install_checks.jsonl; only one summary line is printed, never the installer's output.
#
# Every untrusted-input guard stays: a model's own curl or wget still marks its run, and a marked run
# still pushes no tag.

INSTALL_LOG = "install_checks.jsonl"     # under the project's state: one line per check
INSTALL_RESULT = "install-check.json"    # in the run folder (or the check's temp dir)
INSTALLER = "plugins/tt-project/install.sh"
INSTALL_RAW = "https://raw.githubusercontent.com/{slug}/{commit}/" + INSTALLER
FETCH_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 1800
GITHUB_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                       r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
# left out of the installer's environment: the user's own tool homes and every harness setting
INSTALL_DROP = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CURSOR_CONFIG_DIR", "GH_CONFIG_DIR", "GIT_DIR", "GIT_WORK_TREE",
        "TT_PROJECT_REPO", "TT_PROJECT_REF", "TT_PROJECT_SRC")


def install_slug(repo: Path, cfg: dict) -> str:
    """owner/repo of the push remote's GitHub URL, else origin's; '' when neither is on GitHub."""
    from .channeltag import _git, push_remote
    prefix = ["git", "-C", str(repo)]
    remote = push_remote(prefix, cfg)[0]
    for r in dict.fromkeys(x for x in (remote, "origin") if x):
        out = _git(prefix, "remote", "get-url", r)
        if out.returncode == 0 and (m := GITHUB_RE.match(out.stdout.strip())):
            return m.group(1)
    return ""


def _install_fetch(url: str, dest: Path) -> None:
    """Download the installer to `dest` (tests replace this)."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        dest.write_bytes(r.read())


def _install_env(home: Path, commit: str, repo_url: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in INSTALL_DROP and not k.startswith("TTP_")}
    env.update(HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
               TT_PROJECT_REF=commit, TT_PROJECT_REPO=repo_url, TT_PROJECT_NO_MODIFY_PATH="1")
    return env


def install_check(p, repo: Path, commit: str, out_dir: Path | None = None) -> dict:
    """Fetch and run the installer at `commit` in a fresh HOME; return the recorded result."""
    from .channeltag import _commit
    prefix = ["git", "-C", str(repo)]
    sha = _commit(prefix, commit)
    rec: dict = {"commit": sha or commit, "rc": None, "version": "", "passed": False, "time": int(time.time())}
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="ttp-install-check-"))
    rec["log"] = str(out_dir / "install-check.log")
    s = install_slug(repo, p.config() if p else {})
    if not sha:
        rec["error"] = f"{commit} is no commit here"
    elif not s:
        rec["error"] = "neither the push remote nor origin is a GitHub repository"
    else:
        rec["url"] = INSTALL_RAW.format(slug=s, commit=sha)
        script = out_dir / "install-check.sh"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            home = Path(tempfile.mkdtemp(prefix="install-check-home-", dir=out_dir))   # fresh each time
            _install_fetch(rec["url"], script)
            with open(rec["log"], "wb") as log:
                done = subprocess.run(["sh", str(script)], cwd=home, env=_install_env(home, sha, f"https://github.com/{s}.git"),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=INSTALL_TIMEOUT_S)
            rec["rc"] = done.returncode
            ttp = home / ".local/bin/ttp"
            if ttp.exists():
                v = subprocess.run([str(ttp), "--version"], env=_install_env(home, sha, ""), capture_output=True,
                                   text=True, timeout=120, stdin=subprocess.DEVNULL)
                rec["version"] = v.stdout.strip().splitlines()[-1] if v.returncode == 0 and v.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        if rec["rc"] == 0 and sha[:12] not in rec["version"]:
            rec["error"] = f"installed ttp --version does not name {sha[:12]}"
        rec["passed"] = rec["rc"] == 0 and not rec.get("error")
    from . import project
    out_dir.mkdir(parents=True, exist_ok=True)
    project.durable_write(out_dir / INSTALL_RESULT, json.dumps(rec, indent=1) + "\n")
    if p is not None:
        try:
            project.durable_append(p.state / INSTALL_LOG, json.dumps(rec) + "\n")
        except OSError:
            pass
    return rec


def install_line(rec: dict) -> str:
    c = str(rec.get("commit") or "")[:12]
    if rec.get("passed"):
        return f"install check passed at {c}: {rec['version']} (rc 0; log {rec['log']})"
    why = rec.get("error") or f"installer exit {rec.get('rc')}"
    return f"install check FAILED at {c}: {why} (log {rec['log']})"


def install_command(a) -> None:
    """`ttp install-check [<commit>]`: exit 0 passed, 1 failed, 2 usage."""
    import sys
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else next((c for d in [Path.cwd(), *Path.cwd().parents]
                                         if (c := Project(d)).exists()), None)
    if p is not None and not p.exists():
        p = None
    out = a.out or os.environ.get("TTP_RUN_DIR") or None
    rec = install_check(p, Path.cwd(), a.commit, Path(out) if out else None)
    print(install_line(rec) + f"; result {Path(rec['log']).parent / INSTALL_RESULT}")
    sys.exit(0 if rec["passed"] else 1)

CHECK_S = 3600          # how often the daemon compares the installed release with its harness
HELD_RECHECK_S = 300    # a release held back by a push or an upgrade in flight is looked at again this soon
UPGRADE_TASK_TITLE = "Finish the tt-project template upgrade"
FENCED_PREFIX = "superseded by host upgrade"   # fence()'s blocked_reason: the harness, not the project, cancelled it
STALE_S = 6 * 3600      # a finish task blocked, or with no run or update this long, no longer holds off a host upgrade
KV_MERGE_BASIS = "upgrade_merge_basis"   # {task, files}: the project's own files the last merge task was queued on
KV_RELEASE, KV_AUTO = "release", "upgrade_auto"
KV_CHECKED = "release_checked"   # {}: its row time is when the daemon last compared releases (check_release)
SETUP_JOURNAL = ".setup.journal"   # in HOME_DIR/lib while `ttp setup` copies a release in (cli._recover_setup)
COPY_TRIES = 3          # an --auto upgrade whose template copy fails is held and retried, failed at the third
RECHECK = "rechecked soon"   # the tail of a hold_reason line check_release looks at again in HELD_RECHECK_S
PUSH_HOLD_MIN = 30      # upgrade.push_hold_min: past this, pushes hold an automatic upgrade only while one is inside a step
PUSH_STEP_RECHECK_S = 60   # a release held past that bound by a push step is looked at again this soon
KV_PUSH_HOLD = "upgrade_push_hold"   # {key, since, why}: the automatic upgrade pushes hold now, for line()
PUSH_HELD = "a push is "   # the start of every push_hold line


def installed() -> Path:
    return HOME_DIR / "lib" / "current"


def runtime_version(runtime: Path) -> str:
    try:
        m = re.search(r'__version__ = "([^"]+)"', (runtime / "ttp" / "__init__.py").read_text())
    except OSError:
        return "unknown"
    return m.group(1) if m else "unknown"


def runtime_commit(runtime: Path) -> str:
    try:
        return (runtime / "ttp" / "SOURCE_COMMIT").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _num(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def releases_behind(cur: str, target: str) -> int | None:
    """How many releases version `cur` is behind `target`: the patch gap within one major.minor line
    (every release bumps the patch); None when either is unknown or the lines differ."""
    c, t = _num(cur or ""), _num(target or "")
    if len(c) != 3 or len(t) != 3 or c[:2] != t[:2]:
        return None
    return max(t[2] - c[2], 0)


def is_newer(a: str, b: str) -> bool:
    """Version `a` is strictly newer than `b` (an unknown version is older than any known one)."""
    return _num(a) > _num(b)


def older(p: Project) -> dict | None:
    """The installed release when it is an older version than the project's own harness runtime
    (an older runtime's setup replaced a newer install); None otherwise or when either is unknown."""
    lib = installed() / "runtime"
    if not (lib / "ttp" / "__init__.py").is_file():
        return None
    new_v, cur_v = runtime_version(lib), runtime_version(p.harness / "runtime")
    if not _num(new_v) or not _num(cur_v) or not is_newer(cur_v, new_v):
        return None
    out = {"installed": f"{new_v} ({runtime_commit(lib)})", "harness": cur_v}
    if forced_version() == new_v:
        out["forced"] = True
    return out


def forced_mark() -> Path:
    """Written by `ttp setup --force` when it installs an older version over a newer one; names
    that version. While lib/current still holds it, daemons leave the downgrade alone."""
    return HOME_DIR / "lib" / "forced-downgrade"


def forced_version() -> str:
    try:
        return forced_mark().read_text().strip()
    except OSError:
        return ""


def restorable(at_least: str) -> Path | None:
    """The newest complete release under ~/.tt-project/lib (runtime, template and launcher) whose
    version is `at_least` or newer; None when there is none."""
    best, best_v = None, ()
    for d in (HOME_DIR / "lib").iterdir() if (HOME_DIR / "lib").is_dir() else ():
        if d.name == "current" or d.is_symlink() or not d.is_dir():
            continue
        if not ((d / "runtime" / "ttp" / "__init__.py").is_file() and (d / "template").is_dir()
                and (d / "bin" / "ttp").is_file()):
            continue
        v = _num(runtime_version(d / "runtime"))
        if v and v >= _num(at_least) and v > best_v:
            best, best_v = d, v
    return best


SETUP_SOURCE = "setup-source"   # in HOME_DIR/lib: the plugin folder the last `ttp setup` installed from


def _plugin_root(d: Path) -> bool:
    return ((d / "runtime" / "ttp" / "__init__.py").is_file() and (d / "template").is_dir()
            and (d / "bin" / "ttp").is_file())


def newest_source(claude_dir: Path | None = None, also: tuple[Path, ...] = ()) -> tuple[str, Path] | None:
    """The newest tt-project plugin on this host that `ttp setup` can install from, as (version, plugin folder):
    the folder the last setup used (a checkout), a release in Claude Code's plugin cache or a folder in `also`.
    None when there is none; never a release under ~/.tt-project/lib itself."""
    if claude_dir is None:
        from .providers.claude import claude_config_dir
        claude_dir = claude_config_dir()
    found: list[Path] = list(also)
    try:
        found.append(Path((HOME_DIR / "lib" / SETUP_SOURCE).read_text().strip()))
    except (OSError, ValueError):
        pass
    cache = claude_dir / "plugins" / "cache" / "tenstorrent-skills" / "tt-project"
    try:
        found += [d for d in cache.iterdir() if d.is_dir()]
    except OSError:
        pass
    lib = (HOME_DIR / "lib").resolve()
    best: tuple[str, Path] | None = None
    for d in found:
        try:
            d = d.resolve()
            if not d.name or d.is_relative_to(lib) or not _plugin_root(d):
                continue
        except OSError:
            continue
        v = runtime_version(d / "runtime")
        if v != "unknown" and (best is None or is_newer(v, best[0])):
            best = (v, d)
    return best


def point_current(target: Path) -> None:
    """Re-point lib/current at `target` in one step (a new symlink renamed over the old one), so a
    `ttp` starting meanwhile sees the old release or the new one, never none."""
    cur = installed()
    tmp = cur.with_name(f".current.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, cur)
    fsync_dir(cur.parent)


def drift(p: Project) -> dict | None:
    """The installed release when it is newer than, or the same version from another commit as,
    the project's harness runtime; None when they match or nothing is installed."""
    lib = installed() / "runtime"
    if not (lib / "ttp" / "__init__.py").is_file():
        return None
    new_v, new_c = runtime_version(lib), runtime_commit(lib)
    cur_v, cur_c = runtime_version(p.harness / "runtime"), runtime_commit(p.harness / "runtime")
    if (new_v, new_c) == (cur_v, cur_c) or _num(new_v) < _num(cur_v):
        return None
    return {"installed": f"{new_v} ({new_c})", "current": f"{cur_v} ({cur_c})", "key": f"{new_v} {new_c}",
            "newer": _num(new_v) > _num(cur_v)}


def retried(d: dict | None, cfg: dict) -> bool:
    """The daemon tries an automatic upgrade to drift `d` (check_release): only a strictly newer version,
    and only with upgrade.auto on. A paused project still does once unpaused. `ttp upgrade` counts a
    conflict as deferred on the same rule, so the two cannot disagree."""
    return bool(d and d.get("newer") and (cfg.get("upgrade") or {}).get("auto", True))


UPGRADE_RESOURCE = "harness-upgrade"     # `ttp lock --probe harness-upgrade` tells whether one runs
GUARD_ENV = "TTP_HARNESS_UPGRADE"         # set only while `ttp upgrade` holds the lock and moves main
GUARD_MARK = "# tt-project merge guard"
# Git runs this before it moves any ref. Moving main onto a commit that newly takes in the template
# branch is a merge of `upstream`: only `ttp upgrade` may do it, so a second process can never commit
# its own resolution of the same merge (and drop local wiring) behind an upgrade's back.
GUARD_HOOK = GUARD_MARK + """
[ "$1" = prepared ] || exit 0
[ -n "$TTP_HARNESS_UPGRADE" ] && exit 0
up=$(git rev-parse -q --verify refs/heads/upstream) || exit 0
while read -r old new ref; do
  [ "$ref" = refs/heads/main ] || continue
  case $new in *[!0]*) ;; *) continue ;; esac
  # `git update-ref <ref> <new>` with no old value sends the zero oid: check from main's real value.
  case $old in *[!0]*) ;; *) old=$(git rev-parse -q --verify "$ref") || continue ;; esac
  git merge-base --is-ancestor "$up" "$new" 2>/dev/null || continue
  git merge-base --is-ancestor "$up" "$old" 2>/dev/null && continue
  echo "refused: only \\`ttp upgrade\\` merges the tt-project template (upstream) into main." >&2
  echo "An upgrade task finishes a conflicting merge with \\`ttp upgrade <name> --apply <commit>\\`." >&2
  exit 1
done
exit 0
"""


def upgrade_lock(p: Project) -> Path:
    """The same file as `ttp lock harness-upgrade` takes: held for a whole `ttp upgrade`, released by
    the OS when its process ends, so a crash never wedges the project."""
    return locks.slot_paths(p.state / "locks", UPGRADE_RESOURCE, 1)[0]


def guard_harness(h: Path) -> bool:
    """Install the merge guard as the harness repo's reference-transaction hook (git 2.28+; older git
    skips it). A hook the project wrote itself is left alone. True when it is in place."""
    r = subprocess.run(["git", "-C", str(h), "rev-parse", "--git-path", "hooks"], capture_output=True, text=True)
    if r.returncode != 0:
        return False
    hooks = Path(r.stdout.strip())
    hook = (hooks if hooks.is_absolute() else h / hooks) / "reference-transaction"
    text = "#!/bin/sh\n" + GUARD_HOOK
    try:
        have = hook.read_text() if hook.exists() else ""
        if have and GUARD_MARK not in have:
            return False
        if have != text:
            durable_write(hook, text, mode=0o755)
        return True
    except OSError:
        return False


def _taken(path: Path) -> bool:
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return False
            except OSError:
                return True
    except OSError:
        return False


def open_upgrade_task(p: Project, db=None) -> int | None:
    # `db` is the caller's own connection when it runs on another thread (the web app).
    t = (db or p.db).one("SELECT id FROM tasks WHERE kind='harness' AND title=? AND status NOT IN "
                         "('done','failed','cancelled') ORDER BY id LIMIT 1", (UPGRADE_TASK_TITLE,))
    return int(t["id"]) if t else None


def finish_lock(p: Project) -> Path:
    """Names the harness task that finishes a template merge, and the release it targets. While that
    task is open it holds the live harness: no other `ttp upgrade` merges into main, a newer release
    retargets the task instead, and only the task's own `--apply` lands the merge. The task's status is
    the lock's life: once it is done, failed, cancelled or gone (also after a crash or a restart) the
    record is stale and counts as released."""
    return p.state / "locks" / f"{UPGRADE_RESOURCE}.task"


def _lock_rec(p: Project) -> dict:
    try:
        rec = json.loads(finish_lock(p).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _fence_fields(rec: dict) -> dict:
    """The record's generation and the tasks fenced off it: kept across every rewrite of the record."""
    return {"gen": int(rec.get("gen") or 0), "fenced": rec.get("fenced") or {}} if rec.get("gen") else {}


def take_finish_lock(p: Project, task: int, key: str, target: str) -> None:
    """Called under the upgrade lock (`ttp upgrade`), like every write of this record."""
    durable_write(finish_lock(p), json.dumps({"task": int(task), "key": key, "target": target,
                                              "since": time.time(), **_fence_fields(_lock_rec(p))}) + "\n")


def fence(p: Project, task: int, target: str) -> int:
    """Under the upgrade lock: a host-side upgrade takes the finish lock over from `task`. The lock's
    generation goes up first, with the task's own generation kept, so the task's later `--apply` is
    refused (fenced); then the task is cancelled. Returns the new generation."""
    rec = _lock_rec(p)
    gen = int(rec.get("gen") or 0)
    fenced = {**(rec.get("fenced") or {}), str(int(task)): gen}
    durable_write(finish_lock(p), json.dumps({"gen": gen + 1, "fenced": dict(list(fenced.items())[-20:])}) + "\n")
    p.db.update_task(int(task), status="cancelled", blocked_reason=f"{FENCED_PREFIX} to {target}")
    return gen + 1


def fenced(p: Project, task) -> bool:
    """`task` held the finish lock at an older generation than the lock's now: a host-side upgrade took
    it over, so nothing that task does may land in the live harness."""
    rec = _lock_rec(p)
    g = (rec.get("fenced") or {}).get(str(task).strip())
    return g is not None and int(g) < int(rec.get("gen") or 0)


def task_activity(db, tid: int) -> tuple[dict | None, float]:
    """The task and its last sign of life: its last update or run start or end, now while a run runs."""
    t = db.one("SELECT id, status, blocked_reason, created, updated FROM tasks WHERE id=?", (int(tid),))
    if not t:
        return None, 0.0
    r = db.one("SELECT MAX(COALESCE(ended, started, 0)) AS last, SUM(status='running') AS live FROM runs "
               "WHERE task=?", (int(tid),)) or {}
    if r.get("live"):
        return dict(t), time.time()
    return dict(t), max(float(t["updated"] or 0), float(t["created"] or 0), float(r.get("last") or 0))


def stale_holder(db, h: dict | None) -> str:
    """Why the finish task `h` no longer holds off a host-side upgrade ("" = it still does): it is
    blocked, or has had no run or update for STALE_S."""
    if not h or not h.get("task"):
        return ""
    t, last = task_activity(db, h["task"])
    if not t:
        return ""
    if t["status"] == "blocked":
        return "blocked: " + " ".join(str(t.get("blocked_reason") or "no reason given").split())[:300]
    idle = time.time() - last
    return f"{t['status']} with no run or update for {idle / 3600:.0f} h" if idle > STALE_S else ""


def holder_line(p: Project, h: dict, db=None) -> str:
    """Who owns a deferral behind finish task `h`, and when it resumes, for deferral lines."""
    db = db or p.db
    t, last = task_activity(db, h["task"])
    status = t["status"] if t else "gone"
    if status not in ("done", "failed", "cancelled") and db.kv("paused", False):
        # Its task cannot run, and a takeover only queues the merge again: no deadline that would slide.
        return (f"owner: harness task #{h['task']} ({status}); {p.name} paused; upgrades when resumed "
                f"(the task runs then)")
    return f"owner: harness task #{h['task']} ({status}); resumes when it ends" + takeover_when(p, db, h["task"], t, last)


def takeover_when(p: Project, db, tid: int, t: dict | None, last: float) -> str:
    """When this project's daemon takes stale finish task `tid` over (escalate_stuck), for status lines."""
    rec = db.kv(KV_FINISH_STUCK) or {}
    rec = rec if rec.get("task") == tid else {}
    if rec.get("sent"):
        return "; the daemon's takeover failed and the maintainers were told"
    if rec.get("took"):
        return "; the daemon is taking it over now"
    first = float(rec.get("first") or (time.time() if t and t["status"] == "blocked" else last))
    return (f", or at {timefmt.long(max(first + STALE_S, time.time()), p)} when this project's daemon takes it "
            f"over")


def finish_holder(p: Project, db=None, clean: bool = False) -> dict | None:
    """The open finish task that holds the live harness, {"task", "key", "target"}; None when none does.
    A record whose task has ended counts as released and, with `clean` (only under the upgrade lock),
    is removed. An open finish task without a record (queued by an older release) holds it too, its
    target unknown (key None)."""
    db = db or p.db
    path = finish_lock(p)
    try:
        rec = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        rec = {}
    if rec.get("task"):
        t = db.one("SELECT status FROM tasks WHERE id=?", (int(rec["task"]),))
        if t and t["status"] not in ("done", "failed", "cancelled"):
            return {"task": int(rec["task"]), "key": rec.get("key"), "target": rec.get("target")}
    if clean and path.exists():
        keep = _fence_fields(rec)     # the generation outlives the task: a fenced task's --apply stays refused
        if not keep:
            path.unlink(missing_ok=True)
        elif rec.get("task"):
            durable_write(path, json.dumps(keep) + "\n")
    tid = open_upgrade_task(p, db)
    if tid and fenced(p, tid):      # a crash between fencing it and cancelling it
        if clean:
            db.update_task(tid, status="cancelled", blocked_reason=FENCED_PREFIX)
        return None
    return {"task": tid, "key": None, "target": None} if tid else None


def unchanged_merge(p: Project, target: str, files: str) -> int | None:
    """The newest merge task when it ended without landing and was queued for this same release `target`
    and these same own files: another would end the same way, so none is queued until either changes. No
    timer holds a merge off: a newer release or a local edit queues the next at once. None otherwise (also
    for a cancelled task, which merged nothing, and one queued before its files were recorded)."""
    t = p.db.one("SELECT id, status, spec FROM tasks WHERE kind='harness' AND title=? ORDER BY id DESC LIMIT 1",
                 (UPGRADE_TASK_TITLE,))
    if not t or t["status"] not in ("done", "failed"):
        return None
    m = re.search(r"^Target release: tt-project (.+)$", t["spec"] or "", re.M)
    rec = p.db.kv(KV_MERGE_BASIS) or {}
    if not m or m[1].strip() != target or rec.get("task") != int(t["id"]) or rec.get("files") != files:
        return None
    return int(t["id"])


def defer(p: Project, auto: bool, key: str, frm: str, to: str, until: float, why: str) -> None:
    """A conflicting merge queues no task: record it so the daemon looks at this release again at `until`
    (hold_reason), and status says why it waits."""
    rec = (p.db.kv(KV_AUTO) or {}) if auto else {"key": key, "from": frm, "to": to, "ts": time.time()}
    p.db.set_kv(KV_AUTO, {**rec, "outcome": "deferred", "ended": time.time(), "until": until, "why": why})


KV_STUCK = "upgrade_stuck"      # the template-merge conflict that keeps an upgrade deferred, while it lasts
STUCK_AFTER_S = 2 * 86400       # the same conflict deferred this long is reported once
STUCK_RETRIES = 2               # ... or once this many harness tasks that took it on ended without landing it


def _conflict_sig(why: str) -> str:
    """The same conflict across releases: the conflicting files, else the problem without numbers."""
    m = re.match(r"the merge conflicts in (.+)", why)
    if m:
        return "conflicts in " + ", ".join(sorted(f.strip() for f in m.group(1).split(",")))
    return re.sub(r"\b[0-9a-f]{7,40}\b|\d+", "#", why)[:200]


def note_deferred(p: Project, why: str = "", task: int | None = None) -> None:
    """Every deferred upgrade passes here: `why` is the merge's problem ("" = an open harness task holds
    the upgrade, so no merge ran and the conflict on record stands). Once the same conflict has been
    deferred past STUCK_AFTER_S, or STUCK_RETRIES harness tasks ended without landing it, one normal
    observation tells the coordinator; later deferrals of that conflict stay quiet."""
    now = time.time()
    rec = p.db.kv(KV_STUCK) or {}
    if why and rec.get("sig") != _conflict_sig(why):
        rec = {"sig": _conflict_sig(why), "why": why[:300], "first": now, "tasks": []}
    if not rec:
        return
    if task and int(task) not in rec["tasks"]:
        rec["tasks"].append(int(task))
    rec["last"] = now
    ended = [t for t in rec["tasks"] if (p.db.one("SELECT status FROM tasks WHERE id=?", (t,)) or {"status": "done"})
             ["status"] in ("done", "failed", "cancelled")]
    age = now - float(rec["first"])
    if not rec.get("reported") and (age >= STUCK_AFTER_S or len(ended) >= STUCK_RETRIES):
        tasks = ", ".join(f"#{t}" for t in ended)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
               (now, "upgrade", "observation", "normal",
                f"`ttp upgrade` has deferred this project's template upgrade for {age / 86400:.1f} days on the same "
                f"merge problem: {rec['why']}"
                + (f" Harness tasks {tasks} ended without landing it." if ended else "")
                + " Reported once; later deferrals of this conflict stay quiet.", "queued"))
        rec["reported"] = now
    p.db.set_kv(KV_STUCK, rec)


KV_FINISH_STUCK = "upgrade_finish_stuck"    # the finish task seen blocked or idle: first seen, escalation sent


_takeovers: dict[str, subprocess.Popen] = {}   # this daemon's own takeovers in flight, by project name


def _launch_takeover(p: Project, task: int):
    """Start the installed release's host-side `ttp upgrade <name> --take-over-from <task>` for this project,
    detached (deploy.launch_takeover): a restart of this daemon leaves it running. Tests replace it."""
    from . import deploy
    return deploy.launch_takeover(p, p.name, task)


def takeover_busy(p: Project) -> str:
    """Why the daemon's takeover of a stale finish task waits for its next check ("" = go): a push, another
    upgrade, a `ttp setup` copy or a cycle notice is in flight."""
    from . import cyclenotice
    if h := cyclenotice.project_held(p):
        return cyclenotice.line(h, p)
    if _taken(upgrade_lock(p)):
        return "an upgrade is in flight"
    if live_pushes(p):
        return "a push is in flight"
    if (HOME_DIR / "lib" / SETUP_JOURNAL).exists():
        return "a `ttp setup` (or its recovery) is copying a release in"
    return ""


def _takeover_end(p: Project, t: dict, now: float) -> str | None:
    """How the daemon's takeover `t` ({"pid", "started"}) ended; None while it runs. One past
    deploy.TAKEOVER_TIMEOUT_S is over (stopped, when this daemon started it). Records the end in `t`."""
    if t.get("ended"):
        return str(t.get("rc") or "")
    from . import deploy
    pid = int(t.get("pid") or 0)
    late = now - float(t.get("started") or 0) > deploy.TAKEOVER_TIMEOUT_S
    child = _takeovers.get(p.name)
    if child is not None and child.pid == pid:
        rc = child.poll()
        if rc is None and late:
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
            rc = "timed out"
        if rc is None:
            return None
        _takeovers.pop(p.name, None)
        rc = rc if isinstance(rc, str) else f"exit {rc}"
    else:
        if pid and locks._alive(pid) and not late:
            return None
        rc = "timed out" if late else "ended while this daemon was down (exit code unknown)"
    t.update(pid=None, ended=now, rc=rc)
    return rc


def _log_tail(p: Project, n: int = 8) -> str:
    try:
        from . import deploy
        lines = deploy.takeover_log(p, p.name).read_text(errors="replace").splitlines()
    except (OSError, ImportError):
        return ""
    return " | ".join(ln.strip() for ln in lines[-n:] if ln.strip())[-1200:]


def escalate_stuck(p: Project) -> str:
    """The daemon's hourly look at the finish task. Once it has been blocked or idle past STALE_S from first
    seen, the daemon takes it over itself, as a host-side `ttp upgrade <name>` does: under the upgrade lock
    it cancels the task (superseded), bumps the lock's generation so the task's `--apply` is refused, merges
    the release, queues one new merge task on a conflict, and keeps local harness edits committed. At most
    once per stale task: the first-seen time and the takeover live in kv, so a restart neither repeats nor
    resets it. It waits while the project is paused (its task runs after the resume) or a push or another
    upgrade is in flight. Only a takeover that fails sends ONE note, with its error, to the maintainers'
    upstream inbox. The record goes when the task stops holding the harness. Returns what it did, for the
    log ("" = nothing)."""
    db = p.db
    rec = db.kv(KV_FINISH_STUCK) or {}
    h = finish_holder(p)
    if not h or (rec and rec.get("task") != h["task"]):
        if rec:
            db.x("DELETE FROM kv WHERE key=?", (KV_FINISH_STUCK,))
            rec = {}
        if not h:
            return ""
    why = stale_holder(db, h)
    if not why and not rec.get("took"):
        if rec and not rec.get("sent"):     # it moved again before anything was done
            db.x("DELETE FROM kv WHERE key=?", (KV_FINISH_STUCK,))
        return ""
    now = time.time()
    tid = h["task"]
    target = h.get("target") or "the installed release"
    reason = (why if why.startswith("blocked") else      # no hours: one fingerprint
              f"idle: {why.split(' with ')[0]} with no run or update for over {STALE_S // 3600} h") if why else "stale"
    if not rec:
        rec = {"task": tid, "first": min(now, task_activity(db, tid)[1] or now)}
    if rec.get("sent"):
        return ""
    if rec.get("took"):
        rc = _takeover_end(p, rec["took"], now)
        db.set_kv(KV_FINISH_STUCK, rec)
        if rc is None:
            return ""
        # It ended and the same task still holds the harness: the takeover did not finish it.
        err = f"{rc}; {_log_tail(p)}".strip("; ")
        return _send_stuck_note(p, rec, tid, target, reason or "stale", now,
                                f"The daemon's takeover (`ttp upgrade {p.name} --take-over-from {tid}`) "
                                f"failed: {err}")
    if now - float(rec["first"]) < STALE_S:
        db.set_kv(KV_FINISH_STUCK, rec)
        return ""
    if db.kv("paused", False):      # its task cannot run: it does after the resume, so the clock starts again then
        rec["first"] = now
        db.set_kv(KV_FINISH_STUCK, rec)
        return ""
    if upgrade_hold(p.name):        # a release hold refuses the takeover too: it runs once the hold ends
        db.set_kv(KV_FINISH_STUCK, rec)
        return ""
    if takeover_busy(p):            # retried at the next check; nothing is recorded
        db.set_kv(KV_FINISH_STUCK, rec)
        return ""
    try:
        proc = _launch_takeover(p, tid)
    except Exception as e:      # noqa: BLE001 - whatever stops the start is the error the maintainers get
        return _send_stuck_note(p, rec, tid, target, reason, now,
                                f"The daemon could not start its takeover: {type(e).__name__}: {e}")
    _takeovers[p.name] = proc
    rec.update(took={"pid": proc.pid, "started": now}, target=target, why=reason[:300])
    db.set_kv(KV_FINISH_STUCK, rec)
    return (f"took over: harness task #{tid} (template merge to {target}) is {reason}; the daemon runs "
            f"`ttp upgrade {p.name} --take-over-from {tid}` (pid {proc.pid}, log state/logs/takeover.log)")


def _send_stuck_note(p: Project, rec: dict, tid: int, target: str, reason: str, now: float, error: str) -> str:
    from . import upstream
    note = {"title": f"upstream: {p.name}: template upgrade finish task #{tid} to {target} is stuck",
            "spec": f"Harness task #{tid} holds the template merge of project {p.name} toward tt-project "
                    f"{target} and is {reason}. While it holds, the daemon's automatic upgrades wait and the "
                    f"project runs older code. {error[:1500]}. The takeover cancels task #{tid} (superseded), "
                    f"bumps the lock's generation so that task's `--apply` is refused, merges the release, and "
                    f"queues one new merge task if the merge conflicts; local harness edits are committed and "
                    f"kept. The daemon does not try it again for this task."}
    upstream.append(p.name, tid, [note])
    rec.update(sent=now, target=target, why=reason[:300], error=error[:600])
    p.db.set_kv(KV_FINISH_STUCK, rec)
    return f"takeover of harness task #{tid} failed; one note went to the upstream inbox: {error[:300]}"


def clear_stuck(p: Project) -> None:
    """The harness took in the template: no conflict is outstanding."""
    p.db.x("DELETE FROM kv WHERE key=?", (KV_STUCK,))


def push_hold_s(cfg: dict) -> float:
    try:
        v = float((cfg.get("upgrade") or {}).get("push_hold_min", PUSH_HOLD_MIN))
    except (TypeError, ValueError):
        v = PUSH_HOLD_MIN
    return (v if v > 0 else PUSH_HOLD_MIN) * 60


def live_pushes(p: Project) -> list[dict]:
    """The label of each `push:` lock taken now by a live process. One whose label names a process that is
    gone holds nothing: a process that inherited the file keeps it taken, not the push."""
    out = []
    for path in sorted((p.state / "locks").glob("push:*.lock")):
        if not _taken(path):
            continue
        try:
            h = locks.read_label(path)
        except (OSError, ValueError):
            h = {}
        if locks.pid_alive(h) is not False:
            out.append(h)
    return out


def push_hold(p: Project, key: str = "", cfg: dict | None = None) -> str:
    """Why pushes hold an automatic upgrade now ("" = they do not). Within upgrade.push_hold_min of the
    first push hold of release `key` (KV_PUSH_HOLD) or of the oldest live push, any live push holds it;
    past that only one inside a step (push.Steps: a rebase, the checks, the push itself), so the upgrade
    starts between steps and never in the middle of one."""
    holds = live_pushes(p)
    if not holds:
        return ""
    now = time.time()
    first = min(float(h.get("since") or now) for h in holds)
    rec = p.db.kv(KV_PUSH_HOLD) or {}
    if key and rec.get("key") == key and rec.get("since"):
        first = min(first, float(rec["since"]))
    bound = push_hold_s(p.config() if cfg is None else cfg)
    if now - first < bound:
        return f"{PUSH_HELD}in flight"
    steps = [h for h in holds if h.get("step")]
    if not steps:
        return ""
    h = steps[0]
    return (f"{PUSH_HELD}in its {h['step']} step since {timefmt.long(float(h.get('step_since') or now), p)}; "
            f"past the {bound / 60:g} min bound it waits only for that step to end, {RECHECK}")


def note_push_hold(p: Project, key: str, why: str) -> None:
    """check_release, with its hold_reason `why`: record a push hold for line(), `since` kept while pushes
    keep holding the same release. Another hold clears it; so does none, unless a push still runs: the
    upgrade's own check at its swap (cmd_upgrade) then counts the bound from the same `since`."""
    rec = p.db.kv(KV_PUSH_HOLD) or {}
    if not why.startswith(PUSH_HELD):
        if rec and (why or not live_pushes(p)):
            p.db.x("DELETE FROM kv WHERE key=?", (KV_PUSH_HOLD,))
        elif rec.get("why"):
            p.db.set_kv(KV_PUSH_HOLD, {**rec, "why": ""})   # no longer held: line() drops it
        return
    since = float(rec["since"]) if rec.get("key") == key and rec.get("since") else time.time()
    if rec.get("why") != why or rec.get("key") != key:
        p.db.set_kv(KV_PUSH_HOLD, {"key": key, "since": since, "why": why})


def merge_head(h: Path) -> str:
    """The live harness's MERGE_HEAD sha ("" when no merge is in progress there)."""
    r = subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def hold_merge(p: Project, sha: str) -> None:
    """An unfinished merge (MERGE_HEAD `sha`) in the live harness refused an upgrade, --auto or by hand (a
    deploy, a push batch): record a hold for the installed release that this project's daemon rechecks
    every HELD_RECHECK_S and starts once the merge is gone; `since` stays while the same sha does
    (expire_merge_hold fails it past STALE_S)."""
    rec = p.db.kv(KV_AUTO) or {}
    d = drift(p)
    same = rec.get("outcome") == "held" and rec.get("merge_head") == sha and rec.get("since")
    if d and rec.get("key") != d["key"]:    # a refusal by hand after another release's try: this one's record
        rec = {"key": d["key"], "from": d["current"], "to": d["installed"], "ts": time.time()}
    p.db.set_kv(KV_AUTO, {**rec, "outcome": "held", "ended": time.time(),
                          "why": f"an unfinished merge is in the live harness (MERGE_HEAD {sha[:12]})",
                          "merge_head": sha, "since": float(rec["since"]) if same else time.time()})


def expire_merge_hold(p: Project) -> str:
    """check_release, before hold_reason: a merge hold whose MERGE_HEAD is still the same sha past STALE_S
    is recorded failed; returns the one alert line then ("" otherwise). A new sha restarts the clock."""
    rec = p.db.kv(KV_AUTO) or {}
    if rec.get("outcome") != "held" or not rec.get("merge_head"):
        return ""
    cur = merge_head(p.harness)
    if cur and cur != rec["merge_head"]:
        hold_merge(p, cur)
    if not cur or cur != rec["merge_head"] or time.time() - float(rec.get("since") or 0) < STALE_S:
        return ""
    why = (f"an unfinished merge (MERGE_HEAD {cur[:12]}) has been in the live harness for over "
           f"{STALE_S // 3600} h; `git -C {p.harness} merge --abort` restores HEAD, then `ttp upgrade {p.name}`")
    finish(p, "failed", why=why)
    return why


def copy_failed(p: Project, e: BaseException) -> bool:
    """An --auto upgrade's template copy raised `e` (a setup mid-copy, a full disk): held and retried after
    HELD_RECHECK_S, recorded failed at the COPY_TRIES-th. True while it is held."""
    rec = p.db.kv(KV_AUTO) or {}
    tries = int(rec.get("copy_tries") or 0) + 1
    why = f"copying the release failed ({type(e).__name__}: {str(e)[:160]})"
    if tries >= COPY_TRIES:
        finish(p, "failed", why=f"{why}, {tries} tries", copy_tries=tries)
        return False
    finish(p, "held", why=why, copy_tries=tries, until=time.time() + HELD_RECHECK_S)
    return True


HOLDS_FILE = "upgrade-holds.json"   # in HOME_DIR: {name: {why, until, probe, cwd, set}}, this host's release holds
HOLD_MAX_S = 7 * 86400   # every release hold ends within this, a probe-ended one too
HOLD_PROBE_S = 20        # a hold's end probe runs at most this long; a slow or failing one keeps the hold
HELD_BY_USER = "release "   # hold_reason's line for a release hold starts so: "release held: <why> until <end>"


def holds_path() -> Path:
    return HOME_DIR / HOLDS_FILE


def _read_holds() -> dict:
    try:
        d = json.loads(holds_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _edit_holds(change) -> dict:
    """Apply `change(holds)` under this host's holds lock, drop the holds past their end time, write back."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(HOME_DIR / ".upgrade-holds.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        holds = _read_holds()
        change(holds)
        now = time.time()
        holds = {k: v for k, v in holds.items() if float(v.get("until") or 0) > now}
        durable_write(holds_path(), json.dumps(holds, indent=1, sort_keys=True) + "\n")
        return holds


def set_hold(name: str, why: str, until: float | None = None, probe: str = "", cwd: str = "",
             now: float | None = None) -> dict:
    """Hold project `name` on its current release: its daemon's automatic upgrade, the deploy loop's takeover
    and a plain `ttp upgrade` skip it until `until` (at most HOLD_MAX_S away) or until `probe` exits 0 (run
    from `cwd`), whichever comes first; a probe alone still ends at HOLD_MAX_S. Stored in this host's
    tt-project home, never in the project's own state or config. Replaces an earlier hold of `name`."""
    from . import ends
    now = time.time() if now is None else now
    why = " ".join(str(why or "").split())
    if not why:
        raise ValueError("a release hold needs --why: the reason the user or status reads")
    if until is None and not probe:
        raise ValueError("a release hold needs an end: --until <time> and/or --until-probe <command>")
    if until is not None and not now < until <= now + HOLD_MAX_S + 60:
        raise ValueError(f"a release hold ends ahead of now and at most {HOLD_MAX_S // 86400} days away")
    if probe:
        ends.check_sh(probe, "--until-probe")
        ends.check_not_trivial(probe, "--until-probe")
    rec = {"why": why[:300], "until": until if until is not None else now + HOLD_MAX_S, "set": now,
           "probe": probe, "cwd": cwd or os.getcwd(), "timed": until is not None}
    _edit_holds(lambda h: h.__setitem__(name, rec))
    return rec


def lift_hold(name: str) -> dict | None:
    """Remove project `name`'s release hold; the one removed (None: there was none)."""
    gone: list = []
    _edit_holds(lambda h: gone.append(h.pop(name, None)))
    return gone[0]


def _probe_over(probe: str, cwd: str) -> bool:
    from . import boundedchild
    return boundedchild.run(probe, HOLD_PROBE_S, shell=True, cwd=cwd if os.path.isdir(cwd) else None,
                            stderr="devnull").rc == 0


def upgrade_hold(name: str, now: float | None = None, probe: bool = True) -> dict | None:
    """Project `name`'s release hold while it stands, else None: past its time it is over; its probe (unless
    `probe` is False) exiting 0 ends it too, and that ending is recorded."""
    now = time.time() if now is None else now
    h = _read_holds().get(name)
    if not h or float(h.get("until") or 0) <= now:
        return None
    if probe and h.get("probe") and _probe_over(str(h["probe"]), str(h.get("cwd") or "")):
        try:
            _edit_holds(lambda hs: hs.pop(name, None) if hs.get(name, {}).get("set") == h.get("set") else None)
        except OSError:
            pass
        return None
    return h


def hold_text(h: dict, where=None) -> str:
    """'held: <why> until <end>' of a release hold (upgrade_hold)."""
    at = timefmt.long(float(h["until"]), where)
    if not h.get("probe"):
        end = at
    elif h.get("timed", True):
        end = f"{at} or until `{h['probe']}` exits 0"
    else:
        end = f"`{h['probe']}` exits 0 (at the latest {at})"
    return f"held: {h.get('why') or 'no reason given'} until {end}"


def hold_reason(p: Project, d: dict) -> str:
    """Why the daemon must not start an automatic upgrade to `d` now ("" = go)."""
    from . import cyclenotice
    if hold := upgrade_hold(p.name):   # the user's word, kept in this host's tt-project home (set_hold)
        return HELD_BY_USER + hold_text(hold, p)
    h = cyclenotice.project_held(p)
    if h:       # busy, not a try: nothing is recorded, so it starts once the notice is gone
        return cyclenotice.line(h, p) + "; " + CYCLE_HELD
    if _taken(upgrade_lock(p)):
        return "an upgrade is in flight"
    if why := push_hold(p, d["key"]):
        return why
    if (HOME_DIR / "lib" / SETUP_JOURNAL).exists():   # its copy would race ours; any `ttp` run recovers it
        return f"a `ttp setup` (or its recovery) is copying a release in; {RECHECK}"
    rec = p.db.kv(KV_AUTO) or {}
    if rec.get("outcome") == "held" and rec.get("merge_head") and merge_head(p.harness):
        return (f"an unfinished merge is in the live harness since {timefmt.long(float(rec.get('since') or 0), p)}"
                f"; {RECHECK}")
    if rec.get("key") == d["key"] and rec.get("outcome") == "held" and time.time() < float(rec.get("until") or 0):
        return f"{rec.get('why') or 'held'}; retry {int(rec.get('copy_tries') or 0) + 1} of {COPY_TRIES}, {RECHECK}"
    h = finish_holder(p)
    if h:       # a newer release than it targets: `ttp upgrade --auto` retargets the task (once per release)
        if h["key"] == d["key"] or rec.get("key") == d["key"]:
            return f"harness task #{h['task']} finishes an earlier upgrade ({holder_line(p, h)})"
        return ""
    if rec.get("key") == d["key"] and rec.get("outcome") == "deferred":
        if time.time() < float(rec.get("until") or 0):
            return (f"the last merge task ended without landing this release and nothing changed since (owner: "
                    f"this project's daemon; looks again at {timefmt.long(float(rec.get('until') or 0), p)})")
        return ""
    if rec.get("key") == d["key"] and rec.get("outcome") == "conflict" and own_files_moved(p):
        return ""       # its merge task ended unlanded and the project's own files changed since: merge again
    if rec.get("key") == d["key"] and rec.get("outcome") != "held":
        return "already tried for this release"
    return ""


def own_files_moved(p: Project) -> bool:
    """The project's own runtime and template files at the harness HEAD differ from those the last merge
    task was queued on (KV_MERGE_BASIS): `ttp upgrade --auto` then queues the next merge, or defers it
    through unchanged_merge when they match again. False when no basis was recorded or git fails."""
    from . import cli
    files = (p.db.kv(KV_MERGE_BASIS) or {}).get("files")
    if not files:
        return False
    try:
        return cli._own_files(p.harness, "HEAD") != files
    except (subprocess.SubprocessError, OSError):
        return False


CYCLE_HELD = "held until it is gone"   # the tail of hold_reason's power-cycle line: rechecked soon


def launch(p: Project) -> None:
    """Run the installed release's `ttp upgrade --auto` for this project, detached: it restarts the
    daemon that started it (running workers are kept)."""
    p.logs.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    line = f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} automatic upgrade\n"
    with open_append_or_null(p.logs / "upgrade.log", text=line) as log:
        log.write(line)
        log.flush()
        subprocess.Popen([sys.executable, str(installed() / "bin" / "ttp"), "upgrade", p.name, "--auto",
                          "--project-dir", str(p.base)], cwd=str(p.base), env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def start(p: Project, d: dict) -> None:
    """Record the try first, so a crash or a conflict is never retried for the same release."""
    old = p.db.kv(KV_AUTO) or {}
    same = old.get("key") == d["key"]     # upjournal's cuts and copy_failed's tries count in a row
    kept = {k: int(old[k]) for k in ("interrupted", "copy_tries") if same and old.get(k)}
    p.db.set_kv(KV_AUTO, {"key": d["key"], "from": d["current"], "to": d["installed"], "ts": time.time(),
                          "outcome": "running", **kept})
    launch(p)


def finish(p: Project, outcome: str, **extra) -> None:
    rec = p.db.kv(KV_AUTO) or {}
    p.db.set_kv(KV_AUTO, {**rec, "outcome": outcome, "ended": time.time(), **extra})


def line(p: Project, db, cfg: dict) -> str:
    """One line while the installed release differs from the harness ("" otherwise)."""
    d = db.kv(KV_RELEASE)
    if not d:
        return ""
    text = f"tt-project {d['installed']} available, harness on {d['current']}"
    if not d.get("newer", True):
        return text + " (same version from another commit: not applied automatically)"
    if not (cfg.get("upgrade") or {}).get("auto", True):
        return text + f" (upgrade.auto is off: `ttp upgrade {p.name}` applies it)"
    hold = upgrade_hold(p.name, probe=False)
    if hold:
        return text + f" (release {hold_text(hold, p)}; `ttp upgrade {p.name} --release` lifts it)"
    h = finish_holder(p, db)
    if h:
        return text + f" (the merge needs harness task #{h['task']}; {holder_line(p, h, db)})"
    rec = db.kv(KV_AUTO) or {}
    ph = db.kv(KV_PUSH_HOLD) or {}
    if ph.get("key") == d["key"] and ph.get("why") and rec.get("outcome") != "running":
        return text + (f" (upgrade held since {timefmt.long(float(ph.get('since') or 0), p)}: {ph['why']}; "
                       f"this project's daemon retries)")
    if rec.get("key") == d["key"]:
        if rec.get("outcome") == "running":
            if _taken(upgrade_lock(p)) or time.time() - float(rec.get("ts") or 0) < 900:
                return text + " (upgrading now)"
            return text + " (the automatic upgrade did not finish: see logs/upgrade.log)"
        if rec.get("outcome") == "deferred":
            at = timefmt.long(float(rec.get("until") or 0), p)
            return text + (f" (the merge needs judgment; the last merge task ended without landing it and nothing "
                           f"changed since; a newer release or a local edit queues the next; owner: this project's "
                           f"daemon; looks again at {at})")
        if rec.get("outcome") == "failed":
            return text + f" (the automatic upgrade failed: {rec.get('why') or 'see logs/upgrade.log'})"
        if rec.get("outcome") == "held" and rec.get("why"):
            return text + f" (held: {rec['why']}; this project's daemon retries)"
    return text
