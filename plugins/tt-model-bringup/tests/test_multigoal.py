from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "multigoal"
LOADER = importlib.machinery.SourceFileLoader("tt_metal_multigoal", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
MULTIGOAL = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = MULTIGOAL
LOADER.exec_module(MULTIGOAL)


class GoldenGateIntegrationTests(unittest.TestCase):
    def test_fallback_gate_is_selected_only_for_golden_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = pathlib.Path(directory) / "00-golden-tests.txt"
            prompt.write_text("Use $golden-tests")
            self.assertEqual(MULTIGOAL.find_check_script(prompt), MULTIGOAL.ROOT / "scripts/check_golden_tests.sh")
            prompt.write_text("Legacy goal")
            self.assertIsNone(MULTIGOAL.find_check_script(prompt))

    def test_golden_gate_precedes_existing_checks_and_blocks_on_failure(self):
        for golden_code in (0, 2):
            with self.subTest(golden_code=golden_code), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                existing = root / "existing.check.sh"
                results = [mock.Mock(returncode=golden_code, stdout="golden result"),
                           mock.Mock(returncode=0, stdout="existing result")]
                with mock.patch.object(MULTIGOAL.subprocess, "run", side_effect=results) as run:
                    code = MULTIGOAL.run_check_script(existing, root,
                        {"GOLDEN_TEST_STAGE": "6"}, root / "check.log")
                self.assertEqual(code, golden_code)
                self.assertEqual(run.call_args_list[0].args[0][1],
                                 str(MULTIGOAL.ROOT / "scripts/check_golden_tests.sh"))
                self.assertEqual(run.call_count, 2 if golden_code == 0 else 1)


class ShellProfileConfigTests(unittest.TestCase):
    def test_stage_zero_is_inferred_and_can_resume(self) -> None:
        with mock.patch.object(sys, "argv", ["multigoal", "00-golden-tests.txt",
                                             "--resume-stage", "0", "--log-dir", "/unused"]):
            args = MULTIGOAL.parse_args()
        self.assertEqual(args.start_index, 0)
        self.assertEqual(args.resume_stage, 0)

    def test_original_and_partial_selections_keep_their_numbering(self) -> None:
        for prompts, options, expected in [
            (["01-functional-decoder.txt"], [], 1),
            (["goal.txt"], [], 1),
            (["06-full-model.txt"], ["--start-index", "6"], 6),
            (["goal.txt"], ["--start-index", "0"], 0),
        ]:
            with self.subTest(prompts=prompts, options=options):
                with mock.patch.object(sys, "argv", ["multigoal", *prompts, *options]):
                    self.assertEqual(MULTIGOAL.parse_args().start_index, expected)

    def test_negative_stage_numbers_are_rejected(self) -> None:
        for option in ("--start-index", "--resume-stage"):
            with self.subTest(option=option):
                with mock.patch.object(sys, "argv", ["multigoal", "goal.txt", option, "-1"]):
                    with self.assertRaises(SystemExit):
                        MULTIGOAL.parse_args()

    def test_parse_args_applies_the_default(self) -> None:
        with mock.patch.object(sys, "argv", ["multigoal", "goal.txt"]):
            args = MULTIGOAL.parse_args()

        self.assertEqual(args.config, ["shell_environment_policy.experimental_use_profile=false"])

    def test_shell_profile_loading_is_disabled_by_default(self) -> None:
        self.assertEqual(
            MULTIGOAL.with_shell_profile_default([]),
            ["shell_environment_policy.experimental_use_profile=false"],
        )

    def test_explicit_shell_profile_setting_is_preserved(self) -> None:
        config = ["model_reasoning_effort=high", "shell_environment_policy.experimental_use_profile=true"]

        self.assertEqual(MULTIGOAL.with_shell_profile_default(config), config)

    def test_unrelated_config_overrides_are_preserved(self) -> None:
        self.assertEqual(
            MULTIGOAL.with_shell_profile_default(["model_reasoning_effort=high"]),
            [
                "shell_environment_policy.experimental_use_profile=false",
                "model_reasoning_effort=high",
            ],
        )


class PersistentLogTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "workspace"
        self.repo.mkdir()
        dependency = self.root / "tt-autodebug"
        (dependency / ".codex-plugin").mkdir(parents=True)
        (dependency / ".codex-plugin/plugin.json").write_text('{"name":"tt-autodebug"}')
        for name in MULTIGOAL.DEPENDENCY_SKILLS:
            skill = dependency / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("fixture")
        self.codex_home = self.root / "codex-home"
        self.tmp_dir = self.root / "volatile-tmp"
        self.tmp_dir.mkdir()
        (self.tmp_dir / "scratch.txt").write_text("temporary data")
        self.prompt = self.root / "goal.txt"
        self.prompt.write_text("/goal Complete this model bringup stage.\n")
        self.run_id = "20260904T120000Z"
        self.log_dir = self.repo / "bringup" / "artifacts" / "multigoal-runs" / self.run_id
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(self.codex_home), "TMPDIR": str(self.tmp_dir), "TT_AUTODEBUG_ROOT": str(dependency)},
                clear=True,
            )
        )
        stack.enter_context(mock.patch.object(MULTIGOAL, "timestamp", return_value=self.run_id))
        stack.enter_context(redirect_stderr(io.StringIO()))
        stack.enter_context(mock.patch.object(MULTIGOAL, "verify_enabled_installations"))

    def run_main(self, *options: str) -> None:
        argv = ["multigoal", str(self.prompt), "--repo", str(self.repo), "--codex-bin", "unused", *options]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(MULTIGOAL.main(), 0)

    def test_default_logs_are_written_under_the_selected_repo(self) -> None:
        self.run_main("--dry-run")

        manifest = MULTIGOAL.read_manifest(self.log_dir / "manifest.txt")
        self.assertEqual(manifest["codex_home"], str(self.codex_home))
        self.assertEqual(manifest["stage_1_dry_run"], "true")
        self.assertFalse((self.codex_home / "multigoal-runs").exists())
        self.assertFalse((self.tmp_dir / "codex-multigoal-runs").exists())

    def test_explicit_codex_home_keeps_logs_in_the_workspace(self) -> None:
        explicit_home = self.root / "explicit-codex-home"
        self.run_main("--dry-run", "--codex-home", str(explicit_home))

        manifest = MULTIGOAL.read_manifest(self.log_dir / "manifest.txt")
        self.assertEqual(manifest["codex_home"], str(explicit_home))
        self.assertFalse((explicit_home / "multigoal-runs").exists())

    def test_environment_override_is_a_parent_directory(self) -> None:
        log_root = self.root / "custom-logs"
        with mock.patch.dict(os.environ, {"RUN_MULTIGOAL_LOG_DIR": str(log_root)}):
            self.run_main("--dry-run")

        self.assertTrue((log_root / self.run_id / "manifest.txt").is_file())
        self.assertFalse(self.log_dir.exists())

    def test_log_dir_overrides_environment_and_is_used_exactly(self) -> None:
        log_root = self.root / "custom-logs"
        explicit_dir = self.root / "chosen-run"
        with mock.patch.dict(os.environ, {"RUN_MULTIGOAL_LOG_DIR": str(log_root)}):
            self.run_main("--dry-run", "--log-dir", str(explicit_dir))

        self.assertTrue((explicit_dir / "manifest.txt").is_file())
        self.assertFalse(log_root.exists())
        self.assertFalse(self.log_dir.exists())

    def test_resume_after_temporary_storage_is_cleared(self) -> None:
        def interrupt_goal(*args, on_thread_started):
            on_thread_started("stage-4-thread")
            raise RuntimeError("simulated interruption")

        with mock.patch.object(MULTIGOAL, "AppServerClient"), mock.patch.object(
            MULTIGOAL, "execute_goal", side_effect=interrupt_goal
        ) as start_goal:
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.run_main("--start-index", "4")

            manifest = self.log_dir / "manifest.txt"
            original_manifest = manifest.read_text()
            self.assertEqual(MULTIGOAL.read_manifest(manifest)["stage_4_thread_id"], "stage-4-thread")
            shutil.rmtree(self.tmp_dir)

            with mock.patch.object(MULTIGOAL, "execute_resumed_goal", return_value=("complete", None)) as resume_goal:
                self.run_main("--start-index", "4", "--resume-stage", "4", "--log-dir", str(self.log_dir))

            start_goal.assert_called_once()
            resume_goal.assert_called_once()
            self.assertEqual(resume_goal.call_args.args[4], "stage-4-thread")
            self.assertTrue(manifest.read_text().startswith(original_manifest))
            self.assertEqual(MULTIGOAL.read_manifest(manifest)["stage_4_resume_1_terminal_status"], "complete")

    def test_stage_zero_resumes_the_original_thread(self) -> None:
        self.prompt = self.root / "00-golden-tests.txt"
        self.prompt.write_text("/goal Prepare cached test fixtures.\n")

        def interrupt_goal(*args, on_thread_started):
            on_thread_started("stage-0-thread")
            raise RuntimeError("simulated interruption")

        with mock.patch.object(MULTIGOAL, "AppServerClient"), mock.patch.object(
            MULTIGOAL, "execute_goal", side_effect=interrupt_goal
        ) as start_goal:
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.run_main()
            with mock.patch.object(MULTIGOAL, "execute_resumed_goal", return_value=("complete", None)) as resume_goal:
                self.run_main("--resume-stage", "0", "--log-dir", str(self.log_dir))
            start_goal.assert_called_once()
            resume_goal.assert_called_once()
            self.assertEqual(resume_goal.call_args.args[4], "stage-0-thread")
            manifest = MULTIGOAL.read_manifest(self.log_dir / "manifest.txt")
            self.assertEqual(manifest["stage_0_resume_1_terminal_status"], "complete")


