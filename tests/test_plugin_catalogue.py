"""Cross-host packaging and consent invariants for the plugin marketplace."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGINS = REPO / "plugins"
CODEX_MARKETPLACE = REPO / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"

REFERENCE_BYTE_CAP = 4500
# Line numbers rot on every refactor. File basenames and identifiers are fine.
LINE_CITATION = re.compile(r"\.(?:cpp|hpp|h|py|rst|sh)\s*:\s*\d+")


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_host_catalogues_expose_the_same_plugins():
    codex = load(CODEX_MARKETPLACE)
    claude = load(CLAUDE_MARKETPLACE)
    assert codex["name"] == claude["name"]
    codex_entries = {entry["name"]: entry for entry in codex["plugins"]}
    claude_entries = {entry["name"]: entry for entry in claude["plugins"]}
    assert codex_entries.keys() == claude_entries.keys()

    for name, entry in codex_entries.items():
        expected = f"./plugins/{name}"
        assert entry["source"] == {"source": "local", "path": expected}
        assert claude_entries[name]["source"] == expected


def test_only_finder_is_installed_by_default():
    entries = {entry["name"]: entry for entry in load(CODEX_MARKETPLACE)["plugins"]}
    assert entries["tt-skills"]["policy"]["installation"] == "INSTALLED_BY_DEFAULT"
    for name, entry in entries.items():
        if name != "tt-skills":
            assert entry["policy"]["installation"] == "AVAILABLE"
        assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
        assert entry["category"]


def test_each_plugin_has_matching_host_manifests():
    names = {entry["name"] for entry in load(CODEX_MARKETPLACE)["plugins"]}
    for name in names:
        root = PLUGINS / name
        codex = load(root / ".codex-plugin" / "plugin.json")
        claude = load(root / ".claude-plugin" / "plugin.json")
        assert codex["name"] == claude["name"] == name
        assert codex["version"] == claude["version"]
        assert codex["skills"] == claude["skills"] == "./skills/"
        assert (root / "skills").is_dir()


def test_codex_manifests_have_complete_install_surfaces():
    required_interface = {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }
    for manifest_path in PLUGINS.glob("*/.codex-plugin/plugin.json"):
        manifest = load(manifest_path)
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
        assert manifest["description"]
        assert manifest["author"]["name"]
        assert manifest["license"]
        assert required_interface <= manifest["interface"].keys()
        assert manifest["interface"]["capabilities"]
        assert manifest["interface"]["defaultPrompt"]


def test_packaged_skills_have_discoverable_frontmatter():
    for path in PLUGINS.glob("*/skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.find("\n---", 3)
        assert end != -1
        frontmatter = yaml.safe_load(text[3:end]) or {}
        assert frontmatter.get("name") == path.parent.name
        assert frontmatter.get("description")


def test_packaged_skill_references_stay_short():
    """A reference is loaded whole, so its size is what it costs. Scoped to
    tt-debug-tools because tt-autodebug carries two references over this cap.
    """
    for ref in (PLUGINS / "tt-debug-tools" / "skills").glob("*/references/*.md"):
        size = ref.stat().st_size
        assert size < REFERENCE_BYTE_CAP, (
            f"{ref.relative_to(PLUGINS)} is {size} bytes, cap {REFERENCE_BYTE_CAP}"
        )


def test_packaged_skills_cite_no_source_line_numbers():
    """A file:line citation into upstream is wrong by the next refactor, and reads
    authoritative while being wrong. Basenames and identifiers survive.

    Quoted and fenced lines are exempt, and the distinction is real rather than a
    carve-out: a `file.cpp:212` inside a blockquote or a code fence is showing
    text -- an output template a reviewer should imitate, a sample of what a tool
    prints -- not pointing the reader at a line of source.
    """
    for path in list(PLUGINS.glob("*/skills/*/SKILL.md")) + list(
        PLUGINS.glob("*/skills/*/references/*.md")
    ):
        fenced = False
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced or line.lstrip().startswith(">"):
                continue
            assert not LINE_CITATION.search(line), (
                f"{path}:{n} cites a source line number, which rots"
            )


def test_packaged_skill_references_exist():
    for path in PLUGINS.glob("*/skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        code_refs = re.findall(r"`(references/[\w./-]+\.md)`", text)
        link_refs = re.findall(r"\]\((references/[\w./-]+\.md)\)", text)
        for rel in set(code_refs + link_refs):
            assert (path.parent / rel).is_file(), f"{path}: references missing file {rel}"


def test_plugins_are_individually_owned():
    codeowners = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for plugin in PLUGINS.iterdir():
        if plugin.is_dir():
            assert f"/plugins/{plugin.name}/" in codeowners


def test_finder_catalogues_every_optional_plugin():
    finder = (
        PLUGINS
        / "tt-skills"
        / "skills"
        / "tt-skills-finder"
        / "references"
        / "catalog.md"
    ).read_text(encoding="utf-8")
    entries = load(CODEX_MARKETPLACE)["plugins"]
    optional = {
        entry["name"]
        for entry in entries
        if entry["policy"]["installation"] != "INSTALLED_BY_DEFAULT"
    }
    assert optional
    for name in optional:
        assert f"`{name}`" in finder


def test_finder_is_discovery_only():
    skill = (
        PLUGINS / "tt-skills" / "skills" / "tt-skills-finder" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    assert "a recommendation is not permission" in skill
    assert "do not run an installation command" in skill
    assert "explicitly asks to install" in skill


def test_readme_catalogues_every_plugin():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Plugin catalogue", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))
    expected = {entry["name"] for entry in load(CODEX_MARKETPLACE)["plugins"]}
    assert listed == expected, f"README plugin catalogue is out of sync: {listed ^ expected}"


def test_packaged_review_skills_are_current():
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "sync_review_plugin.py"), "--check"],
        cwd=REPO,
        check=True,
    )
