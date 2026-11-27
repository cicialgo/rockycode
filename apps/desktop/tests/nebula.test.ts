import {test} from 'node:test';
import assert from 'node:assert/strict';
import {COLOUR, declarations, read, rules, slot} from './css';

// Nebula is a full recolour: every colour the base sheets state needs a Nebula value, or a new
// component shows up in the system palette inside the theme. This test is that guarantee.

/** The Nebula block's nested rules. The text before a nested rule can end with the root's own
 * declarations, so a selector starts after the last `;`. */
function nebulaRules() {
  const nebula = read('nebula.css');
  const block = nebula.slice(nebula.indexOf('{') + 1, nebula.lastIndexOf('}'));
  return {block, nested: rules(block).map(rule => ({...rule, selector: rule.selector.slice(rule.selector.lastIndexOf(';') + 1)}))};
}

// Stated once, with the reason, instead of silently skipped.
const untouched = new Set([
  '.artifact-panel iframe { background }',  // a preview document is shown on the white page it was written for
]);

test('every colour in the base stylesheets has a Nebula value', () => {
  const {block, nested} = nebulaRules();
  const themed = new Set(nested.flatMap(rule => declarations(rule).map(({part, property}) => `${part} { ${slot(property)} }`)));
  for (const property of ['color', 'background']) assert.match(block, new RegExp(`(^|;)\\s*${property}:var\\(--n-`), `:root needs a Nebula ${property}`);
  const base = new Set(['styles.css', 'race.css'].flatMap(sheet => rules(read(sheet)).flatMap(rule =>
    declarations(rule).filter(({value}) => COLOUR.test(value)).map(({part, property}) => `${part} { ${slot(property)} }`))));
  base.delete(':root { color }');
  base.delete(':root { background }');
  const missing = [...base].filter(pair => !themed.has(pair) && !untouched.has(pair)).sort();
  assert.deepEqual(missing, [], `nebula.css has no value for these. Add a line for each, or list it in "untouched" with the reason:\n  ${missing.join('\n  ')}`);
});

test('the Nebula layer only uses colours from its own token table', () => {
  const {block, nested} = nebulaRules();
  const stray = nested.flatMap(rule => rule.body.split(';').filter(declaration => COLOUR.test(declaration)).map(declaration => `${rule.selector} { ${declaration.trim()} }`));
  assert.deepEqual(stray, [], 'a rule in nebula.css states a raw colour; name it as a --n- token at the top instead');
  const defined = new Set([...block.matchAll(/(--n-[\w-]+):/g)].map(match => match[1]));
  const used = new Set([...block.matchAll(/var\((--n-[\w-]+)\)/g)].map(match => match[1]));
  assert.deepEqual([...used].filter(token => !defined.has(token)).sort(), [], 'used but never defined');
  assert.deepEqual([...defined].filter(token => !used.has(token)).sort(), [], 'defined but never used');
});
