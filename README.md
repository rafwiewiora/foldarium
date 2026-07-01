# Foldarium — the co-folding pose-triage quiz

A little game: you're shown a protein **binding pocket** with the ligand removed, plus a handful of
**anonymised predicted poses** from co-folding models (AlphaFold3 and, for the Runs-n-Poses set, a pool
of methods). Your job — **pick the pose that actually binds**, or, in Hard mode, decide that **none of
them are right**.

▶ **Play: https://rafwiewiora.github.io/foldarium/**

## Two quizzes
- **CAMEO** — AlphaFold3 poses from the weekly prospective co-folding benchmark.
- **Runs-n-Poses** — poses pooled from multiple co-folding methods (the method is never shown).

## Two difficulties
- **Easy** — every ensemble has a correct pose; find it.
- **Hard** — ensembles are mixed: some have a correct pose, some have **none** (answer "none of these"),
  and some are all-correct. You classify each. The AI baseline can never say "none", so the no-correct
  ensembles are where a human can beat it.

This static demo contains only **novel** targets (dissimilar to the models' training data) — the regime
where automated pose-picking has real headroom. Scores are kept locally in your browser.

*Single-pocket ensembles only. Poses are clustered; carbon colours are non-semantic (identification only).*
