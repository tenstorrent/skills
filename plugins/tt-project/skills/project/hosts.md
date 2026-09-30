# Listening for replies, per host

The listener prints each message for this chat and exits (with `--once`) or streams.
Replies to this chat plus broadcast alerts arrive. Others' replies never do.

## Claude Code

- Run in the background: `ttp listen <name> --chat <id> --once`.
- Its exit wakes you. Print the messages, then start it again.
- Always keep exactly one listener running per attached project.
- NEVER run it in the foreground. NEVER add `sleep` loops.

## Cursor

- Run `ttp listen <name> --chat <id>` as a monitored background shell.
- Notify on output. Each line is a message; relay it.

## Codex

- No background wake-up. At the start of each user turn run:
  `ttp listen <name> --chat <id> --once --timeout 1`
- Also run it right after `ttp say`, with `--timeout 90`.
- The user can always ask "any news from <name>?".

## Any host

- Lost the chat id → `ttp connect <name>` again (new id, same project).
- Listener errors → `ttp doctor <name>`.
