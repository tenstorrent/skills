# Installing tt-project

## The install line

The [README](../README.md#install) line runs `plugins/tt-project/install.sh`, which:

1. stops with a one-line message if `git` or Python 3.9+ is missing;
2. fetches one commit of `tenstorrent/skills` into a clone kept at `~/.local/share/tt-project/skills`
   (under `$XDG_DATA_HOME` when set) and runs that commit's own installer;
3. adds the clone as the `tenstorrent-skills` marketplace for Claude Code and Codex, and copies the
   plugin into Cursor's local plugin folder, for each tool it finds;
4. runs `ttp setup`, which installs `ttp` in `~/.local/bin`, and checks that `ttp --version` runs.

A failed step for one tool does not stop the others; the installer lists failures and exits 1.

To install a release tag or branch, name it in the URL and in `TT_PROJECT_REF` (until tt-project is
on the default branch, the README's line names its branch; a `HEAD` URL reports a 404 and installs
nothing):

```bash
curl -fsSL https://raw.githubusercontent.com/tenstorrent/skills/<tag>/plugins/tt-project/install.sh | TT_PROJECT_REF=<tag> sh
```

Options, set on `sh` at the end of the line:

- `TT_PROJECT_REF`: a release tag, branch or commit; default the default branch. Rerunning with the
  same ref installs what it points at now: a branch or a moving release tag gives its latest commit,
  a fixed tag that exact release.
- `TT_PROJECT_REPO`: another git URL to install from, such as a fork or a local mirror.

Rerun the line to update or switch refs; it updates the clone and the same entries in place.
`sh <checkout>/plugins/tt-project/install.sh` installs the commit a checkout has; a downloaded
`install.sh` run on its own fetches the ref like the piped line.

## Loading it

- Claude Code: start a new session, or run `/reload-plugins` (and `/reload-skills` for `/tt-project`).
- Codex: start a new session.
- Cursor: restart it, or run **Developer: Reload Window**. On Teams and Enterprise plans an admin
  must allow local plugin imports.
- `ttp`: needs `~/.local/bin` on `PATH`; if it is not, the installer adds it to your shell profile
  and prints the command for the current shell. `ttp uninstall` removes that line. That `ttp` runs
  under Python 3.10 or newer, which `ttp checks` needs for its pinned env: `$TTP_PYTHON`, else the
  Python `ttp setup` ran under, else the newest `python3.x` on `PATH`. With only a 3.9 it still runs
  and says why `ttp checks` cannot; with none, it says what it needs and exits 127.

## Skills

To call a skill directly instead of just asking for a project:

| Host | Main skill | Harness skill | Device box skill |
|---|---|---|---|
| Claude Code | `/tt-project` | `/tt-project:tt-project-harness` | `/tt-project:setup-box` |
| Codex | `$tt-project:tt-project` | `$tt-project:tt-project-harness` | `$tt-project:setup-box` |
| Cursor | `/tt-project` | `/tt-project-harness` | `/setup-box` |

The device box skill gets a box (bare metal, a reservation or a Slurm job) where agents work in
their own checkout over ssh; site values stay in a local `~/.tt-project/boxes.json`.

In Claude Code the plugin's own entry is `/tt-project:tt-project`; `ttp setup` adds a personal
shortcut skill, `~/.claude/skills/tt-project/SKILL.md` (under `$CLAUDE_CONFIG_DIR` when set), listed
as `/tt-project`. It leaves an existing skill of that name alone; `ttp setup --no-shortcut` removes it.

## Manual install

The same steps by hand. Workers also need `git` with `user.name` and `user.email` set and a
logged-in agent CLI. Set `TT_PROJECT_REF` to a release tag or branch.

Claude Code (a branch or tag goes after `#`):

```bash
export TT_PROJECT_REF=<release tag or branch>
claude plugin marketplace add "https://github.com/tenstorrent/skills.git#$TT_PROJECT_REF"
claude plugin install tt-project@tenstorrent-skills
claude plugin details tt-project    # Skills (3): setup-box, tt-project, tt-project-harness
"$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Codex (`--ref` takes a branch or tag):

```bash
codex plugin marketplace add tenstorrent/skills --ref $TT_PROJECT_REF
codex plugin add tt-project@tenstorrent-skills
"$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Cursor installs plugins from a team marketplace or a local folder, and neither takes a git ref, so
check out the ref with `git` and copy the plugin into Cursor's local plugin folder (Cursor skips a
symlink there that points outside that folder):

```bash
git init -q tt-skills-release && git -C tt-skills-release fetch -q --depth 1 https://github.com/tenstorrent/skills.git "$TT_PROJECT_REF" && git -C tt-skills-release -c advice.detachedHead=false checkout -q FETCH_HEAD
mkdir -p ~/.cursor/plugins/local
cp -R tt-skills-release/plugins/tt-project ~/.cursor/plugins/local/tt-project
~/.cursor/plugins/local/tt-project/bin/ttp setup
```

Then restart Cursor (or run **Developer: Reload Window**).

## Update

Rerun the install line. After a manual install, remove the marketplace and repeat the steps above
with the new ref, `ttp setup` included:

```bash
claude plugin marketplace remove tenstorrent-skills    # also uninstalls its plugins
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
```

Each `ttp setup` (and so each automatic upgrade) also keeps Claude Code on the release it installs:
`~/.tt-project/lib/current` is a one-plugin `tenstorrent-skills` marketplace, and a
`tenstorrent-skills` directory marketplace that holds an older tt-project is repointed there (the
settings files are backed up first, as `<file>.ttp-backup-<time>`), then the plugin cache is
refreshed with `claude plugin update`. It leaves other marketplaces alone, and also a
`tenstorrent-skills` registration that serves another installed or enabled plugin: rerun the
install line for that one.

When you follow a branch, Claude Code can also update in place (then run `ttp setup` as above):

```bash
claude plugin marketplace update tenstorrent-skills && claude plugin update tt-project@tenstorrent-skills
```

Running projects take in the new release within an hour and keep their work.

## Uninstall

`ttp uninstall` (`--dry-run` first to see each step) stops and removes ttp's services, crontab lines,
shortcut, command and lib. It keeps project folders and linger, and prints the commands that remove the rest.

Then remove the plugin and the per-user files it does not touch:

```bash
claude plugin marketplace remove tenstorrent-skills
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
rm -rf ~/.cursor/plugins/local/tt-project
rm -rf ~/.tt-project "${XDG_DATA_HOME:-$HOME/.local/share}/tt-project"    # registry, saved keys, the install line's clone
```

Each project keeps its files in `<project root>/tt-project/`; delete that folder too to drop its
history. If you ran `ttp notifier install`, run `ttp notifier uninstall` first to remove its user service.

## Known issues

- Cursor install is verified by clone and `ttp setup` only, not end to end. The Cursor agent CLI installs in user space with its documented installer; it has not run live here yet (see [providers.md](providers.md#open-gaps)).
- The setup-box IRD route is tested against a fake `ird`; a live run needs a network that reaches the IRD service.
- The setup-box new-box flow (`new-box.md`) is tested with fakes only; its install steps have not yet run on a live box.
- Codex and Cursor spend is estimated from a price table (see [Limits](guide.md#limits)).
- Not on Codex: worker plugins. Not yet on Cursor: worker plugins, worker isolation, mid-run coordinator updates.
- Older Cursor builds print nothing until a run ends, so set `stall_s` above their longest run.
