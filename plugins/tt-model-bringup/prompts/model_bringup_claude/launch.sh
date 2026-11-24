#!/usr/bin/env bash
# Start a Claude bring-up run in tmux from a few inputs. See README.md.
#   launch.sh --hf-model org/name --tt-metal /path/to/tt-metal [--run-dir DIR] [--config-dir DIR]
#             [--effort LEVEL] [--budget 48]
#             [--scope open|contained] [--resets yes|no] [--notes file] [--inputs '{"targets": {...}, "evals": [...]}']
#             [--session name] [--overseer-interval hours] [--no-overseer] [--dry-run]
# --dry-run writes inputs.json, prompt.md and the config dir, and prints the commands instead of starting them.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); PLUGINS=$(cd "$HERE/../../.." && pwd)
HF_MODEL= TT= BUDGET=48 SCOPE=open RESETS=no NOTES= EXTRA='{}' SESSION= MODEL=${CLAUDE_MODEL:-claude-opus-5-5}
OVERSEER=1 CONFIG_DIR= EFFORT= DRY= RUN_DIR=
while [ $# -gt 0 ]; do case $1 in
  --no-overseer) OVERSEER=; shift; continue;; --dry-run) DRY=1; shift; continue;;
  --overseer-interval) OVERSEER=$2;;
  --hf-model) HF_MODEL=$2;; --tt-metal) TT=$(cd "$2" && pwd);; --budget) BUDGET=$2;; --scope) SCOPE=$2;;
  --resets) RESETS=$2;; --notes) NOTES=$2;; --inputs) EXTRA=$2;; --session) SESSION=$2;;
  --config-dir) CONFIG_DIR=$2;; --run-dir) RUN_DIR=$(cd "$2" && pwd);; --effort) EFFORT=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$HF_MODEL" ] && [ -n "$TT" ] || { echo "need --hf-model and --tt-metal" >&2; exit 2; }
MODEL_DIR=$(echo "$HF_MODEL" | tr 'A-Z' 'a-z' | tr '/.-' '___')
SESSION=${SESSION:-bringup-$MODEL_DIR}
RUN_DIR=${RUN_DIR:-$(dirname "$TT")}
RUN="$TT/models/autoports/$MODEL_DIR/bringup/agentic_evals"; mkdir -p "$RUN"

# A dedicated Claude config dir: only the login, a theme, onboarding done and the folder trust below. Reused if present.
if [ -n "$CONFIG_DIR" ]; then
  mkdir -p "$CONFIG_DIR"; chmod 700 "$CONFIG_DIR"; CONFIG_DIR=$(cd "$CONFIG_DIR" && pwd)
  SRC=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
  if [ ! -f "$CONFIG_DIR/.credentials.json" ]; then
    C="$SRC/.credentials.json"; [ -f "$C" ] || C="$HOME/.claude/.credentials.json"
    [ -f "$C" ] || { echo "no login file at $C: log in once with claude, or CLAUDE_CONFIG_DIR=$CONFIG_DIR claude" >&2; exit 2; }
    install -m 600 "$C" "$CONFIG_DIR/.credentials.json"
  fi
  # The user's .claude.json is $CLAUDE_CONFIG_DIR/.claude.json when set, else ~/.claude.json.
  UJ="$HOME/.claude.json"; [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -f "$CLAUDE_CONFIG_DIR/.claude.json" ] && UJ="$CLAUDE_CONFIG_DIR/.claude.json"
  python3 - "$CONFIG_DIR" "$UJ" "$SRC/settings.json" <<'P'
import json, os, sys
os.umask(0o077)
d, uj, us = sys.argv[1:]
load = lambda p: json.load(open(p)) if os.path.exists(p) else {}
s = os.path.join(d, "settings.json")
if not os.path.exists(s):
    json.dump({"theme": load(us).get("theme", "dark")}, open(s, "w"), indent=2)
j = os.path.join(d, ".claude.json")
if not os.path.exists(j):
    u = load(uj)
    c = {"hasCompletedOnboarding": True, **{k: u[k] for k in ("oauthAccount", "userID") if k in u}}
    json.dump(c, open(j, "w"), indent=2)
