---
name: lean-prover
description: Formalize and machine-verify mathematics AND neural networks in Lean 4 — Mathlib proofs for the math, TorchLean for typed models and robustness certificates, honest compiled / kernel-checked / paranoid verdicts, rendered as an artifact
---

# lean-prover — machine-verified mathematics

A clean `lake env lean` is evidence that the elaborator accepted the file.
It is not, by itself, a kernel recheck. Lean v4.35 ships `lake check`: the
project's default targets are exported and replayed through `leanchecker`
inside a bubblewrap sandbox, and any axiom outside `propext`, `Quot.sound`,
and `Classical.choice` fails the run. `lake check --paranoid` also runs the
bundled kernels (`leanchecker-paranoid`, `lean4lean`, `nanoda`, `con-leche`,
`con-ron`). Compile-clean proofs have been unsound before; do not treat the
compiler as the whole checker when `lake check` exists.

**never say "proved" without a kernel check you actually ran, when this
toolchain has `lake check`.** A sorry-free compile is **green · compiled**.
It becomes proved only at **green · kernel-checked** or **green · paranoid**,
for a file that was actually on the checked targets, with the sandbox left
on. The same labels are implemented in `rockycode/engine/lean_verify.py`
(`detect_toolchain`, `plan_exit_check`, `grade_proof`). Follow them when you
drive `lake` yourself.

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

- **red** — `lake env lean` failed, or the file declares a new `axiom`, or
  `lake check` rejected a non-permitted axiom / failed its sandboxed build.
  Say so plainly, with the first error. Axiom and build failures go back
  into the repair budget. A new `axiom` is cheating, not amber.
- **amber** — the statement typechecks but some goals remain as explicit
  `sorry` or `admit`. Honest partial credit: a correctly formalized statement
  is real value on its own. Always say how many `sorry` and which goals.
- **green · compiled** — `lake env lean` passed, text audit is clean, and a
  kernel recheck did **not** accept this file. Say why: toolchain has no
  `lake check`, the sandbox could not start, the check was not run, or this
  file is not a default target. Do not say proved.
- **green · kernel-checked** — sandboxed `lake check` passed, and this file
  is on a default target that the check replayed. This is the lowest proved
  result when `lake check` exists.
- **green · paranoid** — `lake check --paranoid` passed the same way (every
  bundled kernel). Use it for a final acceptance or when the user asks, not
  on every repair round.
- **statement-locked** — optional overlay from `lake comparator`: the solution
  proves the statement the user agreed to. It does not replace the kernel level.
- **disputed** — the compiler accepted the file and a kernel rejected it, or
  bundled kernels disagree. **Not green.** Stop. Do not repair.
- **unverified** — nothing was compiled (Lean missing, or browser-only).
  Never green, never amber.

Before any green, grep your own file: no `sorry`, no `admit`, no `axiom`
declarations. That grep is not the kernel check. Metaprogramming and a custom
prelude can look clean and still be rejected by `lake check`; when that
happens the verdict is disputed or red, not a prompt to route around the kernel.

## Preflight — is Lean installed? Never assume it is

Before formalizing anything, confirm Lean itself is present. In bash:
`command -v lake elan || ls ~/.elan/bin/lake 2>/dev/null` (elan/lake usually
live in `~/.elan/bin`; prepend it to PATH in your bash calls if needed).

Then record the toolchain, without upgrading it:

- Read `<workspace>/lean-toolchain` (one line, for the report).
- Probe the command, not the version number: `lake check --help`. If that
  help lists `lake check` and mentions an external checker, the exit path
  below is required. `--paranoid` is available when the same help shows it.
  A toolchain older than v4.35, or a `lake` with no `check` subcommand, stays
  at green · compiled — say "not kernel-rechecked".
- Sandbox: `lake check` starts only on Linux with `bwrap` (bubblewrap) on
  PATH. macOS, and Linux without bubblewrap, cannot start it. **Do not pass
  `--inadvisably-no-sandbox` to make it run.** That flag disables the sandbox
  Lake uses because the project is untrusted; it needs a fresh confirmation
  even after `lake` was allowed for the session, and a run that used it is
  labeled unsandboxed and is not proved. Offer instead: recheck on a Linux
  machine or container that has bubblewrap (`lake check --from-export` if the
  export was built elsewhere), or report green · compiled.

If Lean is **not found**, STOP and hand the user the choice — never silently
install a toolchain, and never fabricate a verdict for code you couldn't run:

- **Install it** — `curl https://elan.lean-lang.org/elan-init.sh -sSf | sh`
  installs elan + the Lean toolchain; then set up a workspace (below). Guided
  path: https://leanprover-community.github.io/get_started.html . Best if they
  want to keep proving locally. Prefer a toolchain that ships `lake check`
  (Lean v4.35 or newer) when they are creating the workspace; do not silently
  upgrade one they already have.
- **Verify in the browser (no install)** — rocky writes the formalization; the
  user pastes it into https://live.lean-lang.org and reads the goal state
  there. rocky reports the source as **unverified** and asks them for the
  result before ever calling it green.
- **Formalize only** — rocky produces the Lean statement + proof sketch as an
  artifact, badged **unverified — Lean not installed, not compiled**. Useful to
  review the formalization; it is NOT machine-checked, so it can never be green
  or amber.

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

`lake check` replays **default targets**, not whichever file `lake env lean`
just compiled. In a workspace rocky created, add a library the checker will
see and do not edit a lakefile the user owns without asking:

```toml
[[lean_lib]]
name = "Rocky"
```

