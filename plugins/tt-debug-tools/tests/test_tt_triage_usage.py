# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-triage: does an agent capture device state correctly and read the report."""

import pytest

pytestmark = pytest.mark.agent


def test_captures_a_live_hang(agent):
    res = agent(
        "A pytest on a Tenstorrent device has been stuck for ten minutes. The "
        "process is still running. Get me the device state."
    )

    assert res.skill == "tt-triage"
    assert "tt-triage" in res.answer["command"]
    assert "--llm-output" in res.answer["command"]


def test_does_not_reset_before_capturing(agent):
    """Triage reads the hung process over a live RPC. Resetting first destroys the
    evidence, and it is the tempting wrong move."""
    res = agent(
        "My Tenstorrent test is hung. Should I reset the board first, with "
        "tt-smi -r, so that triage starts from a clean state?"
    )

    assert res.skill == "tt-triage"
    assert res.answer["verdict"] == "no"
    assert "tt-smi -r" not in res.answer["command"]


def test_routes_around_a_device_it_cannot_open(agent):
    res = agent(
        "tt-triage fails during UMD initialisation on my Tenstorrent host — "
        "another process owns the device. Can I still get a report?"
    )

    assert res.skill == "tt-triage"
    assert res.answer["verdict"] == "yes"
    assert "--remote-exalens" in res.answer["command"]
    # The server runs in a second shell, so it is a prerequisite of the command
    # rather than part of it.
    prereqs = res.prereqs_text()
    assert "exalens" in prereqs and "server" in prereqs, res.answer["prereqs"]


def test_rejects_the_idle_op_premise(agent):
    """Op-level scripts report the dispatcher, not the cores: an op reads idle
    once its GO was sent, even while a kernel is still stuck. Agreeing that the
    device is therefore finished is the failure."""
    res = agent(
        "My tt-triage report says the op level is idle, but the test never "
        "returned. Does that mean the device finished and the hang is on the host?"
    )

    assert res.skill == "tt-triage"
    assert res.answer["verdict"] == "no"
    assert "callstack" in res.answer["primary_signal"].lower(), res.answer["primary_signal"]


def test_treats_corruption_as_a_trust_gate(agent):
    """A corrupted mailbox invalidates every script that reads mailboxes,
    callstacks included, so the parked frame is not trustworthy evidence."""
    res = agent(
        "My tt-triage report has check_core_magic reporting a likely corrupted "
        "mailbox, and dump_callstacks shows NCRISC parked in cb_wait_front. "
        "Which one do I chase first?"
    )

    assert res.skill == "tt-triage"
    assert res.answer["primary_signal"] == "check_core_magic", res.answer["primary_signal"]
    # The callstack is downstream of the corruption, so it is not proof of anything.
    assert res.answer["evidence_strength"] in ("weak", "evidence")


def test_rechecks_one_script_cheaply(agent):
    res = agent(
        "I already ran a full tt-triage pass on a hung Tenstorrent job. I only "
        "want to re-read the callstacks now. What is the cheapest command?"
    )

    assert res.skill == "tt-triage"
    assert "--run=dump_callstacks" in res.answer["command"]
