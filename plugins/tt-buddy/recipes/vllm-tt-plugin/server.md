# Run the vllm-tt-plugin Server

Run from the `vllm-tt-plugin` repository root after completing [build.md](build.md)
and [env.md](env.md).

Invoke `tt-buddy:learn` for the selected model's current launch arguments. Read
`README.md` and inspect the current entry point before composing the command:

```bash
"$TT_METAL_HOME/python_env/bin/python" examples/server_example_tt.py --help
: "${MODEL_ID:?Set MODEL_ID from current model guidance}"
: "${MESH_DEVICE:?Set MESH_DEVICE from current model guidance}"
"$TT_METAL_HOME/python_env/bin/python" examples/server_example_tt.py \
  --model "$MODEL_ID"
```

Append only arguments verified in current `vllm-tt-plugin` and tt-metal source.
Report the expected initialization time and HTTP 200 health signal before
submission. Invoke `tt-buddy:run` to submit the server command as a background device
job; do not append `&`.

## Health check

```bash
SERVER_URL="${SERVER_URL:-http://localhost:8000}"
curl -sf "$SERVER_URL/health"
```

Invoke `tt-buddy:run` with the background device job ID to stop the server.
