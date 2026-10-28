# setup-box: agents on the box

An agent that edits and builds on the box runs there, or drives the box over ssh from the project
box. Either way the work happens in the box's own checkout (`ssh-workflow.md`).

## Agent CLIs, logins and editor extensions

The skill runs every step below itself, in the user's own account (`account.md`), never a shared
admin login, and never as root. `box.py` is `<plugin-root>/skills/setup-box/scripts/box.py`.

```
box.py agents check <box-alias>                      # read-only: each gap and the command that fixes it
box.py agents install <box-alias> --run              # Claude Code and Codex CLIs, PATH lines
box.py agents install <box-alias> --cli none --extensions --run   # editor extensions only
box.py agents login <box-alias> claude|codex         # a headless login; shows its link, code and end time
box.py agents login <box-alias> claude|codex --status
box.py agents login <box-alias> claude|codex --mcp glean     # sign the agent in to Glean
box.py agents login <box-alias> claude|codex --wait <seconds>   # how long a new login's link works
box.py agents editor-defaults --run                  # on the machine the editor runs on, only on the user's yes
```

| Agent | Install | Login (on the box; the user enters a code in their browser) | Check |
|---|---|---|---|
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | `claude auth login` | `claude --version`, `claude auth status` |
| Codex | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | `codex login --device-auth` | `codex --version`, `codex login status` |
| Cursor agent | per its official instructions | its own login | `cursor-agent --version` |

- `install` is idempotent: a CLI whose `--version` works is left alone. It puts `~/.local/bin` on
  PATH for a plain `ssh <box-alias> <command>` (the first line of `~/.bashrc`, before any early
  return for non-interactive shells; `~/.zshenv` for zsh) and for login shells (`~/.bash_profile`
  or `~/.profile`), each file backed up once to `<file>.ttp-bak`.
- Editors over Remote-SSH (Cursor, VS Code): the editor's own login carries over only to its own
  AI. The Claude Code (`anthropic.claude-code`) and Codex (`openai.chatgpt`) extensions run on the
  box's editor server and log in with the box's `~/.claude` and `~/.codex`, the same logins as the
  CLIs. `install --extensions` installs both on every server build there (`~/.cursor-server`,
  `~/.vscode-server`) and checks them with `--list-extensions`. A server build appears on the
  editor's first connect: connect once, then install.
- An editor update brings a new server build without the extensions. `editor-defaults` adds both
  to `remote.SSH.defaultExtensions` in Cursor's and VS Code's user settings, so every new build on
  every box gets them. It edits the user's own machine: only on their yes (the setup question),
  merged into the list with existing entries and comments kept, the file backed up first. When
  the editor runs on another machine than the agent, it says so and changes nothing; the box check
  then finds a new build without them and `install --extensions` fixes it.
- `login` starts the CLI's headless login on the box, detached, and prints its sign-in link and
  code. Tell the user both word for word; they open the link and enter the code. When Claude
  Code's page then shows a code to paste back, the user gives it to the agent, which passes it on
  with `login <box-alias> claude --code-stdin` (on stdin; single-use, it only completes that
  pending login). Then `login ... --status`. The login and its tokens stay on the box: never copy
  `~/.claude` or `~/.codex` between machines, never print or log a token, and never put an API
  key in a prompt, task or file.
- `check` reports each missing CLI, PATH gap, login and editor extension with the command that
  fixes it; run that command yourself. A login is the one step that needs the user: its code.
- A login or sign-in link lapses (`--wait <seconds>`; default 15 min for a CLI login, 60 min for
  Glean), so send it only while the user is likely there. In a tt-project run, first
  `ttp hours --check --for 1800`: it reads their usual active hours from the times of their own
  messages (none yet: now). Exit 1: do not start it; hand off `waiting` with that command as
  `retry_when`. The output names when the link works until, in the user's home zone: pass that on.
- gh: a distro's packaged gh can be too old for a newer `hosts.yml`; install it from GitHub's own
  apt repository. Its login is the user's (`gh auth login`, a code in their browser).

## Building and running: tt-buddy

