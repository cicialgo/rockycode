"""Headless bilingual selectors: narrow layouts, safe CJK titles, stable actions."""
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList, Static

from rockycode.engine.modes import Mode, discover
from rockycode.engine.providers import Choice, Endpoint, Provider
from rockycode.i18n import CATALOG
from rockycode.session import SessionInfo
from rockycode.tui.modelpicker import EndpointPicker, ModelPicker, _key_note
from rockycode.tui.modepicker import ModePicker
from rockycode.tui.plangate import InlinePlanGate
from rockycode.tui.resume import ResumePicker, rel_time
from rockycode.tui import cardkeys

cardkeys.ENTER_GUARD = 0  # the half second is tested in smoke_tui_card_keys.py; here Enter answers a card at once


class Host(App):
    def __init__(self, screen=None, gate=None):
        super().__init__()
        self.dialog = screen
        self.gate = gate
        self.result = 'pending'

    def compose(self):
        if self.gate is not None:
            yield self.gate

    def on_mount(self):
        if self.dialog is not None:
            self.push_screen(self.dialog, self.picked)

    def picked(self, result):
        self.result = result


def session(title):
    return SessionInfo('20260717-153000-abc123', Path('/tmp/session.jsonl'), 'project',
                       'project', '/tmp/目录[bold]', 'model', time.time(), 2, title)


def check_guide(widget):
    # Auto height must accommodate all wrapped text, not just its first line.
    text = Text.from_markup(str(widget.render()))
    assert widget.size.height >= 1
    assert widget.size.width > 0
    assert widget.size.height >= len(text.wrap(widget.app.console, widget.size.width))


async def main():
    assert CATALOG['en'].keys() == CATALOG['zh'].keys()
    with patch('rockycode.tui.resume.time.time', return_value=200000):
        for elapsed, en, zh in [(0, 'just now', '刚刚'), (120, '2m ago', '2分钟前'),
                                (7200, '2h ago', '2小时前'), (86400, 'yesterday', '昨天'),
                                (172800, '2d ago', '2天前')]:
            assert rel_time(200000-elapsed) == en
            assert rel_time(200000-elapsed, 'zh') == zh
    ep = Endpoint('test-local', 'http://localhost:1234/v1', '')
    prov = Provider('test', ['model'], [ep], label='Test', vision=True)
    choice = Choice(prov, ep, 'model')
    assert '无需密钥' in _key_note(choice, 'zh')
    for title in ['中文标题' * 20, '中' * 21 + '[/bold]尾', 'a' * 43 + '[/red]', '短标题']:
        row = Text.from_markup(ResumePicker(Path('/tmp'))._fmt(session(title))).plain
        assert set_cell_size(title, 44) in row
        assert cell_len(set_cell_size(title, 44)) == 44
        assert '目录[bold]' in row
    builtin = next(iter(discover()['research'].values()))
    local = Mode('research', builtin.name, 'User description [literal]', 'User preview [literal]',
                 'private contract', False, Path('/tmp/mode.md'))
    for locale in ('en', 'zh'):
        for width in (50, 60, 80):
            picker = ModelPicker([choice], current=choice.id, hidden=3, language=locale)
            app = Host(picker)
            async with app.run_test(size=(width, 28)) as pilot:
                await pilot.pause()
                guide = picker.query_one('#model-guide', Static)
                check_guide(guide)
                guide.scroll_visible(animate=False)
                await pilot.pause()
                assert picker.query_one('#picker').content_region.contains_region(guide.region)
                assert picker.query_one(OptionList).get_option_at_index(1).id == 'all'
                await pilot.press('enter')
                assert app.result == [choice]
            picker = EndpointPicker([choice], current=choice.id, language=locale)
            app = Host(picker)
            async with app.run_test(size=(width, 28)) as pilot:
                await pilot.pause()
                guide = picker.query_one('#model-guide', Static)
                check_guide(guide)
                guide.scroll_visible(animate=False)
                await pilot.pause()
                assert picker.query_one('#picker').content_region.contains_region(guide.region)
                await pilot.press('down', 'enter')
                inp = picker.query_one(Input)
                assert inp.has_focus
                inp.value = 'http://localhost:9999/v1'
                await pilot.press('enter')
                assert app.result == ('custom', 'http://localhost:9999/v1')
            picker = ModePicker('research', [builtin, local], active=builtin.name, language=locale)
            assert picker._mode_text(local, 'description') == local.description
            assert picker._mode_text(local, 'preview') == local.preview
            if locale == 'zh':
                assert picker._mode_text(builtin, 'description') != builtin.description
            app = Host(picker)
            async with app.run_test(size=(width, 28)) as pilot:
                await pilot.pause()
                guide = picker.query_one('#mode-guide', Static)
                check_guide(guide)
                guide.scroll_visible(animate=False)
                await pilot.pause()
                assert picker.query_one('#picker').content_region.contains_region(guide.region)
                await pilot.press('enter')
                assert app.result == 'off'
            picker = ResumePicker(Path('/tmp'), language=locale)
            app = Host(picker)
            with patch('rockycode.tui.resume.list_sessions', return_value=[session('中文[/red]标题' * 8)]):
                async with app.run_test(size=(width, 28)) as pilot:
                    await pilot.pause()
                    check_guide(picker.query_one('#picker-guide', Static))
                    assert picker.query_one('#q', Input).has_focus
                    await pilot.press('ctrl+a')
                    assert picker.scope == 'all'
                    await pilot.press('enter')
                    assert isinstance(app.result, SessionInfo)
            future = asyncio.get_running_loop().create_future()
            gate = InlinePlanGate('plan.md', '# User plan\n' + '- 用户计划\n' * 50,
                                  future, language=locale)
            app = Host(gate=gate)
            async with app.run_test(size=(width, 28)) as pilot:
                await pilot.pause()
                assert gate.query_one('#plan-scroll', VerticalScroll).max_scroll_y > 0
                check_guide(gate.query_one('#plan-keys', Static))
                assert app.screen.region.contains_region(gate.query_one('#plan-keys').region)
                await pilot.press('pagedown')
                assert gate.query_one('#plan-scroll', VerticalScroll).scroll_y > 0
                await pilot.press('down', 'enter')
                assert await future == 'approve'
    for keys, expected in [(('enter',), 'discuss'), (('down', 'enter'), 'approve'), (('down', 'down', 'enter'), 'goal'),
                           (('down', 'down', 'down', 'enter'), 'exit'), (('escape',), 'dismiss')]:
        future = asyncio.get_running_loop().create_future()
        gate = InlinePlanGate('plans/[bold]literal[/bold].md', 'User plan', future, language='zh')
        async with Host(gate=gate).run_test() as pilot:
            await pilot.pause()
            assert 'plans/[bold]literal[/bold].md' in Text.from_markup(gate.border_title).plain
            await pilot.press(*keys)
            assert await future == expected
    bracket_choice = Choice(prov, ep, '[bold]model[/bold]')
    picker = EndpointPicker([bracket_choice], current=bracket_choice.id, language='zh')
    async with Host(picker).run_test() as pilot:
        await pilot.pause()
        assert '[bold]model[/bold]' in Text.from_markup(picker.query_one('#picker').border_title).plain
    print('TUI I18N SELECTORS SMOKE OK — en/zh at 50/60/80 columns; stable actions and CJK markup')


if __name__ == '__main__':
    asyncio.run(main())
