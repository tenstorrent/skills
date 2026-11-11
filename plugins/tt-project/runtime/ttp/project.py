# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project layout, configuration and user-level shared state (registry, secrets)."""
from __future__ import annotations

import copy
import difflib
import errno
import fcntl
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import gitlocks, statcache
from .db import DB

FOLDER = "tt-project"                     # lives at the root of the user's project, ignored by it
HOME_DIR = Path(os.environ.get("TTP_HOME", Path.home() / ".tt-project"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "core_provider": "claude",
    # The providers this project's tasks may run on, the core one always among them (provallow.py):
    # tier routing, failover and task pins stay inside it. Empty: every configured provider.
    "providers_allowed": [],
    "providers": {
        # Tiers name a difficulty class, never a model version: aliases resolve at run time and
        # the project (or the user) pins versions only when it has a reason to. Light runs on the
        # provider's cheaper model where its CLI names one without a version (Claude: sonnet); Codex
        # has no such alias (light keeps the account's default model at low effort). Cursor takes no
        # effort: its `auto` tiers resolve at run time to distinct models from the installed CLI's own
        # model list (cursortiers.py), and stay `auto` when it cannot list them; a tier the project sets
        # itself wins. A task's own model/effort pin wins over its tier, and a
        # failed light try retries a tier up, on the standard model.
        # billing: how the provider's spend is billed (billing.py). auto: plan windows decide; plan: a
        # plan seat with no meter (no dollar cap, spend is plan spend); usage: always the dollar caps.
        "claude": {"tiers": {"light": {"model": "sonnet", "effort": "low"},
                             "standard": {"model": "opus", "effort": "high"},
                             "deep": {"model": "opus", "effort": "max"}}, "billing": "auto"},
        "codex": {"tiers": {"light": {"model": "", "effort": "low"},
                            "standard": {"model": "", "effort": "high"},
                            "deep": {"model": "", "effort": "xhigh"}}, "billing": "auto"},
        "cursor": {"tiers": {"light": {"model": "auto", "effort": ""},
                             "standard": {"model": "auto", "effort": ""},
                             "deep": {"model": "auto", "effort": ""}}, "billing": "auto"},
    },
    "budget": {
        "reserve_pct": 10,              # plan windows: never take the account past 100 - reserve
        "daily_usd": 100.0,             # applied when the plan reports no window (usage-billed)
        # Unset: WEEKS_OF_DAYS times the daily cap in force (weekly_default), never below it.
        "weekly_usd": 700.0,
        # Usage-billed: all of the account's tt-project spend today, every project on every machine
        # tt-project can see (globalcap.py), stops new work at this many $ (0 = off). The day starts at
        # day_start ("HH:MM"; "" = rolling 24 h) in timezone (IANA name, never the host's own zone; "" =
        # the project's home zone, timefmt.py).
        # Usually set once per machine for every project: `ttp config --account KEY VALUE`. $200 and
        # rolling by default; plan windows never count toward it.
        "global_daily_usd": 200.0,
        # A spend goal, not a cap: ttp status and the daily review show today's Claude spend against it
        # (0 = unset, the default; `ttp config --account budget.daily_target_usd <usd>`).
        "daily_target_usd": 0.0,
        "day_start": "",
        "timezone": "",
        "hourly_alarm_x": 4.0,          # spend rate this many times the 7-day hourly norm = runaway
        # Free window resets a provider grants (credits.py): one is consumed by itself, earliest-expiring
        # first and at most once per window, when a window holds work at the line and resets more than
        # reset_credit_min_hours from now, or when the next credit expires within
        # reset_credit_expiry_hours and a window is at least reset_credit_expiry_use_pct % used.
        "reset_credits_auto": True,
        "reset_credit_min_hours": 12.0,
        "reset_credit_expiry_hours": 48.0,
        "reset_credit_expiry_use_pct": 50.0,
        "max_parallel_workers": 6,          # on a plan, all of them run until the last stretch before the line
        "urgent_extra_slots": 2,            # worker slots an urgent task may take beyond them (ttp/urgent.py; 0: off)
        "urgent_preempt": True,             # preempt the lowest-priority routine run for user-ordered urgent work (ttp/preempt.py)
        "restart_max": 3,                   # coordinator restarts of a running task (ttp/preempt.py) per restart_window_h
        "restart_window_h": 6.0,
        "urgent_topup": True,               # an urgent or user-ordered task's spent budget is raised, not held (ttp/topup.py)
        "task_default_usd": {"light": 2.0, "standard": 8.0, "deep": 25.0},
        "run_timeout_s": {"light": 1200, "standard": 3600, "deep": 7200},
        "stall_s": {"light": 900, "standard": 1800, "deep": 2700},
        # Worker and reviewer context is compacted once it nears this many tokens (0 = the agent's
        # own default). Long runs otherwise re-read a huge context on every call. Claude Code
        # accepts 100k to 1M (smaller values become 100k) and compacts about 33k below the window.
        "compact_window_tokens": {"light": 100000, "standard": 150000, "deep": 200000},
        # A worker whose run has re-read this many context tokens (cache reads, summed over its calls)
        # is told once to finish its step and hand the rest on as a follow-up (0 = never, the default:
        # a worker compacts its own context in place instead of ending its run as it grows).
        "split_reread_tokens": {"light": 0, "standard": 0, "deep": 0},
        # Claude Code keeps a Bash command's output inline up to this many characters (it accepts
        # 4000-128000; its own default is 30000); longer output goes to a file, and the worker gets a
        # short preview and the path. 0 leaves the agent's default.
        "bash_output_max_chars": 12000,
        # Prompt cache lifetime of worker, reviewer and every other run but the coordinator's
        # (coordinator.cache_ttl): "5m" (a cache write at 1.25x input; calls within a run are rarely
        # 5 min apart), "1h" (2x input), or "off" (the agent's own default: Claude Code picks 1h on
        # a Claude subscription, which a replay of live runs found costs 11-27% more; cachettl.py).
        "worker_cache_ttl": "5m",
        "exclusive_wait_s": 600,        # an exclusive run waiting for its resource gives up after this
        "exclusive_reserve_s": 600,     # a higher-priority task's reservation of an exclusive resource lapses after
                                        # keeping a free slot unused this long (0: no reservations)
        # A hold kept for the detached jobs of a done, failed or cancelled task raises a low alert
        # once it has held its resource this long (0 = never). Nothing is released automatically.
        "stale_hold_alert_s": 21600,
        # A project paused this long raises one normal alert naming who paused it, why and since when,
        # reminded at most once a day and cleared on resume (projpause; 0 = never).
        "paused_alert_s": 14400,
        # After an outage ends, new workers start this many above those still running and double every
        # ramp_step_s seconds until they pass max_parallel_workers (rampup.py; 0 = all at once).
        "ramp_start": 2,
        "ramp_step_s": 180,
        # A run that ends without reporting its cost is estimated from its tokens at the project's
        # own observed rate; until there is one, this $ per million weighted tokens (set high).
        "estimate_usd_per_mtok": 15.0,
        "max_reboot_losses": 3,         # a task lost to this many abrupt host reboots is blocked: it may cause them
        "max_job_relaunches": 3,        # a `ttp detach --resumable` job is started again after at most this many reboots
        # A run the host took away (reboot, sleep, lost supervisor) after this much spend or time
        # continues its agent session where the provider can; below both, or with `false`, it starts fresh.
        "resume_lost": {"min_usd": 0.5, "min_s": 600},
        # After the host wakes from a sleep, nothing new starts until it has been awake this long, so
        # a laptop's brief maintenance wakes start no runs that the next sleep would cut.
        "wake_settle_s": 300,
    },
    # A waiting task whose `retry_when` probe still says "not yet" sleeps on. This long after its
    # hand-off the coordinator is asked once (`wait_stale`) to fix the probe or cancel; it still sleeps.
    "waiting": {"max_hold_s": 21600,
                # a wait's retry_when / start_when probe runs model-free this often, 20-180 s; a pass
                # wakes the task at once (waitpoll.py)
                "poll_s": 90,
                # an urgent task waiting on a probe that is not a bare clock gate sleeps at most this long
                # per timer; a "not yet" re-sleeps it with no model call (waitpoll.py; 0: off)
                "urgent_retry_cap_s": 300,
                # a queued task whose start_when says not yet this long while it names a device or
                # serving resource is raised once (0: never)
                "start_resource_stale_s": 10800,
                # a start_when still false this long shows as a warning in status and the feed (0: never)
                "start_gate_warn_s": 1800,
                # a landed:#<id> start_when whose producer is done, not landed, with nothing open that
                # can land it, this long, is raised once per producer (deadwait.py; 0: at once)
                "landed_wait_dead_s": 900,
                # a wait whose outside probe gives the same absent answer (404, not listed) this many
                # times in a row is closed as unavailable, with a reopen watch (absentwait.py; 0: off)
                "absent_close_n": 3,
                # a queued code or work task waiting on another task (landed:#, depends_on, waits_end:)
                # this long raises one lane_wait event (lanewait.py; 0: never)
                "lane_wait_event_s": 600,
                "max_stale_wakes": 3,           # max_stale_wakes: 0 = off
                # after a spec wake (urgent or wake_now) whose run hands back the same wait, later spec
                # wakes of that task sleep on its retry_when this long (Daemon._spec_wake; 0: off)
                "spec_wake_hold_s": 7200,
                # the wait-wake gate (waitgate.py): a due wake of a waiting task first checks model-free
                # rules (job, lock, checks or push still pending; same probe answer) then Jev, and re-defers
                # with a doubling backoff instead of a model turn that would only hand the wait back. Any
                # event wakes at once; max_skips skips in a row or max_skip_h hours since the last turn
                # re-ask the watcher (no answer wakes), and cap_hard_h hours force the wake. enabled
                # false: every wake starts a turn, as before
                "wake_gate": {"enabled": True, "max_skips": 4, "max_skip_h": 4.0, "cap_hard_h": 24.0,
                              "backoff_s": 600, "max_backoff_s": 3600, "jev_skip_below": 0.25,
                              # a wait's own checks whose log has not advanced this long are not held
                              "checks_stall_s": 1800},
                # every task that could start waits on hosts this machine cannot reach (probe exit 255),
                # nothing runs: one normal alert this long into the outage (0: never); the outage ends
                # once no probe has said unreachable for the debounce (netwait.py). Hosts whose ssh route
                # ends at loopback (a tunnel from the user's workstation) get a low feed line instead,
                # raised once to normal when the outage is tunnel_outage_page_after_s old (0: at once)
                "unreachable_alert_s": 900, "unreachable_debounce_s": 600, "tunnel_outage_page_after_s": 3600,
                # whether this machine is a workstation, whose VPN or network sign-in the outage alert
                # may ask the user to redo (null: detect; a server never gets that advice)
                "workstation": None,
                # a waiting hand-off's on_pass_cmd, run model-free once its probe passes, is killed after
                "on_pass_timeout_s": 600,
                # and dry-run once (checked, never run; this many seconds) when its run hands off, so a
                # command that cannot run is fixed in that run's session, not after a wake (onpassdry.py)
                "on_pass_dry_run": True, "on_pass_dry_run_timeout_s": 30,
                # detached jobs' use of what they asked for (jobuse.py): sampled every sample_s; cores
                # used under under_frac of the request, or an unused device, over window_s is under-use
                # (one job_underuse event per job and repeat_s; the next check comes within check_cap_s)
                "job_use": {"sample_s": 60, "window_s": 900, "under_frac": 0.25, "check_cap_s": 1800,
                            "repeat_s": 21600},
                # `ttp progress report` (progress.py): stall_after refusals, stalls or failures in a row
                # with no result, the last within window_s, are a stalled driver (one observation)
                "progress": {"stall_after": 5, "window_s": 86400}},
    "resources": {},                    # shared-slot limits, e.g. {"device": 1}
    # How many tasks naming a resource may run at once (shared.task_cap), apart from its `ttp lock`
    # slots above: e.g. {"fleet": 8} runs 8 tasks on a multi-box alias while `ttp lock fleet` stays one
    # holder. Unset: twice the resource's slots.
    "resources_tasks": {},
    # Lock names that all mean the one device (they share one `ttp lock` slot), and how many tasks
    # tagged needs_device may run at once (max_tasks null: derived from the project's device resources
    # and budget.max_parallel_workers, loadcaps.py); `ttp lock` admits one of them at a time to the device.
    # runners: serial device-job runners by name, each {"host", "dir", "health", "drop_check", ...}
    # (devq.RUNNER_DEFAULTS); tasks queue device jobs on one with `ttp devq submit` instead of each
    # running its own detached driver. Empty: none, and workers keep the detached-driver path.
    # nodes: globs of the device files a job opens to use the device (Linux), so a detached job of a
    # task holding the device that opens none of them counts as leaving it unused (jobuse.py).
    # hosts: {resource: ssh host} for `ttp ssh-preflight @<resource>` (hostmap.py), so a probe
    # follows a resource that moves to another box; runners' hosts count too.
    "device": {"locks": [], "max_tasks": None, "runners": {}, "runner_restarts_per_day": 6, "nodes": [],
               "hosts": {}},
    # pre_grant_check: a shell command asked before a task gets a resource's lock (pregrant.py), for
    # a project's own holds on top of the locks; it gets the resource and task id, exit 0 grants, 1 or
    # 75 holds, anything else or no answer in pre_grant_timeout_s holds and is raised once. "": off.
    # wait_alert_min: a waiter on a lock past this many minutes raises one lock-wait:<name> alert per
    # lock, naming its holder (lockreap.py); 0: off. wide: lock names that count as site- or cluster-wide
    # in the lock order a lock-cycle alert checks, beside the words of lockcycle.WIDE_WORDS.
    "locks": {"pre_grant_check": "", "pre_grant_timeout_s": 10, "wait_alert_min": 30, "wide": []},
    "shared_resources": [],             # resources whose slots and pause all the user's projects share
    # Machines in the user's list (ttp machines) this project uses, beside those its resources and
    # shared_resources name: the change ledger, health coverage and idle capacity read only these.
    "machines": {"allowed": []},
    # model / effort, when set, override the coordinator's defaults. Unset, every coordinator turn
    # runs at coordinator.DEFAULT_EFFORT (xhigh, or the provider's highest effort below it) on
    # model_tier's model: the strongest tier's ("deep", read from the provider's tier table; "" = its
    # own tier's), so the cheaper light-tier model for workers does not move the coordinator. A change
    # applies at the next turn (coordinator.coordinator_route); `ttp status` shows it against the last
    # turn's (coordmodel.py).
    # Triggers (coordinator.effort_triggers) mark a tricky or blocking turn, which the turn gates never
    # skip. A task failing repeat_fails_24h times in
    # 24 h makes one, and so do its external waits when their reason, probe or owner changes, and once
    # per run of waits when they have gone on for 24 h or reach repeat_waits_24h in 24 h (0 turns
    # either count off). A wait on its own live
    # `ttp detach` jobs or `ttp lock` resources counts only past live_waits_max waits on the same ones
    # in 24 h or once a job's log stops growing (0: every such wait counts as before).
    # effort_skip_triggers: trigger labels (as recorded in the turn's note) that never count.
    "coordinator": {"tier": "light", "model": "", "model_tier": "deep", "effort": "",
                    "repeat_fails_24h": 2, "repeat_waits_24h": 8, "live_waits_max": 6,
                    "effort_skip_triggers": [],
                    "debounce_s": 15, "max_events_per_turn": 40,
                    "max_turns_per_hour": 30, "max_new_tasks_per_day": 200, "idle_wake_s": 3000,
                    "starve_wake_s": 300,
                    # The median cost of the last 5 session wakes over which one alarm names the cause
                    # and the session compacts in place and sends a leaner digest (wakecost.py; 0: off).
                    "wake_cost_alarm_usd": 1.0,
                    # A project's own harness/prompts/worker.md over this many times the template's size is
                    # flagged to its coordinator, in doctor and the daily review (promptdrift.py; 0: off).
                    "prompt_drift_ratio": 1.5,
                    # An event batch that a turn which decided nothing already met, against the same
                    # state, is settled without a model run for this long after it (turngate.py; 0: off).
                    "skip_repeat_window_s": 86400,
                    # After a turn's action is rejected, the same unchanged cause (event kind, source and
                    # task) is counted, not woken, for this long (rejcool.py; 0: off).
                    "rejection_cooldown_s": 900,
                    # A routine batch (quiet_held_kinds) or idle wake while every open task is held by a
                    # user pause, a deferral or a paused resource, and nothing changed since the last
                    # turn, is settled without a model run; one runs once the last turn started
                    # quiet_held_max_s ago (0: no limit; quietgate.py; quiet_held false: off).
                    "quiet_held": True, "quiet_held_kinds": ["idle_capacity"], "quiet_held_max_s": 86400,
                    # A command run on a batch of only routine events (task_done, upstream_note,
                    # observation) that may settle them without a model; "" is off (settlehook.py).
                    "settle_cmd": "",
                    # No worker run started for this long while dispatch would start a queued task:
                    # one high observation per stall lists those tasks (Daemon.check_stall; 0: off).
                    "stall_wake_h": 3,
                    # The digest's Running workers section (coordinator.workers_lines, from state/runwatch.json)
                    # is cut at this many chars, flagged runs first, then '+N more' (0: off).
                    "live_view_chars": 1200,
                    # A ready obligation owner or priority-1 task held this long only because every
                    # worker slot is taken: one observation per task names it and the running tasks
                    # holding the slots (Daemon.check_slot_wait; 0: off).
                    "urgent_slot_wait_min": 10,
                    # A task waiting this long on a queue, cap or admission (checks, push, worker slots, a device
                    # or lock, an outside pool, CPU): one must-act item per task and queue with unblock options,
                    # raised again each such span until the coordinator acts (queuewait.py; 0: off).
                    "queue_wait_escalate_min": 30,
                    # A coordinator reply or notify that is only an acknowledgement, progress, a queue wait or
                    # a forecast correction (and onhost's 'Status now' footer) waits for one digest per chat,
                    # posted at most this often; results, decisions, outages and asks go at once
                    # (chatdigest.py; 0: every post goes at once).
                    "chat_digest_h": 3,
                    # A status post (progress, counts, ETAs) goes to a chat at most this often; a sooner
                    # one waits for that chat's digest. `chat_status_gap_chats` {chat: hours} sets a
                    # chat's own (chatdigest.py; 0: no gap).
                    "chat_status_gap_h": 1,
                    # A user-ordered urgent task (fresh `urgent:` line) with a machine or device resource
                    # still queued this long, or waiting this long in `ttp lock`: one high observation
                    # names what holds it (Daemon.check_urgent_start; 0: off).
                    "urgent_start_wait_min": 5,
                    # A woken wait (its probe passed) not started this long after it became ready:
                    # one resume_wait observation names what holds it (Daemon.check_resume_wait; 0: off).
                    "resume_wait_min": 15,
                    # A device resource of the project idle this long while a slot is free, the gate is
                    # green or yellow and nothing queued would start: one idle_capacity event (idlecap.py;
                    # 0: off). Still idle after it: raised again after the rearm, doubling up to 48 h.
                    # Only devices a task named in the last idle_capacity_use_days count (0: all).
                    "idle_capacity_s": 1500, "idle_capacity_rearm_s": 21600, "idle_capacity_use_days": 7,
                    # Routine events (task done, follow-ups, notes, normal observations) wait up to
                    # this long for company while every worker slot is busy or runnable work is queued.
                    "batch_s": 300,
                    # A batch of only plain hand-offs (a normal task_done of no review with nothing to
                    # queue or decide) starts no turn: it joins the next wake of any other cause or the
                    # idle wake, at most this long after its oldest event (the idle_wake_s default; 0: off).
                    "plain_done_hold_max_s": 3000,
                    # idle_wake_s (3000) and this hold sit inside the 1h prompt cache with room for
                    # dispatch, so the turn they start reads the cache. While work is queued or running
                    # (no idle wake), a turn comes this long after the coordinator's last cache use, at
                    # most cache_wake_max times in a row on an unchanged state (daemon.cache_wake_at;
                    # 0: off). A user's or coordinator's value wins over these defaults.
                    "cache_wake_s": 3000, "cache_wake_max": 2,
                    # Routine event-only turns (task done, follow-ups, upstream notes, check-slot waits,
                    # waits) start no sooner than this long after the last turn ended, unless a slot is
                    # free with nothing ready to fill it (eventhold.py; 0: off).
                    "event_hold_s": 300,
                    # Event-only batches with no immediate event (task ends, hand-offs, timers, waits)
                    # wake at most once per this long after the last turn ended, a free slot included;
                    # user messages and immediate events wake at once (eventhold.gap_until; 0: off).
                    "wake_gap_s": 300,
                    # Relayed messages (`ttp say` from another agent) with no order or question wait up to
                    # this long after the oldest, so a burst starts one turn; an order or a question, and
                    # the user's own channels, wake at the debounce (msgwake.py; 0: off).
                    "relay_window_s": 90,
                    # The watcher model (babysit.ask) folds such a batch into the next wake, kept on disk;
                    # a folded batch wakes once its oldest event is wake_triage_max_s old (waketriage.py).
                    "wake_triage": True,
                    "wake_triage_max_s": 1800,
                    # High events within this long after the turn high events woke (from its end) start
                    # no turn of their own: one follow-up turn reads them all when it ends. User
                    # messages, critical and safety-class events never wait (bursthold.py; 0: off).
                    "burst_window_s": 120,
                    # The coordinator's system prompt (role, charter, STANDING, standing memory) is a
                    # snapshot, re-rendered when no turn ran for cache_ttl (the cache is cold anyway),
                    # once it is this old, or at once for a restrictions change or a charter edit on
                    # the user's word; until then the digest carries what changed.
                    "prompt_max_age_s": 14400,
                    # Prompt cache lifetime of coordinator turns: "auto" (1h, a write at 2x input
                    # instead of 1.25x, unless a 1h turn with the same prompt, schema, model, effort
                    # and agent CLI started under cache_warm_min minutes ago: then 5m, which still
                    # reads that turn's 1h entries; also 5m when no next turn looks likely within
                    # the hour: nothing running or due, no recent user message, long turn gaps;
                    # cachettl.py), "1h", "5m", or "off" (the agent's
                    # own choice, and charter and memory stay in the system prompt).
                    "cache_ttl": "auto", "cache_warm_min": 50,
                    # The persistent coordinator session (coordsession.py), always on with no
                    # setting to turn it off: every wake resumes one agent session, also after a
                    # daemon restart or a reboot; a failed resume reruns in the same session (a new
                    # one, seeded with the hand-off, only once the provider rejected it). The session
                    # never rolls over as its context grows: it compacts in place once its last call's
                    # context reaches its first prompt (or its size after the last compaction) plus
                    # compact_growth_tokens, and never so soon that it would free under compact_min_free
                    # of it, at most context_window_tokens (0: by model) less compact_margin_tokens
                    # (compact_tokens above 0: a fixed size instead);
                    # row_delta false sends each changed digest section whole (else keyed lists go by row).
                    # worker_context: what a worker or reviewer run starts with (ctxfork.py): "fork" a copy of
                    # this session at any size (same provider, written within worker_fork_max_age_h; it
                    # compacts in place if it must; worker_fork_max_tokens above 0 caps it; a run on another
                    # Claude model forks only about worker_fork_other_model_tokens of its last turns, 0: the
                    # disk block), else "disk" the ledger, its note and worker_chat_chars of chat; "off" neither.
                    "session": {"compact_tokens": 0, "compact_growth_tokens": 100000,
                                "context_window_tokens": 0, "compact_margin_tokens": 40000, "compact_min_free": 0.3,
                                "row_delta": True,
                                "worker_context": "fork", "worker_fork_max_tokens": 0,
                                "worker_fork_max_age_h": 12, "worker_fork_other_model_tokens": 8000,
                                "worker_chat_chars": 30000},
                    "turn_budget_usd": 1.0,
                    # The most the daemon raises one turn's cap to, once, for a single item that went
                    # over turn_budget_usd alone (0: twice turn_budget_usd); never above the
                    # dollar caps (turnsize.py). project.json is not changed.
                    "turn_budget_ceiling_usd": 0,
                    # Turn timeout (coordlimits.turn_timeout): turn_timeouts_s.<provider>.<effort> set
                    # here wins, then a flat turn_timeout_s (0: unset; a stored 600, the old default,
                    # counts as unset), then the built-in table. The ceiling can be lowered, never
                    # raised past the runtime's one-hour hard bound.
                    "turn_timeout_s": 0, "turn_timeouts_s": {},
                    "turn_timeout_ceiling_s": 3600, "sweep_prompt_chars": 12000,
                    "ask_timeout_h": 0.5,   # unanswered asks resolve to their default (askauto.py)
                    # An open restriction, irreversible or human ask that holds a blocked task longer
                    # than this goes back to a tricky turn once to decide it (askrethink.py); 0 off.
                    "ask_rethink_h": 2,
                    # A task deferred with `start_when` that has not started after this long is raised
                    # to the coordinator once.
                    "defer_max_days": 14,
                    # A blocked task with no anchor (see anchors.py) held longer than this is raised to
                    # the coordinator as a stale hold; 0 leaves only the 'user wrote since' rule.
                    "hold_max_h": 12,
                    # A queued task waiting on a task in 'review' longer than this is raised to the
                    # coordinator once per review stint; 0 turns it off.
                    "review_stall_s": 1800,
                    # A task in 'review' whose latest review failed or asked for changes, with no fix
                    # or re-review open, is raised once per failed review this long after it ended; 0 off.
                    "review_failed_unhandled_s": 3600,
                    # Provider failover (failover.py): a queued or waiting task whose provider cannot
                    # start new work for longer than provider_failover_after_h hours (0: off) moves to
                    # the first usable of provider_failover_to (provider names), then the core provider;
                    # a logout counts once it lasts provider_logout_failover_s seconds (0: at once).
                    "provider_failover_after_h": 4, "provider_failover_to": [],
                    "provider_logout_failover_s": 900,
                    # Tier floor for conclusion runs (tierfloor.py): a question or plan task, a chat
                    # answer, a spec with a `conclusion:` line, a report schedule's run, a review or
                    # verdict and a device or machine run starts at least at this tier and effort, never
                    # on the chat provider ('standard/high'; 'off' turns the floor off).
                    "conclusion_floor": {"tier": "standard", "effort": "high"}},
    "notify": {"slack": False, "slack_min_severity": "high", "chat_min_severity": "normal",
               "hourly_status": True},   # hourly.py: the hourly status table in the main chat
    # gone_after_s: a relay chat with no `ttp listen` running that left a message unread this long
    # stops counting as a reader of asks and alerts until it reads again (chatgone.py). batch_s:
    # `ttp listen` holds broadcast alerts this long to print them together; lease_s: another relayer
    # is refused a chat id whose relayer listened this recently (relayers.py).
    "relay": {"gone_after_s": 86400, "batch_s": 120, "lease_s": 600},
    # code_tasks_may_push: true: a code task whose spec asks it to land on delivery.push_branch may run
    # `ttp push` itself, so no separate landing task is needed. "approved" (the default): a code task
    # or the coordinator lands a head an independent review approved (approved_heads_may_push). Off:
    # only review tasks and the push queue push.
    # drafts_during_freeze: opening and updating draft PRs needs no ask even under a code freeze.
    # Off: a draft PR under a code freeze needs the user's word (coordinator prompt and ask gate).
    "delivery": {"draft_prs": True, "review_before_pr": True, "auto_merge_repos": [],
                 "push_allowed": True, "code_tasks_may_push": "approved", "drafts_during_freeze": True},
    # Review tasks run light when the diff under review touches no risky_paths glob and no code file
    # with a risky name (risky_names, default budget.RISKY_NAMES) and is docs and tests only or at
    # most light_max_lines non-doc lines; otherwise standard. A re-review after a failed review applies
    # the same rule to its fix delta (failed head..new head), and runs standard when that delta spans a
    # rebase onto new upstream code. Only the coordinator picks deep. A non-empty light_paths (globs) also requires every
    # non-doc file to match one of them.
    # With `auto`, the daemon queues the review of each finished code task with a routine hand-off
    # whenever delivery has a review step (review_before_pr or a push_branch); auto_notes ends each
    # such review's spec. With area_fail_cap (0 off) reviews of one area failed within 48 h, across
    # stacks, the daemon queues no automatic fix for it and the coordinator re-plans (reviewcap).
    # A failed review gets its fix and re-review from the daemon auto_fix_rounds times per stack (1: a
    # failed re-review goes to the coordinator as one review_nofix event, not a third round). Each fix
    # resumes the session of the task it continues, at most hop_max resumed hops deep in one lineage
    # (0: no cap); past it the fix starts fresh and the coordinator gets one review_hop_cap event (ctxfork).
    # The daily review's self-efficiency audit lists tasks blocked longer than blocked_long_h hours.
    # It also lists each Restrictions item that only blocked work over 7 days: at least rule_blocks_min
    # blocked runs and asks citing it, and no refused action citing it (restriction_audit).
    # A code task left in review with no review sweep_after_s (0 off) after its last run gets one
    # from the daemon (sweep_reviews), which only queues reviews and never closes a task. A harness
    # task gets one too, unless its commit is merged into the harness: then it is closed as done. A done code
    # task with an unlanded head and no review gets one too (sweep_unreviewed): at most
    # unreviewed_per_pass (0 off; null: budget.max_parallel_workers) per pass, for tasks updated within
    # unreviewed_max_age_h hours; one waiting externally (a busy lock too) only after unreviewed_wait_s.
    # A review whose head carries a change with a failed review and an open fix waits for that fix
    # to land or its chain to end, at most carried_hold_max_h hours (0: no cap; reviewhold).
    "review": {"light_max_lines": 80, "risky_paths": [], "light_paths": [], "auto": True, "auto_notes": "",
               "area_fail_cap": 3, "blocked_long_h": 12, "rule_blocks_min": 3, "sweep_after_s": 300,
               "unreviewed_per_pass": None, "unreviewed_max_age_h": 72, "unreviewed_wait_s": 1800,
               "carried_hold_max_h": 24, "auto_fix_rounds": 1, "hop_max": 2,
               "fork_target": 0.9},   # hourly.py: the share of worker and review runs started from a fork or resume
    # Continue chains (chaincap): a task_add that continues a lineage past max_hops hops, max_stalls
    # failed or no-progress hops in a row or max_usd spent (0 turns one off), or one in an area under
    # review.area_fail_cap, is refused unless its `reason` names what changed in a tricky turn.
    # With auto, a done hop with progress whose only proposal is the next `continue:` hop gets it
    # queued by the daemon, with no coordinator turn. With urgent_followups, an urgent task's (or an
    # obligation owner's) continue and next-step follow-ups are queued at once under the same caps (urgentfup).
    # With requeue, a code task whose change is neither approved nor landed is re-queued for that hop
    # itself (chaincap.requeue: same id, worktree and branch) instead of a new task.
    # split_lines (0 off): kind-code.md asks a worker whose change would pass about this many non-test
    # lines to hand it off in parts, each reviewed and landed on its own (prompts.split_lines).
    # A task that ran max_task_runs times since its last explicit requeue, or whose last max_idle_runs
    # runs spent money with no hand-off and no commit, is not started again (0 turns one off): it is
    # blocked and the coordinator gets one run_cap event summing up its runs (runcap).
    "chains": {"max_hops": 8, "max_stalls": 2, "max_usd": 40.0, "auto": True, "urgent_followups": True,
               "fold_followups": True,
               "requeue": True, "split_lines": 200, "max_task_runs": 4, "max_idle_runs": 2},
    # Each Jev use (e.g. "screen") is switched off once its net saving over window_days is not
    # positive, judged after min_calls calls or once its first call is a window old, or once idle_calls
    # calls in the window changed none of the rules' decisions; uses.<use> = "on" or "off" forces it
    # (see jevuse.py).
    "jev": {"enabled": "auto", "window_days": 7, "min_calls": 30, "idle_calls": 20, "uses": {}},
    # Effort A/B (effortab.py): enabled puts each standard-tier code task in a high or medium effort arm
    # by its lineage's root id (on by default; the end rule turns it off once it records a winner);
    # jev_pick lets Jev choose medium or high from the spec (off by default).
    "effort_ab": {"enabled": True, "jev_pick": False},
    "web": {"bind": "127.0.0.1", "port": 0},
    "detach": {},                       # launcher_stops: see EXTRA_KEYS
    "power": {},                        # keep_awake: replaced by runner.prevent_idle_sleep; "off" still turns it off
    # Disk guard: with free space under the project folder below the smaller of min_free_pct of the
    # disk and min_free_gb (either 0 = off), only question and plan tasks start; a machines-list entry's
    # own min_free_gb replaces min_free_gb on that machine (see machines.py). Once tripped, the guard
    # holds until resume_free_gb are free (the threshold when below it; 1.2 times the threshold when
    # at or above the disk's size); null, or a machine's own threshold, resumes at 1.2 times the
    # threshold. Finished tasks'
    # worktrees lose their cache_dirs (null = the built-in list) and are removed once clean with
    # HEAD on a branch and no submodules set up, at least an hour after the task ended, or
    # worktree_retention_days after it (0 = never tidy). One whose only dirty entries are untracked
    # files totalling at most worktree_leftovers_max_mb (0 = never) has them moved to its last run's
    # worktree-leftovers/ first. Branches are never deleted. A task worktree remade on its own branch
    # leaves its loose files in state/worktree-leftovers/<t<id>-date>/, deleted after
    # worktree_leftovers_keep_days (0 = never).
    "disk": {"min_free_pct": 5, "min_free_gb": 150, "resume_free_gb": None, "worktree_retention_days": None,
             "cache_dirs": None, "worktree_leftovers_max_mb": 50, "worktree_leftovers_keep_days": 30,
             "worktree_archive_max_mb": 200},
    # Workers and reviewers run with the project's Python venv active (VIRTUAL_ENV, PATH), so a
    # fresh task worktree does not rebuild one. "auto" finds .venv or venv in the project root; a
    # path (absolute, or relative to the project root) names one; "" turns it off. A working
    # directory with a venv of its own keeps that one. Every worktree of the project's repository
    # (a task's, a reviewer's, the push queue's; worktree.prepare) gets a symlink to each
    # link_paths entry (relative to the project root) that exists and is git-ignored there and is
    # missing in the worktree, so checks naming `.venv/bin/python` work; [] turns it off. Code tasks
    # always run in a worktree of their own; `kinds` adds other task kinds (say ["work"]) so they
    # do not edit the project root's checkout (never review or harness). Making a task's worktree:
    # each git command gets git_timeout_s, scaled by the host's load per CPU (at most
    # git_timeout_max_s); create_attempts tries in one go (a whole worktree a timed-out one left is
    # reused, a half-made one remade); then the task is queued again create_retries times, after
    # retry_backoff_s (doubling), before it is blocked for the coordinator. root_branch: the branch the
    # project root's checkout belongs on (unset: the one the daemon last saw it on); a root left
    # detached or off it is put back when clean, else alerted, and never branched from. shared_branches:
    # more branches many tasks start from (beside main, master, the push branch and delivery.base_ref),
    # which no task claims as its own.
    "worktree": {"venv": "auto", "link_paths": [".venv"], "kinds": [], "root_branch": "", "shared_branches": [],
                 "git_timeout_s": 120,
                 "git_timeout_max_s": 900, "create_attempts": 2, "create_retries": 3, "retry_backoff_s": 120},
    # Commits of the harness's own files (charter, memory, schedules): each git command gets timeout_s.
    # A lock file in its .git older than stale_lock_s (unset: 600 s; 0: off) that no process holds (one a
    # killed commit left) is moved aside under .git/ttp-stale-locks/ before committing and by the daemon's
    # sweep (ttp.gitlocks). Every sweep_every_s the daemon commits charter and memory files a failed
    # commit left uncommitted (see Project.sweep_harness).
    "harness_git": {"timeout_s": 120, "sweep_every_s": 600},
    # A command watcher's known open issue wakes the coordinator again every rewake_after_h
    # while it is still reported (null = never by time alone), or every time its observation says "repeat": true.
    # same_subject_warn: a watcher that opens more issues than this on one subject in 24 h gets a log
    # line and a digest and daily-review note, no wake (0: off). reraise_window_h: a keyed issue that a
    # run reporting nothing closed, sent again under its key within this many hours at the same or a
    # lower severity, only counts; it stays closed and wakes nobody (null or 0: off).
    "screen": {"wake_min_severity": "normal", "rewake_after_h": 6, "same_subject_warn": 3, "reraise_window_h": 6},
    # The stale queued tail (queuetrim.py): a task that never started is cancelled, with its reason and a
    # `ttp task <project> requeue <id> --force` undo, after expire_h queued (no open dependency, no start_when) or
    # dead_dep_h blocked on a failed or cancelled dependency (0: off). Urgent, user-ordered, review and
    # obligation-owner tasks never expire.
    "queue": {"expire_h": 48, "dead_dep_h": 24},
    # Tasks created per day (throughput.daily_line): with daily_target set, `ttp throughput` and the
    # coordinator's STATE show the tasks created in the last 24 h against it (0: unset, no line).
    "tasks": {"daily_target": 0},
    # A code task's `premise_cmd:` (premise.py) runs model-free before each dispatch, killed after
    # cmd_timeout_s; an exit 0 lets the task dispatch for recheck_min minutes, then it runs again.
    "premise": {"cmd_timeout_s": 60, "recheck_min": 30},
    # Builtin watchers (prs, logfile): one run stops at deadline_s (capped at 1800), keeps what it read
    # and reports one error saying it was cut short; each gh call times out after gh_timeout_s, cut to
    # the time left. The prs watcher reads pr_workers PRs at once, each within pr_timeout_s, each PR once
    # however many tasks name it; with no deadline_s in its payload, its deadline grows with the PR count
    # (up to 300 s). A schedule's payload may set any of them for itself.
    # gh_anon_cap: anonymous GitHub calls an hour from this host while gh is logged out (ghanon; 0: none).
    "watchers": {"deadline_s": 90, "gh_timeout_s": 20, "pr_workers": 4, "pr_timeout_s": 60, "gh_anon_cap": 30},
    # credentials false: this project keeps no GitHub login here, so no alert suggests `gh auth login`.
    "gh": {"credentials": True},
    # Who hears of a heal check its self-fix task could not repair (a heal block's own `escalate`
    # wins): "user" gets a high alert; "coordinator" gets one high observation per episode instead.
    # paused_quiet_s: how long an outage stays quiet under a pause the user did not set and whose reason
    # does not name the check, before the coordinator is asked once whether the pause is still right.
    # untenanted_hold_s: how long a check may report a device held with no tenant before it pages, even
    # under a known machine outage or a mute.
    "heal": {"escalate": "user", "paused_quiet_s": 21600, "untenanted_hold_s": 14400},
    # exclude_machines: [{alias, reason}], machines health coverage counts as opted out, named with the reason
    # (responsibilities.excluded_machines).
    "health": {"exclude_machines": []},
    # A daemon that is alive but completes no tick (heartbeat untouched) for hang_s (never below 600) is
    # dumped and restarted by ttp.watchdog; each restart within window_s doubles the wait before the next,
    # and the `restarts`-th in that window sends one high alert. step_budget_s (unset: daemon.STEP_BUDGET_S,
    # 30): the time each tick step may run; past it a step stops at its next item, a few seconds later
    # wherever it is (a child it waits on killed whole), and resumes next tick (ttp.stepclock).
    "watchdog": {"hang_s": 900, "restarts": 3, "window_s": 21600},
    # A newer tt-project installed with `ttp setup` is merged into this project's own harness by its
    # daemon (`ttp upgrade`, once per release, never inside a push step; pushes hold it push_hold_min at
    # most, then it starts between their steps), which then restarts keeping workers.
    # A clean merge whose runtime compiles and imports lands with no model task. A conflict or a failed
    # check queues one harness task at once, never on a timer: one at most is open, and a newer release
    # retargets it. One that ended without landing holds off the next only until a newer release or a
    # change to the project's own runtime or template files.
    "upgrade": {"auto": True, "push_hold_min": 30},
    # Upstream notes (hand-off follow-ups titled `upstream: ...`) are filed in the user's inbox
    # ~/.tt-project/upstream.jsonl. With ingest on, this project's coordinator gets them as events.
    "upstream": {"ingest": False},
    # A wait on another project's reply must be model-free (ackwait.py): `ttp ack-wait probe`, the
    # project's own `<harness>/.../ack_wait.py probe`, or a command listed here (matched on its leading words).
    "ack_wait": {"probe_commands": []},
    # The project's own ask_user refusal rules, on top of the built-in gate (coordinator.ask_gate_rules):
    # each {label, all_of: [regex...], unless, pass_blocking: [...], override_field, reason}. An ask is
    # refused when every all_of regex matches its text, recommendation and least_disruptive.
    "ask_gate": {"refuse": []},
    # The project's own model-free checks on each worker's hand-off (handoffcheck.py), run before it is
    # accepted, its reply posts or its review or push is queued: each {name, run, timeout_s (60), kinds}.
    # `run` runs from the project root with the hand-off in TTP_RESULT_JSON and `{result}`; exit 0
    # passes, 1 sends the run back with the output (handoff_check_max_sendbacks times, then the task
    # goes to review), anything else is a broken check: accepted, the coordinator told once.
    "worker": {"handoff_checks": [], "handoff_check_max_sendbacks": 2},
    # The project's own tool guards (localguard.py): Python files, by path from the harness folder and
    # outside runtime/ and prompts/, each with refusal(cmd) (and optionally tool_refusal(tool, input)),
    # asked after the built-in guards on every worker and coordinator tool call. Never edit runtime/ files.
    # Set as guards.local, in the one "guards" section below (a second "guards" key would replace it).
    # A running worker that has not read a coordinator update (steer.md) for ack_after_s raises one
    # steer_unread event to the coordinator (steerack.py); 0: off.
    "steer": {"ack_after_s": 300},
    # The project's own host-reaching commands (a script in its harness bin/ that wraps ssh), which a
    # fenced Claude worker runs outside the Bash sandbox as a simple command, as it runs ssh (fencefree.py):
    # each a name in bin/ or {command, env: [VAR, ...]} (also frees `VAR=... name ...`).
    # checks: run check and test commands in their own PID namespace (sandbox.py); off by default.
    "sandbox": {"fence_freed_commands": [], "checks": False},
    # Paths no run writes (pathguard): added to its built-in credential folders, never replacing them;
    # also an account setting (ttp config --account), which every project on the host adds.
    # user_site: "auto" refuses installs into the shared ~/.local while `ttp list` shows several projects
    # on the host, "on" always (usersite.py).
    "guards": {"local": [], "protected_paths": [], "user_site": "auto"},
    # Worker and reviewer runs, everything they start (detached checks, tests, reference models) and
    # the pushes and push batches the daemon starts run this many nice levels below the daemon
    # (0-19; 0 = normal priority), so they never slow the host's own work. The daemon and the
    # coordinator's turns keep normal priority.
    # prevent_idle_sleep (auto/on/off): on macOS, while the project has work and the machine is on AC
    # power, the daemon keeps the machine from idle and maintenance sleep (`caffeinate -i -s`; a closed lid
    # without an external display, or a sleep the user asks for, still sleeps it). auto = on for
    # workstations (a Mac is one); other platforms never. off (the default): never held; the user keeps
    # the machine awake themselves as needed.
    # pin_verify: strict refuses an explicit task model/effort pin that cannot be verified before a turn.
    "runner": {"nice": 10, "prevent_idle_sleep": "off", "pin_verify": "lenient"},
    # One limit on concurrent workers across all projects on this machine (machinecap.py), cut only when
    # the machine is truly short: sustained low CPU idle, memory pressure or swapping, a call with low
    # CPU idle, heat. worker_limit: auto (on for laptops and workstations: macOS or a battery present;
    # off on lab and build servers), on, off. max_workers: the base before pacing; null holds nothing
    # while nothing is short and is the cores under pressure. pace_from: moderate, or heavy (only heavy
    # CPU or memory cut the limit, and this project holds to its own base, not the lowest another set).
    # budget.max_parallel_workers stays each project's own ceiling.
    "machine": {"worker_limit": "auto", "max_workers": None, "pace_from": "moderate"},
    # reuse: every ssh the project starts (probes, checks, workers, git over ssh) shares one connection per
    # host for 10 minutes (OpenSSH ControlMaster through the harness's bin/ssh; sockets in ~/.tt-project/cm),
    # so an ssh agent signs once, not per command. Off: each ssh opens its own connection (sshmux.py).
    # reach_probe_s: each listed machine gets a harmless `ssh <alias> true` at most this often (reach.py);
    # reach_fresh_min: a "can't reach <machine>" claim needs a probe this recent, else it is marked.
    "ssh": {"reuse": True, "reach_probe_s": 1200, "reach_fresh_min": 30},
    # `ttp bench run` (benchrun.py): arms {name: {provider, model, effort}} (none shipped) and the
    # limits every cell gets alike.
    "bench": {"arms": {}, "reps": 1, "timeout_s": 3600, "stall_s": 900, "budget_usd": 8.0, "test_timeout_s": 900,
              "scratch_home": True},
    # The machine's shared daily chat model bench (benchowner.py): one project owns it and runs it at
    # bench_at home time; poke_after_h without a result pokes the owner once, take_over_after_h hands it
    # to the next project by name. opt_out: this project never owns it.
    "model_bench": {"opt_out": False, "poke_after_h": 48, "take_over_after_h": 72, "bench_at": "02:00"},
    # Once a day the daemon archives (to memory/archive/, one harness commit, one low feed line) the
    # decision and fact entries, never standing ones, that every prompt has left out for at least
    # archive_hidden_days days (0 = off). `ttp memory <name> --search <words>` still finds them.
    "memory": {"archive_hidden_days": 7},
    # Standby snapshots (replica.py), off until failover.standby names a machine alias or an absolute
    # directory: every snapshot_every_s seconds (and soon after a task finishes) the daemon ships a
    # consistent copy of the database, harness and open tasks' branches there, keeping snapshot_keep.
    # The project lease across hosts (lease.py; keys in EXTRA_KEYS) is off, a single host, while
    # failover.lease_remote is unset.
    "failover": {"snapshot_every_s": 900, "snapshot_keep": 3},
    # A new code task whose spec names files another open or unlanded code task adds or changes gets a
    # warning at task_add, and its worker the list of those branches (overlap.py). Never counted: the
    # `ignore` globs, hubs (tracked files over hub_bytes), folders and the files delivery.push_checks
    # names; done tasks that finished over branch_days days ago. A shared file alone never chains a task;
    # a settled area (settled.py) without an expiry of its own counts for settled_days.
    "overlap": {"enabled": True, "hub_bytes": 150000, "branch_days": 14, "settled_days": 14,
                "ignore": ["CHANGELOG*", ".changeset/*", "changesets/*", "plugin.json", "marketplace.json",
                           "package.json", "package-lock.json", "__init__.py"]},
    # The daily waste ledger (waste.py, `ttp waste`): a project whose avoidable share passes target_share
    # gets one fix task for its largest bucket (fix_task); wake_usd, max_runs and review_rounds are the
    # limits past which a coordinator wake, a task's runs and a change's review rounds count as avoidable.
    # moved_at (epoch, no default): runs started after it count as a stand-in's after a project move.
    "waste": {"target_share": 0.10, "wake_usd": 1.0, "max_runs": 4, "review_rounds": 2, "fix_task": True},
    # Batch runs (batchrun.py): a code task's run also takes up to batch_max small ready code tasks.
    "dispatch": {"batch": True, "batch_max": 5},
}


# project.json keys the runtime reads beyond DEFAULT_CONFIG: identity written at creation, open
# sections, and settings that have no default. A key in neither is reported as unknown.
META_KEYS = {"name", "id", "host", "root", "created", "tt_project_version", "pricing", "restrictions",
             "home_timezone", "home_timezone_from"}
EXTRA_KEYS = {
    "waste": {"moved_at"},
    "budget": {"max_waits", "hourly_floor_usd", "estimate_weights", "hourly_waste_usd", "hourly_waste_runs",
               "hourly_coordinator_turns", "hourly_coordinator_repeats", "max_pace_hold_s",
               "cap_yellow_pct"},   # max_pace_hold_s: deprecated and ignored; accepted so old configs stay quiet
    # blocker_sweep_h, blocker_sweep_min_gap_h, review_backlog_h: the blocker sweep's timing (sweep.py);
    # blocker_sweep_recheck_skip_h: how soon after a model sweep a due re-check may skip model-free (sweepskip.py; 6).
    # user_pause_marker: regex for the marker file a user-pause probe tests for (userpause.py; default `user-*`).
    # promise_default_h: when a chat promise that names no time is due (obligations.py; default 6).
    # mitigation_due_h: when a live-impact mitigation is due after it is filed (mitigate.py; default 4, 0 off).
    # course_check: thresholds of the digest's Course check ({"fails_24h", "queued_h", "spend_ratio", ...}; course.py;
    #   self-review: "missed_deadlines", "same_wait", "self_races", "rule_clash", "recurring", "old_orders";
    #   coursecheck.py;
    #   "long_promise_h": a promise on urgent work due later than this is flagged, eta.py, default 2).
    # progress_check: thresholds of the progress and stall check ({"task_h", "probe_n", "repeat_n", ...}; progress.py).
    # probe_stall_dispose_h: when the blocker sweep settles a probe stall model-free (sweep.py; default 1, 0 off).
    # echo_paths: more files or folders (relative to the project root) the retired-rule echo scan reads,
    # such as a repo's recovery scripts (echoes.py; default none).
    # agent_orders_as_user: the owner's own chat agent's relayed context and findings count as the owner's
    # word (relay.agent_orders; default off, set only on the owner's own message).
    # owner_identities: names, handles or emails the owner writes external records under (tickets, chat
    # comments), read with the project root's git user.name and user.email to verify a cited record's
    # author (restriction_source.owner_identities; default none, set only on the owner's own message).
    # session: the persistent coordinator, always on and with no off switch (coordtools.py,
    # hostcheck.py): "wake_budget_usd" and "wake_budget_s" cap one wake (default: twice the session's dearest
    # recent ok resumed wake, at least the turn's sized budget, within the dollar caps (seedcap.wake_usd);
    # and the turn's timeout), "host_access" ({alias: helper argv in the project folder | ssh config file
    # there}, default none) and "log_paths" ({name: absolute path} `ttp hostcheck log-tail` may read).
    # "tools" ("full" default | "read_only") and "tool_timeout_s" (300, at most 600) set a session
    # wake's tools and its foreground command limit.
    # status_footer: onhost.guard appends a "Status now:" line with the claimed tasks' real status to a message
    # that says unstarted work is under way (default true); false marks the claim in place only and leaves
    # the status to the coordinator's note, for chats that take no status footers.
    # drift: thresholds of the running-run drift flags (drift.py; each 0: off): repeat_fail 3, loop 4,
    # outside_spec 3, quiet_s 900, pace 1.5, and raise_s 600 before a still-flagged run reaches the coordinator.
    "coordinator": {"session", "status_footer", "agent_orders_as_user", "owner_identities", "max_review_tasks_per_day", "charter_approval_days", "approval_lookback_days", "reask_window_days", "blocker_sweep_h", "blocker_sweep_min_gap_h",
                    "blocker_sweep_recheck_skip_h", "review_backlog_h", "user_pause_marker", "promise_default_h", "mitigation_due_h", "course_check",
                    "progress_check", "probe_stall_dispose_h", "echo_paths", "drift",
                    "reply_terms", "reply_max_terms", "reply_soft_chars"},   # plain-reply check (plainreply.py)
    # hold_s: how long a worker's normal notify from a blocked, waiting, failed or needs_review run waits
    # for the coordinator before it is posted unchanged (workernotify.py; default 900, 0 posts at run end).
    "notify": {"slack_poll_s", "hold_s"},
    # stale_lock_s: see "harness_git" in DEFAULT_CONFIG; unset so that the old watchdog key below still counts.
    # root_ref_lock_s: the age (unset: 3600 s; 0: off) past which a ref lock in the project root's repository
    # that nothing may own is moved aside by the daemon's harness sweep (gitlocks.clear_root_refs).
    "harness_git": {"stale_lock_s", "root_ref_lock_s"},
    # stale_git_lock_s: deprecated alias of harness_git.stale_lock_s, read only when that is unset.
    # step_budget_s: seconds each daemon tick step may run (unset: daemon.STEP_BUDGET_S; Daemon._step_budget).
    "watchdog": {"stale_git_lock_s", "step_budget_s"},
    # push_queue..after_push_timeout_s: the daemon-owned push queue (see PUSH_QUEUE_DEFAULTS).
    # backup_remote: a git remote finished code tasks' branches are pushed to, fast-forward only (off: unset).
    # fast_forward_also: more branches each push moves to the pushed commit, fast-forward only (off: unset).
    # mirror_remotes: {remote, branch} tables each push also sends the pushed commit to, fast-forward only.
    # allow_protected_push_branch: lets the push branch be main, master or the remote's default (off: unset).
    # push_checks_by_repo: a check list per repo ("harness", a repo path or folder name), push.repo_checks.
    # own_push_remote: where `ttp push --own` publishes task branches (a remote's name, a URL, or a map
    # keyed by repo path or folder name); unset: the push branch's remote, else origin (push.own_push_remote).
    # own_push_reserved_lane: `ttp push --own` checks take the remote's own-branch push lane (push.own_lane_key;
    # default on, false: an ordinary turn behind task checks).
    # deploy_restart_wait_s: how long `ttp deployed --after-push` lets a daemon restart onto new files (deploy.py).
    # plugin_version_guard: refuse a change that edits plugins/<name>/ but keeps the tip's manifest version (off).
    # push_solo_paths: pathspecs a batch takes at most one routine change touching (pushq.SOLO_PATHS, off); push_cap_tests:
    # the name regex of prompt size cap tests, whose lone failure sends a trim-to-fit fix (conflictfix.CAP_TESTS).
    # push_entry_checks: quick commands (seconds) a batch runs on each entry merged onto its head before
    # it joins the batch, whatever the entry's own scope; one failing sends that entry back alone (batch._entry_check).
    # push_rerun_s: the time limit of one rerun of a failed push check's failing tests (batch.RERUN_S).
    # push_hard_tests: the regex of failing test ids a batch never excuses as the tip's own (batch.HARD_TESTS, the leak scan).
    # mechanical_merge_paths: globs of shared docs whose push-rebase conflicts settle by keeping both sides'
    # lines, carrying the approval (batch.settle_union; default batch.MECHANICAL_PATHS, empty: off).
    # landed_probe: with no push_branch, the read-only command (`{id}`: the task id) that answers
    # `landed:#<id>` (landed.landed_probe); unset there, the harness chains with depends_on instead.
    # push_checks_timeout_s: the run-time limit of each `ttp push` check, grant and test-pool waits left out (push.check_limit_of; 0: none).
    # push_pool_wait_s: how long one push check may wait for test-pool slots in all (push.pool_wait_of; 0: no bound).
    # max_open_branches, max_work_threads: soft caps on open code branches (task branches with commits not on
    # the push branch) and running workers; over one, the Course check and `ttp status` raise one alarm line
    # (branchcap.CAPS defaults, 0: no cap). delete_landed_branches (default on): the daemon deletes a landed
    # task's local branch once its worktree is gone, its tip saved as refs/ttp-archive/<branch> and
    # recorded in state/branch-deletions.jsonl. dead_branch_days (branchcap.DEAD_DAYS, 0: off): the same for
    # a branch whose tasks all ended that long ago, never approved and needed by no unfinished task.
    # push_retries: how often a detached push retries a cause it clears itself, a non-fast-forward
    # (--own) or a reboot that cut it short (push.retries_of; default 2, 0: never).
    "delivery": {"base_ref", "push_branch", "landed_probe", "push_checks", "post_land_checks", "push_checks_by_repo", "push_exclude_paths", "push_rounds", "push_wait_s",
                 "push_checks_timeout_s", "push_pool_wait_s", "push_retries",
                 "version_bump", "push_queue", "push_batch_s", "push_batch_max", "push_min_gap_s", "push_age_s", "push_landing_alert_h", "push_script_torn_escalate_after",
                 "push_setup_fault_escalate_after", "after_push",
                 "push_solo_paths", "push_cap_tests", "push_entry_checks", "push_rerun_s", "push_hard_tests", "mechanical_merge_paths",
                 "after_push_timeout_s", "backup_remote", "fast_forward_also", "mirror_remotes",
                 "allow_protected_push_branch", "own_push", "own_push_remote", "own_push_reserved_lane", "deploy_restart_wait_s", "plugin_version_guard",
                 "checks_fast_globs", "checks_fast_exclude",   # these two: `ttp checks` fast path (push.FAST_GLOBS),
                 "checks_targeted", "checks_targeted_always",   # a task's `ttp checks` runs the tests its diff reaches (testpick)
                 "task_checks",   # a task's own `ttp checks` list (testpick.task_set); unset: the push checks
                 "checks_task_full",   # the user's own: a task's `ttp checks --full` runs with the push queue on (testpick.full_refusal)
                 "checks_host",   # where checks run (remotecheck)
                 "gh_route",   # the ssh alias gh calls run on when this host has no gh login (ghroute)
                 "checks_spill_hosts", "checks_spill_exclude", "checks_spill_after_s", "checks_spill_first", "checks_remote_only",   # where busy checks may spill to (checkspill)
                 "check_prestage",   # a short foreground run before `ttp checks --detach` (checkprestage)
                 "pr_body_check",   # a project's own PR body check before gh pr create/edit (prbody)
                 "ai_tag",   # the AI-authorship tag a post to others ends with (aitag)
                 "max_open_branches", "max_work_threads", "delete_landed_branches", "dead_branch_days",   # open-branch soft cap (branchcap)
                 "rebase_replace", "never_force",   # whether a rebase replaces its branch (rebase_replace)
                 "integration_branch",   # where reviewed changes land on a project with no push branch (integrate)
                 "channel_tags",   # release-channel tags a forward move may update under never_force (channeltag)
                 "express", "express_max_lines", "express_max_files"},   # `ttp express`: one small urgent head lands alone (express)
    "jev": {"via", "url", "model"},
    "effort_ab": {"winner", "decided_at"},   # the end rule's result, written by effortab.conclude
    # launcher_stops: {glob of a project launcher's job folders: stop command} `ttp detach --stop <job
    # folder>` runs when the job has no detached.json entry and no pid file (launcherstop.registered_stop).
    "detach": {"launcher_stops"},
    "power": {"keep_awake"},
    # device_timeout_max_s: a ceiling on device-job timeouts (off: unset); see device_timeout_max.
    # host_commands, host_command_timeout_s: what `ttp host-run` may run outside a worker's sandbox and
    # for how long (hostrun.py; none, 120 s).
    "runner": {"device_timeout_max_s", "host_commands", "host_command_timeout_s"},
    # forbidden_patterns, forbidden_patterns_file: text memory and charter writes refuse (forbidden.py).
    "memory": {"forbidden_patterns", "forbidden_patterns_file"},
    # outage_owner_budget_min (60), outage_task_due_min (30), outage_escalate_min (30): when a shared
    # machine's outage becomes a tracked task and when that task escalates (outagetask.py).
    "heal": {"outage_owner_budget_min", "outage_task_due_min", "outage_escalate_min"},
    # hold_mute_escalate_h: the longest a mute covering a device hold stays silent (screen.hold_cap; 1).
    # throughput_mute_max_h: the longest a throughput-class mute lasts before renewal (throughmute.max_h; 12).
    "screen": {"hold_mute_escalate_h", "throughput_mute_max_h"},
    # standby: where state snapshots go (off: unset); fleet: the identity projects driving one fleet share
    # (also the shared lease identity). The project lease (lease.py), off while lease_remote is unset:
    # lease_remote, a git remote (URL or a code-repo remote name) for refs/ttp/lease/<identity>, never the
    # plugin's own repository and not the delivery remote unless allow_delivery_remote; lease_ttl_s (300).
    "failover": {"standby", "fleet", "lease_remote", "lease_ttl_s", "allow_delivery_remote"},
}
OPEN_SECTIONS = {"resources", "resources_tasks"}           # any name below is fine
# write_fence: true runs the provider's workers in its optional write fence (Claude's Bash sandbox, Codex's
# workspace-write, Provider.fence_args) where the host can run it; off by default (providers.fence_on).
# loopback_ports: extra host loopback ports fenced workers reach (loopproxy.ports_env), or false for none.
# write_dirs: extra directories inside the project root fenced workers may write (write_dirs below).
PROVIDER_KEYS = {"billing", "tiers", "plugin_dirs", "worker_isolation", "write_fence", "mcp_servers", "mcp_withheld",
                 "disallowed_tools", "loopback_ports", "write_dirs",
                 # connect_timeout_ms: how long a run that keeps the account connectors waits for them at
                 # start (Claude's MCP_CONNECT_TIMEOUT_MS; Daemon._with_connector_wait).
                 "connect_timeout_ms",
                 # providers.chat only (chatbridge.py): the `ttp secret endpoint` alias holding the key,
                 # the base URL saved with it, a max_tokens cap and the model's context_window (also per tier).
                 # route_cooldown_s: how long a tier routed to it (tiers.<tier>.route, route.py) runs on the
                 # core provider after a fallback. effort_fields: per model, the run's effort to extra request
                 # fields (chatbridge.effort_fields). route_wait_s: how long a routed start waits for a full
                 # slot before it starts on the core provider (route.wait_s). routes: the ordered rule list
                 # routing runs by kind, tier and label (route.match). model_ranking: its model ids,
                 # smartest first, that stand in for a model that is down (modelrank.py). models_every_s:
                 # seconds between the model-free /models checks (route.models_every). demote: the live
                 # demotion detector's thresholds (modeldemote.DEFAULTS).
                 "endpoint", "base_url", "max_tokens", "context_window", "route_cooldown_s", "effort_fields",
                 "route_wait_s", "routes", "model_ranking", "models_every_s", "demote",
                 # providers.chat only: the rules-first model pick (modelpick.py); its keys (enabled, min_p,
                 # options) are checked by modelpick.config_problems.
                 "dispatch"}


def write_dirs(root: Path, guard: Path, value: Any) -> tuple[list[str], list[str], list[str]]:
    """providers.<name>.write_dirs: (paths granted, "entry: why" refused, "entry: why" skipped as missing).
    Each entry is relative to the project `root` or absolute inside it, symlinks resolved. Refused: one
    outside the root, the root itself, or one inside or holding `guard` (the tt-project folder with the
    charter, memory, config and state). A missing directory is skipped, never created."""
    granted: list[str] = []
    refused: list[str] = []
    missing: list[str] = []
    if value in (None, [], ""):
        return granted, refused, missing
    if not isinstance(value, list):
        return granted, [f"{json.dumps(value)[:60]}: not a list of paths"], missing
    root, guard = root.resolve(), guard.resolve()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            refused.append(f"{json.dumps(entry)[:60]}: not a path")
            continue
        given = Path(os.path.expanduser(entry.strip()))
        given = given if given.is_absolute() else root / given
        real = given.resolve()
        if real == root:
            refused.append(f"{entry}: the project root itself (that would turn the write fence off)")
        elif root not in real.parents:
            refused.append(f"{entry}: resolves to {real}, outside the project root {root}")
        elif real == guard or guard in real.parents or real in guard.parents:
            refused.append(f"{entry}: covers the project's charter, memory, config or state ({guard})")
        elif not real.is_dir():
            missing.append(f"{entry}: {real} is not an existing directory")
        else:
            granted += [str(real)] if str(real) not in granted else []   # never the link: it may be retargeted
    return granted, refused, missing


def _known_keys(path: list[str]) -> set[str] | None:
    """The keys allowed at `path` in project.json, or None when anything goes there."""
    if not path:
        return set(DEFAULT_CONFIG) | META_KEYS | {"checks"}
    if path[0] in OPEN_SECTIONS or path[0] in META_KEYS:
        return None
    if path[0] == "providers":
        return {"claude", "codex", "cursor", "fake", "chat"} if len(path) == 1 else PROVIDER_KEYS if len(path) == 2 else None
    if path == ["checks"]:
        # wrap_cmd: the project's own lock or queue command the commands after `ttp checks --` run
        # behind (push.wrap_of; default "", none); delivery.push_checks never go through it. A map
        # {"cmd": ..., "repos": [repo keys]} scopes it to those repositories; others run unwrapped.
        # wrap_lane: where those runs take its lane, before the wrapper starts or after (push.wrap_lane_of).
        # no_device_cmds: commands that need no device, run without wrap_cmd (checklane.no_device).
        # fallback_lane: where those run while wrap_cmd's lock is paused, a wrapper or "unwrapped" (push.fallback_lane_of;
        # default "", off: they wait for the pause).
        # slots, light_cmds, max_wait_s, wait_event_s, wait_escalate_s, wait_hang_factor, priority_age_s,
        # test_project_max, done_holder_flag_s, done_holder_grace_s: the checks lanes (checklane);
        # test_project_slots: the project's test-worker budget (testslots.project_max); test_project_workers:
        # its ceiling on test workers over all its suites (testslots.project_workers); test_run_max: one
        # suite's most workers, only lowering the account's (testslots.sizes); test_run_min: a full run's
        # fewest workers (testslots.run_min);
        # stall_s: a running check's log silence that makes it stalled (checkstall.stall_s);
        # push_load_wait_s: push checks' longest wait under the load guard (testslots.push_load_wait_s).
        # wrap_needs_device: false says the commands behind wrap_cmd need no device, so they may spill (checkspill).
        # spill_wrap_cmd: the wrapper a check runs behind on a spill host (push.spill_wrap_of; default "", none).
        # spill_beside_device: spill hosts a device job keeps no spill off (checkspill.beside_device; default none).
        # spill_quarantine_after: strikes in a row that quarantine a spill host (checkspill.strike_settle; default 3).
        # python_lock: the pinned requirements file the checks' Python env is built from, relative to the
        # checkout (runnerenv.lock_of; default "", the plugin's own lock of pytest and pytest-xdist).
        # spill_pre_cmd: the project's own read-only go/skip command before a spill to a host (checkspill.pre_check;
        # default none).
        # refuse_sudo: true refuses sudo in the project's test suites (sudowatch; default false: recorded only).
        return {"refuse_sudo", "nice", "python_lock", "ionice", "wall_cap_s", "wrap_cmd", "wrap_lane", "wrap_needs_device", "spill_wrap_cmd",
                "spill_beside_device", "spill_pre_cmd", "spill_quarantine_after", "fallback_lane", "no_device_cmds", "slots", "light_cmds", "max_wait_s", "wait_event_s",
                "wait_escalate_s", "wait_hang_factor", "priority_age_s", "test_project_max", "test_project_slots",
                "test_project_workers", "test_run_max", "test_run_min",
                "done_holder_flag_s", "done_holder_grace_s", "stall_s", "push_load_wait_s",
                "smoke_max_s"}   # the landing check time limit (gatetime.max_s; default 300 s, 0: off)
    if len(path) == 1 and isinstance(DEFAULT_CONFIG.get(path[0]), dict):
        return set(DEFAULT_CONFIG[path[0]]) | EXTRA_KEYS.get(path[0], set())
    return None


def _is_section(parts: list[str]) -> bool:
    """Whether project.json's `parts` holds an object of settings: a top-level section (budget, checks,
    delivery, ...) or one provider's entry (providers.<name>)."""
    if len(parts) == 1:
        return isinstance(DEFAULT_CONFIG.get(parts[0]), dict) or parts[0] in EXTRA_KEYS or parts[0] == "checks"
    return len(parts) == 2 and parts[0] == "providers"


def section_value_problem(dotted: str, value: Any) -> str | None:
    """Why `value` cannot be saved at `dotted`: a section of settings given one plain value (`ttp config
    checks host_max` once saved "checks": "host_max", and every worker start then failed). None when fine."""
    if value is None or isinstance(value, dict) or not _is_section(dotted.split(".")):
        return None
    hint = (" (host-wide checks settings are account-level: ttp config --account checks.host_max N)"
            if dotted == "checks" else "")
    return (f"{dotted} is a section of settings, not one value ({json.dumps(value)[:60]}): set one key in it, "
            f"ttp config {dotted}.<key> VALUE{hint}")


# Provider keys with no built-in default whose value is still of one JSON type (providers.<name>.<key>).
PROVIDER_KEY_TYPES: dict[str, type] = {"write_dirs": list}

# List- or object-typed keys whose readers also take a plain form ({dotted key: forms}), each form as its
# reader reads it: "number" a non-negative number for every tier, "false" off, "names" a comma-separated
# string, "string" one string, "floor" a string tierfloor.floor_of reads. Nothing else is taken.
KEY_EXTRA_FORMS: dict[str, tuple[str, ...]] = {
    "budget.compact_window_tokens": ("number",),   # daemon worker start: one window, 0 off
    "budget.split_reread_tokens": ("number",),     # daemon split check: one split line, 0 off
    "budget.resume_lost": ("false",),              # daemon lost-run resume: not an object, starts fresh
    "waiting.wake_gate": ("false",),               # waitgate.settings: false, every wake starts a turn
    "coordinator.conclusion_floor": ("floor",),    # tierfloor.floor: 'standard/high', 'off'
    "providers_allowed": ("names",),               # provallow.names: 'claude,codex'
    "coordinator.provider_failover_to": ("string",),   # failover.targets: one provider name
    "ack_wait.probe_commands": ("string",),        # ackwait.probe_commands: one command
}
FORM_NAMES = {"number": "a non-negative number", "false": "false (off)",
              "names": "a comma-separated string", "string": "a string",
              "floor": "a tier and effort such as 'standard/high', or 'off'"}


def _extra_form(form: str, value: Any) -> bool:
    if form == "false":
        return value is False
    if form in ("names", "string"):
        return isinstance(value, str)
    if form == "floor":
        from .tierfloor import floor_of
        try:
            return isinstance(value, str) and bool(floor_of(value))
        except ValueError:
            return False
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and value >= 0)


