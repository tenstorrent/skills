# Install vllm-tt-plugin

Run every command from the `vllm-tt-plugin` repository root. Build tt-metal,
activate the tt-metal `python_env`, and install `uv` first.

## Install vLLM and vllm-tt-plugin

Run the install as a background host task when the vLLM source build can exceed
two minutes.

```bash
: "${TT_METAL_HOME:?Set TT_METAL_HOME first}"
source "$TT_METAL_HOME/python_env/bin/activate"
source docs/install-vllm-tt.sh
```

## Refresh vllm-tt-plugin only

```bash
uv pip install --python "$TT_METAL_HOME/python_env/bin/python" -e .
```

## Verify

MUST run after every install or refresh. A failed import means the install failed.

```bash
"$TT_METAL_HOME/python_env/bin/python" -c \
  'import ttnn, vllm, vllm_tt_plugin; print(vllm.__version__, vllm_tt_plugin.__file__)'
```
