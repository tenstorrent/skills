# setup-box: set up a new box

A fresh box gets set up, not only checked. First ask the user what they want: not every box
needs a broker, a workspace or agent CLIs. Nothing is assumed: a suggested answer is offered, and
an unanswered question stays open.

The box needs its site entry first (`site.md`). Every command below is
`<plugin-root>/skills/setup-box/scripts/box.py`, shortened to `box.py`.

## 1. Ask

```
box.py setup journalcheck <box-alias> --save         # first: skips the journal question when readable
box.py setup questions <box-alias>                    # JSON batches for a question tool
box.py setup questions <box-alias> --format numbered  # a plain numbered prompt
```

- Claude Code: ask each batch with its multiple-choice question tool (`AskUserQuestion`): each
  batch has at most 4 questions of 2-4 options. Use each question's `header`, `question`,
  `multiSelect` and option `label`/`description` as given; map the picked labels back to their
  `value`. The tool's free-text choice is how the user types a value (a branch, a project name, a
  number of GB). A "Type it" option means: use the free-text choice.
- Agents without such a tool (Codex, Cursor): show the numbered prompt as it is and read the
  reply (`1 b`, `3 a,c`, or a typed value).
- Ask again only for follow-ups: run `questions` after each save; a follow-up (the broker's
  release or branch, the workspace's branch and ccache, the recovery fallback) appears once its
  question is answered.

| Question | Options |
|---|---|
| Login user | your login on the box; default the same name as on your other machines |
| First login | none (your login works), or the admin or site account's ssh alias |
| uid/gid, Move work | where your uid/gid come from; paths in the admin account to move (follow-ups) |
| ssh keys, Outbound | public keys for authorized_keys; your own outbound keys to restore, or none |
| Device broker | install tt-device-mcp (which release or branch), or none: device work then takes `ttp lock <box-alias>-device -- <command>` |
| Host checks (read-only) | any of driver, firmware, hugepages, PCIe link |
| Journal | add your login to the systemd-journal group so tt-project's reboot notices name what rebooted the box, or leave it; always asked, never done silently, skipped when `journalcheck --save` finds the journal readable |
| Shared mount | none, or a mount point for shared network storage; its source, type, options and how it persists (follow-ups) |
| Workspace | a repo checkout and build under `workdir_root` (branch, ccache), or none |
| Python env | pip ttnn, the workspace's own env, or none |
| Agent CLIs | Claude Code, Codex, Cursor agent, or none; suggested: Claude Code and Codex |
| CLI logins | log them in now (the user enters a code in their browser), or later (follow-up) |
| Editor | Cursor, VS Code, both, or none: how the user opens the box over Remote-SSH |
| Extensions, New builds | the Claude Code and Codex extensions on the box's editor servers, and `remote.SSH.defaultExtensions` in the user's editor settings on this machine; suggested yes (follow-ups) |
| Projects | which of the user's projects may use the box |
| Recovery | the project that recovers it, and a fallback |
| Device lock | shared by all the user's projects, or per project |
| Disk guard | free GB to keep under `workdir_root` (0 = off) |

## 2. Save

```
box.py setup save <box-alias> --set broker=none --set host_checks=driver,pcie ...
box.py setup save <box-alias> --json '{"broker": "install", "broker.ref": "latest-release"}'
```

- Answers go into the box's site entry (`setup`), with a fingerprint of each question.
- A rerun of `questions` asks only what is new, what changed wording or options since it was
  answered, and the follow-ups of an answer that changed. `--all` asks everything again.
- Lists are comma-separated; `none` alone clears a multi-choice answer.

## 3. Act, then check each answer

```
box.py setup plan <box-alias>
```

It refuses while a question is open. Otherwise it prints one step per answer: an action and the
check that proves it. Do each action, run its check, and report both. A failed check is reported,
not skipped.

- Account: first, so everything after it is set up in the user's own account, never in the admin
  or site account access came through: `account.md` (create it with the same uid/gid, keys,
  linger, move earlier work, write the box entry). Run `plan` again after `account entry`: the
  later steps then use the new `workdir_root`.

- Broker, install: the tenstorrent/tt-device-mcp repository's documented installer and upgrade
  path at the chosen release or branch; never hand-edit an installed broker, and never start a
  second one. Check: `box.py bare check <box-alias>` shows the broker present and active.
- Broker, none: no broker is installed. Each command that touches the device runs as
  `ttp lock <box-alias>-device -- <command>`, held only for that command (`bare-metal.md`).
- Host checks: `box.py setup hostcheck <box-alias> [--only driver,pcie]` reads sysfs, /proc and
  `/dev/tenstorrent` listings only; it opens no device. Exit 0 ok, 1 a problem, 255 ssh failed.
  Fixes (driver, firmware, hugepages): tt-buddy's `recipes/developer-setup.md`.
- Journal, add: `sudo usermod -aG systemd-journal <login>` on the box (`sudo -n`; if it needs a
  password, the user runs it once). It applies from the next login, and to the running tt-project
  daemon from the next reboot: never reboot a shared box for it. Check with
  `box.py setup journalcheck <box-alias>` (exit 0 readable, 1 not, 255 ssh failed).
- Shared mount: `box.py setup mount <box-alias> --run` makes it persistent (fstab or a systemd
  unit), `box.py setup mountcheck <box-alias>` verifies it now and `--after-reboot` once the box
  rebooted; record the change with `ttp machines change add`: `storage.md`.
- Workspace and Python env: tt-buddy's `recipes/workspace.md`, `recipes/tt-metal/build.md` and
  `recipes/tt-metal/env.md`, plus the notes in `bare-metal.md` (submodules, ccache, build in
  place). A broker job needs an env too: the `tt-buddy:run` skill's `skills/run/mcp.md`.
- Agent CLIs, logins and editor extensions: `box.py agents install|login|editor-defaults`, checked
  with `box.py agents check <box-alias>`: `agents.md`. The user only enters a login code.
- claude.ai connectors (Atlassian, Google): `box.py connectors check <box-alias> --verify`; an
  account switch only on the user's yes: `connectors.md`.
- Projects: the box goes into each named project's charter Resources; the plan does not edit
  charters.
- Recovery, device lock and disk guard: `ttp machines add <box-alias>` with `--owner`/`--fallback`,
  `--shared <box-alias>-device` or `--unshared`, and `--min-free-gb`.

If tt-buddy is not installed, name it in the hand-off as the plugin that holds these recipes.

## Hand-off

As in `SKILL.md`, plus each plan step with its check's result.
