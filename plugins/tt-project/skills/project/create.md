# Creating a project

Ask only what you cannot infer. One message, all questions at once.

## 1. Name and brief

- Name: letters, digits, `.`, `_`, `-`. Taken → offer to connect instead.
- Brief: inline text, a file path, a link — anything. Pass it on verbatim.
- Save a long inline brief to a file first; pass it with `--describe-file`.

## 2. Where the project lives (the folder)

- Default: the git top level of the current directory.
- Data goes in `<root>/tt-project/`, which ignores itself. Nothing is committed.
- Unclear root (no repo, several repos, a non-code project) → ask the user.

## 3. Which machine runs it

| Situation | Default | Ask |
|---|---|---|
| This machine has TT devices (`/dev/tenstorrent` exists) | this machine | no |
| Always-on Linux box, no devices, none needed | this machine | no |
| Laptop (has a battery) | ask | "Which always-on machine should run it? A laptop works only while awake." |
| Work needs devices the laptop lacks | ask | the box or reservation to use |

- A laptop is allowed. Say it pauses while asleep and resumes on wake.
- Remote machine → `--host <ssh-alias> --dir <root on that machine>`.
- Device details or reservations named by the user go into the brief.

## 4. Jev (optional)

Check `ttp secret show`. No Jev entry → tell the user, in plain words:

> Jev is a cheap decision model. The project uses it to screen logs and alerts before waking an
> LLM. It is optional: without it everything still works, screening just costs more. To add a
> key, run `ttp secret jev` in a terminal and paste it there (not in this chat).

- OpenRouter users: `ttp secret jev --via openrouter`.
- The key is saved once per user and shared by all their projects on that machine.

## 5. Create

```bash
ttp new <name> [--dir <root>] [--host <ssh-alias>] --describe-file <brief.md>
```

- Remote host: the command ships the runtime over ssh and creates it there.
- The daemon installs as a service: systemd user unit (+ linger) or launchd; cron fallback.
- Print the `tt-project://…` line and the web link from the output.

## 6. Then

- `ttp connect <name> --label "<chat label>"` and start the listener (`hosts.md`).
- The coordinator restates goals and asks what it still needs. Relay its message.
- Offer `ttp notifier install` if this is the user's workstation (ask first).
