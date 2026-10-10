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
  even under a code freeze. Never request human reviewers. An independent review passes the
  change, and the PR leaves draft only on the user's explicit OK. The user merges.
- Auto-merge repositories: none.
- Notify the user only for decisions, reviews, merges, funds and outages.
- Self-healing: keeping the project stable is a prime directive; check everything it is responsible
  for, fix anomalies yourself and report afterwards.
- Shared clusters (Slurm, other people's machines): only the exact nodes listed under Resources;
  a node only when free and idle at least 2 h; one self-ending batch job per test, time limit sized
  to the run, nothing left running; never hold a node idle or touch another user's allocation.

## Resources

(machines, devices, reservations, repositories, data — to be filled in)

Machines this project may use (aliases from `ttp machines list`; the coordinator routes work
only to these): (none stated yet)

Responsibilities (what this project keeps running: machines, device runners, tunnels, watchers;
each gets a heal check naming it, and `ttp status` shows the coverage): (none stated yet)