P
  export CLAUDE_CONFIG_DIR=$CONFIG_DIR  # for the folder trust, the overseer and the session below
fi

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

# Working directories besides the checkout: the run dir (helper folders next to the checkout) and HF_HOME outside it.
DIRS=("$RUN_DIR"); case ${HF_HOME:-} in ""|"$RUN_DIR"|"$RUN_DIR"/*) ;; *) DIRS+=("$HF_HOME");; esac
# Accept folder trust ahead (the checkout and the added dirs), so the run does not wait on a dialog.
J="$HOME/.claude.json"; [ -n "${CLAUDE_CONFIG_DIR:-}" ] && J="$CLAUDE_CONFIG_DIR/.claude.json"
python3 - "$J" "$TT" "${DIRS[@]}" <<'P'
import json, os, sys
p, *ds = sys.argv[1:]
j = json.load(open(p)) if os.path.exists(p) else {}
for d in ds:
    pr = j.setdefault("projects", {}).setdefault(d, {})
    pr["hasTrustDialogAccepted"] = True; pr["hasCompletedProjectOnboarding"] = True
t = p + ".tmp"; json.dump(j, open(t, "w"), indent=2)
os.chmod(t, os.stat(p).st_mode if os.path.exists(p) else 0o600); os.replace(t, p)
P

# A tmux server that is already running does not pass the caller's environment to new sessions, so the variables the
# run depends on are set in the session's command. tmux, the watchdog and the overseer below all use the caller's
# TMUX_TMPDIR (or TMUX), so they reach the same server.
ENVS=
for v in CLAUDE_CONFIG_DIR HF_HOME HF_TOKEN_PATH; do
  if [ -n "${!v:-}" ]; then ENVS+="export $v=$(printf %q "${!v}"); "; fi; done
ADD=; for d in "${DIRS[@]}"; do ADD+=" --add-dir $(printf %q "$d")"; done
CMD="${ENVS}[ -f python_env/bin/activate ] && source python_env/bin/activate
  claude --model $MODEL${EFFORT:+ --effort $EFFORT} --permission-mode auto$ADD --plugin-dir $(printf %q "$PLUGINS/tt-model-bringup") --plugin-dir $(printf %q "$PLUGINS/tt-autodebug") -- \"\$(cat $(printf %q "$PROMPT"))\"
  exec bash"
TMUX_CMD=(tmux new-session -d -s "$SESSION" -c "$TT" bash -lc "$CMD")
# Budget watchdog: tell the session when the budget is reached.
WATCH="sleep \$(python3 -c 'print(int($BUDGET*3600))'); tmux send-keys -t $SESSION -l 'Budget reached: start nothing new; finish and commit REPORT.md.'; tmux send-keys -t $SESSION Enter"
# Overseer: every OVERSEER hours, a read-only agent reports on subagents, jobs, devices, CPU, disk and budget.
OVS=("$HERE/overseer.sh" --session "$SESSION" --tt "$TT" --inputs "$RUN/inputs.json" --interval "${OVERSEER:-1}" --started-at "$(date +%s)")

if [ -n "$DRY" ]; then
  echo "dry run; CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR:-unset} (trust written to $J), prompt $PROMPT"
  printf '%q ' "${TMUX_CMD[@]}"; echo; echo "watchdog: bash -c $(printf %q "$WATCH")"
  [ -n "$OVERSEER" ] && { printf 'overseer: '; printf '%q ' "${OVS[@]}"; echo; }
  exit 0
fi
"${TMUX_CMD[@]}"
nohup bash -c "$WATCH" >/dev/null 2>&1 &
[ -n "$OVERSEER" ] && nohup "${OVS[@]}" >/dev/null 2>&1 &
echo "started tmux session $SESSION (prompt: $PROMPT${CLAUDE_CONFIG_DIR:+, config $CLAUDE_CONFIG_DIR})${OVERSEER:+; overseer every $OVERSEER h, log $RUN/overseer.log}"
