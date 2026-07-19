---
name: architecture-viz
description: Visualize a neural-net block (attention, transformer, MoE, diffusion-LLM) as an artifact three linked ways — the paper diagram, the tensor-shape ribbon, and the user's PyTorch — cross-highlighted so hovering one lights the other two. Grounded in the real paper/code, never drawn from memory.
---

# architecture-viz — one block, three linked views

The register (LOCKED — the user chose it after rejecting two others):
**a recognizable paper diagram + a shape ribbon + the user's PyTorch code, all
cross-highlighted.** Hover any box, shape-chip, or code line and the matching
two light up. That link is the whole point — it's what turns an abstract figure
into something the user can tie to the code they actually write.

## Why exactly these three (do not drop any)

- **Paper diagram** — the figure people recognize at a glance (e.g. Attention
  Is All You Need's scaled-dot-product / multi-head). The anchor.
- **Shape ribbon** — how the tensor shape changes step by step
  (`(n,d) → (n,dₖ)×3 → (n,n) → (n,n) → (n,dₖ)`). This is the real value.
- **PyTorch** — the language the user writes. NEVER show a formal language
  (Lean/TorchLean syntax) as the teaching vehicle: a language they never
  learned teaches nothing and frustrates. Proving stays a plain-English
  footnote only: "want a shape proven for every n? `/research prove`".

The three share a `data-step` id per stage; that shared id IS the cross-link.

## Ground it — never draw from memory

The diagram, shapes, and code must be DERIVED, not remembered — a subtly wrong
architecture is worse than none (the user acts on it). Risk scales with how
novel the block is:

- **Standard transformer / attention** — well-known; still label real-vs-example.
- **DeepSeek V3.2 MoE, diffusion-LLM, anything recent** — HIGH risk from memory.
  First get ground truth: `read_file` the user's model code if they have it, or
  `web_fetch` the paper / model card, and build the diagram + shapes + torch
  from THAT. Diffusion-LLM is not autoregressive — its "forward" is a denoising
  loop; don't force it into a transformer figure.
- Put a provenance line at the bottom: `◆ derived` (what came from code/paper)
  vs `◇ illustrative` (example sizes). Be honest about which.

## Build it — rocky artifact rules (important)

`create_artifact` takes BODY content only and STRIPS every `<style>` block, then
applies rocky's light theme. So:

- **No `<style>` block** — it will be deleted. Style with **inline `style=`**
  attributes only (those survive), using rocky's light palette:
  bg is themed for you; use `#efeafa` panels, `#9d7cd8`/`#7c5cba` strokes/accent,
  `#6a4ca3` headings, `#2a2a38` text, `#ede6fb` for the lit/highlight state.
- The cross-highlight is done in a **`<script>`** (scripts survive): on
  `mouseenter`/`focus` of any `[data-step]`, set inline styles on every element
  with the same `data-step`; revert on `mouseleave`/`blur`. No CSS `:hover`.
- Self-contained: no CDN, no external fonts/images. Inline SVG for the diagram.
- Use rocky's classes where they fit: `card`, `tag`/`tag-purple`/`tag-amber`.
- Reuse the SAME artifact title to update in place on a re-run.

## The scaffold

`template.html` in this skill's directory is a complete, working example (scaled
dot-product attention) in exactly this shape. **Copy it and adapt** the three
panels for the block at hand — swap the SVG figure, the ribbon chips, and the
torch lines, keeping the `data-step` ids matched across all three. Don't
re-derive the highlight script; reuse it.

## Multi-panel architectures

For a whole model (MoE: router → experts → combine; a transformer layer:
attention + FFN + residual/norm; diffusion: the denoising steps), give each
sub-block its own diagram+ribbon+torch row, each internally linked, stacked top
to bottom — one story, every stage labeled with the shape it carries.
