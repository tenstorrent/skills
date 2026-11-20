# Box-clean gates

`tt-box-clean-probe` (in the harness's `bin/`, on every probe's PATH) answers "has this box been N hours without a device incident?" with no model, read-only: host uptime plus the tt-device-mcp broker's recent jobs (boots, broker resets, power-cycles, reboots, PCI rescans, health gates, hold starts or a device held degraded, failed broker jobs, tenant jobs the broker killed (or left `[MCP killed ...]` by a chip drop), found hung or that ran a reset), each dated by when it finished (a running job or a hold still on counts as now), in the host's local time as the broker writes it.
Use it as a `start_when`, `retry_when` or resource-pause `end_when` instead of a daily light run that only checks: `tt-box-clean-probe --window-h 24` on the box itself, `ssh <box> python3 - --window-h 24 < "$TTP_PROJECT/harness/bin/tt-box-clean-probe"` for another.
Exit 0 clean, 1 not yet (prints the newest incident and the earliest clean time), 75 cannot tell.
With no broker installed it judges on uptime alone (`--require-broker`: 75 instead); `--broker-python` (env `TT_BROKER_PYTHON`), `--port`, `--limit`, `--jobs-file` (JSON instead of the broker).
