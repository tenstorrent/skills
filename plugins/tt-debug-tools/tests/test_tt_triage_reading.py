# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-triage: can an agent read a real triage report off a real device.

Every test here skips until its fixture is captured on hardware. See
fixtures/README.md.
"""

import pytest

pytestmark = pytest.mark.agent

PROMPT = (
    "Below is the output of a tt-triage run captured from a Tenstorrent device. "
    "Read it and report what it shows. Quote the lines you relied on verbatim in "
    "quoted_evidence — do not paraphrase them.\n\n"
    "----- begin tt-triage output -----\n{output}\n----- end tt-triage output -----"
)


def _check(res, expected):
    """Compare a reading against the hand-authored ground truth.

    Only the keys the fixture actually pins are compared, so a scenario asserts
    what it knows and stays silent on the rest.
    """
    for key in ("fault_found", "primary_signal", "verdict", "evidence_strength"):
        if key in expected:
            assert res.answer[key] == expected[key], (
                f"{key}: expected {expected[key]!r}, got {res.answer[key]!r}"
            )
    if "location_contains" in expected:
        for token in expected["location_contains"]:
            assert token in res.answer["location"], (
                f"location {res.answer['location']!r} missing {token!r}"
            )
    # Verbatim quoting is the guard against a confident reading of nothing: the
    # evidence has to exist in the captured output.
    for quote in res.answer["quoted_evidence"]:
        assert quote.strip(), "empty quote in quoted_evidence"


def test_reads_a_halted_core(agent, load_fixture):
    """A bad NoC write halts a core. The report should name where it stopped."""
    output, expected = load_fixture("tt-triage", "halted-core")
    res = agent(PROMPT.format(output=output))

    assert res.skill == "tt-triage"
    _check(res, expected)
    for quote in res.answer["quoted_evidence"]:
        assert quote in output, f"quoted a line that is not in the output: {quote!r}"


def test_does_not_invent_a_fault_on_a_clean_run(agent, load_fixture):
    """The negative control. A skill that reports a fault for every input passes
    every positive fixture, so a clean report has to read as clean."""
    output, expected = load_fixture("tt-triage", "healthy-run")
    res = agent(PROMPT.format(output=output))

    assert res.skill == "tt-triage"
    assert res.answer["fault_found"] == "no", res.answer["location"]
    _check(res, expected)
