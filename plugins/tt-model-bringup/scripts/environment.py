#!/usr/bin/env python3
"""Validate an explicitly selected AutoDebug installation and print shell exports."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_SKILLS = ('autodebug', 'autotriage', 'autofix')
INSTALL = ('Install tt-autodebug explicitly: Codex: codex plugin add '
           'tt-autodebug@tenstorrent-skills; Claude Code: '
           '/plugin install tt-autodebug@tenstorrent-skills. '
           'Then set TT_AUTODEBUG_ROOT to that enabled installation directory.')


def dependency_root(value: str | None = None) -> Path:
    value = value or os.environ.get('TT_AUTODEBUG_ROOT')
    if not value:
        raise SystemExit('Missing required tt-autodebug dependency. ' + INSTALL)
    root = Path(value).expanduser().resolve()
    manifests = [root / host / 'plugin.json' for host in ('.codex-plugin', '.claude-plugin')]
    valid = False
    for path in manifests:
        if path.is_file():
            try:
                valid |= json.loads(path.read_text()).get('name') == 'tt-autodebug'
            except (ValueError, OSError):
                pass
    if not valid or any(not (root / 'skills' / name / 'SKILL.md').is_file() for name in DEPENDENCY_SKILLS):
        raise SystemExit('Invalid tt-autodebug installation: ' + str(root) + '. ' + INSTALL)
    return root


def environment(root: Path, env: dict[str, str]) -> dict[str, str]:
    return {
        'TT_MODEL_BRINGUP_ROOT': str(ROOT),
        'TT_AUTODEBUG_ROOT': str(root),
        'PYTHONPATH': os.pathsep.join(filter(None, [str(ROOT / 'runtime'), env.get('PYTHONPATH', '')])),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--autodebug-root')
    args = parser.parse_args()
    for key, value in environment(dependency_root(args.autodebug_root), os.environ).items():
        print(f'export {key}={shlex.quote(value)}')
