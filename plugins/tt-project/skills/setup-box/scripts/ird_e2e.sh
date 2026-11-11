#!/usr/bin/env bash
# setup-box IRD route, end to end (ird.md): run it where the IRD management host is reachable.
#
#   ird_e2e.sh [--box <alias>] [--mgmt <alias>] [--pool <pool>] [--arch <arch>] [--workdir <path>]
#              [--repo <git-url>] [--timeout hh:mm:ss] [--extend-min N] [--site <file>] [--dry-run]
#
# Values come from the box's entry in the site file (box.py site, `ird.arch` for the arch), and
# flags override them. Steps: 1 pick a free, non-draining machine; 2 reserve it; 3 ssh in through
# the managed ssh include (ProxyJump); 4 clone a small public repo into a per-run worktree, edit
# it, build a trivial target, run `tt-smi -s`; 5 extend once; 6 release and check `ird list`.
# Before step 2 it snapshots your reservations; on exit, failure, Ctrl-C or TERM the trap diffs
# them again and releases every new one this run's box.py claimed, never one without that claim.
# Every ssh (box.py's too) runs with -F <its own config>: an Include of the managed ssh-hosts file
# and the management host from the site's ird.mgmt_ssh, never the personal ~/.ssh/config, so a
# personal Host block cannot hide a gap in the managed setup.
# --dry-run runs step 1 only and prints the rest. Ends with a short report to paste back.
# Needs bash 3.2+, python3 and ssh. Exit: 0 pass, 1 a step failed, 2 bad usage, 130 interrupted.

set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BOX_PY="$HERE/box.py"
SITE_IN=${TTP_BOXES_FILE:-$HOME/.tt-project/boxes.json}
IRD_DIR=${TTP_IRD_DIR:-$HOME/.tt-project/ird}
BOX="" MGMT="" POOL="" ARCH="" WORKDIR="" DRY=0
REPO="https://github.com/octocat/Hello-World.git"
TIMEOUT="00:30:00" EXTEND_MIN=15

usage() { sed -n '4,5p' "$0" | sed 's/^# *//'; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --box) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; BOX=${2:-}; shift 2 ;;
    --mgmt) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; MGMT=${2:-}; shift 2 ;;
    --pool) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; POOL=${2:-}; shift 2 ;;
    --arch) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; ARCH=${2:-}; shift 2 ;;
    --workdir) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; WORKDIR=${2:-}; shift 2 ;;
    --repo) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; REPO=${2:-}; shift 2 ;;
    --timeout) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; TIMEOUT=${2:-}; shift 2 ;;
    --extend-min) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; EXTEND_MIN=${2:-}; shift 2 ;;
    --site) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; SITE_IN=${2:-}; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ird-e2e.XXXXXX") || { echo "cannot make a temp dir" >&2; exit 1; }
SITE="$RUN_DIR/boxes.json"
REPORT="$RUN_DIR/report.txt"
# box.py claims: this run is one project task, so release passes its ownership gate.
export TTP_PROJECT="ird-e2e" TTP_TASK="ird-e2e-$RUN_ID"
unset TTP_RUN_DIR

RID="" KH="" MACHINE="" REMOTE_ROOT="" RESERVING=0 STEP="setup" RESULT="" RELEASED="n/a"
BEFORE_IDS="" RES_LINE=""
NOTES=()

say() { printf '%s\n' "$*"; }
note() { NOTES+=("$*"); say "$*"; }
fail() { RESULT="FAIL at $STEP: $*"; exit 1; }
# A hung ssh or box.py call ends at its limit (GNU timeout, kept in the run's process group so ^C and
# the trap still reach it); without timeout (stock macOS) a call runs unbounded.
if timeout --foreground 5 true 2>/dev/null; then lim() { timeout --foreground -k 10 "$@"; }; else lim() { shift; "$@"; }; fi
box() { lim 900 python3 "$BOX_PY" --site "$SITE" "$@"; }
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
SSH_CFG="$RUN_DIR/ssh_config"
export TTP_SSH_CONFIG="$SSH_CFG"  # box.py runs its ssh with -F "$TTP_SSH_CONFIG"
on_machine() { lim 900 ssh -F "$SSH_CFG" -n -o BatchMode=yes -o ConnectTimeout=20 "$MACHINE" "$1"; }
ird_rows() { lim 120 ssh -F "$SSH_CFG" -n -o BatchMode=yes -o ConnectTimeout=20 "$MGMT" 'ird list --noheader'; }
row_of() { ird_rows | awk -v id="$1" '$1 == id' ; }

