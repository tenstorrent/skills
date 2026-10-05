# Bundle Questions

B1–B20: tt-model-manager bundles. A bundle id is an HF repo id carrying
`tt_kernel_manifest.json`. `$TM`, `$TT`, `hfapi`, `$MODEL`, `$SEARCH` from
`SKILL.md` § Resolve. In one Bash call: § Resolve, `knowledge/hf-hub.md`
§ Inspect one repo with `M=$MODEL` (bundle repo: `tags`, `private`, sizes),
the block below, then § Inspect one repo again with `M=$W`.

## Commands

```bash
HUB=https://huggingface.co/api/models; CACHE=${XDG_CACHE_HOME:-$HOME/.cache}; IDX=$CACHE/tt-model/installed.json
echo "==B1-3"; T=${SEARCH:-${MODEL##*/}}
curl -sf "$HUB?filter=tt-model-cache&search=$T&limit=50" | python3 -c "
import json,sys
for m in json.load(sys.stdin): print(m['id'],'|',' '.join(t for t in m['tags'] if t in ('tt-model-catalog','tt-model-container','self-contained','thin','blackhole','wormhole_b0') or t.startswith(('p1','p3','n1','n3'))))"
[ -n "$TM" ] && $TM search "$T" --catalog 2>&1 | head -20
echo "==B4"; python3 -c "
import json; d=json.load(open('$IDX'))
for k,v in d.items(): print(k,'|',v.get('image'),'| rev',str(v.get('revision'))[:8],'| profile',v.get('profile'),'| manifest',v.get('manifest'))" 2>/dev/null || echo "no install index"
[ -n "$TM" ] && $TM list 2>&1
echo "==B6-13"; hfapi -L -o $SCRATCH/manifest.json -w 'manifest HTTP %{http_code}\n' https://huggingface.co/$MODEL/resolve/main/tt_kernel_manifest.json
python3 - $SCRATCH/manifest.json <<'PY'
import json,sys
try: m=json.load(open(sys.argv[1]))
except Exception: sys.exit()
c=m.get('container') or {}; s=c.get('serve') or {}; b=c.get('built') or {}; w=m['weights']; tm=b.get('tt_metal') or {}
print("schema=",m['schema_version'],"kind=",'container' if c else 'fat' if m.get('bundled') else 'thin' if m.get('deps') else 'unknown')
print("arch=",m['arch'],"device_count=",m['device_count'],"hardware=",s.get('hardware'),"mesh_device=",s.get('mesh_device'),"mesh=",m.get('mesh'))
print("weights=",w.get('repo_id') or w.get('repo'),"revision=",w.get('revision'),"patterns=",w.get('allow_patterns'),w.get('ignore_patterns'))
print("engine=",c.get('kind'),"image=",(c.get('image') or {}).get('tag'),"digest=",str((c.get('image') or {}).get('digest'))[:19])
print("serve=",json.dumps({k:s.get(k) for k in ('port','max_model_len','max_num_seqs','block_size','capabilities','additional_config','args','env')}))
print("profiles=",[p.get('name') for p in c.get('serve_profiles') or []],"default=",c.get('default_profile'))
print("built=",json.dumps({'created':b.get('created_at'),'tt_metal':tm.get('describe'),'branch':tm.get('branch'),'dirty':tm.get('dirty'),'plugin':str((b.get('plugin') or {}).get('sha'))[:8]}),"tt_metal_version=",m.get('tt_metal_version'))
d=m.get('deps') or {}; bd=m.get('bundled') or {}
print("thin=",json.dumps({'requirements':d.get('requirements'),'wheels':d.get('wheels'),'vllm':d.get('vllm')}),"fat=",json.dumps({'python':bd.get('python'),'vendored':bd.get('deps_vendored'),'install':bd.get('install_script'),'run':bd.get('run_script'),'firmware_min':bd.get('firmware_min')}))
PY
for f in install.sh run.sh requirements.txt; do hfapi -fL https://huggingface.co/$MODEL/resolve/main/$f -o $SCRATCH/bundle_$f && echo "$f: $(wc -l < $SCRATCH/bundle_$f) lines; net/priv hits: $(grep -cE 'curl|wget|pip install|sudo|rm -rf' $SCRATCH/bundle_$f)"; done
grep -iE '^(ttnn|tt-metal-models|vllm|torch)\b' $SCRATCH/bundle_requirements.txt 2>/dev/null
W=$(python3 -c "import json;w=json.load(open('$SCRATCH/manifest.json'))['weights'];print(w.get('repo_id') or w.get('repo'))" 2>/dev/null); echo "weights_repo=$W"
# then knowledge/hf-hub.md § Inspect one repo with M=$W
echo "==B16"; [ -n "$TM" ] && $TM serve "$MODEL" --print --local-only 2>&1 | tail -3
[ -n "$TT" ] && $TT serve "$MODEL" --dry-run --json 2>&1 | head -40
echo "==B19"; NM=$(python3 -c "import json;print(json.load(open('$SCRATCH/manifest.json'))['name'])" 2>/dev/null); [ -n "$NM" ] && du -sh $CACHE/tt-model/$NM $CACHE/tt-model/pulled/${MODEL//\//__} 2>/dev/null; [ -n "$W" ] && du -sh ${HF_HOME:-$HOME/.cache/huggingface}/hub/models--${W//\//--} 2>/dev/null
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| B1 | Bundles for X | `tt-model-cache` hits | `<n> bundles: <id> (<tags>)` per line |
| B2 | Catalog or just findable | `tt-model-catalog` in `tags` (search hits; bundle inspect) | `listed` / `unlisted: public by link` per id |
| B3 | Tagged for my arch | arch + board tags vs `$DEV` | ids with the host arch; board tag ≠ `${DEV,,}` → `board tag <t> ≠ host <dev>` |
| B4 | Already installed | `installed.json` key = `$MODEL` (case-insensitive) | `yes: image <tag>, rev <sha>, profile <p>` / `no` |
| B5 | Also in tt-inference-server | R5 | `yes: see R5` / `no: R5` |
| B6 | Read install.sh / run.sh first | fetched line counts, hit counts | `install.sh <n> lines (<n> net/priv hits), run.sh <n> lines (<n>)`; container kind → `n/a: image entrypoint; see B16` |
| B7 | Mesh packaged for vs mine | `arch`, `hardware`, `device_count` vs `$DEV`, D10 boards | `packaged <hardware> (<n> chips, <arch>) vs host <DEV>: match` / `mismatch: <boards\|arch\|chips>` |
| B8 | Fat / thin / container | `kind` | `v5 fat` / `v5.1 container` / `v6 thin` |
| B9 | Schema accepted | `schema` ∈ {5, 5.1, 6} | `accepted (<schema>)` / `refused: pre-v5; author must re-publish` |
| B10 | v6: resolved at install | pinned `requirements.txt` lines | the grep lines verbatim; non-thin → `n/a: <kind>` |
| B11 | v5: download size | `wheels_gb`, `metal_gb`, `image_gb`, `bundle_total_gb` | `<n> GB bundle (<image\|wheels+metal> <n> GB) + weights <n> GB (R4)` |
| B12 | Weights repo pointer | `weights`, `revision` | `<repo>@<rev\|main>`, plus patterns when set |
| B13 | Weights gated/private/gone | weights-repo inspect: HTTP, `gated`, `private`, `files` | R8/R3 format, for `<W>` |
| B14 | Pull weights now or defer | static | `defer by default; tt-model pull <id> --with-weights (tt model pull <id> does this) fetches now; otherwise first serve fetches` |
| B15 | Credentials, for which repo | bundle `private`; weights `gated`, `private` | `bundle: none\|token; weights: none\|token+terms\|token+access; token: set\|unset` |
| B16 | Exact command before running | `--print` output | the `docker run …`/`run.sh` line verbatim, or `requires pull: bundle not installed` |
| B17 | No network | static | `tt-model serve <id> --local-only; only pull touches the Hub` |
| B18 | vLLM passthrough | static | `tt-model serve <id> -- <args>; tt serve <id> -- <args>` |
| B19 | Where it lands, disk | `du`, B11, R4 | `<cache>/tt-model/<name>/{cache,weights} + HF hub <W>; used now <n>; expected <n> GB` |
| B20 | Remove cleanly | static | `tt-model rm <id> [--keep-cache] [--include-weights]; tt model rm <id>` |

## Rules

- Search filters on the `tt-model-cache` tag; `--catalog` narrows to
  `tt-model-catalog`. Listing is an opt-in pointer, not a working guarantee.
- B3/B7: bundles name boards (`<board>[xN]`), spec keys name compositions.
  Chip counts match across `P300X2`/`P150X4`; a board mismatch is still
  `mismatch` (`knowledge/hardware/boards.md`). Arch mismatch is fatal in
  `tt-model`; chip-count mismatch warns and can be forced.
- B13: the weights repo is a different repo from the bundle. A valid public
  bundle can point at weights the local token cannot read.
- B16: `--print --local-only` never installs. Not installed → say so. NEVER
  drop `--local-only` to make it print.
- `tt model pull <id>` adds `--with-weights`; bare `tt-model pull <id>` does
  not. Say which one B14 quotes.
