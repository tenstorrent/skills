#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROMPT_TEMPLATE="$SCRIPT_DIR/../references/AUTODEBUG_PROMPT.md"
DEFAULT_AGENT="codex"
if [[ -n "${CLAUDECODE:-}" || "$SCRIPT_DIR" == *"/.claude/plugins/"* ]]; then
    DEFAULT_AGENT="claude"
fi
AGENT="${AUTODEBUG_AGENT:-$DEFAULT_AGENT}"
CODEX_MODEL="${AUTODEBUG_CODEX_MODEL:-}"
CLAUDE_MODEL="${AUTODEBUG_CLAUDE_MODEL:-}"
EFFORT="${AUTODEBUG_EFFORT:-xhigh}"
RUN_DIR="$(pwd -P)"
FOCUS_PATHS=()

usage() {
    cat <<'USAGE'
Usage:
  autodebug.sh [options] [--] <problem...>

Run a fresh, inspection-only AutoDebug investigation in the current checkout.
The child agent writes ./AUTODEBUG.md.

Options:
  --focus PATH            Add a focus path. May be repeated.
  --agent codex|claude    Override the inferred agent CLI.
  --model MODEL           Override the selected agent's configured model.
  --effort LEVEL          Reasoning effort. Default: xhigh.
  --help                  Show this help.

Environment:
  AUTODEBUG_ALLOW_UNSANDBOXED=1
    Permit unsandboxed Codex only after a recognized sandbox startup failure.
    Set only with user/operator approval for this environment. Default: 0.

Examples:
  autodebug.sh --focus models/demos/foo -- "decode diverges after token 128"
  autodebug.sh --agent claude -- "why does this test hang?"
USAGE
}

die() {
    echo "autodebug.sh: $*" >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --focus)
            [[ $# -ge 2 ]] || die "--focus requires a path"
            FOCUS_PATHS+=("$2")
            shift 2
            ;;
        --focus=*)
            FOCUS_PATHS+=("${1#*=}")
            shift
            ;;
        --agent)
            [[ $# -ge 2 ]] || die "--agent requires codex or claude"
            AGENT="$2"
            shift 2
            ;;
        --agent=*)
            AGENT="${1#*=}"
            shift
            ;;
        --model)
            [[ $# -ge 2 ]] || die "--model requires a value"
            CODEX_MODEL="$2"
            CLAUDE_MODEL="$2"
            shift 2
            ;;
        --model=*)
            CODEX_MODEL="${1#*=}"
            CLAUDE_MODEL="${1#*=}"
            shift
            ;;
        --effort|--thinking|--thinking-level)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            EFFORT="$2"
            shift 2
            ;;
        --effort=*|--thinking=*|--thinking-level=*)
            EFFORT="${1#*=}"
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option: $1"
            ;;
        *)
            break
            ;;
    esac
done

[[ $# -gt 0 ]] || die "provide a problem description"
[[ -f "$PROMPT_TEMPLATE" ]] || die "bundled prompt not found: $PROMPT_TEMPLATE"

PROBLEM="$*"
PROMPT_FILE="$(mktemp "${TMPDIR:-/tmp}/autodebug-prompt.XXXXXX")"
trap 'rm -f "$PROMPT_FILE"' EXIT

# The conditional expansion also works for an empty array under Bash 3.2's set -u.
python3 - "$PROMPT_TEMPLATE" "$PROBLEM" ${FOCUS_PATHS[@]+"${FOCUS_PATHS[@]}"} >"$PROMPT_FILE" <<'PY'
from pathlib import Path
import sys

template_path = Path(sys.argv[1])
problem = sys.argv[2]
focus_paths = sys.argv[3:]

template = template_path.read_text(encoding="utf-8")
if focus_paths:
    focus_section = "Focus paths:\n\n" + "".join(f"- `{path}`\n" for path in focus_paths) + "\n"
else:
    focus_section = ""
rendered = template.replace("{{FOCUS_PATH_SECTION}}", focus_section)
rendered = rendered.replace("{{PROBLEM}}", problem)

missing = [token for token in ("{{FOCUS_PATH_SECTION}}", "{{PROBLEM}}") if token in rendered]
if missing:
    raise SystemExit(f"unrendered prompt placeholder(s): {', '.join(missing)}")

print(
    "You are the AutoDebug investigator, already running in a fresh isolated session. "
    "Perform the investigation here and write AUTODEBUG.md. "
    "Do not invoke the AutoDebug launcher again.\n"
)
print(rendered.strip())
PY

# Keep the prompt readable on stdin without leaving its file behind after exec.
exec <"$PROMPT_FILE"
rm -f "$PROMPT_FILE"
trap - EXIT

case "$AGENT" in
    [cC][oO][dD][eE][xX])
        command -v codex >/dev/null 2>&1 || die "codex executable not found"
        SANDBOX="$(python3 "$SCRIPT_DIR/codex_sandbox.py")"
        COMMAND=(codex --approve-for-me exec)
        if [[ "$SANDBOX" == "danger-full-access" ]]; then
            COMMAND=(codex --ask-for-approval never exec)
        fi
        [[ -z "$CODEX_MODEL" ]] || COMMAND+=(--model "$CODEX_MODEL")
        COMMAND+=(
            -c "model_reasoning_effort=$EFFORT"
            --sandbox "$SANDBOX"
            --skip-git-repo-check
            --color never
            --cd "$RUN_DIR"
            -
        )
        exec "${COMMAND[@]}"
        ;;
    [cC][lL][aA][uU][dD][eE])
        command -v claude >/dev/null 2>&1 || die "claude executable not found"
        COMMAND=(claude -p --output-format text)
        [[ -z "$CLAUDE_MODEL" ]] || COMMAND+=(--model "$CLAUDE_MODEL")
        COMMAND+=(--effort "$EFFORT" --permission-mode auto)
        exec "${COMMAND[@]}"
        ;;
    *)
        die "--agent must be codex or claude, got: $AGENT"
        ;;
esac
