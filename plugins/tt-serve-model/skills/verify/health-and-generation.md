# Health & Generation Correctness

V1–V6. Requires the server to already answer `/health`.

## Commands

```bash
EP=${ENDPOINT:-http://localhost:8000}

echo "==V1"; curl -s -o /dev/null -w '%{http_code}\n' "$EP/health"

echo "==V2"; curl -s "$EP/v1/models" -o "$SCRATCH/models.json"; E=$(python3 -c "import json,os,sys;print(json.load(open(os.path.join(sys.argv[1],'selection.json')))['entry'])" "$SCRATCH" 2>/dev/null); python3 - "$SCRATCH/models.json" \
  "${E:-/nonexistent}" "$SCRATCH/config.json" <<'PY'
import json,sys,glob
served=json.load(open(sys.argv[1]))['data'][0]
mml=served.get('max_model_len')
print("served=",served['id'],"max_model_len=",mml)
try: ceiling=json.load(open(sys.argv[2]))['device_model_spec']['max_context']
except Exception: ceiling=None
try: native=(lambda c: (c.get('text_config') or c).get('max_position_embeddings'))(json.load(open(sys.argv[3])))
except Exception: native=None
print("spec_max_context=",ceiling,"native_ctx=",native)
if mml and native:
    v = "below native (spec-imposed ceiling)" if mml<native else "equal to native" if mml==native else "above native (rope scaling)"
    print("verdict=",v)
if mml and ceiling and mml!=ceiling: print("WARNING: served != spec max_context")
PY

echo "==V3"; curl -s "$EP/v1/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"${MODEL:?}"'","prompt":"The capital city of France is","temperature":0,"max_tokens":5}'

echo "==V4"; curl -s "$EP/v1/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","prompt":"17*24=","temperature":0,"max_tokens":10}'

echo "==V5"; curl -s "$EP/v1/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","prompt":"1 2 3 4 5 6 7 8","temperature":0,"max_tokens":10}'

echo "==V6"; curl -s "$EP/v1/chat/completions" -H 'Content-Type: application/json' -d \
  '{"model":"'"$MODEL"'","temperature":0,"max_tokens":700,"enable_thinking":false,"messages":[
    {"role":"user","content":"Write a Python function fib(n) returning the first n Fibonacci numbers as a list, starting [1,1,2,3,...]. Reply with ONLY a fenced python code block, nothing else."}
  ]}' -o "$SCRATCH/v6.json"
python3 - "$SCRATCH/v6.json" <<'PY'
import ast, builtins, json, re, sys
reply = json.load(open(sys.argv[1]))['choices'][0]['message']['content']
m = re.search(r"```(?:python)?\n(.*?)```", reply, re.S)
if not m:
    print("v6_check=no fenced code block in reply"); raise SystemExit
# The reply comes from the server under test, which may be remote or faulty. It is
# parsed and inspected, never run: no exec, eval, import, or subprocess on this text.
try:
    tree = ast.parse(m.group(1))
except SyntaxError as e:
    print(f"v6_check=syntax error at line {e.lineno}: {e.msg}"); raise SystemExit
fib = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "fib"), None)
if fib is None:
    print("v6_check=no function named fib"); raise SystemExit
defects = []
if not any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(fib)):
    defects.append("fib never returns a value")
for loop in (n for n in ast.walk(tree) if isinstance(n, ast.While)):
    if isinstance(loop.test, ast.Constant) and loop.test.value and not any(
            isinstance(n, (ast.Break, ast.Return)) for n in ast.walk(loop)):
        defects.append(f"possible infinite loop at line {loop.lineno} (while True, no break or return)")
known = set(dir(builtins))
for n in ast.walk(tree):
    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)): known.add(n.id)
    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): known.add(n.name)
    elif isinstance(n, ast.arg): known.add(n.arg)
    elif isinstance(n, (ast.Import, ast.ImportFrom)): known.update((a.asname or a.name).split('.')[0] for a in n.names)
    elif isinstance(n, ast.ExceptHandler) and n.name: known.add(n.name)
undefined = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known})
if undefined:
    defects.append("undefined name(s): " + ", ".join(undefined))
print("v6_check=" + ("; ".join(defects) if defects else "consistent (static check only, not executed)"))
PY
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| V1 | Is the server up? | HTTP status of `GET /health` | `200` or `503: <error field from body>` |
| V2 | What's served, at what context? | `GET /v1/models` → `id`, `max_model_len`, compared against the spec's `max_context` and the config's `max_position_embeddings` | `<model>, max_model_len <n>: <below native (spec-imposed ceiling) \| equal to native \| above native (rope scaling)>` |
| V3 | Raw gen, no chat template? | completion text | starts with `" Paris."` or flag mismatch |
| V4 | Arithmetic at temp 0? | completion text + `finish_reason` | `408` and `finish_reason: stop`, else flag wrong math |
| V5 | Sequential state held? | completion text | continues `9, 10, 11` — flag any token duplication (e.g. `" 9 9 10 10 1"`) |
| V6 | Generated code consistent? | request a short function via `/v1/chat/completions`, parse the reply (never run it) | `consistent (static check only, not executed)` or lists the defect: syntax error, no `fib`, no return, undefined name, possible infinite loop |

## Rules

- V3–V5 MUST run at `temperature: 0`. A failure at any other temperature is
  not evidence of a bug — see `debug-and-provenance.md` V19.
- V3 deliberately avoids `/v1/chat/completions` — the point is to bypass the
  chat template entirely.
- **V2 is a comparison, never an assertion.** An earlier draft of this row
  stated flatly that `max_model_len` "is the spec-imposed ceiling, not the
  model's native context". On 2026-09-22 that was false: served 262144 =
  `max_position_embeddings`, i.e. exactly native, and *not* the 49152
  hybrid-off ceiling the golden dataset describes. Report which of the three
  outcomes holds; never pre-declare one.
- V2: `max_model_len` differing from the spec's `max_context` is itself a
  finding — the server is not running what the spec says it should.
- V4/V5 are **fixed-by-image, not universal failures.** The golden dataset
  records `10 + 20 = 13` (V4) and `" 9 9 10 10 1"` (V5) as observed defects.
  Neither reproduces on image 0.20.0 — both answered correctly. Treat the
  recorded failures as a property of the older image, and always state the
  image tag with the result. A pass here does not retire the row; it dates it.

- **V6 needs the reasoning branch, exactly as V12 does.** Asked for a short
  function with `max_tokens: 700`, image 0.20.0 returns `content: ''` with
  `finish_reason: length` — all 700 tokens spent inside `<think>`, nothing
  emitted. The row looks like a generation failure and is not one. Send it
  with `enable_thinking:false` (or a much larger budget); with thinking off
  the same prompt returned correct, consistent code. This defect was **not**
  in the original review — it was found by running the row.
- V6: parse the reply, don't eyeball it, and **never run it**: not with `exec()`,
  `eval()`, or a subprocess. A served model's output is untrusted input, and the
  endpoint can be remote (`ENDPOINT`). A filter on the code's text cannot make
  running it safe: an `import` of a harmless-looking module, or a function that
  returns an expression string, gets past one. Running generated code needs a real
  sandbox (no host files, credentials, or network, with resource limits), and this
  plugin ships none. So V6 checks the code statically and says "not executed". It
  cannot confirm that `fib(10)` returns the right numbers. If that matters, run the
  code yourself in your own sandbox.

## Status

All rows re-run 2026-09-22 against run 2 (`/health` at 18:55:56Z + 4 m 37 s).
Raw output: `run-2026-09-22/artifacts/run2-v1-v5.out`, `run2-v6.out`.

| Row | State |
|---|---|
| V1 | **TESTED** 2026-09-22 / p300x2 / 0.20.0 — `/health` → 200, twice, on two separate launches |
| V2 | **TESTED** — served 262144, spec `max_context` 262144, native 262144 → verdict `equal to native`. The three-outcome comparison works end to end |
| V3 | **TESTED** — `' Paris.\nA.'` at temp 0 |
| V4 | **TESTED** — `17*24=` → `'408'`, `finish_reason: stop` |
| V5 | **TESTED** — `1 2 3 4 5 6 7 8` → `' 9 10 11 1'`, no duplication |
| V6 | **PARTLY TESTED.** The request and the empty-content defect were found on a live server: at 700 tokens the reply is empty with `finish_reason: length`, and with `enable_thinking:false` it returned correct `fib` (then run, `f(1..10) = [1,1,2,3,5,8,13,21,34,55]`). The code is no longer run; the current static check is tested on synthetic replies only (valid, syntax error, no `fib`, undefined name, infinite loop, no return) and not yet on a live server |
