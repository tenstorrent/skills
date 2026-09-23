"""Write the final results tables from upstream scores and measured performance."""
from __future__ import annotations
import json
from pathlib import Path

from benchmark_stage.evidence import benchmark_rows
from benchmark_stage.roofline import load_roofline


def cell(value):
    return str(value).replace('|', r'\|').replace('\n', ' ')


def write_report(output, config, summary):
    output = Path(output)
    if set(config.get('references', {})) - set(config['tasks']) or set(config.get('metrics', {})) - set(config['tasks']):
        raise ValueError('report metrics/references name a task that was not run')
    manifest = json.loads(Path(config['manifest']).read_text())
    roofline_error = None
    try:
        roofline = load_roofline(output, required=('1', '32') if summary['status'] == 'completed' else ())
    except (ValueError, OSError) as exc:
        if summary['status'] == 'completed':
            raise
        # Preserve the scores and serving measurements even when accounting
        # fails; the run remains incomplete and no invalid percentage is shown.
        roofline, roofline_error = {}, str(exc)
    lines = [f"# Benchmark: {config['model']}", '',
             f"Status: {summary['status']}. Accuracy concurrency: 32.", '',
             '| Benchmark | Samples / full | Metric | Subset % | Published full % | Difference pp | Source |',
             '|---|---:|---|---:|---:|---:|---|']
    notes = []
    for task in config['tasks']:
        result_path = output / task / 'results.json'
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text())
        group = manifest['groups'][task]
        for metric, score, ref in benchmark_rows(config, manifest, task, result):
            published = f"{ref['score']:.2f}" if ref else '—'
            delta = f"{score - ref['score']:+.2f}" if ref else '—'
            source = f"[reference]({ref['source_url']})" if ref else 'Unavailable'
            lines.append(f"| {cell(task)} | {group['sample_count']} / {group['population']} | {cell(metric)} | {score:.2f} | {published} | {delta} | {source} |")
            if ref and ref.get('protocol_notes'):
                notes.append(f"- {task}, {metric}: {ref['protocol_notes']}")
    lines += ['', '| Profile | Concurrent requests | Server slots | ISL / OSL | TTFT ms | TPOT ms | Decode tokens/s/user | Output tokens/s | Prefill FLOP roofline % (est.) | Decode DRAM roofline % (est.) |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for batch, row in sorted(summary.get('performance', {}).items(), key=lambda item: int(item[0])):
        tpot = row.get('mean_tpot_ms', 0)
        utilization = [f"{roofline[batch][phase]['percent']:.2f}" if roofline.get(batch, {}).get(phase) else '—'
                       for phase in ('prefill', 'decode')]
        capacity = row.get('server_max_num_seqs', 'Unrecorded')
        profile = 'Single user' if batch == '1' and capacity == 1 else '32 users' if batch == '32' and capacity == 32 else 'Serving'
        lines.append(f"| {profile} | {batch} | {capacity} | {row['requested_input_tokens']} / {row['requested_output_tokens']} | {row.get('mean_ttft_ms', 0):.2f} | {tpot:.2f} | {1000/tpot if tpot else 0:.2f} | {row.get('output_throughput', 0):.2f} | " + ' | '.join(utilization) + ' |')
    lines += ['', 'Scores use fixed subsets; published figures cover the full dataset. Missing references are shown as unavailable. The bringup owner decides whether these results meet their needs.', '',
              'Roofline estimates divide modeled work by full-phase elapsed wall time and the participating hardware’s peak rate. Both phases are required for both serving profiles; — indicates incomplete accounting. HTTP concurrency is not a fixed device batch size.', '']
    if roofline_error:
        lines += [f'Phase accounting error: {cell(roofline_error)}', '']
    if notes:
        lines += ['Reference protocol notes:', *notes, '']
    lines += ['## Run details', '', f"Subset: `{manifest['manifest_sha256']}`. Configuration: [run_config.json](run_config.json).", '',
              '| Benchmark | Responses | Token-limited | Empty final at token limit | Wall seconds |',
              '|---|---:|---:|---:|---:|']
    for task, meta in summary.get('accuracy', {}).items():
        count = meta['responses']
        limited = meta.get('finish_reasons', {}).get('length', 0)
        lines.append(f"| [{cell(task)}]({task}/results.json) | {count} | {limited} ({100 * limited / count if count else 0:.1f}%) | {meta.get('empty_final_length_responses', 0)} | {meta.get('elapsed_seconds', 0):.1f} |")
    if summary.get('accuracy_execution') == 'shared':
        lines += ['', 'Accuracy tasks share one request pool; their wall times refer to the same interval.']
    lines += ['', '| Concurrency | Completed / requested | Wall seconds | Requests/s |',
              '|---:|---:|---:|---:|']
    for batch, row in sorted(summary.get('performance', {}).items(), key=lambda item: int(item[0])):
        lines.append(f"| [{batch}](perf-b{batch}.json) | {row['completed']} / {row['requests']} | {row['duration']:.2f} | {row['request_throughput']:.2f} |")
    lines += ['', '| Concurrency | Latency | Mean ms | Median ms | p95 ms | p99 ms |',
              '|---:|---|---:|---:|---:|---:|']
    for batch, row in sorted(summary.get('performance', {}).items(), key=lambda item: int(item[0])):
        for metric in ('ttft', 'tpot', 'itl', 'e2el'):
            values = [row.get(f'{stat}_{metric}_ms') for stat in ('mean', 'median', 'p95', 'p99')]
            formatted = [f'{v:.2f}' if isinstance(v, (int, float)) else '—' for v in values]
            lines.append(f"| {batch} | {metric.upper()} | " + ' | '.join(formatted) + ' |')
    for batch, row in sorted(summary.get('performance', {}).items(), key=lambda item: int(item[0])):
        if row.get('server_identity_sha256'):
            lines += ['', f"Server configuration for {batch} concurrent request(s): [record](perf-b{batch}-server.json)."]
    if roofline:
        lines += ['', 'Roofline inputs: [roofline.json](roofline.json).', '']
        for batch, row in roofline.items():
            for phase in ('prefill', 'decode'):
                entry = row.get(phase)
                if entry:
                    lines.append(f"- Concurrency {batch}, {phase}: {entry['seconds']:.6g} s. {entry['work_method']} Peak: {entry['peak_source']}. Timing: {entry['timing_method']}. [Evidence]({entry['evidence']}).")
    if summary.get('error'):
        lines += ['', f"Error: {summary['error']}"]
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
