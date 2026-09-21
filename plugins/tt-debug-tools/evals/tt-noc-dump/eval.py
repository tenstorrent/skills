# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-noc-dump: does an agent enable NoC tracing correctly and read its output."""

from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"

# The three tools that cannot share a run with the NoC debug dump — they compete
# for the same kernel binary budget. Setting one is a real misconfiguration.
EXCLUSIVE = {"TT_METAL_WATCHER", "TT_METAL_DPRINT_CORES", "TT_METAL_DEVICE_PROFILER"}


def eval_missing_barrier_suspected(agent):
    res = agent.ask(
        "My TTNN data-movement kernel multicasts a write and then increments a "
        "semaphore. The output tensor is corrupt. I think I forgot a barrier. "
        "How do I get the tooling to confirm that, and what will it print?"
    )

    res.assert_dispatched("tt-noc-dump")
    assert "TT_METAL_NOC_DEBUG_DUMP" in res.env()
    # The three tools share on-chip SRAM budget; enabling one alongside the
    # dump silently corrupts the output. The skill teaches the conflict — this
    # asserts the agent did not stomp itself, not that it recited the trap.
    assert not (res.env().keys() & EXCLUSIVE), f"enabled a conflicting tool: {res.env()}"


def eval_reports_the_upstream_fixture(agent):
    res = agent.ask(
        "Run the upstream NoC debug dump unit test on a Tenstorrent device and "
        "tell me the exact command and the shape of the summary it prints."
    )

    res.assert_dispatched("tt-noc-dump")
    assert "unit_tests_noc_debugging" in res.answer["command"]
    assert "NOCDebuggingFixture.McastOnlyWriteFlush" in res.answer["command"]
    assert "NOC Debug Summary" in " ".join(res.answer["output_literals"])


def eval_does_not_invent_a_log_file(agent):
    """The question's premise is wrong: this tool prints to the console. An agent
    that answers with a plausible-looking log path has failed the only thing
    asked of it."""
    res = agent.ask(
        "I enabled the NoC debug dump on a Tenstorrent run. Which log file do I "
        "open to read its findings?"
    )

    res.assert_dispatched("tt-noc-dump")
    assert res.answer["output_destination"] == "console"
    assert res.answer["output_paths"] == [], res.answer["output_paths"]


def eval_a_clean_run_is_weak_evidence(agent):
    """The NoC is non-deterministic, so an acknowledgement can return before the
    trace notices a missing barrier. A clean summary must not be reported as
    proof of correct barrier discipline."""
    res = agent.ask(
        "The NoC debug dump summary on my Tenstorrent kernel is empty. Does that "
        "prove my barrier discipline is correct?"
    )

    res.assert_dispatched("tt-noc-dump")
    assert res.answer["verdict"] == "no"
    assert res.answer["evidence_strength"] == "weak"


# ---- Device execution --------------------------------------------------------

def eval_flags_the_missing_barrier(agent):
    """A provoker's kernel multicasts a write and increments a semaphore
    with no noc_async_write_barrier in between. Under
    TT_METAL_NOC_DEBUG_DUMP=1 the end-of-run summary flags the core as
    having an unflushed async write; the agent has to run the provoker
    with the flag on and report the finding.

    The provoke source is hidden by the harness — grading on the summary's
    "missing noc_async_write_barrier" wording keeps the eval honest."""
    provoke = agent.hide_provoke(PROVOKE / "live_noc_missing_barrier.py")
    res = agent.investigate(
        f"There is a Python program at {provoke} whose kernel issues a "
        f"multicast write followed by a semaphore increment on a "
        f"Tenstorrent device. TT_METAL_HOME is exported. Run the program "
        f"with the NoC debug dump feature turned on, and report what its "
        f"end-of-run summary finds. Quote the exact wording."
    )

    res.assert_invoked_tool(r"TT_METAL_NOC_DEBUG_DUMP=")
    diagnosis = res.answer["diagnosis"].lower()
    assert "missing" in diagnosis and "barrier" in diagnosis, res.answer["diagnosis"]
    assert "noc_async_write_barrier" in diagnosis, res.answer["diagnosis"]
