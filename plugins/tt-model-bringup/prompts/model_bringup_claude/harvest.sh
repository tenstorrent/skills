#!/usr/bin/env bash
# Knowledge harvest for a finished bring-up run. One fresh read-only `claude -p` agent reads the run's lessons.md,
# transcripts and commits, and the ttnn-knowledge skill, and writes a proposal: what knowledge the run was missing
# that is worth adding, how the run used the knowledge it had, and how its lessons file matched the brief. Nothing
# in the skills is changed; a person reviews the proposal. Read-only is enforced by tool settings; the script writes
# the agent's answer to <out>/harvest.md. See README.md (Knowledge).
#   harvest.sh --tt /path/to/tt-metal [--transcripts DIR] [--run-dir DIR] [--knowledge DIR] [--run-knowledge DIR]
#              [--out DIR] [--model M] [--wait --session NAME] [--dry-run]
# --transcripts: the run's transcripts (default: the Claude project dir of the checkout under $CLAUDE_CONFIG_DIR or
#   ~/.claude); a Codex home or sessions dir works too.
# --knowledge: the ttnn-knowledge to propose against (default: the one next to this script). --run-knowledge: the
#   one the run had (default: <run dir>/skills/... if present, else --knowledge).
# --wait: first wait until the run is finished: its tmux session is gone, or REPORT.md exists and nothing was
#   committed for 2 h (hourly overseer reports keep the transcript busy, so its idle time does not tell). launch.sh
#   starts this after the budget.
set -uo pipefail
PATH="$HOME/.local/bin:$PATH"
command -v claude >/dev/null || { echo "claude not found on PATH" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
TT= TR= RUN_DIR= KB=$(cd "$HERE/../../skills/ttnn-knowledge" && pwd) RKB= OUT= MODEL=claude-opus-5-5 WAIT= SESSION= DRY=
while [ $# -gt 0 ]; do case $1 in
  --wait) WAIT=1; shift; continue;; --dry-run) DRY=1; shift; continue;;
  --tt) TT=$(cd "$2" && pwd);; --transcripts) TR=$(cd "$2" && pwd);; --run-dir) RUN_DIR=$(cd "$2" && pwd);;
  --knowledge) KB=$(cd "$2" && pwd);; --run-knowledge) RKB=$(cd "$2" && pwd);; --out) OUT=$2;; --model) MODEL=$2;;
  --session) SESSION=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$TT" ] || { echo "need --tt" >&2; exit 2; }
