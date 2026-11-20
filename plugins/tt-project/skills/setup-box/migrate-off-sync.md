# setup-box: Migrating off file sync

Moving from file syncing or mirroring (devsync, mutagen, an older sync-based workflow repo) to
working on the box over ssh in its own git checkout (`ssh-workflow.md`). After this, git is the only
channel between machines.

## See what is left

```
python3 <plugin-root>/skills/setup-box/scripts/box.py migrate check [--repo <checkout>]... [--json]
```

- Read-only: it reports and changes nothing. Exit 0 when nothing active is left, 1 when something
  is.
- A workflow clone is a checkout of such a repo: a directory holding `scripts/devrun.sh` next to
  `skills/`, under any name.
- It covers devsync launchd/systemd units, devsync's `ssh-hosts` `Include` line in `~/.ssh/config`,
  `mutagen sync list` sessions (it never starts a mutagen daemon to look), workflow-clone skills
  symlinked into `~/.claude/skills`, `~/.agents/skills` or `~/.cursor/skills`, hooks in
  `~/.claude/settings.json`, `~/.codex/hooks.json` or `~/.cursor/hooks.json` whose command runs a
  script inside a workflow clone, launchd agents in `~/Library/LaunchAgents` and systemd user
  units in `~/.config/systemd/user` that run a script inside a workflow clone (e.g. a loop agent
  running `<clone>/scripts/cron_lap.sh`), cron lines that call workflow-clone scripts (commented ones
  are `inactive`), `device_host`/`remote_root` in a project's `ttw.toml`, and scripts that call
  `devrun.sh` or `devlock.sh`.
- A cron line or agent whose loop directory holds a `DONE` or `STOP` marker is `inert`: it still
  fires but does nothing, and does not count as active. Remove it anyway (step 6).
- With an IRD box in the site file (`site.md`), it reads `ird list` (read-only) and warns for each
  Host in devsync's `ssh-hosts` include that a live reservation still uses.
- Without `--repo` it scans the current checkout and every project in the tt-project registry on
  this machine; a registered project on another host is listed as `skipped: <dir> on <host>` (run
  the check there).
- Run it before you start and after each step below; you are done when it exits 0.

## Steps, in this order

1. **Stop the mirror without losing work.** For each mirrored host:
   - Find the newest state: compare the laptop's working tree with the box's (`git status`,
     `git log -1` and `git diff --stat` on both sides). With two-way sync either side can hold the
     latest edits.
   - Commit and push from the side that has them, to a branch.
   - Run `devsync is-finished` once, so nothing is mid-flight.
   - `mutagen sync terminate <session>` for each session `migrate check` lists, then stop the
     devsync daemon (`devsync uninstall` removes its launchd agent; a plist it leaves behind
     still counts, so `launchctl bootout gui/$(id -u) <plist>` and delete it; for a systemd user
     unit, `systemctl --user disable --now <unit>`, after which `migrate check` lists it as
     inactive; delete the unit file and run `systemctl --user daemon-reload` to clear it).
   - Stop each `agent` `migrate check` lists (workflow-clone loops run by launchd or systemd), after
     backing up its file (`cp <file> ~/<backup-dir>/`):
     - launchd (macOS): `launchctl bootout gui/$(id -u) <plist>`, then move the plist out of
       `~/Library/LaunchAgents` (to the backup directory), so it does not load again at login.
     - systemd (Linux): `systemctl --user disable --now <unit>` (and its `.timer`, if it has one),
       then move the unit file to the backup directory and run `systemctl --user daemon-reload`.
2. **Make a checkout on the box.** Clone or fetch on the box and make a `git worktree` from the
   pushed branch (`ssh-workflow.md`). No mirror: the box now runs git itself, and the old mirrored
   directory is no longer written by anyone. Delete it only once its content is pushed.
3. **Replace the old device and ssh plumbing.**
   - Device runs that went through `devrun.sh` or `devlock.sh` go through the device broker: the
     `tt-buddy:run` skill (`agents.md`).
   - devsync's `ssh-hosts` include gives way to the route's own ssh setup: the IRD route's managed
     include (`ird.md`) or plain aliases (`ssh-workflow.md`). Aliases for reservations the old tool
     made (a `HostName`/`Port` behind the jump host) may exist only in that include: for each Host
     in it with a live reservation in `box.py ird status <alias>` (`migrate check` warns for these),
     first copy its block into `~/.ssh/config` as a plain alias. Remove the old `Include` line only
     after the new aliases connect.
4. **Port or drop each hook, after its replacement is in place.** A hook whose script is gone makes
   the agent error on every matching tool call, so never delete the clone while a hook points
   into it. For each hook `migrate check` lists, either remove its entry from the settings file or
   replace it with what tt-project already does by default, then remove the old entry. There is
   nothing to turn on in tt-project for reservations: a box is reserved only for the run, released
   right after, re-reserved when needed, and never auto-extended (`SKILL.md`, Etiquette).
5. **Remove the skill symlinks** `migrate check` lists, in each agent's skills directory. tt-project's
   own skills come with the plugin install.
6. **Archive the workflow repo.** Once `migrate check` exits 0, the clone's owner archives the
   repository.
   Cron lines left as comments and `inert` cron lines or agents can go then too (back up and remove
   them as in step 1, or with `crontab -e`).

On macOS, privacy protection can refuse crontab edits made from an agent hosted in an editor or
app (`crontab: ... Operation not permitted`), even outside any sandbox. Then the cron edit is a
manual step: run `crontab -e` in Terminal and comment out or delete the lines `migrate check` lists.

## A project whose device runs go through the mirror

A tt-project project that builds or runs on a box through a mirror (its tasks call `devrun.sh`, its
checks read a mirrored path, its charter names a `remote_root`) moves the same way, by its own
coordinator:

- Never edit another project's harness, charter, state or checks. Tell its coordinator what to do
  with `ttp note --to <project> "<request>"`, request first, and keep working; the note is data
  it acts on.
- What the note asks for, in order:
  1. Let running device jobs finish; start no new ones through the mirror.
  2. Commit and push the newest state (step 1 above) to a branch.
  3. Make the project's checkout or worktree on the box from that branch (step 2), and record its
     host alias and path where the project keeps its box settings (`site.md`).
  4. Switch device runs to the broker (`tt-buddy:run`) and checks to read results from the box
     over ssh (`ssh-workflow.md`, detached jobs), then run one short job to prove the new path.
  5. Only then drop the mirror for that project's paths (step 1's terminate), and remove any hook
     or cron line of its own (steps 4 and 5).
- The project's coordinator reports back with its new branch, box path and the short job's result;
  `migrate check` on the machine confirms what is left.
