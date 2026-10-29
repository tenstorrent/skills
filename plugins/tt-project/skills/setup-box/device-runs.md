# Device runs on a box

How a task runs device jobs on a box it reached over ssh, reads the results and ties each result
to the exact build it ran. How to build and run tt-metal itself is tt-buddy's (`agents.md`); this
page only adds the queueing and the stamp.

## Before queueing

- Build on the box, in the task's own checkout or worktree, before the job is queued: the device
  waits for nobody's compile. Pre-build kernels and the test binaries the job needs the same way.
- Use the compiler cache the build recipe sets up (tt-buddy `recipes/tt-metal/build.md`), so a
  rebuild after a small change is quick.
- Commit before queueing when the result should be reproducible: a dirty tree is stamped by the
  hash of its diff only.
- On a Blackhole Galaxy, reset with `tt-smi -glx_reset` before and after every device run:
  `recovery.md`.

## Queue the job

Every device job goes through a serializing path, never by opening the device directly:

| Box has | Path |
|---|---|
| A `ttp devq` runner for the box (config `device.runners.<name>`, `host` = the box's alias) | `ttp devq submit` from the task (below) |
| A device broker (tt-device-mcp) | the broker's job CLI or MCP tools: the `tt-buddy:run` skill's `skills/run/mcp.md` |
| Neither | one command at a time under `ttp lock <device-resource> -- ssh <alias> '<cmd>'` |

`ttp devq submit` runs on the project box and queues the job on the box over ssh:

```
ttp devq submit <runner> --id <task>-<what> --workdir <checkout on the box> \
  [--artifact <built library or binary, relative to workdir>] [--timeout <s>] -- <command>
```

- `--workdir` is the checkout or worktree on the box; the job runs there.
- `--artifact` names the built file the job loads (else the runner's `build_artifact`).
- `--timeout` sizes the job's limit; the job sees it as `TTP_DEVQ_TIMEOUT_S`.
- On a broker box the command itself calls the broker's job CLI, so jobs from other users and
  projects stay serialized too.
- Hand off `waiting` with the `retry_when` it prints (`ttp devq probe <runner> <id>`).

## The build stamp

Each submit stamps the job with what it runs, taken on the box (`devq.build_stamp`):

| Field | What |
|---|---|
| `repo` | the checkout's origin URL without credentials (else its top folder) |
| `branch`, `commit` | the checked-out branch and commit |
| `dirty`, `diff_sha256` | uncommitted changes, and a hash of them |
| `artifact` | the built file's path, size, time (`mtime_utc`) and `sha256` (its build id) |
| `stale` | changed C/C++ sources newer than the artifact: rebuild before queueing |

- Submit prints it (`build: <commit>[+dirty] on <branch> artifact <id> built <time>`) and warns on
  a dirty tree, a stale artifact or a missing one.
- The stamp is stored in the job record (`done/<id>.spec.json`) and the result (`done/<id>.json`,
  key `build`). The runner stamps the job again as it starts; when the commit, the diff or the
  artifact changed while it waited, the result says so under `build_changed`.

## Read the result

- `ttp devq probe <runner> <id>` prints the status, exit code, log path and build stamp line.
- `ttp devq status <runner> <id>` prints the whole result, stamp included.
- Read the log on the box (`ssh <alias> tail -n 50 <log>`), never by copying the state folder.
- A `wedged` result means the device needs recovery before the next job (tt-buddy
  `skills/run/recovery.md`; on a Blackhole Galaxy, `recovery.md`).

## Copy inputs and results

Read results on the box where you can. When a file must move (a job input, a log, a results folder),
use the shared helper; it uses rsync when both ends have rsync 3 or later, else tar over ssh:

```
<plugin-root>/skills/setup-box/scripts/box.py copy <alias>:<path on the box> <local folder>
<plugin-root>/skills/setup-box/scripts/box.py copy <local file or folder> <alias>:<folder on the box>
```

- The source lands in the destination folder under its own name; `~/` paths on the box are under
  its home folder. A failure prints the tool's error; exit 124 is a timeout (`--timeout`, default
  1800 s).
- Never copy a checkout or the project box's tree with it: the box keeps its own checkout.

## Report back

Add to the box task's hand-off (`SKILL.md`): the job ids, each result's status, and its stamp's
commit (with `+dirty` and the diff hash when set) and artifact id. A number without its stamp is
not tied to a build.

## After the run

- Nothing stays on the device: the job ends, and the box's allocation or reservation is released
  as soon as the last job is done (`SKILL.md`, etiquette).
