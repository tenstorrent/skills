# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

import pytest
import torch

from readiness_check.run_prefill_resume_check import (
    DEFAULT_THRESHOLDS,
    compare_logits,
    resume_points,
    run_case,
    summarize,
    window_positions,
)

VOCAB = 50


def _position_logits(history, position, *, noise=0.0, seed=0):
    """Peak at a hash of the whole history up to `position`, so history matters."""
    rng = torch.Generator().manual_seed(seed * 100_003 + position)
    logits = torch.randn(VOCAB, generator=rng) * 0.1
    logits[sum(history[: position + 1]) % VOCAB] = 5.0
    if noise:
        logits = logits + noise * torch.randn(VOCAB, generator=rng)
    return logits


class _ContinuationGenerator:
    """Correct resume: logits depend only on the absolute position and history."""

    noise = 0.0

    def __init__(self):
        self.reset_calls = 0
        self.tokenizer = list(range(VOCAB))

    def reset(self):
        self.reset_calls += 1

    def prefill_logits(self, prompt_token_ids):
        raise AssertionError("the resume check must not request all-position logits")

    def prefill_logits_continuation(self, prompt_token_ids, start, *, positions=None, prefill_prefix=True):
        if positions is None:
            positions = range(start, len(prompt_token_ids))
        if not prefill_prefix:
            fake_history = [0] * start + list(prompt_token_ids[start:])
            return torch.stack([_position_logits(fake_history, p, seed=7) for p in positions])[None]
        return torch.stack([_position_logits(prompt_token_ids, p, noise=self.noise) for p in positions])[None]


class _IgnoresStartGenerator(_ContinuationGenerator):
    """Broken resume: the continuation is computed as a fresh request at position 0."""

    def prefill_logits_continuation(self, prompt_token_ids, start, *, positions=None, prefill_prefix=True):
        if start == 0 or not prefill_prefix:
            return super().prefill_logits_continuation(prompt_token_ids, start, positions=positions, prefill_prefix=prefill_prefix)
        if positions is None:
            positions = range(start, len(prompt_token_ids))
        suffix = list(prompt_token_ids[start:])
        return torch.stack([_position_logits(suffix, p - start, seed=3) for p in positions])[None]


class _ControlCannotFailGenerator(_ContinuationGenerator):
    """A generator whose no_history control still matches: the check must flag it."""

    def prefill_logits_continuation(self, prompt_token_ids, start, *, positions=None, prefill_prefix=True):
        return super().prefill_logits_continuation(prompt_token_ids, start, positions=positions, prefill_prefix=True)


def _tokens(length, seed=1):
    rng = torch.Generator().manual_seed(seed)
    return torch.randint(1, VOCAB - 1, (length,), generator=rng).tolist()


def test_resume_points_cover_block_lcm_window_and_prefix_cache_geometry():
    points = {p["kind"]: p["start"] for p in resume_points(2560, block_sizes=[64, 128], sliding_window=1024)}
    assert points["block_aligned"] == 64
    assert points["lcm_aligned"] == 128
    assert points["tile_unaligned"] == 260
    assert points["window_start"] == 1024
    assert points["window_plus_unaligned"] == 1029
    assert points["prefix_cache_hit"] == 2432
    assert points["last_token"] == 2559
    assert [p["start"] for p in resume_points(2560, block_sizes=[64, 128], sliding_window=1024)] == sorted(points.values())


def test_resume_points_drop_starts_outside_the_prompt():
    starts = [p["start"] for p in resume_points(100, block_sizes=[64, 128], sliding_window=1024)]
    assert starts == [64, 99]


def test_window_positions_include_final_position():
    assert window_positions(10, 20, 4) == [10, 11, 12, 13, 19]
    assert window_positions(18, 20, 4) == [18, 19]


def test_compare_logits_reports_exact_agreement_for_identical_inputs():
    logits = torch.randn(1, 6, VOCAB)
    stats = compare_logits(logits, logits.clone())
    assert stats["positions"] == 6
    assert stats["pcc_mean"] == pytest.approx(1.0)
    assert stats["top1_agreement"] == 1.0
    assert stats["last_top1_equal"]


