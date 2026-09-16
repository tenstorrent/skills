# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-inspector: does an agent know that turning Inspector off degrades every
downstream tool silently."""


def eval_treats_disabling_inspector_as_a_trap(agent):
    """`TT_METAL_INSPECTOR=1` is the default. Setting it to `0` does not raise
    an error — it makes `tt-triage`'s dispatcher-aware scripts skip, and the
    thin report reads as clean unless the agent knows to blame Inspector.

    The prompt is symptom-first on purpose — it names the triage report, so
    the routing pick between tt-triage and tt-inspector is not the point of
    the eval. The point is naming `TT_METAL_INSPECTOR` as the switch."""
    res = agent.ask(
        "A hang report has every dispatcher-aware section marked skipped, but "
        "no error was raised and the device itself is fine. Which environment "
        "variable's default has been overridden?"
    )

    env_names = {e["name"] for e in res.answer["env"]}
    primary = res.answer["primary_signal"]
    assert "TT_METAL_INSPECTOR" in env_names \
        or "TT_METAL_INSPECTOR" in primary \
        or "inspector" in primary.lower(), \
        f"env={sorted(env_names)}, primary_signal={primary!r}"


def eval_knows_the_rpc_default_address(agent):
    """The RPC serves at `localhost:50051` by default. A remote client or a
    manual attach needs that address."""
    res = agent.ask(
        "I want to connect to the Inspector RPC of a running Tenstorrent workload "
        "from a second shell. What address does it serve on by default?"
    )

    res.assert_dispatched("tt-inspector")
    lit = " ".join(res.answer["output_literals"]).lower()
    assert "50051" in lit or "50051" in res.answer["command"], res.answer["output_literals"]


# ---- Device execution --------------------------------------------------------

from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"

# The provoker's kernel filename. The agent has to name this string in the
# diagnosis; anything shorter would let the agent guess.
KERNEL_WITNESS = "inspector_witness_kernel_b3f7a1"


def eval_names_a_running_kernel_via_inspector(agent):
    """A provoker holds one program alive with a distinctively-named kernel.
    Inspector's RPC and kernels.yaml both name the kernel path; the agent
    has to query one and report the filename. The provoke source is hidden,
    so the string can only reach the diagnosis through Inspector."""
    res = agent.hang(
        PROVOKE / "live_inspector_witness.py",
        "INSPECTOR_WITNESS_KERNEL_ABOUT_TO_HANG",
        hint=(
            "One program with one built kernel is running on this workload. "
            "Ask Inspector to list its kernels and tell me the exact "
            "filename (basename with extension) of the kernel."
        ),
    )

    assert KERNEL_WITNESS in res.answer["diagnosis"], res.answer["diagnosis"]
