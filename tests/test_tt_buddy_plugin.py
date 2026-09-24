"""Packaging invariants for the tt-buddy plugin."""

from __future__ import annotations

import json
import os
import pathlib
import re


PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-buddy"
SKILLS = PLUGIN / "skills"
RECIPES = PLUGIN / "recipes"
SKILL_FILES = sorted(SKILLS.rglob("*.md"))

RELATIVE_PATH = re.compile(r"[`(]\.\./\w")
RECIPE_LINK = re.compile(r"<plugin-root>/recipes/([\w./<>-]+\.md)")


def test_skills_use_plugin_root_paths():
    # `../` resolves against the skill directory; `<plugin-root>` is the only shared anchor.
    for path in SKILL_FILES:
        assert not RELATIVE_PATH.search(path.read_text(encoding="utf-8")), (
            f"{path}: uses a relative ../ path"
        )


def test_recipe_links_resolve():
    repos = [p.name for p in RECIPES.iterdir() if p.is_dir()]
    seen = 0
    for path in SKILL_FILES:
        for rel in RECIPE_LINK.findall(path.read_text(encoding="utf-8")):
            seen += 1
            if "<repo>" in rel:
                assert any((RECIPES / rel.replace("<repo>", r)).is_file() for r in repos), (
                    f"{path}: no repo provides {rel}"
                )
            else:
                assert (RECIPES / rel).is_file(), f"{path}: missing recipe {rel}"
    assert seen


def test_hook_commands_prefer_the_host_neutral_plugin_root():
    # Codex sets PLUGIN_ROOT natively; Claude sets only CLAUDE_PLUGIN_ROOT.
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands
    for command in commands:
        assert command.startswith('"${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/hooks/'), command


def test_hooks_are_executable():
    for name in ("session-start", "user-prompt-submit"):
        assert os.access(PLUGIN / "hooks" / name, os.X_OK), f"hooks/{name} is not executable"