report() {
  {
    say "--- ird-e2e report (paste this back) ---"
    say "when: $(date '+%Y-%m-%d %H:%M %Z')  host: $(uname -s) $(uname -r)  $(lim 30 ssh -V 2>&1)"
    say "plugin: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
      "$HERE/../../../.claude-plugin/plugin.json" 2>/dev/null || echo '?')  mode: $([ "$DRY" = 1 ] && echo dry-run || echo live)"
    say "pool: ${POOL:-?}  arch: ${ARCH:-?}  timeout: $TIMEOUT  repo: $REPO"
    for l in "${NOTES[@]+"${NOTES[@]}"}"; do say "$l"; done
    say "reservation: ${RES_LINE:-none}  released: $RELEASED"
    say "result: ${RESULT:-PASS}"
    say "--- end ---"
  } > "$REPORT"
  say ""
  cat "$REPORT"
  say "(saved to $REPORT)"
}

ids_of() { awk '$1 ~ /^[0-9]+$/ {printf "%s ", $1}'; }

# Diff your reservations against the snapshot taken before step 2 and release each new one that
# this run's box.py claimed (task $TTP_TASK). A new one without that claim is left alone, and said.
cleanup_reservations() {
  local st id m rest made="" rel="" n=0 one=""
  if ! st=$(box ird status "$BOX" 2>&1); then
    if [ -n "$RID" ] && [ "$RELEASED" != "yes" ]; then
      say "releasing reservation $RID (made by this run) ..."
      if box ird release "$BOX" "$RID"; then RELEASED="yes (by the trap)"
      else RELEASED="NO: release it by hand: python3 $BOX_PY ird release $BOX $RID"; fi
    else
      RELEASED="unknown: box.py ird status failed; check python3 $BOX_PY ird status $BOX"
    fi
    return
  fi
  while read -r id m rest; do
    case "$id" in ''|*[!0-9]*) continue ;; esac
    case " $BEFORE_IDS " in *" $id "*) continue ;; esac
    n=$((n + 1)); one=$id; made="$made, $id on $m"
    case "$rest" in
      *"task='$TTP_TASK'"*)
        say "releasing reservation $id (made by this run) ..."
        if box ird release "$BOX" "$id"; then rel="$rel, $id yes (by the trap)"
        else rel="$rel, $id NO: release it by hand: python3 $BOX_PY ird release $BOX $id"; fi ;;
      *) rel="$rel, $id no: it has no claim of this run, so the trap leaves it alone; if it is yours, release it with ird release $id" ;;
    esac
  done <<< "$st"
  if [ $n -gt 0 ]; then
    RES_LINE=${made#, }
    if [ $n -eq 1 ]; then RELEASED=${rel#, "$one" }; else RELEASED=${rel#, }; fi
  elif [ -z "$RES_LINE" ] && grep -q '^released ' "$RUN_DIR/reserve.out" 2>/dev/null; then
    RES_LINE="$(sed -n 's/^released \([0-9]*\) \([^ ]*\).*/\1 on \2/p' "$RUN_DIR/reserve.out" | head -n 1)"
    RELEASED="yes (by box.py reserve, which was cut off)"
  elif [ -n "$RID" ] && [ "$RELEASED" != "yes" ]; then
    RELEASED="yes (gone from ird list)"
  fi
}

on_exit() {
  local rc=$?
  trap '' INT TERM HUP
  exec 1>&3 2>&4  # a signal during a redirected step runs this inside that redirect
  [ $rc -ne 0 ] && [ -z "$RESULT" ] && RESULT="FAIL at $STEP (exit $rc)"
  if [ -n "$REMOTE_ROOT" ]; then
    on_machine "rm -rf -- $(q "$REMOTE_ROOT")" >/dev/null 2>&1 || note "cleanup: could not remove $REMOTE_ROOT"
  fi
  [ "$RESERVING" = 1 ] && cleanup_reservations
  if [ -n "$KH" ] && [ -e "$KH" ]; then note "cleanup: per-reservation known_hosts $KH is still there"; fi
  report
  exit $rc
}
exec 3>&1 4>&2
trap on_exit EXIT
trap 'RESULT="interrupted at $STEP"; exit 130' INT TERM HUP

# --- values: the site entry, then flags; one temp site file for box.py ---------------------------
STEP="values"
command -v python3 >/dev/null || fail "python3 not found"
command -v ssh >/dev/null || fail "ssh not found"
# The helper goes to a file first: bash 3.2 (macOS) misparses a heredoc inside $(...).
cat > "$RUN_DIR/values.py" <<'PY'
import json, os, sys
src, alias, mgmt, pool, arch, workdir, out, include, ssh_cfg = sys.argv[1:]
try:
    data = json.load(open(os.path.expanduser(src)))
except FileNotFoundError:
    data = {}
except ValueError as e:
    sys.exit(f"site file {src} is not JSON: {e}")
if isinstance(data, dict) and "boxes" not in data and "version" not in data and any(
        isinstance(v, dict) and "route" in v for v in data.values()):
    sys.exit(f"site file {src}: missing version/boxes wrapper: it must look like "
             '{"version": 1, "boxes": {"<alias>": {"route": "ird", ...}}} (see site.md)')
boxes = (data.get("boxes") if isinstance(data, dict) else None) or {}
if not alias:
    ird = [k for k, v in boxes.items() if isinstance(v, dict) and v.get("route") == "ird"]
    if len(ird) == 1:
        alias = ird[0]
    elif len(ird) > 1:
        sys.exit(f"the site file has {len(ird)} ird boxes: pick one with --box")
if alias and alias not in boxes and not mgmt:
    sys.exit(f"no box {alias!r} in {src}")
entry = dict(boxes.get(alias) or {}) if alias else {}
cfg = dict(entry.get("ird") or {})
pools = list(cfg.get("pools") or [])
mgmt = mgmt or cfg.get("mgmt", "")
pool = pool or (pools[0] if pools else "")
arch = arch or cfg.get("arch", "")
workdir = workdir or entry.get("workdir_root", "")
missing = [n for n, v in (("--mgmt", mgmt), ("--pool", pool), ("--arch", arch), ("--workdir", workdir)) if not v]
if missing:
    sys.exit("missing " + ", ".join(missing) + f": set them in the site file ({src}, see site.md) or pass them")
if not workdir.startswith("/"):
    sys.exit("--workdir must be an absolute path on the machine")
alias = alias or "ird-e2e"
cfg.update(mgmt=mgmt, pools=[pool] + [p for p in pools if p != pool])
entry.update(route="ird", workdir_root=workdir, ird=cfg)
json.dump({"version": 1, "boxes": {alias: entry}}, open(out, "w"), indent=2)
# The run's own ssh config: managed content only, never the personal ~/.ssh/config.
keys = ("HostName", "User", "Port", "IdentityFile", "IdentitiesOnly", "CertificateFile", "ProxyJump")
jump = cfg.get("mgmt_ssh") or {}
if not isinstance(jump, dict) or any(k not in keys or "\n" in str(v) or not str(v).strip()
                                     for k, v in jump.items()):
    sys.exit(f"ird.mgmt_ssh must map ssh options ({', '.join(keys)}) to one-line values (site.md)")
lines = [f'Include "{include}"']
if jump:
    lines += ["", f"Host {mgmt}"] + [f'    {k} "{v}"' for k, v in jump.items()]
lines += ["", "Host *", "    LogLevel ERROR"]  # after the Include (a conditional one would scope it); quiets ssh banners
open(ssh_cfg, "w").write("\n".join(lines) + "\n")
print(f"JUMP\t{'site' if jump else ''}")
for k, v in (("BOX", alias), ("MGMT", mgmt), ("POOL", pool), ("ARCH", arch), ("WORKDIR", workdir)):
    print(f"{k}\t{v}")
PY
INCLUDE="$IRD_DIR/ssh-hosts"
VALS=$(python3 "$RUN_DIR/values.py" "$SITE_IN" "$BOX" "$MGMT" "$POOL" "$ARCH" "$WORKDIR" "$SITE" \
  "$INCLUDE" "$SSH_CFG" 2>&1) || fail "$VALS"
JUMP_FROM=""
while IFS=$'\t' read -r k v; do
  case "$k" in BOX) BOX=$v ;; MGMT) MGMT=$v ;; POOL) POOL=$v ;; ARCH) ARCH=$v ;; WORKDIR) WORKDIR=$v ;;
    JUMP) JUMP_FROM=$v ;; esac
