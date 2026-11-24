#!/usr/bin/env bash
# Hourly overseer for a Claude bring-up run (started by launch.sh). Each tick, one fresh read-only `claude -p` agent
# reports facts: what progressed since the previous report (whose text and time it gets from overseer.log), and the
# devices, CPU, disk and budget. The report is typed into the session and appended to overseer.log. Read-only is
# enforced by tool settings (RO/NO below), not only by the prompt.
#   overseer.sh --session NAME --tt /path/to/tt-metal --inputs .../inputs.json [--interval 1] [--model M]
#               [--started-at EPOCH] [--log FILE] [--project-dir DIR] [--dry-run] [--once] [--at ISO-UTC] [--since ISO-UTC]
# --dry-run prints the report instead of sending or logging (it still reads the previous report from the log).
# --at replays a past run: the transcripts are copied cut at that time, and the agent is told to ignore live host
# state. --since sets the window start instead of the previous report's time (or the run start).
set -uo pipefail
PATH="$HOME/.local/bin:$PATH"  # claude installs here; ssh and nohup shells often lack it
command -v claude >/dev/null || { echo "claude not found on PATH" >&2; exit 2; }
SESSION= TT= INPUTS= HOURS=1 MODEL=claude-opus-5-5 STARTED= LOG= DRY=no ONCE=no AT= PROJ= SINCE_OPT=
while [ $# -gt 0 ]; do case $1 in
  --dry-run) DRY=yes; shift; continue;; --once) ONCE=yes; shift; continue;;
  --session) SESSION=$2;; --tt) TT=$2;; --inputs) INPUTS=$2;; --interval) HOURS=$2;; --model) MODEL=$2;;
  --started-at) STARTED=$2;; --log) LOG=$2;; --at) AT=$2;; --project-dir) PROJ=$2;; --since) SINCE_OPT=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$TT" ] || { echo "need --tt" >&2; exit 2; }
[ "$DRY" = yes ] || [ -n "$SESSION" ] || { echo "need --session (or --dry-run)" >&2; exit 2; }
LOG=${LOG:-${INPUTS:+$(dirname "$INPUTS")/overseer.log}}; LOG=${LOG:-$TT/overseer.log}
# Claude Code keeps a session's transcripts under projects/<cwd with every non-alphanumeric character as ->.
PROJ=${PROJ:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf %s "$TT" | sed 's/[^a-zA-Z0-9]/-/g')}
TASKS=/tmp/claude-$(id -u)  # background shell outputs (<project>/<session>/tasks/*.output)
PREFIX="Automated overseer report (hourly, not from the operator):"
DIALOG='Enter to confirm|Esc to cancel|Do you want to|Allow this|\(y/n\)|Press Enter|❯ 1\.'

# Read-only: only these tools, nothing else is asked for (dontAsk denies), no user/project settings, hooks or MCP.
RO=(Read Grep Glob)
for c in ps pgrep tail head grep jq ls df du stat fuser lsof cat find wc date nproc free uptime; do RO+=("Bash($c:*)"); done
NO=(Edit Write NotebookEdit WebFetch WebSearch "Bash(tmux:*)" "Bash(kill:*)" "Bash(pkill:*)" "Bash(rm:*)"
    "Bash(mv:*)" "Bash(cp:*)" "Bash(tt-smi:*)" "Bash(git:*)" "Bash(find * -delete*)" "Bash(find * -exec*)"
    "Bash(find * -ok*)" "Bash(find * -fprint*)" "Bash(find * -fls*)")

