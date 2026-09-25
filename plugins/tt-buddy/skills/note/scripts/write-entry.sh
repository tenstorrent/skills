#!/usr/bin/env bash

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

# Usage: write-entry.sh <topic> <title> < body
# Prepends one entry to <notes>/<topic>.md and commits only that file.
# Run from the source workspace; its repo and HEAD go on the metadata line.

set -euo pipefail

topic="${1:?usage: write-entry.sh <topic> <title> < body}"
title="${2:?usage: write-entry.sh <topic> <title> < body}"
notes="${TT_BUDDY_NOTES:-$HOME/.tt-buddy/notes}"
lock="$notes.lock"

if ! [[ "$topic" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "write-entry: topic must be a lowercase slug: $topic" >&2
    exit 2
fi
body="$(cat)"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    repo="$(basename "$(git remote get-url origin 2>/dev/null || git rev-parse --show-toplevel)" .git)"
    sha="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
    [ -z "$(git status --porcelain 2>/dev/null)" ] || sha="$sha-dirty"
    source="$repo@$sha"
else
    source="$(basename "$PWD")@no-git"
fi

mkdir -p "$(dirname "$notes")"

# mkdir is atomic on every POSIX filesystem, so it serializes writers without flock.
locked=0
for _ in $(seq 1 300); do
    if mkdir "$lock" 2>/dev/null; then
        locked=1
        break
    fi
    sleep 0.1
done
if [ "$locked" -ne 1 ]; then
    echo "write-entry: $lock held for 30s; remove it if no writer is running" >&2
    exit 1
fi
trap 'rmdir "$lock"' EXIT

mkdir -p "$notes"
if ! git -C "$notes" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$notes" init -q
    git -C "$notes" add -A
    git -C "$notes" commit -q --allow-empty -m "init: capture existing notes"
fi

file="$notes/$topic.md"
[ -f "$file" ] || printf '# %s\n\n' "$topic" > "$file"

entry="$(printf '## %s\n**%s** · %s\n\n%s\n' "$title" "$(date '+%Y-%m-%d %H:%M')" "\`$source\`" "$body")"
tmp="$(mktemp "$notes/.entry.XXXXXX")"
{
    head -n 1 "$file"
    printf '\n%s\n\n' "$entry"
    tail -n +2 "$file" | sed '/./,$!d'
} > "$tmp"
mv "$tmp" "$file"

git -C "$notes" add -- "$topic.md"
git -C "$notes" commit -q -m "$topic: $title" -- "$topic.md"
echo "$file $(git -C "$notes" rev-parse --short HEAD)"
