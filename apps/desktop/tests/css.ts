import {readFileSync} from 'node:fs';

/** A stylesheet from src/renderer, comments removed. */
export const read = (name: string) => readFileSync(new URL(`../src/renderer/${name}`, import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

export type Rule = {selector: string; body: string; dark: boolean};

/** Flat rules of a sheet. Rules inside @media are included, and marked when the query is the
 * dark colour scheme; other at-rules are skipped. */
export function rules(css: string, dark = false): Rule[] {
  const found: Rule[] = [];
  let index = 0;
  while (index < css.length) {
    const open = css.indexOf('{', index);
    if (open < 0) break;
    const prelude = css.slice(index, open).trim();
    let depth = 1, close = open + 1;
    for (; close < css.length && depth; close++) depth += css[close] === '{' ? 1 : css[close] === '}' ? -1 : 0;
    const body = css.slice(open + 1, close - 1);
    if (prelude.startsWith('@media')) found.push(...rules(body, dark || prelude.replace(/\s/g, '').includes('prefers-color-scheme:dark')));
    else if (!prelude.startsWith('@')) found.push({selector: prelude, body, dark});
    index = close;
  }
  return found;
}

/** A stated colour: a hex value or `white`. `transparent` and `currentColor` are the same in every theme. */
export const COLOUR = /#[0-9a-f]{3,8}\b|\bwhite\b/i;

/** One name per thing a theme can recolour: `border: 1px solid #x` and `border-color: #y` are the same slot. */
export const slot = (property: string) => property.replace(/^(border(?:-(?:top|right|bottom|left))?|outline)$/, '$1-color').replace(/^background-color$/, 'background');

export type Declaration = {part: string; property: string; value: string};

/** Every declaration of a rule, once per comma-separated selector part. */
export function declarations(rule: Rule, selector = rule.selector): Declaration[] {
  const found: Declaration[] = [];
  for (const declaration of rule.body.split(';')) {
    const [property, ...rest] = declaration.split(':');
    if (!rest.length || property.trim().startsWith('--') || property.trim().startsWith('mask')) continue;
    for (const part of selector.split(',')) found.push({part: part.trim().replace(/\s+/g, ' '), property: property.trim(), value: rest.join(':').trim()});
  }
  return found;
}
