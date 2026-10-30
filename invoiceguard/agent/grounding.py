"""Grounding check for model-written text: every number in it must appear in the facts it was given."""

from __future__ import annotations

import json
import re

NUM = re.compile(r"(?<![\w.-])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.-])\d+(?:\.\d+)?")
SKIP = re.compile(r"\b(?:[A-Z]{2,4}-[\w-]+|\d{4}-\d{2}-\d{2}|PO-\d+|V\d{4}|DOC-\d{3})\b")  # ids and dates


def numbers(text: str) -> list[float]:
    out = []
    for m in NUM.finditer(SKIP.sub(" ", text)):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def unsupported(text: str, facts) -> list[float]:
    """Numbers in `text` that are not in `facts` (allowing rounding and percent forms)."""
    blob = json.dumps(facts, default=str)
    pool = set(numbers(blob))
    pool |= {round(v * 100, 1) for v in pool if v <= 1}
    pool |= {1, 2, 3, 4}  # small counting words ("two lines") are not facts
    bad = []
    for v in numbers(text):
        if not any(abs(v - p) <= max(0.011, 0.005 * abs(p)) for p in pool):
            bad.append(v)
    return bad
