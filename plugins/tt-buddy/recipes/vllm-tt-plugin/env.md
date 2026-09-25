# vllm-tt-plugin Environment

Run from the `vllm-tt-plugin` repository root. Activate the tt-metal environment:

```bash
export TT_METAL_HOME="/path/to/tt-metal"
export PYTHONPATH="$TT_METAL_HOME${PYTHONPATH:+:$PYTHONPATH}"
source "$TT_METAL_HOME/python_env/bin/activate"
```

Set the model and mesh from current tt-metal model guidance:

```bash
export MODEL_ID="<model-id-from-current-guidance>"
export MESH_DEVICE="<mesh-from-current-guidance>"
```

If `VLLM_PLUGINS` is set, include both `tt` and `tt_model_registry` in the
existing allowlist.

Use [../developer-setup.md](../developer-setup.md) for Hugging Face credentials
and cache paths. Invoke `tt-buddy:learn` for every model-specific variable, device
shape, batch limit, context limit, and TT configuration value.
