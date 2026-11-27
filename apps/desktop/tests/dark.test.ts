import {test} from 'node:test';
import assert from 'node:assert/strict';
import {COLOUR, declarations, read, rules, slot} from './css';

// System dark mode recolours by exception: the dark blocks restate only what does not work on a
// dark page. This test finds the exceptions nobody restated — a light fill, a light line, text
// too dark to read — so a new component cannot ship as a white block in dark mode.
const PAGE = '#151519';     // the dark page
const SURFACE = '#1c1c21';  // the lightest dark surface text sits on

function channels(hex: string): [number, number, number, number] {
  let value = hex.replace('#', '');
  if (value.length <= 4) value = [...value].map(digit => digit + digit).join('');
  const at = (index: number) => parseInt(value.slice(index, index + 2), 16) / 255;
  return [at(0), at(2), at(4), value.length === 8 ? at(6) : 1];
}
function luminance(hex: string): number {
  let [red, green, blue, alpha] = channels(hex);
  if (alpha < 1) {  // a translucent colour is seen over the dark page
    const [pageRed, pageGreen, pageBlue] = channels(PAGE);
    [red, green, blue] = [alpha * red + (1 - alpha) * pageRed, alpha * green + (1 - alpha) * pageGreen, alpha * blue + (1 - alpha) * pageBlue];
  }
  const linear = (channel: number) => channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
}
const contrast = (first: number, second: number) => (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
const colours = (value: string) => [...value.matchAll(/#[0-9a-f]{3,8}\b/gi)].map(match => match[0]).concat(/\bwhite\b/i.test(value) ? ['#ffffff'] : []);

/** Why a light-page colour fails on a dark page, or null when it reads on both. */
function problem(property: string, value: string): string | null {
  const stated = colours(value);
  if (!stated.length || property === 'box-shadow' || property === 'accent-color') return null;
  if (property === 'color') return contrast(luminance(stated[0]), luminance(SURFACE)) < 4.5 ? 'text too dark to read' : null;
  const light = stated.some(colour => luminance(colour) > 0.4);
  if (property.startsWith('background')) return light ? 'light fill' : null;
  if (property.startsWith('border') || property.startsWith('outline')) return light ? 'light line' : null;
  return null;
}

// Stated once, with the reason, instead of silently skipped.
const untouched = new Set([
  '.artifact-panel iframe { background }',  // a preview document is shown on the white page it was written for
  '.status-dot { background }',             // a 6 px idle marker: light on purpose, it has to show on a dark sidebar
]);

test('every colour that only works on a light page has a dark-mode value', () => {
  const holes: string[] = [];
  for (const sheet of ['styles.css', 'race.css']) {
    const base = new Map<string, {property: string; value: string}>();
    const dark = new Set<string>();
    for (const rule of rules(read(sheet))) {
      for (const {part, property, value} of declarations(rule)) {
        const pair = `${part} { ${slot(property)} }`;
        if (rule.dark) dark.add(pair);
        else if (COLOUR.test(value)) base.set(pair, {property, value});  // the last base declaration is the effective one
      }
    }
    for (const [pair, {property, value}] of base) {
      if (dark.has(pair) || untouched.has(pair)) continue;
      const reason = problem(property, value);
      if (reason) holes.push(`${sheet}: ${pair} — ${reason} (${value})`);
    }
  }
  assert.deepEqual(holes, [], `these have no value inside @media(prefers-color-scheme:dark). Add one, or list the pair in "untouched" with the reason:\n  ${holes.join('\n  ')}`);
});
