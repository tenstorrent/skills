"""Run the real command snippets from the tt-serve-model skills on synthetic data.

The skills are Markdown, so these tests pull the code blocks out of the files and
execute them. They cover the cases a review of the plugin asked for:

- skill handoffs use the installed plugin namespace, and name skills that exist;
- two model lookups in one scratchpad never return the first model's entry;
- an unsupported model or device clears the selection instead of reusing an old one;
- the override check compares the selected nested entry, not the whole document;
- V6 reads model-generated code without ever running it.
"""

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1] / "plugins/tt-serve-model"
MODEL_SPEC_MD = PLUGIN / "knowledge/recipes/tt-inference-server/model-spec.md"
SPEC_MD = PLUGIN / "skills/retrieve/spec.md"
OVERRIDE_MD = PLUGIN / "skills/launch/override.md"
GENERATION_MD = PLUGIN / "skills/verify/health-and-generation.md"


def heredoc_blocks(path: Path) -> list[str]:
    """Python bodies of every `<<'PY' ... PY` heredoc in a skill file."""
    return re.findall(r"<<'PY'\n(.*?)\nPY\n", path.read_text(), re.S)


def spec_entry(repo: str, device: str, impl: str, image: str) -> dict:
    return {
        "hf_model_repo": repo,
        "device_type": device,
        "inference_engine": "vLLM",
        "impl": {"impl_id": impl},
        "docker_image": image,
        "model_name": repo.split("/")[-1],
        "model_type": "LLM",
        "device_model_spec": {"max_context": 4096, "max_concurrency": 8, "default_impl": impl},
    }


def make_spec() -> dict:
    gpt = spec_entry("openai/gpt-oss-120b", "P300X2", "gpt_oss", "image-gpt")
    qwen = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
    return {
        "model_specs": {
            "openai/gpt-oss-120b": {"P300X2": {"vLLM": {"gpt_oss": gpt}}},
            "Qwen/Qwen3-32B": {"P300X2": {"vLLM": {"tt_transformers": qwen}}},
        }
    }


class Scratch:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "spec.json").write_text(json.dumps(make_spec()))

    def close(self) -> None:
        self._tmp.cleanup()

    def query(self, model: str, device: str) -> str:
        code = heredoc_blocks(MODEL_SPEC_MD)[0]
        run = subprocess.run(
            [sys.executable, "-", str(self.dir / "spec.json"), model, device, str(self.dir)],
            input=code, capture_output=True, text=True, check=True,
        )
        return run.stdout

    def selection(self) -> dict | None:
        path = self.dir / "selection.json"
        return json.loads(path.read_text()) if path.exists() else None

    def read_selection(self, model: str, device: str) -> str:
        """Run the selection block from retrieve/spec.md and return what it echoes."""
        text = SPEC_MD.read_text()
        block = text[text.index("SEL=$("): text.index("esac\n") + len("esac\n")]
        run = subprocess.run(
            ["bash", "-c", block + '\necho "E=$E"'],
            env={"SCRATCH": str(self.dir), "MODEL": model, "DEV": device, "PATH": "/usr/bin:/bin:/usr/local/bin"},
            capture_output=True, text=True, check=True,
        )
        return run.stdout


