---
name: tt-inspector
description: Read what the Metal host runtime thinks is happening on a Tenstorrent device — which programs and mesh workloads exist, which kernels were built, tensor specs, mesh buffers and sockets — over the Inspector RPC or from its serialized logs. Use when device-side state needs host-side names attached to it, when a tt-triage report comes back with dispatcher-aware sections skipped, or when you need to know what Inspector recorded about a run that has ended.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/inspector.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/impl/debug/inspector
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/llrt/rtoptions.cpp
---

# tt-inspector

Two components, both on by default. One logs host-runtime data to disk as the
run proceeds; the other serves a capnp RPC that clients query while the process
is alive. Inspector is what turns a device address into a program, a kernel name
and a tensor spec, which is why the tools that read the device get much less
useful without it.

Mostly you are not enabling this — you are checking it was on, or turning its
knobs up, or explaining why something downstream came back thin.

## When to invoke

- A `tt-triage` report has dispatcher-aware sections skipped, or a
  `dependency failure`, and you need to know why.
- You want host-side names for device state: which program, which kernel binary,
  what the tensors were.
- You need tensor specs, mesh buffers, mesh sockets or runtime entries logged,
  which are off by default.
- A run has ended and you want what Inspector left on disk.

Not this skill: reading the device itself — `tt-triage` for a hung process,
`tt-watcher` for a watched run.

## Surface

Defaults matter more than the syntax here, because everything is already on:

| Variable | Default | Effect |
|---|---|---|
| `TT_METAL_INSPECTOR` | **1** | The whole feature. `0` degrades every consumer silently. |
| `TT_METAL_INSPECTOR_RPC` | **1** | The RPC server. |
| `TT_METAL_INSPECTOR_RPC_SERVER_ADDRESS` | `localhost:50051` | Also accepts `unix:/tmp/inspector_socket`. Rank-shifted under MPI. |
| `TT_METAL_INSPECTOR_INITIALIZATION_IS_IMPORTANT` | **0** | `0` means a failed init is **not** fatal — the run continues without Inspector. |
| `TT_METAL_INSPECTOR_WARN_ON_WRITE_EXCEPTIONS` | **1** | Warns when logging cannot write, e.g. a full disk. |
| `TT_METAL_INSPECTOR_CAPTURE_TENSOR_SPECS` | 0 | Record tensor specs. |
| `TT_METAL_INSPECTOR_LOG_RUNTIME_ENTRIES` | 0 | Record runtime args. |
| `TT_METAL_INSPECTOR_SERIALIZE_ON_DISPATCH_TIMEOUT` | 0 | Serialize when a dispatch timeout fires, so a hang leaves data behind. |

Turning the knobs up for an investigation, and the RPC and serialization model:
`references/rpc-and-logs.md`.

## Force the state

Nothing to force — it is on. To confirm it is actually working:

```bash
build/test/tt_metal/unit_tests_inspector    # 4 RPC startup cases
ss -ltn | grep 50051                        # the server, while a run is alive
```

## Output

`generated/inspector/`, under the logs directory — `TT_METAL_LOGS_PATH`, or the
working directory if unset, **not** `TT_METAL_HOME`:

```
startup.yaml            kernels.yaml            programs_log.yaml
mesh_devices_log.yaml   mesh_workloads_log.yaml
```

Plus the live RPC while the process runs. The two are not equivalent — see Traps.

## Traps

**It fails open.** `TT_METAL_INSPECTOR_INITIALIZATION_IS_IMPORTANT` defaults to
`0`, so an Inspector that failed to initialise leaves the run working and every
downstream tool thin. Set it to `1` when you are relying on the data.

**The logs are not a substitute for the RPC.** Methods that take no arguments are
auto-serialized at process exit; anything taking arguments is not, unless someone
implemented serialization for it. A triage script that needs unserialized data
reports a dependency failure — which is why triaging an *exited* process, or one
frozen so it cannot answer, loses the dispatcher-aware scripts and with them the
callstacks. Keep the process alive and running.

**`TT_METAL_INSPECTOR=0` degrades silently.** Hardware checks still run, so a
report looks plausible while the half that names things is missing. Report the
degradation rather than reading the thin report as a clean bill of health.

**It overrides `TT_METAL_RISCV_DEBUG_INFO`.** Enabling Inspector generates debug
info for the RISC-V ELFs. That is also why kernel enum values print as symbolic
names in `tt-dprint` — turn Inspector off and they become `(TypeName)integer`.

**Under MPI the port is rank-shifted.** A client hardcoding 50051 talks to rank 0
only.
