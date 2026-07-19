"""rockycode engine: the model-and-UI-agnostic agent core.

The engine runs the ReAct loop and emits a stream of events. UIs (TUI now,
desktop later), the trajectory logger, and the future game layer are all
just subscribers to that stream — none of them are imported here.
"""
from rockycode.engine.events import AgentState, Event
from rockycode.engine.loop import Engine

__all__ = ["Engine", "Event", "AgentState"]
