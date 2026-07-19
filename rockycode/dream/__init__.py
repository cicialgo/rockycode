"""rockycode dream: idle-time memory consolidation on a local Ollama model.

M2 of docs/memory-dream.md — episode digestion, reconciliation, project
state, re-embedding. No daemon: manual `rockycode dream` now, TUI idle
trigger in M3.
"""
from rockycode.dream.core import DreamReport, DreamRunner, OllamaChat

__all__ = ["DreamReport", "DreamRunner", "OllamaChat"]
