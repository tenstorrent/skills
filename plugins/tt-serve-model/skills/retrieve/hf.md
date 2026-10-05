# Hub Questions

R1, R2, R3, R4, R8. Run `knowledge/hf-hub.md` § Inspect one repo with
`M=$MODEL`, § Fetch a file for `config.json`, and the block below, in one
Bash call.

## Commands

```bash
HUB=https://huggingface.co/api/models
echo "==R1"; [ -n "$SEARCH" ] && curl -sf "$HUB?search=$SEARCH&limit=20" | python3 -c "import json,sys;[print(m['id'],'|',m.get('pipeline_tag'),'|',m.get('downloads')) for m in json.load(sys.stdin)]"
echo "==R2"; python3 - $SCRATCH/config.json <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1])); d=d.get('text_config') or d
except Exception: sys.exit()
keys=('architectures','model_type','num_hidden_layers','hidden_size','intermediate_size','num_attention_heads','num_key_value_heads','head_dim',
      'max_position_embeddings','rope_scaling','rope_theta','torch_dtype','dtype','vocab_size','tie_word_embeddings','quantization_config',
      'num_experts','num_local_experts','num_experts_per_tok','sliding_window','layer_types')
for k in keys:
    if k in d: print(f"{k}={json.dumps(d[k])[:300]}")
print("config_keys=",len(d))
PY
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| R1 | Models deployable in general | R1 hits | `<n> Hub hits for <term>: <ids>` — TT support is R5 and B1, not this row |
| R8 | Does the model exist | repo HTTP, `files`, `sha`, `modified` | `yes: <n> files, rev <sha>, modified <date>` or `not found or private (HTTP <code>)` |
| R3 | Credentials needed | `gated`, `private`, token state | `none` / `HF token with accepted terms (gated=<auto\|manual>)` / `HF token with repo access (private)`; append `token: set\|unset` |
| R2 | Information about the model | config HTTP, printed fields | the printed `key=value` lines verbatim |
| R4 | Architecture and config parameters | `architectures`, layers, heads, kv heads, `head_dim`, `max_position_embeddings`, dtype, quantization, experts, `safetensors_gb` | `<arch>; <L> layers; <H>/<KV> heads, head_dim <d>; native ctx <n>; <dtype>; quant <q\|none>; MoE <e>×<k>\|dense; weights <n> GB safetensors` |

## Rules

- R8, R3: status-code and `gated` semantics per `knowledge/hf-hub.md` § Traps.
  `config.json` 401 with `token: set` → append `local token lacks access`.
- R4: `head_dim` absent → `hidden_size ÷ num_attention_heads`, reported as
  `derived`. Multimodal configs nest under `text_config`; the block reads it.
- R4: `safetensors_gb` is the download. The spec's `min_disk_gb` adds the
  tensor cache; that comparison is R9's, not this row's.