class NamespaceTests(unittest.TestCase):
    def test_no_unavailable_tt_namespace_remains(self):
        pattern = re.compile(r"\btt:(discover|retrieve|launch|verify|run|note)\b")
        offenders = [str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*.md") if pattern.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_every_handoff_names_an_existing_skill(self):
        skills = {p.name for p in (PLUGIN / "skills").iterdir() if p.is_dir()}
        used = set()
        for p in PLUGIN.rglob("*.md"):
            used |= set(re.findall(r"\btt-serve-model:([a-z]+)", p.read_text()))
        self.assertTrue(used, "expected handoffs to use the plugin namespace")
        self.assertEqual(used - skills, set())

    def test_note_is_an_optional_tt_buddy_dependency(self):
        for rel in ("skills/discover/SKILL.md", "skills/retrieve/SKILL.md"):
            text = (PLUGIN / rel).read_text()
            self.assertIn("`tt-buddy:note`", text)
            self.assertIn("only if the `tt-buddy` plugin is installed", text)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Scratch()
        self.addCleanup(self.scratch.close)

    def test_two_lookups_return_the_second_models_entry(self):
        self.scratch.query("openai/gpt-oss-120b", "P300X2")
        self.scratch.query("Qwen/Qwen3-32B", "P300X2")
        # Both entry files are in the scratchpad now, as in a real session.
        self.assertEqual(len(list(self.scratch.dir.glob("entry_*.json"))), 2)
        sel = self.scratch.selection()
        self.assertEqual(sel["model"], "Qwen/Qwen3-32B")
        self.assertTrue(sel["entry"].endswith("entry_tt_transformers.json"))
        out = self.scratch.read_selection("Qwen/Qwen3-32B", "P300X2")
        self.assertIn("entry_tt_transformers.json", out)
        self.assertNotIn("entry_gpt_oss.json", out)

    def test_unknown_model_after_success_clears_the_selection(self):
        self.scratch.query("Qwen/Qwen3-32B", "P300X2")
        out = self.scratch.query("no/such-model", "P300X2")
        self.assertIn("NOMODEL", out)
        self.assertIsNone(self.scratch.selection())
        self.assertIn("entry=NOENTRY", self.scratch.read_selection("no/such-model", "P300X2"))

    def test_unsupported_device_after_success_clears_the_selection(self):
        self.scratch.query("Qwen/Qwen3-32B", "P300X2")
        out = self.scratch.query("Qwen/Qwen3-32B", "P150")
        self.assertIn("NOENTRY", out)
        self.assertIsNone(self.scratch.selection())

    def test_reader_rejects_a_selection_for_another_model(self):
        self.scratch.query("openai/gpt-oss-120b", "P300X2")
        out = self.scratch.read_selection("Qwen/Qwen3-32B", "P300X2")
        self.assertIn("MISMATCH", out)
        self.assertIn("E=\n", out + "\n")

    def test_reader_accepts_a_bare_model_name_and_lowercase_device(self):
        self.scratch.query("Qwen/Qwen3-32B", "P300X2")
        out = self.scratch.read_selection("qwen3-32b", "p300x2")
        self.assertIn("entry_tt_transformers.json", out)


class OverrideTests(unittest.TestCase):
    def compare(self, override: dict, entry: dict) -> str:
        code = next(b for b in heredoc_blocks(OVERRIDE_MD) if "override_entry" in b)
        with tempfile.TemporaryDirectory() as tmp:
            o, e = Path(tmp) / "override.json", Path(tmp) / "entry.json"
            o.write_text(json.dumps(override))
            e.write_text(json.dumps(entry))
            run = subprocess.run([sys.executable, "-", str(o), str(e)], input=code, capture_output=True, text=True, check=True)
        return run.stdout.strip()

    def wrap(self, entry: dict) -> dict:
        return {
            "schema_version": "0.1.0",
            "model_specs": {
                entry["hf_model_repo"]: {entry["device_type"]: {entry["inference_engine"]: {entry["impl"]["impl_id"]: entry}}}
            },
        }

    def test_unchanged_entry_in_a_spec_document_matches(self):
        entry = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
        self.assertEqual(self.compare(self.wrap(entry), entry), "override_diff=matches upstream entry structurally")

    def test_changed_image_is_reported(self):
        entry = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
        changed = dict(entry, docker_image="image-other")
        self.assertEqual(self.compare(self.wrap(changed), entry), "override_diff=differs in 1 key(s): docker_image")

    def test_changed_limit_is_reported(self):
        entry = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
        changed = json.loads(json.dumps(entry))
        changed["device_model_spec"]["max_context"] = 1
        self.assertEqual(self.compare(self.wrap(changed), entry), "override_diff=differs in 1 key(s): device_model_spec")

    def test_override_missing_the_selected_entry_is_unavailable(self):
        entry = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
        other = spec_entry("openai/gpt-oss-120b", "P300X2", "gpt_oss", "image-gpt")
        self.assertTrue(self.compare(self.wrap(other), entry).startswith("override_diff=unavailable"))

    def test_flat_document_is_unavailable_not_a_false_match(self):
        entry = spec_entry("Qwen/Qwen3-32B", "P300X2", "tt_transformers", "image-qwen")
        self.assertTrue(self.compare(entry, entry).startswith("override_diff=unavailable"))


class V6Tests(unittest.TestCase):
    """V6 parses a served model's code. It must never run it."""

    FIB = "def fib(n):\n    a, b, out = 1, 1, []\n    for _ in range(n):\n        out.append(a)\n        a, b = b, a + b\n    return out\n"

    @classmethod
    def block(cls) -> str:
        return next(b for b in heredoc_blocks(GENERATION_MD) if "v6_check" in b)

    def check(self, reply: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "v6.json"
            f.write_text(json.dumps({"choices": [{"message": {"content": reply}}]}))
            run = subprocess.run([sys.executable, "-", str(f)], input=self.block(), capture_output=True, text=True, check=True)
        return run.stdout.strip()

    def fenced(self, code: str) -> str:
        return "```python\n" + code + "```"

    def test_the_check_contains_no_way_to_run_code(self):
        self.assertIsNone(re.search(r"\b(exec|eval|compile|__import__|subprocess|os\.system)\b\s*[(.]", self.block()))
        self.assertNotIn("import subprocess", self.block())

    def test_valid_fib_is_consistent_and_says_it_was_not_executed(self):
        self.assertEqual(self.check(self.fenced(self.FIB)), "v6_check=consistent (static check only, not executed)")

    def test_pathlib_write_is_not_run(self):
        # The reviewer's first route: passed a text filter and wrote a host file.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"
            code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n" + self.FIB
            out = self.check(self.fenced(code))
            self.assertFalse(marker.exists())
        self.assertTrue(out.startswith("v6_check="))

    def test_constructed_expression_is_not_run(self):
        # The reviewer's second route: a function returns text that gets eval()'d.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"
            code = (
                "def fib(n):\n"
                f"    return \"__import__('pathlib').Path({str(marker)!r}).write_text('x') or [1, 1]\"\n"
                f"open({str(marker)!r}, 'w').write('x')\n"
            )
            self.check(self.fenced(code))
            self.assertFalse(marker.exists())

    def test_syntax_error(self):
        self.assertTrue(self.check(self.fenced("def fib(n:\n    return n\n")).startswith("v6_check=syntax error at line"))

    def test_no_code_block(self):
        self.assertEqual(self.check("Sure! Here is the answer."), "v6_check=no fenced code block in reply")
        self.assertEqual(self.check(""), "v6_check=no fenced code block in reply")

    def test_no_function_named_fib(self):
        self.assertEqual(self.check(self.fenced("def fibonacci(n):\n    return [1]\n")), "v6_check=no function named fib")

    def test_undefined_name(self):
        out = self.check(self.fenced("def fib(n):\n    return [a, b]\n"))
        self.assertEqual(out, "v6_check=undefined name(s): a, b")

    def test_function_that_never_returns_a_value(self):
        out = self.check(self.fenced("def fib(n):\n    x = n\n"))
        self.assertEqual(out, "v6_check=fib never returns a value")

    def test_while_true_without_exit_is_flagged(self):
        out = self.check(self.fenced("def fib(n):\n    while True:\n        n += 1\n    return n\n"))
        self.assertIn("possible infinite loop at line 2", out)

    def test_while_true_with_break_is_fine(self):
        code = "def fib(n):\n    out = [1]\n    while True:\n        break\n    return out\n"
        self.assertEqual(self.check(self.fenced(code)), "v6_check=consistent (static check only, not executed)")

    def test_builtins_imports_and_comprehensions_are_not_undefined(self):
        code = "import math\ndef fib(n):\n    return [int(math.sqrt(i * i)) for i in range(n)]\n"
        self.assertEqual(self.check(self.fenced(code)), "v6_check=consistent (static check only, not executed)")

    def test_docs_do_not_claim_execution(self):
        text = GENERATION_MD.read_text()
        self.assertIn("never run it", text)
        self.assertNotIn("run only the survivors", text)
        self.assertIn("not executed", text)


if __name__ == "__main__":
    unittest.main()
