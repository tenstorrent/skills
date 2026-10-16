# Model Questions

D6, D7, D15, D16. D6 and D11 need the spec; D7 and D15 need `--model`.

## Spec

```bash
RAW=https://raw.githubusercontent.com/tenstorrent/tt-inference-server/main
SHA=$(curl -sf https://api.github.com/repos/tenstorrent/tt-inference-server/commits/main | python3 -c "import json,sys;print(json.load(sys.stdin)['sha'][:7])")
curl -sf $RAW/release_model_spec.json -o $SCRATCH/spec.json
curl -sf $RAW/docs/model_support/models_by_hardware.md -o $SCRATCH/mbh.md
curl -sf $RAW/docs/model_support/llm/README.md -o $SCRATCH/llm.md
echo "spec fetched from main@$SHA $(date -Is)"
```

Any curl failure → the dependent rows read `fetch failed: <url>`.

```bash
DEV=${DEV:?set from D9, uppercase}; python3 - $SCRATCH/spec.json "$DEV" "${MODEL:-}" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); dev=sys.argv[2].upper(); want=sys.argv[3]
print("schema",d['schema_version'],"release",d['release_version'])
print("device union:",sorted({k for m in d['model_specs'].values() for k in m}))
for repo,m in d['model_specs'].items():
    if dev not in m or (want and repo!=want): continue
    for be,impls in m[dev].items():
        for impl,e in impls.items():
            sr=e.get('system_requirements') or {}
            print(f"{repo} | {e['model_name']} | {e['status']} | {be}/{impl} | min_disk_gb={e.get('min_disk_gb')} min_ram_gb={e.get('min_ram_gb')} "
                  f"fw={sr.get('firmware',{}).get('specifier','-')} kmd={sr.get('kmd',{}).get('specifier','-')} tensor_cache={e.get('uses_tensor_model_cache')}")
PY
grep -ohE '\[[^]]+\]\([^)]*_'"${DEV,,}"'\.md\)' $SCRATCH/mbh.md $SCRATCH/llm.md | sed 's/\].*//;s/\[//' | grep -vE 'Complete|Functional|Experimental' | sort -u
```

## Per-model (requires `--model`)

```bash
M=${MODEL:?}; curl -s -o $SCRATCH/hf.json -w '%{http_code}\n' https://huggingface.co/api/models/$M
python3 -c "import json;d=json.load(open('$SCRATCH/hf.json'));print('gated=',d.get('gated'),'private=',d.get('private'),'files=',len(d.get('siblings',[])))"
curl -sf https://huggingface.co/api/models/$M/tree/main | python3 -c "import json,sys;print(round(sum(f.get('size',0) for f in json.load(sys.stdin) if f['path'].endswith('.safetensors'))/1e9,1),'GB safetensors')"
```

## Clean-box

```bash
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep -E 'vllm-tt-metal|tt-inference-server|tt-media'
docker volume ls -q | grep '^volume_id_'
ls -d ~/.cache/tt-metal-cache 2>/dev/null; ls ~/tt-inference-server ~/*/tt-inference-server -d 2>/dev/null
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| D6 | Models deployable here | spec rows for `$DEV`; doc model names from the grep | `<n> in spec for <DEV> (<n> Complete/Functional/Experimental); drift: <names>` then the repo list with status |
| D7 | Available and gated? | HTTP code; `gated`, `private`, `files` | `exists, gated=<false\|auto\|manual>, <n> files` or `not found (HTTP <code>)` |
| D15 | Disk for weights + cache | `min_disk_gb` (spec), safetensors GB (HF), `tensor_cache`, D1 free | `weights <n> GB + tensor cache (spec min <n> GB) vs <n> free: fits/short by <n>` |
| D16 | Box actually clean? | Clean-box output plus D5 | `clean` or lists images, volumes, caches, checkouts found |

## Rules

- D6: the spec is authoritative; the docs are a cross-check. Compare spec
  `model_name` against the grepped doc names. Names on one side only → `drift`.
- D6: `$DEV` not in the device union → `none: <DEV> not a spec device`.
- D7: HF returns 401 for unknown repos when unauthenticated. 401 and 404
  both mean `not found or private`. Only a 200 proves existence.
- D7: `gated` is `false`, `"auto"`, or `"manual"`. Either string means a
  token with accepted terms is required.
- D15: `min_disk_gb` already includes the tensor cache when
  `tensor_cache=True`. Report weights and min_disk separately; the delta is
  the cache cost the model docs omit.
- D16: `clean` is a claim about timing. Cached images and `volume_id_*`
  volumes make a first launch look warm. Disclose; never rely silently.
