# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-watcher: one eval per skill, covering knowledge, reading, and device.

Interface-knowledge tests grade the skill's SKILL.md against how an agent
would answer questions about driving watcher. Reading tests hand the agent
captured watcher output and grade its interpretation. Device tests flip a
watcher guard on real hardware and let the agent read what watcher reports.
"""

from pathlib import Path

PROVOKE = Path(__file__).resolve().parent / "provoke"


# ---- Interface knowledge -----------------------------------------------------

# Watcher, kernel prints and the device profiler share on-chip SRAM. Enabling a
# second one silently corrupts the debug data, so it is a real misconfiguration.
EXCLUSIVE = {"TT_METAL_DPRINT_CORES", "TT_METAL_DEVICE_PROFILER"}


def eval_enables_watcher_for_a_suspected_bad_address(agent):
    res = agent.ask(
        "A kernel on my Tenstorrent device writes a tensor that comes back with "
        "garbage in it, and I suspect a NoC write is going to the wrong core. "
        "How do I get the tooling to name the bad transaction, and where does "
        "the output land?"
    )

    res.assert_dispatched("tt-watcher")
    assert "TT_METAL_WATCHER" in res.env()
    assert not (res.env().keys() & EXCLUSIVE), f"enabled a conflicting tool: {res.env()}"
    assert set(res.answer["must_disable"]) & EXCLUSIVE, res.answer["must_disable"]


def eval_knows_the_log_is_not_under_tt_metal_home(agent):
    """`logs_dir` defaults to the working directory. An agent that answers
    `$TT_METAL_HOME/generated/watcher/watcher.log` sends the reader to a path
    that does not exist unless they happened to launch from there.

    Asserted on the path it resolves, not on whether it explains itself: the
    scenario launches from the home directory, so a correct answer roots the log
    there and a wrong one roots it in the checkout."""
    res = agent.ask(
        "I ran a Tenstorrent job with TT_METAL_WATCHER=1 from my home directory, "
        "with TT_METAL_HOME pointing at a tt-metal checkout elsewhere. Exactly "
        "where is watcher.log, and what decides that?"
    )

    res.assert_dispatched("tt-watcher")
    paths = " ".join(res.answer["output_paths"])
    assert "generated/watcher/watcher.log" in paths, res.answer["output_paths"]
    assert "TT_METAL_HOME" not in paths and "tt-metal/" not in paths, (
        f"rooted the log in the checkout: {res.answer['output_paths']}"
    )


def eval_rejects_the_regression_premise(agent):
    """A check that was never running before cannot have regressed. Reading a
    first-ever watcher trip as a new bug sends the investigation at the wrong
    commit range."""
    res = agent.ask(
        "My Tenstorrent test passed for months. I just ran it under "
        "TT_METAL_WATCHER=10 for the first time and it trips a circular-buffer "
        "overflow. Does that mean a recent commit introduced the overflow?"
    )

    res.assert_dispatched("tt-watcher")
    assert res.answer["verdict"] == "no"


def eval_picks_a_longer_interval_for_an_intermittent_hang(agent):
    """Polling perturbs timing, so the escalation for a bug that comes and goes
    is a longer interval, not a shorter one. The upstream doc says as much and
    prior art gets it backwards.

    Asserted on the interval it actually chooses, not on how it justifies it: a
    millisecond suffix or a sub-second value is the wrong direction whatever the
    prose says."""
    res = agent.ask(
        "I have a Tenstorrent hang that only reproduces about one run in ten. I "
        "want to catch it with watcher. Should I poll more often or less often "
        "than the default, and why? Give me the command."
    )

    res.assert_dispatched("tt-watcher")
    interval = res.env().get("TT_METAL_WATCHER", "")
    assert interval, res.answer["env"]
    assert "ms" not in interval, f"chose a millisecond interval: {interval!r}"
    assert int("".join(c for c in interval if c.isdigit()) or 0) > 1, (
        f"chose an aggressive interval for an intermittent hang: {interval!r}"
    )


def eval_pairs_debug_delay_with_its_preconditions(agent):
    """`TT_METAL_WATCHER_DEBUG_DELAY` asserts on two things: watcher enabled, and
    NoC sanitization not disabled. Naming the delay alone produces a run that
    aborts in rtoptions."""
    res = agent.ask(
        "I want to reproduce a race between two RISCs on a Tenstorrent core by "
        "stalling the writer's NoC writes. Which environment variables do I set, "
        "and what has to be true for them to take effect?"
    )

    res.assert_dispatched("tt-watcher")
    assert "TT_METAL_WATCHER_DEBUG_DELAY" in res.env(), res.answer["env"]
    assert "TT_METAL_WATCHER" in res.env(), res.answer["env"]
    # The delay needs a target as well as a duration.
    targets = [k for k in res.env() if k.endswith(("_CORES", "_RISCVS"))]
    assert targets, res.answer["env"]


def eval_does_not_use_the_nonexistent_disable_name(agent):
    """Three upstream assertion messages name TT_METAL_WATCHER_DISABLE_NOC_SANITIZE,
    which is not a real variable. Copying it from the error text produces a
    setting that does nothing."""
    res = agent.ask(
        "Watcher pushes my Tenstorrent fabric kernel over the binary size limit. "
        "Which watcher checks do I turn off, in what order, and what are the "
        "exact variable names?"
    )

    res.assert_dispatched("tt-watcher")
    named = " ".join(res.answer["must_disable"]) + " " + res.answer["command"] + " " + " ".join(
        e["name"] for e in res.answer["env"]
    )
    assert "TT_METAL_WATCHER_DISABLE_NOC_SANITIZE" not in named, named
    assert "TT_METAL_WATCHER_DISABLE_SANITIZE_NOC" in named, named


# ---- Reading captured output -------------------------------------------------

# Each prompt asks for something the output does not spell out — a waypoint
# expansion, whether a kernel was loaded. Asking only "what does this show" lets
# an agent answer from the text alone and never dispatch, which makes the
# skill assertion measure nothing. The reading has to need the skill.
CONSOLE_PROMPT = (
    "Below is the console output of a Tenstorrent run with watcher enabled. Read "
    "it and report what it shows, and say what the waypoint on the faulting RISC "
    "means and whether it indicates the core was waiting or had finished. Quote "
    "the lines you relied on verbatim in quoted_evidence — do not paraphrase "
    "them.\n\n"
    "----- begin run output -----\n{output}\n----- end run output -----"
)

LOG_PROMPT = (
    "Below is one dump block from a Tenstorrent watcher.log. Read it and report "
    "what it shows. Decode the run-message and k_ids fields, and say whether any "
    "kernel was loaded on these cores at the time of this dump. Quote the lines "
    "you relied on verbatim in quoted_evidence — do not paraphrase them.\n\n"
    "----- begin watcher.log block -----\n{output}\n"
    "----- end watcher.log block -----"
)


def _check(res, expected, output):
    """Compare a reading against the hand-authored ground truth.

    Only the keys the fixture actually pins are compared, so a scenario asserts
    what it knows and stays silent on the rest.
    """
    for key in ("fault_found", "primary_signal", "verdict", "evidence_strength"):
        if key in expected:
            assert res.answer[key] == expected[key], (
                f"{key}: expected {expected[key]!r}, got {res.answer[key]!r}"
            )
    for token in expected.get("location_contains", []):
        assert token in res.answer["location"], (
            f"location {res.answer['location']!r} missing {token!r}"
        )
    # Verbatim quoting is the guard against a confident reading of nothing.
    for quote in res.answer["quoted_evidence"]:
        assert quote.strip(), "empty quote in quoted_evidence"
        assert quote in output, f"quoted a line that is not in the output: {quote!r}"


def eval_reads_a_sanitizer_trip(agent):
    """Watcher names the offending transaction on the console. The reading has to
    come back with the RISC that issued it, not just 'a NoC error'."""
    output, expected = agent.fixture("noc-sanitize-trip")
    res = agent.ask(CONSOLE_PROMPT.format(output=output))

    res.assert_dispatched("tt-watcher")
    _check(res, expected, output)


def eval_does_not_read_an_idle_dump_as_a_hang(agent):
    """The negative control, and the specific mistake it guards against: the
    first dump lands before the first kernel launch, so every core sits at `GW`
    with a blank kernel map. That is an idle device. An agent that reports cores
    'stuck waiting' here would report a hang on every healthy run.

    Dispatch is deliberately **not** asserted here, for the same reason as
    tt-triage's negative control: an idle dump reads as self-explanatory and the
    agent often answers it without loading the skill. The reading is what this
    test is for.
    """
    output, expected = agent.fixture("first-dump-idle")
    res = agent.ask(LOG_PROMPT.format(output=output))

    assert res.answer["fault_found"] == "no", res.answer["location"]
    _check(res, expected, output)


# ---- Device execution --------------------------------------------------------

def eval_reports_a_noc_sanitize_trip(agent):
    """Enable TT_METAL_WATCHER=1 and run a kernel that writes to a virtual
    coordinate that does not exist on Wormhole. The compiled sanitize wrapper
    catches the bad address, watcher stops the device and prints the fault
    line to stderr and to watcher.log. The agent has to name the RISC that
    issued the write and the phantom target coordinate.

    Every non-noc watcher check is disabled at build time — under ttnn's
    fabric-1D dispatch, the full watcher instrumentation overflows the
    idle_erisc code region. Splitting guards across provokers is the price."""
    import os as _os
    _disable = {
        f"TT_METAL_WATCHER_DISABLE_{name}": "1" for name in (
            "ASSERT", "PAUSE", "RING_BUFFER", "STACK_USAGE",
            "SANITIZE_READ_ONLY_L1", "SANITIZE_WRITE_ONLY_L1",
            "WAYPOINT", "DISPATCH", "ETH", "CB_SANITIZE",
        )
    }
    res = agent.hang(
        PROVOKE / "live_watcher_noc_sanitize.py",
        "WATCHER_NOC_SANITIZE_ABOUT_TO_FIRE",
        extra_env={
            "TT_METAL_WATCHER": "1",
            "TT_METAL_CACHE": f"{_os.environ['TT_METAL_HOME']}/jit-cache-watcher-noc",
            **_disable,
        },
    )

    # The agent must have reached watcher's own evidence — watcher.log, the
    # process stderr with the fault line, or tt-triage's watcher reader. The
    # provoke script prints nothing that names the fault (source hidden, and
    # the launch stdout is just "enqueued"), so a diagnosis passing without
    # touching one of those paths would have to fabricate the coord.
    res.assert_invoked_tool(r"watcher\.log|Watcher detected|tt-triage|tt_device_job_logs")
    diagnosis = res.answer["diagnosis"].lower()
    assert "brisc" in diagnosis, res.answer["diagnosis"]
    assert "26-18" in diagnosis or "26,18" in diagnosis or "(26, 18)" in diagnosis, (
        res.answer["diagnosis"]
    )


def eval_reports_a_kernel_assert(agent):
    """Enable TT_METAL_WATCHER=1 with ASSERT-only among the guards. The
    kernel calls ASSERT(0); under WATCHER_ENABLED the macro records the
    failing line and hangs BRISC. Watcher prints the assertion line and
    core coord to stderr and watcher.log, and the agent has to name it."""
    import os as _os
    _disable = {
        f"TT_METAL_WATCHER_DISABLE_{name}": "1" for name in (
            "PAUSE", "RING_BUFFER", "STACK_USAGE",
            "SANITIZE_NOC", "SANITIZE_READ_ONLY_L1", "SANITIZE_WRITE_ONLY_L1",
            "WAYPOINT", "DISPATCH", "ETH", "CB_SANITIZE",
        )
    }
    res = agent.hang(
        PROVOKE / "live_watcher_assert.py",
        "WATCHER_ASSERT_ABOUT_TO_FIRE",
        extra_env={
            "TT_METAL_WATCHER": "1",
            "TT_METAL_CACHE": f"{_os.environ['TT_METAL_HOME']}/jit-cache-watcher-assert",
            **_disable,
        },
    )

    res.assert_invoked_tool(r"watcher\.log|Watcher detected|tt-triage|tt_device_job_logs")
    diagnosis = res.answer["diagnosis"].lower()
    # "assert" alone is easily hallucinated; the line number in watcher's
    # output only exists in the report. Grade on the full compound instead.
    assert "assert" in diagnosis and "line" in diagnosis, res.answer["diagnosis"]
    assert "brisc" in diagnosis, res.answer["diagnosis"]


def eval_reports_a_waypoint_on_a_stalled_core(agent):
    """Enable TT_METAL_WATCHER=1 with WAYPOINT-only among the guards. The
    kernel writes waypoints STRT and STOP, then spins. Watcher.log records
    STOP as the last waypoint on this core; the agent has to name it."""
    import os as _os
    _disable = {
        f"TT_METAL_WATCHER_DISABLE_{name}": "1" for name in (
            "ASSERT", "PAUSE", "RING_BUFFER", "STACK_USAGE",
            "SANITIZE_NOC", "SANITIZE_READ_ONLY_L1", "SANITIZE_WRITE_ONLY_L1",
            "DISPATCH", "ETH", "CB_SANITIZE",
        )
    }
    res = agent.hang(
        PROVOKE / "live_watcher_waypoint.py",
        "WATCHER_WAYPOINT_ABOUT_TO_STALL",
        extra_env={
            "TT_METAL_WATCHER": "1",
            "TT_METAL_CACHE": f"{_os.environ['TT_METAL_HOME']}/jit-cache-watcher-waypoint",
            **_disable,
        },
    )

    res.assert_invoked_tool(r"watcher\.log|tt-triage|tt_device_job_logs")
    diagnosis = res.answer["diagnosis"]
    # STOP is the discriminator — a distinctive 4-char waypoint the source
    # of watcher.log names verbatim.
    assert "STOP" in diagnosis, res.answer["diagnosis"]
