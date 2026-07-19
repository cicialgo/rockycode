---
name: lean-prover
description: Formalize and machine-verify mathematics AND neural networks in Lean 4 — Mathlib proofs for the math, TorchLean for typed models and robustness certificates, honest green/amber/red verdicts, rendered as an artifact
---

# lean-prover — machine-verified mathematics

The Lean compiler is ground truth. A theorem that compiles with zero `sorry`
is true — no judge, no vibes, no trust required. The corollary binds you:
**never say "proved" without a green compile you actually ran.** This is the
one domain where your output is machine-checkable; act like it.

## Two layers — say which one every claim lives in

- **Math layer (Mathlib)** — properties of the mathematics itself, exact reals,
  all sizes at once: "softmax lands in the probability simplex, for every n."
  Default layer; the pipeline below.
- **Model layer (TorchLean)** — properties of a concrete network in Lean's
  typed tensor framework: shape guarantees by compilation, semantics lemmas,
  robustness certificates over float32. Use when the user brings actual model
  code or asks about a specific network. See "Model layer" below.

A claim proved about real-number math is not a claim about a float32 kernel,
and vice versa. Never let the two blur in a report.

## Verdicts — use exactly this vocabulary

- **green** — compiles; zero `sorry`/`admit`; no new `axiom`. Machine-verified.
- **amber** — the statement typechecks but some goals remain as explicit
  `sorry`. Honest partial credit: a correctly formalized statement is real
  value on its own. Always say how many `sorry` and which goals.
- **red** — does not compile. Say so plainly, with the first error.

Before claiming green, grep your own file: no `sorry`, no `admit`, no `axiom`
declarations. An axiom "proof" proves nothing — that is cheating, not amber.

## Preflight — is Lean installed? Never assume it is

Before formalizing anything, confirm Lean itself is present. In bash:
`command -v lake elan || ls ~/.elan/bin/lake 2>/dev/null` (elan/lake usually
live in `~/.elan/bin`; prepend it to PATH in your bash calls if needed).

If Lean is **not found**, STOP and hand the user the choice — never silently
install a toolchain, and never fabricate a verdict for code you couldn't run:

- **Install it** — `curl https://elan.lean-lang.org/elan-init.sh -sSf | sh`
  installs elan + the Lean toolchain; then set up a workspace (below). Guided
  path: https://leanprover-community.github.io/get_started.html . Best if they
  want to keep proving locally.
- **Verify in the browser (no install)** — rocky writes the formalization; the
  user pastes it into https://live.lean-lang.org and reads the goal state /
  green there. rocky reports the source as **unverified** and asks them for the
  compile result before ever calling it green.
- **Formalize only** — rocky produces the Lean statement + proof sketch as an
  artifact, badged **unverified — Lean not installed, not compiled**. Useful to
  review the formalization; it is NOT machine-checked, so it can never be green
  or amber (both mean a compile ran) — it is its own "unverified" verdict.

Only once Lean is confirmed present do you enter the workspace + pipeline below.

## Workspace

Proving needs a Lake project with Mathlib **cache-built**. Find one, in order:

1. a `lakefile.toml` / `lakefile.lean` in the current folder (or one level down)
   whose `lake-manifest.json` lists mathlib
2. `$ROCKYCODE_LEAN_WS`
3. a `lean_probe/` Lake project in the current directory

If none exists, stop and offer setup — do not silently install:
`lake new <name> math && cd <name> && lake exe cache get`
(warn: the Mathlib cache is a ~5 GB download, but without it the build takes
hours, not minutes).

## Pipeline

1. **Formalize.** Write ONE self-contained file at `<workspace>/Rocky/<Slug>.lean`
   (create the folder if needed). Restate the user's informal claim as a comment
   at the top, then the formal statement. Name theorems descriptively. If the
   informal statement is ambiguous, ask before formalizing — a proof of the
   wrong statement is worse than no proof.
2. **Compile.** `lake env lean Rocky/<Slug>.lean` with bash, cwd = workspace.
   Compiles run ~10s on a warm cache; a `sorry` produces a *warning*, so check
   the text, not just the exit code.
