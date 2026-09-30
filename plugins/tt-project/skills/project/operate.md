# Operating a project

## Commands

| Command | Does |
|---|---|
| `ttp list` | projects known on this machine |
| `ttp find <name>` | locate by registry, then by chat-log locators |
| `ttp adopt <name> --host H --dir D` | record where a project lives |
| `ttp status <name> [--json]` | daemon, budget gates, running/blocked tasks, open questions |
| `ttp task <name> list` / `add "<title>" --spec …` | inspect or queue work by hand |
| `ttp memory <name> "<fact>"` | add a durable fact |
| `ttp config <name> <key> [value]` | read or set settings (dotted keys) |
| `ttp pause <name>` / `resume` | stop starting model runs / start again |
| `ttp restart <name>` | restart the daemon |
| `ttp stop <name>` | remove the service; keeps all data |
| `ttp doctor <name>` | providers, accounts, Jev, notifications, web |
| `ttp alerts <name> --after N` | alerts since a message id |

## Budget

- Plan windows (subscription): the project stops at 90% of any window.
- Usage-billed: default caps $100 per 24 h and $200 per 7 days, per project (all providers together).
- A run cut off before it reports its cost counts at an estimate, labelled as such.
- Gates tighten as spend rises: `green` → `yellow` → `orange` → `red` (paused).
- A spend spike far above the project's norm pauses it ("runaway guard").
- Raising a cap: tell the coordinator, or `ttp config <name> budget.daily_usd <n>`.
  Warn the user to raise caps carefully.

## Health

- Daemon not running → `ttp restart <name>`; still down → `ttp logs <name>`.
- Coordinator failing repeatedly → an alert says so; messages are kept, not lost.
- A worker that produces nothing for too long is stopped and retried (stall guard).