def key_type_problem(dotted: str, value: Any) -> str | None:
    """Why `value` cannot be saved at `dotted`: a key whose default is a list or an object (or a provider
    key in PROVIDER_KEY_TYPES) given another JSON type, e.g. "" turning sandbox.fence_freed_commands
    into a string, other than a plain form its reader takes (KEY_EXTRA_FORMS). Sections are
    section_value_problem's. None when fine or the key has no such type. Project.set_config applies it,
    so `ttp config`, the coordinator's config_set and the web app refuse alike."""
    parts = dotted.split(".")
    if _is_section(parts):
        return None
    want = PROVIDER_KEY_TYPES.get(parts[2]) if len(parts) == 3 and parts[0] == "providers" else None
    if want is None:
        node: Any = DEFAULT_CONFIG
        for part in parts:
            node = node.get(part) if isinstance(node, dict) else None
        want = type(node) if isinstance(node, (list, dict)) else None
    extra = KEY_EXTRA_FORMS.get(dotted, ())
    if want is None or isinstance(value, want) or any(_extra_form(f, value) for f in extra):
        return None
    name = "a JSON list" if want is list else "a JSON object"
    names = [f"{name} ({'[...]' if want is list else '{...}'})", *(FORM_NAMES[f] for f in extra)]
    need = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return (f"{dotted} needs {need}, not {json.dumps(value)[:60]}; "
            f"nothing was written ('' removes it, back to its default)")


