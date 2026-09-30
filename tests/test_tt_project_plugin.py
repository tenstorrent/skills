"""Packaging invariants for the tt-project plugin."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

import yaml

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project"
SKILLS = PLUGIN / "skills"
RUNTIME = PLUGIN / "runtime" / "ttp"
MANIFESTS = [PLUGIN / d / "plugin.json" for d in (".claude-plugin", ".codex-plugin", ".cursor-plugin")]

# Anything that would tie the open-source plugin to one company's network or one person's setup.
FORBIDDEN = [
    r"\.local\.tenstorrent\.com", r"tenstorrent\.enterprise\.slack", r"atlassian\.net", r"aus-gitlab",
    r"\bg\d\dblx\d\d\b", r"\bf\d\dcs\d\d\b", r"\bblx0\d\b", r"\bcs0\d\b", r"smarton", r"steel_3d",
    r"xox[bpa]-[0-9A-Za-z]", r"sk-ant-", r"/home/[a-z]+/", r"/Users/[a-z]+/",
]


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no frontmatter"
    return yaml.safe_load(text[3:text.find("\n---", 3)]) or {}


def test_manifests_agree():
    data = [json.loads(p.read_text()) for p in MANIFESTS]
    assert {d["name"] for d in data} == {"tt-project"}
    assert len({d["version"] for d in data}) == 1
    assert all(d["skills"] == "./skills/" for d in data)


def test_skills_are_discoverable_and_self_contained():
    skills = sorted(SKILLS.glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"project", "harness"}
    for path in skills:
        fm = frontmatter(path)
        assert fm["name"] == path.parent.name
        assert len(fm["description"]) > 40
    for md in SKILLS.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert not re.search(r"[`(]\.\./\w", text), f"{md}: relative ../ path"
        for ref in re.findall(r"`([\w-]+\.md)`", text):
            if (md.parent / ref).exists() or ref in {"MEMORY.md", "CHARTER.md", "README.md"}:
                continue
            if ref.startswith(("kind-", "daily-review", "coordinator", "worker")):
                continue   # prompt templates of a project's harness, named for the reader
            raise AssertionError(f"{md}: references missing sibling {ref}")


def test_runtime_is_standard_library_only_and_python39_compatible():
    stdlib = set(sys.stdlib_module_names)
    for py in RUNTIME.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))   # rejects syntax newer than 3.9
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                assert n in stdlib or n == "ttp", f"{py}: imports non-stdlib module {n}"
        if re.search(r"\w\]?\s*\|\s*None\b", src):
            assert "from __future__ import annotations" in src, f"{py}: PEP 604 unions need the future import on 3.9"


def test_no_setup_specific_or_private_details():
    pattern = re.compile("|".join(FORBIDDEN))
    for f in PLUGIN.rglob("*"):
        if f.is_file() and f.suffix in {".py", ".md", ".json", ".js", ".html", ".css", ""}:
            text = f.read_text(encoding="utf-8", errors="replace")
            m = pattern.search(text)
            assert not m, f"{f.relative_to(PLUGIN)}: contains {m.group(0)!r}"


def test_launchers_are_executable():
    import os
    for launcher in (PLUGIN / "bin" / "ttp", PLUGIN / "template" / "bin" / "ttp"):
        assert os.access(launcher, os.X_OK), f"{launcher} is not executable"
    assert (PLUGIN / "bin" / "ttp").read_text() == (PLUGIN / "template" / "bin" / "ttp").read_text()
