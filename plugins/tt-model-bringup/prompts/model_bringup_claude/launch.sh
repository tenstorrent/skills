#!/usr/bin/env bash
# Start a Claude bring-up run in tmux from a few inputs. See README.md.
#   launch.sh --hf-model org/name --tt-metal /path/to/tt-metal [--budget 48] [--scope open|contained]
#             [--resets yes|no] [--notes file] [--inputs '{"targets": {...}, "evals": [...]}'] [--session name]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); PLUGINS=$(cd "$HERE/../../.." && pwd)
HF_MODEL= TT= BUDGET=48 SCOPE=open RESETS=no NOTES= EXTRA='{}' SESSION= MODEL=${CLAUDE_MODEL:-claude-opus-5-5}
while [ $# -gt 0 ]; do case $1 in
  --hf-model) HF_MODEL=$2;; --tt-metal) TT=$(cd "$2" && pwd);; --budget) BUDGET=$2;; --scope) SCOPE=$2;;
  --resets) RESETS=$2;; --notes) NOTES=$2;; --inputs) EXTRA=$2;; --session) SESSION=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$HF_MODEL" ] && [ -n "$TT" ] || { echo "need --hf-model and --tt-metal" >&2; exit 2; }
MODEL_DIR=$(echo "$HF_MODEL" | tr 'A-Z' 'a-z' | tr '/.-' '___')
SESSION=${SESSION:-bringup-$MODEL_DIR}
RUN="$TT/models/autoports/$MODEL_DIR/bringup/agentic_evals"; mkdir -p "$RUN"

python3 - "$RUN/inputs.json" "$SCOPE" "$BUDGET" "$(git -C "$TT" rev-parse HEAD)" "$EXTRA" <<'P'
import json, sys
path, scope, budget, base, extra = sys.argv[1:]
d = {"scope": scope, "budget_hours": float(budget), "batch_sizes": [1, 8, 16, 32], "base_commit": base}
d.update(json.loads(extra))
json.dump(d, open(path, "w"), indent=2)
P

PROMPT="$RUN/prompt.md"
sed "s|HF_MODEL|$HF_MODEL|g" "$HERE/brief.md" > "$PROMPT"
{ echo; echo "## This run"; echo
  echo "- Machine: $(hostname). Work only in $TT and its worktrees. HF_HOME=${HF_HOME:-~/.cache/huggingface}."
  echo "- Budget: $BUDGET h from now; write started_at (UTC) at the start."
  if [ "$RESETS" = yes ]; then echo "- Bounded board resets (tt-smi -r, whole board) and multi-chip health checks are authorized."
  else echo "- Board resets are not authorized on this machine."; fi
  [ -n "$NOTES" ] && cat "$NOTES"; } >> "$PROMPT"

# Accept folder trust ahead, so the run does not wait on a dialog.
CFG=${CLAUDE_CONFIG_DIR:-$HOME}; J="$CFG/.claude.json"; [ -n "${CLAUDE_CONFIG_DIR:-}" ] || J="$HOME/.claude.json"
python3 - "$J" "$TT" <<'P'
import json, os, sys
p, d = sys.argv[1:]
j = json.load(open(p)) if os.path.exists(p) else {}
pr = j.setdefault("projects", {}).setdefault(d, {})
pr["hasTrustDialogAccepted"] = True; pr["hasCompletedProjectOnboarding"] = True
t = p + ".tmp"; json.dump(j, open(t, "w"), indent=2); os.replace(t, p)
P

tmux new-session -d -s "$SESSION" -c "$TT" bash -lc "
  [ -f python_env/bin/activate ] && source python_env/bin/activate
  claude --model $MODEL --permission-mode auto --plugin-dir $PLUGINS/tt-model-bringup --plugin-dir $PLUGINS/tt-autodebug \"\$(cat $PROMPT)\"
  exec bash"
# Budget watchdog: tell the session when the budget is reached.
nohup bash -c "sleep \$(python3 -c 'print(int($BUDGET*3600))'); tmux send-keys -t $SESSION -l 'Budget reached: start nothing new; finish and commit REPORT.md.'; tmux send-keys -t $SESSION Enter" >/dev/null 2>&1 &
echo "started tmux session $SESSION (prompt: $PROMPT)"
