"""Write the final results tables from upstream scores and measured performance."""
from __future__ import annotations
import json
from pathlib import Path

from benchmark_stage.answers import advisories as answer_advisories
from benchmark_stage.evidence import benchmark_rows
from benchmark_stage.roofline import load_roofline


def cell(value):
    return str(value).replace('|', r'\|').replace('\n', ' ')


LONE_LABELS = {'functional': '1 user on largest server (FUNCTIONAL 128/128)',
               'long_context': '1 user on largest server (long context)'}


def perf_rows(summary):
    rows = sorted(summary.get('performance', {}).items(), key=lambda item: int(item[0]))
    return rows + [(name, summary['lone_user'][name]) for name in LONE_LABELS if name in summary.get('lone_user', {})]


def write_report(output, config, summary):
    output = Path(output)
    if set(config.get('references', {})) - set(config['tasks']) or set(config.get('metrics', {})) - set(config['tasks']):
        raise ValueError('report metrics/references name a task that was not run')
    manifest = json.loads(Path(config['manifest']).read_text())
    roofline_error = None
    measured = summary.get('measured_batch_sizes') or []
    try:
        roofline = load_roofline(output, required=tuple(str(b) for b in measured)
                                 if summary['status'] == 'completed' else ())
    except (ValueError, OSError) as exc:
        if summary['status'] == 'completed':
            raise
        # Preserve the scores and serving measurements even when accounting
        # fails; the run remains incomplete and no invalid percentage is shown.
        roofline, roofline_error = {}, str(exc)
    lines = [f"# Benchmark: {config['model']}", '',
             f"Status: {summary['status']}. Accuracy concurrency: {summary.get('accuracy_concurrency', 'unrecorded')} "
             f"(largest measured batch; server prefix caching: {summary.get('accuracy_prefix_caching', 'unrecorded')}).", '',
             '| Benchmark | Samples / full | Metric | Subset % | Published full % | Difference pp | Source |',
             '|---|---:|---|---:|---:|---:|---|']
    notes, notes_quality = [], []
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
    for batch, row in perf_rows(summary):
        tpot = row.get('mean_tpot_ms', 0)
        utilization = [f"{roofline[batch][phase]['percent']:.2f}" if roofline.get(batch, {}).get(phase) else '—'
                       for phase in ('prefill', 'decode')]
        capacity = row.get('server_max_num_seqs', 'Unrecorded')
        if batch in LONE_LABELS:
            profile = LONE_LABELS[batch]
        else:
            profile = 'Single user' if batch == '1' and capacity == 1 else f'{batch} users' if str(capacity) == batch else 'Serving'
        lines.append(f"| {profile} | {row['concurrency']} | {capacity} | {row['requested_input_tokens']} / {row['requested_output_tokens']} | {row.get('mean_ttft_ms', 0):.2f} | {tpot:.2f} | {1000/tpot if tpot else 0:.2f} | {row.get('output_throughput', 0):.2f} | " + ' | '.join(utilization) + ' |')
    for batch, reason in sorted(summary.get('skipped_batch_sizes', {}).items(), key=lambda item: int(item[0])):
        lines.append(f"| {batch} users | — | — | not measured: {cell(reason)} | — | — | — | — | — | — |")
    lines += ['', 'Scores use fixed subsets; published figures cover the full dataset. Missing references are shown as unavailable. The bringup owner decides whether these results meet their needs.', '',
              'Each batch profile sets server max-num-seqs and request concurrency to the batch size. The two one-user rows run on the largest server: the FUNCTIONAL 128/128 reference and the long-context cost a lone user pays on that server.', '',
              'Roofline estimates divide modeled work by full-phase elapsed wall time and the participating hardware’s peak rate. Both phases are required for every batch profile; — indicates incomplete accounting. HTTP concurrency is not a fixed device batch size.', '']
    stats = summary.get('answer_statistics', {})
    if stats or summary.get('answer_statistics_errors'):
        lines += ['## Answer quality', '',
                  '| Benchmark | Extracted by filter | Blank at cap | Answer tokens median / p90 / max | Output cap | Recommended cap |',
                  '|---|---|---:|---:|---:|---:|']
        for task, row in stats.items():
            extraction = '; '.join(f"{cell(name)} {e['extracted']}/{e['answers']} ({100 * e['share']:.1f}%)"
                                   for name, e in row['extraction'].items())
            tokens = row['answer_tokens']
            lines.append(f"| {cell(task)} | {extraction} | {row['blank_at_cap']} ({100 * row['blank_at_cap_share']:.1f}%) | "
                         f"{tokens['median']} / {tokens['p90']} / {tokens['max']} | {row['output_cap']} | {row['recommended_output_cap']} |")
            for advisory in answer_advisories(task, row):
                notes_quality.append(f'- {advisory}')
        for task, error in summary.get('answer_statistics_errors', {}).items():
            notes_quality.append(f'- {task}: statistics unavailable: {cell(error)}')
        lines += ['', 'Targets: extraction at least 95% per filter; blank answers at the cap at most 5%. Recommended cap is p90 × 1.5 rounded up to a power-of-two K, never below the current cap.', '']
        if notes_quality:
            lines += notes_quality + ['']
    for task, row in config.get('sample_reduction', {}).items():
        lines.append(f"- Sample reduction, {cell(task)}: {row.get('from')} → {row.get('to')}. {cell(row.get('reason', ''))}")
    if config.get('sample_reduction'):
        lines.append('')
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
    lines += ['', '| Profile | Completed / requested | Wall seconds | Requests/s |',
              '|---|---:|---:|---:|']
    for batch, row in perf_rows(summary):
        lines.append(f"| [{batch}]({row.get('file', f'perf-b{batch}.json')}) | {row['completed']} / {row['requests']} | {row['duration']:.2f} | {row['request_throughput']:.2f} |")
    lines += ['', '| Profile | Latency | Mean ms | Median ms | p95 ms | p99 ms |',
              '|---|---|---:|---:|---:|---:|']
    for batch, row in perf_rows(summary):
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