def goal_event(status, thread="stage-thread", objective="original objective"):
    return {"method": "thread/goal/updated", "params": {"threadId": thread,
            "goal": {"status": status, "objective": objective}}}


def turn_event(status, turn="t0", code=None, thread="stage-thread"):
    return {"method": "turn/started" if status == "inProgress" else "turn/completed",
            "params": {"threadId": thread, "turn": {"id": turn, "status": status,
            "error": {"codexErrorInfo": code, "message": "fixture error"} if code else None}}}


class RecoveryClient:
    """Protocol peer with a fake clock; no model, credentials, devices, or sleeps."""
    def __init__(self, events=(), outcomes=()):
        self.events = list(events)
        self.outcomes = list(outcomes)
        self.requests = []
        self.now = 0.0
        self.turns = 0
        self.early_failure = False

    def read_event(self, log, on_event, timeout=None):
        if self.events:
            event = self.events.pop(0)
            on_event(event)
            return event
        assert timeout is not None, "unbounded wait in test"
        self.now += timeout
        raise TimeoutError()

    def request(self, method, params, log, on_event=None):
        self.requests.append((method, params))
        # Notifications may precede any JSON-RPC response.
        while self.events:
            event = self.events.pop(0)
            if on_event:
                on_event(event)
        if method == "thread/start":
            return {"thread": {"id": "stage-thread"}}
        if method == "thread/resume":
            return {"thread": {"id": "stage-thread", "turns": []}}
        if method == "thread/goal/set":
            if on_event:
                on_event(goal_event(params["status"]))
            return {"goal": {"status": params["status"], "objective": "original objective"}}
        if method == "turn/start":
            self.turns += 1
            turn = f"t{self.turns}"
            outcome = self.outcomes.pop(0)
            if outcome == "overload":
                events = [goal_event("blocked"), turn_event("failed", turn, "serverOverloaded")]
            else:
                events = [goal_event(outcome), turn_event("completed", turn)]
            if self.early_failure:
                for event in events:
                    on_event(event)
            else:
                self.events.extend(events)
            return {"turn": {"id": turn, "status": "inProgress"}}
        raise AssertionError(method)


class OverloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = pathlib.Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.log = self.stack.enter_context((self.root / "stage.jsonl").open("w"))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.stack.enter_context(mock.patch.object(MULTIGOAL.random, "uniform", return_value=1.0))
        self.stack.enter_context(mock.patch.object(MULTIGOAL, "input_items_for_resume",
            side_effect=lambda repo, message, objective: ([{"type": "text", "text": message}], [])))
        self.stack.enter_context(mock.patch.object(MULTIGOAL, "input_items_for_objective",
            return_value=([{"type": "text", "text": "original objective"}], [])))
        with mock.patch.object(sys, "argv", ["multigoal", "goal.txt", "--model", "gpt-5.6-sol"]):
            self.args = MULTIGOAL.parse_args()

    def state(self):
        return MULTIGOAL.StageState("stage-thread", "original objective", active_turns={"t0"})

    def run_recovery(self, client, state=None):
        with mock.patch.object(MULTIGOAL.time, "monotonic", side_effect=lambda: client.now):
            return MULTIGOAL.wait_with_overload_recovery(client, self.args, self.root, self.log, state or self.state())

    def records(self):
        return [json.loads(s) for s in (self.root / "stage.recovery.jsonl").read_text().splitlines()]

    def test_both_event_orders_retry_same_thread_then_complete(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                events = [goal_event("blocked"), turn_event("failed", code="serverOverloaded")]
                client = RecoveryClient(events[::-1] if reverse else events, ["complete"])
                self.assertEqual(self.run_recovery(client), "complete")
                starts = [p for m, p in client.requests if m == "turn/start"]
                self.assertEqual(len(starts), 1)
                self.assertEqual(starts[0]["threadId"], "stage-thread")
                self.assertEqual(starts[0]["model"], "gpt-5.6-sol")
                self.assertNotIn("thread/start", [m for m, p in client.requests])
                self.assertEqual(client.now, 30)
        self.assertTrue(any(r["event"] == "waiting" and r["retry_at_utc"] for r in self.records()))

    def test_real_blocker_and_noncapacity_failures_do_not_retry(self):
        for code in (None, "usageLimitExceeded", "unauthorized", "internalServerError", "unknown"):
            with self.subTest(code=code):
                terminal = "failed" if code else "completed"
                client = RecoveryClient([goal_event("blocked"), turn_event(terminal, code=code)])
                self.assertEqual(self.run_recovery(client), "turnFailed" if code else "blocked")
                self.assertEqual(client.requests, [])

    def test_foreign_thread_failure_is_ignored(self):
        client = RecoveryClient([turn_event("failed", code="serverOverloaded", thread="child"),
                                 goal_event("complete"), turn_event("completed")])
        self.assertEqual(self.run_recovery(client), "complete")
        self.assertEqual(client.requests, [])

    def test_success_waits_for_final_turn_not_only_goal(self):
        state = self.state()
        MULTIGOAL.update_stage_state(state, goal_event("complete"))
        self.assertIsNone(state.terminal_status)
        MULTIGOAL.update_stage_state(state, turn_event("completed"))
        self.assertEqual(state.terminal_status, "complete")

    def test_missing_turn_completion_is_bounded_and_not_retried(self):
        client = RecoveryClient([goal_event("blocked")])
        state = self.state()
        self.assertEqual(self.run_recovery(client, state), "turnFailed")
        self.assertEqual(client.now, MULTIGOAL.TURN_DRAIN_TIMEOUT)
        self.assertIn("outcome unknown", state.last_turn_error)
        self.assertEqual(client.requests, [])

    def test_pause_limits_and_interrupt_cancel_backoff(self):
        for event in (goal_event("paused"), goal_event("usageLimited"), goal_event("budgetLimited"),
                      turn_event("interrupted")):
            with self.subTest(event=event):
                client = RecoveryClient([goal_event("blocked"), turn_event("failed", code="serverOverloaded"), event])
                self.assertIn(self.run_recovery(client), {"paused", "usageLimited", "budgetLimited", "interrupted"})
                self.assertEqual(client.requests, [])

    def test_disabled_retry_and_exhaustion_preserve_error(self):
        for budget in (0, 90):
            with self.subTest(budget=budget):
                self.args.overload_retry_budget = budget
                client = RecoveryClient([goal_event("blocked"), turn_event("failed", code="serverOverloaded")],
                                        ["overload"] * 3)
                state = self.state()
                self.assertEqual(self.run_recovery(client, state), "overloadRetryExhausted")
                self.assertEqual(client.now, budget)
                self.assertEqual(client.turns, 0 if budget == 0 else 2)
                self.assertTrue(MULTIGOAL.is_model_overload(state.last_turn_error))

    def test_fresh_and_resumed_execution_recover_even_before_start_response(self):
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                client = RecoveryClient(outcomes=["overload", "complete"])
                client.early_failure = True
                log = self.root / f"execution-{resumed}.jsonl"
                with mock.patch.object(MULTIGOAL.time, "monotonic", side_effect=lambda: client.now):
                    if resumed:
                        result = MULTIGOAL.execute_resumed_goal(client, self.args, self.root,
                            "original objective", "stage-thread", log, self.root / "goal.txt")
                    else:
                        result = MULTIGOAL.execute_goal(client, self.args, self.root, "original objective", log)
                self.assertEqual(result[0], "complete")
                self.assertEqual(client.turns, 2)

    def test_keyboard_interrupt_and_connection_loss_do_not_restart(self):
        for error in (KeyboardInterrupt(), RuntimeError("app-server closed stdout")):
            client = RecoveryClient([goal_event("blocked"), turn_event("failed", code="serverOverloaded")])
            original_read = client.read_event
            def read(*args, **kwargs):
                if client.events:
                    return original_read(*args, **kwargs)
                raise error
            client.read_event = read
            with self.assertRaises(type(error)):
                self.run_recovery(client)
            self.assertEqual(client.requests, [])


class RecoveryPipeTests(unittest.TestCase):
    def test_real_pipe_overload_recovery_gate_and_next_stage(self):
        # Exercise actual buffered stdout, JSON-RPC/event interleaving, backoff,
        # stage completion, gate dispatch and next-stage sequencing in main().
        peer = r'''
import json, sys
threads = {}; current = None; attempts = 0
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    req = json.loads(line); method = req.get('method'); p = req.get('params', {})
    if 'id' not in req: continue
    result = {}
    if method == 'thread/start':
        current = 'thread-' + str(len(threads) + 1)
        threads[current] = {}; result = {'thread': {'id': current}}
    elif method == 'thread/goal/set':
        current = p['threadId']; threads[current].update(p)
        result = {'goal': threads[current]}
    elif method == 'turn/start':
        attempts += 1
        result = {'turn': {'id': 'turn-' + str(attempts), 'status': 'inProgress'}}
    send({'id': req['id'], 'result': result})
    if method == 'thread/goal/set' and p['status'] == 'active':
        fail = attempts == 1
        goal = dict(threads[current], status='blocked' if fail else 'complete')
        send({'method': 'thread/goal/updated', 'params': {'threadId': current, 'goal': goal}})
        send({'method': 'turn/completed', 'params': {'threadId': current, 'turn': {
            'id': 'turn-' + str(attempts), 'status': 'failed' if fail else 'completed',
            'error': {'codexErrorInfo': 'serverOverloaded', 'message': 'capacity'} if fail else None}}})
'''
        real_popen = MULTIGOAL.subprocess.Popen
        def popen(*args, **kwargs):
            return real_popen([sys.executable, '-u', '-c', peer], **kwargs)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = pathlib.Path(directory)
            prompts = [root / '03-stage.txt', root / '04-next.txt']
            for prompt in prompts:
                prompt.write_text('/goal Do the stage.\n')
            stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(MULTIGOAL.subprocess, 'Popen', side_effect=popen))
            stack.enter_context(mock.patch.object(MULTIGOAL, 'environment', return_value=os.environ.copy()))
            stack.enter_context(mock.patch.object(MULTIGOAL, 'dependency_root', return_value=root))
            stack.enter_context(mock.patch.object(MULTIGOAL, 'verify_enabled_installations'))
            stack.enter_context(mock.patch.object(MULTIGOAL, 'input_items_for_objective', return_value=([], [])))
            stack.enter_context(mock.patch.object(MULTIGOAL, 'input_items_for_resume', return_value=([], [])))
            gates = stack.enter_context(mock.patch.object(MULTIGOAL, 'run_stage_checks', return_value='pass'))
            stack.enter_context(mock.patch.object(sys, 'argv', ['multigoal', *map(str, prompts),
                '--start-index', '3', '--repo', str(root), '--codex-home', str(root / 'home'),
                '--log-dir', str(root / 'logs'), '--codex-bin', sys.executable,
                '--overload-retry-budget', '1']))
            self.assertEqual(MULTIGOAL.main(), 0)
            self.assertEqual([c.args[5] for c in gates.call_args_list], [3, 4])
            manifest = MULTIGOAL.read_manifest(root / 'logs/manifest.txt')
            self.assertEqual(manifest['stage_3_terminal_status'], 'complete')
            self.assertEqual(manifest['stage_4_terminal_status'], 'complete')
            events = [json.loads(s) for s in (root / 'logs/03-03-stage.jsonl').read_text().splitlines()]
            requests = [e['message'] for e in events if e['direction'] == 'send']
            starts = [r['params'] for r in requests if r.get('method') == 'turn/start']
            self.assertEqual(len(starts), 2)
            self.assertEqual({s['threadId'] for s in starts}, {'thread-1'})
            self.assertEqual(sum(r.get('method') == 'thread/start' for r in requests), 1)


if __name__ == "__main__":
    unittest.main()
