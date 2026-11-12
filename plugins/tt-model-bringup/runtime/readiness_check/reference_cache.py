# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
On-disk cache for HF reference outputs computed on CPU by the readiness runners.

The HF reference (greedy completion in `run_autoregressive`, top-K reference
file in `generate`) depends only on the HF checkpoint, the tokenizer, the
prompt and the generation settings. Stages 6-10 re-run the same check many
times; recomputing a large model on CPU each time costs minutes to hours.

Key: model id, model revision, tokenizer fingerprint, prompt hash, generation
settings, plus the torch and transformers versions. A change to any of them is
a miss. When the revision cannot be determined (an unresolvable hub id) the
cache is bypassed rather than risk a stale hit.

Location: `$READINESS_REFERENCE_CACHE` if set, else
`<model_dir>/readiness/reference_cache/`. Set `READINESS_REFERENCE_CACHE=off`
(or `0`, `none`, `false`) to disable it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

CACHE_ENV = "READINESS_REFERENCE_CACHE"
CACHE_RELPATH = Path("readiness") / "reference_cache"
DISABLED_VALUES = {"off", "0", "none", "false", "no"}
SCHEMA = "readiness_reference_cache_v1"

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".gguf")
CONFIG_FILES = ("config.json", "generation_config.json")
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "tokenizer.model",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "chat_template.json",
    "chat_template.jinja",
)


def cache_dir(model_dir: Optional[Path]) -> Optional[Path]:
    """Resolve the cache directory, or None when caching is disabled or has no location."""
    env = os.environ.get(CACHE_ENV)
    if env is not None and env.strip():
        if env.strip().lower() in DISABLED_VALUES:
            return None
        return Path(env).expanduser()
    if model_dir is None:
        return None
    return Path(model_dir) / CACHE_RELPATH


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_files(directory: Path, names: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in names:
        path = directory / name
        if path.is_file():
            out[name] = _sha256_bytes(path.read_bytes())
    return out


def _resolve_hub_snapshot(hf_model_id: str) -> Optional[Path]:
    """Return the local snapshot directory of a hub model id, without network access."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return None
    try:
        found = try_to_load_from_cache(hf_model_id, "config.json")
    except Exception:
        return None
    if isinstance(found, str) and found:
        return Path(found).parent
    return None


def model_revision(hf_model_id: str) -> Optional[str]:
    """
    A string that changes when the checkpoint changes, or None if unknown.

    Local directory: hash of the config files plus name, size and mtime of each
    weight file. Hub id: the snapshot commit directory of the local HF cache.
    """
    local = Path(hf_model_id).expanduser()
    if local.is_dir():
        parts: Dict[str, Any] = {"config": _hash_files(local, CONFIG_FILES), "weights": []}
        for path in sorted(local.rglob("*")):
            if path.is_file() and path.suffix in WEIGHT_SUFFIXES:
                stat = path.stat()
                parts["weights"].append([str(path.relative_to(local)), stat.st_size, stat.st_mtime_ns])
        return "local:" + _sha256_bytes(json.dumps(parts, sort_keys=True).encode())
    snapshot = _resolve_hub_snapshot(hf_model_id)
    if snapshot is not None and snapshot.parent.name == "snapshots":
        return "hub:" + snapshot.name
    return None


def tokenizer_fingerprint(hf_model_id: str, tokenizer: Any) -> str:
    """Hash of the tokenizer files when they are on disk, plus class name and length."""
    local = Path(hf_model_id).expanduser()
    directory = local if local.is_dir() else _resolve_hub_snapshot(hf_model_id)
    files = _hash_files(directory, TOKENIZER_FILES) if directory is not None else {}
    try:
        length = len(tokenizer)
    except Exception:
        length = None
    parts = {"class": type(tokenizer).__name__, "len": length, "files": files}
    return _sha256_bytes(json.dumps(parts, sort_keys=True).encode())


def prompt_hash(value: Any) -> str:
    """Hash of a prompt: text, token ids, or any JSON-serialisable description."""
    if isinstance(value, bytes):
        return _sha256_bytes(value)
    if isinstance(value, str):
        return _sha256_bytes(value.encode("utf-8"))
    return _sha256_bytes(json.dumps(value, sort_keys=True).encode())


def _library_versions() -> Dict[str, Optional[str]]:
    versions: Dict[str, Optional[str]] = {}
    for name in ("torch", "transformers"):
        try:
            versions[name] = __import__(name).__version__
        except Exception:
            versions[name] = None
    return versions


class ReferenceCache:
    """One cache directory. Entries are `<kind>-<key>.<ext>` plus a `.key.json` sidecar."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = Path(directory) if directory is not None else None

    @classmethod
    def for_model_dir(cls, model_dir: Optional[Path]) -> "ReferenceCache":
        return cls(cache_dir(model_dir))

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    @staticmethod
    def make_key(
        *,
        kind: str,
        hf_model_id: str,
        revision: Optional[str],
        tokenizer: str,
        prompt: str,
        settings: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Return the full key dict, or None when the revision is unknown (no caching)."""
        if revision is None:
            return None
        return {
            "schema": SCHEMA,
            "kind": kind,
            "hf_model_id": str(hf_model_id),
            "revision": revision,
            "tokenizer": tokenizer,
            "prompt": prompt,
            "settings": settings,
            "versions": _library_versions(),
        }

    @staticmethod
    def digest(key: Dict[str, Any]) -> str:
        return _sha256_bytes(json.dumps(key, sort_keys=True).encode())[:32]

    def _path(self, key: Dict[str, Any], ext: str) -> Path:
        assert self.directory is not None
        return self.directory / f"{key['kind']}-{self.digest(key)}{ext}"

    def _key_matches(self, key: Dict[str, Any]) -> bool:
        sidecar = self._path(key, ".key.json")
        try:
            return json.loads(sidecar.read_text(encoding="utf-8")) == key
        except (OSError, ValueError):
            return False

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            # The default location is inside the autoport directory; keep cached tensors out of commits.
            ignore.write_text("*\n", encoding="utf-8")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _store_key(self, key: Dict[str, Any]) -> None:
        self._write_atomic(self._path(key, ".key.json"), json.dumps(key, indent=2, sort_keys=True).encode())

    # JSON payloads (token ids).

    def get_json(self, key: Optional[Dict[str, Any]]) -> Optional[Any]:
        if not self.enabled or key is None or not self._key_matches(key):
            return None
        try:
            return json.loads(self._path(key, ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put_json(self, key: Optional[Dict[str, Any]], value: Any) -> Optional[Path]:
        if not self.enabled or key is None:
            return None
        path = self._path(key, ".json")
        self._write_atomic(path, json.dumps(value).encode())
        self._store_key(key)
        return path

    # File payloads (reference files).

    def get_file(self, key: Optional[Dict[str, Any]], destination: Path, ext: str) -> Optional[Path]:
        """Copy a cached file to `destination` byte for byte; return it, or None on a miss."""
        if not self.enabled or key is None or not self._key_matches(key):
            return None
        source = self._path(key, ext)
        if not source.is_file():
            return None
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        return destination

    def put_file(self, key: Optional[Dict[str, Any]], source: Path, ext: str) -> Optional[Path]:
        if not self.enabled or key is None:
            return None
        path = self._path(key, ext)
        self._write_atomic(path, Path(source).read_bytes())
        self._store_key(key)
        return path
