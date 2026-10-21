# Serving plugin changes

The TT vLLM plugin serves every TT model. A plugin change affects models that this bringup does not test, and every new plugin interface must be maintained for all models. Change the plugin only when the plugin owns the required behavior. A smaller diff, a faster stage, or avoiding rework in an earlier stage is not a reason to change the plugin.

These rules apply to the pinned `tenstorrent/vllm-tt-plugin` checkout and to the upstream vLLM checkout used for serving. Before you change either checkout, also read and follow its own `AGENTS.md` or contribution rules.

## Decide who owns the behavior

The model owns a gap in the model: a missing sampling feature, cache or position behavior, shape restriction, trace defect, or any capability that an earlier stage did not implement. Fix the model or generator. `$vllm-integration` permits small, evidence-backed model and generator changes for a real contract gap; a larger gap goes back to the stage that owns it.

The plugin owns the behavior only in one of these cases:

1. **Plugin defect.** The pinned plugin violates its own documented model interface or capability contract, or upstream vLLM semantics, and a model that meets the documented contract can trigger the failure.
2. **Missing shared behavior.** The model needs a serving behavior that the pinned plugin interface cannot express, and the need is a real support boundary (for example a hardware limit) rather than a feature that the model could implement. Other models must be able to use the same behavior.

## Changes that need human authorization

These changes add shared interface:

- a new `model_capabilities` key, or a new meaning for an existing key;
- a new environment variable that the plugin reads;
- a new `--additional-config` option, including keys under `tt`;
- a new hook, marker attribute, cache spec, or method that the plugin looks up on the model class;
- a change of plugin default behavior for models that do not opt in.

Make such a change only after a human authorizes that exact item. Authorization comes from the human operator: the goal prompt, the acceptance contract, or an operator message that names the item. A general request to bring up the model, make serving work, or pass a gate is not authorization. Unattended runs normally have no authorization. In that case, write the justification record below with status `proposed`, set the goal to `blocked` with the record path as the concrete blocker, and do not implement the change.

## Changes that are not allowed

- Model-name or architecture-specific branches in shared plugin code.
- A built-in plugin registration for an autoport adapter when a runtime registration mechanism of the pinned plugin can select the adapter.
- A change that hides a model gap: routing requests away from a missing model feature, exempting the model from a test, or weakening a plugin test.
- A workaround copied from an older plugin or fork without a current reproducer against the pinned plugin.

## Justification record

Before you change the plugin, or when you propose a change, add an entry to `models/autoports/<model>/doc/vllm_integration/plugin_changes.md`. Each entry contains:

1. **Requirement and failure.** The required serving behavior, the observed failure, and a minimal reproducer.
2. **Owner.** The case from "Decide who owns the behavior" that applies. For a plugin defect, cite the plugin document, source location, or vLLM behavior that the plugin violates.
3. **Model-side alternative.** The model or generator change that would close the gap, and why that change is incorrect. "The model change is larger" and "the model change belongs to an earlier stage" are not reasons.
4. **Existing mechanisms.** The `model_capabilities` keys, `--additional-config` options, environment variables, and registration mechanisms in the pinned plugin documents that you checked, and why each one does not apply.
5. **Effect on other models.** The behavior of models that do not use the change, and the existing models that take the changed path.
6. **Authorization.** For a change that adds shared interface: the person, the source, and the exact item, or `pending` and the exact item for a `proposed` record. Otherwise state that the change adds no shared interface.
7. **Validation.** For `implemented`: a plugin regression test that fails before the change and passes after it, the plugin's own host test results, and the affected serving checks. For `proposed`: the planned regression test, built from the reproducer in item 1, and the checks to run after implementation.
8. **Status.** `implemented` with the local plugin commit SHA, or `proposed`.

## Make the change

Keep the change minimal and model-independent. Add the regression test in the plugin checkout. Run the plugin's host test suite as its documentation describes, then rerun the affected serving checks. Commit the change locally in the plugin checkout as its own stage-owned commit and never push it. Record the pinned base commit and the local commit in the run record and the stage work log.

A record is complete when it contains every item above in the form that its status requires. An `implemented` record without test results is incomplete stage evidence. A `proposed` record does not need test results, because the change does not exist yet.
