"""Real Textual actions: no automatic opening, stable selection, history/edit routing."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ['ROCKYCODE_HOME'] = tempfile.mkdtemp(prefix='rocky-artifact-ui-')
from rockycode.engine.loop import Engine
from rockycode.engine.artifact import build_artifact_tools
from rockycode.tui.app import RockyCodeApp
from rockycode.tui.artifact_panel import ArtifactCard, ArtifactPanel
from rockycode.tui.chatinput import ChatInput
from textual.widgets import Button, Input, OptionList, Static


async def main():
    with tempfile.TemporaryDirectory() as temp:
        prompts, script = [], []  # script: tool calls the fake model makes before it answers
        async def request(**kwargs):
            prompts.append(next(m['content'] for m in reversed(kwargs['messages']) if m['role'] == 'user'))
            calls = script.pop(0) if script else None
            async def stream():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None if calls else 'done', reasoning_content=None, tool_calls=calls))])
                if calls:  # a response that calls a tool ends by saying so
                    yield SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason='tool_calls',
                        delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None))])
            return stream()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        eng = Engine(model='fake', client=client, workdir=Path(temp), thinking=False)
        tools = build_artifact_tools(workdir=Path(temp), engine=eng)
        eng.registry.update(tools)
        app = RockyCodeApp(eng, ui_language='zh', evolve='off', permission='yolo')  # the scripted call runs unasked
        eng.hooks.clear()
        app._titled = True
        with patch('rockycode.engine.artifact._open_browser') as opened:
            async with app.run_test(size=(110,32)) as pilot:
                await pilot.pause(.1)
                await tools['create_artifact'].fn('报告 A', '<p>first</p>')
                await pilot.pause(.2)
                a = eng.artifact_registry.records()[0]
                aid = a.artifact_id
                assert not opened.called and not isinstance(app.screen, ArtifactPanel)
                assert isinstance(app.focused, ChatInput) and len(app.query(ArtifactCard)) == 1
                assert await app._artifact_target() is None
                # The card's Edit button binds the target without overwriting a draft.
                app.query_one(ChatInput).text = '保留草稿'
                edit = next(b for b in app.query(ArtifactCard).first().query(Button) if b.name == 'edit')
                await pilot.click(edit)
                await pilot.pause(.1)
                assert app._artifact_edit_target['artifact_id'] == aid
                assert app.query_one(ChatInput).text == '保留草稿'
                assert 'v1' in str(app.query_one(ChatInput).border_title)
                # Leaving edit mode has a place to click, not only a command to type.
                chip = app.query_one('#editchip')
                assert chip.display and 'v1' not in str(chip.render())
                await pilot.click('#editchip')
                await pilot.pause(.1)
                assert app._artifact_edit_target is None and not chip.display
                assert 'v1' not in str(app.query_one(ChatInput).border_title)
                assert app.query_one(ChatInput).text == '保留草稿'
                await tools['create_artifact'].fn('报告 B', '<p>second</p>')
                await pilot.pause(.1)
                bid = eng.artifact_registry.records()[0].artifact_id
                # /artifact list numbers bind the displayed snapshot, not a later sort.
                await app._handle_artifact('/artifact list')
                assert app._artifact_list_ids[0] == bid
                await tools['create_artifact'].fn('报告 A 新版', '<p>new</p>', aid, 1)
                await pilot.pause(.1)
                assert len(app.query(ArtifactCard)) == 2
                await app._handle_artifact('/artifact edit 1')
                assert app._artifact_edit_target['artifact_id'] == bid
                await app._handle_artifact('/artifact cancel')
                # Search, keyboard navigation and ID selection survive another update.
                await app._handle_artifact('/artifact')
                await pilot.pause(.1)
                panel = app.screen
                assert isinstance(panel, ArtifactPanel)
                panel.query_one(Input).value = '报告 B'
                await pilot.pause(.1)
                assert panel.selected().artifact_id == bid
                await tools['create_artifact'].fn('报告 A 最终', '<p>latest</p>', aid, 2)
                panel.refresh_rows()
                assert panel.selected().artifact_id == bid
                await pilot.press('enter')
                await pilot.pause(.2)
                assert opened.call_count == 1 and eng.artifact_registry.get(bid).path.as_uri() == opened.call_args.args[0]
                # History opens the chosen immutable revision, restore publishes v4.
                await app._artifact_action('history', aid)
                await pilot.pause(.1)
                panel = app.screen
                assert isinstance(panel, ArtifactPanel) and panel.query_one(OptionList).option_count == 3
                panel.query_one(OptionList).highlighted = 2
                await pilot.click('#artifact-restore')
                await pilot.pause(.2)
                assert eng.artifact_registry.get(aid).revision == 4
                assert '<p>first</p>' in eng.artifact_registry.get(aid).path.read_text()
                # Submit through the real input and Engine request path: the
                # selected artifact reaches the model even for an immediate turn.
                await app._artifact_action('edit', aid)
                app.query_one(ChatInput).text = 'make it blue'
                await pilot.press('enter')
                for _ in range(80):
                    if prompts and not app._delivery.running:
                        break
                    await pilot.pause(.025)
                assert len(prompts) == 1 and aid in prompts[0] and 'base_revision' in prompts[0]
                assert prompts[0].startswith('make it blue')
                assert app._artifact_edit_target is None
                # The model's own call ends on its line like every tool call: nothing
                # stays at "…", nothing is kept for it, and the card is there as well.
                script.append([SimpleNamespace(index=0, id='c1', function=SimpleNamespace(
                    name='create_artifact', arguments=json.dumps({'title': '报告 C', 'html': '<p>third</p>'})))])
                app.query_one(ChatInput).text = 'make a third report'
                await pilot.press('enter')
                for _ in range(160):
                    if len(prompts) >= 3 and not app._delivery.running:
                        break
                    await pilot.pause(.025)
                await pilot.pause(.2)
                made = [str(w.render()) for w in app._transcript().query(Static) if 'create_artifact' in str(w.render())]
                assert len(made) == 1 and '✓' in made[0] and '⚒' not in made[0] and 'v1' in made[0], made
                assert not app._tool_lines and len(app.query(ArtifactCard)) == 3
                # The TUI opened nothing, and the model was not told otherwise.
                told = next(m['content'] for m in reversed(eng.history) if m['role'] == 'tool')
                assert 'not opened' in told and 'opened in browser' not in told and opened.call_count == 1, told
                # Capture edit context at submission, including while queued.
                await app._artifact_action('edit', aid)
                target = dict(app._artifact_edit_target)
                app._delivery.paused = True
                from rockycode.engine.delivery import TurnMessage
                app._delivery.pending.append(TurnMessage('blocker'))
                await app._submit_delivery('make it blue', [], 'queue', target)
                queued = app._delivery.pending[-1]
                assert app._artifact_turn_contexts[queued.id] == target
                assert app._artifact_edit_target is None
                assert aid in app._artifact_prompt(queued.prompt, target)
                await app._delivery.close()
                # Narrow screen: all action controls stay inside the screen.
                await pilot.resize_terminal(65,24)
                await app._handle_artifact('/artifact')
                await pilot.pause(.1)
                for button in app.screen.query(Button):
                    assert button.region.right <= 65, (button.id, button.region)
                await pilot.press('escape')
                await pilot.pause(.1)
                assert isinstance(app.focused, ChatInput)
    print('TUI ARTIFACT PANEL OK — cards, no popups, edit draft and a clickable way out, stable IDs, search, open, restore, tool line, queue, narrow layout')


asyncio.run(main())
