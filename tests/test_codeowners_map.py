"""Behavioural tests for `tt-split-pr-by-codeowners`'s matcher.

`test_skill_frontmatter.py` guards structure. This file guards the one piece of
behaviour in this repo that is wrong *silently*: an exclusion that misses still
reports the excluded name, so the output looks correct while the headline
approval count is one that cannot occur. This invariant therefore belongs in a
behavioural test rather than a maintenance convention.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/common/tt-split-pr-by-codeowners/scripts/codeowners_map.py"
)


def run(tmp_path: Path, codeowners: str, files: str, *args: str) -> dict:
    co, fl = tmp_path / "CODEOWNERS", tmp_path / "files.txt"
    co.write_text(codeowners, encoding="utf-8")
    fl.write_text(files, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--codeowners", str(co),
         "--files-from", str(fl), "--json", *args],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


# Two rules sharing @Alice: she alone covers both, so she is the one-person
# cover the optimiser reaches for -- and the one GitHub will never accept when
# she is the author.
TWO_RULES = "/src/ @Alice @bob\n/docs/ @Alice\n"
TWO_FILES = "src/a.py\ndocs/b.md\n"


@pytest.mark.parametrize("spelling", ["@Alice", "@alice", "alice", "ALICE"])
def test_exclude_accepts_any_spelling_of_a_login(tmp_path, spelling):
    """A bare login must exclude as surely as '@login'.

    GitHub logins are case-insensitive and CODEOWNERS always spells them with a
    leading '@', but callers hold a bare login: `gh pr view --json author`
    yields `halghTT`, not `@halghTT`. Keying only '@'-prefixed tokens let the
    bare form miss every owner, leaving the author in the cover.
    """
    out = run(tmp_path, TWO_RULES, TWO_FILES, "--exclude", spelling)
    assert out["approval_cover"] == ["@bob"], (
        f"--exclude {spelling} left the excluded owner in the cover"
    )


def test_exclude_email_owners_are_not_at_prefixed(tmp_path):
    """An email is a principal in its own right and must not grow an '@'.

    RFC 5321 local-parts are case-sensitive, so emails are compared exactly;
    prefixing one would make it match nothing.
    """
    out = run(tmp_path, "/src/ dev@example.com @bob\n", "src/a.py\n",
              "--exclude", "dev@example.com")
    assert out["approval_cover"] == ["@bob"]


def test_a_rule_owned_only_by_excluded_people_is_unsatisfiable(tmp_path):
    """Not silently dropped: nobody can clear code-owner review on those files."""
    out = run(tmp_path, "/src/ @Alice\n", "src/a.py\n", "--exclude", "alice")
    assert out["unsatisfiable_rules"], "rule owned solely by an excluded owner vanished"


def test_required_approvals_is_a_floor_not_a_maximum(tmp_path):
    """Owner coverage is not the only gate: a one-owner cover cannot satisfy a
    branch demanding two approvals."""
    out = run(tmp_path, "/src/ @Alice\n", "src/a.py\n", "--required-approvals", "2")
    assert out["approvals_needed"] == 2
