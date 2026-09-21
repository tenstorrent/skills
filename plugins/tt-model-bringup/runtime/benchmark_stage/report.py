"""Write a compact report from upstream results without redefining scorers."""
from __future__ import annotations
import json
from pathlib import Path

from benchmark_stage.evidence import metric_score


def write_report(output, config, summary):
    output = Path(output)
    manifest = json.loads(Path(config['manifest']).read_text())
    lines = [f"# Benchmark: {config['model']}", '',
             f"Status: {summary['status']}. Accuracy concurrency: 32. Subset: `{manifest['manifest_sha256']}`.", '',
             '| Task | Samples / full | Metric | Subset % | Published % | Difference pp |',
             '|---|---:|---|---:|---:|---:|']
    notes = []
    for task in config['tasks']:
        result_path = output / task / 'results.json'
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text())
        group = manifest['groups'][task]
        rows = result.get('groups', {}).get(task, result.get('results', {}).get(task, {}))
        rows = dict(rows)
        if 'ifeval_mean_four' in config.get('references', {}).get(task, {}):
            rows['ifeval_mean_four'] = metric_score(result, task, 'ifeval_mean_four') / 100
        for metric in config.get('references', {}).get(task, {}):
            if metric.startswith('subject_macro:'):
                rows[metric] = metric_score(result, task, metric, expected_children=group['tasks']) / 100
        for metric, score in rows.items():
            if not isinstance(score, (int, float)) or '_stderr' in metric or (',' not in metric and metric != 'ifeval_mean_four'):
                continue
            ref = config.get('references', {}).get(task, {}).get(metric)
            published = f"{ref['score']:.2f}" if ref else '—'
            delta = f"{100 * score - ref['score']:+.2f}" if ref else '—'
            lines.append(f"| {task} | {group['sample_count']} / {group['population']} | {metric} | {100 * score:.2f} | {published} | {delta} |")
        finish = result['benchmark_stage']['finish_reasons']
        notes += ['', f"{task}: finish reasons `{json.dumps(finish)}`; {result['benchmark_stage']['elapsed_seconds']:.1f} seconds.", '']
    lines += notes
    lines += ['', '| Concurrency | ISL / OSL | TTFT ms | TPOT ms | Decode tokens/s/user | Output tokens/s |',
              '|---:|---:|---:|---:|---:|---:|']
    for batch, row in summary.get('performance', {}).items():
        tpot = row.get('mean_tpot_ms', 0)
        lines.append(f"| {batch} | {row['requested_input_tokens']} / {row['requested_output_tokens']} | {row.get('mean_ttft_ms', 0):.2f} | {tpot:.2f} | {1000/tpot if tpot else 0:.2f} | {row.get('output_throughput', 0):.2f} |")
    lines += ['', '| Concurrency | Latency | Mean ms | Median ms | p95 ms | p99 ms |',
              '|---:|---|---:|---:|---:|---:|']
    for batch, row in summary.get('performance', {}).items():
        for metric in ('ttft', 'tpot', 'itl', 'e2el'):
            values = [row.get(f'{stat}_{metric}_ms') for stat in ('mean', 'median', 'p95', 'p99')]
            formatted = [f'{v:.2f}' if isinstance(v, (int, float)) else '—' for v in values]
            lines.append(f"| {batch} | {metric.upper()} | " + ' | '.join(formatted) + ' |')
    lines += ['', 'Published results cover the full dataset. These are fixed subsets; score differences can reflect sampling, protocol or implementation differences. See the separate accuracy review for uncertainty and comparability.', '']
    if summary.get('error'):
        lines += [f"Error: {summary['error']}", '']
    (output / 'REPORT.md').write_text('\n'.join(lines))
