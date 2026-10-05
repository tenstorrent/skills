# Board Questions

D3, D9, D10, D11, D12, D14. All read one snapshot file. Capture once.

## Capture

```bash
TT=$(command -v tt-smi || ls -1 ~/.tenstorrent-venv/bin/tt-smi ~/tt-smi/.venv/bin/tt-smi 2>/dev/null | head -1)
[ -x "$TT" ] || { echo "tt-smi not found"; exit 1; }
SNAP=$SCRATCH/tt-smi-snapshot.json
$TT -s --snapshot_no_tty > "$SNAP" && $TT -v
```

`$SCRATCH` is the session scratchpad. `-s --snapshot_no_tty` writes pure
JSON to stdout and is read-only on the device.

## Extract

```bash
python3 - "$SNAP" <<'PY'
import json,sys,collections
d=json.load(open(sys.argv[1])); di=d['device_info']
print("host:",d['host_info']['Driver'],"| tt-smi",d['host_sw_vers']['tt_smi'])
boards=collections.OrderedDict()
for i,x in enumerate(di):
    b,f,t=x['board_info'],x['firmwares'],x['telemetry']
    boards.setdefault(b['board_id'],[]).append(i)
    print(f"chip {i} {b['board_type']} serial={b['board_id']} bdf={b['bus_id']} fw={f['fw_bundle_version']} "
          f"power={t['power'].strip()}W temp={t['asic_temperature']}C aiclk={t['aiclk'].strip()}MHz fmax={x['limits']['asic_fmax']}")
print("boards:",len(boards),"asics:",len(di),"types:",sorted({x['board_info']['board_type'] for x in di}))
PY
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| D3 | Boards on machine | `asics`, `types`, arch from `/dev/tenstorrent/by-id/` link prefix | `<n> <arch> ASICs, board type <type>` |
| D10 | Physical boards vs ASICs | `boards` (distinct `board_id`) vs `asics` (entries) | `<n> boards, <n> ASICs` |
| D9 | `--tt-device` string | D10 counts + board type → `knowledge/hardware/boards.md` | `<string>. Derived from <type> × <boards> boards / <asics> ASICs` |
| D11 | Firmware vs spec minimums | `fw` per chip and D2's KMD vs `system_requirements` from `models.md` § Spec | `FW <have> vs ≥<need>; KMD <have> vs ≥<need>: both met` or names the failing side |
| D12 | Idle telemetry baseline | `power`, `temp`, `aiclk` per chip; `fmax` for reference | `<lo>–<hi> W, <lo>–<hi> °C, <aiclk> MHz idle (fmax <n>), all <n> chips` |
| D14 | Reset needed before launch | D13's exited containers with `/dev/tenstorrent`; D13 holders | verdict below |

## Rules

- D9: NEVER take the device string from a product or getting-started page.
  Serial count decides it (`knowledge/hardware/boards.md`). Mixed `types`
  → `unknown: mixed board types`, list them.
- D9: if the derived string is not a key in the spec's device union
  (`models.md` § Spec), report the string and add `not in spec`.
- D11: fill after `models.md` runs. Compare as dotted-integer tuples, never as strings. Use the
  strictest specifier across the device's entries when `--model` is absent.
  Chips disagreeing on `fw` → report each and flag `mixed firmware`.
- D12: any chip already above idle (aiclk at `fmax`, power several × the
  others) → report it as `loaded` and exclude it from the baseline range.
- D14 verdict: `yes` if any exited container listed `/dev/tenstorrent`
  (stopping a container leaves inter-chip Ethernet wedged). `blocked` if
  D13 shows a live holder. Otherwise `no`. On `yes`, add `invoke tt:run
  recovery`. NEVER run the reset here.
