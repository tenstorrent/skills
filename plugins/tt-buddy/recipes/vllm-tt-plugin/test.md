# Test vllm-tt-plugin

Run from the `vllm-tt-plugin` repository root after completing [build.md](build.md).

## Host-only suite

```bash
uv pip install --python "$TT_METAL_HOME/python_env/bin/python" -e ".[dev]"
PYTHONPATH=ci/host-stubs \
  "$TT_METAL_HOME/python_env/bin/python" -m pytest tests/ --ignore=tests/tt
"$TT_METAL_HOME/python_env/bin/pre-commit" run --all-files
```

## Server-facing suite

Start the selected model with [server.md](server.md), wait for HTTP 200 from
`/health`, and set the matching values:

```bash
SERVER_URL="${SERVER_URL:-http://localhost:8000}"
: "${MODEL_ID:?Set MODEL_ID to the served model}"
"$TT_METAL_HOME/python_env/bin/python" -m pytest tests/tt -v \
  --tt-server-url "$SERVER_URL" \
  --tt-model-name "$MODEL_ID"
```

Read `tests/tt/conftest.py` for current optional server-test arguments. Report
the expected duration and zero-failure success signal before submission. Run a
server-facing suite expected to exceed two minutes as a background host task.
