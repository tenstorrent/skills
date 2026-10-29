# setup-box: the user's own account on a box

A box is set up in the user's own account, not in the shared admin or site account through which
access was first given. The new-box questionnaire (`new-box.md`) asks for it first; `box.py setup
plan` puts these steps before every other one. `box.py` below is the script named in `new-box.md`.

## 1. The login

- `account.user`: the user's login on the box. Default: the same user name they have on their
  other machines (the name on the project box).
- `account.admin`: `none` when ssh as the user already works. Otherwise the ssh alias of the admin
  or site account access came through (its own alias, not the box's: `Host <box-alias>` is the
  user's login).

## 2. Create the account (only through an admin login with sudo)

```
box.py account ids [here | <ssh-alias> | <path-on-box> --via <admin-alias>]
box.py account probe <box-alias> --via <admin-alias> --user <user> --ids-from <source>
box.py account create <box-alias> --via <admin-alias> --user <user> --ids-from <source> [--pubkey <file.pub>]
box.py account create ... --run
```

- uid and gid come from an existing machine (`here`, or another machine's ssh alias, read with
  `id`) or from the owner of the user's files on a shared mount (a path on the box), so ownership
  on shared storage matches.
- `probe` is read-only. It checks whether the account or its home exists as root (`sudo -n test`):
  a non-root test on another user's home fails silently.
- `create` prints what it will do and the root script; read it, then add `--run`. It runs with
  `sudo -n` through the admin login and:
  - creates the account with that uid/gid, a home and a login shell (an existing group with the
    gid is used; a missing one is created), or completes an account that already has those ids;
  - gives sudo only if the admin login has it: a sudoers.d drop-in written under a dotted temp name
    (sudo skips those), checked with `visudo -cf`, and put in place only when the check passes;
  - adds the user to the docker group if the box has one;
  - installs the user's public keys and the project host's key in authorized_keys (default: every
    `~/.ssh/*.pub` on the project box; add the user's other keys with `--pubkey`);
  - enables linger, so project daemons run as systemd user services.
- It never changes or deletes another account. It stops, changing nothing, when the login exists
  with other ids, the uid belongs to someone else, the home exists owned by another uid, or the
  admin login has no passwordless sudo for a root shell (checked with `sudo -n -l bash`, never
  `sudo -n true`; some hosts grant it only for specific commands): then the box's admin creates the
  account. Ask the user.
- The user's own login already works: `box.py account keys <box-alias> [--pubkey ...] --run` adds
  the keys as the user, and `ssh <box-alias> loginctl enable-linger` turns linger on (through the
  admin if the box refuses it).
- Outbound ssh keys (git over ssh from the box) are restored only if the user has them: the user
  copies their own key files into `~/.ssh` there, mode 600. Agents never handle private keys.

## 3. What lives where

- In the user's home: checkouts, builds, venvs, logs, the tt-project install, agent CLI logins
  (`agents.md`) and the gh login. `workdir_root` is under the home.
- System-wide, shared: the device broker, system packages, drivers, shared mounts, tool installs
  under /opt or /usr/local.

## 4. Work already done in the admin account

```
box.py account migrate <box-alias> --via <admin-alias> --user <user> --path <abs-path> [--path ...]
box.py account migrate ... --run
```

- The dry run shows each path as to move, moved, missing, or refused (its target exists), plus the
  root script. `--run` moves each path to the same place under the user's home with sudo (`mv`,
  never a delete) and chowns it to the user.
- A moved venv or build tree keeps the old absolute paths and breaks: rebuild each one the run
  lists (`rebuild:`), per tt-buddy's `recipes/tt-metal/build.md` and `recipes/tt-metal/env.md`.
- Then clean up only what setup-box itself added to the admin account (for example a user unit or
  linger it turned on there). Leave the admin's own files alone.

## 5. The box entry

```
box.py account entry <box-alias> --user <user> [--admin <admin-alias>] [--workdir <path>]
```

It writes `user`, `admin` (only when an admin login is still needed) and a `workdir_root` under the
user's home into the site entry (`site.md`): the current one if it is already there, else
`<home>/work`. The route stays as it is. The matching ssh config, after asking the user
(`ssh-workflow.md`):

```
Host <box-alias>
    User <user>

Host <admin-alias>
    HostName <box-host>
    User <admin-user>
```

## Gotchas

- A moved venv or build tree breaks: rebuild it, do not patch paths.
- A distro's packaged gh can be too old to read a newer `hosts.yml`: install gh from GitHub's own
  apt repository (its current official instructions), then `gh auth login` as the user.
