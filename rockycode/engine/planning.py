"""Host-owned planning lifecycle, shared by interactive clients.

Draft writes never authorize execution. Only a human approving the reviewed
revision can leave planning and produce an implementation request.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from rockycode.engine import planmode
from rockycode.engine.tools import Tool, _fn_schema

MAX_PLAN_BYTES = 128 * 1024
_PROMPT_TAG = "\n\n# Active planning contract\n"

ENTRY_MESSAGE = (
    "Enter interactive planning for the task we are discussing. Use the existing "
    "conversation rather than asking me to repeat it. Investigate what the code "
    "can answer, then discuss only decisions that need my input. If there is no "
    "task in the conversation yet, ask what I would like to plan."
)


class PlanningSession:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.path: Path | None = None
        self.status = "off"
        self.revision: str | None = None
        self._prompt_suffix = ""

    @property
    def active(self) -> bool:
        return self.status in {"planning", "review"}

    def _validate_path(self, path: Path) -> Path:
        wd = self.engine.workdir.resolve()
        root = wd / ".rockycode" / "plans"
        path = path if path.is_absolute() else wd / path
        # A plan must stay in its own directory, including after symlink changes.
        if root.resolve() != root or path.parent.resolve() != root or path.is_symlink():
            raise ValueError("plan path must be a regular file in .rockycode/plans")
        if path.suffix != ".md":
            raise ValueError("plan file must be Markdown")
        if path.exists() and not path.is_file():
            raise ValueError("plan path must be a regular file")
        return path.parent.resolve() / path.name

    def attach(self, path: Path) -> None:
        self.path = self._validate_path(path)
        self.status, self.revision = "planning", None
        self._save()

    def start(self) -> Path:
        if not self.active:
            # Validate before mkdir/touch as well as before every later read.
            self._validate_path(self.engine.workdir.resolve() / ".rockycode/plans/new.md")
            self.attach(planmode.create_plan_file(self.engine.workdir.resolve()))
        return self.path

    def read(self) -> tuple[str, str]:
        if self.path is None:
            raise ValueError("no plan in this session")
        path = self._validate_path(self.path)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_PLAN_BYTES + 1)
        except OSError as exc:
            raise ValueError("plan file is unavailable; keep planning or leave the mode") from exc
        if len(raw) > MAX_PLAN_BYTES:
            raise ValueError("plan is too large to review; shorten it before submitting")
        return raw.decode("utf-8", errors="replace"), hashlib.sha256(raw).hexdigest()

    def request_review(self) -> dict:
        if not self.active:
            raise ValueError("enter plan mode before submitting a plan")
        text, revision = self.read()
        if not text.strip():
            raise ValueError("write a plan before submitting it for review")
        self.status, self.revision = "review", revision
        self._save()
        return self.snapshot(include_text=True)

    def continue_discussion(self) -> None:
        if self.active and (self.status != "planning" or self.revision is not None):
            self.status, self.revision = "planning", None
            self._save()

    def approve(self, revision: str) -> str:
        if self.status != "review" or not revision or revision != self.revision:
            raise ValueError("review the current plan before approving it")
        text, current = self.read()
        if current != revision:
            self.continue_discussion()
            raise ValueError("the plan changed after review; review the new version before approving")
        rel = self.path.relative_to(self.engine.workdir.resolve())
        self.status = "approved"
        self._save()
        # Carry the exact approved content: a later file edit cannot silently
        # substitute a different plan between the click and the next model call.
        return (
            f"Plan approved — implement the plan below, phase by phase. Stay inside its scope. "
            f"This is the approved revision of {rel} ({revision[:12]}).\n\n{text}"
        )

    def leave(self) -> None:
        self.status, self.revision = "off", None
        self._save()

    def snapshot(self, *, include_text: bool = False) -> dict:
        data = {
            "status": self.status,
            "path": str(self.path.relative_to(self.engine.workdir.resolve())) if self.path else None,
            "revision": self.revision,
        }
        if include_text and self.path is not None:
            try:
                data["text"], data["current_revision"] = self.read()
            except ValueError as exc:
                data["text"], data["error"] = "", str(exc)
        return data

    def restore(self, data: dict | None) -> None:
        path = None
        if data and data.get("status") in {"planning", "review"}:
            if not isinstance(data.get("path"), str):
                raise ValueError("stored planning state has no valid plan path")
            path = self._validate_path(Path(data["path"]))
        self.path, self.status, self.revision = path, "off", None
        if path is not None:
            self.status = "planning"
            # Approval never resumes automatically; a pending review can be
            # presented again only if the saved content still matches.
            if data.get("status") == "review":
                try:
                    _, current = self.read()
                except ValueError:
                    current = None
                if current and current == data.get("revision"):
                    self.status, self.revision = "review", current
        self._save()

    def sync_prompt(self) -> None:
        if not getattr(self.engine, "history", None):
            return
        current = self.engine.history[0]["content"]
        base = (current[:-len(self._prompt_suffix)]
                if self._prompt_suffix and current.endswith(self._prompt_suffix) else current)
        self._prompt_suffix = ""
        if self.active:
            self._prompt_suffix = _PROMPT_TAG + planmode.marker(
                self.path.relative_to(self.engine.workdir.resolve()))
        updated = base + self._prompt_suffix
        if current != updated:
            self.engine.history[0]["content"] = updated
            self.engine._mark_cache_reset("planning mode change")
            self.engine.trajectory.message(self.engine.history[0])

    def _save(self) -> None:
        self.sync_prompt()
        self.engine.trajectory.note({"planning": self.snapshot()})


def build_plan_tools(engine) -> dict[str, Tool]:
    async def submit_plan() -> str:
        try:
            engine.planning.request_review()
        except ValueError as exc:
            return f"[error] {exc}"
        return (
            "Plan submitted for human review. Summarize the recommendation and stop. "
            "Remain in plan mode until the host reports explicit approval; do not implement."
        )

    return {"submit_plan": Tool(
        name="submit_plan",
        schema=_fn_schema(
            "submit_plan",
            "In plan mode, submit the current plan file for human review once the proposal "
            "is ready. Draft writes do not submit it. This tool never approves or executes "
            "the plan. Use ordinary conversation to discuss requirements and tradeoffs.",
            {}, [],
        ),
        fn=submit_plan,
        risk="safe",
    )}