def bad_sections(raw: dict) -> dict[str, Any]:
    """The sections of a project's own settings that hold no object ({dotted key: its value}): read as
    absent (layered), never kept as the last good copy, and named by config_problems and the daemon."""
    if not isinstance(raw, dict):
        return {}
    out = {k: v for k, v in raw.items() if v is not None and not isinstance(v, dict) and _is_section([k])}
    prov = raw.get("providers")
    if isinstance(prov, dict):
        out.update({f"providers.{k}": v for k, v in prov.items() if v is not None and not isinstance(v, dict)})
    return out


def _without_bad_sections(raw: dict) -> dict:
    bad = bad_sections(raw)
    if not bad:
        return raw
    out = {k: v for k, v in raw.items() if k not in bad}
    if any(k.startswith("providers.") for k in bad):
        out["providers"] = {k: v for k, v in raw["providers"].items() if f"providers.{k}" not in bad}
    return out


# Keys an older plugin version read and this one no longer does, with why each went. `ttp config` refuses
# to set them (unknown_key_hint); `ttp upgrade` drops them from project.json (cli._drop_retired_keys), and
# `ttp config NAME KEY ''` removes any unknown key present.
RETIRED_KEYS = {
    "coordinator.session.mode": "the coordinator session is always on, with no off switch",
    "coordinator.session.pause_h": "the session spend guard is gone: the dollar caps are the cost limits",
    "budget.hourly_coordinator_usd": "the coordinator's hourly dollar guard is gone: the dollar caps are the cost limits",
    "upgrade.merge_retry_h": "a conflicting upgrade queues its merge task at once, never on a timer",
    "upgrade.task_every_h": "a conflicting upgrade queues its merge task at once, never on a timer",
    "coordinator.unblock_effort": "every coordinator runs at one effort level, coordinator.effort",
}