done <<< "$VALS"
[[ "$TIMEOUT" =~ ^[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]$ ]] || fail "--timeout must be hh:mm:ss"
[[ "$EXTEND_MIN" =~ ^[0-9]+$ ]] || fail "--extend-min must be whole minutes"
if ! grep -qsF -e "tt-project/ird/ssh-hosts" -e "$INCLUDE" "$HOME/.ssh/config"; then
  [ "$DRY" = 1 ] || fail "$HOME/.ssh/config lacks 'Include $INCLUDE' at its top (ird.md); add it and rerun"
  note "note: ~/.ssh/config lacks 'Include $INCLUDE' at its top; a live run stops here"
fi
# The management host under the run's own config only: no silent fallback to personal ssh config.
if ! ERR=$(lim 120 ssh -F "$SSH_CFG" -n -o BatchMode=yes -o ConnectTimeout=20 "$MGMT" true 2>&1 </dev/null); then
  if [ -z "$JUMP_FROM" ]; then
    fail "cannot reach $MGMT with managed ssh config only ($(printf '%s' "$ERR" | tail -n 1)): the site file has no ird.mgmt_ssh, so nothing managed defines $MGMT. If your personal ~/.ssh/config defines it, copy its HostName, User (and IdentityFile, ProxyJump if needed) into ird.mgmt_ssh (site.md); this run never uses personal ssh config"
  fi
  fail "cannot reach $MGMT with the site's ird.mgmt_ssh: $(printf '%s' "$ERR" | tail -n 2)"
