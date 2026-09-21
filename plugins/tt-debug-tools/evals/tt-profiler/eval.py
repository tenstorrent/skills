# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-profiler: does an agent know the two entry points and the conflict set."""


def _body(answer) -> str:
    return " ".join([
        answer["primary_signal"], answer["location"], answer["command"],
        " ".join(answer["quoted_evidence"]),
        " ".join(answer["output_literals"]),
        " ".join(answer["prereqs"]),
    ])


def eval_ranks_ops_in_a_test_through_tracy(agent):
    """`python -m tracy -p -r -v -m pytest <test>` is the invocation that writes
    the ops-perf-results CSV under `generated/profiler/reports/<ts>/`, ready for
    `tt-perf-report`. Calling `tt-perf-report` directly, or setting
    `TT_METAL_DEVICE_PROFILER=1` without tracy, does not produce that CSV."""
    res = agent.ask(
        "I have a Tenstorrent ttnn pytest with no profiling data captured "
        "yet. Give me one command that runs the test, captures the ops-perf "
        "CSV, and prints the ranked ops report."
    )

    res.assert_dispatched("tt-profiler")
    cmd = res.answer["command"]
    assert "tracy" in cmd, cmd
    assert "-p" in cmd and "-r" in cmd, cmd
    assert "pytest" in cmd, cmd


def eval_names_the_conflict_set(agent):
    """The device profiler shares on-chip SRAM with DPRINT and watcher. Setting
    two of them together silently corrupts one of the outputs — no error, no
    signal. The skill exists to teach this before the reader debugs bad data."""
    res = agent.ask(
        "I want to profile a Tenstorrent kernel with the device profiler on. "
        "Which other environment variables must I not have set at the same time?"
    )

    res.assert_dispatched("tt-profiler")
    disable = " ".join(res.answer["must_disable"])
    assert "TT_METAL_WATCHER" in disable, res.answer["must_disable"]
    assert "TT_METAL_DPRINT_CORES" in disable, res.answer["must_disable"]


def eval_knows_the_csv_does_not_live_under_logs_path(agent):
    """The raw device-zone CSV lands under `$TT_METAL_HOME/generated/profiler/`,
    not under `TT_METAL_LOGS_PATH` like watcher, DPRINT and Inspector. Looking
    for it in the wrong place reads as "no data collected"."""
    res = agent.ask(
        "I set TT_METAL_DEVICE_PROFILER=1 and ran a program. Where does the "
        "device zone CSV land?"
    )

    res.assert_dispatched("tt-profiler")
    paths = " ".join(res.answer["output_paths"])
    assert "TT_METAL_HOME" in paths or "TT_METAL_HOME" in res.answer["command"] \
        or "generated/profiler" in paths, res.answer["output_paths"]


# ---- Reading captured output -------------------------------------------------

def eval_names_the_dominant_op_in_a_tt_perf_report(agent):
    """Hand the agent a real tt-perf-report ranked table and ask which op
    dominates plus what class of bottleneck it is. The dominant row runs
    on only 2 of 64 worker cores with 0.8% DRAM and 3.0% FLOPs — the
    exact numbers that fall into the skill's `under-parallelized` tag.
    Naming that tag proves the agent read the taxonomy, not just the
    table."""
    output, expected = agent.fixture("op-report")
    prompt = (
        "Below is the output of `tt-perf-report` on a Tenstorrent Tracy "
        "run over several matmuls. Report which single op dominates the "
        "wall-clock time — its shape, its device time, and its total "
        "percentage. Then, using the op-level bottleneck tags from the "
        "tt-profiler skill (one of: reader-bound, compute-bound, "
        "writer-bound, under-parallelized, NOC-stall, host-dominated), "
        "pick the tag whose definition its row matches and cite the "
        "specific percentages you used. Do not paraphrase the tag name; "
        "put the tag verbatim in your answer.\n\n"
        "----- begin tt-perf-report output -----\n" + output +
        "----- end tt-perf-report output -----"
    )
    res = agent.ask(prompt)

    # Not asserting dispatch: the prompt already enumerates the tag names,
    # so an agent can reach the right classification without loading the
    # skill file. The identity + tag substring checks are what matter.
    # Answer body across every field an agent might have used; must_mention
    # substrings only have to land somewhere in it.
    body = " ".join([
        res.answer["primary_signal"], res.answer["location"], res.answer["command"],
        " ".join(res.answer["quoted_evidence"]),
        " ".join(res.answer["output_literals"]),
        " ".join(res.answer["prereqs"]),
    ])
    for token in expected["must_mention"]:
        assert token in body, (
            f"missing {token!r} anywhere in the answer. answer={res.answer}"
        )
    # The bottleneck classification is the skill-specific answer; grading
    # on `under-parallel` covers both hyphen conventions.
    assert "under-parallel" in body.lower() or "under parallel" in body.lower(), (
        f"did not classify as under-parallelized. answer={res.answer}"
    )


def eval_reads_device_zone_csv(agent):
    """Device zones are the CSV rows written by `TT_METAL_DEVICE_PROFILER=1`,
    with one row per zone-boundary event. Grading on the recording RISC, the
    user zone name and the START/END pairing proves the agent read the
    columns rather than paraphrasing the header."""
    output, expected = agent.fixture("device-zones")
    prompt = (
        "Below is device zone data captured by tt-profiler on a Tenstorrent "
        "run. Read it and report which RISC recorded the user zones, the "
        "name of the user zone, and how a zone's duration is expressed. "
        "Quote representative rows in quoted_evidence.\n\n"
        "----- begin device-zones output -----\n" + output +
        "----- end device-zones output -----"
    )
    res = agent.ask(prompt)

    body = _body(res.answer)
    for token in expected["must_mention"]:
        assert token in body, (
            f"missing {token!r} anywhere in the answer. answer={res.answer}"
        )
