# tt-inference-server containers

Shared by `tt:retrieve` (`spec.md` R6/R16, `image.md` R7), `tt:launch`
(L3 mandatory flags, L5 overrides) and `tt:verify` (V22 log reads).

Everything below was checked against the live container `qwen36-golden-test`
on 2026-09-22 — image `0.20.0-de59f8a-03fa3af`, host p300x2. Where a fact is
release-dependent it says so; a fact without a release pin is structural.

## Mandatory docker flags

Three, every time. All three confirmed present on the container that reached
`/health` 200:

```bash
--device /dev/tenstorrent \
--ipc host \
--mount type=bind,src=/dev/hugepages-1G,dst=/dev/hugepages-1G
```

| Flag | Why | How to confirm on a running container |
|---|---|---|
| `--device /dev/tenstorrent` | without it the runtime sees no chips at all | `docker inspect --format '{{json .HostConfig.Devices}}'` |
| `--ipc host` | tt-metal's shared-memory dispatch needs the host IPC namespace; the default 64 MB `/dev/shm` is not enough | `docker inspect --format '{{.HostConfig.IpcMode}}'` → `host` |
| hugepages-1G bind | 1 GB pages back the device queues; missing → device init fails, not a slow path | `docker inspect --format '{{json .Mounts}}'` → a `bind` with destination `/dev/hugepages-1G` |

A `--mount type=bind` does **not** appear in `.HostConfig.Binds`; read
`.Mounts`. Checking only `Binds` reports a correctly-launched container as
missing the flag.

Observed alongside them, not part of the mandatory three: a named cache
volume `volume_id_<impl_id>-<model_name>` → `/home/container_app_user/cache_root`.
That is where the weights and the tensor cache live, so it is what makes a
relaunch "warm"; `run.py` creates it, a hand-built `docker run` must name it
itself or pay the download again.

## Baked spec

Every image carries its own copy of `release_model_spec.json`. R7 compares it
against `main` to detect drift. Extract without starting the container —
`docker create` + `docker cp`, never `docker run`:

```bash
SPEC_IN_IMG=${MODEL_SPECS_JSON_PATH:-/home/container_app_user/model_specs/model_spec.json}
C=$(docker create "$IMG" true) && docker cp "$C:$SPEC_IN_IMG" $SCRATCH/image_spec.json; docker rm -f "$C" >/dev/null
python3 -c "
import json;d=json.load(open('$SCRATCH/image_spec.json'))
print('image_release=',d['release_version'],'schema=',d['schema_version'],'models=',len(d['model_specs']))"
```

The in-image path is the image's own `MODEL_SPECS_JSON_PATH` env var —
verified `/home/container_app_user/model_specs/model_spec.json` on 0.20.0.
Read it from the image rather than hard-coding:
`docker inspect "$IMG" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep MODEL_SPECS_JSON_PATH`.

**Two variable names exist and they are not interchangeable.** The image ENV
is `MODEL_SPECS_JSON_PATH`. The failure signature that killed the 0.10.0 run
names `TT_MODEL_SPEC_JSON_PATH`:
`RuntimeError: TT_MODEL_SPEC_JSON_PATH environment variable is not set`.
Report whichever the log actually printed; do not normalise one into the other.

Drift goes in **either** direction. On 2026-09-22: image 0.20.0 / 69 models,
the copy installed at `~/.local/lib/tt-inference-server/` 0.12.0 / 62 models,
`main` 0.22.0 / 70 models. The question is *which side is stale*, never "the
image is stale".

## run.py vs direct docker

`run.py` is the repo's launcher: it validates the host, names the cache
volume, assembles the flags above and captures logs. A hand-built
`docker run` does the same work with nothing validating it.

The deciding factor is the HF token, because `run.py` gates on it:

| `gated` | `HF_TOKEN` | Verdict |
|---|---|---|
| false | unset | `direct docker run; run.py refuses without a valid HF_TOKEN` |
| false | set | `either; run.py adds host checks, volume naming, log capture` |
| auto / manual | unset | `blocked: get a token and accept the terms first` |
| auto / manual | set | `either; the token must have accepted terms` |

Release-pinned, 0.20.0: the container itself only **warns** on a missing
`HF_TOKEN` for ungated weights — the 2026-09-22 run bypassed `run.py`
deliberately and reached `/health` 200. So "prefer `run.py`" is not a
universal rule: for ungated weights on 0.20.0 the direct `docker run` is the
working path and `run.py` is the blocked one. Pin any such recommendation to
the image release that was tested.

## Launch-time behaviour worth knowing

- `has_builtin_warmup: true` in the spec entry → the stack logs
  `skipping background trace capture`. There is **no** separate trace-capture
  stage for such a model; a step list that always includes one does not
  reproduce.
- The stack silently overrides things at startup and none of it is a failure
  signature. Observed on 0.20.0 / p300x2: `ARCH_NAME wormhole_b0 → blackhole`,
  `MESH_DEVICE P300x2 → (1,4)`, and a device misdetection
  (`Unknown model ... on device P150x4, setting MAX_PREFILL_CHUNK_SIZE to 4`)
  while the host is p300x2. Grep for these at launch, not only after `/health`.
- Never print `HF_TOKEN` or `JWT_SECRET` out of a captured log or `.env`.
  Report `set` / `unset`.
