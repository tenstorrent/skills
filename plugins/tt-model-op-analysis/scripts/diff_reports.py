"""Compare a static op_table.csv with a measured_ops.csv for one profile.

Matching is by op_code and launch count only: Tracy ATTRIBUTES carry the operation attributes,
not the program factory name, so factories cannot be confirmed from a report.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import read_csv, write_csv  # noqa: E402

DIFF_COLUMNS = ("category", "op_code", "static_launches", "measured_launches", "static_ids", "detail")


def diff(static_rows: list[dict], measured_rows: list[dict], profile: str) -> list[dict]:
    out: list[dict] = []
    static_launches: dict[str, int] = collections.Counter()
    static_ids: dict[str, list[str]] = collections.defaultdict(list)
    for row in static_rows:
        code = row["op_code"].strip()
        if not code:
            out.append({"category": "unmatched static row", "op_code": "", "static_launches": row[f"{profile}:launches"],
                        "measured_launches": "", "static_ids": row["id"], "detail": row["ttnn_api"]})
            continue
        static_launches[code] += int(row[f"{profile}:launches"] or 0)
        static_ids[code].append(row["id"])

    device = [r for r in measured_rows if r["op_type"] == "tt_dnn_device"]
    measured_count = collections.Counter(r["op_code"] for r in device)

    for code in sorted(set(static_launches) | set(measured_count)):
        s, m, ids = static_launches.get(code, 0), measured_count.get(code, 0), ",".join(static_ids.get(code, []))
        base = {"op_code": code, "static_launches": s, "measured_launches": m, "static_ids": ids}
        if code not in measured_count:
            if s > 0:
                out.append({"category": "missing in measured", **base, "detail": ""})
            continue
        if code not in static_launches:
            out.append({"category": "extra in measured", **base, "detail": ""})
            continue
        if s != m:
            out.append({"category": "count differs", **base, "detail": f"{s} static vs {m} measured"})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static", type=pathlib.Path, required=True)
    parser.add_argument("--measured", type=pathlib.Path, required=True)
    parser.add_argument("--profile", required=True, choices=("p100", "p150"))
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    rows = diff(read_csv(args.static), read_csv(args.measured), args.profile)
    write_csv(args.out, DIFF_COLUMNS, rows)
    print(f"{len(rows)} differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
