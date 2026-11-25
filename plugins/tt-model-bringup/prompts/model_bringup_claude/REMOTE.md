# Running and watching runs from another machine: what we did

The first runs (Nov 2026) ran on several QB2 hosts at once. One admin Claude session on a separate machine set them
up, watched them and stepped in when a host failed. Nothing here is required; it is what worked, for your own
setup to borrow from.

## Starting a run over ssh

- An ssh command's shell often has a shorter PATH than a login shell; `~/.local/bin` (where `claude` installs) was
  missing. The scripts now add it, but your own wrappers may need it too.
- A launch over ssh can hang because the background processes it starts keep the connection open. Start them with
  `setsid nohup ... >/dev/null 2>&1 < /dev/null &`, or run the launch itself that way.
- Give each run its own folder: a Claude config dir (`--config-dir`), a tmux server (`TMUX_TMPDIR=<run>/tmux`), and
  `HF_HOME`. Then runs on one host share nothing: no settings, memory, CLAUDE.md, or tmux environment. A tmux server
  that is already running does not pass a new caller's environment to its sessions, so set the variables in the
  session's command (`launch.sh` does).
- Use one notes file per run for host facts: other users or CI on the host, known faults, where tools live.

## Watching without disturbing

- Use read-only looks: `TMUX_TMPDIR=<run>/tmux tmux capture-pane -pt <session> -S -40`, `overseer.log`,
  `git log --oneline` in the checkout, and the transcript tail under `<config dir>/projects/`. Attaching to the tmux
  session makes it easy to type into the agent by accident.
- A small loop on the watching machine checked every few minutes that each run's tmux session and `claude` process
  still existed. It alerted on the first miss and sent a heartbeat every few hours. It caught both lost sessions
  below within minutes.
- A fresh read-only `claude -p` every hour wrote a short status per run (stage, latest results, hours used, anything
  that needs a person) to read on a phone. It is separate from the run's own overseer, which reports to the run.
- Before telling a run anything, decide whether it needs to know. A run that works is best left alone.

## When a session or host is lost

- Find out what happened before resuming:
  - `journalctl` around the time for `systemd-oomd` (it kills the whole tmux scope);
  - `last -x` and `journalctl --list-boots` for a reboot; a log that just stops means a hard freeze;
  - `tt-smi` to check the boards came back.
- Resume as in `RESUME.md`, and also bring back what the loss killed:
  - the tmux session on the run's own server and with its config dir;
  - the overseer with the original `--started-at`;
  - the budget timer, set to fire at the original deadline rather than a new 48 h.
- Give the resumed session a short note: what happened, what was lost (running jobs, the server, `/tmp`), how much
  memory is free if memory was the cause. Then let it check what was in flight.
- If a host keeps failing, move the run (`RESUME.md`, another host) rather than resuming into it again.

## Hosts that are also CI runners

- Never touch the runner service, kubelet/rke2, or CI containers and images. `pgrep -f`/`pkill -f` patterns can
  match their own command line, and the CI's, so use exact names (`-x`).
- Before leaving a host, remove what you added: login tokens, the HF token, ssh keys you installed. Then check
  nobody else's keys or files went with them.
