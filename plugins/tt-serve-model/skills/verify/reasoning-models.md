# Reasoning-Model Checks

V13–V18. Only run when the served model is a declared reasoning model
(`reasoning_parser_name` set in its Model Retrieval spec entry). Otherwise
mark all six `n/a: not a reasoning model`.

## Commands

```bash
echo "==V13"; curl -s "$EP/v1/chat/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"What is 2+2?"}],"temperature":0,"max_tokens":700}' \
  | python3 -c "
import json,sys
d=json.load(sys.stdin); c=d['choices'][0]; m=c['message']
print('finish:',c['finish_reason'],'| usage:',d['usage'])
print('reasoning_content:',repr(m.get('reasoning_content'))[:200])
print('content:',repr(m.get('content'))[:200])"

echo "==V14a  engine config — authoritative"; docker logs "$CID" 2>&1 \
  | grep -oE "reasoning_parser='[^']*'|reasoning_parser=[A-Za-z0-9_.]+" | tail -2
echo "==V14b  entrypoint wiring — secondary"; docker exec "$CID" sh -c \
  'grep -n "reasoning" $(find / -maxdepth 4 -iname run_vllm_api_server.py 2>/dev/null | head -1)' 2>&1 | head -5

echo "==V16"; curl -s "$EP/v1/chat/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"What is 2+2?"}],"temperature":0,"max_tokens":700,"chat_template_kwargs":{"enable_thinking":false}}' \
  | python3 -c "import json,sys; print('completion_tokens:',json.load(sys.stdin)['usage']['completion_tokens'])"

echo "==V17"; for B in 40 600; do curl -s "$EP/v1/chat/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"What is 2+2?"}],"temperature":0,"max_tokens":'"$B"'}' \
  | python3 -c "
import json,sys
d=json.load(sys.stdin); c=d['choices'][0]
print('budget=$B finish=',c['finish_reason'],'completion_tokens=',d['usage']['completion_tokens'],
      'content_empty=',not (c['message'].get('content') or '').strip())"; done
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| V13 | `reasoning_content` populated? | response JSON's `reasoning_content` vs `content` vs `usage` | one of three: `populated` / `null — thinking prose leaking into content` / `null — think block generated then dropped` |
| V14 | Parser metadata takes effect? | the engine config line first, the entrypoint grep second | one of three: `active` / `declared but not wired` / `wired and still null` |
| V15 | Undocumented client flags required? | per-model quirks table (below) | named flag **for this model**, or `none known for <model> — see V22` |
| V16 | Direct answers possible? | completion-token count with `enable_thinking:false` vs without | large drop confirms it works |
| V17 | Reasoning token budget | `finish_reason` + `completion_tokens` + whether `content` came back empty, at two budgets | `<n> tokens reached stop` or `budget too small: content empty with finish_reason length at <n>` |
| V18 | Tool-call routing validatable? | gate on V13–V17 all passing | `blocked: fix generation first (see V13/V14)` or `proceed` |

## Known per-model quirks (V15)

| Model | Image | Flag | Why |
|---|---|---|---|
| gemma-4 | — | `"skip_special_tokens": false` | Without it, raw scratch notes land in `content`. Undocumented. |
| Qwen3.6-27B | 0.20.0 | none | Checked 2026-09-22: `skip_special_tokens:false` returns `'\n\nok'`, no raw scratch leak. The gemma-4 requirement does **not** generalise. |

The table is keyed by model **and image** on purpose. A quirk is a property of
one implementation at one release, not of reasoning models in general.

*(Extend this table as new quirks are found — this is exactly the kind of
fact community-feedback triage should be feeding into golden datasets.)*

## Rules

- NEVER conclude "model doesn't support reasoning" from V13 alone — always
  run V14 first.
- **V13 has three outcomes, not two.** The draft listed only `populated` and
  `null — leaking into content`. Image 0.20.0 does neither: the think block is
  generated, stripped from `content` and discarded. The tell is the token
  accounting — 149 completion tokens for a 12-token visible answer, and at
  `max_tokens=40` all 40 are spent with `finish_reason: length`,
  `content=''` and `reasoning_content=None`. Nothing leaks and nothing is
  returned. Report it as `null — think block generated then dropped`.
- **V14 needs the engine config line, not only the entrypoint grep.** On
  0.20.0 the config prints `reasoning_parser='qwen3'` — the parser *is*
  wired — and `reasoning_content` is still null. The old binary verdict
  (`active` / `declared but not wired`) cannot express that; the third
  verdict `wired and still null` is the one that fits, and it points at the
  parser implementation rather than at the launch command.
- V17: the number alone is not the answer. **The diagnostic for "budget too
  small" is `content` empty together with `finish_reason: length`** — that
  pair means the whole budget went inside `<think>`. 600 tokens held as a
  floor on 0.20.0; raise and re-test rather than declaring a hard minimum.
- V18 is a hard gate: do not attempt to validate tool-call parsing against
  malformed generation — a failure there would misattribute a generation bug
  as a tool-calling bug.

## Status

All rows re-run 2026-09-22 against run 2. Raw output:
`run-2026-09-22/artifacts/run2-v12-v17.out`.

| Row | State |
|---|---|
| V13 | **TESTED** — `reasoning_content: None`, content clean (`'\n\n2 + 2 equals 4.'`), 145 completion tokens for that answer → the third outcome, `null — think block generated then dropped` |
| V14 | **TESTED** — `reasoning_parser='qwen3'` present in the engine config while `reasoning_content` is null → the third verdict, `wired and still null`. The binary verdict could not have expressed this |
| V15 | **TESTED** for this model — `skip_special_tokens:false` → `'\n\nok'`, no leak |
| V16 | **TESTED** — 145 → **8** completion tokens with `enable_thinking:false` |
| V17 | **TESTED, and the diagnostic is the load-bearing part.** Budget 40 → `finish_reason: length`, 40 tokens, **content empty**. Budget 600 → `stop`, 145 tokens, content present. The empty-content-plus-length pair is exactly the signal the row now names |
| V18 | **UNTESTED** — the gate requires a V13–V17 failure and there was none |

V17's diagnostic then caught a defect in a row nobody had flagged: V6
(`health-and-generation.md`) returns empty content with `finish_reason:
length` at 700 tokens for a code-generation prompt. Same trap, different
stage — which is the argument for putting the diagnostic in the dataset
rather than in one row's prose.
