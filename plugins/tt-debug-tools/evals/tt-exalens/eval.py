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


def _body(answer) -> str:
    return " ".join([
        answer["primary_signal"], answer["location"], answer["command"],
        " ".join(answer["quoted_evidence"]),
        " ".join(answer["output_literals"]),
        " ".join(answer["prereqs"]),
    ])


def eval_reads_scripted_commands_output(agent):
    """The `--commands` batch echoes each command back before its output.
    Grading on the batch shape (`--commands`, `brxy`) and the RISC-V grid
    (`RRRRR` = five RISCs running per core) proves the agent parsed both
    what was run and what came back, not just one half."""
    output, expected = agent.fixture("scripted-commands")
    prompt = (
        "Below is the output of a `tt-exalens --commands=...` batch. What "
        "shape did the batch have, and what does the `RRRRR` per cell in "
        "the device grid tell you about the RISCs on those cores? Quote "
        "lines you relied on in quoted_evidence.\n\n"
        "----- begin tt-exalens output -----\n" + output +
        "----- end tt-exalens output -----"
    )
    res = agent.ask(prompt)

    body = _body(res.answer)
    for token in expected["must_mention"]:
        assert token in body, (
            f"missing {token!r} anywhere in the answer. answer={res.answer}"
        )


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


def eval_daemon_must_start_before_the_workload(agent):
    """The `--server` daemon is a `--remote-exalens` prerequisite, but with a
    timing rule: a daemon started *after* the workload has taken the device
    cannot open the device either. Answering "just start it now" is the
    plausible wrong move the skill exists to correct."""
    res = agent.ask(
        "A tt-metal workload has already grabbed my Tenstorrent device and is "
        "running. Can I start a tt-exalens daemon on the same host now, so "
        "tt-triage can attach with --remote-exalens?"
    )

    res.assert_dispatched("tt-exalens")
    assert res.answer["verdict"] == "no", res.answer["verdict"]


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
