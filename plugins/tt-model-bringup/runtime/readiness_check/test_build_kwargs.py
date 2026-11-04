import argparse
import json

import pytest

from readiness_check.build_kwargs import add_build_kwargs_args, parse_build_kwargs


def _parse(argv):
    parser = argparse.ArgumentParser()
    add_build_kwargs_args(parser)
    return parser.parse_args(argv)


def test_defaults_to_empty_kwargs():
    assert parse_build_kwargs(_parse([])) == {}


def test_inline_json_and_file_merge_with_inline_winning(tmp_path):
    path = tmp_path / "build.json"
    path.write_text(json.dumps({"precision_config": "a.json", "kv_cache_dtype": "bf16"}))
    args = _parse(["--build-kwargs-file", str(path), "--build-kwargs", '{"precision_config": "b.json"}'])
    assert parse_build_kwargs(args) == {"precision_config": "b.json", "kv_cache_dtype": "bf16"}


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_invalid_or_non_object_json_exits(text):
    with pytest.raises(SystemExit):
        parse_build_kwargs(_parse(["--build-kwargs", text]))
