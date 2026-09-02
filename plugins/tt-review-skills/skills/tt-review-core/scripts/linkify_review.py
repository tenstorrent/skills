#!/usr/bin/env python3
"""Convert plain path:line references in a review body into GitHub permalinks.

Reimplemented from the documented behaviour of the upstream Codex skill script
(see SOURCES.md) -- the original was not available to vendor.

    linkify_review.py --sha <SHA> --input review_raw.txt --output review_comment.txt

Converts `path:line` and `path#Lline` into
`https://github.com/<repo>/blob/<sha>/<path>#L<line>`, including refs that were
wrapped in single backticks. Permalinks pin to a commit SHA; branch-name links
rot as soon as the branch moves.
"""

from __future__ import annotations

import argparse
import re
import sys

# Alternation, and order matters: an already-formed URL is matched first and
# passed through untouched, so we can never rewrite a fragment of one. A bare
# lookbehind is not enough here -- "github.com/x/y/f.py#L1" contains a
# perfectly valid-looking path:line starting after the dot.
URL_OR_REF = re.compile(
    r"(?P<url>https?://\S+)"
    r"|"
    r"(?P<bt>`?)"
    r"(?P<path>(?:[\w.+-]+/)+[\w.+-]+\.[A-Za-z][\w]*)"
    r"(?::|\#L)"
    r"(?P<line>\d+)"
    r"(?P=bt)"
)


def linkify(text: str, sha: str, repo: str) -> tuple[str, int]:
    n = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal n
        if m.group("url"):  # already a link -- leave it exactly as it is
            return m.group("url")
        n += 1
        path, line = m.group("path"), m.group("line")
        return f"https://github.com/{repo}/blob/{sha}/{path}#L{line}"

    # Never rewrite inside a fenced block: suggestion bodies must stay verbatim.
    out, fenced = [], False
    for raw in text.splitlines(keepends=True):
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            out.append(raw)
            continue
        out.append(raw if fenced else URL_OR_REF.sub(sub, raw))
    return "".join(out), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--repo", default="tenstorrent/tt-metal")
    ap.add_argument("--header", default="")
    args = ap.parse_args()

    if not re.fullmatch(r"[0-9a-f]{7,40}", args.sha):
        print(f"error: --sha must be a hex commit sha, got {args.sha!r}", file=sys.stderr)
        return 2

    with open(args.input, encoding="utf-8") as fh:
        body = fh.read()

    body, n = linkify(body, args.sha, args.repo)
    if args.header:
        body = f"{args.header}\n\n{body}"

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(body)

    print(f"linkified {n} reference(s) -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
