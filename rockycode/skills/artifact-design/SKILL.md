---
name: artifact-design
description: How to write an HTML artifact for rocky's own page — what to send to create_artifact so a report, comparison, walkthrough or diagram comes out clear in light and dark without choosing a single colour. Load before making or restyling any artifact that is more than a short note.
---

# artifact-design — write the content, the page does the look

`create_artifact` puts a **fragment** on rocky's page. The page brings the
type, the colours, the spacing and a dark mode that follows the reader's
system. Your job is the content and its order. Send plain HTML and choose
**no colours, no fonts, no sizes**.

A reader opens an artifact to get one thing answered. Lead with it.

## The shape of a good artifact

```html
<h1>How rocky decides where a request goes</h1>
<p>One row in the model table decides everything: the provider, the address
and which key is sent.</p>

<h2>What each part does</h2>
<table>…</table>

<h2>Try it</h2>
<pre><code>…</code></pre>
```

- **One `<h1>`**: the topic or the claim. Leave it out and the title is used.
- **The first `<p>` after it is the answer**, in one or two sentences. The page
  sets it larger, with a bar beside it. Never spend it on "This document…".
- **`<h2>` per section**, three to five of them. A section that needs its own
  sections is usually two artifacts.
- Short paragraphs. A list when the items are parallel, a table when they
  have the same fields, a drawing when it is about how parts connect.

## The parts the page knows

| write | it becomes |
|---|---|
| `<table>` with `<th>` | a quiet comparison table |
| `<blockquote>` | a note: the one thing not to miss |
| `<details><summary>` | depth a reader can skip |
| `<pre><code>`, `<code>`, `<kbd>` | code, inline code, a key |
| `<mark>` | a highlight inside a sentence |
| `<figure>` + `<figcaption>` | a drawing with its caption |
| `<div class="grid">` of `<div class="card">` | two to four things side by side |
| `<span class="tag">` | a small label |
| `class="muted"`, `<small>` | secondary text |

Use at most one note per section, and cards only for things that really are
side by side. Everything else is headings, paragraphs and lists.

## Drawings: inline SVG with four classes

Give the `<svg>` a `viewBox` and no fixed width. Colour comes from classes, so
the drawing follows the look and the dark mode:

| class | on | meaning |
|---|---|---|
| `box` | `<rect>`, `<circle>`, `<path>` | an ordinary part |
| `hot` | the same | the one part this drawing is about |
| `flow` | `<path>`, `<line>` | a connection or arrow |
| `tip` | the arrowhead inside a `<marker>` | |
| `quiet` | `<text>` | a secondary label |

Plain `<text>` is already the text colour. Do not write `fill="#…"` or
`stroke="#…"`: a colour you hard-code is the one that disappears in the dark.

```html
<figure>
  <svg viewBox="0 0 360 80" role="img" aria-label="A request goes from the row to the key">
    <defs><marker id="tip" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7"
      orient="auto"><path class="tip" d="M0 0L8 4L0 8z"/></marker></defs>
    <rect class="hot" x="4" y="14" width="140" height="52" rx="10"/>
    <text x="74" y="38" text-anchor="middle">model row</text>
    <text class="quiet" x="74" y="56" text-anchor="middle">kimi-k3</text>
    <rect class="box" x="216" y="14" width="140" height="52" rx="10"/>
    <text x="286" y="45" text-anchor="middle">key</text>
    <path class="flow" d="M144 40H214" marker-end="url(#tip)"/>
  </svg>
  <figcaption>You pick the row. rocky follows it and never guesses.</figcaption>
</figure>
```

One `hot` part per drawing. A drawing where everything is highlighted says nothing.

## When you need a colour of your own

Use the page's seven, by name, so light and dark both work:
`var(--paper)` the page · `var(--ink)` text · `var(--muted)` captions ·
`var(--line)` hairlines · `var(--wash)` quiet fill · `var(--accent)` the one
colour · `var(--accent-wash)` a note's fill. For example
`style="border-left:3px solid var(--accent)"`. Meaning is never carried by
colour alone: say it in words too.

## Layout

`layout="document"` (the default) is a reading width. `layout="wide"` is for a
dashboard or a table with many columns; text still keeps a readable width.

## The look belongs to the user

A new artifact gets the user's look (`/config artifact_theme ink | amber |
lilac | none`). Leave `theme` out. Pass it only when the user asks for a
particular look for this artifact. A revision keeps the look it has.

## When the user wants their own design

A brand palette, a specific style, an interactive page with its own layout:
send a **complete HTML document** (`<!DOCTYPE html>` …). It is saved exactly
as you wrote it and the page adds nothing, so then you own everything,
dark mode included. Do not mix the two: a fragment with half a palette of its
own fights the page.

## Always

- Self-contained: no CDN, no web fonts, no remote images. Inline SVG, or a
  `data:` image when you must.
- Write in the user's language. Chinese and mixed text are set properly.
- To change an existing artifact: `inspect_artifact`, then `create_artifact`
  with its `artifact_id` and `base_revision`. You get the fragment back, not
  the page around it: send a fragment again. Keep its structure unless asked.
- Do not put the whole conversation in it. An artifact is the result, not the log.
