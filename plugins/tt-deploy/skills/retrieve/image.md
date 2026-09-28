# Image Questions

R7, R13, R14, R15. In one Bash call: § Resolve, then this block. It re-reads
`spec.md`'s entry file; the spec file must already be in `$SCRATCH`.

## Commands

```bash
E=$(ls $SCRATCH/entry_*.json | head -1); IMG=$(python3 -c "import json;print(json.load(open('$E'))['docker_image'])"); TTC=$(python3 -c "import json;print(json.load(open('$E'))['tt_metal_commit'])")
T=$(python3 -c "import json;t=json.load(open('$E'))['model_type'].lower();print('tts' if t=='text_to_speech' else t)"); N=$(python3 -c "import json;print(json.load(open('$E'))['model_name'])"); DOCP=docs/model_support/$T/${N}_${DEV,,}.md
NAME=${IMG%%:*}; NAME=${NAME#ghcr.io/}; TAG=${IMG##*:}
TOK=$(curl -sf "https://ghcr.io/token?scope=repository:$NAME:pull" | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
ACC='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
echo "==R14"; curl -s -o $SCRATCH/man.json -w 'manifest HTTP %{http_code}\n' -H "Authorization: Bearer $TOK" -H "Accept: $ACC" https://ghcr.io/v2/$NAME/manifests/$TAG
CFG=$(python3 - $SCRATCH/man.json <<'PY'
import json,sys; m=json.load(open(sys.argv[1]))
if 'manifests' in m: print("index platforms",[x.get('platform',{}).get('architecture') for x in m['manifests']],file=sys.stderr); sys.exit()
print(m['config']['digest']); print("layers",len(m['layers']),"compressed_gb",round(sum(l['size'] for l in m['layers'])/1e9,1),file=sys.stderr)
PY
)
[ -n "$CFG" ] && curl -sL -H "Authorization: Bearer $TOK" https://ghcr.io/v2/$NAME/blobs/$CFG | python3 -c "import json,sys;d=json.load(sys.stdin);print('created=',d.get('created'),'arch=',d.get('architecture'),'os=',d.get('os'))"
docker image inspect $IMG --format 'local: {{.Size}} bytes, created {{.Created}}, {{.Architecture}}' 2>/dev/null || echo "local: absent"
echo "==R13"; ghapi "https://api.github.com/repos/tenstorrent/tt-inference-server/commits?path=$DOCP&per_page=1" | python3 -c "import json,sys;d=json.load(sys.stdin);d=d[0] if isinstance(d,list) and d else {};print('doc_commit=',d.get('sha','?')[:7],(d.get('commit') or {}).get('committer',{}).get('date','unknown'))"
echo "==R7"; if docker image inspect $IMG >/dev/null 2>&1; then
  # extract per knowledge/recipes/tt-inference-server/container.md § Baked spec, then:
  python3 - $SCRATCH/image_spec.json $SCRATCH/spec.json "$MODEL" "$DEV" <<'PY'
import json,sys
i,r=json.load(open(sys.argv[1])),json.load(open(sys.argv[2])); want=sys.argv[3].lower(); dev=sys.argv[4].upper()
print("image_release=",i['release_version'],"repo_release=",r['release_version'],"schema=",i['schema_version'],r['schema_version'])
print("missing_in_image=",sorted(set(r['model_specs'])-set(i['model_specs'])))
hit=[k for k,m in i['model_specs'].items() if want in (k.lower(),*(e['model_name'].lower() for b in m.values() for x in b.values() for e in x.values()))]
print("model_in_image=",bool(hit),"device_in_image=",bool(hit and dev in i['model_specs'][hit[0]]))
PY
else echo "image not local"; fi
echo "==R15"; NEWER=$(curl -s -H "Authorization: Bearer $TOK" "https://ghcr.io/v2/$NAME/tags/list?n=1000" | python3 -c "
import json,sys,re; t=json.load(sys.stdin)['tags']; rel=re.compile(r'^(\d+)\.(\d+)\.(\d+)-([0-9a-f]{7})(-[0-9a-f]{7})?$')
v=lambda s:tuple(int(x) for x in rel.match(s).groups()[:3]) if rel.match(s) else (0,0,0)
newer=sorted((x for x in t if rel.match(x) and v(x)>v('$TAG')),key=v)
print(len(t),'tags:',len(newer),'newer release tags:',newer[-5:],file=sys.stderr); print(' '.join(rel.match(x).group(4) for x in newer[-2:]))")
echo "newer_tt_metal=$NEWER"
[ -n "$FIX" ] && for s in $TTC $NEWER; do ghapi https://api.github.com/repos/tenstorrent/tt-metal/compare/$FIX...$s | python3 -c "import json,sys;d=json.load(sys.stdin);print('$s',d.get('status') or d.get('message'),'ahead',d.get('ahead_by'),'behind',d.get('behind_by'))"; done
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| R14 | Pinned tag exists, amd64, size | manifest HTTP, `arch`, `compressed_gb`, `local` | `exists: <arch>, <n> GB compressed; local <n> GB\|absent` or `missing (HTTP <code>)` |
| R13 | Doc written vs image built | `doc_commit` date, `created` | `doc <ISO> vs image <ISO>: doc <n> h after\|before image` |
| R7 | Image spec agrees with repo | releases, `missing_in_image`, `model_in_image`, `device_in_image` | `agree (<release>)` / `image <rel> vs repo <rel>: <MODEL>/<DEV> present\|MISSING; <n> models missing` / `not local` |
| R15 | Newer tags, would one help | `newer`, compare `status` per sha | `<n> newer: <tags>; fix <sha> contained in <tags with ahead\|identical>, not in <others>` or `<n> newer: <tags>; requires --fix-sha` |

## Rules

- R14: an index manifest lists platforms; report them and read the amd64
  entry. Compressed size is the download; the local size is the extracted
  image. Both are true; neither is the other.
- R13: positive skew means the doc post-dates the image and may name models
  the image's spec lacks. R7 confirms or clears it.
- R7 `MISSING` → the documented `docker run` dies at spec resolution seconds
  after launch. State it; the override belongs to the launch stage.
- R7 never pulls. Absent image reads `not local`; `pull.md` fetches it.
- R15: `ahead` or `identical` means `$FIX` is an ancestor of that tag's
  tt-metal commit (the tag's second dash field). `behind` or `diverged` means
  not. `Not Found` means `$FIX` is not a commit sha — a PR or issue number is
  not one. Compare the pinned tag and the two newest only.
