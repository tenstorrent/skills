# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-dprint: does an agent enable kernel printing correctly and know its traps."""

from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"

# Watcher and the device profiler share on-chip SRAM with DPRINT. Enabling one
# alongside it silently corrupts the debug data.
EXCLUSIVE = {"TT_METAL_WATCHER", "TT_METAL_DEVICE_PROFILER"}


def eval_enables_printing_for_wrong_values(agent):
    res = agent.ask(
        "My Tenstorrent compute kernel is producing wrong numbers and I want to "
        "see the values it actually reads on core 0,0. How do I turn kernel "
        "printing on, and what do I have to avoid enabling at the same time?"
    )

    res.assert_dispatched("tt-dprint")
    assert "TT_METAL_DPRINT_CORES" in res.env(), res.answer["env"]
    assert not (res.env().keys() & EXCLUSIVE), f"enabled a conflicting tool: {res.env()}"
    assert set(res.answer["must_disable"]) & EXCLUSIVE, res.answer["must_disable"]


def eval_does_not_invent_the_device_print_variable(agent):
    """`TT_METAL_DEVICE_PRINT=1` is prescribed by prior-art agent docs and does
    not exist. Only the three TT_METAL_DEVICE_PRINT_DISPATCH_* variables do, and
    they have nothing to do with printing."""
    res = agent.ask(
        "What is the environment variable that turns on device-side printing for "
        "a Tenstorrent kernel? Give me the exact name and a working command."
    )

    res.assert_dispatched("tt-dprint")
    named = " ".join(e["name"] for e in res.answer["env"]) + " " + res.answer["command"]
    assert "TT_METAL_DEVICE_PRINT=" not in named, named
    assert "TT_METAL_DPRINT_CORES" in named, named


def eval_diagnoses_a_missing_newline(agent):
    """The host server splits per-RISC streams on `\\n` and drops a trailing
    partial line at device detach rather than flushing it. Upstream names this as
    the most common reason prints never appear, and no environment variable
    fixes it — so an answer that only re-checks the env has missed it."""
    res = agent.ask(
        "I added DPRINT calls to my Tenstorrent kernel and set "
        "TT_METAL_DPRINT_CORES=0,0, but nothing at all shows up on the terminal. "
        "The kernel definitely runs. What is the most likely cause?"
    )

    res.assert_dispatched("tt-dprint")
    explanation = " ".join(
        res.answer["prereqs"] + res.answer["output_literals"] + [res.answer["location"]]
    )
    assert "\\n" in explanation or "newline" in explanation.lower(), explanation


def eval_places_a_tile_print_inside_the_cb_window(agent):
    """`TileSlice` samples the CB read pointer at the moment the print runs, so a
    front read has to sit between cb_wait_front and cb_pop_front. Outside that
    window the values belong to another tile, which reads as a kernel bug."""
    res = agent.ask(
        "I want to print the contents of one tile from a circular buffer in my "
        "Tenstorrent compute kernel, reading from the front of the CB. Where "
        "exactly in the kernel does the print have to go, and why there?"
    )

    res.assert_dispatched("tt-dprint")
    text = " ".join(res.answer["prereqs"] + [res.answer["command"]]).lower()
    assert "cb_wait_front" in text, res.answer["prereqs"]
    assert "cb_pop_front" in text, res.answer["prereqs"]


def eval_knows_the_math_risc_has_no_cb_access(agent):
    """`DPRINT_MATH` with a TSLICE is invalid rather than empty: the math RISC
    cannot reach circular buffers."""
    res = agent.ask(
        "Can I use DPRINT_MATH to print a tile out of a circular buffer from the "
        "math RISC of a Tenstorrent core?"
    )

    res.assert_dispatched("tt-dprint")
    assert res.answer["verdict"] == "no", res.answer["verdict"]


def eval_does_not_use_the_removed_stream_api(agent):
    """`DPRINT << x << ENDL()` was removed and its helpers are deprecated stubs.
    An agent that answers in that style is reproducing dead documentation."""
    res = agent.ask(
        "Show me the exact in-kernel call to print a float and an integer from a "
        "Tenstorrent kernel with DPRINT."
    )

    res.assert_dispatched("tt-dprint")
    code = res.answer["command"] + " " + " ".join(res.answer["output_literals"])
    assert "<<" not in code, f"used the removed stream form: {code}"
    assert "ENDL" not in code, f"used the removed stream form: {code}"


# ---- Device execution --------------------------------------------------------

def eval_reads_a_kernel_beacon_when_dprint_is_on(agent):
    """Run a program whose kernel prints one identifiable literal, and grade
    on whether the agent (a) enabled DPRINT for the right core and (b) came
    back with the exact beacon the kernel emitted. A silent run means the
    agent turned the tool on wrong, not that the kernel misbehaved.

    The kernel source names the beacon, so the harness stashes the provoke
    tree outside the agent's readable roots — otherwise Read(kernels/*.cpp)
    is a free path to the answer that never touches DPRINT."""
    provoke = agent.hide_provoke(PROVOKE / "print_beacon.py")
    res = agent.investigate(
        f"There is a Python program at {provoke} that programs one BRISC on a "
        f"Tenstorrent device with a kernel that emits one identifiable DPRINT "
        f"line, then exits. TT_METAL_HOME is exported. Run the program with "
        f"kernel printing turned on for that core and tell me the exact literal "
        f"the kernel printed."
    )

    res.assert_invoked_tool(r"TT_METAL_DPRINT_CORES=")
    diagnosis = res.answer["diagnosis"]
    assert "TT_DPRINT_EVAL_BEACON" in diagnosis, diagnosis
    # The kernel prints 0xC0FFEE; grade case-insensitively — the DPRINT format
    # writes lowercase hex and agents freely paraphrase between the two.
    assert "c0ffee" in diagnosis.lower(), diagnosis
