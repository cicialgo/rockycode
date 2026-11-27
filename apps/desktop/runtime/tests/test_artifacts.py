import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rockycode_desktop.artifacts import read_artifact, MAX_HTML_BYTES


class ArtifactTests(unittest.TestCase):
    def test_local_html_unicode_urls_and_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '项目 介绍.html'
            path.write_text('<h1>你好</h1>', encoding='utf-8-sig')
            for reference in [path.name, path.as_uri() + '#section', str(path)]:
                result = read_artifact(directory, reference)
                self.assertEqual(result['path'], str(path.resolve()))
                self.assertEqual(result['html'], '<h1>你好</h1>')

    def test_reject_escape_remote_file_hosts_non_html_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'workspace'
            root.mkdir()
            secret = Path(directory) / 'secret.html'
            secret.write_text('secret')
            (root / 'plain.txt').write_text('not html')
            (root / 'escape.html').symlink_to(secret)
            (root / 'escape-dir').symlink_to(secret.parent, target_is_directory=True)
            for reference in ['../secret.html', '%2e%2e/secret.html', str(secret), secret.as_uri(), 'file://remote/secret.html', 'https://example.com/secret.html', 'javascript:alert(1)', 'plain.txt', 'escape.html', 'escape-dir/secret.html']:
                with self.subTest(reference=reference), self.assertRaises((ValueError, OSError)):
                    read_artifact(root, reference)

    def test_size_type_and_encoding_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.html'
            path.write_bytes(b'x' * (MAX_HTML_BYTES + 1))
            with self.assertRaises(ValueError):
                read_artifact(directory, path.name)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeError):
                read_artifact(directory, path.name)
            if hasattr(os, 'mkfifo'):
                fifo = Path(directory) / 'pipe.html'
                os.mkfifo(fifo)
                with self.assertRaises(ValueError):
                    read_artifact(directory, fifo.name)
