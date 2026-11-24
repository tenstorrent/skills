#!/usr/bin/env bash
# Hourly overseer for a Claude bring-up run (started by launch.sh). Each tick, one read-only `claude -p` agent
# looks at the session's transcripts and the host and writes a short report; it is typed into the session and
# appended to overseer.log. Read-only is enforced by tool settings (see CLAUDE_ARGS), not only by the prompt.
#   overseer.sh --session NAME --tt /path/to/tt-metal --inputs .../inputs.json [--interval 1] [--model M]
#               [--started-at EPOCH] [--log FILE] [--project-dir DIR] [--dry-run] [--once] [--at ISO-UTC]
# --dry-run prints the report instead of sending or logging. --at replays a past run: the transcripts are copied
# cut at that time, and the agent is told to ignore live host state.
set -uo pipefail
SESSION= TT= INPUTS= HOURS=1 MODEL=claude-sonnet-5-5 STARTED= LOG= DRY=no ONCE=no AT= PROJ=
while [ $# -gt 0 ]; do case $1 in
  --dry-run) DRY=yes; shift; continue;; --once) ONCE=yes; shift; continue;;
  --session) SESSION=$2;; --tt) TT=$2;; --inputs) INPUTS=$2;; --interval) HOURS=$2;; --model) MODEL=$2;;
  --started-at) STARTED=$2;; --log) LOG=$2;; --at) AT=$2;; --project-dir) PROJ=$2;;
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

prompt() {  # $1: transcript dir the agent reads
  cat <<EOF
You oversee an autonomous Claude Code session that is bringing a model up on Tenstorrent hardware. Now is $NOW.
Write a short status report for that session on where things stand now and what happened in the last $HOURS h.
Only read; change nothing.

Where things are:
- tt-metal checkout (the session's working directory): $TT
- run settings and budget (budget_hours, started_at): ${INPUTS:-none}${STARTED:+; the run was launched at $(date -u -d "@$STARTED" +%FT%TZ)}
- transcripts: $1. The main session is the most recently modified *.jsonl there; its subagents are
  <session-id>/subagents/agent-*.jsonl (task in agent-*.meta.json), large tool outputs in <session-id>/tool-results/.
  One JSON object per line with .timestamp, .type and .message.content (tool_use blocks with .name and .input,
  tool_result blocks). A background shell is a Bash tool_use with run_in_background; its result names its ID and
  output file ("Output is being written to: ..."). A subagent or background shell has finished when its result or a
  <task-notification> with its status has arrived; anything started and not finished counts, however long idle.
  The files are large: use tail, grep, jq and wc on them; never read a whole transcript. Bash runs only simple
  read-only commands (ls, cat, head, tail, grep, jq, wc, find, ps, pgrep, fuser, df, du, free, uptime, date) and
  pipes between them; loops, \$(...), redirections and other commands are denied.

Check:
- every subagent and every background job (of the main session and of subagents) not finished: its task,
  and whether it is progressing (recent tool calls, growing logs) or stuck, for example waiting (sleep, until, wait,
  a polling loop) on a job that has already finished or will never finish. Evidence: times, last log lines.
- the main session: when it last acted and what it is waiting for.
- who holds /dev/tenstorrent/* (fuser, ps) and whether that process does work.
- CPU, memory, disk (df on the checkout).
- budget used and left.

Answer in plain text, no headings or markdown: one line per subagent or job with its name, a verdict (progressing,
slow, stuck, waiting on a finished job, done) and the evidence in a few words; then one line each for the main
session, devices, CPU and memory, disk, budget. Name what looks wrong; do not give instructions. If every subagent
and job is progressing and nothing else looks wrong, answer with one line saying so.
EOF
  [ -n "$AT" ] && echo "
This is a replay of a past run: assume now is $NOW; the transcripts were cut at that time. Ignore live host state
(processes, devices, CPU, memory, disk), which is unrelated; say it is not available. Files the transcripts name
outside $1 are gone."
}

tick() {
  local dir=$PROJ tmp= out report cost line err
  NOW=$(date -u +%FT%TZ)
  [ -d "$PROJ" ] || { log "no transcripts at $PROJ, nothing sent"; return; }
  if [ -n "$AT" ]; then  # replay: copy the transcripts cut at --at
    NOW=$(date -u -d "$AT" +%FT%TZ) || { log "bad --at $AT"; return; }
    tmp=$(mktemp -d); dir=$tmp/proj
    (cd "$PROJ" && find . -name '*.jsonl' -o -name '*.meta.json' -o -path '*/tool-results/*' -type f ! -newermt "$NOW" \
      | while read -r f; do mkdir -p "$dir/$(dirname "$f")"
          case $f in *.jsonl) jq -c --arg t "${NOW%Z}" 'select((.timestamp // "z") <= $t)' "$f" > "$dir/$f"
                            touch -d "$(tail -1 "$dir/$f" | jq -r .timestamp)" "$dir/$f" 2>/dev/null;;
                     *) cp -p "$f" "$dir/$f";; esac; done
     find "$dir" -name '*.jsonl' -empty | while read -r f; do rm -f "$f" "${f%.jsonl}.meta.json"; done)
  fi
  local d dirs=("$dir")  # the agent may read only these (and run the allowed commands)
  for d in "$TT" /dev/tenstorrent ${INPUTS:+"$(dirname "$INPUTS")"}; do [ -d "$d" ] && dirs+=("$d"); done
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
  log "($cost) $line"
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
