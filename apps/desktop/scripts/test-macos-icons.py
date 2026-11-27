#!/usr/bin/env python3
"""Offline icon asset contract tests; no Inkscape or third-party Python packages needed."""
import hashlib
import importlib.util
from pathlib import Path
import re
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('generate_icons', Path(__file__).with_name('generate-macos-icons.py'))
icons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(icons)


class MacIconTests(unittest.TestCase):
    def test_master_is_generated_from_current_brand(self):
        svg = (icons.OUTPUT / 'rockycode-macos.svg').read_text()
        self.assertEqual(svg, icons.icon_svg(icons.SOURCE.read_bytes()))
        self.assertIn(hashlib.sha256(icons.SOURCE.read_bytes()).hexdigest(), svg)

    def test_geometry_is_exactly_the_original_rectangles(self):
        source = ET.fromstring(icons.SOURCE.read_bytes())
        root = ET.fromstring(icons.icon_svg(icons.SOURCE.read_bytes()))
        path = root.find(icons.NS + 'path')
        commands = re.findall(r'M([^,]+),([^h]+)h([^v]+)v([^h]+)h([^z]+)z', path.attrib['d'])
        self.assertEqual(len(commands), len(source))
        for rect, command in zip(source, commands):
            x, y, w, h, back = map(float, command)
            self.assertEqual((x, y, w, h), tuple(float(rect.attrib[k]) for k in ('x', 'y', 'width', 'height')))
            self.assertEqual(back, -w)
        self.assertEqual(path.attrib['transform'], 'translate(128 128) scale(32)')
        self.assertEqual(path.attrib['fill'], '#ffffff')

    def test_complete_1x_2x_iconset(self):
        directory = icons.OUTPUT / 'RockyCode.iconset'
        expected = set()
        payloads = {}
        for points in icons.SIZES:
            for scale in (1, 2):
                suffix = '@2x' if scale == 2 else ''
                name = f'icon_{points}x{points}{suffix}.png'
                expected.add(name)
                path = directory / name
                icons.check_png(path, points * scale)
                if points * scale in payloads:
                    self.assertEqual(path.read_bytes(), payloads[points * scale])
                payloads[points * scale] = path.read_bytes()
        self.assertEqual({p.name for p in directory.iterdir()}, expected)
        self.assertEqual(set(payloads), {16, 32, 64, 128, 256, 512, 1024})

    def test_gradient_preserves_mark_and_has_no_filters(self):
        flat = ET.fromstring(icons.icon_svg(icons.SOURCE.read_bytes(), 'flat'))
        gradient = ET.fromstring(icons.icon_svg(icons.SOURCE.read_bytes(), 'soft-gradient'))
        self.assertEqual(flat.find(icons.NS + 'path').attrib, gradient.find(icons.NS + 'path').attrib)
        self.assertIsNone(gradient.find('.//' + icons.NS + 'filter'))
        self.assertEqual(len(gradient.findall('.//' + icons.NS + 'linearGradient')), 2)
        self.assertEqual((icons.OUTPUT / 'rockycode-macos.svg').read_text(), icons.icon_svg(icons.SOURCE.read_bytes(), 'soft-gradient'))

    def test_gradient_iconset(self):
        directory = icons.OUTPUT / 'RockyCode.iconset'
        self.assertEqual(len(list(directory.glob('*.png'))), 10)
        for points in icons.SIZES:
            for scale in (1, 2):
                suffix = '@2x' if scale == 2 else ''
                icons.check_png(directory / f'icon_{points}x{points}{suffix}.png', points * scale)
        with self.assertRaises(ValueError):
            icons.icon_svg(icons.SOURCE.read_bytes(), 'unsupported')

    def test_rejects_unreviewed_svg_geometry(self):
        for svg in (b'<svg/>', b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0"/></svg>', b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"/>'):
            with self.assertRaises(ValueError):
                icons.icon_svg(svg)

    def test_rejects_invalid_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'invalid.png'
            p.write_bytes(b'not a png')
            with self.assertRaises(ValueError):
                icons.check_png(p, 16)
            p.write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>IIBBBBB', 32, 32, 8, 6, 0, 0, 0))
            with self.assertRaises(ValueError):
                icons.check_png(p, 16)


if __name__ == '__main__':
    unittest.main()
