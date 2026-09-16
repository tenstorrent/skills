# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-triage: one eval per skill, covering knowledge and execution.

Two shapes of test live here.

**Interface knowledge** — questions about how to drive the tool: which flag,
which env var, which trap. No hardware. These grade the skill's SKILL.md
against how an agent would answer, and they catch documentation drift cheaply.

**Device execution** — real hung device in front of the agent. A fixture
launches a provoking program through the broker, waits for its DPRINT marker,
lets the agent diagnose with real tools, then kills and resets. The eval
enforces that the tool ran (`assert_invoked_tool`) and that the answer names
what only the tool would surface (`must_mention` in expected.json).

Skips gracefully on hosts without a device, without the broker CLI, or without
a tt-metal checkout. Costs money and needs an accelerator, which is why the
whole file lives under evals/ rather than tests/.
"""

from __future__ import annotations

import os
from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"


# ---- Interface knowledge -----------------------------------------------------


def eval_captures_a_live_hang(agent):
    res = agent.ask(
        "A pytest on a Tenstorrent device has been stuck for ten minutes. The "
        "process is still running. Get me the device state."
    )

    res.assert_dispatched("tt-triage")
    assert "tt-triage" in res.answer["command"]
    assert "--llm-output" in res.answer["command"]


def eval_does_not_reset_before_capturing(agent):
    """Triage reads the hung process over a live RPC. Resetting first destroys the
    evidence, and it is the tempting wrong move."""
    res = agent.ask(
        "My Tenstorrent test is hung. Should I reset the board first, with "
        "tt-smi -r, so that triage starts from a clean state?"
    )

    res.assert_dispatched("tt-triage")
    assert res.answer["verdict"] == "no"
    assert "tt-smi -r" not in res.answer["command"]


def eval_routes_around_a_device_it_cannot_open(agent):
    res = agent.ask(
        "tt-triage fails during UMD initialisation on my Tenstorrent host — "
        "another process owns the device. Can I still get a report?"
    )

    res.assert_dispatched("tt-triage")
    assert res.answer["verdict"] == "yes"
    assert "--remote-exalens" in res.answer["command"]
    # The server runs in a second shell, so it is a prerequisite of the command
    # rather than part of it.
    prereqs = " ".join(res.answer["prereqs"]).lower()
    assert "exalens" in prereqs and "server" in prereqs, res.answer["prereqs"]


def eval_rejects_the_idle_op_premise(agent):
    """Op-level scripts report the dispatcher, not the cores: an op reads idle
    once its GO was sent, even while a kernel is still stuck. Agreeing that the
    device is therefore finished is the failure."""
    res = agent.ask(
        "My tt-triage report says the op level is idle, but the test never "
        "returned. Does that mean the device finished and the hang is on the host?"
    )

    res.assert_dispatched("tt-triage")
    assert res.answer["verdict"] == "no"
    assert "callstack" in res.answer["primary_signal"].lower(), res.answer["primary_signal"]


def eval_treats_corruption_as_a_trust_gate(agent):
    """A corrupted mailbox invalidates every script that reads mailboxes,
    callstacks included, so the parked frame is not trustworthy evidence."""
    res = agent.ask(
        "My tt-triage report has check_core_magic reporting a likely corrupted "
        "mailbox, and dump_callstacks shows NCRISC parked in cb_wait_front. "
        "Which one do I chase first?"
    )

    res.assert_dispatched("tt-triage")
    assert res.answer["primary_signal"] == "check_core_magic", res.answer["primary_signal"]
    # The callstack is downstream of the corruption, so it is not proof of anything.
    assert res.answer["evidence_strength"] in ("weak", "evidence")


def eval_rechecks_one_script_cheaply(agent):
    res = agent.ask(
        "I already ran a full tt-triage pass on a hung Tenstorrent job. I only "
        "want to re-read the callstacks now. What is the cheapest command?"
    )

    res.assert_dispatched("tt-triage")
    assert "--run=dump_callstacks" in res.answer["command"]


# ---- Device execution --------------------------------------------------------

def _grade(agent, res, scenario: str) -> None:
    """Grade the diagnosis on tokens the tool must have surfaced.

    Case-insensitive because agents paraphrase — the source-of-truth CSV writes
    `0xDEADBEEF` and `CoreMagicNumber::WORKER`, the agent may quote them as
    `0xdeadbeef` or `worker`, and grading formatting rather than substance would
    fail correct readings. Substrings are still exact — no fuzzy matching.
    """
    _output, expected = agent.fixture(scenario)
    tokens = expected.get("must_mention") or []
    diagnosis = res.answer["diagnosis"].lower()
    missing = [t for t in tokens if t.lower() not in diagnosis]
    assert not missing, (
        f"diagnosis missing required tokens {missing}. Full diagnosis:\n"
        f"{res.answer['diagnosis']}"
    )


def eval_diagnoses_multicast_acknowledgement_deficit(agent):
    """A worker BRISC parked in noc_async_atomic_barrier, short 200 acks, sitting
    among eight benign ethernet mismatches. The agent must reach for tt-triage,
    read the counters, and name the file:line of the provoking kernel."""
    res = agent.hang(PROVOKE / "live_hang_mcast_ack.py", "MCAST_AT_BARRIER")

    res.assert_invoked_tool(r"tt-triage(\.py)?\b")
    _grade(agent, res, "mcast-ack-deficit")


def eval_diagnoses_llk_assert_halt(agent):
    """TRISC0 halted inside an unpacker configuration check. Every loud signal in
    the report — CB waits, ethernet counter mismatches, host timeout — is
    downstream fanout. dump_lightweight_asserts names the failing condition in
    one row; the agent must find it."""
    res = agent.hang(
        PROVOKE / "live_hang_llk_assert.py",
        "LLK_ABOUT_TO_INIT_MISMATCHED_CB",
        # LLK asserts is in the JIT compile hash, so this scenario needs its own
        # cache — sharing rebuilds firmware and every kernel in both directions.
        extra_env={
            "TT_METAL_LLK_ASSERTS": "1",
            "TT_METAL_CACHE": f"{os.environ['TT_METAL_HOME']}/jit-cache-llk-asserts",
        },
    )

    res.assert_invoked_tool(r"tt-triage(\.py)?\b")
    _grade(agent, res, "llk-srca-mismatch")


def eval_distrusts_callstack_when_mailbox_is_corrupt(agent):
    """A core's magic is overwritten with a known bad value while that same core
    parks on a buffer nothing fills. check_core_magic reports the corruption;
    per the skill's read order, the callstack on that core is no longer
    trustworthy. The agent must name the corrupt magic, not the parked frame."""
    res = agent.hang(PROVOKE / "live_hang_corrupt_magic.py", "CORE_MAGIC_CORRUPTED")

    res.assert_invoked_tool(r"tt-triage(\.py)?\b")
    _grade(agent, res, "corrupt-core-magic")
