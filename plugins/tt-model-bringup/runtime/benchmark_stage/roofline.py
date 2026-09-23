"""Report modeled roofline utilization over measured full-phase wall time."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmark_stage.evidence import finite_number
from benchmark_stage.subsets import digest


def load_roofline(output, *, required=()):
    """Validate phase accounting; require complete rows for the named profiles."""
    output = Path(output)
    path = output / 'roofline.json'
    if not path.exists():
        if required:
            raise ValueError('missing required roofline.json; collect full-phase accounting')
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) - {'1', '32'}:
        raise ValueError('roofline accounting must use concurrency 1 or 32')
    if set(required) - set(data):
        raise ValueError('missing required roofline profile: ' + ', '.join(sorted(set(required) - set(data))))
    for concurrency, row in data.items():
        if not isinstance(row, dict):
            raise ValueError('roofline profile must be an object')
        raw_path = output / f'perf-b{concurrency}.json'
        if row.get('performance_sha256') != hashlib.sha256(raw_path.read_bytes()).hexdigest():
            raise ValueError('roofline accounting does not match the measured performance run')
        raw = json.loads(raw_path.read_text())
        server = json.loads((output / f'perf-b{concurrency}-server.json').read_text())
        if row.get('server_identity_sha256') != digest(server):
            raise ValueError('roofline accounting does not match the measured server configuration')
        for key, source in (('requests', 'completed'), ('input_tokens', 'total_input_tokens'),
                            ('output_tokens', 'total_output_tokens')):
            if row.get(key) != raw[source]:
                raise ValueError(f'roofline {key} does not cover the measured workload')
        duration = raw['duration']
        for phase, work, peak in (
                ('prefill', 'flops', 'peak_flops_per_second'),
                ('decode', 'dram_bytes', 'peak_dram_bytes_per_second')):
            entry = row.get(phase)
            if entry is None:
                if concurrency in required:
                    raise ValueError(f'concurrency {concurrency}: missing required {phase} accounting')
                continue
            if not isinstance(entry, dict):
                raise ValueError(f'{phase}: accounting must be an object')
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
            if entry.get('evidence_sha256') != hashlib.sha256(artifact.read_bytes()).hexdigest():
                raise ValueError(f'{phase}: roofline evidence hash mismatch')
            entry['percent'] = 100 * (entry[work] / entry['seconds']) / entry[peak]
            if not finite_number(entry['percent']):
                raise ValueError(f'{phase}: non-finite roofline utilization')
    return data
