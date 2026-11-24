#!/usr/bin/env bash
# Hourly overseer for a Claude bring-up run (started by launch.sh). Each tick: gather facts read-only
# (overseer_facts.py), have `claude -p` turn them into a short report, type it into the session, log it.
# It never kills, restarts or touches devices, CI or files; it only appends to its log.
#   overseer.sh --session NAME --tt /path/to/tt-metal --inputs .../inputs.json [--interval 1] [--model M]
#               [--started-at EPOCH] [--log FILE] [--dry-run] [--once] [--at ISO-UTC] [--project-dir DIR] [--facts FILE]
# --dry-run prints the facts and the report instead of sending or logging. For testing on a past run: --at reads
# the transcripts as of that time; --facts reports on facts gathered earlier instead of gathering them.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SESSION= TT= INPUTS= HOURS=1 MODEL=${CLAUDE_MODEL:-claude-opus-5-5} STARTED= LOG= DRY=no ONCE=no AT= PROJ= FACTS=
while [ $# -gt 0 ]; do case $1 in
  --dry-run) DRY=yes; shift; continue;; --once) ONCE=yes; shift; continue;;
  --session) SESSION=$2;; --tt) TT=$2;; --inputs) INPUTS=$2;; --interval) HOURS=$2;; --model) MODEL=$2;;
  --started-at) STARTED=$2;; --log) LOG=$2;; --at) AT=$2;; --project-dir) PROJ=$2;; --facts) FACTS=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$TT" ] || { echo "need --tt" >&2; exit 2; }
[ "$DRY" = yes ] || [ -n "$SESSION" ] || { echo "need --session (or --dry-run)" >&2; exit 2; }
LOG=${LOG:-${INPUTS:+$(dirname "$INPUTS")/overseer.log}}; LOG=${LOG:-$TT/overseer.log}

INSTR='You get facts about an autonomous Claude Code session that is bringing a model up on Tenstorrent
hardware: its main agent, its subagents, their background shells, the devices, the host, disk and budget.
Write a short plain-text status report for the main agent. One line per unfinished subagent and per
background shell of the main session: its name, a verdict (progressing, slow, stuck, waiting on a finished
job, or done) and the evidence in a few words (times, tool calls in the window, log growth, last log line).
A shell or agent is "waiting on a finished job" when the work it waits for has visibly ended (an exit line,
a final result, an idle server) but it is still waiting. Then one line each for devices (who holds them and
whether they work), CPU and memory, disk, and budget. Name what looks wrong; do not give instructions.
If every subagent and job is progressing and nothing else looks wrong, answer with one line saying so.
No headings, no markdown, at most one line per item.'

log() { [ "$DRY" = yes ] && { echo "$*" >&2; return; }; printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

tick() {
  local facts report args=(--tt "$TT" --interval-hours "$HOURS")
  [ -n "$INPUTS" ] && args+=(--inputs "$INPUTS"); [ -n "$STARTED" ] && args+=(--started-at "$STARTED")
  [ -n "$AT" ] && args+=(--at "$AT"); [ -n "$PROJ" ] && args+=(--project-dir "$PROJ")
  if [ -n "$FACTS" ]; then facts=$(cat "$FACTS")
  elif ! facts=$(timeout 900 python3 "$HERE/overseer_facts.py" "${args[@]}" 2>&1); then
    log "facts failed, nothing sent: $(echo "$facts" | tail -3 | tr '\n' ' ')"; return; fi
  [ "$DRY" = yes ] && printf '=== facts ===\n%s\n' "$facts"
  # No tools, no saved session; run outside the tt-metal tree so its CLAUDE.md and transcripts stay out.
  if ! report=$(cd "${TMPDIR:-/tmp}" && printf '%s\n' "$facts" | timeout 600 claude -p --model "$MODEL" \
      --tools "" --no-session-persistence --system-prompt "$INSTR" "Facts follow on stdin. Write the report." 2>&1) \
      || [ -z "${report//[[:space:]]/}" ]; then
    log "claude -p failed, nothing sent: $(echo "$report" | tail -3 | tr '\n' ' ')"; return; fi
  # One line for tmux: a newline would submit the message early.
  local line; line="Overseer report (hourly): $(echo "$report" | sed '/^[[:space:]]*$/d' | paste -sd'|' | sed 's/|/ | /g')"
  if [ "$DRY" = yes ]; then printf '=== report ===\n%s\n' "$line"; return; fi
  log "$line"
  tmux send-keys -t "$SESSION" -l "$line" && tmux send-keys -t "$SESSION" Enter
}

SLEEP=$(python3 -c "print(int(float('$HOURS') * 3600))")
while :; do
  [ "$ONCE" = yes ] || sleep "$SLEEP"
  [ "$DRY" = yes ] || tmux has-session -t "$SESSION" 2>/dev/null || exit 0
  tick
  [ "$ONCE" = yes ] && exit 0
done
