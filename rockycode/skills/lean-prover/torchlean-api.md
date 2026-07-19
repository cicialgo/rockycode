# TorchLean API excerpt — the ONLY TorchLean names you may use

TorchLean (github.com/lean-dojo/TorchLean, arXiv 2602.22631) formalizes neural
networks in Lean 4. It is NEWER than your pretraining data: you have zero latent
knowledge of it. Everything below is verbatim from the repo and was validated by
compile on 2026-07-15 (probe: 3/3 machine-verified). Treat this file as the whole
API surface — if a name is not here and not suggested by a compiler error, do not
use it. Read the repo's own files (NN/Examples/) when you need more.

## Imports and namespace

```lean
import NN.API        -- the public API (works from a plain, non-module file)
import NN.Proofs     -- optional: proof helpers
import Mathlib       -- optional: Mathlib is a TorchLean dependency, fully available

open TorchLean       -- REQUIRED before nn./tensor!/shape! names resolve
```

Missing `open TorchLean` is the #1 observed error (unknown identifier `nn.Linear`).

## Tensors (validated by compile)

```lean
-- element type = dtype (Float, ℚ, Int, ℝ)
def v := Tensor.vector (α := Float) [0.1, 0.2, 0.3, 0.4]

-- typed vector, shape in the type
def twoVector : Tensor.T Float (shape![2]) := tensor! [1.0, 2.0]

-- N-D from nested lists (row-major); explicit: Tensor.ofList [2,2,2] [1,...,8]
def x3 : Tensor.T Float (Shape.ofDims [2, 2, 2]) :=
  tensor! [ [ [1, 2], [3, 4] ], [ [5, 6], [7, 8] ] ]

-- flat constructor with dims
def xs : Tensor.T Float (shape![4, 2]) :=
  tensorOfList! [4, 2] [0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0]
```

ℝ-valued tensors are fine for proofs but are noncomputable — do not `Tensor.print` them.

## Models (validated by compile)

```lean
def model :=
  nn.Sequential![
    nn.Linear 2 8,
    nn.ReLU,
    nn.Linear 8 1
  ]
```

Shape-indexed types mean a shape-mismatched literal or layer stack fails to
compile — for pure wiring claims, the file compiling IS the theorem.

## Semantics lemmas (validated by compile)

```lean
theorem shape_roundtrip :
    Shape.ofDims (Shape.toList (shape![2, 3])) = shape![2, 3] := by simp

-- TorchLean.Semantics.relu is real-valued; unfolds to max-style form
theorem relu_eq_self_of_nonnegative (x : ℝ) (hx : 0 ≤ x) :
    TorchLean.Semantics.relu x = x := by
  unfold TorchLean.Semantics.relu
  exact max_eq_left hx
```

Deeper proof libraries: `NN.Proofs.*`, `NN.Verification.*`, `NN.MLTheory.*`.

## From the README — NOT compile-validated here, verify before relying on it

```lean
def data : Trainer.Dataset (.dim 2 .scalar) (.dim 1 .scalar) := Data.tensorDataset xs ys
-- Trainer.new model { task := .regression, optimizer := optim.sgd { lr := 0.05 }, ... }
```

CLI verification workflows: `lake exe verify --help`, `lake exe verify -- torchlean-ibp`
(IBP/CROWN robustness certificates). Examples live in NN/Examples/Verification/.

## Trust boundaries (from the repo's TRUST_BOUNDARIES.md)

TorchLean's executable float32 path and its ℝ semantics are different layers —
say which one a claim is about. CUDA/native-runtime results are runtime evidence,
not Lean proof evidence; the repo itself makes this distinction. So must you.
