#!/usr/bin/env bash
# Prepare a run folder for a Claude bring-up run: tt-metal on a new branch, the skills, the build, the venv, the weights.
#   setup.sh --run-dir DIR --hf-model org/name [--revision REV] [--tt-metal-ref REF] [--skills-ref REF] [--hf-home DIR]
#            [--dry-run]
# Each step is logged with a UTC time to DIR/setup.out (with its output); the last line is SETUP_DONE or
# SETUP_FAILED at <step>. A clone that already exists is kept, so a failed setup can be run again.
# --skills-ref defaults to the commit this script comes from. Override the clone URLs with TT_METAL_URL, SKILLS_URL.
# --dry-run prints the steps instead of running them.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
RUN_DIR= HF_MODEL= REV= TT_REF= SK_REF= DRY=
TT_METAL_URL=${TT_METAL_URL:-https://github.com/tenstorrent/tt-metal.git}
SKILLS_URL=${SKILLS_URL:-https://github.com/tenstorrent/skills.git}
while [ $# -gt 0 ]; do case $1 in
  --dry-run) DRY=1; shift; continue;;
  --run-dir) RUN_DIR=$2;; --hf-model) HF_MODEL=$2;; --revision) REV=$2;; --tt-metal-ref) TT_REF=$2;;
  --skills-ref) SK_REF=$2;; --hf-home) export HF_HOME=$2;;
  *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$RUN_DIR" ] && [ -n "$HF_MODEL" ] || { echo "need --run-dir and --hf-model" >&2; exit 2; }
if [ -z "$SK_REF" ]; then
  SK_REF=$(git -C "$HERE" rev-parse HEAD) || { echo "not in a skills checkout: pass --skills-ref" >&2; exit 2; }
  [ -z "$(git -C "$HERE" status --porcelain -- .)" ] || echo "note: $HERE has uncommitted changes; the run gets $SK_REF" >&2
fi
MODEL_DIR=$(echo "$HF_MODEL" | tr 'A-Z' 'a-z' | tr '/.-' '___')
mkdir -p "$RUN_DIR"; RUN_DIR=$(cd "$RUN_DIR" && pwd)
TT=$RUN_DIR/tt-metal SK=$RUN_DIR/skills OUT=$RUN_DIR/setup.out

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | if [ -n "$DRY" ]; then cat; else tee -a "$OUT"; fi; }
step() {  # step NAME COMMAND...: run it with its output in setup.out, stop at the first failure
  local name=$1; shift
  if [ -n "$DRY" ]; then printf '%s: ' "$name"; printf '%q ' "$@"; echo; return; fi
  log "start $name"
  if "$@" >>"$OUT" 2>&1; then log "done $name"; else log "SETUP_FAILED at $name (rc $?, output above in $OUT)"; exit 1; fi
}

log "setup $HF_MODEL${REV:+ at $REV} in $RUN_DIR; tt-metal ${TT_REF:-default branch}, skills $SK_REF, HF_HOME=${HF_HOME:-default}"
if [ -d "$TT/.git" ]; then log "kept existing $TT"; else
  step clone-tt-metal git clone "$TT_METAL_URL" "$TT"
  step branch git -C "$TT" checkout -b "bringup/$MODEL_DIR" ${TT_REF:+"$TT_REF"}
  step submodules git -C "$TT" submodule update --init --recursive
fi
if [ -d "$SK/.git" ]; then log "kept existing $SK"; else
  step clone-skills git clone "$SKILLS_URL" "$SK"
  step skills-ref git -C "$SK" checkout -q "$SK_REF"
fi
step build env -C "$TT" nice ./build_metal.sh
step venv env -C "$TT" nice ./create_venv.sh
step weights env -C "$TT" python_env/bin/python -c \
  'import sys; from huggingface_hub import snapshot_download; print(snapshot_download(sys.argv[1], revision=sys.argv[2] or None))' \
  "$HF_MODEL" "$REV"
log "SETUP_DONE"
