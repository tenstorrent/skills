# Local backend

The agent runs on the device host.

- **Acquire:** none. Check that no other user's process holds the devices before starting.
- **Run:** run the command in the run environment and send output to a log in the run root.
- **Run in background:** run with `nohup`, record the PID in `run.json`, and read the log file.
- **Health check:** `tt-smi -ls` shows the expected device count.
- **Reset:** follow `../../../shared/device-hangs.md` rule 3. The right reset command depends on
  the hardware, and one of them can drop a Galaxy off the PCIe bus.
- **Kill:** kill only the process groups this run started, as in `../../../shared/device-hangs.md`.
- **Collect:** nothing to copy.
- **Release:** none.

## Failure handling

| Failure | Action |
|---|---|
| Devices held by another user's process | Stop and ask the user. Do not kill it. |
| Reset fails or devices are missing afterwards | Stop. The host may need an operator. |
