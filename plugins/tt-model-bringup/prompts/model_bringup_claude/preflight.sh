#!/usr/bin/env bash
# Check a host before a Claude bring-up run. Prints each check; exits 1 if any required one fails.
#   preflight.sh --hf-model org/name --tt-metal /path/to/tt-metal [--mesh 1x4] [--revision REV]
# Without --revision, any snapshot of the model in the HF cache counts (weights downloaded at a pinned revision have
# no refs/main).
set -uo pipefail
HF_MODEL= TT= MESH=1x4 REV=
while [ $# -gt 0 ]; do case $1 in
  --hf-model) HF_MODEL=$2;; --tt-metal) TT=$2;; --mesh) MESH=$2;; --revision) REV=$2;; *) echo "unknown option $1" >&2; exit 2;; esac; shift 2; done
[ -n "$HF_MODEL" ] && [ -n "$TT" ] || { echo "need --hf-model and --tt-metal" >&2; exit 2; }
FAIL=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; FAIL=1; }
info() { echo "info  $*"; }
PY="$TT/python_env/bin/python"

[ -d "$TT/build/lib" ] && [ -x "$PY" ] && ok "tt-metal built with python_env ($(git -C "$TT" rev-parse --short HEAD))" \
  || bad "tt-metal not built: run ./build_metal.sh and ./create_venv.sh in $TT"
SNAP=$("$PY" - "$HF_MODEL" "$REV" 2>/dev/null <<'P'
import os, sys
from huggingface_hub import constants, snapshot_download
repo, rev = sys.argv[1], sys.argv[2] or None
try:
    print(os.path.basename(snapshot_download(repo, revision=rev, local_files_only=True)))
except Exception:
    if rev:
        raise
    d = os.path.join(constants.HF_HUB_CACHE, "models--" + repo.replace("/", "--"), "snapshots")
    s = sorted(x for x in os.listdir(d) if os.path.isfile(os.path.join(d, x, "config.json")))
    if not s:
        sys.exit(1)
    print(" ".join(s))
P
) && ok "weights for $HF_MODEL in the HF cache (snapshot $SNAP)" || bad "weights for $HF_MODEL${REV:+ at $REV} not in the HF cache (HF_HOME=${HF_HOME:-default})"
"$PY" - 2>/dev/null <<'P' && ok "HF token can read the gated GPQA dataset" || bad "no access to Idavidrein/gpqa: log in with an account that accepted its terms (with a custom HF_HOME, set HF_TOKEN_PATH to the token file)"
from huggingface_hub import hf_hub_download
hf_hub_download("Idavidrein/gpqa", "gpqa_diamond.csv", repo_type="dataset")
P
docker info >/dev/null 2>&1 && ok "docker works (needed for SWE task containers)" || bad "docker not usable by $(whoami)"
info "disk free under $TT: $(df -h --output=avail "$TT" | tail -1 | tr -d ' ')"
info "RAM available: $(free -g | awk '/Mem:/{print $7}') GB"
for m in $(awk '$3 ~ /^nfs/ {print $2}' /etc/fstab); do
  mountpoint -q "$m" && ok "mount $m" || bad "fstab mount $m is not mounted"; done
command -v tt-smi >/dev/null && info "firmware: $(tt-smi -s --snapshot_no_tty 2>/dev/null | grep -o '"fw_bundle_version": "[^"]*"' | sort -u | tr '\n' ' ')"
R=${MESH%x*}; C=${MESH#*x}
(cd "$TT" && timeout 300 "$PY" -c "
import ttnn, torch
m = ttnn.open_mesh_device(ttnn.MeshShape($R, $C))
t = ttnn.from_torch(torch.ones(32, 32), device=m, layout=ttnn.TILE_LAYOUT, mesh_mapper=ttnn.ReplicateTensorToMesh(m))
assert float(ttnn.to_torch(ttnn.get_device_tensors(t)[0]).sum()) == 1024
ttnn.close_mesh_device(m)" >/dev/null 2>&1) && ok "$MESH mesh opens" || bad "$MESH mesh does not open (check tt-smi, links, a cold power cycle)"
exit $FAIL
