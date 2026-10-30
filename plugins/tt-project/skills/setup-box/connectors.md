# setup-box: claude.ai account connectors, here and on each box

claude.ai connectors (Atlassian, Gmail, Google Calendar, Google Drive, Docs, Sheets, Slides, ...) belong to
the user's claude.ai account. They are authorized once per account in claude.ai Settings > Connectors, and
then appear in every Claude Code signed into that account (`claude mcp list`: Connected or Needs
authentication). A machine signed into another account cannot show them. So setting them up on a machine
is: the same account as the user's own machine (only with their yes), an inventory, any missing one
authorized, then a real call. `box.py` is `<plugin-root>/skills/setup-box/scripts/box.py`.

```
box.py connectors check                          # this machine only
box.py connectors check <box-alias> [...]        # this machine, then each box over ssh; read-only
box.py connectors check <box-alias> --verify     # plus one real read-only call per connected family
box.py connectors check ... --family Notes=Notion,Confluence   # add a family or extend one
```

## 1. Account

- Read with `claude auth status --json` only (its non-interactive output). Its account and organization
  are compared in memory as hashes: the check prints `same` or `different` (and whether the organization
  matches), never an email, account or organization name. No token or credential file is read.
- Signed out: said so, with the login step (`box.py agents login <box-alias> claude`, `agents.md`).
- Different from this machine: said plainly, with the one step that fixes it (log out there, then log in
  with this machine's account). Run it only on the user's yes: some keep a separate account per machine
  on purpose. The skill never logs in, logs out or switches accounts by itself.
- Signed in with an API key or a cloud provider instead of claude.ai: no claude.ai connectors there.

## 2. Inventory

- Each connector of each family shows as connected, needs authentication, failed to connect, or missing.
- Default families: Atlassian (Atlassian) and Google (Google Drive, Gmail, Google Calendar, Google Docs,
  Google Sheets, Google Slides). A project extends them in the site file (`site.md`):
  `"connectors": {"<family>": ["<connector>", ...]}` beside `boxes`, or with `--family`.
- Connectors turned off in a machine's environment (`ENABLE_CLAUDEAI_MCP_SERVERS=false`) are a finding.

## 3. Needs authentication or missing

- Authorization is per account, not per machine: the step is given once, for every machine on that
  account: claude.ai Settings > Connectors > Connect `<name>` (missing: add it there first; an
  organization admin may have to allow it). Tell the user that one step, then rerun the check.
- A machine on another account lists that account's connectors: its steps say so.
- This step runs in the user's browser on claude.ai: no callback port on the machine and no ssh
  forward. A sign-in that does run on a box (an MCP server added there, such as Glean or Codex's
  Atlassian server) listens on the box's one fixed callback port, so one standing forward covers
  them all (`agents.md`, MCP endpoints). One forward serves one box: a second box signing in at the
  same time takes its own `--callback-port` and its own forward. Claude Code's sign-in link works
  for 5 min: it is started only while the user is there to open it.

## 4. Verify

- `--verify` runs one `claude -p` per family on each machine that shows it connected: a small model,
  `--max-budget-usd 0.25`, only that family's connector tools allowed, and a PreToolUse hook that lets
  through only their search, fetch, get, list, read, find, query and lookup tools.
- Only pass or fail, the tool's name and an item count leave the machine. Never copy anything a
  connector returns into the plugin, notes, logs or a hand-off.
- Headless sessions (`claude -p`, as tt-project workers run) load claude.ai connectors asynchronously:
  neither Connected in `claude mcp list` nor needs-auth in an early session event proves anything. Each
  call waits for MCP connections (`MCP_CONNECTION_NONBLOCKING=false`, a long `MCP_TIMEOUT`) and, when
  none of the family's tools loaded, tries once more after every family's first try; all calls on one
  machine stop within 2 min, each try held to a fair share of the time left.
- Then per family: pass; needs authentication with the reconnect step only when a connector call
  answered an auth error; "connected, not loaded in headless runs" with a note and no Settings step
  when its tools still did not load (workers then use other configured MCP tools); not verified when
  tools loaded but none could make a read-only call, or when the 2 min cap left it unrun or stopped it
  (no step); any other failure says to rerun.
- Without `--verify`, connector states are `claude mcp list`'s and labelled not verified for headless
  runs.

## 5. Box check

`box.py agents check <box-alias>` (`agents.md`) adds these findings for a logged-in Claude Code: an
account different from this machine's, a signed-out Claude, and each connector that needs authentication,
is missing or failed (`--no-connectors` skips them). The setup plan (`new-box.md`) runs
`connectors check <box-alias> --verify` after the logins.

## Codex

- Codex has no claude.ai connectors. Atlassian has a remote MCP server (Rovo MCP); take its current URL
  from Atlassian's own getting-started page, then, only on the user's yes, on that machine:
  `codex mcp add atlassian --url <url>` and `codex mcp login atlassian` (a browser sign-in; for a box, run
  the sign-in as for Glean in `agents.md`, with `-c mcp_oauth_callback_port=28461` so the standing
  forward for that port completes it). Check with `codex mcp list`.
- Google: offer none unless Google documents an MCP server for that service; do not invent one.

## Related

- Agent CLIs, their logins and Glean: `agents.md`.
- Building and running on the box: the tt-buddy plugin (`agents.md` links its recipes).
- `ttp status` names connected connectors that isolated tt-project workers do not get (the MCP-gap
  line); this page sets the connectors up, that line says who can use them.
