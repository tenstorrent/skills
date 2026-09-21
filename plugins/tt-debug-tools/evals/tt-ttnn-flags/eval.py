# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-ttnn-flags: does an agent turn fast-runtime mode off first."""


def eval_turns_fast_runtime_mode_off_before_enabling_debug(agent):
    """`enable_fast_runtime_mode` defaults to `true` and silently disables every
    debug mode. Turning on graph capture or comparison mode without turning
    fast runtime off first does nothing — no error, no output. This is the
    single trap the skill exists to teach."""
    res = agent.ask(
        "My TTNN model is producing wrong output. I want per-op golden "
        "comparison so I can find the first op that diverges. What do I set?"
    )

    res.assert_dispatched("tt-ttnn-flags")
    joined = str(res.answer["env"]) + res.answer["command"]
    assert "enable_fast_runtime_mode" in joined, joined
    assert "false" in joined.lower(), joined
    assert "enable_comparison_mode" in joined or "comparison" in joined, joined


def eval_uses_the_ttnn_config_overrides_variable(agent):
    """The switch is `TTNN_CONFIG_OVERRIDES` with a JSON payload. Setting the
    individual keys as bare env vars does nothing."""
    res = agent.ask(
        "How do I turn on TTNN graph capture on a single test run — without "
        "editing the source — so I can see the op graph at the point it hangs?"
    )

    res.assert_dispatched("tt-ttnn-flags")
    keys = set(res.env())
    assert "TTNN_CONFIG_OVERRIDES" in keys, res.answer["env"]


# ---- Reading captured output -------------------------------------------------

def eval_reads_the_fast_runtime_trap_from_a_config_dump(agent):
    """Hand the agent a real capture of ttnn's Config dump plus tt-metal's
    own warning — comparison mode enabled while fast runtime mode is still
    on. The agent has to (a) name the flag to set to false and (b) quote
    tt-metal's warning as the evidence."""
    output, expected = agent.fixture("fast-runtime-trap")
    prompt = (
        "Below is the stderr of a ttnn import with "
        "TTNN_CONFIG_OVERRIDES on a Tenstorrent host. Report what's wrong "
        "with this run's flags and which single flag has to be set to "
        "which value to make comparison mode actually work. Quote the "
        "runtime warning in quoted_evidence.\n\n"
        "----- begin ttnn stderr -----\n" + output +
        "----- end ttnn stderr -----"
    )
    res = agent.ask(prompt)

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


def eval_reads_a_comparison_fail_line(agent):
    """Hand the agent the stderr of a ttnn matmul running with
    `enable_comparison_mode=true` and `comparison_mode_pcc=1.0` — the
    ERROR-level per-op comparison line. The agent has to name the op and
    the actual PCC. Same shape as the trap eval, but the input shows the
    tool doing what the SKILL says it does."""
    output, expected = agent.fixture("comparison-fail")
    prompt = (
        "Below is a stderr capture from a ttnn run under comparison mode "
        "on a Tenstorrent host. Report which op fell below the PCC "
        "threshold and the exact actual PCC the runtime measured. Quote "
        "the log line in quoted_evidence.\n\n"
        "----- begin ttnn stderr -----\n" + output +
        "----- end ttnn stderr -----"
    )
    res = agent.ask(prompt)

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