fi
note "ssh: own config $SSH_CFG (Include $INCLUDE${JUMP_FROM:+, $MGMT from ird.mgmt_ssh}), no personal ssh config"

# --- 1 list and pick ------------------------------------------------------------------------------
STEP="1 list"; T0=$(date +%s)
LIST=$(box ird list "$BOX") || fail "box.py ird list: $(printf '%s' "$LIST" | tail -n 3)"
FREE=$(printf '%s\n' "$LIST" | awk '$1 == "free" {print $2}')
MACHINE=$(printf '%s\n' "$FREE" | sed -n 1p)
[ -n "$MACHINE" ] || fail "no free, non-draining machine in pool $POOL"
note "step 1 list: PASS $(printf '%s\n' "$FREE" | grep -c .) free, picked $MACHINE ($(( $(date +%s) - T0 ))s)"

RESERVE=(ird reserve "$BOX" --arch "$ARCH" --timeout "$TIMEOUT" --pool "$POOL" --machine "$MACHINE" --wait 120)
if [ "$DRY" = 1 ]; then
  note "step 2 reserve: DRY box.py ${RESERVE[*]}"
  note "step 3 ssh: DRY ssh -F $SSH_CFG $MACHINE (ProxyJump $MGMT via $INCLUDE, per-reservation known_hosts, accept-new)"
  note "step 4 work: DRY clone $REPO into $WORKDIR/ird-e2e/$RUN_ID, worktree, edit, build, tt-smi -s"
  note "step 5 extend: DRY box.py ird extend $BOX <id> $EXTEND_MIN"
  note "step 6 release: DRY box.py ird release $BOX <id>, then check ird list"
  RESULT="PASS (dry-run: steps 2-6 not run)"
  exit 0
fi

