---
name: qualitative-check
description: Run and review qualitative or prompt-based evaluation checks for Hugging Face text models during TTNN bringup. Use whenever a stage generates text, judges output quality, runs vLLM/TTI/API qualitative smokes, compares HF and TT outputs, investigates wrong-language or base-model autocomplete behavior, or reviews whether prompt format and shifted-left qualitative evidence are valid.
---

# Qualitative Check

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


## Purpose

Make model text-quality checks comparable and prompt-correct. Use this skill for any qualitative generation, prompt-based eval, API smoke, vLLM qualitative run, TTI release text check, or stage review that relies on generated text.

## Prompt Format

Use the prompt format declared by the Hugging Face checkpoint.

1. Load the tokenizer/config for the exact HF model id or local checkpoint used by the stage.
2. If the tokenizer has a non-empty `chat_template` or supports `apply_chat_template` with that template, treat the model as chat/instruct:
   - render message prompts with `tokenizer.apply_chat_template(..., tokenize=False, add_generation_prompt=True)`, or send equivalent messages through `/v1/chat/completions`;
   - keep system/user/assistant roles exactly as the prompt suite defines them;
   - do not judge the model from raw `/v1/completions` prompts alone.
3. If the tokenizer has no chat template, treat the model as base/completion:
   - use plain continuation prompts;
   - do not invent a chat wrapper or judge poor chat-style outputs as model failure.
4. Record the decision in the evidence: HF model/revision, tokenizer class, whether `chat_template` was present, prompt mode (`chat` or `completion`), endpoint or rendering method, generation parameters, and prompt source path.

Raw completion output from a chat/instruct model is allowed only as labeled continuation stress coverage. It is not a pass/fail quality verdict unless a correctly formatted run is also present.

## Controls

Every quality verdict needs a control rendered with the same prompt format:

- Prefer HF reference generation from the same model id/revision and tokenizer.
- For serving regressions, also compare against the most recent full-model or previous-stage TT output on the same prompt suite.
- If HF fails the same prompt in the same way, record that as a model/control behavior, not a TT serving bug.
- If TT output is materially worse than the HF or previous-stage control, treat it as stage work: token feedback, cache/position handling, sampling, trace replay, dtype/fidelity, or adapter state are common causes.

## Test Predicates and Completion Budgets

When a prompt-based test fails, read the request builder, returned output field and literal assertion before interpreting the failure message. In the existing evidence note, separate: what the original test returned; what the output says about task quality or completion; and what a matched TT/HF comparison establishes. Do not substitute one for another.

A matched comparison uses the same checkpoint and tokenizer, complete input IDs, generation and stop settings, and output field; record material runtime or precision differences. An HF failure on a changed prompt can challenge a test assumption, but cannot establish TT fidelity on the original prompt. For sampled generation, equal seeds across different samplers do not establish identical draws; use the agreed statistical or numerical comparison.

Measure the allowance needed for the behavior the test actually checks. For a test that checks a prefix or substring, score that predicate on saved prefixes; EOS and a final channel are required only if the test or an explicit product contract requires them. A captured prefix can establish its own predicate result even when later generation was censored. It does not establish natural completion, final-answer quality or a suite-wide safe allowance.

For completed-answer checks, replay the exact input IDs, template, reasoning effort and generation settings on the native HF reference with a generous bounded cap and normal stopping. Record completion and guard outcomes, actual final output and sampled coverage; count reasoning only where actual framing supports it. Choose any proposed allowance from the relevant measured requirement with an explained margin. Keep failed and censored evidence. Do not enlarge budgets, change inputs or reinterpret the output until something passes.

A matched reference comparison may be useful whether HF passes or fails the task. Do not require HF to pass before comparing TT with it. Preserve the original oracle and report a proposed test change separately; a different prompt, output field or scoring rule is a different experiment.

## Artifacts

Leave small, inspectable artifacts under the stage evidence directory:

- prompt-format metadata, for example `qualitative_prompt_format.json`;
- rendered prompts or prompt token ids for each prompt id;
- HF control outputs;
- TT/full-model/vLLM/TTI outputs;
- degenerate-output check result when available;
- a short verdict that cites concrete prompt ids and output snippets, not only a summary.

Do not store secrets, auth files, model weights, or bulky tensor/profiler dumps as qualitative evidence.

## Verdict Rules

More work is required if:

- an instruct/chat model is judged only from raw completion prompts;
- a base model is judged through invented chat prompts;
- the prompt-format decision is missing or contradicted by artifacts;
- a stage from full-model onward skips the shared qualitative suite without a concrete capability blocker;
- generated text shows wrong language, prompt echo, mechanical repetition, doubled subwords, control-token leakage, cross-request leakage, repeated or corrupt first token, or gibberish and no matching HF/control behavior explains it;
- a runner, API endpoint, or eval harness cannot send the correct prompt format and the stage uses its output anyway.
