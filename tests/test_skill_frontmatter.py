"""Repo invariants for the skill catalogue.

mattpocock/skills keeps four parallel indexes hand-synced with no CI, which its own
CLAUDE.md concedes is fragile. We enforce instead.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
PROMOTED = ("common", "models", "ttnn", "metal", "llk", "inference", "meta")
TIERS = {"model", "op", "kernel", "process"}
SHA = re.compile(r"^[0-9a-f]{40}$")


def skill_files() -> list[pathlib.Path]:
    return sorted(p for p in SKILLS.rglob("SKILL.md") if p.parent.parent.name in PROMOTED)


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no YAML frontmatter"
    end = text.find("\n---", 3)
    assert end != -1, f"{path}: unterminated frontmatter"
    return yaml.safe_load(text[3:end]) or {}


ALL = skill_files()


def test_at_least_one_skill():
    assert ALL, "no skills found -- the glob or the layout changed"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_name_matches_directory(path):
    assert frontmatter(path)["name"] == path.parent.name


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_required_fields(path):
    fm = frontmatter(path)
    assert fm.get("name"), f"{path}: missing name"
    desc = fm.get("description", "")
    assert desc, f"{path}: missing description"
    assert len(desc) > 40, f"{path}: description too thin to route on"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_tier_in_range(path):
    tier = (frontmatter(path).get("metadata") or {}).get("tier")
    assert tier in TIERS, f"{path}: tier {tier!r} not in {TIERS}"


def test_names_globally_unique():
    """gh-aw resolves pins by NAME, not path -- the bucket is invisible to it.
    Two skills sharing a name make `owner/repo/name@sha` ambiguous."""
    seen: dict[str, pathlib.Path] = {}
    for path in ALL:
        name = frontmatter(path)["name"]
        assert name not in seen, f"duplicate skill name {name!r}: {seen.get(name)} and {path}"
        seen[name] = path


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_upstream_shape(path):
    """metadata.upstream is a list of {repo, ref, path} -- the drift audit parses it."""
    upstream = (frontmatter(path).get("metadata") or {}).get("upstream")
    assert upstream is not None, f"{path}: metadata.upstream missing (use [] if none)"
    assert isinstance(upstream, list), f"{path}: metadata.upstream must be a list"
    for entry in upstream:
        assert SHA.match(entry.get("ref", "")), f"{path}: ref must be a 40-char lowercase sha"
        assert entry.get("path"), f"{path}: upstream entry missing path"
        assert re.match(r"^[\w.-]+/[\w.-]+$", entry.get("repo", "")), \
            f"{path}: repo must be owner/name"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_referenced_files_exist(path):
    """Every references/<file>.md named in a SKILL.md must exist. A skill that
    routes to a missing file degrades silently -- the agent just gets nothing."""
    text = path.read_text(encoding="utf-8")
    for rel in set(re.findall(r"`(references/[\w./-]+\.md)`", text)):
        assert (path.parent / rel).is_file(), f"{path}: references missing file {rel}"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_skill_is_a_router_not_a_monolith(path):
    """Progressive disclosure is load-bearing: gh-aw reviewers only read a skill
    file when inline guidance is insufficient, so the entrypoint must stay small."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 130, f"{path}: SKILL.md is {len(lines)} lines; move depth into references/"


@pytest.mark.parametrize(
    "ref",
    sorted(SKILLS.rglob("references/*.md")),
    ids=lambda p: f"{p.parent.parent.name}/{p.name}",
)
def test_reference_files_bounded(ref):
    size = ref.stat().st_size
    assert size < 4500, f"{ref}: {size} bytes; split it"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_no_posting_from_skills(path):
    """Skills emit findings; the workflow posts them via safe-outputs. A skill that
    writes to the GitHub API is a bug -- gh-aw agents run read-only."""
    text = path.read_text(encoding="utf-8")
    for bad in ("gh api -X POST", "gh api -X PATCH", "gh pr review", "gh pr comment"):
        assert bad not in text or "never" in text.lower() or "not a feature" in text.lower(), \
            f"{path}: appears to instruct posting ({bad})"


