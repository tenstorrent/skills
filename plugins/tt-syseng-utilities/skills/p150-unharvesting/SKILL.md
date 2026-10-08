---
name: p150-unharvesting
description: Read, patch, flash and verify the Tensix column disable count on Blackhole P150 cards (p150a/b/c). Use to unharvest a P150 to 130 or 140 cores, check cores in a .fwbundle or live chip, or explain a small Blackhole worker grid.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 5f511eadf03b4b9e095e47f2635da516a61e8825
      path: tt_metal/impl/dispatch/dispatch_core_common.cpp
    - repo: tenstorrent/tt-metal
      ref: 5f511eadf03b4b9e095e47f2635da516a61e8825
      path: ttnn/core/distributed/distributed_nanobind.cpp
---

# p150-unharvesting

## Facts

- Blackhole: 14 Tensix columns, 10 cores each.
- Firmware 19.5.0+ sets disable count 2 on every P150.
- Count lives in the bundle's `cmfwcfg` firmware table.
- Patch tool: `tt-update-tensix-disable-count`. Flash tool: `tt-flash`.
- tt-metal default dispatch takes one Tensix column on Blackhole.
- Source: `resolve_dispatch_core_axis`, when fabric Tensix is disabled.

| Disable count N | Tensix cores | ttnn worker grid |
|---|---|---|
| 0 | 140 | 13 x 10 |
| 1 | 130 | 12 x 10 |
| 2 (stock) | 120 | 11 x 10 |

## When to use

- Unharvest a P150, or go to 130/140 cores.
- Read disable count from a `.fwbundle` or live chips.
- Worker grid is smaller than expected on Blackhole.

Not for P100, P300, Galaxy: core math differs.

## Rules

- MUST get user confirmation before flashing.
- MUST NOT flash while any process uses the devices.
- NEVER overwrite an existing bundle. Write `<name>-dc<N>.fwbundle`.
- MUST tee every tool run to a log file.
- Give the user the log path.
- `$SKILL_DIR` is this skill's directory.

## 0. Environment

- Activate tt-metal's `python_env`.
- Venv has no `pip`. Use `uv pip`.

```bash
LOG_DIR=${LOG_DIR:-~/logs}; mkdir -p "$LOG_DIR"
uv pip install tt-update-tensix-disable-count
tt-flash --version          # need >= 3.6.0
```

Patch tool needs `protoc`. If `which protoc` is empty:

```bash
PIN=$(grep -E '^protobuf==' <tt-metal>/tt_metal/python_env/requirements-dev.txt)
uv pip install grpcio-tools "$PIN"
B=$VIRTUAL_ENV/bin
[ -e "$B/protoc" ] || { printf '#!/bin/sh\nexec "%s/python" -m grpc_tools.protoc "$@"\n' "$B" > "$B/protoc"; chmod +x "$B/protoc"; }
uv pip check
```

- MUST pass the protobuf pin.
- Unpinned `grpcio-tools` upgrades protobuf past tt-metal's pin.

## 1. Read current state

| Target | Command |
|---|---|
| Live chips | `python $SKILL_DIR/scripts/read_chip_harvesting.py` |
| Bundles | `python $SKILL_DIR/scripts/read_fwbundle_harvesting.py ~/*.fwbundle` |
| Board types | `tt-smi -ls` |

- Both scripts are read-only.
- Patch tool has no read mode.
- Bundle script: `--board X` or `--all-boards`.

## 2. Get a bundle

- Prefer the version the chips run now.
- Check for a local copy first.
- Releases: `github.com/tenstorrent/tt-system-firmware/releases`.
- Skip `-rc` tags unless asked.

```bash
curl -sL https://api.github.com/repos/tenstorrent/tt-system-firmware/releases?per_page=10 \
  | python3 -c "import json,sys; [print(r['tag_name'], a['browser_download_url']) for r in json.load(sys.stdin) for a in r['assets'] if a['name'].startswith('fw_pack')]"
```

## 3. Patch

```bash
VER=<version>
N=0             # 0 = 140 cores, 1 = 130, 2 = 120
IN=~/fw_pack-$VER.fwbundle
OUT=~/fw_pack-$VER-dc$N.fwbundle
L=$LOG_DIR/tensix_dc${N}_${VER}_$(date +%Y%m%d_%H%M%S).log
set -o pipefail
if [ -e "$OUT" ]; then
  echo "exists, not patching: $OUT"
else
  tt-update-tensix-disable-count --input "$IN" --output "$OUT" \
    --board P150A-1 --board P150B-1 --board P150C-1 \
    --disable-count $N --verbose 2>&1 | tee "$L" > /dev/null; RC=$?; echo rc=$RC
  [ $RC -eq 0 ] || { echo "patch FAILED, see $L"; rm -f "$OUT"; }
  grep -E 'Processing|Current|Updated|Verified|rror' "$L"
fi
python $SKILL_DIR/scripts/read_fwbundle_harvesting.py "$OUT"
```

- `rc` not 0: stop.
- `$OUT` existed: flash only if reader shows count `N`.
- No `--board`: patches every board, not only P150.
- Per board expect `Current` -> `Updated ... N` -> `Verified ... N`.
- Output is not byte-identical across runs. Expected.

## 4. Flash (user confirmation first)

```bash
# processes holding a device open; must print nothing
for p in /proc/[0-9]*; do ls -l $p/fd 2>/dev/null | grep -q /dev/tenstorrent \
  && echo "${p#/proc/} $(tr '\0' ' ' < $p/cmdline)"; done
L=$LOG_DIR/tt-flash_$(date +%Y%m%d_%H%M%S).log
set -o pipefail
tt-flash flash --force "$OUT" 2>&1 | tee "$L" > /dev/null; echo rc=$?
tail -5 "$L"; grep -cE 'Firmware verification.*SUCCESS' "$L"
```

- Busy check sees only your processes without root.
- Run it with `sudo` or ask user to confirm idle.
- Any device user listed: stop.
- Flash passes only if all three hold:
  - `rc=0`.
  - `FLASH SUCCESS` in log.
  - One verification line per chip.
- Any fails: stop, do not validate, tell the user.
- `--force` needed for same version or downgrade.
- tt-flash resets all chips itself.
- "does not say which hardware it supports": warning only.

## 5. Validate

```bash
python $SKILL_DIR/scripts/read_chip_harvesting.py --expect-disable-count $N
L=$LOG_DIR/mesh_cores_$(date +%Y%m%d_%H%M%S).log
python $SKILL_DIR/scripts/check_mesh_cores.py --disable-count $N > "$L" 2>&1; echo rc=$?
grep -E '__main__' "$L"
```

- Opens full mesh; one 1x1 submesh per device.
- Fails unless worker grid is `(14 - N - 1) x 10`.
- Fails unless DRAM grid is `8 x 1`.
- Both scripts fail when no chip or device found.
- Old count after `FLASH SUCCESS`: run `tt-smi -r`.
- Still wrong: stop, tell the user.

## Revert

- Flash the unmodified `fw_pack-<ver>.fwbundle`.
- Validate with `N=2`.
