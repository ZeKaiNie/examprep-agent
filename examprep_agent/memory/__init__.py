# -*- coding: utf-8 -*-
"""Three-layer long-term memory: episodic events, semantic profile, procedures.

Design note: the layering (raw events → distilled profile → reusable procedure)
is inspired by the memory-tier ideas popularised by Letta/MemGPT (Apache-2.0).
No code was copied; this is an independent implementation with a different
storage model (plain JSON/JSONL in the workspace, no vector store or database).
"""
from .episodic import EpisodicMemory
from .procedural import ProceduralMemory
from .semantic import ERROR_LABELS, SemanticMemory, classify_error
from .store import LongTermMemory

__all__ = [
    "ERROR_LABELS",
    "EpisodicMemory",
    "LongTermMemory",
    "ProceduralMemory",
    "SemanticMemory",
    "classify_error",
]