def unknown_key_hint(dotted: str) -> str | None:
    """'unknown key ... (did you mean ...?)' when no part of the runtime reads `dotted`, else None."""
    if dotted in RETIRED_KEYS:
        return f"{dotted} is retired: {RETIRED_KEYS[dotted]}"
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        known = _known_keys(parts[:i])
        if known is None:
            return None
        if part not in known:
            near = difflib.get_close_matches(part, sorted(known), n=1, cutoff=0.6)
            guess = ".".join(parts[:i] + near[:1])
            return f"unknown key {dotted}" + (f" (did you mean {guess}?)" if near else "")
    return None


def disk_resume_gb(disk: dict) -> float | None:
    """The disk guard's resume point in GB from a `disk` config section: resume_free_gb as set, or None
    (unset or not a number) for the default rule. The daemon raises it to the guard's threshold."""
    v = disk.get("resume_free_gb")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return float(v)


def _disk_problems(disk: Any) -> list[str]:
    if not isinstance(disk, dict) or disk.get("resume_free_gb") is None:
        return []
    v, low = disk["resume_free_gb"], disk.get("min_free_gb", DEFAULT_CONFIG["disk"]["min_free_gb"])
    if disk_resume_gb(disk) is None:
        return [f"disk.resume_free_gb: {v!r} is not a number of GB; the guard resumes at 1.2 times its threshold"]
    if isinstance(low, (int, float)) and v < low:
        return [f"disk.resume_free_gb: {v:g} is below disk.min_free_gb {low:g}; the guard's threshold is used "
                f"instead when it is higher"]
    return []


def code_tasks_may_push(cfg: dict) -> bool:
    """Whether code tasks may land their own work with `ttp push`: delivery.code_tasks_may_push is
    true and delivery.push_branch is set."""
    d = cfg.get("delivery") or {}
    return d.get("code_tasks_may_push") is True and bool(str(d.get("push_branch") or "").strip())


APPROVED_PUSH = "approved"   # delivery.code_tasks_may_push: only heads an independent review approved


def approved_heads_may_push(cfg: dict) -> bool:
    """Whether a code task or the coordinator may land a head an independent review approved with
    `ttp push` (push.approved_head): delivery.code_tasks_may_push is true or "approved" (the default)
    and delivery.push_branch is set."""
    d = cfg.get("delivery") or {}
    v = d.get("code_tasks_may_push", APPROVED_PUSH)
    return (v is True or str(v).strip().lower() == APPROVED_PUSH) and bool(str(d.get("push_branch") or "").strip())


# delivery.push_queue on: reviews approve into the daemon's queue, which pushes approved heads in
# batches (a batch starts at once when no review is running or ready, else once the oldest approval
# waited push_batch_s, push_batch_max are waiting or approvals waited out the last batch; after a batch
# that did not end clean, not before push_min_gap_s passed since it ended unless it is full, priority
# 1-2 or they waited it out; a batch, one train, takes every approval due, push_batch_max caps none)
# push_age_s: an approval sorts one priority better per this many seconds waited, never past priority 2
# (pushq._approved; 0 = off), so a stream of priority 1-2 approvals cannot starve a routine one.
# and then runs after_push (commands like push_checks), killed after after_push_timeout_s.
# push_landing_alert_h: approvals waiting this long with nothing landed raise a high alert (0 = off).
# push_script_torn_escalate_after: batches held script_torn on one check this many times in a row raise a
# high alert (pushq._held_told; 0 = off); push_setup_fault_escalate_after: the same for batches held
# setup_fault (a check that failed before any test ran, after its reruns).
PUSH_QUEUE_DEFAULTS = {"push_queue": False, "push_batch_s": 300, "push_batch_max": 8, "push_min_gap_s": 1800,
                       "push_age_s": 14400,
                       "push_landing_alert_h": 4, "push_script_torn_escalate_after": 3,
                       "push_setup_fault_escalate_after": 3,
                       "after_push_timeout_s": 1800, "deploy_restart_wait_s": 900}
# key: (minimum, whether the minimum itself is allowed, whole numbers only)
PUSH_QUEUE_NUMBERS = {"push_batch_s": (0, True, False), "push_batch_max": (1, True, True),
                      "push_min_gap_s": (0, True, False), "push_age_s": (0, True, False),
                      "push_landing_alert_h": (0, True, False),
                      "push_script_torn_escalate_after": (0, True, True),
                      "push_setup_fault_escalate_after": (0, True, True),
                      "after_push_timeout_s": (0, False, False), "deploy_restart_wait_s": (0, True, False)}


def push_queue_number(key: str, v: Any) -> float | int:
    """delivery.<key> (a PUSH_QUEUE_NUMBERS key) as a number in its range; ValueError otherwise."""
    lo, closed, whole = PUSH_QUEUE_NUMBERS[key]
    try:
        n = float(v) if not isinstance(v, bool) else None
    except (TypeError, ValueError):
        n = None
    if n is None or not abs(n) < float("inf") or (whole and n != int(n)) or n < lo or (n == lo and not closed):
        raise ValueError(f"delivery.{key}: {v!r} is not {'a whole number' if whole else 'a number'} "
                         f"{'>=' if closed else '>'} {lo} (default {PUSH_QUEUE_DEFAULTS[key]})")
    return int(n) if whole else n


def push_allowed(d: dict) -> bool:
    """delivery.push_allowed of the delivery settings `d` as `ttp push` reads it: unset allows."""
    v = d.get("push_allowed")
    return v is None or v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


# delivery.own_push: which remote branches other than delivery.push_branch `ttp push --own` (and any
# push that publishes as with --own: a notes branch, a pr_branch or rebases: task) may push to.
# "on" (set explicitly): the task's own or carried branch, created on the remote when new.
# "existing_only" (the default when unset): only onto a branch the remote already has; never creates
# one, except a code task's own draft-PR branch while unset (own_creates_pr_branch; push.own_route names
# that route when another task's branch is refused). An explicit "existing_only" stays strict. "off": none; the
# push branch, through `ttp push` or the push queue, is the only branch the project pushes, and a
# pr_branch or rebases: task lands there too. A project whose charter allows only one push branch
# sets "off". An unknown value reads as "off": a typo never widens what may be pushed.
OWN_PUSH_MODES = ("on", "existing_only", "off")


def own_push_mode(v: Any) -> str:
    """A delivery.own_push value as one of OWN_PUSH_MODES: unset is "existing_only" (a review once
    created remote branches a project's charter never allowed: creating one takes an explicit "on"),
    true/false are on/off, "existing"/"existing-only" are existing_only, anything else is "off"."""
    if v is None:
        return "existing_only"
    if v is True:
        return "on"
    if v is False:
        return "off"
    s = str(v).strip().lower().replace("-", "_")
    if s in ("on", "true", "yes", "1"):
        return "on"
    return "existing_only" if s in ("existing_only", "existing") else "off"


def own_push(cfg: dict) -> str:
    """The project's delivery.own_push mode (own_push_mode)."""
    return own_push_mode((cfg.get("delivery") or {}).get("own_push"))


def own_creates_pr_branch(cfg: dict) -> bool:
    """Whether a code task's `ttp push --own` may still create its own ttp/t<id>-... branch on the remote
    under the unset default (existing_only): that push is how a draft PR's branch is first published
    (kind-code.md), and nothing else could publish it. Only while delivery.own_push is unset and
    delivery.draft_prs is on; an explicit own_push value, or draft PRs off, keeps the mode strict."""
    d = cfg.get("delivery") or {}
    return d.get("own_push") is None and d.get("draft_prs", True) is not False


# delivery.rebase_replace: how a `rebases:<branch>` task delivers. "new_branch" (the default, also for
# an unknown value): onto its own new branch, never replacing the old one. "lease": `ttp push --own`
# replaces the old branch under --force-with-lease (push.publish), a force push a project opts into.
# delivery.never_force (default false): no run force-pushes at all (gitguard; publish refuses a lease).
REBASE_REPLACE_MODES = ("new_branch", "lease")


def rebase_replace_mode(v: Any) -> str:
    """A delivery.rebase_replace value as one of REBASE_REPLACE_MODES: unset or unknown is "new_branch"."""
    return "lease" if str(v or "").strip().lower() == "lease" else "new_branch"


def rebase_replace(cfg: dict) -> str:
    """The project's delivery.rebase_replace mode (rebase_replace_mode)."""
    return rebase_replace_mode((cfg.get("delivery") or {}).get("rebase_replace"))


def never_force(cfg: dict) -> bool:
    """Whether delivery.never_force is on: no run force-pushes, not even under a lease."""
    v = (cfg.get("delivery") or {}).get("never_force")
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def rebase_problems(delivery: dict) -> list[str]:
    """delivery.rebase_replace and delivery.never_force problems, one line each."""
    out = []
    mode = delivery.get("rebase_replace")
    if mode is not None and str(mode).strip().lower() not in REBASE_REPLACE_MODES:
        out.append(f"delivery.rebase_replace: {mode!r} is not one of {', '.join(REBASE_REPLACE_MODES)}; "
                   f"a rebase delivers on its own new branch")
    if not isinstance(delivery.get("never_force", False), bool):
        out.append(f"delivery.never_force: {delivery['never_force']!r} is not true or false")
    if rebase_replace_mode(mode) == "lease" and never_force({"delivery": delivery}):
        out.append("delivery.rebase_replace: lease replaces a branch with --force-with-lease, a force push, "
                   "and delivery.never_force is true; set rebase_replace to new_branch (no lease push is made)")
    return out


OWN_FLAG = re.compile(r"(?<![\w-])--own\b")


def own_free(text: str, mode: str) -> str:
    """`text` (a daemon-written spec part such as review.auto_notes), stripped, without its sentences
    that mention `--own` while own pushes are off (`mode`, own_push): a spec never asks for one then."""
    text = text.strip()
    if mode != "off" or not OWN_FLAG.search(text):
        return text
    parts = re.split(r"(?<=[.;!?])\s+|\n+", text)
    return " ".join(x for x in parts if x.strip() and not OWN_FLAG.search(x)).strip()


def push_queue_on(cfg: dict) -> bool:
    """Whether reviews approve into the daemon's push queue: delivery.push_queue is true, pushing is
    allowed and delivery.push_branch is set. The prompts and the daemon (pushq.enabled) both ask this."""
    d = cfg.get("delivery") or {}
    return d.get("push_queue") is True and push_allowed(d) and bool(str(d.get("push_branch") or "").strip())


def nice_level(runner: Any) -> tuple[int, str | None]:
    """runner.nice of a `runner` config section as a level 0-19, and a problem line when the value was
    clamped or replaced by the default."""
    default = DEFAULT_CONFIG["runner"]["nice"]
    v = runner.get("nice", default) if isinstance(runner, dict) else default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v != int(v):
        return default, f"runner.nice: {v!r} is not a whole number 0-19; {default} is used"
    if not 0 <= v <= 19:
        n = min(max(int(v), 0), 19)
        return n, f"runner.nice: {v!r} is outside 0-19; {n} is used"
    return int(v), None


def device_timeout_max(cfg: dict) -> tuple[int, str | None]:
    """runner.device_timeout_max_s as whole seconds (0 = no ceiling), and a problem line when the value
    is not a number > 0. When set, no device job the harness queues may run longer: `ttp devq submit`
    refuses a longer --timeout and gives a job without one the ceiling, and workers are told to size
    every device timeout within it."""
    runner = cfg.get("runner") if isinstance(cfg, dict) else None
    v = runner.get("device_timeout_max_s") if isinstance(runner, dict) else None
    if v is None:
        return 0, None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 1:
        return 0, f"runner.device_timeout_max_s: {v!r} is not a number of seconds >= 1; no ceiling is used"
    return int(v), None


def lower_priority(n: int):
    """A Popen preexec_fn that lowers the child's CPU priority by `n` nice levels before it execs,
    so all it starts inherits it; None when `n` is 0. Never stops the child: a failure leaves it
    at the parent's priority, which the caller sees in its effective level. On Linux the I/O
    priority follows the nice level unless set on its own."""
    if n <= 0:
        return None

    def apply() -> None:
        try:
            os.nice(n)
        except OSError:
            pass
    return apply


def renice(pid: int, n: int) -> None:
    """Lower process `pid`'s CPU priority to `n` nice levels below this process, for a child that
    waits for a go before it starts anything (preexec_fn is not safe in the daemon, which has
    threads). Never raises: a failure leaves it at its priority."""
    if n <= 0:
        return
    try:
        # getpriority, not os.nice(0): macOS refuses that with EPERM at some levels.
        os.setpriority(os.PRIO_PROCESS, pid, min(os.getpriority(os.PRIO_PROCESS, 0) + n, 19))
    except OSError:
        pass


def pin_verify_mode(value: Any) -> str:
    """A project's verification policy for explicit task pins."""
    if value not in ("strict", "lenient"):
        raise ValueError("runner.pin_verify must be strict or lenient")
    return value


