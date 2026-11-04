# setup-box: Bare metal

A bare-metal box is a machine you can ssh to and use directly: no reservation system or scheduler
hands it out. You work on it over ssh, in your own checkout or worktree there, through its device
broker where it has one.

## Pick a box

- Candidates are the machines in `ttp machines list` tagged `device` that the site file routes as
  `bare` (`site.md`). Your project's charter may limit which ones it uses; it never widens that.
- Read each candidate's tags and note:
  - `serving`: it runs a service for others. The tag stops nothing: only what `recovery.md`
    lists (a user-set hold or allowlist, a lock or device hold, a pause, a live tenant) does.
  - `unstable`: known to be unreliable. Prefer another box; read its note first.
  - A note with an end (`ttp machines add <box-alias> --note ... --until <when>`) holds until then;
    past it, the note is stale, so check the box instead of trusting it.
- Probe the device lock: `ttp lock --probe <box-alias>-device` exits 0 when it is free. Exit 75
  means busy or paused: hand off `waiting` with that probe as `retry_when`. Never override a pause
  or a held lock, and never lift a pause someone else set. Outside a project it answers only for a
  lock `ttp machines add <box-alias> --shared <box-alias>-device` makes machine-wide; any other exits 3 (n/a).
- Check it in one go, read-only:

```
<plugin-root>/skills/setup-box/scripts/box.py bare check <box-alias>
```

  It reports ssh, free disk under `workdir_root` against the disk guard, whether the
  tt-device-mcp broker is installed and its service active, and the lock probe. Exit 0 ready,
  1 a problem to fix, 75 the lock is busy or paused, 255 ssh failed, so it also works as a
  `retry_when`. `--json` prints the facts as one line.

## Disk guard

- The guard is the machine's own threshold (`ttp machines add <box-alias> --min-free-gb <N>`),
  else the harness default; `box.py bare check --min-free-gb <N>` overrides it for one check
  (0 = off).
- Work that builds nothing (for example a small-repo worktree) may pass a lower `--min-free-gb`
  to its check; a build keeps the guard.
- Below the guard, do not build there: free your own old worktrees and build trees, or pick
  another box. Never delete another user's or task's files to make room.

## Work on the box

- Over ssh, in a per-task worktree under the site file's `workdir_root`:
  `<workdir_root>/<repo>-wt/t<task-id>` (`ssh-workflow.md` names, creates and cleans them).
- Builds and device jobs run detached on the box with a log and an rc file
  (`ssh-workflow.md`); the task hands off `waiting` on the rc file.
- Never sync or mirror files from the project box; the repository is the only channel.

## The device: through the broker

- Where the box runs the tt-device-mcp broker, every device run goes through it. Use the
  `tt-buddy:run` skill and its `skills/run/mcp.md`; do not reconstruct their rules here.
- A broker job needs a Python env even for a non-tt-metal workspace: with no env file `run-bg`
  refuses ("Python env not found at <workspace>/tt-metal/python_env"). Set one up as in
  `tt-buddy:run`'s `skills/run/mcp.md` § Env file (TT_METAL_HOME, PYTHONPATH, PYTHON_ENV_DIR).
- `run-bg` prints `Job <id> queued (position: N)`; `tt-device-mcp wait <id>` blocks until the
  job ends.
- Never start a second broker (no `tt-device-mcp daemon start` beside a running service), and
  never stop, restart or reconfigure the box's broker. A broker that is installed but not active
  is reported, not worked around.
- Never open `/dev/tenstorrent` directly where `TT_VISIBLE_DEVICES` masks it; never unset or
  override the masking.
- With no broker on the box, take `ttp lock <box-alias>-device -- <command>` around each command
  that touches the device, held only for that command.

## Set up a fresh host

- Ask the user what the box needs, then set it up answer by answer: `new-box.md`. These are the
  recipes its steps follow.
- The broker: follow the tenstorrent/tt-device-mcp repository's documented installer and upgrade
  path. Never hand-edit an installed broker.
- Toolchain, drivers, firmware checks and cache paths: tt-buddy's `recipes/developer-setup.md`.
- Building tt-metal: tt-buddy's `recipes/workspace.md` and `recipes/tt-metal/build.md`. Notes
  those recipes do not yet carry:
  - `git worktree add` does not populate submodules: run
    `git submodule update --init --recursive` in the new worktree before building.
  - Use ccache on a build box: with ccache installed, pass `build_metal.sh`'s ccache flag (`-c`
    at the time of writing; check `build_metal.sh --help` in your checkout).
  - A prebuilt build tree or `python_env` copied from elsewhere is not portable (it embeds
    absolute paths): build in place.
- If tt-buddy is not installed: as in `new-box.md`.

## Hand-off

As in `SKILL.md`: host alias, worktree path, commit, job ids, and what still runs or is held on
the box ("nothing" after checking).
