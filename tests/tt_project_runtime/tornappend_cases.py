# Torn-tail tests (outbox._append, project.durable_append) of tests/test_tt_project_runtime.py, which
# runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file. A power cut that cuts the last line short must never lose the next entry.
from __future__ import annotations


def test_outbox_entry_after_a_torn_last_line_is_kept_and_the_fragment_moves_to_corrupt(env):
    from ttp import outbox
    e1 = outbox.add("demo", ["say", "one"], "id-1", None)
    outbox.add("demo", ["say", "two"], "id-2", None)
    p = outbox.path("demo")
    line1 = p.read_bytes().split(b"\n")[0] + b"\n"
    os.truncate(p, len(line1) + 20)    # the power cut tore entry 2's line
    fragment = p.read_bytes()[len(line1):].decode()
    e3 = outbox.add("demo", ["say", "three"], "id-3", None)
    assert outbox.entries("demo") == [e1, e3]
    with outbox.locked("demo"):
        outbox.drop("demo", "id-1")
    assert outbox.entries("demo") == [e3]
    corrupt = outbox.folder() / "demo.jsonl.corrupt"
    assert corrupt.read_text() == fragment + "\n"
    assert stat.S_IMODE(corrupt.stat().st_mode) == 0o600
    with outbox.locked("demo"):
        outbox.drop("demo", "id-3")
    assert not p.exists() and corrupt.read_text() == fragment + "\n", "a fragment is moved once"


def test_outbox_rejected_file_append_after_a_torn_tail_stays_parsable(env):
    from ttp import outbox
    outbox.add("demo", ["say", "one"], "id-1", None)
    rej = outbox.folder() / "demo.rejected.jsonl"
    rej.write_text('{"id": "old", "rej')
    with outbox.locked("demo"):
        outbox.drop("demo", "id-1", rejected="no such task")
    assert json.loads(rej.read_text().splitlines()[1])["id"] == "id-1"


def test_durable_append_after_a_torn_last_line_keeps_the_next_entry(tmp_path):
    from ttp.project import durable_append
    f = tmp_path / "log.jsonl"
    lines = [json.dumps({"n": n, "pad": "x" * 40}) + "\n" for n in (1, 2, 3)]
    durable_append(f, lines[0])
    durable_append(f, lines[1])
    os.truncate(f, len(lines[0]) + 20)
    durable_append(f, lines[2])
    got = []
    for line in f.read_text().splitlines():
        try:
            got.append(json.loads(line)["n"])
        except ValueError:
            pass
    assert got == [1, 3]
    assert f.read_text().endswith(lines[2]) and f.read_text().count("\n") == 3
    durable_append(f, "é\n")    # an untorn tail gets no extra newline; text is written as UTF-8
    assert f.read_text(encoding="utf-8").endswith(lines[2] + "é\n")


def test_upstream_outbox_after_a_torn_tail_gets_one_newline_not_a_blank_line(tmp_path):
    from ttp.project import durable_append
    f = tmp_path / "box.jsonl"
    f.write_text('{"fp": "a"}\n{"fp": "b", "te')
    durable_append(f, '{"fp": "c"}\n')
    assert f.read_text() == '{"fp": "a"}\n{"fp": "b", "te\n{"fp": "c"}\n'
