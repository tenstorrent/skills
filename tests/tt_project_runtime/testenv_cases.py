# Test session environment tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new test-session environment tests here, not there.
from __future__ import annotations


def test_the_session_starts_without_a_worker_runs_variables_but_keeps_the_checks_harness_ones():
    import conftest
    worker = {"TTP_TASK": "72", "TTP_RUN_DIR": "/r", "TTP_PROJECT": "/p", "TTP_NO_DEVICE": "1",
              "TTP_HANDOFF_GUARD": "1", "TTP_ROLE": "worker", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s",
              "CLAUDE_ENV_FILE": "/e", "CODEX_THREAD_ID": "t"}
    harness = {"TTP_TEST_SLOTS": "6", "TTP_TEST_SLOTS_HELD": "6", "TTP_CHECKS_LANES": "2", "TTP_PORT_BASE": "4000",
               "TTP_CHECK_OUTPUT_DIR": "/o", "TTP_LEAK_PATTERNS_FILE": "/l", "TTP_TX_GUARD": "1",
               "TTP_DEVICE_NODES": "", "CLAUDE_CONFIG_DIR": "/c", "PATH": "/bin", "TT_VISIBLE_DEVICES": ""}
    assert conftest.worker_env_cleared({**worker, **harness}) == harness
    # this session too, run inside a worker or not
    assert not set(worker) & set(os.environ), sorted(set(worker) & set(os.environ))
