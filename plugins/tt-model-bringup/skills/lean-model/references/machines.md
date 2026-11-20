# One or two machines

## One machine

- **Dies as slots.** Single-die work (module ports, op tests, microbenchmarks) runs one agent per die with its
  own device visibility. Full-model and multi-die runs take the whole box through the lock; the coordinator
  schedules those windows and keeps them short.
- **The host as a worker.** The CPU reference, captures and teacher-forced checks run on the host with capped
  threads, paused during performance measurements. Watch host memory: one reference model copy at a time.
- **One build.** Rebuilds of the C++ tree are serialized; Python and model-local kernels need none.
- **Order of the queue.** T1 for a landing first, then measurements, then exploration.

## Two machines

Split by role rather than by halves of the work:

- **Development machine**: module work, perf loop, prototypes; T0 and T1.
- **Validation machine**: T2 on landed heads, long context, the served smoke, evals, the CPU reference. It never
  blocks development: it tests what has landed while the next work proceeds.

Keep both on the same base commit and build, and record firmware and driver versions per machine; a difference
there can look like a model bug. Code moves one way, through git. Results come back as files. If one machine is
lost, the other carries both roles at a slower pace; plan for that.
