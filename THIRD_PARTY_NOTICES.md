# Third-party notices and data provenance

Foldarium's source code is MIT-licensed. Dependencies, hosted assets, model
weights, structures, and datasets retain their own licenses and terms.

## Runtime and scientific software

- Mol* viewer: MIT, loaded from jsDelivr.
- Supabase JavaScript client and Supabase platform: Apache-2.0 components and
  Supabase service terms.
- OpenFold3: Apache-2.0 code; checkpoints and datasets may have separate terms.
- Boltz-2: MIT code; model weights and input databases may have separate terms.
- Gemmi: MPL-2.0.
- NumPy: BSD-3-Clause.
- RDKit: BSD-3-Clause.
- ProLIF: MIT.
- Cursor SDK and Claude CLI: optional external integrations governed by their
  package licenses and service terms.

Consult the exact versions in `pipeline/pyproject.toml` and the upstream
projects before distribution or production use.

## Browser assets

The UI loads Mol* and Geist font files from public CDNs. Geist is distributed
under the SIL Open Font License 1.1.

## Structures and benchmark data

PDB/mmCIF structures originate from the RCSB Protein Data Bank / wwPDB and are
subject to wwPDB usage and attribution policies. CAMEO metadata and prediction
outputs remain subject to the originating service's terms. Runs-n-Poses inputs
come from its published release and retain that release's terms.

Checked-in molecular files are research/demo fixtures. Their inclusion does
not grant rights to redistribute unrelated upstream archives, model weights,
or generated prediction collections. Preserve accession identifiers and cite
the originating databases and methods in scientific use.

## Generated artifacts

`weekly-selector-offline/weekly_selector_wasm_bg.wasm` is a generated build
artifact from the corresponding Rust source package. Rebuild and compare its
digest before publishing a release. Generated datasets should carry their own
manifest with source accessions, tool versions, parameters, and SHA-256
digests.
