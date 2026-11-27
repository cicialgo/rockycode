"""A model's request contract above the OpenAI transport, read from its row.

Sharing a reasoning wire shape does NOT imply sharing history, sampling or
completion semantics (e.g. DeepSeek vs GLM), so the contract is per model:
a handful of switches in the model's row in models.toml. A row that sets none
gets the generic contract. Nothing here names a model — a new model with a
known behaviour is one row; a behaviour no model has had yet is a new switch.
Loading a row is not live acceptance of it; that stays a separate step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

REPLAY = ("never", "with_tools", "always")
OUTPUT_LIMITS = ("max_tokens", "max_completion_tokens")
SAMPLING_STATES = ("thinking", "plain")
SAMPLING_PARAMS = ("temperature", "top_p")
# The row fields read here. providers.py refuses a row that misspells one, so a
# typo cannot quietly run the generic contract.
SWITCHES = ("replay_reasoning", "reasoning_required", "strict_finish",
            "tool_choice_none", "output_limit", "sampling")


@dataclass(frozen=True)
class ModelProtocol:
    # Send the model's earlier reasoning back: never · with_tools (only in a
    # request that carries tools) · always.
    replay_reasoning: str = "never"
    # A replayed assistant turn must carry the field, empty when the trace is lost.
    reasoning_required: bool = False
    # A stream that ends without a finish_reason is an error, tool calls or not.
    strict_finish: bool = False
    # False: the endpoint ignores tool_choice="none", so a call that must answer
    # in text is never handed tool schemas.
    tool_choice_none: bool = True
    # The request field that caps the output.
    output_limit: str = "max_tokens"
    # Sampling parameters the model wants fixed, with thinking on and off.
    sampling_thinking: tuple[tuple[str, float], ...] = ()
    sampling_plain: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_row(cls, row: Mapping) -> "ModelProtocol":
        """The contract a catalog row asks for. A ValueError names the switch
        and what it accepts."""
        replay = row.get("replay_reasoning", "never")
        if replay not in REPLAY:
            raise ValueError(f"replay_reasoning must be one of {', '.join(REPLAY)} — got {replay!r}")
        flags = {}
        for name, default in (("reasoning_required", False), ("strict_finish", False),
                              ("tool_choice_none", True)):
            flags[name] = row.get(name, default)
            if not isinstance(flags[name], bool):
                raise ValueError(f"{name} must be true or false — got {flags[name]!r}")
        if flags["reasoning_required"] and replay == "never":
            raise ValueError("reasoning_required needs replay_reasoning = with_tools or always")
        limit = row.get("output_limit", "max_tokens")
        if limit not in OUTPUT_LIMITS:
            raise ValueError(f"output_limit must be one of {', '.join(OUTPUT_LIMITS)} — got {limit!r}")
        sampling = row.get("sampling", {})
        if not isinstance(sampling, Mapping) or set(sampling) - set(SAMPLING_STATES):
            raise ValueError(f"sampling takes two tables, {' and '.join(SAMPLING_STATES)}")
        fixed = {}
        for state in SAMPLING_STATES:
            params = sampling.get(state, {})
            if (not isinstance(params, Mapping) or set(params) - set(SAMPLING_PARAMS)
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in params.values())):
                raise ValueError(f"sampling.{state} takes numbers for {' and '.join(SAMPLING_PARAMS)} only")
            fixed[state] = tuple(sorted(params.items()))
        return cls(replay, **flags, output_limit=limit,
                   sampling_thinking=fixed["thinking"], sampling_plain=fixed["plain"])

    @property
    def label(self) -> str:
        """The switches that differ from the generic contract, as one line for
        records (call identity, receipts, bench settings). "" = generic."""
        parts = []
        if self.replay_reasoning != "never":
            parts.append(f"replay_reasoning={self.replay_reasoning}")
        if self.reasoning_required:
            parts.append("reasoning_required")
        if self.strict_finish:
            parts.append("strict_finish")
        if not self.tool_choice_none:
            parts.append("tool_choice_none=false")
        if self.output_limit != "max_tokens":
            parts.append(f"output_limit={self.output_limit}")
        for state, params in (("thinking", self.sampling_thinking), ("plain", self.sampling_plain)):
            parts += [f"sampling.{state}.{name}={value}" for name, value in params]
        return " ".join(parts)

    def output_cap(self, tokens: int) -> dict:
        return {self.output_limit: tokens}

    def sampling(self, thinking: bool) -> dict:
        return dict(self.sampling_thinking if thinking else self.sampling_plain)

    def keeps_reasoning(self, *, tools: bool) -> bool:
        return self.replay_reasoning == "always" or (tools and self.replay_reasoning == "with_tools")

    def messages(self, history: list[dict], *, tools: bool) -> list[dict]:
        """Project a request without mutating durable history.

        Missing legacy reasoning cannot be reconstructed: where required, an
        empty field keeps structure but does not recover lost model context.
        Models allowing omitted reasoning retain that absence.
        Newly recorded assistant messages always contain the actual trace.
        """
        keep_reasoning = self.keeps_reasoning(tools=tools)
        result = []
        for message in history:
            item = dict(message)
            if keep_reasoning and item.get("role") == "assistant":
                if self.reasoning_required:
                    item.setdefault("reasoning_content", "")
            else:
                item.pop("reasoning_content", None)
            result.append(item)
        return result

    def completion_error(self, reason: str | None, *, tool_calls: bool) -> str | None:
        if reason is None:
            return "missing finish_reason (incomplete stream)" if self.strict_finish or tool_calls else None
        if reason not in ("stop", "tool_calls"):
            return f"finish_reason={reason}"
        # A syntactically complete tool argument is not proof that the server
        # completed the batch. This invariant applies to every provider.
        if (reason == "tool_calls") != bool(tool_calls):
            return "finish_reason disagrees with tool calls"
        return None


# What a row with no switches gets, and what an unlisted model runs on.
GENERIC = ModelProtocol()
