# {{NAME}} — charter

Created {{DATE}}. The coordinator keeps this file current; the user's words win on conflict.

## Brief (verbatim from the user)

{{BRIEF}}

## Goals and success criteria

(to be restated by the coordinator from the brief)

## Restrictions (binding on every task)

(none stated yet)

## Policies

- Pull requests: opening and updating draft PRs is always allowed and needs no permission or ask,
  even under a code freeze (`delivery.drafts_during_freeze`; turn it off if a freeze covers drafts
  too). Never request human reviewers. An independent review passes the change, and the PR
  leaves draft only on the user's explicit OK. The user merges.
- Auto-merge repositories: none.
- Notify the user only for decisions, reviews, merges, funds and outages.
- Ask the user only when a step could harm people (destroy others' work, disturb shared machines)
  or speaks as the user (a post in their name, a PR leaving draft, a merge). Decide the rest
  (inputs, space, routes, reruns): find an accessible copy or a disk with room, act, record it.
- Self-healing: keeping the project stable is a prime directive; check everything it is responsible
  for, fix anomalies yourself and report afterwards.
- Shared clusters (Slurm, other people's machines): nodes only through the scheduler, within any
  nodes, partitions or limits under Resources (beyond them only on the user's word); never a node
  allocated, drained or reserved for others; one self-ending batch job per test, time limit sized
  to the run, released when done, nothing left running; never hold a node idle or touch another
  user's allocation.
- Change windows: none. A team that only allows disruptive work (restarts, rollouts, anything
  that disturbs others) in set windows lists them here instead, with their time zone; the
  coordinator then schedules such work into the next window.

## Resources

(machines, devices, reservations, repositories, data — to be filled in)

Machines this project may use (aliases from `ttp machines list`; the coordinator routes work
only to these): (none stated yet)

Responsibilities (what this project keeps running: machines, device runners, tunnels, watchers;
each gets a heal check naming it, and `ttp status` shows the coverage): (none stated yet)
