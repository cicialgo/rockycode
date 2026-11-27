"""Real filesystem/SQLite revisions, independent registries, conflict and failure controls."""
import asyncio
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rockycode.engine.artifact import ArtifactRegistry, ArtifactServer, _artifact_html, build_artifact_tools
from rockycode.engine.artifact_store import ArtifactStore


async def main():
    full = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{background:navy;color:white}.grid{display:grid}</style></head><body><h1>Custom</h1></body></html>'
    assert _artifact_html('X', full) == full
    assert _artifact_html('X', full, 'rocky', 'wide') == full
    # A full document with no charset gets one and nothing else: the file is UTF-8,
    # and a browser opening it from disk would otherwise guess (Chinese as garbage).
    bare = '<!DOCTYPE html><html lang="zh"><head><title>报告</title></head><body>中文</body></html>'
    assert _artifact_html('X', bare) == bare.replace('<head>', '<head><meta charset="utf-8">')
    assert _artifact_html('X', '<html><body>中文</body></html>') == '<html><meta charset="utf-8"><body>中文</body></html>'
    assert _artifact_html('X', '<body>中文</body>') == '<meta charset="utf-8"><body>中文</body>'
    old_style = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head><body>x</body></html>'
    assert _artifact_html('X', old_style) == old_style
    fragment = '<style>body{background:green}h1{color:yellow}</style><h1>Mine</h1>'
    neutral = _artifact_html('X', fragment)
    assert neutral.count('<h1>') == 1 and fragment in neutral and '#f6f4fb' not in neutral
    assert '#f6f4fb' in _artifact_html('X', fragment, 'rocky')
    assert '<h1>&lt;title&gt;</h1>' in _artifact_html('<title>', '<p>x</p>')
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        def host(session):
            return SimpleNamespace(trajectory=SimpleNamespace(session_id=session), artifact_auto_open=False)
        one = host('session-a')
        tools = build_artifact_tools(workdir=wd, engine=one)
        create, inspect = tools['create_artifact'].fn, tools['inspect_artifact'].fn
        first = await create('Same title', full)
        # This host opens nothing by itself, so the model is not told "opened".
        assert '[ok]' in first and 'not opened' in first and 'opened in browser' not in first, first
        original = one.artifact_registry.records()[0]
        aid = original.artifact_id
        path1 = original.path
        assert path1.read_text() == full
        assert '[ok]' in await create('Same title', '<p>separate</p>')
        assert one.artifact_registry.count == 2
        assert '[ok]' in await create('Renamed', '<p>v2</p>', aid, 1)
        assert one.artifact_registry.get(aid).revision == 2
        assert path1.read_text() == full and len(one.artifact_registry.history(aid)) == 2
        before = one.artifact_registry.get(aid).path
        assert '[error]' in await create('stale', '<p>lost update</p>', aid, 1)
        assert '[error]' in await create('missing base', '<p>x</p>', aid)
        assert '[error]' in await create('invalid theme', '<p>x</p>', aid, 2, theme='random')
        assert one.artifact_registry.get(aid).path == before
        # Independent restarted host sees existing work, but not as its own session.
        two = host('session-b'); tools2 = build_artifact_tools(workdir=wd, engine=two)
        assert len(two.artifact_registry.records()) == 2 and not two.artifact_registry.records('session')
        # What a status line or another host lists is this session's, not the project's.
        assert two.artifact_registry.count == 0 and two.artifact_registry.snapshot('session') == []
        assert len(two.artifact_registry.snapshot()) == 2
        two.artifact_registry.resume_from('session-a')
        assert len(two.artifact_registry.records('session')) == 2
        third = ArtifactRegistry(wd, 'session-c'); third.resume_from('session-b')
        assert len(third.records('session')) == 2
        read = json.loads(await tools2['inspect_artifact'].fn(aid, 1))
        assert read['html'] == full and read['revision'] == 1 and read['title'] == 'Same title'
        assert '[error]' in await inspect(aid, 0)
        # Both runtime ordering paths reconnect resumed-session provenance.
        from rockycode.engine.loop import Engine
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()))
        resumed = Engine(model='fake', client=fake, workdir=wd)
        resumed.resume([], from_session='session-a')
        build_artifact_tools(workdir=wd, engine=resumed)
        assert len(resumed.artifact_registry.records('session')) == 2
        resumed_later = Engine(model='fake', client=fake, workdir=wd)
        build_artifact_tools(workdir=wd, engine=resumed_later)
        resumed_later.resume([], from_session=resumed.trajectory.session_id)
        assert len(resumed_later.artifact_registry.records('session')) == 2
        # Competing writers use database CAS, not stale registry state.
        assert '[ok]' in await tools2['create_artifact'].fn('B edit', '<p>v3</p>', aid, 2)
        assert '[error]' in await create('A edit', '<p>stale</p>', aid, 2)
        assert one.artifact_registry.get(aid).revision == 3
        # Simulate failed filesystem write: published revision stays readable.
        with patch('rockycode.engine.artifact_store.os.replace', side_effect=OSError('disk full')):
            failed = await create('failed', '<p>x</p>', aid, 3)
        assert '[error]' in failed and one.artifact_registry.get(aid).revision == 3
        # A process can die after writing the unpublished file, before committing
        # its metadata. Retrying that revision must not permanently wedge editing.
        store = ArtifactStore(wd)
        orphan = store.path(aid, 4, one.artifact_registry.get(aid).name)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text('unpublished partial edit')
        # Real live server reads the latest revision; stored bytes contain no token.
        server = ArtifactServer(wd, registry=one.artifact_registry)
        async def target():
            await server.start()
            return server
        one.artifact_target = target
        try:
            assert '[ok]' in await create('Live update', '<p>v4</p>', aid, 3)
            import aiohttp
            async with aiohttp.ClientSession() as client:
                async with client.get(one.artifact_registry.get(aid).url) as response:
                    text = await response.text()
                    assert response.status == 200 and '<p>v4</p>' in text and server.token in text
            assert server.token not in one.artifact_registry.get(aid).path.read_text()
        finally:
            await server.stop()
        one.artifact_target = None
        assert '[ok]' in await create('Static again', '<p>v5</p>', aid, 4)
        assert len(one.artifact_registry.history(aid)) == 5
        # Restore old bytes by publishing a new revision (no history deletion).
        old = json.loads(await inspect(aid, 1))
        assert '[ok]' in await create(old['title'], old['html'], aid, 5, theme=old['theme'], layout=old['layout'])
        assert one.artifact_registry.get(aid).revision == 6
        assert one.artifact_registry.get(aid).path.read_text() == full
        # Explicit import leaves old top-level output untouched.
        legacy = wd / '.rockycode/artifacts/legacy.html'; legacy.write_text(full)
        store = ArtifactStore(wd)
        imported = store.import_legacy(legacy, 'session-a')
        assert imported != aid and legacy.read_text() == full
        outside = wd / 'outside.html'; outside.write_text('outside')
        link = store.root / 'linked.html'; link.symlink_to(outside)
        try:
            store.import_legacy(link, 'session-a')
            raise AssertionError('symlink accepted')
        except ValueError:
            pass
        assert len(json.loads(await inspect())) == 3
        # Real simultaneous writers: exactly one CAS update succeeds and the
        # other cannot replace its file, even with separate SQLite connections.
        def concurrent_edit(number):
            try:
                ArtifactStore(wd).save(title='concurrent', document=f'<p>{number}</p>',
                    session_id='session-a', theme='none', layout='document',
                    artifact_id=aid, base_revision=6)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(concurrent_edit, [1, 2])) == [False, True]
        assert one.artifact_registry.get(aid).revision == 7
        assert path1.read_text() == full
        async def broken_preview():
            raise OSError('port in use')
        one.artifact_target = broken_preview
        saved = await create('Preview unavailable', '<p>still saved</p>', aid, 7)
        assert saved.startswith('[ok]') and 'preview unavailable' in saved
        assert one.artifact_registry.get(aid).revision == 8
        # A host that presents artifacts itself gets every static save: each
        # revision is a new file, so an update is a new URL, not a reload.
        shown = []
        presenter = SimpleNamespace(trajectory=SimpleNamespace(session_id='session-p'), artifact_opener=shown.append)
        create_p = build_artifact_tools(workdir=wd, engine=presenter)['create_artifact'].fn
        made = await create_p('Shown', '<p>one</p>')
        pid = presenter.artifact_registry.records('session')[0].artifact_id
        changed = await create_p('Shown', '<p>two</p>', pid, 1)
        assert 'opened in browser: file://' in made and 'opened in browser: file://' in changed
        assert len(shown) == 2 and shown[0] != shown[1] and shown[1].endswith('.html'), shown
        assert presenter.artifact_registry.count == 1
    print('ARTIFACT VERSIONS OK — styles, IDs, history, restart, session ancestry, CAS, failed write, live/static, import')


asyncio.run(main())