def test_compare_logits_rejects_shape_mismatch():
    with pytest.raises(RuntimeError, match="logit shapes differ"):
        compare_logits(torch.zeros(1, 4, VOCAB), torch.zeros(1, 5, VOCAB))


def test_correct_resume_passes_and_controls_fail():
    generator = _ContinuationGenerator()
    case = run_case(
        generator=generator,
        tokens=_tokens(400),
        start=128,
        kind="lcm_aligned",
        window=256,
        thresholds=DEFAULT_THRESHOLDS,
        controls=("no_history", "fresh_suffix"),
    )
    assert case["pass"], case["reasons"]
    assert case["controls"]["no_history"]["failed_as_expected"]
    assert case["controls"]["fresh_suffix"]["failed_as_expected"]
    assert generator.reset_calls == 4
    assert summarize([case]) == ("pass", [])


def test_resume_that_ignores_start_fails_the_gate():
    case = run_case(
        generator=_IgnoresStartGenerator(),
        tokens=_tokens(400),
        start=128,
        kind="lcm_aligned",
        window=256,
        thresholds=DEFAULT_THRESHOLDS,
        controls=("fresh_suffix",),
    )
    assert not case["pass"]
    assert any("top1_agreement" in r for r in case["reasons"])
    status, reasons = summarize([case])
    assert status == "fail"
    assert reasons


def test_control_that_cannot_fail_is_reported_as_a_check_defect():
    case = run_case(
        generator=_ControlCannotFailGenerator(),
        tokens=_tokens(400),
        start=128,
        kind="lcm_aligned",
        window=256,
        thresholds=DEFAULT_THRESHOLDS,
        controls=("no_history",),
    )
    assert case["pass"]
    assert not case["controls"]["no_history"]["failed_as_expected"]
    status, reasons = summarize([case])
    assert status == "fail"
    assert "passed the gates" in reasons[0]


class _WordTokenizer:
    def encode(self, text):
        return [hash(w) % 1000 for w in text.split()]


def test_natural_tokens_are_deterministic_and_exact_length(capsys):
    from readiness_check.run_prefill_resume_check import natural_tokens

    text = "alpha beta gamma delta epsilon zeta eta theta"
    first = natural_tokens(_WordTokenizer(), text, 5)
    assert first == natural_tokens(_WordTokenizer(), text, 5)
    assert first == [hash(w) % 1000 for w in ["alpha", "beta", "gamma", "delta", "epsilon"]]
    repeated = natural_tokens(_WordTokenizer(), "one two", 5)
    assert len(repeated) == 5
    assert "repeated" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="no text"):
        natural_tokens(_WordTokenizer(), "  ", 3)


def test_load_natural_text_prefers_explicit_file_then_book_then_prompts(tmp_path, monkeypatch):
    import bz2

    from readiness_check.run_prefill_resume_check import BOOK_RELPATH, load_natural_text

    prompts = tmp_path / "prompts.txt"
    prompts.write_text("prompt text")
    monkeypatch.chdir(tmp_path)
    assert load_natural_text(None, prompts) == "prompt text"
    book = tmp_path / BOOK_RELPATH
    book.parent.mkdir(parents=True)
    with bz2.open(book, "wt", encoding="utf-8") as handle:
        handle.write("book text")
    assert load_natural_text(None, prompts) == "book text"
    explicit = tmp_path / "corpus.txt"
    explicit.write_text("explicit text")
    assert load_natural_text(explicit, prompts) == "explicit text"
    with pytest.raises(RuntimeError, match="no natural text"):
        load_natural_text(tmp_path / "missing.txt", tmp_path / "also-missing.txt")


def test_controls_are_skipped_for_narrow_windows():
    case = run_case(
        generator=_ContinuationGenerator(),
        tokens=_tokens(400),
        start=399,
        kind="last_token",
        window=256,
        thresholds=DEFAULT_THRESHOLDS,
        controls=("no_history", "fresh_suffix"),
    )
    assert case["pass"]
    assert case["controls"] == {}
