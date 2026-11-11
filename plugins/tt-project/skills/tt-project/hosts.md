# Listening for replies, per host

The listener prints each message for this chat and exits (with `--once`) or streams.
Replies to this chat plus broadcast alerts arrive. Others' replies never do.
Each line starts with the message id: `[#<id> coordinator] <text>`.

## Acknowledge what you showed

- Always pass `--ack <id>`: the highest message id you have shown the user, `0` before the first.
- Only acknowledged messages count as read. Anything printed to a chat that closed or a
  connection that dropped comes back on the next listen.
- A message can therefore arrive twice. Skip ids you have already shown.

## Remote projects and dropped connections

- For a project on another machine the listener runs there over ssh. A dropped connection is
  retried (5 s, doubling to 2 min) until it is back; each message prints once per listener.
- With `--once`, a batch that arrived before a drop ends the listener normally (exit 0).
- Exit codes: `0` messages shown or `--timeout` reached; `2` the project refused it (unknown chat
  or project); `3` a newer listener for this chat took over (do not restart this one); `75` still
  unreachable when `--timeout` ran out (nothing is lost: start it again).

## Claude Code

- Run in the background: `ttp listen <name> --chat <id> --once --ack <last id shown>`.
- Its exit wakes you. Print the messages, then start it again with the new last id.
- Always keep exactly one listener running per attached project.
- NEVER run it in the foreground. NEVER add `sleep` loops.

## Cursor

- Run `ttp listen <name> --chat <id> --ack <last id shown>` as a monitored background shell.
- Notify on output. Each line is a message; relay it.
- When the shell ends or you restart it, pass the last id you relayed.

## Codex and other hosts with no background wake-up

Nothing wakes this chat between user turns. Wait with one long blocking call, never with polls.

- At the start of each user turn, pick up what arrived meanwhile:
  `ttp listen <name> --chat <id> --once --timeout 1 --ack <last id shown>`
- When the user waits for an answer after `ttp say`, run ONE blocking call in the foreground:
  `ttp listen <name> --chat <id> --once --timeout 600 --ack <last id shown>`.
  Set the tool call's own time limit and wait (yield) time above `--timeout`, so the host does not
  hand the command back early to be polled (a host that allows less: lower `--timeout` to fit).
  It returns as soon as a message arrives, or with no output when the timeout runs out. Show what
  came, then listen once more or end your turn.
- NEVER start the listener (or any command) in the background and check its output every few
  seconds. Each check re-reads the whole chat context, and an empty check costs as much as a full one.
- Nothing to wait for: end your turn. The user can always ask "any news from <name>?".

## Any host

- Keep the chat thin: it relays, shows replies and reads `ttp status`. The project does the work;
  never read code, logs or test output here.
- When this chat's context grows large (the host warns, or a long history is re-read every
  turn), write a short hand-over: project name, chat id, last id shown, what the user asked for
  and still waits on, open questions. Continue in a fresh chat or thread from it with the same chat id
  and `--ack <last id shown>` (or `ttp connect <name>` again). Nothing is lost: the project keeps
  the orders and replies, and unacknowledged messages come back.
- Lost the chat id → `ttp connect <name>` again (new id, same project).
- Lost the last id → `--ack 0` re-shows what this chat has not acknowledged.
- Listener errors → `ttp doctor <name>`.
