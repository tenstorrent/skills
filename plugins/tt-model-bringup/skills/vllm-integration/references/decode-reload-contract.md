# Decode reload contract, version 1

Read this before building or changing a generator's serving decode path. It
applies to the low-level generator, the vLLM adapter, and their direct callers.
The standalone plugin introduced it in
[PR #78](https://github.com/tenstorrent/vllm-tt-plugin/pull/78).

Check `docs/DECODE_RELOAD_CONTRACT.md`, `TTDecodeReloadPlan`, and
`TTAsyncDecodeController.plan_decode_reload` in the plugin checkout selected for
the run. The [upstream specification](https://github.com/tenstorrent/vllm-tt-plugin/blob/main/docs/DECODE_RELOAD_CONTRACT.md)
is the source of truth. Record the plugin and tt-metal commits. A merged plugin
change does not prove that a particular tt-metal base class implements it.

## Interface and ownership

New bring-up adapters target `decode_input_update_contract = 1`. Put this marker
on the vLLM-facing class that implements the contract. Audit inherited methods
and overrides before setting it. If the underlying generator still takes
`reset_batch`, update that path first. Do not collapse the four commands into
that old flag or accept them through unused `**kwargs`.

The plugin decides when to reload. The model executes the commands. Require all
four keyword-only arguments in new decode methods and forward them unchanged.
Reject a legacy `reset_batch` passed through `**kwargs`. The internal plugin
flag `decode_layout_changed` is not an adapter argument.

| Command | Model action |
| --- | --- |
| `reload_inputs` | Copy all forward inputs, including tokens, positions, RoPE inputs, and every page table. |
| `reload_page_table` | Copy only page-table inputs. Keep device token, position, and RoPE state. |
| `reload_sampling_params` | Upload sampling settings, including temperature, top-k/top-p, penalty settings, seeds, and logprob settings. |
| `reset_sampling_state` | Rebuild penalty history and random-number state for the current requests. |

A full input reload includes page tables. The plugin never sends both forward
reload flags as true. A sampling-state reset requires a full input reload.
Parameter upload and state reset are separate commands. Do not infer one from
the other. Reject an unsupported command combination before changing state.

`tokens`, `start_pos`, and host RoPE inputs are current only when
`reload_inputs=True`. Otherwise, they can lag device state. Do not use them to
rebuild inputs, reset sampler state, or align seed counters. The plugin finishes
pending work and applies valid results before it builds a full reload. A
page-table-only command uses current scheduler allocation and can preserve
decode overlap.

Do not add model-side tensor comparisons, sampling-mode checks, previous-batch
checks, or async timing checks to override these commands. The model can select
a trace, but must refresh that trace's buffers as commanded. If a new trace key
depends on state the plugin cannot see, expose that requirement or keep resident
decode disabled. Never silently turn a keep-resident command into a full reload.

## Adapter forwarding example

This excerpt assumes that `generator` already implements version 1. It shows
only decode delegation. Implement construction, prefill, cache allocation, and
readback using `runtime/readiness_check/contract_vllm.py` as well.

```python
class DecodeAdapter:
    decode_input_update_contract = 1

    def __init__(self, generator):
        self.generator = generator

    def decode_forward(
        self, *, tokens, start_pos, page_table, kv_cache, enable_trace,
        read_from_device, reload_inputs, reload_page_table,
        reload_sampling_params, reset_sampling_state, slot_remap=None, **kwargs,
    ):
        if "reset_batch" in kwargs:
            raise TypeError("Version 1 does not accept reset_batch")
        return self.generator.decode_forward(
            tokens=tokens, start_pos=start_pos, page_table=page_table,
            kv_cache=kv_cache, enable_trace=enable_trace,
            read_from_device=read_from_device,
            reload_inputs=reload_inputs, reload_page_table=reload_page_table,
            reload_sampling_params=reload_sampling_params,
            reset_sampling_state=reset_sampling_state,
            slot_remap=slot_remap, **kwargs,
        )
```

Keep one owner for each remap and sampling update. The adapter above forwards
them; it must not also apply them before calling the generator.

## Modes and direct callers

The following commands describe ordinary single-token decode. `T` means true.
`F` means false. A transition includes first decode, prefill, request-layout or
sampling-mode changes, and resume. The plugin owns their detection.

| Decode step | Inputs | Page table only | Sampling params | Sampling state |
| --- | --- | --- | --- | --- |
| Device sampling on a transition | T | F | T | T |
| Steady traced device sampling with resident decode support | F | F | F | F |
| Same steady step with new KV blocks | F | T | F | F |
| Host sampling | T | F | F | F |
| Device sampling without tracing or resident decode support | T | F | On transition | On transition |

Demos and readiness drivers also send all four commands. A host-driven loop
can request full inputs on every step. It does not need the plugin's planner.
If the old demo skipped a copy, move that decision into the demo and send the
corresponding command. Do not retain a model-side heuristic for the demo.

Teacher forcing replaces the next input token with a reference token. Its
driver must request a full reload on each such step and provide the current
position. Free-running device decode can keep token and position resident
after an initial full reload. It must still reload on a request or trace
transition and send a page-table-only command when allocation changes.

For device sampling, initialize parameters and state at a new request, then
preserve them on steady steps. Warmup is different: it has no real request
history. Use full input reloads and parameter uploads for each sampled warmup
case, with `reset_sampling_state=False` when no history is supplied. If a warmup
case needs a reset, supply valid dummy history for that case.

## Sampling and slot state

`slot_remap[i] = j` means destination slot `i` takes the previous state in slot
`j`. Use a snapshot so swaps and repeated source indices work. The mapping can
be identity. Apply each supplied mapping exactly once to every slot-bound
subsystem before it reads that state. This includes recurrent/conv state,
cached RoPE state, sampler parameter shadows, and dormant sampler state during
host sampling. Forward-input reloads do not replace a remap.

Do not send remaps only to the active device sampler. Host-sampled decode can
move requests too. If a sampler cannot move some device buffers, invalidate
them and require a commanded full parameter/history rebuild before sampling
uses them again. Document unseeded hardware PRNG state that cannot move by slot;
reinitialize it on the commanded state reset.

Standard DP uses independent runner-local mappings. Single-process lane-DP
uses a merged mapping. Split that mapping by its lane stride, subtract each
lane's base, and pad any unused local sampler tail with identity. Do not use a
sampler's padded capacity as the scheduler lane stride.

A remap does not identify a new request in a reused slot. Prefill uses
`empty_slots` to initialize that request's model state. A partial prefill must
preserve all unlisted live slots: their parameters, seeds, and penalty history.
Do not treat the incoming slots as the complete live set. The next commanded
decode state reset rebuilds sampling state for the complete current layout.

Load seeds on a commanded reset even when both the old and requested seed are
`None`. A seed equality check must not skip first-use device seed initialization.
Align seeded counters from host positions only on a full input reload. Then
advance them once per sampled token. Readback must not sample, reset counters,
or advance position. If sampling is a separate call, carry the forward's
commands into that call without applying them twice.

## Async capability

Contract version and `model_capabilities["supports_async_decode"]` are separate.
Version 1 selects the four-command interface. Enable async support only after
all of these work:

1. `decode_forward(read_from_device=False)` submits without waiting for readback.
2. `read_decode_output(async_read=True)` returns `(host_output, read_events)` for
   that submission. The plugin waits for those events before host processing.
3. Device sampling writes the next token into the persistent decode input.
4. Decode advances persistent position and RoPE state exactly once per token.
5. A page-table-only reload leaves token and position state intact.
6. Host processing only formats completed output. It does not change model state.

A version-1 adapter can leave async support false. The plugin then requests full
inputs on every decode and disables async scheduling for that model. Do not
advertise async support just to enable the new interface. A generator that only
supports full reloads must reject unsupported resident-input commands.

Legacy version 0 is a compatibility path for existing adapters. The plugin
keeps their old `reset_batch` call shape and warns. New bring-up code uses
version 1. Do not change legacy scheduling to make a new adapter appear correct.

## Focused validation

Test the model path as well as the plugin's command selection:

For a full-reload-only adapter, test full reloads and clear rejection of
unsupported resident commands. Keep async support false. The successful steady
and page-table-only cases below are required when the adapter supports them.

- Full reload: change host tokens and positions, then inspect the exact trace
  buffers used by replay. Include a switch back to a previously captured mode
  or batch trace.
- Steady device decode: pass stale host tokens and positions. Verify resident
  token feedback, one position advance, and one seed advance per step.
- Page-table-only reload: grow allocation across used page boundaries. Verify
  only page-table buffers change. Also test an unchanged table with no copies.
- Sampling updates: test parameter-only upload, state reset, seeded requests,
  and a first-use `seed=None` reset that uploads device seeds.
- Slot ownership: test host-sampled remaps followed by device sampling, slot
  reuse, partial prefill beside live requests, and lane-local mapping if used.
- Lifecycle: test prefill-to-decode, host/device sampling switches, request
  completion, ordinary preemption/resume, and forced cache reset with the
  selected plugin. Ordinary preemption retains its in-flight token; forced
  reset discards only the marked stale result. These are plugin bookkeeping
  rules, not new model-side reload heuristics.

Use a non-overlapped control for multi-request page-growth and lifecycle tests.
Serving sampling tests and coherent text alone do not prove these state rules.