Put proofs at `Rocky/<Slug>.lean`. The project needs a `lake-manifest.json`
before `lake check` (a normal `lake build` writes one); the sandbox cannot
write the project directory. If the file is not on a default target, say so
and stay at green · compiled even when `lake check` passes for other targets.

## Pipeline

1. **Formalize.** Write ONE self-contained file at `<workspace>/Rocky/<Slug>.lean`
   (create the folder if needed). Restate the user's informal claim as a comment
   at the top, then the formal statement. Name theorems descriptively. If the
   informal statement is ambiguous, ask before formalizing — a proof of the
   wrong statement is worse than no proof.
2. **Compile.** `lake env lean Rocky/<Slug>.lean` with bash, cwd = workspace.
   Compiles run ~10s on a warm cache; a `sorry` produces a *warning*, so check
   the text, not just the exit code.
3. **Repair, up to 3 rounds, on the compiler only.** Read the errors, fix,
   recompile. Fix the first error first — later ones are often cascade. Do
   not run `lake check` inside this loop.
4. **Escalate, up to 3 more rounds.** If unknown-identifier errors persist,
   STOP guessing names and switch to searching (see name-grounding below).
5. **Amber fallback.** Past ~6 compile rounds without a clean compile: keep
   every goal you closed, replace the stuck ones with `sorry`, and get *that*
   file to compile — the statement itself is then known to elaborate. Report
   amber. Do not grind past the budget; do not delete the file to hide the
   attempt.
6. **Kernel check, once the file is sorry-free.** If preflight found
   `lake check` and the sandbox can start, run `lake check` (add `--paranoid`
   only for a final acceptance or an explicit request). Then:
   - exit 0, and this file is on a default target → **green · kernel-checked**
     (or **green · paranoid**). Quote the command.
   - `Axiom '…' is not permitted` or a sandboxed build failure → not green.
     Feed the log back as one more repair, inside the same budget, then
     check again after the next clean compile.
   - a kernel prints `kernel rejected`, or you cannot tell why exit code 1
     failed → **disputed**. Stop. Write the full log to
     `Rocky/<Slug>.check.log` next to the Lean file and leave both in place.
     Do not rewrite the proof, add `sorry` / `axiom`, or change the
     toolchain to get a green. Tell the user a kernel rejected a file the
     compiler accepted; a disagreement can be reported to contact@lean-fro.org.
   - exit 2, or the sandbox / manifest / default-target start errors → the
     check did not run. Stay at **green · compiled**, quote the error, and
     do not add `--inadvisably-no-sandbox`.
   - `--paranoid` times out or runs out of resources after a plain
     `lake check` already passed → keep **green · kernel-checked** and say
     paranoid did not finish. That is not red and not disputed.
7. **Report.** Verdict label first, then the toolchain line
   (`lean-toolchain`, whether `lake check` exists, whether the sandbox was
   `linux-bwrap` or unavailable), the exact commands you ran, the theorem
   names, and (if amber) what remains. Render the artifact.

Optional statement lock, when the user wants the proved statement pinned:
write the agreed statement as `Rocky/<Slug>/Challenge.lean` with `sorry`
bodies, the proof as `Solution.lean`, and a `comparator.json` whose
`permitted_axioms` are exactly `propext`, `Quot.sound`, `Classical.choice`.
Do not edit Challenge during repair. `lake comparator` is a final check, not
a repair-loop command. If it fails, do not say the user's claim was proved.

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
Working rules, validated by probe (3/3 compile-clean this way, on Lean 4.32 —
**green · compiled**, not kernel-checked; that toolchain has no `lake check`):

- FIRST read `torchlean-api.md` in this skill's directory — it is the only
  TorchLean API you may use. Do not invent names beyond it; when stuck, read
  the repo's own `NN/Examples/` files instead of guessing.
- `import NN.API` then `open TorchLean` — the missing `open` is the #1 error.
- Workspace: a built TorchLean checkout. Look for `$ROCKYCODE_TORCHLEAN_WS`,
  then a `torchlean_probe/` checkout in the current directory. If none exists, offer setup
  (clone + `lake exe cache get && lake build`; warn: multi-GB, ~30 min) —
  never silently install.
- Same compile loop and budgets as the math layer. Verdicts follow the same
  ladder: if that checkout's `lake check --help` has no `check` command, the
  ceiling is **green · compiled**, and the report says so. For pure
  shape/wiring claims, the file compiling IS the theorem — say so in the
  report rather than inventing a redundant proposition.
- Translating user PyTorch code: restate the model in TorchLean's API
  (layer list ↔ `nn.Sequential!`, input shape ↔ `Tensor.T … (shape![…])`),
  and show the correspondence side by side. Semantic claims about training
  or CUDA runtime behavior are runtime evidence, not Lean proof evidence —
  TorchLean's own trust boundaries say so; repeat that honestly.

## Artifact

Render the result with `create_artifact`: the informal statement, the Lean
source in a code block, and the verdict up top (the label from the vocabulary
above — amber lists the remaining goals, disputed says a kernel rejected the
file). One artifact per statement, updated in place on re-runs.

When the work spans both layers (user code + math + model), render the
**triptych**: the user's original code, the math-layer theorem(s), and the
model-layer result, each with its own verdict badge — three panels, one story,
every claim labeled with the layer it lives in.

## Honesty rules

- The report's verdict comes from the commands you ran. A compile does not
  stand in for `lake check` when that command exists.
- Quote compiler and checker output when it disagrees with your expectation.
- If the user's claim is false, say so — a disproof or a counterexample
  (`decide`, `norm_num`, or an explicit witness) is a fully valid result at
  whatever check level you actually reached.
- On disputed, stop and preserve evidence. Do not keep sampling proofs until
  a kernel happens to accept one.