def test_promoted_skills_in_plugin_manifest():
    manifest = REPO / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        pytest.skip("no plugin manifest")
    listed = {s["name"] if isinstance(s, dict) else s
              for s in json.loads(manifest.read_text())["skills"]}
    actual = {frontmatter(p)["name"] for p in ALL}
    assert listed == actual, f"plugin.json out of sync: {listed ^ actual}"


def test_promoted_skills_in_readme():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for path in ALL:
        name = frontmatter(path)["name"]
        assert name in readme, f"{name} missing from README Reference section"


def test_workflow_pins_only_real_skills():
    """gh-aw reports a failed skill install as a non-fatal warning, so a pin that
    does not resolve degrades the review silently. Upstream gh-aw currently ships
    such a broken pin; do not inherit the pattern."""
    wf = REPO / ".github" / "workflows" / "tt-pr-review.md"
    if not wf.is_file():
        pytest.skip("no reference workflow")
    actual = {frontmatter(p)["name"] for p in ALL}
    for pin in re.findall(r"tenstorrent/skills/([\w-]+)@", wf.read_text(encoding="utf-8")):
        assert pin in actual, f"workflow pins {pin!r}, which does not exist"


def test_agents_md_matches_claude_md():
    """AGENTS.md is a real file, not a symlink: symlinks break on Windows checkouts
    and in some archive extractions, and self-containment is the whole premise of
    this repo. Enforced rather than hand-synced."""
    claude, agents = REPO / "CLAUDE.md", REPO / "AGENTS.md"
    assert not agents.is_symlink(), "AGENTS.md must be a real file, not a symlink"
    assert agents.read_text() == claude.read_text(), "AGENTS.md and CLAUDE.md have diverged"


@pytest.mark.parametrize("script", sorted(SKILLS.rglob("*.py")), ids=lambda p: p.name)
def test_review_path_scripts_are_stdlib_only(script):
    """Scripts a review workflow can reach must not need pip installs. The meta
    bucket is exempt: it is maintenance tooling, user-invoked, never pinned by a
    review workflow."""
    if script.parent.parent.parent.name == "meta":
        pytest.skip("maintenance tooling, not on the review path")
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", script.read_text(), re.M)
    third_party = {i.split(".")[0] for i in imports} - {
        "argparse", "json", "pathlib", "re", "subprocess", "sys", "os", "typing",
        "dataclasses", "collections", "itertools", "functools", "__future__", "textwrap",
    }
    assert not third_party, f"{script}: non-stdlib imports on the review path: {third_party}"


def test_every_credited_source_has_an_attribution():
    """Attribution is not decoration: every repo we vendored from must appear in the
    README credit section with a linked GitHub handle. A source added to a skill's
    metadata.upstream but never credited is the failure this guards against."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    repos = {e["repo"] for p in ALL
             for e in ((frontmatter(p).get("metadata") or {}).get("upstream") or [])}
    for repo in repos:
        assert repo in readme, f"{repo} is vendored from but not credited in README.md"
    assert re.search(r"https://github\.com/[A-Za-z0-9-]+\)", readme), \
        "README credit section has no linked GitHub handles"


def test_claude_md_stays_a_rulebook():
    """CLAUDE.md is loaded into context every session in this repo, so it is the one
    file that is always paid for. It carries rules; justification belongs in
    .agents/adr/ where it is read once, when someone is changing a rule rather than
    following one. Same discipline the repo already applies to SKILL.md routers."""
    lines = (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 90, (
        f"CLAUDE.md is {len(lines)} lines; move justification into .agents/adr/"
    )


def test_adrs_referenced_from_claude_md_exist():
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    for rel in set(re.findall(r"\((\.agents/adr/[\w./-]+\.md)\)", text)):
        assert (REPO / rel).is_file(), f"CLAUDE.md links missing ADR {rel}"
