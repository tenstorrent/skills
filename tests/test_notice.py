"""Protect attribution when regenerating the NOTICE provenance table."""

import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/meta/tt-skills-upstream-audit/scripts/check_drift.py"
spec = importlib.util.spec_from_file_location("check_drift", SCRIPT)
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)


def commit(name=None, fallback="Unlinked contributor"):
    return {"author": {"login": name} if name else None,
            "commit": {"author": {"name": fallback}}}


def test_authors_include_all_pages_at_the_imported_revision(monkeypatch):
    def gh(*args):
        assert args[0] == "api"
        assert "--paginate" in args and "--slurp" in args
        query = parse_qs(urlsplit(args[1]).query)
        assert query["sha"] == ["a" * 40]
        assert query["path"] == ["skills/a path/SKILL.md"]
        return json.dumps([[commit("first")] * 100,
                           [commit("late"), commit(), commit("late")]])

    monkeypatch.setattr(drift, "gh", gh)
    assert drift.authors("owner/repo", "skills/a path/SKILL.md", "a" * 40) == [
        "first", "late", "Unlinked contributor"]


@pytest.mark.parametrize("response", [None, "[]", "not JSON", '{}'])
def test_failed_attribution_does_not_emit_a_partial_table(monkeypatch, capsys, response):
    rows = [{"skill": "example", "repo": "owner/repo", "path": "SKILL.md",
             "recorded": "a" * 40, "license": "MIT", "branch": "moving-branch"}]
    monkeypatch.setattr(drift, "sources", lambda: rows * 2)
    responses = iter([json.dumps([[commit("first")]]), response])
    monkeypatch.setattr(drift, "gh", lambda *args: "ok" if args[0] == "auth" else next(responses))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--notice"])
    assert drift.main() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "keep the existing NOTICE table" in out.err


@pytest.mark.parametrize("flag", ["--notice", "--sources"])
def test_notice_reports_license_without_querying_current_branch(monkeypatch, capsys, flag):
    row = {"skill": "example", "repo": "owner/repo", "path": "SKILL.md",
           "recorded": "a" * 40, "branch": "moving-branch", "license": "MIT"}
    monkeypatch.setattr(drift, "sources", lambda: [row])
    monkeypatch.setattr(drift, "collect", lambda: pytest.fail("must not query current upstream"))
    monkeypatch.setattr(drift, "gh", lambda *args: "ok")

    def authors(repo, path, ref):
        assert ref == row["recorded"]
        return ["contributor"]

    monkeypatch.setattr(drift, "authors", authors)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), flag])
    assert drift.main() == 0
    assert "| `owner/repo` | MIT |" in capsys.readouterr().out


def test_notice_covers_every_recorded_upstream_and_license():
    notice = (REPO / "NOTICE").read_text()
    for path in (REPO / "skills").glob("*/*/SKILL.md"):
        fm = yaml.safe_load(path.read_text().split("---", 2)[1])
        for source in fm["metadata"]["upstream"]:
            row = (f"| `{fm['name']}` | `{source['repo']}` | {source['license']} | "
                   f"`{source['path']}` | `{source['ref'][:12]}` |")
            assert row in notice, f"NOTICE missing provenance: {row}"
