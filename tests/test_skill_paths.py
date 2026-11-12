"""tt-metal paths named in tt-model-bringup skills and prompts must exist (offline snapshot check).

Refresh the snapshot with `python plugins/tt-model-bringup/scripts/refresh_tt_metal_paths.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "tt-model-bringup"
SNAPSHOT = ROOT / "tests" / "data" / "tt_metal_paths.txt"

_spec = importlib.util.spec_from_file_location("refresh_tt_metal_paths", PLUGIN / "scripts" / "refresh_tt_metal_paths.py")
paths_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(paths_mod)

# TODO(integrator): remove once the stale-path map for optimize/SKILL.md is applied.
EXEMPT_FILES: set = set()


def _missing(refs, snapshot):
    out = {}
    for ref, files in refs.items():
        users = set(files) - EXEMPT_FILES
        if users and ref not in snapshot:
            out[ref] = sorted(users)
    return out


def test_skill_and_prompt_paths_exist_in_tt_metal_snapshot():
    refs = paths_mod.referenced_paths(PLUGIN)
    assert refs, "no tt-metal paths found; the extractor is broken"
    missing = _missing(refs, paths_mod.load_snapshot(SNAPSHOT))
    assert not missing, (
        "skills/prompts name tt-metal paths absent from tests/data/tt_metal_paths.txt. Fix the pointer, or "
        "refresh the snapshot if the path is new: " + "; ".join(f"{k} ({', '.join(v)})" for k, v in sorted(missing.items()))
    )


def test_snapshot_is_sorted_and_closed_under_parents():
    lines = [line for line in SNAPSHOT.read_text().splitlines() if line and not line.startswith("#")]
    assert lines == sorted(set(lines))
    snapshot = set(lines)
    for path in snapshot:
        for parent in paths_mod.parents(path):
            assert parent in snapshot, (path, parent)


def test_extractor_rules():
    text = (
        "Read `models/ttt_compat/tt/generator.py::Generator` and `ttnn/cpp/ttnn/operations/**`.\n"
        "See https://github.com/tenstorrent/tt-metal/blob/abc/models/tt_transformers/tt/model.py and "
        "`tests/ttnn/tracy/test_trace_runs.py`. Write `models/autoports/<model>/tt/model.py`, "
        "then models/demos/gpt_oss/tt/. Done."
    )
    assert paths_mod.extract_paths(text) == {
        "models/ttt_compat/tt/generator.py",
        "ttnn/cpp/ttnn/operations",
        "models/autoports",
        "models/demos/gpt_oss/tt",
    }


def test_check_fails_on_a_path_absent_from_the_snapshot():
    snapshot = {"models", "models/demos", "models/demos/gpt_oss"}
    refs = {"models/demos/gpt_oss": {"skills/a/SKILL.md"}, "models/common/modules/mlp/mlp_1d.py": {"skills/b/SKILL.md"}}
    assert _missing(refs, snapshot) == {"models/common/modules/mlp/mlp_1d.py": ["skills/b/SKILL.md"]}


def test_build_snapshot_keeps_only_referenced_paths_and_parents():
    tree = {"truncated": False, "tree": [{"path": p} for p in ("models", "models/a", "models/a/b.py", "models/c.py")]}
    assert paths_mod.build_snapshot(tree, ["models/a/b.py", "models/gone.py"]) == ["models", "models/a", "models/a/b.py"]
