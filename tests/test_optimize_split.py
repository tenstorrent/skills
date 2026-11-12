"""The optimize skill split keeps every original line, except the listed intentional edits."""
from __future__ import annotations

from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / 'plugins/tt-model-bringup/skills/optimize'
ORIGINAL = REPO / 'tests/data/optimize_SKILL_pre_split.md'

# Original line number -> text that must appear in the split files instead.
INTENTIONAL_EDITS = {
    # Eval workload instead of batch-1 latency as the optimization target.
    20: 'Optimize for the eval workload: the largest served batch',
    230: 'the eval workload (largest served batch, long answers) is the optimization target',
    264: 'Optimize for the eval workload: the largest served batch with long answers',
    # D3: BFP4 is a candidate here; $datatype-sweep (stage 8, P1 probe) decides.
    219: 'BFP4/LoFi candidates for FF1/FF3 or equivalent gate/up projections were measured and recorded',
    308: 'from the selected precision policy for that tensor group.',
    312: 'or the BFP4/LoFi candidate, record the lower-precision candidate result for `$datatype-sweep`',
    334: 'measure BFP4 attention-weight candidates as well.',
    469: 'Record the results as candidates for `$datatype-sweep`; do not ship BFP4 as the default from this stage.',
    # Relative links re-pointed for the references/ directory.
    107: 'as described in [the method reference](method.md)',
    113: '(opt-advisories.md#opt-015-sweep-blackhole-dram-readers-per-bank)',
    115: '(../../vllm-integration/references/prefill-features.md)',
    117: '(../../functional-decoder/SKILL.md#implement-chunked-prefill)',
    # Path map: models/common/modules moved to tt-transformers; prose casing.
    127: 'Only name a TTNN/runtime/API limitation after you have tried',
    229: 'remaining gap named as a TTNN/runtime/API limitation',
    166: 'Use `LMHead1D` from tt-transformers `src/tt_transformers/modules/lm_head/lm_head_1d.py`',
    172: 'Compare `models/common/sampling/` and tt-transformers `src/tt_transformers/modules/sampling/sampling_1d.py`',
    243: 'tt-transformers `src/tt_transformers/modules/attention/attention_1d.py`',
    244: 'tt-transformers `src/tt_transformers/modules/mlp/mlp_1d.py`',
    245: 'tt-transformers `src/tt_transformers/modules/lm_head/lm_head_1d.py`',
    246: 'tt-transformers `tests/modules/lm_head/test_lm_head_1d.py`',
    249: '`models/common/sampling/generator.py` and tt-transformers `src/tt_transformers/modules/sampling/sampling_1d.py`',
}


def split_files():
    return [SKILL_DIR / 'SKILL.md', *sorted((SKILL_DIR / 'references').glob('*.md'))]


def test_every_original_line_survives_the_split():
    present = set()
    for path in split_files():
        present.update(line.strip() for line in path.read_text().splitlines())
    missing = [
        (number, line[:100])
        for number, line in enumerate(ORIGINAL.read_text().splitlines(), start=1)
        if line.strip() and number not in INTENTIONAL_EDITS and line.strip() not in present
    ]
    assert not missing, missing


def test_intentional_edits_are_applied():
    combined = '\n'.join(path.read_text() for path in split_files())
    original = ORIGINAL.read_text().splitlines()
    for number, marker in INTENTIONAL_EDITS.items():
        assert marker in combined, (number, marker)
        assert original[number - 1].strip() not in combined, number


def test_main_skill_is_short_and_keeps_its_contract():
    text = (SKILL_DIR / 'SKILL.md').read_text()
    assert len(text.encode()) <= 15_000
    assert text.startswith('---\nname: optimize\n')
    assert '## Stop Rule' in text and '## Checklist' in text
    for reference in (SKILL_DIR / 'references').glob('*.md'):
        assert f'references/{reference.name}' in text, reference.name


def test_d3_and_path_map_hold_across_the_skill():
    combined = '\n'.join(path.read_text() for path in split_files())
    assert 'models/common/modules' not in combined
    assert 'models.common.modules' not in combined
    assert 'ttnn/runtime/API' not in combined
    assert 'Do not defer it to datatype sweep' not in combined
    assert 'mandatory BFP4' not in combined
    assert 'BFP4 attention-weight candidates are mandatory' not in combined
    assert 'Optimize primarily for batch-1' not in combined


def _anchors(path: Path) -> set[str]:
    slugs = set()
    for line in path.read_text().splitlines():
        match = re.match(r'#+\s+(.*)', line)
        if match:
            slug = re.sub(r'[^a-z0-9 _-]', '', match.group(1).strip().lower()).replace(' ', '-')
            slugs.add(slug)
    return slugs


def test_links_and_anchors_resolve():
    for path in split_files():
        for ref in re.findall(r'\]\(([^)\s]+)\)', path.read_text()):
            if '://' in ref:
                continue
            file_part, _, anchor = ref.partition('#')
            target = (path.parent / file_part).resolve() if file_part else path
            assert target.exists(), (path.name, ref)
            if anchor and target.suffix == '.md':
                assert anchor in _anchors(target), (path.name, ref)


def test_inbound_optimize_anchors_resolve():
    plugin = REPO / 'plugins/tt-model-bringup'
    anchors = _anchors(SKILL_DIR / 'SKILL.md')
    for path in plugin.rglob('*.md'):
        for anchor in re.findall(r'optimize/SKILL\.md#([A-Za-z0-9_-]+)', path.read_text()):
            assert anchor in anchors, (path, anchor)
