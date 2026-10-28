# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The per-project daemon. Deterministic and cheap: it owns time (schedules, watchers), money
(meters, gates), state (tasks, runs, messages) and I/O (web app, Slack, chats). It calls a model
only through a run: a coordinator turn when something needs a decision, or a worker for a task.

Invariants:
- at most one coordinator turn at a time; turns are batched, debounced and rate-capped;
- no run starts while its provider's gate forbids it; every run has a wall clock and a budget;
- a run's result is read from disk, never from a live pipe, so a daemon restart loses nothing;
- a missed schedule fires once on wake, never once per missed slot.
"""
from __future__ import annotations

import contextlib
import copy
import faulthandler
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import runtimeload   # first of its own: its LOADED_AT is when the daemon imported its code
from . import ackwait
from . import churnwatch
from . import alerts
from . import anchors
from . import askrethink
from . import audit
from . import redact
from . import replica
from . import schedlog
from . import replymsg
from . import msgparts
from . import awake
from . import cells
from . import budget as bud
from . import budgetkind
from . import pregrant
from . import deadwait
from . import cachettl
from . import chaincap
from . import chatbridge
from . import route
from . import checkcap
from . import conflictfix
from . import correction
from . import credits
from . import globalcap as gcap
from . import coordinator as coord
from . import coordlimits
from . import detach
from . import progress
from . import downtime
from . import effort
from . import extchange
from . import failover
from . import outagetask
from . import grants
from . import ends
from . import handofflint
from . import handoffcheck
from . import harnesstree
from . import heal
from . import holdloops
from . import headline
from . import pauseends
from . import hold
from . import initiator
from . import identity
from . import idlecap
from . import integrity
from . import jevuse
from . import jobuse
from . import land
from . import landed
from . import lease as leasemod
from . import localspend
from . import lockprobe
from . import locks
from . import upjournal
from . import machine_ledger
from . import netwait
from . import obligations
from . import onpassdry
from . import outagemsg
from . import pageto
from . import machines
from . import mitigate
from . import prguard
from . import projpause
from . import push
from . import pushclaim
from . import pushq
from . import rebootwait
from . import refwaits
from . import release
from . import responsibilities
from . import reviewcap
from . import reviewhold
from . import reviewreuse
from . import reviewstats
from . import runner
from . import schedule as sched
from . import service
from . import settinghold
from . import sharedchecks
from . import startcheck
from . import startgate
from . import sweep
from . import sweepskip
from . import testslots
from . import timefmt
from . import ttr
from . import turngate
from . import quietgate
from . import turnsize
from . import unblock
from . import upstream
from . import userpause
from . import verified
from . import waitgate
from . import waitheal
from . import watchdup
from . import schedrun
from . import screen as scr
from . import shared
from . import shorttmp
from . import worktree
from .db import (CHANGES_NEEDED, HARNESS_MERGED_KEY, NOT_ABANDONED_SQL, PAUSE_BOOT_KEY, SEVERITY_RANK, TERMINAL_TASK_STATES, WORKSPACE_TRIES_KEY, BranchIndex, assigned_tier, continues_id, deferral,
                 dependency_ids, pin_of, dump_result, load_result, review_subject, reviewed_ids, task_outcome, unplanned_abrupt,
                 without_deferral)
from .project import rebase_replace
from .project import (Project, command_env, deep_merge, layered, disk_resume_gb, durable_append, durable_write, git_fsync_env,
                      file_tail, hostname, nice_level, output_tail, own_free, own_push, push_allowed, zombie)
from .providers import get_provider
from .providers.base import FUNDS_RE, last_json_object, scratch_dir, service_path
from .providers.claude import as_windows
from .providers.jev import Jev, JevOutOfFunds, Pause as JevPause
from .prompts import RUN_DIR_MARK

TICK_S = 3.0
# Free plan-window meters (and credit readings) are read this often; a red provider's on every pass.
METER_EVERY_S = 600
# A red budget alert's episode (Daemon._red_episodes) ends only below this share of its limit, and
# the same reason red again this soon after that end is no new alert.
RED_CLEAR_RATIO = 0.8
RED_REPEAT_S = 3600
CLEAN_STOP_KEY = "clean_stop"   # kv: {boot, ts} of the daemon's last clean stop
SCHEDULE_BUDGET_KEY = "schedule_budget_raised"   # kv: schedule -> when its own-budget skip last raised (_schedule_skip)
SCHEDULE_BUDGET_SOURCE = "daemon:schedule-budget"
SCHEDULE_BUDGET_CLEARED = "the schedule ran again or its spend is back under its daily cap"
SCHEDULE_CAP_KEY = "schedule_cap_warned"   # kv: schedule -> {"day", "at"} of its last 80%-of-own-cap observation
SCHEDULE_CAP_SOURCE = "daemon:schedule-cap"
SCHEDULE_CAP_CLEARED = "a new budget day started"
SCHEDULE_GAP_KEY = "schedule_gap_names"   # kv: schedule-gap issue key -> the schedules it names (sweep_schedule_gaps)
SCHEDULE_GAP_CLEARED = "every schedule named ran again"
LEASE_STALE_S = 180
HEARTBEAT_STALE_S = 300   # longer than any single tick step (a git fetch, a watcher command)
WATCHDOG_S = 2 * HEARTBEAT_STALE_S   # no tick progress this long: the service restarts the daemon
PROGRESS_EVERY_S = 30   # how often a long tick tells the watchdogs it is still moving
STEP_FAILS_ALERT = 3      # consecutive ticks a tick step fails before it is alerted (Daemon._run_step)
STEP_FAILS_HIGH_S = 1800  # a step failing this long since its first failure: the alert rises to high
STEP_FAILING_KEY = "tick_steps_failing"   # kv: {step: since} of the alerted steps (alerts.holds, tick-step:)
# A tick still running this long gets every thread's stack written to STACKS_FILE (faulthandler) and,
# once it ends, an alert: a slow step that keeps telling the watchdogs it moves is never restarted.
TICK_OVERRUN_S = 900
# A tick with no progress this long gets its stacks written too: well before the watchdogs restart it
# (WATCHDOG_S after the last progress they saw), so a step blocked in a call with no timeout leaves them.
TICK_STALL_S = HEARTBEAT_STALE_S
WATCHER_MAX_S = HEARTBEAT_STALE_S - 60   # a command watcher's timeout_s is capped here, well below WATCHDOG_S
# Schedule runs go on off the tick (schedrun); a tick waits this long for them, so quick ones are recorded
# in the tick that started them and a slow one never holds it.
SCHEDULE_WAIT_S = 2.0
SCHEDULE_GRACE_S = 30   # past its own bounds (command timeout, builtin deadline), a run is killed and failed
# Under state/: every thread's stack, on SIGUSR1 or SIGUSR2 (register_stack_dump) and on a tick overrun
# or stall (check_overrun). ttp.watchdog copies each new dump to state/incidents/.
STACKS_FILE = "daemon-stacks.log"
STACKS_MAX_BYTES = 1 << 20          # a longer one is moved to <name>.1 when the daemon starts
RESULT_FILE = "result.json"
NOTIFY_FILE = "notify.jsonl"      # `ttp notify` lines a run left for the user (relay_worker_notifies)
NOTIFIES_PER_RUN = 3
ESCALATE_FILE = "escalate.jsonl"  # `ttp notify --severity high` lines a run wrote (relay_worker_escalations)
ESCALATIONS_PER_RUN = 2
ESCALATE_READ_MAX = 1 << 16       # bytes of ESCALATE_FILE read; lines past it count as nothing
# What a waiting hand-off keeps across a run the account refused (limit, auth).
WAIT_KEYS = ("retry_when", "retry_after_s", "waiting_for", "wake_tier", "next_step", "survives_reboot", "waits",
             "waiting_since", "stale_wakes", "wait_key", "on_pass_cmd", refwaits.FACTS_KEY, "spec_seen", "spec_wakes")
MAX_FOLLOWUPS, FOLLOWUP_SPEC_CHARS = 12, 4000   # per hand-off; each follow-up is its own event
# What a review the daemon queues repeats of the code task's spec and hand-off.
AUTO_REVIEW_SPEC_CHARS, AUTO_REVIEW_SUMMARY_CHARS = 2000, 1000
# With the push queue on, the daemon starts the project's checks on a code task's head as it queues
# its review (Daemon._precheck): their folder under the project's state, how long the review waits
# for them before it starts anyway, and how long finished ones are kept.
PRECHECKS_DIR, PRECHECK_LABEL = "prechecks", "precheck:"
PRECHECK_HOLD_S, PRECHECK_KEEP_S = 3600, 7 * 86400
# A failed review with fix specs gets its fix and re-review from the daemon this many rounds per stack;
# the re-review repeats the failed review's spec (its push and after-push steps) up to this length.
AUTO_FIX_ROUNDS, REVIEW_FIX_SPEC_CHARS = 2, 8000
FIX_BASE_LABEL = "fix_base:"   # a re-review: fix_base:<the head its failed review saw>; its fix must add commits to it
# The daemon's review spec embeds the code task's spec and hand-off, then this line; its own steps
# follow it. A re-review reads the first review's `ttp push --own` step only there, as this sentence.
REVIEW_CHECK_LINE = ("Check that it does what its spec asks, is correct, keeps the charter's restrictions, and "
                     "that its tests fail without it.")
INHERITED_COMMITS_MAX = 10   # commits per task an auto review's spec lists, as the queue's refusal does


# A link a review's spec gives outside GitHub (which `gh` reads): the review may need to open it.
_WEB_LINK = re.compile(r"https?://(?!(?:api\.)?github\.com\b)[\w.-]+", re.I)


def web_needed(role: str, task: dict | None) -> bool:
    """Whether a worker run gets the web tools (where the agent can take them away). Every kind of
    task but a review does: workers looked up third-party docs and APIs in code and question tasks.
    A review checks a diff and its tests (no review used the web in 14 days, ~1,300 runs), so it
    gets them only when its spec links a page outside GitHub. The no_internet restriction wins."""
    if role != "reviewer" and (task or {}).get("kind") != "review":
        return True
    return bool(_WEB_LINK.search(f"{(task or {}).get('title') or ''}\n{(task or {}).get('spec') or ''}"))


def task_env(tid) -> dict[str, str]:
    """The task id every run, check and model-free command sees: TTP_TASK, and TTP_TASK_ID as
    specs and scripts also spell it (empty for the coordinator, never an inherited value)."""
    return {"TTP_TASK": str(tid), "TTP_TASK_ID": str(tid)}


def inherited_line(carried: dict[int, list[str]]) -> str:
    """The review spec's line on the commits its head carries from tasks whose latest review failed
    (pushq.carried), with kind-review.md's choice, so the approval is not refused for them later."""
    parts = []
    for tid, commits in sorted(carried.items()):
        more = f" (+{len(commits) - INHERITED_COMMITS_MAX} more)" if len(commits) > INHERITED_COMMITS_MAX else ""
        parts.append(f"#{tid}: {'; '.join(commits[:INHERITED_COMMITS_MAX])}{more}")
    return (f"Its head carries unreviewed commits from tasks whose latest review failed ({' | '.join(parts)}). "
            f"Approve a head without them, or judge them in this review and, only if they are sound, add "
            f"\"inherited\": {sorted(carried)} to its push entry.")


OWN_STEP = re.compile(r"It (?:delivers onto its pr_branch|rebases \S+ and delivers onto its own branch) "
                      r".*`ttp push --own --detach`")
# What a failed review's hand-off says failed (review_failure; kind-review.md): only a check or gate
# that is missing or does not apply, with no code finding (check_only), code findings (substantive),
# or both (mixed). A check-only failure gets no fix and re-review: nothing in the change is to fix.
# Any other value is unknown and handled like substantive. The daemon writes a fourth, precheck, for a
# review it failed with no reviewer run because the checks it started on the head failed
# (Daemon._precheck_failed): it gets a fix and re-review like substantive.
REVIEW_FAILURE_KEY, REVIEW_FAILURE_KINDS = "review_failure", ("check_only", "substantive", "mixed", "precheck")
# A precheck failure's verdict: the last lines of checks.out it quotes, and the failing command's exit
# codes that mean a timeout or a kill (the environment, not the head), which leave the reviewer to run.
PRECHECK_TAIL_LINES, PRECHECK_KILLED_RCS = 20, (124, 137, 143)
# A PR whose open findings outlive the task that delivered it gets this many fix tasks from the daemon.
PR_FIX_ROUNDS = 2
PROBE_EVERY_S = 180     # how often a waiting task's `retry_when` (or a deferred one's `start_when`) probe runs
NOT_YET_RCS = (1, 75, 255)   # probe exits meaning "not yet": 1, EX_TEMPFAIL (a busy `ttp lock`), ssh unreachable
JOB_FAILED = "detached job failed"   # what a `ttp detach --check` exit detach.FAILED_RC found (Daemon._job_failed)
PROBE_TIMEOUT_S = 60
# A task asleep on its retry_when wakes once when its spec changes (Daemon._spec_wake): its hand-off keeps
# the spec's digest it slept on (SPEC_SEEN_KEY) and its recent spec-change wakes (SPEC_WAKES_KEY), at most
# SPEC_WAKES_PER_H an hour (a loop guard).
SPEC_SEEN_KEY, SPEC_WAKES_KEY, SPEC_WAKES_PER_H = "spec_seen", "spec_wakes", 3
PROBE_STALL_PASS_S = 120   # probe time one probe-stall disposition pass may spend (sweep.dispose_probe_stalls)
# The detached rerun of a shared failure's ids on the push-branch tip (Daemon._start_tip_check):
# argv dir worktree repo timeout_s cmd...; removes the worktree, then writes rc (124 once timed out),
# so a present rc means the rerun is wholly done.
TIP_RUNNER = r'''
import os, signal, subprocess, sys
d, wt, repo, limit, argv = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5:]
with open(os.path.join(d, "out"), "wb") as out:
    try:
        child = subprocess.Popen(argv, cwd=wt, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            rc = child.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            rc = 124
    except OSError:
        rc = 127
subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt], capture_output=True)
with open(os.path.join(d, "rc.tmp"), "w") as f:
    f.write(f"{rc}\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(os.path.join(d, "rc.tmp"), os.path.join(d, "rc"))
'''
ON_PASS_TIMEOUT_S, ON_PASS_DIR, ON_PASS_LOG = 600, "on_pass", "on_pass.log"   # a hand-off's on_pass_cmd
ON_PASS_NOTES_EVERY_S, ON_PASS_NOTES_DAYS = 600, 14   # sweep for notes an unsettled on_pass_cmd queued
AUTH_PROBE_S = 900      # while a provider without a login check is logged out, one run on it checks this often
AUTH_CHECK_S = (60, 120, 300, 600, 1200, 1800)   # backoff of the model-free login checks of an open breaker
ORPHAN_GRACE_S = 10     # TERM to KILL for an agent whose supervisor died
# How long the reaper waits for a group member caught mid-exec (its environment reads empty until the
# new program's is set up) to show the environment it carries.
EXEC_SETTLE_S = 2.0
# A supervisor whose pid lives but whose lease went stale for this long in awake time is hung (its
# watch thread died or it is stuck): the daemon ends it and its agent. Also, as a backstop, any run
# this far past its own wall clock, stall limit and waits.
LEASE_HUNG_S = 600
BACKSTOP_SLACK_S = 600
SLACK_ERROR_KEY = "slack_error"   # kv: {ts, error} of the last failed Slack send; None after a good one
HUNG_STOPS = ("hung", "overdue", "supervisor_error")   # a broken supervisor's ends: they count as a stall
HANDOFF_STATES = ("done", "blocked", "failed", "needs_review", "waiting")
SLEEP_CUT = ("timeout", "stalled", "lost", "failed")   # ends a host sleep can cause
# Ends that cost no attempt: the account refused, it is logged out, or a routed endpoint failed (route.py).
FREE_ENDS = ("limit", "auth", "fallback")
DISK_LIGHT_KINDS = ("question", "plan")   # the only task kinds that still start under the disk guard
LOGOUT_REPORT_S = 300    # how often a project reports into a machine-wide logout and checks its reminders
# While the coordinator cannot run for BLOCKED_RELAY_AFTER_S, high conditions it never saw that persisted
# RELAY_PERSIST_S go to the chat model-free, at most one message per RELAY_EVERY_S.
BLOCKED_RELAY_AFTER_S = 1800
RELAY_PERSIST_S = 7200
RELAY_EVERY_S = 3600
RELAY_MAX_LINES = 10
COORD_BLOCKED_KEY = "coordinator_blocked"   # kv: {since, why} while the coordinator cannot run
RELAYED_KEY = "relayed_issues"              # kv: {"last": ts of the last relay, "ids": {issue id: ts}}
DISK_RESUME = 1.2        # the guard ends once free space is this many times its threshold
DISK_FLOOR_GB = 2        # below this even questions and plans wait
DISK_SWEEP_EVERY_S = 900  # under the disk guard, finished tasks' worktrees are swept at most this often
DISK_DU_TIMEOUT_S = 30   # the guard alert's du breakdown stops after this, keeping what it measured
DISK_DU_TOP = 6          # the biggest top-level directories it names
KV_WORKTREES_LOGGED = "worktrees_logged"   # task -> the keep reason the daemon log last gave (compared by worktree.keep_key)
KV_WORKTREES_DIRTY = "worktrees_dirty"     # task -> modified tracked files a kept finished worktree holds
KEEP_RECHECK_S = 6 * 3600   # a finished task's kept worktree is looked at again this often
CONFIG_UNREADABLE_KEY = "config_unreadable"   # kv: project.json and its last good copy both unreadable
ALERT_KEEP_S = 30 * 86400   # alerts_sent keeps an entry this long: the longest every_s any alert uses
ROOT_CHECKOUT_KEY = "root_checkout"   # kv: task -> its open alert on what a run left in the project root's checkout
ROOT_WAKE_KEY = "root_dirty_wake"   # in the task's result: its one wake to commit or revert paths it dirtied in the root
ROOT_HEAD_KEY = "root_head"   # kv: the open alert on a project root left detached or off its branch (guard_root_head)
MEMORY_ARCHIVED_KEY = "memory_archived_at"   # kv: when the daemon last archived hidden memory (archive_hidden_memory)
LEASE_STATE_KEY = "lease_state"   # kv: the lease state last announced (lease.py), one event per change
# Steps a host that does not hold the project lease skips: they start model work, push, or answer
# people, which only the one live coordinator may do. Maintenance steps go on.
LEASE_STEPS = ("dispatch", "maybe_coordinate", "run_schedules", "start_pushes", "poll_slack", "deliver_outbound",
               "route_machines", "backup_branches", "retry_rejected")
HOST_CYCLE_SEEN_KEY = "host_cycle_seen"   # kv: power-cycle intent id -> held or lapsed, each logged or posted once
ROOT_RECHECK_S = 300   # an open root-checkout alert's condition is looked at again this often
PAGED_KEY = "watch_paged:"   # kv per command schedule: {"<project>\t<line key>": {at, first, n}} (pageto)
PROGRESS_KEY = "progress_raised"   # kv: driver key -> its stalled streak's first report (watch_progress)
STALL_KEY = "stall"   # kv: {"run": the last worker run when a stall was raised} (Daemon.check_stall)
STALL_SETTLE_S = 600   # after a daemon start, dispatch gets this long before a stall is raised
STALL_GAP_S = 600      # checks further apart than this (a paused project): readiness counts again
STALL_LIST_MAX = 30    # ready tasks listed in one stall observation
PRE_GRANT_KEY = "pre_grant_raised"   # kv: pregrant.fault_key -> when it was last raised
PRE_GRANT_REUSE_S = 30                # dispatch reuses a pre-grant check's answer this long
PRE_GRANT_TICK_S = 5                  # a tick starts no new pre-grant check once its checks took this long
PRE_GRANT_RERAISE_S = 6 * 3600        # a broken pre-grant check is raised again at most this often
STALE_HOLDS_KEY = "holds_stale"   # kv: run -> its hold alerted as kept for a closed task (Daemon.alert_stale_holds)
WORKSPACE_RETRY_NOTE = "workspace retry"   # a queued task's note while its workspace is retried
SLEPT_MIN_S = 60        # a run whose wall clock ran this much ahead of its monotonic clock overlapped a host sleep
SLEEPS_KEPT_S = 7 * 86400
SLEPT_LONG_S = 600      # a sleep this long is recorded as a `host_slept` event and tells the user once a day
SLEEP_EVENT_MERGE_S = 1800   # a sleep this soon after the last one (the dark wakes of a closed lid) extends its event
# A run that ended because the provider's API could not be reached (DNS gone after a host sleep, a
# network drop) is lost to the network, not an attempt; new runs on that provider wait (net_held).
NET_LOST_RE = re.compile(r"Can't reach the API server|ENOTFOUND|EAI_AGAIN")
# A resumed session's reported cost is netted of what its earlier runs booked only when the
# difference is at least this share of the run's own token-priced cost, less the slack (in dollars).
SESSION_NET_AGREE = 0.5
SESSION_NET_SLACK_USD = 0.05
REACH_EVERY_S = 30      # while a provider is held offline, how often its API host is resolved again
REACH_TIMEOUT_S = 5
NET_HOLD_MAX_S = 900    # a held provider still lets one run try this often: a lookup that keeps failing never holds forever
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")   # set: the host resolves nothing itself
LOCAL_ONLY_EVERY_S = 3600   # how often done code tasks' branches are checked against the remotes
LOCAL_ONLY_DAYS = 14        # done code tasks finished this recently are checked (flagged ones until cleared)
KV_LOCAL_ONLY = "local_only"   # kv: {task id: {branch, head, ahead, since}} for branches only on this machine
KV_INTEGRITY = "integrity"   # kv: the last boot integrity check (see Daemon.check_integrity)
KV_INTEGRITY_HOST = "integrity_host"   # kv: the change of machine last alerted (see Daemon._check_host)
INTEGRITY_RECHECK_S = 3600
KV_LOCAL_ONLY_FROM = "local_only_from"   # kv: when the check first ran; tasks done before it are not checked
KV_BACKUP = "backup_pending"   # kv: {task id: {branch, tries, next}}: branches for delivery.backup_remote
BACKUP_TRIES = 3            # a backup push that fails this often (network, auth) is given up with an observation
BACKUP_RETRY_S = 900        # times the try count: the wait before a failed backup push is tried again
KV_DIRTY_MAIN = "dirty_main"   # kv: key of the main checkout's dirty tracked paths last reported
UNQUEUED_TAG = "NOT queued by the daemon"   # on a `continue:` follow-up no hop was queued for
CONTINUE_UNQUEUED = "continue_unqueued"   # event: a done task's `continue:` follow-up has no continuing task
CONTINUE_UNQUEUED_AFTER_S = 1800   # how long after task_done before it is raised
CONTINUE_UNQUEUED_WINDOW_S = 86400   # task_done events older than this are not checked
CONTINUE_CHECK_EVERY_S = 300
STALE_DUPLICATE = "stale_duplicate"   # event: open work the plan no longer needs (check_stale_duplicates)
STALE_OPEN = ("queued", "waiting", "blocked")   # a hop not under way, that a later one may duplicate
STALE_REVIEWS_PER_PASS = 20   # queued reviews whose subject is looked up on the push branch per pass
KV_KEPT_MESSAGES = "kept_messages"   # kv: user message ids kept unhandled once after a fixable task_add rejection
REVIEW_SWEEP_EVERY_S = 300   # how often sweep_reviews looks for code and harness tasks left in review with no review
HARNESS_UNMERGED = "harness_review_unmerged"   # event: a harness task passed review but its branch is not merged
# sweep_unreviewed: a done (or long externally waiting) code task with an unlanded head and no review
UNREVIEWED_EVENT = "review_unqueued"
UNREVIEWED_PER_PASS = 2        # review.unreviewed_per_pass: most reviews one pass queues (0 off)
UNREVIEWED_MAX_AGE_H = 72      # review.unreviewed_max_age_h: tasks last updated longer ago are left alone
UNREVIEWED_GRACE_S = 120       # a task updated more recently than this is still settling
UNREVIEWED_PASS_S = 20         # the pass stops looking after this long, so the tick stays short
PUSH_REFS_EVERY_S = 600   # how often the push queue's pins (refs/ttp/push/<id>) of settled rows are pruned


def _push_not_landed(probe, rc) -> bool:
    """Whether a probe's exit is `ttp push --result <marker> --pushed` saying its push finished without landing."""
    return rc == push.NOT_PUSHED and (bud.result_wait(probe) or ("",))[0] == "push"


def log(p: Project, msg: str) -> None:
    p.logs.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {redact.scrub(msg)}\n"
    with open(p.logs / "daemon.log", "a") as f:
        f.write(line)


NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "sshfs", "9p", "afs", "ceph", "glusterfs", "lustre",
              "gpfs", "beegfs", "wekafs", "davfs", "ncpfs", "orangefs", "ocfs2", "gfs2"}


def network_mounts() -> list[Path]:
    """Mount points of network and FUSE filesystems, read from /proc/self/mounts (never stat'ed:
    a hung network mount blocks any stat of it in D state)."""
    try:
        lines = Path("/proc/self/mounts").read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        f = ln.split()
        if len(f) >= 3 and (f[2] in NETWORK_FS or f[2].startswith("fuse")):
            out.append(Path(f[1].replace("\\040", " ")))
    return out


def _scan_ok(d: Path, nets: list[Path]) -> bool:
    """A recursive scan of `d` stays on its own data: never / or a home folder (or above one),
    and no network or FUSE mount at or below it."""
    home = Path(os.path.expanduser("~"))
    if d == Path(d.anchor) or d == home or d in home.parents:
        return False
    return not any(m == d or d in m.parents or m in d.parents for m in nets)


def _run_bounded(cmd: list[str], timeout: float) -> tuple[bytes, bool]:
    """stdout of `cmd` and whether it finished within `timeout`. It runs through boundedchild: on timeout
    its process group is killed and waited for about 2 s more at most, so a child stuck in D state never
    blocks the caller."""
    from . import boundedchild
    res = boundedchild.run(cmd, max(timeout, 1), text=False, stderr="devnull", grace=1)
    return res.out, res.rc is not None and res.rc >= 0   # killed by a signal: not finished


def _du(args: list[str], timeout: float) -> tuple[dict[str, int], bool]:
    """{path: bytes} from `du -k` (unreadable directories are skipped), and whether it finished
    within `timeout`; what it printed before it was stopped is kept."""
    try:
        out, done = _run_bounded(["du", "-k", *args], timeout)
    except OSError:
        return {}, False
    sizes = {}
    for ln in out.decode(errors="replace").splitlines():
        kb, _, name = ln.partition("\t")
        if kb.strip().isdigit() and name:
            sizes[name] = int(kb) * 1024
    return sizes, done


def _mount_of(path: Path) -> Path:
    if os.environ.get("TTP_TEST_DISK_MOUNT"):   # tests measure a small folder, not the real disk
        return Path(os.environ["TTP_TEST_DISK_MOUNT"])
    p = path.resolve()
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def _ancestry(sizes: dict[str, int]) -> list[tuple[str, str, set[str]]]:
    """du's entries for _measured, shallowest first: (key, its path as a string, its ancestors')."""
    out = []
    for k in sizes:
        q = Path(k)
        out.append((k, str(q), {str(a) for a in q.parents}))
    return sorted(out, key=lambda e: len(e[2]))


def _measured(sizes: dict[str, int], root: Path, entries: list | None = None) -> int:
    """Bytes du measured under `root`: its own line once du finished it, else the outermost finished
    entries below it (du prints a directory once its whole subtree is counted). Linear in the
    entries: each one's ancestors are a set of strings (`entries`, from _ancestry; built when not
    given), checked against the set already counted."""
    top = str(root)
    if top in sizes:
        return sizes[top]
    done: set[str] = set()
    total = 0
    for k, path, anc in _ancestry(sizes) if entries is None else entries:
        if top in anc and not anc & done:
            done.add(path)
            total += sizes[k]
    return total


def own_usage(p: Project, dev: int, nets: list[Path], timeout: float) -> dict:
    """What this project's own folders on the filesystem `dev` hold: state/ (du -x -d 3, so each run
    directory and a worktree a worker made in it show) and worktrees/ (du -x -d 1, each task's), within
    `timeout` in all. What du measured before it was stopped counts (_measured). Returns {"bytes"
    (None: nothing measured), "complete", "folders" (the ones looked at), "consumers": [(label,
    bytes, count or None)] biggest first: run worktrees, task worktrees, each other state/ subfolder}."""
    deadline = time.monotonic() + timeout
    roots = []
    for d, depth in ((p.state.resolve(), 3), (p.worktrees.resolve(), 1)):
        try:
            if os.stat(d).st_dev == dev and not any(d == r or r in d.parents for r, _ in roots):
                roots.append((d, depth))
        except OSError:
            continue
    total, complete, any_line, consumers = 0, True, False, []
    run_wts = [d.resolve() for d in worktree.run_worktree_dirs(p)]
    for d, depth in roots:
        if not _scan_ok(d, nets):
            complete = False
            continue
        sizes, done = _du(["-x", "-d", str(depth), str(d)], deadline - time.monotonic())
        complete = complete and done
        any_line = any_line or bool(sizes)
        entries = _ancestry(sizes)   # once per du output: _measured is called per run worktree and subfolder
        total += _measured(sizes, d, entries)
        if d == p.state.resolve():
            wt_bytes = sum(_measured(sizes, w, entries) for w in run_wts)
            if run_wts:
                consumers.append(("run worktrees", wt_bytes, len(run_wts)))
            for sub in sorted({Path(k).relative_to(d).parts[0] for k, _, anc in entries if str(d) in anc}):
                n = _measured(sizes, d / sub, entries) - (wt_bytes if d / sub == p.runs.resolve() else 0)
                consumers.append((f"state/{sub}" + (" (other run files)" if sub == p.runs.name else ""), n, None))
        else:
            kids = [k for k in sizes if Path(k).parent == d]
            consumers.append(("task worktrees", sum(sizes[k] for k in kids), len(kids)))
    consumers = sorted((c for c in consumers if c[1] > 0), key=lambda c: -c[1])[:DISK_DU_TOP]
    return {"bytes": total if any_line else None, "complete": complete and any_line,
            "folders": [str(d) for d, _ in roots], "consumers": consumers}


def disk_breakdown(p: Project, path: Path, timeout: float = DISK_DU_TIMEOUT_S) -> dict:
    """What fills the filesystem holding `path`, so a full shared disk is not taken for project growth:
    this project's own data on it (its state/ and worktrees/ folders, by subfolder, see own_usage) and
    the biggest top-level directories (du -x -d 1). The project gets at most half of `timeout`; each
    du keeps what it measured when stopped. Never scans / or a home folder, nor anything holding a
    network or FUSE mount (see _scan_ok): those come back unknown."""
    deadline = time.monotonic() + timeout
    mount = _mount_of(path)
    nets = network_mounts()
    try:
        dev = os.stat(mount).st_dev
    except OSError:
        return {"mount": str(mount), "top": [], "complete": False, "own_bytes": None, "own_complete": False}
    own = own_usage(p, dev, nets, timeout / 2)
    mine = {"own_bytes": own["bytes"], "own_complete": own["complete"], "own_consumers": own["consumers"],
            "own_folders": own["folders"]}
    if not _scan_ok(mount, nets):
        return {"mount": str(mount), "top": [], "complete": False, "top_skipped": True, **mine}
    sizes, done = _du(["-x", "-d", "1", str(mount)], deadline - time.monotonic())
    sizes = {k: v for k, v in sizes.items() if Path(k) != mount}   # the total line
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:DISK_DU_TOP]
    return {"mount": str(mount), "top": top, "complete": done, **mine}


def disk_usage_line(b: dict, used: float) -> str:
    """One sentence for the guard alert: this project's share of the used space and its biggest
    consumers (run and task worktrees, state/ subfolders), then the biggest top-level directories. A
    du cut short gives the partial total it measured, said to be partial."""
    gb = lambda n: f"{n / 1e9:.1f} GB"   # noqa: E731
    amount = lambda n: gb(n) if n >= 1e8 else f"{n / 1e6:.1f} MB"   # noqa: E731  (never a bare 0.0 GB)
    parts = []
    if b.get("own_bytes") is not None:
        size = amount(b["own_bytes"]) if b.get("own_complete") else (
            f"at least {amount(b['own_bytes'])} (partial: what du measured within {DISK_DU_TIMEOUT_S // 2} s)")
        biggest = ", ".join(f"{label}: {n}, {amount(v)}" if n is not None else f"{label} {amount(v)}"
                            for label, v, n in b.get("own_consumers") or [])
        parts.append(f"This project's own data (state/ and worktrees/) is {size} of the {gb(used)} used on "
                     f"{b['mount']}" + (f" ({biggest})" if biggest else ""))
    elif b.get("own_folders"):
        parts.append(f"{gb(used)} used on {b['mount']}; this project's own data (state/ and worktrees/) was not "
                     f"measured: du printed nothing within {DISK_DU_TIMEOUT_S // 2} s or the folder is not scanned")
    else:
        parts.append(f"{gb(used)} used on {b['mount']}")
    if b.get("top"):
        partial = "" if b.get("complete") else f" (du stopped after {DISK_DU_TIMEOUT_S} s; partial)"
        parts.append(f"biggest top-level directories{partial}: "
                     + ", ".join(f"{name} {gb(n)}" for name, n in b["top"]))
    elif b.get("top_skipped"):
        parts.append("top-level directories unknown (not scanned: filesystem root, home folder or network mount)")
    else:
        parts.append(f"du measured no top-level directory within {DISK_DU_TIMEOUT_S} s")
    return "; ".join(parts) + "."


JEV_FUNDS_TEXT = ("The Jev account is out of credits. Screening and the other Jev checks fall back to rules "
                  "(more model calls, same coverage). Top up the Jev account to restore the savings.")

def _decisions(actions: list) -> list:
    """A coordinator turn's actions other than `noop`: what it decided."""
    return [a for a in actions if not (isinstance(a, dict) and a.get("type") == "noop")]


def cleared_claims(p: Project, key: str) -> list[str]:
    """The claim keys a cleared machine-wide episode ends: the CLI's, or each filesystem this
    project's disk guard watches (the low one is no longer recorded once it cleared)."""
    if key.startswith("auth:") or key in (netwait.KEY, "notifier"):
        return [f"{hostname()}|{key}"]
    out = []
    for path in (p.base, p.worktrees):
        try:
            out.append(f"{hostname()}|disk|{os.stat(path).st_dev}")
        except OSError:
            pass
    return out


def release_claims(p: Project, ep: dict) -> None:
    """A closed machine-wide episode (swept, or acknowledged once over) releases its claim:
    whoever holds it, the next project to see the condition again broadcasts it."""
    if ep.get("cleared") and (ep["key"].startswith("auth:") or ep["key"] in ("disk", netwait.KEY)):
        alerts.release(cleared_claims(p, ep["key"]), ep["cleared"])


def disk_guard_limits(disk: dict) -> tuple[float, float, tuple | None]:
    """(min_free_pct, min_free_gb, this machine's entry or None) of the disk guard, from a `disk`
    config section; this machine's entry in the machines list sets the GB."""
    pct, gb = float(disk.get("min_free_pct", 5) or 0), float(disk.get("min_free_gb", 150) or 0)
    try:
        mine = machines.disk_min_free_gb()
    except Exception:
        mine = None
    if mine:
        gb = mine[1]
    return pct, gb, mine


def disk_guard_bytes(disk: dict, path: Path) -> float:
    """The disk guard's threshold in bytes for the filesystem of `path` (0: the guard is off)."""
    pct, gb, _ = disk_guard_limits(disk)
    return min(pct / 100 * shutil.disk_usage(path).total, gb * 1e9)


class Daemon:
    def __init__(self, base: str | Path):
        self.p = Project(base)
        self.stopping = False
        self.boot, old = runner.boot_ids()
        self._boot_old = old if old != self.boot else None
        self.boot_at = runner.boot_time()
        self.gates: dict[str, bud.Gate] = {}
        self.cfg_status = "ok"
        self._load_config()
        self._jev_pause = JevPause()
        self.jev = Jev(self.cfg, db=self.p.db, pause=self._jev_pause)
        self._slack = None
        self._last_cfg = 0.0
        self._trouble_checked = 0.0
        self._upstream_checked = 0.0
        self._fixes_checked = 0.0
        self._forwarded = 0.0
        self._outbox_drained = 0.0
        self._forwarder: threading.Thread | None = None
        self._last_slack = 0.0
        self._thread_scan = 0.0
        self._slack_rejects: dict[int, int] = {}   # outbound message id -> times Slack refused it
        self._reap_errors: dict[int, int] = {}
        self._metered: dict[int, tuple[int, float]] = {}   # run id -> (output size, when) last priced
        self._escalated: dict[int, tuple[int, int]] = {}   # run id -> (size, mtime_ns) of its ESCALATE_FILE relayed
        self._start_failures = 0
        self._pre_grant_seen: dict[tuple[str, int], tuple[float, pregrant.Answer]] = {}
        self._pre_grant_broken: dict[str, tuple[float, pregrant.Answer]] = {}   # resource -> its last broken answer
        self._pre_grant_spent = (0.0, 0.0)   # (tick it was spent in, seconds of pre-grant checks in it)
        self._lock_fd: int | None = None
        self._started = time.time()
        self._loaded_at = runtimeload.LOADED_AT   # runtime files written later are not what this daemon runs
        self._reload_waits: set[int] = set()   # runs logged as waiting for a restart on their runtime changes
        # what code this process loaded, captured once before the first tick (never re-read)
        self.identity = identity.capture(boot=self.boot, started=self._started)
        # the harness checkout's HEAD and runtime/ tree this process loaded: exact, for runtimeload and status
        self._loaded = runtimeload.loaded(self.p.harness, self.identity.get("runtime"))
        self.identity.update(self._loaded)
        self._tree_asked: str | None = None   # the runtime tree a restart was last asked for (once per tree)
        self._tree_wait = ""                  # why that restart waits, logged once per reason
        self._healthy = False
        self._progressed = 0.0   # when the daemon last told the watchdogs a tick step finished
        self._tick_began = 0.0   # when the running tick started, monotonic (0: between ticks)
        self._moved = 0.0        # when the running tick last made progress, monotonic
        self._push_budget: tuple[float, pushclaim.Budget] | None = None   # this tick's git time for push checks
        self._push_carry: dict[str, tuple[float, dict]] = {}   # push checks' remote answers kept for later ticks
        self._sched_runs: dict[str, schedrun.Run] = {}   # schedule name -> its run going on off the tick
        self._sched_swept = False   # process groups an earlier daemon's schedule runs left are killed
        self._step_name = ""     # the tick step running now
        self._overrun: dict | None = None   # the running or last tick that passed TICK_OVERRUN_S or stalled
        self._last_prune = 0.0
        self._last_leftovers = 0.0   # when prune_leftovers last ran
        self._prune_more: str | None = None   # "plain" or "force": worktrees were left to archive, sweep next tick
        self._release_due = 0.0   # when the installed tt-project release is next compared with the harness
        self._root_checked = 0.0   # when clear_root_checkout last looked at the project root
        self._root_head_checked = 0.0   # when guard_root_head last looked at the project root's HEAD
        self._pruned_upto = 0.0   # the latest finish the last worktree sweep saw
        self._local_only_due = 0.0   # when done code tasks' branches are next checked for remote copies
        self._push_refs_at = 0.0     # when the push queue's pins were last pruned
        self._review_swept = 0.0     # when sweep_reviews last ran
        self._unreviewed_swept = 0.0   # when sweep_unreviewed last ran
        self._continues_checked = 0.0   # when check_unqueued_continues last ran
        self._stale_checked = 0.0   # when check_stale_duplicates last ran
        self._local_only_ok: dict[str, str] = {}   # branch -> head found on a remote: not looked at again
        self._local_only_job: dict | None = None   # the check running in its thread, applied by a later tick
        self._backup_job: dict | None = None   # the backup pushes running in their thread (backup_branches)
        self._replica_job: threading.Thread | None = None   # the state snapshot being taken (replicate_state)
        self._mcp_probe: threading.Thread | None = None   # `claude mcp list` in flight (probe_mcp)
        self._mcp_tried = 0.0   # when it last started: an unwritable answer is not retried every tick
        self._replica_checked = 0.0
        self._replica_stale: bool | None = None   # None: not looked since start
        self._replica_held_from = 0.0   # when this host took the project lease back (0: held since start)
        self._replica_unheld = False    # the last look found another host holding the lease
        if not isinstance(self.p.db.kv(KV_LOCAL_ONLY_FROM), (int, float)):
            self.p.db.set_kv(KV_LOCAL_ONLY_FROM, time.time())   # work done before the check existed is not flagged
        self._kept: dict[int, tuple[float, float, str]] = {}   # task id -> (task updated, checked, why kept)
        self._run_kept: dict[str, tuple[float, str]] = {}   # run worktree path -> (checked, why kept)
        self._disk_low = bool(self.p.db.kv("disk_low"))   # an episode outlives a restart: no second alert
        self._disk_swept = 0.0   # when the disk guard last swept finished tasks' worktrees
        self._disk_free: float | None = None
        self._tick_errors = 0
        self._step_fails: dict[str, tuple[int, float]] = {}   # tick step -> (consecutive failures, first failed)
        self.p.db.set_kv(STEP_FAILING_KEY, {})   # counts start again with this daemon (an episode then clears)
        self._probes: dict[int, tuple[subprocess.Popen, float, str]] = {}   # running: proc, started, probe
        self._probed: dict[int, float] = {}
        self._jobs_sampled = 0.0   # sample_jobs' last pass
        self._docker_off_until = 0.0   # sample_jobs skips docker until then after it could not be read
        self._ends = ends.Ends(self.p, log=lambda m: log(self.p, m))   # temporary instructions' end conditions
        self._pause_ends = pauseends.PauseEnds(self.p, log=lambda m: log(self.p, m))   # resource pauses' ends
        self._mute_ends = pauseends.MuteEnds(self.p, log=lambda m: log(self.p, m))   # observation mutes' end_when
        self._machine_probes = machine_ledger.Probes(self.p, log=lambda m: log(self.p, m))
        self._machines_routed = 0.0
        self._coverage_at = 0.0
        self._probe_rc: dict[int, tuple[int | str, float, str]] = {}   # last verdict: exit code or why, when, probe
        self._start_rc: dict[int, tuple[int | str, float, str]] = {}   # a start_when's last not-yet or broken verdict
        self._host_up: dict[str, tuple[bool, float]] = {}   # host a probe's 255 named -> ssh reached it, when
        self._probe_files: dict[int, object] = {}   # id(probe proc) -> the file its output goes to
        self._probe_out: dict[int, tuple[str, float, str]] = {}   # task -> its probe's last output, unchanged since, probe
        self._probe_live: dict[int, tuple[str, float, str]] = {}   # task -> its probe's last line, when read, probe
        self._woke_on: dict[int, tuple[int | str, float, str]] = {}   # task -> the verdict its last wake came on (wake gate)
        self._spec_capped: set[int] = set()   # tasks whose spec-change wake the loop guard holds (logged once)
        self._wait_what_errs: set[str] = set()   # causes of lock-probe errors already logged (_wait_what)
        # Exclusive slot reservations (_slot_reservations): canonical lock -> {"task", "holder", "free_since"},
        # the (lock, task) pairs whose reservation timed out, and the pass's reservers while dispatch runs.
        self._slot_resv: dict[str, dict] = {}
        self._slot_resv_lapsed: set[tuple[str, int]] = set()
        self._slot_resv_pass: dict[str, int] | None = None
        # running on_pass_cmd: task -> proc, started, timeout, its output dir, the waiting run's id, command
        self._on_pass: dict[int, tuple[subprocess.Popen, float, float, Path, int, str, str, str]] = {}
        self._on_pass_waits: set[tuple[int, int]] = set()   # (task, run) whose wait was logged once
        self._on_pass_notes_due = 0.0   # when runs are next swept for notes an unsettled on_pass_cmd queued
        self._reboot_told = False
        self._linger_at, self._linger_off = 0.0, False   # linger last looked at, and what it said
        self._last_stop: dict = {}   # the clean-stop record this daemon found at start
        self._boot_woken = False
        self._cycles: dict[str, dict | None] = {}   # earlier boot -> its planned host cycle record
        self._held: list[str] | None = None   # lock holders the heartbeat file last recorded
        self._notify: str | None = None   # systemd's socket for the watchdog ping, when it runs us
        self._tick_wall, self._tick_mono = time.time(), time.monotonic()
        self._awake = runner.AwakeClock()   # the daemon's own awake time: a hung run's stale lease counts in it
        self._stale_since: dict[int, float] = {}   # run -> awake time its lease was first seen stale from
        self._lease: leasemod.Lease | None = None   # the project lease, when failover.lease_remote is set
        self._lease_key: tuple | None = None
        self._settle_until = 0.0   # monotonic time before which nothing new starts (the host just woke)
        # provider -> {host, since, checked, checking, up, probe}: its API host must resolve before
        # new runs start on it (net_held). In memory only: a restart tries the network afresh.
        self._net_holds: dict[str, dict] = {}
        self._sched_sig: tuple[int, int] | None = None   # harness/schedules.json (mtime, size) last checked
        self._sched_read = 0.0   # monotonic time it was last read
        self._sched_problem: str | None = None   # what was wrong with it, last logged
        self._idle = awake.IdleHold(record=lambda state: self.p.db.set_kv(awake.KV, state))
        self._note_boot()

    # lifecycle ------------------------------------------------------------------------------------
    def run(self) -> int:
        self.p.state.mkdir(parents=True, exist_ok=True)
        pidfile = self.p.state / "daemon.pid"
        if not self._single_instance():
            print(f"daemon already running (pid {_read_pid(pidfile)})", file=sys.stderr)
            return 1
        pidfile.write_text(str(os.getpid()))
        # The last stop's record was read when this boot was noted (_note_boot); from here on a
        # reboot that finds none of this boot's was abrupt.
        self._last_stop = self.p.db.kv(CLEAN_STOP_KEY) or {}   # for schedules' gap causes (_schedule_gaps)
        self.p.db.x("DELETE FROM kv WHERE key=?", (CLEAN_STOP_KEY,))
        self._note_down()
        # Kept here rather than in the environment, which runs and their tools would inherit.
        self._notify = os.environ.pop("NOTIFY_SOCKET", None)
        self._stacks = register_stack_dump(self.p.state)
        self._mark_start()
        self._recover_upgrade()
        try:   # a project made before home zones gets one, once (timefmt.migrate)
            if timefmt.migrate(self.p, log=lambda m: log(self.p, m)):
                self._load_config()
        except (OSError, RuntimeError) as e:
            log(self.p, f"home time zone not set: {e}")
        try:   # coordinator turns from before runs recorded cache telemetry (bud.backfill_cache_telemetry)
            filled = bud.backfill_cache_telemetry(self.p.db, self.p.runs)
            if any(filled.values()):
                log(self.p, f"cache telemetry backfilled for {filled['filled']} coordinator turns, "
                            f"{filled['legacy']} marked legacy")
        except Exception:  # a report backfill must never keep the daemon from starting
            log(self.p, "cache telemetry backfill error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        try:   # push batch files left `running` by a batch whose row is final (pushq.close_strays)
            if closed := pushq.close_strays(self.p):
                log(self.p, f"closed {len(closed)} stray push batch file(s)")
        except Exception:
            log(self.p, "stray batch file close error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        r = self.identity
        log(self.p, f"daemon start pid={os.getpid()} host={hostname()} boot={self.boot} version={r['version']} "
                    f"commit={r['commit'] or 'none'} instance={r['instance']} provenance={r['provenance']} "
                    f"harness_head={r['harness_head'] or 'none'} runtime_tree={r['runtime_tree'] or 'none'}")
        self.p.db.set_kv("daemon", {"pid": os.getpid(), "host": hostname(), "started": time.time(),
                                    "instance": r["instance"]})
        self._check_config()
        self.sync_schedules(start=True)
        from .web import serve
        threading.Thread(target=serve, args=(self,), daemon=True).start()
        self._watch_ticks()
        self.check_integrity(start=True)
        while not self.stopping:
            try:
                self.tick()
                self._tick_ended()
                self._beat()
                try:
                    service.reap_restart_request(self.p, self._started)   # a restart helper that left no result
                except Exception:  # never in the way of a new restart request
                    log(self.p, "restart reap error: " + traceback.format_exc().replace("\n", " | ")[:2000])
                self._restart_on_moved_runtime()
                service.take_restart_request(self.p, self._started)   # a sandboxed `ttp upgrade --apply` asked
            except Exception:  # a bad tick must never kill the daemon
                log(self.p, "tick error: " + traceback.format_exc().replace("\n", " | ")[:2000])
                if not self._healthy:
                    self._tick_errors += 1
                    self._mark_start()
                time.sleep(10)
            time.sleep(TICK_S)
        log(self.p, "daemon stop")
        try:   # an orderly shutdown stops the daemon first: the next boot tells it from a crash
            self.p.db.set_kv(CLEAN_STOP_KEY, {"boot": self.boot, "ts": time.time(), "clean_stop": True})
        except Exception:
            log(self.p, "clean stop record: " + traceback.format_exc().replace("\n", " | ")[:1000])
        self._idle.release()
        # Runs going on die with the daemon, unrecorded (schedrun.stop): due again at its next start.
        for line in schedrun.stop(self.p.db, list(self._sched_runs.values())):
            log(self.p, line)
        self._ends.stop()   # end-condition probes are rerun after the next start
        self._pause_ends.stop()
        self._mute_ends.stop()
        self._machine_probes.stop()
        if _read_pid(pidfile) == os.getpid():
            pidfile.unlink(missing_ok=True)
        # Running workers are left alone: they write their results to disk and the next start
        # adopts them. `ttp stop --kill` and cancels end them explicitly.
        return 0

    def _single_instance(self) -> bool:
        """Hold an exclusive lock on state/daemon.lock for the process lifetime: the kernel drops it
        when the process dies, so neither a race between two starters nor a recycled pid in a stale
        pid file can matter. Where the file system has no flock, fall back to the pid file, trusting
        it only if that pid is really a tt-project daemon."""
        held = _flock(self.p.state / "daemon.lock")
        if held is False:
            return False
        if held is not None:
            self._lock_fd = held
            return True
        other = _read_pid(self.p.state / "daemon.pid")
        return not (other and other != os.getpid() and _is_daemon(other))

    def _restart_on_moved_runtime(self) -> None:
        """After a tick: the harness checkout's runtime/ tree moved on from the one this daemon loaded
        (ttp upgrade, a commit by hand, a merge, a reset): ask for the restart, with its rollback, a
        harness run's runtime change asks for (runtimeload.tree_moved), once per tree."""
        try:
            tree, why = runtimeload.tree_moved(self.p, self._loaded.get("runtime_tree"), self._tree_asked)
        except Exception:   # never in the way of the tick or of a restart already asked for
            log(self.p, "runtime tree check error: " + traceback.format_exc().replace("\n", " | ")[:2000])
            return
        if tree:
            self._tree_asked, self._tree_wait = tree, ""
            log(self.p, f"runtime tree moved on to {tree[:10]} (loaded {self._loaded['runtime_tree'][:10]}): "
                        f"restart asked")
        elif why and why != self._tree_wait:
            log(self.p, f"runtime tree moved on: restart waits ({why})")
        self._tree_wait = why if not tree else ""

    def _mark_start(self) -> None:
        """Record that this process started, and how often its first tick has failed, so `ttp restart`
        can tell a daemon that is alive but still in a slow first tick from one that is broken."""
        try:
            (self.p.state / "daemon.start").write_text(json.dumps(
                {"pid": os.getpid(), "host": hostname(), "started": self._started, "tick_errors": self._tick_errors,
                 "progress": self._progressed or None, "receipt": self.identity,
                 "stacks": bool(getattr(self, "_stacks", None))}))
        except OSError:
            pass

    def _load_config(self) -> None:
        """Reload project.json. Missing or broken, its last good copy stays in force; with none, an
        established project starts no coordinator turn or worker run (both would route on the
        defaults) and says so in one alert, which clears once the file reads again."""
        raw, status = self.p.read_config()
        self.cfg = layered(raw)
        if status != self.cfg_status:
            log(self.p, {"ok": "project.json reads again",
                         "fallback": "project.json is missing or not valid JSON: its last good copy stays in force",
                         "unavailable": "project.json is missing or not valid JSON and there is no last good copy: "
                                        "no new coordinator turns or worker runs"}[status])
        self.cfg_status = status
        db = self.p.db
        bad = status == "unavailable"
        if bad != bool(db.kv(CONFIG_UNREADABLE_KEY)):
            db.set_kv(CONFIG_UNREADABLE_KEY, bad)
        if bad:
            self.alert("config", f"{self.p.config_path} is missing or not valid JSON, and there is no last good "
                                 f"copy to fall back on. No coordinator turns or worker runs start until it reads "
                                 f"again (running work goes on); its earlier versions are in the harness git history.",
                       every_s=ALERT_KEEP_S)

    def _check_config(self) -> None:
        """Say once (per distinct finding) what in project.json no code reads, or which push check
        is no command, so a typo or a sentence there does not fail a push much later."""
        try:
            from .project import config_problems
            probs = config_problems(self.p.raw_config(), self.p.root)
            if probs:
                key = "config_problems:" + hashlib.sha1("\n".join(probs).encode()).hexdigest()[:10]
                self.alert(key, "project.json: " + "; ".join(probs), severity="low", every_s=ALERT_KEEP_S)
        except Exception:
            log(self.p, "config check: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def sync_schedules(self, start: bool = False) -> None:
        """Apply harness/schedules.json when it changed (at start, always). A hand edit is committed
        so it too leaves a trail; a file that does not check out is reported and left unapplied."""
        sig = None
        try:
            st = sched.file_path(self.p).stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        # The content is compared too, once a minute: an edit can keep both mtime and size.
        if not start and sig == self._sched_sig and time.monotonic() - self._sched_read < 60:
            return
        if sched.held_for_harness_task(self.p):
            return   # read again next tick: applied once it is committed or the harness task ends
        applied, problem = sched.sync_file(self.p, force=start)
        self._sched_sig, self._sched_read = sig, time.monotonic()
        if applied:
            log(self.p, f"applied {sched.FILE}")
            self.p.commit_harness([sched.file_path(self.p)], f"schedules: {sched.FILE} applied")
        if problem and problem != self._sched_problem:
            log(self.p, problem)
        self._sched_problem = problem
        if problem:
            key = "schedules_file:" + hashlib.sha1(problem.encode()).hexdigest()[:10]
            self.alert(key, f"{problem}. The schedules stay as they were until it is fixed.", severity="low",
                       every_s=ALERT_KEEP_S)

    def _note_boot(self) -> None:
        """On a new boot, keep what the earlier boot's last heartbeat said (when, and the resources
        held then) before this daemon's first tick overwrites it; the boot event is written from it
        once the runs the reboot cut short are reaped. The earlier boot ended orderly when the daemon
        recorded a clean stop on it (a shutdown stops the service first), abrupt otherwise (a crash,
        a power cut, a hardware reset). Who or what started the shutdown is read here too, before any
        transaction (initiator.find: bounded, best effort)."""
        try:
            db = self.p.db
            if self._this_boot((db.kv("boot_prev") or {}).get("boot")):
                return
            hb = heartbeat(self.p) or {}
            # An older runtime's heartbeat has no boot id; the boot last told about stands in.
            prev = hb.get("boot") or db.kv("reboot_told")
            if not prev or self._this_boot(prev):
                return
            clean = (db.kv(CLEAN_STOP_KEY) or {}).get("boot") == prev
            last = time.time() - hb["age"] if "age" in hb else None
            db.set_kv("boot_prev", {"boot": self.boot, "prev_boot": prev, "held": hb.get("held") or [],
                                    "last_heartbeat": last, "shutdown": "orderly" if clean else "abrupt",
                                    "initiator": initiator.find(self.p, prev, self.boot_at, last)})
        except Exception:
            log(self.p, "boot record: " + traceback.format_exc().replace("\n", " | ")[:1000])

    @property
    def boots(self) -> tuple[str, str]:
        """This boot's id, then the form an older version recorded for it (so an upgrade is no reboot)."""
        return self.boot, self._boot_old or self.boot

    def _this_boot(self, boot: str | None) -> bool:
        """Whether a recorded boot id names this boot, in the current or the older form."""
        return bool(boot) and boot in self.boots

    def _shutdown(self, boot: str | None) -> str:
        """How the host went down under boot `boot`: "orderly" when this boot's record says the daemon
        stopped cleanly on it, else "abrupt" (also when nothing says, to stay on the safe side)."""
        prev = self.p.db.kv("boot_prev") or {}
        orderly = boot and self._this_boot(prev.get("boot")) and prev.get("prev_boot") == boot \
            and prev.get("shutdown") == "orderly"
        return "orderly" if orderly else "abrupt"

    def _beat(self) -> None:
        """A completed tick. `status`, the web app and `ttp restart` read its age; the first one
        marks the harness commit this runtime is known to run on. The file names the boot, the
        resources held and this process's identity receipt, so the next boot can say what a reboot
        cut off; it is rewritten only when those change. The completed tick itself goes to the tick
        file (identity.mark_tick), which `_progress()` never touches."""
        hb = self.p.state / "heartbeat"
        held = shared.held(self.p, self.cfg)
        if not self._healthy or held != self._held:
            durable_write(hb, json.dumps({"pid": os.getpid(), "host": hostname(), "started": self._started,
                                          "boot": self.boot, "held": held, "takes": ["restart_requests"],
                                          "receipt": self.identity}))
            self._held = held
        else:
            os.utime(hb, None)
        identity.mark_tick(self.p.state, self.identity, first=not self._healthy)
        sd_notify("WATCHDOG=1", self._notify)
        self._progressed = time.time()
        if not self._healthy:
            self._healthy = True
            hang = self.p.db.kv("daemon_hang")   # ttp.watchdog restarted a hung daemon: this one ticks
            if hang and hang.get("pid") != os.getpid():
                self.p.db.set_kv("daemon_hang", None)
            try:
                head = subprocess.run(["git", "-C", str(self.p.harness), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, timeout=30)
                if head.returncode == 0:
                    self.p.db.set_kv("harness_good", {"commit": head.stdout.strip(), "ts": time.time()})
            except (OSError, subprocess.SubprocessError):
                pass

    def _stacks_file(self):
        """The open STACKS_FILE (register_stack_dump), else state/STACKS_FILE opened for appending."""
        if _STACK_FILE and not _STACK_FILE[0].closed:
            return contextlib.nullcontext(_STACK_FILE[0])
        return open(self.p.state / STACKS_FILE, "a")

    def _watch_ticks(self, every_s: float = 30) -> None:
        """A thread that dumps every thread's stack when a tick overruns (check_overrun)."""
        def watch() -> None:
            while not self.stopping:
                time.sleep(every_s)
                try:
                    self.check_overrun()
                except Exception:   # never in the way of the tick
                    log(self.p, "tick watch error: " + traceback.format_exc().replace("\n", " | ")[:1000])
        threading.Thread(target=watch, daemon=True, name="tick-watch").start()

    def check_overrun(self, now: float | None = None) -> dict | None:
        """Once per tick that has run TICK_OVERRUN_S, and once per stall of TICK_STALL_S without progress
        (before the watchdogs restart it): log it with the step it is in, and append every thread's
        stack to state/STACKS_FILE. Monotonic time, which stands still while the host sleeps, so a
        sleep is no overrun. Runs on the watch thread (files only, never the database)."""
        now = time.monotonic() if now is None else now
        began, moved, o = self._tick_began, self._moved, self._overrun or {}
        if not began:
            return None
        same = o.get("began") == began
        stalled = now - moved >= TICK_STALL_S and not (same and o.get("moved") == moved)
        if not stalled and (same or now - began < TICK_OVERRUN_S):
            return None
        path, step, mins = self.p.state / STACKS_FILE, self._step_name, int((now - began) // 60)
        what = (f"tick stalled: no progress for {int((now - moved) // 60)} min, running {mins} min" if stalled
                else f"tick overrun: running {mins} min")
        with self._stacks_file() as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {what}, in step {step or '(before the steps)'}\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
        self._overrun = {"began": began, "moved": moved, "step": step, "stacks": str(path),
                         "stalled": stalled or bool(same and o.get("stalled"))}
        log(self.p, f"{what}, in step {step or '(none yet)'}; "
                    f"every thread's stack is in {path}")
        return self._overrun

    def _tick_ended(self) -> None:
        """After a tick: alert once when it overran (check_overrun), naming the step and its stacks."""
        began, self._tick_began = self._tick_began, 0.0
        o = self._overrun
        if not o or o.get("began") != began or o.get("alerted"):
            return
        o["alerted"] = True
        mins = int((time.monotonic() - began) // 60)
        log(self.p, f"tick overrun over: the tick took {mins} min")
        stall = f" ({TICK_STALL_S // 60} min or more without progress)" if o.get("stalled") else ""
        self.alert("tick-overrun", f"A daemon tick took {mins} min{stall}, in step {o['step'] or '(none)'}: no worker "
                                   f"started and no message was handled meanwhile. Every thread's stack while it "
                                   f"ran is in {o['stacks']}.", severity="normal")

    def _progress(self) -> None:
        """A tick step finished: tell systemd's WatchdogSec and `ttp.watchdog` the daemon still moves,
        so a long tick (a first tick that fetches and adds many worktrees, several slow watchers) is
        not taken for a stuck one. Before this daemon's first completed tick the heartbeat is left
        alone (`ttp restart` reads it as that tick); the start marker carries the progress instead."""
        self._moved = time.monotonic()   # for check_overrun's stall dump
        now = time.time()
        if now - self._progressed < PROGRESS_EVERY_S:
            return
        self._progressed = now
        sd_notify("WATCHDOG=1", self._notify)
        if not self._healthy:
            self._mark_start()
            return
        try:
            os.utime(self.p.state / "heartbeat", None)
        except OSError:
            pass

    def _run_step(self, step, name: str = "", default=None):
        """Run one tick step so that its exception never skips the steps after it (dispatch, Slack,
        outbound delivery, pushes): log it and count it. A step that fails STEP_FAILS_ALERT ticks
        in a row is alerted, normal at first and high once it has failed STEP_FAILS_HIGH_S; its next
        success clears the count and the alert. The watchdogs still hear of progress either way.
        Returns the step's result, or `default` when it raised; `name` labels a step that is not a
        method of this daemon (a lambda)."""
        name = self._step_name = name or step.__name__
        result = default
        try:
            result = step()
        except Exception as e:  # one bad step must not stop the rest of the tick
            log(self.p, f"tick step {name} error: " + traceback.format_exc().replace("\n", " | ")[:2000])
            n, since = self._step_fails.get(name, (0, time.time()))
            self._step_fails[name] = (n + 1, since)
            if n + 1 >= STEP_FAILS_ALERT:
                try:
                    self._alert_step(name, n + 1, since, e)
                except Exception:
                    log(self.p, "tick step alert error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        else:
            if self._step_fails.pop(name, None):
                try:
                    self._clear_step(name)
                except Exception:
                    log(self.p, "tick step clear error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        self._progress()
        return result

    def _alert_step(self, name: str, fails: int, since: float, err: Exception) -> None:
        db = self.p.db
        failing = db.kv(STEP_FAILING_KEY, {}) or {}
        if name not in failing:
            failing[name] = since
            db.set_kv(STEP_FAILING_KEY, failing)
        mins = int((time.time() - since) // 60)
        first = (traceback.format_exception_only(type(err), err) or [type(err).__name__])[0].strip().splitlines()[0]
        severity = "high" if time.time() - since >= STEP_FAILS_HIGH_S else "normal"
        self.alert(f"tick-step:{name}", f"Daemon tick step {name} failed {fails} ticks in a row "
                   f"(for {mins} min): {first[:300]}. The other steps still run; the daemon log has the "
                   f"traceback.", severity=severity)

    def _clear_step(self, name: str) -> None:
        db = self.p.db
        failing = db.kv(STEP_FAILING_KEY, {}) or {}
        if failing.pop(name, None) is None:
            return   # never alerted
        db.set_kv(STEP_FAILING_KEY, failing)
        alerts.close_key(db, f"tick-step:{name}", "the step succeeded",
                         text="Cleared: " + alerts.CLEARED_TEXT["tick-step"].format(arg=name))

    def tick(self) -> None:
        self._tick_began = self._moved = time.monotonic()
        self._step_name = ""
        self._check_sleep()
        now = time.time()
        if now - self._last_cfg > 10:
            self._load_config()
            self._last_cfg = now
            self.jev = Jev(self.cfg, db=self.p.db, pause=self._jev_pause)
        self.tend_lease()
        # Compared as callables, not by name, so a step replaced on the instance is gated the same way.
        lease_off = [] if self.lease_held() else [getattr(self, n) for n in LEASE_STEPS]
        for step in (self.lift_boot_pause, self.reap_runs, self.reap_waiting_checks, self.wake_after_reboot, self.meter_running, self.relay_escalations, self.reconcile_tasks, self.retry_workspaces, self.tend_pushes,
                     self.verify_pushes,
                     self.prune_worktrees, self.prune_leftovers, self.land_harness_worktrees, self.backup_branches, self.replicate_state, self.check_local_only, self.check_disk,
                     self.sweep_alerts, self.check_release, self.sync_shared_pauses, self.check_pause_ends,
                     self.check_integrity, self.heal_harness_git,
                     self.sync_schedules, self.lint_charter, self.tend_holds, self.record_ttr, self.alert_long_pause,
                     self.check_linger, self.check_notifier, self.probe_mcp):
            if step in lease_off:
                continue
            self._run_step(step)
        self._idle.update(self.cfg, self._has_work)
        if self.p.db.kv("paused", False):
            if not lease_off:
                self._run_step(self.deliver_outbound)   # the long-pause alert, and what was posted before, still reach Slack
            return
        self._run_step(self._refresh_meters)
        self._run_step(self.update_gates)
        self._run_step(lambda: coord.expire_asks(self.p, hold=any(g.level == "red" for g in self.gates.values())),
                       "expire_asks")
        self._run_step(lambda: scr.expire_mutes(self.p.db, where=self.p), "expire_mutes")
        self._run_step(lambda: scr.end_task_mutes(self.p.db, where=self.p), "end_task_mutes")
        self._run_step(self.check_mute_ends)
        self._run_step(self.sweep_watcher_issues)
        self._run_step(self.sweep_schedule_gaps)
        self._run_step(self.review_jev)
        settling = self._run_step(self.settling, default=False)
        core = self.cfg.get("core_provider") or "claude"
        core_held = self._run_step(lambda: self.net_held(core) and not self._net_may_probe(core), "net_held", default=False)
        cycle = self._run_step(lambda: self.host_cycle_held(now), "host_cycle_held", default=False)
        for step in (self.run_schedules, self.poll_slack, self.check_resource_trouble, self.read_upstream,
                     self.wake_upstream_fixes, self.drain_note_outbox, self.forward_upstream, self.retry_rejected, self.check_stall, self.check_slot_wait, self.check_progress, self.check_churn, self.check_idle_capacity, self.check_obligations, self.audit_pushes, self.retire_ended, self.archive_hidden_memory, self.route_machines, self.refresh_coverage, self.sweep_holds, self.sweep_reviews, self.sweep_unreviewed, self.check_unqueued_continues, self.check_stale_duplicates, self.tend_mitigations, self.failover_pinned, self.rethink_asks, self.blocker_sweep, self.maybe_coordinate, self.refresh_ref_waits, self.sample_jobs, self.watch_progress, self.probe_waiting, self.start_pushes, self.dispatch, self.raise_pre_grant_faults, self.check_unreachable, self.remind_logouts, self.relay_when_blocked, self.deliver_outbound):
            if self.cfg_status == "unavailable" and step in (self.maybe_coordinate, self.dispatch):
                continue   # no routing to start model work with (see _load_config)
            # While the host settles after a sleep only new work waits: a person who wrote is answered now.
            # So while the core provider's API host does not resolve; dispatch holds each provider itself.
            if settling and step == self.dispatch or (settling or core_held) and step == self.maybe_coordinate \
                    and not self.p.db.one("SELECT id FROM messages WHERE direction='in' AND handled=0"):
                continue
            if cycle and step in (self.dispatch, self.start_pushes):   # the coordinator still runs
                continue
            if step in lease_off:   # another host holds the project lease
                continue
            self._run_step(step)

    def review_jev(self, every_s: float = 600) -> None:
        """Settle screening calls (scr.settle_jev), then switch off the Jev uses that do not save money
        (jevuse.review) and report each one once."""
        now = time.time()
        if now - getattr(self, "_jev_reviewed", 0.0) < every_s:
            return
        self._jev_reviewed = now
        scr.settle_jev(self.p.db, now)
        for use, s in jevuse.review(self.p.db, self.cfg, now):
            self.alert(f"jev-off:{use}", jevuse.off_text(use, s, self.cfg), severity="low", every_s=0)

    def tend_holds(self) -> None:
        """Log each whole-run hold kept for detached jobs once it ends, and rebuild one whose keeper
        died while its jobs still run (hold.tend)."""
        try:
            changed = hold.tend(self.p.state / "holds")
        except Exception:
            log(self.p, "detached-job holds: tending failed\n" + traceback.format_exc())
            return
        for rec in changed:
            res = ", ".join(str(r.get("resource")) for r in rec.get("resources") or [])
            what = (f"kept again for its detached jobs: its keeper had died (keeper pid {rec.get('keeper')})"
                    if rec["state"] == "rebuilt" else rec.get("why") or rec["state"])
            text = f"run {rec.get('run')} of task #{rec.get('task')}: {res} {what}"
            log(self.p, f"hold: {text}")
            task = int(rec["task"]) if str(rec.get("task") or "").isdigit() else None
            self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), "daemon", f"hold_{rec['state']}", "normal" if rec["state"] == "lost" else "low",
                         text[:500], "handled", task))
        self.alert_stale_holds()

    def alert_stale_holds(self) -> None:
        """One low alert per hold kept for detached jobs of a done, failed or cancelled task once it has
        held its resource longer than budget.stale_hold_alert_s: such a hold may starve the resource
        unseen. It is never released here (a job may still use the resource); the alert clears itself
        once the hold ends or its task is open again."""
        db, now = self.p.db, time.time()
        limit = float(self.cfg["budget"].get("stale_hold_alert_s", 21600) or 0)
        seen = db.kv(STALE_HOLDS_KEY, {}) or {}
        stale: dict[str, dict] = {}
        for rec in hold.holding(self.p.state / "holds") if limit > 0 else []:
            run = str(rec.get("run") or Path(rec["run_dir"]).name)
            task = db.one("SELECT id,title,status FROM tasks WHERE id=?",
                          (int(rec["task"]),)) if str(rec.get("task") or "").isdigit() else None
            held_s = now - float(rec.get("since") or now)
            if not task or task["status"] not in ("done", "failed", "cancelled") or held_s < limit:
                continue
            res = ", ".join(str(r.get("resource")) for r in rec.get("resources") or [])
            stale[run] = seen.get(run) or {"key": f"hold-stale:{run}", "task": task["id"], "resources": res,
                                           "run_dir": rec["run_dir"]}
            if run in seen:
                continue
            jobs = hold.live(rec.get("jobs") or []) or list(rec.get("jobs") or [])
            self.alert(stale[run]["key"],
                       f"Task #{task['id']} ({task['title'][:80]}) is {task['status']}, but run {run} has held "
                       f"{res} for its detached jobs for {held_s / 3600:.1f} h; others needing it wait. Jobs: "
                       f"{', '.join(jobs) or 'none recorded'}. The hold ends once each job writes its .rc or "
                       f"ends; nothing is released automatically, as a job may still use it.",
                       severity="low", every_s=0)
        for run, info in seen.items():
            if run in stale:
                continue
            rec = hold.read(Path(info.get("run_dir") or "")) or {}
            why = ("is released" if rec.get("state") != "holding"
                   else f"belongs to an open task again (#{info.get('task')})")
            db.post("out", f"Cleared: the hold of run {run} on {info.get('resources')} {why}.", chat=None,
                    kind="resolved", severity="low", ref=info.get("key"))
            sent = db.kv("alerts_sent", {}) or {}
            if sent.pop(info.get("key"), None) is not None:
                db.set_kv("alerts_sent", sent)   # the same hold may alert again
        if stale != seen:
            db.set_kv(STALE_HOLDS_KEY, stale)

    def check_root_checkout(self, r: dict, before: dict | None, now_at: dict | None = None) -> None:
        """After a worker or reviewer run: when the project root's checkout is on another branch (or
        commit, if it was detached) than as the run started, or has tracked paths with uncommitted
        changes it did not have then, raise one low alert and tell the coordinator. One per
        condition: a branch or paths an open alert already reports are not alerted again by the
        other runs that saw them. It names as the likely cause the runs whose working directory was
        the project root (run.json cwd), else the run that ended; the rest are only 'also running'.
        Nothing in the checkout is changed; the alert clears once the checkout is back and those
        paths are clean (clear_root_checkout). `now_at`: the checkout's state, read by the caller."""
        if not before or not r.get("task"):
            return
        now_at = now_at or worktree.checkout_state(self.p.root)
        if not now_at:
            return
        db = self.p.db
        seen = db.kv(ROOT_CHECKOUT_KEY, {}) or {}
        if seen:
            self._root_checked = 0.0   # the next sweep sees at once whether this run put it back
        was, now = before.get("branch") or before.get("head"), now_at["branch"] or now_at["head"]
        # Moving the root back to the base branch, or to where an open alert wants it, is no fault.
        try:
            home = {worktree.named_base(self.p).removeprefix("origin/")} - {""}
        except Exception:
            home = set()
        home |= {i.get("branch") for i in seen.values() if i.get("branch")}
        off = bool(was) and now != was and now not in home and not any(i.get("at") == now for i in seen.values())
        known = {x for i in seen.values() for x in i.get("new") or []}
        new = (sorted(set(now_at["dirty"]) - set(before["dirty"]) - known)
               if before.get("dirty") is not None and now_at["dirty"] is not None else [])
        if off and before.get("branch") and self._restore_root(r, before["branch"], now_at):
            now_at = worktree.checkout_state(self.p.root) or now_at
            off = (now_at["branch"] or now_at["head"]) != was
        if not off and not new:
            return
        rows = db.q("SELECT id, task, dir FROM runs WHERE role!='coordinator' AND task IS NOT NULL "
                    "AND (id=? OR (started<? AND (status='running' OR ended>=?))) ORDER BY id",
                    (r["id"], time.time(), float(r["started"] or 0)))
        root = os.path.realpath(self.p.root)
        in_root, others = [], []
        for o in rows:
            spec = _read_result(self._run_dir(o) / "run.json") or {}
            t = int(o["task"])
            (in_root if spec.get("cwd") and os.path.realpath(str(spec["cwd"])) == root else others).append(t)
        mine = int(r["task"])
        cause = sorted(set(in_root), key=lambda t: t != mine) or [mine]
        alongside = sorted(set(others) - set(cause))
        tid = str(cause[0])
        if tid in seen:   # the same task changed more: one alert per task, which now covers it too
            info = seen[tid]
            info["new"] = sorted(set(info.get("new") or []) | set(new))[:200]
            if off and not info.get("branch"):
                info.update(branch=was, at=now)
            db.set_kv(ROOT_CHECKOUT_KEY, seen)
            return
        task = db.task(int(tid)) or {"id": int(tid), "title": ""}
        title = str(task["title"])[:80]
        if not in_root:
            who = f"after run {r['id']} of task #{tid} ({title}); no run then worked in the project root"
        elif int(tid) == mine:
            who = f"after run {r['id']} of task #{tid} ({title}), which worked in the project root"
        else:
            who = f"while task #{tid} ({title}) worked in the project root (seen as run {r['id']} of #{mine} ended)"
        if len(cause) > 1:
            who += f"; also in the project root then: {', '.join('#' + str(t) for t in cause[1:10])}"
        where = (f"on {'branch ' + now_at['branch'] if now_at['branch'] else 'a detached HEAD at ' + now_at['head'][:12]}"
                 f" instead of {'branch ' + before['branch'] if before.get('branch') else before.get('head', '')[:12]}"
                 if off else "")
        shown = ", ".join(new[:10]) + (f" and {len(new) - 10} more" if len(new) > 10 else "")
        dirty = f"with uncommitted changes to {len(new)} tracked path{'' if len(new) == 1 else 's'} it did not have before: {shown}" if new else ""
        text = (f"The project root's checkout ({self.p.root}) was left {' and '.join(x for x in (where, dirty) if x)} "
                f"{who}"
                + (f"; also running then, not in the project root: {', '.join('#' + str(t) for t in alongside[:10])}"
                   if alongside else "")
                + ". Other tasks branch from and land on the project's base branch, so work in the root can race "
                  "with them and is on no remote. Nothing was changed; this clears once the checkout is back"
                + (f" on {before['branch'] or before['head'][:12]}" if off else "")
                + (" and those paths are clean" if new else "") + ".")
        seen[tid] = {"key": f"root-checkout:{tid}", "run": r["id"], "branch": was if off else None,
                     "at": now if off else None, "new": new[:200], "since": time.time()}
        db.set_kv(ROOT_CHECKOUT_KEY, seen)
        log(self.p, f"run {r['id']} of #{mine} saw the project root's checkout left {where or ''}"
                    f"{' ' if where and new else ''}{f'with {len(new)} newly dirty path(s)' if new else ''}; "
                    f"alerted as #{tid}'s")
        self.alert(seen[tid]["key"], text, severity="low", every_s=0)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "observation", "normal", text[:1500], "queued", int(tid)))

    def _root_wake(self, r: dict, run_dir: Path, status: str, before: dict, now_at: dict) -> tuple[dict, list]:
        """Which tracked paths a run whose working directory was the project root (run.json cwd) left
        newly dirty there, so its `done` is sent back once to commit or revert them (_finish_worker):
        returns (the start snapshot to judge the run by, those paths). None of them when the run did
        not end with a `done` hand-off, another run in the root overlapped it, a path was dirty as it
        started, or its task was woken for this before. The run after that wake is judged against its
        own start with the paths it was told about counted as clean, so paths still dirty raise
        check_root_checkout's alert. Reads only."""
        if before.get("dirty") is None or now_at.get("dirty") is None:
            return before, []
        root = os.path.realpath(self.p.root)
        cwd = str((_read_result(run_dir / "run.json") or {}).get("cwd") or "")
        task = self.p.db.task(r["task"])
        if not cwd or os.path.realpath(cwd) != root or not task:
            return before, []
        mark = load_result(task["result"]).get(ROOT_WAKE_KEY)
        if isinstance(mark, dict):
            if mark.get("open"):
                before = {**before, "dirty": sorted(set(before["dirty"]) - set(mark.get("paths") or []))}
            return before, []
        if status != "ok" or (_read_result(run_dir / RESULT_FILE) or {}).get("status") != "done":
            return before, []
        new = sorted(set(now_at["dirty"]) - set(before["dirty"]))
        if not new:
            return before, []
        rows = self.p.db.q("SELECT id, dir FROM runs WHERE role!='coordinator' AND id!=? "
                           "AND started<? AND (status='running' OR ended>=?)",
                           (r["id"], time.time(), float(r["started"] or 0)))
        for o in rows:
            spec = _read_result(self._run_dir(o) / "run.json") or {}
            if spec.get("cwd") and os.path.realpath(str(spec["cwd"])) == root:
                return before, []   # whose edits they are is unknown: check_root_checkout alerts as before
        return before, new[:200]

    def _restore_root(self, r: dict, branch: str, now_at: dict) -> bool:
        """check_root_checkout's repair, model-free: a run left the project root detached or on another
        branch than `branch`, where it started. Put it back (worktree.restore_root: a rescue branch
        keeps a detached HEAD's commits; no reset, no force) unless another run still works in the
        root, and record it in the log and as an event for the coordinator. Whether it is back."""
        busy = self._run_in_root(r["id"])
        if busy:
            log(self.p, f"run {r['id']}: the project root is off {branch}; not restored while run {busy} works there")
            return False
        ok, info = worktree.restore_root(self.p.root, branch, r["id"])
        where = f"branch {now_at['branch']}" if now_at["branch"] else f"a detached HEAD at {now_at['head'][:12]}"
        if not ok:
            log(self.p, f"run {r['id']}: could not put the project root back on {branch} from {where}: {info}")
            return False
        text = (f"The project root's checkout was left on {where} after run {r['id']} of task #{r['task']}; the "
                f"daemon put it back on {branch}" + (f" and kept the detached commits on branch {info}" if info else "")
                + ". Nothing was reset or forced.")
        log(self.p, text)
        self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                    (time.time(), "daemon", "observation", "normal", text, "queued", int(r["task"])))
        return True

    def _run_in_root(self, exclude: int = 0) -> int:
        """A running worker or reviewer run (not `exclude`) whose working directory is the project root, or 0."""
        root = os.path.realpath(self.p.root)
        for o in self.p.db.q("SELECT id, dir FROM runs WHERE status='running' AND role!='coordinator' AND id!=?",
                             (exclude,)):
            cwd = (_read_result(self._run_dir(o) / "run.json") or {}).get("cwd")
            if cwd and os.path.realpath(str(cwd)) == root:
                return int(o["id"])
        return 0

    def _root_home(self, branch: str) -> None:
        """Record `branch`, where the project root's checkout is seen, as the branch it belongs on
        (worktree.root_branch), unless a root alert is open or a run works there: a move a task made
        is never adopted, a user's own switch is."""
        db = self.p.db
        if not branch or branch == db.kv(worktree.ROOT_HOME_KEY) or db.kv(ROOT_HEAD_KEY) \
                or any(i.get("branch") for i in (db.kv(ROOT_CHECKOUT_KEY, {}) or {}).values()) or self._run_in_root():
            return
        db.set_kv(worktree.ROOT_HOME_KEY, branch)

    def guard_root_head(self) -> None:
        """Before a worktree is branched from the project root, and every ROOT_RECHECK_S while its alert
        is open: when the root's checkout is detached or on another branch than worktree.root_branch and
        no run works there, put it back (worktree.restore_root: no uncommitted tracked changes, no git
        operation under way, the branch exists; a detached HEAD's commits kept on a rescue branch),
        reported low; else one low alert (ROOT_HEAD_KEY) that clears once it is back. Nothing is reset,
        forced or discarded. Meanwhile new worktrees start from the named base or root_branch, never
        from that HEAD (worktree.root_base). A run still in the root is left to check_root_checkout."""
        db = self.p.db
        self._root_head_checked = time.time()
        try:
            at = worktree.checkout_state(self.p.root, dirty=False)
            if not at:
                return
            self._root_home(at["branch"])
            home = worktree.root_branch(self.p)
            info = db.kv(ROOT_HEAD_KEY) or None
            if at["branch"] and (at["branch"] == home or not home):
                return self._clear_root_head(info, at["branch"])
            busy = self._run_in_root()
            if busy:
                return
            where = f"branch {at['branch']}" if at["branch"] else f"a detached HEAD at {at['head'][:12]}"
            if home:
                ok, why = worktree.restore_root(self.p.root, home, 0, tag="guard")
            else:
                ok, why = False, "no branch is known for it (worktree.root_branch is unset and it was never seen on one)"
            if ok:
                text = (f"The project root's checkout was on {where}; before branching a worktree the daemon put it "
                        f"back on {home}" + (f" and kept the detached commits on branch {why}" if why else "")
                        + ". Nothing was reset or forced.")
                log(self.p, text)
                self.alert(f"root-head-restored:{at['head'][:12]}", text, severity="low", every_s=0)
                return self._clear_root_head(info, home)
            if info and info.get("at") == (at["branch"] or at["head"]):
                return
            key = "root-head"
            text = (f"The project root's checkout ({self.p.root}) is on {where}"
                    + (f", not on its branch {home}" if home else "") + f", and the daemon could not put it back: {why}. "
                    "New worktrees start from the project's base branch"
                    + (f" or {home}" if home else "") + ", never from that HEAD"
                    + ("" if home or worktree.named_base(self.p) else
                       "; with no base named, tasks that need a new worktree wait (set delivery.base_ref)")
                    + ". Nothing was changed; this clears once the root is back on its branch.")
            db.set_kv(ROOT_HEAD_KEY, {"key": key, "at": at["branch"] or at["head"], "since": time.time()})
            log(self.p, text)
            self.alert(key, text, severity="low", every_s=0)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (time.time(), "daemon", "observation", "normal", text[:1500], "queued"))
        except Exception:   # never in the way of dispatch: the base choice (root_base) holds without it
            log(self.p, "root HEAD guard: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def _clear_root_head(self, info: dict | None, branch: str) -> None:
        """guard_root_head's alert is over: the root is on `branch`, its branch."""
        db = self.p.db
        if not info:
            return
        db.set_kv(ROOT_HEAD_KEY, None)
        db.post("out", f"Cleared: the project root's checkout is back on branch {branch}.",
                chat=None, kind="resolved", severity="low", ref=info.get("key"))
        sent = db.kv("alerts_sent", {}) or {}
        if sent.pop(info.get("key"), None) is not None:
            db.set_kv("alerts_sent", sent)   # the same condition may alert again
        self._root_home(branch)

    def clear_root_checkout(self, force: bool = False) -> None:
        """Close each open root-checkout alert (check_root_checkout) whose checkout is back on the
        branch it left and whose newly dirty paths are clean; checked every ROOT_RECHECK_S."""
        db, now = self.p.db, time.time()
        seen = db.kv(ROOT_CHECKOUT_KEY, {}) or {}
        if not seen or (not force and now - self._root_checked < ROOT_RECHECK_S):
            return
        self._root_checked = now
        at = worktree.checkout_state(self.p.root)
        if not at:
            return
        left = {}
        for tid, info in seen.items():
            dirty = set(at["dirty"] or [])
            if (info.get("branch") and (at["branch"] or at["head"]) != info["branch"]) \
                    or (at["dirty"] is None and info.get("new")) \
                    or dirty & set(info.get("new") or []):
                left[tid] = info
                continue
            db.post("out", f"Cleared: the project root's checkout is back as it was before task #{tid}'s run.",
                    chat=None, kind="resolved", severity="low", ref=info.get("key"))
            sent = db.kv("alerts_sent", {}) or {}
            if sent.pop(info.get("key"), None) is not None:
                db.set_kv("alerts_sent", sent)   # the same task may alert again
            log(self.p, f"root-checkout alert of #{tid} cleared")
        if left != seen:
            db.set_kv(ROOT_CHECKOUT_KEY, left or None)

    def retire_ended(self) -> None:
        """Retire memory entries and charter sections whose end condition passed (see ends)."""
        try:
            self._ends.tick()
        except Exception:
            log(self.p, "retiring ended instructions failed\n" + traceback.format_exc())

    def archive_hidden_memory(self) -> None:
        """Once a day, model-free: archive the decisions and facts every prompt has left out for
        memory.archive_hidden_days (Project.archive_hidden_memory), with one harness commit and one
        low feed line, then re-check the memory budget."""
        now = time.time()
        if now - float(self.p.db.kv(MEMORY_ARCHIVED_KEY, 0) or 0) < 86400:
            return
        self.p.db.set_kv(MEMORY_ARCHIVED_KEY, now)
        days = float((self.cfg.get("memory") or {}).get("archive_hidden_days") or 0)
        try:
            names = self.p.archive_hidden_memory(days) if days > 0 else []
            coord.memory_budget_check(self.p)
        except Exception:
            log(self.p, "archiving hidden memory failed\n" + traceback.format_exc())
            return
        if names:
            log(self.p, f"memory: archived {', '.join(names)} (hidden {days:g}+ days)")
            self.p.db.post("out", f"Archived {len(names)} memory entr{'y' if len(names) == 1 else 'ies'} no prompt had "
                           f"shown for {days:g}+ days (decisions and facts only); `ttp memory {self.p.name} --search "
                           f"<words>` still finds them.", chat=None, kind="info", severity="low")

    def record_ttr(self) -> None:
        """Machine incidents for time to recover (ttr.observe), model-free, every ttr.OBSERVE_S. It runs
        while the project is paused too, but heal checks do not: a heal report gone stale then is ignored
        (ttr.down_now), so a pause does not stretch a heal-sourced incident."""
        now = time.time()
        if now - getattr(self, "_ttr_at", 0.0) < ttr.OBSERVE_S:
            return
        self._ttr_at = now
        try:
            opened, ended = ttr.observe(self.p, now)
        except Exception:
            log(self.p, "recording machine incidents failed\n" + traceback.format_exc())
            return
        for alias in opened:
            log(self.p, f"ttr: {alias} incident opened")
        for alias in ended:
            log(self.p, f"ttr: {alias} incident ended")

    def route_machines(self, every_s: float = 60) -> None:
        """Shared machines (machine_ledger): run this project's ledger probes, then send the pages due
        for long down or held machines and overdue changes, model-free. A page is marked delivered only
        once it went out; one that did not stays pending and goes on a later tick."""
        now = time.time()
        if now - self._machines_routed < every_s:
            return
        self._machines_routed = now
        try:
            self.power_cycle_notices(now)
        except Exception:
            log(self.p, "power-cycle notices failed\n" + traceback.format_exc())
        try:
            self._machine_probes.tick(now)
            pages = machine_ledger.route(now, claimer=self.p.name, budgets=outagetask.budgets(self.cfg),
                                          log=lambda m: log(self.p, m))
        except Exception:
            log(self.p, "routing shared-machine pages failed\n" + traceback.format_exc())
            return
        for page in pages:
            try:
                ok = self._machine_page(page, now)
            except Exception:
                ok = False
                log(self.p, f"page for {page['key']} failed\n" + traceback.format_exc())
            try:
                (machine_ledger.delivered if ok else machine_ledger.release)(page)
            except Exception:
                log(self.p, f"settling the page for {page['key']} failed\n" + traceback.format_exc())

    def host_cycle_held(self, now: float) -> dict | None:
        """The planned cycle of this host (hostcycle.hold) that holds this tick's new worker and reviewer
        runs and push-queue pushes, or None; runs in flight go on (power_cycle_notices). Logged once per
        cycle; one whose hold ran out with no new boot gets one low feed line."""
        from . import hostcycle
        try:
            c = hostcycle.hold(self.p, now, settle=True)
            gone = hostcycle.lapsed(now)
        except Exception:
            log(self.p, "planned host cycle check failed\n" + traceback.format_exc())
            return None
        seen = dict(self.p.db.kv(HOST_CYCLE_SEEN_KEY, {}) or {})
        told = dict(seen)
        if c and str(c["id"]) not in seen:
            log(self.p, f"{hostcycle.hold_line(c, self.p, now)}: no new worker or reviewer runs or pushes start "
                        f"(machine change #{c['id']})")
            told[str(c["id"])] = "held"
        for g in gone:
            if told.get(str(g["id"])) != "lapsed":
                at = timefmt.short(float(g["at"]) + machine_ledger.POWER_CYCLE_LEAD_S, self.p, now)
                self.p.db.post("out", f"The planned host cycle by {g.get('by') or 'unknown'} (machine change "
                                      f"#{g['id']}) did not come by {at}: new runs, checks and pushes start again.",
                               chat=None, kind="info", severity="low")
                told[str(g["id"])] = "lapsed"
        if told != seen:
            self.p.db.set_kv(HOST_CYCLE_SEEN_KEY, dict(list(told.items())[-50:]))
        return c

    def power_cycle_notices(self, now: float) -> None:
        """A planned power cycle (machine_ledger.add_power_cycle) close at hand: tell each running worker
        on that machine (this host, or a task resource on it), once, through its steer.md, to commit and
        hand off now, so the cycle takes no work with it."""
        due = machine_ledger.power_cycles_due(now)
        if not due:
            return
        mine = machines.here()
        runs = self.p.db.q("SELECT id, task, dir FROM runs WHERE status='running' AND role!='coordinator' "
                           "AND task IS NOT NULL AND dir IS NOT NULL")
        for c in due:
            alias, local = str(c.get("alias")), bool(mine) and mine[0] == c.get("alias")
            on = machine_ledger.on_machine(alias)
            when = timefmt.short(float(c["at"]), self.p, now)
            for r in runs:
                if not local and not any(on(res) for res in coord.task_resources(self.p.db.task(r["task"]) or {})):
                    continue
                where = "this host" if local else alias
                text = (f"{where} is to be power-cycled at {when} (machine change #{c['id']}). Commit your work now "
                        f"and hand off before then: `waiting` with a `retry_when` that exits 0 once {alias} is back "
                        f"(or `done` with a `continue:` followup). Start no new device job or long step on it.")
                coord._append_update(Path(r["dir"]) / "steer.md", text, f"power-cycle-{c['id']}-{r['id']}")

    def refresh_coverage(self) -> None:
        """The responsibility inventory and its health coverage (responsibilities.py), model-free: a
        file read and a few queries every CHECK_EVERY_S; stored when it changed and once a day."""
        now = time.time()
        if now - self._coverage_at < responsibilities.CHECK_EVERY_S:
            return
        self._coverage_at = now
        try:
            responsibilities.refresh(self.p, self.cfg, now)
        except Exception:
            log(self.p, "health coverage inventory failed\n" + traceback.format_exc())

    def _machine_page(self, page: dict, now: float) -> bool:
        """Deliver one page; whether it went out (or already had: a resend is idempotent)."""
        target, text = page["target"], page["text"]
        severity = page.get("severity") or "high"
        rec = page.get("outage") if page.get("kind") == outagetask.KIND else None
        if target == machine_ledger.USER:
            # A page recorded before outagemsg names no actor: say it here.
            said = text if outagemsg.ACTOR_RE.search(text) else f"{text} {outagemsg.ACTING} {outagemsg.TO_USER}."
            if page.get("kind") == "machine_condition":
                self.alert(page["key"], f"Outage: {said}", severity="high")
            else:   # an outage task open past its due time
                self.alert(page.get("id") or page["key"], f"Outage: {said}", severity=severity)
        elif target == self.p.name and rec is not None:   # the tracked task, in this project's own database
            if outagetask.ensure(self.p.db, rec, self.p, now) is None:
                log(self.p, f"page for {page['key']}: not an outage record, dropped")
        elif target == self.p.name and severity == "low":   # a held page's note: a feed line, no coordinator turn
            if not self.p.db.one("SELECT id FROM messages WHERE ref=? AND text=?", (page.get("id"), text)):
                self.p.db.post("out", text, chat=None, kind="info", severity="low", ref=page.get("id"))
        elif target == self.p.name:
            fp = page.get("id") or page["key"]
            with self.p.db.tx():
                if not self.p.db.one("SELECT id FROM events WHERE fingerprint=? AND kind=?", (fp, page["kind"])):
                    self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) "
                                "VALUES(?,?,?,?,?,?,?)", (now, "daemon", page["kind"], fp, "high", text, "queued"))
        else:
            got = upstream.send(self.p.name, None, target, text, severity, now=now, sender="daemon",
                                extra={"outage": rec} if rec is not None else None)
            if got not in ("sent", "duplicate"):
                log(self.p, f"page for {page['key']} to {target}: {got}; retried on a later tick")
                return False
        log(self.p, f"paged {target}: {text}")
        return True

    def sync_shared_pauses(self) -> None:
        coord.sync_shared_pauses(self.p)

    def check_pause_ends(self) -> None:
        """Lift coordinator pauses on fenced health evidence; report other ends once. User pauses
        only get the existing low line. Model-free, also while paused."""
        try:
            self._pause_ends.tick()
        except Exception:
            log(self.p, "checking resource pause ends failed\n" + traceback.format_exc())

    def check_mute_ends(self) -> None:
        """Observation mutes with an end_when probe: a pass ends the mute (pauseends.MuteEnds). Model-free."""
        try:
            self._mute_ends.tick()
        except Exception:
            log(self.p, "checking observation mute ends failed\n" + traceback.format_exc())

    def tend_pushes(self) -> None:
        """Start the detached pushes and jobs queued from inside a sandbox; record the dead pushes as failed. Then
        the push queue's bookkeeping, also while paused: approvals whose review moved on, a queue
        turned off, and batches that ended (pushq.tend). Now and then, pins of settled rows go and code
        tasks left in 'review' after their review pushed their work are closed (pushq.settle_reviewed)."""
        from . import deploy
        try:
            deploy.tick(self.p, self.alert)
        except Exception:
            log(self.p, "checking pending deploys failed\n" + traceback.format_exc())
        for line in push.tend(self.p):
            log(self.p, line)
        try:
            detach.tend(self.p)
        except Exception:
            log(self.p, "starting queued detached jobs failed\n" + traceback.format_exc())
        try:   # the jobs of tasks that ended without success, and `ttp detach --stop` requests (jobstop)
            from . import jobstop
            due = time.monotonic() >= getattr(self, "_jobstop_due", 0.0)
            if due:
                self._jobstop_due = time.monotonic() + jobstop.SWEEP_EVERY_S
            for r in jobstop.sweep(self.p) if due else jobstop.serve_requests(self.p):
                log(self.p, r["text"])
        except Exception:
            log(self.p, "stopping the detached jobs of ended tasks failed\n" + traceback.format_exc())
        try:
            pushq.tend_receipts(self.p, self.alert)
        except Exception:
            log(self.p, "recording after_push receipts failed\n" + traceback.format_exc())
        try:
            changed = pushq.tend(self.p, self.cfg, self.alert, may_requeue=self.cfg_status != "unavailable")
        except Exception:
            log(self.p, "push queue: finalize failed\n" + traceback.format_exc())
            return
        now = time.time()
        if changed or now - self._push_refs_at > PUSH_REFS_EVERY_S:
            self._push_refs_at = now
            try:
                pushq.prune_refs(self.p)
            except Exception:
                log(self.p, "push queue: pruning pinned refs failed\n" + traceback.format_exc())
            try:
                closed = pushq.settle_reviewed(self.p.db, now)
                if closed:
                    log(self.p, f"push queue: closed code tasks {closed}: their latest review pushed their work")
            except Exception:
                log(self.p, "push queue: closing shipped code tasks failed\n" + traceback.format_exc())

    def lease_held(self) -> bool:
        """The per-call lease check: no lease configured is today's single host, always the holder."""
        if not leasemod.configured(self.cfg):
            return True
        return bool(self._lease and self._lease.held())

    def tend_lease(self) -> None:
        """Take or renew the project lease (lease.py) before this tick's coordinator turn and dispatch,
        when failover.lease_remote is set. A store it may not use, or one that does not answer, holds
        dispatch (fail-closed). One log line, event and alert per change of state."""
        cfg = self.cfg
        if not leasemod.configured(cfg):
            if self._lease_key is not None:
                self._lease, self._lease_key = None, None
                self._lease_announce("off", "project lease switched off: single-host behaviour")
            return
        key = (str(leasemod.settings(cfg).get("lease_remote")), leasemod.identity(self.p, cfg), leasemod.ttl_s(cfg),
               bool(leasemod.settings(cfg).get("allow_delivery_remote")))
        if key != self._lease_key:
            self._lease_key = key
            try:
                self._lease = leasemod.for_project(self.p, cfg)
            except leasemod.Refused as e:
                self._lease = None
                leasemod.save_refused(self.p, cfg, str(e))
                self._lease_announce("refused", f"project lease: {e}. No dispatch, coordinator turns or pushes "
                                                f"until failover.lease_remote names an allowed store.")
                return
        if not self._lease:
            return
        try:
            what = self._lease.step()
        except Exception:
            log(self.p, "project lease: step failed\n" + traceback.format_exc())
            return
        s = self._lease.snapshot()
        if self._lease.held():
            self._lease_announce("held", f"project lease held here (epoch {s['epoch']}, {what})", severity="low")
        elif what == "rejected":
            self._lease_announce("rejected", f"project lease store rejected the lease write ({s['error']}): this "
                                             f"host does not dispatch until the store accepts {leasemod.REF_PREFIX}*")
        elif what == "unreachable":
            self._lease_announce("unreachable", f"project lease store does not answer ({s['error']}): this host "
                                                f"does not dispatch until a renewal succeeds")
        elif what != "kept":
            holder = s.get("holder_host") if not s.get("released") else None
            self._lease_announce(f"other:{holder}", f"lease held by {holder or 'nobody (released)'} "
                                                     f"({s.get('role') or 'primary'}, epoch {s['epoch']}): "
                                                     f"this host does not dispatch")

    def _lease_announce(self, state: str, text: str, severity: str = "normal") -> None:
        db = self.p.db
        if db.kv(LEASE_STATE_KEY) == state:
            return
        db.set_kv(LEASE_STATE_KEY, state)
        log(self.p, text)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (time.time(), "daemon", "lease", severity, text[:500], "handled"))
        if severity != "low":
            self.alert(f"lease:{state}", text, severity="normal", every_s=0)

    def start_pushes(self) -> None:
        """Start a push batch when one is due (pushq.schedule): local checks only, no model."""
        if not self.lease_held():
            return
        try:
            pushq.schedule(self.p, self.cfg, log=lambda m: log(self.p, m))
        except Exception:
            log(self.p, "push queue: starting a batch failed\n" + traceback.format_exc())

    def _check_sleep(self) -> None:
        """Notice that the host slept: the wall clock jumped ahead of the monotonic one, which stands
        still while the host sleeps. A long gap between ticks alone is not a sleep (a slow tick or a
        stopped daemon), and must not make the runs it overlapped free. Each sleep is kept for a week,
        so a run reaped as lost can tell it overlapped one, and starts the settle hold."""
        wall, mono = time.time(), time.monotonic()
        gap = wall - self._tick_wall
        jump = gap - (mono - self._tick_mono)
        since = self._tick_wall
        self._tick_wall, self._tick_mono = wall, mono
        if jump < SLEPT_MIN_S:
            return
        self._stale_since.clear()   # every supervisor slept too: its lease gets a fresh look once it renews
        if self._lease:
            self._lease.lapse()   # the project lease's monotonic deadline stood still too: held again once renewed
        settle = float(self.cfg["budget"].get("wake_settle_s", 300))
        self._settle_until = mono + settle
        db = self.p.db
        sleeps = [s for s in db.kv("host_sleeps", []) or [] if s[1] >= wall - SLEEPS_KEPT_S]
        db.set_kv("host_sleeps", (sleeps + [[since, wall]])[-100:])
        db.set_kv("settle_until", wall + settle)   # for status: when new work starts, if it stays awake
        log(self.p, f"host slept for {jump:.0f} s (tick gap {gap:.0f} s); "
                    f"nothing new starts for {settle:.0f} s of awake time")
        # The network may not be back with the host: new runs wait until the API hosts resolve.
        for prov in {self.cfg.get("core_provider") or "claude", *[t["provider"] for t in db.q(
                "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status='queued'")]}:
            self.hold_offline(prov, "the host slept")
        if jump >= SLEPT_LONG_S:
            self._record_sleep(since, wall, jump)

    def _record_sleep(self, since: float, woke: float, slept: float) -> None:
        """A long sleep. Running runs need no clock change: their supervisors count timeout and stall
        in awake time. Record it as one `host_slept` event per sleepy stretch (a closed lid wakes in
        the dark many times), and tell the user once a day what keeps the project awake."""
        db = self.p.db
        runs = [r["id"] for r in db.q("SELECT id FROM runs WHERE status='running'")]
        last = db.one("SELECT id, data FROM events WHERE kind='host_slept' ORDER BY id DESC LIMIT 1")
        data = json.loads(last["data"] or "{}") if last else {}
        if last and float(data.get("woke") or 0) >= since - SLEEP_EVENT_MERGE_S:
            data.update(woke=woke, slept_s=round(float(data.get("slept_s") or 0) + slept),
                        sleeps=int(data.get("sleeps") or 1) + 1,
                        runs=sorted(set(data.get("runs") or []) | set(runs)))
            db.x("UPDATE events SET text=?, data=? WHERE id=?",
                 (_sleep_text(data), json.dumps(data), last["id"]))
        else:
            data = {"since": since, "woke": woke, "slept_s": round(slept), "sleeps": 1, "runs": runs}
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                 (woke, "host", "host_slept", "normal", _sleep_text(data), json.dumps(data), "record"))
        self.alert("host_slept",
                   f"{hostname()} slept for {slept / 60:.0f} min and the project stood still meanwhile"
                   f"{f' (runs {_ids(runs)} paused)' if runs else ''}. {_sleep_cause(db.kv(awake.KV))} "
                   f"To keep working the project needs one of: the lid open, power "
                   f"plus an external display (clamshell mode), or an always-on host.",
                   "low", every_s=86400)

    def settling(self) -> bool:
        """The host woke from a sleep less than wake_settle_s of awake time ago: hold new runs."""
        return time.monotonic() < self._settle_until

    def hold_offline(self, prov: str, why: str) -> None:
        """New runs on `prov` wait until its API host resolves again (net_held). A host that reaches
        the API through a proxy resolves nothing itself, so it is never held."""
        if _proxied():
            return
        try:
            host = get_provider(prov).reach_host()
        except Exception:
            host = ""
        if not host:
            return
        self._net_holds[prov] = {"host": host, "since": time.monotonic(), "checked": None, "checking": False,
                                 "up": False, "probe": None}
        log(self.p, f"{prov}: {why}; no new runs start on it until {host} resolves")

    def net_held(self, prov: str) -> bool:
        """Whether new runs on `prov` wait for its API host to resolve. The lookup runs in a thread of
        its own, at most every REACH_EVERY_S, so a tick never waits on DNS. A hold that lasts
        NET_HOLD_MAX_S lets one run try anyway (_net_may_probe); how it ends re-arms or ends it."""
        h = self._net_holds.get(prov)
        if not h:
            return False
        now = time.monotonic()
        if not h["up"] and not h["checking"] and (h["checked"] is None or now - h["checked"] >= REACH_EVERY_S):
            h["checking"], h["checked"] = True, now

            def look(h=h) -> None:
                try:
                    h["up"] = _resolves(h["host"])
                finally:
                    h["checking"] = False
            _background(look)
        if h["up"]:
            self._end_net_hold(prov, f"{h['host']} resolves")
            return False
        return True

    def _net_may_probe(self, prov: str) -> bool:
        """Whether one run on held `prov` may start to try the network: the hold (or its last such
        try) is NET_HOLD_MAX_S old. A run that starts on a held provider is that try (start_run)."""
        h = self._net_holds.get(prov)
        if not h:
            return False
        # No try yet: count from the hold. The monotonic clock starts near 0 at boot, so 0.0 is no "never".
        last = h["since"] if h["probe"] is None else max(h["since"], h["probe"])
        return time.monotonic() - last >= NET_HOLD_MAX_S

    def _end_net_hold(self, prov: str, why: str) -> None:
        if self._net_holds.pop(prov, None):
            log(self.p, f"{prov}: {why}; new runs start on it again")

    def _slept_between(self, start: float, end: float) -> bool:
        return any(a < end and b > start for a, b in self.p.db.kv("host_sleeps", []) or [])

    # runs -----------------------------------------------------------------------------------------
    def _connectors_needed(self, prov, provider: str) -> bool:
        """Whether a non-isolated worker keeps the user's account connectors (see Claude's
        CONNECTORS_OFF): the project lists one in `providers.<p>.mcp_servers`, or one of its runs
        called one (finish_run records it, for good). The first look scans the last 14 days' runs."""
        names = coord.name_list(self.cfg["providers"].get(provider, {}).get("mcp_servers") or [])
        if any(prov.is_connector(n) for n in names):
            return True
        key = f"connectors_used:{provider}"
        seen = self.p.db.kv(key)
        if seen is None:
            used: list[str] = []
            for r in self.p.db.q("SELECT id, dir FROM runs WHERE provider=? AND role!='coordinator' AND started>=?",
                                 (provider, time.time() - 14 * 86400)):
                out = self._run_dir(r) / "output.jsonl"
                try:
                    called_mcp = b'"mcp__' in out.read_bytes()   # parse only the few that did
                except OSError:
                    continue
                if called_mcp:
                    used += prov.parse(out).extra.get("connectors_used") or []
            seen = {"used": sorted(set(used)), "since": time.time()}
            self.p.db.set_kv(key, seen)
        return bool(seen.get("used"))

    def _approved_mcp(self, prov, provider: str, cwd: str) -> dict:
        """The project's allowlisted MCP servers (`providers.<p>.mcp_servers`) for an isolated run,
        looked up for the worktree and the project root."""
        names = coord.name_list(self.cfg["providers"].get(provider, {}).get("mcp_servers") or [])
        names = [n for n in names if not prov.is_connector(n)]   # no file defines one: _connector_deny
        if not names:
            return {}
        found, unknown = prov.mcp_servers(names, self._mcp_dirs(cwd))
        if unknown:
            self.alert(f"mcp_servers_unknown:{provider}",
                       f"Workers run without these MCP servers, which your {provider} config does not define: "
                       f"{', '.join(unknown)}. Fix providers.{provider}.mcp_servers in project.json "
                       "(servers from a plugin cannot be listed).",
                       severity="low", every_s=86400)
        return found

    def _mcp_dirs(self, cwd: str) -> list[str]:
        return list(dict.fromkeys(str(d) for d in (cwd, Path(cwd).resolve(), self.p.root, self.p.root.resolve())))

    def _connector_deny(self, prov, provider: str, cwd: str, plugin_dirs: list[str]) -> list[str] | None:
        """The MCP servers an isolated run keeps out by name so that it can keep the account connectors
        the project lists in `providers.<p>.mcp_servers` (mcpgap.plan); None: the run stays strict, as
        it does for a project that lists none. A listed connector that cannot be kept is a low alert."""
        cfg = self.cfg["providers"].get(provider, {})
        if not any(prov.is_connector(n) for n in coord.name_list(cfg.get("mcp_servers") or [])):
            return None
        from . import mcpgap
        plan = mcpgap.plan(cfg, mcpgap.read(self.p.state), prov.mcp_server_names(self._mcp_dirs(cwd)), plugin_dirs)
        if plan["why"]:
            self.alert(f"mcp_connectors_strict:{provider}",
                       f"Isolated workers run without the claude.ai connectors in providers.{provider}.mcp_servers: "
                       f"{plan['why']}. They stay strict (no MCP server but the listed files) until that clears.",
                       severity="low", every_s=86400)
            return None
        return plan["deny"]

    def start_run(self, role: str, prompt: str, provider: str, tier: str, cwd: str, *, task: dict | None = None,
                  budget_usd: float | None = None, timeout_s: float | None = None, read_only: bool = False,
                  schema: dict | None = None, system: str | None = None, append_system: str | None = None,
                  note: dict | None = None, resume: str | None = None,
                  context: str | None = None, cache_ttl: str = "") -> int:
        """`context` (with `system`): stable text that follows the system prompt, sent as a block of
        its own with a cache breakpoint after it where the provider can mark one (else it ends the
        system prompt). `cache_ttl` (`5m`, `1h`) sets the provider's prompt cache lifetime; `auto`
        picks it per coordinator turn (cachettl.py). Unset, every run but the coordinator's takes
        budget.worker_cache_ttl ("off": none set)."""
        if system is not None and append_system is not None:   # both would be run_dir/system.md
            raise ValueError("start_run takes `system` or `append_system`, not both")
        if self.cfg_status == "unavailable":
            raise RuntimeError("project.json is unreadable with no last good copy: no model work starts")
        if not self.lease_held():
            raise RuntimeError("this host does not hold the project lease: no model work starts")
        if provider in self._net_holds:
            self._net_holds[provider]["probe"] = time.monotonic()   # the one run that tries the network
        tiers = self.cfg["providers"].get(provider, {}).get("tiers", {})
        model = tiers.get(tier, {}).get("model", "")
        effort = tiers.get(tier, {}).get("effort", "")
        if role == "coordinator":
            # Every turn at coordinator.effort, else coordinator.DEFAULT_EFFORT, as the provider takes
            # it; coordinator.model pins the model, so moving its tier's model (say light to a cheaper
            # one for workers) does not move the coordinator with it. Read from the config as it is now.
            route = coord.coordinator_route(self.cfg, provider)
            model, effort = route["model"], route["effort"]
            prompt_bytes = sum(len(text.encode("utf-8")) for text in (prompt, system or "", context or "", append_system or ""))
            timeout_s = coordlimits.turn_timeout(self.cfg, provider, effort, prompt_bytes)
            note = {**(note or {}), "turn": {"prompt_bytes": prompt_bytes, "provider": provider,
                    "model": model, "effort": effort, "timeout_s": timeout_s}}
        pin: dict = {}
        if role != "coordinator" and task:
            # The task's own pin (task_add model/effort) wins over its tier: a tier change (an effort
            # pick, a review's sizing, a retry) moves its budget and timeout, never a pinned model or effort.
            # The runner checks that the agent's session resolves the pin before its first turn (check_pin).
            pin = {k: str(task.get(k)) for k in ("model", "effort") if str(task.get(k) or "")}
            model = pin.get("model") or model
            effort = pin.get("effort") or effort
        prices = (self.cfg.get("pricing") or {}).get(provider) or {}
        prov = get_provider(provider).use(model, prices)
        restrictions = self.cfg.get("restrictions", {})
        if read_only and prov.isolate_read_only:
            cwd = scratch_dir(str(self.p.base))
        denied = coord.tool_list(self.cfg["providers"].get(provider, {}).get("disallowed_tools") or [])
        from .project import secrets_path
        from .web import token_path
        argv, env = prov.build(role=role, model=model, effort=effort, cwd=cwd, budget_usd=budget_usd,
                               read_only=read_only, schema=schema, restrictions=restrictions,
                               disallowed_tools=denied, denied_reads=[str(token_path(self.p).resolve()), str(secrets_path().resolve())],
                               web=role == "coordinator" or web_needed(role, task))
        cache_fp = None
        if role != "coordinator" and not cache_ttl:
            cache_ttl = cachettl.worker_setting(self.cfg.get("budget") or {})
        if cache_ttl == cachettl.AUTO:
            if prov.cache_env("1h"):
                cache_fp = cachettl.fingerprint(system, context, schema, model, effort,
                                                cachettl.cli_stamp(prov.binary()))
                now = time.time()
                cache_ttl, ttl_why = cachettl.choose(
                    self.p.db.kv(cachettl.KEY), cache_fp, now,
                    (self.cfg.get("coordinator") or {}).get("cache_warm_min", cachettl.WARM_MIN),
                    lambda: cachettl.signals(self.p.db, now))
            else:
                cache_ttl, ttl_why = "1h", "the agent sets its own cache lifetime"
        else:
            ttl_why = "configured"
        if role == "coordinator":
            # With the turn's usage, so a before/after comparison can tell the lifetimes apart.
            note = {**(note or {}), "turn": {**(note or {}).get("turn", {}),
                                             "cache_ttl": cache_ttl, "cache_ttl_why": ttl_why}}
        # The environment and any cache mark below take the same lifetime: a mismatch is an API error.
        env = {**env, **prov.cache_env(cache_ttl)}
        thread = ""
        if role == "coordinator" and not resume and prov.cache_key_per_session:
            try:   # the agent's cache follows its session: continue the last turn's while warm
                last = self.p.db.one("SELECT provider, status, session_id, started, ended, note, input_tokens, "
                                     "cache_read_tokens, cache_write_tokens FROM runs WHERE role='coordinator' "
                                     "AND status!='running' ORDER BY id DESC LIMIT 1")
                fp = cache_fp or cachettl.fingerprint(system, context, schema, model, effort,
                                                      cachettl.cli_stamp(prov.binary()))
                thread, why = cachettl.pick_thread(
                    last, fp, provider, time.time(), self.cfg.get("coordinator") or {},
                    lambda s: bool(prov.resume_args(s)) and prov.session_saved(s, cwd, env))
                note = {**(note or {}), "turn": {**(note or {}).get("turn", {}), "thread": why}}
            except Exception as e:   # only saves money: the turn starts a session of its own
                log(self.p, f"coordinator session reuse skipped: {type(e).__name__}: {e}")
                thread = ""
        if role != "coordinator":
            window = self.cfg["budget"].get("compact_window_tokens") or 0   # 0: off for every tier
            window = window.get(tier) if isinstance(window, dict) else window
            env = {**env, **prov.compact_env(int(window or 0))}
            argv = _before_stdin(argv, prov.compact_args(int(window or 0)))
            argv = prov.cap_output(argv, int(self.cfg["budget"].get("bash_output_max_chars") or 0))
        mcp_servers: dict = {}
        resume_extra: list[str] = []
        private: list[str] = []   # files that may hold credentials, removed when the run ends
        sandboxed = False   # the agent fences the worker's writes (see Provider.writable_args)
        if not read_only:
            # Skill plugins this project enabled for its workers only (never the user's own setup).
            dirs = [str(Path(os.path.expanduser(d))) for d in
                    coord.dir_list(self.cfg["providers"].get(provider, {}).get("plugin_dirs") or [])]
            missing = [d for d in dirs if not Path(d).is_dir()]
            if missing:
                self.alert(f"plugin_dirs_missing:{provider}",
                           f"Workers run without these {provider} plugin directories, which do not exist: "
                           f"{', '.join(missing)}. Fix providers.{provider}.plugin_dirs in project.json.",
                           severity="normal", every_s=86400)
            roots = [str(self.p.state)] + worktree.git_dirs(Path(cwd))
            # Short temp dirs (shorttmp): the worker's own, and those of the checks and detached
            # jobs it starts. The root must exist for the sandbox to grant it.
            if shorttmp.ready():
                roots += list(dict.fromkeys([str(shorttmp.root()), str(shorttmp.root().resolve())]))
            # `ttp lock` on a resource shared across projects takes its slot under the user's shared
            # lock root, outside the project: only that directory, not the rest of the user's
            # tt-project home. It must exist for the sandbox to grant it.
            try:
                shared.root().mkdir(parents=True, exist_ok=True)
                roots += list(dict.fromkeys([str(shared.root()), str(shared.root().resolve())]))
            except OSError:
                pass   # `ttp lock` then says plainly that it cannot write the shared lock
            # Likewise the host-wide checks slots (checkcap): `ttp checks` refuses to run uncapped.
            try:
                checkcap.slot_dir().mkdir(parents=True, exist_ok=True)
                roots += list(dict.fromkeys([str(checkcap.slot_dir()), str(checkcap.slot_dir().resolve())]))
            except OSError:
                pass   # `ttp checks` then says plainly that it cannot write the slots
            fence = prov.writable_args(roots)
            sandboxed = bool(fence)
            extra = fence + prov.plugin_args([d for d in dirs if d not in missing])
            if self.cfg["providers"].get(provider, {}).get("worker_isolation"):
                deny = self._connector_deny(prov, provider, cwd, [d for d in dirs if d not in missing])
                kept = prov.keep_connectors(argv, deny) if deny is not None else None
                if kept is None:
                    extra += prov.isolation_args()
                else:
                    argv = kept
                mcp_servers = self._approved_mcp(prov, provider, cwd)
            elif prov.connector_env(False):
                env = {**env, **prov.connector_env(self._connectors_needed(prov, provider))}
            # A continued session (see _resumable) gets the current system prompt again, below. Its
            # arguments go last: Codex takes them as a subcommand that must follow every option.
            resume_extra = prov.resume_args(resume) if resume else []
            argv = _before_stdin(argv, extra)
        if thread:
            resume, resume_extra = thread, prov.resume_args(thread)
            prompt = coord.THREAD_CONTINUED + "\n\n" + prompt
        # The agent's session gets its id here, so the run's session is known (and left out of this
        # machine's other Claude Code spend, localspend.py) before the agent writes a line of it.
        session_id = resume if resume_extra else ""
        if not resume_extra:
            fresh = str(uuid.uuid4())
            got = prov.session_args(fresh)
            if got:
                argv, session_id = _before_stdin(argv, got), fresh
        if task and role != "coordinator":
            # Where the project root's checkout stands as the run starts, so its end can tell what the
            # run left there (check_root_checkout).
            root = worktree.checkout_state(self.p.root) if worktree.is_git(self.p.root) else None
            if root:
                note = {**(note or {}), "root": root}
                self._root_home(root.get("branch") or "")
            if Path(cwd).resolve() == self.p.harness.resolve() and (hh := runtimeload.head(self.p.harness)):
                note = {**(note or {}), "harness_head": hh}   # its end compares (runtimeload.check)
        db = self.p.db
        # Where the worker pid it records means something: a reader elsewhere does not judge it (locks).
        note = {**(note or {}), "pid_where": {"node": os.uname().nodename, "pidns": locks._pid_ns()}}
        run_id = db.x("INSERT INTO runs(task,role,provider,model,effort,account,started,boot_id,status,note,"
                      "session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (task["id"] if task else None, role, provider, model, effort, prov.account(), time.time(),
                       self.boot, "running", json.dumps(note or {}), session_id or None))
        run_dir = self.p.runs / str(run_id)
        prompt = prompt.replace(RUN_DIR_MARK, str(run_dir))
        # Raising from here on means nothing was launched: the run row must not stay "running".
        tmp_env: dict = {}
        try:
            redact.private_dir(run_dir)   # owner-only: it holds the run's output and transcript
            # A fenced worker gets a temp dir of its own (a writable root; never in the worktree): on
            # macOS git's xcrun shim otherwise fails to write its cache, and tools that honour TMPDIR
            # stay off the shared /tmp. A short one (shorttmp), so a socket a test binds beneath it
            # stays within the AF_UNIX path limit; else one under the run dir. run.json records it and
            # the runner removes it when the run ends (remove_private if the runner died).
            if sandboxed:
                tmp = shorttmp.make()
                if not tmp:
                    tmp = run_dir / "tmp"
                    tmp.mkdir(exist_ok=True)
                tmp_env = {var: str(tmp) for var in ("TMPDIR", "TMP", "TEMP")}
            if mcp_servers:
                # Outside the repo and the run directory, owner-only: server entries can carry tokens.
                fd, mcp_path = tempfile.mkstemp(prefix=f"ttp-mcp-{run_id}-", suffix=".json")
                private.append(mcp_path)
                with os.fdopen(fd, "w") as f:
                    json.dump({"mcpServers": mcp_servers}, f)
                argv = prov.with_mcp_config(argv, Path(mcp_path))
            stdin = None
            if system is not None and context is not None:
                cached = prov.cached_input(context, prompt, cache_ttl, log=lambda m: log(self.p, m))
                if cached:
                    argv, stdin = _before_stdin(argv, cached[0]), cached[1]
                else:
                    system = coord.join_prompt(system, context)
            if system is not None:
                (run_dir / "system.md").write_text(system)
                if provider == "claude":
                    argv, inline = _with_system_prompt(prov, argv, run_dir / "system.md")
                    if not inline:   # too long for the command line: it leads what the agent reads
                        if stdin is not None:
                            stdin = _lead_input(stdin, system)
                        else:
                            prompt = system + "\n\n" + prompt
                else:   # no replaceable system prompt: added to the agent's own, or leading the prompt
                    sys_args = prov.append_system_args(run_dir / "system.md")
                    if sys_args:
                        argv = _before_stdin(argv, sys_args)
                    else:
                        prompt = system + "\n\n" + prompt
            if append_system is not None:
                (run_dir / "system.md").write_text(append_system)
                sys_args = prov.append_system_args(run_dir / "system.md")
                if sys_args:
                    argv = _before_stdin(argv, sys_args)
                else:
                    prompt = append_system + "\n\n" + prompt
            argv = _before_stdin(argv, resume_extra)
            (run_dir / "prompt.md").write_text(prompt)
            if stdin is not None:   # what the agent reads; prompt.md stays the readable digest
                (run_dir / "input.jsonl").write_text(stdin)
            runtime_dir = str(Path(__file__).resolve().parent.parent)
            path = f"{service_path()}:{os.environ.get('PATH', '')}"
            # The project's venv, so a fresh worktree need not build one; `ttp` stays first.
            venv = worktree.project_venv(self.p, cwd) if role in ("worker", "reviewer") and not read_only else None
            venv_vars = worktree.venv_env(venv, path) if venv else {}
            env = {**env, **venv_vars, **tmp_env, "TTP_RUN_DIR": str(run_dir), "TTP_PROJECT": str(self.p.base),
                   "TTP_RUN_ID": str(run_id), **task_env(task["id"] if task else ""), "PYTHONPATH": runtime_dir,
                   **({"TTP_LEASE_EPOCH": str(self._lease.epoch)} if self._lease else {}),
                   "TTP_PYTHON": sys.executable,   # `ttp` runs under this, not the venv's python3
                   # Tests run straight in its shell take at most one run's share of the host's test slots.
                   **(testslots.worker_env(self.cfg) if role in ("worker", "reviewer") else {}),
                   # A worker's sandbox may run commands in a PID namespace or cgroup of their own, which
                   # ends with the command: `ttp push --detach` and `ttp detach` compare and have the
                   # daemon start the push or job.
                   **({"TTP_PIDNS": ns} if (ns := push.pid_ns()) else {}),
                   **({"TTP_CGROUP": cg} if (cg := push.cgroup()) else {}),
                   "PATH": f"{self.p.harness / 'bin'}:{venv_vars.get('PATH', path)}"}
            # A task that holds no device runs CPU tests only: hooks (the harness skill's pytest plugin)
            # skip tests that would open a device outside the broker. A device task gets "0", never an
            # inherited "1" (the runner adds its own environment under this one).
            if task and role != "coordinator":
                env["TTP_NO_DEVICE"] = "0" if holds_device(task, self.cfg) else "1"
            # On a machine tagged `device` a direct open sees no device, whatever the task: `ttp lock` on
            # a device, devq and the broker (whose jobs never get the mask) are the ways to one.
            if machines.device_host():
                masked = machines.mask_devices({**os.environ, **env})
                env.update({k: masked[k] for k in (*machines.DEVICE_MASK, machines.MASK_RECORD)})
            env.update(git_fsync_env({**os.environ, **env}))   # a power cut must not corrupt workers' commits
            tout = timeout_s or self.cfg["budget"]["run_timeout_s"].get(tier, 3600)
            stall = self.cfg["budget"].get("stall_s", {}).get(tier) if role != "coordinator" else None
            spec = {"argv": argv, "env": env, "cwd": cwd, "timeout_s": tout, "provider": provider, "stall_s": stall,
                    "model": model, "effort": effort,
                    **({"pin": pin, "pin_verify": (task or {}).get("pin_verify")
                        or self.cfg["runner"]["pin_verify"]} if pin else {}),
                    "prices": prices, **({"stdin": "input.jsonl"} if stdin is not None else {}),
                    "budget_usd": None if prov.caps_budget else budget_usd,
                    # The chat provider's loopback bridge (chatbridge.py): where its key is, never the key.
                    # Its effort_fields follow the effort the run launches at, whatever Claude Code asks for.
                    **({"bridge": chatbridge.settings(self.cfg, tier, model, effort)} if provider == "chat" else {}),
                    # What a run without its own budget is priced at when it reports no usage.
                    "default_budget_usd": self.cfg["budget"].get("task_default_usd", {}).get(tier, 8.0),
                    "exclusive": [{"resource": res, "lock": locks.canonical(self.cfg, res),
                                   "paths": [str(x) for x in self._slot_paths(res)],
                                   "reserve": str(self._reserve_path(res)),
                                   "holder": shared.holder(self.p, res, hold.who(task['id']), self.cfg)}
                                  for res in _exclusive(task)] if task else [],
                    "exclusive_wait_s": self.cfg["budget"].get("exclusive_wait_s", 600),
                    # The project's own holds, asked again before each whole-run take (pregrant.py).
                    **({"pre_grant": {"cmd": pregrant.command(self.cfg), "root": str(self.p.root),
                                      "state": str(self.p.state), "timeout_s": pregrant.timeout_s(self.cfg)}}
                       if task and _exclusive(task) and pregrant.command(self.cfg) else {}),
                    "holds": str(self.p.state / "holds"),   # where a hold kept for detached jobs is registered
                    "private_files": private, "tmp_dir": tmp_env.get("TMPDIR"),
                    # Workers and reviewers run niced, and all they start with them; the coordinator not.
                    "nice": nice_level(self.cfg.get("runner"))[0] if role != "coordinator" else 0}
            durable_write(run_dir / "run.json", json.dumps(spec, indent=1))   # read again after a reboot
            with open(run_dir / "runner.log", "wb") as out:
                proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=runtime_dir,
                                        env={**os.environ, "PYTHONPATH": runtime_dir}, stdout=out, stderr=out,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
        except BaseException:
            db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (time.time(), run_id))
            runner.remove_files(private)
            shorttmp.remove(tmp_env.get("TMPDIR"))
            raise
        try:
            # Its start beside its pid, as child.pid has it: a pid reused later is not this supervisor.
            durable_write(run_dir / "runner.pid", f"{proc.pid}\n{runner.proc_start(proc.pid) or ''}\n")
        except OSError as e:
            log(self.p, f"run {run_id}: could not record its supervisor's start: {e}")
        try:
            db.x("UPDATE runs SET pid=?, dir=? WHERE id=?", (proc.pid, str(run_dir), run_id))
        except Exception as e:   # launched all the same: the reaper finds it by its run id's directory
            log(self.p, f"run {run_id} started but its pid was not recorded: {e}")
        if cache_fp is not None and cache_ttl == "1h":
            try:   # the 1h entries this turn writes are what a later turn within cache_warm_min reads
                db.set_kv(cachettl.KEY, cachettl.record(db.kv(cachettl.KEY), cache_fp))
            except Exception as e:   # bookkeeping: the next turn then goes at 1h again
                log(self.p, f"run {run_id}: prompt cache turn not recorded: {e}")
        log(self.p, f"run {run_id} start role={role} provider={provider} tier={tier} task={task and task['id']}")
        return run_id

    def _run_dir(self, r: dict) -> Path:
        # A row whose start was cut short has no dir recorded; its directory is still named by its id.
        return Path(r["dir"]) if r["dir"] else self.p.runs / str(r["id"])

    def _run_alive(self, r: dict) -> bool:
        """The run's supervisor still lives: it renews the lease, or its process is up on this boot."""
        run_dir = self._run_dir(r)
        if (run_dir / "exit.json").exists():
            return False
        try:
            fresh = time.time() - (run_dir / "lease").stat().st_mtime <= LEASE_STALE_S
        except OSError:
            fresh = False
        return fresh or (self._this_boot(r["boot_id"]) and bool(r["pid"]) and _alive(r["pid"])
                         and self._is_supervisor(r) is not False)

    def _is_supervisor(self, r: dict) -> bool | None:
        """Whether the run's pid is still its supervisor: the start recorded beside it at launch
        matches. None when nothing was recorded (a run started before the record was kept)."""
        try:
            pid_text, _, started = (self._run_dir(r) / "runner.pid").read_text().partition("\n")
            if int(pid_text) == r["pid"] and started.strip():
                return runner.proc_start(r["pid"]) == started.strip()
        except (OSError, ValueError):
            pass
        return None

    def _hung(self, r: dict, up: float) -> str | None:
        """Why a run whose supervisor's pid lives must be ended anyway: "hung" when its lease has been
        stale for LEASE_HUNG_S of awake time (a sleep restarts the count), "overdue" when it runs
        BACKSTOP_SLACK_S of awake time past its own wall clock, waits and stall limit. None otherwise."""
        if not self._this_boot(r["boot_id"]):
            return None
        run_dir = self._run_dir(r)
        try:
            age = time.time() - (run_dir / "lease").stat().st_mtime
        except OSError:
            age = None
        if age is not None and age <= LEASE_STALE_S:
            self._stale_since.pop(r["id"], None)
        elif up - self._stale_since.setdefault(r["id"], up - (LEASE_STALE_S if age is not None else 0)) \
                >= LEASE_HUNG_S:
            return "hung"
        try:
            spec = json.loads((run_dir / "run.json").read_text())
            tout = float(spec.get("timeout_s") or 3600)
            limit = (tout + min(locks.waited(run_dir), tout) + min(float(spec.get("exclusive_wait_s") or 0), tout)
                     + float(spec.get("stall_s") or 0) + BACKSTOP_SLACK_S)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        now = time.time()
        start = float(r["started"] or now)
        asleep = sum(max(0.0, min(b, now) - max(a, start)) for a, b in self.p.db.kv("host_sleeps", []) or [])
        return "overdue" if now - start - asleep > limit else None

    def _end_hung(self, r: dict, why: str) -> None:
        """End a run whose supervisor lives but no longer enforces its limits: its group and the
        agent's, TERM then KILL. The run ends as a stall: an attempt, one low feed line, no turn."""
        pid = r["pid"]
        log(self.p, f"run {r['id']} {why}: its supervisor (pid {pid}) no longer enforces its limits; ending it")
        if pid and _alive(pid) and self._is_supervisor(r):
            _end_group(pid, ORPHAN_GRACE_S)
        self._end_orphan(r)
        self._stale_since.pop(r["id"], None)
        self.finish_run(r, {"rc": -1, "stopped": why, "ended": time.time()})
        task = self.p.db.task(r["task"]) if r["task"] else None
        what = f"#{task['id']} {task['title'][:80]}" if task else r["role"]
        cause = "stopped renewing its lease for 10 min" if why == "hung" else "let it run past its limits"
        then = f"; the task is now {task['status']}" if task else ""
        self.p.db.post("out", f"Run {r['id']} ({what}) was ended by the daemon: its supervisor {cause}{then}.",
                       kind="info", severity="low", ref=f"hung:{r['id']}")

    def _last_sign_of_life(self, r) -> float:
        """When a lost run was last seen working, so downtime is not billed to it: the newest mtime
        of its lease and output, kept between its start and now. Now when neither file exists."""
        now, seen = time.time(), []
        for name in ("lease", "output.jsonl"):
            try:
                seen.append((self._run_dir(r) / name).stat().st_mtime)
            except OSError:
                pass
        if not seen:
            return now
        return min(now, max(max(seen), r["started"] or 0))

    def reap_waiting_checks(self, every_s: float = 60.0) -> None:
        """Stop the detached checks of an ended task's runs (done, failed or changes_needed, cancelled)
        that still wait for a host-wide checks slot: nobody reads their result any more, and each holds
        a place other tasks' checks wait behind. A cancel request (cli.CHECKS_SLOT_CANCEL) names the
        waiter by pid and start time, and only that process honours it, only while it waits: checks
        that hold a slot already run on to their end. Tasks that ended within the last day only."""
        now = time.time()
        if now - getattr(self, "_reaped_checks", 0.0) < every_s:
            return
        self._reaped_checks = now
        from .cli import CHECKS_SLOT_CANCEL, CHECKS_SLOT_WAIT, _checks_alive, _read_checks_pid
        marks = ",".join("?" * len(TERMINAL_TASK_STATES))
        for r in self.p.db.q(f"SELECT runs.id, runs.dir, runs.task, tasks.status FROM runs JOIN tasks ON tasks.id=runs.task "
                             f"WHERE tasks.status IN ({marks}) AND tasks.updated > ?",
                             (*TERMINAL_TASK_STATES, now - 86400)):
            run_dir = self._run_dir(r)
            try:
                wait = json.loads((run_dir / CHECKS_SLOT_WAIT).read_text())
            except (OSError, ValueError):
                continue
            info = _read_checks_pid(run_dir)
            if not isinstance(wait, dict) or wait.get("pid") != info.get("pid") or not _checks_alive(info):
                continue
            req = {"pid": info["pid"], "started": info["started"], "why": f"task #{r['task']} ended ({r['status']})"}
            try:
                if json.loads((run_dir / CHECKS_SLOT_CANCEL).read_text()) == req:
                    continue
            except (OSError, ValueError):
                pass
            try:
                durable_write(run_dir / CHECKS_SLOT_CANCEL, json.dumps(req))
            except OSError as e:
                log(self.p, f"run {r['id']}: waiting checks of ended task #{r['task']} not stopped: {e}")
                continue
            log(self.p, f"run {r['id']}: asked the checks of ended task #{r['task']} (pid {info['pid']}) to stop "
                        "waiting for a host-wide checks slot")

    def reap_runs(self) -> None:
        up = self._awake.tick()
        running = self.p.db.q("SELECT * FROM runs WHERE status='running'")
        for gone in set(self._stale_since) - {r["id"] for r in running}:
            self._stale_since.pop(gone, None)
        for r in running:
            try:
                # Liveness first: a runner writes exit.json before it exits, so a run found dead here
                # has its exit.json by the look below. The other order calls a run that ends between
                # the two looks lost.
                alive = self._run_alive(r)
                exit_file = self._run_dir(r) / "exit.json"
                if exit_file.exists():
                    self.finish_run(r, _read_result(exit_file) or {"rc": -1, "stopped": "lost", "ended": time.time()})
                elif not alive:
                    self._end_orphan(r)
                    self.finish_run(r, {"rc": -1, "stopped": "lost", "ended": self._last_sign_of_life(r)})
                elif why := self._hung(r, up):
                    self._end_hung(r, why)
                self._reap_errors.pop(r["id"], None)
            except Exception:
                # One run whose end cannot be processed must not hold up the others or wedge the loop.
                n = self._reap_errors[r["id"]] = self._reap_errors.get(r["id"], 0) + 1
                log(self.p, f"run {r['id']} reap error {n}: " + traceback.format_exc().replace("\n", " | ")[:2000])
                if n >= 3:
                    self._abandon_run(r)
        self._tell_reboot()
        self._tell_down()

    def _note_down(self) -> None:
        """At start, before the first tick rewrites the heartbeat: keep a down notice when the last
        one is older than daemon.down_notice_s (downtime.note_start), with the cause and the schedule
        runs missed, counted before the first tick runs them."""
        try:
            db, now = self.p.db, time.time()
            hb = heartbeat(self.p) or {}
            last = now - hb["age"] if "age" in hb else None
            prev = db.kv("boot_prev") or {}
            if not self._this_boot(prev.get("boot")):
                prev = {}
            # boot_prev stays for every later start on this boot: only a last heartbeat from an
            # earlier boot says the host rebooted (an older one with no boot id: a reboot not told yet).
            rebooted = not self._this_boot(hb["boot"]) if hb.get("boot") \
                else bool(prev) and not self._this_boot(db.kv("reboot_told"))
            if rebooted:
                how = prev.get("shutdown") or "abrupt"
                if prev.get("prev_boot") and self._planned_cycle(prev["prev_boot"]):
                    how = "planned"
                cause = f"the host rebooted ({how})"
            elif self._this_boot(self._last_stop.get("boot")) and self._last_stop.get("ts"):
                cause = "the daemon was stopped"
            else:
                cause = "the daemon was stopped (it ended without a clean stop)"
            sleeps = (db.kv("host_sleeps", []) or []) + (projpause.windows(db, last, now) if last else [])
            downtime.note_start(db, last, now, downtime.down_notice_s(self.cfg), cause,
                                downtime.missed_runs(db, now, sleeps))
        except Exception:
            log(self.p, "down notice: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def _tell_down(self) -> None:
        """Post the pending down notice once the runs an earlier boot left are reaped, unless the
        reboot notice already carried it."""
        db = self.p.db
        if not self._reboot_told or not db.kv(downtime.DOWN_KEY):
            return
        with db.tx():
            rec = downtime.take(db)
            if not rec:
                return
            fmt = lambda ts: timefmt.short(float(ts), self.p)
            db.post("out", downtime.text(rec, fmt, downtime.cut_short(db, rec)), kind="info", severity="normal",
                    ref=f"down:{int(float(rec['last']))}")

    def check_linger(self) -> None:
        """At start and once a day: alert while the systemd service would not start at boot for lack
        of linger (downtime.py); the alert clears once linger is on again."""
        now = time.time()
        if downtime.checked_ago(self._linger_at, self._linger_off, now):
            return
        self._linger_at = now
        name = downtime.user()
        off = downtime.linger_off(service.installed(self.p), name)
        if off is None:
            return   # loginctl could not tell: the last answer stands
        self._linger_off = off
        db = self.p.db
        if off:
            if not db.kv(downtime.LINGER_KV):
                db.set_kv(downtime.LINGER_KV, {"since": now, "user": name})
            self.alert(downtime.LINGER_KEY, downtime.linger_text(name), severity="normal")
        elif db.kv(downtime.LINGER_KV):
            db.x("DELETE FROM kv WHERE key=?", (downtime.LINGER_KV,))

    def probe_mcp(self) -> None:
        """Refresh this host's connected MCP servers for the status feed (mcpgap.py): `claude mcp list` in
        a thread, bounded and at most every few hours, only while Claude workers run isolated."""
        from . import mcpgap
        if not (self.cfg["providers"].get("claude") or {}).get("worker_isolation") or os.environ.get("TTP_TEST_NO_MCP_PROBE"):
            return   # the test switch: daemons under test never start the real `claude mcp list`
        if self._mcp_probe is not None and self._mcp_probe.is_alive():
            return
        now = time.time()
        if now - self._mcp_tried < mcpgap.RETRY_S or not mcpgap.due(mcpgap.read(self.p.state), now):
            return
        binary = get_provider("claude").binary()
        if not binary:
            return
        state, root = self.p.state, str(self.p.root)
        self._mcp_tried = now

        def work() -> None:
            try:
                e = mcpgap.refresh(state, binary, root)
            except Exception:
                e = {"error": traceback.format_exc().replace("\n", " | ")[-300:]}
            if not e.get("ok"):
                log(self.p, f"claude mcp list: unknown ({e.get('error')})")
        self._mcp_probe = threading.Thread(target=work, daemon=True, name="mcp-probe")
        self._mcp_probe.start()

    def check_notifier(self) -> None:
        """Alert once while this machine's desktop notifier fails to deliver (notifier.DELIVERY: a refused
        or timed-out notification, no backend); it clears once a later delivery succeeds."""
        from . import notifier
        bad = notifier.failing()
        if bad:
            since = timefmt.short(float(bad.get("failing_since") or bad.get("ts") or time.time()), self.p)
            self.alert(notifier.ALERT_KEY, f"Desktop notifications on {hostname()} fail since {since} "
                       f"({bad.get('backend')}: {str(bad.get('error'))[:160]}). Alerts still reach this chat and "
                       f"the web app. `ttp notifier test` on {hostname()} shows the fix.", severity="normal")

    def _tell_reboot(self) -> None:
        """Once per boot, after every run of an earlier boot is reaped: record the boot (when, the
        earlier boot's last heartbeat, the runs it cut short and the resources held then) and tell
        which runs it cut short, what they cost, what became of their tasks and how often the host
        rebooted lately. A restart on the same boot says nothing."""
        db = self.p.db
        if self._reboot_told or db.one("SELECT COUNT(*) n FROM runs WHERE status='running' AND boot_id IS NOT NULL "
                                       "AND boot_id NOT IN (?,?)", self.boots)["n"]:
            return
        with db.tx():
            self._reboot_told = True
            if self._this_boot(db.kv("reboot_told")):
                return
            db.set_kv("reboot_told", self.boot)
            lost = [r for r in db.q("SELECT id, task, role, cost_usd, note FROM runs WHERE status='lost' "
                                    "AND boot_id NOT IN (?,?) AND (note LIKE ? OR note LIKE ?)",
                                    (*self.boots, *(f"%{b}%" for b in self.boots)))
                    if self._this_boot(json.loads(r["note"] or "{}").get("lost_to_reboot"))]
            prev = db.kv("boot_prev") or {}
            if not self._this_boot(prev.get("boot")):
                prev = {}
            if not lost and not prev:
                return   # the first start of this project, or nothing says the host rebooted
            now = time.time()
            usd = sum(float(r["cost_usd"] or 0) for r in lost)
            held = list(prev.get("held") or [])
            booted = self.boot_at
            shutdown = prev.get("shutdown") or "abrupt"
            cycle = self._planned_cycle(prev["prev_boot"]) if prev.get("prev_boot") else None
            # Decided once per reboot: a planned one is told as planned, never as abrupt or unstable.
            how = "planned" if cycle else shutdown
            started = prev.get("initiator") if isinstance(prev.get("initiator"), dict) else None
            # A planned cycle already names its planner: only the journal adds to it there.
            by = f"initiator: {started.get('text') or 'unknown'}" \
                if started and (not cycle or started.get("source") == "journal") else ""
            data = {"boot": self.boot, "boot_time": booted, "prev_boot": prev.get("prev_boot"), "shutdown": shutdown,
                    "initiator": started,
                    "planned": {"reason": str(cycle.get("reason") or "")[:300], "record": cycle["path"],
                                "by": cycle.get("planner") or "", "project": cycle.get("project") or ""} if cycle else None,
                    "last_heartbeat": prev.get("last_heartbeat"), "held": held, "lost_usd": round(usd, 2),
                    "lost": [{"run": r["id"], "task": r["task"], "role": r["role"],
                              "usd": round(float(r["cost_usd"] or 0), 2)} for r in lost]}
            # A record, not news: the coordinator sees it in its digest, not as a new event.
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
                 (min(booted or now, now), "host", "boot", f"boot:{self.boot}", "normal",
                  f"the host rebooted ({how}); {len(lost)} run(s) cut short" + (f"; {by}" if by else ""),
                  json.dumps(data), "record"))
            if not lost:
                return
            boots = db.boots(now - 86400)
            # Only abrupt, unplanned reboots say the host is unstable: an orderly one was a shutdown or an
            # update, a planned one was chosen.
            cut = sum(1 for b in boots if unplanned_abrupt(b) and b.get("lost"))
            severity, unstable = ("low" if cycle else "normal"), ""
            if not cycle and shutdown != "orderly" and cut >= 3 \
                    and now - float(db.kv("host_unstable_told") or 0) >= 86400:
                db.set_kv("host_unstable_told", now)
                severity = "high"
                unstable = (f" The host looks unstable: {cut} abrupt reboots cut runs short in 24 h; check its "
                            f"power, cooling and system logs.")
            then = (f" {by[0].upper()}{by[1:]}." if by else "") \
                + (f" Held at its last heartbeat: {', '.join(held)}." if held else "")
            if cycle:
                then += (f" It was a planned host cycle ({cycle.get('planner') or 'a host-cycle record'}): "
                         f"{data['planned']['reason'] or 'no reason recorded'}.")
            parts = []
            for r in lost:
                task = db.task(r["task"]) if r["task"] else None
                parts.append(f"run {r['id']} (#{task['id']} {task['title'][:60]} → {task['status']})" if task
                             else f"run {r['id']} ({r['role']})")
            nth = ordinal(max(len(boots), 1))
            # A long downtime this start also noted goes out in this one message (downtime.py).
            down = downtime.take(db)
            if down:
                fmt = lambda ts: timefmt.short(float(ts), self.p)
                severity = max(severity, "normal", key=lambda s: SEVERITY_RANK.get(s, 1))
            lead = downtime.text(down, fmt, None) + " " if down else ""
            # Information, never a "needs you" alert: the lost runs are already requeued.
            db.post("out", f"{lead}The host rebooted ({how}; {nth} reboot in 24 h); {len(lost)} run(s) were cut short "
                           f"(${usd:.2f}).{then}{unstable} Runs: {'; '.join(parts)}"[:3000],
                    kind="info", severity=severity, ref=f"reboot:{self.boot}")

    def _end_orphan(self, r: dict) -> None:
        """A supervisor that died (kill -9, OOM) leaves its agent running with no wall clock, budget
        or cancel, beside the retry of its task. End the agent's process group, TERM then KILL."""
        if not self._this_boot(r["boot_id"]):
            return   # a reboot already ended it
        try:
            pid_text, _, started = (self._run_dir(r) / "child.pid").read_text().partition("\n")
            pid, started = int(pid_text), started.strip()
        except (OSError, ValueError):
            return
        if _is_agent(pid, started):
            log(self.p, f"run {r['id']} lost its supervisor; ending its agent (pid {pid})")
            _end_group(pid, ORPHAN_GRACE_S)
        elif _run_group_left(pid, self._run_dir(r)):
            log(self.p, f"run {r['id']} lost its supervisor and its agent; ending the agent's tools (group {pid})")
            _end_group(pid, ORPHAN_GRACE_S)

    def _abandon_run(self, r: dict) -> None:
        """Last resort for a run whose end keeps failing to process: close it so it cannot block the
        loop. Its task fails with the reason and the coordinator decides; a coordinator turn backs off."""
        db, now = self.p.db, time.time()
        why = f"run {r['id']} ended but its result could not be processed (details in the daemon log)"
        try:
            source = self._source_for(r)
        except Exception:
            source = f"task:{r['task']}" if r["task"] else r["role"]
        try:
            with db.tx():
                # Book what the run was priced at so far, once: only while it is still running.
                cur = db.one("SELECT * FROM runs WHERE id=? AND status='running'", (r["id"],))
                if not cur:
                    self._reap_errors.pop(r["id"], None)
                    return
                db.x("UPDATE runs SET status='failed', ended=? WHERE id=?", (now, r["id"]))
                cost = cur["cost_usd"] or 0
                if cost:
                    db.spend(r["provider"], cost, source, account=r["account"] or "",
                             estimated=bool(cur["cost_estimated"]), ts=now)
                    if r["task"]:
                        db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (cost, r["task"]))
                if r["role"] == "coordinator":
                    self._coordinator_failed(why)
                elif r["task"] and (db.task(r["task"]) or {}).get("status") == "running":
                    db.update_task(r["task"], status="failed", blocked_reason=why)
                    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                         (now, f"task:{r['task']}", "task_failed", "normal", f"#{r['task']}: {why}", "queued",
                          r["task"]))
            self._reap_errors.pop(r["id"], None)
            log(self.p, f"run {r['id']} abandoned after repeated reap errors")
        except Exception:
            log(self.p, f"run {r['id']} could not be abandoned: " + traceback.format_exc().replace("\n", " | ")[:2000])

    def reconcile_tasks(self) -> None:
        """Repair task states nothing else would. A task marked running with no live run (the daemon
        stopped between marking it and starting the run) goes back to the queue with no attempt spent.
        A queued task whose dependency failed, was cancelled or does not exist is blocked with the
        reason instead of waiting forever. That event does not start a turn: the dependency's own
        event already does, and a turn per block could ping-pong with a requeue."""
        from . import checkgaps
        checkgaps.rerun(self)
        db, now = self.p.db, time.time()
        for t in db.q("SELECT * FROM tasks WHERE status='running' AND NOT EXISTS "
                      "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')"):
            last = db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (t["id"],))
            if last and self._run_alive(last):
                continue
            with db.tx():
                db.update_task(t["id"], status="queued")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, "daemon", "task_requeued", "low", f"#{t['id']} {t['title']} was marked running with no "
                      f"live run; queued again, no attempt spent", "handled", t["id"]))
            log(self.p, f"task {t['id']} requeued: marked running with no live run")
        # A cancel saves the task first and then asks its runs to stop; a crash in between must not
        # leave a cancelled task's run spending until its own limits end it.
        for r in db.q("SELECT runs.id, runs.dir FROM runs JOIN tasks ON tasks.id=runs.task "
                      "WHERE runs.status='running' AND tasks.status='cancelled'"):
            run_dir = self._run_dir(r)
            if run_dir.is_dir() and not (run_dir / "STOP").exists():
                try:
                    runner.request_stop(run_dir)
                    log(self.p, f"run {r['id']} of a cancelled task asked to stop")
                except OSError as e:
                    log(self.p, f"run {r['id']} of a cancelled task could not be asked to stop: {e}")
        for t, dep, why in db.dead_dependencies():
            reason = f"dependency #{dep} {why}" if dep is not None else "a dependency is not a task id"
            with db.tx():
                db.update_task(t["id"], status="blocked", blocked_reason=reason)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, "daemon", "task_blocked", "normal", f"#{t['id']} {t['title']} is blocked: {reason}. "
                      f"The coordinator can re-point it with task_update depends_on (an empty list clears it), "
                      f"requeue it once the dependency is redone, or cancel it.", "handled", t["id"]))
        self._raise_hold_loops(now)
        self._raise_missing_task_refs(now)
        self._raise_review_stalls(now)
        self._raise_unhandled_failed_reviews(now)

    def _raise_review_stalls(self, now: float) -> None:
        """Only 'done' satisfies a dependency, so a task left in 'review' holds its dependents back
        without ever looking dead. Past coordinator.review_stall_s it is raised once per review stint."""
        db = self.p.db
        try:
            stall_s = float(self.cfg["coordinator"].get("review_stall_s", 14400) or 0)
        except (TypeError, ValueError):
            stall_s = 14400.0
        for rev, since, deps in db.stalled_reviews(stall_s, now):
            fp = f"review_stall:{rev['id']}:{since:.0f}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            names = ", ".join(f"#{t['id']} {t['title'][:80]}" for t in deps[:5]) + (
                f" and {len(deps) - 5} more" if len(deps) > 5 else "")
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "review_stall", fp, "normal",
                  f"#{rev['id']} {rev['title']} has been in review {(now - since) / 3600:.1f}h; queued {names} "
                  f"depend on it and cannot start until it is done. Options: get it reviewed, mark it done with "
                  f"task_update status done once its work is verified, requeue it, re-point the dependents with "
                  f"task_update depends_on, or cancel them.", "queued", rev["id"]))
            log(self.p, f"task {rev['id']} in review {(now - since) / 3600:.1f}h holds {len(deps)} task(s); "
                        f"raised to the coordinator")

    def _raise_unhandled_failed_reviews(self, now: float) -> None:
        """A task left in 'review' whose latest review failed or asked for changes, with no fix or
        re-review open for it, waits on a decision nobody is making. Past
        coordinator.review_failed_unhandled_s after that review ended it is raised once per failed
        review, as a tricky-turn trigger; the coordinator queues the fix, never this check."""
        db = self.p.db
        try:
            wait_s = float(self.cfg["coordinator"].get("review_failed_unhandled_s", 3600) or 0)
        except (TypeError, ValueError):
            wait_s = 3600.0
        in_review = db.q("SELECT * FROM tasks WHERE status='review'") if wait_s > 0 else []
        if not in_review:
            return
        ended = {r["task"]: r["ts"] for r in db.q(
            "SELECT task, MAX(ts) ts FROM events WHERE kind IN ('task_failed','task_changes_needed') "
            "AND task IS NOT NULL AND ts>? GROUP BY task", (now - reviewcap.LOOKBACK_S,))}
        failed = [r for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status='failed' ORDER BY id DESC")
                  if r["id"] in ended and now - ended[r["id"]] >= wait_s]
        if not failed:
            return
        index = BranchIndex(in_review)

        def covers(review: dict) -> set[int]:
            return reviewed_ids(review, index) | (set(dependency_ids(review)) & index.ids)
        open_ = db.q("SELECT * FROM tasks WHERE kind IN ('code','review') AND status NOT IN (?,?,?)",
                     TERMINAL_TASK_STATES)
        done: set[int] = set()
        for rev in failed:
            tids = sorted(covers(rev) - done)
            done.update(tids)   # newest review first: an older failed review of these is not their latest
            later = db.q("SELECT * FROM tasks WHERE kind='review' AND id>?", (rev["id"],)) if tids else []
            for tid in tids:
                fp = f"review_failed_unhandled:{rev['id']}:{tid}"
                if any(tid in covers(r) for r in later) or db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                    continue
                if any(t["id"] != tid and reviewcap.links(t) & {tid, rev["id"]} for t in open_):
                    continue
                task = next(t for t in in_review if t["id"] == tid)
                fups = [str(f.get("title") or "")[:120] for f in load_result(rev.get("result")).get("followups") or []
                        if isinstance(f, dict)]
                named = "; ".join(f"{n}. {f}" for n, f in enumerate(fups[:5], 1)) + (
                    f"; and {len(fups) - 5} more" if len(fups) > 5 else "")
                verdict = "asked for changes" if task_outcome(rev) == CHANGES_NEEDED else "failed"
                age = (now - ended[rev["id"]]) / 3600
                db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (now, "daemon", "review_failed_unhandled", fp, "normal",
                      f"#{tid} {task['title']} is still in review: its latest review #{rev['id']} {verdict} "
                      f"{age:.1f}h ago and no fix or re-review is queued or running. Review follow-ups: "
                      f"{named or 'none listed'}. Options: queue a code task that fixes them and continues the failed "
                      f"review #{rev['id']} (task_add continues={rev['id']}: it builds on the reviewed branch; "
                      f"#{tid} itself cannot be continued while in review), then a re-review, "
                      f"mark it done with task_update status done once its work is verified, requeue it, "
                      f"or cancel it.", json.dumps({"review": rev["id"], "followups": fups}), "queued", tid))
                log(self.p, f"task {tid} in review {age:.1f}h after review {rev['id']} {verdict} with no fix "
                            f"queued; raised to the coordinator")

    def task_probes(self, task: dict) -> list[tuple[str, str]]:
        """(what, probe) of every probe `task` waits on: its start_when, a hold's `waits_on when`, and
        a waiting task's retry_when."""
        out = []
        when = deferral(task).get("when")
        if when:
            out.append(("start_when", when))
        held = anchors.anchor(task) if task["status"] == "blocked" else None
        if held and held[0] == "when":
            out.append(("waits_on when", held[1]))
        prev = load_result(task["result"]) if task["status"] == "queued" and not when else {}
        if prev.get("status") == "waiting" and isinstance(prev.get("retry_when"), str) and prev["retry_when"].strip():
            out.append(("retry_when", prev["retry_when"].strip()))
        return out

    def _raise_missing_task_refs(self, now: float) -> None:
        """A task whose probe waits on a task id this project does not have (`landed:#0`, set before the
        coordinator refused such ids) would wait forever: raised once per task and probe. Its probe is not
        run meanwhile (probe_deferred), and status shows it as broken (coordinator.starts_text). A missing
        depends_on id is reconcile_tasks' dead dependency."""
        db = self.p.db
        rows = db.q("SELECT * FROM tasks WHERE status IN ('queued','blocked') AND (labels LIKE '%start_when:%' "
                    "OR labels LIKE '%waits:when:%' OR result LIKE '%retry_when%')")
        for t in rows:
            for what, probe in self.task_probes(t):
                missing = landed.missing_task_refs(db, probe)
                if not missing:
                    continue
                fp = f"missing_task_ref:{t['id']}:{hashlib.sha256(probe.encode()).hexdigest()[:12]}"
                if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                    continue
                ids = landed.missing_words(missing)
                db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                     (now, "daemon", "missing_task_ref", fp, "normal",
                      f"#{t['id']} {t['title']} waits on {ids}, which does not exist: its {what} "
                      f"{probe[:200]} can never pass. Decide: re-point it at an existing task (task_update "
                      f"{'waits_on when:<probe>' if what == 'waits_on when' else 'start_when'}), clear it "
                      f"(start_when \"\" or requeue), or cancel the task.", "queued", t["id"]))
                log(self.p, f"task {t['id']} {what} waits on missing task {ids}; raised to the coordinator")

    def _raise_hold_loops(self, now: float) -> None:
        """Holds nothing will move (holdloops): a time-only hold with no producer, a dependency on a
        blocked, failed or cancelled task, a dependency cycle. Each is raised once as an event that
        wakes the coordinator while it lasts (a dead or blocked dependency only after a coordinator
        turn has seen it, as the block itself does not wake one) and cleared once resolved."""
        db = self.p.db
        with db.tx():
            due = holdloops.due(db, self.cfg, now)
            for key, e in due:
                db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) "
                     "VALUES(?,?,?,?,?,?,?,?)", holdloops.event_row(key, e, now))
        for key, _ in due:
            log(self.p, f"hold loop {key} raised to the coordinator")

    def relay_escalations(self) -> None:
        """Each live worker run's new `ttp notify --severity high` lines become high coordinator events
        now, not when the run ends (relay_worker_escalations); a file is read again only once it changed."""
        running = self.p.db.q("SELECT id, task, dir FROM runs WHERE status='running' AND role!='coordinator' "
                              "AND task IS NOT NULL")
        self._escalated = {k: v for k, v in self._escalated.items() if k in {r["id"] for r in running}}
        for r in running:
            try:
                st = (self._run_dir(r) / ESCALATE_FILE).stat()
            except OSError:
                continue
            if self._escalated.get(r["id"]) == (st.st_size, st.st_mtime_ns):
                continue
            task = self.p.db.task(r["task"])
            if task:
                relay_worker_escalations(self.p, task, r["id"], self._run_dir(r))
            self._escalated[r["id"]] = (st.st_size, st.st_mtime_ns)

    def meter_running(self, every_s: float = 60) -> None:
        """Price runs still going from their stream, so status and the caps see a long run's spend
        before it ends. `finish_run` replaces the figures with the final ones."""
        now = time.time()
        running = self.p.db.q("SELECT * FROM runs WHERE status='running'")
        self._metered = {k: v for k, v in self._metered.items() if k in {r["id"] for r in running}}
        for r in running:
            out = self._run_dir(r) / "output.jsonl"
            try:
                size = out.stat().st_size
            except OSError:
                continue
            seen = self._metered.get(r["id"])
            if seen and (seen[0] == size or now - seen[1] < every_s):
                continue
            self._metered[r["id"]] = (size, now)
            try:
                usage = get_provider(r["provider"]).parse(out)
                self._priced(r, usage)
                cost = self._net_of_session(r, usage)
            except Exception:
                continue
            # The tokens also show a run on a logged-out provider got past the login (alerts.holds).
            self.p.db.x("UPDATE runs SET cost_usd=?, cost_estimated=1, input_tokens=?, output_tokens=?, "
                        "cache_read_tokens=? WHERE id=? AND status='running'",
                        (cost, usage.input_tokens or 0, usage.output_tokens or 0, usage.cache_read_tokens or 0,
                         r["id"]))
            self._suggest_split(r, usage.cache_read_tokens or 0)

    def _suggest_split(self, r: dict, reread: int) -> None:
        """Once a worker's run has re-read more context than `budget.split_reread_tokens` allows for
        its tier, tell it (through steer.md, once) to finish the step it is on and hand the rest on
        as a follow-up: a fresh run starts from a small context instead of re-reading a huge one on
        every call."""
        if r["role"] == "coordinator" or not r["task"] or not r["dir"]:
            return
        limit = self.cfg["budget"].get("split_reread_tokens") or 0   # 0: off for every tier
        task = self.p.db.task(r["task"]) or {}
        if isinstance(limit, dict):
            limit = limit.get(task.get("tier") or "standard") or 0
        if not limit or reread < int(limit):
            return
        head = (f"This run has re-read about {reread / 1e6:.1f} M tokens of context (the split line for its tier "
                f"is {int(limit) / 1e6:.1f} M); every further call re-reads it all again. ")
        if r["role"] == "reviewer" or task.get("kind") == "review":
            # A review's `done` approves and its followups are blocking findings: it cannot hand half on.
            text = head + ("A review does not split: finish it in as few further calls as you can, reading "
                           "only what is still unchecked. If you already have blocking findings, hand off "
                           "`failed` with them now. Never hand off `done` without having checked the whole change.")
        else:
            text = head + ("Finish the step you are on, commit, and hand off: `done` with a `followups` entry "
                           "(title starting `continue:`) whose spec is self-contained (what is done, the branch "
                           "and head, the test ids you added or touched, what is left and the exact next "
                           "command to run), or `waiting` if that fits. A hop the daemon queues gets the "
                           "branch's changed files and line ranges added to its spec. Do not start "
                           "new large steps in this run.")
        coord._append_update(Path(r["dir"]) / "steer.md", text, f"split-{r['id']}")
        # When the steer fired and the re-read then, once: `ttp stats` reports what the run re-read
        # after it and how it handed off (finish_run adds that).
        row = self.p.db.one("SELECT note FROM runs WHERE id=?", (r["id"],)) or {}
        note = json.loads(row.get("note") or "{}")
        if "split" not in note:
            note["split"] = {"ts": time.time(), "cache_read": reread, "limit": int(limit)}
            self.p.db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps(note), r["id"]))
            r["note"] = json.dumps(note)

    def _priced(self, r: dict, usage) -> float:
        if usage.estimated and not usage.cost_usd:
            usage.cost_usd = bud.estimate_cost(self.p.db, self.cfg, r["provider"], r["model"] or "", {
                "input": usage.input_tokens, "output": usage.output_tokens,
                "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        return usage.cost_usd

    def _net_of_session(self, r: dict, usage) -> float:
        """A resumed Claude Code session reports the whole session's total_cost_usd, not this run's:
        book only what this run added beyond the earlier runs of the session. Netted only when the
        report covers what those runs booked and the difference agrees with this run's own tokens
        priced at the project's rate; even then never below that price. Otherwise the full report
        is booked: over-booking is the safe failure, under-booking lets spend run away."""
        session = (json.loads(r["note"] or "{}").get("resumes") or {}).get("session")
        reported = float(usage.cost_usd or 0)
        basis = usage.extra.get("cost_basis")   # chat runs (providers/chat.py)
        if not session or reported <= 0 or basis == "pricing" or (usage.estimated and basis != "fallback"):
            return usage.cost_usd
        # By runs.session_id; rows from before that column fall back to the session id in their note.
        booked = sum(float(o["cost_usd"] or 0) for o in self.p.db.q(
            "SELECT cost_usd, session_id, note FROM runs WHERE id<? AND task IS ? AND "
            "(session_id=? OR (session_id IS NULL AND note LIKE ?))",
            (r["id"], r["task"], session, f"%{session}%"))
            if o["session_id"] == session or json.loads(o["note"] or "{}").get("session_id") == session)
        if booked <= 0:
            return usage.cost_usd
        own = bud.estimate_cost(self.p.db, self.cfg, r["provider"], r["model"] or "", {
            "input": usage.input_tokens, "output": usage.output_tokens,
            "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens})
        delta = reported - booked
        if delta >= -1e-6 and delta >= own * SESSION_NET_AGREE - SESSION_NET_SLACK_USD:
            usage.extra["session_cost_usd"] = reported
            usage.cost_usd = round(max(delta, own, 0.0), 6)
        return usage.cost_usd

    def finish_run(self, r: dict, exit_info: dict) -> None:
        db, p = self.p.db, self.p
        held = ""
        if r["role"] != "coordinator" and r["task"]:
            # A run that changed the daemon's own runtime in the harness checkout is judged after the
            # restart it asks for, under its new code; held if that code did not load (runtimeload).
            try:
                act, held = runtimeload.check(p, r, self._loaded_at, self._loaded.get("runtime_tree"))
            except Exception:   # git failing here never costs the run its end: judged as before
                log(p, f"run {r['id']}: runtime reload check failed\n" + traceback.format_exc())
                act, held = "go", ""
            if act == "defer":
                if r["id"] not in self._reload_waits:
                    self._reload_waits.add(r["id"])
                    log(p, f"run {r['id']} of task {r['task']}: {held}")
                return
        run_dir = self._run_dir(r)
        runner.remove_private(run_dir)   # the runner removes them too, unless it died first
        try:   # the runner masks them too, unless it died first: before the output or hand-off is read
            redact.scrub_run(run_dir)
        except Exception:
            log(p, f"run {r['id']}: masking stored secrets failed\n" + traceback.format_exc())
        prov = get_provider(r["provider"]).use(r["model"] or "", (self.cfg.get("pricing") or {}).get(r["provider"]))
        usage = prov.parse(run_dir / "output.jsonl", run_dir / "stderr.log")
        if exit_info.get("launch_auth"):
            # The chat bridge had no usable key (none saved, or saved for another URL): the provider's
            # auth gate holds it like a logout, never a launch failure that costs attempts.
            usage.auth_failed, usage.error = True, usage.error or str(exit_info.get("launch_error") or "")[:400]
        if usage.extra.get("connectors_used"):
            key = f"connectors_used:{r['provider']}"
            seen = db.kv(key) or {"since": time.time()}
            db.set_kv(key, {**seen, "used": sorted(set(seen.get("used") or []) | set(usage.extra["connectors_used"]))})
        self._priced(r, usage)
        self._net_of_session(r, usage)
        stopped = exit_info.get("stopped")
        refused = str(exit_info.get("refused") or "")
        if refused and not usage.error:
            usage.error = str(exit_info.get("error") or refused)[:400]
        # A long sleep can drop the CLI's login under a run ("Not logged in" after hours of a closed
        # lid). That run is lost to the sleep; a real log-out shows again on the next run.
        slept_auth = usage.auth_failed and float(exit_info.get("slept_s") or 0) >= SLEPT_LONG_S
        slept = slept_auth or self._slept_during(r, exit_info)
        # A resume that failed on its own and reported no tokens never got going, even if it printed
        # events: it costs nothing, so it ends as the free fallback to a fresh start (_finish_worker).
        # One a host sleep cut is lost to the sleep instead, and keeps its session.
        failed_resume = not stopped and not slept and (exit_info.get("rc") != 0 or usage.error) \
            and not _has_tokens(usage) and _resume_never_started(json.loads(r["note"] or "{}"), usage)
        if usage.estimated and not usage.cost_usd and not failed_resume:
            usage.cost_usd = _cut_off_cost(run_dir, exit_info, usage, prov)
        # A run routed to another provider (route.py): booked at its pricing rows, else zero and unpriced.
        routed = route.of(r["note"]) if r["role"] == "worker" else None
        on_route = bool(routed) and r["provider"] == routed.get("to")
        priced = route.price(self.cfg, r["provider"], r["model"] or "", usage) if on_route else None
        status = "ok" if exit_info.get("rc") == 0 and not usage.error else "failed"
        if stopped in ("timeout", "budget", "stopped", "lost", "stalled", "shutdown", "resource_busy"):
            status = stopped if stopped != "stopped" else "killed"
        elif stopped in HUNG_STOPS:
            status = "stalled"   # its supervisor broke: an attempt like a stall, retried without a turn
        cut_off = None
        if r["role"] != "coordinator" and r["task"] and not (run_dir / RESULT_FILE).exists():
            # Before handed_off: a hand-off misplaced in an earlier run dir also spares this run the
            # waste count. Once copied in, _finish_worker finds it and does not adopt it again.
            task = db.task(r["task"])
            if task:
                self._adopt_misplaced_handoff(r, task, run_dir)
        handed_off = r["role"] != "coordinator" and \
            (_read_result(run_dir / RESULT_FILE) or {}).get("status") in HANDOFF_STATES
        if status == "timeout" and handed_off:
            cut_off, status = status, "ok"   # it handed off before the clock ran out: the work is done, not wasted
        if usage.limited:
            status = "limit"
        if usage.auth_failed and not slept_auth:
            status = "auth"
        if status == "failed" and usage.request_error and not failed_resume:
            # The API rejected the request itself (a 400 or a bad schema): a retry, at any tier, fails
            # the same way. The task fails at once (_finish_worker); a resume still starts fresh.
            status = "request_error"
        fell = route.fell_back(status, exit_info) if on_route and not handed_off else ""
        # A routed run that ended without a hand-off on its own (timed out, failed, no valid result.json)
        # also falls back, unless a host sleep cut it (lost to the sleep, as any run).
        own = route.own_cause(status) if on_route and not handed_off and not fell and not slept else ""
        fell = fell or own
        if fell and status not in ("limit", "auth"):
            status = "fallback"   # the endpoint or the routed run failed: no attempt, the same tier on the core provider
        net_lost = status in SLEEP_CUT and bool(NET_LOST_RE.search(usage.error or ""))
        if net_lost:
            self.hold_offline(r["provider"], f"run {r['id']} could not reach its API")
        elif r["provider"] in self._net_holds and (status == "ok" or _has_tokens(usage)):
            self._end_net_hold(r["provider"], f"run {r['id']} reached its API")
        note = json.loads(r["note"] or "{}")
        if routed:
            note[route.KEY] = {**routed, **({"fallback": fell} if fell else {}),
                               **({"cause": _route_cause(stopped or status, exit_info, usage, run_dir)} if own else {}),
                               **({"priced": priced} if priced is not None else {}),
                               "wall_s": round(float(exit_info.get("ended") or time.time()) - float(r["started"] or 0), 1)}
            sent = route.effort_entry(exit_info.get("bridge") or {}) if on_route else None
            if sent:
                note[route.KEY]["effort"] = sent
                if sent.get("rejected") and not db.kv(seen := f"{route.REJECTED}{r['model']}|{sent['effort']}"):
                    db.set_kv(seen, {"run": r["id"], "ts": time.time()})   # logged once per model and effort
                    log(self.p, f"route: {r['model']} rejected its effort_fields at effort "
                                f"{sent['effort'] or '-'} ({', '.join(sent['fields'])}); run {r['id']} went on without them")
            if fell and not own:   # an endpoint fault cools the endpoint; a run's own end only its task
                route.cool(db, self.cfg, routed["to"], fell, r["id"], time.time())
        if "split" not in note:   # the split steer may have fired after `r` was read
            split = json.loads((db.one("SELECT note FROM runs WHERE id=?", (r["id"],)) or {}).get("note") or "{}")
            if isinstance(split.get("split"), dict):
                note["split"] = split["split"]
        if isinstance(note.get("split"), dict):
            note["split"]["handoff"] = (_read_result(run_dir / RESULT_FILE) or {}).get("status") or "none"
        if stopped in HUNG_STOPS:
            note["stopped"] = stopped
        if usage.session_id:
            note["session_id"] = usage.session_id   # a run the host takes away resumes it (_resumable)
        if refused:
            note.update(refused=refused, not_waste="refused")   # never started a model turn: spent nothing
        launch_error = str(exit_info.get("launch_error") or "")
        if launch_error and exit_info.get("launch_auth"):
            note["launch_error"] = launch_error   # the auth gate's (above), not a harness launch failure
        elif launch_error:
            # The runner could not exec the agent (runner.launch_error): a failed run, never a lost
            # one, and one observation per provider and role while it keeps failing the same way.
            usage.error = usage.error or launch_error
            note["launch_error"] = launch_error
            log(p, f"run {r['id']} ({r['role']}, {r['provider']}) never started: {launch_error}")
            self.observe("daemon:launch", f"{r['role']} run {r['id']} on {r['provider']} never started, a harness "
                         f"launch failure: {launch_error}", "normal", whole=True,
                         key=f"launch:{r['provider']}:{r['role']}")
        try:   # the model and effort the agent's session resolved for a pinned task (runner.check_pin)
            session = exit_info.get("session") or json.loads((run_dir / "run.json").read_text()).get("session")
        except (OSError, ValueError, AttributeError):
            session = None
        if isinstance(session, dict):
            note["session"] = {k: session[k] for k in ("model", "effort", "error", "verification", "pin_verify", "checked")
                               if k in session}
        note["cache_telemetry"] = usage.extra.get("cache_telemetry", {"complete": False, "requests": None})
        if "session_cost_usd" in usage.extra:
            note["session_cost_usd"] = usage.extra["session_cost_usd"]   # reported; cost_usd is this run's
        # The runaway guard counts runs that ended without an outcome; a reboot, a host sleep or a
        # hand-off that stands is an outcome, not a loop.
        if status == "lost" and r["boot_id"] and not self._this_boot(r["boot_id"]):
            note.update(not_waste="reboot", lost_to_reboot=self.boot, boot_at=self.boot_at,
                        shutdown=self._shutdown(r["boot_id"]))
        elif status in bud.WASTED and handed_off:
            note["not_waste"] = "handoff"
        elif net_lost:
            # The API host did not resolve: the network went away under the run, not the task.
            note.update(not_waste="network", lost_to_network=True)
            if slept:
                note["slept_s"] = exit_info.get("slept_s")   # most of its time asleep, not stuck
            status = "lost"
        elif slept_auth or (status in SLEEP_CUT and slept):
            # A run that overlapped a host sleep did not time out or fail on its own: the host went
            # away under it. It is lost to the sleep, like a run lost to a reboot. A resume cut this
            # way keeps its session (below), so this comes before the fresh-start fallback.
            note.update(not_waste="sleep", lost_to_sleep=True, slept_s=exit_info.get("slept_s"))
            status = "lost"
        elif status == "failed" and _resume_never_started(note, usage) and not refused:
            note["not_waste"] = "resume"   # nothing ran: the task starts fresh (_finish_worker)
        if status == "lost" and not note.get("session_id") and (note.get("resumes") or {}).get("session"):
            # A resume the sleep or network cut keeps its session for the next resume, not a fresh start.
            note["session_id"] = note["resumes"]["session"]
        source = self._source_for(r)
        # Spend is booked at the run's end, not when the daemon gets to it: a run reaped after
        # downtime must not count toward the current hour. A stamp from the future is clamped.
        ended = min(float(exit_info.get("ended") or time.time()), time.time())
        # Where the project root's checkout stands now (check_root_checkout), read before the
        # transaction: git can take a while in a large repository.
        root_before = note.get("root") if r["role"] != "coordinator" and r["task"] else None
        try:
            root_now = worktree.checkout_state(p.root) if root_before else None
        except Exception:
            root_now = None
        root_wake = None
        if root_now:
            try:   # a run in the root that newly dirtied tracked paths there is woken once (_root_wake)
                root_before, paths = self._root_wake(r, run_dir, status, root_before, root_now)
                if paths:
                    root_wake = {"paths": paths, "before": root_before, "now": root_now}
                    root_before = {**root_before, "dirty": sorted(set(root_before["dirty"]) | set(paths))}
            except Exception:
                log(p, f"run {r['id']}: judging its project root paths failed\n" + traceback.format_exc())
        if r["role"] != "coordinator" and r["task"]:
            self._prefetch_remote(r["task"], _read_result(run_dir / RESULT_FILE), run_dir)
            if status == "ok":
                self._dry_run_on_pass(r, run_dir, usage.session_id or note.get("session_id") or "")
        # The run's end, its spend and what it did to its task commit together: a daemon stopped
        # half way leaves the run "running", and the next tick processes it again from disk.
        with db.tx():
            db.x("UPDATE runs SET ended=?, status=?, exit_code=?, cost_usd=?, cost_estimated=?, input_tokens=?, "
                 "output_tokens=?, cache_read_tokens=?, cache_write_tokens=?, note=?, "
                 "session_id=COALESCE(NULLIF(?,''), session_id) WHERE id=?",
                 (ended, status, exit_info.get("rc"), usage.cost_usd,
                  int(usage.estimated), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                  usage.cache_write_tokens, json.dumps(note), usage.session_id or "", r["id"]))
            db.spend(r["provider"], usage.cost_usd, source, account=r["account"] or "", estimated=usage.estimated,
                     tokens_in=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
                     tokens_out=usage.output_tokens, ts=ended)
            if r["task"]:
                db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (usage.cost_usd, r["task"]))
                runtimeload.clear(p, r["id"])   # finished once: a restart after this never finishes it again
            self._record_readings(db, r["provider"], r["account"] or "", usage, ended)
            if usage.limited:
                until = time.time() + 3600
                db.set_kv(f"limited:{r['provider']}", {"until": until, "note": usage.limit_note})
                # Only an account out of funds needs the user; a usage or rate limit lifts by itself,
                # so it is a self-clearing status line (alerts.STATUS_CONDITIONS), not "needs you".
                if FUNDS_RE.search(usage.limit_note or ""):
                    self.alert(f"limit:{r['provider']}",
                               f"{r['provider']} refused work: {usage.limit_note}. Heavy work on it is paused for an "
                               f"hour; the account ({r['account'] or 'unknown'}) needs more credits or a higher "
                               f"spend limit.", "high")
                else:
                    self.alert(f"limit:{r['provider']}",
                               f"{r['provider']} hit a limit: {usage.limit_note}. New work on it waits and resumes "
                               f"by itself when the limit lifts; other providers and running work go on.", "low")
            if usage.auth_failed and not slept_auth:
                # Logged out is not a task failure and not worth retrying blindly: open the provider's
                # breaker (no run starts on it), say exactly how to fix it, and let check_logins ask
                # its CLI, without a model call, when it is logged in again.
                self.open_breaker(r["provider"], (usage.final_text or usage.error)[:200])
                self.alert(f"auth:{r['provider']}",
                           f"{r['provider']} on {hostname()} is logged out ({(usage.final_text or usage.error)[:120]}). "
                           f"Log in once on that machine ({prov.login_hint}). Nothing starts on it until then; "
                           f"work resumes by itself once a login check passes, and queued work is kept.", "high")
            if r["role"] == "coordinator":
                self._finish_coordinator(r, usage, status, note)
            else:
                # Before the hand-off, so its note on the root's dirty paths (_note_dirty_main) skips
                # what this alert already reported.
                try:
                    if root_now:
                        self.check_root_checkout(r, root_before, root_now)
                except Exception:   # a report only: the run's end stands
                    log(p, f"run {r['id']}: checking the project root's checkout failed\n" + traceback.format_exc())
                self._finish_worker(r, usage, status, run_dir, cut_off if status == "ok" else None,
                                    refused=refused, rebooted=bool(note.get("lost_to_reboot")),
                                    orderly=note.get("shutdown") == "orderly",
                                    slept=bool(note.get("lost_to_sleep") or note.get("lost_to_network")),
                                    held=held, root_wake=root_wake)
        self._check_price_table(r, usage)
        asleep = float(exit_info.get("slept_s") or 0)
        if asleep >= SLEPT_LONG_S:
            took = max(ended - float(exit_info.get("started") or r["started"] or ended), asleep)
            asleep_note = f" (host asleep {asleep / 60:.0f} of its {took / 60:.0f} min)"
        else:
            asleep_note = ""
        log(p, f"run {r['id']} end status={status} cost=${usage.cost_usd:.3f}"
               f"{' (estimated)' if usage.estimated else ''} role={r['role']}{asleep_note}")

    def _check_price_table(self, r: dict, usage) -> None:
        """Price a finished Claude run's session log with the table that estimates other local
        sessions, and raise a low alert when it drifts from the cost Claude Code reported. A resumed
        run is skipped: its session log also holds the calls of the run it resumed, and so is a run on
        plan windows, where the global cap that table serves never applies."""
        if (r["provider"] != "claude" or usage.estimated or not usage.cost_usd or not usage.session_id
                or float((self.cfg.get("budget") or {}).get("global_daily_usd") or 0) <= 0
                or getattr(self.gates.get("claude"), "regime", "caps") != "caps"   # plan runs never count
                or json.loads(r.get("note") or "{}").get("resumes")):
            return
        try:
            drift = localspend.calibrate(usage.session_id, usage.cost_usd)
        except Exception as e:  # noqa: BLE001 - a price check never breaks finishing a run
            log(self.p, f"price table check failed: {e}")
            return
        if drift is not None and abs(drift) > localspend.DRIFT:
            self.alert("claude_price_table",
                       f"The Claude price table that estimates this machine's other Claude Code sessions is "
                       f"{drift * 100:+.1f}% off the costs Claude Code reported for recent runs, so that "
                       f"estimate is off too. Update the table in tt-project or the account-level pricing.claude setting.",
                       "low", every_s=86400)

    def _slept_during(self, r: dict, exit_info: dict) -> bool:
        """Whether the host slept while the run was going: its supervisor saw the wall clock run
        ahead of the monotonic one, or the daemon saw the host sleep between the run's start and end
        (a supervisor that died leaves no clock readings)."""
        if float(exit_info.get("slept_s") or 0) >= SLEPT_MIN_S:
            return True
        start = float(exit_info.get("started") or r["started"] or 0)
        # A lost run ended somewhere between its last sign of life and now.
        end = time.time() if exit_info.get("stopped") == "lost" else float(exit_info.get("ended") or time.time())
        return bool(start) and self._slept_between(start, end)

    def _source_for(self, r: dict) -> str:
        if r["role"] == "coordinator":
            return "coordinator"
        task = self.p.db.task(r["task"]) if r["task"] else None
        if task and task["origin"] == "schedule":
            labels = json.loads(task["labels"] or "[]")
            return f"schedule:{labels[0]}" if labels else f"task:{task['id']}"
        return f"task:{r['task']}" if r["task"] else r["role"]

    def _finish_coordinator(self, r: dict, usage, status: str, note: dict) -> None:
        db = self.p.db
        if status == "timeout":
            details = note.get("turn") or {}
            log(self.p, f"coordinator run {r['id']} timed out: " + json.dumps(details, sort_keys=True))
            db.set_kv("last_coordinator_timeout", {"run": r["id"], "at": time.time(), **details})
        out = usage.structured if isinstance(usage.structured, dict) else last_json_object(usage.final_text or "")
        actions = (out or {}).get("actions")
        lost_to = "sleep" if note.get("lost_to_sleep") else "network" if note.get("lost_to_network") else \
            "reboot" if note.get("lost_to_reboot") and status == "lost" else ""
        if (status == "lost" and not r["dir"]) or status == "shutdown" or lost_to:
            # Never launched, ended by `ttp stop --kill`, or cut by a host sleep, a lost network or a
            # reboot: not a failed turn. Its messages and events stay queued for the next one.
            return
        if status == "auth":
            return   # its breaker holds the next turn until the login is back; the messages stay queued
        if status != "ok" or not isinstance(actions, list):
            if turnsize.over_budget(status, usage) and self._turn_over_budget(note):
                return
            self._coordinator_failed(f"{status} {usage.error[:200]}")
            return
        db.set_kv("coordinator_failures", 0)
        default_chat = note.get("default_chat")
        problems = coord.apply(self.p, actions, default_chat=default_chat, user_turn=coord.user_turn(db, note.get("messages") or []),
                               turn=r.get("id"), messages=note.get("messages") or [])
        chaincap.clear_notices(db, float(r.get("started") or 0))   # this turn's digest showed them
        ids = note.get("messages", [])
        problems = list(problems)
        if ids and not self._keep_messages(r, ids, problems):
            db.x(f"UPDATE messages SET handled=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        evs = note.get("events", [])
        if evs:
            db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(evs))})", evs)
        try:
            correction.settle(db, note, actions, str((out or {}).get("summary") or ""), float(r.get("started") or 0))
        except Exception:   # bookkeeping: the turn is settled either way
            log(self.p, "correction record failed: " + traceback.format_exc().replace("\n", " | ")[:1000])
        decided = len(_decisions(actions))
        self._log_decided(r, note, decided)
        if not decided:
            try:
                turngate.record(db, self.cfg, note, r.get("id"), float(getattr(usage, "cost_usd", 0) or 0))
            except Exception:   # bookkeeping: the turn is settled either way
                log(self.p, "coordinator turn gate record failed: "
                    + traceback.format_exc().replace("\n", " | ")[:1000])
        self._record_rejections([coord.clip_rejection(x) for x in problems])
        db.set_kv("last_coordinator_summary", {"ts": time.time(), "summary": (out or {}).get("summary", "")})
        try:
            quietgate.left(db, self._quiet_fp({k: g.as_dict() for k, g in self.gates.items()}), r.get("id"),
                           float(db.kv("last_coordinator_turn", 0)) or None)
        except Exception:   # bookkeeping: without it the next routine turn runs
            log(self.p, "coordinator quiet state not saved: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def _quiet_fp(self, gates: dict) -> str:
        """The state quietgate compares across turns, with only the ledger fields a probe never rewrites."""
        db = self.p.db
        changes = [[c.get(k) for k in ("id", "alias", "what", "undo", "expires", "probe", "closed")]
                   for c in machine_ledger.changes()]
        outages = {a: [o.get(k) for k in ("why", "owner", "until", "covers")]
                   for a, o in machine_ledger.outages().items()}
        return quietgate.fingerprint(db, self.cfg, wake_fingerprint(self.p, gates, db), projpause.current(db),
                                     [changes, outages])

    def _keep_messages(self, r: dict, ids: list, problems: list[str]) -> bool:
        """A user turn whose task_add was rejected for a reason it can fix (coord.fixable_rejection)
        leaves its messages unhandled, so the next turn sees them again with the rejection, instead of
        the user getting neither a task nor a reply. Once per message (KV_KEPT_MESSAGES): after that the
        message is handled and the rejection still reaches the next digest. Marks the rejection, in
        place in `problems`, so the next turn redoes only that action."""
        db = self.p.db
        fix = [i for i, x in enumerate(problems) if coord.fixable_rejection(x)]
        if not fix:
            return False
        kept = [int(x) for x in db.kv(KV_KEPT_MESSAGES, []) or [] if str(x).isdigit()]
        if set(int(i) for i in ids) & set(kept):
            return False
        db.set_kv(KV_KEPT_MESSAGES, (kept + sorted(int(i) for i in ids))[-200:])
        shown = ", ".join(f"#{i}" for i in ids)
        for i in fix:
            problems[i] += (f" [user message {shown} is shown to you again, once: add the task it asked for "
                            f"(or reply why not); the rest of that turn was applied, do not repeat it]")
        log(self.p, f"coordinator turn {r.get('id')}: messages {shown} kept for the next turn: a task_add "
                    f"was rejected for a reason it can fix")
        return True

    def _log_decided(self, r: dict, note: dict, decided: int) -> None:
        """What the turn decided, for the daily review's audit of turns that decided nothing."""
        self.p.db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps({**note, "decided": decided}), r.get("id")))

    def _record_rejections(self, problems: list[str]) -> None:
        """Rejected actions reach the next turn's digest; they never start a turn by themselves.
        A rejection repeated on consecutive turns is a harness problem and is recorded once; the next
        digest marks it as a repeat so it is not sent again as is."""
        db = self.p.db
        before = set(db.kv(coord.REJECTED_KEY, []) or [])
        db.set_kv(coord.REJECTED_KEY, problems)
        db.set_kv(coord.AGAIN_KEY, [coord.rejection_key(x) for x in problems if x in before])
        if not problems:
            return
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "rejected_actions", "normal", "; ".join(problems)[:1500],
              json.dumps(problems), "handled"))
        seen = db.kv(coord.REPEATS_KEY, []) or []
        for x in problems:
            key = coord.rejection_key(x)
            if x in before and key not in seen:
                seen.append(key)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                     (time.time(), "harness", "rejected_repeat", "normal",
                      f"The coordinator repeated an action that was rejected: {x}", "handled"))
        db.set_kv(coord.REPEATS_KEY, seen[-50:])

    def _turn_over_budget(self, note: dict) -> bool:
        """A turn over its cap: its items are recorded so the next turn splits the batch, raises a
        single item once or parks it (turnsize.py). False leaves it to the usual failure backoff (a
        turn with no batch, or a bookkeeping error)."""
        try:
            got = turnsize.on_fail(self.p.db, self.cfg, self.p.name, note, log=lambda t: log(self.p, t))
        except Exception:
            log(self.p, "coordinator over-budget handling failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
            return False
        if not got:
            return False
        self._turn_budget_alert()
        return True

    def _turn_budget_alert(self) -> None:
        """One normal alert each time an item is parked, naming every parked item; it clears once
        none is (alerts.holds)."""
        text = turnsize.alert_text(self.p.db, self.cfg, self.p.name)
        if text:
            self.alert(turnsize.ALERT_KEY, text, "normal", every_s=0)
            self.p.db.x("UPDATE alerts SET text=? WHERE key=? AND cleared IS NULL", (text[:2000], turnsize.ALERT_KEY))

    def _coordinator_failed(self, why: str) -> None:
        db = self.p.db
        fails = int(db.kv("coordinator_failures", 0)) + 1
        db.set_kv("coordinator_failures", fails)
        db.set_kv("coordinator_backoff_until", time.time() + min(1800, 30 * 2 ** fails))
        if fails >= 3:
            self.alert("coordinator", f"The coordinator failed {fails} turns in a row (last: {why}). "
                       f"Messages are queued, not lost.", "high")

    def _adopt_misplaced_handoff(self, r: dict, task: dict, run_dir: Path) -> None:
        """A worker that copied a path from its context may write its hand-off to an earlier run dir
        of its task. One written there after this run started is this run's: it is copied in and
        counts. Only the same task's run dirs and only files newer than the run, so a stale hand-off
        is never taken."""
        started = float(r.get("started") or 0)
        if not started:
            return
        rows = self.p.db.q("SELECT id, dir FROM runs WHERE task=? AND id<? ORDER BY id DESC",
                           (task["id"], r["id"]))
        found: list[tuple[float, Path]] = []
        for row in rows:
            path = self._run_dir(row) / RESULT_FILE
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > started and _read_result(path) is not None:
                found.append((mtime, path))
        if not found:
            return
        src = max(found)[1]
        try:
            redact.private_dir(run_dir)
            shutil.copyfile(src, run_dir / RESULT_FILE)
        except OSError as e:
            log(self.p, f"run {r['id']} of #{task['id']}: could not adopt the hand-off at {src}: {e}")
            return
        log(self.p, f"warning: run {r['id']} of #{task['id']} wrote its hand-off to {src}, an earlier "
                    f"run's dir; adopted it")
        self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                    (time.time(), "daemon", "handoff_misplaced", "low",
                     f"#{task['id']} run {r['id']} wrote its hand-off to {src} instead of its own run dir; "
                     f"adopted it", "handled", task["id"]))

    def _file_queued_notes(self, run_id, tid: int, run_dir: Path) -> None:
        """File the notes for other projects that a run (or its task's on_pass_cmd) queued in `run_dir`
        because it could not write the inbox itself (ttp note --to), as task `tid`'s."""
        try:
            sent = upstream.send_queued(run_dir, self.p.name, tid, self.p.state)
            if any(sent.values()):
                log(self.p, f"run {run_id} of #{tid}: queued notes to other projects: {sent}")
            if upstream.file_queued_acks(run_dir, self.p.name):
                log(self.p, f"run {run_id} of #{tid}: queued upstream acks recorded")
        except OSError as e:
            log(self.p, f"could not file the queued notes of #{tid}: {e}")

    def _defer_for_push(self, r: dict, task: dict, run_dir: Path, usage, ended, result, summary: str) -> bool:
        """A done hand-off that claims a push waits in `pushing` until verify_pushes finds the push on
        the remote; True when it does. Offline: it runs inside on_pass's finishing transaction."""
        try:
            cs = pushclaim.claims(self.p, task, result if isinstance(result, dict) else {}, r)
        except Exception as e:
            log(self.p, f"task {task['id']}: reading its push claims failed: {type(e).__name__}: {e}")
            return False
        if not cs:
            return False
        db, now = self.p.db, time.time()
        saved = dict(result) if isinstance(result, dict) else {}
        saved.setdefault("summary", summary)
        try:
            durable_write(run_dir / pushclaim.HANDOFF, json.dumps(saved))
        except OSError as e:
            log(self.p, f"task {task['id']}: cannot save its hand-off for the push check: {e}")
            return False
        what = pushclaim.describe(cs)
        db.update_task(task["id"], status="pushing", blocked_reason=f"verifying its claimed push on the remote: {what}"[:500],
                       result=dump_result({"summary": summary, "status": "pushing", pushclaim.KEY: {
                           "claims": cs, "run": r.get("id"), "dir": str(run_dir), "ended": ended, "since": now,
                           "tries": 0, "next": now, "cost": float(usage.cost_usd or 0.0),
                           "estimated": bool(usage.estimated)}}))
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, f"task:{task['id']}", "push_verifying", "low",
              f"#{task['id']} done once its claimed push is on the remote: {what}", "handled", task["id"]))
        return True

    def _push_net(self, carry: str) -> dict | None:
        """The cache a push-claim check asks the remote with: None while the core provider's network
        is held. It holds this tick's shared pushclaim.Budget and the answers `carry` kept from an
        earlier tick that ran out of time, marked pushclaim.CARRIED, unless older than
        pushclaim.CARRY_S or from before a network hold."""
        if self.net_held(self.cfg.get("core_provider") or "claude"):
            self._push_carry.clear()
            return None
        if not self._tick_began or not self._push_budget or self._push_budget[0] != self._tick_began:
            self._push_budget = (self._tick_began, pushclaim.Budget())
        now = time.time()
        at, kept = self._push_carry.pop(carry, (now, {}))
        if not kept or now - at > pushclaim.CARRY_S:
            at, kept = now, {}
        return {**kept, pushclaim.CARRIED: set(kept), "since": at, pushclaim.Budget.KEY: self._push_budget[1]}

    def _push_keep(self, carry: str, cache: dict) -> None:
        """Keeps a push check's remote answers for later ticks, dated by the oldest of them."""
        self._push_carry[carry] = (cache["since"], {k: v for k, v in cache.items() if isinstance(k, tuple)})

    def verify_pushes(self) -> None:
        """Tasks waiting in `pushing` on their claimed pushes (_defer_for_push): asked on the remote,
        outside any transaction, a few per tick with backoff. Confirmed: the hand-off is recorded.
        Missing, or the remote unreadable past pushclaim.BOUND_S: the task fails naming remote, ref, sha.
        Skipped while the network is held; past the tick's Budget the rest wait, untouched, for the next."""
        cache = self._push_net("verify")
        if cache is None:
            return
        db, now = self.p.db, time.time()
        rows = db.q("SELECT id, result FROM tasks WHERE status='pushing' AND result LIKE ? ORDER BY id",
                    (f'%"{pushclaim.KEY}"%',))
        due = []
        for t in rows:
            pv = load_result(t["result"]).get(pushclaim.KEY)
            if isinstance(pv, dict) and float(pv.get("next") or 0) <= now:
                due.append((t["id"], pv))
        for tid, pv in due[:pushclaim.PER_TICK]:
            try:
                state, why, _ = pushclaim.check_all(self.p, list(pv.get("claims") or []), cache)
            except pushclaim.OutOfTime as e:
                self._push_keep("verify", cache)
                log(self.p, f"claimed pushes: {e}; task {tid} and later ones are checked next tick")
                break
            run_dir = Path(pv.get("dir") or ".")
            if state == pushclaim.UNKNOWN and now - float(pv.get("since") or now) < pushclaim.BOUND_S:
                tries = int(pv.get("tries") or 0) + 1
                with db.tx():
                    t = db.task(tid)
                    if t and t["status"] == "pushing":
                        res = load_result(t["result"])
                        res[pushclaim.KEY] = {**pv, "tries": tries, "next": now + pushclaim.delay_s(tries), "last": why}
                        db.update_task(tid, result=dump_result(res),
                                       blocked_reason=f"verifying its claimed push on the remote: {why}"[:500])
                continue
            try:
                handoff = json.loads((run_dir / pushclaim.HANDOFF).read_text())
            except (OSError, ValueError):
                handoff = {"status": "done", "summary": load_result(db.task(tid)["result"]).get("summary", "")}
            if not isinstance(handoff, dict):
                handoff = {"status": "done", "summary": str(handoff)}
            said = str(handoff.get("summary") or "")
            if state == pushclaim.CONFIRMED:
                handoff = {**handoff, "summary": f"{said} (push verified: {why})"[:1500]}
            else:
                if state == pushclaim.UNKNOWN:
                    why = f"the remote could not be read for {int(pushclaim.BOUND_S // 3600)} h: {why}"
                keep = [f for f in handoff.get("followups") or [] if isinstance(f, dict)
                        and (db.task(tid)["kind"] != "review" or str(f.get("title", "")).startswith("upstream:"))]
                gone = "could not be verified on" if state == pushclaim.UNVERIFIABLE else "is not on"
                handoff = {"status": "failed", "followups": keep,
                           "summary": f"claimed a push that {gone} the remote: {why}. Its hand-off said: {said}"[:1500]}
            log(self.p, f"task {tid}: claimed push {state}: {why[:300]}")
            run = db.one("SELECT * FROM runs WHERE id=?", (pv.get("run"),)) if pv.get("run") else None
            from .providers.base import RunUsage
            usage = RunUsage(cost_usd=float(pv.get("cost") or 0.0), estimated=bool(pv.get("estimated")))
            self._prefetch_lint(tid, handoff, run_dir)
            with db.tx():
                t = db.task(tid)
                if not t or t["status"] != "pushing" or pushclaim.KEY not in load_result(t["result"]):
                    continue
                self._finish_worker(dict(run) if run else {"id": pv.get("run"), "task": tid, "note": "{}"}, usage,
                                    "ok", run_dir, pv.get("ended"), handoff=handoff,
                                    push_verified=state == pushclaim.CONFIRMED)

    def audit_pushes(self) -> None:
        """Hourly, model-free: done tasks of the last days whose claimed pushes are not on the remote
        (pushclaim.audit), one keyed observation each; quiet, and earlier ones closed, when none. An open
        ask whose tasks all landed is closed (unblock.close_landed).
        Skipped while the network is held; past the tick's Budget it resumes next tick from the answers so far."""
        db, now = self.p.db, time.time()
        last = db.kv(pushclaim.AUDIT_KV)
        if isinstance(last, dict) and now - float(last.get("at") or 0) < pushclaim.AUDIT_EVERY_S:
            return
        cache = self._push_net("audit")
        if cache is None:
            return
        try:
            a = pushclaim.audit(self.p, now=now, max_refs=4, cache=cache)
        except pushclaim.OutOfTime:
            self._push_keep("audit", cache)
            return
        except Exception as e:
            log(self.p, f"push audit failed: {type(e).__name__}: {e}")
            db.set_kv(pushclaim.AUDIT_KV, {**(last if isinstance(last, dict) else {}), "at": now})
            return
        db.set_kv(pushclaim.AUDIT_KV, {**{k: v for k, v in a.items() if k != "landed"}, "gaps": a["gaps"][:50]})
        try:
            for aid in unblock.close_landed(db, a.get("landed") or [], now):
                log(self.p, f"ask #{aid} resolved: its work landed")
        except Exception as e:
            log(self.p, f"closing asks whose work landed failed: {type(e).__name__}: {e}")
        missing = [g for g in a["gaps"] if g["state"] == pushclaim.MISSING]
        for g in missing:
            self.observe("daemon:push-audit", pushclaim.gap_text(g), "normal", key=pushclaim.gap_key(g), whole=True)
        if not missing:
            scr.close_watcher_issues(db, "daemon:push-audit", why="every claimed push of the last days is on its remote")

    def _prefetch_remote(self, task_id: int, result: dict | None, run_dir: Path | None = None) -> None:
        """What finishing a code or review task's hand-off asks the push remote (pushq.prefetch), asked
        now, before the transaction that records it: inside it the answer is read, never fetched, so
        the write lock never waits on the network. Best effort: an unanswered question reads as before."""
        task = self._prefetch_lint(task_id, result, run_dir)
        if not isinstance(result, dict) or result.get("status") not in ("done", "needs_review"):
            return   # only a finished hand-off approves a push or gets its review
        if not task or task["kind"] not in ("code", "review"):
            return
        try:
            pushq.prefetch(self.p, task, self.cfg)
        except Exception as e:
            log(self.p, f"task {task_id}: asking the push remote before finishing failed: {type(e).__name__}: {e}")

    def _prefetch_lint(self, task_id: int, result, run_dir: Path | None = None) -> dict | None:
        """What the hand-off lint (handofflint.lint, in _finish_worker) reads from the remote, and the
        project's hand-off checks (handoffcheck), asked before the transaction. Returns the task."""
        task = self.p.db.task(task_id)
        try:
            if task:
                handofflint.prefetch(self.p, self.cfg, task, result)
        except Exception as e:
            log(self.p, f"task {task_id}: asking the remote for the hand-off lint failed: {type(e).__name__}: {e}")
        try:
            if task and not _push_rechecked(task):
                handoffcheck.prefetch(self.p, self.cfg, task, result, run_dir)
        except Exception as e:   # verdict runs them again inside the transaction
            log(self.p, f"task {task_id}: the hand-off checks failed to run early: {type(e).__name__}: {e}")
        return task

    def _finish_worker(self, r: dict, usage, status: str, run_dir: Path, ended: str | None = None,
                       rebooted: bool = False, slept: bool = False, handoff: dict | None = None,
                       orderly: bool = False, refused: str = "", push_verified: bool = False,
                       held: str = "", root_wake: dict | None = None) -> None:
        """`root_wake`: tracked paths the run newly dirtied in the project root (_root_wake): a `done`
        is sent back once to commit or revert them; else check_root_checkout reports them.
        `handoff` stands in for the run's result.json: the `on_pass` its checks earned (_probe_passed).
        `held`: why the run's runtime changes did not load (runtimeload.check): its hand-off is not judged.
        `push_verified`: the pushes the hand-off claims were found on the remote (verify_pushes).
        `orderly`: the reboot that cut the run short was an orderly one, which never blocks the task.
        `refused`: why the runner refused to start the agent (runner.check_pin)."""
        db = self.p.db
        task = db.task(r["task"]) if r["task"] else None
        if not task:
            return
        landed.reconcile_pushes(self.p, task["id"])
        relay_worker_notifies(self.p, task, run_dir)
        _, past_cap = relay_worker_escalations(self.p, task, r.get("id"), run_dir)
        self._file_queued_notes(r.get("id"), task["id"], run_dir)
        if held:
            # Judged by the code it replaced, the hand-off could fail for the bug the run fixed: the
            # coordinator decides, no attempt spent. The hand-off stays in the run dir and the result.
            why = (f"held: {held}. The run's hand-off was not judged under the previous runtime; it stays in "
                   f"{run_dir / RESULT_FILE}. Make the runtime start with the change, then requeue the task")
            with db.tx():
                db.update_task(task["id"], status="blocked", blocked_reason=why[:500],
                               result=dump_result({"status": "blocked", "summary": why,
                                                   "handoff": _read_result(run_dir / RESULT_FILE)}))
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "task_blocked", "normal",
                      f"#{task['id']} {task['title']} → blocked: {why}", "queued", task["id"]))
            return
        if handoff is None and not (run_dir / RESULT_FILE).exists():
            self._adopt_misplaced_handoff(r, task, run_dir)
        handoff = handoff if handoff is not None else _read_result(run_dir / RESULT_FILE)
        result = handoff or last_json_object(usage.final_text or "") or {}
        if past_cap and isinstance(result, dict):
            result = dict(result, escalations_dropped=past_cap)
        if isinstance(result, dict) and (found := startcheck.of_run(r.get("note"))):
            result = dict(result, start_checks=found)   # what the run started with, whatever it reported
        try:   # a known defect degrading a live service gets a workaround with a due time, beside its fix
            if (mids := mitigate.file(db, self.cfg, task, result, where=self.p)):
                log(self.p, f"task {task['id']}: live impact; mitigation " + ", ".join(f"#{m}" for m in mids))
        except Exception:
            log(self.p, "mitigation filing failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
        if task["status"] == "cancelled":
            # Cancelled while this run was ending: the decision stands. Keep what the run produced,
            # and tell the coordinator only if the work actually got done.
            summary = str((result.get("summary") if isinstance(result, dict) else "") or "")
            db.update_task(task["id"], result=dump_result({"summary": summary, "status": "cancelled",
                                                           "run_status": status}))
            if isinstance(result, dict) and result.get("status") == "done":
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "cancelled_but_done", "normal",
                      f"#{task['id']} {task['title']} was cancelled, but its run finished the work: "
                      f"{summary[:800]}", "queued", task["id"]))
            return
        if status == "lost" and not r["dir"] and not (run_dir / "lease").exists():
            # The daemon stopped between recording the run and launching it: nothing ran.
            with db.tx():
                db.update_task(task["id"], status="queued", blocked_reason=None)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_requeued", "low", f"#{task['id']} {task['title']}: its run "
                      f"never started; queued again, no attempt spent", "handled", task["id"]))
            return
        # A hand-off written before the run ended badly (supervisor killed, reboot, timeout, stall)
        # stands: the work it reports is done and redoing it would repeat it.
        if status not in ("ok", "killed", "resource_busy") and (
                (handoff or {}).get("status") in HANDOFF_STATES
                or (status == "shutdown" and isinstance(result, dict) and result.get("status"))):
            ended, status = status, "ok"
        elif status == "shutdown":
            # The project was stopped, not the task: it resumes on the next start, on its own branch.
            db.update_task(task["id"], status="queued", blocked_reason="interrupted by `ttp stop --kill`; resumes")
            return
        if refused in ("model_mismatch", "pin_unverified"):
            # A mismatch or strict unverifiable pin: nothing ran or was spent. Retrying the same
            # configuration would loop, so block without spending an attempt. Before the resume
            # fallback: a refused resume must not start fresh under the same pin.
            prefix = "model pin not honoured" if refused == "model_mismatch" else "model pin could not be verified"
            why = f"{prefix}: {usage.error}"[:500]
            remedy = ("Change or clear its model/effort pin (task_update) and requeue it."
                      if refused == "model_mismatch" else
                      "Restore session verification or explicitly change pin_verify, then requeue it.")
            with db.tx():
                db.update_task(task["id"], status="blocked", blocked_reason=why,
                               result=dump_result({"status": "blocked", "summary": why}))
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "task_blocked", "normal",
                      f"#{task['id']} {task['title']} → blocked: {why}. {remedy}", "queued", task["id"]))
            return
        if status == "failed" and _resume_never_started(json.loads(r["note"] or "{}"), usage) and not handoff:
            # The lost run's session could not be continued after all: nothing ran, so the task starts
            # fresh at once, no attempt spent. This run ended 'failed', so it is not resumed again.
            with db.tx():
                db.update_task(task["id"], status="queued", blocked_reason=None, not_before=None)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_requeued", "low", f"#{task['id']} {task['title']}: its lost "
                      f"session could not be resumed; starts fresh, no attempt spent", "handled", task["id"]))
            return
        if status == "resource_busy":
            # The run lost the race for its resource and never started its agent: not an attempt.
            db.update_task(task["id"], status="queued", not_before=time.time() + 30,
                           blocked_reason=f"{RESOURCE_BUSY_NOTE} before the run could start; retries")
            return
        rstatus = result.get("status") if isinstance(result, dict) else None
        # A project's own result rule (`done` plus a verdict): the change must not proceed. The task is
        # stored failed, so its fix and re-review follow and nothing is approved, but the review itself
        # worked: it shows as changes_needed and spends no attempt (db.CHANGES_NEEDED).
        rejects = rstatus == "done" and task["kind"] == "review" and review_rejects(result)
        if rejects:
            rstatus, result = "failed", dict(result, status=CHANGES_NEEDED)
        summary = str((result.get("summary") if isinstance(result, dict) else None)
                      or (usage.final_text or usage.error or "")[:1500])
        if rstatus == "waiting" and result.get("retry_when") is not None and not isinstance(result["retry_when"], str):
            # No shell command (JSON true, a number, a list): never run, nor read as passed through its
            # truthiness. It is dropped, said loudly, and the task wakes at its timer (or on its jobs' probe).
            bad = json.dumps(result["retry_when"])[:200]
            result = {**{k: v for k, v in result.items() if k != "retry_when"}, "retry_when_refused": bad}
            text = (f"#{task['id']} {task['title']}: its waiting hand-off's retry_when {bad} is no shell command, "
                    f"so it was not stored; the task wakes at its retry timer"
                    + ("; its on_pass_cmd runs only after a probe passes, so a run reads it instead"
                       if result.get("on_pass_cmd") is not None else ""))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "handoff_probe_refused", "normal", text, "queued", task["id"]))
            log(self.p, f"task {task['id']} hand-off: retry_when {bad} refused: no shell command")
        jobs = _detached_jobs(run_dir)
        if jobs and (rstatus is None and status in ("ok", "timeout", "stalled")
                     or rstatus == "waiting" and not result.get("retry_when")):
            # A job this run detached is still the task's work in flight: the task waits until each
            # job wrote its .rc or is gone (a kill, a reboot) instead of spending an attempt, and the
            # next run is told where they are.
            listing = "; ".join(f"{j['name']}: log {j['log']}, rc {j['rc']}" for j in jobs)
            if rstatus is None:
                summary = (f"run ended ({status}) without a hand-off after detaching jobs ({listing}). "
                           f"Its last message: {summary}")[:1500]
                result = {"status": "waiting", "summary": summary, "retry_after_s": 1800}
                status, rstatus = "ok", "waiting"
            result = {**result, "retry_when": locks.job_probe([j["rc"] for j in jobs], push._own_ttp(self.p)),
                      "waiting_for": result.get("waiting_for") or f"detached jobs: {listing}"[:300]}
        if status == "ok" and rstatus in ("failed", "blocked"):
            from . import checkgaps
            gap_wait = checkgaps.wait(self.p, task, result, run_dir, self.cfg)
            if gap_wait:
                result, rstatus = gap_wait, "waiting"
                summary = result["summary"]
            elif rstatus == "blocked":
                from . import landwait
                land = landwait.wait(self.p, task, result)
                if land:
                    result, rstatus, summary = land, "waiting", land["summary"]
                    self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                                (time.time(), "daemon", "task_wait_landed", "low",
                                 f"#{task['id']} {task['title']}: blocked only on {land['retry_when'][7:]} landing; "
                                 f"waits on {land['retry_when']}", "handled", task["id"]))
                    _task_note(run_dir, f"blocked question kept: {summary}")
        waiting = status == "ok" and rstatus == "waiting"
        refusal = ackwait.unmanaged_wait(result, self._other_projects(), bool(jobs), self.cfg, self.p.harness) if waiting else ""
        both = on_pass_both_warning(result) if waiting else ""
        if both:
            log(self.p, f"task {r['task']} hand-off: {both}")
        if refusal:
            # A wait on another project's reply without `ttp ack-wait probe` would wake a model run on
            # every timer to read the inbox: not taken as a wait. Its next run hands off again.
            waiting, rstatus = False, "failed"
            summary = f"{refusal} Its summary: {summary}"[:1500]
            result = {**result, "status": "failed", "summary": summary}
        # A host reboot is not the task's failure: no attempt, no delay, unless the task keeps being
        # the run the host went down under.
        reboot_lost = status == "lost" and rebooted
        # So is a host sleep, a few times: past max_reboot_losses since the task was last blocked it
        # counts an attempt again, so a task that fails on its own while the host also slept cannot
        # retry for free forever.
        if status == "lost" and slept:
            reboot_lost = self._reboot_losses(task["id"], "lost_to_sleep") + \
                self._reboot_losses(task["id"], "lost_to_network") <= int(
                self.cfg["budget"].get("max_reboot_losses", 3))
        no_handoff = status == "ok" and rstatus is None
        if waiting:
            new = "queued"   # a busy resource is not a failed attempt: the task comes back later
        elif refusal:
            new, status = "failed", "refused_wait"   # an attempt, but no deep retry (effort.RETRY_FAILED)
        elif no_handoff:
            # A clean exit without a hand-off is not evidence of done work: the worker may have
            # stopped mid-task ("I'll pick up later"). Count an attempt and retry with its last words.
            new, status = "failed", "no_handoff"
            summary = f"ended without a hand-off. Its last message: {summary}"[:1500]
        elif status == "ok" and rstatus in ("done", "blocked", "failed", "needs_review"):
            new = {"done": "done", "blocked": "blocked", "failed": "failed", "needs_review": "review"}[rstatus]
        elif status in FREE_ENDS or reboot_lost:
            new = "queued"   # not an attempt: the account refused or the host went down, the task did not fail
        else:
            new = "failed"
        attempts = int(task["attempts"] or 0) + (0 if status in FREE_ENDS or waiting or reboot_lost
                                                  or rejects else 1)
        if new == "failed" and attempts < int(task["max_attempts"] or 3) and status in ("failed", "lost", "timeout",
                                                                                     "stalled", "no_handoff",
                                                                                     "refused_wait"):
            new = "queued"
        # A done hand-off on a spec's `cells:` line that left requested cells unfinished or unreported
        # is never passed on as complete: its summary says so, and missing cells get a `continue:`
        # follow-up (the usual hop path queues it or leaves it to the coordinator). Status stays done.
        # A review's verdict covers no cells, even when its spec quotes the reviewed task's line.
        cell_chk = cells.check(task.get("spec"), result) \
            if new == "done" and task["kind"] != "review" and isinstance(result, dict) else None
        cell_mark = cells.prefix(cell_chk)
        if new == "done" and task["kind"] != "review" and (skip := cells.ignored(task.get("spec"))):
            # A descriptive `cells:` line stored before spec checks refused it requests no cells.
            log(self.p, f"task {task['id']}: ignored a non-enumerable cells: line ({skip[0][:80]})")
        # `cells_checked` in the stored result is the daemon's own seal (it survives a deferred push and
        # a restart); a worker's own 'partial:' text is not, so its missing cells still get a follow-up.
        if cell_mark and not result.get("cells_checked"):
            summary = summary if cells.marked(summary) else f"{cell_mark}{summary}"
            fups0 = result.get("followups") if isinstance(result.get("followups"), list) else []
            # A hop gets only the cells it could produce: none marked unrunnable, rejected in the
            # chain's tried ledger or named on another open task's `cells:` line (cells.hop_cells).
            hop, skipped = self._hop_cells(task, result, cell_chk["missing"]) \
                if cell_chk["missing"] else ([], [])
            add = [cells.followup(task, hop)] if hop and not cells.has_followup(fups0) else []
            result = {**result, "summary": summary, "followups": [*fups0, *add], "cells_checked": True}
            if skipped:
                result[cells.NO_HOP] = skipped
            log(self.p, f"task {task['id']}: {cell_mark.strip()}"
                        + (f"; no hop for {len(skipped)}: {'; '.join(skipped)[:300]}" if skipped else ""))
        extra: dict = {}
        reason = None
        not_before = None
        if rebooted:
            extra["reboot"] = {"at": self.boot_at, "notes": _last_notes(run_dir)}
        wakes = _reboot_wakes(task)
        if reboot_lost and not slept and not orderly:
            n = self._reboot_losses(task["id"]) + wakes
            if n >= int(self.cfg["budget"].get("max_reboot_losses", 3)):
                new, reason = "blocked", f"lost to a host reboot {n} times; it may be causing them"
            elif held := self._held_at_reboot(task["id"], r["boot_id"]):
                # It held a device or machine when the host went down: a retry may take the host (and
                # every other run on it) down again. The coordinator decides, not a timer.
                new, reason = "blocked", held_at_reboot_reason(held)
                extra["reboot"]["held"] = held
        if waiting:
            # A cheap wake that found real work asks to go on at a higher tier: once per wake, at
            # once, and not as another wait, so the escalated run cannot escalate again.
            wake = json.loads(r["note"] or "{}").get("wake") or {}
            up = result.get("wake_tier")
            escalate = (wake.get("tier") in bud.TIER_ORDER and not wake.get("escalated")
                        and str(result.get("retry_after_s")) in ("0", "0.0") and up in bud.TIER_ORDER
                        and bud.TIER_ORDER.index(up) > bud.TIER_ORDER.index(wake["tier"]))
            try:
                waits = int(load_result(task["result"]).get("waits") or 0) + (0 if escalate else 1)
            except (TypeError, ValueError):
                waits = 1
            what = str(result.get("waiting_for") or summary)[:300]
            if locks.lock_probe_parts(str(result.get("retry_when") or ""))[0]:
                what = self._wait_what(task["id"], result)   # the holders now, not the ones the hand-off saw
            extra["waits"] = waits
            # A wake that hands back the same wait (what it waits for and its probe, on the same branch
            # head) made no progress: from then on the task wakes only at light, and past
            # waiting.max_stale_wakes it is the coordinator's to split or re-plan, not woken again.
            prev_res = load_result(task["result"])
            key = {"key": _wait_key(result), "head": self._branch_head(task)}
            stale = _stale_wakes(prev_res, key, escalate)
            extra.update(wait_key=key, stale_wakes=stale)
            max_stale = int((self.cfg.get("waiting") or {}).get("max_stale_wakes", 3) or 0)
            gate_probe = result.get("retry_when") if escalate else None
            gate_probe = gate_probe.strip() if isinstance(gate_probe, str) and gate_probe.strip() else None
            if escalate and gate_probe:
                # It found work for after its probe passes: the probe says when, not a model run.
                not_before = time.time() + PROBE_TIMEOUT_S
                extra["escalated_wake"] = True
                extra["waiting_since"] = time.time()
                reason = f"woke at {wake['tier']} and asked for {up}; runs at {up} once its probe passes"
            elif escalate:
                not_before = time.time()
                extra["escalated_wake"] = True
                extra["woke"] = f"the {wake['tier']} wake found work and asked for {up}"
                reason = f"woke at {wake['tier']} and found work; runs again now at {up}"
            elif waits > int(self.cfg["budget"].get("max_waits", 24)):
                new, reason = "blocked", f"still waiting after {waits} tries: {what}"
            elif max_stale and stale >= max_stale:
                new, reason = "blocked", (f"no progress after {stale} wakes waiting for {what}: needs a split or "
                                          f"a re-plan (a probe that tells when the wait is over, or smaller steps)")
            elif rebooted and not orderly and result.get("survives_reboot") is not True and (
                    n := self._reboot_losses(task["id"]) + wakes) >= int(
                    self.cfg["budget"].get("max_reboot_losses", 3)):
                # This run is already one of the losses: its wait counts once, not again as a wake.
                new, reason = "blocked", f"lost to a host reboot {n} times; it may be causing them"
            elif rebooted and result.get("survives_reboot") is not True and (
                    not rebootwait.stays_asleep(result, (str(self.p.runs),))
                    or any(not Path(j["rc"]).exists() for j in jobs)):
                # What it waited on died with the host: its next run finds out now, not at the timer.
                not_before = time.time()
                extra["woke"] = "the host rebooted"
                reason = f"waiting for {what}; the host rebooted, so it runs again now"
            else:
                not_before = time.time() + _retry_s(result)
                extra["waiting_since"] = time.time()
                reason = f"waiting for {what}; next try {timefmt.short(not_before, self.p)}"
            if new == "queued" and "waiting_since" in extra and isinstance(result.get("on_pass_cmd"), str):
                result, summary = self._dry_run_verdict(r, task, result, run_dir, extra, summary)
                if onpassdry.KEY in extra:
                    not_before = time.time()
                    reason = "its on_pass_cmd dry run crashed; handed back to its session to fix"
            if new == "blocked":
                # A block is the coordinator's to decide: whoever requeues it starts the count over.
                extra["stale_wakes"] = 0
            if "waiting_since" in extra and (said := worktree.probe_hold_text(task["id"], result.get("retry_when"))):
                log(self.p, said)
            if "waiting_since" in extra:
                # The spec it slept on: a later change (a task_update) wakes it once (_spec_wake).
                from .prompts import spec_digest
                extra[SPEC_SEEN_KEY] = spec_digest(self.p.db.task(task["id"]) or task)
                extra[SPEC_WAKES_KEY] = _recent_spec_wakes(prev_res, time.time())
        # A review that passes may hand its approved commits to the push queue instead of pushing
        # (delivery.push_queue): it waits as 'pushing' until a batch pushed them (pushq.py).
        approval, push_note, push_quiet = None, "", False
        entries = result.get("push") if isinstance(result, dict) else None
        if new == "done" and entries not in (None, "", []):
            check = (pushq.check_approval(self.p, task, entries, cfg=self.cfg) if task["kind"] == "review"
                     else {"ignored": "only a review task approves pushes"})
            if check.get("ignored"):
                log(self.p, f"task {task['id']}: its push list was ignored: {check['ignored']}")
                push_note = f" Its push list was ignored: {check['ignored']}."
            elif check.get("invalid"):
                why = check["invalid"]
                log(self.p, f"task {task['id']}: push approval invalid: {why}")
                extra["push_invalid"] = int(load_result(task["result"]).get("push_invalid") or 0) + 1
                push_note = f" Push approval invalid: {why}."
                if extra["push_invalid"] < 2:
                    new, not_before, push_quiet = "queued", time.time(), True
                    extra["woke"] = f"push approval invalid: {why}"
                    reason = f"push approval invalid: {why}; its review runs again"
                else:
                    new = "failed"
                    summary = f"push approval invalid again: {why}. {summary}"[:1500]
            else:
                approval, new = check, "pushing"
                for note in check.get("notes") or []:
                    log(self.p, f"task {task['id']}: {note}")
        # A push-conflict fix (conflictfix.py) costs no review attempt when it approves a new head. A
        # light one that ends any other way (done with nothing approved included; a refused account, a
        # lost host, a block or a needs_review aside) runs again at once at standard, also without an
        # attempt and with no coordinator turn.
        cfix, cmark = conflictfix.run_note(r.get("note")), conflictfix.of(task)
        if cfix and cmark:
            lost_run = status in FREE_ENDS or reboot_lost
            asks_up = waiting and isinstance(result, dict) and result.get("wake_tier") in ("standard", "deep")
            if cfix.get("tier") == "light" and not lost_run and (asks_up or not waiting) \
                    and new not in ("pushing", "blocked", "review"):
                ended_as = ("waiting for a standard run" if asks_up
                            else "done without approving a new head" if new == "done" else rstatus or status)
                why = f"{conflictfix.LIGHT_FAILED} {ended_as}: {summary[:300]}"
                new, not_before, push_quiet, waiting = "queued", time.time(), True, False
                attempts = int(task["attempts"] or 0)
                extra[conflictfix.KEY] = {**cmark, "escalate": why}
                extra["woke"] = str(load_result(task["result"]).get("woke") or "push conflict")[:1500]
                reason = f"{why[:300]}; its review runs again at standard"
            elif new == "pushing":
                attempts = int(task["attempts"] or 0)
            elif new == "queued":
                extra[conflictfix.KEY] = cmark   # its next run is the same conflict fix
        # The project's own hand-off checks (worker.handoff_checks, handoffcheck.py), run before this
        # transaction: one that fails sends the run back with its output, before any reply posts or
        # review or push is queued; past the cap the task goes to review, its result kept. A broken one
        # never holds a hand-off back: the coordinator hears once per check.
        sent_back = int(load_result(task["result"]).get(handoffcheck.KEY) or 0)
        hcheck_text = ""
        checked: dict = {}
        if status == "ok" and not waiting and not refusal and not push_quiet and not _daemon_precheck(r, handoff) \
                and not _push_rechecked(task):
            try:
                checked = handoffcheck.verdict(self.p, self.cfg, task, handoff, run_dir)
            except Exception:   # never lose a hand-off to the checks themselves
                log(self.p, "hand-off checks failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
        for b in checked.get("broken") or []:
            fp = handoffcheck.broken_fingerprint(b)
            log(self.p, f"task {task['id']}: hand-off check '{b['name']}' is broken ({b['why']}); accepted")
            if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                     (time.time(), "daemon", handoffcheck.BROKEN_EVENT, fp, "normal",
                      handoffcheck.broken_text(task, b), "queued", task["id"]))
        if (bad := checked.get("failed")):
            cap = handoffcheck.max_sendbacks(self.cfg)
            log(self.p, f"task {task['id']}: hand-off check '{bad['name']}' failed ({sent_back} of {cap} send-backs "
                        f"so far)")
            approval = None
            attempts = int(task["attempts"] or 0)   # the cap bounds these runs, not max_attempts
            if sent_back < cap:
                sent_back += 1
                new, not_before, push_quiet = "queued", time.time(), True
                extra["woke"] = handoffcheck.reason(bad, sent_back, cap)
                reason = f"hand-off check '{bad['name']}' failed; sent back ({sent_back} of {cap})"
            else:
                new, sent_back = "review", 0   # a requeue by the coordinator gets its send-backs again
                reason = f"hand-off check '{bad['name']}' still fails after {cap} send-backs; the coordinator decides"
                hcheck_text = (f"\nHand-off check '{bad['name']}' still fails after {cap} send-backs: nothing was "
                               f"posted, reviewed or pushed, and its result and branch are kept. Decide: fix and "
                               f"requeue it, accept it as is, or cancel it. The check's output:\n"
                               f"{handoffcheck.event_tail(bad['output'])}")
        if sent_back and not (checked and not checked.get("failed")):
            extra[handoffcheck.KEY] = sent_back   # until a hand-off passes them
        if isinstance(mark := load_result(task["result"]).get(ROOT_WAKE_KEY), dict):
            # Kept for the task's life: one wake per task. Open until a run of it ends with a hand-off.
            extra[ROOT_WAKE_KEY] = mark if status != "ok" else {k: v for k, v in mark.items() if k != "open"}
        root_sent = False
        if root_wake and new == "done" and not approval and status == "ok":
            paths = root_wake["paths"]
            shown = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
            new, not_before, push_quiet, root_sent = "queued", time.time(), True, True
            attempts = int(task["attempts"] or 0)
            extra[ROOT_WAKE_KEY] = {"open": True, "run": r.get("id"), "paths": paths}
            extra["woke"] = (f"your run worked in the project root ({self.p.root}) and left uncommitted changes "
                             f"to tracked paths that were clean when it started: {shown}. Other tasks branch from "
                             f"that checkout, so the edits can be lost or taken by another task. Commit your own "
                             f"edits to the root's repository, or revert them, then write result.json again; this "
                             f"is asked once")
            reason = f"left {len(paths)} tracked path(s) in the project root uncommitted; sent back once"
            log(self.p, f"task {task['id']}: run {r.get('id')} {reason}")
        elif root_wake:
            try:   # not sent back: reported as a run in the root always was
                self.check_root_checkout(r, root_wake["before"], root_wake["now"])
            except Exception:
                log(self.p, f"run {r.get('id')}: checking the project root's checkout failed\n"
                            + traceback.format_exc())
        if new == "done" and not approval and not push_verified and status == "ok" \
                and self._defer_for_push(r, task, run_dir, usage, ended, result, summary):
            return
        if new in ("done", "pushing"):
            self._mark_verified(task, approval, extra)
        if ended:
            extra["run_status"] = ended
        if wakes and new != "blocked":
            # Waits a reboot cut short count against max_reboot_losses; a block starts the count over.
            extra["reboot_wakes"] = wakes
        shown = rstatus or status
        # A done reply that delivers nothing (replymsg.undelivered) is not posted: the task goes to
        # review saying so, so the coordinator sees that the chat got nothing.
        undelivered = replymsg.undelivered(result) if task["reply_chat"] and new == "done" else ""
        if undelivered:
            new, reason = "review", f"nothing was posted to the chat: {undelivered}"
            extra["undelivered"] = undelivered
            log(self.p, f"task {task['id']}: {reason}")
        if status in FREE_ENDS and not rstatus and (prev := load_result(task["result"])).get("status") == "waiting":
            # A wake the account refused is the same wait: its retry stays a cheap wake.
            extra.update({k: prev[k] for k in WAIT_KEYS if k in prev})
            shown = "waiting"
        upd = {"status": new, "attempts": attempts, "result": dump_result(
            {"summary": summary, "status": shown, **extra,
             **({k: v for k, v in result.items()
                 if k not in ("summary", "waits", "waiting_since", "woke", "reboot_wakes", "escalated_wake",
                              "stale_wakes", "wait_key", SPEC_SEEN_KEY, SPEC_WAKES_KEY, handoffcheck.KEY,
                              ROOT_WAKE_KEY)}
                if isinstance(result, dict) else {})})}
        if reason:
            upd["blocked_reason"] = reason[:500]
        elif new == "blocked":
            upd["blocked_reason"] = str(result.get("question") or result.get("blocked_reason") or summary)[:500]
        else:
            upd["blocked_reason"] = None
        if not_before:
            upd["not_before"] = not_before
        elif reboot_lost:
            upd["not_before"] = None
        elif new == "queued" and status not in FREE_ENDS:
            upd["not_before"] = time.time() + 120 * attempts
        if isinstance(result, dict) and result.get("pr"):
            if url := prguard.safe_pr_url(result["pr"])[:300]:
                upd["pr_url"] = url
            else:   # shown as a link in the web app: only an https:// PR URL is kept
                log(self.p, f"task {task['id']}: hand-off pr {str(result['pr'])[:120]!r} is not an https:// "
                            f"PR URL; not stored")
        if approval:
            with db.tx():   # rows, pins and the status together: a failed pin leaves the run to be retried
                pushq.approve(self.p, task["id"], r.get("id"), approval)
                db.update_task(task["id"], **upd)
                # Handled: nothing to decide until the batch reports (pushq.finalize).
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), f"task:{task['id']}", "push_queued", "normal",
                      f"{pushq.queued_text(task, approval)} (run {ended or status}, "
                      f"{'~' if usage.estimated else ''}${usage.cost_usd:.2f})", "handled", task["id"]))
        else:
            db.update_task(task["id"], **upd)
        effort.settle(db, task["id"], "done" if rejects else new, attempts)
        try:   # the wake-gate decisions behind this run: a wake that only handed the wait back is a miss
            waitgate.settle_run(db, json.loads(r.get("note") or "{}"), load_result(task["result"]), result,
                                isinstance(result, dict) and result.get("status") == "waiting")
        except Exception as e:
            log(self.p, f"task {task['id']}: wake gate settling failed: {type(e).__name__}: {e}")
        if new == "done" and worktree.own_worktree(task):
            self._local_only_due = 0.0   # is its work on a remote? checked this tick
            self._queue_backup(task)
        # git status of the project root can take long in a large repository: after the run-end
        # transaction commits, so it never holds the write lock every other process waits on.
        if not root_sent:   # its next hand-off reports what is left
            db.after_commit(lambda: self._note_dirty_main(task))
        if waiting and new == "queued" and extra.get("escalated_wake") and gate_probe:
            self._start_probe(task["id"], gate_probe, time.time())
        if waiting and new == "queued":
            # Self or external (unblock.classify_wait): only external waits count as stuck work.
            kind, why = ("self", "escalated wake") if extra.get("escalated_wake") else unblock.classify_wait(result)
            data: dict = {"wait": kind, "why": why, "for": what}
            if isinstance(result, dict):   # what coordinator.wait_raises compares besides the reason
                data.update(probe=unblock.wait_probe(result.get("retry_when")),
                            owner=unblock.wait_owner(result, task["id"]))
            if not extra.get("escalated_wake"):
                try:   # its live jobs or locks (unblock.live_wait), read before the job can end
                    live = unblock.live_wait(self.p, result)
                except Exception as e:   # the record of the wait matters more
                    log(self.p, f"could not read the live work #{task['id']} waits on: {e}")
                    live = None
                if live:
                    data["live"] = live
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_waiting", "low",
                  f"#{task['id']} {task['title']}: {reason}", json.dumps(data), "handled", task["id"]))
            if (now_t := db.task(task["id"])) and isinstance(result, dict):
                self._wait_started(now_t, result)
            return
        outcome = CHANGES_NEEDED if rejects else new
        if task["kind"] == "review" and new in ("done", "failed", "pushing", "blocked", "review"):
            try:   # routing data for the efficiency audit (reviewstats); a miss never stops the hand-off
                reviewstats.record(self.p, task, outcome)
            except Exception as e:
                log(self.p, f"review {task['id']}: its stats were not recorded: {e}")
        if task["reply_chat"] and new in ("done", "failed", "blocked"):
            text = replymsg.text(result) if new == "done" else f"(task #{task['id']} {outcome}) {summary}"
            need = upd.get("blocked_reason") if new == "blocked" else None
            if need and need not in summary:
                text += f"\nNeeds from you: {need}"
            chat = None if task["reply_chat"] == "all" else task["reply_chat"]
            msgparts.post(db, msgparts.cut(text, f"task #{task['id']}'s hand-off (`ttp task {self.p.name} show {task['id']}`)"),
                          chat=chat, kind="reply", severity="normal")
        sev = "high" if new == "blocked" else "normal"
        fups = result.get("followups") if isinstance(result, dict) else None
        fups = [f for f in fups if isinstance(f, dict) and f.get("title")] if isinstance(fups, list) else []
        # An `upstream:` note that names nothing of tt-project is this project's own follow-up.
        before = fups
        fups, local = upstream.reroute(fups)
        rerouted = {id(g) for f, g in zip(before, fups) if f is not g}   # never a failed review's fix
        for t in local:
            log(self.p, f"task {task['id']}: `upstream:` follow-up names nothing of tt-project; "
                        f"kept as this project's follow-up: {t[:200]}")
        try:
            upstream.append(self.p.name, task["id"], fups)   # the user's inbox of notes for tt-project
        except OSError as e:
            log(self.p, f"could not file upstream notes of #{task['id']}: {e}")
        # A plan's findings, plugin advice and follow-up specs are its product: each part gets its own
        # event, sized for the digest to show it whole, so an ordinary hand-off does not grow.
        where = _result_ref(self.p, run_dir if handoff is None else run_dir / RESULT_FILE)
        head = (f"#{task['id']} {task['title']} → {outcome} (run {ended or status}, {'~' if usage.estimated else ''}"
                f"${usage.cost_usd:.2f}): ")
        text = hcheck_text   # the daemon's own lines after the summary, which the digest must show whole
        if new == "done" and isinstance(result, dict) and isinstance(result.get(cells.NO_HOP), list):
            text += f"\nNo continue hop for missing cells (none a hop could produce): " \
                    + "; ".join(str(x) for x in result[cells.NO_HOP])[:600]
        if undelivered:
            text += (f"\nNot posted to the chat: {undelivered}. The user got nothing: post the deliverable "
                     f"(a `reply`) or rerun the task asking for it in result.json `{replymsg.FIELD}`.")
        notes = "".join(f"\nNot an upstream note (names nothing of tt-project), so proposed as this project's "
                        f"follow-up: {t[:200]}" for t in local)
        if len(fups) > MAX_FOLLOWUPS:
            notes += (f"\nMore proposed follow-ups (their specs are in {where}): "
                      + "; ".join(str(f["title"])[:120] for f in fups[MAX_FOLLOWUPS:]))
        if isinstance(result, dict):
            facts = [f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("fact")]
            if facts:
                notes += "\nFindings (save the durable ones as memory):" + "".join(
                    f"\n- {f['fact']} [{f.get('source', '')}]" for f in facts)
            plugs = [x for x in (result.get("enable_plugins") or []) if isinstance(x, dict) and x.get("path")]
            if plugs:
                notes += "\nRecommended skill plugins for workers:" + "".join(
                    f"\n- {x['path']}: {x.get('why', '')}" for x in plugs)
        if fups and task["kind"] == "code":
            try:   # model-free, on every code hand-off: no worker has to run it
                for line in handofflint.lint(self.p, self.cfg, task, result):
                    notes += "\n" + line
                    log(self.p, f"task {task['id']}: {line}")
            except Exception:
                log(self.p, "hand-off lint failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
        # A retry the daemon already scheduled, after a refusal (which has its own alert) or a run that
        # ended without a verdict, leaves nothing to decide: the final attempt's outcome starts the turn.
        # A timeout still does, since the task may need splitting before it times out again.
        quiet = new == "queued" and status in (*FREE_ENDS, "failed", "lost", "stalled", "no_handoff",
                                               "refused_wait") or push_quiet
        # A finished code task's next step is its review, queued here in the same step whatever else the
        # hand-off holds: its follow-ups go into the review's spec and stay the coordinator's to queue.
        # A hand-off with nothing else to decide (no follow-ups or notes, normal severity) starts no turn.
        plain = not fups and not notes and sev == "normal" and not hcheck_text and not cell_mark
        # A done hop whose only proposal is the next `continue:` hop, with progress and under the
        # chain caps, gets it from the daemon (chaincap). A code task that left its change unfinished
        # (a split: a `continue:` hop for its branch, or a head its hand-off marks WIP) gets no review:
        # its hop takes the branch over, and the chain's last hop gets the one review.
        ready = self._continue_ready(dict(task, **upd), fups) \
            if new == "done" and not approval and not notes and sev == "normal" else ""
        split = self._split(dict(task, **upd), result, fups) if new == "done" and task["kind"] == "code" else ""
        if split:
            text += (f"\nSplit at head {split[:12]}: no review of it; its continue hop builds on this branch, and "
                     f"the chain's last hop gets the one review.")
        review = self._auto_review(dict(task, **upd), summary) \
            if new == "done" and task["kind"] == "code" and not split else None
        # A fix of a failed review that adds no commit to the head that review saw leaves its re-review
        # nothing new to check: the re-review is blocked, never run on the same head, and the coordinator
        # decides.
        stale = self._fix_without_commits(dict(task, **upd)) \
            if new in ("done", "review") and task["kind"] == "code" else []
        for r in stale:
            why = (f"fix #{task['id']} ended with no new commits on {task['branch']} after the head "
                   f"{r['base']} its review saw: nothing new to re-review")
            db.update_task(r["id"], status="blocked", blocked_reason=why)
            text += f"\nRe-review #{r['id']} blocked: {why}. Requeue a fix, or cancel the re-review."
            log(self.p, f"task {task['id']}: re-review {r['id']} blocked, the fix added no commits")
        if review:
            known = None if review[1] else db.task(review[0])
            text += f"\nReview #{review[0]} " + ("queued by the daemon." if review[1] else
                                                 "already accepted this head: no new review." if known and
                                                 known["status"] == "done" else "was already queued.")
        # A review that failed with fix specs gets its fix and re-review from the daemon, and what waited
        # on it waits on the re-review instead of being blocked. A failure without them still blocks.
        # Upstream notes (also those rerouted to this project) and deferred follow-ups are not fixes:
        # they stay with the coordinator.
        fixes = [f for f in fups if not upstream.is_note(f) and id(f) not in rerouted
                 and not f.get("start_after") and not f.get("start_when")]
        # Across stacks, an area whose reviews keep failing gets no more automatic fixes: the
        # coordinator re-plans it (reviewcap).
        # A check-only failure stays failed: the coordinator routes the missing check to its owner,
        # and what waited on the review stays on it.
        check_only = new == "failed" and task["kind"] == "review" and review_failure(result) == "check_only"
        area = self._review_area(dict(task, **upd)) \
            if new == "failed" and task["kind"] == "review" and not check_only else None
        fix = self._fix_failed_review(dict(task, **upd), fixes, summary) \
            if new == "failed" and task["kind"] == "review" and fixes and len(fups) <= MAX_FOLLOWUPS \
            and not area and not check_only else None
        # A failed review left with no fix (a capped round, no fix follow-ups, findings on branches
        # that are not one stack) wakes the coordinator, never silently.
        nofix = "" if fix or area or check_only or new != "failed" or task["kind"] != "review" else \
            getattr(self, "_nofix", "") if fixes and len(fups) <= MAX_FOLLOWUPS else \
            f"it proposed more than {MAX_FOLLOWUPS} follow-ups" if fixes else "it proposed no fix follow-ups"
        # With no fix follow-ups the task_failed event below is the wake (it is queued): it says why,
        # the branch and what waits. Fixes the daemon could not queue get their own review_nofix event.
        reviewed = next((c["branch"] for i in dependency_ids(task) if isinstance(i, int)
                         and (c := db.task(i)) and c.get("branch")), "") if nofix else ""
        if nofix and not fixes and not approval:
            held = deadwait.waiters(db, {i for i in dependency_ids(task) if isinstance(i, int)} | {task["id"]})
            text += (f"\nNo automatic fix: {nofix}. Branch: {reviewed or 'none found'}. "
                     f"Waiting on it or its change: {deadwait.names(held)}.")
            nofix = ""
        elif nofix:
            text += f"\nNo automatic fix: {nofix} ({deadwait.NOFIX_EVENT} raised)."
        folded = {id(f) for f in fixes} if fix else set()
        if fix:
            moved = ", ".join(f"#{i}" for i in fix[2])
            text += (f"\nFix #{fix[0]} and re-review #{fix[1]} queued by the daemon"
                     + (f"; {moved} now wait on #{fix[1]}." if moved else "."))
        if check_only:
            text += ("\nCheck-only failure (review_failure: check_only): no automatic fix or re-review. Route the "
                     "missing or inapplicable check to the task that owns it; what waits on this review stays on it.")
        if outcome == "done" and task["kind"] == "review":
            text += relay_hint(coord._open_dependents(db, task["id"]))
        if area:
            text += f"\nNo automatic fix: {reviewcap.TRIGGER} (lineage of #{area['root']})."
        # The hop the daemon queues: the next digest says so. A split's hop takes its branch over and
        # starts at once, never on the split's landing (that comes only with the hop's own work). A
        # code hop whose branch has changes and is no split (they already landed under other commits)
        # starts from the base. With changes and no review that is neither, it stays with the
        # coordinator, or its commits would never be reviewed or delivered.
        changes = bool(ready) and not fix and not split and task["kind"] == "code" and self._has_changes(task)
        landed_now = changes and bool(db.one("SELECT 1 FROM task_superseded WHERE task=?", (task["id"],)))
        hop = self._auto_continue(db.task(task["id"]) or dict(task, **upd), fups, ready) \
            if ready and not fix and (review or split or not changes or landed_now) else None
        if hop:
            folded.add(id(fups[0]))
            text += f"\nContinue hop #{hop[0]} queued by the daemon ({hop[1]})" + (
                f"; it builds on #{task['id']}'s branch and starts now." if split else ".")
        elif new == "done" and any(chaincap.CONT_TITLE.match(str(f["title"])) for f in fups):
            # A post-landing live step (a deploy, install or rollout) is no hop: it is proposed as deploy tasks.
            deploys = [d for f in fups if chaincap.CONT_TITLE.match(str(f["title"]))
                       and (d := chaincap.deploy_proposal(f, task["id"]))]
            text += f"\nContinue follow-up {UNQUEUED_TAG}: " + (
                deploys[0] + "." if deploys else f"add it with continues #{task['id']} or drop it.")
        elif split:
            text += (f"\nNo continue hop finishes its split head: add one with continues #{task['id']} (it builds "
                     f"on this branch), or a review of it if the work is complete.")
        routine = ((review is not None and plain) or fix is not None or hop is not None) and not stale
        # The summary takes what the digest's cap leaves after the header, push note and daemon lines.
        room = min(1200, coord.EVENT_CHARS - len(head) - len(push_note) - len(text))
        text = f"{head}{_cut(summary, max(200, room), where)}{push_note}{text}"
        if hop:
            chaincap.add_notice(db, text)
        if not approval:   # an approval has its push_queued event; the batch's outcome closes the task
            # A plain needs_review hand-off: the daemon reviews it, so it raises no effort (coordinator._routine_review).
            data = json.dumps({"plain": True}) if outcome == "review" and plain else None
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", f"task_{outcome}", sev, text, data,
                  "handled" if quiet or routine else "queued", task["id"]))
        if nofix:
            if deadwait.raise_nofix(db, db.task(task["id"]) or dict(task, **upd), nofix, reviewed):
                log(self.p, f"review {task['id']}: no fix queued ({nofix}); raised to the coordinator")
        if area:
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", reviewcap.REVIEW_AREA_EVENT, "normal",
                  reviewcap.event_text(area), json.dumps(area), "queued", task["id"]))
            log(self.p, f"review {task['id']}: {reviewcap.TRIGGER} in the lineage of #{area['root']} "
                        f"(reviews {area['reviews']}); no automatic fix")
        if notes:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_notes", "normal",
                  _cut(f"#{task['id']} {task['title']}:{notes}", coord.EVENT_CHARS_BY_KIND["task_notes"], where),
                  "handled" if quiet and len(fups) <= MAX_FOLLOWUPS else "queued", task["id"]))
        for f in fups[:MAX_FOLLOWUPS]:
            # A deferred follow-up names when it may start; the coordinator adds it with those fields.
            start = "; ".join(f"{k}: {str(f[k])[:300]}" for k in ("start_after", "start_when") if f.get(k))
            # A `continue:` follow-up the daemon did not queue as the next hop says so: only a task_add
            # with `continues` starts it (else continue_unqueued is raised, check_unqueued_continues).
            tag = f" ({UNQUEUED_TAG}: " + (chaincap.deploy_proposal(f, task["id"])
                                            or f"add it with continues #{task['id']} or drop it") + ")" \
                if id(f) not in folded and chaincap.CONT_TITLE.match(str(f["title"])) else ""
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "followup_proposed", "normal",
                  f"proposed follow-up{tag}: "
                  f"{str(f['title'])[:200]}{f' [{start}]' if start else ''} — "
                  f"{_cut(str(f.get('spec', '')), FOLLOWUP_SPEC_CHARS, where)}",
                  "handled" if id(f) in folded else "queued", task["id"]))

    # money ----------------------------------------------------------------------------------------
    def _refresh_meters(self, every_s: float = METER_EVERY_S) -> None:
        """Providers that expose plan windows for free (no model call) are read in the background,
        so headroom stays current between runs. Claude's windows arrive with every run instead. A
        provider whose gate is red is always read, every `every_s` (10 min), so held work starts on
        the first reading under the line. Each read also takes the provider's credits and free
        window resets, and may consume one (credits.consume)."""
        self._report_resets()
        now = time.time()
        if now - getattr(self, "_last_meter", 0) < every_s or getattr(self, "_metering", False):
            return
        self._last_meter, self._metering = now, True
        wanted = {self.cfg.get("core_provider", "claude")} | {t["provider"] for t in self.p.db.q(
            "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")} | {
            p for p, g in self.gates.items() if g.level == "red"}

        def work() -> None:
            from .db import DB
            db = DB(self.p.state / "project.db")        # this thread's own connection
            try:
                self._meter_providers(db, sorted(wanted - {"claude", "fake"}))
            finally:
                db.close()
                self._metering = False
        threading.Thread(target=work, daemon=True).start()

    def _meter_providers(self, db, names: list[str]) -> None:
        """Read each provider's windows and credits; when a free reset is due, read again inside the
        account's machine-wide lock, consume it and read again at once, so held work starts on the
        next tick."""
        for name in names:
            try:
                prov = get_provider(name)
                wins = self._meter_one(db, name, prov)
                remeter = lambda n=name, pv=prov: (self._meter_one(db, n, pv), pv.last_credits)  # noqa: E731
                if wins and credits.consume(db, self.cfg, name, prov, wins, prov.last_credits, remeter=remeter):
                    self._meter_one(db, name, prov)
            except Exception:
                log(self.p, f"meter {name} failed: " + traceback.format_exc().replace("\n", " | ")[:800])

    @staticmethod
    def _record_readings(db, provider: str, account: str, usage, now: float | None = None) -> None:
        """A run's plan windows and its credit reading (Claude's overage fields), for its account."""
        wins = usage.extra.get("windows")
        if wins and provider == "claude":
            bud.record_windows(db, as_windows(wins, account))
        elif wins:
            bud.record_windows(db, [bud.Window(provider, w["window"], w["utilization"], w.get("resets_at"), account)
                                    for w in wins])
        if isinstance(usage.extra.get("credits"), dict):
            credits.record(db, provider, usage.extra["credits"], account, now)

    @staticmethod
    def _meter_one(db, name: str, prov) -> list:
        prov.last_credits = None
        try:
            wins = prov.meter()
        except Exception:
            wins = []
        if wins:
            bud.record_windows(db, wins)
        if prov.last_credits is not None:
            credits.record(db, name, prov.last_credits, wins[0].account if wins else "")
        return wins

    def _report_resets(self) -> None:
        """Log each free reset consumed (or given up on) and report it once at low severity. Kept in
        the state until reported, so a restart between the consume and its report loses neither."""
        for r in self.p.db.q("SELECT key FROM kv WHERE key LIKE ?", (credits.USED + "%",)):
            prov = r["key"][len(credits.USED):]
            news = credits.unreported(self.p.db, prov)
            for u in news:
                text = credits.report_text(prov, u, lambda t: timefmt.long(t, self.p))
                log(self.p, text)
                self.alert(f"reset-credit:{prov}:{u.get('credit')}", text, severity="low", every_s=0)
            if news:
                credits.mark_reported(self.p.db, prov, news)

    def _provider_pause(self, prov: str) -> dict | None:
        """The provider's pause, or None once a logged-out pause is over because a login changed its
        credential files. The check is a stat, so a waiting pause costs no model call."""
        lim = self.p.db.kv(f"limited:{prov}")
        if not lim or lim.get("note") != "logged out" or not lim.get("creds") or lim.get("until", 0) <= time.time():
            return lim
        try:
            stamp = get_provider(prov).credentials_stamp()
        except Exception:   # an unknown provider keeps its pause until it expires
            return lim
        if not stamp or stamp == lim["creds"]:
            return lim
        self.p.db.set_kv(f"limited:{prov}", {**lim, "until": 0, "note": "credentials changed"})
        self.p.db.set_kv(f"auth_probe:{prov}", 0)   # a login is checked by the next run, not in 15 minutes
        log(self.p, f"{prov} credentials changed; ending the logged-out pause")
        return None

    def _logged_out(self, prov: str) -> bool:
        """Whether `prov`'s auth breaker is open (check_logins closes it)."""
        return alerts.breaker(self.p.db, prov) is not None

    def _may_probe(self, prov: str, now: float) -> bool:
        """While `prov` is logged out, whether one run on it may start now to check the login: only for
        a CLI without a model-free login check, when its pause is over, nothing runs on it, and the
        last check started AUTH_PROBE_S ago or more. Starting every queued task instead would burn
        one failed run each per pause."""
        if not (alerts.breaker(self.p.db, prov) or {}).get("probe"):
            return False   # its CLI's own status check decides (check_logins)
        lim = self._provider_pause(prov)
        if lim and lim.get("until", 0) > now:
            return False
        if self.p.db.one("SELECT id FROM runs WHERE provider=? AND status='running' LIMIT 1", (prov,)):
            return False
        return now >= float(self.p.db.kv(f"auth_probe:{prov}", 0) or 0) + AUTH_PROBE_S

    def open_breaker(self, prov: str, why: str = "", at: float | None = None) -> None:
        """Open `prov`'s auth breaker: no run starts on it (queued tasks keep their attempts) until
        check_logins sees it logged in again. Kept in the project's state, so a restart keeps it."""
        db = self.p.db
        prev = db.kv(alerts.BREAKER + prov) or {}
        if prev.get("open"):
            return
        now = time.time() if at is None else at
        try:
            stamp = get_provider(prov).credentials_stamp()
        except Exception:
            stamp = ""
        # The account it ran as before the logout, as runs record it (opaque), and its kind: a login
        # that comes back on another account is held for one more check, then named (check_logins).
        acct = budgetkind.account_hash(bud.current_accounts(db).get(prov) or "")
        # Its CLI said logged in, yet a run was refused again before any got through: that check
        # proves nothing for this logout, or every close would release the queue to fail once more.
        distrust = prev.get("closed_why") == LOGIN_CHECK_PASSED and \
            not alerts.login_proven(db, prov, float(prev.get("closed") or 0))
        db.set_kv(alerts.BREAKER + prov, {"open": True, "opened": now, "checks": 0, "creds": stamp,
                                          **({"account": acct, "account_kind": self._account(prov)[1]} if acct else {}),
                                          "next_check": time.time() + AUTH_CHECK_S[0], "why": why[:200],
                                          **({"distrust": True} if distrust else {})})
        log(self.p, f"{prov}: logged out; no runs start on it until a login check passes")

    def _account(self, prov: str) -> tuple[str, str]:
        """The account `prov` is logged in as now: its opaque id (budgetkind.account_hash of the label
        runs record) and its kind (never an email address)."""
        try:
            agent = get_provider(prov)
            return budgetkind.account_hash(agent.account() or ""), agent.account_kind() or ""
        except Exception:
            return "", ""

    def _account_switched(self, prov: str, rec: dict) -> dict | None:
        """{"from", "to"} kinds when `prov` is logged in again as another account than before the
        logout; None when it is the same one, or either is unknown."""
        now_acct, kind = self._account(prov)
        if not rec.get("account") or not now_acct or now_acct == rec["account"]:
            return None
        return {"from": rec.get("account_kind") or "", "to": kind}

    def _close_breaker(self, prov: str, rec: dict, why: str, switched: dict | None = None) -> None:
        now = time.time()
        with self.p.db.tx():
            self.p.db.set_kv(alerts.BREAKER + prov, {"open": False, "opened": rec.get("opened"), "closed": now,
                                                     "checks": rec.get("checks", 0), "closed_why": why,
                                                     **({"switched": switched} if switched else {})})
            self.p.db.set_kv(f"auth_probe:{prov}", 0)
            if switched:
                # Recorded only: the budget fingerprint (budgetkind) re-plans for the new account.
                self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
                            (now, "daemon", ACCOUNT_SWITCH_EVENT, "low",
                             f"{prov} is logged in again as another account than before the logout "
                             f"({alerts.switch_text(switched)}).", "handled",
                             json.dumps({"provider": prov, **switched})))
        log(self.p, f"{prov}: {why}; runs start on it again"
                    + (f" (another account: {alerts.switch_text(switched)})" if switched else ""))

    def check_logins(self) -> None:
        """Close each open auth breaker once its provider is logged in again: its CLI says so (a
        model-free status check on the AUTH_CHECK_S backoff, at once when its credential files
        change), or a run started since it opened got past the login. A CLI without a status check
        says nothing; one run then checks the login every AUTH_PROBE_S (_may_probe). A login back as
        another account than before the logout waits one more check; still another then, it closes
        and says so (_close_breaker)."""
        db, now = self.p.db, time.time()
        # A logged-out alert from before the breaker existed (an upgrade) opens it.
        for ep in db.q("SELECT key, MIN(raised) raised FROM alerts WHERE key LIKE 'auth:%' AND cleared IS NULL "
                       "GROUP BY key"):
            prov = ep["key"].split(":", 1)[1]
            rec = db.kv(alerts.BREAKER + prov) or {}
            if not rec.get("open") and float(rec.get("closed") or 0) < ep["raised"]:
                self.open_breaker(prov, "logged out", at=ep["raised"])
        for row in db.q("SELECT key FROM kv WHERE key LIKE ?", (alerts.BREAKER + "%",)):
            prov = row["key"][len(alerts.BREAKER):]
            rec = alerts.breaker(db, prov)
            if not rec:
                continue
            if alerts.login_proven(db, prov, float(rec.get("opened") or 0)):
                self._close_breaker(prov, rec, "a run on it got past the login", self._account_switched(prov, rec))
                continue
            try:
                agent = get_provider(prov)
                stamp = agent.credentials_stamp()
            except Exception:
                agent, stamp = None, ""
            changed = bool(stamp) and stamp != rec.get("creds")
            # Held for another account (below): the next check waits its step, so a switch can finish.
            if now < float(rec.get("next_check") or 0) and (not changed or rec.get("account_hold")):
                continue
            try:
                ok = agent.login_check() if agent and not rec.get("distrust") else None
            except Exception:
                log(self.p, f"{prov}: login check failed\n" + traceback.format_exc())
                ok = None
            if ok or (ok is None and changed):
                switched = self._account_switched(prov, rec)
                if switched and not rec.get("account_hold"):
                    # Logged in again as another account than before the logout: the user may be
                    # mid-switch. Nothing starts until one more check; still another account then, it starts.
                    db.set_kv(alerts.BREAKER + prov, {**rec, "account_hold": now, "last_check": now, "creds": stamp,
                                                      "next_check": now + AUTH_CHECK_S[0]})
                    log(self.p, f"{prov}: logged in again as another account; one more login check before runs start")
                    continue
                self._close_breaker(prov, rec, LOGIN_CHECK_PASSED if ok else "its credentials changed", switched)
                continue
            n = int(rec.get("checks") or 0) + 1
            db.set_kv(alerts.BREAKER + prov, {**rec, "checks": n, "last_check": now, "creds": stamp, "probe": ok is None,
                                              "next_check": now + AUTH_CHECK_S[min(n, len(AUTH_CHECK_S) - 1)]})

    def update_gates(self) -> None:
        provs = {self.cfg.get("core_provider", "claude"), *[t["provider"] for t in self.p.db.q(
            "SELECT DISTINCT provider FROM tasks WHERE provider IS NOT NULL AND status IN ('queued','running')")]}
        provs |= {to[0] for tier in bud.TIER_ORDER if (to := route.target(self.cfg, tier))}   # routed slots' gate
        # Plan readings count only for the account each provider is logged in as now, so a switch
        # of account drops the old plan's windows before the next run.
        for prov in provs:
            try:
                bud.note_account(self.p.db, prov, get_provider(prov).account())
            except Exception:
                pass
        windows = bud.plan_windows(self.p.db)
        gates, news, red_sent, episodes = {}, [], {}, {}
        now = time.time()
        # After a restart the last levels come from disk, so a change while the daemon was down is news.
        saved = {} if self.gates else (self.p.db.kv("gates") or {})
        accounts = bud.current_accounts(self.p.db)
        for prov in provs:
            g = bud.evaluate(self.p.db, self.cfg, prov, windows)
            self._budget_changed(prov, g, accounts.get(prov) or "")   # before the pause: it may release it
            lim = self._provider_pause(prov)
            if lim and lim.get("until", 0) > time.time():
                bud._raise(g, "red", f"provider limit: {lim.get('note')}")
                g.max_parallel, g.allow_new_work, g.allow_optional = 0, False, False
            prev = self.gates.get(prov) or _saved_gate(saved.get(prov))
            provider_paused = any(r.startswith("provider limit") for r in g.reasons + (prev.reasons if prev else []))
            # Green, yellow and orange are the budget working as designed (more or fewer workers);
            # only red is news to the user, once per plan window or per episode of its other reasons.
            # Its alert clears itself once the gate leaves red.
            new_episode, episodes[prov] = self._red_episodes(prov, g, now)
            first, mark = False, None
            if prev and prev.level != "red" and g.level == "red" and not provider_paused:   # a pause has its own alert
                plan_first, mark = self._first_red_in_window(prov, g)
                classed = any(bud.red_class(r) for r in g.reasons)
                first = bool(plan_first) or new_episode or (plan_first is None and not classed)
                if not first:
                    log(self.p, f"budget for {prov} red again in the same episode, no new alert: {'; '.join(g.reasons)}")
            if mark is not None:
                red_sent[prov] = mark
            if first:
                capped = any("cap reached" in r for r in g.reasons)
                hint = f"New work on {prov} waits; running work finishes and replies to you continue. " + (
                    "It starts again by itself when the budget day resets. "
                    if any(r.startswith("global daily cap") for r in g.reasons) else
                    "It starts again by itself once the gate leaves red. ") + (
                    "The caps are in the web app's Budget tab." if capped
                    else "The web app's Budget tab shows what spent it.")
                # Every red gate lifts by itself (a window resets, a cap's day or hour passes): a
                # status line that clears itself, never "needs you" (alerts.STATUS_CONDITIONS).
                news.append((f"Budget for {prov} is now red: {'; '.join(g.reasons)}. " + hint,
                             "low", f"budget:{prov}"))
            gates[prov] = g
        # The global cap counts only for providers billed by usage: a project whose providers are all
        # on plan windows needs no other machines' totals and no scan of local sessions.
        if any(g.regime == "caps" for g in gates.values()):
            gcap.refresh_async(self.cfg.get("budget") or {})   # other machines' totals, in the background
            localspend.scan_async(self.cfg.get("budget") or {})   # other local Claude Code sessions
        # One transaction: saved gates without their alert would hide the change from every later
        # tick and restart. Gates first: a relay reading an alert before the gates show red would
        # count it as cleared.
        with self.p.db.tx():
            self.p.db.set_kv("gates", {k: v.as_dict() for k, v in gates.items()})
            if red_sent:   # the window's marker commits with its alert, or neither does
                self.p.db.set_kv("budget_red_sent",
                                 {**(self.p.db.kv("budget_red_sent", {}) or {}), **red_sent})
            known = self.p.db.kv("budget_red_episodes", {}) or {}
            if any((known.get(k) or {}) != v for k, v in episodes.items()):   # so is an episode's start
                self.p.db.set_kv("budget_red_episodes", {**known, **episodes})
            for text, sev, ref in news:
                self.p.db.post("out", text, chat=None, kind="alert", severity=sev, ref=ref)
        self.gates = gates
        # A login with no plan readings of its own yet, its kind unknown: the dollar caps hold it
        # meanwhile (the stricter side). A self-clearing status line, never "needs you": its next run
        # reports its windows, or shows it is billed by use (then budget.evaluate drops the flag).
        for prov, g in gates.items():
            if g.numbers.get("account_mismatch"):
                self.alert(f"account-mismatch:{prov}",
                           f"{prov}'s current account has no plan readings of its own yet, so its spend counts "
                           f"toward the dollar caps (the stricter gate) for now. This clears by itself once a run "
                           f"reports its plan windows or shows it is billed by use.",
                           severity="low")

    def _budget_changed(self, prov: str, g: bud.Gate, account: str) -> None:
        """A change of the provider's account or budget kind (budgetkind.py) releases the holds the old
        limits made and queues the coordinator's re-plan, once per change (none when the kind is the same and
        nothing is tied to the old limits)."""
        try:
            with self.p.db.tx():
                change = budgetkind.observe(self.p.db, prov, budgetkind.parts(self.p.db, self.cfg, prov, g, account),
                                            resets=budgetkind.plan_resets(g))
                done = budgetkind.apply(self.p.db, self.cfg, prov, change) if change else None
        except Exception:
            log(self.p, f"budget change check {prov} failed: " + traceback.format_exc().replace("\n", " | ")[:800])
            return
        if done:
            log(self.p, f"{prov} budget changed: {'; '.join(budgetkind.what_changed(change['old'], change['new']))}; "
                        f"released {len(done['released'])}, {len(done['tasks'])} tied task(s) for the coordinator"
                        + ("" if done.get("event") else "; same budget kind, nothing to re-plan: no event"))

    def _red_episodes(self, prov: str, g: bud.Gate, now: float) -> tuple[bool, dict]:
        """Whether the gate starts a new red episode of one of its reason classes (bud.red_class: the
        runaway guard, a dollar cap, ...), and the provider's new budget_red_episodes entry for the
        caller to save in the alert's transaction. An episode ends only once its measured value falls
        below RED_CLEAR_RATIO of its limit (unmeasured: once the class leaves red), and the same
        class red again within RED_REPEAT_S of that end continues it: a value hovering at its limit
        is one alert, not one per crossing. Gating is unchanged: new work still waits at the limit."""
        old = (self.p.db.kv("budget_red_episodes", {}) or {}).get(prov) or {}
        red = {c for c in map(bud.red_class, g.reasons) if c} if g.level == "red" else set()
        new, out = False, {}
        for cls in sorted(red | set(old)):
            ep = dict(old.get(cls) or {})
            if cls in red:
                if not ep or (not ep.get("open") and now - float(ep.get("cleared") or 0) >= RED_REPEAT_S):
                    new, ep = True, {"since": now}
                ep["open"] = True
            elif ep.get("open"):
                share = bud.red_share(g, cls)
                if share is None or share < RED_CLEAR_RATIO:
                    ep.update(open=False, cleared=now)
            elif now - float(ep.get("cleared") or 0) >= RED_REPEAT_S:
                continue   # long over: a next red is news again
            out[cls] = ep
        return new, out

    def _first_red_in_window(self, prov: str, g: bud.Gate) -> tuple[bool | None, dict | None]:
        """Whether a red gate at a plan line is the first in that plan window, and the provider's
        new budget_red_sent entry (or None) for the caller to save in the alert's transaction. The
        line holds until the window resets, so one alert per window is enough. (None, None) when
        no window is at its line: red for another reason (_red_episodes)."""
        n = g.numbers or {}
        line = float(n.get("limit") or 100)
        at_line = [r for r in n.get("plan") or [] if r.get("resets_at") and r["utilization"] >= line]
        if not at_line:
            return None, None
        sent = self.p.db.kv("budget_red_sent", {}) or {}
        mine = {w: t for w, t in (sent.get(prov) or {}).items() if float(t) > time.time()}
        # A window's reset time may move by seconds between readings; the next window's is hours on.
        if all(abs(float(mine.get(r["window"], 0)) - float(r["resets_at"])) < 600 for r in at_line):
            return False, None
        mine.update({r["window"]: r["resets_at"] for r in at_line})
        return True, mine

    # schedules and watchers ------------------------------------------------------------------------
    def run_schedules(self) -> None:
        """Start each due schedule whose last run ended and record the runs that ended. Commands, heal
        checks and builtin watchers run off the tick (schedrun), so a hung one (a gh call, ssh to a box
        that does not answer) never holds messages or dispatch; the tick waits at most SCHEDULE_WAIT_S
        for them. A run past its timeout (_schedule_timeout) has its process groups killed and is
        recorded as a failed run; one still going schedrun.STUCK_FACTOR x its timeout after that kill (a
        builtin watcher stuck in Python) is observed once more, under its own key. An llm schedule with a
        `precheck` runs off the tick too, so its shell check never holds the tick either."""
        db, runs = self.p.db, self._sched_runs
        if not self._sched_swept:   # what an earlier daemon's runs left: their schedules are still due
            self._sched_swept = True
            for line in schedrun.kill_all(db):
                log(self.p, line)
        due = [s for s in sched.due(db) if s["name"] not in runs]   # a running one: never two at once
        try:
            self._schedule_gaps(due)
        except Exception:
            log(self.p, "schedule gaps: " + traceback.format_exc().replace("\n", " | ")[:1000])
        try:
            self._schedule_cap_warn()
        except Exception:
            log(self.p, "schedule cap warning: " + traceback.format_exc().replace("\n", " | ")[:1000])
        for s in due:
            payload = json.loads(s["payload"] or "{}")
            if s["kind"] in ("command", "watcher") or (s["kind"] == "llm" and _precheck_cmd(payload)):
                runs[s["name"]] = self._start_schedule(s, payload)
                continue
            try:
                status = self._schedule_llm(s, payload)
            except Exception as e:
                status = f"error: {type(e).__name__}: {e}"[:200]
            self._schedule_ran(s, status, None)
        until = time.monotonic() + SCHEDULE_WAIT_S
        for run in list(runs.values()):
            if not run.timed_out:
                run.done.wait(max(0.0, until - time.monotonic()))
        for name, run in list(runs.items()):
            if run.done.is_set():
                runs.pop(name)
                if run.timed_out:
                    log(self.p, f"schedule {name}: its run recorded failed at its timeout ended after "
                                f"{time.monotonic() - run.started:.0f} s; its result is dropped")
                elif run.stopped:
                    log(self.p, f"schedule {name}: its run was stopped with the daemon; nothing recorded")
                else:
                    self._schedule_ran(run.s, *run.result)
            elif run.timed_out:
                if not run.stuck and time.monotonic() >= run.killed_at + schedrun.STUCK_FACTOR * run.timeout:
                    run.stuck = True
                    self.observe(scr.error_source(f"watcher:{name}"), f"schedule {name}: its run killed at its "
                                 f"timeout ({run.timeout:.0f} s) is still going "
                                 f"{time.monotonic() - run.killed_at:.0f} s later, so the schedule cannot run "
                                 f"again until its run ends or the daemon restarts", "normal",
                                 lifecycle=scr.ERROR, key=f"schedule-stuck:{name}", whole=True)
            elif time.monotonic() >= run.deadline:
                run.timed_out = True   # kept in runs until its thread ends: not started again before
                run.killed_at = time.monotonic()
                killed = schedrun.kill(db, run)
                why = f"error: timed out after {run.timeout:.0f} s"
                self.observe(scr.error_source(f"watcher:{name}"), f"schedule {name} {why[7:]}: its run was "
                             f"stopped ({killed} process group(s) killed) and counts as failed", "normal",
                             lifecycle=scr.ERROR)
                self._schedule_ran(run.s, why, None)

    def _schedule_cap_warn(self) -> None:
        """An llm schedule whose last-24-h spend passes sched.CAP_WARN of its own daily cap while the
        budget gate is green raises one normal keyed observation (SCHEDULE_CAP_SOURCE, key
        schedule-cap-80:<name>) per budget day (budget.day_start; without one, per 24 h), before the
        cap starts skipping runs. A new budget day closes it. Nothing changes a cap by itself."""
        db, now = self.p.db, time.time()
        gate = self.gates.get(self.cfg.get("core_provider", "claude"))
        green = gate is None or (gate.allow_optional and gate.level == "green")
        day = gcap.day_bounds(self.cfg.get("budget") or {}, now)
        raised = db.kv(SCHEDULE_CAP_KEY) or {}
        kept = {n: v for n, v in raised.items()
                if (v.get("day") == day[0] if day else v.get("day") is None and now - float(v["at"]) < 86400)}
        for name in set(raised) - set(kept):   # its budget day is over: the warning resets
            for r in db.q("SELECT id, history FROM issues WHERE source=? AND watch_key=? AND status='open'",
                          (SCHEDULE_CAP_SOURCE, f"schedule-cap-80:{name}")):
                history = json.loads(r["history"] or "[]") + [{"ts": now, "action": "closed", "by": "daemon",
                                                               "why": SCHEDULE_CAP_CLEARED}]
                db.x("UPDATE issues SET status='fixed', closed=?, cleared_why=?, history=? WHERE id=?",
                     (now, SCHEDULE_CAP_CLEARED, json.dumps(history), r["id"]))
        if green:
            for r in db.q("SELECT * FROM schedules WHERE enabled=1 AND kind='llm' AND budget_usd_day > 0"):
                name, cap = r["name"], float(r["budget_usd_day"])
                if name in kept or (spent := sched.spent_today(db, name)) < sched.CAP_WARN * cap:
                    continue
                hint = sched.cap_text(sched.suggest_cap(db, r))
                text = (f"Schedule {name} has spent ${spent:.2f} of its own ${cap:.2f} daily cap in the last 24 h "
                        f"({spent / cap:.0%}) while the budget gate is green: past the cap it skips runs the "
                        f"project could afford. {hint + '. ' if hint else ''}If its runs matter, raise its daily "
                        f"cap (`budget_usd`), lower its per-run `budget_usd_run`, or run it less often; caps "
                        f"never change by themselves.")
                kept[name] = {"day": day[0] if day else None, "at": now}
                log(self.p, f"schedule {name}: ${spent:.2f} of ${cap:.2f} daily cap; observation raised")
                self.observe(SCHEDULE_CAP_SOURCE, text, "normal", whole=True, key=f"schedule-cap-80:{name}")
        if kept != raised:
            db.set_kv(SCHEDULE_CAP_KEY, kept)

    def _schedule_gaps(self, due: list[dict]) -> None:
        """Due schedules that start whole periods late (sched.missed) left a hole in what they record:
        log each and tell the coordinator once, with the window and its likely cause (the host down,
        the daemon down, or a tick or the schedule held up while the daemon ran). An orderly cause (a
        clean daemon stop, an orderly or planned reboot) is a record in the digest that wakes nobody;
        any other is a normal observation, so a crash, an outage or a stuck tick is never silent."""
        db, now = self.p.db, time.time()
        sleeps = db.kv("host_sleeps", []) or []
        if due:   # a project pause is no gap either: nothing runs while paused, by design
            sleeps = sleeps + projpause.windows(db, min(float(s["next_run"]) for s in due), now)
        gaps = [(s, n) for s in due if (n := sched.missed(s, now, sleeps))]
        if not gaps:
            return
        t = lambda ts: timefmt.short(float(ts), self.p, now)
        since = min(float(s["next_run"]) for s, _ in gaps)
        prev, stop = db.kv("boot_prev") or {}, self._last_stop
        if self._started <= since:
            cause, orderly = "the daemon was running: its tick or the schedule was held up", False
        elif self._this_boot(prev.get("boot")) and (self.boot_at or 0) > since:
            how = prev.get("shutdown") or "abrupt"
            if prev.get("prev_boot") and self._planned_cycle(prev["prev_boot"]):
                how = "planned"
            hb = prev.get("last_heartbeat")
            cause = (f"the host went down ({how}{f'; last heartbeat {t(hb)}' if hb else ''}) and booted "
                     f"again at {t(self.boot_at)}")
            orderly = how != "abrupt"
        else:
            clean = stop.get("ts") if self._this_boot(stop.get("boot")) else None
            cause = (f"the daemon was stopped at {t(clean)}" if clean else
                     "the daemon was not running (it ended without a clean stop)") + f" until {t(self._started)}"
            orderly = bool(clean)
        each = "; ".join(f"{s['name']} {n} (every {sched._every_text(int(s['every_s']))}, last ran "
                         f"{t(s['last_run'])})" for s, n in gaps)
        text = (f"Schedules missed runs since {t(since)}: {each}. Likely cause: {cause}. Each runs once now; "
                f"what they record has a hole for that window.")
        log(self.p, f"schedule gap: {text}")
        if orderly:
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon:schedule-gap", "schedule_gap", f"schedule-gap:{int(since)}", "low", text[:4000],
                  "record"))
        else:
            key = f"schedule-gap:{int(since)}"
            named = dict(db.kv(SCHEDULE_GAP_KEY) or {})
            named[key] = sorted(set(named.get(key) or ()) | {s["name"] for s, _ in gaps})
            db.set_kv(SCHEDULE_GAP_KEY, named)
            self.observe("daemon:schedule-gap", text, "normal", whole=True, key=key)

    def sweep_schedule_gaps(self) -> int:
        """Close each open schedule-gap issue once every schedule it names has run since its last sighting
        or is off (disabled or removed): the gap is over, and only a new one opens it again. An issue whose
        names were never recorded stays open for a hand close. Returns how many closed."""
        db = self.p.db
        named = db.kv(SCHEDULE_GAP_KEY) or {}
        if not named:
            return 0
        rows = db.q("SELECT id, watch_key, last_seen, history FROM issues WHERE source='daemon:schedule-gap' "
                    "AND status='open'")
        live = {r["name"]: r for r in db.q("SELECT name, enabled, last_run FROM schedules")}
        now, n, still = time.time(), 0, set()
        for r in rows:
            names = named.get(r["watch_key"])
            if not names:
                continue
            still.add(r["watch_key"])
            ran = [x for x in names if live.get(x) and live[x]["enabled"]]
            if any(float(live[x]["last_run"] or 0) <= float(r["last_seen"] or 0) for x in ran):
                continue
            why = SCHEDULE_GAP_CLEARED if len(ran) == len(names) else SCHEDULE_GAP_CLEARED + " or is off"
            history = json.loads(r["history"] or "[]") + [{"ts": now, "action": "closed", "by": "daemon", "why": why}]
            db.x("UPDATE issues SET status='fixed', closed=?, cleared_why=?, history=? WHERE id=?",
                 (now, why, json.dumps(history), r["id"]))
            still.discard(r["watch_key"])
            n += 1
        keep = {k: v for k, v in named.items() if k in still}   # a closed one's names: a new sighting writes them again
        if keep != named:
            db.set_kv(SCHEDULE_GAP_KEY, keep)
        return n

    def _start_schedule(self, s: dict, payload: dict) -> schedrun.Run:
        """Start one command, heal, builtin watcher or prechecked llm run in its own thread. It works on a
        copy of the daemon with its own database connection; its watchdog pings are the tick's own business."""
        self._warn_over_cap(s, payload)
        host = copy.copy(self)
        host.p = copy.copy(self.p)
        host.p._db = None
        host._progress = lambda: None

        def body() -> tuple[str, float | None]:
            host.jev = Jev(host.cfg, db=host.p.db, pause=host._jev_pause)
            if s["kind"] == "llm":
                return host._schedule_llm(s, payload), None
            if s["kind"] == "command" and payload.get("heal"):
                return host._run_heal(s, payload)
            if s["kind"] == "command":
                return host._run_command_watcher(s, payload), None
            from .watchers import run_builtin
            return run_builtin(host, s["name"], payload), None
        return schedrun.start(schedrun.Run(s, payload, self._schedule_timeout(s, payload)), body,
                              lambda: host.p.db)

    CAP_WARNED_KEY = "schedule_cap_warned"   # kv: schedule -> the over-cap timeouts its daemon log line named

    def _warn_over_cap(self, s: dict, payload: dict) -> None:
        """A stored schedule whose timeouts are above their caps (schedule_set refuses those; the file or
        an older row may hold one) runs at the caps: log that once per schedule and values, not per run."""
        over = sched.over_caps(payload) if s["kind"] != "watcher" else []
        warned = dict(self.p.db.kv(self.CAP_WARNED_KEY) or {})
        text = sched.over_cap_text(over)
        if warned.get(s["name"], "") == text:
            return
        if text:
            log(self.p, f"schedule {s['name']}: {text}; it runs at the cap")
            warned[s["name"]] = text
        else:
            warned.pop(s["name"], None)
        self.p.db.set_kv(self.CAP_WARNED_KEY, warned)

    def _schedule_timeout(self, s: dict, payload: dict) -> float:
        """How long one run may go on: its own bounds plus SCHEDULE_GRACE_S, so they normally end it."""
        if s["kind"] == "watcher":
            from .watchers import bounds
            return bounds(self, payload, payload.get("builtin") or s["name"])[0] + SCHEDULE_GRACE_S
        if s["kind"] == "llm":   # its precheck, then the gates and the enqueue around it
            return _watcher_timeout({"timeout_s": payload.get("precheck_timeout_s")}) + 2 * SCHEDULE_GRACE_S
        t = _watcher_timeout(payload) if str(payload.get("command") or "").strip() else 0
        if payload.get("heal"):
            try:
                step = heal.validate(payload["heal"])["timeout_s"]
            except (ValueError, TypeError, KeyError):
                step = heal.MAX_TIMEOUT_S
            t += 3 * step + heal.LOCK_WAIT_S + 10   # check, suppress probe, fix after its lock wait
        return max(t, 1) + SCHEDULE_GRACE_S

    def _schedule_ran(self, s: dict, status: str, again: float | None) -> None:
        """Record one run of schedule `s` (its row when the run started) and its failure handling."""
        db = self.p.db
        row = db.one("SELECT * FROM schedules WHERE name=?", (s["name"],))
        if row is None:
            return   # removed while it ran
        payload = json.loads(row["payload"] or "{}")
        sched.mark_ran(db, row, status, home=timefmt.home(self.p))
        try:
            self._schedule_skip(row, status)
        except Exception as e:   # the run is recorded; its skip history and alert are extras
            log(self.p, f"schedule {row['name']}: skip record failed: {type(e).__name__}: {e}")
        try:
            progress.note_schedule(db, row["name"], status)
        except Exception as e:   # counting must never keep the run from being recorded
            log(self.p, f"progress check: could not count schedule {row['name']}: {type(e).__name__}: {e}")
        if again is not None:   # a heal check's grace end or settle recheck comes before its period
            db.x("UPDATE schedules SET next_run=MIN(next_run, ?) WHERE name=?", (again, row["name"]))
        elif row["kind"] == "command" and (spec := heal.of(payload)):   # backing off while healthy
            parts = status.split("; ")
            if parts[0] != "ok (healthy)" or any(sched.failing(x) for x in parts):
                heal.end_streak(db, row["name"])   # a timeout, crash or failing own command: back to `every`
            elif (slow := heal.interval(db, row, spec)) > int(row["every_s"]):
                db.x("UPDATE schedules SET next_run=MAX(next_run, last_run + ?) WHERE name=?", (slow, row["name"]))
        if (sched.failing(status) and sched.failing(row["last_status"])
                and not (row["kind"] == "command" and payload.get("heal") and heal.owns(db, row["name"]))):
            self._schedule_broken(row, status)
        elif not sched.failing(status):
            self._schedule_mended(row["name"])
        self._progress()

    def _run_heal(self, s: dict, payload: dict) -> tuple[str, float | None]:
        """A command schedule with a heal block: its check, fix and escalation (heal.run), then its
        own command, if it has one, as a watcher."""
        try:
            spec = heal.validate(payload["heal"])
        except ValueError as e:
            return f"error: {e}"[:200], None
        status, again = heal.run(self, s["name"], spec)
        self._progress()
        # After a fix the own command waits for the recheck: check, fix and command in one step could
        # outlast the watchdog.
        if str(payload.get("command") or "").strip() and "rechecking" not in status:
            status += "; " + self._run_command_watcher(s, payload)
        if status.startswith("fixed") or "self-fix task" in status:
            log(self.p, f"heal {s['name']}: {status}")
        return status, again

    SCHEDULE_FIXES_KEY = "schedule_fixes"   # kv: schedule -> {task, since, errors} for its failure stint

    def _schedule_broken(self, s: dict, status: str) -> None:
        """Two failed runs in a row queue one harness task to fix the schedule (once per failure stint).
        The high alert is raised only when that task ends without fixing it (failed or cancelled, or
        done while the schedule still fails); it clears itself on the next run that does not fail."""
        db, name = self.p.db, s["name"]
        fixes = db.kv(self.SCHEDULE_FIXES_KEY, {}) or {}
        fix = fixes.get(name)
        why = ("it has no command to run; the coordinator sets one with schedule_set `command`"
               if status == "no command" else status)
        if not fix:
            payload = json.loads(s["payload"] or "{}")
            errors = [x for x in (s["last_status"], status) if x]
            spec = (f"Schedule `{name}` ({s['kind']}, every {s['every_s']} s) failed twice in a row and does "
                    f"nothing until fixed. Its command: {payload.get('command') or payload.get('prompt') or '(none)'}"
                    f"\nLast errors: {' | '.join(errors)}\n\nFind out why and fix it: the schedule's definition "
                    f"is in harness/schedules.json (the daemon applies it), the command's own script or config, "
                    f"or what it needs. Run the command once by hand to show it now passes. If it cannot be "
                    f"fixed here, say exactly what is missing.")
            tid = db.add_task(f"Fix schedule {name}: it failed twice in a row"[:200], spec, kind="harness",
                              tier="standard", priority=2, origin="daemon", labels=[f"schedule_fix:{name}"])
            db.set_kv(self.SCHEDULE_FIXES_KEY, {**fixes, name: {"task": tid, "since": time.time(), "errors": errors}})
            log(self.p, f"schedule {name} failed twice in a row: queued fix task {tid}")
            return
        t = db.task(int(fix.get("task") or 0))
        if t and t["status"] not in ("done", "failed", "cancelled", "blocked"):
            return   # its fix task is still on it
        key = f"schedule:{name}"
        if db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
            return
        after = (f"; its fix task #{t['id']} ended {t['status']} and it still fails" if t
                 else "; its fix task is gone")
        self.alert(key, f"Schedule {name} failed twice in a row and does nothing until fixed: {why}{after}", "high")

    def _schedule_mended(self, name: str) -> None:
        fixes = self.p.db.kv(self.SCHEDULE_FIXES_KEY, {}) or {}
        if name in fixes:
            fixes.pop(name)
            self.p.db.set_kv(self.SCHEDULE_FIXES_KEY, fixes)

    def sweep_watcher_issues(self) -> int:
        """Close quiet command-watcher issues and errors (each tick, before the schedules run). The
        receipts of explicit_clear schedules stay open: see screen.settle_receipts."""
        rows = self.p.db.q("SELECT name, every_s, payload FROM schedules WHERE kind='command' AND enabled=1")
        periods = {src: r["every_s"] for r in rows
                   for src in (f"watcher:{r['name']}", scr.error_source(f"watcher:{r['name']}"))}
        return scr.close_quiet_watcher_issues(
            self.p.db, periods, receipts=[f"watcher:{r['name']}" for r in rows if _explicit_clear(r["payload"])])

    def _run_command_watcher(self, s: dict, payload: dict) -> str:
        try:
            return self._command_watcher(s, payload)
        finally:
            self._warn_same_subject(s["name"])

    def _warn_same_subject(self, name: str) -> None:
        """One log line when this watcher opened many issues on one subject (watchdup); no wake."""
        source = f"watcher:{name}"
        try:
            for line in watchdup.check(self.p.db, self.cfg, [source, scr.error_source(source)]):
                log(self.p, f"noisy watcher: {line}")
        except Exception as e:   # a warning must not fail the watcher
            log(self.p, f"same-subject count failed: {type(e).__name__}: {e}")

    def _command_watcher(self, s: dict, payload: dict) -> str:
        cmd = payload.get("command")
        if not cmd:
            return "no command"
        source = f"watcher:{s['name']}"
        # Its own failures are errors under a source of their own, so that no rule of its reports covers
        # them; a receipt source's reports stay pending until acknowledged.
        errors = scr.error_source(source)
        receipt = scr.RECEIPT if payload.get("issue_lifecycle") == scr.EXPLICIT_CLEAR else None
        hours = payload.get("rewake_after_h", (self.cfg.get("screen") or {}).get("rewake_after_h", 6))
        try:
            rewake = float(hours) * 3600 if hours is not None else None
        except (TypeError, ValueError):
            rewake = 6 * 3600
        started = time.time()
        timeout = _watcher_timeout(payload)
        try:   # exit 1 is a normal run that found something: never rerun (its reports would be lost)
            out = schedrun.run_group(cmd, cwd=str(self.p.root), timeout=timeout, env=command_env(self.p),
                                     ok=(0, 1))
        except subprocess.TimeoutExpired:   # its whole process group is killed: a failed run
            note = sched.capped_note(payload.get("timeout_s"), WATCHER_MAX_S)
            self.observe(errors, f"watcher command timed out{f' after {timeout} s' + note if note else ''}: {cmd}",
                         "normal", rewake_after_s=rewake, lifecycle=scr.ERROR)
            return f"error: timed out after {timeout} s{note}"
        text = (out.stdout or "").strip()
        ok = out.returncode in (0, 1)
        failed = not ok and not text   # the daemon reports the failure itself
        if failed:
            text = f"watcher command failed rc={out.returncode}: {output_tail(out.stderr) or '(no stderr)'}"
        elif not ok:   # it printed reports, then failed: the reports stand and so does its failure
            self.observe(errors, f"watcher command exited rc={out.returncode}: "
                                 f"{output_tail(out.stderr) or '(no stderr)'}", "normal", rewake_after_s=rewake,
                         lifecycle=scr.ERROR)
        if not text:
            # Nothing to report: what this watcher reported before is over. A recurrence reopens it.
            scr.close_watcher_issues(self.p.db, source, why=scr.CLEAN_RUN_WHY)
            scr.settle_receipts(self.p.db, source, started, ())   # and its errors are repaired
            scr.settle_mutes(self.p.db, source, started)   # and so are its muted conditions' clocks
            if self.p.db.kv(PAGED_KEY + s["name"]):   # a line paged again later is a new episode
                self.p.db.set_kv(PAGED_KEY + s["name"], {})
            return "ok (0 observations)"
        n, subjects = 0, set()
        paged = dict(self.p.db.kv(PAGED_KEY + s["name"]) or {})
        reported: set[str] = set()
        filed = 0
        for obs in _observations(text):
            body = obs.get("text", "")
            # Its own failure lines ("error": true) are errors, not reports.
            error = failed or obs.get("error") is True
            kind = scr.ERROR if error else receipt
            # A JSON line with "whole": true is one observation: never split at '; '.
            whole = obs.get("whole") is True
            if not error and obs.get("machine") and obs.get("condition"):
                self._machine_condition(obs, body)   # an info line reaches the machine ledger too
            if not error and obs.get("to") is not None:
                target = self._page_target(s["name"], obs)
                if target is not None:   # another project's: a note to it, not an observation here
                    k = f"{target}\t{_line_key(obs, body)}"
                    reported.add(k)
                    filed += self._page_line(s["name"], target, obs, body, paged, k, rewake, filed)
                    n += 1
                    continue
            self.observe(errors if error else source, body, obs.get("severity"), rewake_after_s=rewake,
                         repeat=obs.get("repeat") is True, lifecycle=kind, whole=whole,
                         key=obs.get("key") if isinstance(obs.get("key"), str) and obs["key"].strip() else None,
                         episode=obs.get("episode") if isinstance(obs.get("episode"), str) else None,
                         pin=not error and obs.get("pin_severity") is True)
            if kind == scr.RECEIPT:
                subjects.update(scr.normalize(subj) for subj, _, cleared in scr.watcher_conditions(source, body, whole) or ()
                                if not cleared)
            n += 1
        if ok:   # a line no longer reported is over: its recurrence pages again
            paged = {k: v for k, v in paged.items() if k in reported}
        if paged != (self.p.db.kv(PAGED_KEY + s["name"]) or {}):
            self.p.db.set_kv(PAGED_KEY + s["name"], paged)
        if ok:
            scr.settle_receipts(self.p.db, source, started, subjects)
            scr.settle_mutes(self.p.db, source, started)   # muted conditions it no longer reported cleared
        return f"ok ({n} observations)"

    def _page_target(self, name: str, obs: dict) -> str | None:
        """The other local project a line's `to` names (pageto.resolve), else None: the line is then a
        normal observation here, and an unknown target is logged."""
        target, why = pageto.resolve(obs.get("to"), obs.get("machine"))
        if target is None:
            log(self.p, f"watcher {name}: line not paged ({why}); observed here instead")
        return target if target != self.p.name else None

    def _page_line(self, name: str, target: str, obs: dict, body: str, paged: dict, k: str,
                   rewake: float | None, filed: int) -> int:
        """File one line to `target` as a daemon note, once per key (`k`) and again after `rewake`
        seconds (None: never) or on every run with "repeat": true. Returns 1 if it filed a note."""
        now = time.time()
        last = paged.get(k)
        ep = _line_episode(obs)
        if last and ep and last.get("episode") not in (None, ep):
            last = None   # another episode of a keyed line: a new note, as a new issue would be (screen)
        elif last:
            ep = ep or last.get("episode")   # none changes nothing; one paged without adopts the first it sees
            if ep:
                last["episode"] = ep
        if last and obs.get("repeat") is not True and (rewake is None or now - float(last["at"]) < rewake):
            return 0
        if filed >= pageto.PER_RUN:
            log(self.p, f"watcher {name}: {pageto.PER_RUN} notes this run; the rest go on a later run")
            return 0
        sev = obs.get("severity") if obs.get("severity") in upstream.NOTE_SEVERITIES else "normal"
        # Its first send's time keeps a later episode's note apart from this one's in the inbox (upstream.send).
        first = float(last["first"]) if last else now
        again = f" (still reported: note {int(last['n']) + 1})" if last else ""
        text = f"Watcher {name} of project {self.p.name}, reporting since {timefmt.long(first, self.p)}: {body}{again}"
        got = upstream.send(self.p.name, None, target, text, sev, now=now, sender="daemon")
        if got not in ("sent", "duplicate"):
            log(self.p, f"watcher {name}: note to {target}: {got}; retried on its next run")
            return 0
        paged[k] = {"at": now, "first": first, "n": int(last["n"]) + 1 if last else 1,
                    **({"episode": ep} if ep else {})}
        log(self.p, f"watcher {name}: paged {target}: {text[:200]}")
        return 1

    def _machine_condition(self, obs: dict, body: str) -> None:
        """A watcher saw a shared machine down or held (or no longer: `cleared`): tell its recovery owner."""
        try:
            grace = obs.get("grace_s")
            machine_ledger.report(self.p.name, str(obs["machine"]), str(obs["condition"]), body,
                                  float(grace) if isinstance(grace, (int, float)) else None,
                                  cleared=obs.get("cleared") is True)
        except Exception:
            log(self.p, "recording a shared-machine condition failed\n" + traceback.format_exc())

    def _schedule_skip(self, s: dict, status: str) -> None:
        """Keep a skipped trigger in the schedule's skip history (sched.record_skip), whatever held it.
        A skip for the schedule's own daily cap (`budget_usd_day`, checked only once the budget gate
        allows optional work) loses runs the project could afford, so it raises one normal keyed
        observation per schedule (SCHEDULE_BUDGET_SOURCE, key schedule-budget:<name>), at most once a
        day. Expected skips (the gate, a debounce, an open run, the idle guard, a precheck) are only
        recorded. Any later status past the cap check (a run queued, or a later gate) closes the issue."""
        db, name, now = self.p.db, s["name"], time.time()
        if sched.skip(status):
            sched.record_skip(db, name, status, now)
        key = f"schedule-budget:{name}"
        if sched.own_budget_skip(status):
            raised = {k: v for k, v in (db.kv(SCHEDULE_BUDGET_KEY) or {}).items() if now - float(v) < 86400}
            if name in raised:
                return
            cap, spent = float(s["budget_usd_day"] or 0), sched.spent_today(db, name)
            text = (f"Schedule {name} skipped a run: its own daily budget is used (${spent:.2f} of ${cap:.2f} in the "
                    f"last 24 h) while the budget gate allows optional work, so it loses runs the project could "
                    f"afford. It retries every {min(sched.BUDGET_RETRY_S, int(s['every_s'])) // 60} min until its "
                    f"spend falls under the cap. If its runs matter, raise its daily cap (`budget_usd`), lower its "
                    f"per-run `budget_usd_run`, or run it less often; `ttp schedules <project> show {name}` lists "
                    f"its recent skips.")
            raised[name] = now
            db.set_kv(SCHEDULE_BUDGET_KEY, raised)
            log(self.p, f"schedule {name}: {status} (${spent:.2f} of ${cap:.2f}); observation raised")
            self.observe(SCHEDULE_BUDGET_SOURCE, text, "normal", whole=True, key=key)
            return
        if sched.budget_skipped(status):
            return   # the gate held it before its own cap was read
        for r in db.q("SELECT id, history FROM issues WHERE source=? AND watch_key=? AND status='open'",
                      (SCHEDULE_BUDGET_SOURCE, key)):
            history = json.loads(r["history"] or "[]") + [{"ts": now, "action": "closed", "by": "daemon",
                                                           "why": SCHEDULE_BUDGET_CLEARED}]
            db.x("UPDATE issues SET status='fixed', closed=?, cleared_why=?, history=? WHERE id=?",
                 (now, SCHEDULE_BUDGET_CLEARED, json.dumps(history), r["id"]))

    def _schedule_llm(self, s: dict, payload: dict) -> str:
        db = self.p.db
        core = self.cfg.get("core_provider", "claude")
        gate = self.gates.get(core)
        if gate and not gate.allow_optional:
            return f"skipped: budget {gate.level}"
        # A debounced schedule keeps its period owed while it waits on its budget or its own open run.
        held = "deferred" if sched.debounce_s(s["name"], payload) else "skipped"
        spent = sched.spent_today(db, s["name"]) if s["budget_usd_day"] is not None else 0.0
        if s["budget_usd_day"] is not None and spent >= float(s["budget_usd_day"]) - 0.005:
            return f"{held}: daily budget used"
        if db.one("SELECT id FROM tasks WHERE origin='schedule' AND labels=? AND status NOT IN "
                  "('done','failed','cancelled')", (json.dumps([s["name"]]),)):
            return f"{held}: previous run still open"
        duplicate, evidence, owed = sched.debounce_gate(db, s["name"], payload)
        if duplicate:
            return duplicate
        # A review of a period with no work and no user message would report that nothing moved.
        # The built-in daily review opts in by name, so projects created before the flag get it too.
        # A changed outcome (a task outcome moved with no run, e.g. an owner cancelled it) or this
        # schedule's own failed or cancelled run still owes a run: the idle guard sees neither.
        if (not owed and payload.get("skip_if_idle", s["name"] == "daily-review") and s["last_run"]
                and not self._active_since(float(s["last_run"]), s["name"])):
            return "skipped: nothing happened since the last run"
        skip = self._schedule_precheck(s, payload)
        if skip:
            return skip
        spec = payload.get("spec") or s["description"]
        prompt_file = payload.get("prompt")
        if prompt_file and (self.p.harness / "prompts" / prompt_file).exists():
            spec = (self.p.harness / "prompts" / prompt_file).read_text() + "\n\n" + spec
        jev_lines = jevuse.lines(db, self.cfg) if payload.get("jev_report", s["name"] == "daily-review") else []
        if payload.get("jev_report", s["name"] == "daily-review"):
            try:
                if gate_line := waitgate.line(db, self.cfg):
                    jev_lines.append(gate_line)
            except Exception as e:   # a metric must not keep the review from starting
                log(self.p, f"wake gate line failed: {type(e).__name__}: {e}")
        if jev_lines:
            spec += (f"\n\nJev uses over the last {jevuse.window_s(self.cfg) / 86400:g} d (calls, cost, estimated "
                     f"savings, net, error rate; a use with no net saving is switched off by itself):\n"
                     + "\n".join(f"- {line}" for line in jev_lines))
        if payload.get("unblock_report", s["name"] == "daily-review"):
            try:
                spec += "\n\nUnblocking quality:\n" + "\n".join(f"- {line}" for line in unblock.lines(db))
                spec += (f"\n\nStuck longer than {sweep.REVIEW_LONG_H:g}h, with the blocker sweep's outcome:\n"
                         + "\n".join(f"- {line}" for line in sweep.review_lines(db, where=self.p)))
                moved = failover.review_lines(db, where=self.p)
                if moved:
                    spec += ("\n\nProvider failovers (last 24 h; tasks moved off a provider that could not "
                             "start new work):\n" + "\n".join(f"- {line}" for line in moved))
            except Exception as e:   # a metric must not keep the review from starting
                log(self.p, f"unblocking metrics failed: {type(e).__name__}: {e}")
        if payload.get("audit_report", s["name"] == "daily-review"):
            try:
                hours = float((self.cfg.get("review") or {}).get("blocked_long_h", audit.BLOCKED_H))
                spec += ("\n\nSelf-efficiency audit (last 24 h):\n"
                         + "\n".join(f"- {line}" for line in audit.lines(db, blocked_h=hours, p=self.p)))
            except Exception as e:   # nor must the audit
                log(self.p, f"self-efficiency audit failed: {type(e).__name__}: {e}")
        if payload.get("audit_report", s["name"] == "daily-review"):
            try:
                from . import machine_ledger
                quiet = machine_ledger.review_lines(self.p)
                if quiet:
                    spec += ("\n\nShared-machine outages not paged (last 24 h; known, under repair, owned by a "
                             "person or ticket, paused, or needed by no open work):\n"
                             + "\n".join(f"- {line}" for line in quiet))
                gone = machine_ledger.task_lines(self.p)
                if gone:
                    spec += ("\n\nOpen machine changes whose until-task is over (undo and close each, or "
                             "re-extend it for the task that continues the work):\n"
                             + "\n".join(f"- {line}" for line in gone))
            except Exception as e:   # nor must the machine ledger
                log(self.p, f"quiet outage lines failed: {type(e).__name__}: {e}")
        if payload.get("audit_report", s["name"] == "daily-review"):
            try:
                recover = ttr.review_lines(self.p)
                if recover:
                    spec += (f"\n\nTime to recover per machine (last {ttr.REVIEW_DAYS} d; `ttp ttr` for more):\n"
                             + "\n".join(f"- {line}" for line in recover))
            except Exception as e:   # nor must the incident report
                log(self.p, f"ttr lines failed: {type(e).__name__}: {e}")
        if (self.cfg.get("upstream") or {}).get("ingest") and payload.get("audit_report", s["name"] == "daily-review"):
            try:
                line = upstream.orphans_line()
                if line:
                    spec += f"\n\nShipped upstream fixes no filer here can confirm: {line}"
            except Exception as e:   # nor must the orphan list
                log(self.p, f"upstream orphans line failed: {type(e).__name__}: {e}")
        if payload.get("audit_report", s["name"] == "daily-review"):
            try:
                spec += (f"\n\nClaimed pushes of done tasks on their remotes (`ttp push-audit` for more): "
                         f"{pushclaim.daily_line(db)}.")
            except Exception as e:   # nor must the push audit
                log(self.p, f"push audit line failed: {type(e).__name__}: {e}")
        if payload.get("audit_report", s["name"] == "daily-review"):
            noisy = watchdup.review_lines(db)
            if noisy:
                spec += ("\n\nNoisy command watchers (many issues on one subject in 24 h; fix each watcher's "
                         "output today):\n" + "\n".join(f"- {line}" for line in noisy))
        if payload.get("audit_report", s["name"] == "daily-review"):
            try:
                line = responsibilities.line(db)
                if line:
                    spec += f"\n\nResponsibilities: {line}." + responsibilities.review_advice(db)
            except Exception as e:   # nor must the inventory
                log(self.p, f"health coverage line failed: {type(e).__name__}: {e}")
        if s["name"] == "daily-review":
            try:
                line = pushq.target_line(db, timefmt.home(self.p))
                if line:
                    spec += f"\n\nDelivery: {line}."
            except Exception as e:   # nor must a missing or broken push marker
                log(self.p, f"intended target line failed: {type(e).__name__}: {e}")
        if payload.get("charter_report", s["name"] == "daily-review"):
            found = coord.charter_conflicts(self.p)
            if found:
                spec += ("\n\nCharter conflicts (Restrictions items that contradict each other; list them, and say "
                         "whether the coordinator retired the stale side):\n"
                         + "\n".join(f"- \"{x['forbid']}\" ({x['forbid_section']}) vs \"{x['allow']}\" "
                                      f"({x['allow_section']})" for x in found))
        with db.tx():   # the evidence is the state at enqueue: what changes during the run stays new
            tid = db.add_task(f"[{s['name']}] {s['description'][:120] or 'recurring task'}", spec,
                              kind=payload.get("kind", "work"), tier=payload.get("tier", "standard"),
                              priority=int(payload.get("priority", 4)),
                              budget_usd=sched.run_budget(self.cfg, s, payload, spent),
                              origin="schedule", labels=[s["name"]])
            if evidence is not None:
                sched.record_evidence(db, s["name"], tid, evidence)
        return "queued"

    def _schedule_precheck(self, s: dict, payload: dict) -> str:
        """An llm schedule's model-free `precheck` (shell, run from the project root, `precheck_timeout_s`
        capped like a command watcher's): exit 0 skips this trigger, its first output line the reason.
        Any other exit, a timeout or an error lets the run go ahead (fail open) and is logged."""
        cmd = _precheck_cmd(payload)
        if not cmd:
            return ""
        timeout = _watcher_timeout({"timeout_s": payload.get("precheck_timeout_s")})
        try:   # 0 (skip) and 1 (go ahead) are its answers; any other exit may be a script rewritten
            # mid-run, and the rerun's answer (perhaps a skip) saves a model run that fail-open would spend
            out = schedrun.run_group(cmd, cwd=str(self.p.root), timeout=timeout, env=command_env(self.p),
                                     ok=(0, 1))
        except subprocess.TimeoutExpired:
            log(self.p, f"schedule {s['name']}: precheck timed out after {timeout} s"
                        f"{sched.capped_note(payload.get('precheck_timeout_s'), WATCHER_MAX_S)}; the run goes ahead")
            return ""
        except Exception as e:
            log(self.p, f"schedule {s['name']}: precheck failed ({type(e).__name__}: {e}); the run goes ahead")
            return ""
        if out.returncode != 0:
            log(self.p, f"schedule {s['name']}: precheck exited rc={out.returncode} "
                        f"({output_tail(out.stderr) or 'no stderr'}); the run goes ahead")
            return ""
        line = next((x.strip() for x in (out.stdout or "").splitlines() if x.strip()), "") or "exit 0"
        return f"skipped: precheck: {line}"[:200]

    def _active_since(self, since: float, schedule: str) -> bool:
        """Whether any worker ran or finished, other than this schedule's own, or the user wrote, since
        `since`. A worker that started before and finished after counts: its outcome is new."""
        db = self.p.db
        return bool(db.one("SELECT id FROM messages WHERE direction='in' AND ts>?", (since,))
                    or db.one("SELECT runs.id FROM runs LEFT JOIN tasks ON tasks.id=runs.task "
                              "WHERE runs.role!='coordinator' AND (runs.started>? OR runs.ended>?) AND NOT "
                              "(COALESCE(tasks.origin,'')='schedule' AND COALESCE(tasks.labels,'')=?)",
                              (since, since, json.dumps([schedule]))))

    def observe(self, source: str, text: str, hint: str | None = None, rewake_after_s: float | None = None,
                repeat: bool = False, lifecycle: str | None = None, whole: bool = False,
                key: str | None = None, episode: str | None = None, pin: bool = False) -> None:
        if not text.strip():
            return
        again = {"rewake_after_s": rewake_after_s, "repeat": repeat, "lifecycle": lifecycle, "whole": whole,
                 "key": key, "episode": episode, "pin": pin}
        v = scr.screen(self.p.db, self.cfg, source, text, hint, jev=self.jev, **again)
        if v.jev_out_of_funds:
            self.alert("jev-funds", JEV_FUNDS_TEXT, "high")
        if v.wake:
            self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), source, "observation", v.fingerprint, v.severity, text[:4000], "queued"))

    def check_progress(self, every_s: float = 60) -> None:
        """Model-free progress and stall check (progress.py): running work with no result past its
        threshold raises `no_progress` (never-started work is check_stall's); the same not-yet,
        refusal or failure repeating raises `stall`; a machine change of this project whose until-probe
        passed while its task still waits raises `change_probe`. Each key wakes the coordinator once,
        again on escalation or after a long interval."""
        now = time.time()
        if now - getattr(self, "_progress_checked", 0.0) < every_s:
            return
        self._progress_checked = now
        try:
            changes = [c for c in machine_ledger.changes() if c.get("by") == self.p.name]
        except Exception as e:   # an unreadable ledger: its keys are left as they are
            log(self.p, f"progress check: machine ledger not read: {e}")
            changes = None
        for e in progress.check(self.p.db, self.cfg, self.p.state, now, where=self.p, current_probe=_current_probe,
                                changes=changes):
            log(self.p, f"progress check: {e['kind']} for {', '.join(e['keys'])}")

    def check_stall(self, every_s: float = 60) -> None:
        """Model-free stall check: dispatch would start a queued task, yet nothing starts. Once no
        worker run has started for coordinator.stall_wake_h (default 3, 0: off) and a queued task
        has been one dispatch would start now (_start_hold: dependencies, start_after/start_when,
        paused resources, logged-out or net-held providers, the gate's slots, busy locks and the
        rest) for that long at every check, raise one high observation listing those tasks and when
        the last worker run started. One per stall: keyed on the last worker run started; a new
        worker run ends the stall.

        Held tasks are never counted: each hold has its own alert. Accepted gap: a hold that is
        itself wrong (a lock that never clears, a probe that can never pass) is not reported here;
        the lock, hold and wait checks cover it. The observation also lists each ready task dispatch holds only for a busy
        slot or a taken lock, with the hold dispatch recorded (_resource_hold, hold_reason): the
        running tasks, or each lock holder with its pid, named live only while that pid lives. A
        paused task (directly or through a device.locks alias) is a pause there, never listed.
        Nothing here works out a lock, pause or alias again."""
        now = time.time()
        last_check = getattr(self, "_stall_checked", 0.0)
        if now - last_check < every_s:
            return
        self._stall_checked = now
        db = self.p.db
        hours = float(self.cfg["coordinator"].get("stall_wake_h", 3) or 0)
        # Dispatch does not run (settling, no config) or did not run between checks (a paused
        # project, a long tick): readiness counts again from now.
        if hours <= 0 or now - self._started < STALL_SETTLE_S or self.cfg_status == "unavailable" \
                or self.settling() or now - last_check > STALL_GAP_S:
            self._stall_ready = {}
            return
        core = self.cfg.get("core_provider", "claude")
        ready = db.ready_tasks()
        busy = {r["provider"]: r["n"] for r in db.q(
            "SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' GROUP BY provider")}
        ctx = {"now": now, "paused": db.paused_resources(), "busy": busy, "committed": None,
               "logged_out": {prov: self._logged_out(prov) for prov in {t["provider"] or core for t in ready}}}
        holds = {t["id"]: self._start_hold(t, t["provider"] or core, ctx) for t in ready}
        self._res_wait_logged = getattr(self, "_res_wait_logged", set()) & {i for i, h in holds.items() if h == "resource"}
        seen = getattr(self, "_stall_ready", {})
        self._stall_ready = {tid: seen.get(tid, now) for tid, h in holds.items() if h is None}
        stuck = [t for t in ready if now - self._stall_ready.get(t["id"], now) >= hours * 3600]
        if not stuck:
            return
        last = db.one("SELECT id, started FROM runs WHERE role!='coordinator' ORDER BY started DESC, id DESC LIMIT 1")
        last_id = int(last["id"]) if last else 0
        started = float(last["started"] or 0) if last else 0.0
        if now - started < hours * 3600 or int((db.kv(STALL_KEY) or {}).get("run", -1)) == last_id:
            return   # a worker run started meanwhile, or this stall was raised already
        when = timefmt.long(started, self.p) if last else "never"
        lines = [f"#{t['id']} {t['title'][:60]} (ready {(now - self._stall_ready[t['id']]) / 3600:.1f} h)"
                 for t in stuck]
        more = f" (+{len(lines) - STALL_LIST_MAX} more)" if len(lines) > STALL_LIST_MAX else ""
        why = ctx.get("holds", {})
        held = [f"#{t['id']} {t['title'][:60]}: {hold_reason(why[t['id']])}" for t in ready
                if holds[t["id"]] == "resource" and (why.get(t["id"]) or {}).get("kind") in ("busy", "lock", "reserved")]
        text = (f"Stall: dispatch would start {len(stuck)} queued task(s) now, but no worker run has started for "
                f"{hours:g} h or more (last worker run started: {when}). Find out why they do not start "
                f"(daemon log, run start errors):\n"
                + "\n".join(f"- {x}" for x in lines[:STALL_LIST_MAX]) + more
                + ("\nReady but held by a busy slot or lock (dispatch's recorded hold):\n"
                   + "\n".join(f"- {x}" for x in held[:STALL_LIST_MAX]) if held else ""))
        with db.tx():
            db.set_kv(STALL_KEY, {"run": last_id, "at": now})
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", "observation", f"stall:{last_id}", "high", text[:4000], "queued"))
        log(self.p, f"stall: {len(stuck)} task(s) ready for {hours:g} h or more, no worker run since {when}")

    def check_churn(self) -> None:
        """Model-free host process-churn watch (churnwatch): a fork rate that stays high, owned by this
        project's unit, raises one normal observation per episode. Read-only; nothing on hosts without /proc."""
        now = time.time()
        if now - getattr(self, "_churn_checked", 0.0) < churnwatch.CHECK_S:
            return
        self._churn_checked = now
        w = self.__dict__.setdefault("_churn", churnwatch.Watch())
        obs = w.step(now)
        if obs and obs["unit"] == service.unit_name(self.p):
            self.observe("daemon:process-churn", obs["text"], obs["severity"], whole=True, key=obs["key"])

    def check_slot_wait(self, every_s: float = 60) -> None:
        """Model-free urgent slot wait: a ready task that is a required owner of an open obligation, or has
        priority 1, held only because every worker slot of its provider is taken (_start_hold's 'gate' with
        ctx["full"]) at every check for coordinator.urgent_slot_wait_min (default 15, 0: off) raises one
        observation naming it and the running tasks holding the slots with their priorities, so the
        coordinator can free or reorder work before the user notices. High for an obligation owner,
        normal for a plain priority 1. One per task per stay in the queue (keyed on its last run); a free
        slot or any other hold restarts its clock. The clock is kept in memory, like check_stall's: after
        a daemon restart, or checks further apart than STALL_GAP_S, it counts from then. Nothing is
        preempted: dispatch already starts these tasks first when a slot frees (db.ready_tasks)."""
        now = time.time()
        last = getattr(self, "_slot_wait_checked", 0.0)
        if now - last < every_s:
            return
        self._slot_wait_checked = now
        mins = float(self.cfg["coordinator"].get("urgent_slot_wait_min", 15) or 0)
        if mins <= 0 or self.cfg_status == "unavailable" or self.settling() or now - last > STALL_GAP_S:
            self._slot_waits = {}
            return
        db = self.p.db
        due = obligations.owner_due(db)
        urgent = [t for t in db.ready_tasks() if t["id"] in due or (t["priority"] or 0) == 1]
        core = self.cfg.get("core_provider", "claude")
        ctx = {"now": now, "paused": db.paused_resources(), "committed": None,
               "busy": {r["provider"]: r["n"] for r in db.q(
                   "SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' "
                   "GROUP BY provider")},
               "logged_out": {prov: self._logged_out(prov) for prov in {t["provider"] or core for t in urgent}}}
        full = {t["id"]: t for t in urgent
                if self._start_hold(t, t["provider"] or core, ctx) == "gate" and t["id"] in ctx.get("full", {})}
        seen = getattr(self, "_slot_waits", {})
        self._slot_waits = {tid: seen.get(tid, now) for tid in full}
        for tid, t in full.items():
            if now - self._slot_waits[tid] < mins * 60:
                continue
            ran = db.one("SELECT MAX(id) id FROM runs WHERE task=?", (tid,))
            fp = f"slot_wait:{tid}:{(ran or {}).get('id') or 0}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            provider = t["provider"] or core
            owns = obligations.owned_open(db, tid) if tid in due else []
            why = f"p{t['priority']}" + (f", owns open obligation {', '.join(owns)}" if owns else "") + (
                f", next due {timefmt.long(due[tid], self.p)}" if owns and due[tid] != float("inf") else "")
            holders = db.q("SELECT r.id run, r.task, t.priority, t.title FROM runs r LEFT JOIN tasks t ON t.id=r.task "
                           "WHERE r.role!='coordinator' AND r.status='running' AND r.provider IS ? "
                           "ORDER BY t.priority DESC, r.started", (provider,))
            lines = [f"#{h['task']} {(h['title'] or '')[:60]} (p{h['priority']}, run {h['run']})" if h["task"]
                     else f"run {h['run']} (no task)" for h in holders]
            text = (f"Slot wait: #{tid} {t['title'][:80]} ({why}) is ready but has waited "
                    f"{(now - self._slot_waits[tid]) / 60:.0f} min for a {provider} worker slot: all "
                    f"{ctx['full'][tid]} are taken. It starts first when one frees. To start it sooner, end or "
                    f"pause lower-priority work holding a slot:\n" + "\n".join(f"- {x}" for x in lines[:STALL_LIST_MAX]))
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "observation", fp, "high" if owns else "normal", text[:4000], "queued", tid))
            log(self.p, f"slot wait: task {tid} ({why}) waited {mins:g} min or more for a {provider} worker slot")

    def _log_resource_wait(self, task: dict, held: dict | None) -> None:
        """Log once per wait that dispatch skips ready `task` because a resource it needs is held, with
        the hold it recorded (`held`, _resource_hold's; check_stall forgets the task once nothing holds it)."""
        logged = self.__dict__.setdefault("_res_wait_logged", set())
        if task["id"] in logged:
            return
        logged.add(task["id"])
        log(self.p, f"task {task['id']}: ready, not started: "
                    f"{hold_reason(held) if held else 'a resource it needs is busy'}")

    def check_idle_capacity(self, every_s: float = 60) -> None:
        """Model-free idle-capacity wake (idlecap.py): a device resource of the project idle for
        coordinator.idle_capacity_s while a worker slot is free, the core provider's gate is green
        or yellow and dispatch would start nothing raises one normal `idle_capacity` event naming it,
        the running work and the next scheduled check, so the coordinator can queue work for it."""
        now = time.time()
        last = getattr(self, "_idle_cap_checked", 0.0)
        if now - last < every_s:
            return
        self._idle_cap_checked = now
        db = self.p.db
        grace, rearm = idlecap.settings(self.cfg)
        st = db.kv(idlecap.STATE_KEY) or {}
        if grace <= 0 or self.cfg_status == "unavailable":
            if st:
                db.set_kv(idlecap.STATE_KEY, {})
            return
        try:
            idle, free = self._idle_devices(now)
        except Exception:
            log(self.p, "idle capacity check failed\n" + traceback.format_exc())
            return
        declined = self._idle_declines(now)
        may_fire = free > 0 and not self.settling() and now - self._started >= STALL_SETTLE_S
        new, fired = idlecap.step(st, idle, now, grace, rearm, may_fire, declined)
        if new != st:
            db.set_kv(idlecap.STATE_KEY, new)
        if not fired:
            return
        running = [f"#{r['task']} {(r['title'] or '')[:60]}" for r in db.q(
            "SELECT r.task, t.title FROM runs r LEFT JOIN tasks t ON t.id=r.task "
            "WHERE r.role!='coordinator' AND r.status='running' ORDER BY r.id")]
        running += [f"detached jobs of run {Path(h['run_dir']).name} (task #{h.get('task')})"
                    for h in hold.holding(self.p.state / "holds")]
        nxt = db.one("SELECT id, not_before FROM tasks WHERE status='queued' AND not_before>? "
                     "ORDER BY not_before LIMIT 1", (now,))
        when = (f"#{nxt['id']} at {timefmt.short(float(nxt['not_before']), self.p, now)} "
                f"(in {(float(nxt['not_before']) - now) / 3600:.1f} h)" if nxt else "")
        text = idlecap.text(new, fired, now, free, running, when, rearm)
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", idlecap.KIND, f"{idlecap.KIND}:{','.join(fired)}:{int(now)}", "normal",
              text[:4000], json.dumps({"fired": {n: new[n]["n"] for n in fired}}), "queued"))
        log(self.p, f"idle capacity: {', '.join(fired)} idle, {free} slot(s) free; coordinator told")

    def _idle_declines(self, now: float) -> dict:
        """The per-resource declines (idlecap.judge), kept in the database: each idle_capacity event the
        coordinator has handled since is judged once against the tasks created after it; a resource an
        open task names is cleared."""
        db = self.p.db
        dec = dict(db.kv(idlecap.DECLINED_KEY) or {})
        start = dec
        for e in db.q("SELECT id, ts, data FROM events WHERE kind=? AND status='handled' AND ts>? ORDER BY id",
                      (idlecap.KIND, now - idlecap.REARM_MAX_S * 2)):
            try:
                data = json.loads(e["data"] or "{}")
            except ValueError:
                data = {}
            if not isinstance(data, dict) or data.get("judged") or not isinstance(data.get("fired"), dict):
                continue
            named = {n for t in db.q("SELECT labels FROM tasks WHERE created>=?", (float(e["ts"]),))
                     for n in idlecap.task_names(t, self.cfg)}
            dec = idlecap.judge(dec, data["fired"], named, float(e["ts"]))
            db.x("UPDATE events SET data=? WHERE id=?", (json.dumps({**data, "judged": True}), e["id"]))
        open_names = {n for t in db.q("SELECT labels FROM tasks WHERE status IN ('queued','running','waiting')")
                      for n in idlecap.task_names(t, self.cfg)}
        dec = {r: v for r, v in dec.items() if not any(idlecap.uses(n, r) for n in open_names)}
        if dec != start:
            db.set_kv(idlecap.DECLINED_KEY, dec)
        return dec

    def _idle_devices(self, now: float) -> tuple[set[str], int]:
        """(the project's device resources idle now, worker slots free for new work). Free slots are 0
        while the core provider's gate is orange or red or holds new work, or a ready task would start
        now. Paused resources, resources in trouble, machines with an open ledger condition or a change
        entry that holds their device work are never idle: they are out of use on purpose or broken. Nor
        is a device no open task names and no task run in the last idle_capacity_use_days named."""
        db, core = self.p.db, self.cfg.get("core_provider") or "claude"
        open_tasks = db.q("SELECT labels FROM tasks WHERE status IN ('queued','running')")
        recent = db.q("SELECT labels FROM tasks WHERE created>?", (now - idlecap.LABEL_LOOKBACK_S,))
        named = {n for t in recent for n in idlecap.task_names(t, self.cfg)}
        known = machines.load()
        cands = idlecap.candidates(self.cfg, named, machine_ledger.resource_machines(self.p), known)
        days = idlecap.use_days(self.cfg)
        if days > 0:
            wanted = {n for t in db.q(
                "SELECT labels FROM tasks WHERE status IN ('queued','running','waiting') "
                "OR id IN (SELECT task FROM runs WHERE started>?)", (now - days * 86400,))
                for n in idlecap.task_names(t, self.cfg)}
            cands = {c for c in cands if any(idlecap.uses(n, c) for n in wanted)}
        paused = db.paused_resources()
        out = {locks.canonical(self.cfg, str(n)) for n in (
            *paused, *(db.kv("resource_trouble") or {}),
            *(c.get("alias") or "" for c in machine_ledger.conditions().values() if isinstance(c, dict)),
            *(c.get("alias") or "" for c in machine_ledger.changes() if idlecap.holds_work(c)))}
        used = {n for t in open_tasks for n in idlecap.task_names(t, self.cfg)}
        used |= {locks.canonical(self.cfg, str(r.get("resource"))) for h in hold.holding(self.p.state / "holds")
                 for r in h.get("resources") or []}
        idle = set()
        for c in cands:
            if any(idlecap.uses(n, c) for n in out | used):
                continue
            if locks.held_labels(self._slot_paths(c)) or locks.reserved_by(self._reserve_path(c)):
                continue
            idle.add(c)
        if not idle:
            return idle, 0
        gate = self.gates.get(core) or bud.evaluate(db, self.cfg, core, bud.plan_windows(db))
        busy = {r["provider"]: r["n"] for r in db.q(
            "SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' GROUP BY provider")}
        if gate.level in ("orange", "red") or not gate.allow_new_work:
            return idle, 0
        free = max(gate.max_parallel - busy.get(core, 0), 0)
        ready = db.ready_tasks()
        ctx = {"now": now, "paused": paused, "busy": busy, "committed": None,
               "logged_out": {prov: self._logged_out(prov) for prov in {t["provider"] or core for t in ready}}}
        if any(self._start_hold(t, t["provider"] or core, ctx) is None for t in ready):
            return idle, 0   # dispatch fills the slot first
        return idle, free

    def check_obligations(self) -> None:
        """Urgent user obligations (obligations.py), model-free: file the progress and receipts runs
        recorded, then keyed high observations for stalled owners and overdue milestones; each episode
        survives restart and is never repeated on a timer."""
        now = time.time()
        if now - getattr(self, "_obligations_at", 0.0) < obligations.CHECK_EVERY_S:
            return
        self._obligations_at = now
        try:
            for line in obligations.file_runs(self.p, now):
                log(self.p, f"obligation record refused: {line}")
            for fp in obligations.check(self.p, now, self.cfg):
                log(self.p, f"obligation overdue: raised {fp}")
        except Exception:
            log(self.p, "obligations: check failed\n" + traceback.format_exc())

    def check_unreachable(self, every_s: float = 60) -> None:
        """One alert per network outage (netwait): every task that could start waits on a probe saying
        this machine cannot reach its host (exit 255), no worker runs and no other task is ready to start,
        for `waiting.unreachable_alert_s`. Only the user can restore the network (or sign back in); the alert
        says so once, stays in the top section and clears itself once the hosts answer again. Hosts
        reached through a tunnel from the user's workstation get a low feed line instead, raised once to
        normal after `waiting.tunnel_outage_page_after_s`; so do hosts already known down. Only an
        offline workstation gets the VPN advice; hosts down while others answer are named as down
        (netwait.py)."""
        now = time.time()
        if now - getattr(self, "_net_checked", 0.0) < every_s:
            return
        self._net_checked = now
        db = self.p.db
        grace, debounce = netwait.settings(self.cfg)
        st = db.kv(netwait.STATE_KEY) or {}
        if grace <= 0 and not st:
            return
        unreachable, other = self._reachability_holds(now)
        running = db.one("SELECT id FROM runs WHERE role!='coordinator' AND status='running' LIMIT 1")
        blocking = bool(unreachable) and not other and not running
        page_after = netwait.tunnel_page_after(self.cfg)
        new, action = netwait.step(st, blocking, len(unreachable), now, grace, debounce,
                                   healing=blocking and self._healing(), page_after=page_after)
        severity, others = "normal", 0
        if action in ("alert", "raise"):
            others = self._net_others([tid for tid, _ in unreachable])
        if action == "alert":
            raw = list(dict.fromkeys(h for _, hs in unreachable for h in hs))
            names, tunnel = netwait.describe(raw)
            kind, known = self._net_kind(unreachable, raw, now, float(new["since"])) \
                if not tunnel else ("offline", {})
            new.update(tasks=[tid for tid, _ in unreachable], hosts=names, tunnel=tunnel, kind=kind)
            if tunnel and now - float(new["since"]) < page_after:
                severity = "low"   # a tunnel that reconnects by itself: a feed line until page_after
            elif tunnel:
                new["raised"] = now
            elif kind == "known":
                severity = "low"   # every host is already known down: a feed line, never "needs you"
        if new != st:
            db.set_kv(netwait.STATE_KEY, new)
        if action == "alert":
            self.alert(netwait.KEY, netwait.text(
                self.p, float(new["since"]), len(unreachable), names, tunnel, now, kind=kind,
                advise=kind == "offline" and netwait.workstation(self.cfg), others=others,
                known=[f"{h} ({why})" for h, why in known.items()]), severity=severity)
            log(self.p, f"network: {len(unreachable)} task(s) wait on unreachable hosts "
                        f"({', '.join(names) or 'unnamed'}{', tunnel route' if tunnel else ''}; {kind}); "
                        f"alerted once ({severity})")
        elif action == "raise":
            names = list(new.get("hosts") or [])
            self.alert(netwait.KEY, netwait.text(self.p, float(new["since"]), len(unreachable), names, True, now,
                                                 others=others),
                       severity="normal")
            log(self.p, f"network: tunnel route to {', '.join(names) or 'unnamed hosts'} still down "
                        f"after {(now - float(new['since'])) / 60:.0f} min; raised to normal")
        elif action == "clear":
            log(self.p, "network: the hosts answer again; the outage alert clears")

    def _reachability_holds(self, now: float) -> tuple[list[tuple[int, list[str]]], bool]:
        """(queued tasks whose current probe last said unreachable, with the hosts it names; whether a
        task could start now or is held by anything but a neutral hold). Neutral: dependencies, a timer,
        a probe that says plain "not yet", a user-paused resource."""
        db = self.p.db
        core = self.cfg.get("core_provider") or "claude"
        queued = db.q("SELECT * FROM tasks WHERE status='queued' ORDER BY priority, id")
        unmet = db.unmet_dependencies(queued)
        ctx = {"now": now, "paused": db.paused_resources(), "busy": {}, "committed": None, "logged_out": {}}
        unreachable, ready = [], []
        for t in queued:
            if unmet[t["id"]]:
                continue
            when = deferral(t).get("when")
            probe = when or (load_result(t["result"]).get("retry_when") if (t["not_before"] or 0) > now else None)
            got = (self._start_rc if when else self._probe_rc).get(t["id"])
            if probe and got and got[0] == netwait.UNREACHABLE_RC and got[2] == probe \
                    and now - got[1] <= 3 * PROBE_EVERY_S:
                out = self._probe_out.get(t["id"])
                unreachable.append((t["id"], netwait.hosts(probe, out[0] if out and out[2] == probe else "")))
            elif not when and (t["not_before"] or 0) <= now:
                ready.append(t)
        for t in ready if unreachable else []:   # dispatch's checks only while a probe says unreachable
            prov = t["provider"] or core
            if prov not in ctx["logged_out"]:
                ctx["logged_out"][prov] = self._logged_out(prov)
            if self._start_hold(t, prov, ctx) != "paused":
                return unreachable, True
        return unreachable, False

    def _net_kind(self, unreachable: list[tuple[int, list[str]]], raw: list[str],
                  now: float, since: float) -> tuple[str, dict[str, str]]:
        """(netwait.classify's "offline" or "hosts", or "known" when every failing host is already known
        down; {alias: why} of the known-down ones). Hosts that answer, only from evidence dated at or after
        the outage's start (`since`): another queued task's probe that reached its ssh host (any exit but
        255, no ssh connect or resolve error in its output), a heal check on a machine whose last run
        passed or failed on the box itself (exit 0 or 1, not an unknown or known-state run)."""
        hosts_ = [h for h in raw if not netwait.loopback(h)]
        try:
            known = netwait.known_down(self.p, hosts_, now)
        except Exception:
            log(self.p, "network: known-down check failed\n" + traceback.format_exc())
            known = {}
        if hosts_ and all(h in known for h in hosts_):
            return "known", known
        mine = {tid for tid, _ in unreachable}
        outs = [o[0] for o in (self._probe_out.get(tid) for tid in mine) if o]
        answering: list[str] = []
        for t in self.p.db.q("SELECT * FROM tasks WHERE status='queued'"):
            if t["id"] in mine:
                continue
            when = deferral(t).get("when")
            probe = when or load_result(t["result"]).get("retry_when")
            got = (self._start_rc if when else self._probe_rc).get(t["id"])
            out = self._probe_out.get(t["id"])
            if probe and got and isinstance(got[0], int) and got[0] != netwait.UNREACHABLE_RC \
                    and got[2] == probe and got[1] >= since and now - got[1] <= 3 * PROBE_EVERY_S \
                    and not (out and out[2] == probe and netwait.ssh_failed(out[0])):
                answering += netwait.hosts(probe)
        try:
            for r, spec in heal.checks(self.p.db):
                s = heal.state(self.p.db, r["name"])
                alias = heal.machine_of(spec)
                at = float(s.get("last_check") or 0)
                if alias and s.get("last_rc") in (0, 1) and not ({"unknown_since", "known_run"} & set(s)) \
                        and at >= since and now - at <= 3 * float(r["every_s"] or 0) + 300:
                    answering.append(alias)
        except Exception:
            log(self.p, "network: heal checks unreadable\n" + traceback.format_exc())
        return netwait.classify(outs, hosts_, answering), {h: w for h, w in known.items() if h in hosts_}

    def _net_others(self, blocked: list[int]) -> int:
        """Queued tasks outside the network-blocked set (and what depends on it) that can still run
        later: on a timer or a probe of their own, not held by a paused resource."""
        db = self.p.db
        queued = db.q("SELECT * FROM tasks WHERE status='queued'")
        unmet, paused, out = db.unmet_dependencies(queued), db.paused_resources(), set(blocked)
        grew = True
        while grew:
            grew = False
            for t in queued:
                if t["id"] not in out and set(unmet[t["id"]]) & out:
                    out.add(t["id"])
                    grew = True
        return sum(1 for t in queued if t["id"] not in out and not coord.task_resources(t) & paused.keys())

    def _healing(self) -> bool:
        """A heal check is still trying its own fix: unhealthy, not escalated, no self-fix task yet."""
        db = self.p.db
        for r, _ in heal.checks(db):
            s = heal.state(db, r["name"])
            if s.get("phase") in ("fixing", "settling") or (
                    s.get("status") == "unhealthy" and not s.get("escalated") and not heal.has_open_task(db, r["name"])):
                return True
        return False

    # coordinator ------------------------------------------------------------------------------------
    def retry_rejected(self) -> None:
        """Wake the coordinator once a rejected action's blocking condition clears (the task cap's
        next free slot, or a raised cap), so work a turn could not start does not wait for an idle wake."""
        db = self.p.db
        wake = db.kv(coord.RETRY_WAKE_KEY) or {}
        if not wake.get("at"):
            return
        review = bool(wake.get("review"))
        later = coord.next_task_slot(db, coord.task_cap(self.cfg, review), review=review)
        with db.tx():
            if later is not None:
                # The slot is not free yet, or went to other work meanwhile: wait for the next one.
                if float(wake["at"]) <= time.time():
                    db.set_kv(coord.RETRY_WAKE_KEY, {**wake, "at": later})
                return
            db.x("DELETE FROM kv WHERE key=?", (coord.RETRY_WAKE_KEY,))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (time.time(), "daemon", "retry_wake", "normal",
                  f"A slot is free for an action an earlier turn could not do ({wake.get('why', '')[:600]}). "
                  f"Do it now if it is still needed.", "queued"))

    def failover_pinned(self, every_s: float = 60) -> None:
        """Move queued and waiting tasks off a provider that cannot start new work for longer than
        coordinator.provider_failover_after_h (failover.py), and a preference pin to a provider other
        than the core one at once, model-free, at most once a minute. Each move is a feed event and
        one low alert; running tasks are only reported."""
        now = time.time()
        if now - getattr(self, "_failover_checked", 0.0) < every_s or not failover.after_h(self.cfg):
            return
        self._failover_checked = now
        db, core = self.p.db, self.cfg.get("core_provider") or "claude"
        try:
            provs = {r["p"] or core for r in db.q("SELECT DISTINCT provider p FROM tasks "
                                                  "WHERE status IN ('queued','waiting','running')")}
            stops, holds, windows = {}, {}, bud.plan_windows(db)
            for prov in provs:   # update_gates leaves out providers only waiting tasks use
                gate = self.gates.get(prov) or bud.evaluate(db, self.cfg, prov, windows)
                pause, out = self._provider_pause(prov), self._logged_out(prov)
                if why := failover.hold(gate, pause, out, now):
                    holds[prov] = why
                if why := failover.stop(gate, pause, out, now):
                    stops[prov] = why
            if not holds:
                return
            ok = []
            for prov in failover.targets(self.cfg):
                if prov in holds or self._logged_out(prov) or self.net_held(prov):
                    continue
                try:
                    if not get_provider(prov).available():
                        continue
                except Exception:
                    continue
                gate = self.gates.get(prov) or bud.evaluate(db, self.cfg, prov, windows)
                if gate.level != "red" and gate.allow_new_work and gate.max_parallel > 0 \
                        and not failover.hold(gate, self._provider_pause(prov), False, now):
                    ok.append(prov)
            moves, running = failover.plan(db, self.cfg, stops, ok, now, holds)
            where = timefmt.home(self.p)
            lines = [failover.apply(db, self.cfg, m, now, where) for m in moves]
            lines += failover.report_running(db, running, now, where)
        except Exception:
            log(self.p, "provider failover failed\n" + traceback.format_exc())
            return
        for m in moves:
            log(self.p, f"task {m['task']['id']}: provider {m['old']} -> {m['new']} ({m['why']})")
        if lines:
            olds = sorted({m["old"] for m in moves} | {r["provider"] for r in running})
            self.alert(f"failover:{','.join(olds)}", "Provider failover: " + "; ".join(lines)[:1500],
                       severity="low", every_s=0)

    def rethink_asks(self) -> None:
        """Hand an open ask that has held work past coordinator.ask_rethink_h back to a tricky
        turn to decide (askrethink.py), once per ask. Checked once a minute."""
        now = time.time()
        if now - getattr(self, "_rethink_checked", 0.0) < 60:
            return
        self._rethink_checked = now
        try:
            for aid in askrethink.raise_due(self.p.db, self.cfg, now):
                log(self.p, f"ask {aid} has held work past coordinator.ask_rethink_h: raised to the coordinator")
        except Exception:   # a failed check must not stop the tick
            log(self.p, "ask rethink failed: " + traceback.format_exc().replace("\n", " | ")[:1500])

    def blocker_sweep(self) -> None:
        """Queue a blocker sweep (sweep.py) when one is due: a tricky turn that re-checks every
        stuck item. Model-free until then; checked once a minute."""
        now = time.time()
        if now - getattr(self, "_sweep_checked", 0.0) < 60:
            return
        self._sweep_checked = now
        db = self.p.db
        try:
            self._probe_budget = now + PROBE_STALL_PASS_S
            for rec in sweep.dispose_probe_stalls(db, self.cfg, now, _current_probe, self._probe_now,
                                                  self._probe_stall_act):
                log(self.p, f"task {rec['task']} probe stall ({rec['hours']} h not yet): {rec['disposition']}: {rec['why']}")
        except Exception:   # a disposition that fails must not stop the sweep
            log(self.p, "probe stall disposition failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
        try:
            why = sweep.due(db, self.cfg, now)
            if not why:
                return
            core = self.cfg.get("core_provider") or "claude"
            busy = {r["provider"]: r["n"] for r in db.q(
                "SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' GROUP BY provider")}
            ctx = {"now": now, "paused": db.paused_resources(), "busy": busy, "committed": None, "logged_out": {}}

            def hold_of(t: dict) -> str | None:
                prov = t["provider"] or core
                if prov not in ctx["logged_out"]:
                    ctx["logged_out"][prov] = self._logged_out(prov)
                return self._start_hold(t, prov, ctx)
            self.sweep_unreviewed(force=True)   # its data pass: no finished change sits unreviewed into it
            # Every item ends model-free and nothing changed since the last model sweep: a receipt, no turn.
            got = sweepskip.check(db, self.cfg, sweep.items(db, now, self.cfg), now, probe_of=self._probe_rc.get,
                                  project=self.p.name)
            if got["skip"]:
                rec = sweepskip.skip(db, got, why, now)
                log(self.p, f"blocker sweep skipped model-free ({why}): {len(rec['items'])} item(s) unchanged, "
                            f"each ends without a turn; ~${rec['usd']:.2f} saved")
                return
            eid = sweep.start(db, self.cfg, why, now, hold_of=hold_of)
            if eid:
                sweepskip.swept(db, got, now)
                log(self.p, f"blocker sweep queued ({why}; a turn: {got['why']})")
        except Exception:   # a sweep that cannot start must not stop the tick
            log(self.p, "blocker sweep failed: " + traceback.format_exc().replace("\n", " | ")[:1500])

    def _probe_now(self, probe: str) -> int | str | None:
        """Run one probe to its end (a probe stall's re-check): its exit code, "timeout" or "could not
        start"; None once this pass's probe time (PROBE_STALL_PASS_S) is spent."""
        left = getattr(self, "_probe_budget", 0.0) - time.time()
        if left <= 0:
            return None
        try:
            proc = subprocess.Popen(landed.probe_command(probe, self.p), shell=True, cwd=str(self.p.root),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True, env=self._probe_env())
        except OSError:
            return "could not start"
        try:
            return proc.wait(timeout=min(PROBE_TIMEOUT_S, left))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return "timeout"

    def _probe_stall_act(self, task: dict, what: str, note: str) -> None:
        """Lift a probe stall's hold model-free: 'passed' as a passed probe does; 'release' appends
        `note` to the spec and lets the task start once at its own tier."""
        now, tid = time.time(), task["id"]
        waiting = not deferral(task).get("when")
        if what == "passed":
            if waiting:
                self._probe_passed(task, now, why="probe passed on a probe-stall re-check")
            else:
                self._start_verdict(task, 0, now)
            return
        self.p.db.update_task(tid, spec=f"{task['spec'] or ''}\n\n{note}".lstrip())
        task = self.p.db.task(tid)
        progress.forget_probe(self.p.db, tid)
        if waiting:
            self._wake_waiting(task, "probe stale: released once by the blocker sweep", now)
            return
        self._probe_rc.pop(tid, None)
        self._start_rc.pop(tid, None)
        startgate.forget(self.p.db, tid=tid)
        self.p.db.update_task(tid, labels=without_deferral(json.loads(task["labels"] or "[]")), not_before=None)
        log(self.p, f"task {tid} start_when stale: released once by the blocker sweep")

    def maybe_coordinate(self) -> None:
        if not self.lease_held():
            return
        db, c = self.p.db, self.cfg["coordinator"]
        if db.one("SELECT id FROM runs WHERE role='coordinator' AND status='running'"):
            return
        now = time.time()
        if now < float(db.kv("coordinator_backoff_until", 0)):
            return
        msgs = db.q("SELECT id, ts, chat FROM messages WHERE direction='in' AND handled=0 ORDER BY id")
        queued = db.q("SELECT id, ts, kind, severity, task, source FROM events WHERE status='queued' ORDER BY id")
        max_evs = int(c.get("max_events_per_turn", 40))
        budget = float(c.get("turn_budget_usd", 1.0))
        # Items whose turn went over its cap are split, raised once or parked, never retried as is (turnsize.py).
        try:
            g = self.gates.get(self.cfg.get("core_provider", "claude"))
            sized = turnsize.plan(db, self.cfg, msgs, queued, (g.as_dict().get("numbers") or {}) if g else {},
                                  max_msgs=30, max_evs=max_evs)
            msgs, evs, budget = sized["msgs"], sized["evs"], sized["budget"]
        except Exception:   # sizing only bounds spend: a failure runs the batch as before
            log(self.p, "coordinator turn sizing failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
            sized = {"info": {}}
            msgs, evs = msgs[:30], queued[:max_evs]
        try:
            self._turn_budget_alert()   # a setting changed since: the parked set the alert names
        except Exception:
            log(self.p, "coordinator turn budget alert failed: " + traceback.format_exc().replace("\n", " | ")[:1000])
        # Each continuation has its own coverage. Never combine sweep batches back into one turn.
        sweep_event = next((e["id"] for e in evs if e["kind"] == sweep.SWEEP_KIND), None)
        evs = [e for e in evs if e["kind"] != sweep.SWEEP_KIND or e["id"] == sweep_event]
        wake: dict = {}
        w: dict | None = None
        if not msgs and not evs:
            last = float(db.kv("last_coordinator_turn", 0))
            if now - last <= min(float(c.get("idle_wake_s", 3600)), float(c.get("starve_wake_s", 300))):
                return
            w = idle_wake(self.p, self.cfg, {k: g.as_dict() for k, g in self.gates.items()}, now)
            if not w["due"]:
                return
            if w["due"] == "starve":
                db.set_kv("starve", w["starve"])
            wake = w["wake"]
        else:
            newest = max([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            oldest = min([m["ts"] for m in msgs] + [e["ts"] for e in evs])
            debounce = float(c.get("debounce_s", 15))
            if now - newest < debounce and now - oldest < 4 * debounce:
                return
            if not msgs and self._batch_hold(queued, now):
                return
        if not msgs:
            try:
                # Every open task held by what no turn lifts, and nothing new since the last turn: a
                # routine batch or wake is settled without a model run (quietgate.py).
                gates_now = {k: g.as_dict() for k, g in self.gates.items()}
                due_now = (w or {}).get("due")

                def pre() -> list[str]:
                    if any(e["kind"] in coord.UNBLOCK_KINDS for e in evs):
                        return ["unblock kind"]
                    return coord.effort_triggers(db, self.cfg, [e["id"] for e in evs], due_now, [], gates_now,
                                                 now, coord.charter_conflicts(self.p), harness=self.p.harness)[0]
                if quietgate.quiet(db, self.cfg, evs, msgs, pre, due_now, self._quiet_fp(gates_now), now,
                                   coord.UNBLOCK_KINDS, sweep.SWEEP_KIND):
                    usd = quietgate.skip(db, evs, due_now, now)
                    if not evs:
                        # Counts as a repeat, and the next wake backs off from this skip.
                        db.set_kv("idle_wake", {**wake, "skipped": now})
                    log(self.p, f"coordinator turn skipped ({quietgate.QUIET}): "
                        + (f"events {[e['id'] for e in evs]}" if evs else f"{due_now} wake")
                        + f", nothing new since the last turn (~${usd:.2f} saved)")
                    return
            except Exception:   # the gate only saves money: a failure runs the turn
                log(self.p, "coordinator quiet gate failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
        seen_now: dict = {}
        if evs and not msgs:
            try:
                # A batch a turn that decided nothing already met, against the same state, is settled
                # without a model run (turngate.py). A batch with an effort trigger always runs; the
                # triggers' seen-state is saved only by a turn that starts, so here it is dropped.
                gates_now = {k: g.as_dict() for k, g in self.gates.items()}
                seen_now = turngate.material(db, wake_fingerprint(self.p, gates_now), [e["id"] for e in evs])
                pre = [] if any(e["kind"] in coord.UNBLOCK_KINDS for e in evs) else \
                    coord.effort_triggers(db, self.cfg, [e["id"] for e in evs], None, [], gates_now, now,
                                          coord.charter_conflicts(self.p), harness=self.p.harness)[0]
                match = turngate.repeat(db, self.cfg, evs, msgs, seen_now, now, sweep.SWEEP_KIND,
                                        pre, coord.UNBLOCK_KINDS)
                if match:
                    turngate.skip(db, evs, match, now)
                    log(self.p, f"coordinator turn skipped: events {[e['id'] for e in evs]} repeat what "
                        f"turn(s) {match['runs']} met and decided nothing on (~${match['usd']:.2f} saved)")
                    return
            except Exception:   # the gate only saves money: a failure runs the turn
                log(self.p, "coordinator turn gate failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
                seen_now = {}
        hour_turns = db.one("SELECT COUNT(*) n FROM runs WHERE role='coordinator' AND started>?", (now - 3600,))["n"]
        if hour_turns >= int(c.get("max_turns_per_hour", 30)) and not msgs:
            return
        gate = self.gates.get(self.cfg.get("core_provider", "claude"))
        if gate and gate.level == "red" and not msgs:
            return
        lim = self._provider_pause(self.cfg.get("core_provider", "claude"))
        if lim and lim.get("until", 0) > now:
            return   # provider paused (logged out or at its limit); the pause expiry is the retry
        gates = {k: v.as_dict() for k, v in self.gates.items()}
        default_chat = msgs[-1]["chat"] if msgs else None
        provider = self.cfg.get("core_provider", "claude")
        logged_out = self._logged_out(provider)
        if logged_out and not self._may_probe(provider, now):
            return   # logged out: one run at a time checks the login, and this turn is not it
        try:
            if sweep_event is not None:
                sweep.prepare(db, sweep_event, self.cfg, now)
            due = (w or {}).get("due")
            lowered: list[str] = []
            triggers, seen = coord.effort_triggers(db, self.cfg, [e["id"] for e in evs], due,
                                                   [m["id"] for m in msgs], gates, now,
                                                   coord.charter_conflicts(self.p), lowered, harness=self.p.harness)
            unblock = ", ".join(triggers)
            flagged = correction.flag(db, self.cfg, msgs, now)   # likely corrections, tagged in the digest
            # Every turn sees background sections the previous turn saw as one line each.
            parts, shown = coord.digest_parts(self.p, gates, [e["id"] for e in evs], [m["id"] for m in msgs],
                                              db.kv(coord.DIGEST_SEEN_KEY) or {}, flagged)
            # The sections rendered from the prompt snapshot end the cached context block.
            cached, parts = coord.split_cached(parts)
            prompt = "\n".join(text for _, text in parts)
            head, context = coord.prompt_parts(self.p)
            context = coord.with_cached(context, cached)
            self.start_run("coordinator", prompt, provider, c.get("tier", "light"), str(self.p.base),
                           read_only=True, schema=coord.ACTIONS_SCHEMA, system=head, context=context,
                           cache_ttl=cachettl.setting(c),
                           budget_usd=budget,
                           note={"messages": [m["id"] for m in msgs], "events": [e["id"] for e in evs],
                                 "budget_usd": budget, **({"sizing": sized["info"]} if sized["info"] else {}),
                                 "default_chat": default_chat, **({"unblock": unblock} if unblock else {}),
                                 "triggers": triggers, **({"lowered": lowered} if lowered else {}),
                                 **({"corrections": {str(k): v for k, v in flagged.items()}} if flagged else {}),
                                 **({"wake_due": due} if due else {}),
                                 "digest": {**coord.digest_size(parts), "cached_chars": len(cached)}, **({"gate": seen_now} if seen_now else {})})
        except Exception as e:
            # A turn that cannot even start backs off like a failed turn instead of retrying every tick.
            log(self.p, "coordinator start failed: " + traceback.format_exc().replace("\n", " | ")[:2000])
            self._coordinator_failed(f"could not start: {type(e).__name__}: {e}"[:250])
            return
        db.set_kv("last_coordinator_turn", now)
        quietgate.started(db)
        db.set_kv(coord.EFFORT_SEEN_KEY, seen)
        db.set_kv(coord.DIGEST_SEEN_KEY, shown)
        db.set_kv("idle_wake", wake)
        if logged_out:
            db.set_kv(f"auth_probe:{provider}", now)

    def _batch_hold(self, evs: list[dict], now: float) -> bool:
        """Routine events (a task done, its follow-ups and notes, normal upstream notes, info to normal
        observations) wait up to coordinator.batch_s so one turn reads several, but only while no worker
        slot would sit idle for it: every slot is busy, runnable queued work is there to fill each free
        one, or the batch is only plain hand-offs (_plain_done) that leave the turn nothing to queue."""
        batch = float(self.cfg["coordinator"].get("batch_s", 300))
        if batch <= 0 or not evs or now - min(e["ts"] for e in evs) >= batch:
            return False
        if not all(coord.batchable(e["kind"], e["severity"]) for e in evs):
            return False
        db = self.p.db
        gate = self.gates.get(self.cfg.get("core_provider", "claude"))
        busy = db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running'")["n"]
        slots = (gate.max_parallel if gate.allow_new_work else 0) if gate else \
            int(self.cfg["budget"].get("max_parallel_workers", 6))
        if busy >= slots:
            return True   # no slot is free; nothing the turn queues could start before one is
        if all(self._plain_done(e) for e in evs):
            return True   # a plain hand-off leaves the turn nothing to queue: it waits for company
        paused = db.paused_resources()
        # A review held for the checks the daemon started on its head (_precheck_holds) fills no slot yet.
        runnable = sum(1 for t in db.ready_tasks() if not coord.task_resources(t) & paused.keys()
                       and not (t["blocked_reason"] or "").startswith((PAUSED_NOTE, LOGGED_OUT_NOTE, NET_HELD_NOTE,
                                                                         BRANCH_HELD_NOTE))
                       and not self._precheck_holds(t, now))
        return busy + runnable >= slots

    def _plain_done(self, e: dict) -> bool:
        """A normal task_done with no follow-up proposed, no live task depending on it and no waiting
        parent: nothing for a turn to decide, so it may wait for batch_s even while a slot is idle."""
        if e["kind"] != "task_done" or e["severity"] != "normal" or not e.get("task"):
            return False
        db, tid = self.p.db, e["task"]
        if db.one("SELECT 1 FROM events WHERE task=? AND kind='followup_proposed' AND status='queued'", (tid,)):
            return False
        task = db.task(tid)
        if task and task.get("parent") and (db.task(task["parent"]) or {}).get("status") == "waiting":
            return False
        live = db.q("SELECT * FROM tasks WHERE status NOT IN (?,?,?) "
                    "AND depends_on NOT IN ('', '[]')", TERMINAL_TASK_STATES)
        return not any(tid in dependency_ids(t) for t in live)

    # workers ----------------------------------------------------------------------------------------
    def dispatch(self) -> None:
        db = self.p.db
        running = db.q("SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' "
                       "GROUP BY provider")
        busy = {r["provider"]: r["n"] for r in running}
        self.check_disk()
        try:
            self._slot_resv_pass = self._slot_reservations(time.time())
        except Exception:   # a broken reservation pass never stops dispatch: it runs in plain pick order
            log(self.p, "slot reservations failed\n" + traceback.format_exc())
            self._slot_resv_pass = {}
        try:
            self._dispatch(busy)
        finally:
            self._slot_resv_pass = None

    def _dispatch(self, busy: dict) -> None:
        db = self.p.db
        ready = db.ready_tasks()   # read after _slot_reservations, which may have woken a reserving task
        reached = set()     # tasks that got past the gates to the resource check
        paused = db.paused_resources()
        now, core = time.time(), self.cfg.get("core_provider", "claude")
        logged_out = {prov: self._logged_out(prov) for prov in {t["provider"] or core for t in ready}}
        # On a logged-out provider the cheapest task goes first: it is the one run that checks the login.
        out = [t for t in ready if logged_out[t["provider"] or core]]
        if out:
            rank = {t: i for i, t in enumerate(bud.TIER_ORDER)}
            ready = [t for t in ready if not logged_out[t["provider"] or core]] + \
                sorted(out, key=lambda t: rank.get(t["tier"], len(rank)))
        live = self._live_claims() if ready else {}
        ctx = {"now": now, "paused": paused, "logged_out": logged_out, "busy": busy, "committed": None, "live": live}
        holds = settinghold.active(db, self.cfg, self.p.root) if ready else {}
        gate0 = self.gates.get(core)
        free = max(gate0.max_parallel - busy.get(core, 0), 0) if gate0 and gate0.allow_new_work else 0
        fate: dict[int, str] = {}   # task id -> a schedlog reason, "full" or "picked" (_log_slot_fill)
        for task in ready:
            if not self.lease_held():   # lapsed during this tick (a slow start or store): start nothing more
                break
            # Before _start_hold, which may reserve the task's resource.
            if holds and self._setting_hold(task, holds, now):
                fate[task["id"]] = "hold"
                continue
            # No run starts without a dollar cap: an unbudgeted run would get no --max-budget-usd and
            # commit nothing toward the dollar caps, so one tick could start several past them. The
            # cap counts from what the task already spent, so an older unbudgeted task is not stopped.
            if not task["budget_usd"]:
                task = dict(task, budget_usd=round((task["spent_usd"] or 0)
                                                   + bud.task_default_usd(self.cfg, task["tier"]), 2))
                db.update_task(task["id"], budget_usd=task["budget_usd"])
            # Before _start_hold: a review whose pre-review checks failed needs no reviewer, nor its gates.
            if task["kind"] == "review" and self._precheck_failed(task):
                fate[task["id"]] = "other"
                continue
            provider = task["provider"] or core
            why = self._start_hold(task, provider, ctx, reserve=True)
            fate[task["id"]] = "full" if why == "gate" and task["id"] in ctx["full"] else SLOT_HELD_BY.get(why, "other")
            if why == "held":
                if task["id"] in ctx["branch"]:
                    self._note_branch_hold(task, ctx["branch"][task["id"]])
                continue
            # A paused resource holds its tasks in the queue, attempts untouched, until it resumes.
            note = task["blocked_reason"] or ""
            if why == "paused":
                hit = sorted(coord.task_resources(task) & paused.keys())
                why = "; ".join(f"{r}: {paused[r]['reason']}" if paused[r].get("reason") else r for r in hit)
                held = (f"{PAUSED_NOTE} {why}; it starts once resumed "
                        f"(`ttp resume {self.p.name} --resource {hit[0]}`)")[:500]
                if note != held:
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if why == "logged_out":
                # Logged out: the queue keeps its tasks, attempts untouched, until a run succeeds.
                held = (f"{LOGGED_OUT_NOTE} ({provider}); it starts once a login check passes")
                if note != held:
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if why == "net":
                # Its API host does not resolve: the task waits, attempts untouched.
                held = f"{NET_HELD_NOTE} waiting for {provider}'s API host to resolve"
                if note != held and (not note or note.startswith(NET_HELD_NOTE)):
                    db.update_task(task["id"], blocked_reason=held)
                continue
            if note.startswith((PAUSED_NOTE, LOGGED_OUT_NOTE, NET_HELD_NOTE, BRANCH_HELD_NOTE)):
                db.update_task(task["id"], blocked_reason=None)
            gate = ctx["gate"]
            remaining, cost = self._start_cost(task)
            if why == "budget":
                db.update_task(task["id"], status="blocked", blocked_reason="task budget exhausted")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", "task_budget_exhausted", "normal",
                      f"#{task['id']} {task['title']} used its ${task['budget_usd']:.2f} budget", "queued", task["id"]))
                continue
            if why == "caps":
                if any(cap and cost > cap for cap in (gate.numbers.get("daily_cap"), gate.numbers.get("weekly_cap"))):
                    db.update_task(task["id"], status="blocked",
                                   blocked_reason=f"its ${cost:.2f} budget is above the dollar cap")
                continue
            if why in (None, "resource"):
                reached.add(task["id"])
            if why == "resource":
                self._log_resource_wait(task, ctx.get("holds", {}).get(task["id"]))
            if why:
                continue
            skipped, gated = self._wake_gate(task)
            if skipped:
                fate[task["id"]] = "start_after"   # it re-deferred the wake
                continue
            review_pick = None
            if task["kind"] == "review" and self._reuse_review(task):
                continue
            if task["kind"] == "review":
                task, review_pick = self._size_review(task)
            picked = self._pick_effort(task, provider)
            if picked:
                task = dict(task, tier=picked["tier"])
            wake = bud.wake_tier(task["tier"], load_result(task["result"]), self.cfg, self.p.harness)
            tier = bud.clamp_tier(wake or task["tier"], gate)
            # A tier routed to another provider (route.py) runs there unless it is cooling down.
            routed = self._route(task, tier, picked, now)
            if routed and routed["fallback"] == "none":
                routed["fallback"] = self._no_room(routed["to"], ctx) or "none"
            run_prov = routed["to"] if routed and routed["fallback"] == "none" else provider
            if run_prov != provider and (limit := route.timeout_s(self.cfg, tier)):
                routed["timeout_s"] = limit   # the tier's routed run time limit, in place of its run_timeout_s
            if run_prov != ctx["slot"].get(task["id"], provider):
                # Its tier or the route changed after the slot check: it takes a slot where it runs.
                why = self._start_hold(task, run_prov, ctx, reserve=True, reroute=False)
                if why:
                    fate[task["id"]] = "full" if why == "gate" and task["id"] in ctx["full"] \
                        else SLOT_HELD_BY.get(why, "other")
                    if why != "resource":
                        reached.discard(task["id"])
                    continue
                gate = ctx["gate"]
                remaining, cost = self._start_cost(task)
            # Before _workdir_for, which would make a missing worktree afresh.
            lost = self._resumable(task, run_prov)
            try:
                found = startcheck.check(self.p, task)   # a dangling worktree or a lost base clone blocks
                cwd, branch = self._workdir_for(task)
            except Exception as e:
                self._workspace_failed(task, e)
                self._unreserve(task)
                continue
            self._workspace_tries(task["id"], clear=True)
            from .prompts import spec_digest, worker_resume, worker_system, worker_task
            try:
                system = worker_system(self.p, task)
                note = {"spec_sha": spec_digest(task)}
                if found:
                    note["start_checks"] = found
                if wake:
                    note["wake"] = {"tier": tier, "escalated": bool(load_result(task["result"]).get("escalated_wake"))}
                if gated:
                    note[waitgate.USE] = gated
                if picked:
                    note["pick"] = picked
                if routed:
                    note[route.KEY] = routed
                if review_pick:
                    note["review_tier"] = review_pick
                    if review_pick.get(conflictfix.KEY):
                        note[conflictfix.KEY] = review_pick[conflictfix.KEY]
                if lost and lost["cwd"] == cwd and not startcheck.remade(found):
                    # The session holds the task and its own work: a short prompt continues it.
                    prompt = worker_resume(self.p, task, lost)
                    note["resumes"] = {"run": lost["run"], "session": lost["session"]}
                else:
                    prompt, lost = worker_task(self.p, task, cwd, branch, wake=note.get("wake"),
                                               conflict=note.get(conflictfix.KEY)), None
                prompt += startcheck.prompt_lines(found)   # a resumed session is told too
                db.update_task(task["id"], status="running", branch=branch, blocked_reason=None)
                self.start_run("worker" if task["kind"] != "review" else "reviewer", prompt, run_prov, tier, cwd,
                               task=task, budget_usd=max(remaining, 0.5) if task["budget_usd"] else None,
                               timeout_s=(routed or {}).get("timeout_s"),
                               read_only=False, append_system=system, note=note,
                               resume=lost["session"] if lost else None)
            except Exception as e:
                self._start_failed(task, e)
                self._unreserve(task)
                continue
            self._start_failures = 0
            fate[task["id"]] = "picked"
            live[task["id"]] = worktree.claims(self.p, dict(task, branch=branch))   # held from others this tick
            if logged_out[provider] if run_prov == provider else self._logged_out(run_prov):
                db.set_kv(f"auth_probe:{run_prov}", now)   # the one check of the login until the next
            self._progress()   # each start may have added a worktree
            busy[run_prov] = busy.get(run_prov, 0) + 1
            if gate.regime == "caps":
                ctx["committed"] += cost
        # A task a gate kept out this tick cannot start, so it must not hold `ttp lock` commands off.
        for task in ready:
            if task["id"] not in reached:
                self._unreserve(task)
        if free:
            try:
                self._log_slot_fill(free, ready, fate, ctx, core)
            except Exception:   # the audit log never stops dispatch
                log(self.p, "scheduler log failed\n" + traceback.format_exc())

    def _log_slot_fill(self, free: int, ready: list[dict], fate: dict[int, str], ctx: dict, core: str) -> None:
        """One schedlog line for this slot fill: the ready tasks dispatch walked, in its order, that started or
        were held only by the worker slots this pass filled (re-checked with no slot taken, nothing reserved),
        held counts by reason for the rest of the queue, and the ids started. A task whose start this pass
        failed counts as other."""
        db = self.p.db
        try:
            due = obligations.owner_due(db) if ready else {}
        except Exception:   # unreadable obligations only lose the flag, as in db.ready_tasks
            due = {}
        runnable, picked, held = [], [], dict.fromkeys(schedlog.REASONS, 0)
        for t in ready:
            why = fate.get(t["id"], "other")
            if why == "full":
                why = self._start_hold(t, t["provider"] or core, dict(ctx, busy={}, holds={}, full={}, branch={}))
                why = "runnable" if why is None else SLOT_HELD_BY.get(why, "other")
            if why in ("picked", "runnable"):
                runnable.append({"id": t["id"], "priority": t["priority"], "owns_obligation": t["id"] in due})
                if why == "picked":
                    picked.append(t["id"])
            else:
                held[why] += 1
        seen = {t["id"] for t in ready}
        rest = [r for r in db.q("SELECT * FROM tasks WHERE status='queued'") if r["id"] not in seen]
        unmet = db.unmet_dependencies(rest) if rest else {}
        for r in rest:
            held["start_after" if (r["not_before"] or 0) > ctx["now"] else "start_when" if "when" in deferral(r)
                 else "depends_on" if unmet.get(r["id"]) else "other"] += 1
        schedlog.record(schedlog.path(self.p), schedlog.entry(free, runnable, held, picked, ctx["now"]),
                        self.__dict__.setdefault("_schedlog", {}), lambda msg: log(self.p, msg))

    def _live_claims(self) -> dict[int, tuple[set[str], set[str]]]:
        """worktree.claims of each running task, by id."""
        return {t["id"]: worktree.claims(self.p, t) for t in self.p.db.q(
            "SELECT * FROM tasks WHERE status='running' AND kind NOT IN ('review','harness')")}

    def _branch_clash(self, task: dict, live: dict) -> tuple[int, str] | None:
        """(holder id, what it uses) while a running task, or one started earlier this tick (`live`:
        _live_claims plus dispatch's starts), works on a branch or worktree `task` would use
        (worktree.claims), or while the task task_add chained it after (worktree.lineage_hold) is
        queued, running or waiting; else None. Two workers on one branch or in one worktree lose each
        other's work; the running one is never touched. Once it ends, the held task starts in a worktree
        of its own (worktree.ensure) from that branch's head. Changes nothing (_start_hold runs it)."""
        db = self.p.db
        mine, why = worktree.claims(self.p, task), None
        for tid, theirs in sorted(live.items()):
            if tid != task["id"] and (what := worktree.clash(mine, theirs)):
                why = (tid, what)
                break
        if why is None and (hid := worktree.lineage_hold(task)) and (h := db.task(hid)) \
                and h["status"] in ("queued", "running", "waiting"):
            why = (hid, worktree.clash(mine, worktree.claims(self.p, h)) or "its lineage")
        return why

    def _note_branch_hold(self, task: dict, why: tuple[int, str]) -> None:
        """Record a _branch_clash hold as the task's blocked_reason (once; attempts untouched)."""
        held = f"{BRANCH_HELD_NOTE}{why[0]}, which is using {why[1]}; it starts once #{why[0]} ends"[:500]
        if (task["blocked_reason"] or "") != held:
            self.p.db.update_task(task["id"], blocked_reason=held)
            log(self.p, f"task {task['id']} held: {held}")

    def _start_cost(self, task: dict) -> tuple[float, float]:
        """What the task may still spend, and what its start commits under the dollar caps."""
        remaining = (task["budget_usd"] or 0) - (task["spent_usd"] or 0)
        return remaining, (max(remaining, 0.5) if task["budget_usd"] else 0.0)

    def _start_hold(self, task: dict, provider: str, ctx: dict, reserve: bool = False,
                    reroute: bool = True) -> str | None:
        """Why dispatch would not start this ready task now, or None when it would: 'held' (another
        live task's branch or worktree, _branch_clash, recorded in ctx["branch"][task id]; the disk
        guard; a review's prechecks), 'paused' (a paused resource), 'logged_out', 'net' (its API host
        does not resolve), 'gate' (the budget gate or its provider's slots), 'optional', 'device'
        (device task slots), 'budget' (task budget spent), 'caps' (dollar caps) or 'resource' (busy,
        including `ttp lock` and device-lock aliases). With 'paused' or 'resource' it records what holds
        the task in ctx["holds"][task id] (_resource_hold's data): the one record check_stall reads. With
        'gate' because every worker slot is taken it records ctx["full"][task id] = max_parallel
        (check_slot_wait reads it). The one copy of dispatch's checks, in its order,
        so check_stall counts exactly what dispatch would start. Nothing changes but with `reserve`
        (_resource_hold) and ctx's caches: ctx holds now, paused, logged_out (provider -> bool), busy
        (provider -> running), committed (dollars running work may still spend under the caps; None:
        not read yet), live (_live_claims, read once when absent; dispatch adds its starts); it sets
        gate (the task's budget gate, once that check is reached) and slot[task id] (the provider whose
        slots it checked: with `reroute`, the one its tier is routed to when that has room, _slot_provider)."""
        now = ctx["now"]
        ctx.setdefault("holds", {}).pop(task["id"], None)
        ctx.setdefault("full", {}).pop(task["id"], None)
        ctx.setdefault("branch", {}).pop(task["id"], None)
        if "live" not in ctx:
            ctx["live"] = self._live_claims()
        if why := self._branch_clash(task, ctx["live"]):
            ctx["branch"][task["id"]] = why
            return "held"
        if self._disk_holds(task) or self._precheck_holds(task, now):
            return "held"
        if coord.task_resources(task) & ctx["paused"].keys():
            ctx["holds"][task["id"]] = {"kind": "pause", "resource": sorted(coord.task_resources(task)
                                                                            & ctx["paused"].keys())[0]}
            return "paused"
        if reroute:
            provider = self._slot_provider(task, provider, ctx)
        ctx.setdefault("slot", {})[task["id"]] = provider
        if ctx["logged_out"].get(provider) and not self._may_probe(provider, now):
            return "logged_out"
        if self.net_held(provider) and not self._net_may_probe(provider):
            return "net"
        gate = ctx["gate"] = self.gates.get(provider) or \
            bud.evaluate(self.p.db, self.cfg, provider, bud.plan_windows(self.p.db))
        if not gate.allow_new_work or ctx["busy"].get(provider, 0) >= gate.max_parallel:
            if gate.allow_new_work:   # held only because every worker slot is taken (check_slot_wait)
                ctx.setdefault("full", {})[task["id"]] = gate.max_parallel
            return "gate"
        if task["origin"] in ("schedule", "harness") and not gate.allow_optional:
            return "optional"
        if needs_device(task, self.cfg) and self._device_tasks_running() >= self._device_max_tasks():
            return "device"
        remaining, cost = self._start_cost(task)
        if task["budget_usd"] and remaining <= 0.05:
            return "budget"
        # Several runs start in one tick: under the dollar caps, each must fit in what is left
        # of them after what running work may still spend, or one tick commits past a cap.
        if gate.regime == "caps":
            if ctx["committed"] is None:
                ctx["committed"] = self._committed_usd()
            if not self._fits_caps(gate, ctx["committed"] + cost):
                return "caps"
        # Checked last: a task that waits for its resource reserves it, and only a task that
        # would otherwise start now may hold others off the resource.
        why = self._resource_hold(task, reserve=reserve)
        if why:
            ctx["holds"][task["id"]] = why   # the recorded hold reason (kind, resource, holders)
            return "resource"
        return None

    def _pick_effort(self, task: dict, provider: str) -> dict | None:
        """The task's tier for this start (see effort.pick), stored on the task; None leaves it as it is."""
        try:
            picked = effort.pick(self.p.db, self.cfg, task, provider, jev=self.jev)
        except Exception as e:   # picking must never hold a task back
            log(self.p, f"task {task['id']}: effort not picked: {e}")
            return None
        if not picked:
            return None
        if picked["tier"] != task["tier"]:
            log(self.p, f"task {task['id']}: tier {task['tier']} -> {picked['tier']} ({picked['by']})")
            marked = effort.mark_raised(task) if picked["by"] == "retry" else None
            self.p.db.update_task(task["id"], tier=picked["tier"], **({"result": marked} if marked else {}),
                                  tier_source="retry_tier_up" if picked["by"] == "retry" else "effort_pick")
        return picked

    def _route(self, task: dict, tier: str, picked: dict | None, now: float) -> dict | None:
        """The run note's `route` entry for a worker run of a tier routed to another provider
        (route.py), or None: `fallback` is none when it starts there, else why it starts on the core
        provider (a cool-down after a fallback, or the auth gate holding the endpoint)."""
        to = route.target(self.cfg, tier)
        if not to or not route.eligible(task, picked):
            return None
        name, model = to
        after = route.fell_back_before(self.p.db, task["id"]) if task.get("id") else None
        if after:   # the task fell back on its own once: the rest of its runs stay on the core provider
            return {"tier": tier, "to": name, "model": model, "fallback": "rerun", "after": after}
        why = route.cooling(self.p.db, name, now)
        if not why and self._logged_out(name) and not self._may_probe(name, now):
            why = "auth"   # the auth gate holds it; its one login check may be a routed run
        return {"tier": tier, "to": name, "model": model, "fallback": why or "none"}

    def _slot_provider(self, task: dict, provider: str, ctx: dict) -> str:
        """The provider whose worker slots a start of `task` takes: the one its tier is routed to
        (_route at the task's tier) when that can take a run now, else `provider`."""
        if provider != (task["provider"] or self.cfg.get("core_provider", "claude")):
            return provider
        routed = self._route(task, task["tier"], None, ctx["now"])
        if not routed or routed["fallback"] != "none" or self._no_room(routed["to"], ctx):
            return provider
        return routed["to"]

    def _no_room(self, prov: str, ctx: dict) -> str:
        """Why routed-to `prov` cannot take a run now, as a route fallback reason (route.REASONS), or
        '': auth (logged out), down (its API host does not resolve), busy (its gate holds new work, a
        pause, or every one of its worker slots is taken)."""
        now = ctx["now"]
        if self._logged_out(prov) and not self._may_probe(prov, now):
            return "auth"
        if self.net_held(prov) and not self._net_may_probe(prov):
            return "down"
        gate = self.gates.get(prov) or bud.evaluate(self.p.db, self.cfg, prov, bud.plan_windows(self.p.db))
        lim = self._provider_pause(prov)
        if (lim and lim.get("until", 0) > now) or not gate.allow_new_work \
                or ctx["busy"].get(prov, 0) >= gate.max_parallel:
            return "busy"
        return ""

    def _resumable(self, task: dict, provider: str) -> dict | None:
        """The task's last run, when the host took it away (reboot, sleep, lost supervisor) after
        real progress and its agent session can be continued in the same working directory: a fresh
        start would pay again for everything that run read and did. None otherwise: failed,
        timed-out and finished runs never resume, nor does a run whose hand-off stands, except one
        handed back because its on_pass_cmd's dry run crashed (onpassdry.KEY)."""
        back = load_result(task["result"]).get(onpassdry.KEY)
        if isinstance(back, dict):
            r = self.p.db.one("SELECT * FROM runs WHERE id=?", (back.get("run"),))
            session = back.get("session")
            spec = _read_result(Path(r["dir"]) / "run.json") or {} if r and r["dir"] else {}
            cwd, run_env = str(spec.get("cwd") or ""), spec.get("env") if isinstance(spec.get("env"), dict) else {}
            prov = get_provider(provider)
            if not r or r["provider"] != provider or not session or not cwd or not Path(cwd).is_dir() \
                    or not prov.resume_args(session) or not prov.session_saved(session, cwd, run_env):
                return None   # a fresh run reads the dry run's output as why it woke
            return {"run": r["id"], "session": session, "cwd": cwd, "dir": str(r["dir"]), "ended": r["ended"],
                    "cause": "dry_run", "dry_run": back.get("text") or "",
                    "spec_sha": json.loads(r["note"] or "{}").get("spec_sha")}
        want = self.cfg["budget"].get("resume_lost")
        if not isinstance(want, dict):
            return None
        r = self.p.db.one("SELECT * FROM runs WHERE task=? AND role!='coordinator' ORDER BY id DESC LIMIT 1",
                          (task["id"],))
        if not r or r["status"] != "lost" or r["provider"] != provider or not r["dir"]:
            return None
        note = json.loads(r["note"] or "{}")
        session = note.get("session_id")
        run_dir = Path(r["dir"])
        if not session or (_read_result(run_dir / RESULT_FILE) or {}).get("status") in HANDOFF_STATES:
            return None
        # A resume cut short before it did anything carries the work of the runs it continued.
        cost, took, prev, seen = 0.0, 0.0, r, set()
        while prev and prev["id"] not in seen:
            seen.add(prev["id"])
            cost += float(prev["cost_usd"] or 0)
            took += float(prev["ended"] or 0) - float(prev["started"] or 0)
            back = (json.loads(prev["note"] or "{}").get("resumes") or {}).get("run")
            prev = self.p.db.one("SELECT * FROM runs WHERE id=?", (back,)) if back else None
        if cost < float(want.get("min_usd", 0.5)) and took < float(want.get("min_s", 600)):
            return None
        spec = _read_result(run_dir / "run.json") or {}
        cwd, run_env = str(spec.get("cwd") or ""), spec.get("env") if isinstance(spec.get("env"), dict) else {}
        prov = get_provider(provider)
        if not cwd or not Path(cwd).is_dir() or not prov.resume_args(session) or \
                not prov.session_saved(session, cwd, run_env):
            return None
        cause = "reboot" if note.get("lost_to_reboot") else "sleep" if note.get("lost_to_sleep") \
            else "network" if note.get("lost_to_network") else "lost"
        return {"run": r["id"], "session": session, "cwd": cwd, "dir": str(run_dir), "ended": r["ended"],
                "cause": cause, "spec_sha": note.get("spec_sha")}

    def _wake_gate(self, task: dict) -> tuple[bool, dict | None]:
        """The wait-wake gate (waitgate.py) on a waiting task due now. (True, None): it re-deferred the
        wake with no model turn. Else (False, what the run's note keeps for settling, or None when the
        gate does not apply). Any error wakes the model as before."""
        prev = load_result(task["result"])
        if prev.get("status") != "waiting":
            return False, None
        db, now, tid = self.p.db, time.time(), task["id"]
        try:
            from .prompts import spec_digest
            last = db.one("SELECT * FROM runs WHERE task=? AND role!='coordinator' AND status!='running' "
                          "ORDER BY id DESC LIMIT 1", (tid,))
            probe = None
            got = self._probe_rc.get(tid) or self._woke_on.get(tid)
            since = float(prev.get("waiting_since") or 0)
            if got and got[2] == prev.get("retry_when") and got[1] >= since:
                out = self._probe_out.get(tid)
                probe = (got[0], out[0] if out and out[2] == got[2] else "")
            d = waitgate.decide(self.p, self.cfg, task, prev, now, probe=probe, last_run=last,
                                jev=self.jev, spec_sha=spec_digest(task))
        except Exception as e:   # a wake is always safe
            log(self.p, f"task {tid}: wake gate failed, waking: {type(e).__name__}: {e}")
            return False, None
        if d is None:
            return False, None
        if d.wake:
            log(self.p, f"task {tid} wake gate: wake ({d.by}: {d.reason})")
            return False, d.note(prev, d.extra.get("skips") or [])
        nb = now + d.delay_s
        db.update_task(tid, not_before=nb, result=dump_result(waitgate.skipped(prev, d, now)), blocked_reason=(
            f"waiting for {self._wait_what(tid, prev)}; the wake gate skipped a turn ({d.reason}); next try "
            f"{timefmt.short(nb, self.p)}")[:500])
        self._woke_on.pop(tid, None)
        log(self.p, f"task {tid} wake gate: skip ({d.by}: {d.reason}); asleep until {timefmt.short(nb, self.p)}")
        self._unreserve(task)
        return True, None

    def _unreserve(self, task: dict) -> None:
        for res in _exclusive(task):
            locks.unreserve(self._reserve_path(res), shared.holder(self.p, res, hold.who(task['id']), self.cfg))

    def _committed_usd(self) -> float:
        """Budget that running workers on providers under the dollar caps have left to spend."""
        rows = self.p.db.q("SELECT t.provider, t.budget_usd, t.spent_usd FROM runs r JOIN tasks t ON t.id=r.task "
                           "WHERE r.status='running' AND r.role!='coordinator'")
        core = self.cfg.get("core_provider", "claude")
        return sum(max((r["budget_usd"] or 0) - (r["spent_usd"] or 0), 0) for r in rows
                   if getattr(self.gates.get(r["provider"] or core), "regime", "caps") == "caps")

    @staticmethod
    def _fits_caps(gate, usd: float) -> bool:
        n = gate.numbers
        return all(not cap or spent + usd <= cap for spent, cap in (
            (n.get("spent_24h", 0), n.get("daily_cap")), (n.get("spent_7d", 0), n.get("weekly_cap"))))

    def check_disk(self) -> None:
        """The disk guard. Free space under the project folder and its worktrees below the smaller of
        `disk.min_free_pct` of the disk and `disk.min_free_gb` (either at 0 turns it off) holds new
        tasks that may build or check out code; see _disk_holds. One high alert per episode, which ends
        once free space is back at `disk.resume_free_gb` (never below the threshold), or, with that
        unset or at least the disk's size, above DISK_RESUME times the threshold, so a disk hovering
        at the line does not flap.
        The episode is kept in the database: a restart neither re-alerts nor forgets it."""
        cfg = self.cfg.get("disk", {})
        pct, gb, mine = disk_guard_limits(cfg)
        # A machine's own threshold keeps the DISK_RESUME rule: the project's resume point was set
        # against its own threshold and could hold a shared disk that others keep near full forever.
        resume_gb = None if mine else disk_resume_gb(cfg)

        def measure(held: bool):
            worst = None   # (margin, path, free, total, threshold, resume point)
            for path in {self.p.base.resolve(), self.p.worktrees.resolve()}:
                try:
                    u = shutil.disk_usage(path)
                except OSError:
                    continue
                need = min(pct / 100 * u.total, gb * 1e9)
                resume = need * DISK_RESUME if resume_gb is None else max(need, resume_gb * 1e9)
                if resume >= u.total:   # a resume point the disk can never reach would hold it forever
                    resume = need * DISK_RESUME
                margin = u.free - (resume if held else need)
                if worst is None or margin < worst[0]:
                    worst = (margin, path, u.free, u.total, need, resume)
            return worst
        worst = measure(self._disk_low)
        if worst is None:
            return
        _, path, free, total, need, resume = worst
        self._disk_free = free
        low = need > 0 and worst[0] < 0
        now = time.time()
        swept = None
        if low and now - self._disk_swept >= DISK_SWEEP_EVERY_S:
            # Free what finished tasks hold first; held work is released once free space is back above
            # the guard itself (not the resume point: the sweep, not a hovering disk, freed it).
            self._disk_swept = now
            swept = self._disk_sweep()
            if swept["removed"]:
                again = measure(False)
                if again is not None:
                    _, path, free, total, need, resume = again
                    self._disk_free = free
                    low = need > 0 and again[0] < 0
        db = self.p.db
        info = {"path": str(path), "free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1),
                "threshold_gb": round(need / 1e9, 1), "resume_gb": round(resume / 1e9, 1), "low": low,
                "checked": now, **({"machine": mine[0]} if mine else {})}
        last = db.kv("disk") or {}
        if (low != last.get("low") or abs(info["free_gb"] - float(last.get("free_gb") or 0)) >= 1
                or now - float(last.get("checked") or 0) > 600):
            db.set_kv("disk", info)
        if low == self._disk_low:
            return
        self._disk_low = low
        if low:
            usage = disk_usage_line(disk_breakdown(self.p, path), total - free)
            db.set_kv("disk_low", {"path": str(path), "free_gb": info["free_gb"], "threshold_gb": info["threshold_gb"],
                                   "since": now, "usage": usage})
            log(self.p, f"disk low: {free / 1e9:.1f} GB free under {path} (guard {need / 1e9:.1f} GB); "
                        f"only questions and plans start. {usage}")
            source = f"machine {mine[0]}'s min_free_gb" if mine else "disk.min_free_gb"
            done = (f"Swept finished tasks' worktrees: {swept['removed']} removed, {swept['kept']} kept, "
                    f"{swept['freed'] / 1e9:.1f} GB freed; `ttp prune {self.p.name} --dry-run` lists why each is kept."
                    if swept else f"Finished tasks' worktrees are removed as they end; `ttp prune {self.p.name}` "
                                  f"sweeps now and lists the ones kept.")
            self.alert("disk", f"Only {free / 1e9:.1f} GB free under {path} (guard: {need / 1e9:.0f} GB, the smaller "
                               f"of {pct:g}% of the disk and {gb:g} GB from {source}). {usage} New tasks other than "
                               f"questions and plans are held until {resume / 1e9:.0f} GB are free; running "
                               f"work, questions, plans and replies continue. {done} A shared disk "
                               f"that others keep near full by design takes its own threshold: `ttp machines add "
                               f"<alias> --min-free-gb N`.", "high", every_s=0)
        else:
            db.set_kv("disk_low", None)
            log(self.p, f"disk space ok again: {free / 1e9:.1f} GB free under {path}")

    def _archive_reserve(self) -> float:
        """Bytes an archive of the worktree sweep leaves free on state/'s filesystem: the disk guard's threshold."""
        try:
            return disk_guard_bytes(self.cfg.get("disk", {}), self.p.state)
        except OSError:
            return 0.0

    def _disk_sweep(self) -> dict:
        """The disk guard's own sweep of finished tasks' worktrees (prune_worktrees, forced: kept ones
        are looked at again), model-free; check_disk runs it at most every DISK_SWEEP_EVERY_S while
        the disk is low. It archives at most one worktree (see prune_worktrees); the rest follow on
        later ticks, still forced. Logs and returns {"removed", "kept", "freed" (bytes)}."""
        try:
            before = shutil.disk_usage(self.p.worktrees.resolve()).free
        except OSError:
            before = None
        try:
            res = self.prune_worktrees(every_s=0, force=True)
        except Exception:
            log(self.p, "disk guard sweep: " + traceback.format_exc().replace("\n", " | ")[:1000])
            res = []
        removed = sum(1 for r in res if r.get("why") is None)
        try:
            freed = max(shutil.disk_usage(self.p.worktrees.resolve()).free - before, 0) if removed and before else 0
        except OSError:
            freed = 0
        log(self.p, f"disk guard sweep: {removed} finished task worktree(s) removed, {len(res) - removed} kept, "
                    f"{freed / 1e9:.1f} GB freed")
        return {"removed": removed, "kept": len(res) - removed, "freed": freed}

    def lint_charter(self) -> None:
        """Flag dated charter sections that contradict a standing restriction (coord.charter_lint);
        a stat call per tick, the scan only when the charter changed. Then raise a user's charter
        change that failed, but not on the guard's refusal, again once (coord.reraise_charter_changes)."""
        try:
            coord.charter_lint(self.p)
        except Exception:
            log(self.p, "charter lint: " + traceback.format_exc().replace("\n", " | ")[:1000])
        try:
            coord.reraise_charter_changes(self.p)
        except Exception:
            log(self.p, "charter re-raise: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def heal_harness_git(self) -> None:
        """Every harness_git.sweep_every_s, commit the charter and memory files a failed harness commit
        left behind (Project.sweep_harness), moving aside any stale git lock first (ttp.gitlocks): such
        a lock blocks every harness commit. Each move is logged and kept as a handled event."""
        now = time.time()
        if now - getattr(self, "_harness_swept", 0.0) < self.p._harness_git_cfg("sweep_every_s"):
            return
        self._harness_swept = now
        try:
            moved = self.p.clear_stale_git_locks(now)
            if moved:
                self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                            (now, "daemon", "git_lock_cleared", "low",
                             f"Moved stale git lock file(s) no process held aside: {', '.join(moved)}"[:500],
                             "handled"))
            self.p.sweep_harness()
        except Exception:
            log(self.p, "harness git sweep: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def check_integrity(self, start: bool = False) -> None:
        """On the first start of a new boot, check the harness for damage a power cut left and repair
        what its last commit can (see ttp.integrity); also report unfinished code tasks' worktrees
        whose `git status` fails, and kept task worktrees whose repository has no .git directory
        (an empty clone). While something stays broken, check again at each start and hourly.
        Repairs are a note in the feed; what stays broken is one alert keyed `integrity`. At each
        start, a project.json made on another machine is one alert per change (`integrity-host`)."""
        db = self.p.db
        self._check_host(start)
        last = db.kv(KV_INTEGRITY) or {}
        broken = bool(last.get("bad") or last.get("worktrees") or last.get("clones"))
        if start:
            due = not self._this_boot(last.get("boot")) or broken
        else:   # a tick: only the hourly re-check of something still broken
            due = broken and time.time() - float(last.get("at") or 0) >= INTEGRITY_RECHECK_S
        if not due:
            return
        try:
            ids = [r["id"] for r in db.q(f"SELECT id FROM tasks WHERE {worktree.OWN_WORKTREE_SQL} AND status NOT IN "
                                         f"({','.join('?' * len(TERMINAL_TASK_STATES))})", TERMINAL_TASK_STATES)]
            kept = sorted((int(w.name[1:]), w) for w in self.p.worktrees.glob("t*") if w.name[1:].isdigit())
            res = integrity.check(self.p, [(t, self.p.worktrees / f"t{t}") for t in ids], kept)
        except Exception:
            log(self.p, "integrity check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return
        res.update(boot=self.boot, at=time.time())
        db.set_kv(KV_INTEGRITY, res)
        log(self.p, f"integrity check in {res['seconds']} s: fsck {res['fsck']}, {len(res['restored'])} restored, "
                    f"{len(res['bad'])} unrepaired, {len(res['worktrees'])} broken worktree(s), "
                    f"{len(res.get('clones') or [])} repo(s) without .git")
        if res["restored"]:
            db.post("out", "The harness check after a restart repaired files a cut write had damaged: "
                           + "; ".join(res["restored"])[:2000], kind="info", severity="low")
        text = integrity.problem_text(res)
        if text:
            self.alert("integrity", text, severity="high", every_s=86400)

    def _check_host(self, start: bool) -> None:
        """At start, and hourly while it is open: does project.json name another machine than this
        one? One alert per change of machine; kv KV_INTEGRITY_HOST keeps the open change, so a
        restart does not repeat it, and the alert clears once project.json names this machine."""
        db = self.p.db
        seen = db.kv(KV_INTEGRITY_HOST) or None
        if not start and not (seen and time.time() - float(seen.get("at") or 0) >= INTEGRITY_RECHECK_S):
            return
        try:
            change = integrity.host_change(self.p)
        except Exception:
            log(self.p, "host check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return
        db.set_kv(KV_INTEGRITY_HOST, {**change, "at": time.time()} if change else None)
        if change and not (seen and (seen.get("was"), seen.get("now")) == (change["was"], change["now"])):
            log(self.p, f"host check: project.json names {change['was']}, this machine is {change['now']}")
            self.alert("integrity-host", integrity.host_text(change), severity="high", every_s=0)

    def _recover_upgrade(self) -> None:
        """A `ttp upgrade` cut short (a reboot mid-way) is finished or rolled back (upjournal), at start and
        on each release check, under the upgrade lock (a rerun of it may hold the lock: it recovers)."""
        if not upjournal.path(self.p).exists():
            return
        held = locks.try_take([release.upgrade_lock(self.p)], f"ttp daemon (pid {os.getpid()})", "upgrade recovery")
        if held is None:
            return
        try:
            line = upjournal.recover(self.p, daemon_started=self._started, log=lambda m: log(self.p, m))
            if line:
                self.p.db.post("out", f"tt-project: {line}", chat=None, kind="alert", severity="low")
        except Exception:
            log(self.p, "upgrade recovery error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        finally:
            held.close()

    def check_release(self) -> None:
        """Hourly (and at start): is a newer tt-project installed than this harness runs? Status and
        the web app say so while it is. With upgrade.auto on, and no push or upgrade in flight, start
        this project's own `ttp upgrade` once per strictly newer release; it restarts this daemon,
        keeping workers. The same version from another commit is only shown. A project that was paused is
        checked on its first tick after the resume: the upgrade the pause held back starts then."""
        now = time.time()
        self._recover_upgrade()
        paused = bool(self.p.db.kv("paused", False))
        if getattr(self, "_release_paused", False) and not paused:
            self._release_due = 0.0
        self._release_paused = paused
        if now < self._release_due:
            return
        self._release_due = now + release.CHECK_S
        db = self.p.db
        release.guard_harness(self.p.harness)    # harnesses made before the guard get it without an upgrade
        self.check_older_release()   # first: drift must see a lib/current it restored
        try:
            d = release.drift(self.p)
        except Exception as e:   # a half-written install must not stop the tick
            log(self.p, f"release check failed: {type(e).__name__}: {e}")
            return
        if d != db.kv(release.KV_RELEASE):
            db.set_kv(release.KV_RELEASE, d)
            if d:
                log(self.p, f"tt-project {d['installed']} installed; harness on {d['current']}")
        try:        # a finish task stuck past release.STALE_S is told to the maintainers once
            done = release.escalate_stuck(self.p)
            if done:
                log(self.p, f"template upgrade {done}; one note went to the upstream inbox")
        except Exception as e:
            log(self.p, f"finish-task escalation failed: {type(e).__name__}: {e}")
        if not release.retried(d, self.cfg) or db.kv("paused", False):
            return
        why = release.hold_reason(self.p, d)
        if why:
            soon = why.endswith(("in flight", release.CYCLE_HELD))
            if soon:
                self._release_due = now + release.HELD_RECHECK_S
            if (db.kv(release.KV_AUTO) or {}).get("key") != d["key"] or soon:
                log(self.p, f"automatic upgrade to {d['installed']} held: {why}")
            return
        log(self.p, f"automatic upgrade from {d['current']} to {d['installed']}: starting `ttp upgrade`")
        self._release_due = now + release.HELD_RECHECK_S   # an upgrade that finds a push at its swap retries soon
        try:
            release.start(self.p, d)
        except Exception as e:
            release.finish(self.p, "failed", why=f"{type(e).__name__}: {str(e)[:200]}")
            log(self.p, f"automatic upgrade did not start: {type(e).__name__}: {e}")

    def check_older_release(self) -> None:
        """~/.tt-project/lib/current holds an older version than this harness runs: an older plugin's
        `ttp setup` replaced the newer install, so `ttp` on PATH, remote shipping and automatic
        upgrades all work from the older release. When a complete release at the harness version or
        newer is still in ~/.tt-project/lib, point lib/current back at it and say so (low). Otherwise,
        or when `ttp setup --force` made the downgrade on purpose, one alert that clears by itself."""
        db = self.p.db
        try:
            o = release.older(self.p)
            back = (release.restorable(o["harness"]) if o and not o.get("forced")
                    and release.installed().is_symlink() else None)
            if back:
                release.point_current(back)
                log(self.p, f"installed tt-project {o['installed']} was older than this harness "
                            f"({o['harness']}): lib/current now points at {back}")
                self.alert("release-restored",
                           f"An older tt-project ({o['installed']}) had replaced the installed release; "
                           f"lib/current points at {back.name} again, so `ttp` on PATH and automatic "
                           f"upgrades use it.", severity="low", every_s=86400)
                o = release.older(self.p)
        except Exception as e:
            log(self.p, f"installed-release check failed: {type(e).__name__}: {e}")
            return
        if o == db.kv("release_older"):
            return
        db.set_kv("release_older", o)
        if not o:
            log(self.p, "the installed tt-project is no longer older than this harness")
            return
        log(self.p, f"installed tt-project {o['installed']} is older than this harness ({o['harness']})")
        why = ("`ttp setup --force` installed it on purpose, so it is left as is"
               if o.get("forced") else
               f"an older plugin's `ttp setup` replaced a newer install, and ~/.tt-project/lib holds no "
               f"{o['harness']} or newer to point back at")
        self.alert("release-older", f"The installed tt-project ({o['installed']}) is older than this harness "
                                    f"({o['harness']}): {why}. `ttp` on PATH and automatic upgrades use the "
                                    f"older release until `ttp setup` runs from a plugin at {o['harness']} or "
                                    f"newer; this alert clears by itself then.", "high", every_s=0)

    def check_local_only(self) -> None:
        """A done code task may leave the only copy of its work on a local branch. Hourly, and in the
        tick a code task hands off done, fetch and look for the branches of code tasks done in the
        last LOCAL_ONLY_DAYS that hold work no remote has (worktree.local_only: neither the head nor,
        rebased, amended or batched by a reviewer, its changes are on a remote, and no task recorded
        it as superseded by a commit a remote has, landed.record_superseded). Left alone: a task
        an unfinished task still needs (a queued review or a fix, see worktree.needed_by), one a done
        review names (its id, branch or head commit), and one that finished before this check first
        ran (KV_LOCAL_ONLY_FROM), so an upgrade posts no burst for old work. Each newly found one
        posts one coordinator event; status and the web app count it until the work is pushed or
        merged, the task leaves done (cancelled) or falls out of the window. Nothing is pushed. A
        repository without a remote is skipped quietly. After a failed fetch the remote refs may be
        old: flags may clear, but no new one is raised. The fetch and the git comparisons run in a
        thread and a later tick applies what they found: in a large repository they can take minutes,
        which held the tick and delayed every other part of it. While a check runs none starts; a
        check that fails, in the thread or before it, is logged and the next one starts when due."""
        job = self._local_only_job
        if job is not None:
            if job["done"].is_set():
                self._local_only_job = None
                self._apply_local_only(job)
            return
        now = time.time()
        if now < self._local_only_due:
            return
        self._local_only_due = now + LOCAL_ONLY_EVERY_S
        db = self.p.db
        since = db.kv(KV_LOCAL_ONLY_FROM)
        if not isinstance(since, (int, float)):
            since = now
            db.set_kv(KV_LOCAL_ONLY_FROM, since)
        told = db.kv(KV_LOCAL_ONLY) or {}
        tasks = db.q(f"SELECT id, title, branch, updated FROM tasks WHERE {worktree.OWN_WORKTREE_SQL} AND status='done' "
                     f"AND branch IS NOT NULL AND branch!='' AND updated>=? AND {NOT_ABANDONED_SQL}",
                     (max(since, now - LOCAL_ONLY_DAYS * 86400),))
        reviews = []
        if tasks:
            open_tasks = db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE status NOT IN (%s)"
                              % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)
            reviews = db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE kind='review' "
                           "AND status='done' AND updated>=?", (now - 2 * LOCAL_ONLY_DAYS * 86400,))
            tasks = [t for t in tasks if not worktree.needed_by(t, open_tasks) and not worktree.needed_by(t, reviews)]
            backing = db.kv(KV_BACKUP) or {}   # a backup push still to come decides first
            tasks = [t for t in tasks if str(t["id"]) not in backing]
        if not tasks and not told:
            return
        job = {"tasks": tasks, "reviews": reviews, "found": ({}, True, {}), "error": None, "done": threading.Event()}
        if not tasks:
            job["done"].set()
            return self._apply_local_only(job)
        try:
            branches, known = [t["branch"] for t in tasks], set(self._local_only_ok.items())
            targets = [b for b in [worktree.base_ref(self.p)] if b]
            pushed = pushq.pushed_heads(self.p.db)
            recorded = landed.superseded(db, [t["id"] for t in tasks])
            superseded = {t["branch"]: recorded[t["id"]] for t in tasks if t["id"] in recorded}
        except Exception:
            log(self.p, "local-only branch check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return

        def work() -> None:
            try:
                job["found"] = worktree.local_only(self.p.root, branches, targets=targets, known=known, pushed=pushed,
                                                   superseded=superseded)
            except Exception:
                job["error"] = traceback.format_exc().replace("\n", " | ")[:1000]
            finally:
                job["done"].set()
        self._local_only_job = job
        threading.Thread(target=work, daemon=True).start()

    def _apply_local_only(self, job: dict) -> None:
        """Record what a finished check_local_only found: flag the new local-only branches, clear the rest."""
        if job["error"]:
            log(self.p, "local-only branch check: " + job["error"])
            return
        db, now, tasks, reviews, found = self.p.db, time.time(), job["tasks"], job["reviews"], job["found"]
        told = db.kv(KV_LOCAL_ONLY) or {}
        if found is None:
            if told:
                db.set_kv(KV_LOCAL_ONLY, None)
            return
        heads, fetched, clean = found
        self._local_only_ok.update(clean)
        live = {}
        with db.tx():
            for t in tasks:
                key, b = str(t["id"]), t["branch"]
                if b not in heads or not fetched and key not in told:
                    continue
                head, ahead = heads[b]
                if _names_commit(reviews, head):
                    continue
                live[key] = {"branch": b, "head": head, "ahead": ahead, "since": (told.get(key) or {}).get("since", now)}
                if key not in told:
                    log(self.p, f"task {t['id']}: branch {b} exists only on this machine ({ahead} commits ahead)")
                    # A hop whose continuation the daemon queued, with a review still carrying its
                    # branch: the notice goes to the next digest without a turn of its own (chaincap).
                    # The next hop starts from the base, so without that review it stays queued. A split's
                    # branch is carried by its open continue hop, which builds on it.
                    folded = _carrying_review(db, t["id"]) if chaincap.auto_continued(db, t["id"]) else None
                    hop = None if folded or not chaincap.split_head(t) else self._open_hop(t)
                    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                         (now, f"task:{t['id']}", "local_only", "normal",
                          f"task #{t['id']}'s branch {b} exists only on this machine, {ahead} commit"
                          f"{'' if ahead == 1 else 's'} ahead of every remote ({t['title']}). Nothing pushes it "
                          f"automatically: deliver it as the charter allows, or cancel the task if the work is "
                          f"not wanted. If it already landed as another commit (re-landed), a task records that "
                          f"with `ttp task {self.p.name} superseded {t['id']} <sha>`.",
                          "handled" if folded or hop else "queued", t["id"]))
                    if folded or hop:
                        chaincap.add_notice(db, f"task #{t['id']}'s branch {b} exists only on this machine, {ahead} "
                                                f"commit{'' if ahead == 1 else 's'} ahead of every remote; its "
                                                + (f"review #{folded}" if folded else f"continue hop #{hop['id']}")
                                                + " carries it.")
            if live != told:
                db.set_kv(KV_LOCAL_ONLY, live or None)

    def _queue_backup(self, task: dict) -> None:
        """With delivery.backup_remote set, line a done code task's branch up for backup_branches.
        Off (the default), nothing is queued and nothing is pushed."""
        d = self.cfg.get("delivery") or {}
        branch = str(task.get("branch") or "")
        if not str(d.get("backup_remote") or "").strip() or not branch:
            return
        db = self.p.db
        pending = db.kv(KV_BACKUP) or {}
        pending[str(task["id"])] = {"branch": branch, "tries": 0, "next": 0}
        db.set_kv(KV_BACKUP, pending)

    def backup_branches(self) -> None:
        """Push each done code task's own branch to `delivery.backup_remote` (a git remote; off when
        unset), so work that is on no remote yet has a copy off this machine. Fast-forward only, under
        the same name: never with force, never to the push branch, main or the base_ref (push.backup).
        A push that is not a fast-forward is skipped with one observation; one that fails is tried
        again later, BACKUP_TRIES times in all. The queue (KV_BACKUP) survives restarts; the pushes run
        in a thread and a later tick applies what they did. Turning the key off or forbidding pushes
        (delivery.push_allowed) drops the queue without pushing. The local-only check leaves a queued
        branch alone, and looks again once its backup ended."""
        job = self._backup_job
        if job is not None:
            if job["done"].is_set():
                self._backup_job = None
                self._apply_backup(job)
            return
        db = self.p.db
        pending = db.kv(KV_BACKUP) or {}
        if not pending:
            return
        d = self.cfg.get("delivery") or {}
        remote = str(d.get("backup_remote") or "").strip()
        why = push.backup_problem(d) or ("" if push_allowed(d) else "delivery.push_allowed is off")
        if not remote or why:
            if why:
                log(self.p, f"backup of {len(pending)} task branch(es) dropped: {why}")
            db.set_kv(KV_BACKUP, None)
            self._local_only_due = 0.0
            return
        now = time.time()
        due = {k: v for k, v in pending.items() if float(v.get("next") or 0) <= now}
        if not due:
            return
        job = {"remote": remote, "due": due, "out": {}, "done": threading.Event()}
        root = self.p.root

        def work() -> None:
            for k, v in due.items():
                try:
                    job["out"][k] = push.backup(root, remote, v["branch"])
                except Exception:
                    job["out"][k] = ("failed", traceback.format_exc().replace("\n", " | ")[-300:])
            job["done"].set()
        self._backup_job = job
        threading.Thread(target=work, daemon=True).start()

    def replicate_state(self, check_every_s: float = 60) -> None:
        """Ship a state snapshot to failover.standby when one is due (replica.py): in a thread, one at
        a time, with a project of its own (the database connection stays in this thread). While the
        newest snapshot is stale, one keyed observation; closed once a fresh one ships. Nothing is read
        while failover.standby is unset. With failover.lease_remote set only the lease holder snapshots:
        another host's snapshots are the standby's, so this one's stale view raises nothing, and after
        taking the lease back staleness counts from then."""
        if self._replica_job is not None and not self._replica_job.is_alive():
            self._replica_job = None
        s = replica.settings(self.cfg)
        now = time.time()
        if not s:
            return
        if not self.lease_held():
            if self._replica_stale:
                scr.close_watcher_issues(self.p.db, replica.OBS_SOURCE, why="another host holds the project lease")
            self._replica_stale, self._replica_unheld = None, True
            return
        if self._replica_unheld:
            self._replica_unheld, self._replica_held_from, self._replica_checked = False, now, 0.0
        if now - self._replica_checked < check_every_s:
            return
        self._replica_checked = now
        st = replica.load_state(self.p)
        if self._replica_job is None and not st.get("since"):
            st["since"] = now   # when it was turned on: a snapshot that never ships goes stale from here
            replica._save_state(self.p, st)
        stale = replica.stale_text(self.p, self.cfg, now) if now - self._replica_held_from >= s["stale_s"] else ""
        if stale and not self._replica_stale:
            self.observe(replica.OBS_SOURCE, stale, "normal", key=replica.OBS_KEY, whole=True)
        elif not stale and self._replica_stale is not False:
            # an unchanged check (nothing new, standby still current) also lands here; it does not contact the standby
            scr.close_watcher_issues(self.p.db, replica.OBS_SOURCE, why="the standby holds the current state")
        self._replica_stale = bool(stale)
        if self._replica_job is not None:
            return
        mark = (self.p.db.one("SELECT COUNT(*) n FROM tasks WHERE status='done'") or {}).get("n")
        if not replica.due(st, s, now, mark):
            return
        cfg, base = copy.deepcopy(self.cfg), self.p.base

        def work() -> None:
            try:
                r = replica.snapshot(Project(base), cfg, mark=mark)
            except Exception:
                r = {"outcome": "failed", "error": traceback.format_exc().replace("\n", " | ")[-300:]}
            if r.get("outcome") in ("ok", "failed"):
                log(self.p, f"standby snapshot #{r.get('seq')}: {r['outcome']}"
                            + (f" ({r.get('size', 0) / 1e6:.1f} MB, kept {r.get('kept')})" if r["outcome"] == "ok"
                               else f": {r.get('error')}"))
        self._replica_job = threading.Thread(target=work, daemon=True, name="replica")
        self._replica_job.start()

    def _apply_backup(self, job: dict) -> None:
        """Record what a finished backup_branches pass did: drop pushed, skipped and refused branches
        from the queue (the last two with an observation), and schedule failed ones' next try."""
        db, now, remote = self.p.db, time.time(), job["remote"]
        with db.tx():
            pending = db.kv(KV_BACKUP) or {}
            for k, (outcome, detail) in job["out"].items():
                v = pending.get(k)
                if v is None or v.get("branch") != job["due"][k]["branch"]:
                    continue   # dropped or re-queued meanwhile
                b = v["branch"]
                if outcome == "failed" and v.get("tries", 0) + 1 < BACKUP_TRIES:
                    v["tries"] = v.get("tries", 0) + 1
                    v["next"] = now + BACKUP_RETRY_S * v["tries"]
                    log(self.p, f"task {k}: backup of {b} to {remote} failed (try {v['tries']}): {detail}")
                    continue
                del pending[k]
                if outcome == "pushed":
                    self._local_only_ok[b] = detail
                    log(self.p, f"task {k}: branch {b} backed up to {remote} at {detail[:10]}")
                    continue
                log(self.p, f"task {k}: branch {b} not backed up to {remote} ({outcome}): {detail}")
                if outcome == "gone":
                    continue
                why = {"not_ff": "it is not a fast-forward, so it was skipped and nothing was forced",
                       "refused": "the backup refused it",
                       "failed": f"the push failed {BACKUP_TRIES} times"}[outcome]
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, f"task:{k}", "observation", "normal",
                      f"task #{k}'s branch {b} was not backed up to {remote} (delivery.backup_remote): {why} "
                      f"({detail}).", "queued", int(k)))
            db.set_kv(KV_BACKUP, pending or None)
        self._local_only_due = 0.0   # what is still only here is flagged now

    def _note_dirty_main(self, task: dict) -> None:
        """At a hand-off, record one observation when the project's main checkout has uncommitted
        changes to tracked paths: work there is on no branch and no remote. Once per set of paths
        (KV_DIRTY_MAIN); a clean checkout resets it. Paths an open root-checkout alert names
        (check_root_checkout) are left out: that alert told the coordinator already. Nothing is
        committed or changed."""
        paths = worktree.dirty_tracked(self.p.root)
        if paths is None:
            return
        db = self.p.db
        key = hashlib.sha256("\0".join(paths).encode()).hexdigest()[:16] if paths else None
        if key == db.kv(KV_DIRTY_MAIN):
            return
        db.set_kv(KV_DIRTY_MAIN, key)
        told = {x for i in (db.kv(ROOT_CHECKOUT_KEY, {}) or {}).values() for x in i.get("new") or []}
        paths = [x for x in paths if x not in told]
        if not paths:
            return
        shown = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
        log(self.p, f"main checkout has uncommitted changes to {len(paths)} tracked path(s)")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "observation", "low",
              f"The project's main checkout has uncommitted changes to {len(paths)} tracked path"
              f"{'' if len(paths) == 1 else 's'}, seen at #{task['id']}'s hand-off: {shown}. They are on no "
              f"branch and no remote. Nothing was committed or changed.", "queued", task["id"]))

    def read_upstream(self) -> None:
        """With `upstream.ingest` on, new notes in the user's upstream inboxes become coordinator events
        (this machine's every minute, remote ones at most hourly); otherwise only notes addressed to
        this project (`ttp note --to`) in this machine's inbox are."""
        now = time.time()
        if now - self._upstream_checked < upstream.LOCAL_EVERY_S:
            return
        self._upstream_checked = now
        try:
            n = upstream.ingest(self.p, self.cfg, now)
        except (OSError, ValueError) as e:
            log(self.p, f"reading the upstream inbox failed: {type(e).__name__}: {e}")
            return
        if n:
            log(self.p, f"{n} new upstream note(s) for the coordinator")

    def wake_upstream_fixes(self) -> None:
        """On the first tick and every minute after: a resolved upstream note this project filed, whose fix
        the code this daemon loaded (its identity receipt) has, becomes one coordinator event and an early
        blocker sweep (upstream.wake_fixed), once per resolution across restarts. A note closed as wontfix
        (no release) wakes the same way and logs as closed."""
        now = time.time()
        if now - self._fixes_checked < upstream.LOCAL_EVERY_S:
            return
        self._fixes_checked = now
        try:
            got = upstream.wake_fixed(self.p, str(self.identity.get("version") or ""), now)
            for fp in upstream.follow_up_unconfirmed(self.p, str(self.identity.get("version") or ""), self.cfg, now):
                log(self.p, f"upstream note {fp} closed: confirmed by {'quiet' if (upstream.acked(fp) or {}).get('closed_by') == upstream.QUIET else 'silence'}")
        except (OSError, ValueError) as e:
            log(self.p, f"checking upstream fixes failed: {type(e).__name__}: {e}")
            return
        if got:
            log(self.p, "upstream note(s) resolved: " + ", ".join(
                f"{r['fp']} in {r['fixed_in']}" if r.get("fixed_in") else f"{r['fp']} closed ({r.get('kind') or 'resolved'})"
                for r in got))

    def drain_note_outbox(self) -> None:
        """File the notes for other projects that wait in this project's outbox (`ttp note --to` past the
        hourly limit), in order, as the limit allows. A note that has waited upstream.OUTBOX_STUCK_S
        becomes one coordinator event."""
        now = time.time()
        if now - self._outbox_drained < upstream.LOCAL_EVERY_S:
            return
        self._outbox_drained = now
        try:
            filed, stuck = upstream.drain_outbox(self.p.state, self.p.name, now)
        except (OSError, ValueError) as e:
            log(self.p, f"filing the note outbox failed: {type(e).__name__}: {e}")
            return
        if filed:
            log(self.p, f"{filed} waiting note(s) to other projects filed from the outbox")
        if stuck:
            left = upstream.outbox_pending(self.p.state)
            to = ", ".join(sorted({str(n["to"]) for n in stuck}))
            self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                        (now, "daemon", "observation", "low",
                         f"{len(stuck)} note(s) of this project's workers to {to} have waited over "
                         f"{upstream.OUTBOX_STUCK_S // 3600} h in its outbox: other projects get at most "
                         f"{upstream.NOTES_PER_HOUR} of its notes an hour and {left} wait. They are filed in "
                         f"order as the limit allows; a worker sending this many notes may be looping.",
                         "queued"))

    def forward_upstream(self) -> None:
        """Send this machine's new upstream notes on to the machines that cannot reach it (upstream.forward),
        in a thread: a slow or hung ssh never holds up the tick. One pass at a time per daemon; the
        daemons of this user's other projects skip a pass while one runs."""
        now = time.time()
        if now - self._forwarded < upstream.FORWARD_EVERY_S or (self._forwarder and self._forwarder.is_alive()):
            return
        self._forwarded = now

        def work():
            try:
                n = upstream.forward(now)
            except (OSError, ValueError) as e:
                log(self.p, f"sending upstream notes on failed: {type(e).__name__}: {e}")
                return
            if n:
                log(self.p, f"{n} upstream note(s) sent on to other machines")
        self._forwarder = threading.Thread(target=work, daemon=True, name="upstream-forward")
        self._forwarder.start()

    def check_resource_trouble(self, every_s: float = 60) -> None:
        """A resource whose tasks keep failing (machines.trouble) starts a coordinator turn once per
        episode, so it moves the open work on it to a healthy alternative instead of retrying on it.
        Waits alone never start one: a busy resource is not a broken one. The episode ends only once
        the resource has had no failure for 24 h, so a count hovering at the threshold does not
        start a new one each time; it is kept in the database across restarts."""
        now = time.time()
        if now - self._trouble_checked < every_s:
            return
        self._trouble_checked = now
        db = self.p.db
        try:
            seen = machines.stats(db, now)
            bad = machines.trouble(db, now, seen)
            known = machines.load()
        except Exception:
            log(self.p, "resource trouble check: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return
        told = db.kv("resource_trouble") or {}
        new = [n for n in sorted(bad) if n not in told and bad[n]["failures"] >= machines.TROUBLE_AT
               and bad[n].get("tasks")]
        with db.tx():
            for name in new:
                text = machines.trouble_line(name, bad[name], known, set(bad) | set(db.paused_resources()))
                db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                     (now, "daemon", "resource_trouble", "normal",
                      f"Resource {text}. Route around it: move its tasks to a healthy machine the charter "
                      f"allows (task_update `resources`), record the decision and notify the user; ask "
                      f"(blocking access) only if the charter allows no alternative."
                      if name in known else
                      f"Resource {text}. It is a lock, not a machine, so there is nothing to route around: "
                      f"find why its tasks fail and fix that.", "queued"))
            live = {n: told.get(n, now) for n in (*told, *new) if (seen.get(n) or {}).get("failures")}
            if live != told:
                db.set_kv("resource_trouble", live)

    def _disk_holds(self, task: dict) -> bool:
        """Under the disk guard, tasks that may check out, build or test code (all but questions and
        plans) wait in the queue, attempts untouched. Questions and plans still run, so the coordinator can look into it, but not when
        the disk is nearly full: that would corrupt state and fail runs half way."""
        if not self._disk_low:
            return False
        return task["kind"] not in DISK_LIGHT_KINDS or (self._disk_free or 0) < DISK_FLOOR_GB * 1e9

    def prune_leftovers(self, every_s: float = 3600) -> list[str]:
        """Hourly: delete recovered worktrees' leftovers folders (state/worktree-leftovers/) older than
        disk.worktree_leftovers_keep_days (worktree.prune_leftovers; 0 keeps them all)."""
        now = time.time()
        if now - self._last_leftovers < every_s:
            return []
        self._last_leftovers = now
        gone = worktree.prune_leftovers(self.p, self.cfg.get("disk", {}).get("worktree_leftovers_keep_days"), now)
        if gone:
            log(self.p, f"deleted {len(gone)} worktree leftovers folder(s) past disk.worktree_leftovers_keep_days: "
                        f"{', '.join(gone[:10])}")
        return gone

    def prune_worktrees(self, every_s: float = 300, force: bool = False) -> list[dict]:
        """Tidy finished tasks' worktrees (worktree.sweep) soon after they end: every `every_s`, and
        at once when a task finished since the last sweep. `disk.worktree_retention_days` delays
        removal (null, the default: no delay beyond worktree.held_by; 0 or less: never tidy, as
        before). A worktree kept for a reason is checked again when its task changes or after
        KEEP_RECHECK_S, or at once with `force` (the disk guard's sweep), except one whose sweep failed
        (`why` 'error: ...'), so a forced re-sweep does not retry it every tick. Branches are never deleted.
        Removals are cheap and all done at once; archiving (worktree.sweep's costly part) is done for
        at most one worktree per call, and the sweep runs again at the next tick (forced again after a
        forced one) until none is left deferred. Returns the sweep's results."""
        now = time.time()
        cfg = self.cfg.get("disk", {})
        days = cfg.get("worktree_retention_days")
        if days is not None and float(days) <= 0:
            return []
        latest = self.p.db.one("SELECT MAX(updated) m FROM tasks WHERE status IN (%s)"
                               % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)["m"] or 0
        more, self._prune_more = self._prune_more, None
        force = force or more == "force"
        if (now - self._last_prune < every_s and latest <= self._pruned_upto and not force and not more) \
                or not (self.p.worktrees.is_dir() or self.p.runs.is_dir()):
            return []
        self._last_prune, self._pruned_upto = now, latest
        days = float(days or 0)

        def recent(task: dict) -> bool:
            memo = self._kept.get(task["id"])
            return bool(memo) and memo[0] == task["updated"] and now - memo[1] < KEEP_RECHECK_S

        def failed(task: dict) -> bool:   # a forced sweep still skips a recent failure, or it retries it every tick
            memo = self._kept.get(task["id"])
            return recent(task) and str(memo[2] or "").startswith("error:")

        def run_skip(w: dict) -> bool:   # a run directory's kept worktree: the same rechecks, by path
            memo = self._run_kept.get(str(w["path"]))
            return bool(memo) and now - memo[0] < KEEP_RECHECK_S and (
                not force or str(memo[1] or "").startswith("error:"))
        db = self.p.db
        logged = db.kv(KV_WORKTREES_LOGGED) or {}      # task -> the keep reason last logged
        dirty = db.kv(KV_WORKTREES_DIRTY) or {}        # task -> its kept tracked changes
        results = worktree.sweep(self.p, older_than_s=days * 86400, names=cfg.get("cache_dirs"),
                                 skip=failed if force else recent, skip_run=run_skip,
                                 leftovers_max_mb=cfg.get("worktree_leftovers_max_mb"),
                                 archive_max_mb=cfg.get("worktree_archive_max_mb"),
                                 patch_tracked=force,   # the disk guard's sweep also frees tracked changes as patches
                                 # archiving is costly: one worktree per tick step, within half the
                                 # heartbeat's staleness, telling the watchdogs per file; the rest next tick
                                 archive_limit=1, deadline=now + HEARTBEAT_STALE_S / 2, progress=self._progress,
                                 reserve_bytes=self._archive_reserve())
        if any(r.get("deferred") for r in results):
            self._prune_more = "force" if force else "plain"
        for r in results:
            if "run" in r:
                self._run_swept(r, logged, now)
                continue
            key = str(r["task"])
            if r.get("unlocked") is not None:
                log(self.p, f"worktree {r['path']} of task {r['task']}: cleared its stale lock "
                            f"({r['unlocked'] or 'no reason'})" + (", half-made, removed" if r.get("half") else ""))
            if r.get("archived"):
                ar = r["archived"]
                log(self.p, f"worktree {r['path']} of task {r['task']}: archived {ar['files']} file(s) "
                            f"({ar['bytes'] / 1e6:.1f} MB) to {ar['dir']}"
                            + (f"; dropped {ar['dropped']} over the cap ({ar['dropped_bytes'] / 1e6:.1f} MB), "
                               f"listed in its manifest" if ar["dropped"] else ""))
            if r["cleared"] and r["why"] is not None:   # a removed worktree's caches go with it: its line says enough
                log(self.p, f"worktree {r['path']} of task {r['task']}: removed {', '.join(r['cleared'][:10])}")
            if r.get("moved"):
                mv = r["moved"]
                log(self.p, f"worktree {r['path']} of task {r['task']}: moved {mv['files']} untracked file(s) "
                            f"({mv['bytes'] / 1e6:.1f} MB) to {mv['to']}")
            if r.get("deferred"):   # archived by a later tick: no log, checked again at the next sweep
                self._kept[r["task"]] = (r["updated"], 0, r["why"])
                continue
            if r.get("tracked"):
                dirty[key] = self._uncommitted(r, dirty.get(key))
            elif not r.get("held"):
                dirty.pop(key, None)
            if r["why"] is None:
                self._kept.pop(r["task"], None)
                logged.pop(key, None)
                log(self.p, f"worktree {r['path']} of task {r['task']} ({r['status']}) removed; "
                            f"branch {r['branch'] or '?'} kept")
            else:
                if worktree.keep_key(logged.get(key)) != worktree.keep_key(r["why"]):   # once per reason and holder, across restarts too
                    logged[key] = r["why"]
                    log(self.p, f"worktree {r['path']} of task {r['task']} kept: {r['why']}")
                # Held (see worktree.held_by): checked again every sweep (no git work), so it goes soon after the hold ends.
                self._kept[r["task"]] = (r["updated"], 0 if r.get("held") else now, r["why"])
        kept = {str(t): why for t, (_, _, why) in sorted(self._kept.items())
                if (self.p.worktrees / f"t{t}").exists()}
        if kept != (db.kv("worktrees_kept") or {}):
            db.set_kv("worktrees_kept", kept or None)
        for name, memo in ((KV_WORKTREES_LOGGED, logged), (KV_WORKTREES_DIRTY, dirty)):
            memo = {k: v for k, v in memo.items() if (self.p.worktrees / f"t{k}").exists()
                    or (k.startswith("/") and Path(k).is_dir())}
            if memo != (db.kv(name) or {}):
                db.set_kv(name, memo or None)
        return results

    def _run_swept(self, r: dict, logged: dict, now: float) -> None:
        """Log one sweep result for a worktree inside a run directory (worktree.run_worktrees) or a stray
        one elsewhere under the project root (worktree.stray_worktrees) and
        remember a kept one: rechecked after KEEP_RECHECK_S, a held one every sweep. `logged` (path ->
        the keep reason last logged) gives each reason one log line."""
        key, what = r["path"], (f"stray worktree {r['path']} (task {r['task']})" if r.get("stray") else
                                f"worktree {r['path']} of run {r['run'] or '?'}" + (f" (task {r['task']})" if r["task"] else ""))
        if r.get("archived"):
            ar = r["archived"]
            log(self.p, f"{what}: archived {ar['files']} file(s) ({ar['bytes'] / 1e6:.1f} MB) to {ar['dir']}")
        if r.get("moved"):
            log(self.p, f"{what}: moved {r['moved']['files']} untracked file(s) to {r['moved']['to']}")
        if r.get("deferred"):
            self._run_kept[key] = (0, r["why"])
        elif r["why"] is None:
            self._run_kept.pop(key, None)
            logged.pop(key, None)
            log(self.p, f"{what} removed; branch {r['branch'] or '?'} kept")
        else:
            if worktree.keep_key(logged.get(key)) != worktree.keep_key(r["why"]):
                logged[key] = r["why"]
                log(self.p, f"{what} kept: {r['why']}")
            self._run_kept[key] = (0 if r.get("held") else now, r["why"])

    def _uncommitted(self, r: dict, prev: dict | None) -> dict:
        """A finished task's worktree holds modified tracked files: raise it to the coordinator once
        per worktree and content (fingerprint); the worktree stays. Returns its KV_WORKTREES_DIRTY entry."""
        db, now = self.p.db, time.time()
        fp = f"{worktree.DIRTY_EVENT}:{r['task']}:{r['fingerprint']}"
        paths = r["tracked"]
        if not db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            task = db.task(r["task"]) or {}
            shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", worktree.DIRTY_EVENT, fp, "normal",
                  f"#{r['task']} ({r['status']}) {(task.get('title') or '')[:80]} left uncommitted changes to tracked "
                  f"files in its worktree {r['path']}: {shown}. The worktree is kept; it is never removed while "
                  f"they are there. Decide: carry them on in a code task that continues #{r['task']} (commit them "
                  f"on its branch {r['branch'] or '?'}), or discard them if they are not needed.", "queued", r["task"]))
            log(self.p, f"worktree {r['path']} of task {r['task']}: uncommitted changes to {len(paths)} tracked "
                        f"file(s); raised to the coordinator")
        return {"status": r["status"], "paths": paths[:5], "count": len(paths), "fingerprint": r["fingerprint"],
                "since": (prev or {}).get("since") if (prev or {}).get("fingerprint") == r["fingerprint"] else now}

    def sweep_holds(self) -> None:
        """Model-free: requeue a hold whose anchor is over (anchors.py): its ask resolved or expired,
        its until passed, its resource free and not paused, or its when probe exited 0 (run like a
        start_when; a broken one is raised once). Then raise the holds with no anchor that went stale,
        once per set with a new member."""
        db, now = self.p.db, time.time()
        rows = db.q("SELECT * FROM tasks WHERE status='blocked' AND labels LIKE '%\"waits:%'")
        self._sweep_setting_holds(rows, now)
        for t in rows:
            got = anchors.anchor(t)
            if not got or settinghold.held_key(t):
                continue
            kind, value = got
            over = ""
            if kind == "ask" and value.isdigit():
                if not db.one("SELECT id FROM messages WHERE id=? AND kind='ask' AND handled=0", (int(value),)):
                    over = f"ask #{value} was resolved or expired"
            elif kind == "until":
                try:
                    over = "its until passed" if float(value) <= now else ""
                except ValueError:
                    over = ""
            elif kind == "resource":
                over = f"{value} is free" if self._resource_ready(value) else ""
            elif kind == "when":
                over = self._hold_probe(t, value, now)
            if over:
                self._release_hold(t, over, now)
        with db.tx():
            rows = anchors.stale(db, self.cfg, now, anchors.track(db, now))
            ids = sorted(r["task"] for r in rows)
            seen = [int(x) for x in db.kv(anchors.STALE_KEY, []) or []]
            if ids == seen:
                return
            db.set_kv(anchors.STALE_KEY, ids)
            if not set(ids) - set(seen):
                return   # only fewer: nothing new to decide
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, "daemon", anchors.STALE_EVENT, "normal",
                  anchors.event_text(rows) + holdloops.summary(db), "queued"))
        log(self.p, f"stale holds raised to the coordinator: {', '.join(f'#{i}' for i in ids)}")

    def _setting_hold(self, task: dict, holds: dict[str, int], now: float) -> bool:
        """Hold a queued task that a user-only setting being asked about would refuse (settinghold):
        blocked, attempts untouched, anchored on the ask; sweep_holds releases it."""
        key = settinghold.refused_by(task, holds, self.cfg)
        if not key:
            return False
        db = self.p.db
        labels = anchors.without(json.loads(task["labels"] or "[]")) + [anchors.label("ask", str(holds[key]))]
        with db.tx():
            if (db.task(task["id"]) or {}).get("status") != "queued":
                return True
            db.update_task(task["id"], status="blocked", labels=labels,
                           blocked_reason=settinghold.reason(key, holds[key], self.cfg))
        log(self.p, f"task {task['id']} held: {key} is off and ask #{holds[key]} about it is open")
        return True

    def _sweep_setting_holds(self, rows: list[dict], now: float) -> None:
        """Release every task held on a setting together once the setting is on or no open ask names
        it; one still held moves its anchor to the newest open ask about it. A blocked task with no
        anchor that a worker handed off naming a held setting joins that hold first. Then raise the
        setup question (settinghold.setup_question) when its inputs changed."""
        self._setting_setup_question(now)
        held = [(t, settinghold.held_key(t)) for t in rows]
        held = [(t, k) for t, k in held if k]
        db = self.p.db
        names = sorted({k.rpartition(".")[2] for k in settinghold.SETTINGS})
        loose = db.q("SELECT * FROM tasks WHERE status='blocked' AND labels NOT LIKE '%\"waits:%' AND ("
                     + " OR ".join("blocked_reason LIKE ? OR result LIKE ?" for _ in names) + ")",
                     [f"%{n}%" for n in names for _ in (0, 1)])
        if not held and not loose:
            return
        holds = settinghold.active(db, self.cfg, self.p.root)
        for t in loose if holds else []:
            res = load_result(t["result"])
            key = settinghold.anchor_blocked(t, holds, str(res.get("question") or res.get("summary") or ""))
            if not key:
                continue
            labels = anchors.without(json.loads(t["labels"] or "[]")) + [anchors.label("ask", str(holds[key]))]
            with db.tx():
                cur = db.task(t["id"]) or {}
                if cur.get("status") != "blocked" or anchors.anchor(cur):
                    continue
                db.update_task(t["id"], labels=labels,
                               blocked_reason=settinghold.handed_off_reason(key, holds[key], self.cfg,
                                                                            t["blocked_reason"]))
            log(self.p, f"task {t['id']} held with the rest: handed off on {key}, ask #{holds[key]} is open")
        for t, key in held:
            why = settinghold.over(self.cfg, key, holds, self.p.root)
            if why:
                self._release_hold(t, why, now)
            elif anchors.anchor(t) != ("ask", str(holds[key])):
                labels = anchors.without(json.loads(t["labels"] or "[]")) + [anchors.label("ask", str(holds[key]))]
                self.p.db.update_task(t["id"], labels=labels,
                                      blocked_reason=settinghold.reason(key, holds[key], self.cfg))

    def _setting_setup_question(self, now: float) -> None:
        """grants.reconcile, then settinghold.setup_question, run only when the charter or the delivery
        settings changed since the last check (their git check stays off the tick): a grant the user's
        word on record covers is applied first, so it is never asked about; the kv makes the question
        once per state, keyed events make each defect once per state."""
        try:
            state = (self.p.charter_path.stat().st_mtime_ns, json.dumps(self.cfg.get("delivery") or {}, sort_keys=True))
        except OSError:
            return
        if getattr(self, "_setup_state", None) == state:
            return
        self._setup_state = state
        try:
            applied = grants.reconcile(self.p, self.p.db, self.cfg, now, self.p.root)
        except Exception as e:   # a broken check never stops the tick; the setup question still runs
            applied = []
            log(self.p, f"charter grant check failed: {e}")
        if applied:
            self.cfg = self.p.config()
            self._setup_state = (state[0], json.dumps(self.cfg.get("delivery") or {}, sort_keys=True))
            for line in applied:
                log(self.p, "charter grant applied: " + line[:300])
        if settinghold.setup_question(self.p.db, self.cfg, self.p.charter_path.read_text(), now, self.p.root):
            log(self.p, "setup question asked of the user: " + ", ".join(settinghold.SETTINGS))

    def _hold_probe(self, task: dict, probe: str, now: float) -> str:
        """A `when:` hold's probe, run like a start_when: why it is over once it exited 0, or once a
        `ttp detach --check` exited detach.FAILED_RC (the jobs ended, one failed: the why says so), else ''."""
        tid = task["id"]
        self._drop_stale_probe(tid, probe)
        got = self._probe_rc.get(tid)
        if got and got[2] == probe and tid not in self._probes:
            if got[0] == 0:
                self._probe_rc.pop(tid, None)
                return "its when probe passed"
            if detach.failed_check(probe, got[0]):   # the jobs it waits on ended: over, and the why says one failed
                self._probe_rc.pop(tid, None)
                return (f"the detached job its when probe waits on ended but failed or left no exit code "
                        f"({detach.failed_why(probe, got[0], self.p.root)}): {probe[:300]}")
            if got[0] not in NOT_YET_RCS:
                fp = f"hold_probe_broken:{tid}:{hashlib.sha256(probe.encode()).hexdigest()[:12]}"
                if not self.p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                    why = got[0] if isinstance(got[0], str) else f"exit {got[0]}"
                    self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (now, "daemon", "hold_probe_broken", fp, "normal",
                                 f"#{tid} {task['title']} is held until its probe passes, but the probe is broken "
                                 f"({why}; only 0 = over and 1, 75 or 255 = not yet are valid): {probe[:300]}. It "
                                 f"keeps running; decide: task_update waits_on with a fixed probe, requeue the "
                                 f"task, or cancel it.", "queued", tid))
                    log(self.p, f"task {tid} hold_probe_broken ({why}); raised to the coordinator")
        if tid not in self._probes and now - self._probed.get(tid, 0) >= PROBE_EVERY_S:
            self._start_probe(tid, probe, now, "waits_on when")
        return ""

    def _resource_ready(self, name: str) -> bool:
        """Not paused (by any name of the same device) and its lock free, nobody queued for it."""
        lock = locks.canonical(self.cfg, name)
        if lock in {locks.canonical(self.cfg, r) for r in self.p.db.paused_resources()}:
            return False
        return locks.probe(shared.locks_dir(self.p, lock, self.cfg), lock, self._slot_paths(name))

    def _release_hold(self, task: dict, why: str, now: float) -> None:
        db = self.p.db
        prev = load_result(task["result"])
        upd: dict = {"status": "queued", "blocked_reason": None,
                     "labels": anchors.without(json.loads(task["labels"] or "[]"))}
        if "waiting_since" in prev:
            prev.pop("waiting_since")
            upd["result"] = dump_result(prev)
        with db.tx():
            if (db.task(task["id"]) or {}).get("status") != "blocked":
                return
            db.update_task(task["id"], **upd)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", "hold_released", "low", f"#{task['id']} {task['title']} queued again: {why}",
                  "handled", task["id"]))
        log(self.p, f"task {task['id']} hold released: {why}")

    def refresh_ref_waits(self) -> None:
        """Model-free (refwaits.py): a queued task whose wait note names an ask now resolved or a task
        now done gets a note that says so, its cached probe verdicts dropped and its probe (retry_when
        or start_when) rerun at once by probe_waiting, which runs next. Its probe, dependencies, start_when,
        start_after, not_before and attempts stay; each piece of evidence counts once per wait.
        Known gap, on purpose: a wait with no probe only gets its note refreshed and wakes at its own
        retry, never early (evidence from before the wait would wake it after every hand-off). A bare
        `#N` that is really a PR or issue may be read as a task: that only rewrites text or reruns a
        model-free probe."""
        db, now = self.p.db, time.time()
        rows = db.q("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NOT NULL "
                    "OR labels LIKE '%\"start_when:%')")
        seen = db.kv(refwaits.SEEN_KEY, {}) or {}
        keep: dict = {}
        for t in rows:
            prev = load_result(t["result"])
            d = deferral(t)
            waiting = prev.get("status") == "waiting"
            if not d.get("when") and not waiting:
                continue
            ep = d.get("since") if d.get("when") else prev.get("waiting_since")
            ep = f"{float(ep):.0f}" if isinstance(ep, (int, float)) else f"u{float(t['created'] or 0):.0f}"
            text = " ".join(str(x or "") for x in (prev.get("waiting_for") if waiting else "",
                                                   d.get("why"), t["blocked_reason"]))
            named = refwaits.refs(text, own=t["id"])
            if not named:
                continue
            had = seen.get(str(t["id"])) or {}
            done = list(had.get("seen") or []) if had.get("ep") == ep else []
            keep[str(t["id"])] = {"ep": ep, "seen": done}
            new = [(k, fact) for k, fact in refwaits.over(db, named) if k not in done]
            if not new:
                continue
            keep[str(t["id"])]["seen"] = done + [k for k, _ in new]
            self._refresh_ref_wait(t, prev, d, [f for _, f in new], now)
        if keep != seen:
            db.set_kv(refwaits.SEEN_KEY, keep)

    def _refresh_ref_wait(self, t: dict, prev: dict, d: dict, facts: list[str], now: float) -> None:
        tid, db = t["id"], self.p.db
        probe = d.get("when") or prev.get("retry_when")
        probe = probe if isinstance(probe, str) and probe.strip() else ""
        later = (d.get("after") or 0) > now
        upd: dict = {}
        if probe:
            # Its cached verdicts were for before the evidence: drop them (a 0 is acted on at once, so
            # only not-yet and broken ones are cached), and let probe_waiting run it now.
            self._probe_rc.pop(tid, None)
            self._start_rc.pop(tid, None)
            for other, got in list(self._probe_rc.items()):
                if got[2] == probe and got[0] != 0:
                    del self._probe_rc[other]
            self._probed.pop(tid, None)
            then = "its probe runs again after its start_after" if later else "its probe runs again now"
        else:   # no probe: the note only, never an early wake (see refresh_ref_waits)
            then = "it keeps its own window" if (t["not_before"] or 0) > now else "it runs at its turn"
        if prev.get("status") == "waiting":
            facts = [f for f in prev.get(refwaits.FACTS_KEY) or [] if f not in facts] + facts
            upd["result"] = dump_result({**prev, refwaits.FACTS_KEY: facts})
        what = str((prev.get("waiting_for") if prev.get("status") == "waiting" else None)
                   or d.get("why") or prev.get("summary") or "its start condition")[:300]
        on_locks = prev.get("status") == "waiting" and locks.lock_probe_parts(str(prev.get("retry_when") or ""))[0]
        if on_locks:   # who holds each lock now, never the done tasks its hand-off named as holders
            what = self._wait_what(tid, prev)
        if not str(t["blocked_reason"] or "").startswith(refwaits.HELD_NOTES):
            upd["blocked_reason"] = (f"waiting for {what}; {then}" if on_locks else
                                     f"waiting for {what}; {'; '.join(facts)}; {then}")[:500]
        with db.tx():
            cur = db.task(tid) or {}
            if cur.get("status") != "queued" or cur.get("result") != t["result"]:
                return   # it moved meanwhile: the next tick looks again
            db.update_task(tid, **upd)
        log(self.p, f"task {tid} wait note refreshed: {'; '.join(facts)}; {then}")

    def probe_waiting(self) -> None:
        """A waiting task may name a shell probe (`retry_when`) for the thing it waits on. The probe
        runs here, model-free and in the background. Exit 0 makes the task due at once. When its
        `retry_after_s` timer runs out while the probe still says "not yet", the task sleeps another
        `retry_after_s` instead of spending a worker run to find that out. "Not yet" is any exit in
        NOT_YET_RCS: 1, 75 (a busy `ttp lock`) and 255 (ssh could not reach the host a remote marker
        lives on; only when the host named really does not answer, _own_255). A broken probe
        (any other exit, a timeout, a probe that cannot start) wakes it at its timer so a worker can
        fix the probe. Past `waiting.max_hold_s` after the hand-off a "not yet" keeps it asleep too (a
        model run cannot make the probe pass) and raises one `wait_stale` event for the coordinator.
        Tasks with the same probe share its runs."""
        db, now = self.p.db, time.time()
        self._poll_on_pass(now)
        self._sweep_on_pass_notes(now)
        outs: dict[int, str] = {}   # one read of a probe run's output, whichever tasks share it
        lines: dict[int, str] = {}   # and its last line (waitheal.last_line)
        done = []
        for tid, (proc, started, probe) in list(self._probes.items()):
            rc = proc.poll()
            if rc is None and now - started < PROBE_TIMEOUT_S:
                continue
            del self._probes[tid]
            if rc is None:
                _kill_group(proc)
            if id(proc) not in outs:
                outs[id(proc)], lines[id(proc)] = self._probe_texts(proc)
            done.append((tid, proc, probe, rc))
        # Every 255's hosts at once: in an outage the tick waits out one ssh timeout, not one per probe.
        self._check_hosts([h for _, proc, probe, rc in done if rc == netwait.UNREACHABLE_RC
                           for h in netwait.hosts(probe, outs[id(proc)])[:netwait.HOSTS_MAX]], now)
        for tid, proc, probe, rc in done:
            task = db.task(tid)
            if task and _current_probe(task) != probe:
                continue   # re-pointed (`set-when`) while it ran: its verdict is not the new probe's
            self._note_probe_out(tid, probe, outs[id(proc)], now, lines[id(proc)])
            runner = waitheal.devq_runner(probe) if rc == waitheal.DEVQ_RUNNER_DOWN_RC else None
            if runner:
                # Its device runner is down: the daemon brings it back, and the task sleeps on.
                self._restart_runner(runner, tid, now)
                rc = 1
            gone = worktree.missing_cd_worktree(probe, self.p) if rc not in (0, None) else None
            if gone:   # it cds into a pruned worktree: it can never pass, so it is broken, not "not yet"
                rc = f"its cd target {gone} is gone (a removed task worktree)"
            if rc == netwait.UNREACHABLE_RC:
                rc = self._own_255(tid, probe, outs[id(proc)], now)
            if task and task["status"] == "queued" and deferral(task).get("when"):
                self._start_verdict(task, "timeout" if rc is None else rc, now)
                continue
            self._probe_rc[tid] = ("timeout" if rc is None else rc, now, probe)
            if task and task["status"] == "queued":
                self._flag_never_passes(task, probe, rc, outs[id(proc)])
            gated = self._gate_answer(task, probe, self._probe_rc[tid][0], outs[id(proc)], now) if task else None
            if gated == "same":
                continue   # asleep after a wake-gate skip on this very answer: the gate judged it
            if rc == 0 or detach.failed_check(probe, rc) or _push_not_landed(probe, rc):
                task = db.task(tid)
                if task and task["status"] == "queued" and (task["not_before"] or 0) > now:
                    (self._probe_passed if rc == 0 else self._job_failed if detach.failed_check(probe, rc)
                     else self._push_finished_unpushed)(task, now)
            elif gone and task and task["status"] == "queued" and (task["not_before"] or 0) > now:
                self._wake_waiting(task, f"probe broken: {rc}", now)   # no timer can make it pass
            elif gated == "changed" and rc is not None and rc not in NOT_YET_RCS:
                # A new answer after a skip that no branch above handles: wake now, not at the backoff.
                self._wake_waiting(task, f"its probe's answer changed (exit {rc})", now)
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
            prev = load_result(t["result"])
            probe = prev.get("retry_when")
            if prev.get("status") != "waiting" or not isinstance(probe, str) or not probe.strip() \
                    or deferral(t).get("when"):
                continue
            if t["not_before"] > now and self._spec_wake(t, prev, probe, now):
                continue
            self._drop_stale_probe(t["id"], probe)
            if t["not_before"] <= now:
                # Hand-offs from before the hold, and tasks already woken, keep their timer.
                if prev.get("woke") or not isinstance(prev.get("waiting_since"), (int, float)):
                    continue
                if not self._hold_waiting(t, prev, now):
                    continue
            elif t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S:
                continue
            self._start_probe(t["id"], probe, now)
            got = self._probe_rc.get(t["id"])
            if t["id"] not in self._probes and got and got[2] == probe and (t["not_before"] or 0) > now \
                    and (got[0] == 0 or detach.failed_check(probe, got[0])) \
                    and self._gate_answer(t, probe, got[0], (self._probe_out.get(t["id"]) or ("",))[0], now) != "same":
                (self._probe_passed if got[0] == 0 else self._job_failed)(db.task(t["id"]) or t, now)
        self.probe_deferred(now)

    def probe_deferred(self, now: float) -> None:
        """A task added with `start_when` stays queued, undispatched, until its probe exits 0; the
        probe runs here like a waiting task's, once any `start_after` has passed. Exit 1 means not
        yet, and so do 75 (EX_TEMPFAIL, e.g. a busy `ttp lock`) and 255 (ssh could not reach the
        host: a reboot or a network blip; a 255 whose host answers is broken, _own_255). A broken
        probe (another exit, a timeout, a probe that cannot start) is raised to the
        coordinator once, never as a worker run, and keeps being tried. So is a deferral still not
        met after `coordinator.defer_max_days`."""
        db = self.p.db
        max_days = float(self.cfg["coordinator"].get("defer_max_days") or 14)
        deferred = db.q("SELECT * FROM tasks WHERE status='queued' AND labels LIKE '%\"start_when:%'")
        ids = {t["id"] for t in deferred}
        for tid in [k for k in self._start_rc if k not in ids]:
            del self._start_rc[tid]   # started, cancelled or no longer deferred
        startgate.forget(db, keep=ids)
        for t in deferred:
            d = deferral(t)
            if not d.get("when") or (t["not_before"] or 0) > now:
                continue
            since = d.get("since") or t["created"]
            if now - since >= max_days * 86400:
                self._deferral_event(t, since, "deferral_expired",
                                     f"its start_when has not passed in {max_days:g} days: {d['when'][:300]}")
            self._drop_stale_probe(t["id"], d["when"])
            if t["id"] in self._probes or now - self._probed.get(t["id"], 0) < PROBE_EVERY_S:
                continue
            if landed.missing_task_refs(db, d["when"]):
                continue   # it can never pass: _raise_missing_task_refs raised it, once
            self._start_probe(t["id"], d["when"], now, "start_when")
            if t["id"] not in self._probes:
                self._start_verdict(t, self._probe_rc.pop(t["id"])[0], now)
        self._dead_landed_waits(deferred, now)
        self.release_review_holds(now)

    def _dead_landed_waits(self, deferred: list[dict], now: float) -> None:
        """deadwait.check over the queued tasks whose start_when is exactly `landed:#<id>`: one event
        per producer nothing can land. Saved task rows only, never the in-memory probe verdicts
        (_start_rc), which are empty after a restart: a wait in a longer probe is not judged."""
        held: dict[int, list[dict]] = {}
        # Where no landing can be checked the wait is on the producer being done (landed.DONE_ENV):
        # nothing is held on a landing, and an earlier event clears.
        for t in deferred if landed.can_check(self.p) else []:
            m = landed.LANDED_PROBE.fullmatch(deferral(t).get("when") or "")
            if m:
                held.setdefault(int(m.group(1)), []).append(t)
        try:
            deadwait.check(self.p.db, self.cfg, held, now)
        except Exception:
            log(self.p, "dead landed waits: " + traceback.format_exc().replace("\n", " | ")[:1000])

    def release_review_holds(self, now: float) -> None:
        """Model-free: a review held for a carried change's open fix chain (reviewhold) is released
        once its start_when passed (the fix landed), once no such chain is open (one that ended failed
        or cancelled gets a spec line saying so) or at review.carried_hold_max_h hours. Its line on
        inherited commits is refreshed from the head's commits now (pushq.carried)."""
        db = self.p.db
        max_h = float((self.cfg.get("review") or {}).get("carried_hold_max_h", reviewhold.MAX_H) or 0)
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND kind='review' "
                      "AND labels LIKE '%\"" + reviewhold.LABEL + "%'"):
            got = reviewhold.verdict(db, t, now, max_h)
            if got is None:
                continue
            why, line = got
            head, _ = reviewhold.hold_of(t) or ("", [])
            code = next((c for i in dependency_ids(t) if i is not None and (c := db.task(i))), None)
            try:
                carried = pushq.carried(self.p, code, head, self.cfg) if code and head else {}
            except Exception as e:
                log(self.p, f"task {t['id']}: carried commits not read on release: {e}")
                carried = {}
            db.update_task(t["id"], labels=reviewhold.released_labels(t), not_before=None,
                           spec=reviewhold.respec(t["spec"] or "", inherited_line(carried) if carried else "", line))
            log(self.p, f"task {t['id']} review hold released ({why})")

    def _own_255(self, tid: int, probe: str, out: str, now: float) -> int | str:
        """A probe's 255 means "host unreachable" only when it is ssh's own: each host it names is
        checked (netwait.confirm). One that names none, or whose host answers, failed its own remote
        command: why it is broken, which takes the broken-probe path, never the network outage."""
        why = netwait.confirm(probe, out, lambda h: self._host_answers(h, now))
        if why:
            log(self.p, f"task {tid} probe is broken, not waiting on an unreachable host: {why}")
        return why or netwait.UNREACHABLE_RC

    def _host_answers(self, host: str, now: float) -> bool:
        """ssh reaches `host`, asked at most once a minute for all the probes that name it."""
        self._check_hosts([host], now)
        return self._host_up[host][0]

    def _check_hosts(self, hosts: list[str], now: float) -> None:
        """Asks ssh about each host not asked in the last minute, all in one parallel pass."""
        stale = [h for h in dict.fromkeys(hosts) if now - self._host_up.get(h, (False, -1e18))[1] >= 60]
        if stale:
            env = self._probe_env()
            self._host_up.update((h, (ok, now)) for h, ok in netwait.reach(stale, env).items())

    def _drop_stale_probe(self, tid: int, probe: str) -> None:
        """The task's probe was re-pointed (`ttp task set-when`): kill a run of the old one and forget
        its verdict, so it is never credited to the new probe."""
        run = self._probes.get(tid)
        if run and run[2] != probe:
            if not any(o != tid and r[0] is run[0] for o, r in self._probes.items()):
                _kill_group(run[0])   # unless shared: the other task still needs its verdict
                self._probe_text(run[0])
            del self._probes[tid]
            self._probed.pop(tid, None)
        if tid in self._probe_rc and self._probe_rc[tid][2] != probe:
            del self._probe_rc[tid]
            self._probed.pop(tid, None)
        if tid in self._start_rc and self._start_rc[tid][2] != probe:
            del self._start_rc[tid]
            startgate.forget(self.p.db, tid=tid)

    def _start_verdict(self, task: dict, rc, now: float) -> None:
        d = deferral(task)
        if rc == 0:
            self._probe_rc.pop(task["id"], None)
            self._start_rc.pop(task["id"], None)
            startgate.forget(self.p.db, tid=task["id"])
            progress.forget_probe(self.p.db, task["id"])
            self.p.db.update_task(task["id"], labels=without_deferral(json.loads(task["labels"] or "[]")),
                                  not_before=None)
            log(self.p, f"task {task['id']} start_when passed; ready to start")
        else:
            # Kept apart from _probe_rc, whose verdicts tasks on the same probe take and pop: the
            # network outage check reads it.
            self._start_rc[task["id"]] = (rc, now, d.get("when") or "")
            got = self._probe_out.get(task["id"])
            out = got[0] if got and got[2] == d.get("when") else ""
            startgate.record(self.p.db, task["id"], d.get("when") or "", rc,
                             startgate.reason(self.p.db, d.get("when") or "", rc, out), now)
            if rc in NOT_YET_RCS:
                progress.note_probe(self.p.db, task["id"], d.get("when") or "", rc,
                                    startgate.reason(self.p.db, d.get("when") or "", rc, out), now)
        if rc in NOT_YET_RCS:
            self._start_resource_stale(task, d, now)
        elif detach.failed_check(d.get("when") or "", rc):
            self._deferral_event(task, d.get("since") or task["created"], "deferral_job_failed",
                                 f"a detached job its start_when waits on failed or left no exit code (exit {rc} of "
                                 f"`ttp detach --check`, not a broken probe): {d.get('when', '')[:300]}")
        elif rc != 0:
            said = (out or "").strip().splitlines()   # its last line says why (landed: an abandoned producer)
            why = (rc if isinstance(rc, str) else f"exit {rc}") + (f": {said[-1][:300]}" if said else "")
            self._deferral_event(task, d.get("since") or task["created"], "deferral_probe_broken",
                                 f"its start_when probe is broken ({why}; only 0 = start, 1 or 75 = not yet and "
                                 f"ssh's own 255 = host unreachable are valid): {d.get('when', '')[:300]}")

    def _start_resource_stale(self, task: dict, d: dict, now: float) -> None:
        """A start_when that has said not yet for waiting.start_resource_stale_s while the task names a
        device or serving resource raises one event per deferral: the resource may be down, and a
        probe that waits on it would hide that."""
        stale = float((self.cfg.get("waiting") or {}).get("start_resource_stale_s", waitheal.START_STALE_S) or 0)
        since = float(d.get("since") or task["created"] or now)
        if not stale or now - since < stale:
            return
        watched = waitheal.watched_resources(self.cfg, coord.task_resources(task))
        if watched:
            got = self._probe_out.get(task["id"])
            last = got[0] if got and got[2] == d.get("when") else ""
            self._deferral_event(task, since, "start_resource_stale",
                                 f"its start_when has said not yet for {(now - since) / 3600:.1f} h while it needs "
                                 f"{', '.join(watched)}: check that resource is up. Blocking gate: "
                                 f"{str(d.get('when'))[:200]}; last output: {last or '(none seen)'}")

    def _wait_started(self, task: dict, result: dict) -> None:
        """When a wait starts, a probe that can never pass is flagged now, not hours later."""
        probe = str(result.get("retry_when") or "")
        if probe:
            self._flag_never_passes(task, probe, None, "")
            if waitheal.end_marker_only(probe):
                log(self.p, f"task {task['id']} retry_when waits over ssh only for an end marker: a dead driver "
                            f"never writes it (`ttp detach --check --host ... --rows` also sees that)")
            if waitheal.header_grep(probe):
                log(self.p, f"task {task['id']} retry_when {waitheal.header_grep(probe)}")

    def _deferral_event(self, task: dict, since: float, kind: str, what: str) -> None:
        """Once per deferral: the coordinator decides again (fix the probe with task_update
        start_when, start it with start_when "", or cancel). The task stays deferred meanwhile."""
        db = self.p.db
        fp = f"{kind}:{task['id']}:{since:.0f}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "daemon", kind, fp, "normal",
              f"#{task['id']} {task['title']} waits to start, but {what}. The probe keeps running; decide: fix "
              f"it with task_update start_when, start the task now with start_when \"\", or cancel it.",
              "queued", task["id"]))
        log(self.p, f"task {task['id']} {kind}; raised to the coordinator")

    def alert_long_pause(self) -> None:
        """Model-free, run while paused: a pause found with no record is recorded as by unknown, and one
        lasting longer than budget.paused_alert_s raises one normal alert per pause (projpause), reminded
        at most once a day and cleared once the project resumes."""
        db = self.p.db
        if not db.kv("paused", False):
            return
        if projpause.adopt_unknown(db):
            log(self.p, "project found paused with no record of who paused it: recorded as by unknown")
        due = projpause.alert_due(db, self.cfg)
        if due:
            key, info = due
            self.alert(key, f"Project {self.p.name} has been {projpause.describe(info, self.p)}. No new model "
                            f"runs start until it resumes: `ttp resume {self.p.name}` or the web app.",
                       severity="normal")

    def lift_boot_pause(self) -> None:
        """A `ttp pause --until-reboot` ends on the first tick on another boot than the one it recorded
        (schedules and probes do not run while paused, so nothing else would lift it). A plain pause stays."""
        db = self.p.db
        rec = db.kv(PAUSE_BOOT_KEY)
        if not rec:
            return
        if not db.kv("paused", False):
            db.x("DELETE FROM kv WHERE key=?", (PAUSE_BOOT_KEY,))
            return
        if self.boot == "unknown" or self._this_boot(rec.get("boot")):
            return
        db.set_paused(False, by="daemon", via="host reboot", reason="the pause was set until the host reboots")
        since = timefmt.long(float(rec.get("since") or 0), self.p)
        log(self.p, f"pause lifted: set until reboot at {since}, now on boot {self.boot}")
        db.post("out", f"The project pause set until the host reboots ({since}) was lifted: the host has booted since. "
                       "New runs start again.", kind="info", severity="low", ref=f"pause-lifted:{self.boot}")

    def wake_after_reboot(self) -> None:
        """Once per daemon start: a waiting task that handed off before this host booted is due now,
        past its timer and its probe, only when what it waits on is something the reboot may have
        ended (rebootwait.py): its retry_when checks local transient state (a local detached job, its
        own checks, a process, a /tmp or run-folder file), or a detached job of its own from the earlier
        boot wrote no .rc. A timer, a probe of another host or another project's reply sleeps on, and
        `survives_reboot` always does. The wake counts
        toward max_reboot_losses only after an abrupt reboot that cut short a detached job of its own
        (one with no .rc yet): only then may the task be what takes the host down. A task whose
        retry_when checks local detached jobs (rebootwait.local_jobs, also within a compound probe) sleeps
        on while each ended or runs on this boot (relaunched, or a daemon restart within the boot); one
        the reboot cut short and nothing relaunched wakes it at light, told to relaunch and re-arm."""
        if self._boot_woken:
            return
        self._boot_woken = True
        if not self.boot_at:
            return
        db = self.p.db
        earlier = (db.kv("boot_prev") or {}).get("prev_boot")
        cycle = self._planned_cycle(earlier) if earlier and self._this_boot((db.kv("boot_prev") or {}).get("boot")) \
            else None
        for t in db.q("SELECT * FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
            prev = load_result(t["result"])
            if prev.get("status") != "waiting":
                continue
            since = prev.get("waiting_since")
            since = since if isinstance(since, (int, float)) else t["updated"]
            if not since or since >= self.boot_at:
                continue
            if held := self._held_at_reboot(t["id"], earlier):
                # A detached job of its own held a device or machine when the host went down abruptly:
                # neither relaunched nor woken, whatever its retry_when says. The coordinator decides.
                self._block_after_reboot(t, prev, held_at_reboot_reason(held))
                continue
            if prev.get("survives_reboot") is True:
                continue
            asleep, relaunched = self._relaunch_checks(t, prev)
            cut = False
            if not asleep:
                asleep, cut = self._relaunch_jobs(t, prev)
            if asleep:
                continue   # asleep on its retry_when: the same checks or resumable jobs run again
            if rebootwait.stays_asleep(prev, (str(self.p.runs),)) and not self._job_without_rc(t["id"]):
                continue   # nothing it waits on died with the host: its timer and probe stand
            probe = str(prev.get("retry_when") or "")
            rcs = rebootwait.local_jobs(probe)
            lost = self._lost_jobs(t["id"], rcs)
            if rcs and not lost and not rebootwait.transient_besides_jobs(probe, (str(self.p.runs),)):
                # Every local job it waits on ended or runs on this boot (relaunched, or a daemon restart
                # within the boot): its retry_when wakes it once they end.
                log(self.p, f"task {t['id']} waited from before the reboot on detached jobs that ended or "
                            "still run; asleep on its retry_when")
                continue
            self._probe_rc.pop(t["id"], None)
            counts = bool(relaunched) or cut or self._job_cut_by_reboot(t["id"])
            wakes = _reboot_wakes(t) + int(counts)
            # Reboots its relaunched checks or jobs went through count too: a loop stays within the cap.
            n = self._reboot_losses(t["id"]) + wakes + relaunched + self._job_relaunch_boots(t["id"])
            if counts and n >= int(self.cfg["budget"].get("max_reboot_losses", 3)):
                # Its detached job may be what takes the host down: stop waking it.
                self._block_after_reboot(t, prev, f"lost to a host reboot {n} times; it may be causing them")
                continue
            reboot = {"at": self.boot_at}
            if cycle:
                reboot["planned"] = str(cycle.get("reason") or "no reason recorded")[:300]
            woke = {"woke": "the host rebooted"}
            if gone := [rc for rc in rcs if rc in lost]:
                # Only relaunching its jobs and re-arming the same wait is left: a light run does that.
                names = ", ".join(rc.stem for rc in gone[:5]) + (f" and {len(gone) - 5} more" if len(gone) > 5 else "")
                woke = {"woke": f"the host rebooted and cut short detached job{'s' if len(gone) > 1 else ''} {names} "
                                f"(in {gone[0].parent}), not relaunched: relaunch each with the same command and "
                                "hand off waiting again with the same retry_when (naming the new .rc paths)",
                        "wake_tier": "light"}
                prev.pop("escalated_wake", None)
            db.update_task(t["id"], not_before=None, result=dump_result(
                {**prev, **woke, "reboot": reboot, "reboot_wakes": wakes}))
            log(self.p, f"task {t['id']} waited from before the reboot; due now")

    def _block_after_reboot(self, t: dict, prev: dict, reason: str) -> None:
        """Block waiting task t at boot for `reason`, with a high task_blocked event for the coordinator."""
        db = self.p.db
        prev.pop("reboot_wakes", None)
        with db.tx():
            db.update_task(t["id"], status="blocked", blocked_reason=reason[:500], result=dump_result(prev))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{t['id']}", "task_blocked", "high",
                  f"#{t['id']} {t['title']} → blocked: {reason}", "queued", t["id"]))
        log(self.p, f"task {t['id']} blocked: {reason}")

    def _relaunch_checks(self, t: dict, prev: dict) -> tuple[bool, int]:
        """A task waiting on its own `ttp checks --detach` (retry_when `ttp checks --result <its run
        dir>`) that the reboot killed: start the same checks again in that run dir, model-free, and
        keep it asleep on its retry_when. Only while the worktree checks.pid names still exists, holds
        the head they ran on and has no uncommitted changes to tracked files (untracked ones never
        count, as for `ttp checks`), at most once per boot per task and max_reboot_losses times per
        head since the task was last blocked (checks that take the host down are not started forever).
        Returns (asleep, relaunched): asleep when the task stays asleep (relaunched now, or already on
        this boot, a daemon restart), else it wakes as before; relaunched is the number of relaunches
        when the cap refused one after an abrupt reboot, which then count as reboot losses, else 0."""
        from .cli import CHECKS_RC, _checks_alive, _read_checks_pid
        db, tid = self.p.db, t["id"]
        m = re.search(r"\bchecks\s+--result\s+(\S+)", str(prev.get("retry_when") or ""))
        try:
            out = Path(shlex.split(m.group(1))[0]) if m else None
        except ValueError:
            out = None
        if not out or not db.one("SELECT id FROM runs WHERE task=? AND dir=?", (tid, str(out))):
            return False, 0
        if detach.pending(out / CHECKS_RC):
            return True, 0   # the durable request will start on this boot, without a model wake
        mine = f"checks_relaunched:{tid}:{self.boot}:"
        if db.one("SELECT id FROM events WHERE fingerprint LIKE ?", (mine + "%",)):
            return True, 0
        info = _read_checks_pid(out)
        wt, head, boot, cmd = info.get("worktree"), info.get("head"), info.get("boot"), info.get("cmd")
        # A malformed checks.pid (fields of the wrong type) wakes the task as before.
        if not all(isinstance(x, str) and x for x in (wt, head, boot)) or not isinstance(cmd, list):
            return False, 0
        if (out / CHECKS_RC).exists() or self._this_boot(boot) or _checks_alive(info):
            return False, 0
        since = (db.one("SELECT MAX(ts) ts FROM events WHERE task=? AND kind='task_blocked'", (tid,)) or {}).get("ts")
        same = (db.one("SELECT COUNT(*) n FROM events WHERE kind='checks_relaunched' AND task=? AND fingerprint "
                       "LIKE ? AND ts>?", (tid, f"checks_relaunched:{tid}:%:{head[:12]}", since or 0)) or {}).get("n", 0)
        if same >= int(self.cfg["budget"].get("max_reboot_losses", 3)):
            return False, (same if self._shutdown(boot) == "abrupt" else 0)
        why = self._restart_checks(tid, out, info, f"relaunch-t{tid}")
        if why:
            log(self.p, f"task {tid}: the reboot killed its checks, not started again: {why}")
            return False, 0
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "daemon", "checks_relaunched", mine + head[:12], "low",
              f"#{tid} {t['title']}: the reboot killed its checks on {head[:12]}; started them again in {out}, "
              f"no model run", "handled", tid))
        log(self.p, f"task {tid}: the reboot killed its checks; started them again on {head[:12]} ({out})")
        return True, 0

    def _relaunch_jobs(self, t: dict, prev: dict) -> tuple[bool, bool]:
        """A task waiting on local detached jobs (`ttp detach --check <rc...>` in its retry_when, alone
        or in a compound probe: rebootwait.local_jobs) that all
        opted in with `--resumable`: start each the reboot cut short (no .rc) again, model-free, note
        it on the task and keep the task asleep on its retry_when. Returns (asleep, cut). asleep when it
        stays asleep (relaunched now, or already on this boot: a daemon restart). Else it wakes as
        before: a job it waits on that did not opt in, one found lost on an earlier boot, one that
        cannot start (the ones started before it are stopped again: all or none), one past budget.max_job_relaunches relaunches, or a relaunch that would reach
        budget.max_reboot_losses (noted on the task, so its run reads why). A job relaunched on this
        boot before a daemon restart counts as started: stopped when the task wakes, and its
        job_relaunched event written if the restart came first; one the restart cut off between
        counting and starting it is started now. cut when an abrupt,
        unplanned reboot cut short one of its resumable jobs: the wake then counts as a reboot loss.
        Relaunches after such reboots count toward max_reboot_losses too (_job_relaunch_boots)."""
        tid = t["id"]
        rcs = rebootwait.local_jobs(str(prev.get("retry_when") or ""))
        dirs = {r["dir"] for r in self.p.db.q("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL", (tid,))}
        if not rcs or any(str(rc.parent) not in dirs for rc in rcs):
            return False, False   # not only its own runs' jobs: not ours to start
        if rebootwait.transient_besides_jobs(str(prev.get("retry_when") or ""), (str(self.p.runs),)):
            return False, False   # the probe also checks what the reboot ended: it wakes, never sleeps on
        todo, done, cut = [], [], False
        cap = int(self.cfg["budget"].get("max_job_relaunches", detach.RELAUNCH_CAP))

        def running():   # jobs relaunched on this boot that still run: a woken task keeps none
            return [(r, k) for r, k in ((r, detach.running_pid(r, self._this_boot)) for r, _, _ in done) if k]

        def wake(why: str) -> None:
            for r, k in running():
                detach.stop(r, k, f"stopped right after its relaunch: the task wakes instead ({why})")
        for rc in rcs:
            m = detach.read_resume(rc)
            if m is not None and self._this_boot(m.get("boot")) and m.get("relaunches"):
                # Relaunched on this boot already (a daemon restart, maybe partway through the jobs):
                # counted as started below, judged by the boot that cut it short.
                loss = self._abrupt_cut(m.get("cut_boot"))
                cut = cut or loss
                # One counted but never started (the restart came between the two) starts now,
                # counted once (relaunch keeps the count), its job_relaunched written once it runs.
                (done if detach.relaunch_started(rc) else todo).append((rc, int(m["relaunches"]) - 1, loss))
                continue
            if rc.exists():
                continue
            lost = detach.lost(rc)
            if m is None or lost.exists() and lost.stat().st_mtime < (self.boot_at or 0):
                wake(f"detached job {rc.stem} is not resumable or was lost before this boot")
                return False, cut
            loss = self._abrupt_cut(m.get("boot"))
            cut = cut or loss
            n = int(m.get("relaunches") or 0)
            if n >= cap:
                why = (f"the reboot cut short detached job {rc.stem} again; it was relaunched {n} times "
                       f"already (cap {cap}), so the task wakes instead")
                _task_note(rc.parent, why)
                log(self.p, f"task {tid}: {why}")
                wake(why)
                return False, cut
            todo.append((rc, n, loss))
        if not todo and not done:
            return False, False   # all ended before the reboot: woken as before
        limit = int(self.cfg["budget"].get("max_reboot_losses", 3))
        if todo and cut and (k := self._reboot_losses(tid) + _reboot_wakes(t) + self._job_relaunch_boots(tid)) + 1 >= limit:
            # This reboot would reach the cap: wake it, and the wake path blocks it as a reboot loop.
            why = (f"the reboot cut short detached job {todo[0][0].stem}; not relaunched: with {k} earlier "
                   f"reboot losses this one reaches budget.max_reboot_losses ({limit})")
            _task_note(todo[0][0].parent, why)
            log(self.p, f"task {tid}: {why}")
            wake(why)
            return False, True
        # All or none: a job that cannot start wakes the task, so the ones started before it (also by
        # this daemon's earlier life on this boot) are stopped again (no job of a woken task runs on), and
        # the relaunches are recorded only once all started (a boot that wakes the task is not also
        # counted as a relaunch boot).
        nice = nice_level(self.cfg.get("runner"))[0]
        started = running()
        for rc, n, loss in todo:
            why, pid = detach.relaunch(rc, self.boot, nice)
            if why:
                for r, k in started:
                    detach.stop(r, k, f"stopped right after its relaunch: detached job {rc.stem}, which the "
                                      f"task also waits on, could not start ({why}), so the task wakes instead")
                _task_note(rc.parent, f"the reboot cut short detached job {rc.stem}; not relaunched: {why}"
                           + (f"; stopped the jobs relaunched before it ({', '.join(r.stem for r, _ in started)})"
                              if started else ""))
                log(self.p, f"task {tid}: detached job {rc.stem} not relaunched: {why}")
                return False, cut
            started.append((rc, pid))
        for rc, n, loss in done + todo:
            fp = f"job_relaunched:{tid}:{self.boot}:{'loss' if loss else 'free'}:{rc}"
            if self.p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue   # recorded before a daemon restart
            text = (f"the host reboot cut short detached job {rc.stem}; relaunched it "
                    f"({n + 1} of {cap}), no model run")
            _task_note(rc.parent, text)
            self.p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (time.time(), "daemon", "job_relaunched", fp, "low",
                         f"#{tid} {t['title']}: {text}", "handled", tid))
            log(self.p, f"task {tid}: {text} ({rc})")
        return True, cut

    def _abrupt_cut(self, boot: str | None) -> bool:
        """Whether boot `boot` ended in an abrupt reboot no planned host cycle explains: one a job
        it cut short may have caused (as _job_cut_by_reboot judges)."""
        return self._shutdown(boot) != "orderly" and not (boot and self._planned_cycle(boot))

    def _job_relaunch_boots(self, tid: int) -> int:
        """Boots on which resumable jobs of task tid were relaunched after an abrupt, unplanned reboot,
        since it was last blocked: each is a reboot loss the relaunch kept from waking the task."""
        db = self.p.db
        since = (db.one("SELECT MAX(ts) ts FROM events WHERE task=? AND kind='task_blocked'", (tid,)) or {}).get("ts")
        rows = db.q("SELECT fingerprint FROM events WHERE kind='job_relaunched' AND task=? AND ts>? AND fingerprint "
                    "LIKE ?", (tid, since or 0, f"job_relaunched:{tid}:%:loss:%"))
        return len({r["fingerprint"].split(":")[2] for r in rows})

    def _restart_checks(self, tid: int, out: Path, info: dict, run_id: str) -> str:
        """Start the checks that checks.pid `info` names again in run dir `out`, only while their
        worktree still exists, holds the head they ran on and has no uncommitted changes to tracked
        files (untracked ones never count, as for `ttp checks`): '' once started, else why not."""
        wt, head, cmd = info.get("worktree"), info.get("head"), info.get("cmd")
        if not all(isinstance(x, str) and x for x in (wt, head)) or not isinstance(cmd, list):
            return "its checks.pid does not name a worktree, head and command"
        why = ""
        try:
            if not worktree.is_git(Path(wt)):
                why = f"its worktree {wt} is gone"
            elif (now := worktree._git(Path(wt), "rev-parse", "HEAD")) != head:
                why = f"its worktree moved from {head[:12]} to {now[:12]}"
            elif worktree._git(Path(wt), "status", "--porcelain", "--untracked-files=no"):
                why = f"its worktree {wt} has uncommitted changes"
        except Exception as e:
            why = f"its worktree could not be read: {e}"
        if why:
            return why
        argv = (["--fresh"] if info.get("fresh") else []) + (["--full"] if info.get("full") else []) \
            + (["--purpose", "harness"] if info.get("purpose") == "harness" else []) + [str(c) for c in cmd]
        return "" if self._start_checks(Path(wt), out, run_id, tid, argv) else "they could not be started"

    def _start_checks(self, path: Path, out: Path, run_id: str, tid: int, extra: list[str]) -> bool:
        """`ttp checks --detach [extra]` in worktree `path`, writing to `out` as a run's directory,
        as a worker's run would: the project's venv, `ttp` first, niced like workers."""
        runtime_dir = str(Path(__file__).resolve().parent.parent)
        bin_path = f"{service_path()}:{os.environ.get('PATH', '')}"
        venv = worktree.project_venv(self.p, path)
        venv_vars = worktree.venv_env(venv, bin_path) if venv else {}
        env = {**os.environ, **venv_vars, "TTP_RUN_DIR": str(out), "TTP_PROJECT": str(self.p.base),
               "TTP_RUN_ID": run_id, **task_env(tid), "PYTHONPATH": runtime_dir,
               "PATH": f"{self.p.harness / 'bin'}:{venv_vars.get('PATH', bin_path)}"}
        env = machines.mask_devices(env)   # as in the worker's run: no direct device opens
        level = nice_level(self.cfg.get("runner"))[0]
        argv = (["nice", "-n", str(level)] if level else []) + [sys.executable, "-m", "ttp", "checks", "--detach", *extra]
        try:
            out.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(argv, cwd=path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            log(self.p, f"task {tid}: checks not started: {e}")
            return False
        if r.returncode:
            log(self.p, f"task {tid}: checks not started: {(r.stderr or r.stdout).strip()[:300]}")
            return False
        return True

    def _planned_cycle(self, boot: str) -> dict | None:
        """The record of a planned host cycle that ended boot `boot`, if any: a host-cycle record
        (hostcycle.planned), or, for the boot just before this one, a power-cycle intent for this host
        in the machine change ledger (_ledger_cycle). {reason, path, planner (what planned it)}; read once
        per boot."""
        from . import hostcycle
        if boot not in self._cycles:
            try:
                rec = hostcycle.planned(self.p, boot, self.boot_at)
                self._cycles[boot] = {**rec, "planner": f"host-cycle record {rec.get('id') or Path(rec['path']).parent.name}"
                                                       + (f" by {rec['by']}" if rec.get("by") else "")} if rec \
                    else self._ledger_cycle(boot)
            except Exception:
                log(self.p, "host-cycle records: " + traceback.format_exc().replace("\n", " | ")[:1000])
                self._cycles[boot] = None
        return self._cycles[boot]

    def _ledger_cycle(self, boot: str) -> dict | None:
        """A power-cycle intent (`ttp machines change add <this host> --power-cycle-at <t>`) that explains
        the reboot ending boot `boot`: only the boot just before this one, and only when this boot came
        from POWER_CYCLE_LEAD_S before t up to hostcycle.WINDOW_S after it."""
        from . import hostcycle
        prev = self.p.db.kv("boot_prev") or {}
        mine = machines.here()
        if not (mine and self.boot_at and prev.get("prev_boot") == boot and self._this_boot(prev.get("boot"))):
            return None
        for c in reversed(machine_ledger.changes([mine[0]], open_only=False)):
            at = c.get("at")
            if c.get("kind") == machine_ledger.POWER_CYCLE and isinstance(at, (int, float)) \
                    and -machine_ledger.POWER_CYCLE_LEAD_S <= self.boot_at - at <= hostcycle.WINDOW_S:
                who = f"machine change #{c.get('id')}" + (f" by {c['by']}" if c.get("by") else "")
                return {"reason": str(c.get("what") or ""), "path": who, "planner": who, "project": self.p.name}
        return None

    def _other_projects(self) -> list[str]:
        """The names of this user's other projects (the registry), longest first; [] when unreadable."""
        from .project import load_registry
        try:
            names = (load_registry().get("projects") or {}).keys()
        except (OSError, ValueError, AttributeError):
            return []
        return sorted((n for n in names if isinstance(n, str) and n != self.p.name), key=len, reverse=True)

    def _last_earlier_boot(self, tid: int) -> str | None:
        """The boot of task tid's latest run on a boot before this one, or None."""
        last = self.p.db.one("SELECT boot_id FROM runs WHERE task=? AND boot_id IS NOT NULL AND boot_id NOT IN (?,?) "
                             "ORDER BY id DESC LIMIT 1", (tid, *self.boots))
        return last["boot_id"] if last else None

    def _jobs_without_rc(self, tid: int, boot: str) -> bool:
        """Whether a run of task tid on `boot` detached a job that wrote no .rc."""
        dirs = self.p.db.q("SELECT dir FROM runs WHERE task=? AND boot_id=? AND dir IS NOT NULL", (tid, boot))
        return any(not Path(j["rc"]).exists() for r in dirs for j in _detached_jobs(Path(r["dir"])))

    def _lost_jobs(self, tid: int, rcs: list[Path]) -> list[Path]:
        """Of the local detached jobs task tid waits on (`rcs`) and those its runs on the earlier boot
        started, the ones with no .rc that do not run on this boot (detach.running_pid): what the
        reboot cut short and nothing relaunched."""
        boot = self._last_earlier_boot(tid)
        dirs = self.p.db.q("SELECT dir FROM runs WHERE task=? AND boot_id=? AND dir IS NOT NULL", (tid, boot)) \
            if boot else []
        jobs = dict.fromkeys([*rcs, *(Path(j["rc"]) for r in dirs for j in _detached_jobs(Path(r["dir"])))])
        return [rc for rc in jobs if not rc.exists() and not detach.running_pid(rc, self._this_boot)]

    def _job_without_rc(self, tid: int) -> bool:
        """Whether a detached job task tid started on the earlier boot never wrote its .rc: the reboot
        ended it, however the host went down."""
        boot = self._last_earlier_boot(tid)
        return bool(boot) and self._jobs_without_rc(tid, boot)

    def _job_cut_by_reboot(self, tid: int) -> bool:
        """Whether the abrupt reboot behind this boot cut short a detached job of task tid: one its
        runs on the earlier boot started that wrote no .rc."""
        boot = self._last_earlier_boot(tid)
        if not boot or self._shutdown(boot) == "orderly":
            return False
        if cycle := self._planned_cycle(boot):
            # A planned host cycle (a recovery owner power cycled the host on purpose) ended it, not the task.
            log(self.p, f"task {tid}: its detached job was cut by a planned host cycle ({cycle['path']}); "
                        "not counted as a reboot loss")
            return False
        return self._jobs_without_rc(tid, boot)

    def _branch_head(self, task: dict) -> str:
        """The commit a task's own branch points at, or "": a new commit there is progress."""
        if not task["branch"] or not worktree.own_worktree(task):
            return ""
        try:
            return worktree._git(self.p.root, "rev-parse", "--verify", "-q", f"refs/heads/{task['branch']}^{{commit}}",
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            return ""

    def _held_at_reboot(self, tid: int, boot: str | None) -> list[str]:
        """The resources task tid held (its run or a detached job of its own, through `ttp lock`) at
        the last heartbeat before the reboot that ended boot `boot`, when that reboot was abrupt and
        unplanned; [] otherwise (an orderly shutdown or a planned host cycle was not the task's doing).
        A resource named after another machine on the machines list (`<alias>` or `<alias>-...`) is
        used over ssh: this host's reboot did not come from it."""
        prev = self.p.db.kv("boot_prev") or {}
        if not boot or not self._this_boot(prev.get("boot")) or prev.get("prev_boot") != boot \
                or prev.get("shutdown") == "orderly":
            return []
        held = held_by_task(prev.get("held"), tid, self.p.name)
        if held:
            try:
                known = machines.load()
                me = (machines.here(known) or ("",))[0]
            except Exception:   # an unreadable list must not hide a hold
                known, me = {}, ""
            others = [a for a in known if a != me]
            held = [x for x in held if not any(x == a or x.startswith(a + "-") for a in others)]
        return held if held and not self._planned_cycle(boot) else []

    def _reboot_losses(self, tid: int, key: str = "lost_to_reboot") -> int:
        """Runs of the task lost to an abrupt host reboot (or, by key, a host sleep) since it was last
        blocked: a person or the coordinator who requeues a task blocked for reboots starts its count
        over. An orderly reboot (a shutdown, an update) is not one the task may have caused."""
        db = self.p.db
        since = (db.one("SELECT MAX(ts) ts FROM events WHERE task=? AND kind='task_blocked'", (tid,)) or {}).get("ts")
        return sum(1 for x in db.q("SELECT note FROM runs WHERE task=? AND status='lost' AND note LIKE ? "
                                   "AND started>?", (tid, f"%{key}%", since or 0))
                   if (n := json.loads(x["note"] or "{}")).get(key)
                   and not (key == "lost_to_reboot" and n.get("shutdown") == "orderly"))

    def _start_probe(self, tid: int, probe: str, now: float, what: str = "retry_when") -> None:
        self._probed[tid] = now
        if ends.hides_test(probe):   # stored before it was refused: a broken probe, never a pass
            self._probe_rc[tid] = (ends.TRIVIAL_WHY, now, probe)
            return
        # One run per distinct command per probe cycle: tasks waiting on the same thing share it.
        for other, run in self._probes.items():
            if other != tid and run[2] == probe:
                self._probes[tid] = run
                return
        for other, got in self._probe_rc.items():
            if other != tid and got[2] == probe and now - got[1] < PROBE_EVERY_S:
                self._probe_rc[tid] = got
                return
        out = None
        try:
            # Its output (the blocking gate's last word) goes to a file: a pipe nobody reads could fill.
            out = tempfile.TemporaryFile()
            proc = subprocess.Popen(landed.probe_command(probe, self.p), shell=True, cwd=str(self.p.root),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=self._probe_env())
        except OSError as e:
            if out:
                out.close()
            log(self.p, f"task {tid} {what} probe could not start: {e}")
            self._probe_rc[tid] = ("could not start", now, probe)
            return
        self._probes[tid] = (proc, now, probe)
        self._probe_files[id(proc)] = out

    def _probe_text(self, proc) -> str:
        """The end of a finished probe run's output, once (its file is closed after)."""
        return self._probe_texts(proc)[0]

    def _probe_texts(self, proc) -> tuple[str, str]:
        """_probe_text, and the output's last non-empty line (waitheal.last_line) for a wait note."""
        f = self._probe_files.pop(id(proc), None)
        if f is None:
            return "", ""
        try:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            raw = f.read().decode(errors="replace")
            return waitheal.clip(raw), waitheal.last_line(raw)
        except (OSError, ValueError):
            return "", ""
        finally:
            f.close()

    def _gate_answer(self, task: dict, probe: str, rc, out: str, now: float) -> str | None:
        """A task asleep after a wake-gate skip: 'same' when the probe gave the answer the gate already
        judged (it stays asleep), 'changed' when it gave another, None when no skip applies. Only the
        caller acts on 'changed': a pass, a failed job or an unlanded push goes through its own
        model-free path (_probe_passed, _job_failed, _push_finished_unpushed). A failed job or an
        unlanded push is never held, whatever the gate saw."""
        if task["status"] != "queued" or (task["not_before"] or 0) <= now:
            return None
        held = waitgate.holds(load_result(task["result"]), rc, out)
        if held == "same" and (detach.failed_check(probe, rc) or _push_not_landed(probe, rc)):
            return None
        return held

    def _note_probe_out(self, tid: int, probe: str, text: str, now: float, line: str = "") -> None:
        got = self._probe_out.get(tid)
        self._probe_live[tid] = (line, now, probe)
        if not got or got[0] != text or got[2] != probe:
            self._probe_out[tid] = (text, now, probe)

    def _flag_never_passes(self, task: dict, probe: str, rc, text: str, stale: bool = False) -> None:
        """One `probe_never_passes` event per wait for a waiting task whose probe can never pass
        (waitheal.never_passes; at wait_stale also the same output for max_hold_s while a task it names
        has ended). Checked when the wait starts, on each verdict and at wait_stale. The task keeps
        its timer and its probe; the coordinator decides. A probe that exits 0 passed: its task wakes."""
        prev = load_result(task["result"])
        if rc == 0 or detach.failed_check(probe, rc) or prev.get("status") != "waiting" \
                or prev.get("retry_when") != probe:
            return
        tid, db = task["id"], self.p.db
        since = prev.get("waiting_since")
        since = float(since) if isinstance(since, (int, float)) else float(task["updated"] or 0)
        fp = f"probe_never_passes:{tid}:{since:.0f}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        try:
            why = waitheal.never_passes(self.p, probe, rc, text)
            got = self._probe_out.get(tid)
            max_hold = float((self.cfg.get("waiting") or {}).get("max_hold_s") or 6 * 3600)
            if not why and stale and got and got[2] == probe and time.time() - got[1] >= max_hold \
                    and (ended := waitheal.ended_refs(db, probe, tid)):
                why = (f"it has printed the same output for {max_hold / 3600:g} h while "
                       f"{', '.join(ended)} it names has ended")
        except Exception as e:   # a check of the wait must never break the wait
            log(self.p, f"task {tid}: could not check its probe: {type(e).__name__}: {e}")
            return
        if not why:
            return
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "daemon", "probe_never_passes", fp, "normal",
              f"#{tid} {task['title']} waits on a probe that can never pass: {why}. Blocking gate: "
              f"{probe[:200]}; last output: {text or '(none yet)'}. Decide: fix its probe (`ttp task set-when "
              f"{tid} \"<cmd>\"`), clear it (\"\") so it wakes at its timer, or cancel the task.", "queued", tid))
        log(self.p, f"task {tid} probe can never pass: {why}; raised to the coordinator")

    def _restart_runner(self, runner: str, tid: int, now: float) -> None:
        """A waiting task's `ttp devq probe` found its job pending and runner `runner` down: run `ttp
        devq start` here, model-free, at most once per RESTART_EVERY_S and
        device.runner_restarts_per_day times a day per runner. Each restart is logged and recorded
        as a handled `devq_runner_restarted` event. Two failed in a row, or the day's cap reached,
        post one low line and queue one harness task to fix the runner; the task sleeps on."""
        db = self.p.db
        cap = int((self.cfg.get("device") or {}).get("runner_restarts_per_day") or waitheal.RESTARTS_PER_DAY)
        every = (db.kv(waitheal.RUNNER_RESTARTS_KEY, {}) or {})
        st = dict(every.get(runner) or {})
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if st.get("day") != day:
            st.update(day=day, n=0)
        if now - float(st.get("last") or 0) < waitheal.RESTART_EVERY_S:
            return
        if int(st.get("n") or 0) >= cap:
            if st.get("capped") != day:
                st["capped"] = day
                log(self.p, f"device runner {runner}: down again after {cap} restarts today; not restarted")
                self._report_runner(runner, f"went down again after {cap} restarts today", st.get("error") or "", tid)
                db.set_kv(waitheal.RUNNER_RESTARTS_KEY, {**every, runner: st})
            return
        rc, out = self._devq_start(runner)
        ok = rc in (0, waitheal.DEVQ_RUNNER_DOWN_RC)   # 3: an outside device user still runs, not a failure
        st.update(last=now, n=int(st.get("n") or 0) + 1, fails=0 if ok else int(st.get("fails") or 0) + 1,
                  error="" if ok else waitheal.clip(out, 300))
        if ok:
            st.pop("reported", None)
        db.set_kv(waitheal.RUNNER_RESTARTS_KEY, {**every, runner: st})
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "devq_runner_restarted", "low",
              f"device runner {runner} was down while #{tid} waited on it: `ttp devq start` "
              f"{'ran' if ok else f'failed (exit {rc})'}, no model run: {waitheal.clip(out, 200)}", "handled", tid))
        log(self.p, f"device runner {runner} down while task {tid} waits: restart {'ok' if ok else f'failed ({rc})'}: "
                    f"{waitheal.clip(out, 200)}")
        if not ok and st["fails"] >= waitheal.RESTART_FAILS_REPORTED and not st.get("reported"):
            st["reported"] = True
            db.set_kv(waitheal.RUNNER_RESTARTS_KEY, {**every, runner: st})
            self._report_runner(runner, f"could not be restarted {st['fails']} times in a row", st["error"], tid)
        self._progress()

    def _devq_start(self, runner: str) -> tuple[int, str]:
        try:
            r = subprocess.run([sys.executable, "-m", "ttp", "devq", "start", runner], cwd=str(self.p.root),
                               env=self._probe_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=2 * PROBE_TIMEOUT_S + 30)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, f"{type(e).__name__}: {e}"
        return r.returncode, f"{r.stdout or ''} {r.stderr or ''}".strip()

    def _report_runner(self, runner: str, why: str, error: str, tid: int) -> None:
        """One low line for the user and one open harness task to fix the runner."""
        db = self.p.db
        title = f"Fix device runner {runner}: it {why}"[:200]
        if db.one("SELECT id FROM tasks WHERE kind='harness' AND title LIKE ? AND status NOT IN ('done','failed',"
                  "'cancelled')", (f"Fix device runner {runner}: %",)):
            return
        spec = (f"Device runner `{runner}` (config device.runners.{runner}) {why}. The daemon restarts it "
                f"model-free when a waiting task's `ttp devq probe` finds it down (task #{tid} waits on it). "
                f"Last error: {error or '(none recorded)'}.\n\nFind out why: `ttp devq status {runner}`, and the "
                f"runner's runner.out and runner.log in its state folder. Fix what is this project's (its config "
                f"entry, its health gate), bring it back with `ttp devq start {runner}`, and report what is not.")
        fid = db.add_task(title, spec, kind="harness", tier="standard", priority=2, origin="daemon",
                          labels=[f"devq_runner:{runner}"])
        db.post("out", f"Device runner {runner} {why}; queued #{fid} to fix it. Tasks waiting on it keep sleeping.",
                chat=None, kind="info", severity="low", ref=f"devq_runner:{runner}")
        log(self.p, f"device runner {runner} {why}: queued task {fid}")

    def _hold_waiting(self, task: dict, prev: dict, now: float) -> bool:
        """A waiting task whose timer ran out: wake it, or put it back to sleep while its probe says
        "not yet". True when a probe must run before that can be decided."""
        tid = task["id"]
        if prev.get("underuse_wake"):   # sample_jobs pulled this check in: a worker looks at the job
            self._wake_waiting(task, f"its detached job under-uses what it asked for: {prev['underuse_wake']}", now)
            return False
        max_hold = float((self.cfg.get("waiting") or {}).get("max_hold_s") or 6 * 3600)
        since = float(prev["waiting_since"])
        rc, at, _ = self._probe_rc.get(tid, (None, 0.0, ""))
        fresh = at >= since and now - at <= 2 * PROBE_EVERY_S
        if fresh and rc == 0:
            self._probe_passed(task, now)
        elif fresh and detach.failed_check(str(prev.get("retry_when") or ""), rc):
            self._job_failed(task, now)
        elif fresh and _push_not_landed(prev.get("retry_when"), rc):
            self._push_finished_unpushed(task, now)
        elif fresh and rc not in NOT_YET_RCS:
            self._wake_waiting(task, f"probe broken: {rc if isinstance(rc, str) else f'exit {rc}'}", now)
        elif fresh:
            # 75 is EX_TEMPFAIL (e.g. a busy `ttp lock`); 255 is ssh failing to reach the host
            # (a reboot or a network blip). Both mean "not yet". A model run cannot make a "not yet"
            # probe pass, so past `max_hold_s` the coordinator decides instead of a wake.
            held = now >= since + max_hold
            gate = time_gate(str(prev.get("retry_when") or ""))
            # A pure time gate whose epoch is still ahead is not stuck: no turn could make it pass sooner.
            # Nor is a user pause (userpause): only the user's word lifts it. Nor a reply wait while
            # its peer is recovering (ackwait.peer_recovering): it raises once the peer can act again.
            if held and (gate is None or gate <= now) \
                    and not userpause.is_probe(prev.get("retry_when"), self.cfg) \
                    and not ackwait.peer_recovering(prev.get("retry_when"), self.cfg, self.p.harness)[0]:
                self._stale_wait_event(task, prev, since, max_hold)
                self._flag_never_passes(task, str(prev.get("retry_when") or ""), rc,
                                        (self._probe_out.get(tid) or ("",))[0], stale=True)
            nb = now + _retry_s(prev) if held else min(now + _retry_s(prev), since + max_hold)
            what = self._wait_what(tid, prev)
            says = "host unreachable" if rc == 255 else "not yet"
            db = self.p.db
            db.update_task(tid, not_before=nb, blocked_reason=(
                f"waiting for {what}; its probe says {says}; next try "
                f"{timefmt.short(nb, self.p)}")[:500])
            try:
                probe = str(prev.get("retry_when") or "")
                progress.note_probe(db, tid, probe, rc, startgate.reason(
                    db, probe, rc, (self._probe_out.get(tid) or ("",))[0]), at)
            except Exception as e:   # counting must never keep a wait from sleeping
                log(self.p, f"progress check: could not count task {tid}'s probe: {type(e).__name__}: {e}")
            log(self.p, f"task {tid} retry_when probe still failing"
                        f"{' (host unreachable)' if rc == 255 else ''}; asleep until "
                        f"{timefmt.short(nb, self.p)}")
        else:
            # No recent verdict (the daemon restarted): ask the probe before waking a worker.
            self.p.db.update_task(tid, not_before=now + PROBE_TIMEOUT_S)
            return tid not in self._probes
        return False

    def sample_jobs(self) -> None:
        """Detached jobs of live tasks: what each asked for against what it uses (jobuse.py), model-free,
        every `waiting.job_use.sample_s`. An under-use stretch is recorded (kv job_use), raises one
        `job_underuse` event and pulls its waiting task's next check in to `check_cap_s` away."""
        s, now = jobuse.settings(self.cfg), time.time()
        if now - self._jobs_sampled < s["sample_s"]:
            return
        self._jobs_sampled = now
        db = self.p.db
        nodes = [str(x) for x in (self.cfg.get("device") or {}).get("nodes") or []]
        old, new = db.kv(jobuse.KV, {}) or {}, {}
        ctrs = None   # docker containers, read once per pass and only when a job runs

        def with_containers(got: dict | None) -> dict | None:
            nonlocal ctrs
            if not got:
                return got
            if ctrs is None:
                ctrs = [] if now < self._docker_off_until else jobuse.containers()
                if ctrs is None:   # docker there but unreadable: the tree alone counts; retry later
                    ctrs, self._docker_off_until = [], now + 900
            if ctrs:
                got["ctr"] = jobuse.container_cpu(ctrs, got.get("start"))
            return got

        for t in db.q("SELECT * FROM tasks WHERE status IN ('queued','running')"):
            for r in db.q("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL AND started>? ORDER BY id DESC",
                          (t["id"], now - 7 * 86400)):
                for j in _detached_jobs(Path(r["dir"])):
                    rc = Path(j["rc"])
                    if rc.exists() or locks.any_free([locks.job_lock(rc)]):
                        continue   # ended, or not started yet
                    req = dict(j.get("requested") or {})
                    if not req.get("devices") and needs_device(t, self.cfg):
                        req["devices"] = 1   # the task holds the device while the job runs
                    pid = jobuse.root_pid(j)
                    was = old.get(str(rc)) or {}
                    got = with_containers(jobuse.sample(pid, nodes)) if pid else None
                    st, fire = jobuse.assess(was.get("st"), got, req, now, s)
                    entry = {"task": t["id"], "name": j["name"], "req": req, "st": st, "line": jobuse.line(req, st)}
                    if was.get("pulled") and was.get("pulled") == st.get("under"):
                        entry["pulled"] = was["pulled"]
                    new[str(rc)] = entry
                    if fire:
                        self._underuse_event(t, entry, now, s)
                    if st.get("under") and "pulled" not in entry and self._pull_check(t["id"], st["why"], now, s):
                        entry["pulled"] = st["under"]
        # A stretch that ended (use recovered, or the job ended) before its pulled-in check: that check
        # follows the probe as usual instead of waking a worker.
        still = {e["task"] for e in new.values() if e.get("pulled")}
        for tid in {e.get("task") for k, e in old.items() if e.get("pulled") and not (new.get(k) or {}).get("pulled")}:
            if tid and tid not in still:
                self._unpull_check(tid)
        if new or old:
            db.set_kv(jobuse.KV, new)

    def watch_progress(self, every_s: float = 60) -> None:
        """Drivers' reports (`ttp progress report`, progress.py), model-free, every minute: a key whose
        last waiting.progress.stall_after reports are refusals, stalls or failures with no result raises
        one keyed observation to the coordinator with the reasons, once per streak (kv progress_raised:
        key -> the streak's first report). A result ends the streak; a new streak raises again."""
        now = time.time()
        if now - getattr(self, "_progress_read", 0.0) < every_s:
            return
        self._progress_read = now
        s = progress.settings(self.cfg)
        db = self.p.db
        raised = db.kv(PROGRESS_KEY, {}) or {}
        live = {k["key"]: k for k in progress.stalled_keys(progress.entries(self.p.state), s["stall_after"],
                                                           float(s["window_s"]), now)}
        keep = {}
        for key, k in live.items():
            first = round(float(k["first"].get("ts") or 0), 3)
            keep[key] = first
            if raised.get(key) == first:
                continue
            reasons = []
            for r in (k["first"], k["last"]):
                if (r.get("reason") or "") not in reasons:
                    reasons.append(r.get("reason") or "")
            text = (f"Driver {key} made no progress: {progress.describe(k)} (since "
                    f"{timefmt.long(first, self.p)}; {k['last'].get('source') or 'source unknown'}). "
                    f"First reason: {reasons[0] or '(none)'}. Fix its cause or stop it; a looping driver that "
                    f"only retries the same refusal wastes its slot.")
            self.observe("daemon:progress", text, "normal", key=f"progress:{key}", episode=str(first), whole=True)
            log(self.p, f"driver {key}: {k['bad']} reports with no result; observation raised")
        if keep != raised:
            db.set_kv(PROGRESS_KEY, keep)

    def _underuse_event(self, task: dict, entry: dict, now: float, s: dict) -> None:
        db, st = self.p.db, entry["st"]
        fp = f"job_underuse:{task['id']}:{entry['name']}:{st['under']:.0f}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        text = (f"#{task['id']} {task['title']}: its detached job {entry['name']} {st['why']} ({entry['line']}). "
                f"Its next check comes within {s['check_cap_s'] / 60:g} min. Decide: let it run, or have it "
                f"restarted with what it really uses so the cores or device go to other work.")
        # An active 'daemon:job_underuse' mute covering it: recorded and counted there, no coordinator wake.
        status = "muted" if scr.count_muted(db, "daemon:job_underuse", text, "normal", now) else "queued"
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", "job_underuse", fp, "normal", text, status, task["id"]))
        log(self.p, f"task {task['id']} job {entry['name']} under-used: {st['why']}")

    def _pull_check(self, tid: int, why: str, now: float, s: dict) -> bool:
        """Bring a waiting task's next check to at most check_cap_s away; that check wakes a worker.
        False while the task is not waiting on a timer (running: its hand-off is still to come)."""
        db = self.p.db
        with db.tx():
            t = db.task(tid) or {}
            prev = load_result(t.get("result"))
            if t.get("status") != "queued" or prev.get("status") != "waiting" or (t.get("not_before") or 0) <= now:
                return False
            nb = min(float(t["not_before"]), now + s["check_cap_s"])
            db.update_task(tid, not_before=nb, result=dump_result({**prev, "underuse_wake": why[:300]}))
        log(self.p, f"task {tid} next check pulled in to {timefmt.short(nb, self.p)}: its job under-uses its request")
        return True

    def _unpull_check(self, tid: int) -> None:
        db = self.p.db
        with db.tx():
            prev = load_result((db.task(tid) or {}).get("result"))
            if "underuse_wake" not in prev:
                return
            prev.pop("underuse_wake")
            db.update_task(tid, result=dump_result(prev))
        log(self.p, f"task {tid} job use recovered before its pulled-in check: that check follows its probe")

    def _wait_what(self, tid: int, prev: dict) -> str:
        """What a waiting task waits for, as its note says it. A wait on `ttp lock --probe` commands names
        each lock as that probe answers now (lockprobe.answer: live holders with pid and liveness, a done
        task's hold marked stale), never the holders its hand-off named, which may have finished since; any
        other commands of its probe follow as they are. A wait on any other probe, that probe's latest line
        and when it was read, then the hand-off's words labelled as the worker's note from its time
        (waitheal.live_note; with no line, those words marked as possibly stale). A wait with no probe, its
        hand-off's words. Either way with the facts found about the tasks and asks they name (refwaits), and
        for a reply wait whose peer cannot answer now, that the peer is recovering (ackwait.peer_recovering)."""
        found, rest = locks.lock_probe_parts(str(prev.get("retry_when") or ""))
        if found:
            try:   # an unwritable or corrupt shared lock dir must not break a hand-off or a tick
                return lockprobe.wait_text(self.p, self.p.config(), found, rest)
            except Exception as e:
                cause = f"{type(e).__name__}: {e}"
                if cause not in self._wait_what_errs:   # once per cause, not every tick
                    self._wait_what_errs.add(cause)
                    log(self.p, f"task {tid}: lock probe for its wait note failed ({cause[:200]}); "
                                f"its note uses its hand-off's words")
                said = str(prev.get("waiting_for") or prev.get("summary") or "")
                names = ", ".join(r for r, _ in found)
                return (said + ("; " if said else "") + f"locks: {names}")[:300]
        said = refwaits.with_facts(str(prev.get("waiting_for") or prev.get("summary") or "")[:300], prev)
        probe = str(prev.get("retry_when") or "")
        if not probe.strip():
            return said
        peer, why = ackwait.peer_recovering(probe, self.cfg, self.p.harness)
        if peer:
            said = f"{said} (peer {peer} recovering: {why})"
        line, read, by = self._probe_live.get(tid) or ("", None, "")
        return waitheal.live_note(said, prev.get("waiting_since"), line if by == probe else "", read, self.p)[:400]

    def _stale_wait_event(self, task: dict, prev: dict, since: float, max_hold: float) -> None:
        """Once per wait: its probe has said "not yet" for `max_hold_s`. The task keeps sleeping."""
        db = self.p.db
        fp = f"wait_stale:{task['id']}:{since:.0f}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        what = self._wait_what(task["id"], prev)
        probe = str(prev.get("retry_when") or "")
        got = self._probe_out.get(task["id"])
        last = got[0] if got and got[2] == probe else ""
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "daemon", "wait_stale", fp, "normal",
              f"#{task['id']} {task['title']} has waited {max_hold / 3600:g} h for {what}; its probe still says "
              f"not yet. Blocking gate: {probe[:200]}; last output: {last or '(none seen)'}. "
              f"It keeps sleeping and no worker run is spent. "
              + (f"Note: {end}. " if (end := waitheal.probe_flag(probe)) else "")
              + f"Decide: fix its probe (`ttp task set-when {task['id']} \"<cmd>\"`), clear it (\"\") so it wakes "
              f"at its timer, or cancel the task.", "queued", task["id"]))
        log(self.p, f"task {task['id']} probe not yet after {max_hold / 3600:g} h; raised to the coordinator")

    def _wake_waiting(self, task: dict, why: str, now: float, extra: dict | None = None) -> None:
        """Make a waiting task due now and tell its next run why it woke."""
        tid = task["id"]
        got = self._probe_rc.pop(tid, None)
        if got:
            self._woke_on[tid] = got
        prev = load_result(task["result"])
        self.p.db.update_task(tid, not_before=min(task["not_before"] or now, now),
                              result=dump_result({**prev, **(extra or {}), "woke": why}))
        gate = self.gates.get(task["provider"] or self.cfg.get("core_provider", "claude"))
        held = gate and (not gate.allow_new_work or (task["origin"] in ("schedule", "harness")
                                                      and not gate.allow_optional))
        log(self.p, f"task {tid} retry_when {why}; {f'held by gate {gate.level}' if held else 'dispatching'}")

    def _spec_wake(self, t: dict, prev: dict, probe: str, now: float) -> bool:
        """A task asleep on its retry_when whose spec changed since its waiting hand-off (a task_update by
        the coordinator or the user; a worker writes no spec) wakes once, so its worker reads the new spec
        now, not once the probe passes. The hand-off keeps the digest of the spec it slept on; the wake
        stores the new one, so several edits between ticks wake it once and a re-sleep on that spec stays
        asleep. Another hold keeps it asleep with the old digest (_spec_wake_hold): it wakes once that hold
        is over, if the probe has not woken it first. At most SPEC_WAKES_PER_H an hour. True when it woke."""
        from .prompts import spec_digest
        seen, sha, tid = prev.get(SPEC_SEEN_KEY), spec_digest(t), t["id"]
        if seen == sha:
            return False
        if not isinstance(seen, str):   # a hand-off from before the digest was kept: this spec is its baseline
            self.p.db.update_task(tid, result=dump_result({**prev, SPEC_SEEN_KEY: sha}))
            return False
        wakes = _recent_spec_wakes(prev, now)
        if len(wakes) >= SPEC_WAKES_PER_H:
            if tid not in self._spec_capped:
                self._spec_capped.add(tid)
                log(self.p, f"task {tid}: its spec changed, but it woke {len(wakes)} times in the last hour "
                            f"on spec changes; it sleeps on its retry_when until that falls under {SPEC_WAKES_PER_H}")
            return False
        try:
            held = self._spec_wake_hold(t, probe, now)
        except Exception as e:   # an unreadable hold keeps it asleep: its probe and timer still wake it
            held = f"its holds could not be read ({type(e).__name__}: {e})"
        if held:
            return False
        self._spec_capped.discard(tid)
        self._wake_waiting(t, "its spec changed", now, extra={SPEC_SEEN_KEY: sha, SPEC_WAKES_KEY: wakes + [now]})
        self.p.db.update_task(tid, blocked_reason="woken: its spec changed; it runs by rank, before its probe passes")
        return True

    def _spec_wake_hold(self, t: dict, probe: str, now: float) -> str | None:
        """What keeps a waiting task asleep through a spec change, or None: an unmet dependency, a
        start_after still ahead, a wait only on `ttp lock --probe` (a busy resource), or what dispatch
        itself would hold it on (_start_hold: a paused or busy resource, a logged-out or unreachable
        provider, the budget gate, its own budget, the caps, a branch clash). A full worker slot holds
        nothing: the woken task waits its turn by rank."""
        db = self.p.db
        if db.unmet_dependencies([t]).get(t["id"]):
            return "an unmet dependency"
        after = deferral(t).get("after")
        if after and after > now:
            return "its start_after"
        found, rest = locks.lock_probe_parts(probe)
        if found and not rest:
            return "a busy resource"
        prov = t["provider"] or self.cfg.get("core_provider") or "claude"
        busy = {r["provider"]: r["n"] for r in db.q(
            "SELECT provider, COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running' GROUP BY provider")}
        ctx = {"now": now, "paused": db.paused_resources(), "busy": busy, "committed": None,
               "logged_out": {prov: self._logged_out(prov)}}
        why = self._start_hold(t, prov, ctx)
        if why == "device" or why == "gate" and t["id"] in ctx.get("full", {}):
            return None
        return why

    def _probe_passed(self, task: dict, now: float, why: str = "probe passed") -> None:
        """A waiting task's probe passed. A wait on its own detached `ttp checks` is settled here
        without a model run where the outcome is clear: checks that passed on the head still checked
        out record the hand-off's `on_pass` through the normal finish (review and push queue as
        usual); checks that failed only on tests failing elsewhere too are held (_shared_hold); others
        that failed or were killed wake the task at once at its own tier with their output's tail.
        Anything unclear wakes it as any passed probe does. A hand-off that also names an on_pass_cmd
        runs that command first (_start_on_pass): its on_pass counts only once the command exits 0.
        `why` is what passed (a freed exclusive slot, for _slot_reservations). An explicit start_after
        still ahead wins: the task sleeps until then, and the pass is acted on once it is over
        (_hold_waiting, with a fresh answer)."""
        after = deferral(task).get("after")
        if after and after > now:
            if (task["not_before"] or 0) < after:
                self.p.db.update_task(task["id"], not_before=after)
            return
        try:
            got = self._checks_wait(task)
        except Exception as e:   # a wake is always safe; a broken check must not keep the task asleep
            log(self.p, f"task {task['id']}: could not read its checks: {type(e).__name__}: {e}")
            got = None
        if got and got[0] == "pass" and load_result(task["result"]).get("on_pass_cmd") is not None:
            # Its on_pass_cmd (a publish step, say) takes precedence: on_pass is recorded only once
            # that command exits 0 without writing a hand-off of its own; a failure wakes the task.
            _, r, run_dir, on_pass, head = got
            try:
                started = self._start_on_pass(task, now, fallback=on_pass)
            except Exception as e:   # a wake is always safe
                started = f"it could not start: {type(e).__name__}: {e}"
            if started is True:
                return
            self._wake_waiting(task, f"{why}; its checks passed but its on_pass_cmd was not run: {started}", now)
            return
        if got and got[0] == "pass":
            _, r, run_dir, on_pass, head = got
            from .providers.base import RunUsage
            self._probe_rc.pop(task["id"], None)
            self._prefetch_remote(task["id"], on_pass, run_dir)
            with self.p.db.tx():
                self._finish_worker(r, RunUsage(), "ok", run_dir, "model-free", handoff=on_pass)
            log(self.p, f"task {task['id']} retry_when probe passed; its checks passed on {head[:12]}: "
                        f"recorded its on_pass ({on_pass['status']}) model-free")
            return
        if got and got[0] == "fail":
            _, rc, tail, run_dir = got
            try:
                if self._shared_hold(task, rc, run_dir, now):
                    return
            except Exception as e:   # a wake is always safe
                log(self.p, f"task {task['id']}: could not classify its checks failure: {type(e).__name__}: {e}")
            tier = task["tier"] if task["tier"] in bud.TIER_ORDER else "standard"
            self._wake_waiting(task, f"its checks failed ({'killed' if rc == 'killed' else f'exit {rc}'}); "
                                     f"the end of checks.out:\n{tail}", now,
                               extra={"wake_tier": tier, "escalated_wake": True})
            return
        if got:
            why += f"; its on_pass was not applied: {got[1]}"
        else:
            try:
                started = self._start_on_pass(task, now, said=why)
            except Exception as e:   # as above: a wake is always safe
                started = f"it could not start: {type(e).__name__}: {e}"
            if started is True:
                return
            if started:
                why += f"; its on_pass_cmd was not run: {started}"
        more, extra = self._push_landing(task)
        self._wake_waiting(task, why + more, now, extra=extra)

    def _push_finished_unpushed(self, task: dict, now: float) -> None:
        """Its `ttp push --result <marker> --pushed` probe exited 3: the push finished without landing,
        a final result and not a broken probe. It wakes at the task's own tier unless there was nothing
        to push."""
        why, extra = self._push_landing(task)
        self._wake_waiting(task, f"its push finished without pushing{why}", now, extra=extra)

    def _push_landing(self, task: dict) -> tuple[str, dict | None]:
        """For a wake of a task waiting on its push's result: the words to add and the hand-off fields
        to set. A push that did not land needs real work, as failed checks do: it wakes at the task's
        own tier; anything else ("", None) wakes as any passed probe does."""
        try:
            unpushed = self._push_unpushed(task)
        except Exception as e:   # a wake is always safe
            log(self.p, f"task {task['id']}: could not read its push: {type(e).__name__}: {e}")
            unpushed = ""
        if not unpushed:
            return "", None
        return f"; its push did not land: {unpushed}", {
            "wake_tier": task["tier"] if task["tier"] in bud.TIER_ORDER else "standard", "escalated_wake": True}

    def _push_unpushed(self, task: dict) -> str:
        """Why the push a waiting task's `ttp push --result <marker>` probe waits on did not land, or ''."""
        got = bud.result_wait(load_result(task["result"]).get("retry_when"))
        if not got or got[0] != "push":
            return ""
        marker = Path(got[1]).expanduser()
        return push.unpushed(marker if marker.is_absolute() else self.p.root / marker)

    def _shared_hold(self, task: dict, rc: str, run_dir: Path, now: float) -> bool:
        """Its checks failed only on pytest ids that also fail in other tasks' checks or on the push
        queue's known-bad tip (sharedchecks): keep the task asleep without a model run, counted in one
        incident with one fix task. While held, its checks run again on the same head once the push
        branch's tip moves past a commit touching a failing test's file, or after the backoff; after
        RERUNS_MAX reruns it wakes. False: wake it as before (another runner, an id not shared...).
        No new incident while one owns the ids with a fix live or on its way to the push branch
        (sharedchecks.owner); with none, the failing ids are first rerun on the push-branch tip
        (_tip_gate): passing there, no fix task is added and the task wakes to rebase, or to fix its own
        change when its head already holds that tip."""
        from .cli import _read_checks_pid
        db, tid = self.p.db, task["id"]
        if rc == "killed":
            return False
        try:
            fail = json.loads((run_dir / "check_failure.json").read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(fail, dict) or fail.get("kind") != "check_failed" or not isinstance(fail.get("head"), str):
            return False
        head, out = fail["head"], str(fail.get("output") or "")
        ids, complete = sharedchecks.failing_ids(out)
        if not head or not ids or not complete:
            return False
        started = _read_checks_pid(run_dir).get("ts")
        prev = load_result(task["result"])
        hold = prev.get("shared_hold") if isinstance(prev.get("shared_hold"), dict) else {}
        if hold.get("run_dir") != str(run_dir) or hold.get("head") != head:
            hold = {}
        tip = self._push_tip()
        tipped = False   # its ids' rerun on the tip ended without passing: open the incident
        if hold and hold.get("started") == started and hold.get("tip_check"):   # waiting on the tip's rerun
            v = self._tip_result(hold["tip_check"], now)
            if v == "pending":
                if (task["not_before"] or 0) <= now + PROBE_EVERY_S:
                    db.update_task(tid, not_before=now + 2 * PROBE_EVERY_S)
                return True
            if v == "pass":
                return self._tip_passed(task, ids, str(hold.get("tip") or ""), now, bool(hold.get("holds_tip")))
            tipped, hold = True, {k: v for k, v in hold.items() if k != "tip_check"}
        elif hold and hold.get("started") == started:   # the failure it was held on: release or sleep on
            moved = bool(tip and hold.get("tip") and tip != hold["tip"]
                          and pushq.touches(self.p, hold["tip"], tip, sharedchecks.files(hold.get("ids") or ids)))
            if not moved and now < float(hold.get("until") or 0):
                if (task["not_before"] or 0) <= now + PROBE_EVERY_S:
                    db.update_task(tid, not_before=float(hold["until"]) + 2 * PROBE_EVERY_S)
                return True
            why = self._restart_checks(tid, run_dir, _read_checks_pid(run_dir), f"shared-t{tid}")
            if why:
                log(self.p, f"task {tid}: held on a shared checks failure; its checks not run again: {why}")
                return False
            self._probe_rc.pop(tid, None)
            hold = {**hold, "reruns": int(hold.get("reruns") or 0) + 1, "rerun_at": now}
            db.update_task(tid, not_before=now + max(_retry_s(prev), 3600.0),
                           result=dump_result({**prev, "shared_hold": hold}))
            log(self.p, f"task {tid}: shared checks failure, " + ("the push-branch tip changed its failing tests"
                        if moved else "backoff over") + f"; its checks run again on {head[:12]}, no model run")
            return True
        sigs = sharedchecks.failure_sigs(out, ids)
        sharedchecks.record(db, tid, head, ids, now, sigs)
        if int(hold.get("reruns") or 0) >= sharedchecks.RERUNS_MAX:
            return False
        if not sharedchecks.shared(db, tid, head, ids, now, self.cfg, pushq.bad_tip_ids(db), sigs):
            return False
        if not tipped and sharedchecks.owner(db, ids, now) is None:
            gate = self._tip_gate(task, prev, {"run_dir": str(run_dir), "head": head, "ids": ids, "started": started,
                                               "tip": tip, "since": now, "reruns": int(hold.get("reruns") or 0)}, now)
            if gate is not None:
                return gate
        tier = task["tier"] if task["tier"] in bud.TIER_ORDER else "standard"
        with db.tx():
            iid, opened = sharedchecks.incident(db, task, ids, out, now, self.cfg)
            sharedchecks.avoided(db, iid, tid, sharedchecks.wake_usd(db, tier, now))
            until = now + sharedchecks.settings(self.cfg)["backoff_s"]
            db.update_task(tid, not_before=until + 2 * PROBE_EVERY_S, result=dump_result({**prev, "shared_hold": {
                "run_dir": str(run_dir), "head": head, "ids": ids, "incident": iid, "started": started,
                "tip": tip, "since": now, "until": until, "reruns": int(hold.get("reruns") or 0)}}))
        log(self.p, f"task {tid}: its checks failed on {len(ids)} test(s) failing in other tasks too "
                    f"(incident {iid}{', opened' if opened else ''}); held without a model run")
        return True

    def _tip_gate(self, task: dict, prev: dict, hold: dict, now: float) -> bool | None:
        """A shared failure no incident owns: rerun only its failing ids on the push-branch tip before a
        fix task is added (sharedchecks.tip_verdict). True: held while that rerun runs, or woken since
        they pass there (to rebase, or to fix its own change when its head already holds the tip: a
        budget or guard test each change trips with its own code fails the same way on every head).
        None: open the incident as before (no tip, the rerun could not start, failed there or proved
        nothing)."""
        tid, tip, head, ids = task["id"], hold["tip"], hold["head"], hold["ids"]
        if not tip:
            return None
        try:
            holds = worktree.is_ancestor(self.p, tip, head)
        except (OSError, subprocess.SubprocessError):
            return None
        hold = {**hold, "holds_tip": bool(holds)}
        key = sharedchecks.tip_key(tip, ids)
        v = self._tip_result(key, now)
        if v is None and not (self.p.db.kv(sharedchecks.TIP_KEY) or {}).get(key):
            if not self._start_tip_check(tip, ids, key):
                return None
            stale = sharedchecks.tip_started(self.p.db, key, tip, ids, now)
            for old in stale:
                shutil.rmtree(self._tip_dir(old), ignore_errors=True)
            if stale:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    worktree._git(self.p.root, "worktree", "prune", check=False)
            v = "pending"
        if v == "pass":
            return self._tip_passed(task, ids, tip, now, bool(holds))
        if v != "pending":
            return None
        until = now + sharedchecks.settings(self.cfg)["tip_timeout_s"]
        self.p.db.update_task(tid, not_before=now + 2 * PROBE_EVERY_S, result=dump_result(
            {**prev, "shared_hold": {**hold, "tip_check": key, "until": until}}))
        log(self.p, f"task {tid}: its checks failed on {len(ids)} test(s) failing in other tasks too and no fix "
                    f"owns them; held while they rerun on the push-branch tip {tip[:12]}")
        return True

    def _tip_passed(self, task: dict, ids: list[str], tip: str, now: float, holds_tip: bool = False) -> bool:
        """Its failing ids pass on the push-branch tip: no fix task; wake it to rebase and rerun its checks,
        or, when its head already holds that tip, to fix what its own change broke."""
        log(self.p, f"task {task['id']}: its {len(ids)} failing test(s) pass on the push-branch tip {tip[:12]}; "
                    + ("its head holds that tip, so its own change fails them; no fix task, woken to fix it"
                       if holds_tip else "no fix task, woken to rebase onto it and rerun its checks"))
        todo = ("which your head already holds, so your own change fails them: fix it and run your checks again"
                if holds_tip else "rebase onto the tip and run your checks again")
        self._wake_waiting(task, f"its checks failed on {len(ids)} test(s) ({', '.join(ids[:3])}"
                                 f"{', ...' if len(ids) > 3 else ''}) that pass on the push-branch tip {tip[:12]}"
                                 f"{', ' if holds_tip else ': '}{todo}; no fix task was added", now,
                           extra={"wake_tier": task["tier"] if task["tier"] in bud.TIER_ORDER else "standard",
                                  "shared_hold": {}})
        return True

    def _tip_dir(self, key: str) -> Path:
        return self.p.state / "shared-tip" / key

    def _tip_result(self, key: str, now: float) -> str | None:
        try:
            rc = (self._tip_dir(key) / "rc").read_text()
        except OSError:
            rc = None
        return sharedchecks.tip_verdict(self.p.db, key, rc, now, self.cfg)

    def _start_tip_check(self, tip: str, ids: list[str], key: str) -> bool:
        """Rerun only `ids` on the push-branch tip `tip`, model-free and detached: in a worktree of its own
        under state/shared-tip/<key>, `python3 -m pytest -q <ids>` run as a check is (push.check_argv: the
        project's venv, in the process sandbox), niced like workers, devices masked, killed past checks.shared_tip_timeout_s. Its exit code goes to rc there
        and the worktree is removed once it ends. False when it could not start."""
        d = self._tip_dir(key)
        wt = d / "wt"
        try:
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
            worktree._git(self.p.root, "worktree", "add", "--detach", str(wt), tip,
                          timeout=worktree.git_timeout(self.p), env=worktree.checkout_env(self.p))
        except Exception as e:
            log(self.p, f"shared checks: the tip {tip[:12]} could not be checked out to rerun {len(ids)} test(s): {e}")
            return False
        path = os.environ.get("PATH", "")
        venv = worktree.project_venv(self.p, wt)
        env = machines.mask_devices({**os.environ, **(worktree.venv_env(venv, path) if venv else {}),
                                     "TTP_NO_DEVICE": "1"})
        level = nice_level(self.cfg.get("runner"))[0]
        cmd = push.check_argv("python3 -m pytest -q " + shlex.join(ids), top=wt, env=env)
        argv = [sys.executable, "-c", TIP_RUNNER, str(d), str(wt), str(self.p.root),
                str(sharedchecks.settings(self.cfg)["tip_timeout_s"]),
                *(["nice", "-n", str(level)] if level else []), *cmd]
        try:
            subprocess.Popen(argv, cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log(self.p, f"shared checks: the rerun of {len(ids)} test(s) on the tip {tip[:12]} did not start: {e}")
            worktree._git(self.p.root, "worktree", "remove", "--force", str(wt), check=False)
            return False
        log(self.p, f"shared checks: rerunning {len(ids)} failing test(s) on the push-branch tip {tip[:12]}")
        return True

    def _push_tip(self) -> str:
        """The push branch's tip as last fetched ('' without one)."""
        t = pushq.target(self.p)
        try:
            return pushq._tracking_tip(self.p, *t) if t else ""
        except Exception:
            return ""

    def _dry_run_verdict(self, r: dict, task: dict, result: dict, run_dir: Path, extra: dict,
                         summary: str) -> tuple[dict, str]:
        """What a waiting hand-off's on_pass_cmd dry run (_dry_run_on_pass) does to it, in `extra`: a
        crash hands the run back to its own session at once (onpassdry.KEY; once per hand-off), or,
        where that cannot be, keeps the hand-off with a warning in its summary and the task's notes; a
        timeout or other non-zero exit records 'dry run inconclusive'. Returns the result and summary."""
        got = onpassdry.load(run_dir, result.get("on_pass_cmd"))
        if not got or got.get("verdict") == "ok":
            return result, summary
        if got.get("verdict") != "crash":
            extra[onpassdry.NOTE_KEY] = onpassdry.inconclusive(got)
            return result, summary
        if got.get("session") and not load_result(task["result"]).get(onpassdry.KEY):
            text = onpassdry.handback_text(got)
            wake = (json.loads(r["note"] or "{}").get("wake") or {}).get("tier")
            tier = wake if wake in bud.TIER_ORDER else task["tier"] if task["tier"] in bud.TIER_ORDER else "standard"
            extra.pop("waiting_since", None)
            extra.update({onpassdry.KEY: {"run": r["id"], "session": got["session"], "text": text},
                          "woke": text, "escalated_wake": True})
            log(self.p, f"task {task['id']} on_pass_cmd dry run crashed; handed back to session {got['session']}")
            return {**result, "wake_tier": tier}, summary
        warn = onpassdry.warning(got)
        extra[onpassdry.NOTE_KEY] = warn
        _task_note(run_dir, warn)
        log(self.p, f"task {task['id']} {warn}")
        return result, f"{warn}\n{summary}"[:1500]

    def _job_failed(self, task: dict, now: float) -> None:
        """A waiting task's `ttp detach --check` found its jobs ended, but one failed or left no exit
        code (detach.FAILED_RC): never a passed probe. The hand-off's on_pass_cmd, when it names one,
        decides model-free (_start_on_pass); else, or when it cannot run, the task wakes at once at
        its own tier with the check's output (each job's `job finished rc=N`) and each failed job's log
        tail and first error line (detach.failed_logs)."""
        out = (self._probe_out.get(task["id"]) or ("",))[0].strip()[-1500:] or "(no output)"
        why = f"{JOB_FAILED}:\n{out}"
        try:
            started = self._start_on_pass(task, now, said=JOB_FAILED)
        except Exception as e:   # a wake is always safe
            started = f"it could not start: {type(e).__name__}: {e}"
        if started is True:
            return
        if started:
            why += f"\nits on_pass_cmd was not run: {started}"
        try:   # the failed job's log tail and first error line, so the run need not look for them
            logs = detach.failed_logs(str(load_result(task["result"]).get("retry_when") or ""), self.p.root)
        except Exception as e:
            logs = f"its job logs could not be read: {type(e).__name__}: {e}"[:300]
        if logs:
            why += f"\n{logs}"
        tier = task["tier"] if task["tier"] in bud.TIER_ORDER else "standard"
        self._wake_waiting(task, why, now, extra={"wake_tier": tier, "escalated_wake": True})

    def _start_on_pass(self, task: dict, now: float, said: str = "probe passed",
                       fallback: dict | None = None) -> bool | str | None:
        """A passed probe of a hand-off naming an `on_pass_cmd`: run that command model-free from the
        project root with the owner's HOME (command_env), only once the run that named it has ended,
        in the background, and keep the task asleep while it runs (_poll_on_pass settles it). True when
        it runs (or already ran from an earlier pass of the same probe, or waits for its run to end),
        None when the hand-off names none, else why it was not run. `said` is what the probe found
        (JOB_FAILED for a detached job that failed): the log and the wake say it. `fallback` (the
        hand-off's on_pass, once its checks passed) is recorded when the command exits 0 without
        writing a hand-off to $TTP_RESULT."""
        tid = task["id"]
        if tid in self._on_pass:
            return True
        prev = load_result(task["result"])
        cmd = prev.get("on_pass_cmd")
        if task["status"] != "queued" or prev.get("status") != "waiting" or cmd is None:
            return None
        if not isinstance(cmd, str) or not cmd.strip():
            return "on_pass_cmd is not a command"
        r = self.p.db.one("SELECT * FROM runs WHERE task=? AND role!='coordinator' AND dir IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1", (tid,))
        full = _read_result(Path(r["dir"]) / RESULT_FILE) if r and r["dir"] else None
        if not full or full.get("status") != "waiting" or not _same_probe(full, prev, "on_pass_cmd") \
                or not _same_probe(full, prev):
            return "the task's last run's result.json is not the hand-off the task waits on"
        if r["status"] == "running":
            # Only after its run ended and wrote its outputs; a later pass of the probe runs it.
            # Asleep meanwhile: an expired timer must not leave it ready for a model run.
            self.p.db.update_task(tid, not_before=max(task["not_before"] or 0, now + PROBE_EVERY_S))
            if (tid, r["id"]) not in self._on_pass_waits:
                self._on_pass_waits.add((tid, r["id"]))
                log(self.p, f"task {tid} on_pass_cmd waits for run {r['id']} to finish")
            return True
        # Notes an earlier try queued (its daemon restarted before settling it) go out before this one.
        self._file_queued_notes(r["id"], tid, Path(r["dir"]))
        out = Path(r["dir"]) / ON_PASS_DIR
        shutil.rmtree(out, ignore_errors=True)   # a result from an earlier try must not count
        out.mkdir(parents=True)
        timeout = float((self.cfg.get("waiting") or {}).get("on_pass_timeout_s") or ON_PASS_TIMEOUT_S)
        # Keep deferred notes in the original run, where _finish_worker files them, rather than the
        # disposable on_pass output folder.
        env = self._on_pass_env(tid)
        env.update(TTP_NOTE_RUN_DIR=str(r["dir"]), TTP_RESULT=str(out / RESULT_FILE))
        with open(out / ON_PASS_LOG, "wb") as f:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(self.p.root), stdin=subprocess.DEVNULL, stdout=f,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    env=env)
        where = f"HOME={env.get('HOME', '(unset)')}, cwd={self.p.root}"
        self._on_pass[tid] = (proc, now, timeout, out, r["id"], cmd, where, said, fallback)
        # Asleep while it runs: its timer must not start a model run meanwhile.
        self.p.db.update_task(tid, not_before=max(task["not_before"] or 0, now + timeout + PROBE_TIMEOUT_S),
                              blocked_reason=f"its {said}; running its on_pass_cmd model-free "
                                             f"(up to {timeout / 60:g} min)")
        log(self.p, f"task {tid} retry_when {said}; running its on_pass_cmd model-free (pid {proc.pid})")
        return True

    def _poll_on_pass(self, now: float) -> None:
        """Settle finished (or overdue) on_pass commands. Exit 0 with a result.json at $TTP_RESULT that
        says done or needs_review with a summary is the task's hand-off, recorded through the normal
        finish (review and push queue as usual) without a model run. A non-zero exit or a timeout wakes
        the task at once at its own tier; anything else wakes it as any passed probe does. Either way
        the wake carries the command's exit and the end of its output."""
        db = self.p.db
        for tid, (proc, started, timeout, out, run_id, cmd, where, said, fallback) in list(self._on_pass.items()):
            rc = proc.poll()
            if rc is None and now - started < timeout:
                continue
            del self._on_pass[tid]
            if rc is None:
                _kill_group(proc)
            r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
            if r and r["dir"]:
                # Its notes were acknowledged when sent: they go out whatever it ends with.
                self._file_queued_notes(run_id, tid, Path(r["dir"]))
                try:   # and its obligation records, while its task is still queued (file_runs reads those)
                    for line in obligations.file_runs(self.p, now):
                        log(self.p, f"obligation record refused: {line}")
                except Exception:
                    log(self.p, "obligations: filing an on_pass_cmd's records failed\n" + traceback.format_exc())
            task = db.task(tid)
            prev = load_result(task["result"]) if task else {}
            if not task or task["status"] != "queued" or prev.get("status") != "waiting" \
                    or prev.get("on_pass_cmd") != cmd or prev.get("woke"):
                continue   # cancelled, re-planned or woken meanwhile: this result is not its hand-off
            try:
                tail = file_tail(out / ON_PASS_LOG) or "(no output)"
            except OSError:
                tail = "(no output)"
            result = _read_result(out / RESULT_FILE)
            if rc == 0 and result is None and fallback is not None:
                result = fallback   # its checks passed and the command succeeded: the hand-off's on_pass
            if isinstance(result, dict) and r and r["dir"]:
                waited = _read_result(Path(r["dir"]) / RESULT_FILE) or {}
                result = with_waiting_followups(result, waited if _same_probe(waited, prev) else prev)
            if rc is None:
                why = f"timed out after {timeout / 60:g} min"
            elif rc != 0:
                why = f"exit {rc}"
            elif not isinstance(result, dict):
                why = "exit 0 but no result.json at $TTP_RESULT"
            elif result.get("status") not in ("done", "needs_review"):
                why = f"its result.json says {str(result.get('status'))[:40]!r}, not done or needs_review"
            else:
                why = on_pass_problem(result)
            if not why and r:
                from .providers.base import RunUsage
                self._probe_rc.pop(tid, None)
                self._prefetch_remote(tid, result, Path(r["dir"]))
                with db.tx():
                    self._finish_worker(r, RunUsage(), "ok", Path(r["dir"]), "model-free", handoff=result)
                log(self.p, f"task {tid} on_pass_cmd recorded {result['status']} model-free")
                continue
            # Self-explaining: what it ran with goes next to its exit and output.
            log(self.p, f"task {tid} on_pass_cmd did not record done: {why or 'its run is gone'} "
                        f"(ran with {where}); the end of its output:\n{tail}")
            # A command that failed or overran is a real failure a light wake could only pass on:
            # wake at the task's own tier, as failed checks do.
            # So is a detached job that failed.
            failed = rc is None or rc != 0 or said == JOB_FAILED
            tier = task["tier"] if task["tier"] in bud.TIER_ORDER else "standard"
            self._wake_waiting(task, f"{said}; its on_pass_cmd did not record done "
                                     f"({why or 'its run is gone'}; ran with {where}); "
                                     f"the end of its output:\n{tail}", now,
                               extra={"wake_tier": tier, "escalated_wake": True} if failed else None)

    def _sweep_on_pass_notes(self, now: float) -> None:
        """File the notes an on_pass_cmd queued that no settle filed (a daemon restarted while it ran,
        and its task then moved on), once that command cannot still be running. At start, then every
        ON_PASS_NOTES_EVERY_S."""
        if now < self._on_pass_notes_due:
            return
        self._on_pass_notes_due = now + ON_PASS_NOTES_EVERY_S
        quiet = float((self.cfg.get("waiting") or {}).get("on_pass_timeout_s") or ON_PASS_TIMEOUT_S) + PROBE_TIMEOUT_S
        for r in self.p.db.q("SELECT id,task,dir FROM runs WHERE role!='coordinator' AND status!='running' "
                             "AND dir IS NOT NULL AND task IS NOT NULL AND started>=?",
                             (now - ON_PASS_NOTES_DAYS * 86400,)):
            d = Path(r["dir"])
            if r["task"] in self._on_pass or not (d / upstream.QUEUED_FILE).exists():
                continue
            try:
                if now - (d / ON_PASS_DIR / ON_PASS_LOG).stat().st_mtime < quiet:
                    continue
            except OSError:
                continue   # not an on_pass_cmd's: its run's finish files them
            self._file_queued_notes(r["id"], r["task"], d)

    def _checks_wait(self, task: dict) -> tuple | None:
        """What a passed `ttp checks --result <run dir>` probe of one of this task's runs settles:
        ("pass", run row, run dir, on_pass, head), ("fail", rc, tail), ("unclear", why), or None when
        the wait is not on such checks or they left no exit code. A "fail" also carries the run dir."""
        from .cli import CHECKS_OUT, CHECKS_RC
        prev = load_result(task["result"])
        if task["status"] != "queued" or prev.get("status") != "waiting":
            return None
        try:
            words = shlex.split(str(prev.get("retry_when") or ""))
        except ValueError:
            return None
        if len(words) != 4 or Path(words[0]).name != "ttp" or words[1:3] != ["checks", "--result"]:
            return None
        run_dir = Path(words[3])
        r = self.p.db.one("SELECT * FROM runs WHERE task=? AND dir=? AND role!='coordinator' ORDER BY id DESC LIMIT 1",
                          (task["id"], str(run_dir)))
        if not r:
            return None
        try:
            rc = (run_dir / CHECKS_RC).read_text().strip()
        except OSError:
            return None
        if rc == "killed" or re.fullmatch(r"-?[1-9][0-9]*", rc):
            try:
                lines = (run_dir / CHECKS_OUT).read_text(errors="replace").splitlines()[-20:]
            except OSError:
                lines = []
            return "fail", rc, "\n".join(lines)[-1500:] or "(no output)", run_dir
        if rc != "0":
            return None
        full = _read_result(run_dir / RESULT_FILE) or {}
        if "on_pass" not in full:
            return None
        if full.get("status") != "waiting" or not _same_probe(full, prev):
            return "unclear", "the run's result.json is not the hand-off the task waits on"
        problem = on_pass_problem(full["on_pass"])
        if problem:
            return "unclear", problem
        head = _checked_head(run_dir, task)
        if not head:
            try:
                fp = json.loads((run_dir / prguard.CHECKS_FILE).read_text()).get("fast_path")
            except (OSError, ValueError, AttributeError):
                fp = False
            if fp:
                return "unclear", ("the checks were a fast-path pass (docs/data-only diff; the project's checks did "
                                   "not run); run `ttp checks --full`")
            return "unclear", ("the checked commit is no longer the head of its worktree and branch, or it is not clean, "
                               "or its checks were a harness qualification (`ttp checks --purpose harness`)")
        return "pass", r, run_dir, with_waiting_followups(full["on_pass"], full), head

    def _on_pass_env(self, tid: int) -> dict:
        """The environment a task's on_pass_cmd runs with (and its dry run): the probes' (_probe_env), no
        run's own variables, the task's. Only `ttp note` reads this context. Other commands must not
        become worker commands, even when the daemon inherited a run's environment."""
        env = {k: v for k, v in self._probe_env().items() if k not in ("TTP_RUN_DIR", "TTP_RUN_ID")}
        env.update(task_env(tid))
        return env

    def _dry_run_on_pass(self, r: dict, run_dir: Path, session: str) -> None:
        """A run that handed off `waiting` with an on_pass_cmd: that command's dry run (onpassdry), now,
        before the transaction that records the hand-off reads it, with whether the run's agent session
        can be continued to fix a crash. Once per run and command; any error leaves the hand-off as is."""
        try:
            result = _read_result(run_dir / RESULT_FILE)
            cmd = result.get("on_pass_cmd") if isinstance(result, dict) else None
            if not isinstance(result, dict) or result.get("status") != "waiting" or not isinstance(cmd, str) \
                    or not cmd.strip() or not onpassdry.enabled(self.cfg) or onpassdry.load(run_dir, cmd):
                return
            if result.get("retry_when") is not None and not isinstance(result["retry_when"], str):
                return   # a retry_when that is no shell command is refused: its on_pass_cmd never runs
            got = onpassdry.run(cmd, self.p.root, self._on_pass_env(r["task"]), onpassdry.timeout_s(self.cfg))
            if got["verdict"] == "crash" and session:
                spec = _read_result(run_dir / "run.json") or {}
                cwd, env = str(spec.get("cwd") or ""), spec.get("env") if isinstance(spec.get("env"), dict) else {}
                prov = get_provider(r["provider"])
                if cwd and Path(cwd).is_dir() and prov.resume_args(session) and prov.session_saved(session, cwd, env):
                    got["session"] = session
            onpassdry.save(run_dir, got)
            log(self.p, f"task {r['task']} on_pass_cmd dry run: {got['verdict']} ({onpassdry.said(got)})")
        except Exception as e:
            log(self.p, f"task {r['task']}: the on_pass_cmd dry run could not run: {type(e).__name__}: {e}")

    def _probe_env(self) -> dict:
        """Probes and on_pass commands run `ttp` (e.g. `ttp lock --probe device-a`) as a worker would,
        devices masked as in its run. A `ttp landed` in them waits on a task being done where this
        project cannot check a landing (landed.DONE_ENV)."""
        return {**machines.mask_devices(command_env(self.p)), landed.DONE_ENV: "1"}

    def _start_failed(self, task: dict, e: Exception) -> None:
        """Nothing was launched, so no attempt is spent. The task waits a minute before the next try,
        so a lasting cause (a full disk, a broken install) cannot spin; three failures in a row alert."""
        db = self.p.db
        why = f"could not start a run: {type(e).__name__}: {e}"[:400]
        log(self.p, f"task {task['id']} {why}")
        with db.tx():
            db.update_task(task["id"], status="queued", not_before=time.time() + 60, blocked_reason=why)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), "daemon", "run_start_failed", "low", f"#{task['id']} {task['title']}: {why}",
                  "handled", task["id"]))
        self._start_failures += 1
        if self._start_failures >= 3:
            self.alert("run-start", f"Runs cannot start ({why}). Tasks stay queued and retry every minute.", "high")

    def _resources_free(self, task: dict, reserve: bool = False) -> bool:
        """Whether every resource ready `task` needs is free now (_resource_hold)."""
        return self._resource_hold(task, reserve) is None

    def _resource_hold(self, task: dict, reserve: bool = False) -> dict | None:
        """Only tasks labelled `exclusive:<name>` hold a resource for their whole run; they share
        its slot count (config `resources`, default 1; on a shared resource the smallest any project
        sharing it gives). A `resource:<name>` label means the task uses
        the resource for some commands: those take the resource's lock (`ttp lock`) or its own queue,
        so the rest of the task runs in parallel with other work instead of waiting for the slot.
        With reserve, a task kept out only by `ttp lock` commands reserves the resource so new ones
        wait; the reservation lapses unless the next dispatch refreshes it. The names in config
        `device.locks` are one resource with one slot.

        At most twice its slots run at once among the tasks that use a resource either way: more
        would only queue in `ttp lock` on a worker slot and a wall clock that other work could use.

        None when all are free, else the first hold found, as data (dispatch's recorded hold reason,
        which the stall check reads instead of working it out again): {"kind": "pause", "resource"}
        (paused, directly or through a device.locks alias), {"kind": "busy", "resource", "tasks"} (the
        running tasks that fill its slots) or {"kind": "lock", "resource", "holders"} (its `ttp lock`
        slots are taken: each holder's label, pid and alive, from locks.holder_records), or with config
        locks.pre_grant_check set, {"kind": "pre_grant", "resource", "rc", "line"} (the project's own
        check holds it, or is broken: pregrant.py)."""
        paused = self.p.db.paused_resources().keys()
        hit = sorted(coord.task_resources(task) & paused)
        if hit:
            return {"kind": "pause", "resource": hit[0]}
        if locks.device_locks(self.cfg):
            names = {locks.canonical(self.cfg, r) for r in paused}
            hit = sorted(r for r in coord.task_resources(task) if locks.canonical(self.cfg, r) in names)
            if hit:   # a pause of one device name holds the tasks that name another
                return {"kind": "pause", "resource": hit[0]}
        running = self.p.db.q("SELECT id, labels FROM tasks WHERE status='running'")
        reservers = self._slot_resv_pass if self._slot_resv_pass is not None else \
            (self._slot_reservers(time.time()) if _exclusive(task) else {})

        def _locks_of(names: list[str]) -> set[str]:
            return {locks.canonical(self.cfg, r) for r in names}

        for res in _shared(task):
            lock = locks.canonical(self.cfg, res)
            users = [t["id"] for t in running if lock in _locks_of(_shared(t) + _exclusive(t))]
            if len(users) >= 2 * shared.slots(self.p, lock, self.cfg):
                return {"kind": "busy", "resource": res, "tasks": users}
        for res in _exclusive(task):
            lock = locks.canonical(self.cfg, res)
            limit = shared.slots(self.p, lock, self.cfg)
            # Running exclusive tasks count even before their supervisor has taken its slot; the
            # lock files show the slots `ttp lock` commands hold.
            busy = [t["id"] for t in running if lock in _locks_of(_exclusive(t))]
            first = reservers.get(lock)
            if len(busy) >= limit:
                if reserve and first == task["id"]:
                    locks.reserve(self._reserve_path(res), shared.holder(self.p, res, hold.who(task['id']), self.cfg))
                return {"kind": "busy", "resource": res, "tasks": busy}
            # A reservation keeps one slot for its task: it is served once that task runs, and a slot
            # beyond the one it needs stays open to others.
            if first is not None and first != task["id"] and first not in busy and \
                    min(limit - len(busy), sum(locks.any_free([x]) for x in self._slot_paths(res))) < 2:
                return {"kind": "reserved", "resource": res, "task": first}
            paths = self._slot_paths(res)
            if not locks.any_free(paths):
                mine = shared.holder(self.p, res, hold.who(task['id']), self.cfg)
                if any(hold.own_keeper(lb, mine) for lb in locks.held_labels(paths)):
                    continue   # kept for this task's own detached jobs: its run takes the slot over
                if reserve:
                    locks.reserve(self._reserve_path(res), mine)
                try:
                    holders = [{"holder": h.get("holder"), "pid": h.get("pid"), "alive": h.get("alive")}
                               for h in locks.holder_records(paths)]
                except Exception:   # naming the holder never changes the hold
                    holders = []
                return {"kind": "lock", "resource": res, "holders": holders}
        # Asked last, once the slots would let it start: the project's own holds (pregrant.py).
        check = pregrant.command(self.cfg)
        for res in sorted(coord.task_resources(task)) if check else []:
            got = self._pre_grant(check, res, task["id"])
            if not got.granted:
                return {"kind": "pre_grant", "resource": res, "rc": got.rc, "line": got.line}
        return None

    def _pre_grant(self, check: str, res: str, tid: int) -> pregrant.Answer:
        """The pre-grant check's answer for a task and resource, reused for PRE_GRANT_REUSE_S: dispatch
        and the stall checks ask every tick, and the grant itself (the run's take, `ttp lock`) asks again.
        The checks run in the tick, so they are bounded there: a broken answer (a timeout, a bad exit)
        does not depend on the task and is reused for every task on the resource, and once a tick's
        checks took PRE_GRANT_TICK_S (or the check's timeout, if lower) it starts no more: the tasks not
        asked are held until a later tick asks, with nothing recorded or raised."""
        now = time.time()
        got = self._pre_grant_seen.get((res, tid))
        if got and now - got[0] < PRE_GRANT_REUSE_S:
            return got[1]
        got = self._pre_grant_broken.get(res)
        if got and now - got[0] < PRE_GRANT_REUSE_S:
            return got[1]
        timeout = pregrant.timeout_s(self.cfg)
        tick, spent = self._pre_grant_spent
        if not self._tick_began or tick != self._tick_began:
            spent = 0.0
        if spent >= min(PRE_GRANT_TICK_S, timeout):
            return pregrant.Answer(False, None, False, f"{res}: the pre-grant check is asked in a later tick")
        began = time.monotonic()
        ans = pregrant.check(check, self.p.root, res, tid, self.p.state, timeout)
        self._pre_grant_spent = (self._tick_began, spent + time.monotonic() - began)
        self._pre_grant_seen = {k: v for k, v in self._pre_grant_seen.items() if now - v[0] < PRE_GRANT_REUSE_S}
        self._pre_grant_seen[(res, tid)] = (now, ans)
        self._pre_grant_broken = {k: v for k, v in self._pre_grant_broken.items() if now - v[0] < PRE_GRANT_REUSE_S}
        if ans.broken:
            self._pre_grant_broken[res] = (now, ans)
        if not ans.granted:
            log(self.p, f"task {tid}: {ans.line}")
        return ans

    def raise_pre_grant_faults(self) -> None:
        """A broken pre-grant check (an exit other than 0, 1 or 75, or no answer in time), as dispatch, a
        run's take or `ttp lock` recorded it: one observation per resource and exit, raised again at most
        every PRE_GRANT_RERAISE_S, however often the held tasks retry meanwhile."""
        faults = pregrant.take_faults(self.p.state)
        if not faults:
            return
        db, now = self.p.db, time.time()
        raised = {k: v for k, v in (db.kv(PRE_GRANT_KEY, {}) or {}).items() if now - float(v) < PRE_GRANT_RERAISE_S}
        for rec in faults:
            key = pregrant.fault_key(rec)
            if key in raised:
                continue
            raised[key] = now
            self.observe("daemon:pre-grant", pregrant.fault_text(rec), "normal", key=key, whole=True)
        db.set_kv(PRE_GRANT_KEY, raised)

    def _device_max_tasks(self) -> int:
        try:
            return max(int((self.cfg.get("device") or {}).get("max_tasks", 2) or 1), 1)
        except (TypeError, ValueError):
            return 2

    def _device_tasks_running(self) -> int:
        """Running tasks tagged needs_device. They prepare in parallel; `ttp lock` admits one at a
        time to the device phase, in arrival order."""
        return sum(1 for t in self.p.db.q("SELECT labels FROM tasks WHERE status='running'")
                   if needs_device(t, self.cfg))

    def _slot_paths(self, res: str) -> list[Path]:
        lock = locks.canonical(self.cfg, res)
        return locks.slot_paths(shared.locks_dir(self.p, lock, self.cfg), lock, shared.slots(self.p, lock, self.cfg))

    def _reserve_path(self, res: str) -> Path:
        lock = locks.canonical(self.cfg, res)
        return locks.reserve_path(shared.locks_dir(self.p, lock, self.cfg), lock)

    def _slot_reserve_s(self) -> float:
        try:
            return float((self.cfg.get("budget") or {}).get("exclusive_reserve_s", 600) or 0)
        except (TypeError, ValueError):
            return 600.0

    def _slot_contenders(self, now: float) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """Per canonical lock of an `exclusive:` resource: the queued tasks that would take one of its
        slots, in dispatch's pick order (db.ready_tasks), and the ready `resource:` tasks that use it per
        command. A contender is ready now, or waits only because the resource was busy, whatever its
        retry timer says: a hand-off waiting on nothing but `ttp lock --probe` of its exclusive resources,
        or a run that lost the race for its slot. A task that needs a paused resource is none: a pause wins."""
        def canon(r: str) -> str:
            return locks.canonical(self.cfg, r)
        paused = {canon(r) for r in self.p.db.paused_resources()}
        excl: dict[str, list[dict]] = {}
        users: dict[str, list[dict]] = {}
        for t in self.p.db.ready_tasks(later=True):
            if {canon(r) for r in coord.task_resources(t)} & paused:
                continue
            due = not t["not_before"] or t["not_before"] <= now
            mine = {canon(r) for r in _exclusive(t)}
            if mine and not due:
                prev = load_result(t["result"])
                names = locks.probe_resources(str(prev.get("retry_when") or "")) \
                    if prev.get("status") == "waiting" else []
                due = (t["blocked_reason"] or "").startswith(RESOURCE_BUSY_NOTE) or \
                    bool(names) and {canon(n) for n in names} <= mine
                if not due:
                    continue
                for lock in mine:
                    excl.setdefault(lock, []).append(t)
                continue
            if not due:   # a task still on its retry timer takes no slot and uses the resource for nothing yet
                continue
            for lock in mine:
                excl.setdefault(lock, []).append(t)
            for lock in {canon(r) for r in _shared(t)} - mine:
                users.setdefault(lock, []).append(t)
        return excl, users

    def _slot_plan(self, now: float) -> tuple[dict[str, int], dict[str, list[dict]]]:
        """(canonical exclusive lock -> the task that reserves it, the contenders per lock). The reserver is
        a lock's first contender (_slot_contenders) when it outranks every other contender and every ready
        task that uses the resource per command: a required owner of an open obligation above one that is
        none, else a better priority. Equal ranks reserve nothing and keep plain pick order. A reservation
        that lapsed (_slot_reservations) is not made again for that task while the slot stays free.
        None at all with budget.exclusive_reserve_s 0."""
        if self._slot_reserve_s() <= 0:
            return {}, {}
        excl, users = self._slot_contenders(now)
        if not excl:
            return {}, excl
        from . import obligations
        try:
            due = obligations.owner_due(self.p.db)
        except Exception:   # unreadable obligations only lose the boost, as in db.ready_tasks
            due = {}

        def rank(t: dict) -> tuple:
            return t["id"] not in due, int(t["priority"] or 3)
        out = {}
        for lock, ts in excl.items():
            top = ts[0]
            rest = ts[1:] + [u for u in users.get(lock, []) if u["id"] != top["id"]]
            if (lock, top["id"]) not in self._slot_resv_lapsed and all(rank(top) < rank(o) for o in rest):
                out[lock] = top["id"]
        return out, excl

    def _slot_reservers(self, now: float) -> dict[str, int]:
        """Canonical exclusive lock -> the task that reserves it (_slot_plan): no other task starts on it
        as an exclusive holder meanwhile in the one slot kept for it (_resource_hold's "reserved")."""
        return self._slot_plan(now)[0]

    def _slot_open(self, res: str, tid: int) -> bool:
        """Whether an exclusive task `tid` would find a slot of `res` now: fewer running exclusive holders
        than its slots, and a slot lock free or kept for the task's own detached jobs."""
        lock = locks.canonical(self.cfg, res)
        running = self.p.db.q("SELECT labels FROM tasks WHERE status='running'")
        if sum(1 for t in running if lock in {locks.canonical(self.cfg, r) for r in _exclusive(t)}) \
                >= shared.slots(self.p, lock, self.cfg):
            return False
        paths = self._slot_paths(res)
        if locks.any_free(paths):
            return True
        mine = shared.holder(self.p, res, hold.who(tid), self.cfg)
        return any(hold.own_keeper(lb, mine) for lb in locks.held_labels(paths))

    def _slot_reservations(self, now: float) -> dict[str, int]:
        """Dispatch's reservation pass, before it reads the ready tasks; returns the reservers
        (_slot_plan). Each reserver's resource is reserved, so new `ttp lock` commands of other tasks wait
        as well (a ready reserver that dispatch then does not reach drops that, as before). A lock's first
        contender in pick order, reserver or not, still on its retry timer is woken as soon as every
        resource it needs has a free slot, so a free slot goes to it ahead of later tasks (a waiting hand-off
        goes through _probe_passed, so its on_pass_cmd runs model-free). A running
        task is never preempted. A reservation ends once its task starts, leaves the queue (cancelled,
        blocked, deferred), needs a paused resource or is outranked, and lapses (logged) once it has kept
        a free slot unused for budget.exclusive_reserve_s (default 600), so a reserver that cannot start
        never starves the resource; it is not made again for that task until the resource is taken."""
        db = self.p.db

        def name(lock: str, task: dict) -> str:   # the task's own label for the lock (aliases share one lock)
            return next((r for r in _exclusive(task) if locks.canonical(self.cfg, r) == lock), lock)
        for lock, tid in list(self._slot_resv_lapsed):
            t = db.task(tid)
            if not t or t["status"] != "queued" or not self._slot_open(name(lock, t), tid):
                self._slot_resv_lapsed.discard((lock, tid))
        want, excl = self._slot_plan(now)
        for lock, r in list(self._slot_resv.items()):
            if want.get(lock) != r["task"]:
                del self._slot_resv[lock]
                if (db.task(r["task"]) or {}).get("status") != "running":   # a started run keeps its own
                    locks.unreserve(self._reserve_path(lock), r["holder"])
        limit = self._slot_reserve_s()
        for lock, tid in list(want.items()):
            task = next(t for t in excl[lock] if t["id"] == tid)
            res = name(lock, task)
            mine = shared.holder(self.p, res, hold.who(tid), self.cfg)
            r = self._slot_resv.get(lock)
            if not r:
                r = self._slot_resv[lock] = {"task": tid, "holder": mine, "free_since": None}
                log(self.p, f"task {tid}: reserves {res} (priority {task['priority']}; "
                            f"{len(excl[lock]) - 1} other task(s) queued for it)")
            free = self._slot_open(res, tid)
            r["free_since"] = (r["free_since"] or now) if free else None
            path = self._reserve_path(res)
            if free and now - r["free_since"] >= limit:
                self._slot_resv_lapsed.add((lock, tid))
                del self._slot_resv[lock], want[lock]
                locks.unreserve(path, mine)
                log(self.p, f"task {tid}: its reservation of {res} lapsed: the slot stayed free and unused "
                            f"for {limit:g} s (budget.exclusive_reserve_s)")
                continue
            other = locks.reserved_by(path)
            if other and other != mine and other in {shared.holder(self.p, res, hold.who(t["id"]), self.cfg)
                                                     for t in excl[lock]}:
                locks.unreserve(path, other)   # an outranked contender's lock-type reservation
            locks.reserve(path, mine)
        # The first contender of each lock takes a free slot in pick order even when it reserves nothing
        # (an equal rank): one still on its retry timer is woken before dispatch reads the ready tasks.
        woken = set()
        for lock, ts in excl.items():
            task = ts[0]
            # A run that lost the race for its slot keeps its short backoff (it still reserves meanwhile).
            if task["id"] in woken or (task["not_before"] or 0) <= now or \
                    (task["blocked_reason"] or "").startswith(RESOURCE_BUSY_NOTE) or \
                    (lock, task["id"]) in self._slot_resv_lapsed or want.get(lock, task["id"]) != task["id"]:
                continue
            if all(self._slot_open(x, task["id"]) for x in _exclusive(task)):
                woken.add(task["id"])
                why = f"{name(lock, task)} came free" + (" and is reserved for it" if lock in want else "")
                if load_result(task["result"]).get("status") == "waiting":
                    self._probe_passed(task, now, why)   # its on_pass_cmd, if any, runs model-free
                else:
                    db.update_task(task["id"], not_before=now)
                    log(self.p, f"task {task['id']}: {why}; dispatching")
        return want

    def _dispatchable(self) -> bool:
        """Whether any queued task could start now (dependencies done, resources free)."""
        dev_full = self._device_tasks_running() >= self._device_max_tasks()
        return any(self._resources_free(t) and not (dev_full and needs_device(t, self.cfg))
                   for t in self.p.db.ready_tasks())

    def _size_review(self, task: dict) -> tuple[dict, dict | None]:
        """A review runs at the tier its diff needs, not the one it was queued with: the task at that
        tier, and the rule's pick for the run's note (None when it was not measured). Deep stays the
        coordinator's call. A re-review after a failed review and a retry never run light: the
        failed review shows the change needs the closer look, and the light try may be why it
        failed. A diff that cannot be measured keeps the tier the task was given. A push-conflict fix
        runs at conflictfix.tier_for's tier for this run only, deep reviews too."""
        cf = conflictfix.of(task)
        if cf is not None:
            tier, why = conflictfix.tier_for(str(self.p.root), cf)
            if tier == "standard" and task["tier"] == "deep":
                tier = "deep"
            log(self.p, f"task {task['id']}: push-conflict fix at {tier} ({why})")
            return dict(task, tier=tier), {"tier": tier, "from": task["tier"], "rule": why,
                                           conflictfix.KEY: {"tier": tier, "why": why, "head": cf.get("head")}}
        if task["tier"] == "deep":
            return task, None
        prior = self._failed_review_before(task)
        if prior is not None:
            tier, why, changes = "standard", f"re-review after failed review #{prior}", {}
        else:
            try:
                changes = worktree.diff_lines(self.p, worktree.reviewed_refs(self.p, task))
            except Exception as e:
                log(self.p, f"task {task['id']}: review diff not measured: {e}")
                return task, None
            if changes is None:
                return task, None
            tier, why = bud.review_pick(changes, self.cfg)
            if tier == "light" and task["attempts"]:
                tier, why = "standard", f"retry ({why})"
        pick = {"tier": tier, "from": task["tier"], "rule": why, "files": len(changes),
                "lines": sum(n or 0 for n in changes.values())}
        if tier != task["tier"]:
            log(self.p, f"task {task['id']}: review tier {task['tier']} -> {tier} ({why})")
            self.p.db.update_task(task["id"], tier=tier, tier_source="review_diff")
        return dict(task, tier=tier), pick

    def _landed_elsewhere(self, task: dict) -> str:
        """The commit on the push branch that already carries code task `task`'s change under other
        commits (landed.equivalent: a fix or rebase of another task landed it), or "": then the caller
        reviews or fixes it as usual. When found, it is recorded as superseding the task, a task left in
        'review' is closed, and the coordinator is told once, instead of spending a review or fix on
        a duplicate."""
        d = self.cfg.get("delivery") or {}
        branch = task.get("branch")
        if task.get("kind") != "code" or not branch or not d.get("push_branch"):
            return ""
        try:
            found = landed.equivalent(self.p, branch, d.get("version_bump"))
        except Exception as e:
            log(self.p, f"task {task['id']}: could not compare its change with the push branch: {e}")
            return ""
        if not found:
            return ""
        sha, head = found
        line = (f"its change already landed on {d['push_branch']} as {sha[:10]} under another commit (the same "
                f"changed lines, version files aside): branch {branch} at {head[:10]} is superseded")
        if self._close_superseded(task, sha, head, line, "superseded_by", "task_superseded", True,
                                  lambda closed: f"#{task['id']} {task['title']}: {line}; "
                                                 + ("closed as done; " if closed else "")
                                                 + "no review or fix queued for it.") is not None:
            log(self.p, f"task {task['id']}: superseded by {sha[:10]}; no review or fix queued")
        return sha

    def _close_superseded(self, task: dict, sha: str, head: str, line: str, key: str, kind: str, queued: bool,
                          text) -> bool | None:
        """Record commit `sha` on the push branch as carrying code task `task` at branch head `head`
        (task_superseded), and close the task as done if it is still in 'review', its result naming
        `sha` under `key` and its summary ending in `line`; one `kind` event with `text(closed)`. None
        when that head and commit were recorded before (nothing done), else whether it closed it."""
        db, now = self.p.db, time.time()
        with db.tx():
            if not db.conn.execute("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?) "
                                   "ON CONFLICT DO NOTHING", (task["id"], head, sha, now)).rowcount:
                return None
            prev = load_result((db.task(task["id"]) or task).get("result"))
            result = {**prev, "status": "done", key: sha,
                      "summary": f"{str(prev.get('summary') or '').rstrip()} ({line})".strip()[:1500]}
            closed = bool(db.conn.execute("UPDATE tasks SET status='done', blocked_reason=NULL, result=?, updated=? "
                                          "WHERE id=? AND status='review'",
                                          (dump_result(result), now, task["id"])).rowcount)
            db.conn.execute("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                            (now, f"task:{task['id']}", kind, "normal", text(closed),
                             "queued" if queued else "handled", task["id"]))
            if closed and not queued:
                pushq._reply(db, task, "done", result["summary"])
        return closed

    def _reviewed_on_push_branch(self, task: dict, reviews: list[dict], covers: dict[int, set[int]],
                                 since: float) -> str:
        """Close code task `task`, left in 'review', whose review passed but pushed nothing because its
        branch already is on the push branch (landed.on_push_branch: pushed or fast-forwarded as is,
        or rebased there): every review covering it (`covers`) since it entered review (`since`) is
        done, passed (no rejecting verdict) and has no push-queue rows, and there is at least one. The
        landed commit is recorded (task_superseded) and named in its result. Returns it, or "": a
        review that failed, asked for changes, still runs or pushed leaves the task alone (the push
        queue settles a pushed one, pushq.settle_reviewed)."""
        d = self.cfg.get("delivery") or {}
        branch = task.get("branch")
        if task.get("kind") != "code" or not branch or not d.get("push_branch"):
            return ""
        mine = [r for r in reviews if task["id"] in covers.get(r["id"], set())
                and (r["status"] not in ("done", "failed") or float(r["updated"] or 0) >= since)]
        if not mine or any(r["status"] != "done" or review_rejects(load_result(r["result"])) for r in mine):
            return ""
        db = self.p.db
        if db.one("SELECT 1 FROM push_queue WHERE task IN (%s)" % ",".join("?" * len(mine)), [r["id"] for r in mine]):
            return ""
        try:
            found = landed.on_push_branch(self.p, branch)
        except Exception as e:
            log(self.p, f"task {task['id']}: could not look for its branch on the push branch: {e}")
            return ""
        if not found:
            return ""
        sha, head = found
        line = (f"review #{mine[-1]['id']} passed and its branch {branch} at {head[:10]} already is on "
                f"{d['push_branch']} as {sha[:10]}: landed, nothing to push")
        if self._close_superseded(task, sha, head, line, "landed_as", "task_done", False,
                                  lambda closed: f"#{task['id']} {task['title']} → "
                                                 + ("done" if closed else "recorded") + f" ({line})") is not None:
            log(self.p, f"task {task['id']}: reviewed and already on the push branch as {sha[:10]}; closed")
        return sha

    def check_unqueued_continues(self) -> None:
        """Model-free, every CONTINUE_CHECK_EVERY_S: a task done CONTINUE_UNQUEUED_AFTER_S ago (within
        CONTINUE_UNQUEUED_WINDOW_S) whose hand-off proposed a `continue:` follow-up that no task has
        taken up yet raises one high CONTINUE_UNQUEUED event, so a hop no turn queued is not lost. A
        continuing task is any later task that continues it (chaincap.prev_of), has the follow-up's
        title or, for a post-landing deploy step only (chaincap.deploy_proposal), waits on its
        landing (a start_when); every `continue:` follow-up must be taken up. One event per task, ever."""
        now = time.time()
        if now - self._continues_checked < CONTINUE_CHECK_EVERY_S:
            return
        self._continues_checked = now
        db = self.p.db
        done = db.q("SELECT DISTINCT e.task FROM events e JOIN tasks t ON t.id=e.task WHERE e.kind='task_done' "
                    "AND e.ts BETWEEN ? AND ? AND t.status='done' AND NOT EXISTS (SELECT 1 FROM events x "
                    "WHERE x.kind=? AND x.task=e.task) ORDER BY e.task",
                    (now - CONTINUE_UNQUEUED_WINDOW_S, now - CONTINUE_UNQUEUED_AFTER_S, CONTINUE_UNQUEUED))
        for row in done:
            task = db.task(row["task"])
            result = load_result(task["result"]) if task else None
            fups = result.get("followups") if isinstance(result, dict) else None
            cfups = [f for f in fups if isinstance(f, dict) and f.get("title")
                     and chaincap.CONT_TITLE.match(str(f["title"]))] if isinstance(fups, list) else []
            if not cfups:
                continue
            conts = [str(f["title"]).strip() for f in cfups]
            later = db.q("SELECT id, title, labels, parent FROM tasks WHERE id>? ORDER BY id", (task["id"],))
            hop = any(chaincap.prev_of(t) == task["id"] for t in later)
            titles = {t["title"].strip().lower() for t in later}
            waiter = any(chaincap.waits_landing(deferral(t).get("when"), task["id"]) for t in later)
            # Each follow-up by its own rule: a landing waiter takes up only a post-landing deploy step
            # (deploy_proposal), never a split's hop (that head lands only through its hop).
            left = [(c, f) for c, f in zip(conts, cfups)
                    if not (hop or c.lower() in titles or (waiter and chaincap.deploy_proposal(f, task["id"])))]
            if not left:
                continue
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, f"task:{task['id']}", CONTINUE_UNQUEUED, "high",
                  f"#{task['id']} {headline.clip(task['title'], 160)}: its follow-up {left[0][0][:200]!r} is {UNQUEUED_TAG} and no "
                  f"task continues it {int(CONTINUE_UNQUEUED_AFTER_S // 60)} min after it finished. " + (
                      f"{deploy[0].upper()}{deploy[1:]} on purpose." if (
                          deploy := chaincap.deploy_proposal(left[0][1], task["id"])) else
                      f"Add it with task_add continues #{task['id']}, or drop it on purpose."),
                  json.dumps({"followups": conts}), "queued", task["id"]))
            log(self.p, f"task {task['id']}: continue follow-up not taken up; {CONTINUE_UNQUEUED} raised")

    def check_stale_duplicates(self) -> None:
        """Model-free, every CONTINUE_CHECK_EVERY_S: open work the plan no longer needs, surfaced once per
        task as a STALE_DUPLICATE event for the next digest (nothing is cancelled here; the coordinator
        cancels it or lets it run):
        - a duplicate hop: two or more open tasks (STALE_OPEN, reviews aside) that continue the same task
          (chaincap.prev_of: a `continue:` follow-up, `continues:`, a daemon fix), neither depending on the
          other; each later one is named as a likely duplicate of the first;
        - a review of a landed change: a queued review that has pushed nothing itself (no push-queue row or
          landing) whose subject's change is already on the push branch (landed.check, local refs only)."""
        now = time.time()
        if now - self._stale_checked < CONTINUE_CHECK_EVERY_S:
            return
        self._stale_checked = now
        db = self.p.db
        told = {r["task"] for r in db.q("SELECT DISTINCT task FROM events WHERE kind=? AND task IS NOT NULL",
                                        (STALE_DUPLICATE,))}
        found: list[tuple[dict, str]] = []
        hops: dict[int, list[dict]] = {}
        for t in db.q("SELECT * FROM tasks WHERE status IN (%s) AND kind!='review' ORDER BY id"
                      % ",".join("?" * len(STALE_OPEN)), STALE_OPEN):
            if (prev := chaincap.prev_of(t)) is not None:
                hops.setdefault(prev, []).append(t)
        for prev, group in hops.items():
            first = group[0]
            for t in group[1:]:
                if t["id"] in told or first["id"] in dependency_ids(t) or t["id"] in dependency_ids(first):
                    continue
                found.append((t, f"#{t['id']} {headline.clip(t['title'], 120)} ({t['status']}) and #{first['id']} "
                                 f"{headline.clip(first['title'], 120)} ({first['status']}) both continue #{prev}: "
                                 f"#{t['id']} likely duplicates #{first['id']}. Cancel one (task_update cancelled), "
                                 f"or make one depend on the other if both are needed."))
        if (self.cfg.get("delivery") or {}).get("push_branch"):
            reviews = db.q("SELECT * FROM tasks t WHERE kind='review' AND status='queued' AND NOT EXISTS "
                           "(SELECT 1 FROM push_queue q WHERE q.task=t.id) AND NOT EXISTS "
                           "(SELECT 1 FROM task_landings l WHERE l.task=t.id) ORDER BY id")
            for r in [r for r in reviews if r["id"] not in told][:STALE_REVIEWS_PER_PASS]:
                subject = review_subject(r)
                if subject is None or not db.task(subject):
                    continue
                try:
                    rc, words = landed.check(self.p, self.p.root, tid=subject, fetch=False)
                except Exception as e:
                    log(self.p, f"task {r['id']}: could not look up its subject's landing: {e}")
                    continue
                if rc == 0:
                    found.append((r, f"#{r['id']} {headline.clip(r['title'], 160)} is queued, but the change it "
                                     f"reviews is already landed ({words}). Cancel it (task_update cancelled) unless "
                                     f"its spec has steps still to run after the push."))
        for t, text in found:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{t['id']}", STALE_DUPLICATE, "normal", text, "queued", t["id"]))
            log(self.p, f"task {t['id']}: {STALE_DUPLICATE} raised")

    def tend_mitigations(self) -> None:
        """Model-free: an overdue mitigation wakes the coordinator, a done fix lists its measures for undo."""
        try:
            for line in mitigate.tend(self.p.db, self.p.name, where=self.p):
                log(self.p, line)
        except Exception:
            log(self.p, "mitigations: " + traceback.format_exc().replace("\n", " | ")[:1500])

    def sweep_reviews(self) -> None:
        """Model-free retry of reviews left to the coordinator: a code task in 'review' whose last run
        ended review.sweep_after_s ago (0 off) and that no review has covered since it entered review
        gets its review (_auto_review), so a hand-off with notes the coordinator queued no review for
        does not sit there. It only queues reviews and never closes or changes a task, except one
        whose change already landed under other commits (_landed_elsewhere): that one is closed as
        superseded, with no review; and one whose review passed but pushed nothing as its branch already
        is on the push branch (_reviewed_on_push_branch): that one is closed as landed. Coverage here
        is direct (pushq._covers without following continues:<id>): a continuation need not carry the
        task's commits, so a task whose continuation passed review stays in review and gets its own
        review (known gap: one review each, no work lost). A harness task in 'review' is swept too
        (_sweep_harness_review): one whose commit is merged into the harness is closed as done, else it
        gets one review. Every pass first records the harness tasks in review whose commit is merged
        (_mark_harness_merged), so their dependents start."""
        now = time.time()
        if now - self._review_swept < REVIEW_SWEEP_EVERY_S:
            return
        self._review_swept = now
        db = self.p.db
        merged = self._mark_harness_merged()
        try:
            after = float((self.cfg.get("review") or {}).get("sweep_after_s", 1800) or 0)
        except (TypeError, ValueError):
            after = 1800.0
        if after <= 0:
            return
        try:
            todo = db.q("SELECT * FROM tasks WHERE kind IN ('code','harness') AND status='review' ORDER BY id")
            if not todo:
                return
            since = db.review_since()
            code = pushq.BranchIndex(db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' "
                                          "AND branch IS NOT NULL AND branch!=''"))
            reviews = db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled' AND id>? ORDER BY id",
                           (todo[0]["id"],))
            rows: dict[int, list[dict]] = {}
            for x in db.q("SELECT * FROM push_queue WHERE task IN (%s)" % ",".join("?" * len(reviews)),
                          [r["id"] for r in reviews]) if reviews else []:
                rows.setdefault(x["task"], []).append(x)
            covers: dict[int, set[int]] = {}
            for t in todo:
                ended = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (t["id"],))["e"]
                if ended is None or now - float(ended) < after:
                    continue
                if t["kind"] == "harness":
                    self._sweep_harness_review(t, reviews, float(since.get(t["id"]) or 0), merged.get(str(t["id"])), now)
                    continue
                # A change that landed under another task's commits needs no review of its own; the
                # task is closed as superseded (it may have had its say from a review already).
                if self._landed_elsewhere(t):
                    continue
                start = float(since.get(t["id"]) or 0)
                for r in reviews:
                    if r["id"] not in covers:
                        covers[r["id"]] = pushq._covers(db, r, rows.get(r["id"], []), code, follow=False)
                # A review that passed but pushed nothing, as the branch already is on the push branch.
                if self._reviewed_on_push_branch(t, reviews, covers, start):
                    continue
                # A review that ran or still runs since the task entered review had its say: what it
                # found is the coordinator's (or the push queue's) to act on, not a second review's.
                seen = False
                for r in reviews:
                    if r["id"] not in covers:
                        covers[r["id"]] = pushq._covers(db, r, rows.get(r["id"], []), code, follow=False)
                    if t["id"] in covers[r["id"]] and (r["status"] not in ("done", "failed")
                                                       or float(r["updated"] or 0) >= start):
                        seen = True
                        break
                if seen:
                    continue
                review = self._auto_review(t, str(load_result(t["result"]).get("summary") or ""), add=True)
                self._swept(t, review, start, now)
        except Exception:
            log(self.p, "review sweep failed\n" + traceback.format_exc())

    def _swept(self, t: dict, review: tuple[int, bool] | None, start: float, now: float) -> None:
        """Log and record (review_swept) a review the sweep queued now for task `t`, in review since `start`."""
        if review and review[1]:
            log(self.p, f"task {t['id']}: in review {(now - start) / 3600:.1f}h with no review; "
                        f"queued review #{review[0]}")
            self.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                        (now, "daemon", "review_swept", "normal",
                         f"#{t['id']} {t['title']} sat in review with no review; review #{review[0]} queued "
                         f"by the daemon.", "handled", t["id"]))

    def _mark_harness_merged(self) -> dict[str, str]:
        """Record (HARNESS_MERGED_KEY) each harness task in 'review' whose hand-off commit is merged into
        the harness (harnesstree.merged_commit): its dependents may start (DB.unmet_dependencies). Tasks
        that left review drop out. Returns {task id: commit}."""
        db = self.p.db
        try:
            old = db.kv(HARNESS_MERGED_KEY, {}) or {}
            new = {}
            for t in db.q("SELECT * FROM tasks WHERE kind='harness' AND status='review' ORDER BY id"):
                sha = old.get(str(t["id"])) or harnesstree.merged_commit(self.p, t)
                if sha:
                    new[str(t["id"])] = sha
                    if str(t["id"]) not in old:
                        log(self.p, f"task {t['id']}: its harness commit {sha[:10]} is merged; its dependents "
                                    f"may start while it waits in review")
            if new != old:
                db.set_kv(HARNESS_MERGED_KEY, new)
            return new
        except Exception:
            log(self.p, "harness merge check failed: " + traceback.format_exc().replace("\n", " | ")[:1500])
            return {}

    def _sweep_harness_review(self, t: dict, reviews: list[dict], start: float, sha: str | None, now: float) -> None:
        """sweep_reviews for harness task `t`, in review since `start`. A review of it that is open, or
        that ended since it entered review, had its say (a failed one is the coordinator's to act on)
        unless it passed. A commit merged into the harness (`sha`) is delivered, as the harness has no
        remote: the task is closed as done, with no review queued. So is one whose review passed and
        whose worktree branch has nothing unmerged (harnesstree.sweep may have merged and deleted it);
        a branch still holding unmerged commits then raises one HARNESS_UNMERGED event for the
        coordinator. Else it gets one review of its own (_auto_review_harness)."""
        mine = [r for r in reviews if t["id"] in dependency_ids(r) or review_subject(r) == t["id"]]
        if any(r["status"] not in ("done", "failed") for r in mine):
            return
        ended = [r for r in mine if float(r["updated"] or 0) >= start]
        passed = bool(ended) and ended[-1]["status"] == "done" and task_outcome(ended[-1]) != CHANGES_NEEDED
        if ended and not passed:
            return
        if sha:
            why = f"its commit {sha[:10]} is merged into the harness" + (
                f" and review #{ended[-1]['id']} passed" if ended else "")
            self._close_harness_review(t, start, now, why, {"harness_merged": sha}, bool(ended))
        elif not passed:
            self._swept(t, self._auto_review_harness(t), start, now)
        elif left := harnesstree.unmerged(self.p, t["id"]):
            self._harness_unmerged(t, ended[-1]["id"], left, start, now)
        else:
            why = (f"review #{ended[-1]['id']} passed and its branch {harnesstree.branch(t['id'])} has nothing "
                   f"left to merge into the harness")
            self._close_harness_review(t, start, now, why, {"reviewed_by": ended[-1]["id"]}, True)

    def _close_harness_review(self, t: dict, start: float, now: float, why: str, extra: dict,
                              reviewed: bool) -> None:
        """Close harness task `t`, in review since `start`, as done because `why` (`extra` added to its
        result); one review_swept event, and the chat reply (pushq._reply) when this closed it."""
        db = self.p.db
        with db.tx():
            prev = load_result((db.task(t["id"]) or t).get("result"))
            result = {**prev, **extra, "status": "done",
                      "summary": f"{str(prev.get('summary') or '').rstrip()} ({why})".strip()[:1500]}
            if not db.conn.execute("UPDATE tasks SET status='done', blocked_reason=NULL, result=?, updated=? "
                                   "WHERE id=? AND status='review'", (dump_result(result), now, t["id"])).rowcount:
                return
            db.conn.execute("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                            (now, "daemon", "review_swept", "normal",
                             f"#{t['id']} {t['title']} sat in review {(now - start) / 3600:.1f}h; {why}, so it was "
                             f"closed as done" + ("" if reviewed else " with no review queued") + ".", "handled", t["id"]))
            pushq._reply(db, t, "done", pushq._done_text(prev, result["summary"], why))
        log(self.p, f"task {t['id']}: in review {(now - start) / 3600:.1f}h; {why}: closed as done")

    def _harness_unmerged(self, t: dict, rid: int, left: int, start: float, now: float) -> None:
        """Harness task `t` passed review #`rid` but its worktree branch still holds `left` commits the
        harness lacks (-1: git could not tell), e.g. after a merge conflict: one queued event per stay in
        review, for the coordinator to merge or requeue it."""
        db = self.p.db
        if db.one("SELECT id FROM events WHERE kind=? AND task=? AND ts>=?", (HARNESS_UNMERGED, t["id"], start)):
            return
        b = harnesstree.branch(t["id"])
        n = "an unknown number of" if left < 0 else str(left)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", HARNESS_UNMERGED, "normal",
              f"#{t['id']} {t['title']} passed review #{rid}, but its branch {b} has {n} commits not merged "
              f"into the harness (`git -C {self.p.harness} log HEAD..{b}`), so it stays in review: merge them "
              f"(or close it with `ttp task superseded`), or requeue it.", "queued", t["id"]))
        log(self.p, f"task {t['id']}: review #{rid} passed but branch {b} has {n} unmerged commits; told the coordinator")

    def _auto_review_harness(self, task: dict) -> tuple[int, bool] | None:
        """The review of harness task `task`, left in 'review' with its commits not merged into the harness
        (or none named): (review id, whether it was added now), or None when delivery has no review step,
        the review cap is reached or a review of it is already open (by title). Review only: the harness
        has no remote."""
        cfg, db = self.cfg, self.p.db
        d = cfg.get("delivery") or {}
        if not (cfg.get("review") or {}).get("auto", True) or not (d.get("review_before_pr", True)
                                                                    or d.get("push_branch")):
            return None
        title = f"Review #{task['id']}: {task['title']}"[:200]
        dup = db.one("SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled')", (title,))
        if dup:
            return dup["id"], False
        if coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
            return None
        h, b = self.p.harness, harnesstree.branch(task["id"])
        ahead, named = harnesstree.unmerged(self.p, task["id"]), harnesstree.named_commits(task)
        where = (f"its worktree branch {b} has commits not merged into the harness yet (`git -C {h} log -p HEAD..{b}`)"
                 if ahead else f"its hand-off names {', '.join(named[:5])}" if named
                 else f"its hand-off names no commit: find its commits with `git -C {h} log`")
        summary = str(load_result(task.get("result")).get("summary") or "")
        lines = [f"Independently review the change of harness task #{task['id']} ({task['title']}) in this "
                 f"project's harness repo ({h}): {where}.",
                 f"Its spec: {coord.clip(task.get('spec'), AUTO_REVIEW_SPEC_CHARS)}",
                 f"Its hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}",
                 REVIEW_CHECK_LINE,
                 "The harness has no remote: review only. If it passes, hand off `done`; never run `ttp push`, "
                 "approve it for the push queue or copy harness files into a code branch. Leave the harness's "
                 "commits and branches as they are.",
                 "Return the verdict and findings."]
        tier = "light" if task.get("tier") == "light" else "standard"
        rid = db.add_task(title, "\n".join(lines), kind="review", tier=tier, priority=2, origin="daemon",
                          budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                          depends_on=[task["id"]], labels=[f"auto_review:{task['id']}", push.REVIEW_ONLY_LABEL],
                          **pin_of(task))
        log(self.p, f"task {task['id']}: queued review #{rid} of its harness commits ({tier})")
        return rid, True

    def sweep_unreviewed(self, force: bool = False) -> list[int]:
        """Model-free invariant backstop: a code task that is done, or has waited externally for
        review.sweep_after_s, whose branch head is on no push-branch ref (local refs and push_queue
        rows only, no fetch) gets its review queued (_auto_review), with one low feed line naming it.
        Left alone: a review of it that is open or ran since its last run (or that names this head),
        an open task continuing it or on its branch (not one that waits on it landing: a split head
        such a task waits on gets its review), a push-queue entry for it or its head, and a
        recorded reason (superseded or rejected, a `no_push`, a diff all under push_exclude_paths, a
        head its PR carries, a push branch no push can reach, a landing `ttp landed` records). At most
        review.unreviewed_per_pass reviews per pass, only tasks updated within
        review.unreviewed_max_age_h, and one head gets one review (_auto_review's own check).
        Every REVIEW_SWEEP_EVERY_S from the tick, and at once (`force`) in the blocker sweep's data
        pass; returns the tasks it queued a review for."""
        now = time.time()
        if not force and now - self._unreviewed_swept < REVIEW_SWEEP_EVERY_S:
            return []
        self._unreviewed_swept = now
        cfg, db, done = self.cfg, self.p.db, []
        rules, d = cfg.get("review") or {}, cfg.get("delivery") or {}
        try:
            cap = int(rules.get("unreviewed_per_pass", UNREVIEWED_PER_PASS))
            age = float(rules.get("unreviewed_max_age_h", UNREVIEWED_MAX_AGE_H)) * 3600
            waited = float(rules.get("sweep_after_s", 1800) or 0)
        except (TypeError, ValueError):
            cap, age, waited = UNREVIEWED_PER_PASS, UNREVIEWED_MAX_AGE_H * 3600.0, 1800.0
        if cap <= 0 or not rules.get("auto", True) or not (d.get("review_before_pr", True) or d.get("push_branch")):
            return []
        try:
            todo = db.q("SELECT * FROM tasks WHERE kind='code' AND status IN ('done','queued') AND branch IS NOT NULL "
                        "AND branch!='' AND updated>=? AND updated<=? ORDER BY id",
                        (now - age, now - UNREVIEWED_GRACE_S))
            if not todo:
                return []
            code = pushq.BranchIndex(db.q("SELECT id, branch, labels FROM tasks WHERE kind='code' "
                                          "AND branch IS NOT NULL AND branch!=''"))
            reviews = db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled' AND id>? ORDER BY id",
                           (todo[0]["id"],))
            rows: dict[int, list[dict]] = {}
            for x in db.q("SELECT * FROM push_queue WHERE task IN (%s)" % ",".join("?" * len(reviews)),
                          [r["id"] for r in reviews]) if reviews else []:
                rows.setdefault(x["task"], []).append(x)
            covers: dict[int, set[int]] = {}
            live = db.q("SELECT id, branch, labels FROM tasks WHERE status NOT IN (?,?,?)", TERMINAL_TASK_STATES)
            ref = landed._push_ref(self.p, self.p.root) if d.get("push_branch") else ""
            never = push.push_branch_problem(d["push_branch"], self.p.root, push.allow_protected(d)) \
                if d.get("push_branch") and d.get("push_allowed", True) else ""
            never = "" if never and land.local_branch(self.p, self.p.root) else never   # `ttp land` lands it
        except Exception:
            log(self.p, "unreviewed sweep failed\n" + traceback.format_exc())
            return []
        for t in todo:
            if len(done) >= cap or time.time() - now > UNREVIEWED_PASS_S:
                break
            try:
                if self._unreviewed_skip(t, reviews, rows, covers, code, live, ref, never, waited, now):
                    continue
                review = self._auto_review(t, str(load_result(t["result"]).get("summary") or ""), add=True)
            except Exception as e:
                log(self.p, f"task {t['id']}: unreviewed check failed: {e}")
                continue
            if review and review[1]:
                done.append(t["id"])
                log(self.p, f"task {t['id']}: {t['status']} with an unlanded head and no review; queued review "
                            f"#{review[0]}")
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), "daemon", UNREVIEWED_EVENT, "low",
                      f"#{t['id']} {headline.clip(t['title'], 160)}: its commits had no review and were not landed; "
                      f"review #{review[0]} queued by the daemon.", "handled", t["id"]))
        return done

    def _unreviewed_skip(self, t: dict, reviews: list[dict], rows: dict, covers: dict, code, live: list[dict],
                         ref: str, never: str, waited: float, now: float) -> str:
        """Why sweep_unreviewed leaves code task `t` alone, or "" when it needs its review now. Cheap
        record checks first, git last."""
        db, d, tid = self.p.db, self.cfg.get("delivery") or {}, t["id"]
        result = load_result(t["result"])
        if t["status"] == "queued":
            if result.get("status") != "waiting":
                return "not finished"
            if unblock.classify_wait(result)[0] == "self" or now - float(t["updated"] or 0) < waited:
                return "waits on its own work"
        if str(result.get("outcome") or "").lower() in ("rejected", "shelved") or result.get("superseded_by"):
            return "rejected or superseded"
        if never:
            return f"no push can reach the push branch: {never}"
        if db.one("SELECT 1 FROM task_superseded WHERE task=? UNION ALL SELECT 1 FROM task_landings WHERE task=? "
                  "UNION ALL SELECT 1 FROM push_queue WHERE task=?", (tid, tid, tid)):
            return "superseded, landed or queued"
        # A task that waits on this one landing (a deploy or live check) carries none of its work: counted
        # as its continuation, a head no review lands would hold it forever.
        if any(x["id"] != tid and (continues_id(x) == tid or x["branch"] == t["branch"])
               and not self._waits_landing(x, tid) for x in live):
            return "an open task continues it"
        if chaincap.split_head(t) and any(
                not self._waits_landing(x, tid) for x in db.q("SELECT id, labels FROM tasks WHERE labels LIKE ? "
                                                              "AND status!='cancelled'", (f'%"continues:{tid}"%',))):
            return "a later hop of its split chain carries it"
        tip = worktree._git(self.p.root, "rev-parse", "--verify", "--quiet", f"{t['branch']}^{{commit}}", check=False)
        if not tip:
            return "no branch"
        ended = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (tid,))["e"]
        start = float(ended or t["updated"] or 0)
        for r in reviews:
            if r["id"] not in covers:
                covers[r["id"]] = pushq._covers(db, r, rows.get(r["id"], []), code, follow=False)
            if tid in covers[r["id"]] and (r["status"] not in ("done", "failed") or float(r["updated"] or 0) >= start
                                           or f"review_head:{tip}" in (r["labels"] or "")):
                return f"review #{r['id']} covers it"
        if db.one("SELECT 1 FROM push_queue WHERE head=? AND status!='cancelled'", (tip,)):
            return "its head is in the push queue"
        if ref and landed.on_branch(self.p.root, tip, ref):
            return "its head is on the push branch"
        changes = worktree.diff_lines(self.p, [t["branch"]])
        if not changes:
            return "no changes"
        if d.get("push_branch") and (why := push.kept_off(t, changes, d, self.p)):
            return why
        if push.delivered_pr(self.p, t, tip):
            return "its PR carries this head"
        if self._landed_elsewhere(t):
            return "landed under other commits"
        return ""

    def _waits_landing(self, x: dict, tid: int) -> bool:
        """Whether task `x` (a row with id and labels) waits on task `tid` landing: its start_when, or
        the retry_when of its waiting hand-off, names it (chaincap.waits_landing)."""
        if chaincap.waits_landing(deferral(x).get("when"), tid):
            return True
        row = self.p.db.one("SELECT status, result FROM tasks WHERE id=?", (x["id"],))
        result = load_result(row["result"]) if row and row["status"] not in TERMINAL_TASK_STATES else {}
        return result.get("status") == "waiting" and chaincap.waits_landing(result.get("retry_when"), tid)

    def _auto_review(self, task: dict, summary: str, add: bool = True) -> tuple[int, bool] | None:
        """The review of a finished code task, queued here the way the coordinator would, so a
        routine hand-off needs no coordinator turn: (review id, whether it was added now). An open
        review that already covers the task (queued ahead by the coordinator, or a still queued
        re-review of the branch the task's fix branch builds on: worktree.fix_branches), or one that
        already approved its head, is that review: one head gets one review. None
        when delivery has no review step, `add` is off and none is open, the branch changes nothing,
        the review cap is reached or the diff cannot be read: the coordinator then decides. A task with
        no branch on record that published its change as an open PR (pr_url, or the PR its hand-off
        names: it worked in a clone of another repository) is reviewed at that PR's head, its diff read
        from the hosting service (push.pr_change), review only: the PR is its delivery. One whose
        project branch changes nothing (or that has none) and whose hand-off names a valid `published`
        change in another repository (extchange) is reviewed at that head, review only, once per head:
        its diff is not readable here, so the review runs standard."""
        cfg, db = self.cfg, self.p.db
        d = cfg.get("delivery") or {}
        rules = cfg.get("review") or {}
        if not rules.get("auto", True) or not (d.get("review_before_pr", True) or d.get("push_branch")):
            return None
        if self._landed_elsewhere(task):
            return None
        branch = task.get("branch")
        try:
            ext = self._external_change(task)
            if ext and branch and worktree.diff_lines(self.p, [branch]):
                ext = None   # a change in the project's own git is reviewed there, as before
            for t in db.q("SELECT * FROM tasks WHERE kind='review' AND status NOT IN ('done','failed','cancelled')"):
                # A re-review covers a fix branch built on the branch it names only while it is still
                # queued: it reads that branch's head when it starts, and a running one approves only
                # the head it read, so later commits there get their own review.
                named = worktree.named_refs(self.p, t) if branch and not ext else []
                if task["id"] in dependency_ids(t) or (branch and branch in named) or (
                        branch and not ext and t["status"] == "queued"
                        and branch in worktree.fix_branches(self.p, t, named)):
                    if ext:
                        self._name_external_head(task, t, ext)
                    else:
                        self._name_new_head(task, t)
                        self._precheck_open(task, t)
                    return t["id"], False
            if ext and (seen := self._reviewed_external(task, ext)):
                return seen, False
            # No branch on record: the change is the PR it published, read from the hosting service.
            pr_url = "" if branch or ext else prguard.task_pr(task)
            published = push.pr_change(self.p, pr_url) if pr_url else None
            # A review that already approved this exact head (it ran while the task waited on its own
            # checks) passed it: a second review of the same head is a wasted run.
            tip = published["sha"] if published else ext["head"] if ext else (worktree._git(
                self.p.root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False) if branch else "")
            passed = db.one("SELECT q.task FROM push_queue q JOIN tasks t ON t.id=q.task WHERE q.head=? AND "
                            "t.kind='review' AND q.status!='cancelled' ORDER BY q.id DESC LIMIT 1",
                            (tip,)) if tip else None
            if passed:
                return passed["task"], False
            # Or one accepted it without the push queue (review only, an own push): its verdict stands.
            # A review that would push takes only a delivered head's verdict (reviewreuse.pushes).
            prior = reviewreuse.accepted(db, tip, reviewreuse.check_key(cfg),
                                         p=self.p if reviewreuse.pushes(cfg) else None) if tip else None
            if prior:
                return prior["id"], False
            if not add:
                return None
            if published:
                changes, full = published["changes"], published["sha"]
            elif ext:
                changes, full = None, ext["head"]
            else:
                changes = worktree.diff_lines(self.p, [branch]) if branch else None
                full = worktree._git(self.p.root, "rev-parse", branch) if changes else ""
            head = full[:12]
        except Exception as e:
            log(self.p, f"task {task['id']}: no review queued, its diff was not read: {e}")
            return None
        if (not changes and not ext) or coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
            return None
        title = f"Review #{task['id']}: {task['title']}"[:200]
        dup = db.one("SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled')", (title,))
        if dup:
            return dup["id"], False
        # A fix after a failed review is re-reviewed as its continuation: the reviewer gets the earlier
        # findings, and the review runs standard: a failed review shows the change needs the closer look.
        chain, c = {task["id"]}, continues_id(task)
        while c is not None and c not in chain:
            chain.add(c)
            c = continues_id(db.task(c) or {"labels": "[]"})
        prior = next((r for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status='failed' ORDER BY id DESC")
                      if r["id"] in chain or chain & set(dependency_ids(r))), None)
        tier = "standard" if prior or ext else bud.review_tier(changes, cfg)
        pr = "" if ext else pr_url or task.get("pr_url")
        lines = [f"Independently review the change of code task #{task['id']} ({task['title']}): "
                 + (f"repo {ext['repo']}, " if ext else "") + "branch "
                 f"{published['ref'] if published else ext['branch'] if ext else branch}, head {head}"
                 + (f", PR {pr}" if pr else "") + "."]
        if ext:
            lines += [extchange.fetch_line(ext), extchange.NO_PUSH_LINE]
        elif published:
            lines.append(f"No branch of it is on record in this project, so its diff is not in the project root's "
                         f"git: read it with `gh pr diff {pr}`, or check out head {full} in a clone of the PR's "
                         f"repository of your own.")
        lines += [f"Its spec: {coord.clip(task.get('spec'), AUTO_REVIEW_SPEC_CHARS)}",
                  f"Its hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"]
        fups = load_result(task.get("result")).get("followups")
        fups = [str(f["title"])[:120] for f in fups if isinstance(f, dict) and f.get("title")] \
            if isinstance(fups, list) else []
        if fups:
            lines.append("Its proposed follow-ups (the coordinator queues them separately; not part of this "
                         "change, so not a finding by themselves): " + "; ".join(fups[:MAX_FOLLOWUPS]))
        # The last hop of a split chain: one review of the whole chain at its final head.
        hops = chaincap.split_hops(db, task)
        if hops:
            lines.append(f"It is the last hop of a split continue chain: " + ", ".join(
                f"#{h['id']} (head {chaincap.split_head(h)[:12]})" for h in hops) + " ended unfinished, with no "
                f"review of their own. Head {head} of this branch carries their commits: review the chain's whole "
                f"change as one. The chain's first task's spec (#{hops[0]['id']}): "
                + coord.clip(hops[0].get("spec"), AUTO_REVIEW_SPEC_CHARS))
            hfups = [(h, load_result(h.get("result")).get("followups")) for h in hops]
            earlier = [f"#{h['id']}: {str(f['title'])[:120]}" for h, fs in hfups if isinstance(fs, list)
                       for f in fs if isinstance(f, dict) and f.get("title")]
            if earlier:
                lines.append("The earlier hops' follow-ups (each `continue:` one is a later hop of this chain; the "
                             "rest are not findings by themselves): " + "; ".join(earlier[:3 * MAX_FOLLOWUPS]))
        failed = reviewstats.failed_head(db, prior["id"]) if prior else ""
        if prior and failed and not full.startswith(failed[:12]):
            lines.append(f"Review #{prior['id']} failed on an earlier head. "
                         + reviewstats.delta_line(failed, head, head, "each of its findings"))
        elif prior:
            lines.append(f"Review #{prior['id']} failed on an earlier head: check each of its findings is fixed, "
                         f"then review what changed since.")
        if not published and not ext and (line := self._verified_review_line(task, full)):
            lines.append(line)
        lines.append(REVIEW_CHECK_LINE)
        lines.append("Test it with the task's own test selection (the exact test ids or `-k` expression its "
                     "spec, hand-off and `ttp checks` commands name), never a marker the repo may not define. "
                     "With `TTP_NO_DEVICE=1` open a device only through the device broker, `ttp devq` or "
                     "`ttp lock` on a device resource, never directly.")
        # A head its PR already carries is delivered: a pass closes the review, with no push to the
        # push branch or approval for the push queue (that branch may be unrelated, or not exist yet).
        delivered = "" if ext else pr if published else push.delivered_pr(self.p, task, full)
        # A task that delivers onto a pr_branch other than the push branch goes to that PR with
        # `ttp push --own`: a plain push or a push-queue approval would rebase it onto the push branch.
        elsewhere = "" if delivered or ext else push.pr_elsewhere(self.p, task, d)
        # Unless it means to create that branch (its spec asks for `ttp push --own`), a pr_branch the push
        # remote has no branch of is ignored, as the push queue's check_approval ignores it.
        # Under delivery.own_push existing_only `ttp push --own` cannot create it either, whatever the spec asks:
        # with no queue to take it instead (or a rebase, which never lands on the push branch) it is review only.
        ignored, own_mode, uncreatable = "", own_push(cfg), ""
        queued = elsewhere and not worktree.rebased_branch(task) and pushq.enabled(self.p, cfg) \
            and (own_mode == "existing_only" or not settinghold.asks_own(task))
        if (queued or (elsewhere and own_mode == "existing_only")) \
                and (tgt := pushq.target(self.p)) and pushq._absent(self.p, tgt[0], elsewhere):
            if queued:
                ignored, elsewhere = f"Its pr_branch label {elsewhere} was ignored: {tgt[0]} has no such branch.", ""
            else:
                uncreatable = (f"{tgt[0]} has no branch {elsewhere}, and own pushes publish only onto a branch the "
                               f"remote already has (delivery.own_push = existing_only)")
        # Own pushes off: a pr_branch or rebases: task lands on the push branch like any other (pr_elsewhere).
        if own_mode == "off" and not delivered and not ext and (onto := push.labelled_elsewhere(self.p, task, d)):
            ignored = (f"Its branch label {onto} was ignored: own pushes are off in this project "
                       f"(delivery.own_push), so it lands on {d.get('push_branch')} like any other change.")
        # A change that must not reach the push branch (its diff is all push-excluded, or its spec or
        # hand-off forbids it) is review only too: a pass would push what the push refuses or the spec bans.
        off = "" if delivered or elsewhere or ext or not d.get("push_branch") else push.kept_off(task, changes, d, self.p)
        # A push branch no push can ever reach (main/master without allow_protected_push_branch, or a
        # code repo without the remote) makes every review review only: a push step there would only
        # exit 2 and block the review, and a raw `git push` would skip the checks and the queue.
        held: dict[int, int] = {}
        never = "" if delivered or elsewhere or off or ext or not (d.get("push_branch") and d.get("push_allowed", True)) \
            else push.push_branch_problem(d["push_branch"], self.p.root, push.allow_protected(d))
        # A local push branch of a code repo with no git remote lands with `ttp land` (land.py) instead.
        lands = land.local_branch(self.p, self.p.root) if never and full and not (pr or published) else ""
        pushes = bool(d.get("push_branch") and d.get("push_allowed", True)) and not delivered and not elsewhere \
            and not off and not never and not ext
        if ext:
            lines.append(extchange.REVIEW_ONLY)
        elif delivered:
            lines.append(f"Its head is already delivered as PR {delivered}: review only. If it passes, hand off "
                         f"`done`; do not run `ttp push` or approve it for the push queue. Leave the branch and "
                         f"the PR as they are.")
        elif uncreatable:
            lines.append(f"It delivers onto branch {elsewhere}, not {d['push_branch']}, but {uncreatable}. Review "
                         f"only: if it passes, hand off `done`; do not run `ttp push` or approve it for the push "
                         f"queue. Leave the branch" + (" and the PR" if pr else "") + " as they are.")
        elif elsewhere and (old := worktree.rebased_branch(task)) and d.get("push_allowed", True) \
                and rebase_replace(cfg) != "lease":
            lines.append(f"It rebases {old} and delivers onto its own branch {elsewhere}, not {old} (a rebase "
                         f"cannot fast-forward it) and not {d['push_branch']}. If it passes, publish it with "
                         f"`ttp push --own --detach` from the change's worktree; never run plain `ttp push` and do "
                         f"not approve it for the push queue.")
        elif elsewhere and (old := worktree.rebased_branch(task)) and d.get("push_allowed", True):
            lines.append(f"It rebases {old} and delivers back onto {elsewhere}, not {d['push_branch']}. If it "
                         f"passes, publish it with `ttp push --own --detach` from the change's worktree: a rebase "
                         f"cannot fast-forward {old}, so that push replaces it only when the change holds every "
                         f"commit {old} gained since the rebase started (else it refuses, naming them: the "
                         f"change needs rebasing again), then under a lease on the tip it reads. "
                         f"Never force-push it another way, never run plain `ttp push` and do not approve it for "
                         f"the push queue.")
        elif elsewhere and d.get("push_allowed", True):
            lines.append(f"It delivers onto its pr_branch {elsewhere}, not {d['push_branch']}. If it passes, "
                         f"publish it there with `ttp push --own --detach` from the change's worktree; never run "
                         f"plain `ttp push` (it would rebase the work onto {d['push_branch']}) and do not approve "
                         f"it for the push queue.")
        elif elsewhere:
            lines.append(f"It delivers onto its pr_branch {elsewhere}, not {d['push_branch']}. Review only: if it "
                         f"passes, hand off `done`; do not run `ttp push` or approve it for the push queue. Leave "
                         f"the branch" + (" and the PR" if pr else "") + " as they are.")
        elif off:
            lines.append(f"It must not reach {d['push_branch']}: {off}. Review only: if it passes, hand off "
                         f"`done`; do not run `ttp push` or approve it for the push queue. Leave the branch"
                         + (" and the PR" if pr else "") + " as they are.")
        elif lands:
            lines.append(land.step(lands, full))
        elif never:
            lines.append(f"Nothing can be pushed: {never.removeprefix('delivery.push_branch: ')}. Review only: if it "
                         f"passes, hand off `done`; do not run `ttp push` or approve it for the push queue, and "
                         f"never fall back to a raw `git push` to {d['push_branch']}. Leave the branch"
                         + (" and the PR" if pr else "") + " as they are.")
        elif pushes:
            lines.append((f"{ignored} " if ignored else "") + f"If it passes, push it with `ttp push` from the "
                         f"change's worktree (it publishes to {d['push_branch']}).")
            # Named now, so a review that passes need not be woken again by the queue's refusal.
            if carried := pushq.carried(self.p, task, full, cfg):
                lines.append(inherited_line(carried))
                # A carried change whose fix is still open: its reviewer could only repeat the open
                # finding, so the review waits for that fix chain (reviewhold).
                held = reviewhold.blocking(db, carried, skip={task["id"]})
        else:
            lines.append("Review only: leave the branch" + (" and the PR" if pr else "") + " as they are.")
        checks = self._precheck(task, full) if pushes else None
        if checks:
            lines.append(self._precheck_line(task, head, checks))
        if notes := own_free(str(rules.get("auto_notes") or ""), own_mode):
            lines.append(notes)
        own_step = bool(elsewhere and not uncreatable and d.get("push_allowed", True))
        lines.append("Return the verdict and findings" + (", and the pushed commit." if pushes or own_step else
                                                          ", and the landed commit." if lands else "."))
        # A review that publishes with `ttp push --own` carries the branch it publishes onto, as its subject does.
        labels = [f"auto_review:{task['id']}"] + ([f"continues:{prior['id']}"] if prior else []) \
            + ([PRECHECK_LABEL + checks.name] if checks else []) + ([push.REVIEW_ONLY_LABEL] if elsewhere or off or never or ext else []) + ([push.LAND_LABEL] if lands else []) \
            + ([f"pr_branch:{elsewhere}"] if own_step else []) + reviewhold.labels(full, held) \
            + ([extchange.HEAD_LABEL + ext["head"]] if ext else [])
        rid = db.add_task(title, "\n".join(lines), kind="review", tier=tier, priority=2, origin="daemon",
                          budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                          depends_on=[task["id"]], labels=labels, **pin_of(task))
        log(self.p, f"task {task['id']}: queued review #{rid} ({tier})" + (
            f", held until the fix of {', '.join(f'#{t} (#{f})' for t, f in sorted(held.items()))} lands or "
            f"its chain ends" if held else ""))
        return rid, True

    def _external_change(self, task: dict) -> dict | None:
        """The change code task `task`'s hand-off names as published in another repository
        (extchange.parse), or None; an invalid entry is logged with its reason and ignored."""
        ch, why = extchange.parse(load_result(task.get("result")).get(extchange.FIELD))
        if why:
            log(self.p, f"task {task['id']}: hand-off `{extchange.FIELD}` ignored: {why}")
        return ch

    def _reviewed_external(self, task: dict, ch: dict) -> int | None:
        """The review, open or ended (not cancelled), already queued for code task `task`'s external
        head ch["head"] (extchange.HEAD_LABEL): one head gets one review, across sweeps, restarts and
        repeated hand-offs. None when there is none."""
        for r in self.p.db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled' AND labels LIKE ? "
                             "ORDER BY id", (f'%"{extchange.HEAD_LABEL}%',)):
            labels = push._labels(r)
            if (f"auto_review:{task['id']}" in labels or task["id"] in dependency_ids(r)) \
                    and any(extchange.same_head(h, ch["head"]) for h in extchange.heads(labels)):
                return r["id"]
        return None

    def _name_external_head(self, task: dict, review: dict, ch: dict) -> None:
        """A queued review that covers code task `task` (the re-review _fix_failed_review queued with its
        fix) gets the external head the task's hand-off published, once per head: its label, for
        _reviewed_external, and a spec line saying exactly what to fetch and review."""
        labels = push._labels(review)
        if review["status"] != "queued" or any(extchange.same_head(h, ch["head"]) for h in extchange.heads(labels)):
            return
        spec = (f"{review.get('spec') or ''}\nNew head: #{task['id']} published its fix. "
                + extchange.fetch_line(ch))
        self.p.db.update_task(review["id"], spec=spec, labels=[*labels, extchange.HEAD_LABEL + ch["head"]])
        review["spec"] = spec

    def _verified_review_line(self, task: dict, full: str) -> str:
        """The review spec's line on the review marker the code task inherits (verified.py): review only
        what came after it while it is an ancestor of the head, else the whole change. "" without one."""
        try:
            m = verified.scope(self.p.db, task, self.p.root, full, "review")
        except Exception as e:
            log(self.p, f"task {task['id']}: inherited marker not read: {e}")
            return ""
        if not m:
            return ""
        if not m["ok"]:
            return f"Inherited marker: {m['line']}: review the whole change, inherited commits included."
        return (f"Inherited marker: {m['line']}. Review only the commits after it (`git log {m['sha'][:12]}.."
                f"{full[:12]}`); do not review or re-test the inherited commits again. The push checks still run "
                f"in full on the commit that is pushed.")

    def _mark_verified(self, task: dict, approval: dict | None, extra: dict) -> None:
        """A passing review records the head it accepted as the reviewed code tasks' `review` marker
        (verified.py); a finished task's hand-off shows the marker it reached. Never fails the hand-off."""
        try:
            if task["kind"] == "review":
                heads = [e["head"] for e in (approval or {}).get("entries") or [] if e.get("head")]
                head = heads[0] if heads else worktree.reviewed_head(self.p, task)
                if not head:
                    return
                for t in push._reviewed(self.p, str(task["id"])):
                    verified.record(self.p.db, t["id"], "review", head, review=task["id"])
                extra["verified_through"] = head
            elif m := verified.own(self.p.db, task["id"], "checks"):
                extra["verified_through"] = m["sha"]
        except Exception as e:
            log(self.p, f"task {task['id']}: verified-through marker not recorded: {e}")

    def _reuse_review(self, task: dict) -> bool:
        """A review about to start whose head (worktree.reviewed_head) a review already accepted under
        the same checks takes that verdict and closes with no run (reviewreuse.reuse), unless it also
        covers a head that differs from it (reviewreuse.rebased_away): True. Else it
        records the head and check set it starts on, so a later review of that head can reuse it; a
        review forced past an accepted one runs, as does a review of an external change (extchange). False
        whenever it runs."""
        db = self.p.db
        if extchange.heads(push._labels(task)):
            return False   # its change lives in another repository: the project's git holds no head of it
        try:
            sha = worktree.reviewed_head(self.p, task)
        except Exception as e:
            log(self.p, f"review {task['id']}: its head was not read, so it is not checked for reuse: {e}")
            return False
        if not sha:
            return False
        key = reviewreuse.check_key(self.cfg)
        labels = json.loads(task["labels"] or "[]")
        prior = None if reviewreuse.FORCED_LABEL in labels else reviewreuse.accepted(
            db, sha, key, skip=task["id"], p=self.p if reviewreuse.pushes(self.cfg, labels) else None)
        if prior and (why := reviewreuse.rebased_away(self.p, task, sha)):
            log(self.p, f"review {task['id']}: not reusing review #{prior['id']}'s verdict on {sha[:12]}: {why}")
            prior = None
        if not prior:
            db.update_task(task["id"], labels=reviewreuse.with_head(labels, sha, key))
            return False
        with db.tx():
            text = reviewreuse.reuse(db, task, prior, sha, key)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", "task_done", "normal", text[:coord.EVENT_CHARS], "handled",
                  task["id"]))
        log(self.p, f"review {task['id']}: reused the verdict of review #{prior['id']} on {sha[:12]}; no run")
        return True

    def _precheck_line(self, task: dict, head: str, checks: Path) -> str:
        """The review spec's line on the checks _precheck started on the code task's head."""
        return (f"The daemon started the project's checks (`ttp checks`) on head {head} in "
                f"{self.p.worktrees / ('t' + str(task['id']))} as #{task['id']} finished, and starts this review "
                f"once they end: `ttp checks --result {checks}` prints their exit code and output tail "
                f"({checks / 'checks.log'} has it all). A pass is recorded, so `ttp checks` there reuses it. "
                f"The push queue runs the full checks again on the commit it pushes: run only focused tests "
                f"of what changed.")

    def _name_new_head(self, task: dict, review: dict) -> None:
        """A queued re-review of a failed review gets the new head its fix ended on, and the size of
        the fix delta from the failed head (reviewstats): once per head."""
        c, branch = continues_id(review), task.get("branch")
        if review["status"] != "queued" or review["kind"] != "review" or c is None or not branch:
            return
        try:
            failed = reviewstats.failed_head(self.p.db, c)
            full = worktree._git(self.p.root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False)
            if not failed or not full or full.startswith(failed) or f"New head: {branch} at {full[:12]}" in (
                    review.get("spec") or ""):
                return
            line = reviewstats.new_head_line(self.p, failed, branch, full)
        except Exception as e:
            log(self.p, f"task {task['id']}: review #{review['id']} not told its new head: {e}")
            return
        self.p.db.update_task(review["id"], spec=f"{review.get('spec') or ''}\n{line}")
        review["spec"] = f"{review.get('spec') or ''}\n{line}"

    def _precheck_open(self, task: dict, review: dict) -> None:
        """An open review queued before the code task finished (the re-review _fix_failed_review
        queues with its fix, or one the coordinator queued ahead) gets the checks started on the head
        it will review, as a review _auto_review queues does: once per head, while it is still queued."""
        d = self.cfg.get("delivery") or {}
        branch = task.get("branch")
        if review["status"] != "queued" or review["kind"] != "review" or not branch \
                or not (d.get("push_branch") and d.get("push_allowed", True)) \
                or push.REVIEW_ONLY_LABEL in push._labels(review):
            return
        try:
            full = worktree._git(self.p.root, "rev-parse", branch)
            if push.delivered_pr(self.p, task, full):
                return
        except Exception as e:
            log(self.p, f"task {task['id']}: checks not started for review #{review['id']}: {e}")
            return
        labels = push._labels(review)
        if f"{PRECHECK_LABEL}t{task['id']}-{full[:12]}" in labels:
            return
        checks = self._precheck(task, full)
        if not checks:
            return
        self.p.db.update_task(review["id"],
                              spec=f"{review.get('spec') or ''}\n{self._precheck_line(task, full[:12], checks)}",
                              labels=[x for x in labels if not x.startswith(PRECHECK_LABEL)]
                              + [PRECHECK_LABEL + checks.name])

    def _precheck(self, task: dict, full: str) -> Path | None:
        """With the push queue on, the project's checks (`ttp checks --detach`) started on the code
        task's head in its worktree as its review is queued, so the reviewer reads their result in its
        first run instead of running the suite itself and handing off waiting; the pass they record
        lets the reviewer's own `ttp checks` there reuse it. The folder they write to (as a run's
        directory), or None when nothing started: the queue is off, the project sets no checks, or
        the worktree is gone or holds another head."""
        d = self.cfg.get("delivery") or {}
        if not full or not pushq.enabled(self.p, self.cfg) or not push.all_checks(d):
            return None
        path = self.p.worktrees / f"t{task['id']}"
        try:
            if not worktree.is_git(path) or worktree._git(path, "rev-parse", "HEAD") != full:
                return None
        except Exception:
            return None
        base = self.p.state / PRECHECKS_DIR
        self._prune_prechecks(base)
        out = base / f"t{task['id']}-{full[:12]}"
        if not self._start_checks(path, out, f"precheck-t{task['id']}", task["id"], []):
            return None
        log(self.p, f"task {task['id']}: started the checks on {full[:12]} for its review ({out})")
        return out

    @staticmethod
    def _prune_prechecks(base: Path) -> None:
        """Pre-started checks that ended over PRECHECK_KEEP_S ago go; running ones stay."""
        from .cli import _checks_alive, _read_checks_pid
        try:
            old = [x for x in base.iterdir() if x.is_dir() and time.time() - x.stat().st_mtime > PRECHECK_KEEP_S]
        except OSError:
            return
        for x in old:
            if not _checks_alive(_read_checks_pid(x)):
                shutil.rmtree(x, ignore_errors=True)

    def _precheck_holds(self, task: dict, now: float) -> bool:
        """A review waits for the checks the daemon started on its head (_precheck) while they run,
        at most PRECHECK_HOLD_S after they started: the reviewer then reads their result at once."""
        if task["kind"] != "review":
            return False
        name = next((x[len(PRECHECK_LABEL):] for x in push._labels(task) if x.startswith(PRECHECK_LABEL)), "")
        if not name or "/" in name:
            return False
        from .cli import CHECKS_RC, _checks_alive, _read_checks_pid
        out = self.p.state / PRECHECKS_DIR / name
        info = _read_checks_pid(out)
        return (not (out / CHECKS_RC).exists() and _checks_alive(info)
                and now - float(info.get("ts") or 0) < PRECHECK_HOLD_S)

    def _precheck_failed(self, task: dict) -> bool:
        """A review whose checks the daemon started on its head (_precheck) failed on that head, still
        the branch's tip: failed model-free, with the failing test ids and the end of checks.out as its
        verdict (review_failure precheck) and one follow-up to fix them, so _finish_worker queues the
        fix and re-review as for any failed review with fixes (_fix_failed_review). It counts toward
        that per-change round cap (AUTO_FIX_ROUNDS), not toward review_area_cap: no reviewer judged the
        design (reviewcap.unjudged). False, and the reviewer runs as before, while they run or for
        anything but a test failure: a pass, an environment fault (exit 75, an env_problem, as
        `ttp checks` reports a missing runner), a missing target or selector (check:<sig>), a timeout
        or kill, a head that moved, or a failure shared with other changes (sharedchecks), whose
        incident path holds the reviewer's own checks."""
        name = next((x[len(PRECHECK_LABEL):] for x in push._labels(task) if x.startswith(PRECHECK_LABEL)), "")
        m = re.fullmatch(r"t(\d+)-[0-9a-f]{12}", name)
        if not m:
            return False
        from .cli import CHECKS_OUT, CHECKS_RC
        out = self.p.state / PRECHECKS_DIR / name
        try:
            rc = (out / CHECKS_RC).read_text().strip()
            fail = json.loads((out / "check_failure.json").read_text())
        except (OSError, ValueError):
            return False
        if rc != "1" or not isinstance(fail, dict) or fail.get("kind") != "check_failed" or fail.get("env_problem") \
                or not isinstance(fail.get("rc"), int) or fail["rc"] <= 0 or fail["rc"] in PRECHECK_KILLED_RCS:
            return False
        head, code = str(fail.get("head") or ""), self.p.db.task(int(m[1]))
        try:
            tip = worktree._git(self.p.root, "rev-parse", "--verify", "--quiet", f"{code['branch']}^{{commit}}",
                                check=False) if code and code["branch"] else ""
        except Exception:
            tip = ""
        if not head or tip != head:
            return False
        db, now, output = self.p.db, time.time(), str(fail.get("output") or "")
        ids, complete = sharedchecks.failing_ids(output)
        try:
            if ids:
                sigs = sharedchecks.failure_sigs(output, ids)
                sharedchecks.record(db, task["id"], head, ids, now, sigs)
                if complete and sharedchecks.shared(db, task["id"], head, ids, now, self.cfg,
                                                    pushq.bad_tip_ids(db), sigs):
                    return False
        except Exception as e:   # unread: the reviewer runs, as before
            log(self.p, f"review {task['id']}: its failed precheck was not classified: {e}")
            return False
        try:
            tail = "\n".join((out / CHECKS_OUT).read_text(errors="replace").splitlines()[-PRECHECK_TAIL_LINES:])
        except OSError:
            tail = "\n".join(output.splitlines()[-PRECHECK_TAIL_LINES:])
        check = fail.get("check")
        cmd = str(check.get("run") or "") if isinstance(check, dict) else str(check or "")
        failing = ", ".join(ids[:20]) + (f" and {len(ids) - 20} more" if len(ids) > 20 else "") if ids \
            else "no pytest ids in its output"
        said = (f"The project's checks (`ttp checks`) the daemon ran on head {head[:12]} before this review failed"
                + (f" ({cmd}, exit {fail['rc']})" if cmd else f" (exit {fail['rc']})")
                + f"; no reviewer ran. Failing tests: {failing}. The end of {out / CHECKS_OUT}:\n{tail}")
        fix = {"title": f"Make the project's checks pass on {head[:12]}",
               "spec": (f"{said}\nFix the change so these checks pass (or a test, if the test is what is wrong), "
                        f"run the failing tests, then the full checks, and commit.")}
        result = {"status": "failed", "summary": said, REVIEW_FAILURE_KEY: "precheck",
                  sharedchecks.FAILING_KEY: ids, sharedchecks.COMPLETE_KEY: complete, "followups": [fix]}
        from .providers.base import RunUsage
        with db.tx():
            self._finish_worker({"task": task["id"], "id": None, "note": "{}"}, RunUsage(), "ok", out, "model-free",
                                handoff=result)
        log(self.p, f"review {task['id']}: its precheck failed on {head[:12]} ({len(ids)} failing test(s)); "
                    f"failed with no reviewer run")
        return True

    def _hop_cells(self, task: dict, result: dict, missing: list[str]) -> tuple[list[str], list[str]]:
        """cells.hop_cells for done `task`'s missing cells: the tried entries are its chain's earlier
        hops' from the last cells.REJECTED_S, oldest first, then its own hand-off's; the open tasks are
        every other unfinished task but reviews (a review's spec may quote the reviewed line)."""
        db = self.p.db
        items: list = []
        since = time.time() - cells.REJECTED_S
        for t in chaincap.chain(db, task["id"])[:-1]:
            if float(t.get("updated") or t.get("created") or 0) >= since:
                got = load_result(t.get("result")).get("tried")
                items += got if isinstance(got, list) else []
        own = result.get("tried")
        items += own if isinstance(own, list) else []
        marks = ",".join("?" * len(TERMINAL_TASK_STATES))
        open_tasks = db.q(f"SELECT id, spec FROM tasks WHERE status NOT IN ({marks}) AND kind != 'review' "
                          f"AND id != ?", (*TERMINAL_TASK_STATES, task["id"]))
        return cells.hop_cells(missing, result, items, open_tasks)

    def _continue_ready(self, task: dict, fups: list[dict]) -> str:
        """chaincap.hop_ready for a done task whose only proposal is one `continue:` follow-up; ''
        otherwise or when it cannot be read (the coordinator decides then)."""
        if len(fups) != 1 or upstream.is_note(fups[0]) or fups[0].get("start_after") or fups[0].get("start_when") \
                or not chaincap.CONT_TITLE.match(str(fups[0]["title"])) or chaincap.post_landing(fups[0], task["id"]):
            return ""
        try:
            return chaincap.hop_ready(self.p, self.cfg, task, fups[0])
        except Exception:
            log(self.p, f"continue hop after #{task['id']}: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return ""

    def _auto_continue(self, task: dict, fups: list[dict], why: str) -> tuple[int, str] | None:
        """chaincap.auto_hop for a task _continue_ready passed; None when it cannot be queued."""
        try:
            return chaincap.auto_hop(self.p, self.cfg, task, fups[0], why)
        except Exception:
            log(self.p, f"continue hop after #{task['id']}: " + traceback.format_exc().replace("\n", " | ")[:1000])
            return None

    def _split(self, task: dict, result: dict, fups: list[dict]) -> str:
        """The head a done code task left unfinished, labelled chaincap.SPLIT_LABEL on it, or '': a
        branch with changes not yet landed under other commits, and a `continue:` follow-up that is no
        post-landing step (chaincap.post_landing: it would wait on this head landing, which only its
        review brings) or a hand-off that marks the head WIP or split (chaincap.marked_split)."""
        if chaincap.split_head(task):
            return chaincap.split_head(task)
        cont = any(chaincap.CONT_TITLE.match(str(f.get("title") or "")) and not upstream.is_note(f)
                   and not chaincap.post_landing(f, task["id"]) for f in fups)
        branch = task.get("branch")
        if not branch:
            return ""
        try:
            head = worktree._git(self.p.root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False)
            subject = worktree._git(self.p.root, "log", "-1", "--format=%s", head, check=False) if head else ""
            if not head or not (cont or chaincap.marked_split(result, subject)) or not self._has_changes(task) \
                    or self.p.db.one("SELECT 1 FROM task_superseded WHERE task=?", (task["id"],)) \
                    or self._landed_elsewhere(task):
                return ""
            labels = json.loads(task.get("labels") or "[]")
            self.p.db.update_task(task["id"], labels=[*labels, f"{chaincap.SPLIT_LABEL}{head}"])
        except Exception as e:
            log(self.p, f"task {task['id']}: its split was not read: {e}")
            return ""
        log(self.p, f"task {task['id']}: split at {head[:12]}: no review until its last continue hop finishes")
        return head

    def _has_changes(self, task: dict) -> bool:
        """Whether a task's branch changes anything against the base; True when that cannot be read."""
        if not task.get("branch"):
            return False
        try:
            return bool(worktree.diff_lines(self.p, [task["branch"]]))
        except Exception:
            return True

    def _review_area(self, review: dict) -> dict | None:
        """reviewcap.area for a review failing now; None when it cannot be read (a fix still follows)."""
        try:
            return reviewcap.area(self.p, self.cfg, review)
        except Exception as e:
            log(self.p, f"review {review['id']}: its area's failures were not counted: {e}")
            return None

    def _fix_failed_review(self, review: dict, fups: list[dict], summary: str) -> tuple[int, int, list[int]] | None:
        """A review that failed with fix specs, handled the way the coordinator would: one code task
        fixes them all on top of the reviewed branch, and a re-review that waits on it takes over the
        failed review's dependents: (fix id, re-review id, the dependents moved). None when auto
        reviews are off, the review covers no single code branch, AUTO_FIX_ROUNDS reviews of this
        stack already failed, a task cap is reached or a task already continues it: the coordinator
        then decides, and the dependents are blocked as before. Why no fix was queued is left in
        self._nofix ('' when nothing needs raising: auto reviews off, a task already continues it,
        or its change already landed, which has its own notice), for deadwait.raise_nofix.
        The covered code tasks are those it depends on and those its spec names; when they are not
        one stack (a coordinator note naming another change's branch), those it depends on decide.
        A review of a change published in another repository goes to _fix_external_review."""
        cfg, db = self.cfg, self.p.db
        d = cfg.get("delivery") or {}
        self._nofix = ""
        if not (cfg.get("review") or {}).get("auto", True) or not (d.get("review_before_pr", True) or d.get("push_branch")):
            return None
        # A re-review repeats the stack's first review's spec, not the nested specs of the rounds since.
        rounds, seen, c, first = 1, {review["id"]}, continues_id(review), review
        while c is not None and c not in seen and (t := db.task(c)):
            seen.add(c)
            if t["kind"] == "review":
                rounds, first = rounds + 1, t
            c = continues_id(t)
        # One fix per failed review: also one the coordinator added by title, on its own branch label.
        if db.one("SELECT id FROM tasks WHERE labels LIKE ? OR (title LIKE ? AND status!='cancelled')",
                  (f'%"continues:{review["id"]}"%', f'Fix review #{review["id"]}:%')):
            return None
        if rounds > AUTO_FIX_ROUNDS:
            self._nofix = f"{rounds} reviews of this stack failed (the daemon fixes at most {AUTO_FIX_ROUNDS} rounds)"
            return None
        if coord.next_task_slot(db, coord.task_cap(cfg)) is not None \
                or coord.next_task_slot(db, coord.task_cap(cfg, True), review=True) is not None:
            self._nofix = "a 24 h task cap is reached"
            return None
        spec = review.get("spec") or ""
        deps = dependency_ids(review)
        cands = [t for i in sorted(coord._covered(db, deps, spec))
                 for t in [db.task(i)] if t and t["kind"] == "code" and t["branch"]]
        # A review of a change published in another repository is fixed there (_fix_external_review).
        if extchange.heads(push._labels(review) + push._labels(first)):
            ext = next(((t, ch) for i in sorted((i for i in deps if isinstance(i, int)), reverse=True)
                        if (t := db.task(i)) and t["kind"] == "code" and (ch := self._external_change(t))), None)
            if ext:
                return self._fix_external_review(review, first, ext[0], ext[1], fups, summary)
        code = None
        try:
            # A review of a stack (a fix on top of its change) is fixed on the stack's tip. A branch
            # its spec only mentions (another change it must fit with) is no part of the stack.
            for group in (cands, [t for t in cands if t["id"] in deps]):
                tips = [t for t in group if all(o is t or worktree._closest_ancestor(self.p, [o["branch"]], t["branch"])
                                                for o in group)]
                if tips:
                    code = tips[0]
                    break
            head = worktree._git(self.p.root, "rev-parse", "--short=12", code["branch"]) if code else ""
        except Exception as e:
            log(self.p, f"review {review['id']}: no fix queued, its branch was not read: {e}")
            self._nofix = f"its branch was not read ({coord.clip(str(e), 200)})"
            return None
        if not code:
            self._nofix = ("its findings span branches that are not one stack: "
                           + ", ".join(t["branch"] for t in cands)) if cands else "it covers no code task with a branch"
            return None
        if not head:
            self._nofix = f"branch {code['branch']} has no commit"
            return None
        if self._landed_elsewhere(code):
            return None
        base_title = re.sub(r"^(?:Fix review #\d+: )+", "", code["title"])
        # A stack kept off the push branch stays review only through its fix and re-review.
        only = push.REVIEW_ONLY_LABEL in push._labels(review) + push._labels(first)
        tier, tsrc = effort.fix_tier(db, code, review, fups)   # db.add_task: a fix never runs light
        rtier = "deep" if review["tier"] == "deep" else "standard"   # a re-review never runs light
        found = "\n".join(f"{n}. {str(f['title'])[:200]}: {coord.clip(f.get('spec'), FOLLOWUP_SPEC_CHARS)}"
                          for n, f in enumerate(fups, 1))
        # An open fix of the PR's findings (pr_findings_owner) on this stack takes these findings too:
        # a PR has one fixer.
        root, key = self._stack_root(code), prguard.pr_key(code.get("pr_url") or "")
        prfix = next((t for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status NOT IN (?,?,?) "
                                      "AND labels LIKE '%pr_fix:%' ORDER BY id",
                                      TERMINAL_TASK_STATES)
                      if '"pr_fix:' in t["labels"] and (self._stack_root(t) == root
                                                       or (key and f'"pr_fix:{key}"' in t["labels"]))), None)
        # An open continue hop that builds on the reviewed branch (a split's hop) finishes that branch:
        # it takes these findings, and the re-review waits on it. A separate fix would be a second
        # task continuing the same work.
        hop = None if prfix else self._open_hop(code)
        with db.tx():
            if prfix or hop:
                fid = (prfix or hop)["id"]
                fbranch = (prfix or hop)["branch"] or f"ttp/t{fid}-{worktree.slug((prfix or hop)['title'])}"
            else:
                fid = db.add_task(f"Fix review #{review['id']}: {base_title}"[:200], "", kind="code", tier=tier,
                                  tier_source=tsrc, priority=review["priority"], origin="daemon",
                                  budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                                  labels=[f"continues:{code['id']}", f"review_fix:{review['id']}"], **pin_of(code))
                fbranch = f"ttp/t{fid}-{worktree.slug(base_title)}"
            # The stack's pr_branch goes with its `ttp push --own` step: the re-review carries it as a label,
            # and its spec names the fix's branch to publish from (the reviewed branch lacks the fixes).
            # Whether the first review had that step is read from its pr_branch label, or (a review older
            # than the label) from the daemon's own step sentence after REVIEW_CHECK_LINE, never from the
            # code task's spec and hand-off it embeds. Only that step sentence is replaced.
            first_spec = (first.get("spec") or "")[:REVIEW_FIX_SPEC_CHARS]
            onto = push.pr_elsewhere(self.p, prfix or db.task(fid), d) if d.get("push_allowed", True) else ""
            flines = first_spec.splitlines()
            cut = max((i + 1 for i, ln in enumerate(flines) if ln == REVIEW_CHECK_LINE), default=0)
            body, tail = flines[:cut], flines[cut:]
            publish = any(lb.startswith("pr_branch:") for lb in push._labels(first)) \
                or any(OWN_STEP.match(ln) for ln in tail)
            step = ""
            if publish and onto:
                step = (f"If it passes, publish #{fid}'s head onto its pr_branch {onto} with `ttp push --own --detach` "
                        f"from #{fid}'s worktree (branch {fbranch}), never from {code['branch']}, which lacks the "
                        f"fixes; never run plain `ttp push` and do not approve it for the push queue.")
            elif publish:
                tail = [ln for ln in tail if not OWN_STEP.match(ln)]
                was = push.labelled_elsewhere(self.p, prfix or db.task(fid), d)
                if own_push(cfg) == "off":
                    # As the first review does with own pushes off: the stack lands on the push branch
                    # like any other change, unless nothing may reach it.
                    tail = [x for ln in tail if (x := own_free(ln, "off"))]
                    pb = str(d.get("push_branch") or "").strip()
                    why = "nothing may be pushed (delivery.push_allowed is off)" if not d.get("push_allowed", True) \
                        else "there is no push branch (delivery.push_branch)" if not pb else \
                        push.kept_off(code, worktree.diff_lines(self.p, [code["branch"]]), d, self.p) \
                        or push.push_branch_problem(pb, self.p.root, push.allow_protected(d)).removeprefix(
                            "delivery.push_branch: ")
                    if not why:
                        only = False
                        step = ((f"Its branch label {was} was ignored: own" if was else "Own")
                                + f" pushes are off in this project (delivery.own_push), so it lands on {pb} like "
                                f"any other change. If it passes, push #{fid}'s head with `ttp push` from #{fid}'s "
                                f"worktree (branch {fbranch}; it publishes to {pb}). This overrides any push step "
                                f"in the first review's spec.")
                    else:
                        why = f"own pushes are off in this project (delivery.own_push), and {why}"
                else:
                    why = f"#{fid} carries no pr_branch to publish onto"
                if not step:
                    step = (f"Review only: {why}. If it passes, hand off `done`; do not push it anywhere or approve "
                            f"it for the push queue. This overrides any push step in the first review's spec.")
            # A stack that lands with `ttp land` lands the fix's head, not the head the first review named.
            lands = land.local_branch(self.p, self.p.root) \
                if not step and push.LAND_LABEL in push._labels(review) + push._labels(first) else ""
            if lands:
                tail = [ln for ln in tail if not ln.startswith(land.STEP)]
                step = land.step(lands, f"<#{fid}'s head as you reviewed it>")
            first_spec = "\n".join(body + tail)
            rid = db.add_task(f"Re-review #{review['id']}: " + re.sub(r"^(?:Re-review #\d+: )+", "", review["title"]),
                              "\n".join([
                                  f"Re-review after review #{review['id']} failed. Fix task #{fid} (branch {fbranch}, "
                                  f"built on #{code['id']}'s branch {code['branch']} at {head}) addresses its findings:",
                                  found,
                                  reviewstats.delta_line(head, fbranch, f"the tip of {fbranch} once #{fid} is done")
                                  + f" Where the first review's "
                                  f"spec below names a branch or worktree of this stack (such as {code['branch']} or "
                                  f"#{code['id']}'s worktree), use #{fid}'s branch and worktree.",
                                  *([step] if step else []),
                                  f"The first review's spec (#{first['id']}):\n{first_spec}"]),
                              kind="review", tier=rtier, priority=review["priority"], origin="daemon",
                              budget_usd=float(cfg["budget"]["task_default_usd"].get(rtier, 8.0)),
                              depends_on=[fid], labels=[f"auto_review:{fid}", f"continues:{review['id']}",
                                                        f"{FIX_BASE_LABEL}{head}"]
                              + ([push.REVIEW_ONLY_LABEL] if only else []) + ([push.LAND_LABEL] if lands else [])
                              + ([f"pr_branch:{onto}"] if publish and onto else []),
                              **(pin_of(review) or pin_of(code)))
            if prfix or hop:
                owner = (f"failed too. This task is the PR's one fixer: fix its blocking findings as well." if prfix
                         else f"failed. This task continues that branch: fix its blocking findings as well, in this "
                              f"task's own work.")
                extra = "\n".join([
                    f"Review #{review['id']} ({review['title']}) of code task #{code['id']} (branch "
                    f"{code['branch']}, head {head}) {owner} Re-review #{rid} checks each once this task is done.",
                    f"Findings to fix:\n{found}",
                    f"The review's hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"])
                db.update_task(fid, spec=f"{(prfix or hop)['spec'] or ''}\n{extra}",
                               labels=[*json.loads((prfix or hop)["labels"] or "[]"), f"review_fix:{review['id']}"])
                for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running'", (fid,)):
                    if r["dir"]:
                        coord._append_update(Path(r["dir"], "steer.md"), extra, f"review-fix-{review['id']}")
            else:
                db.update_task(fid, branch=fbranch, spec="\n".join([
                    f"Fix the blocking findings of review #{review['id']} ({review['title']}) on code task "
                    f"#{code['id']} ({code['title']}).",
                    f"This task's branch starts from #{code['id']}'s branch {code['branch']} (head {head}): build on "
                    f"it. " + (f"It must not reach the push branch: do not push it. Re-review #{rid} checks each "
                               f"finding once this task is done." if only else
                               f"Leave the push to re-review #{rid}, which checks each finding once this task is done."),
                    f"Findings to fix:\n{found}",
                    f"The review's hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"]))
            moved = [t["id"] for t in coord._open_dependents(db, review["id"])]
            coord._take_over_dependents(db, review["id"], rid)
        log(self.p, f"review {review['id']} failed: "
                    + (f"PR fix #{fid} takes its findings" if prfix else f"continue hop #{fid} takes its findings"
                       if hop else f"queued fix #{fid}") + f" and re-review #{rid}"
                    + (f"; moved {moved} onto it" if moved else ""))
        return fid, rid, moved

    def _fix_external_review(self, review: dict, first: dict, code: dict, ch: dict, fups: list[dict],
                             summary: str) -> tuple[int, int, list[int]]:
        """_fix_failed_review for a review of code task `code`'s change published in another repository
        (`ch`, extchange): one fix task that publishes onto the same branch there and names its new head
        in its hand-off's `published`, and a review-only re-review that waits on it and takes over the
        failed review's dependents. Neither touches the project's own git."""
        cfg, db = self.cfg, self.p.db
        base_title = re.sub(r"^(?:Fix review #\d+: )+", "", code["title"])
        tier, tsrc = effort.fix_tier(db, code, review, fups)
        rtier = "deep" if review["tier"] == "deep" else "standard"
        found = "\n".join(f"{n}. {str(f['title'])[:200]}: {coord.clip(f.get('spec'), FOLLOWUP_SPEC_CHARS)}"
                          for n, f in enumerate(fups, 1))
        where = f"repo {ch['repo']}, branch {ch['branch']}"
        with db.tx():
            fid = db.add_task(f"Fix review #{review['id']}: {base_title}"[:200], "", kind="code", tier=tier,
                              tier_source=tsrc, priority=review["priority"], origin="daemon",
                              budget_usd=float(cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                              labels=[f"continues:{code['id']}", f"review_fix:{review['id']}"], **pin_of(code))
            rid = db.add_task(
                f"Re-review #{review['id']}: " + re.sub(r"^(?:Re-review #\d+: )+", "", review["title"]),
                "\n".join([
                    f"Re-review after review #{review['id']} failed. Fix task #{fid} addresses its findings on "
                    f"code task #{code['id']}'s change in an external repository ({where}, failed head "
                    f"{ch['head']}):", found,
                    f"Review the head #{fid}'s hand-off names in `{extchange.FIELD}` (added below once it is "
                    f"done): check each finding against `git diff {ch['head']}..<new head>` first, then what "
                    f"the fix could regress.", extchange.NO_PUSH_LINE, extchange.REVIEW_ONLY,
                    f"The first review's spec (#{first['id']}):\n{(first.get('spec') or '')[:REVIEW_FIX_SPEC_CHARS]}"]),
                kind="review", tier=rtier, priority=review["priority"], origin="daemon",
                budget_usd=float(cfg["budget"]["task_default_usd"].get(rtier, 8.0)),
                depends_on=[fid], labels=[f"auto_review:{fid}", f"continues:{review['id']}", push.REVIEW_ONLY_LABEL],
                **(pin_of(review) or pin_of(code)))
            db.update_task(fid, spec="\n".join([
                f"Fix the blocking findings of review #{review['id']} ({review['title']}) on code task "
                f"#{code['id']} ({code['title']}).",
                f"Its change lives in an external repository: {where}, head {ch['head']}. Work in your own "
                f"clone of it on top of that head, then publish the fix to the same branch {ch['branch']} of "
                f"{ch['repo']} (a fast-forward, never a force-push), and name the new head in your hand-off: "
                f'"{extchange.FIELD}": {{"repo": "{ch["repo"]}", "branch": "{ch["branch"]}", "head": "<new head sha>"}}. '
                f"Push nothing to this project's push branch, and open no pull request. Re-review #{rid} "
                f"checks each finding once this task is done.",
                f"Findings to fix:\n{found}",
                f"The review's hand-off: {coord.clip(summary, AUTO_REVIEW_SUMMARY_CHARS)}"]))
            moved = [t["id"] for t in coord._open_dependents(db, review["id"])]
            coord._take_over_dependents(db, review["id"], rid)
        log(self.p, f"review {review['id']} failed: queued fix #{fid} of the external change ({where}) and "
                    f"re-review #{rid}" + (f"; moved {moved} onto it" if moved else ""))
        return fid, rid, moved

    def _fix_without_commits(self, task: dict) -> list[dict]:
        """The queued re-reviews waiting on code task `task`, a fix of a failed review (review_fix:),
        whose branch adds no commit to the head their failed review saw (FIX_BASE_LABEL), each with
        that head as `base`. Empty when it added commits or that cannot be read."""
        if '"review_fix:' not in (task.get("labels") or "") or not task.get("branch"):
            return []
        out = []
        for r in self.p.db.q("SELECT * FROM tasks WHERE kind='review' AND status='queued' AND labels LIKE ?",
                             (f'%"{FIX_BASE_LABEL}%',)):
            base = next((lb[len(FIX_BASE_LABEL):] for lb in push._labels(r) if lb.startswith(FIX_BASE_LABEL)), "")
            if task["id"] not in dependency_ids(r) or not base:
                continue
            try:
                new = worktree._git(self.p.root, "rev-list", "--count", f"{base}..{task['branch']}", check=False)
            except Exception as e:
                log(self.p, f"task {task['id']}: its commits since {base} were not read: {e}")
                continue
            if new == "0":
                out.append(dict(r, base=base))
        return out

    def _open_hop(self, code: dict) -> dict | None:
        """The open code task, oldest first, that continues code task `code`'s branch (`continues:`, directly
        or through its chain), other than a fix of a review: a split's hop. None when there is none.
        A hop that waits (start_when or retry_when) on a task of that chain landing is no hop: it can
        add nothing to the branch before the change lands, which needs the fix first (review #3624)."""
        for t in self.p.db.q("SELECT * FROM tasks WHERE kind='code' AND status NOT IN (?,?,?) AND id>? "
                             "AND labels LIKE '%continues:%' ORDER BY id", (*TERMINAL_TASK_STATES, code["id"])):
            if '"review_fix:' in (t["labels"] or "") or '"pr_fix:' in (t["labels"] or ""):
                continue
            c, seen = continues_id(t), {t["id"]}
            while c is not None and c not in seen and c != code["id"] and (x := self.p.db.task(c)):
                seen.add(c)
                c = continues_id(x)
            waits = {r for probe in (deferral(t).get("when"), load_result(t["result"]).get("retry_when"))
                     for r in landed.task_refs(str(probe or ""))}
            if c == code["id"] and not waits & {str(i) for i in seen | {code["id"]}}:
                return t
        return None

    def pr_findings_owner(self, key: str, rec: dict, what: str) -> tuple[dict | None, bool]:
        """The one open task that owns a PR's open findings (pr-watch's record `rec` for PR `key`),
        and whether it was queued now. An open code task that delivers the PR, names it in its spec or
        works on its branch owns them. With none, and the task that delivered it done, the daemon
        queues one code task on the PR's branch to fix or answer each, so findings never outlive their
        owner. None, nothing queued, when that task did not finish done, PR_FIX_ROUNDS such tasks did
        not clear them or the task cap is reached: the coordinator then decides."""
        db = self.p.db
        branch = str(rec.get("branch") or "")
        for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status NOT IN (?,?,?) ORDER BY id",
                      TERMINAL_TASK_STATES):
            if prguard.pr_key(t["pr_url"] or "") == key or key in prguard.pr_keys(t["spec"] or "") \
                    or (branch and t["branch"] == branch) or f'"pr_fix:{key}"' in (t["labels"] or ""):
                return t, False
        src = db.task(int(rec.get("task") or 0))
        if not src or src["status"] != "done":
            return None, False
        # The delivering task's open review, or a fix or re-review of its stack, comes first: a PR fix
        # queued beside it would be a second owner (and a failed review queues its own fix).
        if stack := self._open_stack(src["id"]):
            return stack[0], False
        rounds = db.one("SELECT COUNT(*) n FROM tasks WHERE labels LIKE ?", (f'%"pr_fix:{key}"%',))["n"]
        if rounds >= PR_FIX_ROUNDS or coord.next_task_slot(db, coord.task_cap(self.cfg)) is not None:
            return None, False
        branch = branch or src["branch"] or ""
        try:   # a branch another worktree holds cannot be checked out again: then build on its head
            held = branch and f"branch refs/heads/{branch}" in worktree._git(
                worktree.pr_clone(self.p, key) or self.p.root, "worktree", "list", "--porcelain").splitlines()
        except Exception:
            held = True
        base_title = re.sub(r"^(?:Fix PR findings: )+", "", src["title"])
        tier = assigned_tier(src) if assigned_tier(src) in bud.TIER_ORDER else "standard"
        url = rec.get("url") or key
        off = own_push(self.cfg) == "off"
        on = (f"This task's branch is the PR's branch {branch}: build on it" if branch and not held else
              f"This task's branch starts from #{src['id']}'s branch head" + (
                  f"; the PR's branch {branch} is checked out elsewhere, and `ttp push --own` publishes this "
                  f"task's head onto it (fast-forward only)" if branch and not off else ""))
        deliver = (f"Own pushes are off in this project (delivery.own_push): do not push anywhere yourself; "
                   f"{push.own_landing(self.cfg)}. Keep the PR a draft and open no new PR." if off else
                   "Deliver to that PR: publish onto its branch with `ttp push --own --detach`, keep it a draft "
                   "and its description current, and open no new PR.")
        spec = "\n".join([
            f"Pull request {url} still has open findings after task #{src['id']} ({src['title']}) finished: {what}.",
            "This task owns them now; no other task does. Fix or answer each bot review comment on the PR "
            "(answers end with the hidden line <!-- ttp -->) and get CI green.",
            f"{on}. {deliver}",
            f"Put {url} in the hand-off's `pr`."])
        with db.tx():
            fid = db.add_task(f"Fix PR findings: {base_title}"[:200], spec, kind="code", tier=tier,
                              tier_source="parent_copy", priority=src["priority"], origin="daemon",
                              budget_usd=float(self.cfg["budget"]["task_default_usd"].get(tier, 8.0)),
                              branch=branch if branch and not held else None,
                              labels=[f"continues:{src['id']}", f"pr_fix:{key}"]
                              + ([f"pr_branch:{branch}"] if branch else []), **pin_of(src))
        log(self.p, f"{key}: open findings outlived task {src['id']}; queued fix #{fid}")
        return db.task(fid), True

    def _open_stack(self, root: int) -> list[dict]:
        """Open tasks of task `root`'s stack, oldest first: its reviews (`auto_review:<id>`) and the
        tasks that continue it or one of them, directly or through the chain (fixes, re-reviews)."""
        db, seen, todo, out = self.p.db, {root}, [root], []
        while todo:
            i = todo.pop()
            for t in db.q("SELECT * FROM tasks WHERE labels LIKE ? OR labels LIKE ?",
                          (f'%"continues:{i}"%', f'%"auto_review:{i}"%')):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    todo.append(t["id"])
                    if t["status"] not in TERMINAL_TASK_STATES:
                        out.append(t)
        return sorted(out, key=lambda t: t["id"])

    def _stack_root(self, task: dict) -> int:
        """The first task of `task`'s `continues:` chain."""
        seen, t = {task["id"]}, task
        while (c := continues_id(t)) is not None and c not in seen and (n := self.p.db.task(c)):
            seen.add(c)
            t = n
        return t["id"]

    def _failed_review_before(self, task: dict) -> int | None:
        """The id of a failed review this task continues, directly or through the fix it depends on
        (a `continues:` chain), or None: the task is then no re-review."""
        seen = {task["id"]}
        todo = [continues_id(task)] + [continues_id(t) for i in dependency_ids(task) if i is not None
                                       for t in [self.p.db.task(i)] if t]
        while todo:
            tid = todo.pop()
            t = self.p.db.task(tid) if tid is not None and tid not in seen else None
            if not t:
                continue
            seen.add(tid)
            if t["kind"] == "review" and t["status"] == "failed":
                return tid
            todo.append(continues_id(t))
        return None

    def _workspace_tries(self, task_id: int, clear: bool = False) -> int:
        """How often making task `task_id`'s workspace failed in a row (WORKSPACE_TRIES_KEY); `clear`
        forgets it once one was made."""
        tries = self.p.db.kv(WORKSPACE_TRIES_KEY, {}) or {}
        if clear and str(task_id) in tries:
            tries.pop(str(task_id))
            self.p.db.set_kv(WORKSPACE_TRIES_KEY, tries)
        return int(tries.get(str(task_id), 0))

    def _workspace_failed(self, task: dict, e: Exception) -> None:
        """Making the task's workspace failed (a git command timed out on a loaded host, the disk was
        full...; worktree.ensure already recovered what an earlier attempt left behind). It goes back
        to the queue after a backoff (`worktree.retry_backoff_s`, doubling) up to
        `worktree.create_retries` times without a model run; only a failure that outlasts them is
        blocked as `workspace:`, for the coordinator. One that retrying cannot fix (`final`, e.g.
        worktree.WorkspaceStuck) is blocked at once."""
        db = self.p.db
        w = self.cfg.get("worktree") or {}
        limit = int(w.get("create_retries", 3) or 0)
        with db.tx():
            tries = db.kv(WORKSPACE_TRIES_KEY, {}) or {}
            n = limit + 1 if getattr(e, "final", False) else int(tries.get(str(task["id"]), 0)) + 1
            tries[str(task["id"])] = n
            db.set_kv(WORKSPACE_TRIES_KEY, tries)
            if n <= limit:
                delay = float(w.get("retry_backoff_s", 120) or 0) * 2 ** (n - 1)
                db.update_task(task["id"], status="queued", not_before=time.time() + delay,
                               blocked_reason=f"{WORKSPACE_RETRY_NOTE} ({n}/{limit}): {e}"[:400])
            else:
                # The cut falls in the error (long paths), never in the tail saying what happens next.
                tail = (" (not retried: it cannot recover by itself)" if getattr(e, "final", False)
                        else f" (after {n} tries)")
                db.update_task(task["id"], status="blocked", blocked_reason=f"workspace: {e}"[:400 - len(tail)] + tail)
        log(self.p, f"task {task['id']} workspace failed ({n}/{limit + 1}): {e}")

    def retry_workspaces(self) -> None:
        """A task blocked on its workspace (`workspace:`) with retries left goes back to the queue on its
        own, on daemon start and every pass (an older daemon blocked it on the first failure);
        worktree.ensure then reuses or remakes what the failed attempt left behind. One with an anchor
        (waits:) is the coordinator's hold and stays. The tries of a task closed or gone are dropped."""
        db = self.p.db
        limit = int((self.cfg.get("worktree") or {}).get("create_retries", 3) or 0)
        with db.tx():
            tries = db.kv(WORKSPACE_TRIES_KEY, {}) or {}
            kept = {k: v for k, v in tries.items()
                    if (db.task(int(k)) or {}).get("status", "cancelled") not in TERMINAL_TASK_STATES}
            if kept != tries:
                db.set_kv(WORKSPACE_TRIES_KEY, kept)
        for t in db.q("SELECT * FROM tasks WHERE status='blocked' AND blocked_reason LIKE 'workspace:%'"):
            if anchors.anchor(t) or self._workspace_tries(t["id"]) >= limit:
                continue
            with db.tx():
                if (db.task(t["id"]) or {}).get("status") != "blocked":
                    continue
                db.update_task(t["id"], status="queued", not_before=None,
                               blocked_reason=f"{WORKSPACE_RETRY_NOTE}: {t['blocked_reason'][10:].strip()}"[:400])
            log(self.p, f"task {t['id']} queued again: its workspace is retried ({t['blocked_reason'][:200]})")

    def land_harness_worktrees(self) -> None:
        """Merge the commits of harness tasks that worked in a worktree of their own, once their run ended
        (harnesstree.sweep); one that git refused is tried again next tick, a conflict once its branch or
        the harness moved. It holds the upgrade lock meanwhile and waits while `ttp upgrade` holds it:
        either one's merge in the live checkout would leave MERGE_HEAD in the way of the other."""
        if not (self.p.state / harnesstree.DIR).is_dir():
            return
        held = locks.try_take([release.upgrade_lock(self.p)], f"daemon (pid {os.getpid()}): landing harness worktrees",
                              "harness worktree landing")
        if held is None:    # an upgrade runs: the next tick lands them
            return
        try:
            running = {int(r["task"]) for r in self.p.db.q("SELECT task FROM runs WHERE status='running' "
                                                            "AND task IS NOT NULL")}
            for tid, got in harnesstree.sweep(self.p, running).items():
                if got in ("merged", "conflict"):
                    log(self.p, f"harness worktree of task {tid}: {got}")
        except Exception:  # never in the way of the tick
            log(self.p, "harness worktree landing error: " + traceback.format_exc().replace("\n", " | ")[:2000])
        finally:
            held.close()

    def _workdir_for(self, task: dict) -> tuple[str, str | None]:
        if task["kind"] == "harness":   # its own worktree of the harness while another works there
            return str(harnesstree.workdir(self.p, task["id"])), None
        if task["kind"] not in worktree.NEVER_OWN_KINDS and (repo := worktree.pr_repo(self.p, task)):
            # A PR fix whose PR is in another repository than the root's: a worktree of its clone.
            try:
                path, branch = worktree.ensure_in(self.p, task, repo)
                return str(path), branch
            except RuntimeError as e:   # e.g. a fork's branch, neither local nor on origin: the root, as before
                log(self.p, f"task {task['id']}: no worktree in {repo} ({str(e)[:200]}); working in the project root")
                return str(self.p.root), None
        # A task carrying a branch (pr_branch:) works on it in a worktree, whatever its kind.
        carries = worktree.carried_branch(task) and task["kind"] not in worktree.NEVER_OWN_KINDS
        if (worktree.gets_worktree(self.p, task["kind"]) or carries) and worktree.is_git(self.p.root):
            self.guard_root_head()   # never a worktree branched from a HEAD a task moved the root to
            path, branch = worktree.ensure(self.p, task)
            return str(path), branch
        if task["kind"] == "review" and worktree.is_git(self.p.root):
            # A review that rebases or checks out a head must not do it in the root, which other
            # tasks branch from: it works in the change's worktree, or one of its own.
            prefer = str((conflictfix.of(task) or {}).get("branch") or "")
            path = worktree.review_workdir(self.p, task, prefer)
            if path:
                return str(path), None
        return str(self.p.root), None

    # notifications and Slack -------------------------------------------------------------------------
    def alert(self, key: str, text: str, severity: str = "high", every_s: float = 6 * 3600,
              outage: bool | None = None) -> None:
        """Deduplicated broadcast, across daemon restarts too (an upgrade must not re-announce a
        condition the user already has). A high alert with a key opens an episode that clears
        itself once the condition does (alerts.sweep); while it is open a repeat only updates the
        episode's `last`, with one reminder after alerts.REMIND_S. Other alerts go out at most once
        per `every_s`; `every_s=0` is for callers that alert only when the condition starts again.
        A machine-wide condition is broadcast by the one project holding its claim; the others post
        it quietly (web app and status only). An outage alert (`outage`, or by default a key on an outage
        path, outagemsg.OUTAGE_KEYS) never goes out bare: it names who acts now."""
        now = time.time()
        db = self.p.db
        if outagemsg.outage_key(key) if outage is None else outage:
            text = outagemsg.ensure_actor(text)   # never a bare "nothing was changed" outage alert
        tracked = alerts.tracked("alert", severity, key)
        claim_key = self._machine_wide(key) if tracked else None
        with db.tx():   # marked sent only together with the message
            sent = db.kv("alerts_sent", {})
            last = float(sent.get(key, 0))
            ep = db.one("SELECT * FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1",
                        (key,)) if tracked else None
            # A logout's reminders come from remind_logouts, on its own cadence.
            # A repeat that raises the severity (a status line that turned out to need the user) is new.
            raised = SEVERITY_RANK.get(severity, 1) > SEVERITY_RANK.get(ep["severity"] if ep else None, 1)
            if ep and not raised and alerts.acked(db, ep):   # the user knows: quiet until it clears
                db.x("UPDATE alerts SET last=? WHERE id=?", (now, ep["id"]))
                return
            if ep and every_s and not raised and (key.startswith("auth:") or now - ep["raised"] < alerts.REMIND_S
                                                  or last >= ep["raised"] + alerts.REMIND_S):
                db.x("UPDATE alerts SET last=? WHERE id=?", (now, ep["id"]))
                return
            if not ep and not tracked and now - last < every_s:
                return
            if claim_key:
                prev = db.one("SELECT MAX(cleared) AS c FROM alerts WHERE key=?", (key,))
                quiet = not alerts.claim(claim_key, key, str(self.p.base), now, float(prev["c"] or 0) if prev else 0.0)
            else:
                quiet = False
            sent[key] = now
            # Kept as long as the longest interval any alert uses, so a monthly one is not forgotten
            # (and re-sent) after a week.
            db.set_kv("alerts_sent", {k: v for k, v in sent.items() if now - float(v) < ALERT_KEEP_S})
            if ep and quiet:
                db.x("UPDATE alerts SET last=? WHERE id=?", (now, ep["id"]))   # the reminder is the claimer's
                return
            db.post("out", text, chat=None, channel=alerts.QUIET if quiet else "chat", kind="alert",
                    severity=severity, ref=key)

    def remind_logouts(self) -> None:
        """Each open logout: report what waits on it here into its machine-wide claim, and if this
        project holds the claim and a reminder is due (alerts.LOGOUT_REMIND_S), remind the chats with
        how many projects, queued tasks and high events wait on it. Model-free, so it works while no
        coordinator can run."""
        db, now = self.p.db, time.time()
        if now - getattr(self, "_logout_round", 0.0) < LOGOUT_REPORT_S:
            return
        self._logout_round = now
        for ep in db.q("SELECT key, MIN(raised) raised FROM alerts WHERE key LIKE 'auth:%' AND cleared IS NULL "
                       "GROUP BY key"):
            key, prov = ep["key"], ep["key"].split(":", 1)[1]
            if not alerts.breaker(db, prov):
                continue   # check_logins closes it, and the sweep the alert
            info = {"name": self.p.name,
                    "queued": db.one("SELECT COUNT(*) n FROM tasks WHERE status='queued'")["n"],
                    "events": db.one("SELECT COUNT(*) n FROM events WHERE status IN ('new','queued') "
                                     "AND severity IN ('high','critical')")["n"]}
            prev = db.one("SELECT MAX(cleared) AS c FROM alerts WHERE key=?", (key,))
            due = alerts.machine_round(self._machine_wide(key), key, str(self.p.base), now,
                                       float(prev["c"] or 0) if prev else 0.0, float(ep["raised"]), info)
            if not due:
                continue
            waiting = due["waiting"]
            try:
                hint = get_provider(prov).login_hint
            except Exception:
                hint = ""
            names = ", ".join(sorted(str(w.get("name")) for w in waiting))
            text = (f"{prov} on {hostname()} is still logged out ({(now - due['since']) / 3600:.1f} h). "
                    f"{len(waiting)} project(s) wait on it ({names}): "
                    f"{sum(int(w.get('queued') or 0) for w in waiting)} queued task(s) and "
                    f"{sum(int(w.get('events') or 0) for w in waiting)} high event(s) not handled yet. "
                    f"Log in once on that machine{f' ({hint})' if hint else ''}; work resumes by itself.")
            db.post("out", text, chat=None, kind="alert", severity="high", ref=key)
            log(self.p, f"{key}: logout reminder {due['reminder']} sent")

    def coordinator_blocked(self) -> str | None:
        """Why no coordinator turn can run now (None when one can): its provider is logged out or
        paused, its budget gate is red, or its turns keep failing."""
        db, core = self.p.db, self.cfg.get("core_provider") or "claude"
        if self._logged_out(core):
            return f"{core} is logged out"
        lim = self._provider_pause(core)
        if lim and float(lim.get("until") or 0) > time.time():
            return f"{core} is paused ({lim.get('note') or 'at its limit'})"
        gate = self.gates.get(core)
        if gate and gate.level == "red":
            return f"the {core} budget is red"
        fails = int(db.kv("coordinator_failures", 0))
        if fails >= 3:
            return f"its last {fails} turns failed"
        return None

    def relay_when_blocked(self) -> None:
        """While the coordinator has been unable to run for BLOCKED_RELAY_AFTER_S, tell the chat
        model-free about high or critical conditions it never saw (opened, or reopened, after its last
        good turn) that persisted RELAY_PERSIST_S and are still open: one line each, at most one
        message per RELAY_EVERY_S, each open stretch of a condition once. The usual notify floors
        apply to the message."""
        db, now = self.p.db, time.time()
        why = self.coordinator_blocked()
        state = db.kv(COORD_BLOCKED_KEY)
        if not why:
            if state:
                db.x("DELETE FROM kv WHERE key=?", (COORD_BLOCKED_KEY,))
            return
        if not state:
            db.set_kv(COORD_BLOCKED_KEY, {"since": now, "why": why})
            return
        since = float(state.get("since") or now)
        sent = db.kv(RELAYED_KEY) or {}
        if now - since < BLOCKED_RELAY_AFTER_S or now - float(sent.get("last") or 0) < RELAY_EVERY_S:
            return
        last_ok = db.one("SELECT MAX(started) t FROM runs WHERE role='coordinator' AND status='ok'")["t"] or 0
        told = sent.get("ids") or {}
        # `opened` is when the current open stretch began: a condition fixed and back again counts anew.
        rows = [r for r in db.q("SELECT *, COALESCE(opened, first_seen) since FROM issues WHERE status='open' "
                                "AND severity IN ('high','critical') AND COALESCE(opened, first_seen)>? "
                                "AND last_seen-COALESCE(opened, first_seen)>=? ORDER BY since",
                                (float(last_ok), RELAY_PERSIST_S))
                if float(told.get(str(r["id"])) or 0) < float(r["since"])]
        if not rows:
            return
        lines = [f"- {r['severity']}: {r['title']} (seen for "
                 f"{(float(r['last_seen']) - float(r['since'])) / 3600:.1f} h, {r['source']})"
                 for r in rows[:RELAY_MAX_LINES]]
        if len(rows) > RELAY_MAX_LINES:
            lines.append(f"- and {len(rows) - RELAY_MAX_LINES} more")
        text = (f"The coordinator cannot run ({why}, {(now - since) / 3600:.1f} h), so these conditions are "
                f"waiting unhandled:\n" + "\n".join(lines))
        severity = "critical" if any(r["severity"] == "critical" for r in rows) else "high"
        keep = {k: v for k, v in told.items() if now - float(v) < 14 * alerts.DAY}
        keep.update({str(r["id"]): now for r in rows[:RELAY_MAX_LINES]})
        with db.tx():
            db.post("out", text, chat=None, kind="alert", severity=severity,
                    ref="relayed:" + ",".join(str(r["id"]) for r in rows[:RELAY_MAX_LINES]))
            db.set_kv(RELAYED_KEY, {"last": now, "ids": keep})
        log(self.p, f"coordinator blocked ({why}): {len(rows[:RELAY_MAX_LINES])} condition(s) relayed to the chat")

    def _machine_wide(self, key: str) -> str | None:
        """The per-user claim key of a condition every project on this machine sees alike: a
        logged-out CLI, a lost VPN or network sign-in (netwait), linger off (downtime), or a low disk on the filesystem the
        guard found short. None otherwise."""
        if key.startswith(("auth:", "account-mismatch:")) or key in (netwait.KEY, downtime.LINGER_KEY, "notifier"):
            return f"{hostname()}|{key}"
        if key == "disk":
            path = (self.p.db.kv("disk_low") or {}).get("path")
            try:
                return f"{hostname()}|disk|{os.stat(path).st_dev}" if path else None
            except OSError:
                return None
        return None

    def release_claims(self, ep: dict) -> None:
        release_claims(self.p, ep)

    def sweep_alerts(self) -> None:
        """Close alert episodes whose condition cleared (stored with the time; the chats hear it once).
        Auth breakers go first: a logged-out alert lasts as long as its provider's breaker."""
        self.check_logins()
        for ep in alerts.sweep(self.p.db):
            log(self.p, f"alert cleared: {ep['key']} ({ep['cleared_why']})")
            self.release_claims(ep)
            kind, _, prov = ep["key"].partition(":")
            if kind == "auth":
                # Tasks dispatch skips for another reason (the disk guard) would keep a stale note.
                self.p.db.x("UPDATE tasks SET blocked_reason=NULL WHERE status='queued' AND blocked_reason LIKE ?",
                            (f"{LOGGED_OUT_NOTE} ({prov})%",))
        self.clear_root_checkout()
        if time.time() - self._root_head_checked >= ROOT_RECHECK_S and self.p.db.kv(ROOT_HEAD_KEY):
            self.guard_root_head()

    def slack(self):
        if not self.cfg["notify"].get("slack"):
            return None
        if self._slack is None:
            from .slack import from_config
            self._slack = from_config(self.cfg)
        return self._slack

    def deliver_outbound(self) -> None:
        sl = self.slack()
        if not sl:
            return
        from .web import cleared
        db = self.p.db
        floor = SEVERITY_RANK.get(self.cfg["notify"].get("slack_min_severity", "high"), 2)
        last = int(db.kv("slack_last_out", 0))
        rows = db.q("SELECT * FROM messages WHERE direction='out' AND id>? ORDER BY id LIMIT 20", (last,))
        # Only an ask that reached Slack can back a PR approval (prguard), so those go whatever their severity.
        approval_asks = {prguard.BLOCKING_REF + r for r in prguard.APPROVING_REASONS}
        for m in rows:
            wanted = SEVERITY_RANK.get(m["severity"], 1) >= floor or (m["kind"] == "ask" and m["ref"] in approval_asks)
            to_slack = ((m["chat"] is None and wanted and m["kind"] != "info"
                         and m["channel"] != alerts.QUIET and not cleared(db, m, time.time()))
                        or m["chat"] == "slack")
            if to_slack:
                try:
                    thread = m["ref"] if m["chat"] == "slack" else None
                    ts = sl.post(self.p.name, m["text"], thread_ts=thread)
                    with db.tx():   # the post's ts: pr_approve reads an ask back from Slack by it
                        db.x("UPDATE messages SET ext_id=? WHERE id=?", (ts, m["id"]))
                        threads = set(db.kv("slack_threads", []))
                        threads.add(ts)
                        db.set_kv("slack_threads", sorted(threads)[-500:])
                        if db.kv(SLACK_ERROR_KEY):
                            db.set_kv(SLACK_ERROR_KEY, None)
                except Exception as e:
                    log(self.p, f"slack post failed: {e}")
                    db.set_kv(SLACK_ERROR_KEY, {"ts": time.time(), "error": str(e)[:300]})   # shown in the web app
                    if not sl.rejected_message(e):
                        return
                    tries = self._slack_rejects[m["id"]] = self._slack_rejects.get(m["id"], 0) + 1
                    if tries < 3:
                        return
                    log(self.p, f"slack skipped message {m['id']} after {tries} rejections: {e}")
                self._slack_rejects.pop(m["id"], None)
            db.set_kv("slack_last_out", m["id"])

    def poll_slack(self) -> None:
        sl = self.slack()
        now = time.time()
        if not sl or now - self._last_slack < float(self.cfg["notify"].get("slack_poll_s", 20)):
            return
        self._last_slack = now
        from .slack import THREAD_SCAN_S, THREAD_WINDOW_S, projects_in_dm, route
        db = self.p.db
        oldest = str(db.kv("slack_oldest", f"{now - 60:.6f}"))
        # Each thread has its own read position; `floor` is where a thread not read yet starts: the
        # cursor when all recent posts were last checked, so a reply is not skipped for a newer message.
        replies = db.kv("slack_replies") or {}
        read, floor = dict(replies.get("read") or {}), str(replies.get("floor") or oldest)
        scan = now - self._thread_scan >= THREAD_SCAN_S
        try:
            msgs, posts = sl.poll(oldest)
            threads_new = sl.new_replies(posts + (sl.recent_posts() if scan else []), read, floor)
            siblings = projects_in_dm(sl.call("conversations.history", channel=sl.dm_channel(), limit=200)
                                      .get("messages", [])) or [self.p.name]
            uid = sl.resolve_user()
        except Exception as e:
            log(self.p, f"slack poll failed: {e}")
            return
        threads = set(db.kv("slack_threads", []))
        names = sorted(set(siblings) | {self.p.name})
        for m in msgs:
            text = route(m, self.p.name, threads, names)
            if text:
                with db.tx():   # stored exactly once: the message and the cursor past it commit together
                    db.post("in", text, chat="slack", channel="slack", kind="user", ref=m["ts"],
                            provenance="slack", ext_id=m["ts"])
                    db.set_kv("slack_oldest", m["ts"])
                continue
            if not m.get("thread_ts") and names[0] == self.p.name \
                    and not re.match(r"^\s*[A-Za-z0-9._-]+\s*:", m.get("text") or ""):
                sl.post(self.p.name, f"Which project is this for? Start the message with one of: {', '.join(names)}, "
                                     f"e.g. `{self.p.name}: ...`", thread_ts=m["ts"])
            db.set_kv("slack_oldest", m["ts"])
        for parent, reps in threads_new:
            for r in reps:
                text = route(r, self.p.name, threads, names) if r.get("user") == uid else None
                read[parent] = r["ts"]
                with db.tx():
                    if text:
                        db.post("in", text, chat="slack", channel="slack", kind="user", ref=parent,
                                provenance="slack", ext_id=r["ts"])
                    db.set_kv("slack_replies", {"floor": floor, "read": read})
        if scan:
            self._thread_scan = now
            # A thread older than both the window and the cursor is never read again.
            keep = min(now - THREAD_WINDOW_S, float(oldest))
            db.set_kv("slack_replies", {"floor": oldest, "read": {k: v for k, v in read.items() if float(k) >= keep}})

    # misc -------------------------------------------------------------------------------------------
    def _has_work(self) -> bool:
        """Work that keeps a Mac awake (awake.IdleHold): a running run or push, or, unpaused, a queued
        task that could start now under an open gate. Deferred, waiting, paused and blocked tasks do not count."""
        db = self.p.db
        if db.one("SELECT 1 FROM runs WHERE status='running' LIMIT 1") or \
                db.one("SELECT 1 FROM tasks WHERE status='pushing' LIMIT 1"):
            return True
        if db.kv("paused", False) or self.gates and not any(g.allow_new_work for g in self.gates.values()):
            return False
        return self._dispatchable()


def needs_device(task: dict, cfg: dict) -> bool:
    """Tagged `needs_device`, or names a device lock (config `device.locks`) as a resource. Only with
    device locks configured: without them no task counts, and dispatch is as it always was."""
    dev = locks.device_locks(cfg)
    if not dev:
        return False
    labels = json.loads(task["labels"] or "[]")
    return "needs_device" in labels or any(lb.split(":", 1)[1] in dev for lb in labels
                                           if lb.startswith(("resource:", "exclusive:")))


def holds_device(task: dict, cfg: dict) -> bool:
    """A device task by either test: tagged `needs_device`, or naming a `*-device` resource, a machine
    tagged `device` or a configured device lock."""
    return effort.device_task(task) or needs_device(task, cfg)


def _detached_jobs(run_dir: Path) -> list[dict]:
    try:
        jobs = json.loads((run_dir / "detached.json").read_text())
    except (OSError, ValueError):
        return []
    return [j for j in jobs if isinstance(j, dict) and j.get("rc") and j.get("name")]


PAUSED_NOTE = "waits for a paused resource:"
RESOURCE_BUSY_NOTE = "its resource stayed busy"   # an exclusive run that lost the race for its slot
LOGGED_OUT_NOTE = "held: logged out"
NET_HELD_NOTE = "held: network,"
BRANCH_HELD_NOTE = "held: waits for #"   # Daemon._branch_clash: another live task uses its branch or worktree
# _start_hold's reasons as schedlog reasons (Daemon._log_slot_fill); budget, caps and optional are other.
SLOT_HELD_BY = {"held": "hold", "paused": "hold", "logged_out": "provider_gate", "net": "provider_gate",
                "gate": "provider_gate", "device": "lock", "resource": "lock"}
LOGIN_CHECK_PASSED = "its login check passed"
ACCOUNT_SWITCH_EVENT = "login_account_changed"   # recorded when a login comes back as another account


def _carrying_review(db, task_id: int) -> int | None:
    """A review of the task that has not failed or been cancelled: it reviews, and pushes, its branch."""
    for r in db.q("SELECT id, depends_on FROM tasks WHERE kind='review' AND status NOT IN ('failed','cancelled') "
                  "ORDER BY id DESC"):
        if task_id in dependency_ids(r):
            return r["id"]
    return None


def _names_commit(tasks: list[dict], head: str) -> bool:
    """Whether a task's spec names `head` by an abbreviation of at least 7 hex digits."""
    return any(head.startswith(h.lower()) for t in tasks for h in re.findall(r"\b[0-9a-fA-F]{7,40}\b", t.get("spec") or ""))


def idle_wake(p: Project, cfg: dict, gates: dict[str, dict], now: float, db=None) -> dict:
    """When the coordinator next wakes by itself, with no message or event pending. Both the daemon
    and `ttp status` / the web app read it from here, so the time shown is the one applied.

    The idle wake comes `idle_wake_s` after the last turn once no task is queued or running, and
    only while the core budget gate allows optional work. The idle-slot wake (`starve_state`) can
    come sooner. A wake turn that met the same state as the previous one had nothing new to
    decide: each such repeat doubles the wait for both, up to a day. A wake the quiet gate settled
    without a turn is a repeat too, and the next wait counts from it. Explicit check-backs use
    schedules.

    Returns `at` (next wake, possibly past), `due` ("idle", "starve" or None at `now`), `held`
    (why the idle wake cannot come), and the `wake` / `starve` kv values a starting turn stores."""
    # `db` is the caller's own connection: the web app calls this from its request threads, and an
    # SQLite connection works only in the thread that opened it.
    db, c = db or p.db, cfg["coordinator"]
    gate = gates.get(cfg.get("core_provider", "claude"))
    last = float(db.kv("last_coordinator_turn", 0))
    idle_s = float(c.get("idle_wake_s", 3600))
    fp = wake_fingerprint(p, gates, db)
    prev = db.kv("idle_wake", {}) or {}
    same = prev.get("fp") == fp
    repeats = int(prev.get("n", 0)) if same else 0
    # A wake the quiet gate settled (quietgate.py) is a wake of its own: the next waits from it, but
    # comes no later than the gate's max age after the last turn, when a turn runs.
    skipped = float(prev.get("skipped") or 0) if same else 0.0
    cap = last + quietgate.max_age(cfg) if skipped > last and quietgate.max_age(cfg) else float("inf")
    last = max(last, skipped)
    backoff = min(idle_s * 2 ** min(repeats, 30), 86400.0) if repeats else 0.0
    idle_at, held = None, None
    if not db.one("SELECT id FROM tasks WHERE status IN ('queued','running')"):
        if gate is None or gate["allow_optional"]:
            idle_at = min(last + max(idle_s, backoff), cap)
        else:
            held = f"held by the budget gate ({gate['level']})"
    starve = starve_state(db, cfg, gate, now)
    starve_at = min(last + max(backoff, starve["wait"]), cap) if starve else None
    due = "idle" if idle_at is not None and now > idle_at else \
        "starve" if starve_at is not None and now > starve_at else None
    times = [t for t in (idle_at, starve_at) if t is not None]
    return {"at": min(times) if times else None, "due": due, "held": None if times else held,
            "wake": {"fp": fp, "n": repeats + 1}, "starve": starve}


def wake_fingerprint(p: Project, gates: dict[str, dict], db=None) -> str:
    """The state a wake turn decides on. Running counts as queued: dispatch moves tasks between
    the two without the coordinator. Spend numbers are left out; gate levels carry them."""
    db = db or p.db
    tasks = [(t["id"], "queued" if t["status"] == "running" else t["status"], t["priority"], t["depends_on"])
             for t in db.q("SELECT id, status, priority, depends_on FROM tasks "
                           "WHERE status NOT IN ('done','failed','cancelled') ORDER BY id")]
    asks = [r["id"] for r in db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0 ORDER BY id")]
    scheds = [(s["name"], s["enabled"], s["every_s"], s["at"])
              for s in db.q("SELECT name, enabled, every_s, at FROM schedules ORDER BY name")]
    # Only red changes what a turn can do; green, yellow and orange are pacing and wake nobody.
    levels = sorted((k, g["level"] == "red", g["allow_new_work"]) for k, g in gates.items())
    files = [p.charter_path, p.config_path, p.memory_index,
             *(p.memory_dir.iterdir() if p.memory_dir.is_dir() else [])]
    mtimes = sorted((f.name, f.stat().st_mtime) for f in files if f.exists())
    paused = sorted(db.paused_resources())
    blob = json.dumps([tasks, asks, scheds, levels, mtimes] + ([paused] if paused else []), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def starve_state(db, cfg: dict, gate: dict | None, now: float) -> dict | None:
    """Paid plan capacity sitting idle: worker slots are free, the plan is below its line with room
    for all of them (green), and nothing is ready or about to be. Ask the coordinator for more
    independent work well before the idle wake would. Usage-billed work costs money whether or
    not it runs, so only plans qualify. A turn that adds no task doubles the wait for the next
    one, up to the idle wake; a new task resets it. While every queued task waits on its own probe
    or time, those gates wake the coordinator when they open: the wait starts at the idle wake and
    doubles up to a day. Returns None when this wake does not apply, else the wait after the last
    turn and the newest task id, stored as kv `starve` on firing."""
    c = cfg["coordinator"]
    if gate is None or gate["regime"] != "windows" or gate["level"] != "green" or not gate["allow_new_work"]:
        return None
    running = db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='running' AND role!='coordinator'",
                     (gate["provider"],))["n"]
    if running >= int(gate["max_parallel"]):
        return None
    # Queued work, whether ready or waiting on a retry timer, a dependency or a resource, starts by
    # itself. An open question waits for the user but does not stop the wake: idle capacity can still
    # take other work. A deferred task may sit for days: it holds nothing.
    held = [deferral(t) for t in db.q("SELECT labels FROM tasks WHERE status='queued'")]
    if any("when" not in d and d.get("after", 0) <= now for d in held):
        return None
    if coord.next_task_slot(db, coord.task_cap(cfg)) is not None:
        return None
    idle_s = float(c.get("idle_wake_s", 3600))
    base, cap = (idle_s, 86400.0) if held else (float(c.get("starve_wake_s", 300)), idle_s)
    newest = db.one("SELECT COALESCE(MAX(id),0) n FROM tasks")["n"]
    st = db.kv("starve") or {}
    wait = base if not st or newest > st.get("task", 0) else \
        min(max(float(st.get("wait", base)) * 2, base), max(cap, base))
    return {"task": newest, "wait": wait}


def _exclusive(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("exclusive:")]


def _shared(task: dict) -> list[str]:
    return [lb.split(":", 1)[1] for lb in json.loads(task["labels"] or "[]") if lb.startswith("resource:")]


def _cut_off_cost(run_dir: Path, exit_info: dict, usage=None, prov=None) -> float:
    """A run that ended without any usage to price still spent money: Codex reports usage only when
    a turn completes, and Cursor's result has none at all. Book the elapsed share of its dollar
    budget rather than $0, so the caps keep counting it. A run whose agent never started (it gave
    up waiting for a resource) spent nothing, and so did one that wrote no output and reported no
    tokens, when its CLI streams (a CLI that hung, or a host that slept, before its first event)."""
    if exit_info.get("launched") is False:
        return 0.0
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        return 0.0
    tokens = usage and _has_tokens(usage)
    try:
        silent = not (run_dir / "output.jsonl").stat().st_size
    except OSError:
        silent = True
    if silent and not tokens and (prov is None or prov.streams(spec.get("argv") or [])):
        return 0.0
    budget = float(spec.get("budget_usd") or spec.get("default_budget_usd") or 0)
    timeout = float(spec.get("timeout_s") or 0)
    elapsed = float(exit_info.get("ended") or time.time()) - float(exit_info.get("started") or 0)
    elapsed -= float(exit_info.get("slept_s") or 0)   # a sleeping host spends nothing
    if budget <= 0 or timeout <= 0 or not exit_info.get("started"):
        return 0.0
    elapsed -= min(locks.waited(run_dir, float(exit_info.get("ended") or time.time())), timeout)
    return round(budget * min(max(elapsed, 0.0) / timeout, 1.0), 4)


def _watcher_timeout(payload: dict) -> int:
    """A command watcher's timeout: its timeout_s (default 120), capped at WATCHER_MAX_S so one slow
    watcher cannot hold a tick past the watchdog and restart the daemon over and over. schedule_set
    refuses more; a stored value above it is logged once (Daemon._warn_over_cap) and named in the
    timeout text (schedule.capped_note)."""
    try:
        want = int(payload.get("timeout_s") or 120)
    except (TypeError, ValueError):
        want = 120
    return max(1, min(want, WATCHER_MAX_S))


def _precheck_cmd(payload: dict) -> str:
    """An llm schedule's `precheck` shell command, or "" when it has none."""
    return str(payload.get("precheck") or "").strip()


def _before_stdin(argv: list[str], extra: list[str]) -> list[str]:
    """`argv` with `extra` added; a trailing "-" (prompt on stdin) stays the last argument."""
    return argv[:-1] + extra + ["-"] if argv[-1:] == ["-"] else argv + extra


def _with_system_prompt(prov, argv: list[str], path: Path) -> tuple[list[str], bool]:
    """`argv` for a run whose system prompt is the text in `path`, and whether the text is in it. The
    CLI reads the file itself where it can (--system-prompt-file): text past the per-argument limit
    on the command line fails the exec (E2BIG) before the agent starts. Where it cannot and the text
    is too long, it is left out and leads what the agent reads instead (stdin has no limit)."""
    if prov.name != "claude":
        return argv, False
    sys_args = prov.system_prompt_args(path)
    return argv + ["--safe-mode", "--strict-mcp-config", "--tools", "", *sys_args], bool(sys_args)


def _lead_input(stdin: str, text: str) -> str:
    """The stream-json user message in `stdin` with `text` as its first block (cached with the next)."""
    msg = json.loads(stdin)
    msg["message"]["content"].insert(0, {"type": "text", "text": text})
    return json.dumps(msg) + "\n"


def _explicit_clear(payload: str | None) -> bool:
    """Whether a command schedule's stored payload opts into the receipt lifecycle."""
    try:
        return (json.loads(payload or "{}") or {}).get("issue_lifecycle") == scr.EXPLICIT_CLEAR
    except (ValueError, AttributeError):
        return False


def _line_key(obs: dict, body: str) -> str:
    """What dedupes a paged line: its `key`, else its whitespace-normalized text."""
    key = obs.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return "text:" + hashlib.sha256(" ".join(body.split()).encode()).hexdigest()[:16]


def _line_episode(obs: dict) -> str | None:
    """A keyed line's `episode`, as the screen keeps it (screen.screen), else None."""
    key, ep = obs.get("key"), obs.get("episode")
    if not (isinstance(key, str) and key.strip() and isinstance(ep, str)):
        return None
    return ep.strip()[:scr.EPISODE_MAX] or None


def _observations(text: str) -> list[dict]:
    """Each JSON line with a `text` is its own observation; each run of other lines between them
    is one plain observation, so a stray line never swallows the JSON reports around it."""
    if not text:
        return []
    obs: list[dict] = []
    plain: list[str] = []

    def flush():
        body = "\n".join(plain).strip()
        if body:
            obs.append({"text": body[:6000]})
        plain.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                o = None
            if isinstance(o, dict) and o.get("text"):
                flush()
                obs.append(o)
                continue
        plain.append(raw)
    flush()
    return obs


def _result_ref(p: Project, path: Path) -> str:
    try:
        return str(path.relative_to(p.root))
    except ValueError:
        return str(path)


def _cut(text: str, n: int, where: str) -> str:
    """`text` within `n` characters; a cut one says so and where the whole text is."""
    if len(text) <= n:
        return text
    note = f" … [cut; the whole text is in {where}]"
    return text[:max(0, n - len(note))] + note


def _proxied() -> bool:
    """Whether the agents reach their API through a proxy: the host then resolves nothing itself."""
    return any(os.environ.get(k) for k in PROXY_ENV)


def _background(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _resolves(host: str) -> bool:
    """Whether `host` resolves within REACH_TIMEOUT_S (getaddrinfo has no timeout of its own)."""
    ok: list[bool] = []

    def look() -> None:
        try:
            socket.getaddrinfo(host, 443)
            ok.append(True)
        except OSError:
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(REACH_TIMEOUT_S)
    return bool(ok)


def _sleep_cause(state: dict | None) -> str:
    """Why keep-awake did not stop a sleep, from the awake.KV record made before it (_check_sleep runs
    before the tick's IdleHold.update, so the record still shows the state the host slept in)."""
    state = state or {}
    why = state.get("why") or ""
    if why == awake.BATTERY:
        lid = " and the lid was closed" if state.get("lid_closed") else ""
        return f"Keep-awake was not held because the Mac was on battery power{lid}: it holds only on AC power."
    if why:
        return f"Keep-awake was not held: {why}."
    return "Keep-awake stops idle and maintenance sleep, not a closed lid."


def _sleep_text(d: dict) -> str:
    n = int(d.get("sleeps") or 1)
    return (f"the host slept {float(d.get('slept_s') or 0) / 60:.0f} min"
            f"{f' over {n} sleeps' if n > 1 else ''}; {len(d.get('runs') or [])} running run(s) paused")


def _ids(ids: list) -> str:
    return ", ".join(str(i) for i in ids)


def _has_tokens(usage) -> bool:
    return bool(usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens)


def _resume_never_started(note: dict, usage) -> bool:
    """A run that was to continue a lost session ended before its agent did anything."""
    return bool(note.get("resumes")) and not usage.cost_usd and not usage.output_tokens


# `metrics.verdict` values that make a review handed off `done` one that asked for changes (see review_rejects).
REVIEW_REJECT_VERDICTS = frozenset({"changes_needed", "changes_requested", "rejected"})


def review_rejects(result: dict) -> bool:
    """A review's hand-off says the change must not proceed through `metrics.verdict`, for projects
    whose kind-review.md reports `done` plus a verdict instead of `failed`."""
    metrics = result.get("metrics")
    verdict = metrics.get("verdict") if isinstance(metrics, dict) else None
    return isinstance(verdict, str) and verdict.strip().lower().replace("-", "_").replace(" ", "_") \
        in REVIEW_REJECT_VERDICTS


RELAY_SHOWN = 8   # consuming tasks a passed review's event names


def review_failure(result) -> str:
    """A failed review's hand-off's REVIEW_FAILURE_KEY: one of REVIEW_FAILURE_KINDS, else "unknown"."""
    v = result.get(REVIEW_FAILURE_KEY) if isinstance(result, dict) else None
    return v if v in REVIEW_FAILURE_KINDS else "unknown"


def relay_hint(dependents: list[dict]) -> str:
    """For a review that passed, the open tasks waiting on it and how they get its result: a
    spec-only task_update, never a worker relaunched only to relay the verdict. "" when none wait."""
    if not dependents:
        return ""
    shown = ", ".join(f"#{t['id']} ({t['status']})" for t in dependents[:RELAY_SHOWN])
    if len(dependents) > RELAY_SHOWN:
        shown += f" and {len(dependents) - RELAY_SHOWN} more"
    return (f"\nWaiting on it: {shown}. Hand them the accepted commit, evidence path, verdict and open "
            "gates with a spec-only task_update; requeue or add no task only to relay it.")


def on_pass_both_warning(result: dict) -> str:
    """A waiting hand-off naming both `on_pass` and `on_pass_cmd`: what the daemon does with them,
    or "". The command takes precedence (_probe_passed); on_pass is only its fallback after a passed
    `ttp checks --result` wait, recorded when the command exits 0 without writing $TTP_RESULT."""
    if not isinstance(result, dict) or "on_pass" not in result or result.get("on_pass_cmd") is None:
        return ""
    try:
        words = shlex.split(str(result.get("retry_when") or ""))
    except ValueError:
        words = []
    if len(words) == 4 and Path(words[0]).name == "ttp" and words[1:3] == ["checks", "--result"]:
        return ("names both on_pass and on_pass_cmd: on_pass_cmd takes precedence; on_pass is recorded only "
                "if its checks pass and the command exits 0 without writing a hand-off to $TTP_RESULT")
    return ("names both on_pass and on_pass_cmd, but its retry_when is no `ttp checks --result` wait: on_pass "
            "is ignored and only on_pass_cmd decides")


def on_pass_problem(on_pass) -> str:
    """Why a waiting hand-off's `on_pass` (the result to record if its checks pass) cannot be
    recorded, or "". It is a final hand-off: done or needs_review with a summary, and no wait."""
    if not isinstance(on_pass, dict):
        return "on_pass is not an object"
    if on_pass.get("status") not in ("done", "needs_review"):
        return f"on_pass status must be done or needs_review, not {str(on_pass.get('status'))[:40]!r}"
    if not isinstance(on_pass.get("summary"), str) or not on_pass["summary"].strip():
        return "on_pass has no summary"
    waits = [k for k in (*WAIT_KEYS, "on_pass") if k in on_pass]
    if waits:
        return f"on_pass is final and cannot carry {', '.join(waits)}"
    for key, kind in (("artifacts", list), ("metrics", dict), ("followups", list)):
        if key in on_pass and not isinstance(on_pass[key], kind):
            return f"on_pass {key} must be a {kind.__name__}"
    return ""


def with_waiting_followups(final: dict, waited: dict) -> dict:
    """The final hand-off (a waiting hand-off's on_pass, or what its on_pass_cmd wrote) with the
    waiting hand-off's own top-level followups added, so they are filed as a normal hand-off's are.
    One already in `final` (same title and spec) is not added twice."""
    extra = waited.get("followups") if isinstance(waited, dict) else None
    if not isinstance(final, dict) or not isinstance(extra, list) or not extra:
        return final
    own = final.get("followups") if isinstance(final.get("followups"), list) else []
    seen = {(f.get("title"), f.get("spec")) for f in own if isinstance(f, dict)}
    add = [f for f in extra if isinstance(f, dict) and (f.get("title"), f.get("spec")) not in seen]
    return {**final, "followups": [*own, *add]} if add else final


def _checked_head(run_dir: Path, task: dict) -> str:
    """The commit a run's passed `ttp checks` recorded, when its worktree still has it checked out,
    clean, and a code task's branch still points at it; else ""."""
    try:
        rec = json.loads((run_dir / prguard.CHECKS_FILE).read_text())
    except (OSError, ValueError):
        return ""
    if not prguard.candidate_record(rec):   # a harness qualification never stands for a checked candidate
        return ""
    head, top = rec.get("head"), rec.get("worktree")
    if rec.get("passed") is not True or not isinstance(head, str) or len(head) < 40 or not isinstance(top, str) \
            or not Path(top).is_dir():
        return ""
    try:
        if worktree._git(Path(top), "rev-parse", "HEAD", check=False) != head:
            return ""
        if worktree._git(Path(top), "status", "--porcelain", "--untracked-files=no", check=False):
            return ""
        if task["kind"] == "code" and task["branch"] and worktree._git(
                Path(top), "rev-parse", "--verify", "-q", f"refs/heads/{task['branch']}", check=False) != head:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return head


def _daemon_precheck(r: dict, handoff) -> bool:
    """The daemon's own failed-precheck hand-off of a review no reviewer ran: no worker wrote it."""
    return r.get("id") is None and isinstance(handoff, dict) and handoff.get(REVIEW_FAILURE_KEY) == "precheck"


def _push_rechecked(task: dict) -> bool:
    """The task's hand-off is back from verify_pushes: its hand-off checks already passed it once."""
    return task.get("status") == "pushing" and pushclaim.KEY in load_result(task.get("result"))


def _route_cause(end: str, exit_info: dict, usage, run_dir: Path) -> str:
    """A short cause for a routed run's own fallback (route.own_cause), for its route record."""
    if end in ("timeout", "stalled") or end in HUNG_STOPS:
        return "timed out" if end == "timeout" else "stalled"
    if not (run_dir / RESULT_FILE).exists():
        if exit_info.get("rc") == 0 and not usage.error:
            return "exited without result.json"
    elif _read_result(run_dir / RESULT_FILE) is None:
        return "result.json is not a JSON object"
    elif exit_info.get("rc") == 0 and not usage.error:
        got = str((_read_result(run_dir / RESULT_FILE) or {}).get("status") or "")[:40]
        return f"result.json status {got!r} is not a hand-off"
    return (str(usage.error or "") or f"exit {exit_info.get('rc')}")[:200]


def _read_result(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _saved_gate(d: dict | None) -> bud.Gate | None:
    try:
        return bud.Gate(**d) if isinstance(d, dict) else None
    except TypeError:   # saved by a version with other fields
        return None


def _is_agent(pid: int, started: str) -> bool:
    """pid is still the agent its supervisor recorded: the leader of its own session, with the start
    token the supervisor wrote. A pid reused since then has another start, so it is never signalled."""
    try:
        return bool(started) and os.getsid(pid) == pid and runner.proc_start(pid) == started
    except OSError:
        return False


def _run_group_left(pgid: int, run_dir: Path) -> bool:
    """The agent (leader of session and group pgid) is gone, but tools it started still run in its
    group. The kernel keeps a pid that is still a group's id from being reused, so a process in
    session and group pgid that carries this run's TTP_RUN_DIR can only be one of them. Linux only
    (/proc); elsewhere the runner's watchdog is the only one that ends them."""
    if os.path.exists(f"/proc/{pgid}"):
        return False   # the leader lives on: _is_agent already said it is not the agent
    want = b"TTP_RUN_DIR=" + os.fsencode(str(run_dir))
    try:
        procs = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return False
    for d in procs:
        try:
            fields = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid or int(fields[3]) != pgid:
                continue
            if want in _settled_environ(d).split(b"\0"):
                return True
        except (OSError, IndexError, ValueError):
            continue
    return False


def _settled_environ(pid: str) -> bytes:
    """/proc/<pid>/environ, read again for up to EXEC_SETTLE_S while it reads empty: a process in the
    middle of an exec has no environment to show until the new program's is set up, so one look
    could miss the tool a run left behind."""
    deadline = time.monotonic() + EXEC_SETTLE_S
    while True:
        env = _read_environ(pid)
        if env or time.monotonic() >= deadline:
            return env
        time.sleep(0.02)


def _read_environ(pid: str) -> bytes:
    return Path(f"/proc/{pid}/environ").read_bytes()


def _end_group(pgid: int, grace_s: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _waited_jobs(retry_when: str) -> list[Path]:
    """The .rc paths of a `ttp detach --check <rc...>` retry_when; [] for anything else (a remote
    check, more than that one command)."""
    try:
        words = shlex.split(retry_when)
    except ValueError:
        return []
    i = next((k for k in range(len(words) - 1) if words[k] == "detach" and words[k + 1] == "--check"), None)
    if i is None or i > 1 or i == 1 and Path(words[0]).name != "ttp":
        return []
    rcs = words[i + 2:]
    if not rcs or any(w.startswith("-") or not w.endswith(".rc") or not Path(w).is_absolute() for w in rcs):
        return []
    return [Path(w) for w in rcs]


def _task_note(run_dir: Path, text: str) -> None:
    """A progress note on the task, in its run's notes, as `ttp note` writes it."""
    try:
        durable_append(Path(run_dir) / "progress.md", f"{time.strftime('%H:%M:%S')} daemon: {text}\n")
    except OSError:
        pass


def held_by_task(held, tid: int, project: str) -> list[str]:
    """The resources (a device, a machine, a remote build directory: any lock but the harness's own push
    and upgrade locks) task tid of `project` held, as a heartbeat's `held` list names them ("board: task
    #7 (run 3), whole run since 10:02 PDT"): any hold label hold.label builds, read by hold.task_of. A
    reservation is not a hold.
    Gaps, left open: a device used only through a broker queue (`ttp devq`) without `ttp lock` holds no
    slot, so it is not listed; neither is a hold taken after the last heartbeat before the reboot."""
    from .release import UPGRADE_RESOURCE
    out = []
    for item in held or []:
        res, _, holder = str(item).partition(": ")
        if res.startswith("push:") or res == UPGRADE_RESOURCE:
            continue
        if hold.task_of(holder.rsplit(" since ", 1)[0], project) == int(tid) and res not in out:
            out.append(res)
    return out


def held_at_reboot_reason(held: list[str]) -> str:
    """Why a task that held `held` when the host went down abruptly is not retried automatically."""
    return (f"the host rebooted abruptly while this task held {', '.join(held)}, so it may have caused the "
            f"reset: not retried automatically. Requeue it once its earlier attempt is known not to be implicated")


def _reboot_wakes(task: dict) -> int:
    """Boot-time wakes of a waiting task since it was last blocked, carried in its result."""
    try:
        return max(0, int(load_result(task["result"]).get("reboot_wakes") or 0))
    except (TypeError, ValueError):
        return 0


def relay_worker_notifies(p: Project, task: dict, run_dir: Path) -> int:
    """Post the `ttp notify` lines a worker run left as alerts, the way the coordinator's `notify`
    action does: at most low or normal severity, the first NOTIFIES_PER_RUN, a text already sent in
    the last day not again. The file is renamed once the posts commit, so a second finish does not repost."""
    db, f = p.db, run_dir / NOTIFY_FILE
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    sent = 0
    for line in lines:
        try:
            n = json.loads(line)
        except ValueError:
            continue
        body = " ".join(str(n.get("text") or "").split())[:2000] if isinstance(n, dict) else ""
        if not body:
            continue
        if sent >= NOTIFIES_PER_RUN:
            log(p, f"task #{task['id']}: dropped `ttp notify` lines past {NOTIFIES_PER_RUN} in run {run_dir.name}")
            break
        text = headline.join(task['title'], body, 80, f"#{task['id']} ")
        if db.one("SELECT id FROM messages WHERE direction='out' AND kind='alert' AND text=? AND ts>?",
                  (text, time.time() - 86400)):
            continue
        sev = n.get("severity") if n.get("severity") in ("low", "normal") else "normal"
        db.post("out", text, chat=None, kind="alert", severity=sev)
        sent += 1
    def mark_sent() -> None:
        try:
            f.rename(f.with_name(NOTIFY_FILE + ".sent"))
        except OSError:
            pass

    # Only once the posts commit: a rolled-back run end leaves the file to be posted next time.
    db.after_commit(mark_sent)
    return sent


def relay_worker_escalations(p: Project, task: dict, run_id, run_dir: Path) -> tuple[int, int]:
    """Queue the `ttp notify --severity high` lines a worker run wrote (ESCALATE_FILE) as high
    observations for the coordinator: the user is not paged; the coordinator decides. Read each tick
    while the run lives (Daemon.relay_escalations) and again when it ends or a reboot ended it, so a
    killed run's lines still arrive. One event per distinct text per run, the first
    ESCALATIONS_PER_RUN texts only; the events table is the record, so a second read adds nothing.
    Returns (events queued now, distinct texts past the cap)."""
    try:
        with open(run_dir / ESCALATE_FILE, "rb") as fh:
            raw = fh.read(ESCALATE_READ_MAX)
    except OSError:
        return 0, 0
    texts: list[str] = []
    for line in raw.decode(errors="replace").splitlines():
        try:
            n = json.loads(line)
        except ValueError:
            continue   # a line cut short by the read limit or a crash mid-write
        body = " ".join(str(n.get("text") or "").split())[:2000] if isinstance(n, dict) else ""
        if body and body not in texts:
            texts.append(body)
    if run_id is None:
        run_id = run_dir.name
    queued = 0
    for body in texts[:ESCALATIONS_PER_RUN]:
        fp = f"escalate:{run_id}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"
        if p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            continue
        text = f"Worker run {run_id} of task #{task['id']} {task['title'][:80]} escalates: {body}"
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), f"worker:{task['id']}", "observation", fp, "high", text[:4000], "queued", task["id"]))
        log(p, f"task #{task['id']}: run {run_id} escalated to the coordinator: {body[:200]}")
        queued += 1
    past = max(0, len(texts) - ESCALATIONS_PER_RUN)
    if queued and past:
        log(p, f"task #{task['id']}: {past} `ttp notify --severity high` text(s) past {ESCALATIONS_PER_RUN} "
               f"in run {run_id} not escalated")
    return queued, past


def _last_notes(run_dir: Path, n: int = 5) -> list[str]:
    """The run's last `ttp note` lines."""
    try:
        lines = (run_dir / "progress.md").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [x[:300] for x in lines if x.strip()][-n:]


def _recent_spec_wakes(prev: dict, now: float) -> list[float]:
    """The times of a waiting task's spec-change wakes within the last hour (Daemon._spec_wake)."""
    got = prev.get(SPEC_WAKES_KEY)
    return [x for x in got if isinstance(x, (int, float)) and 0 <= now - x < 3600] if isinstance(got, list) else []


def _same_probe(full: dict, prev: dict, key: str = "retry_when") -> bool:
    """Whether a run's result.json `full` wrote the retry_when (or `key`) the task waits on (`prev`): the
    same one, or the one a resource move or `ttp task set-when` rewrote (kept as `<key>_orig`, None when
    the run wrote none)."""
    probe = full.get(key)
    return probe == prev.get(key) or f"{key}_orig" in prev and probe == prev[f"{key}_orig"]


def _wait_key(result: dict) -> str:
    """What a waiting hand-off waits for, as compared between wakes: its waiting_for and retry_when,
    whitespace and case aside, as a short hash a clipped hand-off keeps whole."""
    text = "\x00".join(" ".join(str(result.get(k) or "").split()).lower() for k in ("waiting_for", "retry_when"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _stale_wakes(prev: dict, key: dict, escalate: bool) -> int:
    """How many wakes in a row handed back the same wait (`key`) as the hand-off they woke from (`prev`).
    A first wait, a changed wait or a new branch head starts over at 0. A light wake that escalates,
    and a wake the host's reboot caused, neither count nor reset."""
    if not isinstance(prev, dict) or prev.get("status") != "waiting":
        return 0
    n = bud.stale_wakes(prev)
    if escalate or str(prev.get("woke") or "").startswith("the host rebooted") or prev.get(onpassdry.KEY):
        return n
    before = prev.get("wait_key")
    if not isinstance(before, dict):
        before = {"key": _wait_key(prev), "head": key.get("head")}   # a hand-off from before this was kept
    return n + 1 if before.get("key") == key.get("key") and before.get("head") == key.get("head") else 0


def _retry_s(result: dict) -> float:
    """A waiting hand-off's `retry_after_s`, kept between 5 minutes and 6 hours."""
    try:
        return min(max(float(result.get("retry_after_s") or 1800), 300.0), 6 * 3600.0)
    except (TypeError, ValueError):
        return 1800.0


# A probe that is only a clock comparison (`test "$(date +%s)" -ge <epoch>`, or in [ ] / [[ ]]).
_TIME_GATE_RE = re.compile(r'^\s*(?:test|\[\[?)\s+"?\$\(date \+%s\)"?\s+-(ge|gt)\s+"?(\d+)"?\s*(?:\]\]?)?\s*$')


def time_gate(probe: str) -> float | None:
    """The epoch a bare date-comparison probe passes at, else None: only time ends such a wait."""
    m = _TIME_GATE_RE.match(probe or "")
    return float(m.group(2)) + (m.group(1) == "gt") if m else None


def hold_reason(hold: dict | None) -> str:
    """The text of the hold dispatch recorded for a ready task it did not start (Daemon._resource_hold's
    data; None: nothing held it). It only formats that record: which pause, slot or lock holds a task
    is dispatch's to decide, never worked out again here. A lock names a holder only while its recorded
    pid lives (or cannot be judged); one whose every holder is gone has no live holder."""
    if not hold:
        return "no holder: dispatch did not start it"
    res, kind = hold.get("resource") or "?", hold.get("kind")
    if kind == "pause":
        return f"{res} paused"
    if kind == "busy":
        return f"{res} held by running task(s) " + (", ".join(f"#{t}" for t in hold.get("tasks") or []) or "?")
    if kind == "reserved":
        return f"{res} reserved for higher-priority task #{hold.get('task') or '?'}"
    if kind == "pre_grant":
        return hold.get("line") or f"{res} held by the project's pre-grant check"
    hs = [h for h in hold.get("holders") or [] if isinstance(h, dict)]

    def _who(h: dict, state: str) -> str:
        return f"{h.get('holder') or 'an unnamed holder'} (pid {h.get('pid') or '?'} {state})"
    live = [h for h in hs if h.get("alive") is not False]
    if live:
        return f"{res} lock held by " + "; ".join(_who(h, "alive" if h.get("alive") else "unknown") for h in live)
    if hs:
        return f"{res} lock taken, no live holder: " + "; ".join(_who(h, "gone") for h in hs)
    return f"{res} lock taken, holder unknown"


def _current_probe(task: dict) -> str | None:
    """The probe the daemon should be running for a task now: a hold's `when:` anchor, else its
    start_when, else its waiting hand-off's retry_when."""
    held = anchors.anchor(task) if task.get("status") == "blocked" else None
    if held and held[0] == "when":
        return held[1]
    when = deferral(task).get("when")
    if when:
        return when
    probe = load_result(task["result"]).get("retry_when")
    return probe if isinstance(probe, str) else None


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _flock(path: Path):
    """An fd holding an exclusive lock on path; False if another process holds it; None where
    the file system does not support flock."""
    try:
        import fcntl
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)   # not inherited by children (PEP 446)
    except (ImportError, OSError):
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return False
    except OSError:
        os.close(fd)
        return None


def _is_daemon(pid: int) -> bool:
    """pid is a live tt-project daemon, not a recycled pid now used by something else."""
    if not _alive(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "ttp" in cmd and "daemon" in cmd


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def heartbeat(p: Project) -> dict | None:
    """The daemon's last sign of life: {pid, host, started, receipt, age, tick_done}, or None if it never
    ticked. `age` is the heartbeat's (a step of a long tick refreshes it); `tick_done` is when the
    daemon last completed a full tick (None for a daemon without a receipt)."""
    hb = p.state / "heartbeat"
    try:
        info = json.loads(hb.read_text())
        info["age"] = max(0.0, time.time() - hb.stat().st_mtime)
        info["tick_done"] = identity.tick_done(p.state, info.get("receipt"))
        return info
    except (OSError, ValueError, TypeError):
        return None


def start_marker(p: Project) -> dict | None:
    """The last daemon process to start: {pid, host, started, tick_errors, progress, receipt}, or None."""
    try:
        info = json.loads((p.state / "daemon.start").read_text())
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


_STACK_FILE: list = []   # the one file faulthandler writes to, kept open while it is registered


def register_stack_dump(state: Path):
    """SIGUSR1 or SIGUSR2 makes this process append every thread's stack to state/STACKS_FILE
    (faulthandler: it works while the main thread is stuck, and needs no ptrace, which hardened hosts
    refuse to py-spy or gdb). ttp.watchdog sends SIGUSR1 to a hung daemon, the systemd unit's
    WatchdogSignal too; SIGUSR2 is for a dump by hand. A file past STACKS_MAX_BYTES is moved to
    <name>.1 first. Returns the open file, or None where it cannot."""
    path = state / STACKS_FILE
    try:
        if path.exists() and path.stat().st_size > STACKS_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        f = open(path, "a")
        faulthandler.register(signal.SIGUSR1, file=f, all_threads=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        return None
    try:
        faulthandler.register(signal.SIGUSR2, file=f, all_threads=True)
    except (ValueError, AttributeError, RuntimeError):
        pass
    for old in _STACK_FILE:
        old.close()
    _STACK_FILE[:] = [f]
    return f


def sd_notify(msg: str, addr: str | None) -> bool:
    """Tell systemd about this service (the unit's WatchdogSec expects WATCHDOG=1 after each completed
    tick, or it restarts the daemon). No-op outside systemd."""
    if not addr:
        return False
    if addr[0] == "@":   # an abstract socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    """pid runs: kill(pid, 0) finds it and it is not a zombie (ended, not reaped yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not zombie(pid)


def main() -> int:
    return Daemon(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).run()


if __name__ == "__main__":
    sys.exit(main())
