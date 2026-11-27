"""One approval pipeline for every host.

Every tool call the engine wants to run passes through a ToolApprover:

    static policy  → session grants → loop-tick rules → verifier → ask the host

`permission.decide` is the static policy (yolo | ask | careful × the tool's risk
tier × the command classifier). A static *block* never runs anywhere. A static
*allow* runs (the verifier may observe it in shadow mode). A would-*ask* call is
covered by a session grant, by a loop tick's one-shot, by the verifier in
enforce mode — or it reaches the host's `ask` callback, which is the only part a
host writes:

  - the TUI mounts its inline approval card and answers once | session | deny |
    cancel;
  - `serve` forwards the request to the editor client and maps its yes/no;
  - `exec` (headless) never asks: it answers deny and records which grant would
    have allowed the call, so the caller can re-run with `--allow <grant>`.

The approver owns the session grants, the loop-tick state and the verifier, so
the policy is the same whichever host is in front of the engine.

yolo never waits. The few calls only a person decides (permission.human_only)
would otherwise put a card in front of a run nobody is watching, and the run
would stand still until someone came back. A host that can show a waiting list
passes `park=True`: in yolo such a call is refused at once, the model is told
to go on without it, and the step waits in `parked` for the user to decide
when they are there. The third parked step in one turn stops the turn.

The user's "run it now" does not allow anything by itself. The host tells the
model to repeat the step, and for the length of THAT turn only it arms a pass
for exactly that call (`arm` / `disarm`). A message that is never delivered
arms nothing, and no pass outlives its turn.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from rockycode.engine.permission import (
    HUMAN_ONLY_WHY, command_binary, decide, human_only, session_grantable, sniff_danger,
)
from rockycode.engine.safety import classify_command


class CancelTurn(Exception):
    """Raised when the human chooses 'cancel turn' (Esc) in the approval
    prompt. Propagates out of Engine.run_turn; the tool-loop finally backfills
    tool responses so history stays API-valid."""


class Refusal(str):
    """A "no" that carries its own words for the model. Falsy, like the plain
    False an approver returns for a deny; the engine uses the text as the tool's
    result instead of the stock "[denied]" line."""

    def __bool__(self) -> bool:
        return False


PARK_LIMIT = 3    # parked steps in one turn; the third stops the turn
PARKED_MAX = 20   # the waiting list is for a person: past this, a step is refused and not listed

_PARKED_NOTE = (
    "[parked] Only the user can allow this step ({why}). rocky is in yolo mode and does not stop "
    "to ask, so the step is now on the user's waiting list. Do not try to reach the same thing "
    "another way. Go on with the rest of the task, and say in your final message that this step "
    "waits for the user."
)
_PARK_LIMIT_NOTE = (
    "[parked] Only the user can allow this step ({why}). It is the third such step in this turn, "
    "so rocky stops the turn here. The user decides what happens next."
)
_PARK_FULL_NOTE = (
    "[denied] Only the user can allow this step ({why}), and their waiting list is full, so it is "
    "not kept. Do not try to reach the same thing another way. Go on with the rest of the task."
)


def tool_brief(name: str, args: dict, limit: int = 96) -> str:
    """What a call acts on, short enough for one transcript line: the command, the
    file, the query. Hosts print it after the tool's name so the user can see
    what rocky did. Never the content being written; secret-shaped text is
    masked. "" when the call has nothing worth showing."""
    from rockycode.engine.redact import redact
    if isinstance(args, dict) and set(args) == {"raw"} and isinstance(args["raw"], str):
        # A ToolStarted event carries the arguments as the model sent them: one JSON string.
        try:
            args = json.loads(args["raw"] or "{}")
        except ValueError:
            return ""
    if not isinstance(args, dict):
        return ""

    def arg(key: str) -> str:
        value = args.get(key)
        return value.strip() if isinstance(value, str) else ""

    if name == "bash":
        brief = arg("command")
    elif name == "read_file":
        offset = args.get("offset")
        brief = arg("path") + (f":{offset}" if isinstance(offset, int) and not isinstance(offset, bool) and offset > 1 else "")
    elif name == "write_file":
        content = args.get("content")
        lines = len(content.splitlines()) if isinstance(content, str) else 0
        brief = arg("path") + (f" · {lines} line{'s' if lines != 1 else ''}" if lines else "")
    elif name == "grep":
        where = " · ".join(p for p in (arg("path"), arg("include")) if p)
        brief = arg("pattern") + (f"  in {where}" if where else "")
    elif name == "explore":
        brief = ": ".join(p for p in (arg("role"), arg("task")) if p)
    elif name == "web_research":
        queries = args.get("queries")
        brief = " · ".join(str(q) for q in queries[:3]) if isinstance(queries, list) else ""
    elif name == "rocky_config":
        # The change itself, not only its name: with no card on screen this line is the record.
        value = args.get("value")
        value = arg("base_url") if value is None or value == "" else str(value).strip()  # a number is a value too
        brief = " ".join(p for p in (arg("action"), arg("key") or arg("provider")) if p) + (f" → {value}" if value else "")
    else:
        # The one argument that names the target, else the first short text value.
        brief = next((arg(key) for key in ("path", "pattern", "query", "url", "name", "branch", "title", "id")
                      if arg(key)), "")
        if not brief:
            brief = next((v.strip() for v in args.values() if isinstance(v, str) and 0 < len(v.strip()) <= 200), "")
    brief = redact(" ".join(brief.split()))  # one line, whatever the argument held
    return brief if len(brief) <= limit else brief[:limit - 1].rstrip() + "…"


SETTING_SHOWN = 600  # characters of one value on the card; the tool refuses anything longer


def _settings_change(args: dict) -> str:
    """A rocky_config call in plain words. This is what the user approves, so
    it is the whole change: the value as it will be saved, the address a
    provider's requests (and its key) will go to. Never JSON, never cut short
    without saying so."""
    def text(key: str) -> str:
        value = args.get(key)
        value = "" if value is None else str(value).strip()  # a number or a flag is shown as sent
        if len(value) <= SETTING_SHOWN:
            return value
        return f"{value[:SETTING_SHOWN]}… ({len(value)} characters: too long, rocky will refuse it)"

    action = text("action").lower()
    if action == "show":
        return "show rocky's settings — names of keys, never their values"
    if action == "set":
        return f"change rocky's own setting, for every session from now on:\n{text('key')} = {text('value')}"
    if action == "set_url":
        if not text("base_url"):
            return f"remove the own address for {text('provider')} — its requests go to the built-in address again"
        return f"send {text('provider')} requests to this address:\n{text('base_url')}"
    if action == "add_provider":
        from rockycode.engine import providers
        name = text("provider")
        key_env = providers.key_env_for(name)
        lines = [f"add the model server {name}:", text("base_url"), f"models: {text('models')}"]
        extras = [f"{label}: {text(key)}" for key, label in (("reasoning", "reasoning"), ("vision", "sees images"),
                                                             ("context", "context"), ("max_output", "max output"))
                  if text(key) not in ("", "0", "False")]
        if extras:
            lines.append(" · ".join(extras))
        if os.environ.get(key_env, "").strip():
            lines.append(f"a key is already saved as {key_env}: it will be sent to this address")
        return "\n".join(lines)
    return json.dumps(args, ensure_ascii=False)


def tool_detail(name: str, args: dict) -> str:
    """A human-readable one-liner of what the call will do."""
    if name == "rocky_config":
        return _settings_change(args)
    if name == "bash":  # str(): a malformed call can carry a list here, and the card and the pass key need text
        return str(args.get("command", "") or "(no command)")
    if name == "web_fetch":
        return str(args.get("url", "") or "(no url)")
    if name == "view_image":
        q = args.get("question", "")
        return f"look at {args.get('path', '?')}" + (f" — {q}" if q else "")
    if name in ("write_file", "edit_file"):
        return f"{name} → {args.get('path', '?')}"
    if name == "remember":
        return f"remember: {str(args.get('name') or args.get('content', ''))[:140]}"
    if name == "loop_start":
        budget = " · ".join(p for p in (
            f"for {args['for']}" if args.get("for") else "",
            f"x{args['count']}" if args.get("count") else "",
            f"max {args['max_spend']}" if args.get("max_spend") else "") if p) or "no budget"
        return f"every {args.get('interval', '?')} — {str(args.get('prompt', ''))[:200]} · {budget}"
    if name == "loop_stop":
        return f"stop loop #{args.get('id', '?')}"
    try:
        return json.dumps(args)[:400]
    except (TypeError, ValueError):
        return str(args)[:400]


@dataclass
class AskRequest:
    """What the host shows (or reports) when the pipeline has to ask."""

    tool: str
    args: dict
    detail: str
    risk: str
    warning: Optional[str]          # advisory danger pattern, if any
    session_grant: Optional[str]    # what a 'session' answer would cover (binary or tool), None = not offered


AskFn = Callable[[AskRequest], Awaitable[str]]   # → "once" | "session" | "deny" | "cancel"
NotifyFn = Callable[..., Awaitable[None]]       # (kind, **info) — blocked | auto_allowed | auto_denied


@dataclass
class SessionGrants:
    """'Allow for this session' answers. A bash grant is scoped to the BINARY,
    never to all of bash; a dangerous command is never grantable."""

    tools: set[str] = field(default_factory=set)
    bins: set[str] = field(default_factory=set)

    def covers(self, name: str, args: dict) -> bool:
        if not session_grantable(name, args):
            return False
        if name == "bash":
            binary = command_binary(str(args.get("command", "")))
            return bool(binary and binary in self.bins)
        return name in self.tools

    def label(self, name: str, args: dict) -> Optional[str]:
        """The unit a 'session' answer would grant, or None when not grantable."""
        if not session_grantable(name, args):
            return None
        if name == "bash":
            return command_binary(str(args.get("command", ""))) or None
        return name

    def grant(self, name: str, args: dict) -> None:
        unit = self.label(name, args)
        if unit is None:
            return
        (self.bins if name == "bash" else self.tools).add(unit)

    def clear(self) -> None:
        self.tools.clear()
        self.bins.clear()


@dataclass
class TickState:
    """A loop tick never prompts (the user may be away): the first would-ask
    call is recorded here and the loop pauses after the tick, unless a
    `/loop allow` one-shot covers it."""

    loop: object = None               # the Loop whose tick runs right now
    blocked: Optional[dict] = None    # first ask-tier denial in that tick
    once: set = field(default_factory=set)  # (tool, detail) one-shot grants


@dataclass
class Parked:
    """A step only the user can allow, refused in yolo and kept for them."""

    tool: str
    args: dict
    detail: str                     # what the card shows: the command, the settings change in words
    risk: str
    warning: Optional[str]
    why: str                        # permission.human_only's token
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)   # how a host names it to its client

    @property
    def key(self) -> tuple[str, str]:
        return (self.tool, self.detail)


class ToolApprover:
    """The engine's approver. Construct one per session; set `mode` live."""

    def __init__(
        self,
        *,
        registry: dict,
        workdir: Path,
        ask: AskFn,
        mode: str = "ask",
        engine=None,
        notify: Optional[NotifyFn] = None,
        verifier=None,               # None → lazy VerifierGate (off unless configured); False → never
        widen_reads: bool = True,    # a human approval of an outside read widens the read jail
        park: bool = False,          # this host shows `parked` to the user: yolo refuses and keeps, never waits
    ) -> None:
        self.registry = registry
        self.workdir = workdir
        self.ask = ask
        self.mode = mode
        self.engine = engine
        self.notify = notify
        self._verifier = verifier
        self.widen_reads = widen_reads
        self.session = SessionGrants()
        self.tick = TickState()
        self.refused: list[dict] = []   # static blocks (evidence)
        self.park = park
        self.parked: list[Parked] = []  # refused in yolo, waiting for the user
        self.once: set = set()          # (tool, detail) passes, armed only for the turn that picks those steps up
        self.turn_parks = 0             # parked in the turn that runs now
        self.stop_turn = False          # the park limit was reached: the host ends the turn

    # -- collaborators --

    @property
    def verifier(self):
        """The session verifier (docs/decision-verifiers.md). Off unless the
        user opts in; tests inject one before the first approval."""
        if self._verifier is None:
            from rockycode.engine.verifier import VerifierGate
            self._verifier = VerifierGate()
        return self._verifier or None

    @verifier.setter
    def verifier(self, gate) -> None:
        self._verifier = gate

    @property
    def read_grants(self) -> set:
        return self.engine.read_grants if self.engine is not None else set()

    def _risk(self, name: str) -> str:
        # The engine's registry can be swapped live (sandbox on/off), so look
        # the tool up there when an engine is attached.
        reg = self.engine.registry if self.engine is not None else self.registry
        return getattr(reg.get(name), "risk", "risky")

    def _task(self) -> str:
        if self.engine is None:
            return ""
        from rockycode.engine.verifier import task_hint
        return task_hint(self.engine.history)

    async def _notify(self, kind: str, **info) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(kind, **info)
        except Exception:  # noqa: BLE001 — a host's rendering never changes the decision
            pass

    def _record(self, review) -> None:
        """Shadow and enforce both land on the trajectory. A log failure must
        not change the approval decision."""
        if review is None or not getattr(review, "consulted", False):
            return
        note = getattr(getattr(self.engine, "trajectory", None), "note", None)
        if not callable(note):
            return
        try:
            note({"verifier": review.record})
        except Exception:  # noqa: BLE001
            pass

    def grant_read_if_escaping(self, name: str, args: dict) -> None:
        """Approving an out-of-scope read must actually let it happen — the
        host-owned read policy (engine/file_access.py) refuses it otherwise, so
        the approval would be a lie. Record the resolved target in
        engine.read_grants (reads only: read_file, grep, glob, view_image);
        the policy and the permission layer both consult it. Secret files are
        still refused inside a granted path. Only a HUMAN approval widens
        (widen_reads)."""
        if not self.widen_reads or self.engine is None:
            return
        from rockycode.engine.file_access import FileAccessPolicy
        FileAccessPolicy(self.workdir, getattr(self.engine, "allowed_roots", ()),
                         self.engine.read_grants).grant_read(name, args)

    # -- yolo never waits: parked steps --

    def begin_turn(self) -> None:
        """The engine calls this when a turn starts: the park limit counts per turn."""
        self.turn_parks = 0
        self.stop_turn = False

    async def _park(self, name: str, args: dict, detail: str, risk: str, human: str) -> Refusal:
        self.turn_parks += 1
        step = Parked(tool=name, args=args, detail=detail, risk=risk, warning=sniff_danger(name, args), why=human)
        listed = any(p.key == step.key for p in self.parked)
        if not listed and len(self.parked) < PARKED_MAX:
            self.parked.append(step)
            listed = True
        self.stop_turn = self.turn_parks >= PARK_LIMIT
        await self._notify("parked", tool=name, detail=detail, warning=step.warning,
                           waiting=len(self.parked), listed=listed, stop=self.stop_turn)
        note = _PARK_LIMIT_NOTE if self.stop_turn else _PARKED_NOTE if listed else _PARK_FULL_NOTE
        return Refusal(note.format(why=HUMAN_ONLY_WHY[human]))

    def settle(self, step: Parked) -> None:
        """The user decided a parked step: it leaves the list. This allows nothing
        by itself; a step they chose to run is armed for its pick-up turn (`arm`)."""
        self.parked = [p for p in self.parked if p is not step]

    def arm(self, steps) -> None:
        """The turn that delivers the user's "run it now" begins: each of these
        exact calls may run once in it."""
        self.once.update(step.key for step in steps)

    def disarm(self, steps) -> None:
        """That turn is over. A pass it did not use ends with it."""
        self.once.difference_update(step.key for step in steps)

    # -- the pipeline --

    async def __call__(self, name: str, args: dict) -> bool:  # True, False, or a Refusal (falsy, with its text)
        risk = self._risk(name)
        detail = tool_detail(name, args)
        static = decide(self.mode, risk, args, self.workdir, tool=name,
                        read_grants=(*getattr(self.engine, "allowed_roots", ()), *self.read_grants))
        if static == "block":
            # A never-allowed command (e.g. sudo rm -rf /). Refused in every
            # mode — this is the system blocking, not the user denying.
            reason = classify_command(str(args.get("command", ""))).reason if name == "bash" else "blocked"
            self.refused.append({"command": detail, "reason": reason})
            await self._notify("blocked", tool=name, detail=detail)
            return False
        if static == "allow":
            gate = self.verifier
            if gate is not None:
                self._record(await gate.on_allowed(mode=self.mode, risk=risk, tool=name, args=args,
                                                   task=self._task()))
            return True  # optional shadow never changes a static allow
        # would-ask: a session grant only covers non-dangerous calls, and for
        # bash it's scoped to the BINARY — approving `lake build` lets `lake …`
        # run but a later `curl`/`rm` still prompts.
        if self.session.covers(name, args):
            self.grant_read_if_escaping(name, args)
            return True
        if self.once and (name, detail) in self.once:
            # The user's answer to a parked step: this exact call, one time, in its pick-up turn.
            self.once.discard((name, detail))
            self.grant_read_if_escaping(name, args)
            return True
        # A loop tick's one-shot (/loop allow) and the loop tools themselves are
        # already decided. The verifier must not second-guess a human grant.
        tick = self.tick
        if tick.loop is not None and name in ("loop_start", "loop_stop"):
            return True
        if tick.loop is not None and (name, detail) in tick.once:
            tick.once.discard((name, detail))
            self.grant_read_if_escaping(name, args)
            return True
        human = human_only(name, args)
        if human and self.park and self.mode == "yolo":
            # Nobody may be there, and yolo's promise is that it does not wait.
            return await self._park(name, args, detail, risk, human)
        warning = sniff_danger(name, args)
        gate = self.verifier
        review = (await gate.on_ask(mode=self.mode, risk=risk, tool=name, args=args, task=self._task())
                  if gate is not None else None)
        self._record(review)
        if review is not None and review.applied and review.effective == "allow":
            await self._notify("auto_allowed", tool=name, detail=detail)
            return True
        if review is not None and review.applied and review.effective == "deny":
            err = (review.record or {}).get("error")
            await self._notify("auto_denied", tool=name, detail=detail, error=err)
            # Keep the refusal visible even if the model replies LOOP QUIET;
            # the tick settlement pauses the loop for an explicit human choice.
            if tick.loop is not None and tick.blocked is None:
                tick.blocked = {"tool": name, "args": args, "detail": detail,
                                "risk": risk, "warning": warning}
            return False
        if tick.loop is not None:
            # A loop tick NEVER prompts — the user may be away. Anything still
            # asking is denied, the FIRST such call is recorded, and the loop
            # pauses after the tick (unless the session is yolo by then).
            if tick.blocked is None:
                tick.blocked = {"tool": name, "args": args, "detail": detail,
                                "risk": risk, "warning": warning}
            return False
        req = AskRequest(tool=name, args=args, detail=detail, risk=risk, warning=warning,
                         session_grant=self.session.label(name, args))
        choice = await self.ask(req)
        if choice in ("once", "session"):
            if choice == "session":
                self.session.grant(name, args)
            self.grant_read_if_escaping(name, args)  # make the approval REAL
            return True
        if choice == "cancel":
            raise CancelTurn()
        return False  # "deny" (or dismissed) → the model gets a [denied] result


