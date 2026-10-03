"""Seeded example processes.

Each demo file holds the source text as numbered clauses plus a process model
that cites those clauses. The model is hand-checked, which is why demo mode
needs no language model. The engine that runs on it is the same one that runs
on extracted models.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .model import Process, Sentence, validate_process

DEMO_DIR = Path(__file__).resolve().parent.parent / "demos"


def build_process(raw: dict, origin: str = "demo") -> Process:
    parts, sentences, pos = [], [], 0
    for i, clause in enumerate(raw["clauses"], start=1):
        prefix = f"{i}. "
        start = pos + len(prefix)
        sentences.append(Sentence(id=f"c{i}", text=clause, start=start, end=start + len(clause)))
        parts.append(prefix + clause)
        pos += len(prefix) + len(clause) + 1
    process = Process(
        title=raw["title"], text="\n".join(parts), sentences=sentences,
        actors=raw["actors"], subject=raw.get("subject"), persona=raw.get("persona"),
        states=raw["states"], transitions=raw["transitions"], facts=raw.get("facts", []),
        start=raw["start"], initial_facts=raw.get("initial_facts", []), origin=origin)
    problems = validate_process(process)
    if problems:
        raise ValueError(f"{raw['title']}: " + "; ".join(problems))
    return process


def list_demos() -> List[dict]:
    out = []
    for path in sorted(DEMO_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        out.append({"id": path.stem, "title": raw["title"], "blurb": raw.get("blurb", ""),
                    "clauses": len(raw["clauses"])})
    return out


def load_demo(demo_id: str) -> Process:
    path = DEMO_DIR / f"{demo_id}.json"
    if not path.exists():
        raise FileNotFoundError(demo_id)
    return build_process(json.loads(path.read_text(encoding="utf-8")))
