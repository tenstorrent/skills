# Serving stack setup

Use the standalone plugin [tenstorrent/vllm-tt-plugin](https://github.com/tenstorrent/vllm-tt-plugin)
with the upstream [vllm-project/vllm](https://github.com/vllm-project/vllm) version it recommends.
The old `tenstorrent/vllm` fork and its bundled plugin are not the default serving stack.

For a new run:

1. Resolve the plugin's current default branch from GitHub and pin the selected commit in a
   separate checkout. Read that checkout's README and the installation script it references
   (currently `docs/install-vllm-tt.sh`), including overrides and compatibility/model notes.
2. Determine the upstream vLLM release/ref and dependency combination recommended **there**.
   Do not hardcode a release in this skill, assume upstream HEAD works, parse one historical
   script format as a permanent API, or reuse another experiment's pin. If the docs/script
   disagree, inspect current upstream compatibility evidence and resolve that before install.
3. Use the plugin's current installation procedure in the source TT-Metal environment.
   Inspect the installation script before running it and stop on a failed command. Only
   make an upstream source checkout when needed, at the recommended ref, applying the
   experiment's repository isolation policy.
4. Export `VLLM_TT_PLUGIN_ROOT` to the plugin's checkout. Use its `tests/tt` suite with
   the same interpreter as the server. Verify `vllm`, `vllm_tt_plugin`, TTNN and model imports,
   installed package versions/locations, TT plugin entry points, `python -m pip check`, server
   CLI parsing and pytest collection. Ensure old fork paths are absent from `PYTHONPATH`,
   editable installs and launch wrappers. A package version alone does not prove provenance.
5. Record plugin commit, upstream vLLM version/ref (and source commit if checked out), source
   URLs, the recommendation file/commit, install commands, interpreter and resolved package
   locations. Keep that pair for the run. Later runs consult upstream afresh.

Before each local runner launch, export these values from the saved run record:

- `VLLM_EXPECTED_VERSION`: the exact vLLM package version, including any build suffix.
- `VLLM_EXPECTED_COMMIT`: the full vLLM commit SHA for a Git checkout or an install from
  a Git URL. Leave this unset for release-package installs without Git commit metadata.

The runner checks the imported plugin and vLLM locations before every local launch.
It rejects legacy fork layouts and compares vLLM's version and available commit with these
values. Keep the expected values across resumes; do not replace them with whatever the
current environment reports. Sampling also requires the plugin's source test suite.
For an external server, verify its stack in the server environment; local package checks
cannot establish what a remote process loaded.

For an explicitly requested migration, preserve the old environment, local patches and all
failed evidence first. Compare local fixes with the new sources rather than blindly applying
fork patches. Use a separate environment for compatibility checks. Update registration,
launch paths and test discovery together; rerun affected serving checks and measurements.
A dependency upgrade is not a passed gate. Do not restart completed goals or overwrite their
historical results just to migrate serving.

Register the new adapter using the pinned plugin's supported extension mechanism. Read its
current registration docs/source first. The standalone plugin currently supports
`TT_MODEL_CLASS_OVERRIDES="TT<Arch>=models.autoports.<model>.tt.generator_vllm:<Class>"`
for a per-launch selection, or `EXTRA_MODELS_DIR` bundles containing `vllm_metadata.json`
with `arch` and `main_class`. Source registrations live under
`src/vllm_tt_plugin/` in the standalone checkout. Verify the resolved registry target is the
new autoport adapter, especially when the architecture already has a built-in implementation.
Record the registration and propagate it to every engine subprocess; model implementation
still belongs in TT-Metal, not upstream vLLM core.
