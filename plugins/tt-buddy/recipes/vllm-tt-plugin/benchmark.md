# Benchmark vllm-tt-plugin

Start the selected model with [server.md](server.md), wait for HTTP 200 from
`/health`, and run the benchmark from the `vllm-tt-plugin` repository root:

```bash
SERVER_URL="${SERVER_URL:-http://localhost:8000}"
BENCHMARK_RESULT_DIR="${BENCHMARK_RESULT_DIR:-output}"
NUM_PROMPTS="${NUM_PROMPTS:-32}"
: "${MODEL_ID:?Set MODEL_ID to the served model}"
mkdir -p "$BENCHMARK_RESULT_DIR"
"$TT_METAL_HOME/python_env/bin/vllm" bench serve \
  --base-url "$SERVER_URL" \
  --model "$MODEL_ID" \
  --dataset-name random \
  --random-input-len 128 \
  --random-output-len 128 \
  --num-prompts "$NUM_PROMPTS" \
  --ignore-eos \
  --percentile-metrics ttft,tpot,itl,e2el \
  --save-result \
  --result-dir "$BENCHMARK_RESULT_DIR" \
  --result-filename vllm-result.json
```

Run a benchmark expected to exceed two minutes as a background host task.
Require `$NUM_PROMPTS` successful requests and zero failed requests. Invoke
`tt-buddy:learn` and inspect current vLLM source for specialized benchmarks.
