# Resuming a Claude bring-up run

The run commits before each device job, so git holds its state. `REPORT.md`, `doc/` and the device log say where it
stopped.

**Same host** (session ended, host rebooted): check the device with `preflight.sh`, then resume the session with
its context:

```bash
cd <tt-metal>; claude --resume <session-id> --permission-mode auto \
  --plugin-dir $SKILLS/plugins/tt-model-bringup --plugin-dir $SKILLS/plugins/tt-autodebug \
  "Resumed after <what happened>. Check what was in flight, record it in the device log, then continue."
```

The session id is the newest `*.jsonl` under the Claude config's `projects/<tt-metal path>/`.

**Another host:** copy the tt-metal tree without `build*/`, `python_env` and `.cpmcache`, plus the gitignored
model-folder files. Rebuild (`./build_metal.sh`, `./create_venv.sh`), get the weights, run `preflight.sh`, then start
a new session with a short note: what happened, which host, what is left. The new session reads `REPORT.md` and the
device log and continues. Perf measured on the new host is recorded as such.
