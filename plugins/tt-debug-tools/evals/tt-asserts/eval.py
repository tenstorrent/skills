# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-asserts: does an agent enable an assert family together with a reporter."""

import os
from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"


def eval_enables_llk_asserts(agent):
    """`TT_METAL_LLK_ASSERTS=1` alone is what the skill teaches. On Wormhole,
    pairing with lightweight asserts overflows the dispatch build under ttnn and
    the detail is recoverable from the ELF anyway, so the agent should reach for
    the LLK flag directly. Watcher pairing is optional and useful only when the
    message on stderr is what you want."""
    res = agent.ask(
        "I want tt-llk's own internal assertions checked while running a "
        "Tenstorrent test, and I want to actually see which one failed. What do I "
        "set?"
    )

    res.assert_dispatched("tt-asserts")
    keys = set(res.env())
    assert "TT_METAL_LLK_ASSERTS" in keys, res.answer["env"]


def eval_knows_a_fired_assert_presents_as_a_hang(agent):
    """The macro expands to an `if` plus `ebreak`: the core halts, nothing is
    printed, and the host waits on a completion that never arrives. An agent that
    expects an error message on stderr will misread the symptom."""
    res = agent.ask(
        "My Tenstorrent test stopped making progress. I had "
        "TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1 set. What most likely happened, "
        "and what do I run to find out which assertion it was?"
    )

    res.assert_dispatched("tt-asserts")
    assert "dump_lightweight_asserts" in res.answer["command"], res.answer["command"]


def eval_sources_rather_than_executes_the_setup_script(agent):
    """The script works by exporting into the caller's shell, so executing it is
    a no-op. It also takes two required positional paths.

    Searched across `command` and `prereqs`: the schema holds a single command
    and this is a three-step recipe, so the sourcing legitimately lands in the
    prerequisites rather than in the command."""
    res = agent.ask(
        "How do I use tt-metal's helper script to run a test with LLK asserts "
        "fully instrumented on a Tenstorrent device? Give me the exact commands."
    )

    res.assert_dispatched("tt-asserts")
    answer = res.answer["command"] + " " + " ".join(res.answer["prereqs"]).lower()
    assert "setup_llk_assert_env.sh" in answer, answer
    assert "source" in answer.lower(), f"did not source the script: {answer}"


def eval_does_not_copy_the_dead_device_print_export(agent):
    """The script exports TT_METAL_DEVICE_PRINT=1, which is not an EnvVarID and
    does nothing. An agent reading the script and reporting that variable as the
    print switch has reproduced an upstream bug."""
    res = agent.ask(
        "Reading tt-metal's setup_llk_assert_env.sh, which environment variable "
        "in it is what actually turns device-side printing on?"
    )

    res.assert_dispatched("tt-asserts")
    named = " ".join(e["name"] for e in res.answer["env"]) + " " + res.answer["command"]
    assert "TT_METAL_DPRINT_CORES" in named, named
    assert "TT_METAL_DEVICE_PRINT=" not in named, named


def eval_rejects_pairing_llk_with_lightweight_on_wormhole(agent):
    """Upstream's setup script pairs LLK asserts with lightweight kernel
    asserts. Measured on Wormhole n300 under ttnn, the pair overflows the
    idle_erisc dispatch build and the run does not launch. LLK asserts alone
    recover the assert message and callstack from the ELF, so the pairing
    buys nothing. An agent that recommends both without a caveat is copying
    the upstream script."""
    res = agent.ask(
        "I am on a Wormhole n300, running a ttnn workload, and I want to be "
        "sure that any LLK internal assertion that fires is diagnosable. "
        "Should I set both TT_METAL_LLK_ASSERTS=1 and "
        "TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1, or one of them?"
    )

    res.assert_dispatched("tt-asserts")
    env_names = {e["name"] for e in res.answer["env"]}
    assert "TT_METAL_LLK_ASSERTS" in env_names, res.answer["env"]
    assert "TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS" not in env_names, (
        f"recommended the pair despite the dispatch-overflow trap: {res.answer['env']}"
    )


def eval_names_a_sanitizer_severity_flag(agent):
    """`TT_METAL_LLK_SANITIZER=1` turns on the instrumentation but reports
    nothing until at least one severity switch is enabled. The six switches
    are independent — no threshold, no default. Setting only the master flag
    and expecting output is the specific mistake the skill catches."""
    res = agent.ask(
        "I set `TT_METAL_LLK_SANITIZER=1` on my Tenstorrent workload and got "
        "no sanitizer output at all. Which environment variable am I missing?"
    )

    res.assert_dispatched("tt-asserts")
    severities = {"PEDANTIC", "WARN", "ERROR", "FAULT", "INFO", "INTERNAL"}
    named = " ".join(e["name"] for e in res.answer["env"])
    assert any(f"TT_METAL_LLK_SANITIZER_{s}" in named for s in severities), (
        f"no severity switch named: env={res.answer['env']}"
    )


# ---- Device execution --------------------------------------------------------

def eval_reads_a_fired_llk_assert(agent):
    """Enable TT_METAL_LLK_ASSERTS=1 and run a kernel that trips one — the
    unpacker-A configuration check fires because the kernel initialises for a
    CB with a different data format than SrcA is holding. The core ebreaks and
    the workload presents as a hang; dump_lightweight_asserts reads back the
    assert expression and callstack the agent has to name.

    The LLK flag is in the JIT compile hash, so the scenario gets its own
    cache — sharing would rebuild firmware and every kernel on every capture."""
    res = agent.hang(
        PROVOKE / "live_llk_assert.py",
        "LLK_ABOUT_TO_INIT_MISMATCHED_CB",
        extra_env={
            "TT_METAL_LLK_ASSERTS": "1",
            "TT_METAL_CACHE": f"{os.environ['TT_METAL_HOME']}/jit-cache-llk-asserts",
        },
    )

    res.assert_invoked_tool(r"tt-triage(\.py)?\b|dump_lightweight_asserts\b")
    diagnosis = res.answer["diagnosis"].lower()
    # Which side of the unpacker-A configuration check fires first depends on
    # runtime state, so grade on the discriminator, not on src-vs-dst.
    assert "unp_a_" in diagnosis and "mismatch" in diagnosis, res.answer["diagnosis"]


# No device eval for lightweight kernel asserts on this hardware.
# TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1 alone overflows the idle_erisc
# dispatch build under ttnn on Wormhole n300 — the exact trap the skill
# teaches at SKILL.md's "Enabling an assert family is not enabling a
# report" section. The pair with LLK asserts overflows too, so there is
# no ttnn-dispatch path that exercises this family on this part. The
# interface-knowledge check below stands in for the missing device eval.
