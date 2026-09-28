# HF Hub

Shared by `tt:retrieve` (`hf.md`, `bundle.md`) and any skill that reads the
Hugging Face Hub. Read-only: these sections never download weights.

`$SCRATCH` is the session scratchpad. `$M` is the repo id being inspected —
callers set it (`M=$MODEL`, `M=$W`) before running § Inspect one repo.

## Inspect one repo

Defines `hfapi` and answers existence, gating, revision and size for `$M`.
Every HF-touching Bash call runs this section first.

```bash
HF_TOKEN=${HF_TOKEN:-$(cat ${HF_HOME:-$HOME/.cache/huggingface}/token 2>/dev/null)}
hfapi(){ curl -s ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} "$@"; }
echo "token=$([ -n "$HF_TOKEN" ] && echo set || echo unset)"
hfapi -o $SCRATCH/repo.json -w "repo HTTP %{http_code}\n" "https://huggingface.co/api/models/$M"
hfapi -o $SCRATCH/tree.json -w "tree HTTP %{http_code}\n" "https://huggingface.co/api/models/$M/tree/main?recursive=true"
python3 - $SCRATCH/repo.json $SCRATCH/tree.json <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("repo=unreadable"); raise SystemExit
if 'id' not in d: print("repo_error=",d.get('error')); raise SystemExit
sib=[s['rfilename'] for s in d.get('siblings') or []]
print("id=",d['id'],"sha=",str(d.get('sha'))[:7],"modified=",d.get('lastModified'))
print("gated=",d.get('gated'),"private=",d.get('private'),"disabled=",d.get('disabled'))
print("files=",len(sib),"tags=",[t for t in d.get('tags') or []][:12])
print("bundle_manifest=", 'tt_kernel_manifest.json' in sib)
try: t=json.load(open(sys.argv[2]))
except Exception: t=[]
if isinstance(t,list):
    st=[f for f in t if f.get('path','').endswith('.safetensors')]
    sz=lambda fs: round(sum((f.get('lfs') or {}).get('size') or f.get('size') or 0 for f in fs)/1e9,1)
    print("safetensors_files=",len(st),"safetensors_gb=",sz(st),"repo_gb=",sz([f for f in t if f.get('type')=='file']))
PY
```

`gated` is `false`, `"auto"` or `"manual"` — never a bare boolean true.
`safetensors_gb` is the weight download only; `repo_gb` includes everything
else in the repo. Neither is the on-disk footprint after conversion.

## Fetch a file

One file out of a repo, into the scratchpad. Callers name the file; `hf.md`
fetches `config.json`, `bundle.md` fetches `tt_kernel_manifest.json`,
`install.sh`, `run.sh`, `requirements.txt`.

```bash
F=config.json
hfapi -L -o $SCRATCH/$F -w "$F HTTP %{http_code}\n" "https://huggingface.co/$M/resolve/main/$F"
```

`-L` is mandatory: `resolve` 302-redirects to the CDN, and without it the
file lands as an empty body with HTTP 302. A missing file returns 404 with a
JSON error body, which still writes to `-o` — check the status before
parsing, never `[ -s file ]`.

## Traps

| Symptom | Meaning |
|---|---|
| repo 200, `gated: auto` | Exists; a token with accepted terms is required. Click-through is automatic once accepted. |
| repo 200, `gated: manual` | Exists; the author approves each request by hand. A valid token is not sufficient. |
| repo 401 / 403 | Gated or private. Indistinguishable from outside — report `not found or private (HTTP <code>)`, never "does not exist". |
| repo 404 | No such id, **or** private and the token lacks access. Same answer as above. |
| repo 200 but `config.json` 401 with `token=set` | The token is valid but lacks access to this repo. Append `local token lacks access`. |
| repo 200, `disabled: true` | Repo exists but is disabled upstream; treat as unusable and say so. |
| `sha` differs between runs | `main` moved. Record the sha in Evidence — an answer about a repo is only true for one revision. |
| Rate limited (429) | Unauthenticated Hub reads are throttled per IP. `HF_TOKEN` lifts it. Report `fetch failed`, never retry in a loop. |

NEVER print the token value. Report `token=set` / `token=unset` only, and
never expand a header string from a variable — `hfapi` carries the header.
