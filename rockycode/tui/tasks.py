"""Single foreground TUI session: switch only at a quiescent boundary.

History remains in native trajectories. This index stores links, drafts and
mail, and maps subsequent resume trajectories back to a stable task identity.
A session becomes a task only when it is created as one (`/task new`, the
`create_new_task` tool) or when `/task` or a task tool is first used in it —
an ordinary chat never leaves a row behind.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from rich.markup import escape
from textual.widgets import Static

from rockycode import session
from rockycode.engine.trajectory import TrajectoryLogger
from rockycode.task_links import TaskError, TaskLinks, build_task_tools, task_text
from rockycode.tui.chatinput import ChatInput


@dataclass
class TaskSwitch:
    info: session.SessionInfo
    sandbox: bool
    permission: str  # the terminal's current mode carries across the switch; grants do not


class TaskSession:
    def __init__(self, app, resumed=None):
        self.app = app
        self.input = app.query_one(ChatInput)
        self.links = TaskLinks(session.HOME_ROOT / "task-links.sqlite3")
        engine = app.engine
        self.path = str(engine.trajectory.path)
        previous = self.links.for_session(resumed.path) if resumed else None
        # Resuming an older snapshot creates a branch; never rewind a task's
        # latest continuation or overwrite the draft saved by that continuation.
        stale = previous is not None and previous["path"] != str(resumed.path)
        self.id = previous["id"] if previous and not stale else session.public_id(engine.trajectory.session_id)
        # An untitled task takes the name its session already has (see retitle).
        self._title = (previous["title"] if previous else "") or (resumed.display_title if resumed else "")
        self._parent = previous["id"] if stale else None
        self._draft = previous["draft"] if previous and not stale else ""
        self.registered = False
        if previous is not None:
            self._register()  # already a task: point its row at this continuation
            self.input.text = self._draft
        tools = build_task_tools(self.links, self.id, self.create, notify=app.refresh_notes,
                                 prepare=self._ensure)
        engine.registry.update(tools)
        if app._local_registry is not None:
            app._local_registry.update(tools)
        runtime = getattr(engine, "runtime", None)
        if runtime:
            runtime.refresh_prompt()

    def _register(self):
        self.links.prune()
        self.links.register(self.id, self.app.engine.workdir, self._title, parent=self._parent,
                            path=self.path, draft=self._draft)
        self.registered = True

    def retitle(self, title):
        """The session got its name (first completed turn). A task that has no
        title of its own takes it, so its note says more than a number."""
        if self._title or not title:
            return
        self._title = title.strip().replace("\n", " ")[:80]
        try:
            if self.registered:
                self._register()
        except (OSError, sqlite3.Error, TaskError):
            pass  # the name is a nicety; the session title itself is already saved
        self.app.refresh_notes()

    def notes(self):
        """This project's tasks for the note strip (see notestrip.py). Reading
        them never writes and never makes this session a task; storage trouble
        shows nothing."""
        try:
            return self.links.in_workspace(self.app.engine.workdir,
                                           keep=self.id if self.registered else None)
        except (OSError, sqlite3.Error, TaskError):
            return []

    def _ensure(self):
        """Register this session as a task on first use (tools and /task)."""
        if not self.registered:
            self._register()

    async def create(self, prompt):
        task_text(prompt)
        self._ensure()
        project = session.get_project(self.app.engine.workdir)
        log = TrajectoryLogger({"model": self.app.engine.model, "workdir": str(project.root),
                                "project_id": project.id, "project_name": project.name})
        log.title(prompt.strip().replace("\n", " ")[:80])
        if log.path is None:
            raise TaskError("save_failed")
        task_id = session.public_id(log.session_id)
        self.links.register(task_id, project.root, prompt.strip().replace("\n", " ")[:80],
                            parent=self.id, path=str(log.path), draft=prompt)
        return {"task_id": task_id, "status": "draft", "parent": self.id,
                "open": f"/task open {task_id}"}

    def save_draft(self):
        if self.registered:
            self.links.draft(self.id, self.input.text)

    def can_switch(self):
        app = self.app
        worker = app._turn_worker
        return not (app._delivery.running or app._delivery.pending or (worker and worker.is_running)
                    or app._sandbox_starting or app._lsp_connecting or app._loops.live())

    async def command(self, text):
        app = self.app
        parts = text.split(maxsplit=2)
        action = parts[1] if len(parts) > 1 else "list"
        arg = parts[2] if len(parts) > 2 else ""
        try:
            self._ensure()
            if action in {"new", "open"}:
                if not self.can_switch():
                    await self.say(app._t("tasks.busy"))
                    return
                if action == "new":
                    arg = (await self.create(arg))["task_id"]
                elif arg.isdigit():  # the number on the note strip: `/task open 2`
                    arg = next((row["id"] for row in self.links.in_workspace(app.engine.workdir, limit=None)
                                if row["number"] == int(arg)), arg)
                target = self.links.get(arg)
                if target["workspace"] != str(app.engine.workdir.resolve()):
                    raise TaskError("cross_workspace")
                if arg == self.id:
                    return
                info = session._read_info(Path(target["path"])) if target["path"] else None
                if info is None:
                    raise TaskError("history_unavailable")
                self.save_draft()
                # The ordinary exit flow (outcome record, final reflect) leaves
                # this session; the CLI loop reopens the target from the result.
                app._switch_target = TaskSwitch(info, app.sandbox is not None, app._permission_mode)
                app._request_exit()
            elif action == "send":
                target, _, message = arg.partition(" ")
                self.links.send(self.id, target, message)
                await self.say(app._t("tasks.sent"))
            elif action == "inbox":
                rows = self.links.inbox(self.id)
                await self.say("\n\n".join(f'{m["id"]} · {m["sender"]}\n{m["content"]}' for m in rows)
                               or app._t("tasks.empty"))
            elif action == "dismiss":
                self.links.dismiss(self.id, arg)
                await self.say(app._t("tasks.dismissed"))
            elif action == "use":
                row = next((m for m in self.links.inbox(self.id) if m["id"] == arg), None)
                if row is None:
                    raise TaskError("unknown_message")
                inp = app.query_one(ChatInput)
                inp.text = (inp.text + "\n\n" + app._t("tasks.peer_note", id=row["sender"]) + "\n"
                            + row["content"]).strip()
                self.save_draft()
            elif action == "list":
                rows = self.links.list(self.id)
                total = self.links.total(self.id)
                lines = [app._t("tasks.current", id=self.id)]
                lines += [f'{row["id"]} · {row["title"]} · {row["parent"] or "—"}' for row in rows]
                if total > len(rows):
                    lines.append(app._t("tasks.more", shown=len(rows), total=total))
                await self.say("\n".join(lines))
                await self.say(app._t("tasks.help"))
            else:
                await self.say(app._t("tasks.help"))
        except TaskError as exc:
            await self.say(app._t(f"tasks.err_{exc.code}", **exc.params))
        except (OSError, sqlite3.Error):
            await self.say(app._t("tasks.storage_error"))
        app.refresh_notes()  # a new task, sent or dismissed mail: the strip follows

    async def say(self, text):
        await self.app._add(Static(escape(text), classes="tool-line"))

    def close(self):
        try:
            self.save_draft()
        except (OSError, sqlite3.Error, TaskError):
            # An unwritable draft must not skip Runtime.close and tool cleanup.
            self.app.log.warning("Could not save the task draft during shutdown")
        finally:
            self.links.close()


def attach(app, resumed=None):
    if getattr(app, "_task_session", None) is None and getattr(app.engine.trajectory, "path", None):
        try:
            app._task_session = TaskSession(app, resumed)
        except (OSError, sqlite3.Error):
            app.notify(app._t("tasks.unavailable"), severity="warning")
        app.refresh_notes()


async def command(app, text):
    if getattr(app, "_task_session", None) is None:
        await app._add(Static(app._t("tasks.unavailable"), classes="error-msg"))
        return
    await app._task_session.command(text)
