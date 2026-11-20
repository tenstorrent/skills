# Notifications

Default channels need no accounts or setup.

| Channel | Setup | Reaches the user |
|---|---|---|
| Attached chats | none | alerts appear in every attached chat |
| Web app | one click: "Turn on notifications" | browser notification while the page is open |
| Desktop notifier | `ttp notifier install` (run it on the user's workstation, say so) | native notification on this machine, all projects |

- The notifier polls local projects directly and remote ones over the user's ssh.
- A remote host ssh cannot reach for 20 min (`host_down_after_s` in `notifier.json`) gets one
  notification, since its projects cannot page, and one more when it answers again. The 20 min
  count only polls in a row: after a sleep or a stopped notifier it starts again.
- A project whose daemon has not beaten for 3 h (`daemon_down_after_s`) while its service should keep it
  running gets one notification naming `ttp restart`, and one more when it runs again.
- Desktop notifications play a sound and are meant to stay until closed. Linux: notify-send, critical,
  no expiry. macOS: setup, upgrade and `ttp notifier install` build a small `tt-project` app
  (`~/Applications/tt-project.app`, ad-hoc signed, Persistent style requested) without root; its first
  notification brings macOS's one Allow prompt. Alerts go through it only once macOS shows it
  allowed: its Notifications settings list it with a style, or, where they list no apps (current
  macOS may have no `com.apple.ncprefs.plist`; `defaults export com.apple.ncprefs -` may give an
  empty dict, and its output for a missing domain is unverified), usernoted's log
  (`log show --predicate 'process == "usernoted"'`, read only, bounded) shows the app's notification
  delivered or presented with no denial since its first post. A denial, nothing, or an unreadable log
  keeps terminal-notifier, else osascript (Script Editor), as does `"app": false`. Whether current
  macOS honours the app's requested Persistent style is not verified, and macOS lets no program set
  another app's alert style, so while the app is not shown allowed setup and `ttp notifier test` print
  one line: alerts still come from the old sender; to keep them on screen, allow tt-project in System
  Settings > Notifications and set its style to Persistent. Clicking Allow once stays manual.
  `"persistent": false` in `~/.tt-project/notifier.json` turns persistence off. `ttp uninstall` removes the app.
- `ttp notifier test` sends one through the same path and says whether it worked, with the fix
  if not; exit 1 = failed. macOS does not report display: in a terminal it asks if you saw it.
  On macOS it also reads (never writes) the app's alert style; only banners, off or a fallback
  sender print the one System Settings > Notifications line to set it to Persistent. It says when
  usernoted's log shows the test muted by screen sharing (display state) or a Focus mode.
- `ttp notifier install`, `ttp setup` and `ttp upgrade` send that test once per installed ttp version.
  `ttp setup` and `ttp upgrade` also restart an enabled notifier on the new code (never one turned
  off); `ttp status` says when it runs older code (`ttp notifier restart`).
- Each failed delivery goes to `~/.tt-project/notifier.log`; while an enabled notifier fails, the
  projects on that machine raise one chat alert, cleared once a delivery works again or the notifier
  is removed. `ttp notifier uninstall` stops and removes it (a machine with no desktop session).
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