# --- 2 reserve ------------------------------------------------------------------------------------
STEP="2 reserve"; T0=$(date +%s)
# The snapshot the trap diffs against: never trust the reserve's exit code alone.
SNAP=$(box ird status "$BOX") || fail "box.py ird status before reserving: $(printf '%s' "$SNAP" | tail -n 3)"
BEFORE_IDS=$(printf '%s\n' "$SNAP" | ids_of)
PICKED=$MACHINE
RESERVING=1
box "${RESERVE[@]}" > "$RUN_DIR/reserve.out" 2>&1; rc=$?
OUT=$(cat "$RUN_DIR/reserve.out")
RID=$(printf '%s\n' "$OUT" | awk '$1 == "reserved" {print $2; exit}')
while IFS= read -r l; do note "  $l"; done < <(printf '%s\n' "$OUT" | grep -e '^warning: ' -e '^note: ird reserved ')
[ $rc -eq 0 ] && [ -n "$RID" ] || fail "box.py ird reserve (rc $rc): $(printf '%s' "$OUT" | grep -v '^warning: ' | tail -n 3)"
EXPIRES=$(printf '%s\n' "$OUT" | sed -n 's/^reserved .* expires=\([^ ]*\).*/\1/p')
MACHINE=$(printf '%s\n' "$OUT" | sed -n 's/^reserved .* machine=\([^ ]*\).*/\1/p')
RES_LINE="$RID on $MACHINE"
KH="$IRD_DIR/known_hosts/$BOX.$RID"
ROW1=$(row_of "$RID")
LANDED=""  # box.py compares the names ignoring a site prefix or domain; "asked for" only when --machine reached ird
printf '%s\n' "$OUT" | grep -q '^warning: asked for ' && LANDED=" (asked for $PICKED; using the machine ird reserved)"
note "step 2 reserve: PASS id $RID on $MACHINE$LANDED, expires $EXPIRES ($(( $(date +%s) - T0 ))s)"
note "  ird list row: ${ROW1:-<none>}"

# --- 3 ssh in -------------------------------------------------------------------------------------
STEP="3 ssh"; T0=$(date +%s)
grep -qs "^# >>> tt-project ird $RID\$" "$INCLUDE" || fail "no ssh block for $RID in $INCLUDE"
# The options that apply to the machine under the run's own config, for the report.
EFF=$(lim 30 ssh -F "$SSH_CFG" -G "$MACHINE" 2>/dev/null)
opt() { printf '%s\n' "$EFF" | awk -v k="$1" '$1 == k {sub(/^[^ ]+ /, ""); gsub(/"/, ""); print; exit}'; }
note "  ssh -G $MACHINE: hostname=$(opt hostname) port=$(opt port) proxyjump=$(opt proxyjump) stricthostkeychecking=$(opt stricthostkeychecking) userknownhostsfile=$(opt userknownhostsfile)"
[ "$(opt proxyjump)" = "$MGMT" ] || fail "ssh -G $MACHINE gives proxyjump '$(opt proxyjump)', not $MGMT: the managed block in $INCLUDE does not apply"
[ "$(opt stricthostkeychecking)" = "accept-new" ] || fail "ssh -G $MACHINE gives stricthostkeychecking '$(opt stricthostkeychecking)', not accept-new"
[ "$(opt userknownhostsfile)" = "$KH" ] || fail "ssh -G $MACHINE gives userknownhostsfile '$(opt userknownhostsfile)', not the reservation's own $KH"
HOSTNAME_R=$(on_machine 'hostname') || fail "ssh -F $SSH_CFG -o BatchMode=yes $MACHINE failed"
note "step 3 ssh: PASS via ProxyJump, remote hostname $HOSTNAME_R, host key in the reservation's own known_hosts ($(( $(date +%s) - T0 ))s)"

# --- 4 clone, worktree, edit, build, tt-smi -------------------------------------------------------
STEP="4 work"; T0=$(date +%s)
REMOTE_ROOT="$WORKDIR/ird-e2e/$RUN_ID"
SCRIPT="set -e; r=$(q "$REMOTE_ROOT"); mkdir -p \"\$r\"; cd \"\$r\"
git clone -q --depth 1 $(q "$REPO") src
cd src; git worktree add -q -b ird-e2e-$RUN_ID ../wt; cd ../wt
printf '#include <stdio.h>\nint main(void) { puts(\"ird-e2e build ok\"); return 0; }\n' > ird_e2e_hello.c
n=\$(git status --short | wc -l); echo \"edited \$((n)) file(s)\"
cc -o ird_e2e_hello ird_e2e_hello.c; ./ird_e2e_hello"
# Summaries come from stdout only; stderr goes to a log and is shown only when a step fails.
ERRF="$RUN_DIR/step4.err"; : > "$ERRF"
WORK=$(on_machine "$SCRIPT" 2>>"$ERRF") || fail "clone/edit/build: $(printf '%s\n%s' "$WORK" "$(cat "$ERRF")" | tail -n 3)"
SMI_ERRF="$RUN_DIR/step4-smi.err"; : > "$SMI_ERRF"  # its own file: the build's stderr never shows here
SMI=$(on_machine "tt-smi -s" 2>>"$SMI_ERRF"); rc=$?
[ $rc -eq 0 ] || fail "tt-smi -s (rc $rc): $(printf '%s\n%s' "$SMI" "$(cat "$SMI_ERRF")" | tail -n 3)"
BOARDS=$(printf '%s\n' "$SMI" | grep -o '"board_type": *"[^"]*"' | sed 's/.*: *"\([^"]*\)"/\1/' | sort | uniq -c | sed 's/^ *\([0-9]*\) /\1x /' | paste -s -d, -)
note "step 4 work: PASS $(printf '%s' "$WORK" | tr '\n' ';' | sed 's/;$//'); tt-smi -s ok, boards: ${BOARDS:-none listed} ($(( $(date +%s) - T0 ))s)"

