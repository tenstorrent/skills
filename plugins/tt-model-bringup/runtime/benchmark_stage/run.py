"""Bound a complete client-side benchmark stage and retain failed-run evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from benchmark_stage.answers import task_statistics
from benchmark_stage.evidence import validate_performance, validate_server
from benchmark_stage.plan import (INPUT_TOKENS, LONG_CONTEXT_INPUT_TOKENS, batch_plan,
                                  lone_user_profiles, perf_requests, validate_sample_reduction)
from benchmark_stage.roofline import load_roofline
from benchmark_stage.subsets import digest


def command(argv, log, deadline, *, terminate_grace_seconds=10):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('benchmark stage exceeded its wall-clock budget')
    with Path(log).open('w') as stream:
        stream.write(json.dumps(argv) + '\n')
        stream.flush()
        process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=remaining)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # A terminated group leader does not imply its children have exited.
            grace_deadline = time.monotonic() + terminate_grace_seconds
            while True:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS can return EPERM briefly while a terminated leader
                    # is being reaped. Keep the bounded cleanup attempt active.
                    pass
                if time.monotonic() >= grace_deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(0.05)
            process.wait()
            raise
    if code:
        raise RuntimeError(f'command exited {code}; inspect {log}')


def run(*, config_path, output, batch_sizes=None):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text())
    if batch_sizes is not None:
        config['batch_sizes'] = list(batch_sizes)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    (output / 'run_config.json').write_text(json.dumps(config, indent=2) + '\n')
    source_manifest = Path(config['manifest'])
    if source_manifest.is_file():
        frozen_manifest = output / 'manifest.json'
        frozen_manifest.write_bytes(source_manifest.read_bytes())
        config['source_manifest'] = str(source_manifest)
        config['manifest'] = str(frozen_manifest)
        (output / 'run_config.json').write_text(json.dumps(config, indent=2) + '\n')
    plan = batch_plan(config)
    validate_sample_reduction(config)
    client_budget, setup_budget = plan['client_budget_seconds'], plan['setup_budget_seconds']
    largest = plan['accuracy_concurrency']
    osl = config.get('output_tokens', 128)
    # Two clocks. Setup (server launch/reload, compilation, warmup) has its own
    # bound; everything else, including reporting, is the client clock.
    clock = {'setup': 0.0}
    summary = {'schema_version': 2, 'status': 'running', 'accuracy': {}, 'performance': {},
               'lone_user': {}, 'answer_statistics': {}, 'setup_seconds_by_batch': {}, **plan}

    def client_elapsed():
        return time.monotonic() - started - clock['setup']

    def client_deadline():
        return time.monotonic() + client_budget - client_elapsed()

    class setup_phase:
        def __init__(self, batch):
            self.batch = str(batch)

        def __enter__(self):
            self.start = time.monotonic()
            return time.monotonic() + setup_budget - clock['setup']

        def __exit__(self, *exc):
            spent = time.monotonic() - self.start
            clock['setup'] += spent
            by_batch = summary['setup_seconds_by_batch']
            by_batch[self.batch] = by_batch.get(self.batch, 0.0) + spent
            return False

    def interrupted(signum, frame):
        raise InterruptedError(f'benchmark interrupted by signal {signum}')
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        server_command = config.get('performance_server_command')
        if not isinstance(server_command, list) or not server_command or not all(isinstance(arg, str) for arg in server_command):
            raise ValueError('performance_server_command is required to select and record one server per batch size')
        roofline_command = config.get('roofline_command')
        if not isinstance(roofline_command, list) or not roofline_command or not all(
                isinstance(arg, str) and arg.strip() for arg in roofline_command):
            raise ValueError('roofline_command is required to collect full-phase prefill/decode accounting')

        def phase_accounting(capacity, action, deadline):
            command([*roofline_command, '--run-dir', str(output), '--concurrency', str(capacity),
                     '--action', action], output / f'roofline-b{capacity}-{action}.log', deadline)
            if action == 'collect':
                load_roofline(output, required=(str(capacity),))

        def select_server(capacity, baseline=None):
            with setup_phase(capacity) as deadline:
                server_path = output / f'perf-b{capacity}-server.json'
                command([*server_command, '--max-num-seqs', str(capacity),
                         '--base-url', config['base_url'], '--output', str(server_path)],
                        output / f'perf-b{capacity}-server.log', deadline)
                server = json.loads(server_path.read_text())
                validate_server(server, capacity, config['model'], config['base_url'],
                                baseline=baseline, output=output)
                phase_accounting(capacity, 'check', deadline)
            return server

        def bench(name, *, concurrency, requests, input_tokens, seed, deadline):
            argv = [*config.get('benchmark_command', [config.get('vllm_cli', 'vllm'), 'bench', 'serve']), '--backend', 'vllm',
                    '--model', config['model'], '--base-url', config['base_url'],
                    '--endpoint', '/v1/completions', '--dataset-name', 'random',
                    '--random-input-len', str(input_tokens), '--random-output-len', str(osl),
                    '--random-range-ratio', '0.0', '--num-prompts', str(requests),
                    '--max-concurrency', str(concurrency), '--request-rate', 'inf', '--ignore-eos',
                    '--temperature', '0', '--seed', str(seed),
                    '--percentile-metrics', 'ttft,tpot,itl,e2el', '--metric-percentiles', '50,95,99',
                    '--save-result', '--save-detailed', '--result-dir', str(output),
                    '--result-filename', f'{name}.json']
            command(argv, output / f'{name}.log', deadline)
            raw = json.loads((output / f'{name}.json').read_text())
            validate_performance(raw, requests, osl, input_tokens)
            if raw.get('model_id') != config['model'] or raw.get('max_concurrency') != concurrency:
                raise ValueError(f'{name}: raw performance model or concurrency mismatch')
            return raw

        def row(raw, server, *, concurrency, requests, input_tokens):
            return {
                'concurrency': concurrency, 'requested_input_tokens': input_tokens,
                'server_max_num_seqs': server['max_num_seqs'], 'server_identity_sha256': digest(server),
                'server_prefix_caching': server['prefix_caching'],
                'requested_output_tokens': osl, 'requests': requests, 'completed': raw['completed'],
                **{k: v for k, v in raw.items() if k.startswith(('mean_', 'median_', 'p95_', 'p99_')) or k in
                   ('duration', 'request_throughput', 'output_throughput', 'total_token_throughput',
                    'total_input_tokens', 'total_output_tokens')},
            }

        def warmup(concurrency, server):
            with setup_phase(concurrency) as deadline:
                bench(f'perf-b{concurrency}-warmup', concurrency=concurrency, requests=concurrency,
                      input_tokens=INPUT_TOKENS, seed=4101 + concurrency, deadline=deadline)

        baseline_server = select_server(largest)
        summary['accuracy_prefix_caching'] = baseline_server['prefix_caching']
        # Warm the largest server before accuracy so compilation never lands on the client clock.
        warmup(largest, baseline_server)
        execution = config.get('accuracy_execution', 'sequential')
        if execution not in ('sequential', 'shared'):
            raise ValueError('accuracy_execution must be sequential or shared')
        summary['accuracy_execution'] = execution
        manifest = json.loads(Path(config['manifest']).read_text())
        batches = [config['tasks']] if execution == 'shared' else [[task] for task in config['tasks']]
        for batch in batches:
            generation = config.get('generation', {}).get(batch[0], {})
            if any(config.get('generation', {}).get(task, {}) != generation for task in batch):
                raise ValueError('shared accuracy requires identical generation overrides for all groups')
            task_output = output if execution == 'shared' else output / batch[0]
            argv = [sys.executable, '-m', 'benchmark_stage', 'evaluate', '--model', config['model'],
                    '--base-url', config['base_url'], '--manifest', config['manifest'],
                    '--task', ','.join(batch), '--output', str(task_output),
                    '--generation', json.dumps(generation), '--concurrency', str(largest)]
            if execution == 'shared':
                argv.append('--shared')
            command(argv, output / ('accuracy-shared.log' if execution == 'shared' else f'{batch[0]}.log'), client_deadline())
            for task in batch:
                result = json.loads((output / task / 'results.json').read_text())
                summary['accuracy'][task] = result['benchmark_stage']
                try:
                    summary['answer_statistics'][task] = task_statistics(output, config, manifest, task)
                except (ValueError, KeyError, OSError) as exc:
                    summary.setdefault('answer_statistics_errors', {})[task] = f'{type(exc).__name__}: {exc}'
        for concurrency in plan['measured_batch_sizes']:
            if concurrency == largest:
                server = baseline_server
            else:
                server = select_server(concurrency, baseline_server)
                warmup(concurrency, server)
            requests = perf_requests(concurrency)
            raw = bench(f'perf-b{concurrency}', concurrency=concurrency, requests=requests,
                        input_tokens=INPUT_TOKENS, seed=4100 + concurrency, deadline=client_deadline())
            summary['performance'][str(concurrency)] = row(raw, server, concurrency=concurrency,
                                                           requests=requests, input_tokens=INPUT_TOKENS)
            # Flush and retain this server's timings before a profile switch can
            # destroy its in-memory counters or task-local logs.
            phase_accounting(concurrency, 'collect', client_deadline())
            if concurrency == largest:
                # Lone-user cost on the server CI uses: FUNCTIONAL 128/128 and long context.
                for profile in lone_user_profiles(largest, server['max_model_len'], osl,
                                                  config.get('long_context_input_tokens', LONG_CONTEXT_INPUT_TOKENS)):
                    with setup_phase(largest) as deadline:
                        bench(profile['file'] + '-warmup', concurrency=1, requests=1,
                              input_tokens=profile['input_tokens'], seed=4200 + profile['input_tokens'], deadline=deadline)
                    raw = bench(profile['file'], concurrency=1, requests=profile['requests'],
                                input_tokens=profile['input_tokens'], seed=4300 + profile['input_tokens'],
                                deadline=client_deadline())
                    summary['lone_user'][profile['name']] = dict(
                        row(raw, server, concurrency=1, requests=profile['requests'], input_tokens=profile['input_tokens']),
                        file=profile['file'] + '.json')
        load_roofline(output, required=tuple(str(c) for c in plan['measured_batch_sizes']))
        summary['status'] = 'completed'
    except BaseException as exc:
        summary['status'] = 'failed'
        summary['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        from benchmark_stage.report import write_report
        try:
            write_report(output, config, summary)
        except Exception as report_error:
            summary['report_error'] = f'{type(report_error).__name__}: {report_error}'
            if summary['status'] == 'completed':
                summary['status'] = 'failed'
                summary['error'] = 'Report generation failed: ' + summary['report_error']
        summary['elapsed_seconds'] = time.monotonic() - started
        summary['setup_seconds'] = clock['setup']
        summary['client_seconds'] = summary['elapsed_seconds'] - clock['setup']
        if summary['status'] == 'completed' and summary['client_seconds'] >= client_budget:
            summary['status'] = 'failed'
            summary['error'] = 'Client stage including reporting exceeded its configured wall-clock budget'
        if summary['status'] == 'completed' and summary['setup_seconds'] >= setup_budget:
            summary['status'] = 'failed'
            summary['error'] = 'Server setup (launch, compile, warmup) exceeded its configured budget'
        report_path = output / 'REPORT.md'
        if summary['status'] == 'failed' and report_path.exists():
            report_path.write_text(report_path.read_text().replace('Status: completed.', 'Status: failed.', 1))
        with (output / 'REPORT.md').open('a') as report:
            if summary['status'] == 'failed':
                report.write(f"\nFinal status: failed. {summary.get('error', 'Benchmark failed')}\n")
            report.write(f"\nClient-clock wall time: {summary['client_seconds']:.1f} of {client_budget:.0f} seconds. "
                         f"Server setup (launch, compile, warmup): {summary['setup_seconds']:.1f} of {setup_budget:.0f} seconds. "
                         f"Total: {summary['elapsed_seconds']:.1f} seconds.\n")
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        signal.signal(signal.SIGTERM, previous_term)
    if summary['status'] != 'completed':
        raise RuntimeError(summary.get('error', 'benchmark failed'))
    return summary
