# Launch Command & Overrides

L3, L5, L6, L7 — everything about how the launch command is built, and how
to fix it safely when it's wrong. For what a running/failed container is
actually doing, see `diagnose.md`.

## Commands

```bash
echo "==L3"; docker inspect "$CID" \
  --format 'devices={{json .HostConfig.Devices}} ipc={{.HostConfig.IpcMode}}'
docker inspect "$CID" --format '{{json .Mounts}}' \
  | python3 -c "import json,sys;[print('mount',m['Type'],m.get('Source'),'->',m['Destination']) for m in json.load(sys.stdin)]"

OVERRIDE=${OVERRIDE:-}
if [ -n "$OVERRIDE" ]; then
  python3 - "$OVERRIDE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print('schema_version=', d.get('schema_version'))
PY
  stat -c '%a %U' "$OVERRIDE"
  readlink -f "$OVERRIDE"
  # L6: byte-diff against the upstream entry tt:retrieve already captured for this
  # model/device (spec.md writes $SCRATCH/entry_<impl_id>.json). Without that file
  # there is nothing to diff against — report unavailable, never "matches upstream".
  ENTRY=$(ls "$SCRATCH"/entry_*.json 2>/dev/null | head -1)
  if [ -n "$ENTRY" ]; then
    python3 - "$OVERRIDE" "$ENTRY" <<'PY'
import json, sys
override, entry = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
if override == entry:
    print("override_diff=matches upstream byte-for-byte")
else:
    keys = sorted(set(override) | set(entry))
    diffs = [k for k in keys if override.get(k) != entry.get(k)]
    print(f"override_diff=differs in {len(diffs)} key(s): {', '.join(diffs[:8])}")
PY
  else
    echo "override_diff=unavailable: no captured upstream entry (run tt:retrieve for this model/device first)"
  fi
fi
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| L3 | Mandatory docker flags | `.HostConfig.Devices`, `.HostConfig.IpcMode`, **`.Mounts`** | `--device /dev/tenstorrent`, `--ipc host`, `--mount type=bind,src=/dev/hugepages-1G,dst=/dev/hugepages-1G` — all three, every time |
| L5 | Minimum override | static, ranked | 1) merge one spec entry + `MODEL_SPECS_JSON_PATH` (surgical) → 2) bind-mount the whole repo spec → 3) `--dev-mode` + `--override-docker-image` (blunt, last resort) |
| L6 | Is override safe | `$OVERRIDE`'s `schema_version`, file mode, `override_diff` vs the captured upstream entry | `safe: schema 0.1.0, chmod 644, matches upstream` / `differs in <n> key(s): <keys>` / `unavailable: no captured upstream entry` |
| L7 | Override survives **reboot** | `readlink -f "$OVERRIDE"` path prefix | `persistent: <host path>` / `survives container restart, lost on reboot: <path>` / `EPHEMERAL: session scratchpad, lost on session end` |

## Rules

- L3: these three flags are non-negotiable for every launch, override or not.
  A launch missing any of them is misconfigured before it even reaches the
  entrypoint.
- **L3: a `--mount type=bind` does not appear in `.HostConfig.Binds`** — it
  is only in `.Mounts`. Checking `Binds` alone reports a correctly-launched
  container as missing the hugepages flag. Verified 2026-09-22 against the
  container that reached `/health` 200: `Binds` held only the cache volume,
  while `.Mounts` held both it and the `/dev/hugepages-1G` bind.
- L5: try the surgical option first every time. Reaching straight for
  `--dev-mode` is a red flag in itself — see `SKILL.md` § Red Flags.
- L6/L7: NEVER treat "the file looks right" as safe — always run the
  byte-diff against the upstream entry. A hand-edited override can silently
  pin the wrong image while looking correct.
- L6: the byte-diff needs `tt:retrieve`'s captured `entry_<impl_id>.json` for
  this exact model/device — invoke it first if `$SCRATCH` has none. Reporting
  `matches upstream` without that file compared is itself the failure mode
  this row exists to catch.
- **L7: reboot and container restart are different questions.** The row asks
  about a reboot; an earlier answer format said "will not survive a container
  restart", which is a third, stricter claim. The three cases:

  | Location | Container restart | Host reboot | Session end |
  |---|---|---|---|
  | host path outside `/tmp` | survives | survives | survives |
  | host `/tmp` or tmpfs | survives | **lost** | survives |
  | session scratchpad | survives | **lost** | **lost** |

  Answer the question that was asked, and name which of the three the path
  falls into rather than collapsing them.
- L6/L7: this is what `tt:retrieve`'s R6/R7 check for the *documented*
  image and spec agreeing. This file checks whether *your local override*
  agrees with either of them — a related but separate question.

## Status

| Row | State |
|---|---|
| L3 | **TESTED** 2026-09-22 — all three flags confirmed present on the container that reached `/health` 200, read via `Devices` / `IpcMode` / `Mounts` |
| L5 | **UNTESTED** — no override was needed on 0.20.0; re-test when an override run actually happens |
| L6 | **UNTESTED** — no override in play; byte-diff logic against `tt:retrieve`'s captured entry added but not yet run against a real override |
| L7 | **UNTESTED** — no override in play; the three-case table above is reasoned from path semantics, not measured |