def config_problems(raw: dict, root: Path | None = None, fetch: bool = False) -> list[str]:
    """Unknown keys, non-command push_checks or after_push, a scalar push_exclude_paths, a malformed version_bump, push queue
    settings out of range and a push branch that can never be pushed to (push_branch_problem; with
    `root`, the code repo, its remotes are checked too) in a project's own settings, one line each. A
    local push branch of a repo with no remote is not one: reviews land on it (lands_locally). Push
    check targets are looked up at the remote-tracking push branch (targets_tip), fetched first when
    `fetch`."""
    out: list[str] = [f"{k}: a section of settings holds {json.dumps(v)[:60]}, not an object: read as absent "
                      f"(set its keys one by one: ttp config {k}.<key> VALUE)" for k, v in bad_sections(raw).items()]

    def walk(node: dict, path: list[str]) -> None:
        for k, v in node.items():
            hint = unknown_key_hint(".".join(path + [str(k)]))
            if hint:
                out.append(hint)
            elif isinstance(v, dict) and _known_keys(path + [str(k)]) is not None:
                walk(v, path + [str(k)])
    walk(raw, [])
    from .push import (allow_protected, applicability_problems, backup_problem, by_repo_problems,   # push imports this module
                       bump_problems, check_problems, targets_tip,
                       fast_forward_check, lands_locally, mirror_check, push_branch_problem, scalar_problem, wrap_lane_of,
                       wrap_of, fallback_lane_of)
    for read in (wrap_of, wrap_lane_of):
        try:
            read(raw)
        except ValueError as e:
            out.append(str(e))
    out += [why for why in [fallback_lane_of(raw, None)[2]] if why]
    delivery = raw.get("delivery") or {}
    out += [why for why in [backup_problem(delivery)] if why]
    if not lands_locally(delivery.get("push_branch"), root, allow_protected(delivery)):
        out += [why for why in [push_branch_problem(delivery.get("push_branch"), root, allow_protected(delivery))] if why]
    try:
        fast_forward_check(delivery.get("fast_forward_also"), str(delivery.get("push_branch") or ""))
    except ValueError as e:
        out.append(str(e))
    try:
        mirror_check(delivery.get("mirror_remotes"), str(delivery.get("push_branch") or ""))
    except ValueError as e:
        out.append(str(e))
    out += [f"delivery.push_checks: {p}" for p in check_problems(delivery.get("push_checks"))]
    tip = targets_tip(root, delivery.get("push_branch"), fetch) if root else "HEAD"
    out += [f"delivery.push_checks: {p}" for p in applicability_problems(delivery.get("push_checks"), root, tip)]
    out += [f"delivery.post_land_checks: {p}" for p in check_problems(delivery.get("post_land_checks"))]
    out += [f"delivery.task_checks: {p}" for p in check_problems(delivery.get("task_checks"))]
    out += [f"delivery.push_checks_by_repo: {p}" for p in by_repo_problems(delivery.get("push_checks_by_repo"))]
    for k in ("checks_targeted", "checks_task_full"):
        if delivery.get(k) not in (None, True, False):
            out.append(f"delivery.{k}: not true or false")
    for k in ("checks_fast_globs", "checks_fast_exclude", "checks_targeted_always"):
        v = delivery.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            out.append(f"delivery.{k}: not a list of " + ("path strings" if k == "checks_targeted_always" else "glob strings"))
    out += [f"delivery.push_exclude_paths: {p}" for p in [scalar_problem(delivery.get("push_exclude_paths"))] if p]
    from .checkprestage import problems as prestage_problems
    out += prestage_problems(delivery.get("check_prestage"))
    from .prbody import problems as pr_body_problems
    out += pr_body_problems(delivery.get("pr_body_check"))
    from .aitag import problems as ai_tag_problems
    out += ai_tag_problems(delivery.get("ai_tag"))
    if delivery.get("checks_host") not in (None, ""):
        from .remotecheck import host_problem
        out += [f"delivery.checks_host: {p}" for p in [host_problem(delivery["checks_host"])] if p]
    from .ghroute import problem as gh_route_problem
    out += [f"delivery.gh_route: {p}" for p in [gh_route_problem(delivery.get("gh_route"))] if p]
    from .checkspill import problems as spill_problems, wrap_problems
    out += spill_problems(delivery) + wrap_problems(raw.get("checks"))
    out += bump_problems(delivery.get("version_bump"))
    coord = raw.get("coordinator")
    if isinstance(coord, dict) and "cache_ttl" in coord and not str(coord["cache_ttl"] or "").strip():
        out.append('coordinator.cache_ttl: "" (left by an older template) is read as "auto"; `ttp upgrade` '
                   'sets it to "auto"')
    from .coordinator import ask_gate_rules
    from .devq import config_problems as runner_problems
    from .hostmap import problems as host_problems
    out += ask_gate_rules(raw)[1]
    from .handoffcheck import rules as handoff_check_rules
    out += handoff_check_rules(raw)[1]
    out += host_problems(raw)
    runners = (raw.get("device") or {}).get("runners")
    if runners is not None and not isinstance(runners, dict):
        out.append("device.runners: not a table of runners by name")
    ceiling, why = device_timeout_max(raw)
    out += [why] if why else []
    for name, rcfg in (runners if isinstance(runners, dict) else {}).items():
        out += runner_problems(name, rcfg)
        jt = rcfg.get("job_timeout_s") if isinstance(rcfg, dict) else None
        if ceiling and isinstance(jt, (int, float)) and not isinstance(jt, bool) and jt > ceiling:
            out.append(f"device.runners.{name}.job_timeout_s: {jt} is above runner.device_timeout_max_s "
                       f"({ceiling}); jobs without their own --timeout get {ceiling}")
    may_push = delivery.get("code_tasks_may_push", APPROVED_PUSH)
    if not isinstance(may_push, bool) and str(may_push).strip().lower() != APPROVED_PUSH:
        out.append(f"delivery.code_tasks_may_push: {may_push!r} is not true, false or \"{APPROVED_PUSH}\"; "
                   "code tasks do not push")
    own = delivery.get("own_push")
    if own is not None and not isinstance(own, bool) and \
            str(own).strip().lower().replace("-", "_") not in OWN_PUSH_MODES + ("existing", "true", "false", "yes", "no"):
        out.append(f"delivery.own_push: {own!r} is not one of {', '.join(OWN_PUSH_MODES)}; `ttp push --own` is off")
    out += rebase_problems(delivery)
    from .channeltag import problems as channel_tag_problems
    out += channel_tag_problems(delivery)
    if not isinstance(delivery.get("plugin_version_guard", False), bool):
        out.append(f"delivery.plugin_version_guard: {delivery['plugin_version_guard']!r} is not true or false; "
                   "the plugin version guard is off")
    queue = delivery.get("push_queue", False)
    if not isinstance(queue, bool):
        out.append(f"delivery.push_queue: {queue!r} is not true or false; reviews push with ttp push")
    for key in PUSH_QUEUE_NUMBERS:
        if key in delivery:
            try:
                push_queue_number(key, delivery[key])
            except ValueError as e:
                out.append(str(e))
    out += [f"delivery.after_push: {p}" for p in check_problems(delivery.get("after_push"))]
    out += _disk_problems(raw.get("disk"))
    from .outagetask import config_problems as outage_problems
    out += outage_problems(raw)
    kinds = (raw.get("worktree") or {}).get("kinds") if isinstance(raw.get("worktree"), dict) else None
    if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
        out.append(f"worktree.kinds: {kinds!r} is not a list of task kinds; only code tasks get worktrees")
    elif kinds and set(kinds) & {"review", "harness"}:
        out.append("worktree.kinds: review and harness tasks never get a worktree of their own; they are skipped")
    from .coordinator import tool_list   # coordinator imports this module
    for prov, pcfg in (raw.get("providers") or {}).items() if isinstance(raw.get("providers"), dict) else ():
        denied = pcfg.get("disallowed_tools") if isinstance(pcfg, dict) else None
        kind = pcfg.get("billing") if isinstance(pcfg, dict) else None
        if kind is not None and str(kind).lower() not in ("auto", "plan", "usage"):
            out.append(f"providers.{prov}.billing: {kind!r} is not auto, plan or usage; auto is used")
        try:
            tool_list(denied or [], strict=True)
        except ValueError as e:
            out.append(f"providers.{prov}.disallowed_tools: {str(e).split(';')[0]}; "
                       "skipped, so workers may still call it")
    from .chatbridge import config_problems as chat_problems
    out += chat_problems(raw)
    from .route import config_problems as route_problems
    out += route_problems(raw)
    from .provallow import problems as allowed_problems
    out += allowed_problems(raw)
    if isinstance(raw.get("budget"), dict):
        from .globalcap import setting_problems
        out += setting_problems(raw["budget"])
        from .cachettl import worker_problems
        out += worker_problems(raw["budget"])
    if isinstance(raw.get("runner"), dict) and "nice" in raw["runner"]:
        out += [why for why in [nice_level(raw["runner"])[1]] if why]
    v = (raw.get("runner") or {}).get("prevent_idle_sleep") if isinstance(raw.get("runner"), dict) else None
    if v is not None and not isinstance(v, bool) and str(v).strip().lower() not in ("auto", "on", "off"):
        out.append(f"runner.prevent_idle_sleep: {v!r} is not auto, on or off; off is used")
    m = raw.get("machine") if isinstance(raw.get("machine"), dict) else {}
    v = m.get("worker_limit")
    if v is not None and str(v).strip().lower() not in ("auto", "on", "off", "true", "false"):
        out.append(f"machine.worker_limit: {v!r} is not auto, on or off; auto is used")
    v = m.get("max_workers")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        out.append(f"machine.max_workers: {v!r} is not a whole number of at least 1 (or null)")
    v = m.get("pace_from")
    if v is not None and str(v).strip().lower() not in ("moderate", "heavy"):
        out.append(f"machine.pace_from: {v!r} is not moderate or heavy; moderate is used")
    v = (raw.get("runner") or {}).get("pin_verify") if isinstance(raw.get("runner"), dict) else None
    if v is not None and v not in ("strict", "lenient"):
        out.append(f"runner.pin_verify: {v!r} is not strict or lenient")
    return out

def deep_merge(base: dict, over: dict) -> dict:
    """A copy of base with over merged in, dict by dict; over's other values are put in as they are.
    It copies base once (config() runs it on every call: copy.deepcopy made each call cost a millisecond)."""
    out = _copy_tree(base)
    _merge_into(out, over)
    return out


def _merge_into(out: dict, over: dict) -> None:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            _merge_into(out[k], v)
        else:
            out[k] = v


def _copy_tree(v: Any) -> Any:
    """copy.deepcopy for plain dicts, lists and scalars, without its memo; anything else goes to it."""
    t = type(v)
    if t is dict:
        return {k: _copy_tree(x) for k, x in v.items()}
    if t is list:
        return [_copy_tree(x) for x in v]
    if t in (str, int, float, bool) or v is None:
        return v
    return copy.deepcopy(v)


_PROC = Path("/proc/self/stat").exists()
_PS = threading.local()   # this thread's open ps_snapshot: .on, and .rows (taken at .at) once taken
# A snapshot older than this is taken again at the next question: a long block (a daemon tick holds one
# open) never judges a process from a stale `ps`, yet its many questions still share one per window.
PS_SNAPSHOT_MAX_S = 2.0


@contextmanager
def ps_snapshot() -> Iterator[None]:
    """Where there is no /proc (macOS), zombie and runner.proc_start answer inside this block from one
    `ps` of every process, taken at the first question, never one `ps` per pid: a wait loop opens it once
    per poll cycle. With /proc both read it without forking, and this does nothing. Nested: the outer one."""
    if _PROC or getattr(_PS, "on", False):
        yield
        return
    _PS.on, _PS.rows, _PS.at = True, None, 0.0
    try:
        yield
    finally:
        _PS.on, _PS.rows = False, None


def ps_rows() -> dict[int, tuple[str, str]] | None:
    """The open ps_snapshot's processes, {pid: (state, start as `ps -o lstart=` prints it)}; None outside
    one, or when `ps` failed (the caller then asks per pid, as without a snapshot)."""
    if _PROC or not getattr(_PS, "on", False):
        return None
    if _PS.rows is None or time.monotonic() - _PS.at > PS_SNAPSHOT_MAX_S:
        rows: dict[int, tuple[str, str]] = {}
        try:
            out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "stat=", "-o", "lstart="], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                rows[int(parts[0])] = (parts[1], parts[2].strip())
        _PS.rows = rows or False   # ps lists itself: no rows at all means it failed
        _PS.at = time.monotonic()
    return _PS.rows or None


def ps_cycle() -> None:
    """A new poll cycle in this thread's open ps_snapshot: its next question takes a fresh `ps`."""
    if getattr(_PS, "on", False):
        _PS.rows = None


def zombie(pid: int) -> bool:
    """True if pid has ended but is not reaped yet: kill(pid, 0) still finds it, yet nothing runs.
    It is read, never reaped, so whoever owns the process still gets its exit code. Linux reads
    /proc; elsewhere (macOS) `ps` reports the state (ps_snapshot's, inside one)."""
    if _PROC:
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False
    if (rows := ps_rows()) is not None:   # one started since the snapshot is not one yet
        return rows.get(pid, ("",))[0].startswith("Z")
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out.startswith("Z")


def hostname() -> str:
    """This machine's short name, restricted to characters that survive markers, ssh and URLs."""
    raw = os.environ.get("TTP_HOST") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "", raw) or "localhost"


# The harness files only the daemon writes, which sweep_harness commits when a commit of them failed.
SWEPT_PATHS = (":(glob)CHARTER*", "MEMORY.md", "memory")
MEMORY_PATHS = ("MEMORY.md", "memory")   # of those, the memory index and entries
# Project.memory_locked: one flock in state/ (never in the checkout git sees) for MEMORY.md writes and the
# harness merge; how long a writer waits for it before it writes anyway.
MEMORY_LOCK = "memory-index.lock"
MEMORY_LOCK_WAIT_S = 20.0
_MEMORY_HELD = threading.local()   # .paths: {lock path: depth} this thread holds

# Memory kinds every prompt carries whatever the budget; decisions and facts fill what is left.
PINNED_MEMORY_KINDS = ("restriction", "preference", "resource")
# Memory each prompt carries, in characters of whole entries (see Project.memory_select). A worker's
# charter digest and memory share theirs (see prompts.worker_context); its Restrictions come on top.
COORDINATOR_MEMORY_CHARS, WORKER_MEMORY_CHARS = 12000, 8000
# Room the coordinator's newest decisions and facts keep whatever pinned entries take: once pinned
# entries leave less, it goes on top of the budget (the memory grows by at most this), so neither side
# is cut for the other (Project.memory_select).
COORDINATOR_RECENT_MIN_CHARS = 3000
# `ttp memory` warns, naming the largest pinned entries, once pinned entries take more than this share
# of COORDINATOR_MEMORY_CHARS (coordinator.coordinator_pinned_total).
COORDINATOR_PINNED_WARN_SHARE = 2 / 3
# Memory pinned for workers gets what the charter digest leaves of WORKER_MEMORY_CHARS, never less
# than this: a long charter neither crowds it out nor is crowded out by it.
WORKER_PINNED_MIN_CHARS = WORKER_MEMORY_CHARS // 2
# Likewise the charter's policies and other items for workers get what its Goals and Resources leave
# of it, never less than this, and never less because pinned memory is large.
WORKER_POLICY_MIN_CHARS = WORKER_MEMORY_CHARS // 2
# Room for a worker's unpinned (task-ranked) memory past the pinned entries shown, whatever the
# charter digest leaves: ranking always has room to show something.
WORKER_RANKED_MIN_CHARS = 1500
# A pinned entry for workers with no room left in their prompt is shown as a pointer: its name and
# its first sentence, the line cut to this many chars (memory_pointer), so even dozens of them stay
# within about the room. Restrictions are never shortened.
MEMORY_POINTER_CHARS = 120
# A user's correction of a fact (a `fact` whose text starts CORRECTION_PREFIX) is listed first, under
# CORRECTIONS_HEADER, in every worker's prompt and the coordinator's digest for CORRECTION_DAYS, and no
# memory budget drops it there; past CORRECTION_MAX entries or CORRECTION_CHARS the oldest go first.
# Out of that list it is an ordinary fact again (Project.user_corrections).
CORRECTION_PREFIX = "Correction (user msg #"
CORRECTIONS_HEADER = "User corrections (outrank project notes and older output)"
CORRECTION_DAYS, CORRECTION_MAX, CORRECTION_CHARS = 30, 10, 2000
# Memory kinds archive_hidden_memory may retire once no prompt shows them (never a standing entry).
ARCHIVABLE_MEMORY_KINDS = ("decision", "fact")
# An entry that cites the user's word (`user msg #N`, `(user, <date>)`, "the user's words"): the
# archive retires one only after the non-user decisions and facts of its age (archive_hidden_memory).
USER_CITE = re.compile(r"\buser(?:'s)?\s+(?:msgs?|messages?)\b|\buser's (?:own |verbatim )?words\b|\(user\b[,);:]"
                       r"|^\s*(?:\[[^\]]*\]\s*)?user\s+\d{4}-\d{2}-\d{2}", re.I | re.M)
MEMORY_SHOWN_KEY = "memory_shown"   # kv: when a prompt last carried each entry (see note_memory_shown)
# Memory audiences an entry's front matter may name (`audience:`); without one, see worker_memory.
MEMORY_AUDIENCES = ("workers", "coordinator")
_WORKER_WORD = re.compile(r"\bworkers?\b", re.I)


def worker_memory(e: dict) -> int:
    """Where memory entry `e` stands in a worker's prompt: 1, pinned (tagged for workers,
    restrictions, resources, standing entries; worker_always says which of them no limit shortens); 2,
    shown first while they fit (entries that speak of workers); 3, then the newest facts that fit; 0,
    never (the rest, written for the coordinator:
    decisions and preferences that are not standing, and anything tagged for the coordinator but a
    restriction)."""
    if e.get("audience") in MEMORY_AUDIENCES:
        return 1 if e["audience"] == "workers" or e["kind"] == "restriction" else 0
    if e["kind"] in ("restriction", "resource") or e.get("standing"):
        return 1
    return 2 if _WORKER_WORD.search(e["line"]) else 3 if e["kind"] == "fact" else 0


def worker_always(e: dict) -> bool:
    """Whether a worker's prompt carries pinned entry `e` whatever its limits: restrictions and resources
    (a worker that misses one breaks a rule or acts on the wrong machine or folder). Standing entries
    and the others tagged for workers fill the room left, standing ones first; past it each is a
    pointer (memory_pointer) to where it is in full."""
    return bool(e["kind"] in ("restriction", "resource"))


def coordinator_memory(e: dict) -> bool:
    """Whether the coordinator's prompt carries memory entry `e` (and its budget counts it): all but
    those tagged for workers alone (`audience: workers`), which only worker_memory counts. A
    restriction binds everyone, whatever its audience."""
    return e.get("audience") != "workers" or e["kind"] == "restriction"


def memory_pointer(e: dict) -> str:
    """The line a worker's prompt shows for pinned entry `e` its room cannot hold whole: its name and
    first sentence, the line at most MEMORY_POINTER_CHARS (its name whole). The prompt says where
    the whole entry is."""
    text = " ".join(e.get("body", "").split())
    line = f"[{e['name']}] " + re.split(r"(?<=[.!?;])\s", text, maxsplit=1)[0]
    return line if len(line) <= MEMORY_POINTER_CHARS else \
        line[:max(MEMORY_POINTER_CHARS, len(e["name"]) + 13) - 1].rstrip() + "…"


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./#:-]*[A-Za-z0-9]")
# A word that names something: a digit, an inner _ / . : # or -, camelCase, or ALL CAPS.
_NAMING = re.compile(r"\d|[_/.:#-]|[a-z][A-Z]|^[A-Z]{3,}$")
_NOT_NAMES = frozenset(("e.g", "i.e", "etc", "and/or", "vs"))


def task_words(text: str) -> set[str]:
    """The words of `text` that can tie a memory entry to a task: those that name something
    (identifiers, paths and their parts, resource names, task and message numbers, and anything in
    backticks), lower-cased, 3+ characters. Plain words never count: they tie every entry to every task."""
    out = set()
    ticked = {w for span in re.findall(r"`([^`\n]+)`", text) for w in _TOKEN.findall(span)}
    for tok in _TOKEN.findall(text):
        for w in {tok, *tok.split("/")}:
            if len(w) >= 3 and (w in ticked or _NAMING.search(w)) and w.lower() not in _NOT_NAMES:
                out.add(w.lower())
    return out


