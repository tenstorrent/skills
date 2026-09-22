# tt-triage evals

Three device scenarios plus interface-knowledge evals. Each device scenario is a
concrete provoker under `provoke/`; the corresponding `fixtures/<scenario>/`
holds captured output and the hand-authored reading.

| Scenario | Provoker | What the report shows |
|---|---|---|
| `mcast-ack-deficit` | `provoke/live_hang_mcast_ack.py` — 20 multicast destinations that do not exist, ten times, so BRISC never leaves `noc_async_atomic_barrier` | one worker BRISC short 200 acks, sitting among eight benign ethernet mismatches |
| `llk-srca-mismatch` | `provoke/live_hang_llk_assert.py` — init a copy for one CB while SrcA still describes another, so the LLK pre-init check ebreaks TRISC0 | `dump_lightweight_asserts` names the failing condition in one row |
| `corrupt-core-magic` | `provoke/live_hang_corrupt_magic.py` — a core overwrites its own mailbox magic, then parks on a buffer nothing fills | `check_core_magic` reports the corruption; the callstack on that core is no longer trustworthy |

## must_mention grading

`expected.json` pins the substrings the diagnosis has to name — the location,
the state, and the line that separates the real signal from the loud decoys.

- **mcast-ack-deficit** — `device 4`, `noc_async_atomic_barrier`,
  `mcast_ack_deficit.cpp`, `200`. Citing the arithmetic (10 iterations of 20
  phantom destinations = 200 outstanding acks) is what proves the reading.
- **llk-srca-mismatch** — `TRISC0`, `unp_A_`, `format`, `mismatch`,
  `llk_srca_format_mismatch`. The exact field (`src` or `dst`) depends on which
  side of the unpacker-A configuration check the runtime hits first, so the
  grader accepts either.
- **corrupt-core-magic** — `core_magic`, `0xDEADBEEF`. The trust gate. An agent
  that misses it quotes the parked frame instead and never mentions the
  corruption.

## Provoker discipline

A hang provoker needs two properties, or the capture is either non-deterministic
or the wrong device state:

**Absorbing state.** A kernel parked on a barrier that can never be satisfied
stays stuck until something kills it, so a triage probe can take as long as it
needs. A fault-report-then-exit case (`TensixTestWatcherSanitize`) is not
usable — freezing the producer does not help either, because a SIGSTOPped
process cannot serve the Inspector RPC.

**Device-side marker.** A cold JIT build takes minutes and is cache-dependent,
so any host-side settle either fires while kernels are still compiling or long
after. The provoking kernel prints on the line before it parks, and the harness
watches that file — so the marker means "this core is stuck", not "some seconds
have passed". The print needs a trailing newline.

The provoker must also fail loudly if the workload completes. A hang fixture
captured from a healthy device reads as a clean run under a fault's name, which
is worse than having no fixture.

## Cost, cache, reset

`llk-srca-mismatch` needs `TT_METAL_LLK_ASSERTS=1`, which is folded into the
JIT compile hash. It gets a dedicated `jit-cache-llk-asserts` so it does not
evict the shared cache on every run, and vice versa.

Every device eval resets the boards on teardown, unconditionally — a test that
leaves the fabric wedged breaks the next one. Fabric health is not checked by
enumeration alone: boards list normally while remote IO is dead.