Link to the tt-buddy plugin's recipes and skills; do not copy them here.

| Need | tt-buddy file |
|---|---|
| A new box: toolchain, drivers, firmware checks | `recipes/developer-setup.md` |
| Where checkouts and build trees live | `recipes/workspace.md` |
| Build tt-metal | `recipes/tt-metal/build.md` |
| Environment variables for runs | `recipes/tt-metal/env.md` |
| Run tests | `recipes/tt-metal/test.md` |
| Device runs (jobs, queueing, logs) | the `tt-buddy:run` skill |
| Device runs through tt-device-mcp | the `tt-buddy:run` skill's `skills/run/mcp.md` |

- tt-buddy is a separate plugin, installed only by the user's choice. When it is missing, name it in
  the hand-off as the plugin that holds these recipes; do not reconstruct them.
- Device runs on a box with a device broker go through it (tt-device-mcp), never by opening the
  device directly.

## MCP endpoints

- A site's MCP endpoints (device broker, other services) come from the site file
  (`mcp` in `site.md`), never from a repo or this skill.
- Glean: `install` adds an HTTP MCP server named `glean` to each CLI it sets up (Claude Code
  `claude mcp add --scope user --transport http glean <url>`, Codex `[mcp_servers.glean]`), and to
  the MCP config of each editor server on the box (Cursor `~/.cursor/mcp.json`, VS Code
  `~/.vscode-server/data/User/mcp.json`; merged, with a `.ttp-bak` copy first). An existing `glean`
  entry is always kept, even on another URL (the user may have added it by hand); `check` only
  notes the other URL. The URL comes from `--glean-url`, the site file's `mcp.glean`, or a `glean`
  server already in this machine's Claude Code, Codex or Cursor config, never from this skill.
  With none, it skips Glean with one line saying so.
- Sign-in: `login <box-alias> <cli> --mcp glean` runs the agent's own headless sign-in
  (`claude mcp login glean --no-browser`, `codex mcp login glean --no-browser`) in a tmux session
  on the box (`tmux -L ttp-login`), started over ssh, so outside any agent sandbox. It waits there
  up to 60 min (`--wait`). Before it hands out the link it checks with `ss` that the sign-in's callback port
  listens on the box; if not, it stops the sign-in and says so, and never hands out a dead link.
  A pending sign-in whose port stopped listening is replaced on the next `login`. The user
  completes it either way: (a) runs the printed `ssh -N -L <port>:<listen address>:<port> <their
  alias for the box>` before opening the link, and the page completes by itself; or (b) opens the
  link, copies the `localhost` address the browser ends on, and the agent passes it on with
  `--code-stdin` (on stdin, never on a command line, never stored). `--status` says signed in,
  pending, or that the link is dead. A Claude Code too old for `mcp login` gets one step instead:
  update it, or the user runs `claude` on the box, types `/mcp`, picks `glean` and chooses
  Authenticate. Editors sign in from their own MCP view.
- Tokens and credential files stay where the agent wrote them: never copied, printed or moved
  between boxes or users. Status and search output are filtered to one line each.
- Reload: agents read MCP servers when a session starts. Sessions open before `glean` was added,
  an editor's agent panel too, do not show it until restarted (`claude` or `codex` again, or
  `Developer: Reload Window` in the editor). `install` says so once when it adds an entry.
- Claude Code also lists the account's own connectors, such as `claude.ai Glean`, beside the
  `glean` server. That connector is separate and needs its own sign-in (`/mcp` in Claude Code, or
  the connector settings on claude.ai). A `Needs authentication` on it says nothing about `glean`.
- Account connectors (Atlassian, Google and others) follow the claude.ai account, not the box:
  `connectors.md`. `check` adds their findings for a logged-in Claude Code (`--no-connectors` skips).
- `check` reports Glean per agent: missing, configured but signed out, or working, and each
  editor's entry as configured or missing. `check --search` proves working: each signed-in CLI
  runs one real non-interactive Glean search (`claude -p ... --allowedTools=mcp__glean`,
  `codex exec`) and reports its result count, or the failure as a gap.
