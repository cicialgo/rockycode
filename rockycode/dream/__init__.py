"""rockycode dream: offline consolidation over reflect records (evolve v2).

Reflect captures each session warm on its own model (dream/reflect.py); the
dream is the offline half — cold-reflect stragglers, fold patterns, reconcile
facts, draft proposals. No daemon: launch catch-up + manual `rockycode dream`.
"""
from rockycode.dream.core import CloudChat, DreamReport, DreamRunner

__all__ = ["CloudChat", "DreamReport", "DreamRunner"]
