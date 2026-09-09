"""Loads the curated facts the agents are allowed to state.

Everything in knowledge/*.md is concatenated and handed to the model as the
only source of truth about DataRail. It is not scraped from the live site --
see the note at the top of knowledge/datarail.md for why.
"""

from __future__ import annotations

import os
from functools import lru_cache

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_KNOWLEDGE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "knowledge"))


class KnowledgeError(RuntimeError):
    """The knowledge base is missing or empty."""


@lru_cache(maxsize=4)
def load(directory: str = DEFAULT_KNOWLEDGE_DIR) -> str:
    """Return every markdown file in the knowledge directory, joined.

    Raises rather than returning an empty string. An agent with no knowledge
    base would answer every question by inventing one, which is the single
    worst failure this system can have.
    """
    if not os.path.isdir(directory):
        raise KnowledgeError("knowledge directory not found: " + directory)

    parts = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(directory, name)
        with open(path, "r", encoding="utf-8") as handle:
            body = handle.read().strip()
        if body:
            parts.append(body)

    if not parts:
        raise KnowledgeError("no markdown files with content in " + directory)

    return "\n\n---\n\n".join(parts)
