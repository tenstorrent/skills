#!/usr/bin/env python3
"""Report drift between vendored skills and their recorded upstream SHAs.

Walks every skills/**/SKILL.md, parses metadata.upstream[], and asks GitHub
whether the upstream path has moved since the recorded ref.

    python3 scripts/check_drift.py                 # table
    python3 scripts/check_drift.py --json          # machine-readable
    python3 scripts/check_drift.py --notice        # NOTICE provenance table (stdout only)

Requires an authenticated `gh`. Private upstreams resolve only if the caller's
own credential can see them -- that is the point of running this locally rather
than in CI. Unreachable upstreams are reported as `unreachable`, never guessed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import pathlib
import subprocess
import sys
from urllib.parse import urlencode

try:
    import yaml
except ImportError:
    sys.exit("error: pyyaml required (pip install pyyaml)")

# scripts/ -> <skill>/ -> <bucket>/ -> skills/ -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def gh(*args: str) -> str | None:
    """Run gh, returning stdout or None when the call fails for any reason."""
    try:
        out = subprocess.run(
            ("gh",) + args, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def latest(repo: str, path: str, branch: str | None = None) -> dict | None:
    """Last commit touching `path`. Returns None if unreachable, {} if the path
    has no commits at all (recorded path is wrong, or was deleted upstream)."""
    q = f"repos/{repo}/commits?path={path}&per_page=1"
    if branch:
        q += f"&sha={branch}"
    raw = gh("api", q, "--jq",
             ".[0] | {sha: .sha, date: .commit.author.date, author: .commit.author.name}")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # jq maps null through the object constructor, so an empty commit list comes
    # back as {"sha": null, ...} rather than null. That means "path not found".
    if not parsed or parsed.get("sha") is None:
        return {}
    return parsed


def authors(repo: str, path: str, ref: str) -> list[str]:
    """Contributors to `path`, most commits first. Prefers the GitHub login over the
    commit author name -- attribution should point at an account someone can follow,
    not a display string that may not resolve to anyone. Read every page at the
    imported revision, so later contributors do not replace the original credit."""
    q = f"repos/{repo}/commits?" + urlencode({"path": path, "per_page": 100, "sha": ref})
    raw = gh("api", q, "--paginate", "--slurp")
    if raw is None:
        raise ValueError(f"cannot read contributors for {repo}/{path}@{ref}")
    try:
        counts = Counter(
            (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"]
            for page in json.loads(raw) for commit in page
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid contributor response for {repo}/{path}@{ref}") from exc
    if not counts or any(not name for name in counts):
        raise ValueError(f"no contributors found for {repo}/{path}@{ref}")
    return sorted(counts, key=lambda name: (-counts[name], name))


def changed_files(repo: str, base: str, head: str, path: str) -> int | None:
    raw = gh(
        "api",
        f"repos/{repo}/compare/{base}...{head}",
        "--jq",
        f'[.files[] | select(.filename | startswith("{path}"))] | length',
    )
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def sources() -> list[dict]:
    rows: list[dict] = []
    for skill in sorted((REPO_ROOT / "skills").rglob("SKILL.md")):
        fm = frontmatter(skill)
        name = fm.get("name", skill.parent.name)
        for up in (fm.get("metadata") or {}).get("upstream") or []:
            repo, ref, path = up.get("repo"), up.get("ref"), up.get("path")
            branch = up.get("branch")  # optional; defaults to the repo default branch
            if not (repo and ref and path):
                rows.append({"skill": name, "status": "malformed", "upstream": str(up)})
                continue
            rows.append({"skill": name, "repo": repo, "path": path, "recorded": ref,
                         "branch": branch, "license": up.get("license", "NOASSERTION")})
    return rows


def collect() -> list[dict]:
    rows: list[dict] = []
    for base in sources():
        if base.get("status") == "malformed":
            rows.append(base)
            continue
        repo, ref, path = base["repo"], base["recorded"], base["path"]
        cur = latest(repo, path, base["branch"])
        if cur is None:
            rows.append({**base, "status": "unreachable"})
            continue
        if cur == {}:
            rows.append({**base, "status": "missing"})
            continue
        # `recorded` is the snapshot we vendored from, which is a repo-level
        # SHA and generally does NOT equal the last commit touching this path.
        # So drift is a path-filtered comparison, not SHA equality -- comparing
        # SHAs directly reports drift on every row forever.
        n = changed_files(repo, ref, cur["sha"], path)
        if n is None:
            rows.append({**base, "status": "unreachable"})
            continue
        rows.append({
            **base, "current": cur["sha"],
            "last_author": cur["author"], "last_date": cur["date"],
            "files_changed": n,
            "status": "drifted" if n > 0 else "current",
        })
    return rows


def notice_table(rows: list[dict]) -> str:
    lines = ["| Skill | Upstream | License | Path | Pinned | Contributors (most commits first) |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("status") == "malformed":
            raise ValueError(f"malformed upstream for {r['skill']}: {r['upstream']}")
        names = ", ".join(authors(r["repo"], r["path"], r["recorded"]))
        names = names.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{r['skill']}` | `{r['repo']}` | {r['license']} | `{r['path']}` "
                     f"| `{r['recorded'][:12]}` | {names} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    output = ap.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--notice", "--sources", dest="notice", action="store_true",
                        help="emit only the NOTICE provenance table to stdout (--sources is an alias)")
    args = ap.parse_args()

    if gh("auth", "status") is None and not args.json:
        print("warning: gh is not authenticated; every upstream will read as unreachable\n",
              file=sys.stderr)

    if args.notice:
        try:
            table = notice_table(sources())
        except ValueError as exc:
            print(f"error: {exc}; keep the existing NOTICE table", file=sys.stderr)
            return 1
        print(table)
        return 0

    rows = collect()

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    width = max((len(r["skill"]) for r in rows), default=5)
    for r in rows:
        mark = {"drifted": "DRIFT ", "current": "ok    ", "missing": "GONE  ",
                "unreachable": "UNRCH ", "malformed": "BAD   "}[r["status"]]
        detail = ""
        if r["status"] == "drifted":
            detail = (f"{r['files_changed']} file(s) since {r['recorded'][:8]}, "
                      f"last by {r['last_author']} on {r['last_date'][:10]}")
        elif r["status"] == "missing":
            detail = "path has no commits upstream -- moved, deleted, or recorded wrong"
        elif r["status"] == "unreachable":
            detail = "no access from this credential"
        elif r["status"] == "malformed":
            detail = r.get("upstream", "")
        print(f"{mark} {r['skill']:<{width}}  {r.get('path', '')}  {detail}")

    drift = sum(r["status"] == "drifted" for r in rows)
    unreach = sum(r["status"] == "unreachable" for r in rows)
    gone = sum(r["status"] == "missing" for r in rows)
    print(f"\n{len(rows)} upstream(s): {drift} drifted, {gone} missing, {unreach} unreachable")
    print("\nDrift is a reading list, not a verdict. Most skills synthesise several "
          "upstreams,\nso a moved upstream does not mean the vendored text is wrong -- "
          "read the diff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
