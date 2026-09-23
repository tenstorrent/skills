"""Report modeled roofline utilization over measured full-phase wall time."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmark_stage.evidence import finite_number


def load_roofline(output):
    """Validate optional server accounting tied to the measured performance files."""
    output = Path(output)
    path = output / 'roofline.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if set(data) - {'1', '32'}:
        raise ValueError('roofline accounting must use concurrency 1 or 32')
    for concurrency, row in data.items():
        raw_path = output / f'perf-b{concurrency}.json'
        if row.get('performance_sha256') != hashlib.sha256(raw_path.read_bytes()).hexdigest():
            raise ValueError('roofline accounting does not match the measured performance run')
        duration = json.loads(raw_path.read_text())['duration']
        for phase, work, peak in (
                ('prefill', 'flops', 'peak_flops_per_second'),
                ('decode', 'dram_bytes', 'peak_dram_bytes_per_second')):
            entry = row.get(phase)
            if entry is None:
                continue
            for key in (work, peak, 'seconds'):
                if not finite_number(entry.get(key)) or entry[key] <= 0:
                    raise ValueError(f'{phase}: invalid roofline {key}')
            if entry.get('timing_scope') != 'full_phase_wall_time' or entry['seconds'] > duration:
                raise ValueError(f'{phase}: roofline needs full-phase wall time within the measured run')
            for key in ('work_method', 'peak_source', 'timing_method', 'evidence'):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    raise ValueError(f'{phase}: missing roofline {key}')
            artifact = (output / entry['evidence']).resolve()
            if not artifact.is_relative_to(output.resolve()) or not artifact.is_file() or not artifact.stat().st_size:
                raise ValueError(f'{phase}: missing local roofline evidence')
            entry['percent'] = 100 * (entry[work] / entry['seconds']) / entry[peak]
            if not finite_number(entry['percent']):
                raise ValueError(f'{phase}: non-finite roofline utilization')
    return data
