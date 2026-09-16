# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-exalens: does an agent know the command surface — modes, batching, ports."""

from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"

# The provoker's compile-time constants; the eval prompt names both to the
# agent, so grading on the value proves the agent went to the device rather
# than parroting the prompt.
MAGIC_ADDR = 0x50000
MAGIC_VALUE = 0xC0DEBABE


def eval_batches_commands_with_semicolons(agent):
    """`--commands=<cmds>` runs semicolon-separated commands and exits. Newline
    or comma will not parse. This is what the skill teaches instead of the REPL
    for automation."""
    res = agent.ask(
        "I want to run three tt-exalens commands non-interactively and see the "
        "output — read L1 word, read a register, exit. What is the exact CLI shape?"
    )

    res.assert_dispatched("tt-exalens")
    assert "--commands" in res.answer["command"], res.answer["command"]
    assert ";" in res.answer["command"], res.answer["command"]


def eval_serves_a_daemon_for_remote_clients(agent):
    """The daemon mode is what lets `tt-triage --remote-exalens` attach and what
    lets another shell reach the device with `--remote`. Default port 5555."""
    res = agent.ask(
        "I want to leave tt-exalens running so tt-triage can attach to it "
        "instead of initialising UMD itself. What do I start?"
    )

    res.assert_dispatched("tt-exalens")
    assert "--server" in res.answer["command"], res.answer["command"]


# ---- Device execution --------------------------------------------------------

def eval_reads_an_l1_magic_word(agent):
    """A provoker writes a distinctive magic word to a fixed L1 offset on
    worker core (0, 0) and spins. The agent is told the address and the
    core — not the value — and has to use tt-exalens to read the word back.
    Grading on the value proves the tool was actually used: the provoke
    source is hidden, so the only way to get the magic is to read L1."""
    res = agent.hang(
        PROVOKE / "live_exalens_magic.py",
        "EXALENS_MAGIC_WRITTEN",
        hint=(
            f"The kernel wrote one 32-bit magic word to L1 address "
            f"{MAGIC_ADDR:#x} on device 0 worker core (0, 0). Read the word "
            f"back and tell me its value."
        ),
    )

    diagnosis = res.answer["diagnosis"].lower()
    # The kernel writes 0xC0DEBABE; exalens prints hex as lowercase, agents
    # freely paraphrase. Either form is a substring hit.
    assert f"{MAGIC_VALUE:x}" in diagnosis, res.answer["diagnosis"]
