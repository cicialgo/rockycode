#!/usr/bin/env python3
"""Render the existing Rocky brand mark as a macOS iconset. Python stdlib + Inkscape."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'brand/rockycode-note.svg'
OUTPUT = ROOT / 'apps/desktop/assets'
SIZES = (16, 32, 128, 256, 512)
NS = '{http://www.w3.org/2000/svg}'


def icon_svg(source: bytes, variant: str = 'soft-gradient') -> str:
    """Keep the brand's rectangles exact, but fill them together to avoid hairline seams."""
    if variant not in ('flat', 'soft-gradient'):
        raise ValueError(f'Unknown icon variant: {variant}')
    root = ET.fromstring(source)
    if root.tag != NS + 'svg' or root.attrib.get('viewBox') != '0 0 24 24':
        raise ValueError('Expected the canonical 24 x 24 brand SVG')
    segments = []
    for rect in root:
        if rect.tag != NS + 'rect' or set(rect.attrib) != {'x', 'y', 'width', 'height'}:
            raise ValueError('Brand geometry changed; review the icon generator before regenerating')
        x, y, w, h = (float(rect.attrib[k]) for k in ('x', 'y', 'width', 'height'))
        if not (0 <= x < x+w <= 24 and 0 <= y < y+h <= 24):
            raise ValueError('Rectangle outside canonical viewBox')
        segments.append(f'M{x:g},{y:g}h{w:g}v{h:g}h{-w:g}z')
    if not segments:
        raise ValueError('Empty brand mark')
    digest = hashlib.sha256(source).hexdigest()
    # Background: 896/1024 canvas. Mark: 768/1024 canvas, 85.7% of background width.
    # Original visible y-bounds are 3.6..20.4, centered at 12, so no vertical crop.
    background = '<rect x="64" y="64" width="896" height="896" rx="196" fill="#7d5cc6"/>'
    if variant == 'soft-gradient':
        background = '''<defs>
    <linearGradient id="purple" x1="0" y1="0" x2="0.8" y2="1">
      <stop offset="0" stop-color="#a38ade"/>
      <stop offset="0.48" stop-color="#8867cc"/>
      <stop offset="1" stop-color="#7351b9"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.42"/>
      <stop offset="0.5" stop-color="#ffffff" stop-opacity="0.12"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect x="64" y="64" width="896" height="896" rx="196" fill="url(#purple)"/>
  <rect x="68" y="68" width="888" height="888" rx="192" fill="none" stroke="url(#edge)" stroke-width="8"/>'''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <!-- Generated from brand/rockycode-note.svg; SHA-256 {digest}. -->
  {background}
  <path transform="translate(128 128) scale(32)" fill="#ffffff" d="{''.join(segments)}"/>
</svg>
'''


def check_png(path: Path, size: int) -> None:
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError(f'Not a PNG: {path}')
    width, height, depth, color = struct.unpack('>IIBB', data[16:26])
    if (width, height, depth, color) != (size, size, 8, 6):
        raise ValueError(f'Expected {size} x {size}, 8-bit RGBA: {path}')


def generate(output: Path, inkscape: str, variant: str = 'soft-gradient') -> None:
    output.mkdir(parents=True, exist_ok=True)
    svg = output / 'rockycode-macos.svg'
    svg.write_text(icon_svg(SOURCE.read_bytes(), variant), encoding='utf-8')
    iconset = output / 'RockyCode.iconset'
    iconset.mkdir(exist_ok=True)
    # Duplicate pixel sizes are copied exactly; every unique size is rendered from vector.
    rendered: dict[int, Path] = {}
    for points in SIZES:
        for scale in (1, 2):
            pixels = points * scale
            suffix = '@2x' if scale == 2 else ''
            target = iconset / f'icon_{points}x{points}{suffix}.png'
            if pixels in rendered:
                shutil.copyfile(rendered[pixels], target)
            else:
                subprocess.run([inkscape, str(svg), '--export-type=png',
                    f'--export-filename={target}', f'--export-width={pixels}',
                    f'--export-height={pixels}', '--export-background-opacity=0'], check=True)
                rendered[pixels] = target
            check_png(target, pixels)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('flat', 'soft-gradient'), default='soft-gradient')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--inkscape', default='inkscape', help='Inkscape executable (tested with 1.4)')
    parser.add_argument('--check', action='store_true', help='Compare freshly rendered bytes against committed files')
    args = parser.parse_args()
    if args.variant != 'soft-gradient':
        args.output = args.output / args.variant
    if args.check:
        with tempfile.TemporaryDirectory(prefix='rocky-icons-') as tmp:
            temp = Path(tmp)
            generate(temp, args.inkscape, args.variant)
            expected = {p.relative_to(temp) for p in temp.rglob('*') if p.is_file()}
            actual = {Path('rockycode-macos.svg')} | {
                p.relative_to(args.output) for p in (args.output / 'RockyCode.iconset').glob('*') if p.is_file()}
            if actual != expected:
                raise SystemExit('Iconset has missing or unexpected files')
            for path in sorted(expected):
                if (temp / path).read_bytes() != (args.output / path).read_bytes():
                    raise SystemExit(f'Generated asset differs: {path}; use the documented renderer version')
        print('All 10 PNGs and master SVG match regenerated assets')
    else:
        generate(args.output, args.inkscape, args.variant)
        print(f'Generated 10 macOS iconset PNGs in {args.output}')


if __name__ == '__main__':
    main()