3. **Repair, up to 3 rounds.** Read the errors, fix, recompile. Fix the first
   error first — later ones are often cascade.
4. **Escalate, up to 3 more rounds.** If unknown-identifier errors persist,
   STOP guessing names and switch to searching (see name-grounding below).
5. **Amber fallback.** Past ~6 compile rounds without green: keep every goal
   you closed, replace the stuck ones with `sorry`, and get *that* file to
   compile — the statement itself is then certified well-formed. Report amber.
   Do not grind past the budget; do not delete the file to hide the attempt.
6. **Report.** Verdict first, then the exact compile command you ran, the
   theorem names, and (if amber) what remains. Render the artifact.

## Name-grounding — the known failure mode is hallucinated identifiers

Measured on this exact pipeline: proof *architecture* is nearly always right;
what fails is invented Mathlib names. Rules:

- `import Mathlib` — bare, nothing else. NEVER `import Mathlib.Some.Module`:
  guessed module names kill the whole file at the header (the module split
  changes between versions; the bare import always works on a cached build).
- On `unknown identifier`/`unknown constant`: do NOT retry a similar guess.
  Search instead —
  - put `exact?` (or `apply?`, `rw?`) at the stuck goal and compile: the
    output's "Try this:" line is a *verified* lemma name;
  - batch-check candidates cheaply in a scratch file: `#check @Filter.Tendsto`
    lines, one compile validates them all;
  - `open` the relevant namespace and retry `exact?` — suggestions improve.
- Prefer big hammer tactics you know exist (`simp`, `norm_num`, `nlinarith`,
  `positivity`, `field_simp`, `ring`, `omega`, `fun_prop`, `measurability`)
  before hunting for the perfectly named lemma.

## Model layer (TorchLean)

TorchLean (github.com/lean-dojo/TorchLean) formalizes neural networks in
Lean 4 — PyTorch-shaped API, shape-indexed tensors, IBP/CROWN certificates.
It is **newer than your pretraining: you have ZERO latent knowledge of it.**
Working rules, validated by probe (3/3 machine-verified this way):

- FIRST read `torchlean-api.md` in this skill's directory — it is the only
  TorchLean API you may use. Do not invent names beyond it; when stuck, read
  the repo's own `NN/Examples/` files instead of guessing.
- `import NN.API` then `open TorchLean` — the missing `open` is the #1 error.
- Workspace: a built TorchLean checkout. Look for `$ROCKYCODE_TORCHLEAN_WS`,
  then a `torchlean_probe/` checkout in the current directory. If none exists, offer setup
  (clone + `lake exe cache get && lake build`; warn: multi-GB, ~30 min) —
  never silently install.
- Same compile loop, budgets, and verdicts as the math layer. For pure
  shape/wiring claims, the file compiling IS the theorem — say so in the
  report rather than inventing a redundant proposition.
- Translating user PyTorch code: restate the model in TorchLean's API
  (layer list ↔ `nn.Sequential!`, input shape ↔ `Tensor.T … (shape![…])`),
  and show the correspondence side by side. Semantic claims about training
  or CUDA runtime behavior are runtime evidence, not Lean proof evidence —
  TorchLean's own trust boundaries say so; repeat that honestly.

## Artifact

Render the result with `create_artifact`: the informal statement, the Lean
source in a code block, and the verdict up top (green/amber/red — amber lists
the remaining goals). One artifact per statement, updated in place on re-runs.

When the work spans both layers (user code + math + model), render the
**triptych**: the user's original code, the math-layer theorem(s), and the
model-layer result, each with its own verdict badge — three panels, one story,
every claim labeled with the layer it lives in.

## Honesty rules

- The report's verdict comes from the last compile you ran, nothing else.
- Quote compiler output when it disagrees with your expectation.
- If the user's claim is false, say so — a disproof or a counterexample
  (`decide`, `norm_num`, or an explicit witness) is a fully valid green result.
