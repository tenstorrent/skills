---
name: tt-project
description: "Start, connect to, and talk with a long-running tt-project: a local, self-driving project with its own coordinator, workers, memory, budget guard and web app, running on this machine or an always-on box. Use when the user says tt-project, asks to start or connect to a project by name, or messages an existing project."
---

# tt-project: project

## Purpose

- The chat is a thin client. The project's coordinator runs as a daemon elsewhere.
- You create or find the project, relay messages, and show replies.
- NEVER do the project's work in this chat. The project does it.

## When to Invoke

- "Start a tt-project called X…", "connect to project X", "tell X to…".
- Any message the user addresses to an attached project.
- Not for one-off tasks the user wants done here and now.

## Pipeline

| Step | Do |
|---|---|
| 1. Install | Run `<plugin-root>/bin/ttp setup` when `ttp` is missing or `ttp --version` is older than this plugin's version. To deploy a repository branch tip, run `plugins/tt-project/bin/ttp setup` from a checkout at that tip, not the installed `ttp setup` (it reinstalls its own version). Afterwards use `ttp`; projects pick up the new release by themselves. |
| 2. Name | Get the project name. `ttp find <name>` says whether it exists. |
| 3a. New | Follow `create.md`. |
| 3b. Existing | `ttp connect <name> --label "<short chat label>"`. Keep the printed `chat:` id. |
| 3c. Link | Reply with the `tt-project://…` line and the verified web app link: the last `web app:` line `new` or `connect` printed. They open a remote project's kept local forward and check the link first. |
| 4. Listen | Start the listener for this host: `hosts.md`. |
| 5. Relay | User message for the project → `ttp say <name> --chat <id> "<message>"`. |
| 6. Show | Print coordinator replies as they arrive, lightly formatted, no additions. Restart the listener with `--ack <last id shown>`. |

## Relay rules

- Never ask the user to do what you or the project can do, and never offer to do it: do it and
  say so. Ask only when a choice is truly ambiguous, dangerous, or involves another human.
- Forward the user's words verbatim. Do not rephrase instructions.
- A project question you can answer from `ttp status <name>`: answer directly.
- Anything else goes to the coordinator. Answers arrive through the listener.
- Keep this chat free: after `ttp say`, end your turn. Do not wait in the foreground.
- `ttp say` replies "queued on this machine": the project is unreachable right now and the message
  is safe. It goes out by itself, in order and once, when the project is reachable. Tell the user;
  do not resend it.
- Alerts arrive in every attached chat. Replies arrive only in the chat that asked.
- A repeated message id was already shown. Skip it.
- The user asks what is running, costs, or blockers → `ttp status <name>`.
- Never give an unverified web app link. Give only a `web app: http…` line that `ttp new`,
  `ttp connect` or `ttp web` just printed: they check it first (HTTP 200 naming this project,
  through that exact port and token). Never build a link by hand or reuse an old one.
- `web app: NOT AVAILABLE (…)` instead of a link: they already tried to repair it (kept tunnel,
  daemon). Tell the user that statement plainly; give no link.

## Always print the project locator

- After `new` or `connect`, show the `tt-project://…` line once, with the verified web app link.
- Later chats find the project by it, even without the registry.

## Web app and notifications

- `ttp web <name>` prints the checked link. Remote project → `ttp web <name> --tunnel --keep` opens a
  kept local forward without asking (`remote.md`).
- The web app has a one-click "Turn on notifications" button.
- On the user's own workstation, run `ttp notifier install` once and say so (desktop alerts).
- Details and optional Slack: `notifications.md`.

## Operating

| User wants | Command |
|---|---|
| status, spend, blockers, why idle | `ttp status <name>` |
| pause or resume all model work | `ttp pause <name>` / `ttp resume <name>` |
| stop all use of one resource (a device) | `ttp pause <name> --resource <r> --reason "..." --until 2d` (or `--end-when "<probe>"`) / `ttp resume <name> --resource <r>` |
| record their machines (shared by all projects) | `ttp machines add <alias> --tags device,...` / `ttp machines list`; say in the charter which ones the project may use. Projects on other machines (`--host`) get a merged copy on add/remove, `ttp upgrade` and `ttp machines push` |
| change caps | tell the coordinator, or `ttp config <name> budget.daily_usd 150` |
| read a schedule | `ttp schedules <name> show <schedule>` (or `list`): read-only, writes nothing |
| keep schedules in git | `ttp schedules <name> --export --yes` once (it moves them into the harness and commits): `harness/schedules.json` then holds them, the daemon applies edits to it, and every change is a harness commit |
| restart after trouble | `ttp restart <name>`, then `ttp doctor <name>` |
| stop for good | `ttp stop <name>` (removes the service; data stays; running workers finish) |
| stop and end running work now | `ttp stop <name> --kill` (their tasks resume on the next start) |
| logs | `ttp logs <name>` |

More: `operate.md`.

## Red Flags

| Thought | Reality |
|---|---|
| "I'll just do this quick task myself" | The project owns the work. Relay it. |
| "I'll poll until the answer comes" | The listener wakes you. End your turn. |
| "I'll paste the key here" | Secrets go through `ttp secret`, typed by the user. |
| "Shall I open a tunnel to the web app?" | Local forwards to view it: open and keep them, then say so. Ask only before a tunnel that exposes the user's machine. |
| "Would you like me to…?" / "You can run…" | If tt-project can do it, do it and report. Ask only real decisions. |
| "The coordinator is slow, I'll answer" | Say it is working; `ttp status` shows progress. |
