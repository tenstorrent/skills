# setup-box: shared network storage that survives a reboot

A network filesystem mounted by hand on a device box is gone after the next reboot, and projects on
the box lose the caches and backups they keep there. The new-box flow (`new-box.md`) asks once
whether the box should mount shared storage at boot; on yes it makes the mount persistent and
verifies it now and again after the next reboot.

`box.py` below is the script named in `new-box.md`.

## 1. Ask

`box.py setup questions <box-alias>` asks `storage` once: no shared mount, or its mount point. A
mount point brings the follow-ups. Each answer is the user's, saved in the local site file, never
taken from a repository:

| Question | Answer |
|---|---|
| Mount source | `live` (what is mounted there now, by hand) or typed, such as `<server>:/<export>` |
| Mount type | `live` or typed, such as the filesystem's type name |
| Mount opts | `defaults` or typed; `_netdev` (and `nofail` in fstab) are always added |
| Persist how | `fstab`, or `systemd` for a filesystem whose client agent service must run first |
| Client agent | systemd only: that service's unit name, or none |

Nothing is assumed: no filesystem, server, path or option is built in.

## 2. Persist

```
box.py setup mount <box-alias>          # dry run: probes the box, prints the root script, the undo and the ledger line
box.py setup mount <box-alias> --run    # runs the root script with sudo -n, then the check below
```

- `fstab`: one /etc/fstab line `<source> <mount point> <type> <options>,_netdev,nofail 0 0`.
  `_netdev` waits for the network; `nofail` lets the box boot without the mount. /etc/fstab is
  backed up first; a line already there is left as it is.
- `systemd`: a `.mount` unit (named for the mount point, as `systemd-escape --path` names it) with
  `Wants=` and `After=` on `network-online.target` and the client agent service, enabled with
  `WantedBy=remote-fs.target`.
- It never replaces another fstab entry or unit for the mount point: it stops with exit 3 and
  says what is there.
- Root goes only through `sudo -n` on the user's own login (`account.md`). Without passwordless
  sudo, nothing changes: report it to the user as a step needing their privileges, with the root
  script the dry run printed.
- Record the live change in the same run, with the line `mount` prints:
  `ttp machines change add <box-alias> "<what>" --undo "<cmd>" --until-probe "box.py setup mountcheck <box-alias> --rebooted"`.
  The probe names the stable copy `ttp setup` installs (`$TTP_HOME/lib/current`, which survives plugin
  upgrades) and the site file by its full path, so it reads the same box wherever the daemon runs it.
  Without that copy, `mount` says so: after `ttp setup`, re-point the entry with
  `ttp machines change edit <id> --until-probe "<stable box.py> --site <file> setup mountcheck <box-alias> --rebooted"`.
  The undo unmounts and removes only what was added. A successful `--run` makes this boot the
  baseline the post-reboot check compares against.

## 3. Verify now

`box.py setup mountcheck <box-alias>`: `mountpoint -q` and a read of the mount point, each under a
timeout (a hung mount is a problem, not an ssh failure), the mounted source against the saved one,
and the fstab entry or the unit enabled. Exit 0 ok, 1 a problem, 124 no answer in time, 255 ssh
failed (errors go to stderr). A first pass records the box's boot id in the site file as the
baseline; a later pass keeps it, unless the last post-reboot check failed.

## 4. Verify after the next reboot

`box.py setup mountcheck <box-alias> --after-reboot`: 0 the box rebooted since the baseline and the
mount came back, 1 no reboot yet, 2 failed after a reboot (or no first check), 124 no answer in time,
255 ssh failed. A failure makes that boot the new baseline, so a fix or a mount by hand in the same
boot never passes it: only the next reboot does.

- The change entry's `--until-probe` is `mountcheck --rebooted`, which reads only the boot id and
  never the mount: 0 the box rebooted (the entry turns due, whatever the result), 1 not yet (also no
  answer within 40 s, as when sshd accepts mid-boot), 255 ssh could not reach the box (it names the
  box on stderr, so the ledger counts it as down while it reboots, not as a broken probe). No task waits for it; never reboot a shared box only to run it.
- When the entry turns due, run `--after-reboot` and act on its exit. Never run the undo for a
  mount that is still wanted:
  - 0: keep the mount and close the entry (`ttp machines change close <id>`).
  - 1: the ledger made the entry due after 7 days with no reboot. The persistent entry was checked
    in step 3: close the entry and keep the mount. The baseline stays in the site file, so
    `--after-reboot` still proves the next reboot whenever it is run.
  - 2: fix the cause (network order, the agent service, the options), run `mount --run` again,
    close the entry and record the new line it prints, then repeat the checks.
