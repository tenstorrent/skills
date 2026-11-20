# Bench v2 statistics tests (benchv2.py: repeat runs, repeat grades, intervals, paired verdicts)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file and from bench_cases.py.
# Add new bench v2 tests here.
from __future__ import annotations


def test_wilson_interval_widens_with_small_n_and_covers_the_rate():
    from ttp import benchv2
    lo, hi = benchv2.wilson(8, 10)
    assert round(lo, 2) == 0.49 and round(hi, 2) == 0.94, "the textbook interval for 8/10"
    lo0, hi0 = benchv2.wilson(0, 0)
    assert lo0 == 0.0 and hi0 == 1.0, "an empty set is (0, 1)"
    assert benchv2.wilson(0, 4)[0] == 0.0 and benchv2.wilson(4, 4)[1] == 1.0


def test_paired_difference_and_the_beats_verdict_need_the_interval_to_exclude_zero():
    from ttp import benchv2
    a = [{"task": i, "landed": True} for i in range(5)]
    b = [{"task": i, "landed": False} for i in range(5)]
    d = benchv2.paired(a, b)
    assert d["n"] == 5 and d["a_rate"] == 1.0 and d["b_rate"] == 0.0
    assert "beats" not in d, "the verdict is named by the caller, never baked into the numbers"
    assert benchv2.verdict(d, "A", "B") == "A beats B (n=5, interval +0.3855..+1.6145)"
    mixed_a = [{"task": i, "landed": l} for i, l in enumerate([1, 1, 0, 1, 0])]
    mixed_b = [{"task": i, "landed": l} for i, l in enumerate([1, 0, 0, 1, 1])]
    d2 = benchv2.paired(mixed_a, mixed_b)
    assert "cannot separate" in benchv2.verdict(d2, "A", "B")
    assert benchv2.verdict(None, "A", "B") == "cannot compare A and B (no shared task)"


def test_tables_marked_v1_say_the_models_cannot_be_separated():
    from ttp import benchv2
    # A v1-shaped record: one run, one grade, too few tasks to separate arms.
    rows_a = [{"task": i, "landed": l} for i, l in enumerate([1, 1, 0])]
    rows_b = [{"task": i, "landed": l} for i, l in enumerate([1, 0, 0])]
    d = benchv2.paired(rows_a, rows_b)
    line = benchv2.verdict(d, "sonnet", "opus")
    assert "cannot separate" in line, "a v1-sized sample never ranks the models"