# ---- exec: the host that never asks ----------------------------------------------

# Headless-only ask tier: deletion. classify_command deliberately allows
# `rm -rf build/` because goal mode runs on an isolated copy — exec runs on
# the REAL working tree, so every delete needs an explicit `delete` grant.
_DELETE = re.compile(
    r"(?:^|[\s;&|(])(?:rm|rmdir|unlink)\b"
    r"|\bgit\s+clean\b"
    r"|\bfind\b[^\n|;]*\s-delete\b"
)


class HeadlessApprover(ToolApprover):
    """The approver for runs where the CALLER, not a human, supervises.

    Never waits: a would-ask action records `blocked` (the exec driver stops
    the run and exits 2 so the caller can request a granted follow-up) and a
    block-tier action lands in `refused` (the model gets the denial and keeps
    going). The grant token IS the safety pattern name, so `blocked_on.grant`
    tells the caller exactly what to pass to --allow. File tools are jailed to
    the workdir (+ --allow-dir) by the registry, so safe/moderate tiers run
    and the jail decides; a human never widens anything here.
    """

    def __init__(self, registry_or_engine, grants: frozenset[str] = frozenset()) -> None:
        engine = None if isinstance(registry_or_engine, dict) else registry_or_engine
        registry = registry_or_engine if engine is None else engine.registry
        super().__init__(registry=registry, workdir=engine.workdir if engine is not None else Path.cwd(),
                         ask=self._never, mode="ask", engine=engine, verifier=False, widen_reads=False)
        self.grants = grants
        self.blocked: Optional[dict] = None  # first ask-tier hit ends the run

    async def _never(self, req: AskRequest) -> str:
        if req.tool == "bash":
            cmd = str(req.args.get("command", ""))
            # A command only a person decides takes its own grant, whatever else it matches:
            # `sudo cat ~/.rockycode/.env` is not covered by a `privilege` grant.
            human = human_only(req.tool, req.args)
            if human and human not in self.grants:
                self.blocked = {"grant": human, "reason": HUMAN_ONLY_WHY[human], "command": cmd}
                return "deny"
            v = classify_command(cmd)
            if v.action == "ask" and v.pattern not in self.grants:
                self.blocked = {"grant": v.pattern, "reason": v.reason, "command": cmd}
                return "deny"
            if _DELETE.search(cmd) and "delete" not in self.grants:
                self.blocked = {"grant": "delete", "reason": "deletes files on the real working tree",
                                "command": cmd}
                return "deny"
            return "once"
        if req.risk in ("safe", "moderate"):
            return "once"  # the jail decides (workdir + --allow-dir)
        # "tool:<name>" tokens work exactly like bash pattern grants (the
        # routine envelope relies on it).
        if f"tool:{req.tool}" in self.grants:
            return "once"
        self.blocked = {"grant": f"tool:{req.tool}", "reason": f"unclassified risky tool '{req.tool}'"}
        return "deny"
