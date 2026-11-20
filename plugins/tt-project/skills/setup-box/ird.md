# setup-box: IRD reservations

IRD reserves a Tenstorrent machine for a set time and gives you ssh into a container on it.
IRD may be deprecated: prefer Slurm where your site has it (`slurm.md`).

## Reach

- IRD is reachable only from networks that can reach the IRD service (e.g. the corporate VPN).
- `ird` runs only on the management host. Inside a reservation container it allows only
  `list-machines`, `run` and `sync`, so every call goes from the project box:
  `ssh -n -o BatchMode=yes <ird-mgmt-alias> ird ...`.
- `box.py ird` checks first that the management host's ssh hop has a DNS name and answers on its
  ssh port. If not, it says so and exits 255 instead of hanging; run it from a host that can
  reach IRD.

## Site entry

```json
"<box-alias>": {
  "route": "ird",
  "workdir_root": "/<workdir>",
  "ird": {"mgmt": "<ird-mgmt-alias>", "pools": ["<pool>"], "user": "<your-ird-user>", "arch": "<arch>"}
}
```

`ird.mgmt` is the management host's ssh alias, `ird.pools` the pools you may reserve from (first
is the default). `ird.user` is optional: without it `box.py` asks the management host (`id -un`).
`ird.arch` is optional: the arch `ird_e2e.sh` reserves when no `--arch` is given.
`excluded_nodes` and `demo_nodes` are never offered or reserved. See `site.md`.

## Reserve only for the run

Reserve, use, release right after, and re-reserve when needed. Never auto-extend, and never hold
a machine idle between runs.

```
S=<plugin-root>/skills/setup-box/scripts/box.py
$S ird list <box-alias>                 # machines you can reserve now (--all: skipped ones and why)
$S ird reserve <box-alias> --arch <arch> --timeout <hh:mm:ss> [--pool <pool>] [--machine <m>] [--ssh-port <p>]
$S ird status <box-alias>               # your reservations, their claims and expiry
$S ird release <box-alias> <id>         # right after the run
```

- `list` skips machines that are draining, down, failed or pending a reboot (`Reboot ASAP`), and
  busy ones (`alloc`).
- `reserve` runs `ird reserve --cluster <pool> --timeout <hh:mm:ss> [--machine <m>]
  [--ssh-port <p>] --no-shell <arch>`. Every flag goes before the arch: some `ird` builds reject a
  flag after it and silently never reserve. Reserves sharing a state dir run one at a time.
- The outcome comes from `ird list`, never from `ird reserve`'s exit code alone: `ird` can exit
  non-zero after the reservation completed, or be cut off. `box.py` snapshots your reservations
  first, diffs a fresh `ird list` after the reserve and claims the one new reservation (claim
  written before anything is reported). A non-zero exit with a new reservation is a success with a
  warning; no new reservation is a failure. That includes ssh dropping mid-reserve (exit 255):
  `box.py` retries `ird list` a few times, and if it stays unreachable it says a reservation may
  have landed and to check `status`. If several new reservations of yours could be this
  one (another task reserved at once), it narrows by `--ssh-port` and the machine `ird` says it
  reserved; if still several, it claims and releases none and says so: check `status`, retry with
  `--machine`, and release by hand what is yours and unused.
- The machine is the one in the new `ird list` row, not the one asked for: `ird` may place the
  reservation elsewhere. Everything after (claim, ssh block, the printed line) uses `<B>`. When
  `--machine <A>` reached `ird`, `reserve` warns `asked for <A>, ird reserved <B>`; when it
  reserved without `--machine` (below), it only notes `ird reserved <B>: use <B>`. Names are compared ignoring case and a
  site prefix or domain suffix, since `ird list-machines` and `ird list` may print them differently.
- `--machine` takes either form. `ird reserve --machine` needs the `ird list` form and places the
  reservation anywhere when given another, so `reserve` maps the name, learning the prefix from
  machines both lists name. When it cannot tell (nothing in `ird list`, or the lists disagree),
  it warns and reserves without `--machine`.
