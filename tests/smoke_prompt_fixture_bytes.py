"""Read-only observations must preserve real repository CRLF files."""
from pathlib import Path
from tempfile import TemporaryDirectory
from bench.prompt_ablation import snapshot, capture_workspace, restore_workspace, changed

with TemporaryDirectory() as d:
    root=Path(d)/'original';root.mkdir()
    raw=b'# keep\r\nx = 1\r\n'
    (root/'windows.toml').write_bytes(raw)
    before=snapshot(root)
    assert before=={'windows.toml':raw.decode()}
    for cache in ['__pycache__','.pytest_cache']:
        (root/cache).mkdir();(root/cache/'temporary').write_bytes(b'cache')
    assert snapshot(root)==before
    target=Path(d)/'restored';target.mkdir()
    restore_workspace(target,capture_workspace(root))
    assert (target/'windows.toml').read_bytes()==raw
    assert changed(before,snapshot(target))==[]
    (root/'windows.toml').write_bytes(raw.replace(b'\r\n',b'\n'))
    assert changed(before,snapshot(root))==['windows.toml']
print('FIXTURE BYTES SMOKE OK — CRLF preserved, test caches excluded, newline-only edits observed')