class Project:
    """A project rooted at <root>/tt-project. `harness/` is its own git repo (charter, memory,
    config, prompts, runtime); `state/` holds the database, run directories and logs."""

    def __init__(self, base: str | Path):
        base = Path(base).resolve()
        self.base = base if base.name == FOLDER else base / FOLDER
        self.root = self.base.parent
        self.harness = self.base / "harness"
        from . import noindex      # on macOS these live in state.noindex/ and worktrees.noindex/
        self.state = noindex.real(self.base, "state")
        self.runs = self.state / "runs"
        self.logs = self.state / "logs"
        self.worktrees = noindex.real(self.base, "worktrees")
        self.memory_dir = self.harness / "memory"
        self._db: DB | None = None
        self._harness_queued: dict[str, int] = {}   # harness path -> commits queued for it (commit_harness)
        self._last_good: dict | None = None   # project.json as this process last read it valid
        self.config_status = "ok"             # how raw_config last found it (see read_config)

    # layout -----------------------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.harness / "project.json"

    @property
    def charter_path(self) -> Path:
        return self.harness / "CHARTER.md"

    @property
    def memory_index(self) -> Path:
        return self.harness / "MEMORY.md"

    def exists(self) -> bool:
        return self.config_path.is_file()

    @contextmanager
    def memory_locked(self, wait_s: float | None = None) -> Iterator[bool]:
        """Hold the memory index lock for one MEMORY.md write or a harness merge (harnesstree.land), so an
        index write never lands between git's look at the file and its rewrite by the merge. Held only
        for the write or merge, never across a model call. Waits up to `wait_s` (MEMORY_LOCK_WAIT_S),
        polling; yields True once held, False past the bound or when the lock file cannot be opened:
        the caller goes on (a writer writes anyway, never dropping its entry; land answers 'busy' and
        tries next pass).
        Re-entrant in one thread; a dead holder's flock goes with it."""
        lock = str(self.state / MEMORY_LOCK)
        held = _MEMORY_HELD.__dict__.setdefault("paths", {})
        if held.get(lock):
            held[lock] += 1
            try:
                yield True
            finally:
                held[lock] -= 1
            return
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            fd = None
        wait_s = MEMORY_LOCK_WAIT_S if wait_s is None else wait_s
        got, end = False, time.monotonic() + max(wait_s, 0)
        try:
            while fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        self._git_log(f"memory index lock busy past {wait_s:g}s: going on without it")
                        break
                except OSError:   # a file system without flock: as before the lock
                    break
                time.sleep(0.05)
            if got:
                held[lock] = 1
            try:
                yield got
            finally:
                if got:
                    held.pop(lock, None)
        finally:
            if fd is not None:
                os.close(fd)

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.state / "project.db")
            try:   # the tag a message to the user drops (aitag.for_user); the default one goes anyway
                cfg = json.loads(self.config_path.read_text())
                self._db.ai_tag = str(((cfg.get("delivery") or {}).get("ai_tag")) or "")
            except (OSError, ValueError, AttributeError):
                pass
        return self._db

    # config -----------------------------------------------------------------------------------
    def raw_config(self) -> dict:
        """The project's own settings (see read_config); `config_status` keeps how they were found."""
        data, self.config_status = self.read_config()
        return data

    def read_config(self) -> tuple[dict, str]:
        """The project's own settings and how they were found. A hand edit that breaks the JSON, or a
        file that goes missing for a moment, keeps the last good copy (on disk, else the one this
        process last read) in force instead of taking the project down or quietly moving its model
        and provider routing to the defaults: "fallback". With neither, an established project (one
        with a database) is "unavailable" and the daemon starts no model work until the file reads
        again; a project still being made has an empty config, "ok"."""
        good = self.state / "project.last-good.json"
        try:
            data = statcache.load_json(self.config_path)
            if not isinstance(data, dict):
                raise ValueError("project.json is not a JSON object")
        except (OSError, ValueError):
            for read in (lambda: json.loads(good.read_text()), lambda: self._last_good):
                try:
                    kept = read()
                except (OSError, ValueError):
                    continue
                if isinstance(kept, dict):
                    return copy.deepcopy(kept), "fallback"
            return {}, "unavailable" if self.established() else "ok"
        if bad_sections(data):
            return data, "ok"   # read with those sections absent (layered), never kept as the last good copy
        self._last_good = copy.deepcopy(data)
        try:
            # <=: two writes within one filesystem clock tick share an mtime.
            if self.state.is_dir() and (not good.exists() or good.stat().st_mtime <= self.config_path.stat().st_mtime):
                write_json(good, data)
        except OSError:
            pass
        return data, "ok"

    def established(self) -> bool:
        """The project has run: its database exists (never created by asking)."""
        return (self.state / "project.db").is_file()

    def config(self) -> dict:
        return layered(self.raw_config())

    def set_config(self, dotted: str, value: Any, by: dict | None = None) -> None:
        """Write one key into project.json and record who set it (`by`, configwho.by; none: unknown)."""
        raw = self.raw_config()
        if self.config_status == "unavailable":
            # Writing the one key would make a project.json of defaults and that key alone.
            raise RuntimeError(f"{self.config_path} is missing or not valid JSON and there is no last good copy: "
                               f"restore it before changing settings")
        if why := section_value_problem(dotted, value) or key_type_problem(dotted, value):
            raise ValueError(why)
        if dotted in HOST_WIDE_KEYS:
            raise ValueError(f"{dotted} is host-wide, one value for every project on this machine: "
                             f"ttp config --account {dotted} VALUE")
        before = copy.deepcopy(raw)
        node = raw
        parts = dotted.split(".")
        for i, p in enumerate(parts[:-1]):
            cur = node.get(p)
            if not isinstance(cur, dict):
                # Only an unset key or a section saved as one plain value (bad_sections) becomes a new
                # section: any other value (a list, a string setting) is refused, never overwritten.
                if cur is not None and not _is_section(parts[:i + 1]):
                    raise ValueError(f"{'.'.join(parts[:i + 1])} is one value, not a section of settings "
                                     f"({json.dumps(cur)[:60]}): set it as a whole")
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value
        from . import routeguard
        if why := routeguard.refusal(before, raw, by):
            raise ValueError(why)
        if self._guarded(dotted, by, before, raw):
            return
        if self.config_status == "fallback":
            self.keep_broken_config()
        write_json(self.config_path, raw)
        if self.established():   # a project still being made has no database to record it in yet
            try:
                from . import configwho
                configwho.changed(self, dotted, by, before, raw)
            except Exception:   # the setting is written; a busy or broken database only loses its setter
                pass

    def _guarded(self, dotted: str, by: dict | None, before: dict, after: dict) -> bool:
        """configwho.guard: raises configwho.Protected when an automated setter would change a value the
        user or a coordinator set; True when there is nothing to write."""
        if not self.established():
            return False
        from . import configwho
        try:
            return configwho.guard(self, dotted, by, before, after)
        except configwho.Protected:
            raise
        except Exception:   # a busy or broken database never stops the change itself
            return False

    def unset_config(self, dotted: str, by: dict | None = None) -> bool:
        """Remove one key from project.json, so it reads its default again, and record who did; sections
        it leaves empty go too. False when the key was not set (nothing written)."""
        raw = self.raw_config()
        if self.config_status == "unavailable":
            raise RuntimeError(f"{self.config_path} is missing or not valid JSON and there is no last good copy: "
                               f"restore it before changing settings")
        before = copy.deepcopy(raw)
        parts = dotted.split(".")
        trail = [raw]
        for p in parts[:-1]:
            nxt = trail[-1].get(p)
            if not isinstance(nxt, dict):
                return False
            trail.append(nxt)
        if parts[-1] not in trail[-1]:
            return False
        del trail[-1][parts[-1]]
        for i in range(len(parts) - 1, 0, -1):
            if trail[i]:
                break
            del trail[i - 1][parts[i - 1]]
        from . import routeguard
        if why := routeguard.refusal(before, raw, by):
            raise ValueError(why)
        self._guarded(dotted, by, before, raw)
        if self.config_status == "fallback":
            self.keep_broken_config()
        write_json(self.config_path, raw)
        if self.established():
            try:
                from . import configwho
                configwho.changed(self, dotted, by, before, raw)
            except Exception:   # as in set_config: the removal is written; only its setter is lost
                pass
        return True

    def keep_broken_config(self) -> None:
        """Before set_config writes the last good copy over a project.json that did not read (a hand
        edit that broke the JSON), keep its bytes in project.json.broken-<stamp> and say so: the
        edit is never lost without a word. A missing project.json has nothing to keep."""
        src = self.config_path
        if not src.exists():
            return
        stamp = time.strftime("%Y%m%dT%H%M%S")
        kept = src.with_name(f"{src.name}.broken-{stamp}")
        n = 1
        while kept.exists():
            kept = src.with_name(f"{src.name}.broken-{stamp}-{n}")
            n += 1
        try:
            os.link(src, kept)   # the same bytes under a second name; write_json then replaces src
        except OSError:
            shutil.copy2(src, kept)
        msg = f"config: {src.name} did not read as JSON; kept it as {kept} before writing the last good copy"
        print(f"ttp: {msg}", file=sys.stderr)
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with open_append(self.logs / "daemon.log") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
        except OSError:
            pass

    @property
    def name(self) -> str:
        return self.raw_config().get("name", self.root.name)

    # memory -----------------------------------------------------------------------------------
    def commit_harness(self, paths: list[Path], message: str, removed: list[Path] = ()) -> None:
        """Commit just these harness files, so the project's history shows what it learned and when.
        `removed` are files this change took away (their deletion is committed).

        Only the named paths are committed: a harness task may be editing other files right now,
        and its half-done work must not be swept into this commit. Inside a database transaction
        the commit waits until the transaction ends, so git never holds the write lock; until it
        runs, this process's sweep leaves its paths to it, so each change keeps its own commit
        (a file change made in the transaction registers its undo with db.on_rollback).
        """
        top = self.harness.resolve()
        rel = [str(Path(x).resolve().relative_to(top)) for x in paths if Path(x).exists()]
        gone = [str(Path(x).resolve().relative_to(top)) for x in removed if not Path(x).exists()]
        if not (rel or gone) or not (self.harness / ".git").exists():
            return
        if not self.db.conn.in_transaction:
            self._git_commit(rel, message, gone)
            return
        mine = [*rel, *gone]
        queued = self._harness_queued

        def release() -> None:
            for x in mine:
                if queued.get(x, 0) <= 1:
                    queued.pop(x, None)
                else:
                    queued[x] -= 1

        def run() -> None:
            release()
            self._git_commit(rel, message, gone)

        for x in mine:
            queued[x] = queued.get(x, 0) + 1
        self.db.after_commit(run)
        self.db.on_rollback(release)

    def _harness_git_cfg(self, key: str) -> float:
        # Read project.json directly: read_config would keep this process's last good copy, which a
        # later set_config on a missing project.json would write back.
        try:
            v = float((statcache.load_json(self.config_path).get("harness_git") or {}).get(key) or 0)
        except (OSError, TypeError, ValueError, AttributeError):
            v = 0
        return v if v > 0 else DEFAULT_CONFIG["harness_git"][key]

    def _git_log(self, msg: str) -> None:
        """A line in the daemon's log, which every process of the project may append to."""
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with open_append(self.logs / "daemon.log") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} harness git: {msg}\n")
        except OSError:
            pass

    def clear_stale_git_locks(self, now: float | None = None) -> list[str]:
        """Move aside the lock files a killed git left in the harness repo, once they are older than
        harness_git.stale_lock_s and no process holds them (ttp.gitlocks). Each is kept under
        .git/ttp-stale-locks/<stamp>/, never deleted. Returns the moved paths (relative to .git)."""
        try:
            cfg = statcache.load_json(self.config_path)   # directly, as _harness_git_cfg does
        except (OSError, ValueError):
            cfg = {}
        gdir = gitlocks.git_dir(self.harness)
        now = time.time() if now is None else now
        try:
            moved, held = gitlocks.clear(self.harness, gitlocks.settings(cfg if isinstance(cfg, dict) else {}), now)
        except Exception as e:
            self._git_log(f"stale lock check failed: {e!r}")
            return []
        if held:
            self._git_log(f"old lock file(s) left alone: pid(s) {', '.join(map(str, held))} may hold them")
        if not moved:
            return []
        rel = [str(x.relative_to(gdir)) for x in moved]
        self._git_log(f"moved stale lock file(s) aside to {gitlocks.keep_dir(gdir, now)}: {', '.join(rel)}")
        return rel

    def clear_stale_root_ref_locks(self, now: float | None = None) -> list[str]:
        """Move aside the ref locks a killed or rebooted writer left in the project root's repository,
        once older than harness_git.root_ref_lock_s and nothing may own them (gitlocks.clear_root_refs).
        Each lock past the age is logged with its age and holder check. Returns the moved paths."""
        try:
            cfg = statcache.load_json(self.config_path)
        except (OSError, ValueError):
            cfg = {}
        now = time.time() if now is None else now
        try:
            moved, kept = gitlocks.clear_root_refs(self.root, gitlocks.root_settings(cfg if isinstance(cfg, dict) else {}), now)
        except Exception as e:
            self._git_log(f"root ref lock check failed: {e!r}")
            return []
        for path, age, why in kept:
            self._git_log(f"root ref lock {path} ({age / 3600:.1f} h old) left alone: {why}")
        for path, age, to in moved:
            self._git_log(f"root ref lock {path} ({age / 3600:.1f} h old, no holder) moved aside to {to}")
        return [str(p) for p, _, _ in moved]

    def _git_commit(self, rel: list[str], message: str, gone: list[str] = (), sweep: bool = True) -> bool:
        """Commit these harness paths. A failure is logged with git's stderr and exit code; files it
        leaves uncommitted are picked up by the next successful commit's sweep or the daemon's."""
        ident = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
        git = ["git", "-C", str(self.harness), *git_fsync_args()]
        timeout = self._harness_git_cfg("timeout_s")
        step = "ls-files"

        def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            r = subprocess.run([*git, *args], capture_output=True, text=True, timeout=timeout)
            if check and r.returncode:
                raise subprocess.CalledProcessError(r.returncode, args, r.stdout, r.stderr)
            return r

        self.clear_stale_git_locks()
        try:
            if gone:
                gone = run("ls-files", "--", *gone).stdout.split()
                if gone:
                    step = "rm"
                    run("rm", "-q", "--cached", "--", *gone)
            if rel:
                step = "add"
                run("add", "--", *rel)
            if not (rel or gone):
                return False
            step = "commit"
            r = run(*ident, "commit", "-q", "-m", message[:200], "--", *rel, *gone, check=False)
            if r.returncode:
                if not run("status", "--porcelain", "--", *rel, *gone, check=False).stdout.strip():
                    return True   # another process committed these already
                raise subprocess.CalledProcessError(r.returncode, "commit", r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            self._git_log(f"{step} timed out after {timeout:g} s ({message[:80]!r}); harness_git.timeout_s sets it")
            return False
        except subprocess.CalledProcessError as e:
            err = " | ".join(((e.stderr or "") + (e.stdout or "")).strip().splitlines())[:500]
            self._git_log(f"{step} failed with exit {e.returncode} ({message[:80]!r}): {err}")
            return False
        except OSError as e:
            self._git_log(f"{step} could not run ({message[:80]!r}): {e}")
            return False
        if sweep:
            self.sweep_harness()
        return True

    def sweep_harness(self, min_age_s: float = 60, paths: tuple[str, ...] = SWEPT_PATHS,
                      message: str = "harness: commit files an earlier failed commit left") -> bool:
        """Commit the charter and memory files (`paths`: CHARTER*, MEMORY.md, memory/) a failed commit
        left uncommitted, so it heals itself. A file changed in the last `min_age_s` is left to the commit
        its writer is about to make. A file showing the marks of a cut write is never committed (see
        integrity.looks_cut), and nothing is swept on a boot whose integrity check has not run yet:
        after a power cut the uncommitted files are what that check puts back from the last commit.
        True when it committed something."""
        from . import integrity
        from .runner import same_boot
        if not (self.harness / ".git").exists():
            return False
        try:   # kv "integrity" is Daemon.check_integrity's record; none yet: no daemon has run it
            boot = (self.db.kv("integrity") or {}).get("boot") if (self.state / "project.db").exists() else None
        except Exception:
            boot = None
        if boot and not same_boot(boot):
            return False
        try:
            out = subprocess.run(["git", "-C", str(self.harness), "status", "--porcelain=v1", "-z", "--no-renames",
                                  "--untracked-files=all", "--", *paths],
                                 capture_output=True, text=True, timeout=self._harness_git_cfg("timeout_s"))
        except (OSError, subprocess.SubprocessError) as e:
            self._git_log(f"sweep: status could not run: {e}")
            return False
        if out.returncode:
            self._git_log(f"sweep: status failed with exit {out.returncode}: {out.stderr.strip()[:300]}")
            return False
        now = time.time()
        rel, gone, cut = [], [], []
        for entry in filter(None, out.stdout.split("\0")):
            path = entry[3:]
            if path in self._harness_queued:
                continue   # a commit this process queued (commit_harness) takes it, with its message
            f = self.harness / path
            try:
                if now - f.stat().st_mtime < min_age_s:
                    continue
                if integrity.looks_cut(self, path, f.read_bytes()):
                    cut.append(path)
                    continue
                rel.append(path)
            except FileNotFoundError:
                gone.append(path)
            except OSError:
                continue
        if cut:
            self._git_log(f"sweep: left {len(cut)} file(s) that look like a cut write for the integrity check: "
                          + ", ".join(cut)[:300])
        if not (rel or gone):
            return False
        self._git_log(f"sweep: committing {len(rel) + len(gone)} file(s) ({message}): "
                      + ", ".join([*rel, *gone])[:300])
        return self._git_commit(rel, message, gone, sweep=False)

    def add_memory(self, text: str, kind: str = "fact", title: str | None = None, key: str | None = None,
                   end: dict | None = None, standing: bool = False, audience: str | None = None,
                   commit: bool = True, dry_run: bool = False, reason: str | None = None,
                   context: str | None = None) -> Path:
        """One fact per file plus a one-line pointer in MEMORY.md, so the index stays cheap to load.
        A memory written again under the same `key` (a replayed coordinator turn) keeps its one
        file and line. `end` (see ends.from_action) makes it temporary: the daemon retires it then.
        `standing` marks an instruction that applies every time its case comes up (a preference
        unless pinned already): finishing one instance never retires it (see forget_memory).
        `audience` (one of MEMORY_AUDIENCES) says whether workers get it (see worker_memory).
        `commit` False leaves the harness commit to the caller (see supersede_memory). `dry_run`
        only returns the path it would write: no file, index line or commit. `reason` (why the rule
        was given) and `context` (the user's words around it) let an old order be re-read by its
        intent once that reason is gone (see oldorders)."""
        from .ends import front_matter
        from .forbidden import check
        from .oldorders import clean_context, one_line
        reason, context = one_line(reason), one_line(context)
        check(self, "memory", text, title or "", reason or "")   # before any write: nothing refused reaches git
        context = clean_context(self, context)   # harness-attached user words: dropped when forbidden, never refused
        if standing and kind not in PINNED_MEMORY_KINDS:
            kind = "preference"
        if not dry_run:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        title = (title or text.splitlines()[0])[:80]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "note"
        tag = f"turn: {key}\n" if key else ""
        same = [f for f in sorted(self.memory_dir.glob(f"{kind}-{slug}*.md"))
                if f"\n{tag}---\n" in f.read_text()] if key else []
        if same:
            path = same[0]
        else:
            # Never a retired entry's name either: a replayed turn that forgot it and then added
            # this one would retire the new entry over the archived copy.
            path = self.memory_dir / f"{kind}-{slug}.md"
            n = 2
            while path.exists() or (self.memory_dir / "archive" / path.name).exists():
                path = self.memory_dir / f"{kind}-{slug}-{n}.md"
                n += 1
        if dry_run:
            return path
        with self.memory_locked():   # the entry and its line, never split by a harness merge
            if not same:
                durable_write(path, f"---\nkind: {kind}\ncreated: {_stamp(time.time())}\n{front_matter(end or {})}"
                                    f"{'standing: true' + chr(10) if standing else ''}"
                                    f"{f'audience: {audience}' + chr(10) if audience in MEMORY_AUDIENCES else ''}"
                                    f"{f'reason: {reason}' + chr(10) if reason else ''}"
                                    f"{f'context: {context}' + chr(10) if context else ''}"
                                    f"{tag}---\n{text}\n")
            index = self.memory_index.read_text() if self.memory_index.exists() else ""
            line = None
            if f"](memory/{path.name})" not in index:
                line = f"- [{title}](memory/{path.name}) ({kind}{', standing' if standing else ''})\n"
                durable_append(self.memory_index, line)

        def undo() -> None:   # the transaction this ran in rolled back: so does the entry
            if not same:
                path.unlink(missing_ok=True)
                note_protected(path)
            if line:
                self._reindex_memory(drop=[line])
        self.db.on_rollback(undo)
        if commit:
            self.commit_harness([path, self.memory_index], f"memory ({kind}): {title}")
        return path

    def _reindex_memory(self, drop: list[str] = (), add: list[str] = (), before: str | None = None,
                        after: str | None = None) -> None:
        """Undo an index change of a rolled-back transaction: the text `before` it while the index is
        still the `after` it wrote, else take out the lines `drop` and put back the lines `add`,
        leaving every other line (one another process wrote meanwhile) as it is."""
        with self.memory_locked():
            text = self.memory_index.read_text() if self.memory_index.exists() else ""
            if before is not None and text == after:
                durable_write(self.memory_index, before)
                return
            lines = text.splitlines(keepends=True)
            for x in drop:
                if x in lines:
                    lines.remove(x)
            lines += [x for x in add if x not in lines]
            if "".join(lines) != text:
                durable_write(self.memory_index, "".join(lines))

    def _memory_entries(self) -> list[dict]:
        """Live memory entries, oldest first: `name` (the file stem shown in prompts), `kind`, `line`,
        `end` (see ends) for a temporary one, whose line says when it ends, `standing` (see
        add_memory), whose line says so, `audience` (see worker_memory) and `reason` and `context`
        (see add_memory; "" when not recorded).
        Ordered by the front matter's `created`, then name; the file's mtime stands in only where
        `created` is missing, so a checkout or copy that touches the files does not reorder them
        (which would change the coordinator's cached prompt)."""
        if not self.memory_dir.is_dir():
            return []
        from .ends import describe, read_front_matter
        out = []
        for p in self.memory_dir.glob("*.md"):
            raw = p.read_text()
            head, body = (raw.split("---", 2)[1:] if raw.startswith("---") and raw.count("---") >= 2
                          else ("", raw))
            m = re.search(r"^kind:\s*(\S+)", head, re.M)
            kind = m.group(1) if m else p.stem.split("-", 1)[0]
            c = re.search(r"^created:\s*(\S+)", head, re.M)
            end = read_front_matter(head)
            standing = bool(re.search(r"^standing:[ \t]*true[ \t]*$", head, re.M))
            aud = re.search(r"^audience:[ \t]*(\S+)", head, re.M)
            why = re.search(r"^reason:[ \t]*(.*)$", head, re.M)
            ctx = re.search(r"^context:[ \t]*(.*)$", head, re.M)
            ends = (" (standing)" if standing else "") + (f" ({describe(end, self)})" if end else "")
            created = c.group(1) if c else _stamp(p.stat().st_mtime)
            out.append({"name": p.stem, "kind": kind, "line": f"[{p.stem}] {body.strip()}{ends}", "end": end,
                        "body": body.strip(),
                        "standing": standing, "audience": aud.group(1) if aud else "", "created": created,
                        "reason": why.group(1).strip() if why else "", "context": ctx.group(1).strip() if ctx else "",
                        "order": (created, p.stem)})
        out.sort(key=lambda e: e.pop("order"))
        return out

    def user_corrections(self, now: float | None = None) -> list[dict]:
        """The user's corrections of facts younger than CORRECTION_DAYS, oldest first: the newest
        CORRECTION_MAX that fit CORRECTION_CHARS together (see CORRECTION_PREFIX)."""
        since = (time.time() if now is None else now) - CORRECTION_DAYS * 86400
        out, used = [], 0
        for e in reversed(self._memory_entries()):
            if e["kind"] != "fact" or not e["body"].startswith(CORRECTION_PREFIX) or _stamp_ts(e["created"]) < since:
                continue
            if len(out) == CORRECTION_MAX or used + len(e["line"]) + 1 > CORRECTION_CHARS:
                break
            out.append(e)
            used += len(e["line"]) + 1
        return out[::-1]

    def memory_select(self, limit_chars: int = COORDINATOR_MEMORY_CHARS,
                      recent_min: int = COORDINATOR_RECENT_MIN_CHARS) -> tuple[list[dict], dict]:
        """The entries a prompt of `limit_chars` gets, whole: every pinned entry (restrictions,
        preferences, resources, standing entries, user_corrections) first, then the newest decisions and
        facts that fit the room pinned entries leave, never less than `recent_min` (capped at
        `limit_chars`): past the budget that reserve goes on top. One too big for the room left is
        skipped and older, smaller ones still considered. Pinned entries are kept even past the budget.
        Only entries the coordinator sees count (coordinator_memory; user_corrections always do).
        Also returns the usage numbers for the digest: `recent_left` decisions and facts left out,
        `crowded` when pinned entries took all but the reserve so that some were."""
        corrected = {e["name"] for e in self.user_corrections()}
        entries = [e for e in self._memory_entries() if coordinator_memory(e) or e["name"] in corrected]
        pin = lambda e: e["kind"] in PINNED_MEMORY_KINDS or e["standing"] or e["name"] in corrected
        pinned = [e for e in entries if pin(e)]
        pinned_chars = sum(len(e["line"]) + 1 for e in pinned)
        reserve = max(0, min(recent_min, limit_chars))
        room = max(limit_chars - pinned_chars, reserve)
        unpinned = [e for e in entries if not pin(e)]
        rest, used = [], 0
        for e in reversed(unpinned):
            if used + len(e["line"]) + 1 <= room:   # whole entries only; a too-big one is skipped
                rest.append(e)
                used += len(e["line"]) + 1
        chosen = pinned + rest[::-1]
        left = len(unpinned) - len(rest)
        return chosen, {"entries": len(entries), "shown": len(chosen), "pinned": len(pinned),
                        "chars": sum(len(e["line"]) + 1 for e in entries), "pinned_chars": pinned_chars,
                        "limit": limit_chars, "pinned_over": pinned_chars > limit_chars,
                        "recent_shown": len(rest), "recent_left": left, "recent_room": room, "reserve": reserve,
                        "crowded": bool(left) and pinned_chars + reserve > limit_chars}

    def worker_memory_select(self, limit_chars: int, task: dict | None = None, pinned_limit: int | None = None,
                             restated=None, entries: list[dict] | None = None) -> tuple[list[dict], dict]:
        """The entries a worker's prompt gets, whole, oldest first: those worker_memory pins, the
        restrictions and resources always (worker_always), whatever the limits, then the
        others while they fit `pinned_limit` (None:
        all; when not all fit, those that share the most words with `task` first, then standing ones,
        then the oldest);
        then, only if every pinned one fits: for `task`, the
        others that share words with its title and spec (task_words; a word few entries share weighs
        more), most shared first, then newest, each that fits; then those that speak of workers and
        then the facts, newest first while all fit `limit_chars`. A pinned one with no room whole is
        never left out: it gets a pointer (memory_pointer), whose room is set aside before any of
        them is shown whole; a restriction is never one. An entry whose text `restated(text)`
        says the prompt already states (the charter's restrictions) is left out. Usage numbers as
        memory_select's, counting only the entries meant for workers, plus `duplicates` (left out as
        restated), `pinned_dropped` (pinned ones past `pinned_limit`, shown as `pointers`: dicts of
        `name` and `line`, oldest first), `kept`, `kept_chars` and
        `kept_over` (those always carried alone outgrow `pinned_limit`, or `limit_chars` without one).
        user_corrections are left out: worker_system lists them on their own, outside this budget.
        `entries`: the live entries to select from in place of memory/'s (a write not made yet)."""
        corrected = {e["name"] for e in self.user_corrections()}
        entries = [e for e in (self._memory_entries() if entries is None else entries)
                   if worker_memory(e) and e["name"] not in corrected]
        dup = {e["name"] for e in entries if restated and restated(e.get("body", ""))}
        pinned = [e for e in entries if worker_memory(e) == 1 and e["name"] not in dup]
        pinned_chars = sum(len(e["line"]) + 1 for e in pinned)
        want = task_words(f"{task.get('title') or ''}\n{task.get('spec') or ''}") if task else set()

        def scores(group: list[dict]) -> dict[str, float]:
            words = {e["name"]: task_words(e["line"]) & want for e in group}
            df: dict[str, int] = {}
            for ws in words.values():
                for w in ws:
                    df[w] = df.get(w, 0) + 1
            return {n: sum(1 / df[w] for w in ws) for n, ws in words.items()}
        # Restrictions and resources first, every one whatever the limits: a worker that misses one breaks
        # a rule or acts on the wrong machine or folder. Standing entries lead the rest, within the room.
        kept = [e for e in pinned if worker_always(e)]
        used = kept_chars = sum(len(e["line"]) + 1 for e in kept)
        chosen = {e["name"] for e in kept}
        over = pinned_limit is not None and pinned_chars > pinned_limit
        score = scores(pinned) if over and want else {}
        # Past the room, each of the others is at least pointed to: their pointers' room comes first,
        # then each shown whole (in place of its pointer) while that fits.
        short = {e["name"]: memory_pointer(e) for e in pinned if over and e["name"] not in chosen}
        used += sum(len(x) + 1 for x in short.values())
        for _, e in sorted(enumerate(pinned), key=lambda x: (-score.get(x[1]["name"], 0),
                                                             not x[1].get("standing"), x[0])):
            if e["name"] in chosen:
                continue
            ptr = len(short[e["name"]]) + 1 if e["name"] in short else 0
            if pinned_limit is None or used - ptr + len(e["line"]) + 1 <= pinned_limit:
                chosen.add(e["name"])
                used += len(e["line"]) + 1 - ptr
        # None of the others while a pinned one is only pointed to.
        rest = [e for e in entries if worker_memory(e) != 1 and e["name"] not in dup] \
            if len(chosen) == len(pinned) else []
        if task:
            score = scores(rest)
            for i, e in sorted(enumerate(rest), key=lambda x: (-score[x[1]["name"]], -x[0])):
                if score[e["name"]] and used + len(e["line"]) + 1 <= limit_chars:
                    chosen.add(e["name"])
                    used += len(e["line"]) + 1
        for rank in (2, 3):
            for e in reversed([e for e in rest if worker_memory(e) == rank]):
                if e["name"] in chosen:
                    continue
                if used + len(e["line"]) + 1 > limit_chars:
                    break   # whole entries only, and no older one slips in past a gap
                chosen.add(e["name"])
                used += len(e["line"]) + 1
        shown = [e for e in entries if e["name"] in chosen]
        pointers = [{"name": e["name"], "line": short[e["name"]]} for e in pinned if e["name"] not in chosen]
        return shown, {"entries": len(entries), "shown": len(shown), "pinned": len(pinned), "pointers": pointers,
                       "chars": sum(len(e["line"]) + 1 for e in entries), "pinned_chars": pinned_chars,
                       "limit": limit_chars, "duplicates": len(dup),
                       "pinned_over": pinned_chars > (limit_chars if pinned_limit is None else pinned_limit),
                       "pinned_dropped": len([e for e in pinned if e["name"] not in chosen]),
                       "kept": len(kept), "kept_chars": kept_chars,
                       "kept_over": kept_chars > (limit_chars if pinned_limit is None else pinned_limit)}

    def memory_text(self, limit_chars: int = COORDINATOR_MEMORY_CHARS) -> str:
        """Pinned entries, then the newest others (newest last), whole and bounded so they never
        crowd the prompt."""
        return "\n".join(e["line"] for e in self.memory_select(limit_chars)[0])

    def _resolve_memory_name(self, name: str, keep: str | None = None) -> tuple[str, bool]:
        """(entry, archived) for `name`. An exact name wins, live or archived, so a replayed forget
        of an archived entry never reaches a live one. Otherwise `name` may be a prefix of exactly
        one entry, live or archived; one ending on a '-' word boundary ('fact-94' of 'fact-94-a',
        not 'fact-940-b') is preferred. Names are title slugs, so nothing beyond the prefix is
        guessed. `keep` (the entry just added) is never a candidate. Ambiguous or missing: refused."""
        if not name:
            raise ValueError("no memory entry named; use the name in [brackets] from MEMORY")
        archive = self.memory_dir / "archive"
        entries = [(f.stem, d == archive) for d in (self.memory_dir, archive) if d.is_dir()
                   for f in sorted(d.glob("*.md")) if f.stem != keep]
        for e in entries:
            if e[0] == name:
                return e
        for found in ([e for e in entries if e[0].startswith(name + "-")],
                      [e for e in entries if e[0].startswith(name)]):
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ValueError(f"memory entry {name!r} is ambiguous; nothing retired. Use one of: "
                                 + ", ".join(f"[{n}]" + (" (archived)" if old else "") for n, old in found))
        raise ValueError(f"no memory entry {name!r}; use the name in [brackets] from MEMORY")

    def forget_memory(self, name: str, keep: str | None = None, why: str | None = None) -> Path:
        """Retire an entry: its file moves to memory/archive/ and its line leaves MEMORY.md, so no
        prompt carries it again. Forgetting an entry already archived (a replayed turn) is a no-op.
        `keep` names an entry that must survive (the one a `supersedes` adds). A standing entry is
        refused unless `why` names its end condition or the user's words ending it: one finished
        instance, a review's tidy-up or memory pressure never retires a duty that recurs."""
        done, errors = self.forget_memories([name], keep, why)
        if errors:
            raise ValueError(errors[0])
        return done[0]

    def forget_memories(self, names: list[str], keep: str | None = None, why: str | None = None,
                        commit: bool = True, dry_run: bool = False) -> tuple[list[Path], list[str]]:
        """forget_memory for several entries with one MEMORY.md rewrite and one harness commit:
        (archived paths, one error per name refused). A refused name (unknown, ambiguous, standing
        without `why`) leaves the others retired. `commit` False leaves the commit to the caller.
        `dry_run` returns the same without moving, rewriting or committing anything."""
        why = " ".join((why or "").split())
        from .forbidden import check
        check(self, "memory retirement reason (why)", why)   # it goes into the archive and the commit message
        done, errors, moved, again = [], [], [], []
        for raw in names:
            try:
                name, archived = self._resolve_memory_name(Path(raw.strip().strip("[]")).stem, keep)
            except ValueError as e:
                errors.append(str(e))
                continue
            src, dst = self.memory_dir / f"{name}.md", self.memory_dir / "archive" / f"{name}.md"
            if archived:
                done.append(dst)
                again.append(name)   # a replay: commit it if a killed run left it uncommitted
                continue
            if not why and re.search(r"^standing:[ \t]*true[ \t]*$", src.read_text().split("\n---\n", 1)[0], re.M):
                errors.append(f"[{name}] is a standing instruction; nothing retired. It applies every time its "
                              "case comes up, so one finished instance does not end it. Retire it only with a "
                              "`why` naming its end condition or quoting the user's words ending it; to make "
                              "it shorter, add a standing entry that supersedes it")
                continue
            if dry_run:
                done.append(dst)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            note_protected(src), note_protected(dst)
            done.append(dst)
            moved.append(name)
        if dry_run:
            return done, errors
        # Whether a killed run left a replayed name uncommitted takes git, which never runs under the
        # write lock: inside a transaction that check waits until it ends.
        later = again if commit and self.db.conn.in_transaction else []
        again = [n for n in again if commit and not later and self._harness_dirty(self._retired_paths(n))]
        if not (moved or again):
            self._commit_replayed_later(later, why)
            return done, errors
        fsync_dir(self.memory_dir / "archive")
        fsync_dir(self.memory_dir)
        dropped, before, after = [], None, None
        if moved:
            with self.memory_locked():   # read and rewrite as one step: no append or merge in between
                if self.memory_index.exists():
                    before = self.memory_index.read_text()
                    lines = before.splitlines(keepends=True)
                    dropped = [x for x in lines if any(f"](memory/{n}.md)" in x for n in moved)]
                    after = "".join(x for x in lines if x not in dropped)
                    durable_write(self.memory_index, after)

        def undo() -> None:   # the transaction this ran in rolled back: so does the retirement
            for n in moved:
                a, b = self.memory_dir / "archive" / f"{n}.md", self.memory_dir / f"{n}.md"
                if a.exists() and not b.exists():
                    os.replace(a, b)
                    note_protected(a), note_protected(b)
            fsync_dir(self.memory_dir)
            fsync_dir(self.memory_dir / "archive")
            self._reindex_memory(add=dropped, before=before, after=after)
        self.db.on_rollback(undo)
        if commit:
            self._commit_retired([*moved, *again], why)
        self._commit_replayed_later(later, why)   # after this retirement's own commit
        return done, errors

    @staticmethod
    def _retired_paths(name: str) -> list[str]:
        return [f"memory/{name}.md", f"memory/archive/{name}.md"]

    def _commit_retired(self, gone: list[str], why: str) -> None:
        self.commit_harness([*(self.memory_dir / "archive" / f"{n}.md" for n in gone), self.memory_index],
                            f"memory retired: {', '.join(gone)}" + (f" ({why})" if why else ""),
                            removed=[self.memory_dir / f"{n}.md" for n in gone])

    def _commit_replayed_later(self, names: list[str], why: str) -> None:
        """Once the open transaction commits, commit the replayed retirements `names` a killed run left
        uncommitted; one already committed makes no commit. A rollback drops it (after_commit)."""
        if not names:
            return

        def run() -> None:
            dirty = [n for n in names if self._harness_dirty(self._retired_paths(n))]
            if dirty:
                self._commit_retired(dirty, why)
        self.db.after_commit(run)

    def _harness_dirty(self, rel: list[str]) -> bool:
        """Whether git shows any of these harness paths changed from the last commit."""
        if not (self.harness / ".git").exists():
            return False
        try:
            out = subprocess.run(["git", "-C", str(self.harness), "status", "--porcelain", "--", *rel],
                                 capture_output=True, text=True, timeout=self._harness_git_cfg("timeout_s"))
        except (OSError, subprocess.SubprocessError):
            return False
        return bool(out.stdout.strip())

    def note_memory_shown(self, names: list[str], now: float | None = None) -> None:
        """Record that a prompt being built carries the entries `names`, and when a prompt was last
        built, so hidden_memory can tell which entries every prompt leaves out. `since` marks when
        recording began: no entry counts as hidden from before it."""
        now = time.time() if now is None else now
        with self.db.tx():
            m = self.db.kv(MEMORY_SHOWN_KEY) or {}
            shown = dict(m.get("entries") or {})
            shown.update((n, now) for n in names)
            self.db.set_kv(MEMORY_SHOWN_KEY, {"since": m.get("since") or now, "built": now, "entries": shown})

    def hidden_memory(self, days: float) -> list[str]:
        """The decisions and facts (never standing) that every prompt built in the last `days` days
        left out: not shown since `days` before the newest prompt, nor created or recorded since. It
        counts up to the newest prompt, not now, so a project that built none (paused, idle) hides nothing."""
        m = self.db.kv(MEMORY_SHOWN_KEY) or {}
        if days <= 0 or not m.get("built"):
            return []
        built, shown = float(m["built"]), m.get("entries") or {}
        since = float(m.get("since") or built)
        # An entry pinned for workers (worker_memory 1: tagged `audience: workers`) is never hidden,
        # like the coordinator's pinned kinds: worker_memory_select may leave it out of a prompt for
        # pinned overflow or because it matches a charter restriction, and neither is a sign it is
        # spent. The restriction match is fuzzy and the charter can change, so it is no proof the entry
        # is covered either. Retire one by hand (`ttp memory --forget`).
        return [e["name"] for e in self._memory_entries()
                if e["kind"] in ARCHIVABLE_MEMORY_KINDS and not e["standing"] and worker_memory(e) != 1
                and built - max(float(shown.get(e["name"]) or 0), since, _stamp_ts(e["created"])) >= days * 86400]

    def archive_hidden_memory(self, days: float) -> list[str]:
        """Retire hidden_memory(days) to memory/archive/ with one harness commit, and drop retired
        names from the shown map. Returns the names retired. An entry that cites the user's word
        (USER_CITE) stays while a decision or fact that does not, created no later than a day after
        it, stays live: the archive never retires the user's decision before a non-user one of its age."""
        names = self.hidden_memory(days)
        if names:
            entries = {e["name"]: e for e in self._memory_entries()}
            cites = {n for n in names if USER_CITE.search(entries[n]["line"])}
            kept = [_stamp_ts(e["created"]) for n, e in entries.items() if n not in names
                    and e["kind"] in ARCHIVABLE_MEMORY_KINDS and not e["standing"] and not USER_CITE.search(e["line"])]
            names = [n for n in names if n not in cites
                     or not any(t <= _stamp_ts(entries[n]["created"]) + 86400 for t in kept)]
        done = self.forget_memories(names, why=f"no prompt showed it for {days:g}+ days")[0] if names else []
        with self.db.tx():
            m = self.db.kv(MEMORY_SHOWN_KEY)
            if m:
                live = {e["name"] for e in self._memory_entries()}
                m["entries"] = {n: t for n, t in (m.get("entries") or {}).items() if n in live}
                self.db.set_kv(MEMORY_SHOWN_KEY, m)
        return [d.stem for d in done]

    def search_memory(self, words: list[str]) -> list[dict]:
        """Live and archived entries whose name or text holds every one of `words` (any case), oldest
        first: `name`, `kind`, `standing`, `archived` and `text` (the body)."""
        want = [w.lower() for w in " ".join(words).split()]
        out = []
        for d, archived in ((self.memory_dir, False), (self.memory_dir / "archive", True)):
            for f in sorted(d.glob("*.md")) if d.is_dir() else []:
                head, body = _memory_parts(f.read_text())
                if not all(w in f"{f.stem}\n{body}".lower() for w in want):
                    continue
                m = re.search(r"^kind:\s*(\S+)", head, re.M)
                c = re.search(r"^created:\s*(\S+)", head, re.M)
                out.append({"name": f.stem, "kind": m.group(1) if m else f.stem.split("-", 1)[0],
                            "standing": bool(re.search(r"^standing:[ \t]*true[ \t]*$", head, re.M)),
                            "archived": archived, "text": body.strip(), "created": c.group(1) if c else ""})
        return sorted(out, key=lambda e: (e["created"], e["name"]))

    def superseded_end(self, names: list[str]) -> dict:
        """The end (see ends.from_action) an entry replacing the live entries `names` carries over, so a
        temporary entry never turns permanent by a merge: the earliest `expires` with that entry's
        until and probe, else the first until or probe; {} when none has an end. Unknown, ambiguous
        and archived names are skipped (the retirement reports them)."""
        from .ends import read_front_matter
        found = []
        for raw in names:
            try:
                name, archived = self._resolve_memory_name(Path(str(raw).strip().strip("[]")).stem)
            except ValueError:
                continue
            if not archived:
                found.append(read_front_matter(_memory_parts((self.memory_dir / f"{name}.md").read_text())[0]))
        found = [e for e in found if e]
        return dict(min(found, key=lambda e: e.get("expires", math.inf))) if found else {}

    def supersede_memory(self, text: str, names: list[str], kind: str = "fact", standing: bool = False,
                         title: str | None = None, audience: str | None = None, end: dict | None = None,
                         permanent: bool = False, dry_run: bool = False) -> tuple[Path, list[Path]]:
        """Add a merged entry and retire the live entries it replaces, with one harness commit:
        (new entry, archived paths). Refused whole, nothing written, when a name is unknown, ambiguous
        or already archived, or the merge would lose a user rule: a standing entry is replaced only
        by a standing one, a restriction only by a restriction, a preference only by a preference or
        restriction, a resource only by a resource, and the quoted words of a restriction, preference
        or standing entry (the user's verbatim words) stay word for word in the new text. `title` and
        `audience` as add_memory's; with no `audience` the new entry keeps the one its entries share,
        "default" stores none. With no `end` it keeps the replaced entries' earliest (superseded_end),
        unless `permanent`. `dry_run` checks the same and returns the same paths, writing and committing nothing."""
        from .forbidden import check
        check(self, "memory", text, title or "")
        if end and permanent:
            raise ValueError("nothing changed: an end and permanent contradict each other; give one")
        if standing and kind not in PINNED_MEMORY_KINDS:
            kind = "preference"
        new = " ".join(text.split())
        errors, olds, auds, bodies = [], [], set(), {}
        for raw in names:
            try:
                name, archived = self._resolve_memory_name(Path(raw.strip().strip("[]")).stem)
            except ValueError as e:
                errors.append(str(e))
                continue
            if archived:
                errors.append(f"[{name}] is archived already")
                continue
            head, body = _memory_parts((self.memory_dir / f"{name}.md").read_text())
            m = re.search(r"^kind:\s*(\S+)", head, re.M)
            old = m.group(1) if m else name.split("-", 1)[0]
            old_standing = bool(re.search(r"^standing:[ \t]*true[ \t]*$", head, re.M))
            am = re.search(r"^audience:[ \t]*(\S+)", head, re.M)
            auds.add(am.group(1) if am else "")
            if old_standing and not standing:
                errors.append(f"[{name}] is a standing instruction: only a --standing entry replaces it")
            allowed = {"restriction": ("restriction",), "preference": ("preference", "restriction"),
                       "resource": ("resource",)}.get(old)
            if allowed and kind not in allowed:
                errors.append(f"[{name}] is a {old}: only a {' or '.join(allowed)} entry replaces it (--kind)")
            if old in ("restriction", "preference") or old_standing:
                lost = [q for q in _quoted(body) if " ".join(q.split()) not in new]
                if lost:
                    errors.append(f"[{name}] holds the user's words; keep them verbatim in the new text: "
                                  + "; ".join(f'"{q}"' for q in lost))
            olds.append(name)
            bodies[name] = body
        if errors:
            raise ValueError("nothing changed: " + "\n".join(errors))
        # Merged entries that all share one audience keep it.
        added = self.add_memory(text, kind=kind, title=title, standing=standing, commit=False, dry_run=dry_run,
                                end=end or ({} if permanent else self.superseded_end(olds)),
                                audience=None if audience == "default"
                                else audience or (next(iter(auds)) if len(auds) == 1 else None))
        if dry_run:
            return added, [self.memory_dir / "archive" / f"{n}.md" for n in olds]
        done, errors = self.forget_memories(olds, keep=added.stem, why=f"merged into [{added.stem}]", commit=False)
        self.commit_harness([added, self.memory_index, *done],
                            f"memory ({kind}): [{added.stem}] supersedes {', '.join(d.stem for d in done)}",
                            removed=[self.memory_dir / d.name for d in done])
        from . import echoes
        for d in done:   # the replaced text, searched for in what still binds
            echoes.record(self.db, f"memory [{d.stem}] superseded by [{added.stem}]", bodies.get(d.stem, ""), text,
                          skip=[f"memory:{added.stem}"], kind=echoes.COMPACTED)   # a merge keeps what holds
        if errors:
            raise ValueError("\n".join(errors))
        return added, done


OUTPUT_TAIL = 2000   # chars of a failed daemon-run command's output kept for its event or observation


# A model-free completion command (a task's on_pass_cmd) gets only this: `ttp note` writes to that
# run, and every guard that refuses user-only actions inside a run refuses them here too.
NOTE_RUN_ENV = "TTP_NOTE_RUN_DIR"


def here(start: str | Path | None = None) -> Project | None:
    """The project this process runs in: its run's project, else the one at the folder it was started
    in (`start`, else the current folder) or above it (its git top level included), else, in a git
    worktree, the one at its main checkout."""
    cwd = Path(start) if start else Path.cwd()
    if os.environ.get("TTP_PROJECT") and Project(os.environ["TTP_PROJECT"]).exists():
        return Project(os.environ["TTP_PROJECT"])
    if p := next((c for d in [cwd, *cwd.parents] if (c := Project(d)).exists()), None):
        return p
    if not cwd.is_dir():
        return None
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd, capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = (cwd / r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    if common and common.name == ".git" and (c := Project(common.parent)).exists():
        return c
    return None


def in_run(env=None) -> bool:
    """Whether a command runs for a worker (a run, or a completion command with note-only context):
    it may not act as the user (post a user message, change user-only settings)."""
    env = os.environ if env is None else env
    return any(env.get(k) for k in ("TTP_RUN_ID", "TTP_RUN_DIR", NOTE_RUN_ENV))


def _account() -> tuple[str, str]:
    """This user's name and home from the password database ("" where it has none)."""
    import pwd
    try:
        pw = pwd.getpwuid(os.getuid())
    except (KeyError, OSError):
        return "", ""
    return pw.pw_name, pw.pw_dir


def owner_home(p: Project, env: dict | None = None) -> str:
    """The home a command the daemon runs for `p` gets: the inherited HOME when it is a real home
    (an existing directory of this user's, outside the project's harness folder), else this user's
    home from the password database. A daemon started from a sandbox, a worker or another user's
    shell must not make `~` in a hook mean somewhere else."""
    home = (os.environ if env is None else env).get("HOME") or ""
    try:
        real = (os.path.isabs(home) and os.path.isdir(home) and os.stat(home).st_uid == os.getuid()
                and not Path(home).resolve().is_relative_to(p.base.resolve()))
    except OSError:
        real = False
    return home if real else (_account()[1] or home)


def command_env(p: Project, base: dict | None = None) -> dict[str, str]:
    """The environment of a shell command the daemon runs for `p` (command schedule, heal check and
    fix, start_when/retry_when, until_probe, pause end_when, after_push, a hand-off's on_pass_cmd), as a
    worker's commands see it: TTP_PROJECT set, this runtime importable, `ttp` first on PATH, and HOME
    the project owner's (owner_home), USER and LOGNAME set. Run it with cwd p.root (an after_push in its
    own checkout), so `ttp lock` resolves the project whatever the daemon inherited."""
    from .providers.base import service_path
    env = dict(os.environ if base is None else base)
    dirs = [str(p.harness / "bin"), *service_path().split(":"), *env.get("PATH", "").split(":")]
    user = _account()[0]
    for k in ("USER", "LOGNAME"):
        if user and not env.get(k):
            env[k] = user
    # This runtime only: an inherited PYTHONPATH may name another install's.
    home = owner_home(p, env)
    from . import sshmux
    try:
        reuse = sshmux.env(p.config())
    except Exception:   # an unreadable config keeps the default
        reuse = sshmux.env(None)
    return {**env, **reuse, **({"HOME": home} if home else {}), "TTP_PROJECT": str(p.base),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PATH": ":".join(dict.fromkeys(d for d in dirs if d))}


def output_tail(text: str | bytes | None, limit: int = OUTPUT_TAIL) -> str:
    """The last `limit` chars of a command's output (stderr), stripped: bounded for events and state."""
    if isinstance(text, bytes):
        text = text[-4 * limit:].decode(errors="replace")
    return (text or "").strip()[-limit:].strip()


_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TAIL_READ_BYTES = 256 * 1024   # file_tail reads at most this much of a file's end


def clean_line(line: str, width: int = 400) -> str:
    """One line of a command's output made safe to show a model or a chat: terminal escapes and control
    characters out (a progress bar redrawn with carriage returns keeps its last frame), cut at `width`."""
    line = _ESCAPES.sub("", line).rstrip("\r").rsplit("\r", 1)[-1]
    line = _CONTROL.sub("", line)
    return line if len(line) <= width else line[:width - 1] + "…"


def file_tail(path, lines: int = 20, limit: int = 1500, width: int = 400) -> str:
    """The last `lines` lines of a log (read from its last TAIL_READ_BYTES), each clean_line'd, at most
    `limit` chars with the end kept. Raises OSError for a missing or unreadable file."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL_READ_BYTES))
        data = f.read()
    text = "\n".join(clean_line(x, width) for x in data.decode(errors="replace").splitlines()[-lines:])
    return text[-limit:]


def _stamp_ts(stamp: str) -> float:
    """The time a memory entry's `created` (see _stamp, or a bare date) names; 0 when unreadable."""
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _memory_parts(raw: str) -> tuple[str, str]:
    """A memory file's (front matter, body)."""
    return tuple(raw.split("---", 2)[1:]) if raw.startswith("---") and raw.count("---") >= 2 else ("", raw)


def _quoted(text: str) -> list[str]:
    """The quoted spans of a memory entry ("...", “...”): the words it keeps as said."""
    return [a or b for a, b in re.findall(r'"([^"\n]{3,})"|“([^”\n]{3,})”', text)]


def _stamp(ts: float) -> str:
    """A memory entry's `created`: UTC to the microsecond, so entries sort in the order they were
    written. Older entries carry the date alone, which sorts before any stamp of the same day."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1e6):06d}Z"


def fsync_dir(path: Path) -> None:
    """Make a rename or a new file in this directory survive a power cut. Some file systems
    cannot open or sync a directory; they get what the OS gives."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sync_all() -> None:
    """Flush every filesystem (setup and upgrades write whole trees). Tests skip it: on a busy host
    one flush can take minutes, and test folders need no power-loss durability."""
    if hasattr(os, "sync") and not os.environ.get("TTP_TEST_NO_SYNC"):
        os.sync()


TMP_TRIES = 8


def _tmp_suffix() -> str:
    return os.urandom(6).hex()


def durable_write(path: Path, text: str | bytes, mode: int | None = None, sync: bool = True) -> None:
    """Replace `path` with `text` so a power cut leaves the old content or the new, never an empty
    or half-written file: a temporary file in the same directory is written and synced, renamed over
    `path`, and the directory is synced so the rename lasts too. The file keeps its mode unless
    `mode` is given (a new file gets `mode`, else the umask's default). Never written in place, so a
    shell running the old file as a script keeps reading it whole. `sync=False` skips both syncs (a
    caller that syncs the whole tree after)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode() if isinstance(text, str) else text
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except OSError:
            pass
    # A fresh name opened O_EXCL|O_NOFOLLOW: workers can write the state and run folders, and a
    # symlink or file planted at the temporary name is never followed, written or removed.
    for tries in range(TMP_TRIES):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{_tmp_suffix()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600 if mode is not None else 0o666)
            break
        except FileExistsError:
            if tries == TMP_TRIES - 1:
                raise
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if sync:
                os.fsync(f.fileno())
            if mode is not None:
                os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if sync:
        fsync_dir(path.parent)
    note_protected(path, data)


PROTECTED_LOG = "protected-writes.jsonl"   # in a project's state dir (protected_log)
PROTECTED_LOG_MAX = 256 * 1024   # then it moves to <name>.1, which coordtools.guard reads too


def protected_log(path: str | Path) -> Path | None:
    """The log of ttp's own writes to `path` when it is a project's charter, memory index, a memory entry
    (also archived) or project.json; else None. A session coordinator's Bash guard (coordtools.guard)
    keeps a change it finds logged and puts back any other."""
    s = str(path)
    name = os.path.basename(s)
    if not (name in ("project.json", "MEMORY.md") or name.startswith("CHARTER") or f"{os.sep}memory{os.sep}" in s):
        return None
    real = Path(os.path.realpath(os.path.dirname(os.path.abspath(s)))) / name   # a planted symlink is not followed
    for harness in list(real.parents)[:3]:
        if harness.name == "harness" and harness.parent.name == FOLDER:
            rel = real.relative_to(harness).parts
            if rel[0] == "memory" and len(rel) > 1 or len(rel) == 1 and rel[0] in ("project.json", "MEMORY.md") \
                    or len(rel) == 1 and rel[0].startswith("CHARTER"):
                state = harness.parent / "state"
                return state / PROTECTED_LOG if state.is_dir() else None
            return None
    return None


def note_protected(path: str | Path, data: bytes | None = None) -> None:
    """Log that ttp wrote `path` (its new bytes `data`, else read now; a missing file logs its removal)
    when it is protected (protected_log). Never fails the write it follows."""
    try:
        log = protected_log(path)
        if log is None:
            return
        import hashlib
        if data is None:
            try:
                data = Path(path).read_bytes()
            except FileNotFoundError:
                pass
        sha = hashlib.sha256(data).hexdigest() if data is not None else None
        if log.exists() and log.stat().st_size > PROTECTED_LOG_MAX:
            os.replace(log, log.with_name(log.name + ".1"))
        durable_append(log, json.dumps({"ts": time.time(), "path": os.path.abspath(path), "sha": sha}) + "\n")
    except OSError:
        pass


def protected_files(harness: str | Path) -> dict[str, bytes]:
    """The protected files (protected_log) in a harness checkout, path -> bytes: the charter files, the
    memory index, every memory entry (archived too) and project.json."""
    h = Path(harness)
    try:
        files = [h / n for n in os.listdir(h) if n.startswith("CHARTER") or n in ("MEMORY.md", "project.json")]
    except OSError:
        return {}
    files += [Path(d) / n for d, _, names in os.walk(h / "memory") for n in names
              if not (n.startswith(".") and n.endswith(".tmp"))]   # durable_write's temporary files
    out = {}
    for f in files:
        try:
            out[os.path.abspath(f)] = f.read_bytes()
        except OSError:
            continue
    return out


@contextmanager
def tree_writes(harness: str | Path) -> Iterator[None]:
    """The one way ttp runs a git command (or a put-back) that can change tracked files in a harness
    checkout (a harness task's landing merge and its abort, the upgrade's fast-forward or merge and the
    upgrade journal's put-back): logs each protected file it changed, created or removed (note_protected),
    so a session coordinator's Bash guard (coordtools.guard) keeps it. A static test fails on a git writer
    on the harness checkout outside it."""
    before = protected_files(harness)
    try:
        yield
    finally:
        after = protected_files(harness)
        for f in sorted(set(before) | set(after)):
            if before.get(f) != after.get(f):
                note_protected(f, after.get(f))


def replace_copy(src: str | Path, dst: str | Path, *, follow_symlinks: bool = True) -> str:
    """shutil.copy2 that never rewrites `dst` in place (shutil.copytree's copy_function for installed
    trees): a temporary file renamed over it, with the source's mode and times, so a script a shell is
    running keeps its old content to the end. Not synced: the caller syncs the tree (sync_all)."""
    src, dst = Path(src), Path(dst)
    if dst.is_dir():
        dst = dst / src.name
    durable_write(dst, src.read_bytes(), mode=src.stat().st_mode & 0o7777, sync=False)
    shutil.copystat(src, dst)
    return str(dst)


class AppendRefused(OSError):
    """An append target that is a symlink or not a regular file: never followed, never written."""


_REFUSED_APPENDS: list[dict] = []   # refused durable_append calls, until the daemon records them
_REFUSED_LOCK = threading.Lock()
REFUSED_KEEP = 200


def open_append(path: Path, mode: str = "a", **kw):
    """open(path, mode) for appending ("a", "ab", "a+", "ab+") that never follows a symlink at `path`
    and opens nothing but a regular file (a FIFO would hang the writer): those raise AppendRefused.
    Workers can write the state and run folders, and the daemon appends there outside the sandbox: a
    planted symlink would aim the text it appends at any file the user can write."""
    flags = os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR if "+" in mode else os.O_WRONLY)
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as e:
        if os.path.islink(path) or e.errno == errno.ENXIO:   # ELOOP (EMLINK on BSD); ENXIO: a FIFO
            raise AppendRefused(e.errno, "a symlink or special file is never appended to", str(path)) from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AppendRefused(errno.EINVAL, "not a regular file: never appended to", str(path))
        return os.fdopen(fd, mode, **kw)
    except BaseException:
        os.close(fd)
        raise


def refused_appends() -> list[dict]:
    """The appends durable_append refused in this process since the last call, oldest first."""
    with _REFUSED_LOCK:
        out = _REFUSED_APPENDS[:]
        del _REFUSED_APPENDS[:]
    return out


def note_refused_append(path, why: str, text: str = "", p=None) -> None:
    """A refused append (open_append): stderr says so and the text is kept for refused_appends, which the
    daemon records as an append_refused event each tick. With project `p` (a process other than the
    daemon) it goes into p's events at once: a refusal in a short-lived command is never only on its
    stderr."""
    print(f"ttp: refused to append to {path}: {why}", file=sys.stderr)
    with _REFUSED_LOCK:
        _REFUSED_APPENDS.append({"ts": time.time(), "path": str(path), "why": why, "text": text[:2000]})
        del _REFUSED_APPENDS[:-REFUSED_KEEP]
    if p is not None:
        try:
            record_refused_appends(p.db)
        except Exception:
            pass   # still kept in this process's list


def record_refused_appends(db) -> list[dict]:
    """Insert each refused append kept in this process as a handled append_refused event (no model runs
    for it); returns them. One that fails to insert stays kept, with the ones after it."""
    rows = refused_appends()
    for i, r in enumerate(rows):
        try:
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                 (r["ts"], "daemon", "append_refused", "normal",
                  f"refused to append to {r['path']}: {r['why']}"[:500], json.dumps(r), "handled"))
        except Exception:
            with _REFUSED_LOCK:
                _REFUSED_APPENDS[:0] = rows[i:]
            raise
    return rows


def open_append_or_null(path: Path, mode: str = "a", text: str = "", p=None):
    """open_append(path, mode), or, when it refuses the path (a symlink or special file a worker
    planted), the null device opened in `mode`: the caller carries on and what it writes is dropped.
    The refusal is noted with `text`, the line the caller meant to write (note_refused_append)."""
    try:
        return open_append(path, mode)
    except AppendRefused as e:
        note_refused_append(path, e.strerror, text, p)
        return open(os.devnull, mode.replace("+", ""))


def append_text(path: Path, text: str, p=None) -> bool:
    """Append `text` to `path` through open_append; False when it refused the path (noted, never raised)."""
    try:
        f = open_append(path)
    except AppendRefused as e:
        note_refused_append(path, e.strerror, text, p)
        return False
    with f:
        f.write(text)
    return True


def durable_append(path: Path, text: str) -> bool:
    """Append `text` and sync it, so an appended line that was reported written survives a power
    cut. A file this creates gets its directory synced as well. A last line a crash cut short (no
    newline at the end) gets one first, so it never swallows this entry. A symlink or special file
    at `path` is refused (open_append): stderr says so, the text is kept for refused_appends (the
    daemon records it as an event) and False comes back; other write failures raise."""
    path = Path(path)
    new = not os.path.lexists(path)
    try:
        f = open_append(path, "ab+")
    except AppendRefused as e:
        note_refused_append(path, e.strerror, text)
        return False
    with f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                text = "\n" + text
        f.write(text.encode())
        f.flush()
        os.fsync(f.fileno())
    if new:
        fsync_dir(path.parent)
    note_protected(path)
    return True


_git_version: tuple[int, ...] | None = None


def git_version() -> tuple[int, ...]:
    """The installed git's version, (0,) when it cannot be told. Asked once per process."""
    global _git_version
    if _git_version is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
            _git_version = tuple(int(x) for x in m.groups(default="0")) if m else (0,)
        except (OSError, subprocess.SubprocessError):
            _git_version = (0,)
    return _git_version


def git_fsync_config() -> list[tuple[str, str]]:
    """Git settings that make a commit's objects and refs reach the disk before it returns, so a
    power cut cannot leave a commit pointing at empty objects. Given per command or per process,
    never written into anyone's repository config."""
    if git_version() >= (2, 36):
        return [("core.fsync", "committed")]
    return [("core.fsyncObjectFiles", "true")]


def git_fsync_args() -> list[str]:
    return [a for k, v in git_fsync_config() for a in ("-c", f"{k}={v}")]


def git_fsync_env(base: dict[str, str]) -> dict[str, str]:
    """The fsync settings as GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> on top of
    those already in `base` (an environment), for every git a worker runs. Pairs `base` already
    carries are not repeated; a count git would reject is left alone."""
    try:
        n = int(base.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        return {}
    if n < 0:
        return {}
    have = {(base.get(f"GIT_CONFIG_KEY_{i}"), base.get(f"GIT_CONFIG_VALUE_{i}")) for i in range(n)}
    out: dict[str, str] = {}
    for k, v in git_fsync_config():
        if (k, v) in have:
            continue
        out[f"GIT_CONFIG_KEY_{n}"], out[f"GIT_CONFIG_VALUE_{n}"] = k, v
        n += 1
    if out:
        out["GIT_CONFIG_COUNT"] = str(n)
    return out


def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    durable_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


# user-level registry and secrets ------------------------------------------------------------------
def registry_path() -> Path:
    return HOME_DIR / "registry.json"


def load_registry() -> dict:
    try:
        return json.loads(registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"projects": {}}


@contextmanager
def _registry_locked() -> Iterator[None]:
    """One process at a time reads, changes and writes registry.json, so no entry is lost."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path.with_name("registry.json.lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def register(name: str, entry: dict) -> None:
    with _registry_locked():
        reg = load_registry()
        reg.setdefault("projects", {})[name] = {**reg["projects"].get(name, {}), **entry, "updated": time.time()}
        write_json(registry_path(), reg, mode=0o600)


def unregister(name: str) -> None:
    with _registry_locked():
        reg = load_registry()
        if reg.get("projects", {}).pop(name, None) is not None:
            write_json(registry_path(), reg)


# Account-level settings: one file per user and machine that every project reads, below the
# project's own project.json, so the projects on a machine agree unless one overrides a key.
ACCOUNT_KEYS = {"budget": {"global_daily_usd", "daily_target_usd", "day_start", "timezone", "push_spend_to"}, "pricing": {"claude", "codex"},
                "guards": {"protected_paths"},   # paths no run on the host writes (pathguard)
                "route": {"host_max", "classes", "review_small_lines"},   # route.host_limit; run-class routing (runclass.py)
                "checks": {"host_max", "bare_slots", "idle_hold_s", "load_extra", "live_cores", "nice", "ionice", "priority_turns", "urgent_slots", "preempt_after_s", "test_pool", "test_run_max",
                           "test_run_min", "test_project_max", "test_project_slots", "test_project_workers", "load_guard", "load_sustain_s", "build_pool", "build_run_max", "build_project_max", "wall_cap_s",
                           "slots", "light_cmds", "max_wait_s", "wait_event_s", "priority_age_s",
                           "wait_escalate_s", "wait_hang_factor", "done_holder_flag_s", "done_holder_grace_s", "stall_s", "starve_s", "small_tests",
                           "push_load_wait_s",
                           "push_priority_wait_s",   # a push's longest wait behind another priority holder (checkcap.priority_wait_s)
                           "priority_beyond_cap",   # push and urgent runs beyond checks.host_max (checkcap.priority_beyond_cap)
                           "dirty_max_mb", "io_full_max",   # the disk gate on new local runs (diskgate)
                           "spill_hosts",   # the spill pool every project's checks may use (checkspill.account_hosts)
                           "no_spill_hosts",   # hosts no project's checks spill to (checkspill.no_spill; a run only adds)
                           "slurm_overflow",   # a Slurm cluster the pool's overflow runs on (slurmcheck)
                           "smoke_max_s"}}   # the landing check time limit (gatetime.max_s), project first
# Account keys a run may only clear (ttp config --account refuses setting them inside a run): remove them
# ('') or set them to their off value (ACCOUNT_OFF_VALUE), which only reduces their reach.
# The ranking's source and key file too: the key goes to that source, so only the user names either.
ACCOUNT_USER_ONLY = {"checks.spill_hosts", "checks.slurm_overflow", "providers.chat.ranking_source",
                     "providers.chat.ranking_key_file"}
ACCOUNT_OFF_VALUE = {"checks.spill_hosts": [], "checks.slurm_overflow": {}}
# Host-wide account keys the runtime reads only from the account settings: a project's own copy would
# show a value nothing uses, so layered() shows the account's in every project and set_config refuses one.
# Account keys three deep, below a provider: never merged into a project's settings (a project that does not
# use the provider gets no providers.<name> entry); their reader asks for them (account_value).
# providers.chat.model_ranking: read under the project's own (modelrank.ranking), kept current by rankfresh.py
# with its source, interval and key file; providers.chat.class_ranking and class_prefs: the model scoreboard's
# per-class orders and their prefer settings (modelscore.py). providers.chat.served_every_s and served_idle_s: the
# host's shared /models poll interval while any project routes to chat, and else (served.every_s).
ACCOUNT_NESTED_KEYS = {"providers.chat.model_ranking", "providers.chat.class_ranking", "providers.chat.class_prefs", "providers.chat.ranking_source", "providers.chat.ranking_every_s",
                       "providers.chat.ranking_key_file", "providers.chat.served_every_s", "providers.chat.served_idle_s"}
HOST_WIDE_KEYS = {"checks.host_max", "route.host_max", "checks.spill_hosts", "checks.no_spill_hosts", "checks.slurm_overflow", "checks.dirty_max_mb", "checks.io_full_max",
                  "checks.push_priority_wait_s"}


WEEKS_OF_DAYS = 7   # an unset budget.weekly_usd: this many days at the daily cap, so it never stops work first


def weekly_default(b: Any, *layers: Any) -> None:
    """Fill an unset budget.weekly_usd in the merged budget `b` with WEEKS_OF_DAYS times its daily cap:
    a fixed default below a raised daily cap stopped ordered work within hours. A weekly cap set in any
    of `layers` (account settings, project.json) is kept as it is (config provenance)."""
    if not isinstance(b, dict) or any(isinstance(x, dict) and x.get("weekly_usd") not in (None, "") for x in layers):
        return
    try:
        b["weekly_usd"] = round(WEEKS_OF_DAYS * float(b.get("daily_usd") or 0), 2)
    except (TypeError, ValueError):
        pass


def weekly_below_daily(b: Any) -> str:
    """A warning when the merged budget's weekly cap is set below its daily cap ('' when not)."""
    try:
        day, week = float((b or {}).get("daily_usd") or 0), float((b or {}).get("weekly_usd") or 0)
    except (TypeError, ValueError, AttributeError):
        return ""
    if day > 0 and 0 < week < day:
        return (f"budget.weekly_usd ${week:g} is below budget.daily_usd ${day:g}: the weekly cap stops work "
                f"before a day's spend is reached; raise it or remove it (unset: {WEEKS_OF_DAYS} x daily)")
    return ""


def layered(raw: dict) -> dict:
    """A project's settings: the defaults, then the account-level ones, then its project.json (a section
    holding no object read as absent: bad_sections). budget.timezone set nowhere is the project's home zone (UTC without one)."""
    account = load_account_settings()
    cfg = deep_merge(DEFAULT_CONFIG, _copy_tree(account))   # the merge below would write into account's sections
    _merge_into(cfg, _without_bad_sections(raw))
    for dotted in HOST_WIDE_KEYS:
        sec, _, key = dotted.partition(".")
        if isinstance(cfg.get(sec), dict):
            if key in (account.get(sec) or {}):
                cfg[sec][key] = _copy_tree(account[sec][key])
            else:
                cfg[sec].pop(key, None)
    _chat_inherits(cfg)
    weekly_default(cfg.get("budget"), account.get("budget"), raw.get("budget"))
    if isinstance(cfg.get("budget"), dict) and not cfg["budget"].get("timezone"):
        from .timefmt import zone_name
        cfg["budget"]["timezone"] = zone_name(raw)
    return cfg


# A chat worker is Claude Code too: unset, these keys take claude's value, so a routed model never
# gets the user's plugins, MCP servers or withheld connectors that claude workers are kept from.
CHAT_INHERITS = ("worker_isolation", "mcp_withheld", "loopback_ports", "connect_timeout_ms")


def _chat_inherits(cfg: dict) -> None:
    provs = cfg.get("providers")
    chat, claude = (provs.get("chat"), provs.get("claude")) if isinstance(provs, dict) else (None, None)
    if not (isinstance(chat, dict) and isinstance(claude, dict)):
        return
    for key in CHAT_INHERITS:
        if chat.get(key) is None and claude.get(key) is not None:
            chat[key] = _copy_tree(claude[key])


def account_settings_path() -> Path:
    return HOME_DIR / "settings.json"


def load_account_settings() -> dict:
    """The account-level settings, only the keys ACCOUNT_KEYS allows; {} when unreadable."""
    try:
        data = statcache.load_json(account_settings_path())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {sec: {k: v for k, v in data[sec].items() if k in keys}
            for sec, keys in ACCOUNT_KEYS.items() if isinstance(data.get(sec), dict)}


def account_value(dotted: str) -> Any:
    """An ACCOUNT_NESTED_KEYS value from the account settings, or None."""
    try:
        node = statcache.load_json(account_settings_path())
    except (OSError, ValueError):
        return None
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def account_keys() -> list[str]:
    return sorted([f"{s}.{k}" for s, ks in ACCOUNT_KEYS.items() for k in ks] + list(ACCOUNT_NESTED_KEYS))


def set_account_setting(dotted: str, value: Any) -> None:
    sec, _, key = dotted.partition(".")
    if key not in ACCOUNT_KEYS.get(sec, set()) and dotted not in ACCOUNT_NESTED_KEYS:
        raise ValueError(f"{dotted} is not an account-level setting; those are: {', '.join(account_keys())}")
    data = load_account_settings()
    for k in sorted(ACCOUNT_NESTED_KEYS | {dotted}):   # load_account_settings leaves these out: keep them
        v = value if k == dotted else account_value(k)
        *path, last = k.split(".") if k in ACCOUNT_NESTED_KEYS else (sec, key)
        node = data
        for part in path:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if v is None:
            node.pop(last, None)
        else:
            node[last] = v
    if sec != "providers" and data.get(sec) == {}:
        data.pop(sec)
    provs = data.get("providers") or {}
    for name in [n for n, e in provs.items() if not e]:
        provs.pop(name)
    if "providers" in data and not provs:
        data.pop("providers")
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    write_json(account_settings_path(), data, mode=0o600)


def secrets_path() -> Path:
    return HOME_DIR / "secrets.json"


class SecretsUnreadable(OSError):
    """The secrets store is there but cannot be read: a worker sandbox hides it, or its owner, mode or
    type is wrong."""


def load_secrets() -> dict:
    """Per-user credentials shared by every project of this user on this machine, read at run time from
    this machine's store (secretstore: secrets.json mode 0600 by default, or the keychain, a fetch
    command, the environment). Never copied into a project folder, never printed. A store that is there
    but cannot be read raises SecretsUnreadable, so a caller that needs a secret fails clearly; read-only
    status uses secrets_state."""
    from . import secretstore
    return secretstore.load()


def secrets_state() -> dict:
    """For read-only status (`ttp status`, its --json, the web state): {"readable": True, "path"}, or
    readable False with the error, never raising."""
    try:
        load_secrets()
    except SecretsUnreadable as e:
        return {"readable": False, "path": str(secrets_path()), "error": str(e)}
    return {"readable": True, "path": str(secrets_path())}


def save_secret(key: str, value: Any) -> None:
    """One slot into this machine's store; secretstore.ReadOnly where it takes no writes (a fetch
    command, the environment, or plaintext on a host marked shared)."""
    from . import secretstore
    data = load_secrets()
    data[key] = value
    secretstore.save(data)