- `ird` may print stderr lines about the management host's environment (changing ownership of
  your home directory, skipping a CA bundle while rehashing certificates). They say nothing about
  the reservation: `reserve` shows them as `warning: ird: ...`.
- `--wait` (default 270 s) caps how long `ird reserve` may take. On a timeout, Ctrl-C, SIGTERM or
  SIGHUP, `reserve` diffs `ird list` again and releases a reservation that landed (claiming it
  first), then exits 1 (timeout) or 130 (signal). One can still land after it exits: check
  `$S ird status <box-alias>` before reserving again.
- Set `--timeout` to the run's expected length plus a small margin.
- It prints `reserved <id> machine=<m> port=<p> expires=<time>`. Put the id and expiry in the
  task's hand-off (`jobs` in SKILL.md), and release before the hand-off unless the run continues.
- A run that turns out longer: `$S ird extend <box-alias> <id> <minutes>`, on demand from that
  task only, never from a daemon, hook or timer. `ird change-timeout` sets the time left rather
  than adding to it, so `extend` asks for the time left plus `<minutes>`, then reads the time left
  back from `ird list`. It prints `extended <id> by <minutes> min: expires=<time> (was <time>, ...)`
  and exits 1 when the expiry did not move forward by the minutes asked, when `change-timeout`
  fails (its exit code and output), or when an expiry cannot be read.

## ssh to the reservation

`reserve` writes one block per reservation to `~/.tt-project/ird/ssh-hosts`
(`$TTP_IRD_DIR/ssh-hosts`), and `release` removes it:

```
Host <machine>
    HostName <machine>
    Port <port>
    ProxyJump <ird-mgmt-alias>
    UserKnownHostsFile "~/.tt-project/ird/known_hosts/<box-alias>.<id>"
    StrictHostKeyChecking accept-new
```

- `~/.ssh/config` needs `Include ~/.tt-project/ird/ssh-hosts` at its top. `reserve` says when it
  is missing: ask the user to add it (`ssh-workflow.md`); never edit their ssh config yourself.
- Host keys: each reservation is a fresh container, so the machine's host key changes from one
  reservation to the next, even on the same machine and port. A key verified once would fail the
  next reservation, and a shared `known_hosts` would collect stale keys. So each reservation gets
  its own empty `known_hosts` file under `~/.tt-project/ird/known_hosts/` (keyed by box and
  reservation id), and the block trusts the first key seen on it (`accept-new`). That first
  connection runs only through the authenticated jump host, and a changed key within the
  reservation is still refused: checking stays on. `release` (and the e2e trap, `release --stale`)
  deletes the file with the block; `reserve` and `list` prune files no live reservation of yours
  owns. Never write `~/.ssh/known_hosts`.
- Never use `StrictHostKeyChecking=no` or `UserKnownHostsFile=/dev/null`.
- Then work as on any box: `ssh <machine>`, checkout or worktree under `workdir_root`
  (`ssh-workflow.md`). Never sync or mirror files to it.

## Ownership and claims

- `box.py ird` acts only on reservations whose user column in `ird list` is yours. It never
  releases, extends or reboots anyone else's.
- `reserve` writes a claim per reservation in `~/.tt-project/ird/claims/`: project
  (`$TTP_PROJECT`), task (`$TTP_TASK`), owner pid (the run's `runner.pid`) and a heartbeat.
- `release <id>`, `extend` and `reboot` refuse a reservation with no claim (made by hand or
  elsewhere), one claimed by another project, and one with a live claim of another task. A claim
  is live while its heartbeat is newer than `--ttl` (default 1 h) or its owner pid still runs.
- `release --stale [--dry-run]` releases only your own reservations with a stale claim of this
  project (left by a run that died), and drops claims whose reservation has ended. It skips the
  rest and says why. Release the others by hand if they are yours.
- No path of `box.py ird` or `ird_e2e.sh` releases, extends or reboots a reservation without a
  claim of this tool: `status` lists it as `no claim`, and every cleanup leaves it alone.

## Unwedge a machine

First triage and recover the device with tt-buddy's `skills/run/recovery.md` (triage, kill,
reset, verify). When it says a host reboot is needed:

