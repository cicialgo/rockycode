"""Desktop events and approval bridge; upstream Runtime owns tool composition."""
import os

from .messages import HostError, text

# Catalog keys (desktop.diagnostic.*); the client renders them in its language.
LIMITATIONS = ["diagnostic.limitationServices", "diagnostic.limitationDesktop", "diagnostic.limitationPlatforms"]


class EngineFailure(RuntimeError):
    """A turn the engine ended with an error. `kind` and `params` (engine/errors.py)
    let the host show a failed model request as one translated sentence."""

    def __init__(self, message, kind="", params=None):
        super().__init__(message)
        self.kind, self.params = kind, dict(params or {})


def launch_model(named):
    """The model a new task starts on: the configured default when its route
    works (engine/route.py), else the first model that has a key or runs
    locally. "" = nothing can run yet. "Ready" must mean a task can be sent."""
    from rockycode.engine import providers
    from rockycode.engine import route
    if named:
        try:
            route.route(named)
            return named
        except route.RouteError:
            pass
    return next((choice.id for choice in providers.configured_choices() if choice.configured), "")


def setup_command():
    """The terminal command that opens rocky's first-run setup with THIS host's
    Python — exact for a source checkout and for an installed wheel alike."""
    import shlex
    import sys
    from pathlib import Path
    script = Path(sys.executable).parent / "rockycode"
    return shlex.quote(str(script)) if script.is_file() else f"{shlex.quote(sys.executable)} -m rockycode.cli"


def inspect_runtime(fixture=False):
    if fixture:
        return {"mode": "fixture", "available": True, "model": "fixture", "version": "fixture/1", "limitations": ["diagnostic.limitationFixture"]}
    try:
        import rockycode
        from rockycode.runtime import build_runtime  # noqa: F401 — require the shared core
        from rockycode.config import load
        from rockycode.onboarding import bootstrap_credentials, keychain_silent
        bootstrap_credentials()
        model = launch_model(os.environ.get("ROCKYCODE_MODEL") or str(load().get("model") or ""))
        return {"mode": "live", "available": bool(model), "model": model, "version": rockycode.__version__, "limitations": LIMITATIONS,
                "error": None if model else text("diagnostic.configureModel"), "error_key": None if model else "diagnostic.configureModel",
                "setup_command": setup_command(),
                # keys the system keychain should hold and did not give: names, for one sentence on the card
                "keychain_silent": keychain_silent()}
    except ImportError:
        return {"mode": "live", "available": False, "model": "", "version": "", "limitations": LIMITATIONS,
                "error": text("diagnostic.missingRuntime"), "error_key": "diagnostic.missingRuntime"}


class EngineAdapter:
    def __init__(self, model, workspace, history, approve, **options):
        from pathlib import Path
        from rockycode.runtime import build_runtime
        # `approve` is the startup consent for MCP/LSP processes; the host then
        # installs the shared ToolApprover as engine.approver (Host._approver).
        self.runtime = build_runtime(
            model=model, workdir=Path(workspace), source="desktop", approver=approve,
            # No plan-mode UI on the desktop yet, so interactive planning tools
            # stay off here; the step budget is the desktop's explicit choice.
            startup_approver=approve, loops=False, **{"max_steps": 50, "planning": False, **options},
        )
        self.engine = self.runtime.engine
        # The desktop owns presentation; never launch the system browser here.
        self.engine.artifact_opener = lambda url: None
        if history:
            self.engine.resume(history)

    async def start(self):
        await self.runtime.start()

    def describe(self):
        registry = getattr(self.engine, "artifact_registry", None)
        # This task's artifacts only, oldest first: the task view shows the last one.
        # The project's other artifacts belong to the tasks that made them.
        return {**self.runtime.describe(), "artifacts": [
            {"id": record.artifact_id, "path": str(record.path), "title": record.title}
            for record in reversed(registry.records("session"))
        ] if registry else []}

    @property
    def history(self):
        return self.engine.history

    @property
    def tools(self):
        return sorted(self.engine.registry)

    async def stream(self, prompt):
        from rockycode.server import _event_to_notification
        from rockycode.engine.events import EngineError
        gen = self.engine.run_turn(prompt)
        try:
            async for event in gen:
                if isinstance(event, EngineError) and not event.message.startswith("compaction summary failed"):
                    raise EngineFailure(event.message, event.kind, event.params)
                mapped = _event_to_notification("", event)
                if mapped:
                    yield mapped["method"].removeprefix("session/"), mapped["params"]
        finally:
            await gen.aclose()

    async def close(self):
        await self.runtime.close()
        if self.runtime.cleanup_errors:
            raise HostError("diagnostic.cleanup")
