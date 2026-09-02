# EDM channel configuration consistency

Ring CCL operations pass data between devices through ethernet data movers. Each EDM channel has
buffers, semaphores and configuration parameters that must agree **between host-side setup and
device-side kernel arguments**, and between the sender and receiver ends of a channel.

A whole bug family lives in that agreement: values computed on the host and passed inconsistently to
kernels, or two sides of a channel disagreeing. Distilled from merged tt-metal fix PRs — see
`SOURCES.md`.

## What to check

**1. `num_buffers_per_channel` agreement.** Set during `EriscDatamoverBuilder` construction and
passed as a compile-time argument to erisc kernels. The value used in `add_sender_channel()` /
`add_receiver_channel()` must match what the kernel receives.

```cpp
// BUG: 4 on the host, kernel compiled expecting 8
auto config = EriscDatamoverConfig(4 /* num_buffers_per_channel */);
uint32_t ct_args[] = { ..., 8, ... };   // should be 4
```

The two sites are usually in different files, which is exactly why this survives review — check the
kernel's compile-time args against the builder, not just the builder against itself.

**2. Buffer address and semaphore vector lengths.** `local_buffer_addresses` and
`local_semaphore_addresses` must be the same size, and indexing must stay consistent as channels are
added or iterated.

```cpp
// BUG: 3 buffer addresses, 2 semaphores -> OOB access or silent corruption
std::vector<uint32_t> buf_addrs = {addr0, addr1, addr2};
std::vector<uint32_t> sem_addrs = {sem0, sem1};
builder.add_sender_channel(worker_semaphore, buf_addrs, sem_addrs);
```

**3. `eth_buffer_size_bytes` drift.** Computed by `EriscDatamoverConfig::compute_buffer_size()` from
`num_edm_channels`, `num_buffers_per_channel` and `page_size`. If any input changes after that
computation without recomputing, the size is stale. **Flag a diff that changes one of those three
inputs without a visible recompute** — that is the whole finding, and it is cheap to check.

**4. Ring size versus device count.** `get_topological_dimension()` computes ring size from device
coordinates. Using it inconsistently — one site taking cluster-axis size, another a hardcoded count
— makes neighbour lookups wrong.

```cpp
// BUG: ring_size from the cyclic order, neighbour lookup on a different axis
auto ring_size = cyclic_order.size();                          // e.g. 8
auto neighbor  = get_physical_neighbor(..., ClusterAxis::X);   // only 4 devices on X
```

This connects to the gather/reduce axis check in `SKILL.md`: same underlying mistake — an axis
assumed rather than derived — surfacing in topology rather than in tensor layout.

**5. Sender/receiver channel symmetry.** Every device in a ring is both. The sender channel count
must match the receiver channel count on the paired device.

## Severity

All of these are `MUST-FIX`. The failure modes are out-of-bounds access, silent corruption, and
watchdog hangs — the same class as a wrong `num_links`, and for the same reason: a host/device
disagreement that no type system catches.
