# Spec Questions

R5, R6, R9, R10, R11, R12, R16. In one Bash call: § Resolve, then fetch and
query per `knowledge/recipes/tt-inference-server/model-spec.md` (§ Fetch,
§ Query: one model on one device), then the block below (`$RAW` comes from
the fetch).

## Commands

```bash
E=$(ls $SCRATCH/entry_*.json 2>/dev/null | head -1); echo "entry=${E:-NOENTRY}"
[ -n "$E" ] && python3 - "$E" $SCRATCH/config.json <<'PY'
import json,sys
e=json.load(open(sys.argv[1])); dm=e['device_model_spec']
try: c=json.load(open(sys.argv[2])); c=c.get('text_config') or c
except Exception: c={}
p=lambda k,v: print(f"{k}={v if isinstance(v,str) else json.dumps(v)}")
for k in ('status','model_name','model_type','inference_engine','docker_image','tt_metal_commit','vllm_commit','version','code_link',
          'hf_weights_repo','min_disk_gb','min_ram_gb','uses_tensor_model_cache','env_vars','override_tt_config','metadata','system_requirements'): p(k,e.get(k))
p('impl',e['impl']['impl_id']); p('max_concurrency',dm['max_concurrency']); p('max_context',dm['max_context']); p('vllm_args',dm.get('vllm_args')); p('known_issues',dm.get('known_issues'))
L,kv,nat=c.get('num_hidden_layers'),c.get('num_key_value_heads'),c.get('max_position_embeddings')
hd=c.get('head_dim') or (c.get('hidden_size',0)//max(c.get('num_attention_heads') or 1,1))
if L and kv and hd and nat:
    p('native_ctx',nat); p('ctx_ratio',round(nat/dm['max_context'],2)); p('kv_bf16_gb_at_ceiling',round(2*L*kv*hd*2*dm['max_context']*dm['max_concurrency']/1e9,1))
PY
T=$(python3 -c "import json;t=json.load(open('$E'))['model_type'].lower();print('tts' if t=='text_to_speech' else t)"); N=$(python3 -c "import json;print(json.load(open('$E'))['model_name'])")
DOCP=docs/model_support/$T/${N}_${DEV,,}.md; echo "doc=$DOCP"
curl -sf $RAW/$DOCP -o $SCRATCH/doc.md && grep -nE 'docker run|^  --|^  ghcr|run\.py|Max Batch|Max Context|Model Status|Docker Image|Commit' $SCRATCH/doc.md
CL=$(python3 -c "import json;print(json.load(open('$E'))['code_link'])"); curl -sfL "$(echo $CL | sed 's#github.com#raw.githubusercontent.com#;s#/tree/#/#')/README.md" -o $SCRATCH/impl_readme.md && grep -inE 'max(imum)? (context|seq|batch)|context length|kv.?cache|dram' $SCRATCH/impl_readme.md | head -8
[ -n "$TT" ] && $TT model info "$MODEL" --json 2>&1 | python3 -c "
import json,sys; d=json.load(sys.stdin); s=d['devices'].get('$DEV'.lower()) or {}
print('tt:',d['name'],'servable=',bool(d.get('tt_model_id')),'supported=',s.get('supported'),'status=',s.get('status'),'image=',s.get('docker_image'),'ctx=',s.get('max_context'),'unsupported=',s.get('unsupported'))"
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| R5 | Implementation for my hardware | `entry` vs `NOENTRY`; `tt` `supported` | `yes: <impl> via <engine>, <status>` or `no: <MODEL> has no <DEV> entry (has: <model_devices>)`; append `tt marks unsupported: <details> (<verified_on>)` when set |
| R6 | Exact tt implementation | doc `docker run` block and `run.py` line; `docker_image`, commits, `status`, `max_concurrency`, `max_context` | both commands verbatim, then `image <tag>; tt-metal <sha>; vLLM <sha>; <status>; batch ≤<n>; context ≤<n>` |
| R9 | Full runtime spec values | `env_vars`, `override_tt_config`, `vllm_args`, `metadata`, `min_disk_gb`, `min_ram_gb`, `system_requirements` | one `key=value` per line, verbatim |
| R10 | Maturity status | `status` | `COMPLETE` / `FUNCTIONAL` / `EXPERIMENTAL` |
| R11 | known_issues | `known_issues` | `none` or one `<workflow_type>/<task_name>: <reason>` per issue |
| R12 | Why the batch/context limits | `max_context` vs `native_ctx`, `kv_bf16_gb_at_ceiling`, README grep | `ceiling <n> vs native <n> (<ratio>×): <spec-imposed\|native\|extended>; KV at ceiling ≈<n> GB bf16 (2·L·KV·d·2B·ctx·batch); impl README: <line\|no statement>` |
| R16 | run.py or direct docker | R3 `gated`, `hf_token` | row from the table below |

## Rules

- R5: spec first, `tt model info` second. `tt` hides board-specific breakage
  the spec still lists; report both verdicts, never merge them.
- R6: the `docker run` block is the doc's; the image tag is the spec's. When
  they disagree, report both tags and flag `doc lags spec`.
- R6: doc fetch fails → synthesize the command from `knowledge/recipes/
  tt-inference-server/container.md` § Mandatory docker flags and say
  `synthesized`.
- R12: `spec-imposed` when native > ceiling; `native` when equal; `extended
  past native (rope scaling)` when ceiling > native — then quote
  `rope_scaling` from R2. The KV figure sizes the hardware reason; the README
  line states it. Neither proves the other.
- R16: rule in `knowledge/recipes/tt-inference-server/container.md`
  § run.py vs direct docker. Verdict:

| `gated` | token | R16 |
|---|---|---|
| false | unset | `direct docker run; run.py refuses without a valid HF_TOKEN` |
| false | set | `either; run.py adds host checks, volume naming, log capture` |
| auto/manual | unset | `blocked: get a token and accept the terms first (R3)` |
| auto/manual | set | `either; the token must have accepted terms (R3)` |
