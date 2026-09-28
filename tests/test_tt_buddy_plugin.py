"""Packaging invariants for the tt-buddy plugin."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor


PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-buddy"
SKILLS = PLUGIN / "skills"
RECIPES = PLUGIN / "recipes"
WRITE_ENTRY = SKILLS / "note" / "scripts" / "write-entry.sh"
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
    assert os.access(WRITE_ENTRY, os.X_OK), "write-entry.sh is not executable"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _source_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "src")
    (src / "untracked.txt").write_text("unrelated work\n")
    return src


def _write(src, notes, topic, title, body="- entry"):
    env = dict(os.environ, TT_BUDDY_NOTES=str(notes), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run([str(WRITE_ENTRY), topic, title], cwd=src, env=env, input=body,
                          check=True, capture_output=True, text=True)


def _subjects(notes):
    return _git(notes, "log", "--format=%s").splitlines()


def test_first_write_creates_the_notes_repo_and_leaves_the_source_alone(tmp_path):
    src, notes = _source_repo(tmp_path), tmp_path / "notes"
    _write(src, notes, "topic", "first")
    assert _subjects(notes) == ["topic: first", "init: capture existing notes"]
    assert _git(src, "log", "--format=%s").splitlines() == ["src"]
    assert "untracked.txt" in _git(src, "status", "--porcelain")


def test_first_write_into_an_empty_notes_directory(tmp_path):
    src, notes = _source_repo(tmp_path), tmp_path / "notes"
    notes.mkdir()
    _write(src, notes, "topic", "first")
    assert _subjects(notes) == ["topic: first", "init: capture existing notes"]


def test_concurrent_writers_keep_every_entry_and_commit_once_each(tmp_path):
    src, notes = _source_repo(tmp_path), tmp_path / "notes"
    _write(src, notes, "topic", "seed")
    titles = [f"writer {i}" for i in range(8)]
    with ThreadPoolExecutor(len(titles)) as pool:
        list(pool.map(lambda title: _write(src, notes, "topic", title), titles))
    text = (notes / "topic.md").read_text()
    for title in titles + ["seed"]:
        assert text.count(f"## {title}\n") == 1
    assert sorted(_subjects(notes)) == sorted([f"topic: {t}" for t in titles] + ["topic: seed", "init: capture existing notes"])
    assert not (tmp_path / "notes.lock").exists()


def test_write_commits_only_the_note_file(tmp_path):
    src, notes = _source_repo(tmp_path), tmp_path / "notes"
    _write(src, notes, "topic", "first")
    (notes / "staged.md").write_text("unrelated\n")
    _git(notes, "add", "staged.md")
    _write(src, notes, "topic", "second")
    assert _git(notes, "show", "--name-only", "--format=", "HEAD").split() == ["topic.md"]
    assert "staged.md" in _git(notes, "diff", "--cached", "--name-only")


def test_writer_waits_for_a_held_lock(tmp_path):
    src, notes = _source_repo(tmp_path), tmp_path / "notes"
    _write(src, notes, "topic", "first")
    lock = tmp_path / "notes.lock"
    lock.mkdir()
    env = dict(os.environ, TT_BUDDY_NOTES=str(notes), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    writer = subprocess.Popen([str(WRITE_ENTRY), "topic", "second"], cwd=src, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    writer.stdin.write("- entry")
    writer.stdin.close()
    try:
        writer.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    assert writer.poll() is None, "writer did not wait for the held lock"
    assert _subjects(notes)[0] == "topic: first"
    lock.rmdir()
    assert writer.wait(timeout=10) == 0
    assert _subjects(notes)[0] == "topic: second"
