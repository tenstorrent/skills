"""tt-train-bringup multi-chip rules: the seven M1.0 skill changes are present, the old Tracy
depth protocol is gone, and tt_mesh_smoke.py's expected sums are right."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "tt-train-bringup"
SKILLS = PLUGIN / "skills"


def text(*parts: str) -> str:
    return (PLUGIN.joinpath(*parts)).read_text(encoding="utf-8")


def plugin_texts() -> dict[str, str]:
    out = {}
    for path in PLUGIN.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".txt", ".json"}:
            out[str(path.relative_to(PLUGIN))] = path.read_text(encoding="utf-8")
    return out


OLD_PROTOCOL = [
    re.compile(r"2, 4, 8 and 16", re.I),
    re.compile(r"four doubling", re.I),
    re.compile(r"doubling depths?", re.I),
    re.compile(r"projected to the full depth", re.I),
]


@pytest.mark.parametrize("pattern", OLD_PROTOCOL, ids=lambda p: p.pattern)
def test_old_tracy_depth_protocol_is_gone(pattern):
    hits = [name for name, body in plugin_texts().items() if pattern.search(body)]
    assert not hits, f"old Tracy protocol {pattern.pattern!r} still in {hits}"


def test_new_tracy_protocol_everywhere_it_is_stated():
    for rel in [
        ("skills", "optimization-loop", "SKILL.md"),
        ("skills", "composite-bringup", "SKILL.md"),
        ("skills", "tt-device-usage", "SKILL.md"),
        ("skills", "stage-review", "SKILL.md"),
        ("README.md",),
    ]:
        body = text(*rel)
        assert re.search(r"1x, 2x and 4x", body), f"{'/'.join(rel)} does not state the 1x/2x/4x slice protocol"
    loop = text("skills", "optimization-loop", "SKILL.md")
    assert "never runs on the full model" in loop
    assert "Scaling check" in loop


def test_device_usage_mesh_setup():
    body = text("skills", "tt-device-usage", "SKILL.md")
    assert "## Mesh Setup" in body
    assert "TT_MESH_GRAPH_DESC_PATH" in body and "configs/mgd/" in body
    assert "tt_mesh_smoke.py" in body
    assert "every chip" in body  # resets/health and triage cover every chip


def test_plan_costs_every_layout_and_has_a_reuse_map():
    body = text("skills", "train-plan", "SKILL.md")
    assert "Layout choice on an authorised box" in body
    assert "even if the model fits one chip" in body
    for word in ("DP x TP", "per-rank memory", "collective bytes per step"):
        assert word in body
    assert "Multi-chip reuse map" in body
    for piece in ("sync_gradients", "llama.cpp", "pipeline_parallel_llama", "linear_regression_tp_dp", "qwen3", "fsdp.py"):
        assert piece in body, piece
    assert re.search(r"`used`, `extended`.*`not used`", body)
    assert "`slice` yaml block" in body


def test_composite_mesh_code_rules():
    body = text("skills", "composite-bringup", "SKILL.md")
    assert "## Mesh code rules" in body
    for rule in (
        "LoRA adapters are sharded with their base weight",
        "DP syncs trainable gradients only",
        "The seed is the same on every rank",
        "Data is sharded from the global batch in reference order",
        "The loss is the mean over the global batch",
    ):
        assert rule in body, rule


def test_loop_collectives_class():
    body = text("skills", "optimization-loop", "SKILL.md")
    assert "six classes" in body
    assert "Collectives are the inter-chip work" in body
    for action in ("overlap with the backward", "bucketing", "a cheaper layout", "fewer bytes"):
        assert action in body, action
    assert "every chip" in body and "critical chip" in body
    assert "collective term" in body


def test_stage_review_findings():
    body = text("skills", "stage-review", "SKILL.md")
    for finding in (
        "PCC was run without the shard shapes",
        "DP syncs a frozen gradient",
        "no collective term",
        "chosen without the alternatives",
        "single-chip loss match never stands in",
    ):
        assert finding in body, finding


def load_smoke():
    spec = importlib.util.spec_from_file_location("tt_mesh_smoke", PLUGIN / "scripts" / "tt_mesh_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mesh_smoke_expected_sums():
    smoke = load_smoke()
    values = [[1, 2], [3, 4]]
    assert smoke.expected_after_reduce(values, 1) == [[3, 3], [7, 7]]
    assert smoke.expected_after_reduce(values, 0) == [[4, 6], [4, 6]]
    assert smoke.expected_after_reduce([[1, 2, 3, 4]], 1) == [[10, 10, 10, 10]]


def test_mesh_smoke_shape_and_mgd(tmp_path, monkeypatch):
    smoke = load_smoke()
    assert smoke.parse_shape("2x2") == (2, 2)
    assert smoke.parse_shape("1X4") == (1, 4)
    with pytest.raises(Exception):
        smoke.parse_shape("4")
    monkeypatch.delenv("TT_MESH_GRAPH_DESC_PATH", raising=False)
    mgd = tmp_path / "tt-train" / "configs" / "mgd"
    mgd.mkdir(parents=True)
    (mgd / "bh_2_2_line_line.textproto").write_text("x")
    monkeypatch.setenv("TT_METAL_HOME", str(tmp_path))
    assert smoke.resolve_mgd((2, 2), None).endswith("bh_2_2_line_line.textproto")
    assert smoke.resolve_mgd((1, 4), None) is None
    assert smoke.resolve_mgd((1, 4), "/x.textproto") == "/x.textproto"
    monkeypatch.setenv("TT_MESH_GRAPH_DESC_PATH", "/env.textproto")
    assert smoke.resolve_mgd((2, 2), None) == "/env.textproto"
