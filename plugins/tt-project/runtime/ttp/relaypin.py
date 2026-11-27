"""Two checks on how the coordinator reads a user's or relayed message.

`pin_line`: a relayed order that names a model is compared with the model pins on record (memory entries of
kind preference, decision or restriction, the config's tier models and the route settings' models), and a
name no pin holds gets one digest line, so the coordinator sees the clash before it acts on the order.

`opens_order`: a clause that opens a stop order or added scope (correction.answers reads it: a message with
such a clause and nothing contradicting a fact, status or figure the project stated is an order, no correction).
"""
from __future__ import annotations

import re

from . import relay

KINDS = ("preference", "decision", "restriction")   # memory kinds that record a pin
FAMILIES = frozenset("opus sonnet haiku fable codex gemini composer glm gpt".split())
_STOP = ("stop", "don't", "do not", "dont", "never", "halt", "pause", "cancel", "hold off", "no more")
_ADD = ("also", "and also", "additionally", "in addition", "plus", "add", "now also", "one more", "as well")
_LEAD = re.compile(r"^\W*(?:(?:no|nope|ok|okay|and|but|please|pls)\b\W*)+", re.I)


def _toks(name: str) -> list[str]:
    return name.lower().replace(".", "-").split("-")


def _model_family(name: str) -> str:
    return next((t for t in _toks(name) if t in FAMILIES), _toks(name)[0])


def _is_pin(name: str, pin: str) -> bool:
    """`name` (opus, opus-5-5) is `pin` (claude-opus-5-5) or a whole-token part of it."""
    a, b = _toks(name), _toks(pin)
    return any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


def model_names(text: str, terms: set[str] | frozenset = frozenset()) -> list[str]:
    """The model names in `text`: a family name, a known config model, or a 'name-5.5'-shaped term."""
    out = []
    for w in relay._words(text):
        if (w in FAMILIES or w in terms or relay._PIN_TERM.match(w) and w != "xhigh") and w not in out:
            out.append(w)
    return out


def _models(node) -> list[str]:
    """Every string under a `model` key of a config subtree."""
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in ([str(v).lower()] if k == "model" and isinstance(v, str)
                                                    else _models(v))]
    return []


def pins(p) -> list[tuple[str, str]]:
    """(model name, where) for each pin on record: memory first, newest first, then the config."""
    cfg = p.config()
    terms = relay.pin_terms(cfg)
    out = [(n, f"memory [{e['name']}]") for e in reversed(p._memory_entries()) if e["kind"] in KINDS
           for n in model_names(e["body"], terms)]
    out += [(m, "config") for m in _models(cfg.get("providers")) + _models(cfg.get("route")) if m not in ("auto", "default")]
    return out


def pin_line(p, text: str) -> str:
    """'relayed order names <model>; pin on record is <model> (<where>)' for the first model `text` names
    that no pin on record holds, or '' (no model named, no pin on record, or every name matches a pin). A
    'name-4'-shaped word counts as a model only in a known family or the family of a pin on record (`step-4` is no model)."""
    held = pins(p)
    if not held:
        return ""
    terms = relay.pin_terms(p.config())
    families = {_model_family(pin) for pin, _ in held}
    for name in model_names(text, terms):
        if name not in terms and _model_family(name) not in FAMILIES | families:
            continue
        if any(_is_pin(name, pin) for pin, _ in held):
            continue
        pin, where = next((x for x in held if _model_family(x[0]) == _model_family(name)), held[0])
        return f"relayed order names {name}; pin on record is {pin} ({where})"
    return ""


def opens_order(clause: str) -> bool:
    """Whether the lowercase `clause` opens with a stop or add word (after a bare 'no' or 'and')."""
    rest = _LEAD.sub("", clause)
    return any(rest == w or rest.startswith(w + " ") for w in _STOP + _ADD)
