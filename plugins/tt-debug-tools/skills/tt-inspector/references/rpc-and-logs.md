# The RPC, the logs, and why they differ

Inspector has two faces and they do not carry the same data. Most confusion
downstream comes from assuming they do.

## The live RPC

A capnp server, on by default at `localhost:50051`. Clients query the running
process and get whatever the interface exposes, including methods that take
arguments.

```bash
export TT_METAL_INSPECTOR_RPC_SERVER_ADDRESS=localhost:50051
export TT_METAL_INSPECTOR_RPC_SERVER_ADDRESS=unix:/tmp/inspector_socket   # or a socket
```

Under MPI the port is shifted by rank, so a client pinned to 50051 sees rank 0
and nothing else.

The server only answers while the process is **alive and running**. A process
stopped with SIGSTOP still holds the listening socket, so a client connects and
then waits for a reply that never comes — which looks like a hang rather than a
refusal.

## The serialized logs

Written to `generated/inspector/` under the logs directory:

| File | Holds |
|---|---|
| `startup.yaml` | Process and version information |
| `kernels.yaml` | Kernels built, with their paths |
| `programs_log.yaml` | Programs, and their binary status per device |
| `mesh_devices_log.yaml` | Mesh devices |
| `mesh_workloads_log.yaml` | Mesh workloads |

**Only methods that take no arguments are auto-serialized**, and that happens at
runtime exit. Anything parameterised needs serialization implemented by hand on
the host side and deserialization in the consumer. So the logs are a subset of
what the RPC can answer, by design.

## What that costs a consumer

`tt-triage` tries the RPC first and falls back to the log directory. The fallback
is where the asymmetry bites: a triage script whose data was never serialized
reports a **dependency failure**, and every script depending on it skips.

Observed on Wormhole at the pinned ref, triaging a process that could not answer
the RPC:

```
metal_device_id_mapping.py:
  Data provider script failed: AttributeError: 'NoneType' object has no attribute 'mappings'
```

and downstream of that, fourteen sections reporting
`Skipping: dependency ... failed` — including `dump_callstacks`, which is the one
that says where a core stopped. The remaining hardware checks all pass, so the
report reads like a healthy device.

That is the documented limitation, not a bug in triage: keep the process alive
and running, or accept a report with no host-side half.

## Turning the knobs up

Off by default, worth enabling for an investigation:

```bash
export TT_METAL_INSPECTOR_CAPTURE_TENSOR_SPECS=1
export TT_METAL_INSPECTOR_LOG_RUNTIME_ENTRIES=1
export TT_METAL_INSPECTOR_INITIALIZATION_IS_IMPORTANT=1
export TT_METAL_INSPECTOR_SERIALIZE_ON_DISPATCH_TIMEOUT=1
```

The third turns a silent failure into a loud one and is the first thing to set
when you are relying on Inspector data. The fourth is what leaves data behind
when a run hangs rather than exits — pair it with a dispatch timeout, as
`tt-asserts` does.

## Extending it

New RPC methods go in `tt_metal/impl/debug/inspector/rpc.capnp`, which generates
a callback to attach with
`Inspector::get_rpc_server().setYourNewMethodCallback(...)`. A triage script then
consumes it by declaring `depends=["inspector_data"]` and calling the generated
method. If the method takes arguments, serialization is yours to write, or the
data is unavailable to any consumer reading logs rather than the live RPC.
