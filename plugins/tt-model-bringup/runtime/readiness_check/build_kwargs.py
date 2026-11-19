"""Command-line plumbing for generator build keyword arguments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


def add_build_kwargs_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--build-kwargs",
        type=str,
        default=None,
        help="JSON object of keyword arguments passed through to build_generator (for example a precision config path).",
    )
    parser.add_argument(
        "--build-kwargs-file",
        type=Path,
        default=None,
        help="JSON file whose top-level object is passed through to build_generator; --build-kwargs entries override it.",
    )


def parse_build_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    path = getattr(args, "build_kwargs_file", None)
    if path is not None:
        merged.update(_load_object(Path(path).read_text(), f"--build-kwargs-file {path}"))
    text = getattr(args, "build_kwargs", None)
    if text:
        merged.update(_load_object(text, "--build-kwargs"))
    return merged


def _load_object(text: str, source: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"{source}: invalid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{source}: expected a JSON object, got {type(value).__name__}")
    return value