log() { [ "$DRY" = yes ] && { echo "$*" >&2; return; }; printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# Inventory of every subagent and background job (shell or Monitor) in the transcripts, one line each with
# UNFINISHED/finished: starts from tool results (backgroundTaskId, async_launched agentId, Monitor taskId), ends from
# the latest <task-notification> (an agent "waiting on its own background work" is unfinished) or a TaskStop result.
INV=$(sed 's/^ *//' <<'EOF' | tr '\n' ' '
[inputs | (input_filename | capture("agent-(?<a>[0-9a-f]+)\\.jsonl$").a // "main-" + (input_filename | sub(".*/"; "") | .[:8])) as $o | .timestamp as $t
| ({k: "seen", o: $o, t: $t},
   (.message.content? | arrays | .[] | select(.type == "tool_use") | {k: "use", u: .id, c: (.input.command // "")}),
   ((.message.content? | arrays | .[] | select(.type == "tool_result") | .tool_use_id) as $u | .toolUseResult? | objects
    | if .backgroundTaskId then {k: "start", id: .backgroundTaskId, kind: "shell", o: $o, t: $t, u: $u}
      elif .status == "async_launched" then {k: "start", id: .agentId, kind: "agent", o: $o, t: $t, d: .description}
      elif .taskId and .timeoutMs then {k: "start", id: .taskId, kind: "monitor", o: $o, t: $t, u: $u}
      elif (.message? | strings | test("^Successfully stopped task")) then {k: "note", id: (.message | capture("task: (?<i>[a-z0-9]+)").i), t: $t, s: "stopped"}
      else empty end),
   (tostring | split("<task-notification>")[1:][] | capture("<task-id>(?<i>[a-z0-9]+)</task-id>").i as $i
    | (if test("waiting on its own background work") then "waiting-on-own-jobs" elif test("<status>") then capture("<status>(?<s>[a-z_]+)</status>").s
       elif test("Monitor expired|stream ended") then "expired" else empty end) | {k: "note", id: $i, t: $t, s: .}))]
| (map(select(.k == "use") | {(.u): .c}) | add) as $cmd
| (map(select(.k == "seen")) | group_by(.o) | map({(.[0].o): (map(.t) | max)}) | add) as $last
| (map(select(.k == "note")) | group_by(.id) | map({(.[0].id): max_by(.t)}) | add // {}) as $note
| map(select(.k == "start")) | unique_by(.id) | map(($note[.id] // {s: "none", t: ""}) as $n
  | (if $n.s == "waiting-on-own-jobs" or $n.s == "none" or ($last[.id] // "") > $n.t then "UNFINISHED" else "finished" end) as $f
  | [$f, .kind, .id, "by " + .o, "start " + .t[5:16], "notice " + $n.s + " " + $n.t[5:16],
     if .kind == "agent" then "last-act \($last[.id][5:16] // "-") " + .d else $cmd[.u] // "?" | gsub("\\s+"; " ") | .[:100] end])
| sort_by(.[0], .[4])[] | join("  ")
EOF
)

prompt() {  # $1: transcript dir the agent reads
  cat <<EOF
You check on an autonomous Claude Code session that is bringing a model up on Tenstorrent hardware. Report facts only:
what progressed between $SINCE (the previous check, or the run start) and now, $NOW. The session diagnoses and
decides: no diagnosis beyond the evidence, no instructions. Only read; change nothing.

Where things are:
- tt-metal checkout (the session's working directory): $TT
- run settings and budget (budget_hours, started_at): ${INPUTS:-none}${STARTED:+; the run was launched at $(date -u -d "@$STARTED" +%FT%TZ)}
- transcripts: $1. The main session is the most recently modified *.jsonl there, its subagents are
  <session-id>/subagents/agent-<id>.jsonl, large tool outputs are in <session-id>/tool-results/, and background job
  output is in $TASKS/<project>/<session-id>/tasks/<id>.output.
- Bash runs only simple read-only commands (ls, cat, head, tail, grep, jq, wc, find, stat, ps, pgrep, fuser, df, du,
  free, uptime, date) and pipes between them; loops, \$(...), redirections and other commands are denied.

Step 1. Run this command exactly as written, first. It lists every subagent and background job, unfinished first:
jq -rn '$INV' $1/*.jsonl $1/*/subagents/*.jsonl

Step 2. For the main session and each UNFINISHED item, find what progressed since $SINCE: new results, files written,
logs that grew. Tool calls alone are not progress (a sleep or poll loop is not). Look only at the window, never read
whole files:
- a transcript's records in the window (count them with | wc -l):
  jq -c 'select(.timestamp > "$SINCE" and .message) | [.timestamp[11:19], .type, (.message.content | if type == "string" then .[:200] else map(.text // .input.command // (.content | tostring)) | map(.[:200]?) | join(" ; ") end)]' FILE | tail -n 30
  and its last record: tail -n 1 FILE | jq -r .timestamp
- outputs and logs: find DIR -newermt "$SINCE" -type f; stat -c '%y %s %n' FILE; tail -n 20 FILE for the new lines.
- devices or CPU: who holds /dev/tenstorrent/* (fuser, ps) and whether that process is doing work.

$(if [ -n "$PREV" ]; then echo "The previous report (check at $SINCE), one item per line:"; echo "$PREV" | sed 's/ | /\n/g' | sed 's/^/> /'
  echo "For anything with no progress since $SINCE, say whether this previous report also showed none."
  else echo "This is the first check: there is no previous report, so do not mention one."; fi)

Answer in plain text, no headings or markdown, one line per item: the id (main for the main session), what it is in a
few words, "progressed" or "NO PROGRESS since <time of its last progress, not of its last record or poll>" (adding
"also none in previous report" when so), and the evidence with sizes and times the next check can compare against
(for example "log X 12.3 MB, last write 10:41Z; transcript last record 04:49Z"). If everything progressed, one line
for all of them is enough. Then one line for resources, for example "Resources: devices held by <pid/command>, busy
or idle since <time>; load 0.3/16, RAM 236/261 GB free; disk 1.6 TB free (df on the checkout); budget 19 h used, 29 h
left". Spell out a resource in more detail only if it looks short or changed a lot since the previous report.
EOF
  [ -n "$AT" ] && echo "
This is a replay of a past run: assume now is $NOW; the transcripts were cut at that time. Ignore live host state
(processes, devices, CPU, memory, disk), which is unrelated; say it is not available. Files the transcripts name
outside $1, including job outputs, are gone."
}

tick() {
  local dir=$PROJ tmp= out report cost line err last
  NOW=$(date -u +%FT%TZ)
  [ -d "$PROJ" ] || { log "no transcripts at $PROJ, nothing sent"; return; }
  [ -n "$AT" ] && { NOW=$(date -u -d "$AT" +%FT%TZ) || { log "bad --at $AT"; return; }; }
  # Window: from the previous report (its line starts with the time it covered up to) or the run start, to now.
  last=$(grep -F "$PREFIX" "$LOG" 2>/dev/null | tail -n 1)
  PREV=${last#*"$PREFIX" }; [ -n "$last" ] || PREV=
  SINCE=${last%% *}
  [ -n "$last" ] || SINCE=$( { [ -n "$STARTED" ] && date -u -d "@$STARTED" +%FT%TZ; } ||
    { [ -f "${INPUTS:-}" ] && jq -er .started_at "$INPUTS"; } || date -u -d "$NOW - $HOURS hours" +%FT%TZ)
  [ -n "$SINCE_OPT" ] && SINCE=$(date -u -d "$SINCE_OPT" +%FT%TZ)
  if [ -n "$AT" ]; then  # replay: copy the transcripts cut at --at
    tmp=$(mktemp -d); dir=$tmp/proj
    (cd "$PROJ" && find . -name '*.jsonl' -o -name '*.meta.json' -o -path '*/tool-results/*' -type f ! -newermt "$NOW" \
      | while read -r f; do mkdir -p "$dir/$(dirname "$f")"
          case $f in *.jsonl) jq -c --arg t "${NOW%Z}" 'select((.timestamp // "z") <= $t)' "$f" > "$dir/$f"
                            touch -d "$(tail -1 "$dir/$f" | jq -r .timestamp)" "$dir/$f" 2>/dev/null;;
                     *) cp -p "$f" "$dir/$f";; esac; done
     find "$dir" -name '*.jsonl' -empty | while read -r f; do rm -f "$f" "${f%.jsonl}.meta.json"; done)
  fi
  local d dirs=("$dir")  # the agent may read only these (and run the allowed commands)
  # The run folder (parent of the checkout, unless that is $HOME) holds logs and eval runs too.
  local run; run=$(dirname "$TT"); [ "$run" = "$HOME" ] && run=$TT
  for d in "$run" "$TT" /dev/tenstorrent ${INPUTS:+"$(dirname "$INPUTS")"}; do [ -d "$d" ] && dirs+=("$d"); done
  [ -z "$AT" ] && [ -d "$TASKS" ] && dirs+=("$TASKS")
  err=$(mktemp)
  out=$(cd "$dir" && timeout 1200 claude -p --model "$MODEL" --no-session-persistence --output-format json \
      --restricted --strict-mcp-config --disable-slash-commands --permission-mode dontAsk --permission-prompts none \
      --tools "Read,Grep,Glob,Bash" --add-dir "${dirs[@]}" --allowedTools "${RO[@]}" --disallowedTools "${NO[@]}" \
      -- "$(prompt "$dir")" 2>"$err")
  local rc=$?; [ -n "$tmp" ] && rm -rf "$tmp"
  report=$(printf '%s' "$out" | jq -r 'select(.is_error == false) | .result' 2>/dev/null)
  cost=$(printf '%s' "$out" | jq -r '"\(.duration_ms/1000|floor) s, $\(.total_cost_usd*100|round/100)"' 2>/dev/null)
  if [ $rc -ne 0 ] || [ -z "${report//[[:space:]]/}" ]; then
    log "overseer agent failed (rc $rc), nothing sent: $({ tail -c 300 "$err"; printf '%s' "$out" | tail -c 300; } | tr '\n' ' ')"
    rm -f "$err"; return; fi
  rm -f "$err"
  # One line for tmux: a newline would submit the message early.
  line="$PREFIX $(echo "$report" | sed '/^[[:space:]]*$/d' | paste -sd'|' | sed 's/|/ | /g')"
  if [ "$DRY" = yes ]; then printf '%s\n(%s)\n' "$line" "$cost"; return; fi
  printf '%s (%s) %s\n' "$NOW" "$cost" "$line" >> "$LOG"  # stamped with the window end
  # Never type into a dialog: the keystrokes would answer it. Retry next tick.
  if tmux capture-pane -p -t "$SESSION" | grep -Eq "$DIALOG"; then  # the visible screen
    log "dialog or prompt on screen, report not sent"; return; fi
  tmux send-keys -t "$SESSION" -l "$line" && tmux send-keys -t "$SESSION" Enter
}

SLEEP=$(awk -v h="$HOURS" 'BEGIN { print int(h * 3600) }')
while :; do
  [ "$ONCE" = yes ] || sleep "$SLEEP"
  [ "$DRY" = yes ] || tmux has-session -t "$SESSION" 2>/dev/null || exit 0
  tick
  [ "$ONCE" = yes ] && exit 0
done