RUN_DIR=${RUN_DIR:-$(dirname "$TT")}; [ "$RUN_DIR" = "$HOME" ] && RUN_DIR=$TT
TR=${TR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf %s "$TT" | sed 's/[^a-zA-Z0-9]/-/g')}
[ -d "$TR" ] || { echo "no transcripts at $TR (use --transcripts)" >&2; exit 2; }
P="$RUN_DIR/skills/plugins/tt-model-bringup/skills/ttnn-knowledge"; RKB=${RKB:-$( [ -d "$P" ] && echo "$P" || echo "$KB")}
BRINGUP=$(ls -d "$TT"/models/autoports/*/bringup 2>/dev/null | head -n 1)
[ -n "$BRINGUP" ] || { echo "no models/autoports/*/bringup in $TT" >&2; exit 2; }
OUT=${OUT:-$RUN_DIR/harvest}

if [ -n "$WAIT" ]; then
  while :; do
    [ -n "$SESSION" ] && ! tmux has-session -t "$SESSION" 2>/dev/null && break
    [ -f "$BRINGUP/../REPORT.md" ] || [ -f "$BRINGUP/REPORT.md" ] &&
      [ $(( $(date +%s) - $(git -C "$TT" log -1 --format=%ct) )) -gt 7200 ] && break
    sleep 600; done
fi

RO=(Read Grep Glob Agent)
for c in ps tail head grep jq ls du stat cat find wc date sort uniq cut tr diff "git log" "git show" "git diff" \
  "git -C * log" "git -C * show" "git -C * diff" "git blame" "git -C * blame"; do RO+=("Bash($c:*)"); done
NO=(Edit Write NotebookEdit WebFetch WebSearch "Bash(tmux:*)" "Bash(kill:*)" "Bash(pkill:*)" "Bash(rm:*)"
    "Bash(mv:*)" "Bash(cp:*)" "Bash(tt-smi:*)" "Bash(find * -delete*)" "Bash(find * -exec*)" "Bash(find * -ok*)"
    "Bash(find * -fprint*)" "Bash(find * -fls*)")

prompt() { cat <<EOF
A Claude Code or Codex session brought a model up on Tenstorrent hardware (Blackhole, QB2) in the tt-metal checkout
$TT; the run is finished. Agents doing such runs read the \`ttnn-knowledge\` skill: facts about the hardware and the
software stack that cost earlier runs time. Your job is to propose what, if anything, this run shows that skill
should gain, change or lose. Only read; change nothing. A person reviews your answer.

Where things are:
- the run's notes: $BRINGUP/lessons.md (the brief asked: "When something took long to understand and is solved,
  add a few lines to bringup/lessons.md"), its REPORT.md and doc/, and its commits (git -C $TT log; the base is
  base_commit in $BRINGUP/agentic_evals/inputs.json when present, else the merge base with origin/main).
- the transcripts: $TR. Claude Code: <session>.jsonl (records with .timestamp, .message.content), subagents in
  <session>/subagents/*.jsonl, large outputs in <session>/tool-results/. Codex: sessions/YYYY/MM/DD/rollout-*.jsonl
  (records with .timestamp, .type, .payload); keep only rollouts whose session_meta cwd is in $RUN_DIR. They are
  large: query them with jq and grep, never read them whole. Subagents (Agent tool) help to split the reading.
- the knowledge the run had: $RKB. The current knowledge to propose against: $KB.
- Bash runs only simple read-only commands (ls, cat, head, tail, grep, jq, wc, find, stat, sort, uniq, cut, tr,
  diff, git log/show/diff/blame) and pipes between them; loops, \$(...) and redirections are denied.

1. Where the run lost time. Start from lessons.md, then check the transcripts for problems that took long and are
   not in it. For each: when it started and ended (transcript times), what the agent tried, what in the end showed
   the cause, and how it was solved, if it was.
2. Keep only what the next run on another model would plausibly need. The bar is high: the agent spent hours, or
   several failed attempts, and the cause lies in the hardware or the stack (tt-metal, TTNN, the vLLM plugin, the
   eval harness, the host), not in this model's own code or config. A fact the agent found quickly, a fact about
   this model only, and a fact already in the current knowledge are not proposed (for one already there, say only
   whether the run read it). Proposing nothing is a good outcome when nothing clears the bar.
3. Write each proposal as the knowledge that was missing: what about the hardware or the stack the agent did not
   know, that would have saved the time had it known. Not the story of the run. Label it:
   - hardware: a limit of the chip or board that will not change;
   - tt-metal: low-level behaviour an agent is unlikely to change itself, which a newer tt-metal can change; give
     the base commit it was seen on;
   - stack: code the agent can change (model code, the vLLM plugin or integration, eval harness, scripts).
   Then one line on how this run solved it, as one way that worked ("one run did X"), never as the only right way;
   if the record does not make the solution clear, say "solution unclear from the record".
4. How the run used the knowledge it had. Find each read of a file under ttnn-knowledge (Read calls, skill loads,
   cat or grep of its paths) by the main session and subagents. For each topic read: when, what the agent was
   working on, and what the record shows next: acted on it (and whether that helped), no visible effect, misled
   (followed it and lost time), contradicted (the run found it wrong), or stale (fixed in the run's tt-metal). A
   single run is weak evidence: report what you saw, with the transcript time, and judge only what the record shows.
5. The lessons file against the brief: in one or two lines, did it hold what the brief asked (length, focus,
   problems that took long), and what was missing or extra.

Answer in markdown, without preamble, exactly these sections:
# Knowledge harvest: <model>, <run folder>
One line: tt-metal base, skills commit the run used, run dates and hours, transcript coverage (say if a part of the
run is missing).
## Proposals
For each (P1, P2, ...): a one-line title; Topic: the topics/ file and whether it is a new entry or a change to (or
removal of) a named entry; Label; Text: the entry as it would read in the topic, a few lines in the topic's style
(a heads-up, not a rule; no numbers you did not see in the record); Cost in this run: hours and the transcript
times; Why another model would hit it; How this run solved it.
## Not proposed
One line per lesson or time sink left out, with the reason (model-specific, found quickly, already covered by
<entry>, not confirmed by the record).
## Knowledge used
A table: topic or entry | read at | working on | effect (acted and helped / no visible effect / misled /
contradicted / stale / never read) | evidence. Topics never read go in one line under it.
## Lessons file vs brief
One or two lines.
EOF
}

if [ -n "$DRY" ]; then prompt; echo "---"; echo "transcripts $TR; knowledge $KB; run knowledge $RKB; out $OUT"; exit 0; fi
mkdir -p "$OUT"; err=$(mktemp)
dirs=("$TT" "$RUN_DIR" "$TR" "$KB" "$RKB")
out=$(cd "$RUN_DIR" && timeout 7200 claude -p --model "$MODEL" --no-session-persistence --output-format json \
    --restricted --strict-mcp-config --disable-slash-commands --permission-mode dontAsk --permission-prompts none \
    --tools "Read,Grep,Glob,Bash,Agent" --add-dir "${dirs[@]}" --allowedTools "${RO[@]}" --disallowedTools "${NO[@]}" \
    -- "$(prompt)" 2>"$err")
rc=$?
res=$(printf '%s' "$out" | jq -r 'select(.is_error == false) | .result' 2>/dev/null)
cost=$(printf '%s' "$out" | jq -r '"\(.duration_ms/60000|floor) min, $\(.total_cost_usd*100|round/100)"' 2>/dev/null)
if [ $rc -ne 0 ] || [ -z "${res//[[:space:]]/}" ]; then
  { echo "harvest failed (rc $rc)"; tail -c 2000 "$err"; printf '%s' "$out" | tail -c 2000; } > "$OUT/harvest.err"
  rm -f "$err"; echo "harvest failed (rc $rc), see $OUT/harvest.err" >&2; exit 1; fi
rm -f "$err"
{ printf '%s\n\n' "$res"; echo "_Harvested $(date -u +%FT%TZ) by $MODEL ($cost); knowledge compared: $KB._"; } > "$OUT/harvest.md"
echo "wrote $OUT/harvest.md ($cost)"
