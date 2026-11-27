"""Artifacts in the TUI: cards in the chat, the /artifact picker, edit targets, live preview.

Part of the TUI shell (rockycode/tui/app.py), split out so each concern reads on its
own; the app class mixes this in and the methods keep `self` = the app.
"""
from __future__ import annotations

import json
import sqlite3

from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Static

from rockycode.palette import LAVENDER, MUTED, VIOLET
from rockycode.tui.artifact_panel import ArtifactAction, ArtifactCard, ArtifactPanel
from rockycode.tui.chatinput import ChatInput


class ArtifactsMixin:
    def _setup_artifacts(self):
        self._artifact_cards = {}
        self._artifact_edit_target = None
        self._artifact_turn_contexts = {}
        self._artifact_list_ids = []
        self.engine.artifact_auto_open = False
        self.engine.artifact_target = self._artifact_target
        registry = getattr(self.engine, 'artifact_registry', None)
        if registry is not None:
            registry.subscribe(self._artifact_changed)

    def _artifact_changed(self, action, record):
        # A registry callback must not block a model/tool worker or steal focus.
        if action in ('created', 'updated'):
            self.call_later(self._show_artifact_card, record)
        self.call_later(self._render_cwd)

    async def _show_artifact_card(self, record):
        key = record.artifact_id or record.name
        card = self._artifact_cards.get(key)
        if card is not None and card.is_mounted:
            card.update_record(record)
        else:
            card = ArtifactCard(record, self._language)
            self._artifact_cards[key] = card
            await self._add(card)

    def _artifact_panel(self, artifact_id=None):
        registry = getattr(self.engine, 'artifact_registry', None)
        if registry is None:
            self.notify(self._t('artifact_ui.empty'))
            return
        def selected(result):
            if result:
                self.run_worker(self._artifact_action(*result), exclusive=False)
            else:
                self.query_one(ChatInput).focus()
        self.push_screen(ArtifactPanel(registry, language=self._language, artifact_id=artifact_id), selected)

    def on_artifact_action(self, event: ArtifactAction):
        event.stop()
        self.run_worker(self._artifact_action(event.action, event.artifact_id, event.revision), exclusive=False)

    async def _artifact_action(self, action, artifact_id, revision=None):
        registry = getattr(self.engine, 'artifact_registry', None)
        record = registry.get(artifact_id) if registry else None
        if record is None:
            self.notify(self._t('app.artifact_not_found_artifact_list'), severity='warning')
            return
        path = record.path
        history = registry.history(record.artifact_id)
        selected = next((r for r in history if r['revision'] == revision), None) if revision else None
        if revision and selected is None:
            self.notify(self._t('artifact_ui.missing'), severity='warning')
            return
        if selected:
            path = selected['path']
        if action == 'history':
            self._artifact_panel(record.artifact_id)
            return
        if not path.is_file():
            self.notify(self._t('artifact_ui.missing'), severity='warning')
            return
        if action == 'edit':
            self._artifact_edit_target = {'artifact_id': record.artifact_id, 'base_revision': record.revision,
                                          'title': record.title}
            prompt = self.query_one(ChatInput)
            prompt.border_title = self._t('artifact_ui.editing', title=escape(record.title), revision=record.revision)
            prompt.focus()
            self._render_edit_chip()
            self.notify(self._t('artifact_ui.edit_hint'), timeout=5)
        elif action == 'copy':
            self.copy_to_clipboard(str(path))
            self._pbcopy(str(path))
            self.notify(self._t('artifact_ui.copied'))
        elif action == 'restore' and selected:
            try:
                aid = registry.store.save(title=selected['title'], document=path.read_text(encoding='utf-8'),
                                          session_id=registry.session_id, theme=selected['theme'], layout=selected['layout'],
                                          artifact_id=record.artifact_id, base_revision=record.revision)
                restored = registry.get(aid)
                registry._emit('updated', restored)
                if self._artifact_server and self._artifact_server.is_running:
                    await self._artifact_server.broadcast('reload', {'name': restored.name})
                self.notify(self._t('artifact_ui.restored', revision=restored.revision))
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.notify(str(exc), severity='error')
        elif action in ('open', 'live'):
            if action == 'live':
                self._artifact_live = True
                await self._artifact_target()
            server = self._artifact_server
            url = path.as_uri()
            if not revision and server and server.is_running and (record.live or self._artifact_live):
                from urllib.parse import quote
                record.live = True
                record.url = f'{server.base_url}/artifacts/{quote(record.name)}?t={server.token}'
                url = record.url
            from rockycode.engine.artifact import _open_browser
            _open_browser(url)
            self.notify(self._t('artifact_ui.open_requested'))

    def _cancel_artifact_edit(self):
        self._artifact_edit_target = None
        self.query_one(ChatInput).border_title = self._t('app.ask_rocky')
        self._render_edit_chip()

    def _render_edit_chip(self):
        """The status-bar chip shown while the next message edits an artifact (the
        input's title says which). Clicking it stops that; /artifact cancel is the
        typed way."""
        try:
            chip = self.query_one("#editchip", Static)
        except NoMatches:
            return  # a screen is on top of chat — the chip isn't visible now
        chip.display = self._artifact_edit_target is not None
        chip.update(self._t('artifact_ui.edit_chip', accent=LAVENDER))

    @staticmethod
    def _artifact_prompt(prompt, target):
        if not target:
            return prompt
        return (prompt + "\n\n[Selected artifact for this request: " + json.dumps(target, ensure_ascii=False)
                + "]\nUse inspect_artifact to read that revision, then create_artifact with this artifact_id "
                  "and base_revision. Preserve its design unless asked to change it. If the revision "
                  "has changed, report the conflict; do not silently overwrite a newer revision.")

    async def _artifact_target(self):
        # Saving is independent of preview. Only --live or an explicit action
        # starts the server; generation no longer opens a blocking mode dialog.
        if not self._artifact_live:
            return None
        if self._artifact_server is None:
            from rockycode.engine.artifact import ArtifactServer
            srv = ArtifactServer(self.engine.workdir, registry=getattr(self.engine, 'artifact_registry', None))
            await srv.start()
            self._artifact_server = self.engine.artifact_server = srv
        return self._artifact_server

    async def _stop_artifact_server(self) -> bool:
        server = self._artifact_server
        if server is None:
            return False
        await server.stop()
        self._artifact_server = None
        self.engine.artifact_server = None
        self._render_cwd()
        return True

    async def _handle_artifact(self, text: str) -> None:
        """Manage the current session's Artifact inventory and live server."""
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "panel"
        arg = parts[2].strip() if len(parts) > 2 else ""
        registry = getattr(self.engine, "artifact_registry", None)

        if sub == "panel":
            self._artifact_panel()
            return
        if sub == "cancel":
            self._cancel_artifact_edit()
            return
        if sub == "import":
            try:
                if registry is None or registry.store is None:
                    raise ValueError('artifact storage is unavailable')
                aid = registry.store.import_legacy(registry.store.root / arg, registry.session_id)
                registry.refresh()
                registry._emit('created', registry.get(aid))
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.notify(str(exc), severity='error')
            return
        if sub == "live" and arg.lower() == "on":
            self._artifact_live = True
            await self._artifact_target()
        elif sub == "live" and arg.lower() == "off":
            self._artifact_live = False
            await self._stop_artifact_server()
        elif sub == "stop":
            stopped = await self._stop_artifact_server()
            note = self._t('app.stopped_saved_files_remain') if stopped else self._t('app.already_stopped')
            await self._add(Static(
                self._t('app.artifact_server', p1=note), classes="tool-line"))
            return
        elif sub == "list":
            records = registry.records() if registry is not None else []
            if not records:
                await self._add(Static(
                    self._t('app.no_artifacts_in_this_session_yet'), classes="tool-line"))
                return
            self._artifact_list_ids = [r.artifact_id or r.name for r in records]
            lines = [self._t('artifact_ui.title')]
            for i, record in enumerate(records, 1):
                mark = f"[{VIOLET}]●[/]" if record.open_clients else f"[{MUTED}]○[/]"
                state = (self._t('app.artifact_open_count', p0=record.open_clients) if record.open_clients
                         else self._t('app.saved'))
                mode = self._t('app.live') if record.live else self._t('app.static')
                lines.append(
                    f"  {mark} [{LAVENDER}]{i}. {escape(record.title)}[/] "
                    f"[dim]v{record.revision} · {mode} · {state} · {escape(record.artifact_id or record.name)}[/]")
            lines.append(self._t('app.artifact_open_number_name_artifact_stop'))
            await self._add(Static("\n".join(lines), classes="tool-line"))
            return
        elif sub in ("open", "edit", "history", "copy"):
            records = registry.records() if registry is not None else []
            record = None
            if arg.isdigit():
                ids = self._artifact_list_ids or [r.artifact_id or r.name for r in records]
                if 1 <= int(arg) <= len(ids):
                    record = registry.get(ids[int(arg) - 1])
            else:
                record = registry.get(arg) if registry else None
                if record is None:
                    matches = [r for r in records if r.title.casefold() == arg.casefold()]
                    record = matches[0] if len(matches) == 1 else None
            if record is None:
                self.notify(self._t('app.artifact_not_found_artifact_list'), severity='warning')
                return
            await self._artifact_action(sub, record.artifact_id or record.name)
            return
        elif sub not in ("status", "live"):
            await self._add(Static(
                self._t('app.artifact_usage'),
                classes="tool-line"))
            return

        state = (self._t('app.live') if self._artifact_live else
                 self._t('app.static_file') if self._artifact_live is False else
                 self._t('app.static_file'))
        url = (f" [dim]{self._artifact_server.base_url}[/]"
               if self._artifact_server is not None else "")
        count = registry.count if registry is not None else 0
        opened = registry.open_clients if registry is not None else 0
        await self._add(Static(
            self._t('app.artifact_status', p2=state, p3=url, p4=count, p5=opened),
            classes="tool-line"))

    # ---- exit + slash commands -----------------------------------------------
