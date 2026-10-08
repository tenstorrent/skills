# tt-syseng-utilities

System-engineering utilities for Tenstorrent hosts. One skill per task.

```bash
/plugin install tt-syseng-utilities@tenstorrent-skills     # Claude Code
codex plugin add tt-syseng-utilities@tenstorrent-skills    # Codex
```

| Skill | Task | Needs |
|---|---|---|
| `p150-unharvesting` | Read, patch, flash, verify P150 Tensix disable count | P150 host, tt-metal `python_env` |

## p150-unharvesting

- Flashing changes firmware for every user of the host.

| Script | Reads | Writes |
|---|---|---|
| `read_fwbundle_harvesting.py` | Disable count per board in `.fwbundle` | nothing |
| `read_chip_harvesting.py` | Enabled Tensix columns, DRAM channels (telemetry) | nothing |
| `check_mesh_cores.py` | Worker and DRAM grid per device (ttnn) | nothing |
