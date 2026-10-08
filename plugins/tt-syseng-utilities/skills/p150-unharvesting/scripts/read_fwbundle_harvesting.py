#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only: print product_spec_harvesting per board from Blackhole .fwbundle files.

Reuses the internals of the tt-update-tensix-disable-count package, which has no
read-only mode of its own. Never writes to the input bundles.

Needs `tt-update-tensix-disable-count` installed, plus either `grpcio-tools` (preferred)
or a `protoc` binary on PATH to compile the firmware-table protobuf.

Usage: read_fwbundle_harvesting.py BUNDLE [BUNDLE ...] [--board P150A-1 ...] [--all-boards]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import tt_update_tensix_disable_count as pkg

PKG_DIR = Path(pkg.__file__).parent
sys.path.append(str(PKG_DIR))
import fwtable_tooling  # noqa: E402
import tt_boot_fs  # noqa: E402
import tt_fwbundle  # noqa: E402

DEFAULT_BOARDS = ["P150A-1", "P150B-1", "P150C-1"]
P150_TENSIX_COLUMNS = 14
ROWS_PER_COLUMN = 10


def protoc_command() -> list[str]:
    try:
        import grpc_tools.protoc  # noqa: F401

        return [sys.executable, "-m", "grpc_tools.protoc"]
    except ImportError:
        pass
    protoc = shutil.which("protoc")
    if protoc is None:
        sys.exit("need grpcio-tools (pip install grpcio-tools) or protoc on PATH")
    return [protoc]


def load_fw_table_pb2(out_dir: Path):
    os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
    subprocess.run(
        protoc_command()
        + [f"--python_out={out_dir}", str(PKG_DIR / "fw_table.proto"), "-I", str(PKG_DIR)],
        check=True,
        capture_output=True,
    )
    sys.path.append(str(out_dir))
    import fw_table_pb2

    return fw_table_pb2


def read_bundle(bundle: Path, boards, fw_table_pb2):
    meta = tt_fwbundle.bundle_metadata(bundle)
    available = fwtable_tooling.get_board_names_with_cmfwcfg_from_bundle_metadata(meta)
    selected = sorted(available) if boards is None else [b for b in boards if b in available]
    v = meta.get("manifest", {}).get("bundle_version", {})
    print(f"{bundle}  (fw {v.get('fwId', '?')}.{v.get('releaseId', '?')}.{v.get('patch', '?')})")
    with tarfile.open(bundle, "r:gz") as tar, tempfile.TemporaryDirectory() as td:
        names = set(tar.getnames())
        for b in selected:
            name = next(n for n in (f"./{b}/image.bin", f"{b}/image.bin") if n in names)
            b16 = Path(td) / f"{b}.b16"
            b16.write_bytes(tar.extractfile(name).read())
            bootfs = tt_boot_fs.BootFs.from_binary(tt_boot_fs.extract_all(b16, input_base64=True))
            raw = bootfs.entries[fwtable_tooling.BOOTFS_FWTABLE_NAME].data
            table = fw_table_pb2.FwTable()
            table.ParseFromString(fwtable_tooling.nanopb_remove_framing(raw))
            ps = table.product_spec_harvesting
            dc = ps.tensix_col_disable_count
            cores = f"{(P150_TENSIX_COLUMNS - dc) * ROWS_PER_COLUMN} cores" if b.startswith("P150") else "n/a"
            print(
                f"  {b:<10} tensix_col_disable_count={dc}  ({cores})"
                f"  dram_disable_count={ps.dram_disable_count}  eth_disabled={ps.eth_disabled}"
            )
    for b in [] if boards is None else [b for b in boards if b not in available]:
        print(f"  {b:<10} not in bundle")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundles", nargs="+", type=Path)
    ap.add_argument("--board", action="append", help=f"repeatable; default: {' '.join(DEFAULT_BOARDS)}")
    ap.add_argument("--all-boards", action="store_true", help="every board in the bundle")
    args = ap.parse_args()
    boards = None if args.all_boards else (args.board or DEFAULT_BOARDS)
    with tempfile.TemporaryDirectory() as td:
        pb2 = load_fw_table_pb2(Path(td))
        for bundle in args.bundles:
            read_bundle(bundle, boards, pb2)


if __name__ == "__main__":
    main()
