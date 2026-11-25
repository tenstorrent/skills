# Environment and small traps

Containers, sandboxes, caches, offline Hugging Face, commit hooks, network storage and small API traps that cost
runs time.

- **AutoDebug sandbox.** Hosts without unprivileged user namespaces fail the AutoDebug child sandbox preflight
  (bubblewrap) at every stage; `AUTODEBUG_SKIP_CHILD_SANDBOX=1` under an explicitly authorized unrestricted
  policy was used.
- **Caches and home in containers.** A non-root container without a writable home failed on the TT-Metal kernel
  cache, pre-commit and uv caches; setting `TT_METAL_CACHE`, `PRE_COMMIT_HOME`, `UV_CACHE_DIR` and a user name
  up front avoided repeated failures.
- **Offline Hugging Face.** Partial blob downloads and hanging network lookups cost time; `HF_HUB_OFFLINE=1`
  with a verified local snapshot avoided them, and a cache without `refs/main` breaks name-based loading.
- **Commit hooks on evidence.** Formatting hooks rewrote raw model outputs and evidence files; keep raw outputs
  out of git (`model-bringup`) or commit them with hooks skipped after verifying the bytes.
- **`ttnn.Shape` is not a list:** no slicing; convert with `list(shape)` before reshaping. This failed first runs
  repeatedly after long compiles.
- **Enum and docstring traps:** the binary multiply SFPU enum is `MUL_UNARY_SFPU`; one paged decode attention mask
  layout differed from its docstring.
- **Environment flags read as booleans:** a flag set to `0` was read as unset by a truthiness check.
- **Network storage limits:** a shared NFS mount ran out of inodes with terabytes free.
- **Truncated tensor-cache entries.** A cache entry truncated by a process killed mid-write stalled every later
  loader on the host (one thread at full CPU, no I/O, no log line) and looked like a hang. A separate tensor-cache
  directory per experiment series avoided it.
- **Host-only tests opened chips.** A parametrized device fixture in an unrelated test module opened every chip on
  the host, including chips another job held; setting `TT_VISIBLE_DEVICES` on host-only test sweeps too avoided it.