1. `$S ird reboot <box-alias> <id>`: runs `ird reboot <id>`, which only schedules a drain-gated
   reboot that fires once the reservation ends, then releases it. `ird reboot --force` does
   nothing inside a container; do not use it.
2. Wait until `$S ird list <box-alias>` shows the machine free again (a few minutes), from a
   `retry_when`, not a sleep loop.
3. Re-reserve the same machine and port, as `reboot` prints:
   `$S ird reserve <box-alias> --arch <arch> --machine <m> --ssh-port <p> --timeout <hh:mm:ss>`.
   The ssh block keeps working.

If the machine does not come back, or the device still fails after the reboot, release and
reserve another machine.

## End-to-end test (run by the user where IRD is reachable)

`scripts/ird_e2e.sh` runs the whole route once and prints a report. Agents cannot run it when the
management host is reachable only from the user's laptop: the user runs it there (macOS or Linux).

1. Install: needs `bash`, `python3` and `ssh` (on macOS, `xcode-select --install` gives
   `python3`). Get the plugin's files: install the tt-project plugin, or clone the
   tenstorrent/skills repository; the script is `<plugin-root>/skills/setup-box/scripts/ird_e2e.sh`.
2. Fill in the site file `~/.tt-project/boxes.json` (`site.md`) with one `ird` box as above:
   `ird.mgmt`, `ird.pools`, `ird.arch` and `workdir_root` (a writable path on the reserved
   machine). Flags override them: `--box <box-alias> --mgmt <ird-mgmt-alias> --pool <pool>
   --arch <arch> --workdir /<workdir>`. There are no built-in defaults for these.
3. Put `Include ~/.tt-project/ird/ssh-hosts` at the top of `~/.ssh/config` (agents' later `ssh
   <machine>` needs it). The test itself runs every ssh with `-F <run-dir>/ssh_config`, a config
   holding only that Include, so your personal ssh config never shapes the result. When
   `<ird-mgmt-alias>` is defined only in your personal config, give its options in the site file
   as `ird.mgmt_ssh` (`site.md`), e.g. `{"HostName": "<mgmt-host>", "User": "<user>"}`: the run's
   config then defines it. Without it, an unreachable management host fails the run at once and
   says `ird.mgmt_ssh` is missing; it never falls back to your config.
4. Run `bash <plugin-root>/skills/setup-box/scripts/ird_e2e.sh --dry-run` first: it only lists
   machines and prints the rest. Then run it without `--dry-run`. Optional: `--timeout hh:mm:ss`
   (default 00:30:00), `--extend-min N` (default 15), `--repo <git-url>` (a small public repo).
5. Paste back the block from `--- ird-e2e report (paste this back) ---` to `--- end ---` (also
   saved to the `report.txt` path it prints) into your project's chat, not a public place: it
   names machines and pools.

The steps: list and pick a free, non-draining machine; reserve it (every flag before the arch,
`--wait 120`); check the managed ssh block, print the machine's effective ssh options
(`ssh -G <machine>: hostname=... proxyjump=... stricthostkeychecking=accept-new
userknownhostsfile=.../known_hosts/<box-alias>.<id>`) and ssh in through `ProxyJump`; clone the repo into a
per-run worktree under `workdir_root`, add a file, build it, run `tt-smi -s`; extend once with
`extend` and pass only if the expiry moved forward by the minutes asked (absolute times, zone
included); release and check the id is gone from `ird list` and its ssh block and `known_hosts` file are
removed.
Before reserving it snapshots your reservations. On exit, any failure, Ctrl-C or SIGTERM a trap
diffs them again and releases every new one this run claimed; a new one without this run's claim
is left alone and named in the report. The report's `reservation:` line gives the real id and
machine, and `released:` what happened to each. `reserve` warnings (machine, exit code, stderr
noise) are copied into the report.
Exit 0 pass, 1 a step failed, 2 bad usage, 130 interrupted.

## Exit codes

0 done; 1 refused or failed (the line says why); 255 the management host is unreachable.
