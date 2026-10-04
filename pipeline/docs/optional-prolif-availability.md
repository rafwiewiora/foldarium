# Optional ProLIF H-bond evidence

A predicted receptor containing an actual PDB residue named `UNK` has unspecified
residue chemistry. ProLIF's standardizer cannot infer a chemically justified
amino-acid template for it. The assembly previously failed the entire weekly
Preview when this optional metric raised, despite preserved successful prediction
and Smina results. We now preserve every pose and explicitly mark this one metric
unavailable. We neither delete receptor atoms nor substitute an amino acid or zero.

The standardizer's supported-template behavior is documented in the
[ProLIF standardizer source](https://prolif.readthedocs.io/en/stable/_modules/prolif/io/molecule_standardizer.html).
The exception is deliberately limited to observed `UNK` residues in the parsed,
exact predicted receptor. Other parsing, dependency, ligand graph/coordinate,
standardization and fingerprint failures continue to fail the assembly.

The blind marker is:

```json
{
  "metric": "prolif_hbond_residue_count",
  "value": null,
  "policy": "prolif-implicit-hbond-unique-protein-residue/v2",
  "status": "unavailable",
  "availability_policy": "foldarium.prolif-availability/v1",
  "reason": "unsupported_receptor_residue",
  "unsupported_residues": ["UNK"]
}
```

No residue positions, coordinates, execution identifiers, reference information,
free-form errors or arbitrary unsupported-residue values enter this marker.
A genuine measured zero remains numeric zero. An unqualified null is invalid.
The full private scorer result retains its existing provenance. Normal numeric
results retain their previous schema and semantics.

## Consumers and population

- `interactions.py` validates ligand topology and coordinates before recognizing
  `UNK` in the parsed receptor, then emits only the versioned unavailability marker.
- Execution adapters must preserve successful Smina scoring and the complete
  interaction summary in the existing pose-score result. Every scoring worker
  must use the updated pipeline module.
- `weekly_quiz.py` checks the remote scorer identity, successful status and finite
  Smina score before projecting the strict optional-metric contract.
- `quiz.py` validates the same contract and retains every item, choice and pose ID.
  Sampling and featured question selection do not use this metric or exclude it.
- Supabase's service-only `open_weekly_quiz_round` accepts only the exact marker
  through migration `20261004240000`. Existing answer, window, identity and
  environment guards remain in place. Existing stored manifests are unchanged.
- Selector kits retain the unchanged complete blind manifest. The marker is
  evidence availability, not a correctness or benchmark-denominator adjustment.
- `lib/weekly-retrospectives.js` projects this marker with the shared strict
  `lib/interaction-metric.js` validator, including archived numeric legacy metrics.
- `app.js` shows `H-bonds unavailable`; neither display path coerces null to zero.
  Answer scoring, RMSD, scientific reference dispositions, benchmark inclusion and
  human/LLM denominators remain unchanged.

## Rollout and checks

Apply the additive database migration and update both scoring workers and the main
pipeline before authorizing a failed Preview assembly to retry. Observe existing
outbox leases rather than starting overlapping manual attempts. The already
successful prediction outputs are reused; no prediction inference is needed.

Python tests exercise an actual RDKit-parsed `UNK` receptor, malformed ligand
rejection, successful Smina preservation, full pose identity preservation and
strict marker validation. JavaScript tests cover the archive projector and both
UI evidence labels, including genuine zero and invalid-null cases. The isolated
PostgreSQL harness checks service-only round opening, exact marker acceptance,
invalid markers and retained answer/window guards:

```sh
node pipeline/tests/check_interaction_availability_database.mjs /path/to/pglite/dist/index.js
```
