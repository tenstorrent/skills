---
"tt-project": patch
---

`tt-project`: the top of the web app, `ttp status` and the chat relay show only what needs the user
now: open questions and problems that are active. High alerts with a condition open an episode that
clears itself and keeps its history (logged out → the next successful run, budget red → the gate
leaves red, coordinator failures → a successful turn, disk low → space is back); the chats hear once
that it cleared, and the same condition can alert again at once. A host reboot is information only.
Everything else (FYI notes, decisions, reboots, cleared alerts) is a feed below, newest first. The
budget at the top is a few plain lines: per plan window the percent used, time to reset and history
(daily 5-hour peaks over 14 days, the last two weekly finals), and one line for dollar caps; pacing,
gate reasons and top spenders are in the Budget tab. A page that cannot reach its daemon says so and
shows how to reconnect. `ttp web <name> --tunnel --keep` keeps the tunnel up as a launchd or
systemd user service (adopting or replacing an existing one); `--unkeep` removes it.