# --- 5 extend once --------------------------------------------------------------------------------
STEP="5 extend"; T0=$(date +%s)
OUT=$(box ird extend "$BOX" "$RID" "$EXTEND_MIN" 2>&1); rc=$?
[ $rc -eq 0 ] || fail "box.py ird extend (rc $rc): $(printf '%s' "$OUT" | tail -n 3)"
NEW=$(printf '%s\n' "$OUT" | sed -n 's/^extended .* expires=\([^ ]*\).*/\1/p')
[ -n "$NEW" ] || fail "box.py ird extend printed no expiry: $OUT"
# Pass only if the expiry moved forward by the minutes asked (less one for rounding): compare the
# absolute times, zone included, never whether the ird list row changed (time left always does).
GAIN=$(python3 - "$EXPIRES" "$NEW" "$EXTEND_MIN" <<'PY'
import datetime as dt, sys
def t(s):
    try:
        v = dt.datetime.fromisoformat(s)
    except ValueError:
        v = None
    if v is None or v.tzinfo is None:
        print(f"cannot parse expiry {s!r} (want an ISO time with its zone)")
        sys.exit(2)
    return v
old, new = t(sys.argv[1]), t(sys.argv[2])
gain = (new - old).total_seconds() / 60
print(f"{gain:+.0f}")
sys.exit(0 if gain >= int(sys.argv[3]) - 1 else 1)
PY
); rc=$?
[ $rc -ne 2 ] || fail "$GAIN: reserve said '$EXPIRES', extend said '$NEW'"
[ $rc -eq 0 ] || fail "expiry $EXPIRES -> $NEW is $GAIN min, not +$EXTEND_MIN: box.py ird extend printed: $OUT"
STATUS=$(box ird status "$BOX") || fail "box.py ird status: $STATUS"
printf '%s\n' "$STATUS" | grep -q "^$RID .*expires=$NEW" || fail "status does not show $RID expiring $NEW: $STATUS"
ROW2=$(row_of "$RID")
[ -n "$ROW2" ] || fail "reservation $RID is gone from ird list after the extend"
note "step 5 extend: PASS +$EXTEND_MIN min, expires $EXPIRES -> $NEW ($GAIN min) ($(( $(date +%s) - T0 ))s)"
note "  ird list row: $ROW2"

# --- 6 release ------------------------------------------------------------------------------------
STEP="6 release"; T0=$(date +%s)
on_machine "rm -rf -- $(q "$REMOTE_ROOT")" >/dev/null 2>&1 && REMOTE_ROOT=""
OUT=$(box ird release "$BOX" "$RID") || fail "box.py ird release: $(printf '%s' "$OUT" | tail -n 3)"
RELEASED="yes"
ROWS=$(ird_rows) || fail "ird list after release failed"
printf '%s\n' "$ROWS" | awk -v id="$RID" '$1 == id {f=1} END {exit !f}' && fail "$RID still in ird list after release"
grep -qs "^# >>> tt-project ird $RID\$" "$INCLUDE" && fail "ssh block for $RID still in $INCLUDE"
[ -e "$KH" ] && fail "per-reservation known_hosts $KH still there after release"
note "step 6 release: PASS $RID gone from ird list, ssh block and known_hosts file removed ($(( $(date +%s) - T0 ))s)"
