# tt-inference-server release spec

`release_model_spec.json` is the upstream source of truth for *what runs on
what*: image tag, commits, maturity status, ceilings, env, known issues.
Shared by `tt:retrieve` (`spec.md`, `image.md`), `tt:discover` (`models.md`
D6/D7/D11/D15) and `tt:launch` (what the launch *should* have been).

## Layout

```
release_model_spec.json
├── schema_version                       "0.1.0"
├── release_version                      "0.20.0"   ← the spec's own release
└── model_specs
    └── "<HF repo id>"                   "Qwen/Qwen3.6-27B"
        └── "<DEVICE KEY>"               "P300X2" (upper-case, knowledge/hardware/boards.md)
            └── "<ENGINE>"               "vLLM"
                └── "<impl_id>"          "qwen36_blackhole"
                    ├── status, model_name, model_type, version
                    ├── docker_image, tt_metal_commit, vllm_commit, code_link
                    ├── hf_model_repo, hf_weights_repo, min_disk_gb, min_ram_gb
                    ├── env_vars, override_tt_config, cli_args, metadata
                    ├── has_builtin_warmup, uses_tensor_model_cache, repacked
                    ├── impl { impl_id, impl_name, repo_url, code_path }
                    └── device_model_spec { max_context, max_concurrency,
                        vllm_args, known_issues, default_impl, env_vars,
                        override_tt_config, perf_targets_map, ... }
```

Four levels below `model_specs`, not two. The per-device ceilings live in
`device_model_spec`; the per-impl identity lives beside it.

## Fetch

Always from GitHub raw at `main`. NEVER a local checkout: the copy installed
under `~/.local/lib/tt-inference-server/` was release **0.12.0 / 62 models**
on 2026-09-22 while `main` was **0.22.0 / 70 models** — a ten-release skew.

```bash
RAW=https://raw.githubusercontent.com/tenstorrent/tt-inference-server/main
curl -sf -o $SCRATCH/spec.json -w "spec HTTP %{http_code}\n" $RAW/release_model_spec.json
SPEC_SHA=$(ghapi "https://api.github.com/repos/tenstorrent/tt-inference-server/commits?path=release_model_spec.json&per_page=1" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print((d[0]['sha'][:7]) if isinstance(d,list) and d else '?')")
python3 -c "
import json;d=json.load(open('$SCRATCH/spec.json'))
print('release=',d['release_version'],'schema=',d['schema_version'],'models=',len(d['model_specs']))
print('devices=',sorted({k for m in d['model_specs'].values() for k in m}))"
echo "spec_sha=$SPEC_SHA"
```

`$RAW` and `$SPEC_SHA` stay set for the rest of the call — `spec.md` builds
the per-model doc URL from `$RAW`, and the Sources footer quotes `$SPEC_SHA`.
One fetch per run, reused by every row.

## Query: one model on one device

Writes `$SCRATCH/entry_<impl_id>.json` — the flat entry that `spec.md` and
`image.md` both re-read. Callers set `$MODEL` and `$DEV` (upper-case).

```bash
python3 - $SCRATCH/spec.json "$MODEL" "$DEV" $SCRATCH <<'PY'
import json,sys,os
spec,want,dev,scratch=json.load(open(sys.argv[1])),sys.argv[2].lower(),sys.argv[3].upper(),sys.argv[4]
key=next((k for k in spec['model_specs'] if k.lower()==want or k.split('/')[-1].lower()==want), None)
if not key: print("spec_model=NOMODEL"); raise SystemExit
devs=spec['model_specs'][key]
print("spec_model=",key,"model_devices=",sorted(devs))
if dev not in devs: print("entry=NOENTRY"); raise SystemExit
engines=devs[dev]
eng='vLLM' if 'vLLM' in engines else sorted(engines)[0]
impls=engines[eng]
dflt=next((i.get('device_model_spec',{}).get('default_impl') for i in impls.values() if i.get('device_model_spec')), None)
iid=dflt if dflt in impls else sorted(impls)[0]
e=impls[iid]
json.dump(e, open(os.path.join(scratch,f"entry_{iid}.json"),"w"), indent=1)
print("engine=",eng,"impl_id=",iid,"impls_available=",sorted(impls),"engines_available=",sorted(engines))
print("entry=",os.path.join(scratch,f"entry_{iid}.json"))
PY
```

Then read the entry with `spec.md` § Commands. The two outcomes the callers
branch on are the literal strings `NOMODEL` / `NOENTRY` and an `entry=` path.

## Rules

- Resolve the model key case-insensitively, and accept a bare name
  (`qwen3.6-27b`) as well as the full repo id. The spec keys the HF repo id.
- More than one `impl_id` under one engine is normal. `default_impl` in
  `device_model_spec` picks the one the stack would run; report the others
  as `impls_available` rather than silently dropping them.
- Ceilings (`max_context`, `max_concurrency`) are **per device**, in
  `device_model_spec`. Never quote a ceiling without the device it belongs to.
- `status` is one of `COMPLETE` / `FUNCTIONAL` / `EXPERIMENTAL`. It describes
  the impl on that device, not the model.
- `release_version` inside the file is the spec's release, which is not
  necessarily the release of any image it names. The image carries its own
  copy — `container.md` § Baked spec reads it, and the two drift in *either*
  direction (on 2026-09-22 the installed repo copy was stale at 0.12.0 while
  the image was current at 0.20.0).
- `min_disk_gb` / `min_ram_gb` include the tensor cache; the HF safetensors
  total does not. They are different numbers and both are true.
