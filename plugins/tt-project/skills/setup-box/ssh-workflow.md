# setup-box: ssh workflow

## Aliases and the jump host

- Reach every box by an ssh config alias, the same name as its `ttp machines` alias. Never put a
  raw hostname in a prompt, task or repo.
- A box behind a jump host gets `ProxyJump` in the user's `~/.ssh/config`:

```
Host <jump-alias>
    HostName <jump-host>
    User <user>

Host <box-alias>
    HostName <box-host>
    User <user>
    ProxyJump <jump-alias>
```

- The site file names the jump alias (`jump`), so tools can check it: `site.md`.
- Ask the user before editing their ssh config. Host key checking stays on everywhere:
  `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null` are not allowed. A box with a
  stable host key (bare metal, cluster login and compute nodes) has its key verified by the user
  once, in their own `known_hosts`. IRD reservations are fresh containers with a new key each
  time: their managed ssh block trusts the first key per reservation, in a file only that
  reservation uses and deleted with it (`ird.md`).

## Many short commands

- Reuse one connection for many short commands:

```
Host <box-alias>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

- Every scripted call uses `-o BatchMode=yes -o ConnectTimeout=15`, so a missing key or a dead
  host fails at once instead of waiting on a prompt. `scripts/box.py` does this in `ssh_run()`.
- Exit 255 is ssh itself failing (unreachable, auth); any other code is the remote command's.

## Long work: detached on the box

Run builds and device jobs detached on the box, never in a held ssh session:

```
ssh <box-alias> 'mkdir -p <workdir>/runs/<task> && cd <workdir>/runs/<task> &&
  setsid nohup bash -c "<command>; echo \$? > job.rc" > job.log 2>&1 < /dev/null &'
```

- The task hands off `waiting` with a `retry_when` that reads the rc file and exits 0 once it
  exists, 1 while it does not, 255 while ssh fails:
  `ssh -o BatchMode=yes -o ConnectTimeout=15 <box-alias> test -e <workdir>/runs/<task>/job.rc`
  (or `ttp detach --remote <box-alias> ...` and the `retry_when` it prints).
- On waking, read `job.rc` and the tail of `job.log`; quote the last 20 lines before diagnosing a
  failure.
- Copy a script to the box under a temporary name and `mv` it into place; never overwrite a
  running script.

## Checkouts and worktrees on the box

- One clone per repository under `<workdir_root>/<repo>`; one `git worktree` per task:
  `<workdir_root>/<repo>-wt/t<task-id>` on branch `ttp/t<task-id>-<slug>`.
- Fetch before branching: `git -C <workdir_root>/<repo> fetch origin`.
  In a shared clone, `git fetch` moves its `origin/*` refs (the clone then shows "behind N"), but
  never moves its checkout.
- Commit and push from the worktree, or report `git -C <path> rev-parse HEAD` and a diff.
- Clean up when the task ends: remove only your own worktree,
  `git -C <workdir_root>/<repo> worktree remove <own path>`, once everything is pushed or reported.
  Never run a bare `git worktree prune` on a shared clone: it deletes the git records of every
  worktree whose folder it cannot see (another user's, one outside your view), and git cannot make
  them again. Pruning stays with the clone's owner. Keep build trees others reuse.

## Keys and agents

- Never copy a private key to a box, into a prompt, or into a repo. The box uses its own key or a
  deploy key the user set up.
- Use agent forwarding (`ForwardAgent`) only where the user's ssh config already sets it; never
  add it.
- A script copied to another box finds its location from its own directory (`$(dirname "$0")`,
  `Path(__file__)`), never from a hard-coded absolute path.
