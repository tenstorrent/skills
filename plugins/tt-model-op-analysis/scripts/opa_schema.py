"""Column names, profiles and status values shared by every tt-model-op-analysis script."""

from __future__ import annotations

import csv
import pathlib

PROFILES = ("p100", "p150", "quasar")
STATUS_VALUES = ("✅", "⚠️", "❌", "n/a", "variant not used by port")
CONFIDENCE_VALUES = ("verified", "unverified")

OP_TABLE_BASE = (
    "id", "stage", "ttnn_api", "op_code", "device_op", "program_factory",
    "call_site", "evidence", "confidence", "grid_dependency",
)
CALL_TRACE_COLUMNS = ("id", "profile", "stage", "ops", "repeats", "launches_per_repeat", "notes")
MATCH_KEY = ("stage", "ttnn_api", "device_op", "program_factory")

QUASAR_STATUS_COLUMNS = ("quasar:as_written", "quasar:port")


def op_table_columns(profiles: list[str]) -> list[str]:
    columns = list(OP_TABLE_BASE)
    for profile in profiles:
        if profile == "quasar":
            columns += ["quasar:as_written", "quasar:port", "quasar:evidence"]
        else:
            columns += [f"{profile}:shapes", f"{profile}:launches", f"{profile}:status"]
    return columns


def status_columns(profiles: list[str]) -> list[str]:
    cols = []
    for profile in profiles:
        cols += list(QUASAR_STATUS_COLUMNS) if profile == "quasar" else [f"{profile}:status"]
    return cols


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with pathlib.Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict]) -> None:
    with pathlib.Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
