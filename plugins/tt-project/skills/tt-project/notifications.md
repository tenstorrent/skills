# Notifications

Default channels need no accounts or setup.

| Channel | Setup | Reaches the user |
|---|---|---|
| Attached chats | none | alerts appear in every attached chat |
| Web app | one click: "Turn on notifications" | browser notification while the page is open |
| Desktop notifier | `ttp notifier install` (run it on the user's workstation, say so) | native notification on this machine, all projects |

- The notifier polls local projects directly and remote ones over the user's ssh.
- `ttp notifier test` shows a sample notification.
- Only `high` severity notifies by default: decisions, reviews, merges, funds, outages.
- The user can ask the coordinator for more or fewer alerts.

## Slack (optional)

- Reading Slack links (bug reports, threads): workers use the user's Slack connector or
  enterprise search if installed. The user just pastes links.
- Posting team-facing updates to a channel: workers can use the Slack connector when the
  user asks. It posts as the user, so it does NOT notify the user.
- A team that already has a Slack bot token can enable DMs from it:
  `ttp secret slack --email <user@company>` (token typed in a terminal), then
  `ttp config <name> notify.slack true`. Replies in the bot's DM route back to the project.
