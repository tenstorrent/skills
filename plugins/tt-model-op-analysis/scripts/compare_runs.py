"""Compare two runs of the same table by key, ignoring row ids, for the merge option."""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import MATCH_KEY, read_csv, write_csv  # noqa: E402

CHANGE_COLUMNS = ("change", "key", "column", "old", "new")
IGNORED = {"id"}


def index(rows: list[dict], key: tuple[str, ...]) -> dict[str, dict[str, str]]:
    # Rows sharing a key are variants of one entry; their values are sorted before joining so
    # that reordering rows between runs is not reported as a change.
    collected: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        k = " / ".join(row.get(c, "") for c in key)
        entry = collected.setdefault(k, {})
        for col, value in row.items():
            entry.setdefault(col, []).append(value or "")
    return {k: {col: " | ".join(sorted(values)) for col, values in entry.items()} for k, entry in collected.items()}


def compare(old: list[dict], new: list[dict], key: tuple[str, ...]) -> list[dict]:
    a, b = index(old, key), index(new, key)
    out = [{"change": "removed", "key": k, "column": "", "old": "", "new": ""} for k in a if k not in b]
    out += [{"change": "added", "key": k, "column": "", "old": "", "new": ""} for k in b if k not in a]
    for k in (k for k in b if k in a):
        for col in sorted((set(a[k]) | set(b[k])) - IGNORED - set(key)):
            if a[k].get(col, "") != b[k].get(col, ""):
                out.append({"change": "changed", "key": k, "column": col,
                            "old": a[k].get(col, ""), "new": b[k].get(col, "")})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=pathlib.Path, required=True)
    parser.add_argument("--new", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--key", default=",".join(MATCH_KEY))
    args = parser.parse_args()
    key = tuple(args.key.split(","))
    old, new = read_csv(args.old), read_csv(args.new)
    for name, rows in (("old", old), ("new", new)):
        present = set(rows[0]) if rows else set()
        missing = [c for c in key if rows and c not in present]
        if missing:
            print(f"ERROR: key column {missing[0]} missing from --{name}")
            return 2
    rows = compare(old, new, key)
    write_csv(args.out, CHANGE_COLUMNS, rows)
    print(f"{len(rows)} changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
