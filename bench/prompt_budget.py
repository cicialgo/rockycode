"""Durable, externally enforced budgets for prompt experiments (no model nudges)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


class BudgetStop(RuntimeError):
    """A clean pause BEFORE a request; no unknown charge or tool execution."""


class Meter:
    def __init__(self, client, directory: Path, *, token_cap: int, call_cap: int, turn_cap: int,
                 episode_cap: int = 100_000, restore: bool = False):
        self.client, self.directory = client, directory
        self.token_cap, self.call_cap, self.turn_cap = token_cap, call_cap, turn_cap
        self.episode_cap = episode_cap
        self.calls = self.tokens = self.turn_calls = 0
        self.episode_start = 0
        self.usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'prompt_cache_hit_tokens': 0}
        self.stop_reason = ''
        self.request_context = {}
        self.before_request = None
        self.safe = True
        self.unknown_usage = False
        if restore:
            state = json.loads((directory / 'ledger.json').read_text())
            if not state['safe'] or state['unknown_usage']:
                raise RuntimeError('unreconciled request/tool state; automatic replay is refused')
            for key in ('calls', 'tokens', 'turn_calls', 'episode_start', 'usage'):
                setattr(self, key, state[key])
            if self.tokens > token_cap or self.calls > call_cap:
                raise ValueError('resume caps cannot be below already consumed resources')
        self.save()

    def save(self):
        atomic_json(self.directory / 'ledger.json', {
            k: getattr(self, k) for k in ('calls', 'tokens', 'turn_calls', 'episode_start',
                                         'usage', 'safe', 'unknown_usage', 'stop_reason')})

    def checkpoint(self):
        # Caller has durably captured history AND workspace at a tool boundary.
        self.safe = not self.unknown_usage
        self.save()

    async def create(self, **kwargs):
        if self.stop_reason:
            raise RuntimeError(self.stop_reason)
        if self.before_request:
            self.before_request()
        self.checkpoint()
        # Deliberately conservative; not a claim about a provider tokenizer.
        # Keep the same output allowance across arms instead of shrinking it
        # near the limit and changing the treatment for expensive episodes.
        reserve = len(json.dumps(kwargs, ensure_ascii=False).encode()) + kwargs.get('max_tokens', 0) + 4096
        reason = None
        if self.calls >= self.call_cap or self.tokens + reserve > self.token_cap:
            reason = 'global budget exhausted before request'
        elif self.tokens - self.episode_start + reserve > self.episode_cap:
            reason = 'episode budget exhausted before request'
        elif self.turn_calls >= self.turn_cap:
            reason = 'per-turn experiment call cap reached'
        if reason:
            raise BudgetStop(reason)
        self.calls += 1
        self.turn_calls += 1
        self.safe = False  # remains unsafe until history + tool effects are saved
        self.unknown_usage = True
        self.save()  # crash during an API call must not silently replay/bill again
        record = {**self.request_context, 'call': self.calls,
                  'request': {k: v for k, v in kwargs.items() if k not in ('messages', 'tools')},
                  'messages_sha256': digest(kwargs['messages']),
                  'tools_sha256': digest(kwargs.get('tools')), 'input_reserve': reserve}
        with (self.directory / 'requests.jsonl').open('a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        try:
            stream = await self.client.chat.completions.create(**kwargs)
        except BaseException:
            self.stop_reason = 'API request failed; usage may be unknown; stop without retry'
            self.save()
            raise

        async def observed():
            seen = False
            truncated = False
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        if seen:
                            self.stop_reason = 'multiple usage records; reconciliation required'
                            self.unknown_usage = True
                            raise RuntimeError(self.stop_reason)
                        seen = True
                        u = chunk.usage.model_dump()
                        if any(not isinstance(u.get(k), int) or u[k] < 0
                               for k in ('prompt_tokens', 'completion_tokens')):
                            raise RuntimeError('invalid usage record')
                        for k in self.usage:
                            self.usage[k] += u.get(k, 0) or 0
                        self.tokens += u['prompt_tokens'] + u['completion_tokens']
                        self.unknown_usage = False
                        self.save()
                    for choice in chunk.choices:
                        if choice.finish_reason == 'length':
                            truncated = True
                    yield chunk
                if not seen:
                    raise RuntimeError('request ended without usage')
                if truncated:
                    # The usage receipt can arrive AFTER finish_reason=length.
                    # Drain it before failing; never execute truncated tools.
                    self.stop_reason = 'model output hit max_tokens; episode is incomplete'
                    raise RuntimeError(self.stop_reason)
            finally:
                await stream.close()
                if not seen or self.unknown_usage:
                    self.stop_reason = 'request ended without reliable usage; reconciliation required'
                self.save()
        return observed()
