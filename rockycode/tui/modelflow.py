"""/model and /effort: the picker, the switch, the preflight card, cost notes.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import asyncio

from rich.markup import escape
from textual import work
from textual.widgets import Static

from rockycode.engine.effort import wire_tier

from rockycode.palette import AMBER, LAVENDER, MUTED
from rockycode.tui.chatinput import ChatInput


class ModelMixin:
    async def _handle_model(self, text: str) -> None:
        """/model — switch provider AND exact model live. Bare opens the PICKER
        over the options you've KEYED (↑↓/click + ↵, so an EN/CN catalog stays
        short) with an "N more" row and an "add or change a key" row; `/model
        all` opens the full catalog. A spec (provider, provider-region,
        provider:model, or a unique model id) still switches directly — the
        picker's guide line teaches that path."""
        from rockycode.engine import providers as P

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg or arg == "all":
            show_all = arg == "all"
            picks = P.choices() if show_all else P.configured_choices()
            hidden = 0 if show_all else len(P.choices()) - len(picks)
            self._model_picker_flow(picks, hidden)
            return

        resolved = P.resolve(arg)
        if resolved is None:
            # a LOCAL provider that isn't ready gets its exact fix, not the
            # generic shrug — "ollama isn't running — start it: ollama serve"
            hint = P.local_hint(arg) or P.refused_hint(arg)
            msg = hint or (self._t('app.model_unknown'))
            await self._add(Static(f"[{AMBER}]· {escape(msg)}[/]", classes="tool-line"))
            return
        prov, ep, model = resolved
        await self._apply_model_choice(prov, ep, model)

    @work
    async def _model_picker_flow(self, picks: list, hidden: int) -> None:
        from rockycode.engine import providers as P
        from rockycode.tui.modelpicker import EndpointPicker, ModelPicker

        current = f"{self.engine.provider_name}:{self.engine.model}"
        # "ollama — not running · start it: …" and "<model> — row refused · …" rows
        hints = P.local_hints() + P.refused_hints()
        result = await self.push_screen_wait(
            ModelPicker(picks, current=current, hidden=hidden, hints=hints, language=self._language))
        if result == "all":  # the "N more" row reopens over the full catalog
            result = await self.push_screen_wait(
                ModelPicker(P.choices(), current=current, hidden=0, hints=hints, language=self._language))
        if result == "keys":  # "add or change a key": the only door to a key that exists
            await self._pick_key()
            return
        if isinstance(result, list):  # a model group — model first, URL second
            if len(result) == 1:  # one endpoint: nothing to ask, switch now
                result = result[0]
            else:
                group = result
                result = await self.push_screen_wait(
                    EndpointPicker(group, current=current, language=self._language))
                if isinstance(result, tuple) and result[0] == "custom":
                    # persist the typed URL as <provider>-custom, then pick it
                    eid, err = P.set_custom_url(group[0].provider.name, result[1])
                    result = None
                    if err:
                        await self._add(Static(f"[{AMBER}]· {escape(err)}[/]",
                                               classes="tool-line"))
                    else:
                        hit = P.resolve(f"{eid}:{group[0].model}")
                        if hit is not None:
                            result = P.Choice(*hit)
        if isinstance(result, P.Choice):
            await self._apply_model_choice(result.provider, result.endpoint, result.model)
        self.query_one(ChatInput).focus()

    async def _pick_key(self) -> None:
        """The picker's "add or change a key" row: which endpoint, then the hidden
        field. Runs inside the picker's worker (it waits on screens)."""
        from rockycode.engine import providers as P
        from rockycode.tui.keyprompt import KeyPicker

        picked = await self.push_screen_wait(
            KeyPicker(P.key_rows(), current=self.engine.provider_name, language=self._language))
        if picked == "move":
            from rockycode import onboarding
            # Each key is read back from the keychain before its line leaves the file.
            moved, left = await asyncio.to_thread(onboarding.move_to_keychain)
            if moved:
                await self._add(Static(self._t("key_prompt.moved", count=len(moved), names=escape(", ".join(moved))),
                                       classes="tool-line"))
            if left:
                await self._add(Static(f"[{AMBER}]" + self._t("key_prompt.not_moved", names=escape(", ".join(left)))
                                       + "[/]", classes="tool-line"))
            self.query_one(ChatInput).focus()
        elif picked is not None:
            await self._take_key(*picked, then_model=None)

    async def _take_key(self, prov, ep, *, then_model: str | None) -> None:
        """One key for one endpoint, typed into a hidden field (never the chat
        input), tested, saved to the OS keychain or rocky's key file and used
        from the next reply. then_model: the switch that asked for the key,
        made once the key exists."""
        from rockycode import onboarding
        from rockycode.engine import providers as P
        from rockycode.tui.keyprompt import KeyScreen

        label = prov.label + (f" · {ep.label}" if ep.label else "")
        result = await self.push_screen_wait(KeyScreen(label, ep.key_env, ep.base_url, language=self._language))
        if result is None:
            await self._add(Static(self._t("key_prompt.cancelled"), classes="tool-line"))
        else:
            where, verdict = result
            if where == onboarding.KEYCHAIN:
                where = self._t("key_prompt.keychain")
            await self._add(Static(self._t("key_prompt.saved", where=escape(where), key_env=escape(ep.key_env)),
                                   classes="tool-line"))
            if verdict != "ok":
                await self._add(Static(self._t("key_prompt.unverified", host=escape(ep.base_url)), classes="tool-line"))
            if then_model:
                await self._apply_model_choice(prov, ep, then_model)
            else:
                # The model in use rides this key: reopen its client with the new one.
                live = next(((p, e) for p in P.discover().values() for e in p.endpoints
                             if e.eid == self.engine.provider_name), None)
                if live is not None and live[1].key_env == ep.key_env and self.engine.model in live[0].models:
                    await self._apply_model_choice(*live, self.engine.model)
        self.query_one(ChatInput).focus()

    @work
    async def _key_flow(self, prov, ep, *, then_model: str | None) -> None:
        await self._take_key(prov, ep, then_model=then_model)

    async def _apply_model_choice(self, prov, ep, model: str) -> None:
        """The one switch path — picker picks and typed specs both land here.
        A LOCAL target runs a readiness preflight FIRST (server up? model
        pulled? tools? serving ctx?) — not ready means the engine is left
        untouched and the card says exactly what to fix; ready means the
        switch proceeds with context_window paced to the server's reality."""
        prev_model = self.engine.model
        from rockycode.engine import route as route_mod
        try:
            to = route_mod.bind(prov, ep, model)
        except route_mod.RouteError as e:
            if e.key_env:  # only the key is missing: take it here, then come back to this switch
                self._key_flow(prov, ep, then_model=model)
            else:
                await self._add(Static(f"[{AMBER}]· {escape(str(e))}[/]", classes="tool-line"))
            return
        from rockycode.engine.providers import Choice
        vision = Choice(prov, ep, model).vision  # model-level, not provider-level
        pf = None
        if prov.local:
            from rockycode.engine.localcheck import preflight
            pf = await asyncio.to_thread(preflight, ep.base_url, model)
            await self._add(Static(self._preflight_card(pf, ep.eid),
                                   classes="tool-line"))
            if not pf.ok:
                return  # nothing changed — the card carries the fixes + retry
            vision = vision or pf.vision  # the server said it sees
        client = route_mod.open_client(to)
        # The switch adopts the model's registry spec — wire shape, tiers,
        # cache field, and (unless pinned via /config) its context window and
        # output cap — so a cloud→cloud switch re-paces itself and a
        # local→cloud one undoes the local pacing by construction.
        self.engine.switch_provider(
            client, model, provider_name=ep.eid,
            reasoning_policy=prov.reasoning, tools_enabled=(prov.tools == "native"),
            vision=vision, local=prov.local, provider=prov, endpoint=ep, route=to,
        )
        if pf is not None and not self.engine._explicit_limits["context_window"]:
            self.engine.context_window = pf.context_window  # the server's VERIFIED ctx wins
        if prov.local:
            self._ledger.mark_free(model)  # $0 · local — never the price nag
        self._render_status()
        # The topbar title bakes the model name in at compose() time — repaint
        # it here or it shows the launch model forever.
        self.query_one("#title-block", Static).update(self._title_text())
        note = "" if prov.tools == "native" else self._t('app.tools_off_plain_responder')
        if vision:
            note += self._t('app.sees_images_paste_or_ctrl_v_to_attach')
        await self._add(Static(
            self._t('app.model_applies_from_the_next_reply', p1=escape(ep.eid), p2=escape(model), p3=note), classes="tool-line"))
        # Cost reminder on a real switch: the new model's API rate, and that the
        # prompt cache resets (first turn on it is all cache-miss → pricier).
        if model != prev_model:
            await self._add(Static(self._model_cost_note(model), classes="tool-line"))

    def _preflight_card(self, pf, eid: str) -> str:
        """The local-readiness checklist as one transcript card. Every ✗/⚠
        line carries its exact fix — the card IS the setup guide, so a not-
        ready switch teaches instead of dead-ending."""
        head = (self._t('app.preflight', p1=escape(eid), p2=escape(pf.model))
                if pf.ok else
                self._t('app.preflight_not_ready_model_unchanged', p1=escape(eid)))
        mark = {"ok": f"[{MUTED}]✓", "warn": f"[{AMBER}]⚠",
                "fail": f"[{AMBER}]✗", "info": f"[{MUTED}]·"}
        lines = [head]
        for c in pf.checks:
            lines.append(f"  {mark[c.status]} {escape(c.text)}[/]")
            if c.fix:
                lines.append(self._t('app.fix', p1=escape(c.fix)))
        if pf.ok:
            how = self._t('app.verified') if pf.ctx_verified else self._t('app.local_default')
            lines.append(self._t('app.context_window_local', p1=pf.context_window, p2=how))
        else:
            lines.append(self._t('app.fix_the_line_then_model', p2=escape(eid), p3=escape(pf.model)))
        return "\n".join(lines)

    def _model_cost_note(self, model: str) -> str:
        """One-line API-fee reminder for a just-switched model: its input/output
        rate in the session currency, or a nudge to set it. All pricing is
        per-token API fee (not membership/plan) — the ledger prices each turn by
        the model that produced it."""
        sym = "¥" if self._currency == "cny" else "$"
        if model in self._ledger.free_models:
            return (self._t('app.model_local_note', p0=escape(model)))
        if self._ledger.priced(model):
            r = self._ledger.rate(model, self._currency)
            body = (self._t('app.model_cost_note', p0=escape(model), p1=sym, p2=r['in_miss'], p3=sym, p4=r['out']))
        else:
            body = (self._t('app.model_no_price', p1=escape(model)))
        return body

    def _effort_note(self) -> str:
        """Current effort-dial reading, wire-honest: names what the provider is
        actually sent when the dial value and its tier differ (high → medium
        on an OpenAI-style provider; off → the lowest tier on a model that
        can't switch thinking off; nothing on a provider without tiers)."""
        eng = self.engine
        prov = eng.provider_name
        if not eng.thinking:
            if eng.thinking_off:
                return (self._t('app.effort_off_kept', p1=eng.reasoning_effort))
            low = wire_tier("low", eng.reasoning_policy, eng.efforts)
            return (self._t('app.effort_off_lowest_tier', p1=prov, p2=f', {low}' if low else ''))
        eff = eng.reasoning_effort
        sent = wire_tier(eff, eng.reasoning_policy, eng.efforts)
        if sent is None:
            wire = self._t('app.effort_no_tiers', p0=prov)
        elif sent != eff:
            wire = self._t('app.sends_to', p0=sent, p1=prov)
        else:
            wire = ""
        return f"[{LAVENDER}]{eff}[/]{wire}"
