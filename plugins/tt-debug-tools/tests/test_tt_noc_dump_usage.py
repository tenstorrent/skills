# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-noc-dump: does an agent enable NoC tracing correctly and read its output."""

import pytest

pytestmark = pytest.mark.agent

# The three tools that cannot share a run with the NoC debug dump — they compete
# for the same kernel binary budget. Setting one is a real misconfiguration.
EXCLUSIVE = {"TT_METAL_WATCHER", "TT_METAL_DPRINT_CORES", "TT_METAL_DEVICE_PROFILER"}


def test_missing_barrier_suspected(agent):
    res = agent(
        "My TTNN data-movement kernel multicasts a write and then increments a "
        "semaphore. The output tensor is corrupt. I think I forgot a barrier. "
        "How do I get the tooling to confirm that, and what will it print?"
    )

    assert res.skill == "tt-noc-dump"
    assert "TT_METAL_NOC_DEBUG_DUMP" in res.env_keys()
    assert not (res.env_keys() & EXCLUSIVE), f"enabled a conflicting tool: {res.env_keys()}"
    # The conflict is load-bearing, so the skill has to surface it rather than
    # merely avoid setting it.
    assert res.must_disable() & EXCLUSIVE, res.answer["must_disable"]


def test_reports_the_upstream_fixture(agent):
    res = agent(
        "Run the upstream NoC debug dump unit test on a Tenstorrent device and "
        "tell me the exact command and the shape of the summary it prints."
    )

    assert res.skill == "tt-noc-dump"
    assert "unit_tests_noc_debugging" in res.answer["command"]
    assert "NOCDebuggingFixture.McastOnlyWriteFlush" in res.answer["command"]
    assert "NOC Debug Summary" in res.literals_text()


def test_does_not_invent_a_log_file(agent):
    """The question's premise is wrong: this tool prints to the console. An agent
    that answers with a plausible-looking log path has failed the only thing
    asked of it."""
    res = agent(
        "I enabled the NoC debug dump on a Tenstorrent run. Which log file do I "
        "open to read its findings?"
    )

    assert res.skill == "tt-noc-dump"
    assert res.answer["output_destination"] == "console"
    assert res.answer["output_paths"] == [], res.answer["output_paths"]


def test_a_clean_run_is_weak_evidence(agent):
    """The NoC is non-deterministic, so an acknowledgement can return before the
    trace notices a missing barrier. A clean summary must not be reported as
    proof of correct barrier discipline."""
    res = agent(
        "The NoC debug dump summary on my Tenstorrent kernel is empty. Does that "
        "prove my barrier discipline is correct?"
    )

    assert res.skill == "tt-noc-dump"
    assert res.answer["verdict"] == "no"
    assert res.answer["evidence_strength"] == "weak"
